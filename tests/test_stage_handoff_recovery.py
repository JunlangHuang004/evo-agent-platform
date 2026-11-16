"""Soft stage targets allow real handoff, never new work or fabricated success."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform,clock_ms
from backend.stage_control import stage_control,stage_action_feedback
from backend.generation_retry import generation_retry_decision
from tests.test_generation_retry import failure
from tests.test_time_only_runtime import Model,Files


ACTUAL_ROUND3_N1=json.loads(r'''{"exportSha256":"1955cd8a84253b879c62ba3c18c9ce681e7087cd68822dc08988e91e12b4b66c","failureEvent":{"actor":"reporter","agentId":"reporter","id":"evt-30c42588cb7a4063","message":"模型 HTTP 传输协议失败；尚未获得可验证的完整响应。","payload":{"error":{"code":"transport","message":"模型 HTTP 传输协议失败；尚未获得可验证的完整响应。"},"taskId":"task-f7e173ec16094094"},"time":"2026-11-15T19:11:09Z","type":"task_failed"},"observedFailureAtMs":1794769869000,"run":{"id":"run-8209a2d8b2cb4aa2","mode":"task","budget":{"agentTurns":null,"deadlineAt":1794771181884,"depth":3,"modelCalls":null,"profile":"time_only","tasks":12,"timeBasis":"wall","timeMs":1800000,"tokens":null}},"task":{"id":"task-f7e173ec16094094","agentId":"reporter","revision":0,"stageControl":{"agentId":"reporter","allocatedAt":1794769512165,"boundarySemantics":"Next action only; in-flight effects are not cancelled or replayed.","criticalStages":5,"expectedHandoffBy":1794769846108,"handoffAt":1794769812714,"instruction":"Before synthesisAt collect necessary evidence and begin notes. In synthesis use actual retained sources to write/patch/test/publish the deliverable; no new web acquisition or delegation. At handoffAt return a real final with current output IDs, evidence and gaps. Unmet contracts stay partial/failed; no additional time, calls/turn caps, permissions or automatic acceptance.","policy":"fixed_wall_checkpoints_within_original_deadline","revision":0,"runDeadlineAt":1794771181884,"runId":"run-8209a2d8b2cb4aa2","schemaVersion":1,"synthesisAt":1794769729227,"taskId":"task-f7e173ec16094094"}},"failedCalls":[{"id":"call-2504f45768bf49f4","status":"failed","durationMs":125687,"error":{"code":"transport","phase":"wait_headers","retryable":false,"transport":{"bodyComplete":false,"configuredWriteChunkBytes":512,"configuredWriteDelayMs":30,"deadlineBoundary":"provider_process","decodedBytes":0,"elapsedMs":125604.945,"exceptionType":"RemoteDisconnected","expectedBytes":null,"generationRetryEligible":true,"headersReceived":false,"modelResultAccepted":false,"receivedBytes":0,"requestBytes":77450,"requestEncoding":"identity","requestMayContinueServerSide":true,"requestedResponseFormat":{"type":"json_object"},"requestedThinkingMode":"disabled","retryScope":"model_generation_only","slotWaitMs":0.003,"transportMode":"compatible","upstreamRetryable":true,"usageUnknown":true,"wireComplete":false,"workerExitCode":0,"workerReaped":true,"workerResponseBytes":648,"workerStarted":true,"workerTerminated":true}}},{"id":"call-580b0cab53724d00","status":"failed","durationMs":122497,"error":{"code":"transport","phase":"wait_headers","retryable":false,"transport":{"bodyComplete":false,"configuredWriteChunkBytes":512,"configuredWriteDelayMs":30,"deadlineBoundary":"provider_process","decodedBytes":0,"elapsedMs":122410.398,"exceptionType":"RemoteDisconnected","expectedBytes":null,"generationRetryEligible":true,"headersReceived":false,"modelResultAccepted":false,"receivedBytes":0,"requestBytes":85449,"requestEncoding":"identity","requestMayContinueServerSide":true,"requestedResponseFormat":{"type":"json_object"},"requestedThinkingMode":"disabled","retryScope":"model_generation_only","slotWaitMs":0.002,"transportMode":"compatible","upstreamRetryable":true,"usageUnknown":true,"wireComplete":false,"workerExitCode":0,"workerReaped":true,"workerResponseBytes":648,"workerStarted":true,"workerTerminated":true}}}]}''')


class HandoffPolicyTests(unittest.TestCase):
    def test_actual_round3_n1_two_failed_generations_cross_soft_target_but_leave_global_handoff_time(self):
        f=copy.deepcopy(ACTUAL_ROUND3_N1);run=f['run'];task=f['task'];run.update(status='running',steps=[task]);task['status']='running'
        before=copy.deepcopy(f);now=f['observedFailureAtMs'];control=stage_control(run,task,now)
        self.assertEqual(len(f['failedCalls']),2);self.assertGreater(now,control['expectedHandoffBy'])
        self.assertLess(now,run['budget']['deadlineAt'])
        for row in f['failedCalls']:
            data=row['error'];from backend.provider import ProviderError
            error=ProviderError('Actual recorded model transport failure',data['code'],False,phase=data['phase'],transport=data['transport'])
            self.assertEqual(generation_retry_decision(error,run,task,now,'handoff_final'),'retry')
            self.assertEqual(generation_retry_decision(error,run,task,now,'work'),'refresh_phase')
        self.assertIsNone(stage_action_feedback(control,{'type':'publish_artifact','path':'reporting_brief.md'}))
        self.assertEqual(f,before,'replay never rewrites actual failed calls or stage/run time')

    def test_soft_target_allows_only_explicit_handoff_until_original_run_deadline(self):
        task={'id':'task','agentId':'executor','revision':0,'status':'running'}
        run={'id':'run','mode':'task','status':'running','budget':{'profile':'time_only','deadlineAt':2000000},'steps':[task]}
        original=copy.deepcopy(run['budget']);control=stage_control(run,task,1000)
        saved=copy.deepcopy(task['stageControl']);now=control['expectedHandoffBy']+1
        # For a downstream-bearing plan the soft target precedes the run wall.
        task['stageControl'].update(synthesisAt=1100,handoffAt=1200,expectedHandoffBy=1300)
        saved=copy.deepcopy(task['stageControl']);control=stage_control(run,task,1500)
        self.assertTrue(control['handoffTargetIsSoft']);self.assertEqual(control['handoffTargetOverdueMs'],200)
        for kind in ('publish_artifact','publish_artifacts','package_delivery','review','final'):
            self.assertIsNone(stage_action_feedback(control,{'type':kind}),kind)
        for name in ('file.write','file.read','file.patch','code.test'):
            self.assertIsNone(stage_action_feedback(control,{'type':'tool','name':name}))
        for action in ({'type':'tool','name':'file.delete'},{'type':'tool','name':'git.push'},
                       {'type':'tool','name':'code.run'},{'type':'tool','name':'web.fetch'},
                       {'type':'tool','name':'web.search'},{'type':'delegate'},{'type':'ask_human'}):
            self.assertFalse(stage_action_feedback(control,action)['actionExecuted'])
        self.assertEqual(generation_retry_decision(failure(),run,task,1500,'work'),'refresh_phase')
        self.assertEqual(generation_retry_decision(failure(),run,task,1500,'handoff_final'),'retry')
        self.assertEqual(generation_retry_decision(failure(),run,task,2000000,'handoff_final'),'deny')
        self.assertEqual(run['budget'],original);self.assertEqual(task['stageControl'],saved)
        for field in ('_pendingTool','_uncertainCalls'):
            blocked=copy.deepcopy(task);blocked[field]=['unknown']
            self.assertEqual(generation_retry_decision(failure(),run,blocked,1500,'handoff_final'),'deny')


class StageHandoffRuntimeTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.tmp.cleanup()
    def execute(self,partial=False,global_close=False,wrong_contract=False):
        observed={};counts={};config=Config(Path(self.tmp.name),{'LLM_API_KEY':'offline-handoff','MAX_RETRIES':'0'})
        config.apply_time_only_profile()
        plan=[{'id':'build','title':'Build','agentId':'executor','outputContract':{'kind':'artifact','extensions':['.html' if wrong_contract else '.md'],'minCount':1}},
              {'id':'qa','title':'QA','agentId':'reviewer','requiredCapabilities':['review'],'dependsOn':['build']},
              {'id':'release','title':'Release','agentId':'coordinator','dependsOn':['build','qa']}]
        def action(n,c,m):
            title=c['task']['title'];i=counts.get(title,0);counts[title]=i+1
            run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if t['id']==c['task']['id'])
            if title=='Build':
                if i==0:
                    observed['deadline']=run['budget']['deadlineAt']
                    return {'type':'tool','name':'file.write','args':{'path':'brief.md','content':'Actual retained researched note with explicit limitations.'}}
                if i in (1,2):
                    if i==2:
                        now=clock_ms();task['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-700,handoffAt=now-500,expectedHandoffBy=now-100)
                        observed['softTarget']=task['stageControl']['expectedHandoffBy']
                        if global_close:
                            run['budget']['deadlineAt']=now+60000;task['stageControl']['runDeadlineAt']=run['budget']['deadlineAt'];observed['deadline']=run['budget']['deadlineAt']
                    raise failure()
                if i==3:
                    self.assertEqual(c['stageControl']['phase'],'handoff_final')
                    if not global_close:self.assertEqual(c['progressReceipt']['executionPhase'],'handoff')
                    return {'type':'publish_artifact','path':'brief.md'}
                return {'type':'final','status':'partial' if partial or wrong_contract else 'done','text':'Published actual source note; limitations remain.'}
            if title=='QA':
                if i==0:
                    dep=c['dependencyResults'][0];self.assertEqual(dep['status'],'partial' if partial else 'done')
                    self.assertEqual(len(c['authorizedArtifacts']),1)
                    if partial:self.assertEqual(dep['partialOutput']['acceptance'],'unassessed')
                    return {'type':'tool','name':'file.read','args':{'path':c['authorizedArtifacts'][0]['path']}}
                if i==1:
                    result=json.loads(next(x['content'] for x in reversed(m) if x['content'].startswith('动作的真实结果：')).split('：',1)[1])
                    self.assertEqual(result['content'],'Actual retained researched note with explicit limitations.')
                    if partial:
                        return {'type':'review','taskId':c['dependencyResults'][0]['id'],'verdict':'pass',
                                'feedback':'Actually read the partial note; editorial assessment only, not producer completion.',
                                'evidenceCallIds':[result['callId']]}
                return {'type':'final','text':'Read actual immutable granted source; no business acceptance claim.'}
            return {'type':'final','text':'No product acceptance is inferred from this offline fixture.'}
        class Plan(Model):
            def complete(self,messages,max_tokens=None,deadline=None):
                if messages[0]['content'].startswith('Plan an executable task DAG'):
                    return {'content':json.dumps({'tasks':plan}),'finish_reason':'stop','usage':{'total_tokens':1}}
                return super().complete(messages,max_tokens,deadline)
        model=Plan(action);app=Platform(config,provider=model,tools=Files(config));model.app=app;self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['executor','reviewer','coordinator'],'text':'Deliver real notes and verify their handoff.'})
        self.assertTrue(app.wait_for_idle(5));return app,app.state['sessions']['group']['run'],observed
    def test_two_generation_failures_cross_soft_handoff_then_actual_publish_and_authorized_read(self):
        app,run,observed=self.execute();by={t['title']:t for t in run['steps']}
        self.assertEqual(by['Build']['status'],'done');self.assertEqual(by['QA']['status'],'done')
        self.assertEqual(run['unknownModelUsages'],2);self.assertEqual(len(run['artifacts']),1)
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),1)
        self.assertTrue(any(c['tool']=='file.read' and c['agentId']=='reviewer' and c['status']=='success' for c in run['calls']))
        self.assertEqual(run['budget']['deadlineAt'],observed['deadline']);self.assertEqual(by['Build']['stageControl']['expectedHandoffBy'],observed['softTarget'])
        self.assertEqual(len([e for e in run['events'] if e['type']=='generation_retry']),1)
        self.assertEqual(len([e for e in run['events'] if e['type']=='generation_phase_refresh']),1)
    def test_honest_handoff_partial_remains_partial_and_only_explicit_qa_can_read_it(self):
        app,run,observed=self.execute(partial=True);by={t['title']:t for t in run['steps']}
        self.assertEqual(by['Build']['status'],'partial');self.assertEqual(by['Build']['closureReason'],'stage_handoff_final')
        self.assertEqual(by['Build']['error']['code'],'budget');self.assertEqual(by['Build']['partialOutput']['executionStatus'],'partial')
        self.assertEqual(by['QA']['status'],'done');self.assertEqual(by['Release']['status'],'skipped')
        self.assertEqual(run['status'],'partial');self.assertEqual(run['budget']['deadlineAt'],observed['deadline'])
        self.assertFalse(by['Build']['validation']['verified'])
        self.assertTrue(any(c['tool']=='file.read' and c['agentId']=='reviewer' and c['status']=='success' for c in run['calls']))
    def test_wrong_contract_is_not_promoted_to_budget_partial_review(self):
        app,run,observed=self.execute(partial=True,wrong_contract=True);by={t['title']:t for t in run['steps']}
        self.assertEqual(by['Build']['error']['code'],'output_contract');self.assertNotIn('partialOutput',by['Build'])
        self.assertEqual(by['QA']['status'],'skipped');self.assertEqual(by['Release']['status'],'skipped')
    def test_global_final_only_still_blocks_publication_and_does_not_extend_wall(self):
        app,run,observed=self.execute(global_close=True);by={t['title']:t for t in run['steps']}
        self.assertFalse(run['artifacts']);self.assertEqual(by['Build']['finalizationFailure']['code'],'final_action_required')
        self.assertEqual(by['Build']['error']['code'],'budget');self.assertEqual(by['QA']['status'],'skipped')
        self.assertEqual(run['budget']['deadlineAt'],observed['deadline'])


if __name__=='__main__':unittest.main()
