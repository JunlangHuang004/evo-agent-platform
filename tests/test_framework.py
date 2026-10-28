"""Small regression suite. No external model calls or dependencies."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from backend.config import Config
from backend.engine import Platform, ActionError
from backend.provider import ProviderError
from backend.server import make_handler


class Reply:
    def complete(self, messages):
        return {'content': '真实传输接口的本地测试回复', 'usage': {'total_tokens': 9}}


class Failure:
    def complete(self, messages):
        raise ProviderError('HTTP 401: unavailable')


class SlowReply:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, messages):
        self.entered.set()
        self.release.wait(3)
        return {'content': '迟到的回复'}


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name), {'LLM_API_KEY': 'private-test-key'})
        self.app = Platform(self.config, Reply())

    def tearDown(self):
        self.app.wait_for_idle(4)
        self.app.repo.db.close()
        self.temp.cleanup()

    def send(self, **overrides):
        return self.app.action({'action': 'send', 'channelId': 'coordinator', 'text': '测试', 'mode': 'chat', **overrides})

    def test_success_has_one_actual_actor_without_fake_tools_or_memories(self):
        self.send(channelId='group', participants=['research', 'reviewer'])
        self.assertTrue(self.app.wait_for_idle())
        state = self.app.snapshot()
        run = state['sessions']['group']['run']
        self.assertEqual(run['status'], 'done')
        self.assertEqual(run['respondingAgentId'], 'research')
        self.assertEqual([item['tool'] for item in run['calls']], ['llm.chat'])
        self.assertFalse(run['memoryHits'])
        self.assertTrue(state['connection']['features']['multiAgent'])
        self.assertEqual(state['sessions']['reviewer']['messages'], [])
        self.assertNotIn(self.config.api_key, json.dumps(state))

    def test_provider_failure_is_failed(self):
        self.app.provider = Failure()
        self.send()
        self.assertTrue(self.app.wait_for_idle())
        state = self.app.snapshot()['sessions']['coordinator']
        self.assertEqual(state['run']['status'], 'failed')
        self.assertEqual(state['run']['calls'][0]['status'], 'failed')
        self.assertFalse(any(item['role'] == 'agent' for item in state['messages']))

    def test_reset_discards_late_response(self):
        slow = SlowReply()
        self.app.provider = slow
        self.send()
        self.assertTrue(slow.entered.wait(1))
        self.app.action({'action': 'reset', 'channelId': 'coordinator'})
        slow.release.set()
        self.assertTrue(self.app.wait_for_idle())
        session = self.app.snapshot()['sessions']['coordinator']
        self.assertIsNone(session['run'])
        self.assertEqual(session['messages'], [])

    def test_completed_chat_survives_restart(self):
        self.send()
        self.assertTrue(self.app.wait_for_idle())
        other = Platform(self.config, Reply())
        try:
            self.assertEqual(other.snapshot()['sessions']['coordinator']['run']['status'], 'done')
            self.assertEqual(len(other.snapshot()['sessions']['coordinator']['messages']), 2)
        finally:
            other.repo.db.close()

    def test_unknown_actions_and_invalid_participants_are_rejected(self):
        with self.assertRaises(ActionError):
            self.send(channelId='group', participants=[])
        with self.assertRaises(ActionError) as caught:
            self.app.action({'action': 'pause', 'channelId': 'group'})
        self.assertEqual(caught.exception.status, 409)

    def test_http_exposes_only_public_files_and_rejects_cross_origin_post(self):
        (self.config.root / 'index.html').write_text('<h1>Test</h1>')
        (self.config.root / '.env').write_text('LLM_API_KEY=do-not-serve')
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            for path in ('/.env', '/%2eenv', '/backend/config.py', '/.runtime/platform.sqlite', '/js/../.env'):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + path)
                self.assertEqual(caught.exception.code, 404)
            with urllib.request.urlopen(base + '/api/state') as response:
                self.assertNotIn(self.config.api_key, response.read().decode())
            request = urllib.request.Request(base + '/api/action', data=b'{"action":"reset"}',
                       headers={'Content-Type': 'application/json', 'X-EvoMAC-Client': '1', 'Origin': 'https://example.com'})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == '__main__':
    unittest.main()
