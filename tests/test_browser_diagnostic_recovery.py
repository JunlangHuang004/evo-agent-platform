"""Recovery policy over retained real failed browser reports, without model calls."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.browser_diagnostic_recovery import (
    browser_diagnostic_recovery, current_browser_diagnostic_checkpoint,
)
from backend.browser_execution_diagnostics import browser_execution_failure_series
from backend.feedback_context import latest_feedback_proof


FIXTURE = Path(__file__).parent / 'fixtures' / 'r11_browser_execution_diagnostics.json'


def fixture(kind='page_errors'):
    data = json.loads(FIXTURE.read_text())[kind]
    run, action = data['run'], data['action']
    task = next(t for t in run['steps'] if t['id'] == data['task']['id'])
    agents = {'qa': {'capabilities': ['review']}, 'dev': {'capabilities': ['coding']}}
    return run, task, action, agents


def model_response(run, task, action):
    index = len(run['calls'])
    call = {'id': 'model-' + str(index), 'tool': 'llm.chat', 'status': 'success',
            'runId': run['id'], 'taskId': task['id'], 'agentId': task['agentId'], 'revision': task['revision']}
    row = {'id': 'response-' + str(index), 'runId': run['id'], 'taskId': task['id'],
           'agentId': task['agentId'], 'callId': call['id'], 'role': 'agent',
           'kind': 'model_response', 'text': json.dumps(action)}
    run['calls'].append(call)
    run['transcript'].append(row)
    return call, row


def record_feedback(run, task, action, agents):
    call, _ = model_response(run, task, action)
    feedback = browser_diagnostic_recovery(run, task, action, agents)
    if feedback is None:
        raise AssertionError('Fixture should have a current real diagnostic series')
    identifier = call['id']
    row = {'id': 'receipt-' + identifier, 'runId': run['id'], 'taskId': task['id'],
           'agentId': task['agentId'], 'role': 'system', 'kind': 'browser_failure_recovery',
           'text': json.dumps(feedback, ensure_ascii=False)}
    event = {'id': 'event-' + identifier, 'type': 'browser_failure_recovery',
             'actor': task['agentId'], 'agentId': task['agentId'], 'payload': {
                 'taskId': task['id'], 'observedCallIds': feedback['observedFailures']['callIds'],
                 'failureSignature': feedback['failureSignature'], 'recovery': copy.deepcopy(feedback['recovery'])}}
    run['transcript'].append(row)
    run['events'].append(event)
    return feedback


def close_checkpoint(run, task, action, agents):
    for _ in range(3):
        feedback = record_feedback(run, task, action, agents)
    task['_browserDiagnosticClosing'] = {'proposal': copy.deepcopy(action),
                                         'failureSignature': feedback['failureSignature']}
    return feedback


def add_review_target(run, task):
    manifest = run['calls'][-1]['result']['browser']['sourceManifest']
    artifact = {'id': 'current-product', 'runId': run['id'], 'taskId': 'producer',
                'ownerAgentId': 'dev', 'sha256': manifest[0]['sha256']}
    target = {'id': 'producer', 'agentId': 'dev', 'revision': 0, 'status': 'done', 'outputs': [artifact['id']]}
    run['steps'].append(target)
    run['artifacts'] = [artifact]
    task.update(dependsOn=['producer'], reviewOf=['producer'])
    snapshot = {'targetRevision': 0, 'artifactHashes': {artifact['id']: artifact['sha256']}}
    for call in run['calls']:
        if call['tool'] == 'code.test':
            call['reviewTargets'] = {'producer': copy.deepcopy(snapshot)}
    return target, artifact


class BrowserDiagnosticRecoveryTests(unittest.TestCase):
    def test_retained_real_reports_return_unchanged_diagnosis_without_execution_or_mutation(self):
        for kind in ('page_errors', 'step_execution_error'):
            with self.subTest(kind=kind):
                run, task, action, agents = fixture(kind)
                before = copy.deepcopy((run, task, action, agents))
                expected = browser_execution_failure_series(run, task, action)
                result = browser_diagnostic_recovery(run, task, action, agents)
                self.assertFalse(result['ok'])
                self.assertTrue(result['notExecuted'])
                self.assertFalse(result['actionExecuted'])
                for key in ('failureSignature', 'observedFailures', 'diagnostic', 'sourceManifest'):
                    self.assertEqual(result[key], expected[key])
                self.assertEqual(result['observedFailures']['count'], 2)
                self.assertEqual(result['recovery'], {'kind': 'browser_execution_diagnostic',
                    'attempt': 1, 'priorFeedbacks': 0, 'requireFinal': False})
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))), 10000)
                self.assertNotIn('stopTask', result['recovery'])
                self.assertEqual((run, task, action, agents), before)

    def test_two_real_feedbacks_then_third_requires_real_final_and_does_not_inflate_tests(self):
        run, task, action, agents = fixture()
        feedbacks = [record_feedback(run, task, action, agents) for _ in range(3)]
        self.assertEqual([r['recovery']['attempt'] for r in feedbacks], [1, 2, 3])
        self.assertEqual([r['recovery']['requireFinal'] for r in feedbacks], [False, False, True])
        self.assertEqual(len([c for c in run['calls'] if c['tool'] == 'code.test']), 2)
        self.assertEqual(task['status'], 'running')
        self.assertNotIn('final', task)
        self.assertEqual(len(run['events']), 3)
        self.assertIn('next response must be an explicit final', feedbacks[-1]['instruction'])
        self.assertIn('No further action', feedbacks[-1]['instruction'])

    def test_read_activity_does_not_reset_the_same_source_recovery(self):
        run, task, action, agents = fixture('step_execution_error')
        record_feedback(run, task, action, agents)
        run['calls'].append({'id': 'read', 'tool': 'file.read', 'taskId': task['id'],
            'agentId': task['agentId'], 'revision': 0, 'status': 'failed',
            'result': {'ok': False, 'error': {'code': 'not_found'}}})
        self.assertEqual(browser_diagnostic_recovery(run, task, action, agents)['recovery']['attempt'], 2)

    def test_standalone_forged_event_or_user_prefix_cannot_spend_corrective_feedbacks(self):
        for variant in ('event_only', 'user_role', 'missing_model_call', 'changed_proposal', 'unknown_model'):
            with self.subTest(variant=variant):
                run, task, action, agents = fixture()
                record_feedback(run, task, action, agents)
                if variant == 'event_only':
                    run['transcript'].clear()
                elif variant == 'user_role':
                    run['transcript'][-1]['role'] = 'user'
                elif variant == 'missing_model_call':
                    run['calls'].pop()
                elif variant == 'changed_proposal':
                    run['transcript'][-2]['text'] = json.dumps({'type': 'final', 'text': 'Invented receipt'})
                else:
                    run['calls'][-1]['status'] = 'unknown'
                self.assertEqual(browser_diagnostic_recovery(run, task, action, agents)['recovery']['attempt'], 1)

    def test_foreign_actor_revision_signature_and_other_recovery_kind_are_not_counted(self):
        mutations = {
            'event_actor': lambda r: r['events'][0].update(actor='other'),
            'event_run': lambda r: r['events'][0].update(runId='foreign'),
            'event_task': lambda r: r['events'][0]['payload'].update(taskId='other'),
            'event_signature': lambda r: r['events'][0]['payload'].update(failureSignature='0' * 64),
            'event_kind': lambda r: r['events'][0]['payload']['recovery'].update(kind='other'),
            'transcript_run': lambda r: r['transcript'][-1].update(runId='foreign'),
            'transcript_agent': lambda r: r['transcript'][-1].update(agentId='other'),
            'model_revision': lambda r: r['calls'][-1].update(revision=1),
            'model_bool_revision': lambda r: r['calls'][-1].update(revision=False),
            'model_owner': lambda r: r['calls'][-1].update(agentId='other'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                run, task, action, agents = fixture()
                record_feedback(run, task, action, agents)
                mutate(run)
                self.assertEqual(browser_diagnostic_recovery(run, task, action, agents)['recovery']['attempt'], 1)

    def test_tampered_feedback_or_skipped_attempt_cannot_force_final(self):
        for field, value in (('revision', False), ('observedFailures', {'count': 99}),
                             ('diagnostic', {'cause': 'invented'}),
                             ('recovery', {'kind': 'browser_execution_diagnostic',
                                           'attempt': 3, 'priorFeedbacks': 2, 'requireFinal': True})):
            run, task, action, agents = fixture()
            record_feedback(run, task, action, agents)
            payload = json.loads(run['transcript'][-1]['text'])
            payload[field] = value
            run['transcript'][-1]['text'] = json.dumps(payload)
            if field == 'recovery':
                run['events'][0]['payload']['recovery'] = copy.deepcopy(value)
            self.assertEqual(browser_diagnostic_recovery(run, task, action, agents)['recovery']['attempt'], 1)

    def test_duplicate_receipts_or_ids_never_count_as_another_model_decision(self):
        for variant in ('same_model', 'duplicate_event_id', 'duplicate_transcript_id'):
            with self.subTest(variant=variant):
                run, task, action, agents = fixture()
                record_feedback(run, task, action, agents)
                if variant == 'same_model':
                    row = copy.deepcopy(run['transcript'][-1]); row['id'] += '-copy'
                    event = copy.deepcopy(run['events'][-1]); event['id'] += '-copy'
                    payload = json.loads(row['text'])
                    payload['recovery'].update(attempt=2, priorFeedbacks=1)
                    row['text'] = json.dumps(payload); event['payload']['recovery'] = payload['recovery']
                    run['transcript'].append(row); run['events'].append(event)
                    expected = 2
                elif variant == 'duplicate_event_id':
                    run['events'].append(copy.deepcopy(run['events'][0])); expected = 1
                else:
                    run['transcript'].append(copy.deepcopy(run['transcript'][-1])); expected = 1
                self.assertEqual(browser_diagnostic_recovery(run, task, action, agents)['recovery']['attempt'], expected)

    def test_checkpoint_requires_actual_third_receipt_not_just_a_marker(self):
        run, task, action, agents = fixture()
        result = record_feedback(run, task, action, agents)
        task['_browserDiagnosticClosing'] = {'proposal': copy.deepcopy(action), 'failureSignature': result['failureSignature']}
        self.assertIsNone(current_browser_diagnostic_checkpoint(run, task, agents))
        record_feedback(run, task, action, agents)
        self.assertIsNone(current_browser_diagnostic_checkpoint(run, task, agents))
        record_feedback(run, task, action, agents)
        self.assertTrue(current_browser_diagnostic_checkpoint(run, task, agents)['recovery']['requireFinal'])

    def test_checkpoint_is_readonly_survives_real_final_generation_and_never_counts_it_again(self):
        run, task, action, agents = fixture()
        feedback = close_checkpoint(run, task, action, agents)
        model_response(run, task, {'type': 'final', 'status': 'partial', 'text': 'Actual retained gaps.'})
        before = copy.deepcopy(run)
        for _ in range(3):
            current = current_browser_diagnostic_checkpoint(run, task, agents)
            self.assertEqual(current, feedback)
            self.assertEqual(current['recovery']['attempt'], 3)
        self.assertEqual(run, before)

    def test_changed_source_revision_or_unknown_execution_invalidates_checkpoint(self):
        for variant in ('revision', 'write', 'unknown', 'marker_proposal', 'marker_signature'):
            with self.subTest(variant=variant):
                run, task, action, agents = fixture()
                close_checkpoint(run, task, action, agents)
                if variant == 'revision':
                    task['revision'] += 1
                elif variant == 'marker_proposal':
                    task['_browserDiagnosticClosing']['proposal']['args']['path'] = 'other.html'
                elif variant == 'marker_signature':
                    task['_browserDiagnosticClosing']['failureSignature'] = '0' * 64
                else:
                    run['calls'].append({'id': 'new-effect', 'tool': 'file.write', 'taskId': task['id'],
                        'agentId': task['agentId'], 'revision': 0, 'status': 'unknown' if variant == 'unknown' else 'success',
                        'result': {'ok': True}})
                self.assertIsNone(current_browser_diagnostic_checkpoint(run, task, agents))

    def test_upstream_target_revision_or_artifact_change_invalidates_unchanged_reviewer_checkpoint(self):
        for variant in ('revision', 'artifact'):
            run, task, action, agents = fixture()
            target, artifact = add_review_target(run, task)
            close_checkpoint(run, task, action, agents)
            self.assertIsNotNone(current_browser_diagnostic_checkpoint(run, task, agents))
            if variant == 'revision':
                target['revision'] += 1
            else:
                artifact['sha256'] = 'f' * 64
            self.assertEqual(task['revision'], 0)
            self.assertIsNone(current_browser_diagnostic_checkpoint(run, task, agents))

    def test_changed_test_is_left_to_existing_execution_validation(self):
        run, task, action, agents = fixture('step_execution_error')
        close_checkpoint(run, task, action, agents)
        action['args']['checks'][1]['selector'] = '[data-item="actual-source-supported-selector"]'
        self.assertIsNone(browser_diagnostic_recovery(run, task, action, agents))

    def test_current_declared_independent_target_gets_real_rework_route_without_grants(self):
        run, task, action, agents = fixture()
        target, _ = add_review_target(run, task)
        result = browser_diagnostic_recovery(run, task, action, agents)
        self.assertEqual(result['reviewTargets'], [{'taskId': target['id'], 'agentId': 'dev', 'revision': 0}])
        self.assertIn('actual review with verdict rework', result['instruction'])
        self.assertIn('top-level taskId', result['instruction'])
        self.assertIn('not proof that this test covers', result['instruction'])
        self.assertNotIn('grant_artifact', result['instruction'])
        self.assertEqual(run['events'], [])

    def test_missing_foreign_self_undeclared_or_stale_targets_are_not_rework_advice(self):
        for variant in ('missing', 'undeclared', 'not_dependency', 'self_owner', 'stale_revision', 'hash', 'foreign_artifact', 'no_capability'):
            with self.subTest(variant=variant):
                run, task, action, agents = fixture()
                target, artifact = add_review_target(run, task)
                if variant == 'missing':
                    run['calls'][-1].pop('reviewTargets')
                elif variant == 'undeclared':
                    task['reviewOf'] = ['other']
                elif variant == 'not_dependency':
                    task['dependsOn'] = []
                elif variant == 'self_owner':
                    target['agentId'] = task['agentId']
                elif variant == 'stale_revision':
                    target['revision'] += 1
                elif variant == 'hash':
                    artifact['sha256'] = '0' * 64
                elif variant == 'foreign_artifact':
                    artifact['runId'] = 'foreign'
                else:
                    agents['qa']['capabilities'] = []
                result = browser_diagnostic_recovery(run, task, action, agents)
                self.assertTrue(result is None or result['reviewTargets'] == [])
                if result:
                    self.assertNotIn('actual review with verdict rework', result['instruction'])

    def test_developer_has_no_downstream_qa_wait_or_automatic_candidate(self):
        run, task, action, agents = fixture('step_execution_error')
        run['steps'].append({'id': 'downstream', 'agentId': 'qa', 'revision': 0,
                             'status': 'pending', 'dependsOn': [task['id']]})
        result = browser_diagnostic_recovery(run, task, action, agents)
        self.assertEqual(result['reviewTargets'], [])
        self.assertIn('Fix your own selector', result['instruction'])
        self.assertIn('explicitly publish usable files', result['instruction'])
        self.assertIn('do not invent a target or wait for downstream QA', result['instruction'])
        self.assertNotIn('deliveries', run)

    def test_finished_foreign_or_malformed_scope_is_not_intercepted(self):
        for variant in ('finished_task', 'finished_run', 'foreign_task', 'foreign_run', 'bad_owner', 'bool_revision'):
            run, task, action, agents = fixture()
            if variant == 'finished_task':
                task['status'] = 'done'
            elif variant == 'finished_run':
                run['status'] = 'partial'
            elif variant == 'foreign_task':
                task = {**task, 'dependsOn': ['invented']}
            elif variant == 'foreign_run':
                run['calls'][0]['runId'] = 'foreign'
            elif variant == 'bad_owner':
                task['agentId'] = []
            else:
                task['revision'] = False
            self.assertIsNone(browser_diagnostic_recovery(run, task, action, agents))
            self.assertIsNone(current_browser_diagnostic_checkpoint(run, task, agents))

    def test_other_task_human_wait_does_not_disable_running_task_diagnostics(self):
        run, task, action, agents = fixture()
        run['status'] = 'waiting_human'
        run['steps'].append({'id': 'parallel', 'agentId': 'dev', 'revision': 0,
                             'status': 'waiting_human', 'pendingApprovalId': 'actual-other-approval'})
        self.assertIsNotNone(browser_diagnostic_recovery(run, task, action, agents))
        close_checkpoint(run, task, action, agents)
        self.assertIsNotNone(current_browser_diagnostic_checkpoint(run, task, agents))
        task['status'] = 'waiting_human'
        self.assertIsNone(browser_diagnostic_recovery(run, task, action, agents))
        self.assertIsNone(current_browser_diagnostic_checkpoint(run, task, agents))

    def test_feedback_uses_existing_host_proof_and_is_superseded_by_next_model_response(self):
        run, task, action, agents = fixture()
        record_feedback(run, task, action, agents)
        proof = latest_feedback_proof(run, task)
        self.assertIsNotNone(proof)
        self.assertIn('浏览器测试失败恢复：', proof.messages[-1][1])
        model_response(run, task, {'type': 'tool', 'name': 'file.read', 'args': {'path': 'authorized.html'}})
        self.assertIsNone(latest_feedback_proof(run, task))

    def test_oversize_diagnostic_is_declined_instead_of_silently_cutting_evidence(self):
        run, task, action, agents = fixture()
        series = browser_execution_failure_series(run, task, action)
        series['diagnostic']['pageErrors'] = [{'message': 'x' * 12000}]
        with patch('backend.browser_diagnostic_recovery.browser_execution_failure_series', return_value=series):
            self.assertIsNone(browser_diagnostic_recovery(run, task, action, agents))


if __name__ == '__main__':
    unittest.main()
