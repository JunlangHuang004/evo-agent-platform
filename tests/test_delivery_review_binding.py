"""Real immutable files, grants, packaging actions and ZIPs with offline QA reports.

The browser reports below are controlled protocol fixtures, not live product QA.
No passing browser behavior is inferred from file existence or a model assertion.
"""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from backend.agents import AGENTS
from backend.config import Config
from backend.delivery import create_delivery, delivery_zip
from backend.delivery_review import unchanged_bundle_binding
from backend.engine import Platform, clock_ms
from tests.test_delivery_ancestor_grants import FileTools, NeverModel
from backend.tools import ToolError


PRODUCT = {
    'index.html': '<!doctype html><link rel="stylesheet" href="style.css"><h1>Snapshot</h1><script src="app.js"></script>',
    'style.css': 'h1 { color: navy; }',
    'app.js': 'document.documentElement.dataset.product = "fixture";',
    'README.md': 'Open index.html. The controlled QA fixture is not a live product test.',
}


class PackageModel:
    def __init__(self, case, files=None, selected=None, entry='index.html'):
        self.case, self.files, self.selected, self.entry = case, files or {}, selected, entry
        self.index, self.contexts = 0, []

    def complete(self, messages, max_tokens=None, deadline=None):
        self.contexts.append(json.loads(messages[1]['content']))
        actions = [{'type': 'tool', 'name': 'file.write', 'args': {'path': p, 'content': body}}
                   for p, body in self.files.items()]
        if self.files:
            actions.append({'type': 'publish_artifacts', 'paths': list(self.files)})
        ids = (self.selected or []) + self.case.pack['outputs']
        actions.extend([{'type': 'package_delivery', 'artifactIds': ids,
                         'entrypoint': self.entry, 'instructions': self.files.get('README.md', PRODUCT['README.md'])},
                        {'type': 'final', 'status': 'done', 'text': 'Packaged actual immutable bytes; no new review decision.'}])
        action = actions[self.index]; self.index += 1
        return {'content': json.dumps(action), 'finish_reason': 'stop', 'usage': {'total_tokens': 1}}


class DeliveryReviewBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name), {'LLM_API_KEY': 'offline-fixture', 'MAX_AGENT_TURNS': '24',
            'MAX_MODEL_CALLS': '60', 'MAX_RUN_TOKENS': '500000'})
        self.tools = FileTools(self.config)
        self.app = Platform(self.config, provider=NeverModel(), tools=self.tools)
        def task(title, owner, deps=(), kind='text'):
            value = self.app._new_task(title, title, 'human', list(AGENTS), owner)
            value.update(status='done', dependsOn=list(deps), outputContract={'kind': kind})
            return value
        self.build = task('Build', 'executor')
        self.qa = task('QA', 'reviewer', [self.build['id']])
        self.note = task('Scope', 'research')
        self.pack = task('Package', 'coordinator', [self.qa['id']], 'delivery')
        self.pack['status'] = 'pending'
        self.run = {'id': 'run-delivery-proof', 'teamId': 'default', 'channelId': 'group', 'mode': 'task',
            'title': 'Controlled packaging workflow', 'status': 'running', 'participants': list(AGENTS),
            'respondingAgentId': 'coordinator', 'startedAt': clock_ms(), 'steps': [self.build, self.qa, self.note, self.pack],
            'calls': [], 'events': [], 'artifacts': [], 'deliveries': [], 'approvals': [], 'messages': [],
            'memoryHits': [], 'memoryEnabled': False, 'modelCalls': 0, 'tokenUsage': 0, 'activeMs': 0,
            'budget': {'profile': 'bounded', 'agentTurns': 24, 'modelCalls': 60, 'tokens': 500000, 'timeMs': 60000}}
        self.app.state['sessions']['group']['run'] = self.run
        self.originals = [self.publish(self.build, p, body) for p, body in PRODUCT.items()]
        self.app._prepare_task('group', self.run['id'], self.qa)
        self.call = self.browser_call()
        self.run['calls'].append(self.call)
        accepted = self.app._review(self.run, self.qa, {'taskId': self.build['id'], 'verdict': 'pass',
            'feedback': 'Controlled independent QA report for immutable fixture bytes.', 'evidenceCallIds': [self.call['id']]})
        self.assertTrue(accepted['ok'])
        self.qa['status'] = 'done'
        self.app._prepare_task('group', self.run['id'], self.pack)
        self.original_validation = copy.deepcopy(self.build['validation'])

    def tearDown(self):
        self.run['status'] = 'done'
        self.app.shutdown(); self.app.wait_for_idle(2); self.app.repo.close(); self.tmp.cleanup()

    def publish(self, task, path, body, run_id=None):
        rid = run_id or self.run['id']
        args = {'path': path, 'content': body}
        try:
            prior = self.tools.execute(task['agentId'], rid, 'file.read', {'path': path})
        except ToolError:
            pass
        else:
            args['expectedSha256'] = prior['sha256']
        self.tools.execute(task['agentId'], rid, 'file.write', args, approved=True)
        artifact = self.app.artifacts.register(task['agentId'], rid, path, task['id'])
        if run_id is None:
            self.run['artifacts'].append(artifact); task['outputs'].append(artifact['id'])
        return artifact

    def browser_call(self):
        refs = [{'path': 'imports/' + a['id'] + '/' + a['name'], 'sha256': a['sha256']}
                for a in self.originals if a['path'] != 'README.md']
        checks = [{'action': 'assert_visible', 'selector': 'h1'}]
        return {'id': 'fixture-qa-call', 'taskId': self.qa['id'], 'agentId': 'reviewer', 'revision': 0,
            'tool': 'code.test', 'status': 'success', 'args': {'language': 'browser', 'path': refs[0]['path'],
                'resources': {'style.css': refs[1]['path'], 'app.js': refs[2]['path']}, 'checks': checks},
            'reviewTargets': {self.build['id']: self.app._review_snapshot(self.run, self.build)},
            'result': {'runtime': 'browser', 'ok': True, 'exitCode': 0, 'browser': {
                'executed': True, 'ok': True, 'entry': refs[0], 'sourceManifest': copy.deepcopy(refs), 'testedFiles': copy.deepcopy(refs),
                'assertions': [{'action': 'assert_visible', 'selector': 'h1', 'passed': True}],
                'assertionCount': 1, 'assertionsPassed': 1, 'pageErrors': [], 'httpErrors': []}}}

    def sources(self):
        return self.app._reviewed_delivery_sources(self.run, self.pack)

    def package(self, files=None, selected=None, entry='index.html'):
        model = PackageModel(self, files, selected, entry)
        self.app.provider = model
        self.pack['_messages'] = []
        self.app._task_worker('group', self.run['id'], self.pack['id'])
        self.assertEqual(self.pack['status'], 'done')
        self.assertEqual(len(self.run['deliveries']), 1)
        return self.run['deliveries'][0], model

    def finish(self):
        self.app._finish('group', self.run['id'])

    def test_actual_repack_action_retains_lineage_without_another_review(self):
        delivery, model = self.package(PRODUCT)
        proof = delivery['reviewedSourceBinding']
        self.assertEqual(proof['sourceTaskId'], self.build['id'])
        self.assertEqual(proof['reviewerTaskId'], self.qa['id'])
        self.assertEqual(proof['evidenceCallIds'], [self.call['id']])
        self.assertEqual(proof['scope'], 'packaging_integrity_only')
        self.assertFalse(proof['newReviewCreated'])
        self.assertEqual(proof['semanticQuality'], 'unassessed')
        self.assertEqual({f['sourceArtifactId'] for f in proof['files']}, set(self.build['outputs']))
        self.assertTrue(set(self.pack['outputs']).isdisjoint(self.build['outputs']))
        context = model.contexts[0]['deliverySourceHandoff']
        self.assertEqual({f['id'] for f in context['sources'][0]['files']}, set(self.build['outputs']))
        self.assertIn('Prefer package_delivery', context['instruction'])
        self.assertNotIn('validation', self.pack)
        self.assertEqual(self.build['validation'], self.original_validation)
        self.assertEqual(sum(e['type'] == 'review_pass' for e in self.run['events']), 1)
        self.finish()
        self.assertEqual(self.run['status'], 'done')
        self.assertEqual(self.run['acceptanceGaps'], [])
        self.assertEqual(self.pack['deliveryReviewBinding']['deliveryId'], delivery['id'])
        with zipfile.ZipFile(io.BytesIO(delivery_zip(delivery, self.run, self.app.artifacts))) as archive:
            for path, body in PRODUCT.items(): self.assertEqual(archive.read(path).decode(), body)
        self.assertEqual(delivery['semanticQuality'], 'unassessed')

    def test_direct_reviewed_originals_plus_new_readme_still_package_normally(self):
        code = [a['id'] for a in self.originals if a['path'] != 'README.md']
        delivery, _ = self.package({'README.md': 'New delivery instructions: open index.html.'}, selected=code)
        self.assertNotIn('reviewedSourceBinding', delivery)
        self.assertFalse(self.app._has_code_work(self.run, self.pack))
        self.finish()
        self.assertEqual(self.run['status'], 'done')
        self.assertEqual(self.run['acceptanceGaps'], [])
        self.assertNotIn('validation', self.pack)
        self.assertEqual(delivery['semanticQuality'], 'unassessed')
        with zipfile.ZipFile(io.BytesIO(delivery_zip(delivery, self.run, self.app.artifacts))) as archive:
            self.assertEqual(archive.read('app.js').decode(), PRODUCT['app.js'])
            self.assertIn('New delivery', archive.read('README.md').decode())

    def test_modified_readme_requires_review_of_republished_code(self):
        files = dict(PRODUCT, **{'README.md': 'Run a different script: node app.js.'})
        delivery, _ = self.package(files)
        self.assertNotIn('reviewedSourceBinding', delivery)
        self.finish()
        self.assertEqual(self.run['status'], 'partial')
        self.assertEqual(len(self.run['acceptanceGaps']), 1)
        self.assertIn('Package', self.run['acceptanceGaps'][0])

    def test_modified_runtime_bytes_require_review(self):
        delivery, _ = self.package(dict(PRODUCT, **{'app.js': PRODUCT['app.js'] + '\nalert("New behavior");'}))
        self.assertNotIn('reviewedSourceBinding', delivery)
        self.finish(); self.assertEqual(self.run['status'], 'partial')

    def test_relocation_and_extra_configuration_are_not_packaging_equivalence(self):
        for label, files, entry in [
            ('relocated', {'delivery/' + p: s for p, s in PRODUCT.items()}, 'delivery/index.html'),
            ('new-config', {**PRODUCT, 'config.json': '{"mode":"new"}'}, 'index.html'),
            ('omitted-doc', {p: s for p, s in PRODUCT.items() if p != 'README.md'}, 'index.html')]:
            with self.subTest(label=label):
                self.pack['outputs'] = []
                own = [self.publish(self.pack, p, s) for p, s in files.items()]
                delivery = create_delivery(self.run, self.pack, self.app.artifacts, [a['id'] for a in own],
                                           entry, PRODUCT['README.md'])
                self.assertIsNone(unchanged_bundle_binding(self.run, self.pack, delivery, self.app.artifacts, self.sources()))

    def test_changed_generated_run_instructions_do_not_reuse_proof(self):
        delivery, _ = self.package(PRODUCT)
        delivery['instructions'] = 'Run node app.js instead of opening the page.'
        self.assertIsNone(self.app._current_packaging_binding(self.run, self.pack))
        self.finish(); self.assertEqual(self.run['status'], 'partial')

    def test_context_refresh_removes_outdated_source_handoff(self):
        context = json.loads(self.pack['_messages'][1]['content'])
        self.assertIn('deliverySourceHandoff', context)
        self.build['revision'] += 1
        self.app._prepare_task('group', self.run['id'], self.pack)
        context = json.loads(self.pack['_messages'][1]['content'])
        self.assertNotIn('deliverySourceHandoff', context)

    def test_source_and_reviewer_revision_changes_invalidate_stored_proof(self):
        delivery, _ = self.package(PRODUCT)
        self.assertIsNotNone(self.app._current_packaging_binding(self.run, self.pack))
        for target in (self.build, self.qa, self.pack):
            with self.subTest(target=target['title']):
                target['revision'] += 1
                self.assertIsNone(self.app._current_packaging_binding(self.run, self.pack))
                target['revision'] -= 1
        self.build['revision'] += 1
        self.pack['deliveryReviewBinding'] = {'stale': True}
        self.finish()
        self.assertEqual(self.run['status'], 'partial')
        self.assertNotIn('deliveryReviewBinding', self.pack)
        self.assertIn('reviewedSourceBinding', delivery, 'Historical proof stays auditable but cannot satisfy current acceptance.')

    def test_missing_or_tampered_server_receipt_cannot_close_acceptance_gap(self):
        delivery, _ = self.package(PRODUCT)
        receipt = delivery.pop('reviewedSourceBinding')
        self.assertIsNone(self.app._current_packaging_binding(self.run, self.pack))
        delivery['reviewedSourceBinding'] = receipt
        receipt['files'][0]['sourceArtifactId'] = 'invented-id'
        self.assertIsNone(self.app._current_packaging_binding(self.run, self.pack))
        self.finish(); self.assertEqual(self.run['status'], 'partial')

    def test_actual_authority_required_for_source_and_reviewer_and_destination(self):
        self.package(PRODUCT)
        can_access = self.app.artifacts.can_access
        for actor in ('coordinator', 'reviewer'):
            with self.subTest(actor=actor):
                with patch.object(self.app.artifacts, 'can_access', side_effect=lambda aid, who, rid:
                        False if who == actor else can_access(aid, who, rid)):
                    self.assertIsNone(self.app._current_packaging_binding(self.run, self.pack))
        self.assertIsNotNone(self.app._current_packaging_binding(self.run, self.pack))

    def test_same_sha_unrelated_source_or_nonreview_role_cannot_lend_acceptance(self):
        self.package(PRODUCT)
        old_deps = self.pack['dependsOn']
        self.pack['dependsOn'] = [self.note['id']]
        self.assertEqual(self.sources(), [])
        self.pack['dependsOn'] = old_deps
        old_actor = self.qa['agentId']; self.qa['agentId'] = 'research'
        self.assertEqual(self.sources(), [])
        self.qa['agentId'] = old_actor
        self.qa['agentId'] = self.pack['agentId']
        self.assertEqual(self.sources(), [])

    def test_foreign_inherited_misbound_or_forged_source_metadata_rejected(self):
        self.package(PRODUCT)
        row = self.originals[0]
        for field, value in [('runId', 'foreign-run'), ('sourceRunId', 'prior-run'),
                             ('inheritedReadOnly', True), ('taskId', self.note['id']),
                             ('ownerAgentId', 'research'), ('sha256', '0'*64)]:
            with self.subTest(field=field):
                original = copy.deepcopy(row)
                row[field] = value
                self.assertEqual(self.sources(), [])
                row.clear(); row.update(original)

    def test_latest_source_republication_requires_new_explicit_review_even_same_bytes(self):
        self.package(PRODUCT)
        self.publish(self.build, 'app.js', PRODUCT['app.js'])
        self.assertEqual(self.sources(), [])
        self.assertIsNone(self.app._current_packaging_binding(self.run, self.pack))

    def test_runtime_alias_private_copy_missing_manifest_and_failed_evidence_rejected(self):
        self.package(PRODUCT)
        original = copy.deepcopy(self.call)
        changes = [
            lambda c: c.update(status='failed'),
            lambda c: c.update(revision=1),
            lambda c: c.update(revision=False),
            lambda c: c.update(agentId='executor'),
            lambda c: c['result'].update(deadlineExceeded=True),
            lambda c: c['result'].update(timedOut=True),
            lambda c: c['result'].update(outputLimitExceeded=True),
            lambda c: c['result'].update(notExecuted=True),
            lambda c: c['result'].update(actionExecuted=False),
            lambda c: c['result'].update(error='Execution failed'),
            lambda c: c['result']['browser'].update(ok=False),
            lambda c: c['result']['browser'].update(error='Assertion failed'),
            lambda c: c['result']['browser'].update(timedOut=True),
            lambda c: c['result']['browser'].update(actionExecuted=False),
            lambda c: c['result']['browser'].pop('sourceManifest'),
            lambda c: c['args']['resources'].update({'different.css': c['args']['resources'].pop('style.css')}),
            lambda c: c['args']['resources'].update({'app.js': 'private-copy.js'}),
            lambda c: c['result']['browser'].update(testedFiles=c['result']['browser']['testedFiles'][:1]),
        ]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                change(self.call)
                self.assertIsNone(self.app._current_packaging_binding(self.run, self.pack))
                self.call.clear(); self.call.update(copy.deepcopy(original))

    def test_other_failed_call_cannot_approve_an_untested_entry(self):
        self.package(PRODUCT)
        failed = copy.deepcopy(self.call)
        failed.update(id='failed-entry', status='failed')
        failed['result']['browser']['entry']['sha256'] = '0'*64
        self.run['calls'].append(failed)
        self.build['validation']['evidenceCallIds'].append(failed['id'])
        sources = self.sources()
        self.assertEqual([b['callId'] for b in sources[0]['runtimeBindings']], [self.call['id']])
        self.assertEqual(self.app._current_packaging_binding(self.run, self.pack)['evidenceCallIds'], [self.call['id']])


if __name__ == '__main__':
    unittest.main()
