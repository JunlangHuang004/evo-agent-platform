"""Personal-learning evidence gates with real SQLite, synthetic immutable receipts.

These are regression fixtures, not product QA results or measured model gains.
"""
from pathlib import Path
import copy
import hashlib
import json
import tempfile
import unittest

from backend.learning import LearningService
from backend.memory import MemoryService
from backend.persistence import Repository


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp.name))
        self.memory = MemoryService(self.repo)
        self.learning = LearningService(self.memory, 'software')
        self.sequence = 0

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def source(self, rule='read'):
        self.sequence += 1
        task = {'id': f'source-task-{self.sequence}', 'agentId': 'dev', 'revision': 0,
            'title': 'booking source workflow', 'goal': 'booking source workflow',
            'status': 'failed', 'error': {'code': 'budget'}, 'outputs': []}
        run = {'id': f'source-run-{self.sequence}', 'teamId': 'software', 'channelId': 'group',
            'status': 'failed', 'participants': ['dev', 'qa'], 'steps': [task], 'calls': []}
        if rule == 'read':
            run['calls'] = [self.read_call(f'read-{index}', task) for index in range(3)]
        else:
            for version in range(3):
                for path in ('app.js', 'index.html'):
                    run['calls'].append({'id': f'write-{version}-{path}', 'taskId': task['id'], 'agentId': 'dev', 'revision': 0,
                        'tool': 'file.write', 'status': 'success', 'args': {'path': path},
                        'result': {'ok': True, 'path': path, 'sha256': digest(path + str(version))}})
        return run, task

    @staticmethod
    def read_call(identifier, task, content='const privateSourceBody = 123;', path='app.js'):
        return {'id': identifier, 'taskId': task['id'], 'agentId': task['agentId'], 'revision': task['revision'],
            'tool': 'file.read', 'status': 'success', 'args': {'path': path},
            'result': {'ok': True, 'path': path, 'sha256': digest(content), 'content': content, 'mode': 'full'}}

    def candidate(self, rule='read'):
        run, task = self.source(rule)
        observed = self.learning.observe_failure('dev', run, task)
        self.assertIsNotNone(observed['candidate'])
        return observed['candidate'], run, task

    def trial_run(self):
        task = {'id': 'trial-task', 'agentId': 'dev', 'revision': 0, 'title': 'booking source workflow',
                'goal': 'booking source workflow', 'status': 'done', 'outputs': ['html', 'js']}
        reviewer = {'id': 'review-task', 'agentId': 'qa', 'revision': 0, 'status': 'done'}
        artifacts = [{'id': 'html', 'name': 'index.html', 'path': 'index.html', 'sha256': digest('<h1>booking</h1>'),
                      'ownerAgentId': 'dev', 'runId': 'trial-run', 'taskId': task['id']},
                     {'id': 'js', 'name': 'app.js', 'path': 'app.js', 'sha256': digest('const fixed = 1;'),
                      'ownerAgentId': 'dev', 'runId': 'trial-run', 'taskId': task['id']}]
        snapshot = {'targetRevision': 0, 'artifactHashes': {item['id']: item['sha256'] for item in artifacts}}
        task['validation'] = {'verdict': 'pass', 'verified': True, 'reviewerTaskId': reviewer['id'],
                              'evidenceCallIds': ['qa-browser'], **copy.deepcopy(snapshot)}
        calls = [self.read_call('current-read', task)]
        for artifact in artifacts:
            calls.append({'id': 'edit-' + artifact['id'], 'taskId': task['id'], 'agentId': 'dev', 'revision': 0,
                'tool': 'file.patch', 'status': 'success', 'args': {'path': artifact['name']},
                'result': {'ok': True, 'path': artifact['name'], 'sha256': artifact['sha256']}})
        calls.append({'id': 'own-test', 'taskId': task['id'], 'agentId': 'dev', 'revision': 0,
            'tool': 'code.test', 'status': 'success', 'result': {'ok': True, 'exitCode': 0, 'runtime': 'javascript'}})
        browser = {'ok': True, 'executed': True, 'entry': {'path': 'index.html', 'sha256': artifacts[0]['sha256']},
            'testedFiles': [{'path': item['name'], 'sha256': item['sha256']} for item in artifacts],
            'sourceManifest': [{'path': item['name'], 'sha256': item['sha256']} for item in artifacts],
            'assertions': [{'action': 'assert_count', 'passed': True}], 'assertionCount': 1, 'assertionsPassed': 1,
            'pageErrors': [], 'httpErrors': [], 'blockedRequests': []}
        calls[-1]['result'] = {'ok': True, 'exitCode': 0, 'runtime': 'browser', 'browser': copy.deepcopy(browser)}
        calls.append({'id': 'qa-browser', 'taskId': reviewer['id'], 'agentId': 'qa', 'revision': 0,
            'tool': 'code.test', 'status': 'success', 'reviewTargets': {task['id']: snapshot},
            'result': {'ok': True, 'exitCode': 0, 'runtime': 'browser', 'browser': browser}})
        run = {'id': 'trial-run', 'teamId': 'software', 'channelId': 'group', 'status': 'done',
               'participants': ['dev', 'qa'], 'steps': [task, reviewer], 'calls': calls, 'artifacts': artifacts}
        return run, task

    def settle(self, candidate, run, task, ack=True):
        self.offer(candidate, run, task)
        return self.learning.settle_trial(candidate['id'], 'dev', run, task,
            acknowledged_memory_ids=[candidate['id']] if ack else [])

    def offer(self, candidate, run, task):
        # Stage the prospective offer first; synthetic receipts represent later work.
        calls, status = run['calls'], task['status']
        run['calls'], task['status'] = [], 'running'
        try:
            return self.learning.begin_trial(candidate['id'], 'dev', run, task)
        finally:
            run['calls'], task['status'] = calls, status

    def test_failure_facts_and_candidates_are_separate_owner_only_and_do_not_copy_source_bodies(self):
        candidate, run, task = self.candidate()
        repeated = self.learning.observe_failure('dev', run, task)
        self.assertEqual(repeated['candidate']['id'], candidate['id'])
        self.assertEqual(len(self.memory.list('dev')), 2)
        context = self.learning.context('dev', 'booking source workflow')
        self.assertEqual(len(context['factualEpisodes']), 1)
        self.assertEqual(context['experimentalCandidates'][0]['verificationStatus'], 'unverified')
        self.assertEqual(context['verifiedStrategies'], [])
        self.assertNotIn('const privateSourceBody', json.dumps(context))
        self.assertIn('omitted/lost', candidate['lesson'])
        self.assertIn('unknown', candidate['learningBaseline']['bodyAvailabilityAtDecision'])
        other = self.memory.propose('qa', 'Private booking strategy', 'booking', 'other', 'other-run', [])
        self.memory.confirm(other['id'], 'qa', True, 'human:qa-confirm')
        self.memory.share(other['id'], 'qa', 'human:explicit-legacy-share')
        self.assertEqual(self.learning.context('dev', 'booking')['verifiedStrategies'], [])
        self.assertEqual(self.learning.context('qa', 'booking')['factualEpisodes'], [])

    def test_owner_team_channel_and_revision_are_strict(self):
        run, task = self.source()
        for owner, changed, channel in [('qa', run, 'group'), ('dev', {**run, 'teamId': 'fund'}, 'group'),
                ('dev', run, 'dev'), ('dev', {key: value for key, value in run.items() if key != 'channelId'}, 'group')]:
            with self.subTest(owner=owner, channel=channel), self.assertRaises(ValueError):
                self.learning.observe_failure(owner, changed, task, channel)
        with self.assertRaises(ValueError):
            self.learning.observe_failure('dev', run, {**task, 'revision': False})
        private = copy.deepcopy(run); private['channelId'] = 'dev'
        self.learning.observe_failure('dev', private, private['steps'][0], 'dev')
        self.assertEqual(self.learning.context('dev', 'booking', 'group')['factualEpisodes'], [])
        self.assertEqual(len(self.learning.context('dev', 'booking', 'dev')['factualEpisodes']), 1)

    def test_actual_window_and_sha_not_args_determine_repeat_count(self):
        run, task = self.source()
        for index, call in enumerate(run['calls']):
            call['args']['startLine'] = index + 1  # Ignored: actual result still same full file.
        self.assertIsNotNone(self.learning.observe_failure('dev', run, task)['candidate'])
        run, task = self.source()
        for index, call in enumerate(run['calls']):
            content = 'version' + str(index)
            call['result'].update(content=content, sha256=digest(content))
        self.assertIsNone(self.learning.observe_failure('dev', run, task)['candidate'])
        for call in run['calls']:
            call.pop('revision')
        self.assertEqual(self.learning.observe_failure('dev', run, task)['episode']['learningFacts']['readCount'], 0)

    def test_trial_requires_new_binding_and_survives_sqlite_restart_idempotently(self):
        candidate, source, original = self.candidate()
        with self.assertRaises(ValueError):
            self.learning.begin_trial(candidate['id'], 'dev', source, original)
        run, task = self.trial_run()
        with self.assertRaises(ValueError):
            self.learning.begin_trial(candidate['id'], 'dev', run, task)
        offered = self.offer(candidate, run, task)
        self.repo.close(); self.repo = Repository(Path(self.temp.name))
        self.memory = MemoryService(self.repo); self.learning = LearningService(self.memory, 'software')
        self.assertEqual(self.learning.begin_trial(candidate['id'], 'dev', run, task), offered)
        with self.assertRaises(KeyError):
            self.learning.begin_trial(candidate['id'], 'qa', run, run['steps'][1])
        self.assertEqual(len(self.repo.get_memory(candidate['id'], 'dev')['learningTrials']), 1)

    def test_explicit_ack_is_required_even_if_observed_workflow_and_review_pass(self):
        candidate, _, _ = self.candidate()
        run, task = self.trial_run()
        observed = self.settle(candidate, run, task, ack=False)
        self.assertEqual(observed['assessment'], 'unassessed')
        self.assertTrue(observed['ruleBehaviorObserved'])
        self.assertFalse(observed['adoptionProven'])
        self.assertFalse(self.repo.get_memory(candidate['id'], 'dev')['verified'])

    def test_learning_context_has_a_total_json_budget_and_omits_whole_oversized_entries(self):
        candidate, _, _ = self.candidate()
        huge = self.memory.propose('dev', 'booking ' + 'full-condition ' * 500, 'booking', 'large-task', 'large-run', [])
        self.memory.confirm(huge['id'], 'dev', True, 'human:large-rule')
        context = self.learning.context('dev', 'booking source workflow')
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 2400)
        self.assertGreaterEqual(context['omitted']['verifiedStrategies']['count'], 1)
        self.assertIn(huge['id'], context['omitted']['verifiedStrategies']['memoryIds'])
        suggestions = context['experimentalCandidates']
        self.assertEqual(suggestions[0]['memoryId'], candidate['id'])
        self.assertEqual(suggestions[0]['lesson'], candidate['lesson'], 'do not shorten applicability text to fit')
        tighter = self.learning.context('dev', 'booking source workflow', max_chars=1200)
        self.assertLessEqual(len(json.dumps(tighter, ensure_ascii=False)), 1200)
        self.assertEqual(json.loads(json.dumps(tighter)), tighter)

    def test_only_current_specific_independent_browser_evidence_can_confirm(self):
        changes = ('raw_done', 'python_only', 'stale_revision', 'stale_file', 'missing_qa_revision',
                   'bool_target_revision', 'bool_qa_call_revision', 'bool_review_snapshot', 'boolean_count', 'page_error', 'no_necessary_read', 'unpublished_edit', 'other_unpublished_html', 'extra_unpublished_css')
        for change in changes:
            with self.subTest(change=change):
                candidate, _, _ = self.candidate()
                run, task = self.trial_run()
                run['id'] += '-' + change
                for artifact in run['artifacts']:
                    artifact['runId'] = run['id']
                if change == 'raw_done':
                    task.pop('validation')
                elif change == 'python_only':
                    run['calls'][-1]['result'] = {'ok': True, 'exitCode': 0, 'runtime': 'python', 'stdout': 'all passed'}
                elif change == 'stale_revision':
                    task['validation']['targetRevision'] = 1
                elif change == 'stale_file':
                    run['artifacts'][0]['sha256'] = digest('changed after review')
                elif change == 'missing_qa_revision':
                    run['steps'][1].pop('revision'); run['calls'][-1].pop('revision')
                elif change == 'bool_target_revision':
                    task['validation']['targetRevision'] = False
                elif change == 'bool_qa_call_revision':
                    run['calls'][-1]['revision'] = False
                elif change == 'bool_review_snapshot':
                    run['calls'][-1]['reviewTargets'][task['id']]['targetRevision'] = False
                elif change == 'boolean_count':
                    run['calls'][-1]['result']['browser']['assertionsPassed'] = True
                elif change == 'page_error':
                    run['calls'][-1]['result']['browser']['pageErrors'] = [{'message': 'real error'}]
                elif change == 'no_necessary_read':
                    run['calls'] = run['calls'][1:]
                elif change == 'unpublished_edit':
                    run['calls'][2]['result']['sha256'] = digest('unpublished new JS version')
                elif change == 'other_unpublished_html':
                    run['calls'][1]['result']['sha256'] = digest('unpublished new HTML while JS matches')
                elif change == 'extra_unpublished_css':
                    run['calls'].insert(-2, {'id': 'unpublished-css', 'taskId': task['id'], 'agentId': 'dev', 'revision': 0,
                        'tool': 'file.write', 'status': 'success', 'result': {'ok': True, 'path': 'styles.css', 'sha256': digest('body{color:red}')}})
                observed = self.settle(candidate, run, task)
                self.assertEqual(observed['assessment'], 'unassessed')
                self.assertFalse(self.repo.get_memory(candidate['id'], 'dev')['verified'])

    def test_ack_behavior_edit_test_and_current_review_provide_limited_workflow_evidence_once(self):
        candidate, _, _ = self.candidate()
        run, task = self.trial_run()
        observed = self.settle(candidate, run, task)
        self.assertEqual(observed['assessment'], 'passed')
        self.assertEqual(observed['evidenceCallIds'], ['qa-browser'])
        self.assertTrue(observed['adoptionProven'])
        record = self.repo.get_memory(candidate['id'], 'dev')
        self.assertTrue(record['verified'])
        self.assertEqual(record['confirmation']['id'], 'qa-browser')
        self.assertIn('not causal proof', record['evidenceScope'])
        self.assertEqual(record['uses'], 0, 'trial evidence is separate from subsequent trusted-strategy usage')
        self.assertEqual(self.learning.settle_trial(candidate['id'], 'dev', run, task), observed)
        self.assertEqual(self.repo.get_memory(candidate['id'], 'dev')['learningTrialCounts']['passed'], 1)

    def test_recurrence_records_failed_attempt_and_downgrades_without_claiming_causal_blame(self):
        candidate, _, _ = self.candidate()
        run, task = self.trial_run()
        task.update(status='failed', error={'code': 'budget'})
        run['status'] = 'failed'
        run['calls'] = [self.read_call('repeat-' + str(index), task) for index in range(3)]
        observed = self.settle(candidate, run, task)
        record = self.repo.get_memory(candidate['id'], 'dev')
        self.assertEqual(observed['assessment'], 'failed')
        self.assertLess(record['confidence'], candidate['confidence'])
        self.assertFalse(record['verified'])
        self.assertIn('not causal blame', observed['reason'])

    def test_write_churn_requires_testing_between_versions_not_just_fewer_total_writes(self):
        candidate, _, _ = self.candidate('write')
        self.assertEqual(candidate['learningRule'], 'write_churn_without_test')
        run, task = self.trial_run()
        observed = self.settle(candidate, run, task)
        self.assertEqual(observed['assessment'], 'passed')
        candidate2, _, _ = self.candidate('write')
        run, task = self.trial_run(); run['id'] += '-churn'
        for artifact in run['artifacts']:
            artifact['runId'] = run['id']
        churn = copy.deepcopy(run['calls'][1]); churn['id'] = 'rewrite-before-test'; churn['result']['sha256'] = digest('second untested version')
        run['calls'].insert(2, churn)
        observed = self.settle(candidate2, run, task)
        self.assertEqual(observed['assessment'], 'unassessed')

    def test_print_exit_zero_cannot_clear_untested_file_version_intervals(self):
        candidate, _, _ = self.candidate('write')
        run, task = self.trial_run()
        original = copy.deepcopy(run['calls'][1])
        inserted = []
        for index in range(2):
            inserted.append({'id': f'print-{index}', 'taskId': task['id'], 'agentId': 'dev', 'revision': 0,
                'tool': 'code.test', 'status': 'success', 'args': {'code': 'print(1)'},
                'result': {'ok': True, 'exitCode': 0, 'runtime': 'python'}})
            changed = copy.deepcopy(original)
            changed['id'] = f'untested-{index}'
            changed['result']['sha256'] = digest(f'untested-{index}')
            inserted.append(changed)
        run['calls'][2:2] = inserted
        # Restore the final current artifact, then perform the real browser test.
        restored = copy.deepcopy(original); restored['id'] = 'restored-current'
        run['calls'].insert(-2, restored)
        observed = self.settle(candidate, run, task)
        self.assertEqual(observed['assessment'], 'unassessed')
        self.assertGreaterEqual(observed['observations']['maxVersionsBetweenTests'], 3)
        self.assertFalse(self.repo.get_memory(candidate['id'], 'dev')['verified'])

    def test_generic_reflection_and_pending_review_never_auto_confirm(self):
        generic = self.memory.propose('dev', 'Use a better architecture', 'booking', 'old-task', 'old-run', [])
        run, task = self.trial_run()
        self.assertEqual(self.settle(generic, run, task)['assessment'], 'unassessed')
        candidate, _, _ = self.candidate()
        run, task = self.trial_run(); run['status'] = 'running'
        validation = task.pop('validation')
        waiting = self.settle(candidate, run, task)
        self.assertEqual(waiting['status'], 'awaiting_evidence')
        self.assertNotIn('settledAt', waiting)
        task['validation'] = validation
        settled = self.learning.settle_trial(candidate['id'], 'dev', run, task, acknowledged_memory_ids=[candidate['id']])
        self.assertEqual(settled['assessment'], 'passed')


if __name__ == '__main__':
    unittest.main()
