import copy
import hashlib
import unittest

from backend.browser_review_diagnostics import review_source_gaps
from backend.stage_control import latest_test_diagnostic


class BrowserReviewDiagnosticTests(unittest.TestCase):
    def fixture(self):
        artifacts = []
        for name, body in [('index.html', '<script src="app.js"></script>'), ('app.js', 'new production source'), ('style.css', 'body{color:black}')]:
            artifacts.append({'id': name, 'name': name, 'content': body, 'sha256': hashlib.sha256(body.encode()).hexdigest(),
                              'runId': 'run', 'taskId': 'producer'})
        producer = {'id': 'producer', 'revision': 2, 'outputs': [a['id'] for a in artifacts]}
        task = {'id': 'qa-task', 'agentId': 'qa', 'revision': 0, 'dependsOn': ['producer']}
        call = {'id': 'real-test', 'tool': 'code.test', 'status': 'failed', 'taskId': task['id'], 'agentId': 'qa', 'revision': 0,
                'reviewTargets': {'producer': {'targetRevision': 2, 'artifactHashes': {a['id']: a['sha256'] for a in artifacts}}},
                'result': {'ok': False, 'runtime': 'browser', 'exitCode': 1, 'browser': {'executed': True,
                           'testedFiles': [{'path': 'local/index.html', 'sha256': artifacts[0]['sha256']},
                                           {'path': 'local/app.js', 'sha256': 'a'*64}],
                           'assertions': [{'action': 'assert_count', 'expected': 1, 'actual': 0, 'passed': False}]}}}
        run = {'id': 'run', 'steps': [producer, task], 'artifacts': artifacts, 'calls': [call], 'events': [
            {'type': 'artifact_granted', 'payload': {'artifactId': a['id'], 'toAgent': 'qa'}} for a in artifacts]}
        return run, task, call

    def test_real_missing_sources_are_explained_without_altering_any_input_or_assertion(self):
        run, task, call = self.fixture(); before = copy.deepcopy(run)
        result = review_source_gaps(run, task, call)
        row = result['currentTargetGaps'][0]
        self.assertEqual([r['artifactId'] for r in row['notObserved']], ['app.js', 'style.css'])
        self.assertEqual(row['notObserved'][0]['authorizedImportPath'], 'imports/app.js/app.js')
        self.assertEqual(row['notObserved'][0]['expectedSha256'], run['artifacts'][1]['sha256'])
        self.assertEqual(latest_test_diagnostic(run, task)['reviewSourceCoverage'], result)
        self.assertEqual(run, before)

    def test_identical_local_copies_cover_hashes_without_forcing_import_only_policy(self):
        run, task, call = self.fixture()
        call['result']['browser']['testedFiles'] = [{'path': 'local/'+a['name'], 'sha256': a['sha256']} for a in run['artifacts']]
        self.assertIsNone(review_source_gaps(run, task, call))

    def test_undeclared_dependency_and_stale_snapshot_have_no_current_claim(self):
        for change in ('unrelated', 'revision', 'hashes'):
            with self.subTest(change=change):
                run, task, call = self.fixture()
                if change == 'unrelated': task['dependsOn'] = []
                elif change == 'revision': run['steps'][0]['revision'] += 1
                else: call['reviewTargets']['producer']['artifactHashes']['app.js'] = 'b'*64
                self.assertIsNone(review_source_gaps(run, task, call))

    def test_no_grant_never_exposes_import_path_or_grants_permission(self):
        run, task, call = self.fixture(); run['events'] = []
        result = review_source_gaps(run, task, call)
        self.assertTrue(all('authorizedImportPath' not in row for row in result['currentTargetGaps'][0]['notObserved']))
        self.assertFalse(run['events'])

    def test_unknown_unexecuted_forged_or_wrong_owner_receipts_are_ineligible(self):
        changes = [lambda r,t,c: c['result'].update(outcomeUnknown=True),
                   lambda r,t,c: c['result']['browser'].update(executed=False),
                   lambda r,t,c: c.update(agentId='other'),
                   lambda r,t,c: c.update(revision=True),
                   lambda r,t,c: r['artifacts'][1].update(content='unregistered changed bytes'),
                   lambda r,t,c: r['artifacts'][1].update(sourceRunId='old-run'),
                   lambda r,t,c: c['result']['browser']['testedFiles'][0].update(sha256='invalid')]
        for change in changes:
            run, task, call = self.fixture(); change(run, task, call)
            self.assertIsNone(review_source_gaps(run, task, call))

    def test_manifest_only_does_not_prove_resource_was_actually_served(self):
        run, task, call = self.fixture()
        call['result']['browser']['sourceManifest'] = [{'path': a['name'], 'sha256': a['sha256']} for a in run['artifacts']]
        result = review_source_gaps(run, task, call)
        self.assertEqual(len(result['currentTargetGaps'][0]['notObserved']), 2)

    def test_boolean_revisions_and_unsafe_import_names_never_get_trusted_references(self):
        changes = [lambda r,t,c: t.update(revision=False),
                   lambda r,t,c: (r['steps'][0].update(revision=0), c['reviewTargets']['producer'].update(targetRevision=False)),
                   lambda r,t,c: r['artifacts'][1].update(name='../app.js'),
                   lambda r,t,c: r['artifacts'][1].update(name='folder\\app.js'),
                   lambda r,t,c: r['artifacts'][1].update(name='.hidden.js')]
        for change in changes:
            run, task, call = self.fixture(); change(run, task, call)
            self.assertIsNone(review_source_gaps(run, task, call))


if __name__ == '__main__': unittest.main()
