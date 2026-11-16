"""Historical cross-version failures are observations, never a product verdict."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.browser_failure_recovery import browser_failure_trend, browser_failure_feedback
from backend.config import Config
from backend.engine import Platform
from backend.runtime import _compact_progress_receipt
from backend.teams import get_team_agents


class BrowserFailureTrendTests(unittest.TestCase):
    def setUp(self):
        self.fixture=json.loads((Path(__file__).parent/'fixtures/r7_repeated_browser_failure.json').read_text())
        self.run=self.fixture['run'];self.task=self.fixture['task']

    def test_actual_five_matching_tests_across_three_versions_do_not_diagnose_or_block(self):
        original=copy.deepcopy(self.run)
        result=browser_failure_trend(self.run,self.task)
        self.assertEqual(result['observedFailureCount'],5)
        self.assertEqual(result['testedSourceVersions'],3)
        self.assertEqual(result['latestFailedAssertion'],{
            'action':'assert_count','pageId':'main','selector':'td.free','expected':120,'actual':0,'passed':False})
        self.assertIn('dateInput=2026-11-15',result['latestFailureObservation']['visibleFormValues'])
        self.assertEqual(result['cause'],'unassessed')
        self.assertEqual(result['currentUntestedChanges'],'unassessed')
        self.assertFalse(any(key in result for key in ('ok','verdict','accepted','actionExecuted','notExecuted')))
        action={'type':'tool','name':'code.test','args':copy.deepcopy(self.run['calls'][-1]['args'])}
        self.assertIsNone(browser_failure_feedback(self.run,self.task,action),
                          'the new-version test stays executable; one failed test on latest sources')
        self.assertEqual(self.run,original)

    def test_changed_test_setup_assertion_and_success_each_reset_the_pattern(self):
        for mode in ('date_setup','expectation','success','other_runtime'):
            with self.subTest(mode=mode):
                run=copy.deepcopy(self.run);last=run['calls'][-1]
                if mode=='date_setup':last['args']['checks'].insert(0,{'action':'fill','selector':'#dateInput','value':'2026-11-17'})
                elif mode=='expectation':last['result']['browser']['assertions'][-1]['expected']=0
                elif mode=='success':
                    last['status']='success';last['result']['ok']=True;last['result']['exitCode']=0;last['result']['browser']['ok']=True
                else:last['args']['language']='python'
                self.assertIsNone(browser_failure_trend(run,self.task))

    def test_scope_unknown_effects_duplicate_ids_and_unverified_reports_do_not_gain_trend(self):
        for mode in ('owner','task','revision','missing_revision','unknown','nested_unknown','duplicate','manifest','not_executed','different_run'):
            with self.subTest(mode=mode):
                run=copy.deepcopy(self.run);task=copy.deepcopy(self.task)
                if mode=='owner':task['agentId']='other'
                elif mode=='task':task['id']='other'
                elif mode=='revision':task['revision']=1
                elif mode=='missing_revision':task.pop('revision')
                elif mode=='unknown':run['steps'][0]['_pendingTool']={'id':'pending'}
                elif mode=='nested_unknown':run['calls'][-1]['result']['error']={'outcomeUnknown':'yes'}
                elif mode=='duplicate':run['calls'][-1]['id']=run['calls'][-2]['id']
                elif mode=='manifest':run['calls'][-1]['result']['browser']['sourceManifest'][0]['sha256']='invalid'
                elif mode=='different_run':run['calls'][-1]['runId']='different-run'
                else:run['calls'][-1]['result']['notExecuted']=True
                self.assertIsNone(browser_failure_trend(run,task))

    def test_same_source_only_is_left_to_existing_unchanged_source_guard(self):
        run=copy.deepcopy(self.run);last=copy.deepcopy(run['calls'][-1]);run['calls']=[]
        for index in range(4):
            row=copy.deepcopy(last);row['id']='same-version-'+str(index);run['calls'].append(row)
        self.assertIsNone(browser_failure_trend(run,self.task))
        action={'type':'tool','name':'code.test','args':copy.deepcopy(last['args'])}
        self.assertEqual(browser_failure_feedback(run,self.task,action)['error']['code'],'repeated_browser_assertion_failure')

    def test_changing_never_loaded_manifest_resources_does_not_count_as_new_tested_bytes(self):
        run=copy.deepcopy(self.run);last=copy.deepcopy(run['calls'][-1]);run['calls']=[]
        for index in range(4):
            row=copy.deepcopy(last);row['id']='unloaded-change-'+str(index)
            row['result']['browser']['sourceManifest'].append({'path':'never-loaded.js','sha256':str(index)*64})
            run['calls'].append(row)
        self.assertIsNone(browser_failure_trend(run,self.task))

    def test_later_private_edit_is_not_claimed_as_a_tested_or_failing_current_version(self):
        run=copy.deepcopy(self.run)
        run['calls'].append({'id':'later-mutation','taskId':self.task['id'],'agentId':self.task['agentId'],
            'revision':self.task['revision'],'tool':'file.patch','status':'success',
            'args':{'path':'ui.js'},'result':{'ok':True,'sha256':'f'*64}})
        result=browser_failure_trend(run,self.task)
        self.assertEqual(result['observedFailureCount'],5)
        self.assertEqual(result['testedSourceVersions'],3)
        self.assertEqual(result['currentUntestedChanges'],'unassessed')
        self.assertEqual(result['scope'],'historical_actual_tests_only')
        self.assertNotIn('later-mutation',[row['callId'] for row in result['recentTests']])

    def test_minimal_progress_projection_preserves_actual_bound_observation_and_no_source_body(self):
        result=browser_failure_trend(self.run,self.task)
        receipt={'turn':22,'privateFiles':[],'fileReads':[],'completion':'not_assessed','browserFailureTrend':result}
        original=copy.deepcopy(receipt);view=_compact_progress_receipt(receipt)
        self.assertEqual(view['browserFailureTrend'],result)
        self.assertEqual(receipt,original)
        self.assertLess(len(json.dumps(view,ensure_ascii=False)),4000)
        self.assertNotIn('content',result)

    def test_real_progress_path_exposes_history_without_creating_calls_outputs_or_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Config(Path(directory),{'LLM_API_KEY':'offline-observation','MAX_AGENT_TURNS':'60'})
            app=Platform(config,agents=get_team_agents('software'),team_id='software')
            try:
                run=copy.deepcopy(self.run);task=run['steps'][0]
                task.update(title='Actual repeated browser checks',status='running',turns=22,outputs=[],childIds=[],
                            _messages=[{'role':'system','content':'Preserved execution protocol.'},
                                       {'role':'user','content':json.dumps({'task':{'id':task['id']}})}])
                run.update(mode='task',status='running',channelId='group',teamId='software',artifacts=[],events=[],transcript=[],
                           budget={'timeMs':1800000,'agentTurns':60},activeMs=0)
                original_calls=copy.deepcopy(run['calls'])
                app._progress_receipt(run,task)
                actual=json.loads(task['_messages'][1]['content'])['progressReceipt']['browserFailureTrend']
                self.assertEqual(actual['observedFailureCount'],5)
                self.assertEqual(actual['testedSourceVersions'],3)
                self.assertEqual(actual,task['progress']['browserFailureTrend'])
                self.assertEqual(actual['cause'],'unassessed')
                self.assertEqual(run['calls'],original_calls)
                self.assertEqual(run['artifacts'],[])
                self.assertEqual(task['status'],'running')
                self.assertEqual(run['budget'],{'timeMs':1800000,'agentTurns':60})
                self.assertFalse(any(event['type'] in ('artifact_registered','review_pass','task_accepted') for event in run['events']))
            finally:
                app.shutdown();app.wait_for_idle(3);app.repo.close()


if __name__=='__main__':unittest.main()
