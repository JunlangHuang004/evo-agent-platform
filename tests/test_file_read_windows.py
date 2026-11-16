"""Precise, bounded file-read windows keep the original confinement and full-file integrity."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest

from backend.artifacts import ArtifactStore
from backend.sandbox import ToolError, MAX_FILE_BYTES
from backend.tools import (ToolGateway, MAX_READ_WINDOW_BYTES, MAX_READ_WINDOW_LINES,
                           MAX_FIND_MATCHES, MAX_FIND_SNIPPET_BYTES)


class FileReadWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(data_dir=Path(self.temp.name) / 'runtime',
                                      docker_image='python:3.11-alpine', git_remote='')
        self.gateway = ToolGateway(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, content, path='source.js', agent='executor'):
        self.gateway.execute(agent, 'run1', 'file.write', {'path': path, 'content': content})
        return path

    def read(self, path='source.js', agent='executor', **options):
        return self.gateway.execute(agent, 'run1', 'file.read', {'path': path, **options})

    def exact_slice(self, source, window):
        lines = source.splitlines(keepends=True)
        start = sum(map(len, lines[:window['startLine'] - 1])) + window['startColumn'] - 1
        end = sum(map(len, lines[:window['endLine'] - 1])) + window['endColumn']
        self.assertEqual(window['content'], source[start:end])
        self.assertLessEqual(len(window['content'].encode()), MAX_READ_WINDOW_BYTES)

    def test_full_read_remains_byte_exact_with_unicode_crlf_and_complete_sha(self):
        source = 'α\r\nbeta\n末尾'
        self.put(source)
        result = self.read()
        self.assertEqual(result['content'], source)
        self.assertEqual(result['sha256'], hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(result['totalLines'], 3)
        self.assertEqual(result['mode'], 'full')
        self.assertFalse(result['truncated'])

    def test_inclusive_line_windows_keep_original_endings_and_full_file_hash(self):
        source = 'α\r\nbeta\n末尾'
        self.put(source)
        result = self.read(startLine=2, endLine=3)
        self.assertEqual(result['content'], 'beta\n末尾')
        self.assertEqual((result['startLine'], result['endLine']), (2, 3))
        self.assertEqual(result['mode'], 'lines')
        self.assertFalse(result['truncated'])
        self.assertFalse(result['partialFirstLine']); self.assertFalse(result['partialLastLine'])
        self.assertEqual(result['sha256'], self.read()['sha256'])
        self.exact_slice(source, result)
        self.assertEqual(self.read(endLine=1)['content'], 'α\r\n')
        self.assertEqual(self.read(startLine=2)['content'], 'beta\n末尾')
        beyond_eof = self.read(startLine=2, endLine=200)
        self.assertEqual(beyond_eof['endLine'], 3)
        self.assertFalse(beyond_eof['truncated'], 'natural EOF is distinct from a safety-limit truncation')

    def test_empty_file_trailing_newline_and_out_of_range_are_explicit(self):
        self.put('')
        result = self.read()
        self.assertEqual(result['totalLines'], 0); self.assertEqual(result['content'], '')
        result = self.read(startLine=1)
        self.assertTrue(result['outOfRange']); self.assertIsNone(result['startLine'])
        self.assertIsNone(result['endLine']); self.assertFalse(result['truncated'])
        self.put('one\n', 'one.txt')
        self.assertEqual(self.read('one.txt')['totalLines'], 1)
        result = self.read('one.txt', startLine=2, endLine=5)
        self.assertEqual(result['content'], ''); self.assertTrue(result['outOfRange'])
        self.assertEqual(result['requestedStartLine'], 2)

    def test_invalid_or_mixed_modes_fail_without_loosening_path_permissions(self):
        self.put('source')
        for options in [dict(startLine=0), dict(startLine=True), dict(startLine=1.5), dict(endLine=-1),
                        dict(startLine=3, endLine=2), dict(find=''), dict(find=None), dict(find='x\0'),
                        dict(find='x' * 1025), dict(find='x', contextLines=21), dict(find='x', contextLines=-1),
                        dict(find='x', contextLines=True), dict(contextLines=1),
                        dict(find='x', startLine=1), dict(find='x', endLine=1)]:
            with self.subTest(options=options), self.assertRaises(ToolError): self.read(**options)
        for path in ['../executor/run1/source.js', '/etc/passwd', '.env', 'imports/../source.js']:
            for options in [dict(startLine=1), dict(find='source')]:
                with self.subTest(path=path, options=options), self.assertRaises(ToolError): self.read(path, **options)

    def test_line_and_utf8_byte_caps_report_exact_partial_ranges(self):
        source = ''.join(f'line {i}\n' for i in range(300))
        self.put(source)
        result = self.read(startLine=1, endLine=300)
        self.assertEqual(result['endLine'], MAX_READ_WINDOW_LINES)
        self.assertTrue(result['truncated']); self.assertFalse(result['partialLastLine'])
        self.exact_slice(source, result)
        huge = '😀' * 20000
        self.put(huge, 'long.js')
        result = self.read('long.js', startLine=1, endLine=1)
        self.assertEqual(len(result['content'].encode()), MAX_READ_WINDOW_BYTES)
        self.assertTrue(result['partialLastLine']); self.assertTrue(result['truncated'])
        self.assertEqual(result['endLine'], 1)
        self.assertEqual(result['endColumn'], MAX_READ_WINDOW_BYTES // 4)
        self.exact_slice(huge, result)

    def test_find_is_case_sensitive_literal_with_exact_match_and_context_locations(self):
        source = "before\r\nFunction book()\r\nfunction book() { return 'a+b.*'; }\r\nafter\r\n"
        self.put(source)
        result = self.read(find='function book', contextLines=1)
        self.assertEqual(result['mode'], 'find')
        self.assertNotIn('content', result)
        self.assertEqual((result['matchCount'], result['returnedMatches']), (1, 1))
        match = result['matches'][0]
        self.assertEqual((match['matchLine'], match['matchColumn']), (3, 1))
        self.assertEqual((match['startLine'], match['endLine']), (2, 4))
        self.assertEqual(match['content'], ''.join(source.splitlines(keepends=True)[1:4]))
        self.assertFalse(result['truncated'])
        self.exact_slice(source, match)
        literal = self.read(find='a+b.*', contextLines=0)
        self.assertEqual(literal['matchCount'], 1, 'regex metacharacters are literal')
        missing = self.read(find='FUNCTION', contextLines=0)
        self.assertEqual(missing['matches'], []); self.assertEqual(missing['matchCount'], 0)
        self.assertFalse(missing['truncated'])

    def test_find_supports_multiline_literals_and_nonoverlapping_match_count(self):
        source = 'aa\nbb\ncc\n'
        self.put(source)
        result = self.read(find='b\nc', contextLines=0)
        match = result['matches'][0]
        self.assertEqual((match['matchLine'], match['matchColumn']), (2, 2))
        self.assertEqual((match['matchEndLine'], match['matchEndColumn']), (3, 1))
        self.assertIn('b\nc', match['content']); self.exact_slice(source, match)
        self.put('aaa', 'overlap.txt')
        self.assertEqual(self.read('overlap.txt', find='aa')['matchCount'], 1)

    def test_find_long_minified_line_never_omits_the_actual_match(self):
        source = '😀' * 16000 + 'function rare方法() {}' + '尾' * 1000
        self.put(source)
        result = self.read(find='function rare方法', contextLines=0)
        match = result['matches'][0]
        self.assertIn('function rare方法', match['content'])
        self.assertTrue(match['partialFirstLine']); self.assertTrue(result['truncated'])
        self.assertLessEqual(len(match['content'].encode()), MAX_FIND_SNIPPET_BYTES)
        self.exact_slice(source, match)
        self.assertEqual(result['sha256'], hashlib.sha256(source.encode()).hexdigest())

    def test_find_match_count_and_total_snippet_caps_are_not_silent(self):
        source = 'needle\n' * 30
        self.put(source)
        result = self.read(find='needle', contextLines=0)
        self.assertEqual(result['matchCount'], 30)
        self.assertEqual(result['returnedMatches'], MAX_FIND_MATCHES)
        self.assertTrue(result['truncated'])
        source = ''.join('x' * 5000 + ' needle\n' for _ in range(20))
        self.put(source, 'large.txt')
        result = self.read('large.txt', find='needle', contextLines=1)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(sum(len(item['content'].encode()) for item in result['matches']), MAX_READ_WINDOW_BYTES)
        for match in result['matches']:
            self.assertIn('needle', match['content']); self.exact_slice(source, match)

    def test_authorized_imports_support_both_modes_without_cross_owner_access(self):
        source = 'header\nfunction shared() {}\nfooter\n'
        self.put(source, 'shared.js', agent='research')
        store = ArtifactStore(self.config.data_dir)
        artifact = store.register('research', 'run1', 'shared.js', 'research-task')
        with self.assertRaises(ToolError): self.read('shared.js', agent='reviewer', find='shared')
        grant = store.grant(artifact['id'], 'reviewer', 'run1', 'orchestrator:review-task')
        path = 'imports/' + grant['path']
        result = self.read(path, agent='reviewer', startLine=2, endLine=2)
        self.assertEqual(result['content'], 'function shared() {}\n')
        self.assertEqual(result['sha256'], artifact['sha256'])
        self.assertEqual(self.read(path, agent='reviewer', find='shared', contextLines=0)['matchCount'], 1)
        with self.assertRaises(ToolError): self.read(path, agent='executor', find='shared')
        with self.assertRaises(ToolError): self.read(path, agent='research', startLine=1)

    def test_windowing_does_not_bypass_symlink_hardlink_or_original_size_limits(self):
        work = self.gateway.workspace('executor', 'run1')
        outside = Path(self.temp.name) / 'outside.txt'; outside.write_text('secret')
        (work / 'link.txt').symlink_to(outside); os.link(outside, work / 'hard.txt')
        (work / 'huge.txt').write_bytes(b'x' * (MAX_FILE_BYTES + 1))
        for path in ['link.txt', 'hard.txt', 'huge.txt']:
            for options in [dict(startLine=1, endLine=1), dict(find='x')]:
                with self.subTest(path=path, options=options), self.assertRaises(ToolError): self.read(path, **options)


if __name__ == '__main__':
    unittest.main()
