"""Field-aware retrieval regressions: relevance, isolation and immutable data."""
from pathlib import Path
import copy
import hashlib
import json
import tempfile
import unittest

from backend.memory import MemoryService
from backend.persistence import Repository
from benchmarks.memory_retrieval import FIXTURE, evaluate


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp.name))
        self.now = 1_790_000_000.0
        self.memory = MemoryService(self.repo, clock=lambda: self.now)

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def add(self, identifier, *, owner='executor', **changes):
        instant = self.memory._iso(self.now)
        item = {'id': identifier, 'memorySchema': 2, 'owner': owner, 'ownerAgentId': owner,
                'kind': 'procedural', 'status': 'verified', 'verified': True,
                'confirmation': {'type': 'human', 'id': 'approval-' + identifier},
                'visibility': 'group', 'channelId': 'group', 'confidence': .75,
                'lastActivityAt': instant, 'createdAt': instant, 'uses': 0,
                'title': 'Handle an overwrite conflict', 'trigger': 'overwrite conflict',
                'conditions': 'existing file changed', 'lesson': 'Preserve user edits.',
                'sourceTaskId': 'task-' + identifier, 'evidence': ['call-' + identifier]}
        item.update(changes)
        self.repo.add_memory(owner, item)
        return item

    def test_exact_lesson_dedup_is_only_within_owner_and_scope(self):
        self.add('group-old', lesson='  Preserve user edits. ', confidence=.7)
        self.add('group-new', lesson='Preserve user edits.', confidence=.9)
        self.add('private', visibility='private', channelId='executor')
        self.add('shared-other-owner', owner='research', visibility='shared',
                 shareConfirmation={'type': 'human', 'id': 'share-approval'})
        self.add('private-other-owner', owner='research', visibility='private', channelId='executor')
        before = copy.deepcopy(self.repo.all_memories())
        group = self.memory.recall('executor', 'overwrite conflict')
        self.assertEqual({item['id'] for item in group}, {'group-new'})
        private = self.memory.recall('executor', 'overwrite conflict', 'executor')
        self.assertEqual({item['id'] for item in private}, {'group-new', 'private'})
        explicit = self.memory.recall('executor', 'overwrite conflict', include_shared=True)
        self.assertEqual({item['id'] for item in explicit}, {'group-new', 'shared-other-owner'})
        self.assertEqual(self.repo.all_memories(), before, 'retrieval does not merge, delete or modify original evidence')
        private[0]['evidence'].append('injected')
        self.assertEqual(self.repo.all_memories(), before, 'retrieval returns independent snapshots')

    def test_distinct_applicability_or_identifier_case_is_not_a_duplicate(self):
        self.add('base', lesson='Read config.py before an overwrite.')
        self.add('case-sensitive', lesson='Read Config.py before an overwrite.')
        self.add('other-trigger', lesson='Read config.py before an overwrite.', trigger='deployment overwrite')
        self.add('other-condition', lesson='Read config.py before an overwrite.', conditions='remote deployment file')
        self.add('other-kind', lesson='Read config.py before an overwrite.', kind='semantic')
        ranked = self.memory.recall('executor', 'overwrite config', limit=10)
        self.assertEqual({item['id'] for item in ranked},
                         {'base', 'case-sensitive', 'other-trigger', 'other-condition', 'other-kind'})

    def test_repeated_identical_receipts_do_not_change_unique_ranking(self):
        records = [
            {'trigger':'eta epsilon zeta gamma beta iota kappa alpha delta',
             'conditions':'eta kappa epsilon iota', 'title':'zeta eta gamma epsilon alpha beta iota kappa theta',
             'lesson':'iota beta kappa gamma delta alpha'},
            {'trigger':'theta gamma eta kappa epsilon iota alpha beta zeta', 'conditions':'zeta',
             'title':'theta', 'lesson':'kappa alpha eta beta iota epsilon'},
            {'trigger':'delta iota zeta alpha', 'conditions':'beta epsilon delta zeta iota',
             'title':'iota kappa gamma theta zeta eta epsilon beta alpha', 'lesson':'beta epsilon zeta alpha eta gamma'}]
        for index, record in enumerate(records):
            self.add('original-' + str(index), confidence=.8, **record)
        before = [item['lesson'] for item in self.memory.recall('executor', 'alpha beta')]
        for index in range(30):
            self.add('duplicate-' + str(index), confidence=.8, **records[0])
        after = [item['lesson'] for item in self.memory.recall('executor', 'alpha beta')]
        self.assertEqual(after, before)

    def test_ineligible_records_cannot_influence_corpus_ranking(self):
        self.add('target', lesson='Read the exact sha256 before overwriting.', trigger='sha256 overwrite conflict')
        self.add('other', lesson='Keep a separate backup.', trigger='backup overwrite')
        query = 'sha256 overwrite conflict backup'
        before = self.memory.recall('executor', query)
        for number in range(12):
            common = {'lesson': 'sha256 overwrite conflict backup ' + str(number), 'confidence': 1.0}
            self.add('candidate-' + str(number), status='candidate', confirmation=None, **common)
            self.add('private-' + str(number), visibility='private', channelId='research', **common)
            self.add('foreign-' + str(number), owner='reviewer', **common)
            self.add('unshared-' + str(number), owner='research', visibility='shared', **common)
            self.add('stale-' + str(number), needsReconfirmation=True, **common)
            self.add('weak-' + str(number), **{**common, 'confidence': .1})
        self.assertEqual(self.memory.recall('executor', query), before)

    def test_applicability_field_outweighs_incidental_lesson_mention(self):
        self.add('timeout', title='Execution timeout', trigger='Docker code.run timeout',
                 conditions='container starts', lesson='Inspect the infinite loop.', confidence=.75)
        self.add('daemon', title='Docker daemon unavailable', trigger='Docker daemon unavailable',
                 conditions='container cannot start',
                 lesson='Start the Docker daemon; code.run timeout is a different failure.', confidence=.95)
        ranked = self.memory.recall('executor', 'Docker code.run timeout')
        self.assertEqual(ranked[0]['id'], 'timeout')
        self.assertEqual({item['id'] for item in ranked}, {'timeout', 'daemon'})

    def test_confidence_recency_limits_and_empty_queries_still_apply(self):
        self.add('older', title='old approach', lesson='Keep the user revision.', confidence=.9,
                 lastActivityAt=self.memory._iso(self.now - 90 * 86400))
        self.add('recent', title='new approach', lesson='Keep the user changes.', confidence=.75)
        self.assertEqual(self.memory.recall('executor', 'overwrite conflict', limit=1)[0]['id'], 'recent')
        self.assertEqual(self.memory.recall('executor', 'overwrite', limit=0), [])
        self.assertEqual(self.memory.recall('executor', '', limit=4), [])
        self.assertEqual(self.memory.recall('executor', 'unrelated astrophysics'), [])

    def test_frozen_bilingual_fixture_improves_aggregate_retrieval(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                         '4de9afd2454b2b63088d3529ef2e22afe16e9d803f5cc2eedfcf37367b0371c4')
        baseline = json.loads(FIXTURE.with_name('memory_retrieval_baseline_v2.json').read_text())
        result = evaluate(Path(__file__).resolve().parents[1])
        self.assertEqual(result['fixtureSha256'], baseline['fixtureSha256'])
        self.assertEqual(result['metrics']['queries'], 24)
        self.assertEqual(len(result['results']), len(baseline['results']))
        for metric in ('top1', 'mrr', 'precision3'):
            self.assertGreater(result['metrics'][metric], baseline['metrics'][metric], metric)
        self.assertEqual(result['metrics']['duplicateSlots3'], 0)
        # Full query rankings make subgroup regressions visible; aggregate gains
        # do not assert every query/language improved or claim agent task gains.
        self.assertEqual(set(result['byLanguage']), {'en', 'zh'})


if __name__ == '__main__':
    unittest.main()
