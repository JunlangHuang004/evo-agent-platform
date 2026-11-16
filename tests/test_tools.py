"""Meaningful security/contract tests; no model key, public network or remote push required."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import hashlib
import base64
import json
import os
import shutil
import tempfile
import unittest

from backend.agents import AGENTS
from backend.artifacts import ArtifactStore, ArtifactError
from backend.sandbox import ToolError, SandboxManager
from backend.tools import ToolGateway, _validate_url


def public_dns(host, port, **kwargs):
    return [(2, 1, 6, '', ('93.184.216.34', port))]


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(data_dir=Path(self.temp.name) / 'runtime', docker_image='python:3.11-alpine', git_remote='')
        self.gateway = ToolGateway(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, tool, args, agent='executor', run='run1', approved=False):
        return self.gateway.execute(agent, run, tool, args, approved=approved)

    def test_private_workspaces_and_parameter_permissions(self):
        self.call('file.write', {'path': 'output/result.md', 'content': 'private'})
        self.assertEqual(self.call('file.read', {'path': 'output/result.md'})['content'], 'private')
        with self.assertRaises(ToolError):
            self.call('file.read', {'path': 'output/result.md'}, agent='reviewer')
        self.assertNotEqual(self.gateway.workspace('executor', 'run1'), self.gateway.workspace('executor', 'run2'))
        for path in ('../reviewer/run1/data.txt', '/etc/passwd', '.env', 'safe/.git/config', 'C:\\Windows\\file', 'a//b', 'imports/../file'):
            with self.subTest(path=path), self.assertRaises(ToolError):
                self.call('file.read', {'path': path})
        for agent, name, args in [('unknown', 'file.list', {}), ('research', 'code.run', {'code': 'print(1)'}), ('reviewer', 'git.status', {}), ('executor', 'shell.exec', {})]:
            with self.subTest(agent=agent, tool=name), self.assertRaises(ToolError):
                self.call(name, args, agent=agent)
        with self.assertRaises(ToolError):
            self.call('file.write', {'path': 'x.txt', 'content': 'a', 'shell': 'rm'})
        with self.assertRaises(ToolError):
            self.call('file.write', {'path': 'x.txt', 'content': 'x' * (256 * 1024 + 1)})

    def test_symlinks_hardlinks_special_files_are_not_read_or_written(self):
        work = self.gateway.workspace('executor', 'run1')
        outside = Path(self.temp.name) / 'outside.txt'
        outside.write_text('untouched')
        (work / 'escape.txt').symlink_to(outside)
        (work / 'directory').symlink_to(outside.parent, target_is_directory=True)
        os.link(outside, work / 'hard.txt')
        if hasattr(os, 'mkfifo'):
            os.mkfifo(work / 'pipe.txt')
        for path in ('escape.txt', 'directory/outside.txt', 'hard.txt', 'pipe.txt'):
            for tool, args in [('file.read', {'path': path}), ('file.write', {'path': path, 'content': 'bad'}), ('file.delete', {'path': path})]:
                with self.subTest(path=path, tool=tool), self.assertRaises(ToolError):
                    self.call(tool, args, approved=True)
        self.assertEqual(outside.read_text(), 'untouched')

    def test_overwrite_patch_delete_use_content_bound_approval(self):
        self.call('file.write', {'path': 'main.py', 'content': 'print(1)\n'})
        pending = self.call('file.patch', {'path': 'main.py', 'old': '1', 'new': '2'})
        self.assertTrue(pending['needsApproval'])
        self.assertEqual(pending['args']['expectedSha256'], hashlib.sha256(b'print(1)\n').hexdigest())
        self.assertEqual(self.call('file.read', {'path': 'main.py'})['content'], 'print(1)\n')
        self.call(pending['tool'], pending['args'], approved=True)
        self.assertEqual(self.call('file.read', {'path': 'main.py'})['content'], 'print(2)\n')
        stale = self.call('file.write', {'path': 'main.py', 'content': 'approved replacement'})
        (self.gateway.workspace('executor', 'run1') / 'main.py').write_text('changed by another action')
        with self.assertRaises(ToolError):
            self.call(stale['tool'], stale['args'], approved=True)
        with self.assertRaises(ToolError):
            self.call('file.write', {'path': 'main.py', 'content': 'unbound'}, approved=True)
        pending = self.call('file.delete', {'path': 'main.py'})
        self.call(pending['tool'], pending['args'], approved=True)
        self.assertFalse((self.gateway.workspace('executor', 'run1') / 'main.py').exists())
        self.call('file.write', {'path': 'duplicate.txt', 'content': 'same same'})
        with self.assertRaises(ToolError):
            self.call('file.patch', {'path': 'duplicate.txt', 'old': 'same', 'new': 'x'})

    def test_imports_require_explicit_artifact_grant_and_are_read_only(self):
        self.call('file.write', {'path': 'research.md', 'content': 'evidence'}, agent='research')
        store = ArtifactStore(self.config.data_dir)
        artifact = store.register('research', 'run1', 'research.md', 'task1')
        self.assertEqual(store.get(artifact['id'])['content'], 'evidence')
        grant = store.grant(artifact['id'], 'reviewer', 'run1', 'orchestrator:task2')
        path = 'imports/' + grant['path']
        self.assertEqual(self.call('file.read', {'path': path}, agent='reviewer')['content'], 'evidence')
        for tool, args in [('file.write', {'path': path, 'content': 'changed'}), ('file.patch', {'path': path, 'old': 'evidence', 'new': 'changed'}), ('file.delete', {'path': path})]:
            with self.assertRaises(ToolError):
                self.call(tool, args, agent='reviewer', approved=True)
        with self.assertRaises(ToolError):
            self.call('file.read', {'path': path}, agent='executor')
        with self.assertRaises(ArtifactError):
            store.grant(artifact['id'], 'executor', 'run2', 'orchestrator:task2')
        self.call('file.write', {'path': 'secret.bin', 'content': 'not allowed'}, agent='research')
        with self.assertRaises(ArtifactError):
            store.register('research', 'run1', 'secret.bin', 'task1')

    def test_ssrf_and_mixed_dns_are_rejected(self):
        for url in ('http://127.0.0.1/', 'http://169.254.169.254/latest/meta-data', 'http://10.0.0.1/', 'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://localhost/', 'http://service.local/', 'file:///etc/passwd', 'https://u:p@example.com/', 'http://example.com:8080/', 'http://2130706433/'):
            with self.subTest(url=url), mock.patch('backend.tools.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 80))]), self.assertRaises(ToolError):
                _validate_url(url)
        with mock.patch('backend.tools.socket.getaddrinfo', side_effect=public_dns):
            normalized, host, port, addresses = _validate_url('https://example.com/path#fragment')
            self.assertEqual(normalized, 'https://example.com/path')
            self.assertEqual(addresses, ['93.184.216.34'])
        with mock.patch('backend.tools.socket.getaddrinfo', return_value=[(2,1,6,'',('93.184.216.34',443)),(2,1,6,'',('10.0.0.1',443))]), self.assertRaises(ToolError):
            _validate_url('https://example.com/')

    def test_redirect_target_is_validated_before_connect(self):
        class Response:
            status = 302
            def getheader(self, name, default=None):
                return 'http://169.254.169.254/latest/meta-data' if name == 'Location' else default
        class Connection:
            calls = []
            def __init__(self, host, address, port, timeout, tls=False):
                self.calls.append((host, address))
            def request(self, *args, **kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with mock.patch('backend.tools.socket.getaddrinfo', side_effect=public_dns), mock.patch('backend.tools._PinnedHTTP', Connection), self.assertRaises(ToolError):
            self.gateway._fetch_url('https://example.com/')
        self.assertEqual(len(Connection.calls), 1)

    def test_web_fetch_empty_response_is_not_successful_source_content(self):
        url = 'https://example.com/empty'
        for mime, content in (('text/plain', ''), ('text/plain', ' \n\t '),
                              ('text/html', ''), ('text/html', '<html><body> \n </body></html>')):
            with self.subTest(mime=mime, content=content), mock.patch.object(self.gateway, '_fetch_url',
                    return_value={'url': url, 'contentType': mime, 'content': content, 'truncated': False}) as fetched:
                with self.assertRaises(ToolError) as caught:
                    self.call('web.fetch', {'url': url}, agent='research')
                self.assertEqual(caught.exception.code, 'no-readable-content')
                self.assertFalse(caught.exception.retryable)
                fetched.assert_called_once_with(url)

    def test_web_fetch_script_only_html_is_not_successful_even_if_truncated(self):
        url = 'https://example.com/script-only'
        for truncated in (False, True):
            with self.subTest(truncated=truncated), mock.patch.object(self.gateway, '_fetch_url',
                    return_value={'url': url, 'contentType': 'text/html',
                                  'content': '<html><body><script>window.app={text:"not readable article text"};</script><style>body{color:red}</style></body></html>',
                                  'truncated': truncated}):
                with self.assertRaises(ToolError) as caught:
                    self.call('web.fetch', {'url': url}, agent='research')
                self.assertEqual(caught.exception.code, 'no-readable-content')

    def test_web_fetch_normal_html_and_nonempty_plain_text_keep_existing_contract(self):
        url = 'https://example.com/readable'
        fixtures = [('text/html', '<html><head><title>Article</title></head><body><p>Actual readable body.</p><script>not evidence</script></body></html>'),
                    ('text/plain', ' \nActual plain text.\n ')]
        for mime, content in fixtures:
            with self.subTest(mime=mime), mock.patch.object(self.gateway, '_fetch_url',
                    return_value={'url': url, 'contentType': mime, 'content': content, 'truncated': False}):
                result = self.call('web.fetch', {'url': url}, agent='research')
                self.assertTrue(result['ok'])
                self.assertFalse(result['truncated'])
                self.assertEqual(result['url'], url)
                self.assertEqual(result['sources'], [{'title': result['title'], 'url': url}])
                if mime == 'text/plain':
                    self.assertEqual(result['content'], content)
                else:
                    self.assertEqual(result['title'], 'Article')
                    self.assertIn('Actual readable body.', result['content'])
                    self.assertNotIn('not evidence', result['content'])

    def test_web_fetch_truncated_but_nonempty_content_remains_successful_and_labeled(self):
        url = 'https://example.com/partial'
        for mime, content in (('text/plain', 'Available partial text.'),
                              ('text/html', '<p>Available partial body.</p><script>incomplete app payload')):
            with self.subTest(mime=mime), mock.patch.object(self.gateway, '_fetch_url',
                    return_value={'url': url, 'contentType': mime, 'content': content, 'truncated': True}):
                result = self.call('web.fetch', {'url': url}, agent='research')
                self.assertTrue(result['ok'])
                self.assertTrue(result['truncated'])
                self.assertIn('Available partial', result['content'])
                self.assertEqual(result['sources'][0]['url'], url)

    def test_docker_unavailable_has_no_host_fallback(self):
        with mock.patch.object(self.gateway.sandbox, 'capabilities', return_value={'dockerAvailable': False, 'dockerImageReady': False, 'reason': 'Docker unavailable'}), mock.patch('backend.sandbox.run_process') as process:
            with self.assertRaisesRegex(ToolError, 'Docker unavailable'):
                self.call('code.run', {'code': 'print(1)'})
            process.assert_not_called()

    def test_docker_flags_private_mounts_and_bounded_output_contract(self):
        commands = []
        def fake_run(argv, **kwargs):
            commands.append(argv)
            return {'exitCode': 1 if 'run' in argv else 0, 'stdout': '', 'stderr': 'test failure', 'timedOut': False, 'outputLimitExceeded': False, 'durationMs': 10}
        self.gateway.sandbox.docker = '/fake/docker'
        with mock.patch.object(self.gateway.sandbox, 'capabilities', return_value={'dockerAvailable': True, 'dockerImageReady': True}), mock.patch('backend.sandbox.run_process', side_effect=fake_run):
            result = self.call('code.test', {'code': 'assert False'})
        command = commands[0]
        for flag in ('--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=256m', '--memory-swap=256m', '--cpus=1', '--pids-limit=64', '--pull=never'):
            self.assertIn(flag, command)
        mounts = [command[index + 1] for index, item in enumerate(command) if item == '--mount']
        self.assertEqual(len(mounts), 2)
        self.assertTrue(mounts[0].endswith('dst=/workspace'))
        self.assertTrue(mounts[1].endswith('dst=/imports,readonly'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['exitCode'], 1)
        self.assertEqual(commands[-1][1:3], ['rm', '-f'])
        self.assertEqual(commands[-1][-1], command[command.index('--name') + 1])

    def test_cancel_only_removes_own_tracked_run_containers(self):
        manager = self.gateway.sandbox
        manager.docker = '/fake/docker'
        manager._containers = {'evo-owned': 'run1', 'evo-other': 'other-run'}
        with mock.patch('backend.sandbox.run_process', return_value={}) as execute:
            self.assertEqual(manager.cancel('run1')['cancelledContainers'], 1)
        self.assertEqual(execute.call_args.args[0], ['/fake/docker', 'rm', '-f', 'evo-owned'])
        with self.assertRaises(ToolError):
            self.call('code.run', {'code': 'print(1)'})

    @unittest.skipUnless(shutil.which('git'), 'Git not installed')
    def test_git_metadata_is_external_hooks_ignored_and_push_bound(self):
        work = self.gateway.workspace('executor', 'run1')
        self.call('file.write', {'path': 'main.py', 'content': 'print(1)\n'})
        malicious = work / '.git' / 'hooks'
        malicious.mkdir(parents=True)
        hook = malicious / 'pre-commit'
        hook.write_text('#!/bin/sh\ntouch "' + str(work / 'HOOK_RAN') + '"\n')
        hook.chmod(0o755)
        result = self.call('git.commit', {'message': 'first', 'files': ['main.py']})
        self.assertTrue(result['ok'], result)
        self.assertFalse((work / 'HOOK_RAN').exists())
        self.assertTrue((self.config.data_dir / 'gitmeta' / 'executor' / 'run1' / 'HEAD').is_file())
        self.assertEqual(result['pushTarget']['branch'], 'agent-runs/executor/run1')
        self.config.git_remote = 'https://github.com/example/course.git'
        gateway = ToolGateway(self.config)
        target = gateway.execute('executor', 'run1', 'git.status', {})['pushTarget']
        with mock.patch('backend.tools.socket.getaddrinfo', side_effect=public_dns):
            pending = gateway.execute('executor', 'run1', 'git.push', target)
        self.assertTrue(pending['needsApproval'])
        for changed in ({**target, 'branch': 'main'}, {**target, 'remote': 'file:///tmp/remote'}, {**target, 'commit': '0'*40}):
            with self.assertRaises(ToolError):
                gateway.execute('executor', 'run1', 'git.push', changed, approved=True)
        new = gateway.execute('executor', 'run1', 'file.write', {'path': 'main.py', 'content': 'print(2)\n'})
        gateway.execute('executor', 'run1', new['tool'], new['args'], approved=True)
        gateway.execute('executor', 'run1', 'git.commit', {'message': 'second', 'files': ['main.py']})
        with self.assertRaises(ToolError):
            gateway.execute('executor', 'run1', 'git.push', pending['args'], approved=True)
        for remote in ('file:///tmp/repo', '/tmp/repo', 'git@github.com:example/repo', 'https://token@github.com/example/repo', 'https://@github.com/example/repo'):
            self.config.git_remote = remote
            with self.assertRaises(ToolError): ToolGateway(self.config)


    @unittest.skipUnless(shutil.which('git'), 'Git not installed')
    def test_git_token_is_push_only_scoped_env_and_all_output_is_redacted(self):
        token = 'fake-git-token-for-unit-tests-12345'
        basic = base64.b64encode(('x-access-token:' + token).encode()).decode()
        token64 = base64.b64encode(token.encode()).decode()
        self.config.git_remote = 'https://github.com/example/private.git'
        self.config.git_token = token
        gateway = ToolGateway(self.config)
        gateway.execute('executor', 'authrun', 'file.write', {'path': 'main.py', 'content': 'print(1)\n'})
        from backend import tools as module
        actual_run = module.run_process
        executions = []
        def process(argv, **kwargs):
            executions.append((list(argv), dict(kwargs['env'])))
            if 'push' in argv:
                return {'exitCode': 128, 'stdout': token + ' ' + basic,
                        'stderr': 'fake authentication failure ' + token64,
                        'timedOut': False, 'outputLimitExceeded': False, 'durationMs': 1}
            return actual_run(argv, **kwargs)
        with mock.patch('backend.tools.run_process', side_effect=process), mock.patch('backend.tools.socket.getaddrinfo', side_effect=public_dns), mock.patch.dict(os.environ, {'LLM_API_KEY': 'fake-llm-key-must-not-forward'}):
            commit = gateway.execute('executor', 'authrun', 'git.commit', {'message': 'local only', 'files': ['main.py']})
            self.assertTrue(commit['ok'])
            target = gateway.execute('executor', 'authrun', 'git.status', {})['pushTarget']
            pending = gateway.execute('executor', 'authrun', 'git.push', target)
            self.assertTrue(pending['needsApproval'])
            result = gateway.execute('executor', 'authrun', 'git.push', pending['args'], approved=True)
        self.assertFalse(result['ok'])
        self.assertIn('[REDACTED]', result['stdout'])
        self.assertIn('[REDACTED]', result['stderr'])
        self.assertEqual(len([argv for argv, _ in executions if 'push' in argv]), 1)
        for argv, environment in executions:
            for secret in (token, basic, token64):
                self.assertNotIn(secret, repr(argv))
                self.assertNotIn(secret, json.dumps(result))
                self.assertNotIn(secret, json.dumps(pending))
            self.assertNotIn('LLM_API_KEY', environment)
            if 'push' in argv:
                self.assertEqual(environment['GIT_CONFIG_COUNT'], '1')
                self.assertEqual(environment['GIT_CONFIG_KEY_0'], 'http.https://github.com/.extraheader')
                self.assertEqual(environment['GIT_CONFIG_VALUE_0'], 'AUTHORIZATION: basic ' + basic)
                self.assertIn('http.followRedirects=false', argv)
                self.assertTrue(any(value.startswith('http.curloptResolve=github.com:443:') for value in argv))
            else:
                self.assertNotIn('GIT_CONFIG_COUNT', environment)
                self.assertNotIn('GIT_CONFIG_VALUE_0', environment)
                for secret in (token, basic, token64):
                    self.assertNotIn(secret, repr(environment))
        for file in self.config.data_dir.rglob('*'):
            if file.is_file():
                content = file.read_bytes()
                for secret in (token, basic, token64):
                    self.assertNotIn(secret.encode(), content)
        self.assertNotIn(token, repr(gateway.sandbox._environment()))
        with mock.patch.object(gateway.sandbox, 'run', return_value={'ok': False, 'stdout': token, 'stderr': basic + ' ' + token64}):
            code_result = gateway.execute('executor', 'authrun', 'code.run', {'code': 'print(1)'})
        self.assertEqual(code_result['stdout'], '[REDACTED]')
        self.assertEqual(code_result['stderr'], '[REDACTED] [REDACTED]')
        with mock.patch('backend.tools.socket.getaddrinfo', side_effect=public_dns), mock.patch.object(gateway, '_git_command', side_effect=ToolError('transient', 'failed ' + token + ' ' + basic, True)):
            with self.assertRaises(ToolError) as caught:
                gateway.execute('executor', 'authrun', 'git.status', {})
            self.assertNotIn(token, str(caught.exception))
            self.assertNotIn(basic, str(caught.exception))
            self.assertIn('[REDACTED]', str(caught.exception))

    @unittest.skipUnless(shutil.which('git'), 'Git not installed')
    def test_git_push_without_token_fails_explicitly_without_network_or_credentials(self):
        self.config.git_remote = 'https://github.com/example/private.git'
        gateway = ToolGateway(self.config)
        gateway.execute('executor', 'authrun', 'file.write', {'path': 'main.py', 'content': 'print(1)\n'})
        target = gateway.execute('executor', 'authrun', 'git.commit', {'message': 'local only'})['pushTarget']
        with mock.patch('backend.tools.socket.getaddrinfo', side_effect=public_dns):
            pending = gateway.execute('executor', 'authrun', 'git.push', target)
            with self.assertRaisesRegex(ToolError, 'GIT_TOKEN is not configured'):
                gateway.execute('executor', 'authrun', 'git.push', pending['args'], approved=True)
        for invalid in ('unsafe\nheader', 'unsafe\rheader', 'unsafe token', 123):
            self.config.git_token = invalid
            with self.assertRaises(ToolError):
                ToolGateway(self.config)


if __name__ == '__main__':
    unittest.main()
