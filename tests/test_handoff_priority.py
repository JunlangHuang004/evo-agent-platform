"""Offline advice/actual-publication tests; browser reports are explicit fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.config import Config
from backend.engine import Platform,clock_ms
from backend.handoff_priority import handoff_priority,priority_tail,PREFIX,protected_priority_indices
from backend.runtime import compress_messages,ProtocolError
from backend.teams import get_team_agents
from backend.tools import ToolGateway


class Files(ToolGateway):
    def capabilities(self):return {}


def browser_result(args,refs):
    steps=[];assertions=[]
    for check in args['checks']:
        row={'action':check['action'],'pageId':check.get('pageId','main'),'ok':True}
        row.update({k:check[k] for k in ('selector','key') if k in check})
        if check['action'].startswith('assert_'):
            expected=True if check['action']=='assert_visible' else check.get('count',check.get('text'))
            row.update(passed=True,expected=expected,actual=expected)
            assertions.append({k:v for k,v in row.items() if k!='ok'})
        steps.append(row)
    return {'ok':True,'runtime':'browser','exitCode':0,'test':True,'testDouble':True,'browser':{
        'executed':True,'ok':True,'entry':refs[0],'sourceManifest':refs,'testedFiles':copy.deepcopy(refs),
        'steps':steps,'assertions':assertions,'assertionCount':len(assertions),'assertionsPassed':len(assertions),
        'pageErrors':[],'httpErrors':[],'blockedRequests':[]}}


class BrowserFixtureFiles(Files):
    """Real confined writes/reads/grants; code execution explicitly replaced."""
    def execute(self,actor,run_id,name,args,approved=False,deadline=None):
        if name!='code.test':return super().execute(actor,run_id,name,args,approved=approved,deadline=deadline)
        self._validate_args(name,args)
        refs=[]
        for path in [args['path'],*(args.get('resources') or {}).values()]:
            body=super().execute(actor,run_id,'file.read',{'path':path},deadline=deadline)['content']
            refs.append({'path':path,'sha256':hashlib.sha256(body.encode()).hexdigest()})
        return browser_result(args,refs)


class HandoffPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.apps=[]
        self.fixture=json.loads((Path(__file__).parent/'fixtures/handoff_priority_r8.json').read_text())
        config=Config(Path(self.tmp.name),{'LLM_API_KEY':'offline-only'});config.apply_time_only_profile()
        agents=get_team_agents('software');self.app=Platform(config,tools=Files(config,agents),agents=agents,team_id='software');self.apps.append(self.app)
        self.task=self.app._new_task('Product','Original acceptance goal','human',list(agents),'dev')
        self.task.update(copy.deepcopy(self.fixture['task']),status='running',outputs=[],turns=35)
        self.run={'id':self.fixture['runId'],'status':'running','mode':'task','teamId':'software','channelId':'group',
            'steps':[self.task],'calls':copy.deepcopy(self.fixture['calls']),'artifacts':[],
            'budget':{'profile':'time_only','deadlineAt':self.fixture['stageControl']['runDeadlineAt']}}
        self.stage=copy.deepcopy(self.fixture['stageControl']);self.now=self.stage['expectedHandoffBy']+1000
        self.coverage,self.qa=self.app._browser_progress_coverage(self.run,self.task)
        self.hashes={r['path']:r['sha256'] for r in self.qa['sources']}
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.tmp.cleanup()
    def result(self):return handoff_priority(self.run,self.task,self.stage,self.qa,self.hashes,self.now,65000)

    def test_actual_r8_many_different_passes_keep_overdue_priority_independent_of_stagnation(self):
        actual=[c for c in self.run['calls'] if c['tool']=='code.test']
        self.assertGreaterEqual(len(actual),15)
        self.assertGreaterEqual(len({json.dumps(c['args']['checks'],sort_keys=True) for c in actual}),15)
        self.assertGreater(len(self.coverage),15)
        original=copy.deepcopy((self.run,self.task))
        self.task['_stagnantTurns']=0
        priority=self.result()
        self.assertIsNotNone(priority);self.assertEqual(priority['semanticCoverage'],'unassessed')
        self.assertEqual(len(priority['currentOwnedUnpublishedTestedFiles']),3)
        self.assertEqual(priority['publicationProposal'],{'type':'publish_artifacts','paths':['app/app.js','app/index.html','app/styles.css']})
        self.assertEqual(self.run['calls'],original[0]['calls']);self.assertEqual(self.task['outputs'],[])
        self.assertEqual(self.task['status'],'running');self.assertEqual(self.run['budget'],original[0]['budget'])
        self.assertIn('missing test or repair remains allowed',priority['instruction'])

    def test_before_target_global_final_no_files_and_unknown_effects_do_not_trigger(self):
        for change in ('before','global','no_write','unknown','sibling_unknown','bad_rev','foreign_stage','no_qa'):
            with self.subTest(change=change):
                state=copy.deepcopy((self.run,self.task,self.stage,self.qa,self.hashes,self.now))
                if change=='before':self.now=self.stage['expectedHandoffBy']-1
                elif change=='global':self.now=self.run['budget']['deadlineAt']-65000
                elif change=='no_write':self.run['calls']=[c for c in self.run['calls'] if c['tool']!='file.write']
                elif change=='unknown':self.task['_pendingTool']={'name':'file.patch'}
                elif change=='sibling_unknown':self.run['calls'].append({'tool':'file.write','agentId':'dev','taskId':'other','status':'failed','result':{'outcomeUnknown':True}})
                elif change=='bad_rev':self.task['revision']=False
                elif change=='foreign_stage':self.stage['agentId']='qa'
                elif change=='no_qa':self.qa={}
                self.assertIsNone(self.result())
                self.run,self.task,self.stage,self.qa,self.hashes,self.now=state

    def test_stale_actual_disk_sha_later_mutation_and_already_published_are_excluded(self):
        self.hashes={path:'f'*64 for path in self.hashes};self.assertIsNone(self.result())
        self.hashes={r['path']:r['sha256'] for r in self.qa['sources']}
        for path in list(self.hashes):
            self.run['calls'].append({'id':'new-'+path,'tool':'file.patch','agentId':'dev','taskId':self.task['id'],'revision':0,
                'status':'success','args':{'path':path},'result':{'ok':True,'sha256':'f'*64}})
        self.assertIsNone(self.result())
        self.run['calls']=copy.deepcopy(self.fixture['calls'])
        self.run['artifacts']=[{'id':path,'runId':self.run['id'],'taskId':self.task['id'],'ownerAgentId':'dev','path':path,'sha256':sha} for path,sha in self.hashes.items()]
        self.task['outputs']=list(self.hashes);self.assertIsNone(self.result())

    def test_current_published_candidate_keeps_handoff_advice_after_publication(self):
        self.run['artifacts']=[{'id':path,'runId':self.run['id'],'taskId':self.task['id'],
            'ownerAgentId':'dev','path':path,'sha256':digest} for path,digest in self.hashes.items()]
        self.task['outputs']=list(self.hashes)
        candidate={'id':'candidate-current','status':'candidate','runId':self.run['id'],
                   'taskId':self.task['id'],'taskRevision':self.task['revision'],
                   'files':[{'path':path,'sha256':digest} for path,digest in self.hashes.items()]}
        priority=handoff_priority(self.run,self.task,self.stage,self.qa,self.hashes,self.now,65000,candidate)
        self.assertEqual(priority['candidateDeliveryId'],'candidate-current')
        self.assertEqual(priority['currentOwnedUnpublishedTestedFiles'],[])
        self.assertEqual(priority_tail(priority)['candidateDeliveryId'],'candidate-current')
        candidate['taskRevision']+=1
        self.assertIsNone(handoff_priority(self.run,self.task,self.stage,self.qa,self.hashes,self.now,65000,candidate))

    def test_manifest_only_or_unowned_assets_are_not_treated_as_tested(self):
        for c in self.run['calls']:
            if c['tool']=='code.test':c['result']['browser']['testedFiles']=[]
        self.assertIsNone(self.result())
        self.run['calls']=copy.deepcopy(self.fixture['calls'])
        self.task['outputContract']['extensions']=['.md'];self.assertIsNone(self.result())

    def test_html_contract_does_not_omit_tested_js_css_and_index_omissions_are_explicit(self):
        self.task['outputContract']['extensions']=['.html']
        priority=self.result()
        self.assertEqual(priority['publicationProposal']['paths'],['app/app.js','app/index.html','app/styles.css'])
        self.assertIn('not_proven',priority['bundleCompleteness'])
        self.qa['sources']=[r for r in self.qa['sources'] if r['path']!='app/styles.css']
        self.qa['omittedSources']=1
        priority=self.result()
        self.assertEqual(priority['validatedSourceIndexOmitted'],1)
        self.assertEqual(priority['publicationProposal']['paths'],['app/app.js','app/index.html'])
        self.assertIn('other required current dependencies',priority['instruction'])

    def test_current_priority_and_tail_survive_repeated_compaction_without_growth(self):
        priority=self.result();tail=PREFIX+json.dumps(priority_tail(priority),ensure_ascii=False)
        initial={'task':{'id':self.task['id'],'goal':'Preserve original goal','acceptanceCriteria':self.task['acceptanceCriteria']},
            'humanClarifications':[{'receiptId':'real-receipt','answer':'Keep the actual requirement intact.'}],
            'progressReceipt':{'handoffPriority':priority}}
        messages=[{'role':'system','content':'Original role/permissions.'},{'role':'user','content':json.dumps(initial)},
            {'role':'user','content':tail},{'role':'assistant','content':'optional old text '*4000}]
        for _ in range(3):
            messages=compress_messages(messages,9000)
            self.assertLessEqual(sum(len(m['content']) for m in messages),9000)
            self.assertEqual(json.loads(messages[1]['content'])['progressReceipt']['handoffPriority'],priority)
            self.assertEqual(sum(m['content']==tail for m in messages),1)
            self.assertEqual(len(protected_priority_indices(messages)),1)
            messages.append({'role':'assistant','content':'more optional text '*4000})
        with self.assertRaises(ProtocolError):compress_messages(messages,200)
        arbitrary=[{'role':'system','content':'Role'},{'role':'user','content':json.dumps({'task':{'id':'x'}})}, {'role':'user','content':tail}]
        self.assertEqual(protected_priority_indices(arbitrary),[])

    def test_scripted_model_explicitly_publishes_then_independent_role_reads_real_snapshot(self):
        model=PublishModel();config=Config(Path(self.tmp.name)/'workflow',{'LLM_API_KEY':'offline','MAX_RETRIES':'0'});config.apply_time_only_profile()
        agents=get_team_agents('software');app=Platform(config,provider=model,tools=BrowserFixtureFiles(config,agents),agents=agents,team_id='software')
        model.app=app;self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['lead','dev','qa'],'text':'Deliver actual files to independent QA; do not invent acceptance.'})
        self.assertTrue(app.wait_for_idle(6))
        run=app.snapshot()['sessions']['group']['run'];producer=next(t for t in run['steps'] if t['title']=='Produce')
        reviewer=next(t for t in run['steps'] if t['title']=='Read independently')
        self.assertEqual(producer['status'],'done');self.assertEqual(len(producer['outputs']),2)
        self.assertEqual(reviewer['status'],'done');self.assertTrue(model.saw_priority)
        self.assertEqual(model.read_content,'<h1>Actual fixture product</h1>')
        self.assertEqual([c['tool'] for c in run['calls'] if c['taskId']==reviewer['id'] and c['tool']!='llm.chat'],['file.read'])
        self.assertEqual(run['budgetExtensions'],0);self.assertIsNone(run['budget']['agentTurns'])
        self.assertNotIn('validation',producer,'publication/read handoff is not a fabricated business review')
        self.assertEqual(sum(e['type']=='artifact_registered' for e in run['events']),2)

    def test_legal_none_contract_reaches_real_handoff_progress_without_artifact_advice(self):
        from tests.test_time_only_runtime import Model
        config=Config(Path(self.tmp.name)/'none-contract',{'LLM_API_KEY':'offline','MAX_RETRIES':'0'})
        config.apply_time_only_profile();seen=[]
        def action(index,context,messages):
            seen.append(copy.deepcopy(context))
            self.assertIsNone(context['task'].get('outputContract'))
            if index==0:
                run=app.state['sessions']['group']['run']
                task=next(t for t in run['steps'] if t['id']==context['task']['id'])
                now=clock_ms()
                task['stageControl'].update(allocatedAt=now-60000,synthesisAt=now-50000,
                    handoffAt=now-40000,expectedHandoffBy=now-1000)
                return {'type':'tool','name':'file.write','args':{'path':'notes.md','content':'Actual unconstrained-task note.'}}
            self.assertEqual(context['progressReceipt']['executionPhase'],'handoff')
            self.assertNotIn('handoffPriority',context['progressReceipt'])
            return {'type':'final','status':'done','text':'Plain text result; no artifact contract or independent acceptance claim.'}
        model=Model(action);app=Platform(config,provider=model,tools=Files(config));model.app=app;self.apps.append(app)
        state=app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Return a plain text answer; no artifact contract is declared.'})
        original_deadline=state['sessions']['group']['run']['budget']['deadlineAt']
        self.assertTrue(app.wait_for_idle(5))
        run=app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
        self.assertEqual(run['status'],'done');self.assertEqual(task['status'],'done')
        self.assertIsNone(task['outputContract']);self.assertIsNone(task['error'])
        self.assertEqual(len(seen),2);self.assertEqual(run['modelCalls'],3)
        self.assertEqual([c['tool'] for c in run['calls'] if c['tool']!='llm.chat'],['file.write'])
        self.assertEqual(run['budget']['deadlineAt'],original_deadline)
        self.assertEqual(task['outputs'],[]);self.assertEqual(run['artifacts'],[])


class PublishModel:
    def __init__(self):self.app=None;self.counts={};self.saw_priority=False;self.read_content=None
    def complete(self,messages,max_tokens=None,deadline=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            action={'tasks':[{'id':'p','title':'Produce','goal':'Keep original criteria','agentId':'dev','acceptanceCriteria':['Usable deliverable; independent quality is still to assess'],
                'outputContract':{'kind':'artifact','extensions':['.html','.js'],'minCount':2}},
                {'id':'q','title':'Read independently','agentId':'qa','dependsOn':['p'],'outputContract':{'kind':'text'}}]}
        else:
            ctx=json.loads(messages[1]['content']);title=ctx['task']['title'];i=self.counts.get(title,0);self.counts[title]=i+1
            if title=='Produce':
                with self.app.lock:
                    run=self.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if t['id']==ctx['task']['id'])
                    s=task['stageControl'];now=clock_ms();s.update(allocatedAt=now-60000,synthesisAt=now-50000,handoffAt=now-40000,expectedHandoffBy=now-1000)
                actions=[{'type':'tool','name':'file.write','args':{'path':'app/index.html','content':'<h1>Actual fixture product</h1>'}},
                    {'type':'tool','name':'file.write','args':{'path':'app/app.js','content':'// Actual fixture code\n'}},
                    {'type':'tool','name':'code.test','args':{'language':'browser','path':'app/index.html','resources':{'app.js':'app/app.js'},'checks':[{'action':'assert_visible','selector':'h1'}]}},
                    {'type':'publish_artifacts','paths':['app/index.html','app/app.js']},
                    {'type':'final','status':'done','text':'Actual files published; independent quality remains to assess.'}]
                action=actions[i]
                if i==3:
                    priority=ctx['progressReceipt'].get('handoffPriority');self.saw_priority=bool(priority)
                    assert priority and priority['semanticCoverage']=='unassessed'
                    assert sum(m['content'].startswith(PREFIX) for m in messages)==1
                    assert ctx['progressReceipt']['turnsWithoutNewOutputOrTest']==0
                    assert self.app.state['sessions']['group']['run']['artifacts']==[]
            elif title=='Read independently':
                if i==0:action={'type':'tool','name':'file.read','args':{'path':next(a['path'] for a in ctx['authorizedArtifacts'] if a['name']=='index.html')}}
                else:
                    actual=next(json.loads(m['content'].split('：',1)[1]) for m in reversed(messages) if m['content'].startswith('动作的真实结果：'))
                    self.read_content=actual['content'];action={'type':'final','status':'done','text':'Read actual snapshot only; not semantic approval.'}
            else:action={'type':'final','status':'partial','text':'No product acceptance inferred from fixture handoff.'}
        return {'content':json.dumps(action),'finish_reason':'stop','usage':{'total_tokens':1}}


if __name__=='__main__':unittest.main()
