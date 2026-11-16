"""Real R10 call replay plus scoped synthetic boundary and Platform tests.

Synthetic observations are explicit offline fixtures, never product acceptance.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from backend.browser_stagnation import (browser_stagnation_feedback, browser_assertion_semantics,
                                        _failed_check, _successful_checks)
from backend.stage_control import latest_test_diagnostic
from backend.config import Config
from backend.engine import Platform
from tests.test_browser_stagnation import ExecutedPrefixFixtureTools
from tests.test_time_only_runtime import Model

DATA = json.loads((Path(__file__).parent/'fixtures/r10_browser_interleaved.json').read_text())

def fixture(name='interleaved'):
    d=copy.deepcopy(DATA[name]);return d['run'],d['task'],d['proposedCall']

def action(call): return {'type':'tool','name':'code.test','args':copy.deepcopy(call['args'])}

def passed_copy(call, identifier):
    """Synthetic host report resolves exactly its prior failing scenario."""
    c=copy.deepcopy(call);c['id']=identifier;c['status']='success';result=c['result'];result.update(ok=True,exitCode=0,error=None)
    report=result['browser'];report['ok']=True;report.pop('failureObservation',None)
    result['test']=True;report['assertionCount']=len(report['assertions']);report['assertionsPassed']=len(report['assertions'])
    c['args']['checks']=c['args']['checks'][:len(report['steps'])]
    for row in report['steps']+report['assertions']:
        if row.get('passed') is False:row.update(passed=True,actual=copy.deepcopy(row['expected']))
        if 'ok' in row:row['ok']=True
    for k in ('error','pageErrors','httpErrors','blockedRequests'):report.pop(k,None)
    return c


class ScopedBrowserRecoveryTests(unittest.TestCase):
    def test_actual_r10_unrelated_passes_preserve_unresolved_failure(self):
        run,task,proposal=fixture();before=copy.deepcopy((run,task,proposal))
        passed=[c for c in run['calls'] if _successful_checks(c)]
        self.assertGreater(len(passed),1)
        result=browser_stagnation_feedback(run,task,action(proposal))
        self.assertEqual(result['observedFailures']['count'],2)
        self.assertEqual(result['observedFailures']['callIds'],['call-f6021279f8894e32','call-17f82b4c2b034faf'])
        self.assertTrue(result['notExecuted']);self.assertFalse(result['actionExecuted'])
        self.assertFalse(result['rejectionScope']['unrelatedPassingChecksResolveThisFailure'])
        self.assertEqual((run,task,proposal),before)

    def test_passing_its_own_exact_scenario_only_retires_that_group(self):
        run,task,proposal=fixture();a=run['calls'][0]
        # Independent second failure group from real R10, repeated synthetically.
        b=copy.deepcopy(run['calls'][1]);b2=copy.deepcopy(b);b2['id']='offline-b-repeat'
        run['calls']=[a,copy.deepcopy(a),b,b2];run['calls'][1]['id']='offline-a-repeat'
        resolution=passed_copy(a,'offline-exact-pass');self.assertIsNotNone(_successful_checks(resolution))
        run['calls'].append(resolution)
        self.assertIsNone(browser_stagnation_feedback(run,task,action(a)))
        self.assertIsNotNone(browser_stagnation_feedback(run,task,action(b)))
        self.assertFalse(any(c.get('result',{}).get('modelViewOnly') for c in run['calls']))

    def test_true_resolution_starts_new_failure_episode_without_inherited_rejections(self):
        run,task,_=fixture();a=run['calls'][0];a2=copy.deepcopy(a);a2['id']='repeat-a';run['calls']=[a,a2]
        old=browser_stagnation_feedback(run,task,action(a))
        run['events']=[{'id':str(i),'type':'browser_failure_recovery','agentId':task['agentId'],'payload':{'taskId':task['id'],'failureSignature':old['failureSignature']}} for i in range(2)]
        run['calls'].append(passed_copy(a,'resolved'))
        for i in range(2):c=copy.deepcopy(a);c['id']='new-failure-'+str(i);run['calls'].append(c)
        new=browser_stagnation_feedback(run,task,action(a))
        self.assertNotEqual(new['failureSignature'],old['failureSignature'])
        self.assertEqual(new['recovery']['blockedProposalCount'],1)
        self.assertFalse(new['recovery']['stopTask'])

    def test_success_report_binding_negative_cases_and_conservative_boundaries(self):
        run,task,_=fixture();original=next(c for c in run['calls'] if _successful_checks(c))
        modes=('timedOut','actionExecuted','wrong_entry','boolean_exit','missing_step','wrong_selector','wrong_expected','assertion_mismatch','missing_manifest','browser_error','page_error','false_executed','wrong_runtime','not_test','missing_test','wrong_count','wrong_pass_count','boolean_count','missing_count')
        for mode in modes:
            c=copy.deepcopy(original);r=c['result'];b=r['browser']
            if mode=='timedOut':r['timedOut']=True
            elif mode=='actionExecuted':r['actionExecuted']=False
            elif mode=='wrong_entry':b['entry']['path']='foreign.html'
            elif mode=='boolean_exit':r['exitCode']=False
            elif mode=='missing_step':b['steps'].pop()
            elif mode=='wrong_selector':b['steps'][0]['selector']='#foreign'
            elif mode=='wrong_expected':next(s for s in b['steps'] if s['action'].startswith('assert_'))['expected']='foreign'
            elif mode=='assertion_mismatch':b['assertions'][0]['actual']='foreign'
            elif mode=='missing_manifest':b.pop('sourceManifest')
            elif mode=='browser_error':b['error']='timeout'
            elif mode=='page_error':b['pageErrors']=['error']
            elif mode=='false_executed':b['executed']=False
            elif mode=='wrong_runtime':r['runtime']='python'
            elif mode=='not_test':r['test']=False
            elif mode=='missing_test':r.pop('test')
            elif mode=='wrong_count':b['assertionCount']+=1
            elif mode=='wrong_pass_count':b['assertionsPassed']-=1
            elif mode=='boolean_count':b['assertionCount']=True
            elif mode=='missing_count':b.pop('assertionsPassed')
            self.assertIsNone(_successful_checks(c),mode)
        for mode in ('file.patch','revision','source','pending','other_run'):
            r,t,p=fixture()
            if mode=='file.patch':r['calls'].append({'id':'mutate','agentId':t['agentId'],'tool':'file.patch'})
            elif mode=='revision':t['revision']+=1
            elif mode=='source':
                c=copy.deepcopy(original);c['id']='new-version';b=c['result']['browser'];path=b['entry']['path'];b['entry']['sha256']='f'*64
                for key in ('sourceManifest','testedFiles'):
                    for item in b[key]:
                        if item['path']==path:item['sha256']='f'*64
                r['calls'].append(c)
            elif mode=='pending':r['steps']=[{**t,'_pendingTool':{'name':'file.write'}}]
            else:r['calls'][-1]['runId']='foreign'
            self.assertIsNone(browser_stagnation_feedback(r,t,action(p)),mode)

    def test_corrected_expectation_or_preparation_never_blocked_or_edited(self):
        run,task,p=fixture();proposal=action(p);found=browser_stagnation_feedback(run,task,proposal);index=found['rejectionScope']['proposalFailedCheckIndex']
        proposal['args']['checks'][index]['value']='a corrected requirement-derived full value'
        before=copy.deepcopy(proposal);self.assertIsNone(browser_stagnation_feedback(run,task,proposal));self.assertEqual(proposal,before)
        proposal=action(p);proposal['args']['checks'].insert(0,{'action':'reload'})
        self.assertIsNone(browser_stagnation_feedback(run,task,proposal))

    def test_actual_dynamic_literal_reuse_is_advisory_not_product_classification(self):
        run,task,current=fixture('literal');run['calls'].append(current);before=copy.deepcopy(run)
        result=browser_assertion_semantics(run,task,current)
        self.assertEqual(result['comparison'],'exact_stored_string')
        self.assertEqual(result['previousActualReusedAsExpected']['callId'],'call-57444f9c25a646c1')
        self.assertEqual(result['cause'],'unassessed');self.assertFalse(result['productDefectEstablished'])
        self.assertTrue(result['failurePreserved']);self.assertFalse(result['expectationChanged'])
        self.assertIsNone(browser_stagnation_feedback({**run,'calls':run['calls'][:-1]},task,action(current)))
        self.assertEqual(latest_test_diagnostic(run,task)['assertionSemantics'],result)
        self.assertEqual(run,before)

    def test_literal_reuse_not_claimed_across_source_mutation_task_or_setup_changes(self):
        for mode in ('file.patch','revision','task','preparation','source','duplicate'):
            run,task,current=fixture('literal');prior=run['calls'][0]
            if mode=='file.patch':run['calls'].append({'id':'mutate','agentId':task['agentId'],'tool':'file.patch','status':'success'})
            elif mode=='revision':prior['revision']+=1
            elif mode=='task':prior['taskId']='other'
            elif mode=='preparation':prior['args']['storage']={'known':'different'}
            elif mode=='source':
                b=prior['result']['browser'];path=b['entry']['path'];b['entry']['sha256']='f'*64
                for key in ('sourceManifest','testedFiles'):
                    for item in b[key]:
                        if item['path']==path:item['sha256']='f'*64
            else:run['calls'].append(copy.deepcopy(current))
            run['calls'].append(current);result=browser_assertion_semantics(run,task,current)
            self.assertNotIn('previousActualReusedAsExpected',result,mode)
            self.assertFalse(result['productDefectEstablished'])

    def test_unknown_effects_and_pending_mutations_do_not_gain_literal_reuse_diagnosis(self):
        for mode in ('nested_unknown', 'pending', 'bad_run'):
            run,task,current=fixture('literal');run['calls'].append(current)
            if mode=='nested_unknown':run['calls'][0]['transport']={'outcomeUnknown':True}
            elif mode=='pending':run['steps']=[{**task,'_pendingTool':{'name':'file.write'}}]
            else:run['id']=None
            self.assertIsNone(browser_assertion_semantics(run,task,current),mode)

    def test_storage_absence_and_ordinary_mismatches_preserve_uncertainty(self):
        run,task,p=fixture();missing=copy.deepcopy(DATA['missingStorage'])
        detail=browser_assertion_semantics(run,task,missing)
        self.assertEqual(detail['comparison'],'key_absence_requires_null');self.assertFalse(detail['productDefectEstablished'])
        run,task,current=fixture('literal');run['calls'].append(current)
        prior=run['calls'][0];detail=browser_assertion_semantics(run,task,prior)
        self.assertNotIn('previousActualReusedAsExpected',detail);self.assertFalse(detail['productDefectEstablished'])
        current['result']['browser']['entry']['path']='foreign.html'
        self.assertIsNone(browser_assertion_semantics(run,task,current))


class CountedExecutedPrefixTools(ExecutedPrefixFixtureTools):
    def execute(self, agent, run, name, args, approved=False, deadline=None):
        result=super().execute(agent,run,name,args,approved,deadline)
        if name=='code.test':
            result['test']=True;report=result['browser'];report['assertionCount']=len(report['assertions'])
            report['assertionsPassed']=sum(a['passed'] is True for a in report['assertions'])
        return result


class InterleavedPlatformTests(unittest.TestCase):
    def test_actual_model_context_receives_advisory_without_changing_failed_tools(self):
        class StorageFixture(ExecutedPrefixFixtureTools):
            observed = 0
            def execute(self, agent, run, name, args, approved=False, deadline=None):
                if name != 'code.test':return super().execute(agent,run,name,args,approved,deadline)
                self.observed += 1
                body=super().execute(agent,run,'file.read',{'path':args['path']},approved,deadline)
                entry={'path':args['path'],'sha256':body['sha256']}
                row={'action':'assert_storage','pageId':'main','key':'records','expected':args['checks'][0]['value'],
                     'actual':json.dumps([{'generated':self.observed}],separators=(',',':')),'passed':False}
                return {'ok':False,'runtime':'browser','exitCode':1,'error':'Offline storage assertion fixture',
                        'browser':{'engine':'chromium','executed':True,'ok':False,'entry':entry,'testedFiles':[entry],
                                   'sourceManifest':[entry],'steps':[{**row,'ok':False}],'assertions':[row]}}
        with tempfile.TemporaryDirectory() as temp:
            config=Config(Path(temp),{'LLM_API_KEY':'offline-storage','MAX_RETRIES':'0'});config.apply_time_only_profile();seen=[];counter=[0]
            def next_action(n,context,messages):
                n=counter[0];counter[0]+=1
                if n==0:return {'type':'tool','name':'file.write','args':{'path':'app/index.html','content':'<h1>Offline fixture</h1>'}}
                if n in (1,2):
                    return {'type':'tool','name':'code.test','args':{'language':'browser','path':'app/index.html',
                            'checks':[{'action':'assert_storage','key':'records','value':'[]' if n==1 else '[{"generated":1}]'}]}}
                if n==3:
                    seen.append(context['lastTestDiagnostic']['assertionSemantics'])
                    return {'type':'final','status':'partial','text':'Exact storage assertions failed in the offline fixture. No product defect or acceptance inferred.'}
                return {'type':'final','status':'partial','text':'Unresolved fixture failures retained.'}
            class StoragePlan(Model):
                def complete(self,messages,max_tokens=None,deadline=None):
                    if messages[0]['content'].startswith('Plan an executable task DAG'):
                        return {'content':json.dumps({'tasks':[{'id':'work','title':'Work','agentId':'executor'}]}),'finish_reason':'stop','usage':{'total_tokens':1}}
                    return super().complete(messages,max_tokens,deadline)
            model=StoragePlan(next_action);app=Platform(config,provider=model,tools=StorageFixture(config));model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['executor'],'text':'Inspect a fixture and report uncertainty honestly.'});self.assertTrue(app.wait_for_idle(8))
                run=app.state['sessions']['group']['run'];tests=[c for c in run['calls'] if c['tool']=='code.test']
                self.assertEqual(len(tests),2,repr([(c['tool'],c['status'],c.get('error')) for c in run['calls']])+repr([(t['status'],t.get('error')) for t in run['steps']]));self.assertTrue(all(c['status']=='failed' for c in tests))
                self.assertEqual(tests[1]['args']['checks'][0]['value'],'[{"generated":1}]')
                self.assertEqual(len(seen),1);self.assertEqual(seen[0]['previousActualReusedAsExpected']['callId'],tests[0]['id'])
                self.assertFalse(seen[0]['productDefectEstablished']);self.assertFalse(seen[0]['expectationChanged'])
                self.assertFalse(run['artifacts'])
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()

    def test_real_platform_interleaved_recovery_then_publish_and_authorized_review(self):
        with tempfile.TemporaryDirectory() as temp:
            config=Config(Path(temp),{'LLM_API_KEY':'offline-scoped','MAX_RETRIES':'0'});config.apply_time_only_profile();counts={'Build':0,'Check':0}
            def test(path,wrong=False,irrelevant=False):
                return {'type':'tool','name':'code.test','args':{'language':'browser','path':path,'checks':[{'action':'assert_text','selector':'#unrelated' if irrelevant else 'h1','text':'unchanged' if irrelevant else 'Wrong' if wrong else 'Real heading'}]}}
            def next_action(n,context,messages):
                name=context['task']['title'];i=counts[name];counts[name]+=1
                if name=='Check':
                    if i==0:return test(context['authorizedArtifacts'][0]['path'])
                    return {'type':'final','text':'Independent offline fixture inspected authorized bytes; semantic product acceptance unassessed.'}
                if i==0:return {'type':'tool','name':'file.write','args':{'path':'app/index.html','content':'<h1>Real heading</h1>'}}
                if i in (1,3,5):return test('app/index.html',wrong=True)
                if i in (2,4):return test('app/index.html',irrelevant=True)
                if i==6:
                    self.assertTrue(any('same_failing_check_and_stateful_setup' in m['content'] for m in messages));return test('app/index.html')
                if i==7:return {'type':'publish_artifact','path':'app/index.html'}
                return {'type':'final','text':'Actual source published with explicit offline fixture limitation.'}
            class Plan(Model):
                def complete(self,messages,max_tokens=None,deadline=None):
                    if messages[0]['content'].startswith('Plan an executable task DAG'):
                        return {'content':json.dumps({'tasks':[{'id':'b','title':'Build','agentId':'executor','outputContract':{'kind':'artifact','extensions':['.html'],'minCount':1}},{'id':'c','title':'Check','agentId':'reviewer','dependsOn':['b']}]}),'finish_reason':'stop','usage':{'total_tokens':1}}
                    return super().complete(messages,max_tokens,deadline)
            model=Plan(next_action);app=Platform(config,provider=model,tools=CountedExecutedPrefixTools(config));model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['executor','reviewer'],'text':'Build and independently inspect published source.'});self.assertTrue(app.wait_for_idle(10));run=app.state['sessions']['group']['run']
                build=next(t for t in run['steps'] if t['title']=='Build');qa=next(t for t in run['steps'] if t['title']=='Check')
                self.assertEqual((build['status'],qa['status']),('done','done'))
                actual=[c for c in run['calls'] if c['tool']=='code.test' and c['taskId']==build['id']]
                self.assertEqual([c['status'] for c in actual],['failed','success','failed','success','success'])
                events=[e for e in run['events'] if e['type']=='browser_failure_recovery'];self.assertEqual(len(events),1)
                self.assertEqual(len(events[0]['payload']['observedCallIds']),2);self.assertEqual(len(run['artifacts']),1)
                self.assertFalse(build.get('validation',{}).get('verified'));self.assertFalse(run.get('budgetExtensions'))
                self.assertTrue(all(run['budget'][k] is None for k in ('tokens','modelCalls','agentTurns')))
                qa_call=next(c for c in run['calls'] if c['tool']=='code.test' and c['taskId']==qa['id']);self.assertTrue(qa_call['args']['path'].startswith('imports/'))
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()

if __name__=='__main__':unittest.main()
