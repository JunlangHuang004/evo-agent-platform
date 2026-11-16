"""Finite generation retry is separate from replaying tools/tasks. No real APIs."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform,clock_ms
from backend.generation_retry import generation_retry_allowed
from backend.provider import ProviderError
from backend import provider_deadline
from tests import test_provider_deadline as deadline_fixtures
from tests.test_time_only_runtime import Model,Files


def transport():
    return {'workerTerminated':True,'workerReaped':True,'modelResultAccepted':False,
            'generationRetryEligible':True,'retryScope':'model_generation_only','upstreamRetryable':True,
            'usageUnknown':True,'requestMayContinueServerSide':True,'headersReceived':False,'receivedBytes':0}


def failure(code='transport',**fields):
    details=transport();details.update(fields)
    return ProviderError('No complete model response; no action accepted.',code,False,phase='wait_headers',transport=details)


class GenerationRetryPolicyTests(unittest.TestCase):
    def test_only_trusted_closed_worker_generation_errors_qualify(self):
        now=1000;run={'id':'run','status':'running','budget':{'profile':'time_only','deadlineAt':2000}}
        task={'id':'task','revision':0}
        self.assertTrue(generation_retry_allowed(failure(),run,task,now))
        for code in ('authorization','parameters','worker_protocol','worker_output_limit','worker_transport',
                     'worker_termination_failed','deadline_exceeded','output_truncated','response_schema'):
            self.assertFalse(generation_retry_allowed(failure(code),run,task,now),code)
        for field,value in (('workerReaped',False),('workerTerminated',False),('modelResultAccepted',True),
                            ('generationRetryEligible',False),('upstreamRetryable',False),('retryScope','entire_task')):
            self.assertFalse(generation_retry_allowed(failure(**{field:value}),run,task,now),field)
        self.assertFalse(generation_retry_allowed(ProviderError('untrusted retryable','network',True),run,task,now))
        self.assertFalse(generation_retry_allowed(failure('provider_http',httpStatus=401),run,task,now))
        self.assertTrue(generation_retry_allowed(failure('provider_http',httpStatus=503),run,task,now))

    def test_original_deadline_stage_and_unknown_effects_are_not_extended(self):
        run={'id':'run','status':'running','budget':{'profile':'time_only','deadlineAt':2000}}
        task={'id':'task','agentId':'coordinator','revision':0,'stageControl':{'runId':'run','taskId':'task','agentId':'coordinator','revision':0,'runDeadlineAt':2000,'allocatedAt':900,
              'synthesisAt':1200,'handoffAt':1500,'expectedHandoffBy':1800}}
        original=copy.deepcopy((run,task));self.assertTrue(generation_retry_allowed(failure(),run,task,1499))
        self.assertFalse(generation_retry_allowed(failure(),run,task,1500));self.assertFalse(generation_retry_allowed(failure(),run,task,2000))
        self.assertEqual((run,task),original)
        for field,value in (('agentId','different-owner'),('synthesisAt',1700),('runDeadlineAt',True),('expectedHandoffBy',2100)):
            broken=copy.deepcopy(task);broken['stageControl'][field]=value
            self.assertFalse(generation_retry_allowed(failure(),run,broken,1100),field)
        for key in ('_pendingTool','_uncertainCalls'):
            blocked=copy.deepcopy(task);blocked[key]=['unknown'];self.assertFalse(generation_retry_allowed(failure(),run,blocked,1200))
        run['status']='cancelled';self.assertFalse(generation_retry_allowed(failure(),run,task,1200))

    def test_real_worker_error_envelope_marks_narrow_scope_but_never_generic_task_retryable(self):
        local=deadline_fixtures.ProviderDeadlineTests();local.setUp();self.addCleanup(local.tearDown)
        for code,retryable,phase,details in (
            ('transport',True,'wait_headers',{'headersReceived':False,'receivedBytes':0}),
            ('transport_incomplete',True,'read_body',{'httpStatus':200,'headersReceived':True,'receivedBytes':13,'bodyComplete':False}),
            ('transport_incomplete',True,'decompress_body',{'httpStatus':200,'headersReceived':True,'wireComplete':True,'bodyComplete':False}),
            ('authorization',False,'wait_headers',{}),('parameters',False,'wait_headers',{}),('worker_protocol',True,'wait_headers',{})):
            envelope={'ok':False,'error':{'message':'Fixture error','code':code,'phase':phase,
                       'retryable':retryable,'transport':details}}
            command=local.command('print('+repr(json.dumps(envelope))+')\n')
            with self.subTest(code=code),patch.object(provider_deadline,'_worker_command',return_value=command):
                with self.assertRaises(ProviderError) as caught:
                    local.client.complete(local.messages,deadline=time.monotonic()+2)
                error=caught.exception;self.assertFalse(error.retryable)
                self.assertTrue(error.transport['workerReaped']);self.assertTrue(error.transport['workerTerminated'])
                self.assertEqual(error.transport['generationRetryEligible'],code in ('transport','transport_incomplete'))
                self.assertFalse(error.transport['modelResultAccepted'])
                if code=='transport_incomplete':self.assertTrue(error.transport['usageUnknown'])
                self.assertNotIn(local.secret,str(error)+json.dumps(error.transport))


class GenerationRetryRuntimeTests(unittest.TestCase):
    def setUp(self):self.directory=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(4);app.repo.close()
        self.directory.cleanup()
    def create(self,action,settings=None):
        values={'LLM_API_KEY':'offline-generation','MAX_RETRIES':'0'};values.update(settings or {})
        config=Config(Path(self.directory.name)/str(len(self.apps)),values);config.apply_time_only_profile()
        model=Model(action);app=Platform(config,provider=model,tools=Files(config));model.app=app;self.apps.append(app)
        return app,model
    def send(self,app):app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Perform actual local actions once.'})
    def test_failed_generation_retries_only_generation_and_executes_each_tool_exactly_once(self):
        timing={}
        def action(n,context,messages):
            if n==0:return {'type':'tool','name':'file.write','args':{'path':'before.md','content':'Actual first version.'}}
            if n==1:
                timing['failure']=time.monotonic()
                error=failure();error.response_content=json.dumps({'type':'tool','name':'file.write','args':{'path':'MUST-NOT-RUN.md','content':'partial'}})
                raise error
            if n==2:
                timing['retry']=time.monotonic()
                return {'type':'tool','name':'file.write','args':{'path':'after.md','content':'Actual second version.'}}
            return {'type':'final','text':'Actual actions completed once, no semantic product claim.'}
        app,model=self.create(action);self.send(app);self.assertTrue(app.wait_for_idle(4));run=app.state['sessions']['group']['run']
        self.assertEqual(run['status'],'done');writes=[c for c in run['calls'] if c['tool']=='file.write']
        self.assertEqual([c['args']['path'] for c in writes],['before.md','after.md']);self.assertTrue(all(c['status']=='success' for c in writes))
        self.assertEqual(run['modelCalls'],5);self.assertEqual(run['unknownModelUsages'],1)
        self.assertGreaterEqual(timing['retry']-timing['failure'],.45,'scheduler notifications cannot skip backoff')
        self.assertEqual(sum(c.get('usageUnknown') is True for c in run['calls']),1)
        self.assertEqual(len([e for e in run['events'] if e['type']=='generation_retry']),1)
        self.assertEqual(app.config.max_retries,0);self.assertEqual(app.config.model_generation_retries,2)
        self.assertEqual(run['budget']['timeMs'],1800000);self.assertIsNone(run['budget']['modelCalls'])
        self.assertFalse((app.tools.workspace('coordinator',run['id'])/'MUST-NOT-RUN.md').exists())
        saved=app.repo.get_run(run['id']);self.assertEqual(saved['unknownModelUsages'],1)
        self.assertEqual(saved['budget']['deadlineAt'],run['budget']['deadlineAt'])
        self.assertEqual(next(t for t in run['steps'] if not t.get('_planning'))['retries'],0)

    def test_time_only_burst_exhaustion_enters_cooldown_with_every_failed_call_and_unknown_usage_preserved(self):
        def action(n,c,m):raise failure()
        app,model=self.create(action);self.send(app);until=time.monotonic()+4
        while time.monotonic()<until:
            run=app.state['sessions']['group']['run']
            if any(t.get('generationCooldown') for t in run['steps']):break
            time.sleep(.01)
        else:self.fail('Time-only generation burst did not enter durable cooldown')
        self.assertEqual(run['modelCalls'],4);self.assertEqual(run['unknownModelUsages'],3)
        self.assertEqual(len([e for e in run['events'] if e['type']=='generation_retry']),2)
        self.assertEqual([c['status'] for c in run['calls'] if c['tool']=='llm.chat'],['success','failed','failed','failed'])
        self.assertFalse(any(c['tool']=='file.write' for c in run['calls']));self.assertNotEqual(run['status'],'done')
        app.action({'action':'cancel','runId':run['id']});self.assertTrue(app.wait_for_idle(3))

    def test_permanent_error_and_disabled_recovery_do_not_retry(self):
        for mode in ('authorization','disabled'):
            def action(n,c,m):
                raise failure('authorization' if mode=='authorization' else 'transport')
            app,model=self.create(action,{'MODEL_GENERATION_RETRIES':'0'} if mode=='disabled' else {})
            self.send(app);self.assertTrue(app.wait_for_idle(3));run=app.state['sessions']['group']['run']
            with self.subTest(mode=mode):
                self.assertEqual(run['modelCalls'],2);self.assertFalse(any(e['type']=='generation_retry' for e in run['events']))

    def test_failed_work_generation_crossing_synthesis_refreshes_phase_before_drafting(self):
        def action(n,c,m):
            if n==0:
                run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
                now=clock_ms();task['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-100,handoffAt=now+5000,expectedHandoffBy=now+10000)
                raise failure()
            self.assertEqual(c['stageControl']['phase'],'synthesis')
            if n==1:return {'type':'tool','name':'file.write','args':{'path':'brief.md','content':'Actual retained evidence and explicit gaps.'}}
            if n==2:return {'type':'publish_artifact','path':'brief.md'}
            return {'type':'final','text':'Actual concise brief published.'}
        app,model=self.create(action);self.send(app);self.assertTrue(app.wait_for_idle(4));run=app.state['sessions']['group']['run']
        self.assertEqual(run['status'],'done');self.assertEqual(len(run['artifacts']),1)
        self.assertEqual(len([e for e in run['events'] if e['type']=='generation_phase_refresh']),1)
        self.assertFalse(any(e['type']=='generation_retry' for e in run['events']))
        self.assertFalse(any(c['tool'].startswith('web.') for c in run['calls']));self.assertEqual(run['unknownModelUsages'],1)

    def test_current_handoff_final_generation_can_retry_within_original_close_budget(self):
        def action(n,c,m):
            if n==0:
                run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
                now=clock_ms();task['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-700,handoffAt=now-100,expectedHandoffBy=now+5000)
                return {'type':'accept'}
            self.assertEqual(c['progressReceipt']['executionPhase'],'handoff')
            if n==1:raise failure()
            return {'type':'final','text':'An actual final response after transient generation failure.'}
        app,model=self.create(action);self.send(app);self.assertTrue(app.wait_for_idle(4));run=app.state['sessions']['group']['run']
        self.assertEqual(run['status'],'done');self.assertEqual(run['modelCalls'],4)
        self.assertEqual(len([e for e in run['events'] if e['type']=='generation_retry']),1)
        self.assertFalse(any(c['tool']!='llm.chat' for c in run['calls']));self.assertEqual(run['unknownModelUsages'],1)

    def test_global_final_only_crossing_synthesis_keeps_same_closing_operation(self):
        observed={}
        def action(n,c,m):
            run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
            if n==0:
                now=clock_ms();run['budget']['deadlineAt']=now+60000
                task['stageControl'].update(allocatedAt=now-1000,synthesisAt=now+150,handoffAt=now+20000,
                                           expectedHandoffBy=now+45000,runDeadlineAt=run['budget']['deadlineAt'])
                observed.update(deadline=run['budget']['deadlineAt'],handoff=task['stageControl']['expectedHandoffBy'])
                return {'type':'accept'}
            self.assertEqual(c['progressReceipt']['executionPhase'],'final_only')
            if n==1:time.sleep(.2);raise failure()
            return {'type':'final','text':'The actual closing response was obtained after retry.'}
        app,model=self.create(action);self.send(app);self.assertTrue(app.wait_for_idle(4));run=app.state['sessions']['group']['run']
        self.assertEqual(run['status'],'done');self.assertEqual(run['modelCalls'],4)
        self.assertFalse(any(e['type']=='generation_phase_refresh' for e in run['events']))
        self.assertEqual(run['unknownModelUsages'],1);self.assertEqual(run['budget']['deadlineAt'],observed['deadline'])
        task=next(t for t in run['steps'] if not t.get('_planning'));self.assertEqual(task['stageControl']['expectedHandoffBy'],observed['handoff'])
        self.assertFalse(any(c['tool']!='llm.chat' for c in run['calls']))

    def test_zero_generation_retries_disables_phase_refresh_as_well(self):
        def action(n,c,m):
            run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
            now=clock_ms();task['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-100,handoffAt=now+5000,expectedHandoffBy=now+10000)
            raise failure()
        app,model=self.create(action,{'MODEL_GENERATION_RETRIES':'0'});self.send(app);self.assertTrue(app.wait_for_idle(3))
        run=app.state['sessions']['group']['run'];self.assertEqual(run['modelCalls'],2)
        self.assertFalse(any(e['type'] in ('generation_retry','generation_phase_refresh') for e in run['events']))

    def test_cancel_during_retry_backoff_cannot_start_next_model_or_tool(self):
        def action(n,c,m):raise failure()
        app,model=self.create(action);self.send(app);until=time.monotonic()+2
        while time.monotonic()<until:
            if any(e['type']=='generation_retry' for e in app.snapshot()['sessions']['group']['run']['events']):break
            time.sleep(.005)
        else:self.fail('No retry checkpoint observed')
        run=app.snapshot()['sessions']['group']['run'];app.action({'action':'cancel','runId':run['id']})
        self.assertTrue(app.wait_for_idle(3));run=app.state['sessions']['group']['run']
        self.assertEqual(run['status'],'cancelled');self.assertEqual(run['modelCalls'],2)
        self.assertFalse(any(c['tool']!='llm.chat' for c in run['calls']))


if __name__=='__main__':unittest.main()
