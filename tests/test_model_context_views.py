"""Lossless evidence storage with smaller model views and real human handoffs.

Scripted responses and actual local file/SQLite operations only; no network/model.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.config import Config
from backend.artifacts import ArtifactStore
from backend.engine import Platform, _artifact_model_view
from backend.runtime import ProtocolError, compress_messages, fingerprint
from backend.tools import ToolGateway


class LocalFiles(ToolGateway):
    def capabilities(self):return {}


class ArtifactModel:
    CONTENT='Full immutable evidence.\n'*400
    def __init__(self):self.messages=[];self.counts={}
    def complete(self,messages,max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            action={'tasks':[{'id':'publish','title':'Publish source','agentId':'coordinator',
                              'outputContract':{'kind':'artifact','extensions':['.md']}},
                             {'id':'read','title':'Read source','agentId':'coordinator','dependsOn':['publish']}]}
        else:
            context=json.loads(messages[1]['content']);title=context['task']['title']
            self.messages.append((title,copy.deepcopy(messages)))
            n=self.counts.get(title,0);self.counts[title]=n+1
            actions=([{'type':'tool','name':'file.write','args':{'path':'source.md','content':self.CONTENT}},
                      {'type':'publish_artifact','path':'source.md'},
                      {'type':'final','text':'Published actual source.'}] if title=='Publish source' else
                     [{'type':'tool','name':'file.read','args':{'path':'source.md'}},
                      {'type':'final','text':'Read actual source.'}])
            action=actions[min(n,len(actions)-1)]
        return {'content':json.dumps(action),'finish_reason':'stop','usage':{'total_tokens':8}}


class DelegationModel:
    QUESTION='Which audience, length, sources and tone should the report use?'
    ANSWER='清水湾校园近期AI事件；网页与社交帖子；约800词；中立语气。'
    def __init__(self):self.parent_calls=0;self.child_context=None;self.parent_messages=[]
    def complete(self,messages,max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            action={'tasks':[{'id':'parent','title':'Clarify and delegate','agentId':'coordinator'}]}
        else:
            context=json.loads(messages[1]['content'])
            if context['task']['title']=='Write requested report':
                self.child_context=copy.deepcopy(context)
                action={'type':'final','text':'Received the real user requirements.'}
            else:
                self.parent_messages.append(copy.deepcopy(messages))
                n=self.parent_calls;self.parent_calls+=1
                if n==0:action={'type':'ask_human','question':self.QUESTION,'choices':['Use those requirements']}
                elif n==1:action={'type':'delegate','task':{'title':'Write requested report','agentId':'research'}}
                elif n==2:
                    result=json.loads(next(m['content'][len('动作的真实结果：'):] for m in reversed(messages)
                                           if m['content'].startswith('动作的真实结果：')))
                    action={'type':'await_task','taskId':result['task']['id']}
                else:action={'type':'final','text':'Child completed using the saved actual answer.'}
        return {'content':json.dumps(action,ensure_ascii=False),'finish_reason':'stop','usage':{'total_tokens':8}}


class ModelContextViewTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.app=None
    def tearDown(self):
        if self.app:self.app.shutdown();self.app.wait_for_idle(3);self.app.repo.close()
        self.temp.cleanup()
    def create(self,model):
        config=Config(Path(self.temp.name),{'LLM_API_KEY':'context-fixture','MAX_RETRIES':'0',
            'MAX_AGENT_TURNS':'10','MAX_MODEL_CALLS':'20','MAX_RUN_TOKENS':'400000','MAX_CONTEXT_CHARS':'80000'})
        self.app=Platform(config,provider=model,tools=LocalFiles(config));return self.app

    def test_artifact_views_omit_only_duplicate_content_and_real_reads_remain_full(self):
        model=ArtifactModel();app=self.create(model)
        app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Publish and read real evidence.'})
        self.assertTrue(app.wait_for_idle(4));run=app.snapshot()['sessions']['group']['run']
        self.assertEqual(run['status'],'done');artifact=run['artifacts'][0]
        digest=hashlib.sha256(model.CONTENT.encode()).hexdigest()
        self.assertEqual(artifact['content'],model.CONTENT);self.assertEqual(artifact['sha256'],digest)
        pub_messages=next(ms for title,ms in model.messages if title=='Publish source' and any('"artifact":' in m['content'] for m in ms))
        pub_result=next(json.loads(m['content'][len('动作的真实结果：'):]) for m in pub_messages
                        if m['content'].startswith('动作的真实结果：') and '"artifact":' in m['content'])
        view=pub_result['artifact'];self.assertNotIn('content',view)
        self.assertEqual(view['id'],artifact['id']);self.assertEqual(view['sha256'],digest)
        self.assertEqual(view['path'],'source.md');self.assertTrue(view['contentOmitted'])
        self.assertEqual(view['contentAccess']['tool'],'file.read')
        self.assertEqual(view['contentAccess']['immutableSnapshotAction']['toAgent'],'coordinator')
        self.assertLess(len(json.dumps(pub_result)),len(model.CONTENT)//4)
        read_messages=[ms for title,ms in model.messages if title=='Read source']
        own=json.loads(read_messages[0][1]['content'])['ownArtifacts'][0]
        self.assertNotIn('content',own);self.assertEqual(own['sha256'],digest)
        read_result=next(json.loads(m['content'][len('动作的真实结果：'):]) for m in read_messages[-1]
                         if m['content'].startswith('动作的真实结果：'))
        self.assertEqual(read_result['content'],model.CONTENT,'file.read body is never metadata-projected')
        stored=app.repo.get_run(run['id'])
        self.assertEqual(stored['artifacts'][0]['content'],model.CONTENT)
        self.assertEqual(app.artifacts.get(artifact['id'])['content'],model.CONTENT)
        self.assertEqual(ArtifactStore(app.config.data_dir).get(artifact['id'])['content'],model.CONTENT)
        read_call=next(c for c in stored['calls'] if c.get('tool')=='file.read')
        self.assertEqual(read_call['result']['content'],model.CONTENT)
        full_publish=next(json.loads(t['text']) for t in stored['transcript']
                          if t.get('kind')=='action_result' and t.get('action')=='publish_artifact')
        self.assertEqual(full_publish['artifact']['content'],model.CONTENT)
        original=copy.deepcopy(artifact);_artifact_model_view(artifact);self.assertEqual(artifact,original)

    def test_parent_real_clarification_reaches_delegated_child_once_not_operation_grants(self):
        model=DelegationModel();app=self.create(model)
        app.action({'action':'send','mode':'task','participants':['coordinator','research'],'text':'Clarify and delegate.'})
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            run=app.snapshot()['sessions']['group']['run']
            if run['approvals']:break
            time.sleep(.01)
        else:self.fail('Expected an actual pending clarification')
        with app.changed:
            current=app.state['sessions']['group']['run']
            # Critical approvals cannot become shared requirements even if they
            # carry a misleading similarly named receipt field.
            for kind in ('permission','budget','reconciliation'):
                fake=copy.deepcopy(current['approvals'][0]);fake.update(id='fake-'+kind,type=kind,status='resolved')
                fake['clarificationReceipt']={'id':'fake-receipt','answer':'OPERATION-SECRET'}
                current['approvals'].append(fake)
        app.action({'action':'approval','runId':run['id'],'approvalId':run['approvals'][0]['id'],
                    'choiceId':'option-0','answer':model.ANSWER})
        self.assertTrue(app.wait_for_idle(4));self.assertIsNotNone(model.child_context)
        answers=model.child_context['humanClarifications'];self.assertEqual(len(answers),1)
        self.assertEqual(answers[0]['question'],model.QUESTION);self.assertEqual(answers[0]['answer'],model.ANSWER)
        self.assertEqual(answers[0]['choiceLabel'],'Use those requirements')
        self.assertNotIn('OPERATION-SECRET',json.dumps(model.child_context))
        self.assertEqual(sum(m['content'].count(model.ANSWER) for m in model.parent_messages[1]),1)
        current=app.state['sessions']['group']['run'];approval=current['approvals'][0]
        self.assertEqual(answers[0]['receiptId'],approval['clarificationReceipt']['id'])
        child=next(t for t in current['steps'] if t.get('parentId'))
        for key,value in (('runId','another-run'),('teamId','another-team'),('channelId','research'),('bindingHash','bad-hash')):
            bad=copy.deepcopy(approval);bad['id']='wrong-'+key
            bad['clarificationReceipt'].update(id='bad-receipt-'+key,approvalId=bad['id'])
            bad['clarificationReceipt'][key]=value;current['approvals'].append(bad)
        duplicate=copy.deepcopy(approval);current['approvals'].append(duplicate)
        self.assertEqual(len(app._human_clarifications(current,child,'group')),1)
        self.assertEqual(app._human_clarifications(current,child,'coordinator'),[])

    def test_compression_keeps_all_exact_clarifications_and_artifact_evidence(self):
        answers=[{'receiptId':'reply-'+str(i),'question':'Full question '+str(i)+'?'*400,
                  'choiceLabel':'Original label','answer':'精确条件'+str(i)+'。'*200} for i in range(6)]
        context={'task':{'id':'target','goal':'Use actual answers'},'humanClarifications':answers,
                 'groupHistory':[{'text':'old bulk '*10000}],
                 'ownArtifacts':[{'id':'artifact-1','name':'source.md','path':'source.md','sha256':'a'*64,
                                  'size':9000,'contentOmitted':True}]}
        messages=[{'role':'system','content':'Return one JSON action. '*70},
                  {'role':'user','content':json.dumps(context,ensure_ascii=False)},
                  {'role':'assistant','content':'large old response '*6000}]
        original=copy.deepcopy(messages);compressed=compress_messages(messages,10000)
        self.assertEqual(messages,original)
        self.assertLessEqual(sum(len(m['content']) for m in compressed),10000)
        shared=json.loads(compressed[1]['content']);self.assertEqual(shared['humanClarifications'],answers)
        self.assertEqual(shared['ownArtifacts'][0]['sha256'],'a'*64)
        self.assertEqual(shared['ownArtifacts'][0]['size'],9000)

    def test_clarification_capacity_failure_is_explicit_not_silent_requirement_loss(self):
        context={'task':{'id':'target'},'humanClarifications':[{'receiptId':'reply','question':'Q','answer':'a'*5000}]}
        messages=[{'role':'system','content':'protocol'}, {'role':'user','content':json.dumps(context)}]
        for limit in (0,1000):
            with self.subTest(limit=limit),self.assertRaisesRegex(ProtocolError,'真实澄清问答'):
                compress_messages(messages,limit)

    def test_restored_task_updates_canonical_answers_before_old_tail_is_dropped(self):
        app=self.create(ArtifactModel());source=app._new_task('Parent','Clarify','human',['coordinator','research'],'coordinator')
        target=app._new_task('Child','Use requirements','coordinator',['coordinator','research'],'research',parent=source['id'])
        target['_messages']=[{'role':'system','content':'Return one JSON action.'},
                             {'role':'user','content':json.dumps({'task':{'id':target['id']},'keep':'original objective','humanClarifications':[]})}]
        binding={'question':'Use the exact later answer?'};answer='Precise real requirements. '*100
        receipt={'id':'human-receipt','approvalId':'approval-1','runId':'run-context','teamId':'default','channelId':'group',
                 'taskId':source['id'],'agentId':'coordinator','revision':0,'bindingHash':fingerprint(binding),
                 'choiceId':'answer','answer':answer,'late':False,'receivedAt':1}
        approval={'id':'approval-1','type':'clarification','taskId':source['id'],'agentId':'coordinator',
                  'binding':binding,'bindingHash':fingerprint(binding),'clarificationReceipt':receipt,'choices':[]}
        run={'id':'run-context','title':'Restore exact requirements','status':'running','teamId':'default','channelId':'group',
             'participants':['coordinator','research'],'steps':[source,target],'approvals':[approval],
             'artifacts':[],'calls':[],'events':[],'memoryEnabled':False}
        app.state['sessions']['group']['run']=run
        app._prepare_task('group',run['id'],target)
        initial=json.loads(target['_messages'][1]['content']);self.assertEqual(initial['keep'],'original objective')
        self.assertEqual(initial['humanClarifications'][0]['answer'],answer)
        self.assertEqual(sum(m['content'].count(answer) for m in target['_messages']),1)
        target['_messages'] += [{'role':'assistant','content':'Later large turn. '*2000} for _ in range(6)]
        compressed=compress_messages(target['_messages'],7000)
        self.assertEqual(json.loads(compressed[1]['content'])['humanClarifications'][0]['answer'],answer)
        with self.assertRaisesRegex(ProtocolError,'真实澄清问答'):
            compress_messages(target['_messages'],1000)

    def test_consumed_own_answer_survives_thirteen_turns_compression_and_restart(self):
        app=self.create(ArtifactModel());task=app._new_task('Use own answer','Continue','human',['coordinator'],'coordinator')
        answer='Exact own human requirements. '*80;binding={'question':'What should I build?'}
        receipt={'id':'own-reply','approvalId':'own-approval','runId':'own-run','teamId':'default','channelId':'group',
                 'taskId':task['id'],'agentId':'coordinator','revision':0,'bindingHash':fingerprint(binding),
                 'choiceId':'answer','answer':answer,'late':False,'receivedAt':1}
        approval={'id':'own-approval','type':'clarification','taskId':task['id'],'agentId':'coordinator',
                  'binding':binding,'bindingHash':fingerprint(binding),'clarificationReceipt':receipt,'choices':[],
                  'answerDelivery':{'receiptId':receipt['id'],'appliedAt':2}}
        raw=app._clarification_message(receipt)
        task['_messages']=[{'role':'system','content':'Return one JSON action.'},
                           {'role':'user','content':json.dumps({'task':{'id':task['id']},'humanClarifications':[]})},
                           {'role':'user','content':raw}]
        task['_messages'] += [{'role':'assistant','content':str(i)+' large later turn. '*1000} for i in range(13)]
        run={'id':'own-run','title':'Preserve own requirements','status':'running','teamId':'default','channelId':'group',
             'participants':['coordinator'],'steps':[task],'approvals':[approval],
             'artifacts':[],'calls':[],'events':[],'memoryEnabled':False,
             'transcript':[{'kind':'action_result','action':'ask_human','text':raw}]}
        app.state['sessions']['group']['run']=run
        self.assertEqual(app._human_clarifications(run,task,'group'),[],'raw receipt already contains the answer once')
        app._protect_clarification_context(run,task,'group')
        task['_messages'][:]=compress_messages(task['_messages'],6500)
        self.assertEqual(json.loads(task['_messages'][1]['content'])['humanClarifications'][0]['answer'],answer)
        self.assertFalse(any(m['content']==raw for m in task['_messages']))
        self.assertEqual(sum(m['content'].count(answer) for m in task['_messages']),1)
        self.assertEqual(run['transcript'][0]['text'],raw)
        self.assertEqual(approval['answerDelivery'],{'receiptId':'own-reply','appliedAt':2})
        self.assertEqual(app._human_clarifications(run,task,'group')[0]['answer'],answer)
        app._prepare_task('group',run['id'],task)
        self.assertEqual(sum(m['content'].count(answer) for m in task['_messages']),1)
        app.shutdown();app.wait_for_idle(3);app.repo.close();self.app=None
        restored=self.create(ArtifactModel())
        stored=restored.state['sessions']['group']['run']
        restored_task=stored['steps'][0]
        self.assertEqual(sum(m['content'].count(answer) for m in restored_task['_messages']),1)
        self.assertEqual(stored['approvals'][0]['answerDelivery']['receiptId'],'own-reply')
        self.assertEqual(stored['transcript'][0]['text'],raw)


if __name__=='__main__':unittest.main()
