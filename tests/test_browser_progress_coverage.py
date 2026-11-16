"""Offline browser-report fixtures exercise progress, never product acceptance."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.agents import AGENTS
from backend.config import Config
from backend.engine import Platform, clock_ms
from backend.runtime import compress_messages


class NoTools:
    def capabilities(self):return {}
    def definitions(self, actor):return []
    def cancel(self, run_id):pass


class BrowserProgressCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.apps=[]
        self.config=Config(Path(self.temp.name),{'LLM_API_KEY':'offline-only'})
        self.config.apply_time_only_profile()
        self.app=self.new_app()
        self.target=self.app._new_task('Product','Deliver','human',list(AGENTS),'executor')
        self.target.update(status='done',outputs=['html','script'])
        self.task=self.app._new_task('QA','Review criteria','human',list(AGENTS),'reviewer')
        self.task.update(status='running',dependsOn=[self.target['id']],turns=1)
        self.task['_messages']=[{'role':'system','content':'Original role and protocol.'},
            {'role':'user','content':json.dumps({'runGoal':'Review actual product','task':{'id':self.task['id']}})},
            {'role':'user','content':'动作的真实结果：{"ok":true,"actualToolResult":true}'}]
        self.run={'id':'progress-browser-run','status':'running','mode':'task','title':'Actual product QA',
            'participants':list(AGENTS),'respondingAgentId':'coordinator','steps':[self.target,self.task],
            'calls':[],'events':[],'messages':[],'approvals':[],'artifacts':[
                {'id':'html','runId':'progress-browser-run','taskId':self.target['id'],'ownerAgentId':'executor',
                 'name':'index.html','path':'index.html','sha256':'a'*64},
                {'id':'script','runId':'progress-browser-run','taskId':self.target['id'],'ownerAgentId':'executor',
                 'name':'app.js','path':'app.js','sha256':'b'*64}],
            'budget':{'profile':'time_only','deadlineAt':clock_ms()+1800000,'agentTurns':None,'modelCalls':None,'tokens':None},
            'modelCalls':0,'tokenUsage':0,'activeMs':0,'memoryHits':[]}
        self.app.state['sessions']['group']['run']=self.run

    def new_app(self):
        app=Platform(self.config,tools=NoTools());self.apps.append(app);return app

    def tearDown(self):
        for app in self.apps:
            if not app.closing:app.shutdown()
            app.repo.close()
        self.temp.cleanup()

    def call(self,checks=None):
        checks=checks or [{'action':'fill','selector':'#name','value':'Alice'},
            {'action':'assert_visible','selector':'#rooms'},
            {'action':'assert_count','selector':'.slot','count':10}]
        refs=[{'path':'imports/'+a['id']+'/'+a['name'],'sha256':a['sha256']} for a in self.run['artifacts']]
        args={'language':'browser','path':refs[0]['path'],'resources':{'app.js':refs[1]['path']},'checks':copy.deepcopy(checks)}
        assertions=[];steps=[]
        for check in checks:
            record={'action':check['action'],'pageId':check.get('pageId','main')}
            record.update({k:check[k] for k in ('selector','key','name') if k in check})
            if check['action'].startswith('assert_'):
                expected={'assert_visible':True,'assert_storage_missing':None}.get(check['action'],
                    next((check[k] for k in ('count','text','value') if k in check),None))
                record.update(expected=expected,actual=expected,passed=True)
                assertions.append(copy.deepcopy(record))
            steps.append({**record,'ok':True})
        return {'id':'call-'+str(len(self.run['calls'])),'taskId':self.task['id'],'agentId':self.task['agentId'],
            'revision':self.task['revision'],'tool':'code.test','status':'success','args':args,
            'reviewTargets':{self.target['id']:self.app._review_snapshot(self.run,self.target)},
            'result':{'runtime':'browser','test':True,'ok':True,'exitCode':0,'browser':{
                'engine':'chromium','executed':True,'ok':True,'entry':refs[0],'sourceManifest':refs,
                'testedFiles':copy.deepcopy(refs),'assertions':assertions,'steps':steps,
                'assertionCount':len(assertions),'assertionsPassed':len(assertions),
                'pageErrors':[],'httpErrors':[],'blockedRequests':[]}}}

    def observe(self,call=None):
        if call is not None:self.run['calls'].append(call)
        self.task['turns']+=1
        self.app._progress_receipt(self.run,self.task)
        return json.loads(self.task['_messages'][1]['content'])['progressReceipt']

    def test_check_permutations_and_subsets_are_not_new_coverage_or_automatic_acceptance(self):
        first=self.call();r=self.observe(first)
        self.assertEqual(r['qaEvidence']['coverageCount'],2);self.assertEqual(r['turnsWithoutNewOutputOrTest'],0)
        for count,checks in enumerate(([first['args']['checks'][0],*reversed(first['args']['checks'][1:])],
                                      first['args']['checks'][:2],first['args']['checks']),1):
            r=self.observe(self.call(checks))
            self.assertEqual(r['qaEvidence']['coverageCount'],2)
            self.assertEqual(r['turnsWithoutNewOutputOrTest'],count)
            self.assertEqual(r['closingRequired'],count==3)
        self.assertIn('submit an explicit review',r['instruction'])
        self.assertEqual(self.target['status'],'done');self.assertNotIn('validation',self.target)
        self.assertEqual(self.task['status'],'running');self.assertEqual(self.task['outputs'],[])
        self.assertEqual(r['completion'],'not_assessed');self.assertEqual(r['qaEvidence']['semanticQuality'],'unassessed')

    def test_new_inputs_interactions_seed_or_source_are_actual_progress(self):
        original=self.call();self.observe(original);self.observe(self.call())
        for change in ('input','interaction','storage','source','assertion'):
            call=self.call()
            if change=='input':call['args']['checks'][0]['value']='Bob'
            elif change=='interaction':
                call=self.call([{'action':'reload'},*call['args']['checks']])
            elif change=='storage':call['args']['storage']={'bookings':'[]'}
            elif change=='source':
                self.run['artifacts'][1]['sha256']='c'*64;call=self.call()
            else:call=self.call([{'action':'assert_count','selector':'.new-slot','count':10}])
            r=self.observe(call)
            self.assertEqual(r['turnsWithoutNewOutputOrTest'],0,change)
            self.assertFalse(r['closingRequired'],change)

    def test_missing_old_bool_revision_foreign_actor_and_stale_producer_do_not_count(self):
        cases=['missing_revision','old_revision','bool_revision','foreign_actor','stale_target','bool_snapshot_revision','unknown_artifact']
        for case in cases:
            with self.subTest(case=case):
                call=self.call()
                if case=='missing_revision':call.pop('revision')
                elif case=='old_revision':call['revision']=-1
                elif case=='bool_revision':call['revision']=False
                elif case=='foreign_actor':call['agentId']='executor'
                elif case=='stale_target':call['reviewTargets'][self.target['id']]['targetRevision']=99
                elif case=='bool_snapshot_revision':call['reviewTargets'][self.target['id']]['targetRevision']=False
                else:call['result']['browser']['sourceManifest'][1]['sha256']='f'*64
                self.run['calls']=[call]
                self.assertEqual(self.app._browser_progress_coverage(self.run,self.task),(set(),{}))
        call=self.call();self.run['calls']=[call];self.target['revision']+=1
        self.assertFalse(self.app._browser_progress_coverage(self.run,self.task)[0])
        self.target['revision']=0;call=self.call();self.run['calls']=[call]
        for revision in (None,False):
            self.target['revision']=revision
            self.assertFalse(self.app._browser_progress_coverage(self.run,self.task)[0])

    def test_failed_unknown_incomplete_or_unexecuted_reports_are_not_browser_coverage(self):
        changes=[lambda c:c.update(status='failed'),lambda c:c['result'].update(ok=False),
            lambda c:c['result'].update(outcomeUnknown=True),lambda c:c['result'].update(deadlineExceeded=True),
            lambda c:c['result'].update(modelViewOnly='browser-tool-context-v1'),
            lambda c:c['result']['browser'].pop('sourceManifest'),lambda c:c['result']['browser'].update(executed=False),
            lambda c:c['result']['browser'].update(assertions=[]),lambda c:c['result']['browser'].update(steps=[]),
            lambda c:c['result']['browser'].update(testedFiles=c['result']['browser']['testedFiles'][:1]),
            lambda c:c['result']['browser']['assertions'][0].update(passed=False),
            lambda c:c['result']['browser']['steps'][0].update(ok=False),
            lambda c:c['result']['browser'].update(assertionsPassed=True),
            lambda c:c['result'].update(runtime='python'),lambda c:c['args'].update(resources=[])]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                call=self.call();change(call);self.run['calls']=[call]
                self.assertEqual(self.app._browser_progress_coverage(self.run,self.task),(set(),{}))

    def test_private_source_mutation_invalidates_coverage_until_matching_observation(self):
        self.run['artifacts']=[];self.target['outputs']=[]
        checks=[{'action':'assert_visible','selector':'#rooms'}]
        # A private source is proven by the trusted test's own frozen manifest;
        # later code may mutate it, even when that code reports failure.
        call={'id':'private-browser','taskId':self.task['id'],'agentId':self.task['agentId'],'revision':0,
            'tool':'code.test','status':'success','args':{'language':'browser','path':'index.html','checks':checks},
            'result':{'runtime':'browser','test':True,'ok':True,'exitCode':0,'browser':{
                'executed':True,'ok':True,'entry':{'path':'index.html','sha256':'a'*64},
                'testedFiles':[{'path':'index.html','sha256':'a'*64}],
                'sourceManifest':[{'path':'index.html','sha256':'a'*64}],
                'assertions':[{'action':'assert_visible','pageId':'main','selector':'#rooms','expected':True,'actual':True,'passed':True}],
                'steps':[{'action':'assert_visible','pageId':'main','selector':'#rooms','expected':True,'actual':True,'passed':True,'ok':True}],
                'assertionCount':1,'assertionsPassed':1}}}
        self.run['calls']=[call];self.assertTrue(self.app._browser_progress_coverage(self.run,self.task)[0])
        mutation={'id':'mutation','agentId':self.task['agentId'],'taskId':self.task['id'],'tool':'code.run',
            'args':{'language':'python'},'status':'failed','result':{'ok':False}}
        self.run['calls'].append(mutation);self.assertFalse(self.app._browser_progress_coverage(self.run,self.task)[0])
        self.run['calls'].append({'id':'read-current','agentId':self.task['agentId'],'taskId':self.task['id'],
            'tool':'file.read','args':{'path':'index.html'},'status':'success','result':{'ok':True,'sha256':'a'*64}})
        self.assertTrue(self.app._browser_progress_coverage(self.run,self.task)[0])
        self.run['calls'].append({'id':'unknown-write','agentId':self.task['agentId'],'taskId':self.task['id'],
            'tool':'file.write','args':{'path':'index.html'},'status':'unknown','result':None})
        self.assertFalse(self.app._browser_progress_coverage(self.run,self.task)[0])

    def test_summary_is_bounded_exact_bindings_or_explicit_omission_and_full_audit_unchanged(self):
        checks=[{'action':'assert_text','selector':'#'+str(i),'text':'Observed content '*100} for i in range(12)]
        call=self.call(checks);before=copy.deepcopy(call);r=self.observe(call)
        qa=r['qaEvidence'];self.assertLessEqual(len(json.dumps(qa,ensure_ascii=False)),2400)
        self.assertEqual(qa['coverageCount'],12);self.assertGreater(qa['omittedAssertions'],0)
        for ref in qa['sources']:self.assertEqual(len(ref['sha256']),64)
        self.assertEqual(call,before);self.assertEqual(self.run['modelCalls'],0)
        self.assertEqual(self.run['budget']['agentTurns'],None)
        self.assertEqual(self.run['budget']['tokens'],None);self.assertEqual(self.run['budget']['modelCalls'],None)
        self.assertEqual(self.task['_messages'][-1]['content'],'动作的真实结果：{"ok":true,"actualToolResult":true}')

    def test_restart_recomputes_same_coverage_and_preserves_deadline_and_stagnation(self):
        self.observe(self.call());self.observe(self.call());deadline=self.run['budget']['deadlineAt']
        self.app._persist();self.app.shutdown();self.app.repo.close();self.apps.remove(self.app)
        self.app=self.new_app();self.run=self.app.state['sessions']['group']['run']
        self.task=next(t for t in self.run['steps'] if t['agentId']=='reviewer');self.target=self.run['steps'][0]
        self.assertEqual(self.run['status'],'interrupted')
        prior_status=self.task['status']
        r=self.observe(self.call())
        self.assertEqual(r['qaEvidence']['coverageCount'],2);self.assertEqual(r['turnsWithoutNewOutputOrTest'],2)
        self.assertEqual(self.run['budget']['deadlineAt'],deadline)
        self.assertEqual(self.task['status'],prior_status,'progress alone must not turn a restored task into a started worker')
        self.assertEqual(self.run['status'],'interrupted')

    def test_generic_python_success_remains_separate_from_browser_evidence(self):
        self.run['calls']=[{'id':'python','taskId':self.task['id'],'agentId':self.task['agentId'],'revision':0,
            'tool':'code.test','status':'success','args':{'language':'python','code':'print(1)'},
            'result':{'ok':True,'runtime':'python','exitCode':0}}]
        r=self.observe();self.assertNotIn('qaEvidence',r)
        self.assertEqual(r['successfulTestCallIds'],['python']);self.assertEqual(r['completion'],'not_assessed')

    def test_compaction_preserves_bounded_qa_evidence_and_unassessed_semantics(self):
        original=self.observe(self.call())['qaEvidence']
        messages=copy.deepcopy(self.task['_messages'])
        messages.extend([{'role':'assistant','content':'Historical model prose. '*5000} for _ in range(3)])
        for limit in (40000,80000):
            with self.subTest(limit=limit):
                compressed=compress_messages(messages,limit)
                evidence=json.loads(compressed[1]['content'])['progressReceipt']['qaEvidence']
                self.assertEqual(evidence,original)
                self.assertEqual(evidence['semanticQuality'],'unassessed')
                self.assertEqual(evidence['criteriaComparison'],'required_from_agent')
                self.assertEqual(evidence['successfulBrowserCallIds'],[self.run['calls'][0]['id']])
                self.assertEqual({r['sha256'] for r in evidence['sources']},{'a'*64,'b'*64})
                self.assertLessEqual(sum(len(m.get('content','')) for m in compressed),limit)


if __name__=='__main__':unittest.main()
