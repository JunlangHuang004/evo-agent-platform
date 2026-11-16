import copy
import hashlib
import unittest

from backend.partial_review import budget_partial_receipt, review_dependency_exceptions


class PartialReviewTests(unittest.TestCase):
    def setUp(self):
        content = '<!doctype html><button>Actual generated file</button>'
        digest = hashlib.sha256(content.encode()).hexdigest()
        self.producer = {'id': 'build', 'agentId': 'dev', 'revision': 0, 'status': 'partial',
                         'outputs': ['html'], 'error': {'code': 'budget', 'message': 'Turn limit reached.'}}
        self.reviewer = {'id': 'check', 'agentId': 'qa', 'dependsOn': ['build'],
                         'requiredCapabilities': ['review'], 'status': 'pending'}
        self.run = {'id': 'run-real', 'steps': [self.producer, self.reviewer],
                    'artifacts': [{'id': 'html', 'runId': 'run-real', 'taskId': 'build',
                                   'ownerAgentId': 'dev', 'path': 'app/index.html', 'name': 'index.html',
                                   'content': content, 'size': len(content.encode()), 'sha256': digest}],
                    'calls': [{'id': 'write-real', 'taskId': 'build', 'agentId': 'dev', 'revision': 0,
                               'tool': 'file.write', 'args': {'path': 'app/index.html'}, 'status': 'success',
                               'result': {'ok': True, 'sha256': digest}}]}
        self.agents = {'dev': {'capabilities': ['coding']}, 'qa': {'capabilities': ['review']},
                       'release': {'capabilities': ['release']}}

    def test_current_budget_limited_files_are_reviewable_without_mutating_or_passing_them(self):
        before = copy.deepcopy(self.run)
        receipt = review_dependency_exceptions(self.run, self.reviewer, self.agents)['build']
        self.assertEqual(receipt['acceptance'], 'unassessed')
        self.assertEqual(receipt['error']['code'], 'budget')
        self.assertEqual(self.run, before)
        receipt['error']['message'] = 'caller changed its copy'
        self.assertEqual(self.producer['error']['message'], 'Turn limit reached.')
        self.producer['status'] = 'failed'
        self.assertIsNotNone(budget_partial_receipt(self.run, self.producer))
        self.assertEqual(review_dependency_exceptions(self.run, self.reviewer, self.agents), {})

    def test_empty_stale_foreign_uncertain_and_nonbudget_outputs_are_not_promoted(self):
        changes = [lambda r: r['steps'][0].update(outputs=[]),
                   lambda r: r['steps'][0].update(error={'code': 'runtime'}),
                   lambda r: r['steps'][0].update(_pendingTool={'callId': 'unfinished'}),
                   lambda r: r['steps'][0].update(revision=1),
                   lambda r: r['artifacts'][0].update(content='changed'),
                   lambda r: r['artifacts'][0].update(runId='different'),
                   lambda r: r['artifacts'][0].update(ownerAgentId='qa'),
                   lambda r: r['artifacts'][0].update(sourceRunId='previous-run'),
                   lambda r: r['calls'][0].pop('revision'),
                   lambda r: r['calls'][0].update(status='running')]
        for change in changes:
            run = copy.deepcopy(self.run)
            change(run)
            with self.subTest(change=change):
                self.assertIsNone(budget_partial_receipt(run, run['steps'][0]))

    def test_later_mutations_or_another_task_at_same_path_invalidate_snapshot(self):
        for tool, result, task in [('file.delete', {'ok': True}, 'build'),
                                   ('file.write', {'ok': True, 'sha256': '0' * 64}, 'build'),
                                   ('file.read', self.run['calls'][0]['result'], 'other-task')]:
            run = copy.deepcopy(self.run)
            run['calls'].append({**run['calls'][0], 'id': 'later', 'tool': tool,
                                 'taskId': task, 'result': result})
            with self.subTest(tool=tool, task=task):
                self.assertIsNone(budget_partial_receipt(run, run['steps'][0]))
        run = copy.deepcopy(self.run)
        run['artifacts'].append({**run['artifacts'][0], 'id': 'replacement', 'taskId': 'other-task'})
        self.assertIsNone(budget_partial_receipt(run, run['steps'][0]))

    def test_review_exception_does_not_open_release_self_review_or_undeclared_inputs(self):
        for changes in ({'agentId': 'release'}, {'agentId': 'dev'}, {'requiredCapabilities': []},
                        {'dependsOn': []}, {'finalSummary': True}):
            consumer = {**self.reviewer, **changes}
            with self.subTest(changes=changes):
                self.assertEqual(review_dependency_exceptions(self.run, consumer, self.agents), {})
        pending = {'id': 'still-working', 'status': 'running', 'agentId': 'lead'}
        self.run['steps'].append(pending)
        self.reviewer['dependsOn'].append(pending['id'])
        # The scheduler must continue waiting on this normal dependency.
        self.assertEqual(set(review_dependency_exceptions(self.run, self.reviewer, self.agents)), {'build'})

    def test_code_can_mutate_source_even_when_it_fails_and_requires_reobservation(self):
        for status in ('success', 'failed', 'running', 'uncertain'):
            run = copy.deepcopy(self.run)
            run['calls'].append({'id': 'script', 'agentId': 'dev', 'taskId': 'build',
                                 'tool': 'code.run', 'status': status,
                                 'args': {'code': "open('app/index.html','w').write('changed')"},
                                 'result': {'ok': status == 'success'}})
            with self.subTest(status=status):
                self.assertIsNone(budget_partial_receipt(run, run['steps'][0]))
            if status in ('success', 'failed'):
                run['calls'].append({**run['calls'][0], 'id': 'fresh-read', 'tool': 'file.read'})
                self.assertIsNotNone(budget_partial_receipt(run, run['steps'][0]))

    def test_frozen_browser_test_does_not_make_source_stale(self):
        self.run['calls'].append({'id': 'browser', 'agentId': 'dev', 'taskId': 'build',
                                 'tool': 'code.test', 'status': 'success',
                                 'args': {'language': 'browser', 'path': 'app/index.html'},
                                 'result': {'ok': True, 'runtime': 'browser'}})
        self.assertIsNotNone(budget_partial_receipt(self.run, self.producer))

    def test_reconciled_or_failed_mutations_need_a_later_matching_observation(self):
        for status in ('reconciled', 'failed', 'unknown'):
            run = copy.deepcopy(self.run)
            run['calls'].append({**run['calls'][0], 'id': 'uncertain-write',
                                 'status': status, 'result': {'ok': False, 'reconciled': True}})
            self.assertIsNone(budget_partial_receipt(run, run['steps'][0]))
            run['calls'].append({**run['calls'][0], 'id': 'fresh-read', 'tool': 'file.read'})
            self.assertIsNotNone(budget_partial_receipt(run, run['steps'][0]))

    def test_malformed_or_unknown_receipts_fail_closed(self):
        changes = [lambda r: r['steps'][0].update(error='budget'),
                   lambda r: r['steps'][0].update(outputs=[{}]),
                   lambda r: r['calls'][0].update(args='app/index.html'),
                   lambda r: r['calls'][0].update(revision=False),
                   lambda r: r.update(calls=[None]),
                   lambda r: r.update(artifacts=[None]),
                   lambda r: r['artifacts'][0].update(size=True)]
        for change in changes:
            run = copy.deepcopy(self.run)
            change(run)
            with self.subTest(change=change):
                self.assertIsNone(budget_partial_receipt(run, run['steps'][0]))
        self.assertEqual(review_dependency_exceptions(self.run, {**self.reviewer, 'requiredCapabilities': None}, self.agents), {})
        self.assertEqual(review_dependency_exceptions(self.run, {**self.reviewer, 'agentId': []}, self.agents), {})


if __name__ == '__main__':
    unittest.main()
