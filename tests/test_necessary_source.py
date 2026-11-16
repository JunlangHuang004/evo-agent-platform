"""Known-source phase admission does not reopen search, time or permissions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform,clock_ms
from backend.necessary_source import necessary_source_admission
from backend.stage_control import stage_control
from tests.test_time_only_runtime import Model,Files

URL='https://example.org/actual-report'

class NecessarySourceTests(unittest.TestCase):
    def fixture(self):
        task={'id':'t','agentId':'researcher','status':'running','revision':0,'_messages':[]}
        run={'id':'r','teamId':'fund','mode':'task','status':'running','budget':{'profile':'time_only','deadlineAt':1000000},
             'steps':[task],'transcript':[],'calls':[]}
        stage_control(run,task,1000);task['stageControl'].update(synthesisAt=2000,handoffAt=3000,expectedHandoffBy=4000)
        control=stage_control(run,task,5000)
        call={'id':'found','taskId':'t','agentId':'researcher','revision':0,'tool':'web.search','status':'success',
              'result':{'ok':True,'results':[{'url':URL,'title':'Actual observed link'}]}}
        run['calls'].append(call)
        action={'type':'tool','name':'web.fetch','args':{'url':URL},'evidenceGap':'Need the actual source body before assessing this claim.'}
        return run,task,action,{'researcher':{'tools':['web.fetch','web.search']}},control
    def admit(self,f,now=5000):return necessary_source_admission(*f,now)
    def test_zero_body_known_link_is_admission_not_a_source_or_execution(self):
        f=self.fixture();before=copy.deepcopy(f);receipt=self.admit(f)
        self.assertTrue(receipt['admitted']);self.assertFalse(receipt['actionExecuted']);self.assertEqual(receipt['sourceProof']['callId'],'found')
        self.assertEqual(receipt['remainingMs'],995000);self.assertNotIn('content',receipt);self.assertEqual(f,before)
    def test_real_current_run_human_url_is_allowed_not_agent_prose(self):
        for role,expected in [('human',True),('agent',False)]:
            f=self.fixture();f[0]['calls']=[];f[0]['transcript']=[{'id':'m','role':role,'runId':'r','text':'Please check '+URL}]
            self.assertEqual(self.admit(f) is not None,expected)
        f=self.fixture();f[0]['calls']=[];f[0]['title']=URL;self.assertIsNone(self.admit(f))
    def test_current_owner_revision_success_required_and_original_acl_retained(self):
        changes=[('agentId','other'),('taskId','other'),('revision',True),('revision',1),('status','failed'),('runId','other')]
        for key,value in changes:
            with self.subTest(key=key,value=value):
                f=self.fixture();f[0]['calls'][0][key]=value;self.assertIsNone(self.admit(f))
        for key,value in [('ok',False),('notExecuted',True),('outcomeUnknown',True),('modelViewOnly',True)]:
            f=self.fixture();f[0]['calls'][0]['result'][key]=value;self.assertIsNone(self.admit(f))
        f=self.fixture();f[3]['researcher']['tools']=[];self.assertIsNone(self.admit(f))
    def test_final_global_deadline_and_unknown_effects_cannot_be_bypassed(self):
        for now in [880000,999999,1000000]:self.assertIsNone(self.admit(self.fixture(),now))
        for key in ['_pendingTool','_uncertainCalls']:
            f=self.fixture();f[1][key]={'unknown':True};self.assertIsNone(self.admit(f))
        f=self.fixture();f[4]['runDeadlineAt']+=1;self.assertIsNone(self.admit(f))
        f=self.fixture();f[4]['revision']=False;self.assertIsNone(self.admit(f))
    def test_bounded_top_level_gap_and_known_fetch_only(self):
        for change in [dict(evidenceGap=''),dict(evidenceGap='x'*601),dict(evidenceGap='x\ud800xxxxxx'),dict(name='web.search'),dict(args={'url':URL,'evidenceGap':'wrong place'}),dict(args={'url':'https://invented.example/no-proof'})]:
            f=self.fixture();f[2].update(change);self.assertIsNone(self.admit(f))
    def test_successful_fetch_actual_links_allow_target_not_arbitrary_sources_field(self):
        f=self.fixture();c=f[0]['calls'][0];c['tool']='web.fetch';c['result']={'ok':True,'url':'https://example.org/index','content':'index','links':[{'url':URL}]}
        self.assertIsNotNone(self.admit(f));c['result']['links']=[];c['result']['sources']=[{'url':URL}];self.assertIsNone(self.admit(f))
    def test_past_success_is_not_current_body_and_does_not_permanently_deny_recovery(self):
        f=self.fixture();c={'id':'body','taskId':'t','agentId':'researcher','revision':0,'tool':'web.fetch','status':'success','result':{'ok':True,'url':URL,'content':'Actual source body','callId':'body'}};f[0]['calls'].append(c)
        self.assertIsNotNone(self.admit(f),'ledger says fetched, but current working view has no body')
        action={'type':'tool','name':'web.fetch','args':{'url':URL}}
        f[1]['_messages']=[{'role':'system','content':'policy'},{'role':'user','content':'{}'},
            {'role':'assistant','content':json.dumps(action)},{'role':'user','content':'动作的真实结果：'+json.dumps(c['result'])}]
        self.assertIsNone(self.admit(f),'actual current same body should be used rather than refetched')
        f[1]['_messages'][-1]['content']='动作的真实结果：'+json.dumps({'ok':True,'callId':'body','url':URL,'contentOmitted':True})
        self.assertIsNotNone(self.admit(f),'omitted marker is not the body')

    def test_only_actual_successful_file_read_text_discovers_urls(self):
        for mode in ('full','lines','find'):
            f=self.fixture();c=f[0]['calls'][0];c['tool']='file.read';c['args']={'path':'imports/artifact-actual/brief.md'}
            c['result']={'ok':True,'path':c['args']['path'],'sha256':'a'*64,'mode':mode}
            if mode=='find':c['result']['matches']=[{'content':'Primary source '+URL,'startLine':3,'endLine':3}]
            else:c['result']['content']='Primary source '+URL
            receipt=self.admit(f);self.assertEqual(receipt['sourceProof']['kind'],'successful_file_read_url')
            self.assertEqual(receipt['sourceProof']['sha256'],'a'*64);self.assertEqual(receipt['sourceProof']['callId'],'found')
            self.assertNotIn('content',receipt['sourceProof'])
            c['result'].pop('content',None);c['result'].pop('matches',None);c['result']['metadata']={'url':URL}
            self.assertIsNone(self.admit(f),'unread metadata is not actual returned text')
        f=self.fixture();c=f[0]['calls'][0];c.update(tool='file.read',args={'path':'private/brief.md'},result={'ok':True,'mode':'full','path':'private/brief.md','sha256':'b'*64,'content':URL})
        c['agentId']='other';self.assertIsNone(self.admit(f),'never mine another owner read')
        c['agentId']='researcher';c['result']['path']='different.md';self.assertIsNone(self.admit(f))

    def test_owner_effect_gate_includes_failed_nested_flags_and_sibling_tasks(self):
        for nesting in [('result',),('error',),('error','transport'),('result','lateResult')]:
            for value in (True,'unknown',1,[],{}):
                f=self.fixture();c={'id':'mutation','agentId':'researcher','taskId':'sibling','tool':'file.write','status':'failed'}
                dest=c
                for key in nesting:dest[key]={};dest=dest[key]
                dest['outcomeUnknown']=value
                f[0]['calls'].append(c);self.assertIsNone(self.admit(f),(nesting,value))
        for marker in ('_pendingTool','_uncertainCalls'):
            f=self.fixture();f[0]['steps'].append({'id':'sibling','agentId':'researcher',marker:{'unknown':True}})
            self.assertIsNone(self.admit(f))
        f=self.fixture();f[0]['calls'].append({'id':'closed','agentId':'researcher','tool':'file.write','status':'failed','result':{'outcomeUnknown':False}})
        self.assertIsNotNone(self.admit(f))
        f=self.fixture();f[0]['calls'].append({'id':'other','agentId':'another-owner','taskId':'not-owned','tool':'file.write','status':'unknown'})
        self.assertIsNotNone(self.admit(f),'another private owner does not lend or block access')
    def test_exact_url_tokens_and_explicit_markdown_never_expand_path_query_prefix(self):
        for text in [URL+'/extra',URL+'?secret=one',URL+'!']:
            f=self.fixture();f[0]['calls']=[];f[0]['transcript']=[{'id':'h','runId':'r','role':'human','text':text}]
            self.assertIsNone(self.admit(f),'do not strip path/query/legitimate URL punctuation')
        for text in ['[source]('+URL+')','<'+URL+'>',URL]:
            f=self.fixture();f[0]['calls']=[];f[0]['transcript']=[{'id':'h','runId':'r','role':'human','text':text}]
            self.assertIsNotNone(self.admit(f))

    def test_visible_redirected_or_fragment_fetch_body_prevents_duplicate_fetch_without_expanding_proof(self):
        for target,requested,final in [(URL+'#section',URL+'#section',URL),('https://example.org/redirect','https://example.org/redirect',URL),('https://example.org/redirect#section','https://example.org/redirect',URL)]:
            f=self.fixture();f[2]['args']['url']=target;f[0]['calls'][0]['result']['results'][0]['url']=target
            action={'type':'tool','name':'web.fetch','args':{'url':requested}}
            result={'ok':True,'url':final,'content':'Actual redirected source body','callId':'body'}
            f[0]['calls'].append({'id':'body','taskId':'t','agentId':'researcher','revision':0,'tool':'web.fetch','status':'success','args':action['args'],'result':result})
            f[1]['_messages']=[{'role':'system','content':'policy'},{'role':'user','content':'{}'},
                {'role':'assistant','content':json.dumps(action)},{'role':'user','content':'动作的真实结果：'+json.dumps(result)}]
            self.assertIsNone(self.admit(f),'same actual finalURL/SHA is already visible')
            f[1]['_messages']=[];self.assertIsNotNone(self.admit(f),'past success alone does not prove current body')
    def test_actual_full_snapshot_body_is_visible_but_pointer_or_window_is_not_the_full_body(self):
        import hashlib
        f=self.fixture();body='Actual source body saved by the host';digest=hashlib.sha256(body.encode()).hexdigest();path='imports/web-source-actual/body.txt'
        result={'ok':True,'url':URL,'content':body,'sourceSnapshot':{'path':path,'sha256':digest}}
        f[0]['calls'].append({'id':'body','taskId':'t','agentId':'researcher','revision':0,'tool':'web.fetch','status':'success','args':{'url':URL},'result':result})
        self.assertIsNotNone(self.admit(f),'snapshot metadata is not a read body')
        action={'type':'tool','name':'file.read','args':{'path':path}}
        value={'ok':True,'path':path,'sha256':digest,'mode':'full','content':body,'truncated':False,'callId':'read'}
        f[1]['_messages']=[{'role':'system','content':'policy'},{'role':'user','content':'{}'},
            {'role':'assistant','content':json.dumps(action)},{'role':'user','content':'动作的真实结果：'+json.dumps(value)}]
        self.assertIsNone(self.admit(f),'real current full snapshot body should be used')
        action['args'].update(startLine=1,endLine=1);value.update(mode='lines',startLine=1,endLine=1,startColumn=1,endColumn=8,partialFirstLine=False,partialLastLine=True,content='Actual ')
        f[1]['_messages'][2]['content']=json.dumps(action);f[1]['_messages'][3]['content']='动作的真实结果：'+json.dumps(value)
        self.assertIsNotNone(self.admit(f),'a partial source window is not proof the full body is available')

    def test_new_final_url_version_invalidates_an_older_visible_alias_body(self):
        f=self.fixture();alias='https://example.org/redirect';f[2]['args']['url']=alias;f[0]['calls'][0]['result']['results'][0]['url']=alias
        action={'type':'tool','name':'web.fetch','args':{'url':alias}}
        old={'ok':True,'url':URL,'content':'Old body','callId':'old'}
        f[0]['calls'].append({'id':'old','taskId':'t','agentId':'researcher','revision':0,'tool':'web.fetch','status':'success','args':action['args'],'result':old})
        f[1]['_messages']=[{'role':'system','content':'policy'},{'role':'user','content':'{}'},
            {'role':'assistant','content':json.dumps(action)},{'role':'user','content':'动作的真实结果：'+json.dumps(old)}]
        self.assertIsNone(self.admit(f))
        newer={'ok':True,'url':URL,'content':'New body','callId':'new'}
        f[0]['calls'].append({'id':'new','taskId':'t','agentId':'researcher','revision':0,'tool':'web.fetch','status':'success','args':{'url':URL},'result':newer})
        self.assertIsNotNone(self.admit(f),'visible old alias bytes are not the new actual finalURL version')
        f[1]['_messages']+=[{'role':'assistant','content':json.dumps({'type':'tool','name':'web.fetch','args':{'url':URL}})},
            {'role':'user','content':'动作的真实结果：'+json.dumps(newer)}]
        self.assertIsNone(self.admit(f),'latest actual finalURL body is now visible')

class NecessarySourceRuntimeTests(unittest.TestCase):
    def test_actual_known_url_fetch_after_stage_then_write_publish_and_independent_read(self):
        with tempfile.TemporaryDirectory() as d:
            config=Config(Path(d),{'LLM_API_KEY':'offline','MAX_RETRIES':'0'});config.apply_time_only_profile()
            plan=[{'id':'r','title':'Research','agentId':'research','outputContract':{'kind':'artifact','extensions':['.md'],'minCount':1}},
                  {'id':'q','title':'Review','agentId':'reviewer','dependsOn':['r'],'requiredCapabilities':['review']}]
            seen={};counts={}
            class Web(Files):
                def execute(self,agent,run,name,args,**kwargs):
                    if name=='web.search':return {'ok':True,'results':[{'url':URL,'title':'Actual discovered link'}]}
                    if name=='web.fetch':
                        self.fetches=getattr(self,'fetches',0)+1
                        return {'ok':True,'url':URL,'content':'Actual source evidence read via original gateway fixture.'}
                    return super().execute(agent,run,name,args,**kwargs)
            def action(n,c,m):
                title=c['task']['title'];i=counts.get(title,0);counts[title]=i+1
                run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if t['id']==c['task']['id'])
                if title=='Research':
                    if i==0:return {'type':'tool','name':'web.search','args':{'query':'needed source'}}
                    if i==1:
                        now=clock_ms();task['stageControl'].update(allocatedAt=now-4000,synthesisAt=now-3000,handoffAt=now-2000,expectedHandoffBy=now-1000)
                        seen['deadline']=run['budget']['deadlineAt']
                        return {'type':'tool','name':'web.fetch','args':{'url':URL},'evidenceGap':'Need the actual discovered report body to support this brief.'}
                    if i==2:return {'type':'tool','name':'file.write','args':{'path':'brief.md','content':'Sourced brief from '+URL+'; limitations remain.'}}
                    if i==3:return {'type':'publish_artifact','path':'brief.md'}
                    return {'type':'final','status':'done','text':'Published sourced brief, not independent quality acceptance.'}
                if i==0:return {'type':'tool','name':'file.read','args':{'path':c['authorizedArtifacts'][0]['path']}}
                return {'type':'final','status':'done','text':'Read the actual granted brief; no invented semantic pass.'}
            class Plan(Model):
                def complete(self,messages,max_tokens=None,deadline=None):
                    if messages[0]['content'].startswith('Plan an executable task DAG'):return {'content':json.dumps({'tasks':plan}),'finish_reason':'stop','usage':{'total_tokens':1}}
                    return super().complete(messages,max_tokens,deadline)
            model=Plan(action);tools=Web(config);app=Platform(config,provider=model,tools=tools);model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['research','reviewer'],'text':'Research one actual source then pass the published brief for independent reading.'})
                self.assertTrue(app.wait_for_idle(5));run=app.state['sessions']['group']['run']
                self.assertEqual(run['status'],'done');self.assertEqual(tools.fetches,1)
                self.assertEqual(run['budget']['deadlineAt'],seen['deadline']);self.assertEqual(run['budgetExtensions'],0)
                self.assertEqual(len([e for e in run['events'] if e['type']=='necessary_source_admitted']),1)
                self.assertTrue(any(c['tool']=='file.read' and c['agentId']=='reviewer' and c['status']=='success' for c in run['calls']))
                self.assertFalse(any(e['type']=='review_pass' for e in run['events']))
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()

    def test_existing_permanent_read_guard_still_refuses_third_network_call_without_killing_notes(self):
        from backend.tools import ToolError
        with tempfile.TemporaryDirectory() as d:
            config=Config(Path(d),{'LLM_API_KEY':'offline','MAX_RETRIES':'0'});config.apply_time_only_profile()
            class Web(Files):
                fetches=0
                def execute(self,agent,run,name,args,**kwargs):
                    if name=='web.search':return {'ok':True,'results':[{'url':URL}]}
                    if name=='web.fetch':
                        self.fetches+=1
                        raise ToolError('permanent','Actual unavailable source fixture',False)
                    return super().execute(agent,run,name,args,**kwargs)
            counts={'n':0}
            def action(n,c,m):
                n=counts['n'];counts['n']+=1
                task=next(t for t in model.app.state['sessions']['group']['run']['steps'] if t['id']==c['task']['id'])
                if n==0:return {'type':'tool','name':'web.search','args':{'query':'source'}}
                if n in (1,2,3):
                    now=clock_ms()
                    if n==1:task['stageControl'].update(allocatedAt=now-4000,synthesisAt=now-3000,handoffAt=now-2000,expectedHandoffBy=now-1000)
                    return {'type':'tool','name':'web.fetch','args':{'url':URL},'evidenceGap':'Need this discovered report body; no source body yet.'}
                if n==4:return {'type':'tool','name':'file.write','args':{'path':'gaps.md','content':'No source body obtained. Two actual permanent failures; URL is discovery only.'}}
                if n==5:return {'type':'publish_artifact','path':'gaps.md'}
                return {'type':'final','status':'partial','text':'Published explicit source gaps; no evidence quality pass.'}
            class Plan(Model):
                def complete(self,messages,max_tokens=None,deadline=None):
                    if messages[0]['content'].startswith('Plan an executable task DAG'):return {'content':json.dumps({'tasks':[{'id':'r','title':'Research','agentId':'research','outputContract':{'kind':'artifact','extensions':['.md'],'minCount':1}}]}),'finish_reason':'stop','usage':{'total_tokens':1}}
                    return super().complete(messages,max_tokens,deadline)
            model=Plan(action);tools=Web(config);app=Platform(config,provider=model,tools=tools);model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['research'],'text':'Research the actual discovered source and report gaps honestly.'})
                self.assertTrue(app.wait_for_idle(5));run=app.state['sessions']['group']['run']
                self.assertEqual(tools.fetches,2);self.assertEqual(len([c for c in run['calls'] if c['tool']=='web.fetch']),2)
                self.assertTrue(all(c['status']=='failed' for c in run['calls'] if c['tool']=='web.fetch'))
                self.assertEqual(len([e for e in run['events'] if e['type']=='readonly_failure_recovery']),1)
                self.assertEqual(run['status'],'partial');self.assertEqual(len(run['artifacts']),1)
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()

if __name__=='__main__':unittest.main()
