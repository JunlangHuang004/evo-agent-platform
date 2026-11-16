"""Actual local files, immutable candidate ZIPs and scripted scheduler transitions.

These are offline protocol/packaging regressions, not live product-quality tests.
Editorial pass/reject fixtures never manufacture executed browser-test evidence.
"""
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

from backend.artifacts import ArtifactStore
from backend.candidate_delivery import create_candidate_delivery, current_candidate, publication_candidate
from backend.config import Config
from backend.contracts import normalize_output_contract, validate_task_output
from backend.delivery import DeliveryError, create_delivery, delivery_zip
from backend.engine import Platform
from backend.evaluation import RunEvidence
from backend.product_acceptance import ProductVerifier
from backend.runtime import ProtocolError
from backend.sandbox import SandboxManager
from backend.server import make_handler
from tests.test_engine import ScriptedProvider
from tests.test_output_contract_runtime import LocalFiles


CONTRACT = {'kind': 'artifact', 'extensions': ['.md'], 'minCount': 1, 'candidateDelivery': True}
DECLARATION = {'entrypoint': 'report.md', 'instructions': 'Read report.md. This is an unassessed offline fixture.'}
BODY = '# Fixture report\n\nAn actual immutable manuscript; no real-world factual acceptance is claimed.\n'


def write(path='report.md', body=BODY):
    return {'type': 'tool', 'name': 'file.write', 'args': {'path': path, 'content': body}}


def final(status='done'):
    return {'type': 'final', 'status': status, 'text': 'Report the actual retained artifacts and limits.'}


def structural_check(run, store):
    evidence = RunEvidence({'runId': run['id'], 'status': run.get('status', 'partial'),
        'rawRun': run, 'tasks': [{'taskId': t['id'], 'revision': t.get('revision', 0),
            'ownerAgentId': t['agentId'], 'artifactIds': t.get('outputs', [])}
            for t in run['steps'] if t.get('kind') != 'planning']}, store.get)
    return ProductVerifier({'caseId': 'software-1'})._delivery(evidence)


class CandidateSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name), {'LLM_API_KEY': 'offline-candidate-fixture'})
        self.store = ArtifactStore(self.config.data_dir)
        self.sandbox = SandboxManager(self.config)
        self.producer = {'id': 'task-producer', 'agentId': 'executor', 'revision': 0,
                         'outputs': [], 'status': 'done', 'outputContract': copy.deepcopy(CONTRACT)}
        self.asset_task = {'id': 'task-assets', 'agentId': 'research', 'revision': 0,
                           'outputs': [], 'status': 'done'}
        self.release = {'id': 'task-release', 'agentId': 'coordinator', 'revision': 0,
                        'outputs': [], 'status': 'done', 'outputContract': {'kind': 'delivery'}}
        self.run = {'id': 'run-candidate', 'status': 'partial', 'artifacts': [], 'deliveries': [],
                    'steps': [self.producer, self.asset_task, self.release]}

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, path='report.md', body=BODY, task=None):
        task = task or self.producer
        target = self.sandbox.workspace(task['agentId'], self.run['id']) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        artifact = self.store.register(task['agentId'], self.run['id'], path, task['id'])
        if artifact['id'] not in task['outputs']:
            task['outputs'].append(artifact['id'])
        if not any(a['id'] == artifact['id'] for a in self.run['artifacts']):
            self.run['artifacts'].append(artifact)
        return artifact

    def candidate(self, artifacts, entry='report.md'):
        result = create_candidate_delivery(self.run, self.producer, self.store,
            [a['id'] for a in artifacts], entry, DECLARATION['instructions'])
        self.run['deliveries'].append(result)
        return result

    def test_contract_opt_in_is_typed_and_only_for_artifact_kind(self):
        self.assertEqual(normalize_output_contract(CONTRACT), CONTRACT)
        self.assertNotIn('candidateDelivery', normalize_output_contract({'kind': 'artifact'}))
        for contract in ({'kind': 'text', 'candidateDelivery': True},
                         {'kind': 'delivery', 'candidateDelivery': True},
                         dict(CONTRACT, candidateDelivery='true'),
                         dict(CONTRACT, candidateDelivery=1)):
            with self.subTest(contract=contract), self.assertRaises(ProtocolError):
                normalize_output_contract(contract)

    def test_real_candidate_zip_is_immutable_and_not_formal_delivery_or_acceptance(self):
        artifact = self.publish()
        self.assertEqual(validate_task_output(self.producer, self.run, self.store, 'Ready')['status'], 'failed')
        candidate = self.candidate([artifact])
        (self.sandbox.workspace('executor', self.run['id']) / 'report.md').write_text('Unpublished replacement')
        with zipfile.ZipFile(io.BytesIO(delivery_zip(candidate, self.run, self.store))) as zipped:
            self.assertIsNone(zipped.testzip())
            self.assertEqual(zipped.read('report.md'), BODY.encode())
            metadata = json.loads(zipped.read('DELIVERY.json'))
            self.assertEqual(metadata['status'], 'candidate')
            self.assertEqual(metadata['reviewStatusAtCreation'], 'pending')
            self.assertEqual(metadata['semanticQuality'], 'unassessed')
        output = validate_task_output(self.producer, self.run, self.store, 'Candidate available')
        self.assertEqual(output['status'], 'passed')
        self.assertEqual(output['candidateDeliveryId'], candidate['id'])
        self.assertEqual(output['semanticQuality'], 'unassessed')
        # Same owner/task isolates status exclusion from an unrelated task mismatch.
        formal_task = dict(self.producer, outputContract={'kind': 'delivery'})
        self.assertEqual(validate_task_output(formal_task, self.run, self.store, 'Delivered')['status'], 'failed')
        self.assertEqual(structural_check(self.run, self.store).verdict, 'fail')
        self.assertNotIn('validation', self.producer)

    def test_missing_opt_in_and_unknown_publication_fields_are_rejected(self):
        artifact = self.publish()
        for contract in (None, {'kind': 'artifact'}, dict(CONTRACT, candidateDelivery=False)):
            self.producer['outputContract'] = contract
            with self.subTest(contract=contract), self.assertRaises(DeliveryError):
                self.candidate([artifact])
        self.producer['outputContract'] = copy.deepcopy(CONTRACT)
        for declaration in (None, dict(DECLARATION, scanWorkspace=True),
                            dict(DECLARATION, artifactIds='all')):
            with self.subTest(declaration=declaration), self.assertRaises(DeliveryError):
                publication_candidate(self.run, self.producer, self.store, [artifact], declaration)
        self.assertEqual(self.run['deliveries'], [])

    def test_explicit_cross_batch_asset_requires_actual_grant(self):
        report = self.publish()
        asset = self.publish('appendix.txt', 'Explicit supporting asset.', self.asset_task)
        declaration = dict(DECLARATION, artifactIds=[asset['id']])
        with self.assertRaisesRegex(DeliveryError, 'access'):
            publication_candidate(self.run, self.producer, self.store, [report], declaration)
        self.store.grant(asset['id'], 'executor', self.run['id'], 'orchestrator:task-producer')
        candidate = publication_candidate(self.run, self.producer, self.store, [report], declaration)
        with zipfile.ZipFile(io.BytesIO(delivery_zip(candidate, self.run, self.store))) as zipped:
            self.assertEqual(zipped.read('appendix.txt').decode(), asset['content'])
        self.assertEqual({f['artifactId'] for f in candidate['files']}, {report['id'], asset['id']})

    def test_inherited_entrypoint_plus_new_readme_is_not_new_candidate(self):
        original = self.publish()
        self.run.update(teamId='default', channelId='group', mode='task', status='done',
                        participants=['executor', 'coordinator', 'research'])
        previous = self.run
        self.producer = dict(self.producer, id='task-new-producer', outputs=[])
        self.run = {'id': 'run-continued-candidate', 'teamId': 'default', 'channelId': 'group',
                    'mode': 'task', 'status': 'running', 'participants': previous['participants'],
                    'previousRunId': previous['id'], 'steps': [self.producer], 'artifacts': [], 'deliveries': []}
        carried = self.store.carry_forward(previous, self.run, self.run['participants'])
        self.run['artifacts'].extend(carried['artifacts'])
        inherited = next(a for a in carried['artifacts'] if a['sourceArtifactId'] == original['id'])
        readme = self.publish('README.md', 'New wrapper around old work.')
        with self.assertRaisesRegex(DeliveryError, 'entrypoint'):
            self.candidate([inherited, readme])
        self.assertFalse(self.run['deliveries'])

    def test_other_task_entrypoint_wrong_run_and_path_collisions_are_rejected(self):
        report = self.publish()
        other = self.publish('other.md', 'Other task output.', self.asset_task)
        self.store.grant(other['id'], 'executor', self.run['id'], 'orchestrator:task-producer')
        with self.assertRaisesRegex(DeliveryError, 'entrypoint'):
            self.candidate([other], entry='other.md')
        with self.assertRaises(DeliveryError):
            create_candidate_delivery(dict(self.run, id='run-wrong'), self.producer, self.store,
                                      [report['id']], 'report.md', 'Read it.')
        collision = self.publish('REPORT.md', 'Conflicting path.', self.asset_task)
        self.store.grant(collision['id'], 'executor', self.run['id'], 'orchestrator:task-producer')
        with self.assertRaisesRegex(DeliveryError, 'collide'):
            self.candidate([report, collision])

    def test_old_revision_and_superseded_bytes_do_not_satisfy_candidate_contract(self):
        original = self.publish()
        candidate = self.candidate([original])
        self.producer['revision'] = 1
        self.assertIsNone(current_candidate(self.producer, self.run, self.store, [original['id']]))
        self.assertEqual(validate_task_output(self.producer, self.run, self.store, 'Ready')['status'], 'failed')
        self.producer['revision'] = 0
        replacement = self.publish(body=BODY + '\nActually changed version.')
        with self.assertRaisesRegex(DeliveryError, 'latest'):
            self.candidate([original])
        self.assertIsNone(current_candidate(self.producer, self.run, self.store, [replacement['id']]))
        with zipfile.ZipFile(io.BytesIO(delivery_zip(candidate, self.run, self.store))) as zipped:
            self.assertEqual(zipped.read('report.md'), BODY.encode())

    def test_changed_asset_revision_and_tampered_manifest_fail_current_validation(self):
        report = self.publish()
        asset = self.publish('asset.txt', 'Actual asset.', self.asset_task)
        self.store.grant(asset['id'], 'executor', self.run['id'], 'orchestrator:task-producer')
        candidate = self.candidate([report, asset])
        self.asset_task['revision'] = 1
        self.assertIsNone(current_candidate(self.producer, self.run, self.store, [report['id']]))
        self.asset_task['revision'] = 0
        candidate['files'][0]['sha256'] = '0' * 64
        self.assertIsNone(current_candidate(self.producer, self.run, self.store, [report['id']]))
        with self.assertRaises(DeliveryError):
            delivery_zip(candidate, self.run, self.store)

    def test_explicit_product_selection_need_not_include_supporting_research_notes(self):
        self.publish('supporting_notes.md', 'A published supporting note, not part of the selected product.')
        report = self.publish()
        candidate = self.candidate([report])
        result = validate_task_output(self.producer, self.run, self.store, 'Explicit selected product is available.')
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['candidateDeliveryId'], candidate['id'])
        self.assertEqual([f['path'] for f in candidate['files']], ['report.md'])

    def test_selection_still_requires_original_minimum_and_formats(self):
        self.producer['outputContract']['minCount'] = 2
        report = self.publish()
        social = self.publish('social.md', 'A separately required channel manuscript.')
        note = self.publish('support.txt', 'An asset does not count as a required Markdown manuscript.')
        self.candidate([report, note])
        self.assertEqual(validate_task_output(self.producer, self.run, self.store, 'Ready')['status'], 'failed')
        selected = self.candidate([report, social, note])
        result = validate_task_output(self.producer, self.run, self.store, 'Ready')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['candidateDeliveryId'], selected['id'])

    def test_old_outputs_removed_by_rework_cannot_create_a_new_revision_candidate(self):
        original = self.publish()
        historical = self.candidate([original])
        self.producer.update(revision=1, outputs=[])
        with self.assertRaisesRegex(DeliveryError, 'no longer an output'):
            self.candidate([original])
        self.assertIsNone(current_candidate(self.producer, self.run, self.store, [original['id']]))
        with zipfile.ZipFile(io.BytesIO(delivery_zip(historical, self.run, self.store))) as archive:
            self.assertEqual(archive.read('report.md'), BODY.encode())


class CandidateRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.shutdown(); app.wait_for_idle(3); app.repo.close()
        self.tmp.cleanup()

    def run_model(self, plan, scripts, participants=None, holder=None):
        config = Config(Path(self.tmp.name) / str(len(self.apps)), {'LLM_API_KEY': 'offline-fixture',
            'MAX_MODEL_CALLS': '60', 'MAX_RUN_TOKENS': '500000', 'MAX_AGENT_TURNS': '16', 'MAX_RETRIES': '0'})
        model = ScriptedProvider(plan, scripts)
        app = Platform(config, provider=model, tools=LocalFiles(config)); self.apps.append(app)
        if holder is not None: holder['app'] = app
        app.action({'action': 'send', 'channelId': 'group', 'mode': 'task',
                    'participants': participants or ['executor'], 'text': 'Deliver the explicitly declared fixture product.'})
        self.assertTrue(app.wait_for_idle(8), 'Offline fixture must settle without live-model calls.')
        return app, model, app.snapshot()['sessions']['group']['run']

    @staticmethod
    def build_plan(contract=None):
        return [{'id': 'build', 'title': 'Produce', 'agentId': 'executor',
                 'outputContract': copy.deepcopy(contract or CONTRACT)}]

    def test_publish_batch_creates_real_candidate_before_final_and_survives_reload(self):
        actions = [write(), {'type': 'publish_artifacts', 'paths': ['report.md'],
                            'delivery': copy.deepcopy(DECLARATION)}, final()]
        app, model, run = self.run_model(self.build_plan(), {'Produce': actions})
        self.assertEqual(run['status'], 'done')
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate'])
        self.assertFalse(any(e['type'] == 'review_pass' for e in run['events']))
        self.assertEqual(structural_check(run, app.artifacts).verdict, 'fail')
        persisted = app.repo.get_run(run['id'])
        with zipfile.ZipFile(io.BytesIO(delivery_zip(persisted['deliveries'][0], persisted, app.artifacts))) as archive:
            self.assertEqual(archive.read('report.md'), BODY.encode())
        self.assertEqual(model.counts['Produce'], 3)

    def test_ordinary_research_publication_does_not_implicitly_package(self):
        contract = {'kind': 'artifact', 'extensions': ['.md'], 'minCount': 1}
        app, _, run = self.run_model(self.build_plan(contract), {'Produce': [write(),
            {'type': 'publish_artifacts', 'paths': ['report.md']}, final()]})
        self.assertEqual(run['status'], 'done')
        self.assertFalse(run.get('deliveries'))
        self.assertEqual(len(run['artifacts']), 1)

    def test_failed_publication_batch_keeps_actual_file_without_partial_candidate(self):
        app, _, run = self.run_model(self.build_plan(), {'Produce': [write(),
            {'type': 'publish_artifacts', 'paths': ['report.md', 'missing.md'],
             'delivery': copy.deepcopy(DECLARATION)}, final('partial')]})
        self.assertEqual(run['status'], 'partial')
        self.assertEqual(len(run['artifacts']), 1)
        self.assertFalse(run.get('deliveries'))
        actual = [json.loads(t['text']) for t in run['transcript'] if t.get('kind') == 'action_result']
        failure = next(x for x in actual if x.get('failedPath') == 'missing.md')
        self.assertFalse(failure['ok']); self.assertEqual(len(failure['artifacts']), 1)
        self.assertFalse(any(e['type'] == 'candidate_delivery_packaged' for e in run['events']))

    def test_bad_manifest_can_be_repaired_without_rewriting_published_bytes(self):
        holder = {}
        def script(index, context, messages):
            if index == 0: return write()
            if index == 1:
                return {'type': 'publish_artifacts', 'paths': ['report.md'],
                        'delivery': dict(DECLARATION, entrypoint='missing.md')}
            if index == 2:
                run = holder['app'].state['sessions']['group']['run']
                return {'type': 'package_delivery', 'candidate': True,
                        'artifactIds': [a['id'] for a in run['artifacts']], **DECLARATION}
            return final()
        app, _, run = self.run_model(self.build_plan(), {'Produce': script}, holder=holder)
        self.assertEqual(run['status'], 'done')
        self.assertEqual(len(run['artifacts']), 1)
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate'])
        self.assertEqual(sum(c['tool'] == 'file.write' for c in run['calls']), 1)
        results = [json.loads(t['text']) for t in run['transcript'] if t.get('kind') == 'action_result']
        self.assertTrue(any(x.get('error', {}).get('code') == 'candidate_delivery' for x in results))

    def test_repeated_explicit_package_reuses_same_candidate_but_changed_instructions_do_not(self):
        holder = {}
        def script(index, context, messages):
            if index == 0: return write()
            if index == 1:
                return {'type': 'publish_artifacts', 'paths': ['report.md'],
                        'delivery': copy.deepcopy(DECLARATION)}
            if index in (2, 3):
                run = holder['app'].state['sessions']['group']['run']
                declaration = dict(DECLARATION)
                if index == 3:
                    declaration['instructions'] += ' Changed usage conditions require a distinct declaration.'
                return {'type': 'package_delivery', 'candidate': True,
                        'artifactIds': [a['id'] for a in run['artifacts']], **declaration}
            return final()
        app, _, run = self.run_model(self.build_plan(), {'Produce': script}, holder=holder)
        self.assertEqual(run['status'], 'done')
        self.assertEqual(len(run['artifacts']), 1)
        self.assertEqual(len(run['deliveries']), 2)
        self.assertEqual(sum(e['type'] == 'candidate_delivery_packaged' for e in run['events']), 2)
        results = [json.loads(t['text']) for t in run['transcript'] if t.get('kind') == 'action_result']
        reused = [r for r in results if r.get('candidateReused') is True]
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]['delivery']['id'], run['deliveries'][0]['id'])
        self.assertEqual([f['artifactId'] for f in run['deliveries'][0]['files']],
                         [f['artifactId'] for f in run['deliveries'][1]['files']])
        self.assertEqual(structural_check(run, app.artifacts).verdict, 'fail')

    def test_publication_declaration_without_opt_in_reports_error_and_keeps_only_artifact(self):
        contract = {'kind': 'artifact', 'extensions': ['.md'], 'minCount': 1}
        app, _, run = self.run_model(self.build_plan(contract), {'Produce': [write(),
            {'type': 'publish_artifacts', 'paths': ['report.md'], 'delivery': copy.deepcopy(DECLARATION)}, final()]})
        self.assertEqual(run['status'], 'done')  # Original artifact-only contract remains intact.
        self.assertEqual(len(run['artifacts']), 1)
        self.assertEqual(run.get('deliveries', []), [])
        results = [json.loads(t['text']) for t in run['transcript'] if t.get('kind') == 'action_result']
        rejected = next(r for r in results if r.get('error', {}).get('code') == 'candidate_delivery')
        self.assertFalse(rejected['ok'])
        self.assertEqual(rejected['artifacts'][0]['id'], run['artifacts'][0]['id'])

    def pipeline(self, reject=False, candidate_attempts=1):
        holder = {}; observed = {}
        plan = self.build_plan() + [
            {'id': 'qa', 'title': 'Assess', 'agentId': 'reviewer', 'dependsOn': ['build'],
             'reviewOf': ['build'], 'outputContract': {'kind': 'text'}},
            {'id': 'release', 'title': 'Release', 'agentId': 'coordinator', 'dependsOn': ['build', 'qa'],
             'outputContract': {'kind': 'delivery'}}]
        def qa(index, context, messages):
            run = holder['app'].state['sessions']['group']['run']
            producer = next(t for t in run['steps'] if t['title'] == 'Produce')
            if index == 0:
                observed['beforeQA'] = copy.deepcopy(run['deliveries'])
                return {'type': 'tool', 'name': 'file.read',
                        'args': {'path': 'imports/' + producer['outputs'][0] + '/report.md'}}
            if index == 1:
                call = next(c for c in reversed(run['calls']) if c['tool'] == 'file.read' and c['agentId'] == 'reviewer')
                return {'type': 'review', 'taskId': producer['id'], 'verdict': 'reject' if reject else 'pass',
                        'feedback': 'Offline editorial protocol fixture: ' + ('missing required evidence.' if reject else 'read the exact declared fixture text.'),
                        'evidenceCallIds': [call['id']]}
            return final('failed' if reject else 'done')
        def release(index, context, messages):
            if index == 0: return write('README.md', 'Read report.md. This usage note was not separately reviewed.')
            if index == 1: return {'type': 'publish_artifact', 'path': 'README.md'}
            if index == 2:
                run = holder['app'].state['sessions']['group']['run']
                return {'type': 'package_delivery', 'artifactIds': [a['id'] for a in run['artifacts']], **DECLARATION}
            return final()
        def produce(index, context, messages):
            if index == 0: return write()
            if index == 1:
                return {'type': 'publish_artifacts', 'paths': ['report.md'], 'delivery': copy.deepcopy(DECLARATION)}
            if index <= candidate_attempts:
                run = holder['app'].state['sessions']['group']['run']
                return {'type': 'package_delivery', 'candidate': True,
                        'artifactIds': [a['id'] for a in run['artifacts']], 'entrypoint': 'report.md',
                        'instructions': DECLARATION['instructions'] + f' Separate declared usage note {index}.'}
            return final()
        app, model, run = self.run_model(plan, {'Produce': produce,
            'Assess': qa, 'Release': release}, ['executor', 'reviewer', 'coordinator'], holder)
        return app, model, run, observed

    def test_full_candidate_quota_does_not_prevent_reviewed_formal_release(self):
        app, _, run, observed = self.pipeline(candidate_attempts=9)
        self.assertEqual(run['status'], 'done', run.get('summary'))
        self.assertEqual(len(observed['beforeQA']), 8)
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate'] * 8 + ['packaged'])
        self.assertEqual(sum(e['type'] == 'candidate_delivery_packaged' for e in run['events']), 8)
        self.assertEqual(sum(e['type'] == 'delivery_packaged' for e in run['events']), 1)
        self.assertEqual(sum(e['type'] == 'review_pass' for e in run['events']), 1)
        self.assertEqual(len(run['artifacts']), 2)  # Same original manuscript and actual new release README.
        self.assertEqual(structural_check(run, app.artifacts).verdict, 'pass')
        results = [json.loads(t['text']) for t in run['transcript'] if t.get('kind') == 'action_result']
        self.assertTrue(any(r.get('ok') is False and '8' in r.get('error', {}).get('message', '')
                            for r in results), 'The ninth distinct candidate must be honestly refused.')

    def test_real_reviewer_rejection_preserves_downloadable_candidate_but_overall_partial(self):
        app, _, run, observed = self.pipeline(reject=True)
        self.assertEqual([d['status'] for d in observed['beforeQA']], ['candidate'])
        self.assertEqual(run['status'], 'partial')
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate'])
        self.assertEqual(sum(e['type'] == 'review_reject' for e in run['events']), 1)
        self.assertFalse(any(e['type'] == 'review_pass' for e in run['events']))
        self.assertEqual(structural_check(run, app.artifacts).verdict, 'fail')
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/runs/{run['id']}/deliveries/{run['deliveries'][0]['id']}"
            with urllib.request.urlopen(url, timeout=3) as response:
                self.assertEqual(response.headers['Content-Type'], 'application/zip')
                with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(archive.read('report.md'), BODY.encode())
                    self.assertEqual(json.loads(archive.read('DELIVERY.json'))['status'], 'candidate')
        finally:
            server.shutdown(); server.server_close()

    def test_normal_formal_release_reuses_reviewed_original_and_adds_new_readme(self):
        app, _, run, observed = self.pipeline()
        self.assertEqual(run['status'], 'done', run.get('summary'))
        self.assertEqual([d['status'] for d in observed['beforeQA']], ['candidate'])
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate', 'packaged'])
        producer = next(t for t in run['steps'] if t['title'] == 'Produce')
        released = run['deliveries'][-1]
        self.assertEqual(next(f['artifactId'] for f in released['files'] if f['path'] == 'report.md'), producer['outputs'][0])
        self.assertEqual(producer['validation']['kind'], 'editorial_assessment')
        self.assertFalse(producer['validation']['verified'])
        self.assertEqual(sum(e['type'] == 'review_pass' for e in run['events']), 1)
        self.assertFalse(any(c['tool'] == 'code.test' for c in run['calls']))
        self.assertEqual(structural_check(run, app.artifacts).verdict, 'pass')
        self.assertEqual(released['semanticQuality'], 'unassessed')
        with zipfile.ZipFile(io.BytesIO(delivery_zip(released, run, app.artifacts))) as archive:
            self.assertEqual(archive.read('report.md'), BODY.encode())
            self.assertIn('not separately reviewed', archive.read('README.md').decode())


if __name__ == '__main__':
    unittest.main()
