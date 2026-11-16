"""R12 recorded assertion exceptions and bounded recovery, not model evaluation."""
import copy
import json
import os
from pathlib import Path
import unittest

from backend.browser_execution_diagnostics import (
    browser_execution_failure_series, current_browser_execution_diagnostic,
)
from backend.browser_failure_recovery import browser_failure_feedback
from backend.browser_diagnostic_recovery import (
    browser_diagnostic_recovery, current_browser_diagnostic_checkpoint,
)
from tests.test_browser_diagnostic_recovery import record_feedback
from tests.test_software_diagnostic_handoff import Harness, CONTRACT, final, publish, write


def fixture():
    data = json.loads((Path(__file__).parent / 'fixtures/r12_assertion_execution_timeout.json').read_text())
    run = data['run']
    task = next(t for t in run['steps'] if t['id'] == data['task']['id'])
    return run, task, data['action'], {'dev': {'capabilities': ['coding']}}


class AssertionExceptionRecoveryTests(unittest.TestCase):
    def test_real_r12_shape_is_execution_error_not_completed_comparison(self):
        run, task, action, agents = fixture(); before = copy.deepcopy(run)
        report = run['calls'][-1]['result']['browser']
        self.assertNotIn('passed', report['steps'][-1])
        self.assertNotIn('actual', report['assertions'][-1])
        self.assertIs(report['assertions'][-1]['passed'], False)
        self.assertIsNotNone(browser_failure_feedback(run, task, action), 'Original unbounded guard matched this shape.')
        diagnostic = current_browser_execution_diagnostic(run, task)
        self.assertEqual(diagnostic['kind'], 'step_execution_error')
        self.assertFalse(diagnostic['failedStep']['comparisonCompleted'])
        self.assertFalse(diagnostic['failedStep']['actualRecorded'])
        self.assertEqual(diagnostic['failedStep']['expected'], 'Alice')
        self.assertEqual(diagnostic['cause'], 'unassessed')
        self.assertFalse(diagnostic['acceptanceCreated'])
        self.assertEqual(browser_execution_failure_series(run, task, action)['observedFailures']['count'], 2)
        self.assertEqual(run, before)

    def test_two_diagnostic_opportunities_then_real_final_checkpoint(self):
        run, task, action, agents = fixture()
        feedback = [record_feedback(run, task, action, agents) for _ in range(3)]
        self.assertEqual([f['recovery']['requireFinal'] for f in feedback], [False, False, True])
        self.assertEqual([f['observedFailures']['count'] for f in feedback], [2, 2, 2])
        task['_browserDiagnosticClosing'] = {'proposal': copy.deepcopy(action), 'failureSignature': feedback[-1]['failureSignature']}
        self.assertTrue(current_browser_diagnostic_checkpoint(run, task, agents)['recovery']['requireFinal'])
        self.assertEqual(task['status'], 'running', 'Pure policy never manufactures completion.')
        self.assertEqual(len([c for c in run['calls'] if c['tool'] == 'code.test']), 2)

    def test_one_real_failure_is_diagnosable_but_not_a_repeated_failure(self):
        run, task, action, agents = fixture(); run['calls'] = run['calls'][-1:]
        self.assertIsNotNone(current_browser_execution_diagnostic(run, task))
        self.assertIsNone(browser_diagnostic_recovery(run, task, action, agents))

    def test_normal_assertion_value_failure_stays_on_existing_guard(self):
        run, task, action, agents = fixture()
        for call in run['calls']:
            report = call['result']['browser']
            report['steps'][-1].update(passed=False, actual='Other', error='Assertion failed')
            report['assertions'][-1].update(actual='Other'); report['assertions'][-1].pop('error')
        self.assertIsNotNone(browser_failure_feedback(run, task, action))
        self.assertIsNone(browser_execution_failure_series(run, task, action))

    def test_exception_assertion_receipt_must_match_actual_step_and_counts(self):
        for variant in ('missing_error', 'error_changed', 'passed_true', 'actual_added', 'null_actual_added', 'expected_changed', 'count', 'missing_assertion'):
            with self.subTest(variant=variant):
                run, task, action, agents = fixture(); report = run['calls'][-1]['result']['browser']
                if variant == 'missing_error': report['steps'][-1].pop('error')
                elif variant == 'error_changed': report['assertions'][-1]['error'] = 'Different exception'
                elif variant == 'passed_true': report['assertions'][-1]['passed'] = True
                elif variant == 'actual_added': report['assertions'][-1]['actual'] = 'Alice'
                elif variant == 'null_actual_added': report['assertions'][-1]['actual'] = None
                elif variant == 'expected_changed': report['assertions'][-1]['expected'] = 'Other'
                elif variant == 'count': report['assertionCount'] += 1
                else: report['assertions'].pop(); report['assertionCount'] -= 1
                self.assertIsNone(current_browser_execution_diagnostic(run, task))
                self.assertIsNone(browser_execution_failure_series(run, task, action))

    def test_foreign_revision_unknown_and_mutation_do_not_reuse_failures(self):
        for variant in ('foreign_run', 'owner', 'revision', 'unknown', 'mutation', 'source_changed'):
            with self.subTest(variant=variant):
                run, task, action, agents = fixture(); latest = run['calls'][-1]
                if variant == 'foreign_run': latest['runId'] = 'different'
                elif variant == 'owner': latest['result']['isolation']['ownerAgentId'] = 'other'
                elif variant == 'revision': latest['revision'] += 1
                elif variant == 'unknown': run['calls'][0]['result']['browser']['outcomeUnknown'] = True
                elif variant == 'mutation': run['calls'].append({'id': 'mutation', 'tool': 'file.write', 'agentId': task['agentId'], 'taskId': task['id'], 'revision': 0, 'status': 'success', 'result': {'ok': True}})
                else:
                    report = latest['result']['browser']; path = report['entry']['path']
                    report['entry']['sha256'] = 'f' * 64
                    for items in (report['sourceManifest'], report['testedFiles']):
                        for item in items:
                            if item['path'] == path: item['sha256'] = 'f' * 64
                self.assertIsNone(browser_execution_failure_series(run, task, action))

    def test_read_preserves_attempts_but_changed_setup_is_a_new_proposal(self):
        run, task, action, agents = fixture(); record_feedback(run, task, action, agents)
        run['calls'].append({'id': 'read', 'tool': 'file.read', 'agentId': task['agentId'], 'taskId': task['id'], 'revision': 0, 'status': 'success', 'result': {'ok': True}})
        self.assertEqual(browser_diagnostic_recovery(run, task, action, agents)['recovery']['attempt'], 2)
        changed = copy.deepcopy(action); changed['args']['checks'][-1]['selector'] = '#observed-current-view .booker'
        self.assertIsNone(browser_diagnostic_recovery(run, task, changed, agents))


def assert_text_test(selector='#missing'):
    return {'type': 'tool', 'name': 'code.test', 'args': {'language': 'browser', 'path': 'app.html',
        'checks': [{'action': 'assert_text', 'selector': selector, 'text': 'ready', 'exact': True}], 'timeout': 20}}


@unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS') == '1', 'Explicit local pinned Docker browser tests')
class AssertionExceptionPlatformTests(Harness):
    def test_actual_runner_exception_closes_after_bounded_feedback_with_candidate_preserved(self):
        body = '<!doctype html><p id="state">ready</p>'
        app, model, run = self.run_model([{'id': 'b', 'title': 'Build', 'agentId': 'executor', 'outputContract': copy.deepcopy(CONTRACT)}],
            {'Build': [write('app.html', body), publish('app.html'), *[assert_text_test() for _ in range(5)], final('partial')]})
        calls = self.actual_browser_calls(run); events = self.diagnostic_events(run)
        self.assertEqual(len(calls), 2); self.assertTrue(all(c['status'] == 'failed' for c in calls))
        self.assertNotIn('passed', calls[-1]['result']['browser']['steps'][-1])
        self.assertEqual([e['payload']['recovery']['requireFinal'] for e in events], [False, False, True])
        self.assertEqual(run['status'], 'partial'); self.assertEqual(run['steps'][-1]['closureReason'], 'browser_diagnostic_unresolved')
        self.assertEqual(len(run['deliveries']), 1); self.assertEqual(run['deliveries'][0]['status'], 'candidate')
        self.verify_zip(app, run, run['deliveries'][0], 'app.html', body)
        self.assertFalse(any(e['type'] == 'review_pass' for e in run['events']))

    def test_source_supported_selector_correction_retests_same_expected_value(self):
        body = '<!doctype html><p id="state">ready</p>'
        app, model, run = self.run_model([{'id': 'b', 'title': 'Build', 'agentId': 'executor', 'outputContract': copy.deepcopy(CONTRACT)}],
            {'Build': [write('app.html', body), publish('app.html'), assert_text_test(), assert_text_test(), assert_text_test(),
                {'type': 'tool', 'name': 'file.read', 'args': {'path': 'app.html'}}, assert_text_test('#state'), final()]})
        self.assertEqual(run['status'], 'done'); self.assertEqual(len(self.diagnostic_events(run)), 1)
        calls = self.actual_browser_calls(run)
        self.assertEqual([c['status'] for c in calls], ['failed', 'failed', 'success'])
        self.assertEqual({c['args']['checks'][0]['text'] for c in calls}, {'ready'})
        self.assertTrue(calls[-1]['result']['browser']['assertions'][0]['passed'])
        self.verify_zip(app, run, run['deliveries'][0], 'app.html', body)
