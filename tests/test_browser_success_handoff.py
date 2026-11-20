"""Reproduce successful-test loops without inventing acceptance or execution."""
import copy
import json
import os
import unittest

from backend.browser_success_handoff import browser_success_handoff
from backend.runtime import fingerprint
from backend.runtime import ProtocolError
from backend.feedback_context import latest_feedback_proof
from tests import test_delivery_review_binding as fixture


class BrowserSuccessHandoffTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.DeliveryReviewBindingTests(); self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.app, self.run, self.qa = self.case.app, self.case.run, self.case.qa
        self.app.config.max_context_chars = 120000
        self.run['budget']['timeMs'] = 1800000
        self.run['events'] = []
        self.case.build.pop('validation', None)
        self.qa.update(status='running', _actions=[])
        call = self.case.browser_call()
        call['result']['test'] = True
        report = call['result']['browser']; report['engine'] = 'chromium'
        report['assertions'][0].update(pageId='main', expected=True, actual=True)
        report['steps'] = [{**report['assertions'][0], 'ok':True}]
        self.run['calls'] = [copy.deepcopy(call), copy.deepcopy(call)]
        self.run['calls'][0]['id'] = 'observed-pass-1'
        self.run['calls'][1]['id'] = 'observed-pass-2'
        self.action = {'type':'tool','name':'code.test','args':copy.deepcopy(call['args'])}

    def feedback(self):
        return browser_success_handoff(self.run, self.qa, self.action, self.app.agents,
            self.app._review_context(self.run, self.qa), self.app._review_snapshot,
            self.app._browser_review_evidence, self.app.artifacts, self.app.tools)

    def test_current_original_receipts_prompt_explicit_decision_once(self):
        before = copy.deepcopy(self.run)
        result = self.feedback()
        self.assertEqual(result['evidenceCallIds'], ['observed-pass-1','observed-pass-2'])
        self.assertTrue(result['notExecuted']); self.assertFalse(result['actionExecuted'])
        self.assertEqual(self.run, before)
        self.qa['_browserSuccessHandoffs'] = [result['bindingSha256']]
        # Durable markers survive JSON persistence without renewing the prompt.
        self.qa['_browserSuccessHandoffs'] = json.loads(json.dumps(self.qa['_browserSuccessHandoffs']))
        self.assertIsNone(self.feedback())

    def test_failed_unknown_foreign_stale_or_incomplete_receipts_do_not_qualify(self):
        original = copy.deepcopy(self.run['calls'])
        for mode in ('failed','unknown','projection','unexecuted','stale','foreign','duplicate','missing_steps','missing_asset','boolean_revision'):
            with self.subTest(mode=mode):
                self.run['calls'] = copy.deepcopy(original)
                call = self.run['calls'][-1]
                if mode == 'failed': call['status'] = 'failed'
                elif mode == 'unknown': call['result']['outcomeUnknown'] = True
                elif mode == 'projection': call['result']['modelViewOnly'] = 'browser-tool-context-v1'
                elif mode == 'unexecuted': call['result']['browser']['executed'] = False
                elif mode == 'stale': call['reviewTargets'][self.case.build['id']]['targetRevision'] += 1
                elif mode == 'foreign': call['agentId'] = 'coordinator'
                elif mode == 'duplicate': call['id'] = original[0]['id']
                elif mode == 'missing_steps': call['result']['browser'].pop('steps')
                elif mode == 'missing_asset': call['result']['browser']['testedFiles'].pop()
                else: call['revision'] = False
                self.assertIsNone(self.feedback())

    def test_changed_checks_storage_or_sources_invalidate_reuse(self):
        original = copy.deepcopy(self.action)
        for field, value in [('checks',[{'action':'assert_count','selector':'h1','count':2}]), ('storage',{'key':'different'}), ('resources',{})]:
            self.action = copy.deepcopy(original); self.action['args'][field] = value
            self.assertIsNone(self.feedback(), field)
        self.action = original
        self.case.build['revision'] += 1
        self.assertIsNone(self.feedback())

    def test_missing_current_private_copy_cannot_reuse_old_receipts(self):
        self.case.tools.execute('reviewer',self.run['id'],'file.write',{'path':'copy.js','content':fixture.PRODUCT['app.js']})
        old = self.action['args']['resources']['app.js']
        self.action['args']['resources']['app.js'] = 'copy.js'
        for call in self.run['calls']:
            call['args']['resources']['app.js'] = 'copy.js'
            for field in ('sourceManifest','testedFiles'):
                for row in call['result']['browser'][field]:
                    if row['path'] == old: row['path'] = 'copy.js'
        self.assertIsNotNone(self.feedback())
        (self.app.tools.workspace('reviewer',self.run['id'])/'copy.js').unlink()
        self.assertIsNone(self.feedback())

    @unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS') == '1', 'Opt-in real Chromium receipts.')
    def test_real_chromium_receipts_can_be_cited_without_a_third_test(self):
        from backend.tools import ToolGateway
        self.app.tools = ToolGateway(self.case.config)
        self.run['calls'] = []
        ids = [self.app._tool('group',self.run['id'],self.qa,'code.test',self.action['args'])['callId'] for _ in range(2)]
        prompt = self.feedback()
        self.assertIsNotNone(prompt)
        self.assertEqual(prompt['evidenceCallIds'],ids)
        accepted = self.app._review(self.run,self.qa,{'taskId':self.case.build['id'],'verdict':'pass',
            'targetSnapshot':self.app._review_snapshot(self.run,self.case.build),
            'evidenceCallIds':[ids[-1]],'feedback':'Actual Chromium verified immutable original runtime; no third test was needed.'})
        self.assertTrue(accepted['ok'],accepted)
        self.assertTrue(self.app._valid_review(self.run,self.case.build))
        self.assertEqual(len([c for c in self.run['calls'] if c['tool']=='code.test']),2)

    def test_decision_loop_uses_existing_receipts_and_requires_real_review_then_final(self):
        outer = self
        class Model:
            index = 0
            def complete(self, messages, max_tokens=None, deadline=None):
                self.index += 1
                if self.index == 1:
                    action = outer.action
                elif self.index == 2:
                    self_feedback = next(m for m in messages if m['content'].startswith('Browser evidence handoff: '))
                    outer.assertIn('observed-pass-2', self_feedback['content'])
                    outer.assertIsNotNone(latest_feedback_proof(outer.run,outer.qa))
                    action = {'type':'review','taskId':outer.case.build['id'],'verdict':'pass',
                        'targetSnapshot':outer.app._review_snapshot(outer.run,outer.case.build),
                        'evidenceCallIds':['observed-pass-2'],'feedback':'Reviewed the current immutable fixture and actual complete browser receipt.'}
                else:
                    action = {'type':'final','text':'Explicit review submitted using actual prior evidence.'}
                return {'content':json.dumps(action),'finish_reason':'stop','usage':{'total_tokens':1}}
        self.app.provider = Model()
        self.qa['_actions'] = [fingerprint(self.action)] * 2
        self.app._agent_loop('group',self.run['id'],self.qa)
        self.assertEqual(len([c for c in self.run['calls'] if c['tool']=='code.test']),2)
        self.assertEqual(len([e for e in self.run['events'] if e['type']=='browser_success_handoff']),1)
        self.assertTrue(self.app._valid_review(self.run,self.case.build))

    def test_ignoring_the_single_handoff_stops_without_a_third_execution_or_pass(self):
        outer = self
        class Model:
            def complete(self, messages, max_tokens=None, deadline=None):
                return {'content':json.dumps(outer.action),'finish_reason':'stop','usage':{'total_tokens':1}}
        self.app.provider = Model()
        self.qa['_actions'] = [fingerprint(self.action)] * 2
        with self.assertRaises(ProtocolError):
            self.app._agent_loop('group',self.run['id'],self.qa)
        self.assertEqual(len([e for e in self.run['events'] if e['type']=='browser_success_handoff']),1)
        self.assertEqual(len([c for c in self.run['calls'] if c['tool']=='code.test']),2)
        self.assertFalse(self.case.build.get('validation'))


if __name__ == '__main__':
    unittest.main()
