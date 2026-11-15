"""Deterministic contracts use real artifact snapshots; no model/API/Docker."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.agents import AGENTS
from backend.artifacts import ArtifactStore
from backend.contracts import normalize_output_contract, validate_task_output
from backend.runtime import ProtocolError
from backend.sandbox import SandboxManager


class OutputContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(data_dir=Path(self.temp.name))
        self.sandbox = SandboxManager(self.config)
        self.store = ArtifactStore(self.config.data_dir)
        self.task = {'id': 'task-output', 'agentId': 'executor', 'outputs': []}
        self.run = {'id': 'run-now', 'artifacts': []}

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, name='solution.html', content='<html>Actual published output.</html>',
                owner='executor', run='run-now', task='task-output'):
        workspace = self.sandbox.workspace(owner, run)
        (workspace / name).write_text(content, encoding='utf-8')
        item = self.store.register(owner, run, name, task)
        self.run['artifacts'].append(item)
        self.task['outputs'].append(item['id'])
        return item

    def validate(self, text='The actual output is published.'):
        return validate_task_output(self.task, self.run, self.store, text)

    def test_optional_contract_preserves_default_text_and_unassessed_quality(self):
        self.assertIsNone(normalize_output_contract(None))
        result = self.validate('A normal text-only result without code.')
        self.assertEqual(result['status'], 'not_configured')
        self.assertEqual(result['kind'], 'text')
        self.assertEqual(result['semanticQuality'], 'unassessed')
        self.assertEqual(result['issues'], [])
        self.task['outputContract'] = {'kind': 'text'}
        self.assertEqual(self.validate()['status'], 'passed')

    def test_empty_and_oversized_text_are_not_valid_results(self):
        for text in ('', ' \n\t', None, '\0', 'x' * (256 * 1024 + 1), '\ud800'):
            with self.subTest(text_type=type(text).__name__):
                self.assertEqual(self.validate(text)['status'], 'failed')

    def test_contract_schema_rejects_illegal_fields_sizes_and_paths(self):
        invalid = [True, 'artifact', [], {}, {'kind': []}, {'kind': 'code'},
                   {'kind': 'text', 'minCount': 1}, {'kind': 'text', 'callback': 'shell'},
                   {'kind': 'artifact', 'path': '/tmp/result.html'},
                   {'kind': 'artifact', 'minCount': True}, {'kind': 'artifact', 'minCount': 0},
                   {'kind': 'artifact', 'minCount': 41}, {'kind': 'artifact', 'minCount': 1.0},
                   {'kind': 'artifact', 'extensions': '.html'},
                   {'kind': 'artifact', 'extensions': ['html']},
                   {'kind': 'artifact', 'extensions': ['report.html']},
                   {'kind': 'artifact', 'extensions': ['../html']},
                   {'kind': 'artifact', 'extensions': ['.*']},
                   {'kind': 'artifact', 'extensions': ['.tar.gz']},
                   {'kind': 'artifact', 'extensions': ['.HTML', '.html']},
                   {'kind': 'artifact', 'extensions': ['.x' + str(i) for i in range(17)]},
                   {'kind': 'json', 'requiredKeys': 'answer'},
                   {'kind': 'json', 'requiredKeys': ['']},
                   {'kind': 'json', 'requiredKeys': ['a', 'a']},
                   {'kind': 'json', 'requiredKeys': ['a\n']},
                   {'kind': 'json', 'requiredKeys': ['x' * 121]},
                   {'kind': 'json', 'requiredKeys': list(map(str, range(33)))},
                   {'kind': 'json', 'requiredKeys': [], 'extensions': []}]
        for contract in invalid:
            with self.subTest(contract=contract), self.assertRaises(ProtocolError):
                normalize_output_contract(contract)
        self.task['outputContract'] = {'kind': 'artifact', 'minCount': 0}
        with self.assertRaises(ProtocolError):
            self.validate()

    def test_normalization_is_copied_and_explicit(self):
        value = {'kind': 'artifact', 'extensions': ['.HTML']}
        result = normalize_output_contract(value)
        value['extensions'].append('.py')
        self.assertEqual(result, {'kind': 'artifact', 'extensions': ['.html'], 'minCount': 1})
        self.assertEqual(normalize_output_contract({'kind': 'json'}), {'kind': 'json', 'requiredKeys': []})

    def test_prose_claim_and_wrong_extension_do_not_satisfy_artifact_contract(self):
        self.task['outputContract'] = {'kind': 'artifact', 'extensions': ['.html']}
        empty = self.validate('I have created, tested and uploaded the HTML slides.')
        self.assertEqual(empty['status'], 'failed')
        self.assertEqual(empty['matchedArtifactIds'], [])
        self.publish('report.md', '# This is not the requested HTML file.')
        self.assertEqual(self.validate()['status'], 'failed')

    def test_actual_snapshot_counts_while_ancillary_formats_do_not_inflate_count(self):
        self.task['outputContract'] = {'kind': 'artifact', 'extensions': ['.HTML'], 'minCount': 1}
        html = self.publish()
        note = self.publish('notes.md', '# Notes\nAdditional legitimate documentation.')
        (self.sandbox.workspace('executor', 'run-now') / 'solution.html').write_text('Later live-file edit does not replace the published snapshot.')
        result = self.validate()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['matchedArtifactIds'], [html['id']])
        self.assertEqual(set(result['checkedArtifactIds']), {html['id'], note['id']})
        self.assertEqual(result['semanticQuality'], 'unassessed')
        self.task['outputContract']['minCount'] = 2
        self.assertEqual(self.validate()['status'], 'failed')

    def test_empty_published_file_is_not_a_deliverable(self):
        self.task['outputContract'] = {'kind': 'artifact'}
        self.publish(content=' \n\t')
        self.assertEqual(self.validate()['status'], 'failed')

    def test_repeated_publication_of_one_path_does_not_inflate_required_file_count(self):
        self.task['outputContract'] = {'kind': 'artifact', 'extensions': ['.html'], 'minCount': 2}
        first = self.publish()
        repeated = self.store.register('executor', 'run-now', 'solution.html', 'task-output')
        self.run['artifacts'].append(repeated)
        self.task['outputs'].append(repeated['id'])
        result = self.validate()
        self.assertNotEqual(first['id'], repeated['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['matchedArtifactIds'], [repeated['id']])
        self.assertEqual(set(result['checkedArtifactIds']), {first['id'], repeated['id']})
        second_file = self.publish('second.html', '<html>A distinct published file.</html>')
        result = self.validate()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(set(result['matchedArtifactIds']), {repeated['id'], second_file['id']})

    def test_other_task_owner_or_run_artifacts_do_not_count(self):
        self.task['outputContract'] = {'kind': 'artifact'}
        for case in ({'task': 'other-task'}, {'owner': 'reviewer'}, {'run': 'other-run'}):
            with self.subTest(case=case):
                self.run['artifacts'] = []
                self.task['outputs'] = []
                self.publish(**case)
                self.assertEqual(self.validate()['status'], 'failed')

    def test_real_inherited_snapshot_cannot_masquerade_as_new_output(self):
        self.task['outputContract'] = {'kind': 'artifact'}
        original = self.publish(run='old-run', task='old-task')
        source = {'id': 'old-run', 'teamId': 'default', 'channelId': 'group', 'mode': 'task',
                  'status': 'done', 'participants': list(AGENTS), 'artifacts': [original]}
        target = {'id': 'run-now', 'teamId': 'default', 'channelId': 'group', 'mode': 'task',
                  'status': 'running', 'previousRunId': 'old-run', 'participants': list(AGENTS)}
        inherited = self.store.carry_forward(source, target, list(AGENTS))['artifacts'][0]
        self.run['artifacts'] = [inherited]
        self.task.update(id='old-task', outputs=[inherited['id']])
        self.assertEqual(self.validate()['status'], 'failed')

    def test_forged_missing_duplicate_and_tampered_metadata_fail_closed(self):
        self.task['outputContract'] = {'kind': 'artifact'}
        artifact = self.publish()
        original = copy.deepcopy(artifact)
        self.task['outputs'].append(artifact['id'])
        self.assertEqual(self.validate()['status'], 'failed')
        self.task['outputs'] = [artifact['id']]
        self.run['artifacts'].append(copy.deepcopy(artifact))
        self.assertEqual(self.validate()['status'], 'failed')
        self.run['artifacts'] = [artifact]
        artifact['sha256'] = 'a' * 64
        self.assertEqual(self.validate()['status'], 'failed')
        self.run['artifacts'] = [original]
        with patch.object(self.store, 'get', side_effect=ValueError('unavailable')):
            self.assertEqual(self.validate()['status'], 'failed')
        self.task['outputs'] = ['artifact-unregistered']
        self.assertEqual(self.validate()['status'], 'failed')
        self.task['outputs'] = ['../../host-path']
        self.assertEqual(self.validate()['status'], 'failed')

    def test_snapshot_content_hash_is_recomputed_not_trusted(self):
        self.task['outputContract'] = {'kind': 'artifact'}
        artifact = self.publish()
        self.store._artifacts[artifact['id']]['content'] = 'tampered content'
        self.assertEqual(self.validate()['status'], 'failed')

    def test_json_uses_actual_final_text_and_required_top_level_keys(self):
        self.task['outputContract'] = {'kind': 'json', 'requiredKeys': ['answer', 'sources']}
        self.assertEqual(self.validate('{"answer":"actual data","sources":[]}')['status'], 'passed')
        for text in ('The JSON was written successfully.', '{"answer":"only"}',
                     '[{"answer":1,"sources":[]}]', '{"nested":{"answer":1,"sources":[]}}',
                     '```json\n{"answer":1,"sources":[]}\n```',
                     '{"answer":NaN,"sources":[]}', '{"answer":Infinity,"sources":[]}',
                     '{"answer":1,"answer":2,"sources":[]}',
                     '{"answer":1,"sources":[]} trailing prose'):
            with self.subTest(text=text):
                self.assertEqual(self.validate(text)['status'], 'failed')
        result = self.validate('{"answer":null,"sources":[]}')
        self.assertEqual(result['status'], 'passed')  # Presence only, not semantic/type quality.
        self.assertEqual(result['semanticQuality'], 'unassessed')

    def test_json_must_be_finite_and_utf8_serializable_for_handoff_and_persistence(self):
        self.task['outputContract'] = {'kind':'json', 'requiredKeys':['answer']}
        for text in ('{"answer":1e999}', '{"answer":-1e999}', '{"answer":"\\ud800"}'):
            with self.subTest(text=text):
                self.assertEqual(self.validate(text)['status'], 'failed')
        self.assertEqual(self.validate('{"answer":"中文结果"}')['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
