"""Human replies remain real final user turns, not assistant prefill questions."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.config import Config
from backend.context_budget import adapt_context
from backend.engine import Platform
from backend.runtime import compress_messages,fingerprint,ProtocolError
from tests.test_clarification_recovery import NoTools


class TailModel:
    def __init__(self):self.calls=[]
    def complete(self,messages,max_tokens=None,deadline=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            value={'tasks':[{'id':'clarify','title':'Confirm real scope','agentId':'coordinator'}]}
        else:
            self.calls.append(copy.deepcopy(messages))
            value=({'type':'ask_human','question':'Which actual scope should I use?'} if len(self.calls)==1 else
                   {'type':'final','text':'Used the actual requester response.','status':'done'})
        return {'content':json.dumps(value),'finish_reason':'stop','usage':{'total_tokens':2}}


class ClarificationContinuationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.model=TailModel()
        self.config=Config(Path(self.directory.name),{'LLM_API_KEY':'offline-tail','MAX_RETRIES':'0'})
        self.app=Platform(self.config,provider=self.model,tools=NoTools());self.apps=[self.app]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.directory.cleanup()
    def fixture(self,answer='A real user requirement.'):
        task=self.app._new_task('Use human reply','Continue real work','human',['coordinator'],'coordinator')
        binding={'question':'Which actual scope should I use?'}
        receipt={'id':'real-reply','approvalId':'actual-approval','runId':'run-tail','teamId':'default','channelId':'group',
                 'taskId':task['id'],'agentId':'coordinator','revision':0,'bindingHash':fingerprint(binding),
                 'choiceId':'answer','answer':answer,'late':False,'receivedAt':1}
        approval={'id':'actual-approval','type':'clarification','taskId':task['id'],'agentId':'coordinator',
                  'taskRevision':0,'binding':binding,'bindingHash':fingerprint(binding),'clarificationReceipt':receipt,
                  'choices':[],'answerDelivery':{'receiptId':receipt['id'],'appliedAt':2}}
        ask={'role':'assistant','content':json.dumps({'type':'ask_human','question':binding['question']})}
        raw=self.app._clarification_message(receipt)
        task['_messages']=[{'role':'system','content':'Return ONE action.'},
                           {'role':'user','content':json.dumps({'task':{'id':task['id']},'humanClarifications':[]})},
                           ask,{'role':'user','content':raw}]
        run={'id':'run-tail','teamId':'default','channelId':'group','steps':[task],'approvals':[approval],
             'events':[],'transcript':[{'kind':'action_result','text':raw}]}
        return run,task,approval,ask,raw

    def test_immediate_real_answer_stays_after_the_question_and_is_idempotent(self):
        run,task,approval,ask,raw=self.fixture();original=copy.deepcopy(approval)
        for _ in range(3):self.app._protect_clarification_context(run,task,'group')
        self.assertEqual(task['_messages'][-2:], [ask,{'role':'user','content':raw}])
        self.assertEqual(sum(message['content']==raw for message in task['_messages']),1)
        self.assertEqual(json.loads(task['_messages'][1]['content'])['humanClarifications'][0]['answer'],approval['clarificationReceipt']['answer'])
        self.assertEqual(approval,original);self.assertEqual(run['events'],[]);self.assertEqual(len(run['transcript']),1)

    def test_projection_restores_the_original_ask_and_receipt_if_compaction_removed_them(self):
        run,task,approval,ask,raw=self.fixture('Actual answer '*70)
        source=copy.deepcopy(task['_messages'])
        projected=copy.deepcopy(task['_messages'][:2])
        self.app._protect_clarification_context(run,{**task,'_messages':projected},'group',source_messages=source)
        self.assertEqual(projected[-2:],[ask,{'role':'user','content':raw}])
        task['_messages']+= [{'role':'user','content':'执行进度收据：'+('noise'*3000)} for _ in range(4)]
        self.app._protect_clarification_context(run,task,'group')
        for _ in range(3):
            original=copy.deepcopy(task['_messages'])
            def protect(view):
                self.app._protect_clarification_context(run,{**task,'_messages':view},'group',source_messages=original)
                return view
            projected,state=adapt_context(original,40000,protect_projection=protect)
            self.assertEqual(original,task['_messages'])
            self.assertEqual(projected[-1],{'role':'user','content':raw})
            self.assertLessEqual(state['characters'],state['activeChars']);task['_messages']=projected

    def test_completed_next_action_keeps_long_lived_canonical_without_old_answer_tail(self):
        run,task,approval,ask,raw=self.fixture()
        task['_messages'].append({'role':'assistant','content':json.dumps({'type':'accept'})})
        self.app._protect_clarification_context(run,task,'group')
        self.assertNotEqual(task['_messages'][-1]['content'],raw)
        self.assertFalse(any(message['content']==raw for message in task['_messages']))
        self.assertEqual(json.loads(task['_messages'][1]['content'])['humanClarifications'][0]['receiptId'],'real-reply')

    def test_fake_missing_wrong_revision_and_permission_receipts_cannot_create_answer_turns(self):
        for corruption in ('binding','delivery','revision','permission'):
            run,task,approval,ask,raw=self.fixture();task['_messages']=task['_messages'][:-1]
            if corruption=='binding':approval['clarificationReceipt']['bindingHash']='fake'
            elif corruption=='delivery':approval.pop('answerDelivery')
            elif corruption=='revision':task['revision']=1
            else:approval['type']='permission'
            self.app._protect_clarification_context(run,task,'group')
            with self.subTest(corruption=corruption):
                self.assertEqual(task['_messages'][-1],ask)
                self.assertFalse(any(message['content']==raw for message in task['_messages']))

    def test_same_question_with_changed_or_reordered_choices_cannot_reuse_old_reply(self):
        for choices in (['A','B'],['B','A'],['A','different B']):
            run,task,approval,ask,raw=self.fixture()
            approval['choices']=[{'id':'option-0','label':'A'},{'id':'option-1','label':'B'}]
            approval['clarificationReceipt']['choiceId']='option-0'
            raw=self.app._clarification_message(approval['clarificationReceipt'])
            ask['content']=json.dumps({'type':'ask_human','question':approval['binding']['question'],'choices':choices})
            task['_messages']=task['_messages'][:-1]
            self.app._protect_clarification_context(run,task,'group')
            with self.subTest(choices=choices):
                self.assertEqual(task['_messages'][-1]['content']==raw,choices==['A','B'])
        run,task,approval,ask,raw=self.fixture()
        task['_messages']=task['_messages'][:-1];task['_approvalId']='new-unanswered-request'
        self.app._protect_clarification_context(run,task,'group')
        self.assertEqual(task['_messages'][-1],ask,'a still waiting request cannot borrow an older answer')

    def test_time_only_model_receives_real_user_answer_at_end_and_restart_does_not_reapply(self):
        self.config.apply_time_only_profile()
        self.app.action({'action':'send','channelId':'group','mode':'task','participants':['coordinator'],'text':'Use the actual human reply.'})
        until=time.monotonic()+3
        while time.monotonic()<until:
            run=self.app.snapshot()['sessions']['group']['run']
            if run['approvals']:break
            time.sleep(.01)
        else:self.fail('Did not reach real clarification fixture.')
        self.app.action({'action':'approval','runId':run['id'],'approvalId':run['approvals'][0]['id'],
                         'choiceId':'answer','answer':'Use the actual simulated assumptions and continue.'})
        self.assertTrue(self.app.wait_for_idle(3));run=self.app.state['sessions']['group']['run']
        receipt=run['approvals'][0]['clarificationReceipt'];raw=self.app._clarification_message(receipt)
        self.assertEqual(self.model.calls[-1][-1],{'role':'user','content':raw})
        self.assertEqual(sum(event['type']=='clarification_applied' for event in run['events']),1)
        expected=copy.deepcopy(run['approvals'][0]['answerDelivery'])
        self.app.shutdown();self.app.wait_for_idle(3);self.app.repo.close();self.apps=[]
        restored=Platform(self.config,provider=TailModel(),tools=NoTools());self.apps.append(restored)
        saved=restored.state['sessions']['group']['run'];task=next(item for item in saved['steps'] if not item.get('_planning'))
        restored._protect_clarification_context(saved,task,'group')
        self.assertEqual(saved['approvals'][0]['answerDelivery'],expected)
        self.assertEqual(sum(event['type']=='clarification_applied' for event in saved['events']),1)
        self.assertFalse(any(message['content']==raw for message in task['_messages']))


if __name__=='__main__':unittest.main()
