"""Dynamic role permissions and scoped continuation use snapshots, never live files."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from backend.artifacts import ArtifactError, ArtifactStore
from backend.sandbox import SandboxManager, ToolError
from backend.tools import ToolGateway


ROLES = {
    'developer': {'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'code.run']},
    'analyst': {'tools': ['file.read', 'file.list']},
    'newcomer': {'tools': ['file.read', 'file.list']},
}


class ArtifactInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(data_dir=Path(self.temp.name) / 'team-a', git_remote='', docker_image='python:3.11-alpine')
        self.gateway = ToolGateway(self.config, agents=ROLES)
        self.store = ArtifactStore(self.config.data_dir, agents=ROLES)

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, name='solution.py', content='print("published")\n'):
        self.gateway.execute('developer', 'old-run', 'file.write', {'path': name, 'content': content})
        return self.store.register('developer', 'old-run', name, 'old-task')

    def runs(self, artifacts):
        source = {'id': 'old-run', 'teamId': 'software', 'channelId': 'group', 'mode': 'task',
                  'status': 'done', 'participants': list(ROLES), 'artifacts': artifacts}
        target = {'id': 'new-run', 'teamId': 'software', 'channelId': 'group', 'mode': 'task',
                  'status': 'running', 'previousRunId': 'old-run', 'participants': list(ROLES), 'artifacts': []}
        return source, target

    def test_dynamic_registry_permissions_are_real_and_frozen(self):
        registry = copy.deepcopy(ROLES)
        gateway = ToolGateway(self.config, agents=registry)
        registry['analyst']['tools'].append('file.write')
        self.assertEqual([item['name'] for item in gateway.definitions('analyst')], ['file.read', 'file.list'])
        for agent, name in [('executor', 'file.list'), ('analyst', 'file.write')]:
            with self.subTest(agent=agent), self.assertRaises(ToolError):
                gateway.execute(agent, 'new-run', name, {'path': 'x.txt', 'content': 'x'} if name == 'file.write' else {})
        self.assertTrue(gateway.workspace('developer', 'new-run').is_dir())
        with self.assertRaises(ToolError):
            gateway.sandbox.imports('executor', 'new-run')
        with self.assertRaises(ArtifactError):
            self.store.register('executor', 'new-run', 'x.txt', 'task')
        for bad in ({}, {'../escape': {'tools': []}}, {'developer': {'tools': ['shell.exec']}}):
            with self.subTest(registry=bad), self.assertRaises(ToolError):
                ToolGateway(self.config, agents=bad)

    def test_carry_is_immutable_snapshot_and_preserves_existing_acl(self):
        original = self.publish()
        self.store.grant(original['id'], 'analyst', 'old-run', 'orchestrator:task-review')
        workspace = self.gateway.workspace('developer', 'old-run')
        (workspace / 'solution.py').write_text('not the published snapshot')
        (workspace / 'unpublished.csv').write_text('private data')
        source, target = self.runs([original])
        result = self.store.carry_forward(source, target, list(ROLES))
        inherited = result['artifacts'][0]
        self.assertNotEqual(inherited['id'], original['id'])
        self.assertEqual(inherited['runId'], 'new-run')
        self.assertEqual(inherited['content'], original['content'])
        self.assertEqual(inherited['sha256'], original['sha256'])
        self.assertEqual(inherited['sourceSha256'], original['sha256'])
        self.assertEqual(inherited['sourceRunId'], 'old-run')
        self.assertEqual(inherited['sourceArtifactId'], original['id'])
        self.assertEqual(inherited['sourceTaskId'], 'old-task')
        self.assertEqual({grant['toAgentId'] for grant in result['grants']}, {'developer', 'analyst'})
        self.assertFalse((self.gateway.workspace('developer', 'new-run') / 'solution.py').exists())
        self.assertFalse((self.gateway.workspace('developer', 'new-run') / 'unpublished.csv').exists())
        for grant in result['grants']:
            path = 'imports/' + grant['path']
            self.assertTrue(grant['readOnly'])
            read = self.gateway.execute(grant['toAgentId'], 'new-run', 'file.read', {'path': path})
            self.assertEqual(read['sha256'], original['sha256'])
            copied = self.gateway.sandbox.imports(grant['toAgentId'], 'new-run') / grant['path']
            self.assertEqual(copied.stat().st_mode & 0o777, 0o444)
            with self.assertRaises(ToolError):
                self.gateway.execute('newcomer', 'new-run', 'file.read', {'path': path})
            for name, args in [('file.write', {'path': path, 'content': 'overwrite'}),
                               ('file.patch', {'path': path, 'old': 'published', 'new': 'changed'}),
                               ('file.delete', {'path': path})]:
                with self.assertRaises(ToolError):
                    self.gateway.execute('developer', 'new-run', name, args, approved=True)
        self.assertEqual(len(self.store.list_for_run('new-run')), 1)
        with self.assertRaises(ArtifactError):
            self.store.grant(original['id'], 'developer', 'new-run', 'orchestrator:no-cross-run')

    def test_restart_idempotency_and_continuation_lineage(self):
        original = self.publish('report.html', '<html>Safe inert text artifact</html>')
        source, target = self.runs([original])
        first = self.store.carry_forward(source, target, list(ROLES))
        self.store = ArtifactStore(self.config.data_dir, agents=ROLES)
        again = self.store.carry_forward(source, target, list(ROLES))
        self.assertEqual(first, again)
        target.update(status='done', artifacts=first['artifacts'])
        next_run = dict(target, id='third-run', previousRunId='new-run', status='running', artifacts=[])
        continued = self.store.carry_forward(target, next_run, list(ROLES))['artifacts'][0]
        self.assertEqual(continued['sourceRunId'], 'new-run')
        self.assertEqual(continued['originRunId'], 'old-run')
        self.assertEqual(continued['originArtifactId'], original['id'])
        self.assertEqual(continued['sha256'], hashlib.sha256(original['content'].encode()).hexdigest())
        restored = ArtifactStore(self.config.data_dir, agents=ROLES)
        self.assertEqual(restored.get(continued['id']), continued)

    def test_cross_team_private_active_forged_and_foreign_source_are_rejected(self):
        original = self.publish()
        source, target = self.runs([original])
        cases = [('source', 'teamId', 'other'), ('target', 'teamId', 'other'),
                 ('source', 'channelId', 'developer'), ('target', 'channelId', 'developer'),
                 ('source', 'mode', 'chat'), ('target', 'mode', 'chat'),
                 ('source', 'status', 'running'), ('target', 'previousRunId', 'unrelated'),
                 ('source', 'teamId', None)]
        for side, key, value in cases:
            a, b = copy.deepcopy(source), copy.deepcopy(target)
            (a if side == 'source' else b)[key] = value
            with self.subTest(side=side, key=key), self.assertRaises(ArtifactError):
                self.store.carry_forward(a, b, list(ROLES))
        with self.assertRaises(ArtifactError):
            self.store.carry_forward(source, target, ['developer', 'missing'])
        source['artifacts'][0]['sha256'] = '0' * 64
        with self.assertRaises(ArtifactError):
            self.store.carry_forward(source, target, list(ROLES))
        source, target = self.runs([original])
        other = ArtifactStore(Path(self.temp.name) / 'other-team', agents=ROLES)
        with self.assertRaises(ArtifactError):
            other.carry_forward(source, target, list(ROLES))
        self.assertFalse(self.store.list_for_run('new-run'))

    def test_credentials_hidden_paths_and_symlink_sources_do_not_propagate(self):
        safe = self.publish()
        credential = self.publish('credentials.json', '{"password":"test-only-placeholder"}')
        content_secret = self.publish('config.txt', 'ACCESS_TOKEN=test-only-placeholder\n')
        for path in ('.env', '.git/config', '../escape.py'):
            with self.subTest(path=path), self.assertRaises(ArtifactError):
                self.store.register('developer', 'old-run', path, 'old-task')
        workspace = self.gateway.workspace('developer', 'old-run')
        (workspace / 'symlink.py').symlink_to(workspace / 'solution.py')
        with self.assertRaises(ArtifactError):
            self.store.register('developer', 'old-run', 'symlink.py', 'old-task')
        source, target = self.runs([safe, credential, content_secret])
        result = self.store.carry_forward(source, target, list(ROLES))
        self.assertEqual([item['name'] for item in result['artifacts']], ['solution.py'])
        self.assertEqual({item['id'] for item in result['skipped']}, {credential['id'], content_secret['id']})

    def test_symlink_import_destination_rolls_back_index_and_grants(self):
        original = self.publish()
        source, target = self.runs([original])
        imports = self.gateway.sandbox.imports('developer', 'new-run')
        imports.rmdir()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        imports.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ArtifactError):
            self.store.carry_forward(source, target, list(ROLES))
        self.assertEqual(self.store.list_for_run('new-run'), [])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(ArtifactStore(self.config.data_dir, agents=ROLES).list_for_run('new-run'), [])

    def test_corrupt_inherited_provenance_fails_closed_on_reload(self):
        original = self.publish()
        source, target = self.runs([original])
        inherited = self.store.carry_forward(source, target, list(ROLES))['artifacts'][0]
        index = self.config.data_dir / 'artifacts.json'
        payload = json.loads(index.read_text())
        next(item for item in payload['artifacts'] if item['id'] == inherited['id'])['sourceSha256'] = 'f' * 64
        index.write_text(json.dumps(payload))
        with self.assertRaises(ArtifactError):
            ArtifactStore(self.config.data_dir, agents=ROLES)

    def test_carry_quota_failure_does_not_partially_grant(self):
        original = self.publish()
        source, target = self.runs([original])
        with mock.patch('backend.artifacts._MAX_RUN_COUNT', 0), self.assertRaises(ArtifactError):
            self.store.carry_forward(source, target, list(ROLES))
        self.assertEqual(self.store.list_for_run('new-run'), [])
        self.assertFalse((self.config.data_dir / 'imports' / 'developer' / 'new-run').exists())


if __name__ == '__main__':
    unittest.main()
