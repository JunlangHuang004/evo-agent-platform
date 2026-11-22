"""User-created teams start as four unnamed slots with profile and prompt setup.

No model, Docker or network. The registry is cleared afterwards so later tests
still see only the built-in teams.
"""
from pathlib import Path
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from backend.config import Config
from backend.engine import Platform
from backend.server import TeamHub, make_handler
from backend.teams import (allows_role_setup, clear_custom_teams, create_custom_team,
                           exam_role_prompt_placeholder, get_team_agents, is_exam_team,
                           list_teams, load_custom_teams)


class CustomTeamRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name), {'LLM_API_KEY': 'custom-team-test'})
        load_custom_teams(self.config.data_dir)
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.shutdown()
            app.wait_for_idle(4)
            app.repo.close()
        clear_custom_teams()
        self.tmp.cleanup()

    def test_new_team_matches_the_initial_exam_slots(self):
        before = [item['id'] for item in list_teams()]
        created = create_custom_team('  课程实验组  ')
        self.assertEqual(created['name'], '课程实验组')
        self.assertTrue(created['id'].startswith('custom-'))
        self.assertEqual([item['id'] for item in list_teams()][:len(before)], before)
        self.assertIn(created['id'], [item['id'] for item in list_teams()])
        self.assertFalse(is_exam_team(created['id']))
        self.assertTrue(allows_role_setup(created['id']))
        self.assertFalse(allows_role_setup('software'))
        agents = get_team_agents(created['id'])
        exam = get_team_agents('exam')
        self.assertEqual(list(agents), ['agent1', 'agent2', 'agent3', 'agent4'])
        placeholder = exam_role_prompt_placeholder()
        for slot, agent in agents.items():
            self.assertEqual(agent['name'], exam[slot]['name'])
            self.assertEqual(agent['tools'], exam[slot]['tools'])
            self.assertEqual(agent['profileIds'], exam[slot]['profileIds'])
            self.assertTrue(agent['rolePromptCustomizable'])
            self.assertFalse(agent['rolePromptInjected'])
            self.assertEqual(agent['rolePrompt'], placeholder)

    def test_names_must_be_new_and_the_registry_reloads(self):
        create_custom_team('实验一组')
        for name in ('', '   ', '期末测试团队', '实验一组', 'x' * 41, 'bad/name'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    create_custom_team(name)
        load_custom_teams(self.config.data_dir)
        names = [item['name'] for item in list_teams()]
        self.assertEqual(names.count('实验一组'), 1)
        stored = json.loads((self.config.data_dir / 'custom_teams.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['version'], 1)
        self.assertEqual(stored['teams'][0]['name'], '实验一组')

    def test_created_team_accepts_profile_and_prompt(self):
        created = create_custom_team('可配置小组')
        config = Config(Path(self.tmp.name) / 'runtime', {'LLM_API_KEY': 'custom-team-test'})
        config.data_dir = self.config.data_dir / 'teams' / created['id']
        app = Platform(config, agents=get_team_agents(created['id']), team_id=created['id'])
        self.apps.append(app)
        self.assertIn('availableProfiles', app.snapshot())
        self.assertFalse(app.agents['agent1']['rolePromptInjected'])
        prompt = 'You are the editor for this custom team.'
        snapshot = app.action({'action': 'set_role_prompts', 'rolePrompts': {
            'agent1': {'prompt': prompt, 'profile': 'writer'}}})
        agent = next(item for item in snapshot['agents'] if item['id'] == 'agent1')
        self.assertEqual(agent['rolePrompt'], prompt)
        self.assertTrue(agent['rolePromptInjected'])
        self.assertEqual(agent['profileIds'], ['writer'])
        self.assertIn('file.write', [tool['name'] for tool in agent['tools']])
        self.assertNotIn('code.run', [tool['name'] for tool in agent['tools']])


class CustomTeamHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name), {'LLM_API_KEY': 'custom-team-http'})
        self.hub = TeamHub(self.config, platform_factory=Platform)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.hub))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.hub.shutdown()
        clear_custom_teams()
        self.tmp.cleanup()

    def request(self, path, payload=None):
        import urllib.request
        if payload is None:
            request = urllib.request.Request(self.base + path)
        else:
            request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json', 'X-EvoMAC-Client': '1'})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode())

    def test_post_creates_a_switchable_blank_team(self):
        created = self.request('/api/teams', {'name': '界面新建组'})
        team_id = created['team']['id']
        self.assertIn(team_id, [item['id'] for item in created['teams']])
        listed = self.request('/api/teams')
        self.assertIn(team_id, [item['id'] for item in listed['teams']])
        state = self.request('/api/state?team=' + team_id)
        self.assertEqual(state['teamName'], '界面新建组')
        self.assertEqual({item['id'] for item in state['agents']}, {'agent1', 'agent2', 'agent3', 'agent4'})
        self.assertTrue(all(item['rolePromptCustomizable'] for item in state['agents']))
        self.assertTrue(state['availableProfiles'])
        software = self.request('/api/state?team=software')
        self.assertNotIn('availableProfiles', software)


if __name__ == '__main__':
    unittest.main()
