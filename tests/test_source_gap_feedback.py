"""Observed known-URL refusal gets explanation, never execution or admission."""
import copy
import datetime
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform, clock_ms
from backend.feedback_context import latest_feedback_proof
from backend.necessary_source import necessary_source_admission
from backend.runtime import compress_messages
from backend.stage_control import stage_action_feedback
from tests.test_time_only_runtime import Files, Model


FIXTURE=Path(__file__).parent/'fixtures/source-gap-fund2.json'
URL='https://www.infosys.com/iki/research/semiconductor-industry-outlook2026.html'


class SourceGapFeedbackTests(unittest.TestCase):
    def replay(self):
        data=json.loads(FIXTURE.read_text());chain=data['chains'][0]
        task={'id':data['taskId'],'agentId':'research','revision':0,'status':'running','_messages':[]}
        stage=chain['feedback']['stageControl']
        run={'id':data['runId'],'mode':'task','status':'running','budget':{'profile':'time_only','deadlineAt':stage['runDeadlineAt']},
             'steps':[task],'calls':data['actualSourceCalls']+[data['actualModelCall']],
             'transcript':[data['actualProposalTranscript']]}
        now=int(datetime.datetime.fromisoformat(chain['time'].replace('Z','+00:00')).timestamp()*1000)
        return data,run,task,copy.deepcopy(chain['action']),copy.deepcopy(stage),now

    def test_actual_fund2_refusal_explains_missing_gap_without_admitting_original_action(self):
        data,run,task,action,stage,now=self.replay();before=copy.deepcopy((run,task,action,stage))
        agents={'research':{'tools':['web.fetch','web.search']}}
        self.assertIsNone(necessary_source_admission(run,task,action,agents,stage,now))
        receipt=stage_action_feedback(stage,action)
        self.assertFalse(receipt['actionExecuted']);self.assertFalse(receipt['ok'])
        hint=receipt['necessarySourceRetry'];self.assertTrue(hint['guidanceOnly'])
        self.assertEqual(hint['knownUrlStatus'],'not_assessed_by_this_feedback')
        self.assertEqual(hint['example']['args'],{'url':URL})
        self.assertIn('top-level evidenceGap',hint['instruction']);self.assertIn('8–600',hint['instruction'])
        self.assertEqual(hint['example']['evidenceGap'],'')
        self.assertIsNone(necessary_source_admission(run,task,hint['example'],agents,stage,now),
                          'copying the supplied example must not provide a fabricated concrete gap')
        self.assertEqual((run,task,action,stage),before)
        corrected={**action,'evidenceGap':'Need the actual known report to assess the missing supply and capex evidence.'}
        admission=necessary_source_admission(run,task,corrected,agents,stage,now)
        self.assertTrue(admission['admitted']);self.assertFalse(admission['actionExecuted'])
        self.assertIn(admission['sourceProof']['callId'],[p['callId'] for p in data['chains'][0]['knownURLProofs']])
        self.assertNotIn('content',admission)

    def test_hint_does_not_open_unknown_url_acl_final_only_or_unknown_effects(self):
        for variation in ('unknown_url','no_acl','global_final','unknown_effect','gap_in_args','short_gap'):
            with self.subTest(variation=variation):
                _,run,task,action,stage,now=self.replay();agents={'research':{'tools':['web.fetch']}}
                action['evidenceGap']='Need the actual known source to resolve a material evidence gap.'
                if variation=='unknown_url':action['args']['url']='https://unknown.example/not-observed'
                elif variation=='no_acl':agents['research']['tools']=[]
                elif variation=='global_final':now=run['budget']['deadlineAt']-120000
                elif variation=='unknown_effect':run['calls'].append({'id':'unknown','tool':'file.write','agentId':'research','taskId':task['id'],'status':'uncertain'})
                elif variation=='gap_in_args':action['args']['evidenceGap']=action.pop('evidenceGap')
                else:action['evidenceGap']='tiny'
                self.assertIsNotNone(stage_action_feedback(stage,action))
                self.assertIsNone(necessary_source_admission(run,task,action,agents,stage,now))

    def test_guidance_is_only_for_fetch_and_does_not_parse_or_fix_bad_args(self):
        _,_,_,action,stage,_=self.replay()
        for args in (None,[],7,'bad',{'url':'x'*2049}):
            candidate={**action,'args':args};before=copy.deepcopy(candidate)
            receipt=stage_action_feedback(stage,candidate)
            self.assertTrue(receipt['necessarySourceRetry']['guidanceOnly']);self.assertEqual(candidate,before)
            self.assertIn('<exact',receipt['necessarySourceRetry']['example']['args']['url'])
        for candidate in ({'type':'tool','name':'web.search','args':{'query':'missing evidence'}},{'type':'delegate'}):
            self.assertNotIn('necessarySourceRetry',stage_action_feedback(stage,candidate))
        self.assertIsNone(stage_action_feedback({**stage,'phase':'work'},action))

    def test_actual_bound_feedback_hint_survives_repeated_compaction(self):
        _,run,task,action,stage,_=self.replay();feedback=stage_action_feedback(stage,action)
        row=copy.deepcopy(json.loads(FIXTURE.read_text())['actualFeedbackTranscript']);row['text']=json.dumps(feedback)
        run['transcript'].append(row);proof=latest_feedback_proof(run,task);self.assertIsNotNone(proof)
        messages=[{'role':'system','content':'Original role, task and tool protocol. '*120},
                  {'role':'user','content':json.dumps({'task':{'id':task['id'],'criteria':['Actual evidence, no invented figures.']},'stageControl':stage})},
                  {'role':'user','content':'Old optional history '+('x'*100000)},
                  {'role':'assistant','content':run['transcript'][0]['text']},
                  {'role':'user','content':'阶段动作结果：'+json.dumps(feedback)}]
        for limit in (40000,80000,40000):
            messages=compress_messages(messages,limit,trusted_feedback=proof)
            self.assertLessEqual(sum(len(m['content']) for m in messages),limit)
            self.assertTrue(messages[-1]['content'].startswith('阶段动作结果：'))
            self.assertEqual(json.loads(messages[-1]['content'].split('：',1)[1])['necessarySourceRetry'],feedback['necessarySourceRetry'])


class SourceGapRuntimeTests(unittest.TestCase):
    def test_global_final_only_does_not_offer_a_followup_fetch_or_extra_model_request(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder),{'LLM_API_KEY':'offline-source-gap','MAX_RETRIES':'0'});config.apply_time_only_profile()
            def action(n,c,m):
                if n==0:
                    run=app.state['sessions']['group']['run'];run['budget']['deadlineAt']=clock_ms()+50000
                    for task in run['steps']:
                        if control:=task.get('stageControl'):
                            control['runDeadlineAt']=run['budget']['deadlineAt']
                            for key in ('synthesisAt','handoffAt','expectedHandoffBy'):
                                control[key]=min(control[key],control['runDeadlineAt'])
                    return {'type':'accept'}
                self.assertEqual(c['progressReceipt']['executionPhase'],'final_only')
                return {'type':'tool','name':'web.fetch','args':{'url':URL},'evidenceGap':'Attempt at final-only must never execute.'}
            model=Model(action);app=Platform(config,provider=model,tools=Files(config));model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Respect the original final-only boundary.'})
                self.assertTrue(app.wait_for_idle(4));run=app.state['sessions']['group']['run']
                self.assertEqual(len(model.calls),3)
                self.assertFalse(any(c['tool']=='web.fetch' for c in run['calls']))
                self.assertFalse(any(e['type']=='necessary_source_admitted' for e in run['events']))
                self.assertFalse(any(x.get('kind')=='stage_action_blocked' and 'necessarySourceRetry' in x.get('text','') for x in run['transcript']))
                self.assertNotEqual(run['status'],'done')
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()

    def test_next_real_model_view_has_hint_then_explicit_corrected_action_runs_once(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder),{'LLM_API_KEY':'offline-source-gap','MAX_RETRIES':'0'});config.apply_time_only_profile()
            class Web(Files):
                fetches=0
                def execute(self,agent,run,name,args,**kwargs):
                    if name=='web.search':return {'ok':True,'results':[{'url':URL,'title':'Scripted observed source link'}]}
                    if name=='web.fetch':
                        self.fetches+=1
                        return {'ok':True,'url':URL,'content':'Offline fixture actual source body, not a live research claim.'}
                    return super().execute(agent,run,name,args,**kwargs)
            counts={'n':0}
            def action(n,c,m):
                n=counts['n'];counts['n']+=1
                task=next(t for t in app.state['sessions']['group']['run']['steps'] if t['id']==c['task']['id'])
                if n==0:return {'type':'tool','name':'web.search','args':{'query':'fixture source'}}
                if n==1:
                    now=clock_ms();task['stageControl'].update(allocatedAt=now-4000,synthesisAt=now-3000,handoffAt=now-2000,expectedHandoffBy=now-1000)
                    task['_messages'].append({'role':'user','content':'Optional old fixture prose '+('z'*140000)})
                    return {'type':'tool','name':'web.fetch','args':{'url':URL}}
                if n==2:
                    self.assertEqual(tools.fetches,0,'the original rejected action must not execute')
                    self.assertTrue(m[-1]['content'].startswith('阶段动作结果：'))
                    hint=json.loads(m[-1]['content'].split('：',1)[1])['necessarySourceRetry']
                    self.assertTrue(hint['guidanceOnly']);self.assertIn('top-level evidenceGap',hint['instruction'])
                    self.assertIn('protectedFeedback',app.state['sessions']['group']['run']['calls'][-1]['contextCapacity'])
                    return {'type':'tool','name':'web.fetch','args':{'url':URL},'evidenceGap':'Need this actually discovered source body to support the requested evidence note.'}
                if n==3:
                    self.assertEqual(tools.fetches,1)
                    return {'type':'tool','name':'file.write','args':{'path':'brief.md','content':'Fixture source note with explicit limitations.'}}
                if n==4:return {'type':'publish_artifact','path':'brief.md'}
                return {'type':'final','status':'done','text':'Published the fixture note; semantic quality remains unassessed.'}
            class Plan(Model):
                def complete(self,messages,max_tokens=None,deadline=None):
                    if messages[0]['content'].startswith('Plan an executable task DAG'):
                        return {'content':json.dumps({'tasks':[{'id':'r','agentId':'research','title':'Read source and publish evidence note','outputContract':{'kind':'artifact','extensions':['.md']}}]}),'finish_reason':'stop','usage':{'total_tokens':1}}
                    return super().complete(messages,max_tokens,deadline)
            model=Plan(action);tools=Web(config);app=Platform(config,provider=model,tools=tools);model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['research'],'text':'Read one real observed source in this offline fixture, report limitations and publish.'})
                self.assertTrue(app.wait_for_idle(5));run=app.state['sessions']['group']['run']
                self.assertEqual(run['status'],'done',run.get('summary'));self.assertEqual(tools.fetches,1)
                self.assertEqual(len([c for c in run['calls'] if c['tool']=='web.fetch']),1)
                self.assertEqual(len([e for e in run['events'] if e['type']=='stage_action_blocked']),1)
                self.assertEqual(len([e for e in run['events'] if e['type']=='necessary_source_admitted']),1)
                self.assertEqual(run['budgetExtensions'],0);self.assertEqual(run['budget']['timeMs'],1800000)
                self.assertIsNone(run['budget']['tokens']);self.assertEqual(len(run['artifacts']),1)
                self.assertFalse(any(e['type']=='review_pass' for e in run['events']))
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()


if __name__=='__main__':unittest.main()
