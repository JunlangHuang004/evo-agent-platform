"""Run-scoped monotonic deadlines; no public network, model, or live state."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

from backend.sandbox import ToolError, SandboxManager, run_process
from backend.tools import ToolGateway, _resolve_addresses, _validate_url


def result(stdout='', **extra):
    return {'exitCode': 0, 'stdout': stdout, 'stderr': '', 'timedOut': False,
            'outputLimitExceeded': False, 'durationMs': 1, **extra}


class ToolDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = ToolGateway(SimpleNamespace(data_dir=Path(self.temp.name),
            tool_timeout=20, docker_image='python:3.11-alpine', git_remote=''))

    def tearDown(self):
        self.temp.cleanup()

    def test_expired_or_invalid_deadline_cannot_start_approved_effect(self):
        self.gateway.execute('executor', 'run1', 'file.write', {'path': 'keep.txt', 'content': 'keep'})
        request = self.gateway.execute('executor', 'run1', 'file.delete', {'path': 'keep.txt'})
        for deadline in (time.monotonic() - 1, True, float('nan'), float('inf')):
            with self.subTest(deadline=deadline), self.assertRaises(ToolError) as caught:
                self.gateway.execute('executor', 'run1', 'file.delete', request['args'], approved=True, deadline=deadline)
            self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual((self.gateway.workspace('executor', 'run1') / 'keep.txt').read_text(), 'keep')

    def test_workspace_lock_wait_consumes_same_deadline(self):
        lock = threading.RLock()
        self.gateway._locks[('executor', 'run1')] = lock
        held, release = threading.Event(), threading.Event()
        def hold():
            with lock:
                held.set()
                release.wait(2)
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(held.wait(1))
        started = time.monotonic()
        try:
            with self.assertRaises(ToolError) as caught:
                self.gateway.execute('executor', 'run1', 'file.write', {'path': 'late.txt', 'content': 'no'}, deadline=started + .06)
            self.assertEqual(caught.exception.code, 'deadline')
            self.assertFalse(caught.exception.outcome_unknown)
            self.assertLess(time.monotonic() - started, .7)
            self.assertFalse((self.gateway.workspace('executor', 'run1') / 'late.txt').exists())
        finally:
            release.set()
            worker.join(2)

    def test_late_file_effect_retains_actual_result_and_unknown_outcome(self):
        clock = [10.0]
        from backend.tools import write_text as original_write
        def write(*args, **kwargs):
            original_write(*args, **kwargs)
            clock[0] = 12.0
        with mock.patch('time.monotonic', side_effect=lambda: clock[0]), mock.patch('backend.tools.write_text', side_effect=write):
            observed = self.gateway.execute('executor', 'run1', 'file.write', {'path': 'saved.txt', 'content': 'real'}, deadline=11.0)
        self.assertFalse(observed['ok'])
        self.assertTrue(observed['outcomeUnknown'])
        self.assertTrue(observed['deadlineExceeded'])
        self.assertTrue(observed['created'])
        self.assertEqual((self.gateway.workspace('executor', 'run1') / 'saved.txt').read_text(), 'real')

    def test_git_commands_share_one_deadline_and_do_not_reset_timeout(self):
        self.gateway.git = '/trusted/git'
        self.gateway.execute('executor', 'run1', 'file.write', {'path': 'app.py', 'content': 'print(1)'})
        clock, seen = [10.0], []
        def command(argv, **kwargs):
            seen.append((list(argv), kwargs['timeout'], kwargs.get('deadline')))
            clock[0] += 2
            return result('a' * 40 if 'rev-parse' in argv else '')
        with mock.patch('time.monotonic', side_effect=lambda: clock[0]), mock.patch('backend.tools.run_process', side_effect=command):
            with self.assertRaises(ToolError) as caught:
                self.gateway.execute('executor', 'run1', 'git.commit', {'message': 'real staged files', 'files': ['app.py']}, deadline=19.0)
        self.assertEqual(caught.exception.code, 'deadline')
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual([entry[1] for entry in seen], [9, 7, 5, 3, 1])
        self.assertEqual([entry[2] for entry in seen], [19] * 5)
        self.assertEqual(self.gateway.timeout, 20)

    def test_web_worker_bounds_entire_blocking_operation_not_only_socket(self):
        started = time.monotonic()
        with mock.patch('backend.tools._WEB_FETCH_WORKER', 'import time; time.sleep(5)'):
            with self.assertRaises(ToolError) as caught:
                self.gateway._fetch_url('https://example.com/', deadline=started + .12)
        self.assertEqual(caught.exception.code, 'deadline')
        self.assertLess(time.monotonic() - started, 1.0)

    def test_dns_child_has_hard_deadline_and_keeps_ssrf_checks(self):
        started = time.monotonic()
        with mock.patch('backend.tools._DNS_RESOLVER', 'import time; time.sleep(5)'):
            with self.assertRaises(ToolError) as caught:
                _resolve_addresses('example.com', 443, started + .12)
        self.assertEqual(caught.exception.code, 'deadline')
        self.assertLess(time.monotonic() - started, 1.0)
        with mock.patch('backend.tools._resolve_addresses', return_value=['93.184.216.34', '127.0.0.1']), self.assertRaises(ToolError) as caught:
            _validate_url('https://example.com/', deadline=time.monotonic() + 1)
        self.assertEqual(caught.exception.code, 'permission')

    def test_worker_transport_is_fixed_and_returns_true_content_without_shared_timeout_mutation(self):
        response = {'url': 'https://example.com/', 'contentType': 'text/plain', 'content': 'real source', 'truncated': False}
        def child(argv, **kwargs):
            self.assertEqual(argv[:3], [sys.executable, '-I', '-c'])
            self.assertNotIn('API_KEY', kwargs['env'])
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], .5)
            return result(json.dumps({'response': response}))
        with mock.patch('backend.tools.run_process', side_effect=child):
            observed = self.gateway.execute('research', 'run1', 'web.fetch', {'url': response['url']}, deadline=time.monotonic() + .5)
        self.assertTrue(observed['ok'])
        self.assertEqual(observed['content'], 'real source')
        self.assertEqual(self.gateway.timeout, 20)
        # Real worker executes the unchanged SSRF transport, rejecting before network.
        with self.assertRaises(ToolError) as caught:
            self.gateway._fetch_url('http://127.0.0.1/', deadline=time.monotonic() + 2)
        self.assertEqual(caught.exception.code, 'permission')

    def test_docker_python_and_browser_launch_share_deadline_and_exact_cleanup(self):
        manager = self.gateway.sandbox
        manager.docker = '/trusted/docker'
        clock = [10.0]
        ready = {'dockerAvailable': True, 'dockerImageReady': True, 'browserImageReady': True}
        self.gateway.execute('executor', 'run1', 'file.write', {'path': 'index.html', 'content': '<h1>offline</h1>'})
        for language in ('python', 'browser'):
            clock[0], seen = 10.0, []
            def command(argv, **kwargs):
                seen.append((list(argv), kwargs))
                if argv[1] == 'run':
                    clock[0] = 11.0
                    return result(exitCode=-9, timedOut=True)
                return result()
            args = {'code': 'print(1)', 'timeout': 60} if language == 'python' else {
                'language': 'browser', 'path': 'index.html', 'timeout': 60,
                'checks': [{'action': 'assert_text', 'selector': 'h1', 'text': 'offline'}]}
            with self.subTest(language=language), mock.patch('time.monotonic', side_effect=lambda: clock[0]), mock.patch.object(manager, 'capabilities', return_value=ready), mock.patch('backend.sandbox.run_process', side_effect=command):
                observed = manager.run('executor', 'run1', args, test=True, deadline=11.0)
            self.assertFalse(observed['ok'])
            self.assertTrue(observed['deadlineExceeded'])
            self.assertTrue(observed['outcomeUnknown'])
            launch, cleanup = seen
            self.assertEqual(launch[1]['timeout'], 1)
            self.assertEqual(launch[1]['deadline'], 11)
            name = launch[0][launch[0].index('--name') + 1]
            self.assertEqual(cleanup[0], ['/trusted/docker', 'rm', '-f', name])
            self.assertEqual(manager._containers, {})

    def test_expired_sandbox_never_checks_or_launches_docker(self):
        with mock.patch('backend.sandbox.run_process') as process, self.assertRaises(ToolError):
            self.gateway.sandbox.run('executor', 'run1', {'code': 'print(1)'}, deadline=time.monotonic() - 1)
        process.assert_not_called()


class BoundedProcessDeadlineTests(unittest.TestCase):
    def test_trusted_subprocess_killed_at_deadline_including_closed_output(self):
        for script in ('import time;time.sleep(5)', 'import os,time;os.close(1);os.close(2);time.sleep(5)'):
            with self.subTest(script=script):
                started = time.monotonic()
                observed = run_process([sys.executable, '-I', '-c', script], timeout=20, deadline=started + .12)
                self.assertTrue(observed['timedOut'])
                self.assertLess(time.monotonic() - started, 1.0)

    def test_none_deadline_preserves_regular_execution(self):
        observed = run_process([sys.executable, '-I', '-c', 'print("trusted")'], timeout=2)
        self.assertEqual(observed['stdout'], 'trusted\n')
        self.assertFalse(observed['timedOut'])


@unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS') == '1' and shutil.which('docker'), 'Opt-in real Docker smoke')
class DockerDeadlineTests(unittest.TestCase):
    def test_real_sleep_container_stops_at_deadline_and_exact_name_is_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SandboxManager(SimpleNamespace(data_dir=Path(directory), docker_image='python:3.11-alpine'))
            started = time.monotonic()
            observed = manager.run('executor', 'run-deadline', {'code': 'import time;print("started",flush=True);time.sleep(30)', 'timeout': 60}, deadline=started + 3)
            self.assertFalse(observed['ok'])
            self.assertTrue(observed['timedOut'])
            self.assertTrue(observed['deadlineExceeded'])
            self.assertLess(time.monotonic() - started, 9)
            inspected = run_process([manager.docker, 'inspect', observed['sandboxId']], timeout=3, env=manager._environment())
            self.assertNotEqual(inspected['exitCode'], 0)


if __name__ == '__main__':
    unittest.main()
