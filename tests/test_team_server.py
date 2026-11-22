"""Team-scoped HTTP routing and persistence; no live model, Docker or Git."""
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock

from backend.config import Config
from backend.engine import Platform
from backend.provider import ProviderError
from backend.server import TeamHub, make_handler
from backend.teams import get_team_agents
from backend.tools import ToolGateway


class NoExecutionGateway(ToolGateway):
    def capabilities(self):
        return {'dockerAvailable': False, 'dockerImageReady': False, 'gitInstalled': False}

    def cancel(self, run_id):
        pass


class LocalReply:
    def __init__(self, team):
        self.team = team

    def complete(self, messages):
        return {'content': 'Local test response for ' + self.team, 'usage': {'total_tokens': 1}}


class TeamServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name), {'LLM_API_KEY': 'server-test-private-key'})
        (self.config.root / 'index.html').write_text('<h1>Test UI</h1>')
        (self.config.root / '.env').write_text('LLM_API_KEY=never-serve-this-file\n')
        self.created = []

        def factory(config, agents, team_id):
            app = Platform(config, provider=LocalReply(team_id), agents=agents, team_id=team_id,
                           tools=NoExecutionGateway(config, agents=agents))
            self.created.append((team_id, config, app))
            return app
        self.hub = TeamHub(self.config, platform_factory=factory)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.hub))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.hub.shutdown()
        self.temp.cleanup()

    def request(self, path, payload=None, **headers):
        if payload is None:
            request = urllib.request.Request(self.base + path, headers=headers)
        else:
            request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json', 'X-EvoMAC-Client': '1', **headers})
        with urllib.request.urlopen(request, timeout=5) as response:
            content = response.read()
            return json.loads(content) if response.headers.get_content_type() == 'application/json' else content.decode()

    def send_chat(self, team, text):
        members = list(get_team_agents(team))
        self.request('/api/action', {'action': 'send', 'team': team, 'channelId': 'group',
                     'mode': 'chat', 'participants': members, 'text': text})
        self.assertTrue(self.hub.get(team).wait_for_idle(3))
        return self.request('/api/state?team=' + team)['sessions']['group']['run']

    def test_team_inventory_is_lazy_and_each_team_has_real_roles(self):
        inventory = self.request('/api/teams')
        self.assertEqual({item['id'] for item in inventory['teams']}, {'default', 'software', 'newsroom', 'fund', 'exam'})
        self.assertEqual(inventory['defaultTeamId'], 'default')
        self.assertEqual(self.created, [])
        for team in ('software', 'newsroom', 'fund', 'exam'):
            state = self.request('/api/state?team=' + team)
            self.assertEqual(state['teamId'], team)
            self.assertEqual(set(state['sessions']), {'group', *get_team_agents(team)})
            self.assertEqual({item['id'] for item in state['agents']}, set(get_team_agents(team)))
            self.assertTrue(all(item.get('rolePrompt') for item in state['agents']))
            self.assertTrue(all(isinstance(tool['parameters'], dict) for item in state['agents'] for tool in item['tools']))
        self.assertNotIn('default', self.hub.platforms)

    def test_same_team_two_chats_keep_context_and_history_without_cross_team_data(self):
        first = self.send_chat('software', 'FIRST_TEAM_ONLY_MARKER')
        second = self.send_chat('software', 'Extend the previous work.')
        state = self.request('/api/state?team=software')
        messages = state['sessions']['group']['messages']
        self.assertTrue(any(message['text'] == 'FIRST_TEAM_ONLY_MARKER' for message in messages))
        self.assertTrue(any(message['text'] == 'Extend the previous work.' for message in messages))
        self.assertNotEqual(first['id'], second['id'])
        history = self.request('/api/runs?team=software')
        self.assertEqual({item['id'] for item in history['runs']}, {first['id'], second['id']})
        detail = self.request('/api/runs/' + first['id'] + '?team=software')
        self.assertEqual(detail['id'], first['id'])
        self.assertTrue(any(message.get('text') == 'FIRST_TEAM_ONLY_MARKER' for message in detail['conversation']))
        for team in ('default', 'newsroom', 'fund'):
            clean = self.request('/api/state?team=' + team)
            self.assertEqual(clean['sessions']['group']['messages'], [])
            self.assertNotIn('FIRST_TEAM_ONLY_MARKER', json.dumps(clean))
            self.assertEqual(self.request('/api/runs?team=' + team)['runs'], [])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request('/api/runs/' + first['id'] + '?team=' + team)
            self.assertEqual(caught.exception.code, 404)

    def test_team_config_is_copied_and_single_initialization_is_atomic(self):
        original = self.config.data_dir
        env_contents = (self.config.root / '.env').read_bytes()
        with ThreadPoolExecutor(max_workers=6) as pool:
            apps = list(pool.map(lambda _: self.hub.get('software'), range(6)))
        self.assertTrue(all(app is apps[0] for app in apps))
        self.assertEqual(len([row for row in self.created if row[0] == 'software']), 1)
        self.assertIsNot(apps[0].config, self.config)
        self.assertEqual(apps[0].config.data_dir, original / 'teams' / 'software')
        self.assertEqual(self.hub.get().config.data_dir, original)
        self.assertEqual(self.config.data_dir, original)
        self.assertEqual((self.config.root / '.env').read_bytes(), env_contents)

    def test_invalid_teams_and_cross_origin_requests_are_rejected_without_side_effects(self):
        for path in ('/api/state?team=../outside', '/api/state?team=', '/api/state?team=software&team=fund'):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path)
            self.assertIn(caught.exception.code, (400, 404))
        for team in ('missing', None, [], '../outside'):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request('/api/action', {'action': 'reset', 'team': team})
            self.assertIn(caught.exception.code, (400, 404))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/action', {'action': 'reset', 'team': 'software'}, Origin='https://example.com')
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.created, [])

    def test_static_allowlist_and_scrubbing_apply_to_team_endpoints(self):
        for path in ('/.env', '/%2eenv', '/backend/teams.py', '/.runtime/teams/software/platform.sqlite', '/js/../.env'):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path)
            self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.request('/'), '<h1>Test UI</h1>')
        state = self.request('/api/state?team=software')
        self.assertNotIn(self.config.api_key, json.dumps(state))
        self.assertNotIn('never-serve-this-file', json.dumps(state))

    def test_make_handler_accepts_legacy_platform_and_default_data_directory(self):
        legacy = self.hub.get()
        old_server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(legacy))
        worker = threading.Thread(target=old_server.serve_forever, daemon=True)
        worker.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{old_server.server_port}/api/state') as response:
                state = json.load(response)
            self.assertEqual(state['teamId'], 'default')
            self.assertEqual(set(state['sessions']), {'group', *get_team_agents('default')})
            self.assertEqual(legacy.config.data_dir, self.config.data_dir)
        finally:
            old_server.shutdown()
            old_server.server_close()
            worker.join(2)

    def test_shutdown_keeps_database_open_until_provider_workers_are_idle(self):
        busy = Mock()
        busy.config.model_timeout = 17
        busy.wait_for_idle.return_value = False
        hub = TeamHub(self.config, default_platform=busy)
        hub.shutdown()
        busy.wait_for_idle.assert_called_once_with(19)
        busy.repo.close.assert_not_called()
        with self.assertRaises(ValueError):
            hub.get()
        idle = Mock()
        idle.config.model_timeout = 1
        idle.wait_for_idle.return_value = True
        other = TeamHub(self.config, default_platform=idle)
        other.shutdown()
        idle.wait_for_idle.assert_called_once_with(5)
        idle.repo.close.assert_called_once()

    def test_failed_provider_evidence_survives_http_history_without_failed_replay(self):
        app = self.hub.get('software')
        app.config.max_retries = 0
        calls = []
        class FailedProvider:
            def complete(inner, messages):
                calls.append(messages)
                raise ProviderError('Synthetic timeout ' + app.config.api_key, 'network', True)
        app.provider = FailedProvider()
        run = self.send_chat('software', 'Show the real failure.')
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['calls'][0]['error'], {'code': 'network', 'retryable': True})
        self.assertNotIn(app.config.api_key, json.dumps(run))
        detail = self.request('/api/runs/' + run['id'] + '?team=software')
        self.assertEqual(detail['calls'][0]['error'], run['calls'][0]['error'])
        self.assertIn('Synthetic timeout', detail['calls'][0]['result'])
        self.assertIn('[REDACTED]', detail['calls'][0]['result'])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/action', {'action': 'recover', 'team': 'software', 'channelId': 'group', 'runId': run['id']})
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(len(calls), 1, 'Failed recovery requests must not replay model or tool work.')

    def test_http_preserves_task_output_contract_and_format_validation_separately(self):
        run = self.send_chat('software', 'A local fixture for metadata transport.')
        app = self.hub.get('software')
        contract = {'kind': 'json', 'requiredKeys': ['title']}
        validation = {'status': 'failed', 'issues': ['Missing required key: title']}
        with app.lock:
            task = app.state['sessions']['group']['run']['steps'][0]
            task.update(outputContract=contract, outputValidation=validation)
            app._persist()
        state = self.request('/api/state?team=software')
        public_task = state['sessions']['group']['run']['steps'][0]
        self.assertEqual(public_task['outputContract'], contract)
        self.assertEqual(public_task['outputValidation'], validation)
        detail = self.request('/api/runs/' + run['id'] + '?team=software')
        self.assertEqual(detail['steps'][0]['outputValidation'], validation)
        self.assertNotEqual(detail['steps'][0].get('validation'), validation,
                            'Format validation must not replace independent semantic/test review.')


if __name__ == '__main__':
    unittest.main()
