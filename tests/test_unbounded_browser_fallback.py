"""Incomplete old reports must not bypass the generic repeated-action stop."""
import tempfile
from pathlib import Path
import unittest

from backend.config import Config
from backend.engine import Platform
from tests.test_browser_failure_recovery import BrowserFixtureTools
from tests.test_software_diagnostic_handoff import DeadlineScriptedProvider


class IncompleteReportTools(BrowserFixtureTools):
    def execute(self, agent, run, name, args, approved=False, deadline=None):
        result = super().execute(agent, run, name, args, approved, deadline)
        if name == 'code.test':
            result['browser'].pop('steps', None)
        return result


class LegacyRecoveryBoundaryTests(unittest.TestCase):
    def test_unsupported_report_cannot_create_unlimited_feedback_turns(self):
        with tempfile.TemporaryDirectory() as root:
            config = Config(Path(root), {'LLM_API_KEY': 'offline', 'MAX_RETRIES': '0'})
            config.apply_time_only_profile()
            def script(index, context, messages):
                if index == 0:
                    return {'type': 'tool', 'name': 'file.write', 'args': {
                        'path': 'index.html', 'content': '<h1>Real heading</h1>'}}
                return {'type': 'tool', 'name': 'code.test', 'args': {
                    'language': 'browser', 'path': 'index.html',
                    'checks': [{'action': 'assert_text', 'selector': 'h1', 'text': 'Wrong'}]}}
            model = DeadlineScriptedProvider([{'id': 'build', 'title': 'Build', 'agentId': 'executor'}],
                                     {'Build': script})
            app = Platform(config, provider=model, tools=IncompleteReportTools(config))
            try:
                app.action({'action': 'send', 'mode': 'task', 'participants': ['executor'],
                            'text': 'Exercise the legacy report boundary with an offline fixture.'})
                self.assertTrue(app.wait_for_idle(5))
                run = app.state['sessions']['group']['run']
                task = next(t for t in run['steps'] if t['title'] == 'Build')
                calls = [c for c in run['calls'] if c['tool'] == 'code.test']
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(c['status'] == 'failed' for c in calls))
                self.assertEqual(task['status'], 'failed')
                self.assertEqual(task['turns'], 4)
                self.assertIn('重复动作', task['error']['message'])
                self.assertFalse(any(e['type'] == 'browser_failure_recovery' for e in run['events']))
                self.assertTrue(any(e['type'] == 'repeated_action_warning' for e in run['events']))
                self.assertEqual(run['budgetExtensions'], 0)
            finally:
                app.shutdown()
                app.wait_for_idle(5)
                app.repo.close()


if __name__ == '__main__':
    unittest.main()
