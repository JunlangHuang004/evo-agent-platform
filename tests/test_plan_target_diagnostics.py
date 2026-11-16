"""Locate rejected submitted IDs without repairing plans or weakening review ACLs."""
import copy
import hashlib
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from backend.agents import AGENTS
from backend.teams import get_team_agents
from backend.config import Config
from backend.engine import Platform
from backend.runtime import normalize_plan, ProtocolError
from tests.test_engine import ScriptedProvider, task, final
from tests.test_execution_closure import LocalFiles, tool


FIXTURE = Path(__file__).parent / 'fixtures/r9-plan-reviewof-errors.json'


class PlanTargetDiagnosticsTests(unittest.TestCase):
    def normalize(self, plan, participants=None, agents=None):
        ids = itertools.count()
        return normalize_plan(plan, participants or list(AGENTS),
                              lambda _: 'opaque-generated-' + str(next(ids)), 12, agents)

    def test_all_three_real_rejected_plans_identify_original_ids_and_owners(self):
        fixture = json.loads(FIXTURE.read_text())
        registry = get_team_agents('fund')
        self.assertEqual(len(fixture['attempts']), 3)
        for attempt in fixture['attempts']:
            with self.subTest(callId=attempt['callId']):
                self.assertEqual(hashlib.sha256(attempt['rawResponse'].encode()).hexdigest(),
                                 attempt['responseSha256'])
                plan = json.loads(attempt['rawResponse'])
                before = copy.deepcopy(plan)
                with self.assertRaises(ProtocolError) as caught:
                    self.normalize(plan, list(registry), registry)
                text = str(caught.exception)
                self.assertIn('reviewer id="t6", owner="risk"', text)
                self.assertIn('target id="t3", owner="risk"', text)
                self.assertIn('supporting analysis may remain in dependsOn', text)
                self.assertIn('not be an independent reviewOf target', text)
                self.assertNotIn('opaque-generated-', text)
                self.assertEqual(plan, before)

    def test_reviewer_must_explicitly_submit_the_correction_and_supporting_edge_stays(self):
        registry = get_team_agents('fund')
        fixture = json.loads(FIXTURE.read_text())
        bad = json.loads(fixture['attempts'][2]['rawResponse'])
        revised = copy.deepcopy(bad)
        review = next(t for t in revised['tasks'] if t['id'] == 't6')
        self.assertIn('t3', review['dependsOn'])
        review['reviewOf'] = [target for target in review['reviewOf'] if target != 't3']
        before = copy.deepcopy(revised)
        rows = self.normalize(revised, list(registry), registry)
        by_title = {t['title']: t for t in rows}
        actual = by_title[review['title']]
        own_analysis = by_title[next(t['title'] for t in revised['tasks'] if t['id'] == 't3')]
        self.assertIn(own_analysis['id'], actual['dependsOn'])
        self.assertNotIn(own_analysis['id'], actual['reviewOf'])
        self.assertEqual(revised, before)
        self.assertTrue(all(t['status'] == 'pending' and not t['outputs'] and 'validation' not in t
                            for t in rows))
        with self.assertRaises(ProtocolError):
            self.normalize(bad, list(registry), registry)

    def test_self_and_missing_explicit_owner_remain_rejected_with_truthful_details(self):
        plan = {'tasks': [task('Draft', 'research'),
                         {**task('Review', 'reviewer', ['Draft']), 'reviewOf': ['Draft']}]}
        for mode in ('self', 'missing_owner'):
            with self.subTest(mode=mode):
                bad = copy.deepcopy(plan)
                if mode == 'self':
                    bad['tasks'][1]['reviewOf'] = ['Review']
                else:
                    bad['tasks'][0].pop('agentId')
                with self.assertRaises(ProtocolError) as caught:
                    self.normalize(bad, ['research', 'reviewer'])
                self.assertIn('reviewer id="Review", owner="reviewer"', str(caught.exception))
                self.assertIn('target id="Review"' if mode == 'self' else 'target id="Draft", owner=null',
                              str(caught.exception))


class PlanTargetWorkflowTests(unittest.TestCase):
    def test_actual_error_reaches_next_model_and_only_new_plan_runs_explicit_read_and_review(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(Path(directory), {'LLM_API_KEY': 'offline-only', 'MAX_RUN_SECONDS': '20'})
            good = [task('Supporting analysis', 'reviewer'),
                    {**task('Draft', 'research'),
                     'outputContract': {'kind': 'artifact', 'extensions': ['.md']}},
                    {**task('Independent review', 'reviewer', ['Supporting analysis', 'Draft']),
                     'reviewOf': ['Draft']}]
            bad = copy.deepcopy(good)
            bad[2]['reviewOf'] = ['Supporting analysis', 'Draft']
            originals = copy.deepcopy((good, bad))
            planning = []

            def review(index, context, messages):
                target = next(t for t in context['dependencyResults'] if t['agentId'] == 'research')
                self.assertEqual(context['task']['reviewOf'], [target['id']])
                if index == 0:
                    return tool('file.read', path=context['authorizedArtifacts'][0]['path'])
                if index == 1:
                    run = app.state['sessions']['group']['run']
                    read = next(c for c in reversed(run['calls'])
                                if c['tool'] == 'file.read' and c['taskId'] == context['task']['id'])
                    return {'type': 'review', 'taskId': target['id'], 'verdict': 'pass',
                            'feedback': 'Read the actual editorial fixture; no executable verification claim.',
                            'evidenceCallIds': [read['id']]}
                return final('Explicit independent editorial assessment completed.')

            model = ScriptedProvider(good, {
                'Supporting analysis': [final('Own analysis is supporting input, not an independent self-review.')],
                'Draft': [tool('file.write', path='draft.md', content='Actual complete editorial fixture.'),
                          {'type': 'publish_artifact', 'path': 'draft.md'}, final('Draft published.')],
                'Independent review': review})
            original = model.complete

            def complete(messages, max_tokens=None, deadline=None):
                if messages[0]['content'].startswith('Plan an executable task DAG'):
                    planning.append(copy.deepcopy(messages))
                    if len(planning) == 1:
                        return {'content': json.dumps({'tasks': bad}), 'usage': {'total_tokens': 1}}
                    feedback = messages[-1]['content']
                    self.assertIn('reviewer id="Independent review", owner="reviewer"', feedback)
                    self.assertIn('target id="Supporting analysis", owner="reviewer"', feedback)
                    self.assertIn('supporting analysis may remain in dependsOn', feedback)
                    run = app.state['sessions']['group']['run']
                    self.assertFalse(any(c['tool'] != 'llm.chat' for c in run['calls']))
                    self.assertFalse(any(e['type'] == 'dag_created' for e in run['events']))
                return original(messages)

            model.complete = complete
            app = Platform(config, provider=model, tools=LocalFiles(config))
            try:
                app.action({'action': 'send', 'mode': 'task', 'participants': ['research', 'reviewer'],
                            'text': 'Produce and independently review a short report.'})
                self.assertTrue(app.wait_for_idle(6))
                run = app.state['sessions']['group']['run']
                self.assertEqual(run['status'], 'done')
                self.assertEqual(len(planning), 2)
                self.assertEqual(sum(e['type'] == 'dag_created' for e in run['events']), 1)
                self.assertEqual(sum(e['type'] == 'artifact_registered' for e in run['events']), 1)
                self.assertEqual(sum(e['type'] == 'review_pass' for e in run['events']), 1)
                self.assertEqual(sum(c['tool'] == 'file.write' for c in run['calls']), 1)
                self.assertEqual(sum(c['tool'] == 'file.read' for c in run['calls']), 1)
                by_title = {t['title']: t for t in run['steps']}
                reviewer = by_title['Independent review']
                self.assertIn(by_title['Supporting analysis']['id'], reviewer['dependsOn'])
                self.assertEqual(reviewer['reviewOf'], [by_title['Draft']['id']])
                validation = by_title['Draft']['validation']
                self.assertEqual(validation['kind'], 'editorial_assessment')
                self.assertFalse(validation['verified'])
                self.assertEqual((good, bad), originals)
            finally:
                app.shutdown()
                app.wait_for_idle(3)
                app.repo.close()


if __name__ == '__main__':
    unittest.main()
