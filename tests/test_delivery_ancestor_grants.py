"""Real confined file/snapshot handoff; scripted model, no network or code execution."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from backend.config import Config
from backend.delivery import create_delivery, delivery_zip, validate_delivery, DeliveryError
from backend.engine import Platform
from backend.tools import ToolGateway, ToolError


PRODUCT = {
    'app/index.html': '<!doctype html><link rel="stylesheet" href="style.css"><h1>Actual snapshot</h1><script src="app.js"></script>',
    'app/style.css': 'h1 { color: navy; }',
    'app/app.js': 'document.documentElement.dataset.product = "fixture";',
    'app/README.md': 'Open index.html locally. Independent functional acceptance is not established by packaging.',
}


class FileTools(ToolGateway):
    def capabilities(self):
        return {'dockerAvailable': False, 'gitInstalled': False}


class NeverModel:
    def complete(self, *args, **kwargs):
        raise AssertionError('Manual snapshot tests must not call a model.')


class HandoffModel:
    def __init__(self):
        self.counts = {}
        self.package_context = None
        self.package_results = []
        self.missing = False

    def complete(self, messages, max_tokens=None, deadline=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            action = {'tasks': [
                {'id': 'build', 'title': 'Produce', 'agentId': 'executor',
                 'outputContract': {'kind': 'artifact', 'extensions': ['.html', '.css', '.js', '.md'], 'minCount': 4}},
                {'id': 'qa', 'title': 'QA note only', 'agentId': 'reviewer', 'dependsOn': ['build'],
                 'requiredCapabilities': ['review'], 'outputContract': {'kind': 'artifact', 'extensions': ['.md']}},
                {'id': 'pack', 'title': 'Package', 'agentId': 'coordinator', 'dependsOn': ['qa'],
                 'outputContract': {'kind': 'delivery'}},
            ]}
        else:
            context = json.loads(messages[1]['content'])
            title = context['task']['title']
            index = self.counts.get(title, 0)
            self.counts[title] = index + 1
            if title == 'Produce':
                actions = [{'type': 'tool', 'name': 'file.write', 'args': {'path': p, 'content': s}}
                           for p, s in PRODUCT.items()]
                actions += [{'type': 'publish_artifacts', 'paths': list(PRODUCT)},
                            {'type': 'final', 'status': 'done', 'text': 'Published actual bytes; independent functional QA remains unassessed.'}]
                action = actions[index]
            elif title == 'QA note only':
                action = [
                    {'type': 'tool', 'name': 'file.write', 'args': {'path': 'qa.md', 'content': 'A review note only. No browser test or acceptance decision is claimed.'}},
                    {'type': 'publish_artifact', 'path': 'qa.md'},
                    {'type': 'final', 'status': 'done', 'text': 'Published only the requested note; no review pass or producer promotion.'},
                ][index]
            elif title == 'Package':
                self.package_context = context
                materials = {a['sourcePath']: a for a in context['authorizedArtifacts']}
                self.missing = not all(p in materials for p in PRODUCT)
                if self.missing:
                    action = {'type': 'final', 'status': 'partial', 'text': 'Only QA note is authorized; producer snapshots are unavailable.'}
                elif index < len(PRODUCT):
                    if index:
                        result = next(json.loads(m['content'].split('：', 1)[1]) for m in reversed(messages)
                                      if m['content'].startswith('动作的真实结果：'))
                        self.package_results.append(result)
                    path = list(PRODUCT)[index]
                    action = {'type': 'tool', 'name': 'file.read', 'args': {'path': materials[path]['path']}}
                elif index == len(PRODUCT):
                    result = next(json.loads(m['content'].split('：', 1)[1]) for m in reversed(messages)
                                  if m['content'].startswith('动作的真实结果：'))
                    self.package_results.append(result)
                    action = {'type': 'package_delivery', 'artifactIds': [materials[p]['id'] for p in PRODUCT],
                              'entrypoint': 'app/index.html', 'instructions': 'Open app/index.html locally. Packaging is not functional acceptance.'}
                else:
                    action = {'type': 'final', 'status': 'done', 'text': 'Packaged the actual authorized immutable producer snapshots, without claiming semantic acceptance.'}
            else:
                action = {'type': 'final', 'status': 'partial', 'text': 'No other work is part of this fixture.'}
        return {'content': json.dumps(action), 'finish_reason': 'stop', 'usage': {'total_tokens': 1}}


class DeliveryAncestorWorkflowTests(unittest.TestCase):
    def test_note_only_qa_dependency_can_package_real_producer_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp), {'LLM_API_KEY': 'offline-fixture', 'MAX_RETRIES': '0',
                            'MAX_AGENT_TURNS': '16', 'MAX_MODEL_CALLS': '40', 'MAX_RUN_TOKENS': '500000'})
            model = HandoffModel()
            app = Platform(config, provider=model, tools=FileTools(config))
            try:
                app.action({'action': 'send', 'mode': 'task', 'channelId': 'group',
                            'participants': ['coordinator', 'executor', 'reviewer'],
                            'text': 'Produce files, publish a QA note, and package the producer files through that QA dependency.'})
                self.assertTrue(app.wait_for_idle(8))
                run = app.snapshot()['sessions']['group']['run']
                build = next(t for t in run['steps'] if t['title'] == 'Produce')
                qa = next(t for t in run['steps'] if t['title'] == 'QA note only')
                pack = next(t for t in run['steps'] if t['title'] == 'Package')
                self.assertFalse(model.missing, 'Delivery-only dependency on QA must expose declared producer snapshots.')
                self.assertEqual(pack['dependsOn'], [qa['id']], 'The declared DAG must not be rewritten.')
                self.assertEqual(qa['dependsOn'], [build['id']])
                self.assertEqual(pack['status'], 'done')
                self.assertEqual(len(run['deliveries']), 1)
                self.assertEqual({result['content'] for result in model.package_results}, set(PRODUCT.values()))
                self.assertEqual(set(pack['dependencySnapshots']), {build['id'], qa['id']})
                self.assertEqual(app._dependency_validation(run, pack)['status'], 'passed')
                grant_events = [e for e in run['events'] if e['type'] == 'artifact_granted'
                                and e['payload'].get('toAgent') == 'coordinator']
                self.assertEqual(len(grant_events), 5)
                self.assertTrue(all(e['payload']['grant']['readOnly'] for e in grant_events))
                self.assertTrue(all(e['payload']['grant']['authorizedBy'] == 'orchestrator:' + pack['id']
                                    for e in grant_events))
                with zipfile.ZipFile(io.BytesIO(delivery_zip(run['deliveries'][0], run, app.artifacts))) as archive:
                    for path, body in PRODUCT.items():
                        self.assertEqual(archive.read(path).decode(), body)
                self.assertEqual(run['deliveries'][0]['semanticQuality'], 'unassessed')
                self.assertNotIn('validation', build, 'A QA note must not manufacture independent code acceptance.')
                self.assertFalse(any(e['type'] == 'review_pass' for e in run['events']))
                self.assertEqual(run['status'], 'done', 'This fixture closes workflow only; its package remains semantically unassessed.')
            finally:
                app.shutdown(); app.wait_for_idle(3); app.repo.close()


class DeliveryAncestorGrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name), {'LLM_API_KEY': 'offline-fixture'})
        self.tools = FileTools(self.config)
        self.app = Platform(self.config, provider=NeverModel(), tools=self.tools)
        participants = ['coordinator', 'executor', 'reviewer', 'research']
        def task(title, owner, deps=(), kind='text'):
            value = self.app._new_task(title, title, 'human', participants, owner)
            value.update(status='done', dependsOn=list(deps), outputContract={'kind': kind})
            return value
        self.build = task('Build', 'executor')
        self.qa = task('QA', 'reviewer', [self.build['id']])
        self.unrelated = task('Unrelated', 'research')
        self.pack = task('Package', 'coordinator', [self.qa['id']], 'delivery')
        self.pack['status'] = 'running'
        self.run = {'id': 'run-snapshot', 'teamId': 'default', 'channelId': 'group', 'mode': 'task',
                    'title': 'Declared dependency handoff', 'status': 'running', 'participants': participants,
                    'steps': [self.build, self.qa, self.unrelated, self.pack], 'calls': [], 'events': [],
                    'artifacts': [], 'deliveries': [], 'approvals': [], 'messages': [], 'memoryHits': [],
                    'memoryEnabled': False, 'budget': {'profile': 'bounded', 'agentTurns': 8,
                    'modelCalls': 40, 'tokens': 200000, 'timeMs': 60000}}
        self.app.state['sessions']['group']['run'] = self.run
        self.old = self.publish(self.build, 'app/index.html', '<h1>Old published version</h1>')
        self.current = self.publish(self.build, 'app/index.html', '<h1>Current published version</h1>')
        self.note = self.publish(self.qa, 'qa.md', 'Review note only.')
        self.other = self.publish(self.unrelated, 'unrelated.md', 'Other task private-to-its-owner snapshot.')
        self.write(self.build, 'private.txt', 'Not published; never a grant candidate.')
        self.qa['dependencySnapshots'] = {self.build['id']: {'status': self.build['status'],
                                                          **self.app._review_snapshot(self.run, self.build)}}

    def tearDown(self):
        self.run['status'] = 'done'
        self.app.shutdown(); self.app.wait_for_idle(3); self.app.repo.close(); self.tmp.cleanup()

    def write(self, task, path, body, run_id=None):
        args = {'path': path, 'content': body}
        if (self.tools.workspace(task['agentId'], run_id or self.run['id']) / path).exists():
            prior = self.tools.execute(task['agentId'], run_id or self.run['id'], 'file.read', {'path': path})
            args['expectedSha256'] = prior['sha256']
        return self.tools.execute(task['agentId'], run_id or self.run['id'], 'file.write',
                                  args, approved=True)

    def publish(self, task, path, body, run_id=None):
        result = self.write(task, path, body, run_id)
        artifact = self.app.artifacts.register(task['agentId'], run_id or self.run['id'], path, task['id'])
        self.assertEqual(result['sha256'], artifact['sha256'])
        if run_id is None:
            task['outputs'].append(artifact['id']); self.run['artifacts'].append(artifact)
        return artifact

    def prepare(self):
        self.app._prepare_task('group', self.run['id'], self.pack)

    def test_only_current_published_ancestors_are_granted_readonly(self):
        self.prepare()
        allowed = {e['payload']['artifactId'] for e in self.run['events'] if e['type'] == 'artifact_granted'}
        self.assertEqual(allowed, {self.current['id'], self.note['id']})
        self.assertFalse(self.app.artifacts.can_access(self.old['id'], 'coordinator', self.run['id']))
        self.assertFalse(self.app.artifacts.can_access(self.other['id'], 'coordinator', self.run['id']))
        path = 'imports/' + self.current['id'] + '/' + self.current['name']
        result = self.tools.execute('coordinator', self.run['id'], 'file.read', {'path': path})
        self.assertEqual(result['content'], self.current['content'])
        with self.assertRaises(ToolError):
            self.tools.execute('coordinator', self.run['id'], 'file.write', {'path': path, 'content': 'No mutation'}, approved=True)
        with self.assertRaises(ToolError):
            self.tools.execute('coordinator', self.run['id'], 'file.read', {'path': '../executor/' + self.run['id'] + '/private.txt'})
        self.assertEqual(self.run['deliveries'], [])
        self.assertEqual(self.pack['status'], 'running')

    def test_text_and_review_tasks_keep_direct_dependency_scope(self):
        for kind in ('text', 'json'):
            with self.subTest(kind=kind):
                self.pack['outputContract'] = {'kind': kind}
                self.pack['requiredCapabilities'] = ['review'] if kind == 'text' else []
                self.pack['_messages'] = []
                self.prepare()
                self.assertEqual(set(self.pack['dependencySnapshots']), {self.qa['id']})
                self.assertFalse(self.app.artifacts.can_access(self.current['id'], 'coordinator', self.run['id']))
                self.assertTrue(self.app.artifacts.can_access(self.note['id'], 'coordinator', self.run['id']))

    def test_direct_dependency_preserves_historical_readonly_snapshots_not_current_packages(self):
        self.pack['dependsOn'] = [self.build['id']]
        self.prepare()
        allowed = {e['payload']['artifactId'] for e in self.run['events'] if e['type'] == 'artifact_granted'}
        self.assertEqual(allowed, {self.old['id'], self.current['id']},
                         'Existing direct-dependency read access includes historical immutable outputs.')
        for artifact in (self.old, self.current):
            path = 'imports/' + artifact['id'] + '/' + artifact['name']
            receipt = self.tools.execute('coordinator', self.run['id'], 'file.read', {'path': path})
            self.assertEqual(receipt['sha256'], artifact['sha256'])
            self.assertEqual(receipt['content'], artifact['content'])
            with self.assertRaises(ToolError):
                self.tools.execute('coordinator', self.run['id'], 'file.write',
                                   {'path': path, 'content': 'Historical imports stay readonly.'}, approved=True)
        with self.assertRaises(DeliveryError):
            create_delivery(self.run, self.pack, self.app.artifacts, [self.old['id']],
                            self.old['path'], 'Old immutable bytes cannot represent the current product.')
        current = create_delivery(self.run, self.pack, self.app.artifacts, [self.current['id']],
                                  self.current['path'], 'Read the current immutable fixture.')
        self.assertEqual(current['semanticQuality'], 'unassessed')
        self.assertEqual(set(self.pack['dependencySnapshots']), {self.build['id']})
        self.assertEqual(self.pack['dependsOn'], [self.build['id']])

    def test_foreign_inherited_and_misbound_output_ids_do_not_expand_access(self):
        foreign = self.publish(self.build, 'other.md', 'Other run immutable text.', run_id='run-foreign')
        self.run['artifacts'].append(foreign); self.build['outputs'].append(foreign['id'])
        self.build['outputs'].append(self.other['id'])
        source = {'id': 'run-foreign', 'teamId': 'default', 'channelId': 'group', 'mode': 'task',
                  'status': 'done', 'participants': self.run['participants'], 'artifacts': [foreign]}
        self.run['previousRunId'] = source['id']
        inherited = self.app.artifacts.carry_forward(source, self.run, self.run['participants'])['artifacts'][0]
        self.run['artifacts'].append(inherited); self.build['outputs'].append(inherited['id'])
        self.prepare()
        allowed = {e['payload']['artifactId'] for e in self.run['events'] if e['type'] == 'artifact_granted'}
        self.assertEqual(allowed, {self.current['id'], self.note['id']})
        for artifact in (foreign, inherited, self.other):
            self.assertFalse(self.app.artifacts.can_access(artifact['id'], 'coordinator', self.run['id']))

    def test_incomplete_or_unknown_revision_ancestor_is_not_newly_granted(self):
        for status, revision in [('running', 0), ('partial', 0), ('done', None), ('done', False)]:
            with self.subTest(status=status, revision=revision):
                self.build.update(status=status, revision=revision)
                self.pack['_messages'] = []
                self.prepare()
                self.assertFalse(self.app.artifacts.can_access(self.current['id'], 'coordinator', self.run['id']))
        self.assertEqual(self.pack['dependsOn'], [self.qa['id']])

    def test_changed_producer_stays_stale_even_after_repreparing_delivery(self):
        self.prepare()
        original = create_delivery(self.run, self.pack, self.app.artifacts, [self.current['id']],
                                   self.current['path'], 'Read the current immutable fixture.')
        self.assertEqual(self.app._dependency_validation(self.run, self.pack)['status'], 'passed')
        self.build['revision'] += 1
        self.build['outputs'] = []
        new = self.publish(self.build, 'app/index.html', '<h1>Reworked, not re-reviewed</h1>')
        self.assertEqual(self.app._dependency_validation(self.run, self.pack)['status'], 'stale')
        with self.assertRaises(DeliveryError):
            validate_delivery(original, self.run, self.app.artifacts)
        self.prepare()
        self.assertTrue(self.app.artifacts.can_access(new['id'], 'coordinator', self.run['id']))
        self.assertEqual(self.app._dependency_validation(self.run, self.pack)['status'], 'stale',
                         'Refreshing packaging inputs must not silently refresh the old QA dependency snapshot.')
        self.assertEqual(self.qa['dependencySnapshots'][self.build['id']]['targetRevision'], 0)
        self.assertEqual(self.pack['dependencySnapshots'][self.build['id']]['targetRevision'], 1)


if __name__ == '__main__':
    unittest.main()

    def test_review_gets_current_design_ancestor_but_not_unrelated_or_private_files(self):
        self.build['dependsOn']=[self.unrelated['id']]
        self.qa.update(status='running',reviewOf=[self.build['id']])
        self.app._prepare_task('group',self.run['id'],self.qa)
        self.assertTrue(self.app.artifacts.can_access(self.other['id'],'reviewer',self.run['id']))
        receipt=self.tools.execute('reviewer',self.run['id'],'file.read',
            {'path':'imports/'+self.other['id']+'/'+self.other['name']})
        self.assertEqual(receipt['content'],self.other['content'])
        self.assertEqual(self.qa['dependsOn'],[self.build['id']])
        self.assertIn(self.unrelated['id'],self.qa['dependencySnapshots'])
        self.assertNotIn('validation',self.build)

    def test_ordinary_consumer_does_not_get_transitive_design_files(self):
        self.build['dependsOn']=[self.unrelated['id']]
        self.qa.update(status='running',reviewOf=[])
        self.app._prepare_task('group',self.run['id'],self.qa)
        self.assertFalse(self.app.artifacts.can_access(self.other['id'],'reviewer',self.run['id']))

    def test_review_does_not_get_partial_ancestor(self):
        self.build['dependsOn']=[self.unrelated['id']]
        self.unrelated['status']='partial'
        self.qa.update(status='running',reviewOf=[self.build['id']])
        self.app._prepare_task('group',self.run['id'],self.qa)
        self.assertFalse(self.app.artifacts.can_access(self.other['id'],'reviewer',self.run['id']))
