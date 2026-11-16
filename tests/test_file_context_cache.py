"""Independent read-cache behavior under realistic protocol/context pressure; no model calls."""
import copy
import hashlib
import json
import unittest

from backend.runtime import compress_messages

PREFIX = '动作的真实结果：'


def tool(name, args, result):
    return [{'role': 'assistant', 'content': json.dumps({'type': 'tool', 'name': name, 'args': args}, ensure_ascii=False)},
            {'role': 'user', 'content': PREFIX + json.dumps(result, ensure_ascii=False)}]


def full(path, content):
    return {'ok': True, 'path': path, 'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest(),
            'mode': 'full', 'totalLines': len(content.splitlines()), 'truncated': False}


def actual_results(messages):
    results = []
    for message in messages:
        text = message.get('content', '')
        if message.get('role') == 'user' and text.startswith(PREFIX):
            try:
                value = json.loads(text[len(PREFIX):])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict): results.append(value)
    return results


def noise(count=10, width=2000):
    return [{'role': 'user', 'content': '执行进度收据：' + json.dumps({'turn': number,
        'instruction': 'Retain existing private source; do not claim unperformed testing.', 'prose': 'n' * width})}
        for number in range(count)]


def padded(label, size):
    beginning, ending = '/* ' + label + ' BEGIN */\n', '\n/* ' + label + ' END */'
    return beginning + 'x' * (size - len(beginning) - len(ending)) + ending


class FileContextCacheTests(unittest.TestCase):
    def base(self, system=900, mandatory=500):
        context = {'runGoal': 'Independently test and finish the existing product.',
                   'task': {'id': 'task-dev', 'title': 'Continue the existing app', 'goal': 'G' * mandatory},
                   'ownArtifacts': [], 'authorizedArtifacts': [], 'dependencyResults': [],
                   'progressReceipt': {'privateFiles': [], 'remainingAfterThisTurn': 12}}
        return [{'role': 'system', 'content': padded('server-owned protocol', system)},
                {'role': 'user', 'content': json.dumps(context)}]

    def test_four_distinct_modules_and_real_browser_failure_survive_repeated_40k_compression(self):
        sources = {path: padded(path, size) for path, size in [('app/index.html', 4100),
            ('app/config.js', 4300), ('app/store.js', 4200), ('app/app.js', 4400)]}
        messages = self.base(system=13200, mandatory=5700)
        failed = {'ok': False, 'runtime': 'browser', 'error': 'QA-FAIL: owner cancellation did not update the occupied cell',
                  'browser': {'executed': True, 'assertions': [{'selector': '#occupied', 'passed': False,
                      'expected': 'Free', 'actual': 'Alice'}], 'pageErrors': [], 'httpErrors': []}}
        for round_number in range(3):
            for path, content in sources.items():
                messages += tool('file.read', {'path': path}, full(path, content))
            if round_number == 1:
                messages += tool('code.test', {'language': 'browser', 'path': 'app/index.html',
                    'checks': [{'action': 'assert_text', 'selector': '#occupied', 'text': 'Free'}]}, failed)
            messages += noise(2, 300)
        original = copy.deepcopy(messages)
        for repetition in range(3):
            before = copy.deepcopy(messages)
            compact = compress_messages(messages, 40000)
            self.assertLessEqual(sum(len(item['content']) for item in compact), 40000)
            self.assertEqual(compact[0]['content'], original[0]['content'])
            results = actual_results(compact)
            for path, content in sources.items():
                rows = [row for row in results if row.get('path') == path and row.get('content') == content]
                self.assertTrue(rows, f'actual complete {path} body missing after compression {repetition}')
                self.assertTrue(all(row['sha256'] == hashlib.sha256(content.encode()).hexdigest() for row in rows))
            self.assertTrue(any(row.get('error') == failed['error'] for row in results), 'latest actual browser failure must remain readable')
            self.assertEqual(messages, before, 'helper must not mutate its input')
            messages = compact + noise(8, 3000)

    def test_successful_write_or_patch_invalidates_old_read_and_new_sha_is_retained(self):
        for name, args in [('file.write', {'path': 'app.js', 'content': 'NEW_SOURCE'}),
                           ('file.patch', {'path': 'app.js', 'old': 'OLD_SOURCE', 'new': 'NEW_SOURCE'})]:
            with self.subTest(tool=name):
                old = full('app.js', 'OLD_SOURCE')
                new = full('app.js', 'NEW_SOURCE')
                history = self.base() + tool('file.read', {'path': 'app.js'}, old)
                history += tool(name, args, {'ok': True, 'path': 'app.js', 'sha256': new['sha256']})
                history += noise()
                compact = compress_messages(history, 7000)
                self.assertFalse(any(row.get('path') == 'app.js' and row.get('content') == old['content']
                                     for row in actual_results(compact)), 'mutated file cannot keep an old body as current cache')
                updated = history + tool('file.read', {'path': 'app.js'}, new) + noise()
                compact = compress_messages(updated, 7000)
                rows = [row for row in actual_results(compact) if row.get('path') == 'app.js' and 'content' in row]
                self.assertTrue(any(row.get('content') == new['content'] and row.get('sha256') == new['sha256'] for row in rows))
                self.assertFalse(any(row.get('content') == old['content'] for row in rows))

    def test_failed_patch_does_not_invalidate_the_last_real_source_read(self):
        original = full('app.js', 'KEEP_SOURCE_AFTER_REJECTED_PATCH')
        messages = self.base() + tool('file.read', {'path': 'app.js'}, original)
        messages += tool('file.patch', {'path': 'app.js', 'old': 'not found', 'new': 'unused'},
                         {'ok': False, 'error': {'code': 'parameter', 'message': 'old text not unique'}})
        messages += noise()
        results = actual_results(compress_messages(messages, 7000))
        self.assertTrue(any(row.get('path') == 'app.js' and row.get('content') == original['content'] for row in results))

    def test_oversized_source_uses_valid_json_omission_and_targeted_reread_metadata(self):
        source = full('huge.js', padded('large real source', 50000))
        messages = self.base() + tool('file.read', {'path': 'huge.js'}, source) + noise()
        compact = compress_messages(messages, 7000)
        rows = [row for row in actual_results(compact) if row.get('path') == 'huge.js']
        self.assertTrue(rows, 'even an omitted body needs a reliable path/hash record')
        marker = next((row for row in rows if row.get('contentOmitted') is True), None)
        self.assertIsNotNone(marker)
        self.assertEqual(marker['sha256'], source['sha256'])
        self.assertTrue(marker.get('contextBudgetReason'))
        self.assertNotIn('content', marker, 'do not truncate source bytes into a different pretend complete file')
        encoded = json.dumps(marker)
        self.assertTrue(any(word in encoded.lower() for word in ('startline', 'contextlines', 'range', 'find')),
                        'the omission record must tell the model to reread a targeted range/find window')
        for message in compact:
            if message['content'].startswith(PREFIX):
                json.loads(message['content'][len(PREFIX):])

    def test_partial_line_and_find_reads_remain_explicitly_partial(self):
        source_lines = [f'// line {number}\n' for number in range(1, 101)]
        source_lines[49] = 'function match() { /* ACTUAL_FIND_BODY */ }\n'
        source = ''.join(source_lines)
        digest = hashlib.sha256(source.encode()).hexdigest()
        lines = {'ok': True, 'path': 'range.js', 'sha256': digest, 'mode': 'lines', 'totalLines': 100,
                 'startLine': 20, 'endLine': 25, 'content': ''.join(source_lines[19:25]), 'truncated': False,
                 'partialFirstLine': False, 'partialLastLine': False}
        find = {'ok': True, 'path': 'find.js', 'sha256': digest, 'mode': 'find', 'totalLines': 100,
                'find': 'function match', 'contextLines': 2, 'matchCount': 1, 'returnedMatches': 1,
                'matches': [{'matchLine': 50, 'startLine': 48, 'endLine': 52,
                             'content': ''.join(source_lines[47:52]), 'truncated': False}], 'truncated': False}
        messages = self.base() + tool('file.read', {'path': 'range.js', 'startLine': 20, 'endLine': 25}, lines)
        messages += tool('file.read', {'path': 'find.js', 'find': 'function match', 'contextLines': 2}, find) + noise()
        rows = actual_results(compress_messages(messages, 7000))
        result_range = next(row for row in rows if row.get('path') == 'range.js' and row.get('content') == lines['content'])
        self.assertEqual(result_range['mode'], 'lines')
        self.assertEqual((result_range['startLine'], result_range['endLine'], result_range['totalLines']), (20, 25, 100))
        result_find = next(row for row in rows if row.get('path') == 'find.js' and row.get('matches'))
        self.assertEqual(result_find['mode'], 'find')
        self.assertEqual(result_find['matches'][0]['content'], find['matches'][0]['content'])
        self.assertNotIn('content', result_find)

    def test_unpaired_mismatched_or_invalid_json_is_not_promoted_to_a_valid_full_file_cache(self):
        messages = self.base()
        orphan = full('orphan.js', 'ORPHAN_CONTENT_NOT_A_TRUSTED_READ')
        messages.append({'role': 'user', 'content': PREFIX + json.dumps(orphan)})
        messages += tool('file.read', {'path': 'real.js'}, full('real.js', 'ACTUAL_PAIRED_SOURCE'))
        messages += tool('file.read', {'path': 'requested.js'}, full('different.js', 'MISMATCHED_PATH_SOURCE'))
        messages.append({'role': 'assistant', 'content': json.dumps({'type': 'tool', 'name': 'file.read', 'args': {'path': 'broken.js'}})})
        messages.append({'role': 'user', 'content': PREFIX + '{"ok":true,"path":"broken.js","content":"BROKEN_JSON_SOURCE"'})
        messages += noise()
        results = actual_results(compress_messages(messages, 7000))
        current_bodies = {row.get('content') for row in results if row.get('mode') == 'full' and not row.get('contentOmitted')}
        self.assertIn('ACTUAL_PAIRED_SOURCE', current_bodies)
        self.assertNotIn(orphan['content'], current_bodies)
        self.assertNotIn('MISMATCHED_PATH_SOURCE', current_bodies)
        self.assertNotIn('BROKEN_JSON_SOURCE', current_bodies)


if __name__ == '__main__':
    unittest.main()
