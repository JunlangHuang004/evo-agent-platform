"""Do not permanently skip ordinary consumers while independent repair is viable.

Scripted models and actual local artifact snapshots only; no network/model API.
Partial data is never granted to an ordinary consumer as an executable input.
"""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from backend.config import Config
from backend.engine import Platform,clock_ms
from tests.test_engine import ScriptedProvider,task,final
from tests.test_execution_closure import LocalFiles,tool
from tests.test_time_only_runtime import wait_until


class OrdinaryConsumerReviewWaitTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.apps=[];self.releases=[]
    def tearDown(self):
        for event in self.releases:event.set()
        for app in self.apps:app.shutdown();app.wait_for_idle(4);app.repo.close()
        self.tmp.cleanup()
    def create(self,mode='repair',extra_failed=False,block=None,time_only=False):
        config=Config(Path(self.tmp.name)/str(len(self.apps)),{'LLM_API_KEY':'offline-review-wait','MAX_AGENT_TURNS':'3',
            'MAX_RETRIES':'1','MAX_MODEL_CALLS':'40','MAX_RUN_TOKENS':'400000','PARALLEL_TASKS':'2'})
        if time_only:config.apply_time_only_profile()
        plan=[{**task('Build','executor'),'outputContract':{'kind':'artifact','extensions':['.md']}},
              task('QA','reviewer',['Build'],['review'])]
        if extra_failed:plan.append(task('Other failed input','coordinator'))
        plan.append(task('Package','coordinator',['Build','QA']+(['Other failed input'] if extra_failed else [])))
        def build(i,c,m):
            revised=bool(c['task'].get('feedback'));path='result-v'+str(int(revised)+1)+'.md'
            if time_only and i==2 and not revised:
                self.assertEqual(c['progressReceipt']['stageControl']['phase'],'work')
                run=model.app.state['sessions']['group']['run'];t=next(t for t in run['steps'] if t['id']==c['task']['id']);now=clock_ms()
                t['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-700,handoffAt=now-500,expectedHandoffBy=now-100)
            if time_only and i==3 and not revised:
                receipt=json.loads(next(x['content'] for x in reversed(m)
                    if x['content'].startswith('动作的真实结果：')).split('：',1)[1])
                self.assertEqual(receipt['error']['code'],'completion_scope_clarification')
                self.assertEqual(receipt['binding']['taskId'],c['task']['id'])
                self.assertEqual(receipt['binding']['revision'],0)
                return final('Honest incomplete initial snapshot; keep partial after scope clarification.',status='partial')
            return [tool('file.write',path=path,content='Version '+str(int(revised)+1)),
                    {'type':'publish_artifact','path':path},
                    final('Actual revised output.' if revised else 'Honest incomplete initial snapshot.',status='done' if revised else 'partial')][i%3]
        def qa(i,c,m):
            if i==0 and block is not None:block[0].set();block[1].wait(3)
            if mode in ('partial','failed'):return final('Review cannot recover the producer.',status=mode)
            verdict=('rework' if i==0 else 'pass') if mode=='repair' else mode
            if i==0 or mode=='repair' and i==1:
                return {'type':'review','taskId':c['dependencyResults'][0]['id'],'verdict':verdict,
                        'feedback':'Revise the actual text.' if verdict=='rework' else 'Editorial text assessment only, not code verification.'}
            return final('Current revision independently assessed; no fabricated executable proof.')
        def package(i,c,m):
            dep=next(d for d in c['dependencyResults'] if d['title']=='Build')
            self.assertEqual(dep['status'],'done');self.assertEqual(dep['revision'],1)
            self.assertEqual(len(c['authorizedArtifacts']),1)
            if i==0:return tool('file.read',path=c['authorizedArtifacts'][0]['path'])
            receipt=json.loads(next(x['content'] for x in reversed(m) if x['content'].startswith('动作的真实结果：')).split('：',1)[1])
            self.assertEqual(receipt['content'],'Version 2')
            return final('Ordinary consumer used actual successful current revision only.')
        model=ScriptedProvider(plan,{'Build':build,'QA':qa,'Package':package,'Other failed input':[final('Actual failed unrelated input.',status='failed')]})
        if time_only:
            original=model.complete
            def deadline_complete(messages,max_tokens=None,deadline=None):return original(messages)
            model.complete=deadline_complete
        app=Platform(config,provider=model,tools=LocalFiles(config));model.app=app;self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['executor','reviewer','coordinator'],'text':'Deliver only after explicit review has resolved the actual producer revision.'})
        return app,model
    def test_actual_partial_rework_chain_then_ordinary_consumer_runs_once_on_new_done_revision(self):
        app,model=self.create();self.assertTrue(app.wait_for_idle(5));run=app.snapshot()['sessions']['group']['run'];by={t['title']:t for t in run['steps']}
        self.assertEqual([by[n]['status'] for n in ('Build','QA','Package')],['done','done','done'])
        self.assertEqual(model.counts['Build'],6);self.assertEqual(model.counts['QA'],3);self.assertEqual(model.counts['Package'],2)
        self.assertEqual(by['Build']['revision'],1)
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),2)
        self.assertEqual(by['Package']['dependencySnapshots'][by['Build']['id']]['targetRevision'],1)
        self.assertEqual(len([e for e in run['events'] if e['type']=='dependency_review_wait']),1)
        self.assertFalse(any(e['type']=='dependency_blocked' and e['payload']['taskId']==by['Package']['id'] for e in run['events']))
        self.assertFalse(by['Build']['validation']['verified'],'editorial pass is not executable semantic proof')
        self.assertEqual(app.repo.get_run(run['id'])['steps'][-1]['status'],'done')
    def test_review_terminal_without_successful_repair_skips_ordinary_consumer_without_grants(self):
        for mode in ('pass','partial','failed','reject'):
            with self.subTest(mode=mode):
                app,model=self.create(mode=mode);self.assertTrue(app.wait_for_idle(5));run=app.snapshot()['sessions']['group']['run'];by={t['title']:t for t in run['steps']}
                self.assertEqual(by['Package']['status'],'skipped');self.assertEqual(model.counts.get('Package',0),0)
                self.assertNotEqual(by['Build']['status'],'done');self.assertEqual(by['Build']['revision'],0)
                self.assertFalse(any(c['taskId']==by['Package']['id'] for c in run['calls']))
    def test_other_irrecoverable_dependency_is_not_masked_by_one_recoverable_partial(self):
        app,model=self.create(extra_failed=True);self.assertTrue(app.wait_for_idle(5));run=app.snapshot()['sessions']['group']['run'];by={t['title']:t for t in run['steps']}
        self.assertEqual(by['Build']['status'],'done');self.assertEqual(by['QA']['status'],'done')
        self.assertEqual(by['Other failed input']['status'],'failed');self.assertEqual(by['Package']['status'],'skipped')
        self.assertEqual(model.counts.get('Package',0),0)
    def test_cancel_or_original_wall_expiry_during_review_wait_never_executes_consumer(self):
        for mode in ('cancel','deadline'):
            with self.subTest(mode=mode):
                entered=threading.Event();release=threading.Event();self.releases.append(release)
                app,model=self.create(block=(entered,release),time_only=True)
                self.assertTrue(entered.wait(3));self.assertTrue(wait_until(lambda:any(e['type']=='dependency_review_wait' for e in app.snapshot()['sessions']['group']['run']['events'])))
                run=app.snapshot()['sessions']['group']['run'];deadline=run['budget']['deadlineAt']
                producer=next(t for t in run['steps'] if t['title']=='Build')
                self.assertEqual(producer['closureReason'],'stage_handoff_final')
                self.assertEqual(producer['error']['causeCode'],'stage_handoff_final')
                self.assertEqual(producer['partialOutput']['executionStatus'],'partial')
                if mode=='cancel':app.action({'action':'cancel','runId':run['id']})
                else:
                    with app.changed:
                        live=app.state['sessions']['group']['run'];live['budget']['deadlineAt']=clock_ms()-1
                        app._persist();app.changed.notify_all()
                    self.assertTrue(wait_until(lambda:app.snapshot()['sessions']['group']['run'].get('timeExpired')))
                release.set();self.assertTrue(app.wait_for_idle(4));run=app.snapshot()['sessions']['group']['run']
                self.assertEqual(model.counts.get('Package',0),0);self.assertFalse(any(c['taskId']==next(t['id'] for t in run['steps'] if t['title']=='Package') for c in run['calls']))
                if mode=='cancel':self.assertEqual(run['status'],'cancelled');self.assertEqual(run['budget']['deadlineAt'],deadline)
                else:self.assertTrue(run['timeExpired']);self.assertLessEqual(run['budget']['deadlineAt'],clock_ms())

    def test_repair_viability_rejects_cycle_bad_other_dependency_exhausted_revision_and_unknown(self):
        from tests.test_partial_review import PartialReviewTests
        fixture=PartialReviewTests();fixture.setUp()
        config=Config(Path(self.tmp.name)/'pure',{'MAX_RETRIES':'1'});app=Platform(config,tools=LocalFiles(config));self.apps.append(app)
        app.agents=fixture.agents
        producer,reviewer,run=fixture.producer,fixture.reviewer,fixture.run
        consumer={'id':'release','agentId':'release','status':'pending','dependsOn':['build','check']}
        run.update(status='running',participants=list(fixture.agents));run['steps'].append(consumer)
        self.assertEqual(app._reviewers_that_may_repair(run,producer,consumer),['check'])
        for change in ('cycle','other_failed','limit','uncertain','no_review','same_owner','hypothetical_candidate','unowned'):
            candidate=copy.deepcopy(run);p=candidate['steps'][0];q=candidate['steps'][1];c=candidate['steps'][2]
            if change=='cycle':q['dependsOn'].append(c['id'])
            elif change=='other_failed':candidate['steps'].append({'id':'bad','status':'failed','agentId':'dev'});q['dependsOn'].append('bad')
            elif change=='limit':p['revision']=1
            elif change=='uncertain':p['_uncertainCalls']=['unknown']
            elif change=='no_review':q['requiredCapabilities']=[]
            elif change=='unowned':q['agentId']=None;q['candidateAgentIds']=['qa']
            else:q['agentId']='dev';q['candidateAgentIds']=['qa'] if change=='hypothetical_candidate' else ['dev']
            self.assertEqual(app._reviewers_that_may_repair(candidate,p,c),[],change)


if __name__=='__main__':unittest.main()
