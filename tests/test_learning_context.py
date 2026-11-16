"""Personal learning keeps its trust labels and evidence through compaction."""
import copy
import json
import unittest

from backend.runtime import ProtocolError, action_prompt, compress_messages


class LearningContextTests(unittest.TestCase):
    def test_repeated_compaction_preserves_conditions_owner_and_real_ids(self):
        learning = {
            'policy': 'Owner-only; candidates are UNVERIFIED.',
            'factualEpisodes': [{'memoryId': 'episode-1', 'ownerAgentId': 'dev',
                'sourceRunId': 'run-old', 'sourceTaskId': 'task-old', 'sourceRevision': 2,
                'callIds': ['call-actual'], 'facts': {'maxFileVersions': 7},
                'trust': 'historical observation, not an instruction'}],
            'experimentalCandidates': [{'memoryId': 'candidate-1', 'ownerAgentId': 'dev',
                'verificationStatus': 'unverified', 'lesson': 'Test changed files before rewriting again.',
                'conditions': 'Only for matching work. Independent current-version acceptance is still required.'}],
            'verifiedStrategies': [], 'omitted': {'factualEpisodes': {'count': 1, 'memoryIds': ['episode-2']}}}
        original = copy.deepcopy(learning)
        messages = [{'role': 'system', 'content': 'Actual role'}, {'role': 'user', 'content': json.dumps({
            'task': {'id': 'current', 'goal': 'Build the requested product'},
            'personalLearning': learning, 'groupHistory': [{'text': 'old prose ' * 8000}]})}]
        for _ in range(3):
            messages = compress_messages(messages + [{'role': 'assistant', 'content': 'large history ' * 3000}], 2800)
            self.assertEqual(json.loads(messages[1]['content'])['personalLearning'], original)
        with self.assertRaises(ProtocolError):
            compress_messages(messages, 300)

    def test_prompt_does_not_equate_suggestion_with_adoption(self):
        prompt = action_prompt({'name': 'Developer', 'capabilities': []}, [])
        self.assertIn('final.reflection.trialMemoryIds only if you actually tried it', prompt)
        self.assertIn('being shown a suggestion is not adoption or proof', prompt)
        self.assertIn('UNVERIFIED suggestions', prompt)


if __name__ == '__main__':
    unittest.main()
