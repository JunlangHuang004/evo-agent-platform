"""Local handoff can finish real reports/tests/packages, never grant acceptance.

Scripted model and explicit code-boundary doubles; real files/artifacts/SQLite.
No generated code executes on the host, and no real model/network is contacted.
"""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.agents import AGENTS
from backend.config import Config
from backend.engine import Platform, clock_ms
from backend.stage_control import stage_action_feedback, stage_control
from tests.test_four_agents import BoundaryFixtureGateway, FourRoleModel, step


class DeadlineFixtureGateway(BoundaryFixtureGateway):
    def execute(self, agent_id, run_id, name, args, approved=False, deadline=None):
        if deadline is not None and time.monotonic() >= deadline:
            raise AssertionError('Fixture action attempted after the unchanged deadline')
        return super().execute(agent_id, run_id, name, args, approved)


class HandoffPlatform(Platform):
    def _prepare_task(self, channel, run_id, task):
        value = super()._prepare_task(channel, run_id, task)
        run = self._current(channel, run_id)
        if task['title'] in ('review', 'release') and not task.get('_testHandoffPinned'):
            now = clock_ms()
            control = stage_control(run, task, now)
            task['stageControl'].update(allocatedAt=now-2000, synthesisAt=now-1500,
                                        handoffAt=now-1000, expectedHandoffBy=now-500)
            task['_testHandoffPinned'] = True
        return value


class HandoffModel(FourRoleModel):
    def __init__(self):
        super().__init__()
        release = step('release', 'coordinator', ['implement', 'review'], 'planning')
        release['outputContract'] = {'kind': 'delivery'}
        self.plan['tasks'].insert(-1, release)
        self.plan['tasks'][-1]['dependsOn'] = ['release']
        self.phases = []

    def complete(self, messages, max_tokens=None, deadline=None):
        if 'Plan an executable task DAG' not in messages[0]['content']:
            context = self.context(messages)
            title = context['task']['title']
            if title in ('review', 'release'):
                self.phases.append((title, context['stageControl']['phase']))
            if title == 'release':
                index = self.counts[title]
                self.counts[title] += 1
                if index == 0:
                    action = {'type': 'package_delivery',
                              'artifactIds': [a['id'] for a in context['authorizedArtifacts']],
                              'entrypoint': 'solution.py',
                              'instructions': 'Inspect solution.py and the published independent review; use Python 3.11 to import total. This fixture is not a semantic product benchmark.'}
                else:
                    action = {'type': 'final', 'status': 'done', 'text': 'A real package was created; test-double execution is disclosed.'}
                return {'content': json.dumps(action), 'usage': {'total_tokens': 1}}
        return super().complete(messages)


class LocalCompletionHandoffTests(unittest.TestCase):
    def test_real_report_independent_test_review_and_package_finish_after_soft_target(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(Path(folder), {'LLM_API_KEY': 'offline-fixture', 'MAX_RETRIES': '0'})
            config.apply_time_only_profile()
            model = HandoffModel()
            app = HandoffPlatform(config, provider=model, tools=DeadlineFixtureGateway(config))
            try:
                state = app.action({'action': 'send', 'channelId': 'group', 'mode': 'task',
                                    'participants': list(AGENTS), 'text': 'Research, implement, independently review and package.'})
                original_deadline = state['sessions']['group']['run']['budget']['deadlineAt']
                self.assertTrue(app.wait_for_idle(10))
                run = app.snapshot()['sessions']['group']['run']
                by_title = {t['title']: t for t in run['steps']}
                self.assertEqual(run['status'], 'done', [(t['title'], t['status'], t.get('error')) for t in run['steps']])
                self.assertEqual(run['budget']['deadlineAt'], original_deadline)
                self.assertTrue(model.phases)
                self.assertTrue(all(phase == 'handoff_final' for _, phase in model.phases))
                review = by_title['review']
                actual_tests = [c for c in run['calls'] if c.get('taskId') == review['id'] and c['tool'] == 'code.test']
                self.assertEqual(len(actual_tests), 1)
                self.assertEqual(actual_tests[0]['status'], 'success')
                self.assertTrue(actual_tests[0]['result']['testDouble'])
                validation = by_title['implement']['validation']
                self.assertTrue(validation['verified'])
                self.assertEqual(validation['evidenceCallIds'], [actual_tests[0]['id']])
                report = next(a for a in run['artifacts'] if a['name'] == 'review.md')
                self.assertEqual(report['taskId'], review['id'])
                self.assertIn('"verified": true', report['content'])
                self.assertEqual(len(run['deliveries']), 1)
                self.assertEqual(run['deliveries'][0]['taskId'], by_title['release']['id'])
                self.assertEqual(run['deliveries'][0]['semanticQuality'], 'unassessed')
                self.assertEqual(by_title['release']['outputValidation']['status'], 'passed')
                self.assertEqual(len([e for e in run['events'] if e['type'] == 'delivery_packaged']), 1)
                self.assertFalse(any(e['type'] == 'stage_action_blocked' for e in run['events']))
            finally:
                app.shutdown(); app.wait_for_idle(4); app.repo.close()

    def test_local_handoff_admission_never_executes_or_grants_network_mutation_or_permissions(self):
        task = {'id': 'task', 'agentId': 'coordinator', 'revision': 0, 'status': 'running'}
        run = {'id': 'run', 'mode': 'task', 'status': 'running',
               'budget': {'profile': 'time_only', 'deadlineAt': 2000000}, 'steps': [task]}
        stage_control(run, task, 1000)
        task['stageControl'].update(synthesisAt=1100, handoffAt=1200, expectedHandoffBy=1300)
        before = copy.deepcopy(run)
        control = stage_control(run, task, 1500)
        self.assertEqual(control['phase'], 'handoff_final')
        self.assertEqual(control['handoffAllowedTools'], ['code.test', 'file.patch', 'file.read', 'file.write'])
        for name in ('file.read', 'file.write', 'file.patch', 'code.test'):
            self.assertIsNone(stage_action_feedback(control, {'type': 'tool', 'name': name}))
        for name in ('web.fetch', 'web.search', 'file.delete', 'git.status', 'git.commit', 'git.push', 'code.run'):
            result = stage_action_feedback(control, {'type': 'tool', 'name': name})
            self.assertFalse(result['actionExecuted'])
        for kind in ('delegate', 'ask_human', 'grant_artifact'):
            self.assertFalse(stage_action_feedback(control, {'type': kind})['actionExecuted'])
        self.assertEqual(run, before, 'admission executes no code, approves no SHA, changes no status or budget')


if __name__ == '__main__':
    unittest.main()
