"""Explicit bounded operator continuation; scripted model and real local files."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import ActionError, Platform
from backend.tools import ToolGateway


class LocalFiles(ToolGateway):
    def capabilities(self):return {}


class ContinuationModel:
    def __init__(self):self.counts={};self.resumed_context=None
    def complete(self,messages,max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            action={'tasks':[{'id':'design','title':'Design','agentId':'research'},
                {'id':'build','title':'Build','agentId':'executor','dependsOn':['design'],
                 'outputContract':{'kind':'artifact','extensions':['.txt']}},
                {'id':'qa','title':'QA','agentId':'reviewer','dependsOn':['build']},
                {'id':'pack','title':'Package','agentId':'coordinator','dependsOn':['qa','build'],'outputContract':{'kind':'delivery'}},
                {'id':'summary','title':'Summary','agentId':'coordinator','dependsOn':['pack']}]}
        else:
            context=json.loads(messages[1]['content']);title=context['task']['title']
            n=self.counts.get(title,0);self.counts[title]=n+1
            results=[json.loads(m['content'][len('动作的真实结果：'):]) for m in messages if m['content'].startswith('动作的真实结果：')]
            if title=='Build':
                if n==0:action={'type':'tool','name':'file.write','args':{'path':'product.txt','content':'Actual retained product bytes.\n'}}
                elif n==1:raise RuntimeError('Known bookkeeping failure after the completed write')
                elif n==2:
                    self.resumed_context=copy.deepcopy(context)
                    action={'type':'publish_artifact','path':'product.txt'}
                else:action={'type':'final','text':'Published the retained file without repeating the write.'}
            elif title=='QA':
                if n==0:action={'type':'tool','name':'file.read','args':{'path':context['authorizedArtifacts'][0]['path']}}
                elif n==1:
                    assert results[-1]['content']=='Actual retained product bytes.\n'
                    action={'type':'review','taskId':context['dependencyResults'][0]['id'],'verdict':'pass',
                            'feedback':'Independently read the actual current text output.'}
                else:action={'type':'final','text':'Recorded the actual editorial verification.'}
            elif title=='Package':
                if n==0:
                    build=next(d for d in context['dependencyResults'] if d['title']=='Build')
                    action={'type':'package_delivery','artifactIds':build['outputs'],'entrypoint':'product.txt','instructions':'Read product.txt.'}
                else:action={'type':'final','text':'The actual immutable bundle was packaged.'}
            elif title=='Summary':
                action={'type':'final','status':'partial' if n==0 else 'done',
                        'text':'Build failed; no complete delivery.' if n==0 else 'Existing artifact, independent verification and package are recorded.'}
            elif title=='Design':
                action=[{'type':'tool','name':'file.write','args':{'path':'requirements.md','content':'Actual agreed requirements.'}},
                        {'type':'publish_artifact','path':'requirements.md'},
                        {'type':'final','text':'Requirements were recorded once.'}][min(n,2)]
            else:action={'type':'final','text':'Recorded actual result.'}
        return {'content':json.dumps(action),'finish_reason':'stop','usage':{'total_tokens':7}}


class RetryTaskTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.app=None
    def tearDown(self):
        if self.app:self.app.shutdown();self.app.wait_for_idle(3);self.app.repo.close()
        self.temp.cleanup()
    def failed(self):
        config=Config(Path(self.temp.name),{'LLM_API_KEY':'offline-retry-fixture','MAX_RETRIES':'1','MAX_AGENT_TURNS':'12',
            'MAX_MODEL_CALLS':'50','MAX_RUN_TOKENS':'400000','MAX_RUN_SECONDS':'600','MAX_CONTEXT_CHARS':'80000'})
        model=ContinuationModel();self.app=Platform(config,provider=model,tools=LocalFiles(config))
        self.app.action({'action':'send','mode':'task','participants':['coordinator','research','executor','reviewer'],
                         'text':'Deliver a small actual text product with independent review.'})
        self.assertTrue(self.app.wait_for_idle(4));run=self.app.state['sessions']['group']['run']
        self.assertEqual(run['status'],'partial');target=next(t for t in run['steps'] if t['title']=='Build')
        self.assertEqual(target['status'],'failed');self.assertFalse(target['outputs'])
        return model,run,target
    def payload(self,run,target):return {'action':'retry_task','runId':run['id'],'taskId':target['id']}

    def test_retry_continues_existing_files_and_new_qa_without_replaying_completed_work(self):
        model,run,target=self.failed();before=copy.deepcopy(run);export=Path(self.temp.name)/'old-observation.json'
        export.write_text(json.dumps(before,ensure_ascii=False));original_export=export.read_bytes()
        old_calls=[c['id'] for c in run['calls']];old_turns=target['turns'];old_budget=copy.deepcopy(run['budget'])
        old_artifacts=copy.deepcopy(run['artifacts']);design_calls=model.counts['Design']
        payload=self.payload(run,target);payload['feedback']='The bookkeeping defect is fixed; inspect and publish retained files.'
        self.app.action(payload);self.assertTrue(self.app.wait_for_idle(5))
        run=self.app.state['sessions']['group']['run'];self.assertEqual(run['status'],'done',run.get('summary'))
        self.assertEqual(run['budget'],old_budget);self.assertEqual([c['id'] for c in run['calls'][:len(old_calls)]],old_calls)
        self.assertIn(payload['feedback'],model.resumed_context['task']['feedback'][-1])
        self.assertEqual(model.resumed_context['task']['goal'],target['goal'])
        writes=[c for c in run['calls'] if c['tool']=='file.write' and c['taskId']==target['id']];self.assertEqual(len(writes),1)
        original_write=next(c for c in before['calls'] if c['tool']=='file.write' and c['taskId']==target['id'])
        self.assertEqual(writes[0]['result']['sha256'],original_write['result']['sha256'])
        self.assertEqual(run['artifacts'][:len(old_artifacts)],old_artifacts)
        self.assertGreater(target['turns'],old_turns);self.assertEqual(target['retries'],1)
        history=target['attemptHistory'][0];self.assertEqual(history['status'],'failed');self.assertEqual(history['turns'],old_turns)
        self.assertIn('bookkeeping',history['error']['message'])
        self.assertEqual(model.counts['Design'],design_calls,'completed tasks are not rerun')
        qa=next(t for t in run['steps'] if t['title']=='QA');self.assertEqual(qa['attemptHistory'][0]['turns'],0)
        self.assertEqual(qa['status'],'done');self.assertTrue(any(c['taskId']==qa['id'] and c['tool']=='file.read' for c in run['calls']))
        summary=next(t for t in run['steps'] if t.get('finalSummary'))
        self.assertEqual(summary['attemptHistory'][0]['status'],'partial');self.assertGreater(summary['turns'],summary['attemptHistory'][0]['turns'])
        self.assertIn('Retained error fact: code=runtime', '\n'.join(model.resumed_context['task']['feedback']))
        self.assertIn('Known bookkeeping failure', '\n'.join(model.resumed_context['task']['feedback']))
        self.assertEqual(len(run['deliveries']),1);self.assertEqual(run['deliveries'][0]['semanticQuality'],'unassessed')
        self.assertEqual(export.read_bytes(),original_export)
        self.assertEqual(len([e for e in run['events'] if e['type']=='task_retry_requested']),1)
        self.assertEqual(self.app.repo.get_run(run['id'])['calls'],run['calls'])

    def test_partial_step_without_outputs_continues_and_keeps_its_error(self):
        model,run,target=self.failed()
        target['status']='partial'
        target['error']={'code':'budget','message':'已达到人工扩展后的资源预算。'}
        self.app.action(self.payload(run,target));self.assertTrue(self.app.wait_for_idle(5))
        run=self.app.state['sessions']['group']['run'];self.assertEqual(run['status'],'done',run.get('summary'))
        self.assertEqual(target['attemptHistory'][0]['status'],'partial')
        self.assertEqual(target['attemptHistory'][0]['error']['message'],'已达到人工扩展后的资源预算。')
        self.assertIn('已达到人工扩展后的资源预算。','\n'.join(model.resumed_context['task']['feedback']))

    def test_active_stale_missing_ids_and_live_workers_are_rejected_without_mutation(self):
        _,run,target=self.failed();payload=self.payload(run,target)
        for override in ({'runId':None},{'runId':'stale-run'},{'taskId':None}):
            with self.subTest(override=override),self.assertRaises(ActionError):self.app.action({**payload,**override})
        run['status']='running'
        with self.assertRaises(ActionError):self.app.action(payload)
        run['status']='partial'
        worker=type('LiveWorker',(),{'is_alive':lambda self:True})();self.app.workers.add(worker)
        try:
            with self.assertRaises(ActionError):self.app.action(payload)
        finally:self.app.workers.remove(worker)
        for registry,key in ((self.app.runners,run['id']),(self.app.task_workers,target['id'])):
            previous=registry.get(key);registry[key]=worker
            try:
                with self.assertRaises(ActionError):self.app.action(payload)
            finally:
                if previous is None:registry.pop(key)
                else:registry[key]=previous
        self.assertNotIn('attemptHistory',target)

    def test_pending_calls_reserved_effects_and_task_bindings_cannot_be_bypassed(self):
        _,run,target=self.failed();payload=self.payload(run,target)
        for key,value in (('_pendingTool',{'phase':'executing'}),('_uncertainCalls',['unknown']),('_approvalId','old'),('_reworkWait',{'taskId':'old'})):
            target[key]=value
            with self.subTest(key=key),self.assertRaises(ActionError):self.app.action(payload)
            target.pop(key)
        call=next(c for c in run['calls'] if c['tool']=='file.write');old=call['status'];call['status']='unknown'
        with self.assertRaises(ActionError):self.app.action(payload)
        call['status']=old
        self.app.repo.reserve_effect('unknown-effect',{'runId':run['id'],'taskId':target['id'],'tool':'git.push'})
        with self.assertRaisesRegex(ActionError,'未确认 effect'):self.app.action(payload)
        self.assertEqual(self.app.repo.effect('unknown-effect')['status'],'reserved');self.assertNotIn('attemptHistory',target)

    def test_retries_and_original_turn_global_budgets_are_hard_limits(self):
        _,run,target=self.failed();payload=self.payload(run,target)
        for field,value in (('modelCalls',run['budget']['modelCalls']),('tokenUsage',run['budget']['tokens']),('activeMs',run['budget']['timeMs'])):
            old=run[field];run[field]=value
            with self.subTest(field=field),self.assertRaises(ActionError):self.app.action(payload)
            run[field]=old
        target['retries']=1
        with self.assertRaises(ActionError):self.app.action(payload)
        target['retries']=0;target['turns']=self.app.config.max_agent_turns
        with self.assertRaises(ActionError):self.app.action(payload)
        target['turns']=2;target.setdefault('progress',{})['turnLimit']=self.app.config.max_agent_turns-1
        with self.assertRaisesRegex(ActionError,'增加已保存'):self.app.action(payload)
        self.assertNotIn('attemptHistory',target)

    def test_summary_with_output_or_side_effect_is_not_reopened(self):
        _,run,target=self.failed();summary=next(t for t in run['steps'] if t.get('finalSummary'))
        run['calls'].append({'id':'summary-write','taskId':summary['id'],'agentId':summary['agentId'],
                             'tool':'file.write','status':'success','result':{'ok':True}})
        with patch.object(self.app,'_start'):
            self.app.action(self.payload(run,target))
        self.assertEqual(summary['status'],'partial');self.assertNotIn('attemptHistory',summary)
        self.assertEqual(target['status'],'pending')


if __name__=='__main__':unittest.main()
