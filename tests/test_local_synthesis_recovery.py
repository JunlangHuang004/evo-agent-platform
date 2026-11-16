"""A genuine lost synthesis window admits local work once; never grants success."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform,clock_ms
from backend.runtime import ProtocolError
from backend.stage_control import allocate_synthesis_recovery,allocate_review_rework,stage_control,stage_action_feedback
from backend.generation_retry import generation_retry_decision
from tests.test_generation_retry import failure,transport
from tests.test_time_only_runtime import Model,Files


class RecoveryPolicyTests(unittest.TestCase):
    def fixture(self):
        task={'id':'task','agentId':'coordinator','revision':0,'status':'running'}
        run={'id':'run','mode':'task','status':'running','budget':{'profile':'time_only','deadlineAt':2000000},'steps':[task],'calls':[]}
        stage_control(run,task,1000)
        task['stageControl'].update(synthesisAt=1100,handoffAt=1200,expectedHandoffBy=1300)
        call={'id':'call-failed','taskId':'task','agentId':'coordinator','revision':0,'tool':'llm.chat','status':'failed',
              'requestStartedAt':1050,'requestFinishedAt':1400,'durationMs':350,'requestStagePhase':'work',
              'requestStageControl':copy.deepcopy(task['stageControl']),
              'error':{'code':'transport','phase':'wait_headers','transport':transport()}}
        run['calls'].append(call)
        return run,task,call
    def test_once_per_revision_90_second_admission_with_unchanged_stage_and_deadline(self):
        run,task,call=self.fixture();original=copy.deepcopy((run['budget'],task['stageControl']))
        record=allocate_synthesis_recovery(run,task,call,1400)
        self.assertEqual(record['expiresAt'],91400)
        self.assertEqual(record['failedCallId'],call['id']);self.assertEqual(record['acceptance'],'unassessed')
        self.assertEqual((run['budget'],task['stageControl']),original)
        self.assertIsNone(allocate_synthesis_recovery(run,task,call,1500))
        self.assertEqual(task['synthesisRecoveries'],[record])
        for now in (1400,91399):
            control=stage_control(run,task,now);self.assertEqual(control['phase'],'local_synthesis_recovery')
            for name in ('file.read','file.write','file.patch','code.test'):
                self.assertIsNone(stage_action_feedback(control,{'type':'tool','name':name}))
            for name in ('web.fetch','web.search','file.delete','git.commit','git.push','code.run'):
                self.assertFalse(stage_action_feedback(control,{'type':'tool','name':name})['actionExecuted'])
            for kind in ('publish_artifact','publish_artifacts','package_delivery','review','final'):
                self.assertIsNone(stage_action_feedback(control,{'type':kind}))
            for kind in ('delegate','ask_human'):
                self.assertFalse(stage_action_feedback(control,{'type':kind})['actionExecuted'])
        self.assertEqual(stage_control(run,task,91400)['phase'],'handoff_final')
        restored=json.loads(json.dumps((run,task)))
        self.assertEqual(stage_control(restored[0],restored[1],91500)['phase'],'handoff_final')
        self.assertIsNone(allocate_synthesis_recovery(restored[0],restored[1],restored[0]['calls'][0],91500))
        self.assertEqual(restored[1]['synthesisRecoveries'][0],record)
    def test_recovery_retains_original_handoff_without_executing_review_or_packaging(self):
        run,task,call=self.fixture();record=allocate_synthesis_recovery(run,task,call,1400)
        before=copy.deepcopy((run,task));control=stage_control(run,task,1500)
        for action in ({'type':'review','taskId':'not-implicitly-authorized','verdict':'pass'},
                       {'type':'package_delivery','artifactIds':['not-real'],'entrypoint':'missing.html','instructions':'Not executed'}):
            self.assertIsNone(stage_action_feedback(control,action))
            self.assertEqual((run,task),before,'admission creates no package, review, artifact, status or permission')
        self.assertEqual(control['handoffAllowedActions'],['publish_artifact','publish_artifacts','package_delivery','review','final'])
        broken=copy.deepcopy(task);broken['stageControl']['runId']='foreign'
        with self.assertRaises(ProtocolError):stage_control(run,broken,1500)
        self.assertEqual(stage_control(run,task,record['finalOnlyAt'])['phase'],'handoff_final')
    def test_strict_failure_binding_and_no_unknown_effects_or_history_guessing(self):
        for mode in ('not_failed','wrong_owner','wrong_task','bool_revision','snapshot_bool','snapshot_changed','missing_start',
                     'started_in_synthesis','ended_before_handoff','inconsistent_duration','worker_alive','accepted','not_eligible',
                     'pending_tool','pending_answer','uncertain','unknown_call','another_call_after','cancelled','legacy_missing_stage','duplicate_id'):
            run,task,call=self.fixture()
            if mode=='not_failed':call['status']='success'
            elif mode=='wrong_owner':call['agentId']='executor'
            elif mode=='wrong_task':call['taskId']='other'
            elif mode=='bool_revision':call['revision']=False
            elif mode=='snapshot_bool':call['requestStageControl']['revision']=False
            elif mode=='snapshot_changed':call['requestStageControl']['synthesisAt']+=1
            elif mode=='missing_start':call.pop('requestStartedAt')
            elif mode=='started_in_synthesis':call['requestStartedAt']=1100;call['durationMs']=300
            elif mode=='ended_before_handoff':call['requestFinishedAt']=1199;call['durationMs']=149
            elif mode=='inconsistent_duration':call['durationMs']=0
            elif mode=='worker_alive':call['error']['transport']['workerTerminated']=False
            elif mode=='accepted':call['error']['transport']['modelResultAccepted']=True
            elif mode=='not_eligible':call['error']['transport']['generationRetryEligible']=False
            elif mode=='pending_tool':task['_pendingTool']={'id':'unknown'}
            elif mode=='pending_answer':task['_approvalId']='unconsumed-question'
            elif mode=='uncertain':task['_uncertainCalls']=['unknown']
            elif mode=='unknown_call':run['calls'].insert(0,{'id':'unknown','taskId':'task','tool':'code.run','status':'unknown'})
            elif mode=='another_call_after':run['calls'].append({'id':'later','taskId':'task','tool':'llm.chat','status':'success'})
            elif mode=='cancelled':run['status']='cancelled'
            elif mode=='legacy_missing_stage':call.pop('requestStageControl')
            elif mode=='duplicate_id':run['calls'].append(copy.deepcopy(call))
            before=copy.deepcopy(task)
            with self.subTest(mode=mode):
                self.assertIsNone(allocate_synthesis_recovery(run,task,call,1400));self.assertEqual(task,before)
    def test_final_only_is_hard_and_no_late_action_or_repeat_failure_renews_window(self):
        run,task,call=self.fixture();now=run['budget']['deadlineAt']-120001
        record=allocate_synthesis_recovery(run,task,call,now)
        self.assertEqual(record['expiresAt'],now+1)
        self.assertEqual(stage_control(run,task,now+1)['phase'],'handoff_final')
        run2,task2,call2=self.fixture();self.assertIsNone(allocate_synthesis_recovery(run2,task2,call2,1880000))
        control=stage_control(run,task,now)
        self.assertEqual(generation_retry_decision(failure(),run,task,now,'local_synthesis_recovery'),'retry')
        self.assertEqual(generation_retry_decision(failure(),run,task,now+1,'local_synthesis_recovery'),'refresh_phase')
        self.assertEqual(generation_retry_decision(failure(),run,task,2000000,'local_synthesis_recovery'),'deny')
        self.assertIsNone(allocate_synthesis_recovery(run,task,call,now+1));self.assertEqual(task['synthesisRecoveries'],[record])

    def test_real_rework_binding_permits_a_distinct_revision_without_renewing_old_ticket(self):
        run,task,call=self.fixture();original_deadline=run['budget']['deadlineAt']
        first=allocate_synthesis_recovery(run,task,call,1400)
        validation={'verdict':'rework','reviewerTaskId':'qa','targetRevision':0,'artifactHashes':{}}
        task.setdefault('validationHistory',[]).append(validation)
        allocate_review_rework(run,task,{'id':'qa'},validation);task['revision']=1
        run['steps'].append({'id':'downstream','status':'pending','dependsOn':['task']})
        stage_control(run,task,100000);stage=task['stageControl']
        revised=copy.deepcopy(call);revised.update(id='call-revision-1',revision=1,requestStageControl=copy.deepcopy(stage),
            requestStartedAt=100001,requestFinishedAt=stage['handoffAt']+1,
            durationMs=stage['handoffAt']+1-100001)
        run['calls'].append(revised)
        second=allocate_synthesis_recovery(run,task,revised,revised['requestFinishedAt'])
        self.assertEqual(second['revision'],1);self.assertEqual(task['synthesisRecoveries'],[first,second])
        self.assertEqual(run['budget']['deadlineAt'],original_deadline)
        self.assertEqual(task['stageControlHistory'][0]['control']['synthesisAt'],1100)

    def test_owner_workspace_unknown_effect_shapes_fail_closed_but_reconciled_observed_state_is_allowed(self):
        # Actual tool-call shape from _tool. Each negative changes one field only.
        original={'id':'actual-write','taskId':'task','agentId':'coordinator','revision':0,'tool':'file.write',
                  'args':{'path':'existing.md','content':'actual'},'status':'failed',
                  'result':{'ok':False,'error':{'code':'network','message':'write may have completed','retryable':False},'outcomeUnknown':False},
                  'durationMs':8,'sandboxId':'coordinator/run'}
        changes=[('status',('status',),'uncertain'),('top',('outcomeUnknown',),True),
                 ('result',('result','outcomeUnknown'),True),('error',('result','error','outcome_unknown'),True),
                 ('transport',('result','error','transport'),{'outcomeUnknown':True}),
                 ('top_error_transport',('error',),{'transport':{'outcome_unknown':True}}),
                 ('result_transport',('result','transport'),{'outcomeUnknown':True}),
                 ('error_code',('result','error','code'),'uncertain')]
        for name,keys,value in changes:
            run,task,call=self.fixture();effect=copy.deepcopy(original);target=effect
            for key in keys[:-1]:target=target[key]
            target[keys[-1]]=value;run['calls'].insert(0,effect)
            with self.subTest(name=name):self.assertIsNone(allocate_synthesis_recovery(run,task,call,1400))
        for flag in ('_pendingTool','_uncertainCalls'):
            run,task,call=self.fixture();run['steps'].append({'id':'other-task','agentId':'coordinator',flag:['unsettled']})
            self.assertIsNone(allocate_synthesis_recovery(run,task,call,1400),flag)
        for status in ('pending','uncertain'):
            run,task,call=self.fixture();effect=copy.deepcopy(original);effect.update(taskId='other-task',status=status)
            run['calls'].insert(0,effect);self.assertIsNone(allocate_synthesis_recovery(run,task,call,1400),status)
        # Human reconciliation retained as such, followed by a real state read;
        # neither that known history nor LLM usageUnknown is an unresolved effect.
        run,task,call=self.fixture();effect=copy.deepcopy(original)
        effect.update(status='reconciled',result={'ok':False,'reconciled':True,'message':'Human confirmed executed; independently read current state.'})
        run['calls'][:0]=[effect,{'id':'actual-read','taskId':'task','agentId':'coordinator','revision':0,'tool':'file.read',
            'args':{'path':'existing.md'},'status':'success','result':{'ok':True,'path':'existing.md','content':'actual',
            'sha256':__import__('hashlib').sha256(b'actual').hexdigest()}}]
        call['usageUnknown']=True
        run['calls'].insert(0,{'id':'other-private-owner','taskId':'executor-task','agentId':'executor','tool':'code.run','status':'uncertain','outcomeUnknown':True})
        record=allocate_synthesis_recovery(run,task,call,1400);self.assertIsNotNone(record)
        run['steps'].append({'id':'same-owner-later','agentId':'coordinator','_uncertainCalls':['new-unknown']})
        self.assertEqual(stage_control(run,task,1500)['phase'],'handoff_final')
        run['steps'][-1]['_uncertainCalls']=[]
        self.assertEqual(stage_control(run,task,1501)['phase'],'local_synthesis_recovery')
        self.assertEqual(task['synthesisRecoveries'],[record],'temporary safety refusal never renews the saved window')


class RecoveryRuntimeTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.tmp.cleanup()
    def test_global_final_only_still_prevents_review_and_package_actions_from_executing(self):
        for kind in ('review','package_delivery'):
            with self.subTest(kind=kind):
                config=Config(Path(self.tmp.name)/kind,{'LLM_API_KEY':'offline-global-final','MAX_RETRIES':'0'});config.apply_time_only_profile()
                def action(n,context,messages):
                    if n==0:
                        run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
                        deadline=clock_ms()+60000;run['budget']['deadlineAt']=deadline
                        task['stageControl']['runDeadlineAt']=deadline
                        for key in ('synthesisAt','handoffAt','expectedHandoffBy'):
                            task['stageControl'][key]=min(task['stageControl'][key],deadline)
                        return {'type':'accept'}
                    self.assertEqual(context['progressReceipt']['executionPhase'],'final_only')
                    return {'type':kind,'taskId':'not-real','verdict':'pass','artifactIds':['not-real'],
                            'entrypoint':'not-real.html','instructions':'Must never execute'}
                model=Model(action);app=Platform(config,provider=model,tools=Files(config));model.app=app;self.apps.append(app)
                app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Only final is permitted in the original global closing window.'})
                self.assertTrue(app.wait_for_idle(3));run=app.state['sessions']['group']['run']
                task=next(t for t in run['steps'] if not t.get('_planning'))
                self.assertEqual(task['finalizationFailure']['action'],kind)
                self.assertEqual(task['error']['code'],'budget');self.assertEqual(run.get('deliveries',[]),[])
                self.assertFalse(any(e['type'] in ('review_pass','delivery_packaged') for e in run['events']))
                self.assertEqual(len(model.calls),3)
    def execute(self,mode='success'):
        now=[1000000];observed={};config=Config(Path(self.tmp.name)/mode,{'LLM_API_KEY':'offline-local-recovery','MAX_RETRIES':'0'})
        config.apply_time_only_profile()
        def short_stage(run,task,stamp):
            control=stage_control(run,task,stamp)
            if control and not task.get('_testPinned'):
                task['_testPinned']=True;task['stageControl'].update(synthesisAt=stamp+10,handoffAt=stamp+20,expectedHandoffBy=stamp+30)
                control=stage_control(run,task,stamp)
            return control
        def action(n,c,m):
            if n==0:
                observed['originalStage']=copy.deepcopy(c['stageControl']);now[0]+=30;raise failure()
            if n==1:
                self.assertEqual(c['stageControl']['phase'],'local_synthesis_recovery')
                observed['record']=copy.deepcopy(c['stageControl']['localSynthesisRecovery'])
                if mode=='late_action':now[0]+=90001
                if mode=='second_failure':now[0]+=90001;raise failure()
                return {'type':'tool','name':'file.write','args':{'path':'evidence.md','content':'Actual locally synthesized retained evidence; gaps remain.'}}
            if mode in ('late_action','second_failure'):
                self.assertEqual(c['stageControl']['phase'],'handoff_final')
                return {'type':'final','status':'partial','text':('A real local file was created but not published; the legacy recovery marker expired without changing the original deadline.' if mode=='late_action' else 'No local file was created after the failed generation; missing output remains.')}
            if n==2:return {'type':'publish_artifact','path':'evidence.md'}
            return {'type':'final','text':'Published actual file, not a claim of independent acceptance.'}
        model=Model(action)
        class AdmissionFiles(Files):
            def execute(self,agent_id,run_id,name,args,approved=False,**kwargs):
                result=super().execute(agent_id,run_id,name,args,approved,**kwargs)
                if mode=='inflight_crosses' and name=='file.write':now[0]+=90001
                return result
        app=Platform(config,provider=model,tools=AdmissionFiles(config));model.app=app;self.apps.append(app)
        with patch('backend.engine.clock_ms',side_effect=lambda:now[0]),patch('backend.engine.stage_control',side_effect=short_stage):
            app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Use retained evidence and honestly report a real artifact.'})
            self.assertTrue(app.wait_for_idle(4))
        return app,app.state['sessions']['group']['run'],observed
    def test_failed_work_request_loses_whole_synthesis_but_next_actual_write_and_publish_succeed(self):
        app,run,observed=self.execute();task=next(t for t in run['steps'] if not t.get('_planning'))
        self.assertEqual(run['status'],'done');self.assertEqual(len(run['artifacts']),1)
        self.assertEqual((app.tools.workspace('coordinator',run['id'])/'evidence.md').read_text(),'Actual locally synthesized retained evidence; gaps remain.')
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),1)
        self.assertEqual(len([e for e in run['events'] if e['type']=='local_synthesis_recovery']),1)
        failed=next(c for c in run['calls'] if c.get('usageUnknown'))
        self.assertEqual(failed['requestStagePhase'],'work');self.assertEqual(failed['requestFinishedAt']-failed['requestStartedAt'],30)
        self.assertEqual(run['unknownModelUsages'],1);self.assertEqual(run['modelCalls'],5)
        self.assertEqual(task['stageControl']['synthesisAt'],observed['originalStage']['synthesisAt'])
        self.assertEqual(run['budget']['deadlineAt'],observed['originalStage']['runDeadlineAt'])
        self.assertFalse(task.get('validation',{}).get('verified'))
        self.assertEqual(app.repo.get_run(run['id'])['steps'][-1]['synthesisRecoveries'],[observed['record']])
    def test_expired_legacy_marker_keeps_ordinary_local_completion_without_extra_time(self):
        app,run,observed=self.execute('late_action');self.assertEqual(run['status'],'partial')
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write' and c['status']=='success']),1)
        self.assertFalse(run['artifacts']);self.assertFalse(run.get('deliveries'))
        self.assertFalse(any(e['type']=='stage_action_blocked' for e in run['events']))
        self.assertTrue((app.tools.workspace('coordinator',run['id'])/'evidence.md').is_file())
        self.assertEqual(run['budget']['deadlineAt'],observed['originalStage']['runDeadlineAt'])
    def test_second_failure_does_not_grant_new_window_or_replay_tools(self):
        app,run,observed=self.execute('second_failure');task=run['steps'][-1]
        self.assertEqual(run['status'],'partial');self.assertEqual(run['unknownModelUsages'],2)
        self.assertEqual(task['synthesisRecoveries'],[observed['record']]);self.assertFalse(run['artifacts'])
        self.assertEqual(len([e for e in run['events'] if e['type']=='local_synthesis_recovery']),1)
    def test_already_admitted_effect_is_not_cancelled_or_replayed_at_window_expiry(self):
        app,run,observed=self.execute('inflight_crosses')
        self.assertEqual(run['status'],'done');self.assertEqual(len(run['artifacts']),1)
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),1)
        self.assertEqual(run['steps'][-1]['stageControl']['runDeadlineAt'],run['budget']['deadlineAt'])


if __name__=='__main__':unittest.main()
