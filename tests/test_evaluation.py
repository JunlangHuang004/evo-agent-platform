"""Regression tests for the reusable acceptance suite and evidence adapter."""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.evaluation import Check, evaluate_case, persist_result, summarize, timing
from backend.evaluation_platform import platform_record, platform_review
from backend.order_acceptance import ExactJsonVerifier, OrdersVerifier, read_summary
from backend.persistence import Repository
from benchmarks.acceptance import entries, recorded_artifacts


FIXTURES = Path(__file__).resolve().parents[1] / 'benchmarks' / 'fixtures' / 'acceptance'


class StubSandbox:
    """Report the expected verification receipt without executing any file."""

    def __call__(self, script, input_text):
        filename = ('expected_summary.json' if 'O001' in input_text else 'expected_review_summary.json')
        content = (FIXTURES / 'inputs' / filename).read_text()
        return read_summary(content), 'test-sandbox', None


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        _, self.cases = entries(FIXTURES / 'suite.json', 'cases', 'caseId')
        _, self.runs = entries(FIXTURES / 'runs.json', 'runs', 'caseId')

    def assess(self, run, checker=None):
        case = self.cases[run['caseId']]
        checker = checker or OrdersVerifier(case, FIXTURES, 'test-image')
        with patch.object(checker, 'rerun', StubSandbox()):
            return evaluate_case(case, run, recorded_artifacts(run, FIXTURES), checker)

    def test_revision_is_counted_once_and_previous_evidence_is_excluded(self):
        run = deepcopy(self.runs['orders-basic'])
        result = self.assess(run)
        self.assertEqual(result['goalVerdict'], 'pass')
        self.assertEqual(len(result['tasks']), 4)
        self.assertEqual(len(result['taskRevisionHistory']), 5)
        self.assertEqual(result['feedback'][0]['revision'], 1)
        self.assertEqual(result['feedback'][0]['stage'], 'final')

        run['evidence']['review']['target']['revision'] = 0
        result = self.assess(run)
        self.assertEqual(result['checks'][2]['verdict'], 'unassessed')
        self.assertEqual(result['checks'][3]['verdict'], 'unassessed')

    def test_static_summary_does_not_replace_docker_rerun(self):
        case = self.cases['orders-zero-bug']
        run = self.runs['orders-zero-bug']
        verifier = OrdersVerifier(case, FIXTURES, 'test-image')
        wrong = dict(verifier.main_expected, valid_rows=3, invalid_amount_rows=2)
        with patch.object(verifier, 'rerun', return_value=(wrong, 'test-sandbox', None)):
            result = evaluate_case(case, run, recorded_artifacts(run, FIXTURES), verifier)
        self.assertEqual(result['goalVerdict'], 'fail')
        self.assertEqual(result['checks'][1]['errorCode'], 'rerun_mismatch')
        self.assertIn('test-sandbox', result['checks'][1]['evidenceIds'])

    def test_mismatched_hash_and_unavailable_sandbox_never_settle_memory(self):
        run = deepcopy(self.runs['orders-basic'])
        run['evidence']['script']['sha256'] = '0' * 64
        result = self.assess(run)
        self.assertEqual(result['checks'][1]['verdict'], 'unassessed')
        self.assertEqual(result['feedback'], [])

        run = self.runs['orders-basic']
        case = self.cases[run['caseId']]
        verifier = OrdersVerifier(case, FIXTURES, 'test-image')
        error = ('unassessed', 'sandbox_unavailable', 'Docker is unavailable.')
        with patch.object(verifier, 'rerun', return_value=(None, None, error)):
            result = evaluate_case(case, run, recorded_artifacts(run, FIXTURES), verifier)
        self.assertEqual(result['checks'][1]['errorCode'], 'sandbox_unavailable')
        self.assertEqual(result['feedback'], [])

    def test_independent_check_requires_actual_review_input_and_research_receipt(self):
        run = deepcopy(self.runs['orders-basic'])
        run['evidence']['review']['inputSha256'] = '0' * 64
        run['evidence']['research'].pop('callId')
        result = self.assess(run)
        self.assertEqual([check['verdict'] for check in result['checks'][:3]],
                         ['unassessed', 'pass', 'unassessed'])

    def test_stop_cases_stay_in_denominator_and_planning_is_excluded(self):
        rows = [self.assess(self.runs[case]) for case in ('orders-basic', 'orders-timeout', 'orders-crash')]
        result = summarize(rows)
        self.assertEqual(result['overallGoals']['total'], 3)
        self.assertEqual(result['overallGoals']['failed'], 2)
        self.assertEqual(result['businessTasks']['total'], 5)
        self.assertEqual(result['fixedChecks']['total'], 12)
        self.assertEqual(result['humanWaitMs'], 6000)
        self.assertEqual(rows[2]['tasks'], [])

    def test_overlapping_waits_are_counted_once_and_open_waits_stop_at_deadline(self):
        waited = [{'startedAtMs': 3, 'endedAtMs': 5}, {'startedAtMs': 4, 'endedAtMs': 7},
                  {'startedAtMs': 8, 'endedAtMs': None}]
        self.assertEqual(timing({'startedAtMs': 4, 'deadlineAtMs': 10, 'humanWaits': waited}), (6, 5))

    def test_exact_json_and_storage_use_the_existing_repository(self):
        run = deepcopy(self.runs['orders-basic'])
        case = dict(self.cases[run['caseId']], verifier='exact-json', outputName='summary.json')
        run['evidence']['output'] = run['evidence']['summary']
        result = evaluate_case(case, run, recorded_artifacts(run, FIXTURES),
                               ExactJsonVerifier(case, FIXTURES, 'unused'))
        self.assertEqual(result['goalVerdict'], 'pass')
        self.assertEqual(summarize([result])['businessTasks']['passed'], 1)
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory))
            persist_result(repo, 'suite', 'batch', result)
            repo.close()
            repo = Repository(Path(directory))
            self.addCleanup(repo.close)
            item, = repo.evaluation_runs()
            self.assertEqual(item['status'], 'passed')
            self.assertEqual(item['verifier'], {'type': 'exact-json', 'passed': True})
            self.assertEqual(item['elapsedSec'], 2.6)

    def test_fixed_checks_and_task_counts_are_independent(self):
        case = self.cases['orders-basic']
        run = self.runs['orders-basic']
        verifier = SimpleNamespace(check_ids=('first', 'second'))
        verifier.evaluate = lambda state: {
            'first': Check('pass', 'First deliverable matched.', ('t-e',)),
            'second': Check('pass', 'Second deliverable matched.', ('t-e',)),
        }
        row = evaluate_case(case, run, recorded_artifacts(run, FIXTURES), verifier)
        result = summarize([row])
        self.assertEqual(result['fixedChecks']['passed'], 2)
        self.assertEqual(result['businessTasks']['passed'], 1)
        self.assertEqual(result['businessTasks']['unassessed'], 3)


class PlatformEvidenceTests(unittest.TestCase):
    def test_call_arguments_grants_and_output_order_are_verified(self):
        target = dict(taskId='exec', revision=1, artifactId='script', sha256='script-sha')
        ref = dict(taskId='review', revision=0, target=target, callId='test', inputArtifactId='input',
                   result={'artifactId': 'output'})
        call = dict(id='test', taskId='review', agentId='reviewer', tool='code.test', status='success',
                    reviewTargets={'exec': dict(targetRevision=1, artifactHashes={'script': 'script-sha'})},
                    result=dict(ok=True, exitCode=0, sandboxId='docker'),
                    args=dict(path='imports/script/summarize_orders.py',
                              args=['/imports/input/review_orders.csv', 'review_summary.json']))

        def grant(artifact_id, filename):
            return {'type': 'artifact_granted', 'payload': {
                'artifactId': artifact_id, 'toAgent': 'reviewer',
                'grant': dict(path=f'{artifact_id}/{filename}', readOnly=True, sha256=artifact_id + '-sha')}}

        grants = [grant('script', 'summarize_orders.py'), grant('input', 'review_orders.csv')]
        done = {'type': 'tool_finished', 'payload': {'callId': 'test'}}
        output = {'type': 'artifact_registered', 'payload': {'artifactId': 'output'}}
        artifacts = {'input': dict(name='review_orders.csv', runId='group-run', sha256='input-sha', ownerAgentId='reviewer')}
        evidence = platform_review(ref, {'test': call}, [*grants, done, output], artifacts, 'group-run')
        self.assertEqual(evidence['status'], 'success')
        self.assertEqual(evidence['inputSha256'], 'input-sha')
        for events in ([grants[0], done, output], [*grants, output, done]):
            self.assertIsNone(platform_review(ref, {'test': call}, events, artifacts, 'group-run')['status'])
        call['args']['args'][0] = 'orders.csv'
        self.assertIsNone(platform_review(ref, {'test': call}, [*grants, done, output], artifacts, 'group-run')['status'])

    def test_run_status_and_business_tasks_come_from_persisted_run(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory))
            try:
                repo.save_run({'id': 'saved', 'startedAt': 500, 'finishedAt': 700,
                               'status': 'failed', 'error': {'code': 'runtime', 'message': 'Worker exited.'},
                               'steps': [{'id': 'plan', 'revision': 0, '_planning': True},
                                         {'id': 'actual', 'revision': 0, 'status': 'failed'}]}, 'group')
                binding = dict(runId='saved', caseId='case', status='done', tasks=[])
                result = platform_record(binding, repo, None)
            finally:
                repo.close()
        self.assertEqual(result['status'], 'crashed')
        self.assertEqual([task['taskId'] for task in result['tasks']], ['actual'])
        self.assertEqual(timing(result), (200, 0))


if __name__ == '__main__':
    unittest.main()
