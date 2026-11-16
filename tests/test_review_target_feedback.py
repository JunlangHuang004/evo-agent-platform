"""Invalid review targets are explained without selecting, granting or reviewing."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform,clock_ms
from backend.review_target_feedback import review_target_feedback
from backend.runtime import ProtocolError
from tests.test_engine import ScriptedProvider,task,final
from tests.test_execution_closure import LocalFiles,tool


class ReviewTargetFeedbackTests(unittest.TestCase):
    def fixture(self):
        f=json.loads((Path(__file__).parent/'fixtures/r8-self-target-review.json').read_text())
        run=f['run'];q=next(t for t in run['steps'] if t['id']==f['reviewerTaskId'])
        agents={actor:{'capabilities':['review'] if actor=='risk' else []} for actor in run['participants']}
        return run,q,agents

    def test_actual_r8_self_target_has_current_candidates_not_a_selected_verdict(self):
        r,q,a=self.fixture();before=copy.deepcopy((r,q));p=review_target_feedback(r,q,q['id'],a)
        self.assertFalse(p['ok']);self.assertFalse(p['actionExecuted']);self.assertEqual(p['error']['reason'],'self_target')
        self.assertEqual(p['reviewerTaskId'],q['id']);self.assertEqual(p['actionTemplate']['taskId'],'');self.assertEqual(p['actionTemplate']['verdict'],'')
        self.assertTrue(p['candidateTargets']);self.assertNotIn(q['id'],[t['taskId'] for t in p['candidateTargets']])
        self.assertTrue(all(t['status'] in ('done','partial') for t in p['candidateTargets']))
        final_task=next(t for t in r['steps'] if t.get('finalSummary'))
        self.assertEqual(final_task['status'],'pending');self.assertNotIn(final_task['id'],[t['taskId'] for t in p['candidateTargets']])
        self.assertEqual((r,q),before);self.assertNotIn('content',json.dumps(p));self.assertLess(len(json.dumps(p)),14000)

    def test_real_delivered_target_uses_original_path_and_missing_or_pending_only_get_guidance(self):
        r,q,a=self.fixture();target=next(t for t in r['steps'] if t['agentId']=='marketing')
        self.assertIsNone(review_target_feedback(r,q,target['id'],a))
        self.assertEqual(review_target_feedback(r,q,'not-a-task',a)['error']['reason'],'missing_target')
        pending=next(t for t in r['steps'] if t.get('finalSummary'))
        self.assertEqual(review_target_feedback(r,q,pending['id'],a)['error']['reason'],'target_not_delivered')
        self.assertIsNone(review_target_feedback(r,q,q['id'],{**a,'risk':{'capabilities':[]}}))
        self.assertIsNone(review_target_feedback(r,copy.deepcopy(q),q['id'],a),'not an actual current task object')

    def test_declared_targets_unknown_revision_and_foreign_artifact_do_not_gain_references(self):
        r,q,a=self.fixture();target=next(t for t in r['steps'] if t['agentId']=='marketing');q['reviewOf']=[target['id']]
        p=review_target_feedback(r,q,q['id'],a);self.assertEqual([t['taskId'] for t in p['candidateTargets']],[target['id']])
        r['artifacts']=[{**x,'runId':'foreign-run'} for x in r['artifacts']]
        self.assertEqual(review_target_feedback(r,q,q['id'],a)['candidateTargets'][0]['artifactRefs'],[])
        target['revision']=False;self.assertEqual(review_target_feedback(r,q,q['id'],a)['candidateTargets'],[])
        q['reviewOf']=[{}];self.assertIsNone(review_target_feedback(r,q,q['id'],a))


class ReviewTargetRuntimeTests(unittest.TestCase):
    def test_original_final_only_gate_does_not_offer_an_extra_review_repair_request(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder),{'LLM_API_KEY':'offline-review-target'});config.apply_time_only_profile()
            def action(i,c,m):
                run=app.state['sessions']['group']['run']
                if i==0:
                    run['budget']['deadlineAt']=clock_ms()+50000
                    for row in run['steps']:
                        if stage:=row.get('stageControl'):
                            stage['runDeadlineAt']=run['budget']['deadlineAt']
                            for key in ('synthesisAt','handoffAt','expectedHandoffBy'):stage[key]=min(stage[key],stage['runDeadlineAt'])
                    return {'type':'accept'}
                self.assertEqual(c['progressReceipt']['executionPhase'],'final_only')
                return {'type':'review','taskId':c['task']['id'],'verdict':'pass','feedback':'This must not execute.'}
            model=ScriptedProvider([task('QA','reviewer')],{'QA':action});original=model.complete
            model.complete=lambda messages,max_tokens=None,deadline=None:original(messages)
            app=Platform(config,provider=model,tools=LocalFiles(config))
            try:
                app.action({'action':'send','mode':'task','participants':['reviewer'],'text':'Respect the original closing boundary.'})
                self.assertTrue(app.wait_for_idle(4));run=app.state['sessions']['group']['run']
                self.assertEqual(model.counts['QA'],2);self.assertEqual(run['modelCalls'],3)
                self.assertFalse(any('candidateTargets' in m.get('text','') for m in run['transcript']))
                self.assertFalse(any(e['type']=='review_pass' for e in run['events']));self.assertNotEqual(run['status'],'done')
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()

    def test_real_self_target_refusal_survives_projection_then_new_correct_review_executes_once(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder),{'LLM_API_KEY':'offline-review-target','MAX_RETRIES':'0'});config.apply_time_only_profile()
            plan=[{**task('Brief','research'),'outputContract':{'kind':'artifact','extensions':['.md']}},task('QA','reviewer',['Brief'],['review'])]
            seen={}
            def qa(i,c,m):
                run=app.state['sessions']['group']['run'];q=next(t for t in run['steps'] if t['id']==c['task']['id'])
                if i==0:return tool('file.read',path=c['authorizedArtifacts'][0]['path'])
                if i==1:
                    result=json.loads(next(x['content'] for x in reversed(m) if x['content'].startswith('动作的真实结果：')).split('：',1)[1]);seen['read']=result['callId']
                    q['_messages'].append({'role':'user','content':'Optional obsolete fixture prose '+('x'*140000)})
                    return {'type':'review','taskId':q['id'],'verdict':'pass','feedback':'Wrong target proposal; no automatic correction allowed.'}
                if i==2:
                    packet=json.loads(m[-1]['content'].split('：',1)[1]);self.assertEqual(packet['error']['reason'],'self_target')
                    self.assertIn('protectedFeedback',run['calls'][-1]['contextCapacity'])
                    self.assertFalse(any(e['type']=='review_pass' for e in run['events']))
                    self.assertEqual(len([e for e in run['events'] if e['type']=='artifact_granted']),1)
                    target=next(t for t in packet['candidateTargets'] if t['title']=='Brief')
                    return {'type':'review','taskId':target['taskId'],'verdict':'pass','feedback':'Actually read current brief; editorial assessment only.','evidenceCallIds':[seen['read']]}
                return final('Explicit editorial review recorded; no executable acceptance claim.')
            model=ScriptedProvider(plan,{'Brief':[tool('file.write',path='brief.md',content='Actual local note.'),{'type':'publish_artifact','path':'brief.md'},final('Current brief produced.')],'QA':qa})
            original=model.complete;model.complete=lambda messages,max_tokens=None,deadline=None:original(messages)
            app=Platform(config,provider=model,tools=LocalFiles(config))
            try:
                app.action({'action':'send','mode':'task','participants':['research','reviewer'],'text':'Review an actual delivered text using its real producer ID.'})
                self.assertTrue(app.wait_for_idle(5));run=app.state['sessions']['group']['run'];p=next(t for t in run['steps'] if t['title']=='Brief')
                self.assertEqual(run['status'],'done');self.assertEqual(len([e for e in run['events'] if e['type']=='review_pass']),1)
                self.assertEqual(len([e for e in run['events'] if e['type']=='action_rejected' and e['payload'].get('reason')=='self_target']),1)
                self.assertFalse(p['validation']['verified']);self.assertEqual(p['validation']['kind'],'editorial_assessment')
                self.assertEqual(len(run['artifacts']),1);self.assertEqual(run['budgetExtensions'],0)
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()

    def test_missing_review_capability_still_rejects_before_candidate_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            app=Platform(Config(Path(folder),{'LLM_API_KEY':'offline-review-target'}))
            try:
                run={'id':'run','mode':'task','status':'running','participants':['research'],'steps':[],'artifacts':[]}
                q={'id':'q','agentId':'research','revision':0,'status':'running'};run['steps'].append(q)
                with self.assertRaisesRegex(ProtocolError,'review'):
                    app._review(run,q,{'taskId':'q','verdict':'pass','feedback':'Not authorized.'})
                self.assertNotIn('events',run);self.assertNotIn('validation',q)
            finally:app.shutdown();app.repo.close()


if __name__=='__main__':unittest.main()
