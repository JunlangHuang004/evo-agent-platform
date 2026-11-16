"""Original wall time, durable receipts and no task/tool replay. No API calls."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform,clock_ms
from backend.generation_cooldown import new_cooldown,cooldown_binding
from tests.test_generation_retry import failure
from tests.test_time_only_runtime import Model,Files,wait_until


class CooldownPolicyTests(unittest.TestCase):
    def fixture(self):
        data=json.loads((Path(__file__).parent/'fixtures/generation_failure_chain_r6.json').read_text())
        task={'id':data['taskId'],'agentId':data['agentId'],'revision':data['revision'],
              'status':'running','stageControl':data['stageControl']}
        run={'id':data['runId'],'mode':'task','status':'running','budget':data['budget'],
             'calls':data['calls'],'steps':[task]}
        return run,task,run['calls'][-1]['requestFinishedAt']+1

    def test_actual_three_failure_chain_cools_without_extending_original_wall_or_known_usage(self):
        run,task,now=self.fixture();original=copy.deepcopy(run)
        row=new_cooldown(run,task,run['calls'][-1],now,'cooldown-actual','handoff_final')
        self.assertEqual(row['delayMs'],5000);self.assertEqual(row['runDeadlineAt'],run['budget']['deadlineAt'])
        self.assertLess(row['nextEligibleAt'],run['budget']['deadlineAt'])
        self.assertTrue(row['usageUnknown']);self.assertEqual(run,original)
        task['generationCooldown']=row
        saved=json.loads(json.dumps(run));restored=saved['steps'][0]
        bound=cooldown_binding(saved,restored,now+2000)
        self.assertIsNotNone(bound);self.assertEqual(bound[0]['nextEligibleAt']-(now+2000),3000)
        self.assertEqual([c['error']['transport']['requestBytes'] for c in run['calls']],[84313]*3)
        self.assertTrue(all(c['error']['transport']['usageUnknown'] for c in run['calls']))

    def test_unknown_same_owner_effects_and_changed_binding_fail_closed(self):
        for field in ('_pendingTool','_uncertainCalls'):
            run,task,now=self.fixture();run['steps'].append({'id':'other','agentId':task['agentId'],field:['unresolved']})
            self.assertIsNone(new_cooldown(run,task,run['calls'][-1],now,'no','handoff_final'))
        for status,extra in [('uncertain',{}),('success',{'outcomeUnknown':True}),
                             ('failed',{'result':{'error':{'code':'outcome_unknown'}}}),
                             ('failed',{'error':{'transport':{'outcome_unknown':'unknown'}}}),
                             ('failed',{'lateResult':{'error':{'code':'uncertain'}}})]:
            run,task,now=self.fixture();run['calls'].insert(0,{'id':'tool','taskId':'other','agentId':task['agentId'],
                'tool':'file.write','status':status,**extra})
            self.assertIsNone(new_cooldown(run,task,run['calls'][-1],now,'no','handoff_final'))
        for mutate in (lambda r,t:setattr_dummy(t,'revision',True),
                       lambda r,t:setattr_dummy(r['calls'][-1],'revision',True),
                       lambda r,t:setattr_dummy(r,'status','cancelled'),
                       lambda r,t:setattr_dummy(r['budget'],'profile','bounded'),
                       lambda r,t:setattr_dummy(r['calls'][-1]['error']['transport'],'workerReaped',False),
                       lambda r,t:setattr_dummy(r['calls'][-1]['error']['transport'],'modelResultAccepted',True)):
            run,task,now=self.fixture();mutate(run,task)
            self.assertIsNone(new_cooldown(run,task,run['calls'][-1],now,'no','handoff_final'))

    def test_restart_cannot_refill_or_rebind_delay_and_new_response_supersedes_failure(self):
        run,task,now=self.fixture();task['generationCooldown']=new_cooldown(run,task,run['calls'][-1],now,'saved','handoff_final')
        row=copy.deepcopy(task['generationCooldown'])
        self.assertIsNotNone(cooldown_binding(run,task,row['nextEligibleAt']))
        self.assertEqual(task['generationCooldown'],row)
        task['generationCooldown']['nextEligibleAt']+=1
        self.assertIsNone(cooldown_binding(run,task,now+1))
        task['generationCooldown']=copy.deepcopy(row)
        task['generationCooldown'].update(cycle=2,delayMs=10000,nextEligibleAt=row['createdAt']+10000)
        self.assertIsNone(cooldown_binding(run,task,now+1))
        task['generationCooldown']=copy.deepcopy(row)
        run['calls'].append({**run['calls'][-1],'id':'later','status':'success'})
        self.assertIsNone(cooldown_binding(run,task,now+1))
        run['calls'].pop();task['revision']+=1
        self.assertIsNone(cooldown_binding(run,task,now+1))

    def test_deadline_and_stage_pins_are_strict_but_phase_crossing_can_refresh(self):
        run,task,now=self.fixture();deadline=run['budget']['deadlineAt']
        self.assertIsNone(new_cooldown(run,task,run['calls'][-1],deadline,'late','handoff_final'))
        row=new_cooldown(run,task,run['calls'][-1],now,'valid','work');self.assertIsNotNone(row)
        task['generationCooldown']=row
        self.assertIsNotNone(cooldown_binding(run,task,now))
        task['stageControl']['runDeadlineAt']-=1
        self.assertIsNone(cooldown_binding(run,task,now))
        for invalid_now in (True,False,-1,1.5,'1000'):
            run,task,now=self.fixture()
            self.assertIsNone(new_cooldown(run,task,run['calls'][-1],invalid_now,'invalid','handoff_final'))
        for created,sync_formula in ((deadline+1,False),(deadline+1,True),(-1,False),(-1,True)):
            run,task,now=self.fixture();row=new_cooldown(run,task,run['calls'][-1],now,'saved','handoff_final')
            row['createdAt']=created
            if sync_formula:row['nextEligibleAt']=min(created+row['delayMs'],row['runDeadlineAt'])
            task['generationCooldown']=row
            self.assertIsNone(cooldown_binding(run,task,now))


def setattr_dummy(value,key,new):
    value[key]=new


class CooldownRuntimeTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(5);app.repo.close()
        self.tmp.cleanup()
    def create(self,action,settings=None,path=None,bounded=False,model=None):
        values={'LLM_API_KEY':'offline','MAX_RETRIES':'0'};values.update(settings or {})
        config=Config(Path(path or self.tmp.name)/str(len(self.apps)),values)
        if not bounded:config.apply_time_only_profile()
        model=model or Model(action);app=Platform(config,provider=model,tools=Files(config));model.app=app
        self.apps.append(app);return app,model
    def send(self,app):app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Perform local work and report actual evidence.'})
    def test_three_failures_then_fresh_context_success_never_replays_completed_tool(self):
        def action(n,c,m):
            if n==0:return {'type':'tool','name':'file.write','args':{'path':'before.md','content':'one actual version'}}
            if n in (1,2,3):raise failure()
            self.assertTrue(any('generationCooldown' in x['content'] for x in m))
            self.assertGreater(c['progressReceipt']['remainingMs'],0)
            if n==4:return {'type':'tool','name':'file.write','args':{'path':'after.md','content':'second actual version'}}
            return {'type':'final','text':'Actual work completed once; no semantic product claim.'}
        app,model=self.create(action)
        with patch('backend.generation_cooldown.cooldown_delay',return_value=250):
            self.send(app);self.assertTrue(app.wait_for_idle(5))
        run=app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
        self.assertEqual(run['status'],'done');self.assertEqual(run['unknownModelUsages'],3)
        self.assertEqual([c['args']['path'] for c in run['calls'] if c['tool']=='file.write'],['before.md','after.md'])
        self.assertNotIn('generationCooldown',task);self.assertEqual(len(task['generationCooldownHistory']),1)
        self.assertEqual(run['budget']['deadlineAt'],task['generationCooldownHistory'][0]['runDeadlineAt'])
        self.assertIsNone(run['budget']['modelCalls']);self.assertIsNone(run['budget']['tokens'])
        self.assertEqual(sum(x.get('kind')=='generation_phase_refresh' for x in run['transcript']),1)

    def test_cancel_interrupts_cooldown_without_new_request_or_tool(self):
        app,model=self.create(lambda n,c,m:(_ for _ in ()).throw(failure()))
        self.send(app);self.assertTrue(wait_until(lambda:any(t.get('generationCooldown') for t in app.state['sessions']['group']['run']['steps']),3))
        run=app.snapshot()['sessions']['group']['run'];calls=run['modelCalls'];app.action({'action':'cancel','runId':run['id']})
        self.assertTrue(app.wait_for_idle(3));self.assertEqual(len(model.calls),calls)
        self.assertEqual(app.state['sessions']['group']['run']['status'],'cancelled')
        self.assertFalse(any(c['tool']!='llm.chat' for c in run['calls']))

    def test_deadline_during_cooldown_stops_with_unknown_usage_not_fake_success(self):
        app,model=self.create(lambda n,c,m:(_ for _ in ()).throw(failure()))
        self.send(app);self.assertTrue(wait_until(lambda:any(t.get('generationCooldown') for t in app.state['sessions']['group']['run']['steps']),3))
        with app.changed:
            run=app.state['sessions']['group']['run'];calls=run['modelCalls'];run['budget']['deadlineAt']=clock_ms()-1
            app.changed.notify_all()
        self.assertTrue(app.wait_for_idle(3));self.assertEqual(len(model.calls),calls)
        self.assertTrue(run.get('timeExpired'));self.assertEqual(run['unknownModelUsages'],3)
        self.assertNotEqual(run['status'],'done')

    def test_global_final_only_still_gets_honest_final_after_cooldown_no_tools(self):
        state={}
        def action(n,c,m):
            run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
            if n==0:
                now=clock_ms();deadline=now+60000;run['budget']['deadlineAt']=deadline
                task['stageControl'].update(allocatedAt=now-1000,synthesisAt=now+150,handoffAt=now+20000,
                    expectedHandoffBy=now+45000,runDeadlineAt=deadline);state['deadline']=deadline
                return {'type':'accept'}
            self.assertEqual(c['progressReceipt']['executionPhase'],'final_only')
            if n in (1,2,3):raise failure()
            return {'type':'final','status':'partial','text':'No product; actual failed-generation evidence and remaining gaps.'}
        app,model=self.create(action)
        with patch('backend.generation_cooldown.cooldown_delay',return_value=250):
            self.send(app);self.assertTrue(app.wait_for_idle(5))
        run=app.state['sessions']['group']['run'];self.assertEqual(run['status'],'partial')
        self.assertEqual(run['budget']['deadlineAt'],state['deadline'])
        self.assertEqual(run['unknownModelUsages'],3)
        self.assertFalse(any(c['tool']!='llm.chat' for c in run['calls']))
        self.assertIn('No product',next(t for t in run['steps'] if not t.get('_planning'))['result'])

    def test_actual_shutdown_resume_keeps_remaining_cooldown_and_delivers_once(self):
        app,model=self.create(lambda n,c,m:(_ for _ in ()).throw(failure()))
        self.send(app);self.assertTrue(wait_until(lambda:any(t.get('generationCooldown') for t in app.state['sessions']['group']['run']['steps']),3))
        run=app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
        deadline=run['budget']['deadlineAt'];original=copy.deepcopy(task['generationCooldown']);config=app.config
        app.shutdown();self.assertTrue(app.wait_for_idle(3));app.repo.close();self.apps.remove(app)
        def resumed(n,c,m):
            self.assertTrue(any(original['id'] in msg['content'] for msg in m))
            return {'type':'final','text':'Actual response after explicit recovery.'}
        resumed_model=Model(resumed);restored=Platform(config,provider=resumed_model,tools=Files(config));resumed_model.app=restored;self.apps.append(restored)
        before=clock_ms();remaining=max(0,original['nextEligibleAt']-before)
        restored.action({'action':'recover','runId':run['id']});self.assertTrue(restored.wait_for_idle(6))
        self.assertGreaterEqual(clock_ms()-before,remaining-30)
        final=restored.state['sessions']['group']['run'];self.assertEqual(final['status'],'done')
        current=next(t for t in final['steps'] if t['id']==task['id'])
        self.assertEqual(final['budget']['deadlineAt'],deadline)
        self.assertEqual(current['generationCooldownHistory'][0]['id'],original['id'])
        self.assertEqual(current['generationCooldownHistory'][0]['createdAt'],original['createdAt'])
        self.assertEqual(sum(t.get('kind')=='generation_phase_refresh' for t in final['transcript']),1)
        self.assertNotIn('generationCooldown',current)

    def test_work_cooldown_shutdown_then_global_final_only_recovery_still_generates(self):
        app,model=self.create(lambda n,c,m:(_ for _ in ()).throw(failure()))
        self.send(app);self.assertTrue(wait_until(lambda:any(t.get('generationCooldown') for t in app.state['sessions']['group']['run']['steps']),3))
        run=app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
        self.assertFalse(task['generationCooldown']['closingGeneration'])
        original=copy.deepcopy(task['generationCooldown']);deadline=run['budget']['deadlineAt'];config=app.config
        app.shutdown();self.assertTrue(app.wait_for_idle(3));app.repo.close();self.apps.remove(app)
        def resumed(n,c,m):
            self.assertEqual(c['progressReceipt']['executionPhase'],'final_only')
            self.assertTrue(any(original['id'] in msg['content'] for msg in m))
            return {'type':'final','status':'partial','text':'Missing product; truthful closing after actual connection failures.'}
        resumed_model=Model(resumed);restored=Platform(config,provider=resumed_model,tools=Files(config));resumed_model.app=restored;self.apps.append(restored)
        with patch('backend.engine.clock_ms',return_value=deadline-60000):
            restored.action({'action':'recover','runId':run['id']});self.assertTrue(restored.wait_for_idle(4))
        current=restored.state['sessions']['group']['run'];self.assertEqual(current['status'],'partial')
        self.assertEqual(current['budget']['deadlineAt'],deadline);self.assertEqual(len(resumed_model.calls),1)
        task=next(t for t in current['steps'] if t['id']==task['id'])
        self.assertIn('truthful closing',task['result']);self.assertNotIn('generationCooldown',task)
        self.assertEqual(task['generationCooldownHistory'][0]['nextEligibleAt'],original['nextEligibleAt'])

    def test_cooldown_crossing_synthesis_rebuilds_phase_instead_of_using_old_work_prompt(self):
        state={}
        def action(n,c,m):
            if n<3:
                if n==2:
                    run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
                    now=clock_ms();task['stageControl'].update(allocatedAt=now-1000,synthesisAt=now+50,handoffAt=now+5000,expectedHandoffBy=now+10000)
                    state['deadline']=run['budget']['deadlineAt']
                raise failure()
            self.assertEqual(c['stageControl']['phase'],'synthesis')
            return {'type':'final','text':'Actual synthesis from existing context, with gaps.'}
        app,model=self.create(action)
        with patch('backend.generation_cooldown.cooldown_delay',return_value=250):
            self.send(app);self.assertTrue(app.wait_for_idle(5))
        run=app.state['sessions']['group']['run'];self.assertEqual(run['status'],'done')
        self.assertEqual(run['budget']['deadlineAt'],state['deadline'])
        self.assertEqual(run['unknownModelUsages'],3)

    def test_planning_generation_cooldown_preserves_goal_and_does_not_spend_format_repairs(self):
        class PlanningFailures(Model):
            failures=0
            def complete(self,messages,max_tokens=None,deadline=None):
                if messages[0]['content'].startswith('Plan an executable task DAG') and self.failures<3:
                    self.calls.append({'messages':copy.deepcopy(messages),'deadline':deadline});self.failures+=1
                    raise failure()
                return super().complete(messages,max_tokens=max_tokens,deadline=deadline)
        model=PlanningFailures(lambda n,c,m:{'type':'final','text':'Actual result after planning became available.'})
        app,model=self.create(None,model=model)
        with patch('backend.generation_cooldown.cooldown_delay',return_value=250):
            self.send(app);self.assertTrue(app.wait_for_idle(5))
        run=app.state['sessions']['group']['run'];self.assertEqual(run['status'],'done')
        planning=next(t for t in run['steps'] if t.get('_planning'))
        self.assertEqual(planning['generationCooldownHistory'][0]['scope'],'model_generation_only')
        self.assertEqual(run['unknownModelUsages'],3);self.assertEqual(model.failures,3)
        successful_planning=next(call for call in model.calls if any('generationCooldown' in row['content'] for row in call['messages']))
        bound=json.loads(successful_planning['messages'][1]['content'])
        self.assertEqual(bound['runGoal'],'Perform local work and report actual evidence.')
        self.assertEqual(bound['task']['id'],planning['id'])

    def test_bounded_profile_and_explicit_zero_keep_existing_finite_policy(self):
        for bounded,settings in ((True,{}),(False,{'MODEL_GENERATION_RETRIES':'0'})):
            app,model=self.create(lambda n,c,m:(_ for _ in ()).throw(failure()),settings,bounded=bounded)
            self.send(app);self.assertTrue(app.wait_for_idle(3));run=app.state['sessions']['group']['run']
            self.assertEqual(run['modelCalls'],2);self.assertFalse(any(t.get('generationCooldown') for t in run['steps']))


if __name__=='__main__':unittest.main()
