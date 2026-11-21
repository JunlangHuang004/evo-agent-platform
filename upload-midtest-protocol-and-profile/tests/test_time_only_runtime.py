"""Real optional null caps and a fixed wall deadline, with scripted providers only."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
import io

from backend.config import Config
from backend.engine import Platform,ActionError,clock_ms
from backend.provider import ProviderError
from backend.server import main,TeamHub
from backend.tools import ToolGateway,ToolError


class Files(ToolGateway):
    def capabilities(self):return {}


class Model:
    def __init__(self,action=None,usage=1):self.action=action;self.usage=usage;self.calls=[];self.app=None
    def complete(self,messages,max_tokens=None,deadline=None):
        self.calls.append({'deadline':deadline,'maxTokens':max_tokens,'messages':copy.deepcopy(messages)})
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            result={'tasks':[{'id':'one','title':'Work','agentId':'coordinator'}]}
        else:
            result=self.action(len(self.calls)-2,json.loads(messages[1]['content']),messages) if self.action else {'type':'final','text':'A real plain-text answer.'}
        return {'content':result if isinstance(result,str) else json.dumps(result),'finish_reason':'stop','usage':{'total_tokens':self.usage}}


def wait_until(fn,timeout=2):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if fn():return True
        time.sleep(.01)
    return False


class TimeOnlyRuntimeTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.apps=[];self.releases=[]
    def tearDown(self):
        for event in self.releases:event.set()
        for app in self.apps:
            app.shutdown();app.wait_for_idle(4);app.repo.close()
        self.tmp.cleanup()
    def create(self,model=None,tools=None,path=None):
        config=Config(Path(path or self.tmp.name),{'LLM_API_KEY':'offline-time-only','MAX_RETRIES':'0'})
        before=(config.max_completion_tokens,config.model_timeout,config.max_tasks,config.max_depth)
        profile=config.apply_time_only_profile()
        self.assertEqual(before,(config.max_completion_tokens,config.model_timeout,config.max_tasks,config.max_depth))
        self.assertEqual([profile[k] for k in ('maxAgentTurns','maxModelCalls','maxRunTokens')],[None,None,None])
        self.assertEqual(profile['maxContextChars'],120000)
        model=model or Model();app=Platform(config,provider=model,tools=tools or Files(config));model.app=app;self.apps.append(app)
        return app,model
    def send(self,app):
        return app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Return real work with a fixed wall deadline.'})
    def current_run(self,app):return app.state['sessions']['group']['run']
    def expire(self,app,delay=0):
        with app.changed:
            run=self.current_run(app);run['budget']['deadlineAt']=clock_ms()+delay
            app._persist();app.changed.notify_all()

    def test_three_limits_are_null_and_more_than_60_calls_24_turns_400k_tokens_are_allowed(self):
        model=Model(lambda n,c,m:{'type':'accept','sequence':n} if n<70 else {'type':'final','text':'Completed an evidence-neutral protocol exercise.'},usage=10000)
        # This checks absence of count/token caps, not a throughput target.
        # Real SQLite/context work can exceed 12s on slower Docker hosts.
        # All 72-call, token, deadline and persisted-state assertions remain.
        app,model=self.create(model);self.send(app);self.assertTrue(app.wait_for_idle(60));run=self.current_run(app)
        self.assertEqual(run['status'],'done');self.assertGreater(run['modelCalls'],60);self.assertGreater(run['tokenUsage'],400000)
        task=next(t for t in run['steps'] if not t.get('_planning'));self.assertGreater(task['turns'],24)
        self.assertIsNone(task['progress']['turnLimit']);self.assertIsNone(task['progress']['remainingAfterThisTurn'])
        self.assertTrue(all(run['budget'][key] is None for key in ('tokens','modelCalls','agentTurns')))
        self.assertEqual(run['budget']['timeMs'],1800000);self.assertEqual(run['budgetExtensions'],0)
        self.assertFalse(any(a['type']=='budget' for a in run['approvals']))
        self.assertTrue(all(c['deadline'] is not None and c['maxTokens']<=app.config.max_completion_tokens for c in model.calls))
        self.assertEqual(app.repo.get_run(run['id'])['budget'],run['budget'])
        self.assertIsNone(app.snapshot()['sessions']['group']['run']['budget']['tokens'])

    def test_human_wait_and_pause_count_toward_wall_time_without_inventing_a_reply(self):
        for paused in (False,True):
            app,model=self.create(Model(lambda n,c,m:{'type':'ask_human','question':'Need a consequential requirement?','choices':['A','B']}),path=Path(self.tmp.name)/str(paused))
            self.send(app);self.assertTrue(wait_until(lambda:any(a['status']=='pending' for a in self.current_run(app)['approvals'])))
            if paused:app.action({'action':'pause'})
            before_calls=self.current_run(app)['modelCalls'];self.expire(app,20)
            self.assertTrue(app.wait_for_idle(3));run=self.current_run(app)
            self.assertTrue(run['timeExpired']);self.assertEqual(run['status'],'failed')
            self.assertEqual(run['modelCalls'],before_calls);self.assertEqual(run['budgetExtensions'],0)
            approval=run['approvals'][0];self.assertEqual(approval['status'],'expired')
            self.assertEqual(approval['expirationReason'],'run_deadline');self.assertNotIn('clarificationReceipt',approval)
            with self.assertRaises(ActionError):app.action({'action':'approval','approvalId':approval['id'],'choiceId':'option-0'})

    def test_restart_does_not_refill_or_extend_deadline(self):
        app,_=self.create(Model(lambda n,c,m:{'type':'ask_human','question':'Actual unresolved requirement?','choices':['A']}))
        self.send(app);self.assertTrue(wait_until(lambda:bool(self.current_run(app)['approvals'])))
        with app.changed:
            self.current_run(app)['budget']['deadlineAt']=clock_ms()-1;deadline=self.current_run(app)['budget']['deadlineAt'];app.shutdown()
        self.assertTrue(app.wait_for_idle(3));app.repo.close();self.apps.remove(app)
        restored,model=self.create();self.assertEqual(self.current_run(restored)['budget']['deadlineAt'],deadline)
        with self.assertRaises(ActionError):restored.action({'action':'recover'})
        self.assertEqual(model.calls,[]);self.assertEqual(self.current_run(restored)['budget']['deadlineAt'],deadline)
        self.assertIn(self.current_run(restored)['status'],('partial','failed'))

    def test_late_provider_response_is_audited_but_cannot_execute_its_tool(self):
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        def action(n,c,m):
            entered.set();release.wait(2)
            return {'type':'tool','name':'file.write','args':{'path':'late.txt','content':'MUST NOT EXECUTE'}}
        app,model=self.create(Model(action));self.send(app);self.assertTrue(entered.wait(2));self.expire(app,20)
        # Expiry publishes task/run terminal state under app.lock. Observe the
        # same atomic snapshot as clients, not the internal intermediate write.
        self.assertTrue(wait_until(lambda:app.snapshot()['sessions']['group']['run'].get('timeExpired')))
        self.assertEqual(self.current_run(app)['status'],'failed');release.set();self.assertTrue(app.wait_for_idle(3));run=self.current_run(app)
        self.assertEqual(len(model.calls),2);self.assertFalse(any(c['tool']=='file.write' for c in run['calls']))
        self.assertFalse((app.tools.workspace('coordinator',run['id'])/'late.txt').exists())
        self.assertEqual(run['calls'][-1]['status'],'late_result')
        self.assertTrue(any(x.get('kind')=='late_model_response' for x in run['transcript']))

    def test_final_only_is_time_based_one_request_and_never_executes_a_new_tool(self):
        def action(n,c,m):
            if n==0:
                with app.changed:
                    run=self.current_run(app);run['budget']['deadlineAt']=clock_ms()+50000
                    # This is an originally short-wall fixture. Keep its saved
                    # server stage binding consistent; stale deadline rejection
                    # has an independent explicit boundary regression.
                    for task in run['steps']:
                        if control:=task.get('stageControl'):
                            control['runDeadlineAt']=run['budget']['deadlineAt']
                            for key in ('synthesisAt','handoffAt','expectedHandoffBy'):
                                control[key]=min(control[key],control['runDeadlineAt'])
                return {'type':'accept'}
            self.assertEqual(c['progressReceipt']['executionPhase'],'final_only')
            self.assertIsNone(c['progressReceipt']['turnLimit'])
            self.assertTrue(any(x['content'].startswith('最后收尾约束：') for x in m))
            return {'type':'tool','name':'file.write','args':{'path':'blocked.txt','content':'NO'}}
        app,model=self.create(Model(action));self.send(app);self.assertTrue(app.wait_for_idle(3));run=self.current_run(app)
        self.assertEqual(len(model.calls),3);self.assertFalse(any(c['tool']=='file.write' for c in run['calls']))
        self.assertFalse(run['artifacts']);self.assertNotEqual(run['status'],'done')
        self.assertEqual(run['steps'][-1]['finalizationFailure']['code'],'final_action_required')

    def test_deadline_preserves_actual_published_artifact_as_unassessed_partial(self):
        def action(n,c,m):
            return [{'type':'tool','name':'file.write','args':{'path':'draft.md','content':'Actual snapshot.'}},
                    {'type':'publish_artifact','path':'draft.md'},
                    {'type':'ask_human','question':'A remaining requirement?','choices':['A']}][min(n,2)]
        app,_=self.create(Model(action));self.send(app);self.assertTrue(wait_until(lambda:bool(self.current_run(app)['approvals'])))
        self.expire(app,20);self.assertTrue(app.wait_for_idle(3));run=self.current_run(app);task=run['steps'][-1]
        self.assertEqual(run['status'],'partial');self.assertEqual(task['error']['causeCode'],'deadline_exceeded')
        self.assertEqual(task['partialOutput']['acceptance'],'unassessed');self.assertEqual(run['artifacts'][0]['content'],'Actual snapshot.')
        self.assertEqual(task['status'],'partial');self.assertFalse(task.get('validation',{}).get('verified'))

    def test_late_side_effect_keeps_unknown_effect_reserved_and_is_not_replayed(self):
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        class Slow(Files):
            def execute(self,agent,run,name,args,approved=False,deadline=None):
                if name=='file.write':
                    entered.set();release.wait(2)
                    return {'ok':False,'deadlineExceeded':True,'outcomeUnknown':True}
                return super().execute(agent,run,name,args,approved,deadline=deadline)
        config=Config(Path(self.tmp.name)/'tool',{'LLM_API_KEY':'offline-time'});config.apply_time_only_profile();tools=Slow(config)
        model=Model(lambda n,c,m:{'type':'tool','name':'file.write','args':{'path':'possibly-written.md','content':'Unknown side effect'}})
        app=Platform(config,provider=model,tools=tools);self.apps.append(app);self.send(app);self.assertTrue(entered.wait(2));self.expire(app,20)
        self.assertTrue(wait_until(lambda:self.current_run(app).get('timeExpired')));release.set();self.assertTrue(app.wait_for_idle(3));run=self.current_run(app)
        call=next(c for c in run['calls'] if c['tool']=='file.write');self.assertEqual(call['status'],'unknown')
        task=run['steps'][-1];self.assertIn(call['id'],task['_uncertainCalls']);self.assertIn('_pendingTool',task)
        self.assertEqual(app.repo.effect(task['_pendingTool']['effectKey'])['status'],'reserved')
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),1);self.assertNotIn('partialOutput',task)

    def test_profile_cli_and_lazy_team_copy_keep_true_nulls_without_writing_env(self):
        config=Config(Path(self.tmp.name)/'cli',{'LLM_API_KEY':'fixture-not-exposed'})
        output=io.StringIO()
        with patch('backend.server.Config',return_value=config),patch('backend.server.ThreadingHTTPServer') as factory,patch('sys.argv',['backend.server','--time-only']),redirect_stdout(output):
            factory.return_value.serve_forever.side_effect=KeyboardInterrupt;main()
        self.assertIn('"maxAgentTurns": null',output.getvalue());self.assertNotIn(config.api_key,output.getvalue())
        self.assertFalse((config.root/'.env').exists())
        class Stub:
            def __init__(self,config,**kwargs):self.config=config
        hub=TeamHub(config,platform_factory=Stub)
        self.assertIsNone(hub.get('software').config.max_agent_turns);self.assertIsNone(hub.get('newsroom').config.max_run_tokens)
        self.assertNotEqual(hub.get('software').config.data_dir,hub.get('newsroom').config.data_dir)

    def test_active_operation_sum_is_not_the_wall_deadline_and_cannot_double_count_parallel_time(self):
        app,_=self.create(Model(lambda n,c,m:{'type':'ask_human','question':'Real requirement?','choices':['A']}))
        self.send(app);self.assertTrue(wait_until(lambda:bool(self.current_run(app)['approvals'])))
        with app.changed:
            run=self.current_run(app);run['activeMs']=4000000;run['tokenUsage']=2000000;run['modelCalls']=1000
            app._budget('group',run['id'],run['steps'][-1],estimated_tokens=999999)
            self.assertFalse(run.get('timeExpired'));self.assertGreater(app._remaining_ms(run),120000)
            self.assertFalse(any(a['type']=='budget' for a in run['approvals']))
        self.expire(app,10);self.assertTrue(app.wait_for_idle(3))

    def test_deadline_race_between_budget_gate_and_atomic_reservation_starts_no_provider_call(self):
        app,model=self.create();original=app._budget
        def expire_after_check(channel,run_id,task,estimated_tokens=0):
            original(channel,run_id,task,estimated_tokens)
            with app.changed:self.current_run(app)['budget']['deadlineAt']=clock_ms()-1
        with patch.object(app,'_budget',side_effect=expire_after_check):
            self.send(app);self.assertTrue(app.wait_for_idle(3))
        run=self.current_run(app);self.assertEqual(model.calls,[]);self.assertEqual(run['modelCalls'],0)
        self.assertEqual(run['status'],'failed');self.assertFalse(run['calls'])

    def test_permission_expiry_does_not_approve_or_perform_the_proposed_overwrite(self):
        def action(n,c,m):return {'type':'tool','name':'file.write','args':{'path':'kept.md','content':'Original' if n==0 else 'New overwrite'}}
        app,_=self.create(Model(action));self.send(app)
        self.assertTrue(wait_until(lambda:any(a['type']=='permission' for a in self.current_run(app)['approvals'])))
        self.expire(app,10);self.assertTrue(app.wait_for_idle(3));run=self.current_run(app)
        self.assertEqual((app.tools.workspace('coordinator',run['id'])/'kept.md').read_text(),'Original')
        proposal=next(c for c in run['calls'] if c['tool']=='file.write' and c['args']['content']=='New overwrite')
        self.assertEqual(proposal['status'],'not_executed');self.assertTrue(proposal['result']['notExecuted'])
        approval=next(a for a in run['approvals'] if a['type']=='permission')
        self.assertEqual(approval['status'],'expired');self.assertNotIn('response',approval)

    def test_time_only_does_not_automatically_replay_an_upstream_error_even_when_marked_retryable(self):
        def action(n,c,m):raise ProviderError('An upstream request may have been sent.','network',True)
        app,model=self.create(Model(action));app.config.max_retries=2
        self.send(app);self.assertTrue(app.wait_for_idle(3));run=self.current_run(app)
        self.assertEqual(len(model.calls),2);self.assertEqual(run['modelCalls'],2)
        self.assertEqual(run['steps'][-1]['error']['code'],'network')

    def test_unknown_result_and_retryable_unknown_exception_stop_for_reconciliation_without_replay(self):
        for mode in ('result','exception'):
            with self.subTest(mode=mode):
                class Unknown(Files):
                    actual_calls=0
                    def execute(self,agent,run,name,args,approved=False,deadline=None):
                        if name=='file.write':
                            self.actual_calls+=1
                            if mode=='exception':raise ToolError('network','A write may have completed.',retryable=True,outcome_unknown=True)
                            return {'ok':False,'outcomeUnknown':True,'receipt':'Actual unknown response, not a known failure.'}
                        return super().execute(agent,run,name,args,approved,deadline=deadline)
                root=Path(self.tmp.name)/mode;config=Config(root,{'LLM_API_KEY':'offline-unknown','MAX_RETRIES':'2'});config.apply_time_only_profile()
                tools=Unknown(config);model=Model(lambda n,c,m:{'type':'tool','name':'file.write','args':{'path':'unknown.md','content':'Actual proposed content.'}})
                app=Platform(config,provider=model,tools=tools);self.apps.append(app);self.send(app)
                self.assertTrue(wait_until(lambda:self.current_run(app)['status']=='interrupted'))
                run=self.current_run(app);task=run['steps'][-1];call=next(c for c in run['calls'] if c['tool']=='file.write')
                self.assertEqual(tools.actual_calls,1);self.assertEqual(len(model.calls),2)
                self.assertEqual(call['status'],'unknown');self.assertEqual(task['_uncertainCalls'],[call['id']])
                self.assertEqual(task['_pendingTool']['phase'],'uncertain')
                self.assertEqual(app.repo.effect(task['_pendingTool']['effectKey'])['status'],'reserved')
                self.assertNotIn('partialOutput',task);self.assertFalse(run.get('timeExpired'))
                app.shutdown();self.assertTrue(app.wait_for_idle(3))


if __name__=='__main__':unittest.main()
