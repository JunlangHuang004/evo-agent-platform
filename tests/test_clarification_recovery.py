"""Durable real human replies: local fixtures only, no model or tool network."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from backend.config import Config
from backend.engine import ActionError, Platform


class NoTools:
    def capabilities(self): return {}
    def definitions(self, agent): return []
    def cancel(self, run_id): return None


class QuestionModel:
    def __init__(self, ask=False, block=None):
        self.ask = ask
        self.messages = []
        self.entered = threading.Event()
        self.block = block

    def complete(self, messages, max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            result = {'tasks':[{'id':'question','title':'Confirm requirements','agentId':'coordinator'}]}
        else:
            self.messages.append(copy.deepcopy(messages))
            if self.ask and len(self.messages)==1:
                result = {'type':'ask_human','question':'What actual opening hours should be used?'}
            else:
                self.entered.set()
                if self.block:
                    self.block.wait(3)
                result = {'type':'final','text':'The actual human answer was retained.','status':'done'}
        return {'content':json.dumps(result),'finish_reason':'stop','usage':{'total_tokens':4}}


class ClarificationRecoveryTests(unittest.TestCase):
    ANSWER = '真实答复：08:00–20:00，仅预约人可以取消。'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.apps = []
        self.releases = []
        self.sequence = 0

    def tearDown(self):
        for event in self.releases: event.set()
        for app in self.apps: app.shutdown()
        for app in self.apps:
            app.wait_for_idle(3)
            app.repo.close()
        self.temp.cleanup()

    def wait(self, predicate):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            value=predicate()
            if value: return value
            time.sleep(.01)
        self.fail('Local clarification fixture did not reach the expected state.')

    def create(self, model, config=None):
        if config is None:
            self.sequence+=1
            config=Config(Path(self.temp.name)/str(self.sequence),
                          {'LLM_API_KEY':'test-only','MAX_RETRIES':'0','MAX_MODEL_CALLS':'20',
                           'MAX_RUN_TOKENS':'200000','MAX_CONTEXT_CHARS':'80000'})
        app=Platform(config,provider=model,tools=NoTools())
        self.apps.append(app)
        return app

    def stop(self, app):
        app.shutdown()
        self.assertTrue(app.wait_for_idle(3))
        app.repo.close()
        self.apps.remove(app)

    def current(self, app): return app.snapshot()['sessions']['group']['run']

    def waiting(self):
        app=self.create(QuestionModel(ask=True))
        app.action({'action':'send','channelId':'group','mode':'task','participants':['coordinator'],
                    'text':'Ask the real human for the missing requirements.'})
        self.wait(lambda: self.current(app)['approvals'])
        return app

    def interrupted(self, expired=True):
        original=self.waiting()
        if expired:
            with original.changed:
                original.state['sessions']['group']['run']['approvals'][0]['expiresAt']=0
                original._persist()
        config=original.config
        self.stop(original)
        model=QuestionModel()
        return self.create(model,config),model

    def reply(self, app, answer=None):
        run=self.current(app)
        return app.action({'action':'approval','channelId':'group','runId':run['id'],
                           'approvalId':run['approvals'][0]['id'],'choiceId':'answer',
                           'answer':self.ANSWER if answer is None else answer})

    def deliveries(self, app):
        task=next(t for t in app.state['sessions']['group']['run']['steps']
                  if t.get('agentId')=='coordinator' and not t.get('_planning') and t.get('kind')!='planning')
        return [m for m in task['_messages'] if m['role']=='user' and m['content'].startswith('动作的真实结果：')]

    def assert_delivered_once(self, app, model):
        self.assertEqual(len(self.deliveries(app)),1)
        self.assertIn(self.ANSWER,self.deliveries(app)[0]['content'])
        self.assertEqual(sum(m['content'].count(self.ANSWER) for m in model.messages[0]),1)
        approval=self.current(app)['approvals'][0]
        self.assertEqual(approval['answerDelivery']['receiptId'],approval['clarificationReceipt']['id'])
        self.assertEqual(sum(e['type']=='clarification_applied' for e in self.current(app)['events']),1)
        self.assertFalse(any(t.get('_approvalId') for t in app.state['sessions']['group']['run']['steps']))

    def test_normal_answer_has_one_durable_delivery(self):
        app=self.waiting()
        self.reply(app)
        self.assertTrue(app.wait_for_idle(3))
        self.assertEqual(self.current(app)['status'],'done')
        self.assertEqual(len(self.deliveries(app)),1)
        approval=self.current(app)['approvals'][0]
        self.assertEqual(approval['status'],'resolved')
        self.assertFalse(approval['canRespond'])
        self.assertEqual(approval['answerDelivery']['receiptId'],approval['clarificationReceipt']['id'])

    def test_original_revision_zero_row_without_new_metadata_accepts_only_bound_late_answer(self):
        app,model=self.interrupted()
        with app.changed:
            approval=app.state['sessions']['group']['run']['approvals'][0]
            for key in ('runId','teamId','channelId','taskRevision'):
                approval.pop(key,None)
            app._persist()
        self.assertTrue(self.current(app)['approvals'][0]['canRespond'])
        self.reply(app)
        app.action({'action':'recover','channelId':'group'})
        self.assertTrue(app.wait_for_idle(3))
        self.assert_delivered_once(app,model)

    def test_legacy_resolved_real_answer_is_migrated_before_model_call(self):
        app,model=self.interrupted(expired=False)
        with app.changed:
            approval=app.state['sessions']['group']['run']['approvals'][0]
            approval.update(status='resolved',resolvedAt=approval['createdAt'],
                            response={'choiceId':'answer','answer':self.ANSWER})
            app._persist()
        app.action({'action':'recover','channelId':'group'})
        self.assertTrue(app.wait_for_idle(3))
        self.assertEqual(self.current(app)['status'],'done')
        self.assert_delivered_once(app,model)

    def test_corrupt_receipt_binding_never_reaches_model(self):
        app,model=self.interrupted()
        self.reply(app)
        with app.changed:
            approval=app.state['sessions']['group']['run']['approvals'][0]
            approval['clarificationReceipt']['revision']=99
            app._persist()
        app.action({'action':'recover','channelId':'group'})
        self.assertTrue(app.wait_for_idle(3))
        self.assertEqual(model.messages,[])
        self.assertEqual(self.deliveries(app),[])
        self.assertEqual(self.current(app)['status'],'failed')

    def test_late_reply_persists_across_another_restart_and_needs_explicit_recover(self):
        app,model=self.interrupted()
        approval=self.current(app)['approvals'][0]
        self.assertTrue(approval['canRespond'])
        self.assertTrue(approval['acceptsLateResponse'])
        self.reply(app)
        self.assertEqual(self.current(app)['status'],'interrupted')
        self.assertEqual(len(model.messages),0)
        self.assertEqual(self.deliveries(app),[])
        original=self.current(app)['approvals'][0]
        self.assertEqual(original['status'],'expired')
        self.assertEqual(original['expiresAt'],0)
        self.assertEqual(original['lateResponse']['answer'],self.ANSWER)
        self.assertFalse(original['canRespond'])
        count=len(app.snapshot()['sessions']['group']['messages'])
        self.reply(app)  # retrying the same browser request is an idempotent no-op
        self.assertEqual(len(app.snapshot()['sessions']['group']['messages']),count)
        with self.assertRaises(ActionError): self.reply(app,'A different answer cannot overwrite the receipt.')
        config=app.config
        self.stop(app)
        resumed_model=QuestionModel()
        restored=self.create(resumed_model,config)
        self.assertEqual(self.current(restored)['status'],'interrupted')
        restored.action({'action':'recover','channelId':'group'})
        self.assertTrue(restored.wait_for_idle(3))
        self.assertEqual(self.current(restored)['status'],'done')
        self.assert_delivered_once(restored,resumed_model)

    def test_expired_unanswered_restore_does_not_call_model_or_inject_synthetic_denial(self):
        app,model=self.interrupted()
        # Older releases stored this automatic expiry denial. It is not a human reply.
        with app.changed:
            approval=app.state['sessions']['group']['run']['approvals'][0]
            approval.update(status='expired',response={'choiceId':'deny','answer':'授权已过期，未执行动作。'})
            app._persist()
        app.action({'action':'recover','channelId':'group'})
        self.wait(lambda: self.current(app)['status']=='waiting_human')
        time.sleep(.06)
        self.assertEqual(model.messages,[])
        self.assertEqual(self.deliveries(app),[])
        self.assertTrue(self.current(app)['approvals'][0]['canRespond'])
        self.reply(app)
        self.assertTrue(app.wait_for_idle(3))
        self.assert_delivered_once(app,model)
        self.assertNotIn('授权已过期，未执行动作。',self.deliveries(app)[0]['content'])

    def test_restart_after_delivery_does_not_duplicate_answer(self):
        release=threading.Event();self.releases.append(release)
        app,unused=self.interrupted()
        blocking=QuestionModel(block=release)
        app.provider=blocking
        self.reply(app)
        app.action({'action':'recover','channelId':'group'})
        self.assertTrue(blocking.entered.wait(3))
        self.assertEqual(len(self.deliveries(app)),1)
        app.shutdown()
        release.set()
        self.assertTrue(app.wait_for_idle(3))
        config=app.config
        app.repo.close();self.apps.remove(app)
        model=QuestionModel()
        restored=self.create(model,config)
        restored.action({'action':'recover','channelId':'group'})
        self.assertTrue(restored.wait_for_idle(3))
        self.assert_delivered_once(restored,model)

    def test_paused_reply_is_saved_without_unpausing_or_model_call(self):
        app=self.waiting()
        app.action({'action':'pause','channelId':'group'})
        self.reply(app)
        self.assertEqual(self.current(app)['status'],'paused')
        self.assertEqual(len(app.provider.messages),1)
        self.assertEqual(self.deliveries(app),[])
        app.action({'action':'resume','channelId':'group'})
        self.assertTrue(app.wait_for_idle(3))
        self.assertEqual(len(self.deliveries(app)),1)

    def test_stale_revision_replaced_pointer_owner_or_binding_cannot_accept_late_reply(self):
        for change in ('revision','pointer','owner','binding','terminal'):
            with self.subTest(change=change):
                app,model=self.interrupted()
                with app.changed:
                    run=app.state['sessions']['group']['run'];approval=run['approvals'][0]
                    task=app._task(run,approval['taskId'])
                    if change=='revision':task['revision']=1
                    if change=='pointer':task['_approvalId']='some-new-question'
                    if change=='owner':task['agentId']='research'
                    if change=='binding':approval['binding']['question']='A substituted question'
                    if change=='terminal':task['status']='done'
                    app._persist()
                self.assertFalse(self.current(app)['approvals'][0]['canRespond'])
                with self.assertRaises(ActionError):self.reply(app)
                self.assertNotIn('clarificationReceipt',self.current(app)['approvals'][0])
                self.assertEqual(model.messages,[])

    def test_critical_expired_approvals_never_gain_late_response_support(self):
        for kind in ('permission','budget','reconciliation'):
            with self.subTest(kind=kind):
                app,model=self.interrupted()
                with app.changed:
                    approval=app.state['sessions']['group']['run']['approvals'][0]
                    approval.update(type=kind,status='pending',choices=[{'id':'allow','label':'Allow'}])
                    app._persist()
                public=self.current(app)['approvals'][0]
                self.assertFalse(public['canRespond'])
                run=self.current(app)
                with self.assertRaises(ActionError):
                    app.action({'action':'approval','channelId':'group','runId':run['id'],
                                'approvalId':public['id'],'choiceId':'allow'})
                result=self.current(app)['approvals'][0]
                self.assertEqual(result['status'],'expired')
                self.assertNotIn('clarificationReceipt',result)
                self.assertNotIn('lateResponse',result)
                self.assertEqual(model.messages,[])


if __name__=='__main__': unittest.main()
