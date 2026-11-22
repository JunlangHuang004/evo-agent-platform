"""Exam-team role prompt injection: policy, persistence, and runtime refusals.

No model, Docker or network is exercised. The tests cover only the server-owned
behaviour of the new ``set_role_prompts`` action: default placeholder, prompt
sanitation and length caps, team-scoped rejection, refusal while a run is
active, SQLite persistence, capability profile switching, ToolGateway
permission sync, and the fixed profile allowlist regardless of input text.
"""
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import ActionError, Platform
from backend.runtime import action_prompt
from backend.teams import (EXAM_DEFAULT_PROFILES, EXAM_PROFILES, EXAM_TEAM_ID,
                           apply_exam_profile, exam_role_prompt_limit,
                           exam_role_prompt_placeholder,
                           get_team_agents, is_exam_team, list_exam_profiles,
                           list_teams)
from backend.tools import ToolError


class ExamTeamRegistryTests(unittest.TestCase):
    def test_exam_team_is_listed_with_four_numbered_agents(self):
        self.assertIn('exam', [team['id'] for team in list_teams()])
        self.assertTrue(is_exam_team('exam'))
        self.assertFalse(is_exam_team('default'))
        agents = get_team_agents('exam')
        self.assertEqual(list(agents), ['agent1', 'agent2', 'agent3', 'agent4'])
        placeholder = exam_role_prompt_placeholder()
        for slot, agent in agents.items():
            self.assertTrue(agent['rolePromptCustomizable'])
            self.assertFalse(agent['rolePromptInjected'])
            self.assertEqual(agent['rolePrompt'], placeholder)
            self.assertEqual(agent['description'], placeholder)
            self.assertEqual(agent['profileId'], EXAM_DEFAULT_PROFILES[slot])

    def test_profile_list_covers_every_registered_profile(self):
        published = list_exam_profiles()
        self.assertEqual({entry['id'] for entry in published}, set(EXAM_PROFILES))
        for entry in published:
            self.assertIn('避免重复动作', entry['description'])
        prompt = action_prompt({'name': 'Agent', 'rolePrompt': 'Role.', 'capabilities': []}, [])
        self.assertIn('避免重复动作', prompt)
        # Each profile exposes a non-empty tool list and a stable primary cap.
        for entry in list_exam_profiles():
            self.assertTrue(entry['tools'])
            self.assertTrue(entry['capabilities'])
            self.assertIn(entry['primaryCapability'], entry['capabilities'])

    def test_apply_exam_profile_rejects_unknown_profile(self):
        agents = get_team_agents('exam')
        with self.assertRaises(ValueError):
            apply_exam_profile(agents['agent1'], 'bogus')

    def test_fixed_tool_allowlist_per_default_profile(self):
        agents = get_team_agents('exam')
        self.assertIn('web.search', agents['agent1']['tools'])
        self.assertIn('web.fetch', agents['agent2']['tools'])
        self.assertIn('code.test', agents['agent3']['tools'])
        self.assertIn('web.search', agents['agent3']['tools'])
        self.assertNotIn('git.push', agents['agent3']['tools'])
        self.assertIn('code.test', agents['agent4']['tools'])
        self.assertNotIn('git.push', agents['agent4']['tools'])
        release = dict(agents['agent4'])
        apply_exam_profile(release, 'release')
        self.assertIn('git.push', release['tools'])
        self.assertIn('code.test', release['tools'])
        self.assertNotIn('web.search', release['tools'])


class ExamRolePromptActionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path('.'))
        self.config.data_dir = Path(self._tmp.name)
        self.platform = Platform(self.config,
                                 agents=get_team_agents(EXAM_TEAM_ID),
                                 team_id=EXAM_TEAM_ID)

    def tearDown(self):
        self.platform.shutdown()
        self.platform.wait_for_idle(5)
        self.platform.repo.close()
        self._tmp.cleanup()

    def test_injection_updates_agents_and_persists_state(self):
        before_tools = {agent_id: list(agent['tools'])
                        for agent_id, agent in self.platform.agents.items()}
        snapshot = self.platform.action({'action': 'set_role_prompts',
            'rolePrompts': {'agent1': 'You are A1.', 'agent3': 'You are A3.'}})
        self.assertEqual(snapshot['teamId'], EXAM_TEAM_ID)
        self.assertEqual(self.platform.agents['agent1']['rolePrompt'], 'You are A1.')
        self.assertTrue(self.platform.agents['agent1']['rolePromptInjected'])
        self.assertFalse(self.platform.agents['agent2']['rolePromptInjected'])
        self.assertTrue(self.platform.agents['agent3']['rolePromptInjected'])
        # Tool permissions never shift with a prompt edit.
        after_tools = {agent_id: list(agent['tools'])
                       for agent_id, agent in self.platform.agents.items()}
        self.assertEqual(before_tools, after_tools)
        # Snapshot exposes the injected text to the UI.
        snapshot_agent1 = next(a for a in snapshot['agents'] if a['id'] == 'agent1')
        self.assertEqual(snapshot_agent1['rolePrompt'], 'You are A1.')
        self.assertTrue(snapshot_agent1['rolePromptInjected'])

    def test_restart_recovers_injected_prompts_from_state(self):
        self.platform.action({'action': 'set_role_prompts',
            'rolePrompts': {'agent2': 'You are A2.'}})
        self.platform.shutdown()
        self.platform.wait_for_idle(5)
        self.platform.repo.close()
        revived = Platform(self.config, agents=get_team_agents(EXAM_TEAM_ID),
                           team_id=EXAM_TEAM_ID)
        # Replace the fixture's handle so tearDown closes exactly one live db.
        self.platform = revived
        self.assertEqual(revived.agents['agent2']['rolePrompt'], 'You are A2.')
        self.assertTrue(revived.agents['agent2']['rolePromptInjected'])
        self.assertFalse(revived.agents['agent1']['rolePromptInjected'])

    def test_clearing_resets_slot_to_placeholder(self):
        self.platform.action({'action': 'set_role_prompts',
            'rolePrompts': {'agent1': 'You are A1.'}})
        self.assertTrue(self.platform.agents['agent1']['rolePromptInjected'])
        self.platform.action({'action': 'set_role_prompts',
            'rolePrompts': {'agent1': '   '}})
        self.assertFalse(self.platform.agents['agent1']['rolePromptInjected'])
        self.assertEqual(self.platform.agents['agent1']['rolePrompt'],
                         exam_role_prompt_placeholder())
        self.assertNotIn('agent1', self.platform.state['rolePrompts'])

    def test_unknown_agent_rejected(self):
        with self.assertRaises(ActionError) as caught:
            self.platform.action({'action': 'set_role_prompts',
                'rolePrompts': {'agent9': 'nope'}})
        self.assertEqual(caught.exception.status, 400)
        self.assertIn('agent9', str(caught.exception))

    def test_length_cap_rejected(self):
        oversize = 'x' * (exam_role_prompt_limit() + 1)
        with self.assertRaises(ActionError) as caught:
            self.platform.action({'action': 'set_role_prompts',
                'rolePrompts': {'agent1': oversize}})
        self.assertEqual(caught.exception.status, 400)

    def test_non_string_value_rejected(self):
        with self.assertRaises(ActionError):
            self.platform.action({'action': 'set_role_prompts',
                'rolePrompts': {'agent1': 123}})

    def test_empty_prompts_object_rejected(self):
        with self.assertRaises(ActionError):
            self.platform.action({'action': 'set_role_prompts',
                'rolePrompts': {}})

    def test_active_run_blocks_injection(self):
        self.platform.state['sessions']['group']['run'] = {
            'id': 'run-fake', 'status': 'running', 'channelId': 'group',
            'teamId': EXAM_TEAM_ID}
        with self.assertRaises(ActionError) as caught:
            self.platform.action({'action': 'set_role_prompts',
                'rolePrompts': {'agent1': 'You are A1.'}})
        self.assertEqual(caught.exception.status, 409)

    def test_profile_switch_updates_tools_capabilities_and_gateway(self):
        before_tools = list(self.platform.agents['agent2']['tools'])
        self.assertNotIn('code.test', before_tools)
        self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
            'agent2': {'profile': 'analyst',
                       'prompt': 'You are a market analyst.'}}})
        after = self.platform.agents['agent2']
        self.assertEqual(after['profileId'], 'analyst')
        self.assertEqual(after['primaryCapability'], 'market_analysis')
        self.assertIn('code.test', after['tools'])
        # ToolGateway holds its own copy; it must observe the new permission.
        gateway_agent = self.platform.tools.agents['agent2']
        self.assertEqual(gateway_agent['tools'], after['tools'])
        self.assertEqual(gateway_agent['primaryCapability'],
                         after['primaryCapability'])

    def test_profile_switch_persists_across_restart(self):
        self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
            'agent3': {'profile': 'writer'}}})
        self.platform.shutdown()
        self.platform.wait_for_idle(5)
        self.platform.repo.close()
        revived = Platform(self.config, agents=get_team_agents(EXAM_TEAM_ID),
                           team_id=EXAM_TEAM_ID)
        self.platform = revived
        self.assertEqual(revived.agents['agent3']['profileId'], 'writer')
        self.assertNotIn('code.run', revived.agents['agent3']['tools'])
        self.assertNotIn('git.push', revived.agents['agent3']['tools'])
        # ToolGateway created during restart must also see the new whitelist.
        self.assertEqual(revived.tools.agents['agent3']['tools'],
                         revived.agents['agent3']['tools'])

    def test_profile_only_edit_leaves_prompt_untouched(self):
        self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
            'agent4': 'You are the fact-checker.'}})
        self.assertTrue(self.platform.agents['agent4']['rolePromptInjected'])
        self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
            'agent4': {'profile': 'writer'}}})
        self.assertEqual(self.platform.agents['agent4']['profileId'], 'writer')
        self.assertTrue(self.platform.agents['agent4']['rolePromptInjected'])
        self.assertEqual(self.platform.agents['agent4']['rolePrompt'],
                         'You are the fact-checker.')

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ActionError) as caught:
            self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
                'agent1': {'profile': 'bogus'}}})
        self.assertEqual(caught.exception.status, 400)
        self.assertIn('profile', str(caught.exception))

    def test_empty_structured_entry_rejected(self):
        with self.assertRaises(ActionError):
            self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
                'agent1': {}}})

    def test_multiple_profiles_union_tools_and_persist(self):
        self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
            'agent3': {'profile': ['release', 'coder', 'coder']}}})
        agent = self.platform.agents['agent3']
        self.assertEqual(agent['profileIds'], ['coder', 'release'])
        self.assertEqual(agent['profileId'], 'coder')
        self.assertIn('web.search', agent['tools'])
        self.assertIn('git.push', agent['tools'])
        self.assertIn('coding', agent['capabilities'])
        self.assertIn('release', agent['capabilities'])
        self.assertEqual(self.platform.tools.agents['agent3']['tools'], agent['tools'])
        self.assertEqual(self.platform.state['roleProfiles']['agent3'], ['coder', 'release'])
        self.platform.shutdown()
        self.platform.wait_for_idle(5)
        self.platform.repo.close()
        revived = Platform(self.config, agents=get_team_agents(EXAM_TEAM_ID),
                           team_id=EXAM_TEAM_ID)
        self.platform = revived
        self.assertEqual(revived.agents['agent3']['profileIds'], ['coder', 'release'])
        self.assertIn('git.push', revived.tools.agents['agent3']['tools'])

    def test_profile_list_rejects_empty_and_unknown(self):
        with self.assertRaises(ActionError):
            self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
                'agent1': {'profile': []}}})
        with self.assertRaises(ActionError):
            self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
                'agent1': {'profile': ['planner', 'bogus']}}})
        self.assertEqual(self.platform.agents['agent1']['profileIds'], ['planner'])

    def test_tool_gateway_denies_actions_outside_new_profile(self):
        """Release can push; leaving that profile revokes git.push at the gateway."""
        self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
            'agent3': {'profile': 'release'}}})
        self.assertIn('git.push', self.platform.tools.agents['agent3']['tools'])
        self.platform.action({'action': 'set_role_prompts', 'rolePrompts': {
            'agent3': {'profile': 'writer'}}})
        with self.assertRaises(ToolError) as caught:
            self.platform.tools.execute('agent3', 'run-ignored', 'git.push', {})
        self.assertEqual(caught.exception.code, 'permission')

    def test_snapshot_exposes_available_profiles(self):
        snapshot = self.platform.snapshot()
        self.assertEqual({p['id'] for p in snapshot['availableProfiles']},
                         set(EXAM_PROFILES))

    def test_non_exam_team_snapshot_has_no_profiles(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            config = Config(Path('.'))
            config.data_dir = Path(tmp.name)
            platform = Platform(config, agents=get_team_agents('default'),
                                team_id='default')
            try:
                self.assertNotIn('availableProfiles', platform.snapshot())
            finally:
                platform.shutdown()
                platform.wait_for_idle(5)
                platform.repo.close()
        finally:
            tmp.cleanup()


class NonExamTeamRejectsInjectionTests(unittest.TestCase):
    def test_default_team_rejects_set_role_prompts(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            config = Config(Path('.'))
            config.data_dir = Path(tmp.name)
            platform = Platform(config, agents=get_team_agents('default'),
                                team_id='default')
            try:
                with self.assertRaises(ActionError) as caught:
                    platform.action({'action': 'set_role_prompts',
                        'rolePrompts': {'coordinator': 'nope'}})
                self.assertEqual(caught.exception.status, 400)
            finally:
                platform.shutdown()
                platform.wait_for_idle(5)
                platform.repo.close()
        finally:
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
