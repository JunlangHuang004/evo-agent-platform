"""Text contracts need actual current published files, not self-reported MET."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.completion_guardrail import completion_clarification_feedback
from backend.config import Config
from backend.contracts import validate_task_output
from backend.engine import Platform
from tests.test_completion_guardrail import LocalFiles, candidate, feedback, write
from tests.test_engine import ScriptedProvider, task, final


def text_candidate():
    run, producer, action, validation = candidate()
    producer['outputContract'] = {'kind': 'text'}
    validation.update(kind='text', checkedArtifactIds=[], matchedArtifactIds=[])
    return run, producer, action, validation


class TextArtifactEligibilityTests(unittest.TestCase):
    def test_text_format_plus_current_real_artifact_only_produces_unassessed_feedback(self):
        run, producer, action, validation = text_candidate(); before = copy.deepcopy(run)
        result = feedback(run, producer, action, validation, stage_phase='handoff_final')
        self.assertIsNotNone(result)
        self.assertEqual(result['outputValidation']['kind'], 'text')
        self.assertEqual(result['outputValidation']['matchedArtifactIds'], [])
        self.assertEqual(result['binding']['artifactHashes'], {'findings': run['artifacts'][0]['sha256']})
        self.assertEqual(result['semanticQuality'], 'unassessed')
        self.assertIn('semanticQuality=unassessed means only', result['instruction'])
        self.assertIn('not an extra acceptance criterion', result['instruction'])
        self.assertIn('original criteria/contract require it', result['instruction'])
        self.assertIn('Missing actual files or unperformed mandatory checks', result['instruction'])
        self.assertEqual(run, before)

    def test_text_self_claim_no_artifact_inherited_or_stale_bytes_never_get_extra_turn(self):
        for change in ('no_output', 'inherited', 'stale', 'bad_hash', 'fake_matched'):
            run, producer, action, validation = text_candidate()
            if change == 'no_output':producer['outputs'] = []
            elif change == 'inherited':run['artifacts'][0]['inheritedReadOnly'] = True
            elif change == 'stale':run['calls'].insert(-1, {**run['calls'][0], 'id': 'delete', 'tool': 'file.delete'})
            elif change == 'bad_hash':run['artifacts'][0]['sha256'] = '0'*64
            else:validation['matchedArtifactIds'] = ['findings']
            with self.subTest(change=change):
                self.assertIsNone(feedback(run, producer, action, validation))

    def test_unknown_effect_pending_and_global_final_guards_are_not_relaxed_for_text(self):
        for change in ('uncertain', 'pending', 'effect_unknown', 'final_summary', 'final_only'):
            run, producer, action, validation = text_candidate(); options = {}
            if change == 'uncertain':producer['_uncertainCalls'] = ['unknown-tool']
            elif change == 'pending':producer['_pendingTool'] = {'name': 'file.write'}
            elif change == 'effect_unknown':run['calls'].insert(-1, {
                'id': 'unknown-web', 'agentId': producer['agentId'], 'taskId': producer['id'],
                'revision': 0, 'tool': 'web.fetch', 'args': {}, 'status': 'failed',
                'result': {'ok': False, 'outcomeUnknown': True}})
            elif change == 'final_summary':producer['finalSummary'] = True
            else:options['final_only'] = True
            with self.subTest(change=change):
                self.assertIsNone(feedback(run, producer, action, validation, **options))

    def test_exact_r6_frozen_partial_is_eligible_only_at_reconstructed_preclosure_decision(self):
        path = Path(__file__).with_name('fixtures')/'r6_newsroom1_text_partial.json'
        fixture = json.loads(path.read_text()); run = fixture['run']; producer = run['steps'][0]
        self.assertEqual(producer['status'], 'partial')
        self.assertEqual(producer['error']['causeCode'], 'stage_handoff_final')
        self.assertEqual(producer['outputContract'], {'kind': 'text'})
        action = json.loads(run['transcript'][-1]['text'])
        class FrozenSnapshots:
            def get(self, artifact_id):return next(a for a in run['artifacts'] if a['id'] == artifact_id)
        validation = validate_task_output(producer, run, FrozenSnapshots(), action['text'])
        self.assertEqual(validation['status'], 'passed')
        self.assertIsNone(completion_clarification_feedback(run, producer, action, validation,
            now_ms=fixture['closureDecisionEpochMs'], stage_phase='handoff_final'))
        original = copy.deepcopy(run)
        replay = copy.deepcopy(run); replay['status'] = 'running'; current = replay['steps'][0]
        # These are explicit known pre-worker-closure state overrides, never
        # edits to the historical run or proof that the model changes its choice.
        current.update(status='running', error=None)
        current.pop('closureReason', None); current.pop('finishedAt', None)
        result = completion_clarification_feedback(replay, current, action, validation,
            now_ms=fixture['closureDecisionEpochMs'], stage_phase='handoff_final')
        self.assertIsNotNone(result)
        self.assertEqual(result['acceptanceCriteria'], producer['acceptanceCriteria'])
        self.assertEqual(result['binding']['artifactHashes'], {
            'artifact-4c732960d3824adba59560c126837040': 'c4fe5d3e46f59787967e82ffea6ea0e7dd1cd60fbfbea473137b4d06360803c7'})
        self.assertEqual(run, original)
        self.assertEqual(len([c for c in replay['calls'] if c.get('usageUnknown')]), 3)


class TextArtifactEngineTests(unittest.TestCase):
    def setUp(self):self.temp = tempfile.TemporaryDirectory(); self.apps = []
    def tearDown(self):
        for app in self.apps:app.shutdown(); app.wait_for_idle(4); app.repo.close()
        self.temp.cleanup()
    def make_app(self, script):
        config = Config(Path(self.temp.name)/str(len(self.apps)), {'LLM_API_KEY': 'offline-text-scope',
            'MAX_RETRIES': '0', 'MAX_AGENT_TURNS': '8', 'MAX_MODEL_CALLS': '20', 'MAX_RUN_TOKENS': '400000'})
        spec = {**task('Bounded note', 'research'), 'outputContract': {'kind': 'text'},
                'acceptanceCriteria': ['Provide claim ratings and a required independent review receipt.']}
        model = ScriptedProvider([spec], {'Bounded note': script})
        app = Platform(config, provider=model, tools=LocalFiles(config)); self.apps.append(app)
        app.action({'action': 'send', 'mode': 'task', 'participants': ['research'],
                    'text': 'Provide actual scoped output; preserve genuinely unfinished required checks.'})
        self.assertTrue(app.wait_for_idle(5))
        return app, model, app.snapshot()['sessions']['group']['run']

    def test_actual_missing_criterion_can_remain_partial_after_the_single_text_scope_clarification(self):
        def script(index, context, messages):
            if index == 0:return write('findings.md', 'Claims rated; independent review is still absent.')
            if index == 1:return {'type': 'publish_artifact', 'path': 'findings.md'}
            if index == 2:return final('Ratings are documented but required independent review is missing.', status='partial')
            self.assertEqual(index, 3)
            receipt = json.loads(next(m['content'] for m in reversed(messages)
                if m['content'].startswith('动作的真实结果：')).split('：', 1)[1])
            self.assertEqual(receipt['error']['code'], 'completion_scope_clarification')
            self.assertEqual(receipt['outputValidation']['kind'], 'text')
            self.assertEqual(receipt['acceptanceCriteria'], context['task']['acceptanceCriteria'])
            return final('Claim ratings met via the published note; the required independent review is not met. Keep partial.', status='partial')
        app, model, run = self.make_app(script)
        producer = next(t for t in run['steps'] if t['title'] == 'Bounded note')
        self.assertEqual(producer['status'], 'partial'); self.assertIsNone(producer['error'])
        self.assertEqual(model.counts['Bounded note'], 4)
        self.assertEqual(sum(e['type'] == 'completion_scope_clarification' for e in run['events']), 1)
        self.assertFalse(any(e['type'].startswith('review_') for e in run['events']))
        self.assertEqual(sum(c['tool'] == 'file.write' for c in run['calls']), 1)
        self.assertEqual(app.repo.get_run(run['id'])['status'], 'partial')

    def test_text_without_published_output_is_not_nudged_on_self_asserted_met(self):
        _, model, run = self.make_app([final('I claim every criterion MET.', status='partial')])
        self.assertEqual(model.counts['Bounded note'], 1)
        self.assertFalse(run['artifacts'])
        self.assertFalse(any(e['type'] == 'completion_scope_clarification' for e in run['events']))


if __name__ == '__main__':unittest.main()
