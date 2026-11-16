"""Fake-clock observations of real ledger shapes; no model or tool execution."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.progress_watchdog import assess_task_progress, current_watchdog_notice, watchdog_tick


def fixture():
    task = {'id': 'task-dev', 'agentId': 'dev', 'revision': 0, 'status': 'running',
            'outputs': [], 'dependsOn': [], 'childIds': []}
    run = {'id': 'run-watch', 'status': 'running', 'steps': [task], 'calls': [],
           'artifacts': [], 'deliveries': [], 'approvals': [],
           'budget': {'profile': 'time_only', 'deadlineAt': 2700000, 'timeMs': 2700000}}
    return run, task


def model(run, identifier, status='success'):
    call = {'id': identifier, 'taskId': 'task-dev', 'agentId': 'dev', 'revision': 0,
            'tool': 'llm.chat', 'status': status, 'result': 'Same proposed action, new message ID.'}
    run['calls'].append(call)
    return call


def write(run, identifier, sha='a' * 64):
    call = {'id': identifier, 'taskId': 'task-dev', 'agentId': 'dev', 'revision': 0,
            'tool': 'file.write', 'status': 'success', 'args': {'path': 'app.html'},
            'result': {'ok': True, 'path': 'app.html', 'sha256': sha}}
    run['calls'].append(call)
    return call


def browser(run, identifier, checks=None):
    call = {'id': identifier, 'taskId': 'task-dev', 'agentId': 'dev', 'revision': 0,
            'tool': 'code.test', 'status': 'success', 'args': {},
            'result': {'ok': True, 'test': True, 'runtime': 'browser', 'exitCode': 0,
                'browser': {'executed': True, 'ok': True,
                    'testedFiles': [{'path': 'app.html', 'sha256': 'a' * 64}],
                    'assertions': checks or [{'action': 'assert_text', 'selector': 'h1',
                                              'expected': 'Ready', 'actual': 'Ready', 'passed': True}]}}}
    run['calls'].append(call)
    return call


class ProgressWatchdogTests(unittest.TestCase):
    def stalled(self, busy=False):
        run, task = fixture(); write(run, 'initial')
        state = watchdog_tick(run, None, 0)['state']
        model(run, 'first'); state = watchdog_tick(run, state, 90000)['state']
        model(run, 'second')
        if busy:
            model(run, 'currently-active', 'running')
        result = watchdog_tick(run, state, 180000)
        self.assertEqual(len(result['notices']), 1)
        return run, task, result['state'], result['notices'][0]

    def test_config_default_bounds_and_delivery_profiles_do_not_change_cadence_or_deadline(self):
        with tempfile.TemporaryDirectory() as path:
            for supplied, expected in (({}, 90), ({'PROGRESS_WATCHDOG_SECONDS': '20'}, 60),
                    ({'PROGRESS_WATCHDOG_SECONDS': '200'}, 120)):
                config = Config(Path(path), supplied)
                self.assertEqual(config.progress_watchdog_seconds, expected)
                config.apply_time_only_profile(); config.max_run_seconds = 2700
                self.assertEqual(config.progress_watchdog_seconds, expected)
                self.assertEqual(config.max_run_seconds, 2700)

    def test_early_tick_is_constant_time_and_does_not_scan(self):
        run, task = fixture(); state = watchdog_tick(run, None, 0)['state']
        before = copy.deepcopy((run, state))
        with patch('backend.progress_watchdog.assess_task_progress', side_effect=AssertionError('Must not scan')):
            result = watchdog_tick(run, state, 89999)
        self.assertFalse(result['due']); self.assertFalse(result['notices'])
        self.assertEqual((run, state), before)

    def test_repeated_messages_and_new_call_ids_queue_only_one_notice_without_mutation(self):
        run, task, state, notice = self.stalled()
        self.assertEqual(current_watchdog_notice(run, task, state, 180000), notice)
        original = copy.deepcopy(run)
        for now in (270000, 360000, 450000):
            model(run, 'repeat-' + str(now))
            result = watchdog_tick(run, state, now); state = result['state']
            self.assertFalse(result['notices'])
        self.assertEqual(run['budget'], original['budget'])
        self.assertEqual(task['status'], 'running')
        self.assertFalse(notice['actionExecuted']); self.assertFalse(notice['acceptanceCreated'])
        task['_watchdogDeliveredNoticeId'] = notice['noticeId']
        self.assertIsNone(current_watchdog_notice(run, task, state, 450000))

    def test_busy_loop_can_queue_but_never_interrupts_current_model(self):
        run, task, state, notice = self.stalled(busy=True)
        self.assertIsNone(current_watchdog_notice(run, task, state, 180000))
        self.assertEqual(run['calls'][-1]['status'], 'running')
        run['calls'][-1]['status'] = 'success'
        self.assertEqual(current_watchdog_notice(run, task, state, 180001)['noticeId'], notice['noticeId'])

    def test_single_long_model_or_tool_is_waiting_not_a_loop(self):
        for name in ('llm.chat', 'code.test'):
            run, task = fixture(); call = model(run, 'long', 'running'); call['tool'] = name
            state = None
            for now in (0, 90000, 180000, 900000):
                result = watchdog_tick(run, state, now); state = result['state']
                self.assertFalse(result['notices'])
                self.assertIn(result['assessments'][0]['waitState'], ('active_model', 'active_tool'))
            self.assertEqual(call['status'], 'running')

    def test_approval_dependency_pause_and_unknown_effect_never_receive_notice(self):
        for kind in ('approval', 'dependency', 'paused', 'unknown_effect'):
            run, task, state, notice = self.stalled()
            if kind == 'approval':
                run['approvals'].append({'id': 'approval-1', 'taskId': task['id'], 'status': 'pending'})
            elif kind == 'dependency':
                task.update(status='waiting_children', childIds=['child'])
            elif kind == 'paused':
                run['userPaused'] = True
            else:
                call = write(run, 'unknown'); call['status'] = 'unknown'; call['result']['outcomeUnknown'] = True
            self.assertIsNone(current_watchdog_notice(run, task, state, 180001), kind)
            result = watchdog_tick(run, state, 270000)
            self.assertEqual(result['assessments'][0]['waitState'], kind)
            self.assertFalse(result['notices'])

    def test_real_changed_bytes_or_new_executed_check_invalidates_pending_notice(self):
        for progress in ('write', 'test'):
            run, task, state, notice = self.stalled()
            write(run, 'changed', 'b' * 64) if progress == 'write' else browser(run, 'actual-check')
            self.assertIsNone(current_watchdog_notice(run, task, state, 180001))
            result = watchdog_tick(run, state, 270000)
            self.assertFalse(result['notices'])
            self.assertIsNone(result['state']['tasks'][task['id']]['notice'])
            self.assertEqual(result['state']['tasks'][task['id']]['lastProgressAt'], 270000)

    def test_same_bytes_republished_under_new_artifact_ids_are_not_progress(self):
        run, task = fixture()
        a = {'id': 'artifact-' + '1' * 32, 'runId': run['id'], 'taskId': task['id'],
             'ownerAgentId': 'dev', 'path': 'app.html', 'sha256': 'a' * 64}
        run['artifacts'].append(a); task['outputs'] = [a['id']]
        first = assess_task_progress(run, task)['evidenceSignature']
        b = {**a, 'id': 'artifact-' + '2' * 32}; run['artifacts'].append(b); task['outputs'] = [b['id']]
        self.assertEqual(first, assess_task_progress(run, task)['evidenceSignature'])
        # Re-reading an identical granted import under its new artifact ID is
        # still the same content/window, not new coverage.
        for i, artifact in enumerate((a, b)):
            call = write(run, 'read-' + str(i)); call['tool'] = 'file.read'
            call['result']['path'] = 'imports/' + artifact['id'] + '/app.html'
            signature = assess_task_progress(run, task)['evidenceSignature']
            if i:
                self.assertEqual(signature, previous)
            previous = signature

    def test_rearranged_passing_assertions_and_repeated_failures_are_not_new_progress(self):
        run, task = fixture()
        one = {'action': 'assert_text', 'selector': 'h1', 'expected': 'A', 'actual': 'A', 'passed': True}
        two = {**one, 'selector': 'h2', 'expected': 'B', 'actual': 'B'}
        browser(run, 'a', [one]); browser(run, 'b', [two]); before = assess_task_progress(run, task)['evidenceSignature']
        browser(run, 'ab', [two, one]); self.assertEqual(before, assess_task_progress(run, task)['evidenceSignature'])
        bad = browser(run, 'bad'); bad['status'] = 'failed'; bad['result'].update(ok=False, exitCode=1)
        bad['result']['browser'].update(ok=False, steps=[{'action': 'click', 'selector': '#missing', 'ok': False, 'error': 'Timeout'}])
        before = assess_task_progress(run, task)['evidenceSignature']
        again = copy.deepcopy(bad); again['id'] = 'new-id'; run['calls'].append(again)
        self.assertEqual(before, assess_task_progress(run, task)['evidenceSignature'])

    def test_current_review_and_real_fetch_snapshot_shapes_are_progress(self):
        run, task = fixture(); before = assess_task_progress(run, task)['evidenceSignature']
        fetch = write(run, 'fetch'); fetch.update(tool='web.fetch', result={'ok': True, 'sourceSnapshot': {
            'url': 'https://example.test/source', 'path': 'imports/web-source/body.txt', 'sha256': 'b' * 64}})
        after_fetch = assess_task_progress(run, task)['evidenceSignature']; self.assertNotEqual(before, after_fetch)
        target = {'id': 'target', 'agentId': 'author', 'revision': 0, 'status': 'done', 'outputs': ['artifact-target']}
        run['steps'].append(target); run['artifacts'].append({'id': 'artifact-target', 'sha256': 'c' * 64,
            'runId': run['id'], 'taskId': 'target', 'ownerAgentId': 'author'})
        target['validation'] = {'reviewerTaskId': task['id'], 'reviewerRevision': 0, 'targetRevision': 0,
            'artifactHashes': {'artifact-target': 'c' * 64}, 'verdict': 'pass', 'verified': False}
        self.assertNotEqual(after_fetch, assess_task_progress(run, task)['evidenceSignature'])
        target['validation']['targetRevision'] = 1
        self.assertEqual(after_fetch, assess_task_progress(run, task)['evidenceSignature'])

    def test_wrong_run_owner_revision_and_changed_hash_reject_safe_boundary_notice(self):
        for key in ('run', 'owner', 'revision', 'hash'):
            run, task, state, notice = self.stalled()
            if key == 'run': run['id'] = 'foreign-run'
            if key == 'owner': task['agentId'] = 'other'
            if key == 'revision': task['revision'] = 1
            if key == 'hash': write(run, 'new', 'c' * 64)
            self.assertIsNone(current_watchdog_notice(run, task, state, 180001), key)

    def test_terminal_run_exits_and_terminal_task_is_removed_without_work(self):
        run, task, state, _ = self.stalled(); task['status'] = 'done'
        result = watchdog_tick(run, state, 270000); self.assertFalse(result['state']['tasks'])
        run['status'] = 'partial'
        with patch('backend.progress_watchdog.assess_task_progress', side_effect=AssertionError('No terminal scan')):
            result = watchdog_tick(run, state, 270001)
        self.assertTrue(result['terminal']); self.assertFalse(result['notices'])
        self.assertIsNone(current_watchdog_notice(run, task, state, 270001))


if __name__ == '__main__':
    unittest.main()
