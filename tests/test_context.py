"""Bounded working context remains useful without duplicating short histories."""
import copy
import json
import unittest
from backend.runtime import compress_messages


class ContextTests(unittest.TestCase):
    def test_giant_initial_context_and_single_recent_result_are_bounded(self):
        context = {
            'runGoal': 'Create a tested report',
            'task': {'id': 'task-1', 'goal': 'Verify imported data', 'acceptanceCriteria': ['test output']},
            'dependencyResults': [{'id': 'task-0', 'outputs': ['artifact-input'], 'result': 'evidence ' * 50000,
                                   'acceptanceCriteria': ['Independently validate the output'],
                                   'validation': {'verdict': 'pass', 'verified': True, 'targetRevision': 3,
                                                  'artifactHashes': {'artifact-input': 'a'*64}, 'reviewerTaskId': 'task-review'}}],
            'sourceReferences': [{'url': 'https://docs.python.org/3/library/json.html', 'title': 'JSON docs', 'callId': 'call-source'}],
            'authorizedArtifacts': [{'id': 'artifact-input', 'name': 'input.csv', 'path': 'imports/artifact-input/input.csv', 'content': 'private bulk ' * 50000}],
            'ownArtifacts': [{'id': 'artifact-own', 'name': 'report.md', 'content': 'bulk ' * 50000}],
        }
        messages = [
            {'role': 'system', 'content': 'Protocol: return one JSON action. ' * 40},
            {'role': 'user', 'content': json.dumps(context)},
            {'role': 'user', 'content': 'RESULT-BEGIN ' + 'large tool output ' * 50000 + ' RESULT-END'},
        ]
        original = copy.deepcopy(messages)
        compressed = compress_messages(messages, 4000)
        self.assertEqual(messages, original, 'helper must not mutate source/audit records')
        self.assertLessEqual(sum(len(item['content']) for item in compressed), 4000)
        self.assertEqual(len(compressed), 3, 'short history must not duplicate head into tail')
        core = json.loads(compressed[1]['content'])
        self.assertEqual(core['task']['id'], 'task-1')
        self.assertEqual(core['task']['goal'], 'Verify imported data')
        self.assertEqual(core['authorizedArtifacts'][0]['id'], 'artifact-input')
        self.assertEqual(core['dependencyResults'][0]['outputs'], ['artifact-input'])
        self.assertTrue(core['dependencyResults'][0]['validation']['verified'])
        self.assertEqual(core['dependencyResults'][0]['validation']['targetRevision'],3)
        self.assertEqual(core['dependencyResults'][0]['validation']['artifactHashes'],{'artifact-input':'a'*64})
        self.assertEqual(core['dependencyResults'][0]['acceptanceCriteria'],['Independently validate the output'])
        self.assertEqual(core['sourceReferences'][0]['url'],'https://docs.python.org/3/library/json.html')
        self.assertNotIn('content', core['ownArtifacts'][0])
        self.assertTrue(core['_contextTruncated'])
        self.assertIn('[TRUNCATED]', compressed[-1]['content'])
        self.assertIn('RESULT-BEGIN', compressed[-1]['content'])
        self.assertIn('RESULT-END', compressed[-1]['content'])
        for limit in (0, 1, 2, 20, 200, 1000, 8000):
            with self.subTest(limit=limit):
                self.assertLessEqual(sum(len(item['content']) for item in compress_messages(messages, limit)), limit)

    def test_normal_messages_unchanged_and_old_turns_dropped_once(self):
        small = [{'role': 'system', 'content': 'protocol'}, {'role': 'user', 'content': 'goal'}]
        self.assertEqual(compress_messages(small, 100), small)
        long = small + [{'role': 'user', 'content': str(i) + ' ' + 'data ' * 1000} for i in range(10)]
        compressed = compress_messages(long, 1000)
        self.assertEqual([item['content'].split(' ', 1)[0] for item in compressed[2:]], ['6', '7', '8', '9'])
        self.assertLessEqual(sum(len(item['content']) for item in compressed), 1000)


if __name__ == '__main__':
    unittest.main()
