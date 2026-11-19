"""Regression coverage for the version and final-package gaps observed in R13."""

import os
import json
import unittest

from backend.delivery import delivery_zip
from backend.review_versions import browser_source_feedback, version_feedback
from tests import test_delivery_review_binding as fixture


class MidTestReviewVersionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.DeliveryReviewBindingTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.app, self.run = self.case.app, self.case.run

    def test_modified_private_js_is_rejected_before_tool_execution(self):
        entry = self.case.originals[0]
        style, script = self.case.originals[1:3]
        self.case.tools.execute('reviewer', self.run['id'], 'file.write',
                                {'path': 'app.js', 'content': 'throw new Error("Reviewer changed the file");'})
        args = {'language': 'browser', 'path': 'imports/' + entry['id'] + '/' + entry['name'],
                'resources': {'style.css': 'imports/' + style['id'] + '/' + style['name'], 'app.js': 'app.js'},
                'checks': [{'action': 'assert_visible', 'selector': 'h1'}]}
        count = len(self.run['calls'])
        result = self.app._tool('group', self.run['id'], self.case.qa, 'code.test', args)
        self.assertEqual(result['error']['code'], 'review_source_mismatch')
        self.assertFalse(result['actionExecuted'])
        self.assertEqual(len(self.run['calls']), count)
        self.assertEqual(result['missingSources'][0]['artifactId'], script['id'])
        # A private byte-identical copy preserves the earlier supported workflow.
        self.case.tools.execute('reviewer', self.run['id'], 'file.write',
                                {'path': 'copy.js', 'content': fixture.PRODUCT['app.js']})
        args['resources']['app.js'] = 'copy.js'
        self.assertIsNone(browser_source_feedback(self.run, self.case.qa, args,
                                                  self.app.artifacts, self.case.tools, self.app.agents))

    def test_stale_rejection_preserves_current_revision_and_rework_allowance(self):
        target = self.case.build
        old_snapshot = self.app._review_snapshot(self.run, target)
        old = self.case.originals[0]
        target['revision'] = 1
        target['outputs'] = []
        self.case.publish(target, 'index.html', '<h1>Corrected current draft</h1>')
        action = {'taskId': target['id'], 'verdict': 'rework', 'targetSnapshot': old_snapshot,
                  'feedback': 'The previous version ' + old['id'] + ' is still wrong.'}
        result = self.app._review(self.run, self.case.qa, action)
        self.assertEqual(result['error']['code'], 'stale_review')
        self.assertEqual(target['revision'], 1)
        self.assertEqual(target['status'], 'done')
        self.assertFalse(target.get('validationHistory'))
        self.assertEqual(result['currentTarget']['artifactHashes'], self.app._review_snapshot(self.run, target)['artifactHashes'])

    def test_old_test_cannot_reject_new_revision_even_when_feedback_omits_old_id(self):
        target = self.case.build
        target['revision'] = 1
        result = version_feedback(self.run, self.case.qa, target,
            {'verdict': 'reject', 'feedback': 'The old test failed.', 'evidenceCallIds': [self.case.call['id']]},
            self.app._review_snapshot)
        self.assertEqual(result['error']['code'], 'stale_review')

    def prepare_package_review(self):
        code = [artifact['id'] for artifact in self.case.originals if artifact['path'] != 'README.md']
        package, _ = self.case.package({'README.md': 'Open index.html offline. Revised usage instructions.'}, selected=code)
        review = self.app._new_task('Final package review', 'Check the final package and new instructions.',
                                     'human', list(self.app.agents), 'reviewer')
        review.update(status='running', dependsOn=[self.case.pack['id']], reviewOf=[self.case.pack['id']],
                      outputContract={'kind': 'text'})
        self.run['steps'].append(review)
        self.app._prepare_task('group', self.run['id'], review)
        return package, review

    def assess_package(self, package, reviewer):
        reads = []
        for item in package['files']:
            if item['path'].endswith(('.md', '.txt')):
                artifact = self.app.artifacts.get(item['artifactId'])
                reads.append(self.app._tool('group', self.run['id'], reviewer, 'file.read',
                    {'path': 'imports/' + artifact['id'] + '/' + artifact['name']})['callId'])
        action = {'taskId': self.case.pack['id'], 'verdict': 'pass',
                  'feedback': 'Read the actual revised instructions, checked the entrypoint and unchanged runtime layout.',
                  'targetSnapshot': self.app._review_snapshot(self.run, self.case.pack), 'evidenceCallIds': reads}
        return self.app._review(self.run, reviewer, action), action

    def test_final_package_review_reuses_unchanged_runtime_and_reads_new_documents(self):
        package, reviewer = self.prepare_package_review()
        initial = json.loads(reviewer['_messages'][1]['content'])
        delta = initial['deliveryReviewTargets'][0]
        self.assertEqual(delta['deliveryId'], package['id'])
        self.assertEqual([item['change'] for item in delta['files']].count('modified'), 1)
        before = len([call for call in self.run['calls'] if call['tool'] == 'code.test'])
        result, action = self.assess_package(package, reviewer)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['validation']['kind'], 'delivery_delta')
        self.assertTrue(self.app._valid_review(self.run, self.case.pack))
        self.assertEqual(len([call for call in self.run['calls'] if call['tool'] == 'code.test']), before)
        reviewer['status'] = 'done'
        repeated = self.app._review(self.run, reviewer, action)
        self.assertTrue(repeated['duplicateReview'])
        self.case.finish()
        self.assertEqual(self.run['status'], 'done', self.run.get('summary'))
        self.assertTrue(delivery_zip(package, self.run, self.app.artifacts).startswith(b'PK'))
        restored = fixture.Platform(self.case.config, provider=fixture.NeverModel(), tools=fixture.FileTools(self.case.config))
        try:
            saved = restored.state['sessions']['group']['run']
            saved_package_task = restored._task(saved, self.case.pack['id'])
            self.assertTrue(restored._valid_review(saved, saved_package_task))
        finally:
            restored.shutdown()
            restored.repo.close()

    def test_unread_document_or_changed_instructions_cannot_reuse_review(self):
        package, reviewer = self.prepare_package_review()
        snapshot = self.app._review_snapshot(self.run, self.case.pack)
        action = {'taskId': self.case.pack['id'], 'verdict': 'pass', 'feedback': 'Did not read the new README.',
                  'targetSnapshot': snapshot, 'evidenceCallIds': []}
        result = self.app._review(self.run, reviewer, action)
        self.assertFalse(result['ok'])
        self.assertFalse(self.app._valid_review(self.run, self.case.pack))
        accepted, _ = self.assess_package(package, reviewer)
        self.assertTrue(accepted['ok'])
        package['instructions'] += ' Run another command.'
        self.assertFalse(self.app._valid_review(self.run, self.case.pack))
        self.assertIsNone(self.app._current_review_receipt(self.run, reviewer, self.case.pack))

    @unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS') == '1',
                         'Set RUN_BROWSER_DOCKER_TESTS=1 for the local pinned browser image.')
    def test_real_browser_original_review_to_final_package_delta(self):
        from backend.tools import ToolGateway
        self.app.tools = ToolGateway(self.case.config)
        self.case.build.pop('validation', None)
        args = self.case.browser_call()['args']
        result = self.app._tool('group', self.run['id'], self.case.qa, 'code.test', args)
        self.assertTrue(result['ok'], result)
        accepted = self.app._review(self.run, self.case.qa, {
            'taskId': self.case.build['id'], 'verdict': 'pass',
            'feedback': 'Actual isolated Chromium tested the original entry and runtime resources.',
            'evidenceCallIds': [result['callId']]})
        self.assertTrue(accepted['ok'], accepted)
        package, reviewer = self.prepare_package_review()
        accepted, _ = self.assess_package(package, reviewer)
        self.assertTrue(accepted['ok'], accepted)
        self.assertEqual(accepted['validation']['deliveryDelta']['runtimeEvidenceCallIds'], [result['callId']])
        reviewer['status'] = 'done'
        self.case.finish()
        self.assertEqual(self.run['status'], 'done', self.run.get('summary'))

    def test_unselected_packaging_scratch_file_is_outside_final_snapshot(self):
        package, reviewer = self.prepare_package_review()
        before = self.app._review_snapshot(self.run, self.case.pack)
        self.case.publish(self.case.pack, 'scratch.html', '<h1>Unselected experiment</h1>')
        self.assertEqual(self.app._review_snapshot(self.run, self.case.pack), before)
        result, _ = self.assess_package(package, reviewer)
        self.assertTrue(result['ok'], result)
        self.assertTrue(self.app._valid_review(self.run, self.case.pack))

    def test_changed_runtime_needs_fresh_browser_evidence(self):
        files = dict(fixture.PRODUCT, **{'app.js': 'throw new Error("A changed release");'})
        package, _ = self.case.package(files)
        reviewer = self.app._new_task('Final QA', 'Inspect the final release.', 'human', list(self.app.agents), 'reviewer')
        reviewer.update(status='running', dependsOn=[self.case.pack['id']], reviewOf=[self.case.pack['id']])
        self.run['steps'].append(reviewer)
        self.app._prepare_task('group', self.run['id'], reviewer)
        result, _ = self.assess_package(package, reviewer)
        self.assertFalse(result['ok'])
        self.assertFalse(self.app._valid_review(self.run, self.case.pack))


if __name__ == '__main__':
    unittest.main()
