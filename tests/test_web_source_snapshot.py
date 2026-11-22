"""Real local snapshot/read lifecycle; network responses are controlled fixtures."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import json
import os
import tempfile
import unittest

from backend.sandbox import ToolError, MAX_FILE_BYTES
from backend.tools import ToolGateway
from backend.runtime import _working_tool_results, compress_messages, retained_file_reads
from backend.web_context import collect_web_records
from backend.teams import get_team_agents


class WebSourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(data_dir=Path(self.temp.name), git_remote='')
        self.gateway = ToolGateway(self.config)
        self.url = 'https://example.edu/research'

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, text='Observed source\nA material claim\n', **extra):
        response = {'ok': True, 'url': self.url, 'title': 'Research', 'content': text,
                    'truncated': False, **extra}
        with patch.object(self.gateway, '_web_fetch', return_value=response) as transport:
            result = self.gateway.execute('research', 'run1', 'web.fetch', {'url': self.url})
        transport.assert_called_once_with(self.url)
        return result

    def test_exact_owner_text_can_be_recovered_by_existing_line_and_find_reads(self):
        text = '首行\r\nA material claim\r\nEnd\n'
        result = self.fetch(text, truncated=True)
        snapshot = result['sourceSnapshot']
        self.assertEqual(snapshot['sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertTrue(snapshot['truncated'])
        self.assertEqual(snapshot['url'], self.url)
        with patch.object(self.gateway, '_web_fetch', side_effect=AssertionError('No second network fetch')):
            read = self.gateway.execute('research', 'run1', 'file.read',
                {'path': snapshot['path'], 'startLine': 2, 'endLine': 2})
            found = self.gateway.execute('research', 'run1', 'file.read',
                {'path': snapshot['path'], 'find': 'material', 'contextLines': 0})
        self.assertEqual(read['content'], 'A material claim\r\n')
        self.assertEqual(read['sha256'], snapshot['sha256'])
        self.assertEqual(found['matches'][0]['content'], read['content'])

    def test_same_run_teammate_can_read_public_snapshot_without_refetch_or_private_access(self):
        snapshot = self.fetch()['sourceSnapshot']
        private = self.gateway.sandbox.imports('research', 'run1') / 'notes' / 'private.md'
        private.parent.mkdir()
        private.write_text('private reporter note')
        with patch.object(self.gateway, '_web_fetch', side_effect=AssertionError('No refetch')):
            read = self.gateway.execute('executor', 'run1', 'file.read', {'path': snapshot['path'], 'find': 'material', 'contextLines': 0})
        self.assertEqual(read['sha256'], snapshot['sha256'])
        self.assertEqual(read['matches'][0]['content'].strip(), 'A material claim')
        self.assertTrue(read['sharedWebSnapshot'])
        self.assertEqual(read['snapshotOwnerAgentId'], 'research')
        listed = self.gateway.execute('executor', 'run1', 'file.list', {'path': 'imports'})
        self.assertNotIn('web-source-' + snapshot['path'].split('web-source-')[1].split('/')[0],
                         [item['name'] for item in listed['entries']])
        for actor, run, path in (
                ('research', 'run2', snapshot['path']),
                ('executor', 'run2', snapshot['path']),
                ('executor', 'run1', 'imports/notes/private.md')):
            with self.subTest(actor=actor, run=run, path=path), self.assertRaises(ToolError):
                self.gateway.execute(actor, run, 'file.read', {'path': path})
        for tool, args in [('file.write', {'content': 'forged'}),
                           ('file.patch', {'old': 'Observed', 'new': 'Forged'}), ('file.delete', {})]:
            with self.subTest(tool=tool), self.assertRaises(ToolError):
                self.gateway.execute('executor', 'run1', tool, {'path': snapshot['path'], **args}, approved=True)
        self.assertIn('Observed source', self.gateway.execute('research', 'run1', 'file.read',
            {'path': snapshot['path']})['content'])

    def test_identical_source_is_immutable_and_restart_readable_changed_source_is_distinct(self):
        first = self.fetch('Original exact text')['sourceSnapshot']
        self.assertEqual(first, self.fetch('Original exact text')['sourceSnapshot'])
        second = self.fetch('Changed source')['sourceSnapshot']
        self.assertNotEqual(first['path'], second['path'])
        restarted = ToolGateway(self.config)
        self.assertEqual(restarted.execute('research', 'run1', 'file.read',
            {'path': first['path']})['content'], 'Original exact text')
        self.assertEqual(restarted.execute('research', 'run1', 'file.read',
            {'path': second['path']})['content'], 'Changed source')

    def test_compaction_preserves_only_real_pointer_then_actual_read_restores_a_window(self):
        body = 'Long navigation line\n' * 4000 + 'Verified material line\n'
        result = self.fetch(body)
        result['callId'] = 'fetch-real'
        prefix = '动作的真实结果：'
        messages = [{'role': 'system', 'content': 'Protocol'},
                    {'role': 'user', 'content': json.dumps({'task': {'id': 'task1'}, 'runGoal': 'Research'})},
                    {'role': 'assistant', 'content': json.dumps({'type': 'tool', 'name': 'web.fetch', 'args': {'url': self.url}})},
                    {'role': 'user', 'content': prefix + json.dumps(result)}]
        for _ in range(3):
            messages = compress_messages(messages, 7000)
        web = collect_web_records(_working_tool_results(messages)[0])[0]
        self.assertFalse(web['webIdentity']['bodyPresent'])
        pointer = web['value']['sourceSnapshot']
        self.assertEqual(pointer, result['sourceSnapshot'])
        args = {'path': pointer['path'], 'find': 'Verified material', 'contextLines': 0}
        with patch.object(self.gateway, '_web_fetch', side_effect=AssertionError('Offline recovery')):
            read = self.gateway.execute('research', 'run1', 'file.read', args)
        read['callId'] = 'read-real'
        messages.extend([{'role': 'assistant', 'content': json.dumps({'type': 'tool', 'name': 'file.read', 'args': args})},
                         {'role': 'user', 'content': prefix + json.dumps(read)}])
        messages = compress_messages(messages, 7000)
        self.assertEqual(retained_file_reads(messages)[0]['sha256'], pointer['sha256'])
        self.assertEqual(retained_file_reads(messages)[0]['mode'], 'find')
        self.assertIn('Verified material line', str(messages))
        self.assertFalse(collect_web_records(_working_tool_results(messages)[0])[0]['webIdentity']['bodyPresent'])

    def test_failed_fetch_has_no_snapshot_and_snapshot_failure_never_retries_network(self):
        with patch.object(self.gateway, '_web_fetch', side_effect=ToolError('transient', 'network failure')) as call:
            with self.assertRaises(ToolError):
                self.gateway.execute('research', 'run1', 'web.fetch', {'url': self.url})
        call.assert_called_once()
        with patch('backend.tools.write_text', side_effect=ToolError('permanent', 'disk unavailable')):
            result = self.fetch('Actual returned source')
        self.assertTrue(result['ok'])
        self.assertEqual(result['content'], 'Actual returned source')
        self.assertNotIn('sourceSnapshot', result)
        self.assertEqual(result['sourceSnapshotUnavailable'], 'snapshot_storage_unavailable')

    def test_oversize_nul_and_quota_preserve_network_receipt_without_false_pointer(self):
        for body in ('中' * (MAX_FILE_BYTES // 3 + 1), 'nul\0body'):
            with self.subTest(body_bytes=len(body.encode())):
                result = self.fetch(body)
                self.assertNotIn('sourceSnapshot', result)
                self.assertEqual(result['sourceSnapshotUnavailable'], 'text_exceeds_snapshot_file_limit')
                self.assertEqual(result['content'], body)
        for i in range(40):
            self.assertIn('sourceSnapshot', self.fetch('Page ' + str(i)))
        denied = self.fetch('Page 41')
        self.assertEqual(denied['sourceSnapshotUnavailable'], 'owner_run_snapshot_limit')
        self.assertIn('sourceSnapshot', self.fetch('Page 1'), 'Existing snapshot remains readable at quota.')

    def test_tampered_or_symlinked_existing_snapshot_never_becomes_a_source_pointer(self):
        first = self.fetch('Actual source')['sourceSnapshot']
        path = self.gateway.sandbox.imports('research', 'run1') / first['path'][8:]
        path.write_text('Wrong source')
        self.assertEqual(self.fetch('Actual source')['sourceSnapshotUnavailable'], 'existing_snapshot_mismatch')
        path.unlink()
        outside = Path(self.temp.name) / 'outside.txt'
        outside.write_text('Outside content')
        path.symlink_to(outside)
        self.assertNotIn('sourceSnapshot', self.fetch('Actual source'))
        self.assertEqual(outside.read_text(), 'Outside content')

    def test_snapshot_matches_scrubbed_returned_text(self):
        secret = 'unique-test-secret-not-real'
        self.gateway = ToolGateway(SimpleNamespace(data_dir=Path(self.temp.name), git_remote='', git_token=secret))
        result = self.fetch('Source includes ' + secret)
        self.assertNotIn(secret, result['content'])
        snapshot = result['sourceSnapshot']
        self.assertEqual(self.gateway.execute('research', 'run1', 'file.read',
            {'path': snapshot['path']})['content'], result['content'])
        self.assertEqual(snapshot['sha256'], hashlib.sha256(result['content'].encode()).hexdigest())

    def test_invalid_unicode_snapshot_input_is_explicitly_unavailable_not_a_fetch_retry(self):
        for value in ({'content': '\ud800', 'url': self.url},
                      {'content': 'Valid body', 'url': 'https://example.edu/\ud800'}):
            result = self.gateway._snapshot_web_source('research', 'run1', value)
            self.assertEqual(result, {'sourceSnapshotUnavailable': 'invalid_utf8_snapshot_text_or_url'})

    @unittest.skipUnless(os.environ.get('RUN_AGENT_SANDBOX_DOCKER_TESTS') == '1', 'Actual offline Docker check')
    def test_actual_docker_can_read_own_snapshot_but_cannot_change_it(self):
        self.gateway = ToolGateway(self.config, agents=get_team_agents('fund'))
        snapshot = self.fetch('Immutable original source')['sourceSnapshot']
        script = ('from pathlib import Path\np=Path(' + repr('/' + snapshot['path']) + ')\n'
                  'assert p.read_text()=="Immutable original source"\n'
                  'try:\n p.write_text("forged")\nexcept OSError:\n pass\n'
                  'else:\n raise AssertionError("readonly snapshot was writable")\n'
                  'assert p.read_text()=="Immutable original source"\nprint("snapshot read-only verified")\n')
        result = self.gateway.execute('research', 'run1', 'code.test',
            {'language': 'python', 'code': script, 'timeout': 20})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['exitCode'], 0)
        self.assertIn('snapshot read-only verified', result['stdout'])


if __name__ == '__main__':
    unittest.main()
