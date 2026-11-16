"""Killable request deadlines; only trusted local fixtures, never model requests."""
import io
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.provider import ModelClient, ProviderError
from backend import provider_deadline, provider_worker


@unittest.skipUnless(os.name == 'posix', 'The opt-in process deadline explicitly requires POSIX.')
class ProviderDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.secret = 'deadline-fixture-private-key'
        self.config = Config(self.root, {'LLM_API_KEY': self.secret,
            'LLM_TRANSPORT_MODE': 'compatible', 'LLM_THINKING_MODE': 'disabled',
            'LLM_JSON_MODE': 'json_object', 'MODEL_MAX_TOKENS': '6000'})
        self.client = ModelClient(self.config)
        self.messages = [{'role': 'user', 'content': 'Offline deadline test only.'}]

    def tearDown(self):
        self.temp.cleanup()

    def command(self, body, read_packet=True):
        fixture = self.root / 'trusted_fixture.py'
        fixture.write_text('import json, os, sys, time\n' +
            ('packet=json.loads(sys.stdin.buffer.read())\n' if read_packet else '') + body)
        return [sys.executable, '-I', '-B', '-u', str(fixture)]

    def response_script(self, content='ok'):
        return ('result={"content":' + repr(content) + ',"usage":{"total_tokens":8},'
                '"model":"fixture","finish_reason":"stop","transport":{"bodyComplete":True}}\n'
                'print(json.dumps({"ok":True,"result":result}))\n')

    def test_none_deadline_keeps_both_original_paths_without_starting_a_worker(self):
        with patch.object(self.client, '_complete', return_value={'content': 'legacy'}) as complete, \
                patch('backend.provider_deadline.subprocess.Popen', side_effect=AssertionError('No child expected')):
            self.assertEqual(self.client.complete(self.messages)['content'], 'legacy')
            complete.assert_called_once_with(self.messages, None)
            complete.reset_mock()
            self.client.complete_json(self.messages, max_tokens=300, deadline=None)
            complete.assert_called_once_with(self.messages, 300, json_protocol=True)

    def test_invalid_or_already_expired_deadline_never_starts_or_sends(self):
        with patch('backend.provider_deadline.subprocess.Popen') as spawn:
            for value in (True, 'tomorrow', math.inf, math.nan):
                with self.subTest(deadline=value), self.assertRaises(ProviderError) as caught:
                    self.client.complete(self.messages, deadline=value)
                self.assertEqual(caught.exception.code, 'parameters')
            with self.assertRaises(ProviderError) as caught:
                self.client.complete_json(self.messages, deadline=time.monotonic() - 1)
            self.assertEqual(caught.exception.code, 'deadline_exceeded')
            self.assertFalse(caught.exception.retryable)
            self.assertFalse(caught.exception.transport['workerStarted'])
            self.assertFalse(caught.exception.transport['usageUnknown'])
            spawn.assert_not_called()

    def test_slot_wait_uses_deadline_without_spawning_an_extra_network_request(self):
        for _ in range(3): self.client._slots.acquire()
        started = time.monotonic()
        try:
            with patch('backend.provider_deadline.subprocess.Popen') as spawn:
                with self.assertRaises(ProviderError) as caught:
                    self.client.complete(self.messages, deadline=started + .08)
                self.assertEqual(caught.exception.code, 'deadline_exceeded')
                self.assertEqual(caught.exception.phase, 'wait_slot')
                spawn.assert_not_called()
            self.assertLess(time.monotonic() - started, .5)
        finally:
            for _ in range(3): self.client._slots.release()

    def test_blocked_resolver_process_is_killed_reaped_and_not_retried(self):
        command = self.command('import socket\nsocket.getaddrinfo=lambda *a: time.sleep(60)\nsocket.getaddrinfo("offline.test",443)\n')
        original_spawn = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = original_spawn(*args, **kwargs); children.append(child); return child
        started = time.monotonic()
        with patch.object(provider_deadline, '_worker_command', return_value=command), \
                patch.object(provider_deadline.subprocess, 'Popen', side_effect=spawn) as calls:
            with self.assertRaises(ProviderError) as caught:
                self.client.complete(self.messages, deadline=started + .15)
        error = caught.exception
        self.assertEqual(error.code, 'deadline_exceeded')
        self.assertFalse(error.retryable)
        self.assertTrue(error.transport['workerTerminated'])
        self.assertTrue(error.transport['workerReaped'])
        self.assertTrue(error.transport['usageUnknown'])
        self.assertTrue(error.transport['requestMayContinueServerSide'])
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(children[0].returncode, -signal.SIGKILL)
        self.assertLess(time.monotonic() - started, 1.5)
        with self.assertRaises(ProcessLookupError): os.kill(children[0].pid, 0)

    def test_late_complete_reply_is_not_adopted_and_only_own_group_is_killed(self):
        command = self.command('time.sleep(.4)\n' + self.response_script('late-success'))
        unrelated = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'],
                                     start_new_session=True, stdout=subprocess.DEVNULL)
        killpg = os.killpg
        try:
            with patch.object(provider_deadline, '_worker_command', return_value=command), \
                    patch.object(provider_deadline.os, 'killpg', wraps=killpg) as kills:
                with self.assertRaises(ProviderError) as caught:
                    self.client.complete_json(self.messages, deadline=time.monotonic() + .12)
            self.assertEqual(caught.exception.code, 'deadline_exceeded')
            self.assertNotEqual(kills.call_args.args[0], unrelated.pid)
            self.assertEqual(kills.call_args.args[1], signal.SIGKILL)
            self.assertIsNone(unrelated.poll(), 'Do not terminate another request/process group.')
        finally:
            unrelated.kill(); unrelated.wait(timeout=2)

    def test_blocking_socket_and_stdin_backpressure_are_killable_without_a_real_network(self):
        cases = [('socket', 'import socket\na,b=socket.socketpair()\na.recv(1)\n', True),
                 ('stdin', 'time.sleep(60)\n', False),
                 ('pacing_sleep', 'time.sleep(60)\n', True)]
        for label, body, reads in cases:
            command = self.command(body, read_packet=reads)
            messages = [{'role': 'user', 'content': 'x' * 400000}]
            with self.subTest(block=label), patch.object(provider_deadline, '_worker_command', return_value=command):
                started = time.monotonic()
                with self.assertRaises(ProviderError) as caught:
                    self.client.complete(messages, deadline=started + .12)
                self.assertEqual(caught.exception.code, 'deadline_exceeded')
                self.assertTrue(caught.exception.transport['workerTerminated'])
                self.assertTrue(caught.exception.transport['workerReaped'])
                self.assertLess(time.monotonic() - started, 1.5)

    def test_success_preserves_protocol_budget_and_proxy_but_never_secret_argv_or_env(self):
        body = self.response_script()
        body = body.replace('"transport":{"bodyComplete":True}',
            '"transport":{"bodyComplete":True,"packetJson":packet["json_protocol"],'
            '"packetMaxTokens":packet["max_tokens"],"settingsKeys":list(packet["settings"]),'
            '"childEnvHasKey":any("API_KEY" in k or "GIT_TOKEN" in k for k in os.environ)}')
        body = body.replace('"content":\'ok\'', '"content":"ok "+packet["settings"]["api_key"]')
        command = self.command(body)
        real_spawn = subprocess.Popen
        with patch.dict(os.environ, {'LLM_API_KEY': self.secret, 'GIT_TOKEN': 'private-git-token',
                                     'HTTPS_PROXY': 'http://127.0.0.1:57777'}), \
                patch.object(provider_deadline, '_worker_command', return_value=command), \
                patch.object(provider_deadline.subprocess, 'Popen', wraps=real_spawn) as spawn:
            result = self.client.complete_json(self.messages, max_tokens=123, deadline=time.monotonic() + 2)
        self.assertEqual(result['content'], 'ok [REDACTED]')
        self.assertEqual(result['usage'], {'total_tokens': 8})
        details = result['transport']
        self.assertTrue(details['packetJson']); self.assertEqual(details['packetMaxTokens'], 123)
        self.assertFalse(details['childEnvHasKey'])
        self.assertEqual(set(details['settingsKeys']), set(provider_deadline._SETTINGS))
        self.assertNotIn('git_token', details['settingsKeys'])
        self.assertTrue(details['workerReaped']); self.assertFalse(details['usageUnknown'])
        self.assertNotIn(self.secret, repr(spawn.call_args))
        self.assertNotIn('private-git-token', repr(spawn.call_args))
        self.assertEqual(spawn.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertEqual(spawn.call_args.kwargs['env']['HTTPS_PROXY'], 'http://127.0.0.1:57777')

    def test_oversized_partial_or_crashed_worker_never_returns_a_model_result(self):
        fixtures = [('print("x"*5000)', 'worker_output_limit'),
                    ('print(\'{"ok":true,"result":\')', 'worker_protocol'),
                    ('sys.stderr.write(packet["settings"]["api_key"]);sys.exit(7)', 'worker_transport')]
        for body, expected in fixtures:
            with self.subTest(expected=expected), \
                    patch.object(provider_deadline, '_worker_command', return_value=self.command(body)), \
                    patch.object(provider_deadline, 'MAX_WORKER_BYTES', 1024):
                with self.assertRaises(ProviderError) as caught:
                    self.client.complete(self.messages, deadline=time.monotonic() + 2)
                error = caught.exception
                self.assertEqual(error.code, expected)
                self.assertFalse(error.retryable)
                self.assertNotIn(self.secret, str(error) + json.dumps(error.transport))
                self.assertTrue(error.transport['workerReaped'])

    def test_result_that_finishes_parsing_after_deadline_is_rejected(self):
        command = self.command(self.response_script())
        loads = json.loads
        def delayed(raw):
            result = loads(raw); time.sleep(.3); return result
        with patch.object(provider_deadline, '_worker_command', return_value=command), \
                patch.object(provider_deadline.json, 'loads', side_effect=delayed):
            with self.assertRaises(ProviderError) as caught:
                self.client.complete(self.messages, deadline=time.monotonic() + .25)
        self.assertEqual(caught.exception.code, 'deadline_exceeded')
        self.assertTrue(caught.exception.transport['workerReaped'])

    def test_real_trusted_worker_reports_missing_key_without_any_upstream_call(self):
        self.config.api_key = ''
        with self.assertRaises(ProviderError) as caught:
            self.client.complete_json(self.messages, deadline=time.monotonic() + 2)
        self.assertEqual(caught.exception.phase, 'prepare_request')
        self.assertFalse(caught.exception.transport['requestMayContinueServerSide'])
        self.assertFalse(caught.exception.transport['usageUnknown'])
        self.assertTrue(caught.exception.transport['workerReaped'])

    def test_worker_reuses_existing_complete_once_without_loading_env_or_other_config(self):
        settings = {name: getattr(self.config, name) for name in provider_deadline._SETTINGS}
        packet = {'settings': settings, 'messages': self.messages, 'max_tokens': 128, 'json_protocol': True}
        incoming = type('Input', (), {'buffer': io.BytesIO(json.dumps(packet).encode())})()
        outgoing = type('Output', (), {'buffer': io.BytesIO()})()
        response = {'content': 'actual fixture', 'usage': {'total_tokens': 8}, 'finish_reason': 'stop'}
        with patch.object(provider_worker.sys, 'stdin', incoming), patch.object(provider_worker.sys, 'stdout', outgoing), \
                patch.object(provider_worker.ModelClient, '_complete', return_value=response) as complete:
            provider_worker.main()
        complete.assert_called_once_with(self.messages, 128, json_protocol=True)
        self.assertEqual(json.loads(outgoing.buffer.getvalue()), {'ok': True, 'result': response})


if __name__ == '__main__':
    unittest.main()
