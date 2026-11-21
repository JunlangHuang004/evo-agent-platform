"""Generic fixed-wall phase admission; no real provider or user product."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.config import Config
from backend.engine import Platform,clock_ms
from backend.runtime import compress_messages,ProtocolError
from backend.stage_control import stage_control,stage_action_feedback,failure_references,latest_test_diagnostic
from backend.tools import ToolGateway


def fixture(now=10000):
    tasks=[{'id':str(i),'agentId':'coordinator','status':'pending','dependsOn':[str(i-1)] if i else [],'revision':0} for i in range(4)]
    run={'id':'run-stage','mode':'task','budget':{'profile':'time_only','deadlineAt':now+1800000},'steps':tasks}
    return run,tasks[0]


class StageControlTests(unittest.TestCase):
    def test_original_deadline_is_shared_by_critical_path_and_fixed_across_same_revision_restores(self):
        run,task=fixture();original=copy.deepcopy(run['budget'])
        first=stage_control(run,task,10000)
        self.assertEqual(first['criticalStages'],4);self.assertLess(first['expectedHandoffBy'],run['budget']['deadlineAt'])
        saved=copy.deepcopy(task['stageControl'])
        run['steps'][1]['status']='done'
        again=stage_control(copy.deepcopy(run),copy.deepcopy(task),first['handoffAt']+1)
        self.assertEqual(again['phase'],'handoff_final');self.assertEqual(task['stageControl'],saved)
        self.assertEqual(again['expectedHandoffBy'],first['expectedHandoffBy']);self.assertEqual(run['budget'],original)
        task['revision']=2
        with self.assertRaises(ProtocolError):stage_control(run,task,10001)

    def test_synthesis_rejects_acquisition_and_delegation_but_allows_real_draft_test_and_publish(self):
        run,task=fixture();policy=stage_control(run,task,10000);policy=stage_control(run,task,policy['synthesisAt'])
        self.assertEqual(policy['phase'],'synthesis')
        for action in ({'type':'tool','name':'web.search'},{'type':'tool','name':'web.fetch'},{'type':'delegate'}):
            result=stage_action_feedback(policy,action);self.assertEqual(result['error']['code'],'stage_acquisition_closed')
            self.assertFalse(result['actionExecuted'])
        for action in ({'type':'tool','name':'file.write'},{'type':'tool','name':'file.patch'},
                       {'type':'tool','name':'code.test'},{'type':'publish_artifact'},{'type':'review'},{'type':'final','status':'partial'}):
            self.assertIsNone(stage_action_feedback(policy,action))
        policy=stage_control(run,task,policy['handoffAt'])
        self.assertIsNone(stage_action_feedback(policy,{'type':'tool','name':'file.write'}))
        self.assertIsNotNone(stage_action_feedback(policy,{'type':'tool','name':'web.fetch'}))
        self.assertIsNone(stage_action_feedback(policy,{'type':'final','status':'done'}),'original contract remains checked by engine')

    def test_receipt_and_exact_failures_survive_compaction_without_source_reconstruction(self):
        run,task=fixture();policy=stage_control(run,task,10000)
        calls=[{'id':'bad-test','taskId':task['id'],'agentId':'coordinator','revision':0,'tool':'code.test','status':'failed',
                'result':{'error':{'code':'browser_assertion','message':'expected Free, actual Cancellation aborted'},
                          'browser':{'sourceManifest':[{'path':'imports/css/style.css','sha256':'a'*64}],
                                     'assertions':[{'action':'assert_text','selector':'#status','passed':False,
                                                    'expected':'Free','actual':'Cancellation aborted'}]}}}]
        run['calls']=calls;before=copy.deepcopy(run)
        diagnostic=latest_test_diagnostic(run,task)
        initial={'task':{'id':task['id']},'stageControl':policy,'lastTestDiagnostic':diagnostic,
                 'dependencyResults':[{'id':'producer','failureDiagnostics':failure_references(run,task)}]}
        messages=[{'role':'system','content':'Real complete protocol'},{'role':'user','content':json.dumps(initial)}]
        messages += [{'role':'user','content':'optional '+('x'*5000)} for _ in range(8)]
        compact=compress_messages(messages,6000);value=json.loads(compact[1]['content'])
        self.assertEqual(value['stageControl'],policy);self.assertEqual(value['lastTestDiagnostic'],diagnostic)
        self.assertEqual(value['dependencyResults'][0]['failureDiagnostics']['records'][0]['callId'],'bad-test')
        self.assertEqual(run,before);self.assertIn('not proof',diagnostic['instruction'])

    def test_invalid_saved_binding_does_not_reallocate_and_small_remaining_time_is_ordered(self):
        run,task=fixture();policy=stage_control(run,task,10000);task['stageControl']['taskId']='foreign'
        with self.assertRaises(ProtocolError):stage_control(run,task,10001)
        for remaining in (0,1,10000):
            run,task=fixture();run['budget']['deadlineAt']=10000+remaining
            result=stage_control(run,task,10000)
            self.assertLessEqual(result['synthesisAt'],result['handoffAt']);self.assertLessEqual(result['expectedHandoffBy'],run['budget']['deadlineAt'])
        run,task=fixture();run['budget']['profile']='bounded';self.assertIsNone(stage_control(run,task,10000))

    def test_failed_browser_diagnostic_is_actual_tail_feedback_and_bound_to_rework_context(self):
        from tests.test_browser_progress_coverage import BrowserProgressCoverageTests
        local=BrowserProgressCoverageTests();local.setUp();self.addCleanup(local.tearDown)
        call=local.call();call['status']='failed';call['result'].update(ok=False,exitCode=1,error='Expected duplicate error; actual Cancellation aborted.')
        check=call['result']['browser']['assertions'][0];check.update(passed=False,expected='Duplicate',actual='Cancellation aborted.')
        call['result']['browser']['assertionsPassed']=len(call['result']['browser']['assertions'])-1
        local.run['calls'].append(call);audit=copy.deepcopy(call)
        local.run['memoryEnabled']=False
        local.app._progress_receipt(local.run,local.task)
        tail=local.task['_messages'][-1]
        self.assertTrue(tail['content'].startswith('测试诊断回执：'))
        packet=json.loads(tail['content'].split('：',1)[1])
        self.assertEqual(packet['callId'],call['id']);self.assertEqual(packet['failedAssertions'][0]['actual'],'Cancellation aborted.')
        self.assertIn('not proof',packet['instruction']);self.assertEqual(local.run['calls'][0],audit)
        local.app._review(local.run,local.task,{'taskId':local.target['id'],'verdict':'rework',
                          'feedback':'Inspect the actual test setup and requested interaction.','evidenceCallIds':[call['id']]})
        local.app._prepare_task('group',local.run['id'],local.target)
        context=json.loads(local.target['_messages'][1]['content']);diagnostic=context['task']['reworkDiagnostics']
        self.assertEqual(diagnostic['lastTest']['callId'],call['id']);self.assertEqual(diagnostic['targetRevision'],0)
        self.assertEqual(local.target['revision'],1)
        self.assertEqual(local.run['calls'][0],audit,'failed evidence and tested source bytes are never rewritten')


class StageModel:
    def __init__(self,close=False):self.app=None;self.actions={};self.messages=[];self.close=close
    def complete(self,messages,max_tokens=None,deadline=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            value={'tasks':[{'id':'notes','title':'Research notes','agentId':'coordinator','outputContract':{'kind':'artifact','extensions':['.md'],'minCount':1}},
                            {'id':'use','title':'Use real notes','agentId':'executor','dependsOn':['notes']}]}
        else:
            self.messages.append(copy.deepcopy(messages));context=json.loads(messages[1]['content']);title=context['task']['title']
            index=self.actions.get(title,0);self.actions[title]=index+1
            run=self.app.state['sessions']['group']['run'];task=next(row for row in run['steps'] if row['id']==context['task']['id'])
            if title=='Research notes':
                if index==0:
                    now=clock_ms();task['stageControl']['synthesisAt']=now-1
                    if self.close:task['stageControl']['handoffAt']=now-1
                    value={'type':'tool','name':'web.search','args':{'query':'MUST NOT ACCESS NETWORK'}}
                elif self.close:value={'type':'final','status':'done','text':'No source artifact actually produced.'}
                else:value=[None,{'type':'tool','name':'file.write','args':{'path':'notes.md','content':'Actual scripted source note with explicit gaps.'}},
                            {'type':'publish_artifact','path':'notes.md'},{'type':'final','status':'done','text':'Actual source note published.'}][index]
            else:
                value=({'type':'tool','name':'file.read','args':{'path':context['authorizedArtifacts'][0]['path']}} if index==0 else
                       {'type':'final','status':'done','text':'Read the actual authorized notes; no semantic acceptance claimed.'})
        return {'content':json.dumps(value),'finish_reason':'stop','usage':{'total_tokens':2}}


class StageRuntimeTests(unittest.TestCase):
    def test_real_review_rework_after_old_checkpoint_can_create_test_publish_and_retarget_qa(self):
        from tests.test_rework_delivery_loop import ReworkModel,LocalBrowser
        class LateReworkModel(ReworkModel):
            def complete(self,messages,max_tokens=None,deadline=None):
                response=super().complete(messages,max_tokens=max_tokens)
                action=json.loads(response['content'])
                if action.get('type')=='review' and action.get('verdict')=='rework':
                    run=self.app.state['sessions']['group']['run'];self.original_deadline=run['budget']['deadlineAt']
                    target=next(row for row in run['steps'] if row['id']==action['taskId'])
                    now=clock_ms();target['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-700,
                                                               handoffAt=now-500,expectedHandoffBy=now-100)
                return response
        class DeadlineBrowser(LocalBrowser):
            def execute(self,agent,run,name,args,approved=False,deadline=None):
                return super().execute(agent,run,name,args,approved)
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        config=Config(Path(directory.name),{'LLM_API_KEY':'offline-real-rework','MAX_RETRIES':'1'})
        config.apply_time_only_profile();model=LateReworkModel();app=Platform(config,provider=model,tools=DeadlineBrowser(config));model.app=app
        self.addCleanup(lambda:(app.shutdown(),app.wait_for_idle(3),app.repo.close()))
        app.action({'action':'send','mode':'task','participants':['coordinator','research','executor','reviewer'],
                    'text':'Deliver the actual independently tested second revision.'})
        # Keep the original revision/evidence assertions; allow slower hosts
        # time to persist and complete the asynchronous rework sequence.
        self.assertTrue(app.wait_for_idle(60));run=app.state['sessions']['group']['run']
        self.assertEqual(run['status'],'done',run.get('summary'));target=next(row for row in run['steps'] if row['title']=='Build')
        self.assertEqual(target['revision'],1);self.assertEqual(target['stageControl']['revision'],1)
        self.assertEqual(target['stageControlHistory'][0]['revision'],0)
        self.assertLess(target['stageControlHistory'][0]['control']['expectedHandoffBy'],target['stageControl']['allocatedAt'])
        self.assertEqual(run['budget']['deadlineAt'],model.original_deadline)
        self.assertEqual(target['validation']['targetRevision'],1);self.assertTrue(target['validation']['verified'])
        self.assertTrue(any(call['tool']=='file.write' and call.get('revision')==1 and call['status']=='success' for call in run['calls']))
        self.assertTrue(any(call['tool']=='code.test' and call.get('reviewTargets',{}).get(target['id'],{}).get('targetRevision')==1
                            and call['status']=='success' for call in run['calls']))
        self.assertTrue(run['deliveries']);self.assertLessEqual(target['stageControl']['expectedHandoffBy'],model.original_deadline)

    def execute(self,close=False):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        config=Config(Path(directory.name),{'LLM_API_KEY':'offline-stage','MAX_RETRIES':'0'});config.apply_time_only_profile()
        model=StageModel(close);app=Platform(config,provider=model,tools=ToolGateway(config));model.app=app
        self.addCleanup(lambda:(app.shutdown(),app.wait_for_idle(3),app.repo.close()))
        app.action({'action':'send','mode':'task','participants':['coordinator','executor'],'text':'Create a real note and pass it to another selected role.'})
        self.assertTrue(app.wait_for_idle(4));return app,model,app.state['sessions']['group']['run']

    def test_acquisition_cutoff_runs_real_write_publish_and_authorized_downstream_without_extra_caps(self):
        app,model,run=self.execute();self.assertEqual(run['status'],'done')
        self.assertFalse(any(call['tool'].startswith('web.') for call in run['calls']))
        self.assertTrue(any(call['tool']=='file.write' and call['status']=='success' for call in run['calls']))
        self.assertTrue(any(call['tool']=='file.read' and call['agentId']=='executor' and call['status']=='success' for call in run['calls']))
        self.assertEqual(run['modelCalls'],7);self.assertEqual(len(run['artifacts']),1)
        self.assertTrue(any(event['type']=='stage_action_blocked' for event in run['events']))
        self.assertIsNone(run['budget']['tokens']);self.assertIsNone(run['budget']['modelCalls']);self.assertEqual(run['budget']['timeMs'],1800000)
        self.assertEqual(app.repo.get_run(run['id'])['steps'][1]['stageControl'],run['steps'][1]['stageControl'])

    def test_final_checkpoint_does_not_turn_missing_artifact_into_done_or_run_downstream(self):
        app,model,run=self.execute(close=True)
        task=next(row for row in run['steps'] if row.get('title')=='Research notes')
        child=next(row for row in run['steps'] if row.get('title')=='Use real notes')
        self.assertEqual(task['status'],'partial');self.assertEqual(task['outputValidation']['status'],'failed')
        self.assertEqual(task['error']['code'],'output_contract');self.assertEqual(child['status'],'skipped')
        self.assertEqual(run['artifacts'],[]);self.assertFalse(any(call['tool'].startswith('web.') for call in run['calls']))
        self.assertNotEqual(run['status'],'done')


if __name__=='__main__':unittest.main()
