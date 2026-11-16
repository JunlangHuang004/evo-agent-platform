"""Memory evolution and durable audit regressions; no model/network calls."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from backend.memory import MemoryService
from backend.persistence import Repository


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp.name))
        self.time = 1_790_000_000.0
        self.memory = MemoryService(self.repo, clock=lambda: self.time)

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def propose(self, owner='executor', **changes):
        args = {'agent_id': owner, 'lesson': 'Use Python csv standard library when an offline dependency fails.',
                'trigger': 'Python offline dependency failure', 'conditions': 'network disabled',
                'source_task': 'source-task', 'source_run': 'source-run', 'evidence': ['call-source'],
                'channel_id': 'group'}
        args.update(changes)
        return self.memory.propose(**args)

    def verified(self, owner='executor', **changes):
        candidate = self.propose(owner, **changes)
        return self.memory.confirm(candidate['id'], owner, True, {'type': 'tool', 'id': 'actual-check-call'})

    def test_confirmation_gate_excludes_candidates_and_model_self_reports(self):
        candidate = self.propose()
        self.assertEqual(candidate['status'], 'candidate')
        self.assertEqual(self.memory.recall('executor', 'Python dependency'), [])
        self.assertEqual(self.repo.search_memories('executor', 'Python dependency'), [])
        for reason in ('', 'the model says tests passed', {'type': 'model', 'id': 'assertion'}, {'type': 'tool', 'id': ''}):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.memory.confirm(candidate['id'], 'executor', True, reason)
        with self.assertRaises(ValueError):
            self.memory.confirm(candidate['id'], 'executor', 'true', 'human:approval')
        confirmed = self.memory.confirm(candidate['id'], 'executor', True, {'type': 'human', 'id': 'approval-real'})
        self.assertEqual(confirmed['confirmation']['id'], 'approval-real')
        self.assertTrue(confirmed['verified'])
        self.assertEqual([item['id'] for item in self.memory.recall('executor', 'Python dependency')], [candidate['id']])
        self.assertEqual(self.memory.recall('executor', 'unrelated astronomy'), [])
        self.assertEqual(self.memory.list('executor')[0]['uses'], 0, 'retrieval is not actual usage')
        self.memory.confirm(candidate['id'], 'executor', False, 'verification withdrawn')
        self.assertEqual(self.memory.recall('executor', 'Python'), [])

    def test_owner_isolation_and_private_scope(self):
        item = self.verified(channel_id='executor')
        self.assertEqual(self.memory.recall('executor', 'Python', 'group'), [])
        self.assertEqual(self.memory.recall('executor', 'Python', 'research'), [])
        self.assertEqual(len(self.memory.recall('executor', 'Python', 'executor')), 1)
        self.assertEqual(self.memory.recall('research', 'Python', 'executor'), [])
        self.assertEqual(self.memory.list('research'), [])
        for operation in (
            lambda: self.memory.confirm(item['id'], 'research', True, 'human:approval'),
            lambda: self.memory.feedback('research', item['id'], True, 'later-task'),
            lambda: self.memory.share(item['id'], 'research', 'human:approval')):
            with self.assertRaises(KeyError):
                operation()
        with self.assertRaises(ValueError):
            self.repo.add_memory('research', {**item, 'id': 'forged'})

    def test_explicit_human_share_is_required_for_cross_owner_knowledge(self):
        item = self.verified(channel_id='executor', kind='semantic')
        self.assertEqual(self.memory.recall('research', 'Python'), [])
        with self.assertRaises(ValueError):
            self.memory.share(item['id'], 'executor', 'tool:check')
        self.memory.share(item['id'], 'executor', {'type': 'human', 'id': 'publish-approval'})
        self.assertEqual(self.memory.recall('research', 'Python'), [], 'Agent recall remains owner-only even for historical shared records')
        shared = self.memory.recall('research', 'Python', include_shared=True)
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]['owner'], 'executor')
        self.assertEqual(shared[0]['shareConfirmation']['id'], 'publish-approval')
        self.assertEqual(self.memory.list('research'), [], 'sharing preserves ownership')

    def test_episode_has_provenance_and_never_becomes_strategy(self):
        run = {'id': 'run-episode', 'participants': ['executor']}
        task = {'id': 'task-episode', 'title': 'Python dependency test', 'agentId': 'executor'}
        episode = self.memory.record_episode('executor', run, task, False, 'dependency missing', ['failed-call'], 'group')
        self.assertEqual(episode['kind'], 'episodic')
        self.assertEqual(episode['call_ids'], ['failed-call'])
        self.assertEqual(episode['source_run'], run['id'])
        self.assertEqual(self.memory.record_episode('executor', run, task, False, 'retry recording', [], 'group')['id'], episode['id'])
        self.assertEqual(len(self.memory.list('executor')), 1)
        self.assertEqual(self.memory.recall('executor', 'Python dependency'), [])
        revised = self.memory.record_episode('executor', run, {**task, 'revision': 1}, True, 'rework fixed the dependency', ['success-call'], 'group')
        self.assertNotEqual(revised['id'], episode['id'])
        self.assertEqual(revised['source_revision'], 1)
        self.assertEqual(len(self.memory.list('executor')), 2, 'rework preserves both original failure and revised outcome')
        with self.assertRaises(ValueError):
            self.memory.confirm(episode['id'], 'executor', True, 'human:approve')
        with self.assertRaises(ValueError):
            self.memory.record_episode('research', run, task, True, 'not its task', [], 'group')

    def test_confirmed_supersession_preserves_history_without_candidate_poisoning(self):
        old = self.verified()
        new = self.propose(lesson='Read pyproject first, then select an available standard library.', supersedes=old['id'])
        self.assertEqual(self.memory.recall('executor', 'Python dependency')[0]['id'], old['id'])
        confirmed = self.memory.confirm(new['id'], 'executor', True, 'independent:review-task')
        stored = self.repo.get_memory(old['id'], 'executor')
        self.assertEqual(stored['status'], 'superseded')
        self.assertEqual(stored['supersededBy'], confirmed['id'])
        self.assertLess(stored['confidence'], old['confidence'])
        self.assertEqual(len(self.memory.list('executor')), 2)
        self.assertEqual([item['id'] for item in self.memory.recall('executor', 'Python dependency')], [new['id']])
        with self.assertRaises(KeyError):
            self.propose(owner='research', supersedes=new['id'])
        with self.assertRaises(ValueError):
            self.propose(channel_id='executor', supersedes=new['id'])

    def test_feedback_is_owner_scoped_deduplicated_and_requires_new_actual_task(self):
        item = self.verified()
        with self.assertRaises(ValueError):
            self.memory.feedback('executor', item['id'], True, 'source-task')
        success = self.memory.feedback('executor', item['id'], True, 'task-later')
        self.assertTrue(success['feedbackApplied'])
        self.assertEqual((success['uses'], success['success_count'], success['failure_count']), (1, 1, 0))
        self.assertGreater(success['confidence'], item['confidence'])
        duplicate = self.memory.feedback('executor', item['id'], False, 'task-later')
        self.assertFalse(duplicate['feedbackApplied'])
        self.assertEqual((duplicate['uses'], duplicate['success_count'], duplicate['failure_count']), (1, 1, 0))
        failure = self.memory.feedback('executor', item['id'], False, 'task-failed-1')
        self.assertLess(failure['confidence'], success['confidence'])
        self.memory.feedback('executor', item['id'], False, 'task-failed-2')
        demoted = self.memory.feedback('executor', item['id'], False, 'task-failed-3')
        self.assertEqual(demoted['status'], 'candidate')
        self.assertTrue(demoted['needsReconfirmation'])
        self.assertEqual(self.memory.recall('executor', 'Python'), [])
        self.assertFalse(self.memory.feedback('executor', item['id'], False, 'task-failed-3')['feedbackApplied'])

    def test_concurrent_duplicate_feedback_has_single_effect(self):
        item = self.verified()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.memory.feedback('executor', item['id'], True, 'one-task'), range(20)))
        self.assertEqual(sum(result['feedbackApplied'] for result in results), 1)
        self.assertEqual(self.repo.get_memory(item['id'], 'executor')['uses'], 1)

    def test_decay_and_capacity_archive_without_deleting_history(self):
        item = self.verified()
        self.time += 40 * 86400
        result = self.memory.maintenance()
        self.assertEqual(result['decayed'], 1)
        self.assertLess(self.repo.get_memory(item['id'], 'executor')['confidence'], item['confidence'])
        self.assertEqual(self.memory.maintenance()['decayed'], 0, 'maintenance does not compound decay for same instant')
        self.time += 200 * 86400
        self.assertEqual(self.memory.maintenance()['archived'], 1)
        self.assertEqual(self.memory.list('executor')[0]['status'], 'archived')
        self.assertEqual(self.memory.recall('executor', 'Python'), [])
        self.memory.capacity = 2
        for index in range(4):
            self.verified(source_task=f'new-{index}', lesson=f'Python dependency strategy number {index}')
        self.assertEqual(self.memory.maintenance()['archived'], 2)
        records = self.memory.list('executor')
        self.assertEqual(sum(record['status'] == 'verified' for record in records), 2)
        self.assertEqual(len(records), 5)

    def test_sqlite_recovery_keeps_confirmation_and_dedup_outcomes(self):
        item = self.verified()
        self.memory.feedback('executor', item['id'], True, 'later-task')
        self.repo.close()
        self.repo = Repository(Path(self.temp.name))
        self.memory = MemoryService(self.repo, clock=lambda: self.time)
        self.assertEqual(self.memory.recall('executor', 'Python')[0]['id'], item['id'])
        self.assertFalse(self.memory.feedback('executor', item['id'], True, 'later-task')['feedbackApplied'])
        self.assertEqual(self.memory.list('executor')[0]['uses'], 1)

    def test_checkpoint_events_effect_audit_survive_restart_and_prevent_replay(self):
        self.repo.save_state({'sessions': {'research': {'messages': [{'text': 'keep me'}]}}})
        run = {'id': 'run-audit', 'title': 'Audit', 'status': 'done', 'startedAt': 1000, 'finishedAt': 3000}
        self.repo.save_run(run, 'group')
        self.repo.save_checkpoint(run['id'], {'phase': 4, 'approvalId': 'approval-only-this-call'})
        event = {'id': 'stable-event', 'type': 'tool_called', 'callId': 'call-1'}
        self.repo.append_event(run['id'], event)
        self.repo.append_event(run['id'], event)
        self.assertEqual(len(self.repo.events(run['id'])), 1)
        with self.assertRaises(ValueError):
            self.repo.append_event(run['id'], {**event, 'callId': 'different'})
        self.assertTrue(self.repo.reserve_effect('effect-1', {'tool': 'git.push', 'branch': 'demo'})['new'])
        self.assertFalse(self.repo.reserve_effect('effect-1', {'branch': 'demo', 'tool': 'git.push'})['new'])
        with self.assertRaises(ValueError):
            self.repo.reserve_effect('effect-1', {'tool': 'git.push', 'branch': 'main'})
        self.repo.close(); self.repo = Repository(Path(self.temp.name))
        self.assertEqual(self.repo.checkpoint(run['id'])['phase'], 4)
        self.assertEqual(len(self.repo.events(run['id'])), 1)
        self.assertEqual(self.repo.load_state()['sessions']['research']['messages'][0]['text'], 'keep me')
        self.assertEqual(self.repo.get_run(run['id']), run)
        self.assertEqual(self.repo.effect('effect-1')['status'], 'reserved', 'uncertain effect stays reserved, not silently replayable')
        self.assertFalse(self.repo.reserve_effect('effect-1', {'tool': 'git.push', 'branch': 'demo'})['new'])
        completed = self.repo.complete_effect('effect-1', {'commit': 'abc', 'ok': True})
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(self.repo.complete_effect('effect-1', {'ok': True, 'commit': 'abc'}), completed)
        with self.assertRaises(ValueError):
            self.repo.complete_effect('effect-1', {'ok': False})
        self.assertEqual(self.repo.evaluation_runs()[0]['status'], 'unassessed')
        self.repo.save_evaluation({'id': 'eval-1', 'runId': run['id'], 'status': 'passed', 'verified': True})
        self.assertEqual(len(self.repo.evaluation_runs()), 1)

    def test_migration_preserves_old_chat_and_requires_legacy_memory_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            db = sqlite3.connect(Path(folder) / 'platform.sqlite')
            db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT NOT NULL); CREATE TABLE runs(id TEXT PRIMARY KEY,channel TEXT NOT NULL,payload TEXT NOT NULL); CREATE TABLE memories(id TEXT PRIMARY KEY,owner TEXT NOT NULL,payload TEXT NOT NULL);')
            chat = {'sessions': {'coordinator': {'messages': [{'role': 'human', 'text': 'old conversation'}]}}}
            db.execute('INSERT INTO state VALUES(1,?)', (json.dumps(chat),))
            legacy = {'id': 'old-memory', 'title': 'Python dependency', 'lesson': 'unconfirmed legacy rule', 'status': 'verified'}
            db.execute('INSERT INTO memories VALUES(?,?,?)', ('old-memory', 'executor', json.dumps(legacy)))
            db.commit(); db.close()
            migrated = Repository(Path(folder))
            try:
                self.assertEqual(migrated.load_state(), chat)
                record = migrated.get_memory('old-memory', 'executor')
                self.assertEqual(record['legacyStatus'], 'verified')
                self.assertEqual(record['status'], 'candidate')
                self.assertEqual(migrated.search_memories('executor', 'Python', 'executor'), [])
                service = MemoryService(migrated)
                service.confirm('old-memory', 'executor', True, 'human:review-legacy')
                self.assertEqual(len(service.recall('executor', 'Python', 'executor')), 1)
            finally:
                migrated.close()


if __name__ == '__main__':
    unittest.main()
