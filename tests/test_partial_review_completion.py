"""Only an actual partial-review admission has this one completion guardrail."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform,clock_ms
from backend.partial_review_completion import (make_partial_review_admission,
    partial_review_context,partial_review_completion_feedback)
from tests.test_engine import ScriptedProvider,task,final
from tests.test_execution_closure import LocalFiles,tool
from tests.test_time_only_runtime import wait_until


class PartialReviewCompletionTests(unittest.TestCase):
    def fixture(self):
        data=json.loads((Path(__file__).parent/'fixtures/r7-fund2-partial-review.json').read_text())
        run=data['run'];producer,reviewer=run['steps'];reviewer['status']='assigned'
        agents={'research':{'capabilities':['research']},'risk':{'capabilities':['review']}}
        admission=make_partial_review_admission(run,reviewer,agents,producer['finishedAt']+1)
        self.assertIsNotNone(admission)
        run['events'].append({'id':'prospective-new-admission','type':'partial_review_admitted',
                              'agentId':'risk','actor':'risk','payload':copy.deepcopy(admission)})
        reviewer['_partialReviewAdmission']={**admission,'admissionEventId':'prospective-new-admission'}
        reviewer['status']='running'
        return run,producer,reviewer,agents,data['actualRiskFinal']

    def test_actual_fund2_done_proposal_is_observed_not_rewritten_into_a_verdict(self):
        run,producer,reviewer,agents,action=self.fixture();before=copy.deepcopy((run,producer,reviewer,action))
        feedback=partial_review_completion_feedback(run,reviewer,action,agents)
        self.assertEqual(feedback['error']['code'],'partial_review_unresolved');self.assertFalse(feedback['actionExecuted'])
        self.assertEqual(feedback['partialDependencyReview']['targets'][0]['taskId'],producer['id'])
        self.assertEqual(feedback['partialDependencyReview']['targets'][0]['revision'],0)
        self.assertEqual(feedback['partialDependencyReview']['targets'][0]['producerStatus'],producer['status'])
        template=feedback['partialDependencyReview']['actionTemplate']
        self.assertEqual(template['taskId'],producer['id']);self.assertEqual(template['verdict'],'')
        self.assertEqual(feedback['maxAttemptsPerRevision'],1)
        self.assertIn('never makes a partial producer done',feedback['instruction'])
        self.assertEqual((run,producer,reviewer,action),before)

    def test_no_actual_admission_or_stale_unknown_and_foreign_binding_create_no_obligation(self):
        for mode in ('absent','missing_event','other_run','owner','reviewer_revision','boolean_revision',
                     'producer_revision','producer_failed','changed_sha','unknown_effect','missing_call','wrong_proposal'):
            with self.subTest(mode=mode):
                run,producer,reviewer,agents,action=self.fixture()
                if mode=='absent':reviewer.pop('_partialReviewAdmission')
                elif mode=='missing_event':run['events']=[]
                elif mode=='other_run':run['id']='other-run'
                elif mode=='owner':reviewer['agentId']='other'
                elif mode=='reviewer_revision':reviewer['revision']=1
                elif mode=='boolean_revision':reviewer['revision']=False
                elif mode=='producer_revision':producer['revision']=1
                elif mode=='producer_failed':producer['status']='failed'
                elif mode=='changed_sha':run['artifacts'][0]['sha256']='a'*64
                elif mode=='unknown_effect':reviewer['_pendingTool']={'id':'unknown'}
                elif mode=='missing_call':run['calls']=[c for c in run['calls'] if c['tool']!='llm.chat']
                else:action={**action,'text':'Not the actual recorded model proposal.'}
                self.assertIsNone(partial_review_completion_feedback(run,reviewer,action,agents))

    def test_only_genuine_current_explicit_editorial_assessment_resolves_the_obligation(self):
        run,producer,reviewer,agents,action=self.fixture()
        validation={'reviewerTaskId':reviewer['id'],'targetRevision':0,'verdict':'pass',
                    'artifactHashes':copy.deepcopy(reviewer['_partialReviewAdmission']['targets'][producer['id']]['artifactHashes']),
                    'feedback':'Source-limited editorial assessment only.','verified':False,
                    'kind':'editorial_assessment','assessed':True}
        producer['validation']=validation
        self.assertIsNotNone(partial_review_completion_feedback(run,reviewer,action,agents),'a fabricated field without an actual review event is insufficient')
        run['events'].append({'id':'review-event','type':'review_pass','actor':'risk','agentId':'risk',
                              'payload':{'taskId':producer['id'],'validation':copy.deepcopy(validation)}})
        self.assertIsNone(partial_review_completion_feedback(run,reviewer,action,agents))
        self.assertEqual(producer['status'],'partial');self.assertFalse(producer['validation']['verified'])
        validation.update(kind='code_test',assessed=True,verified=False)
        run['events'][-1]['payload']['validation']=copy.deepcopy(validation)
        self.assertIsNotNone(partial_review_completion_feedback(run,reviewer,action,agents),
                             'a recorded but rejected unverified executable pass is not a resolved review')


class PartialReviewCompletionRuntimeTests(unittest.TestCase):
    def setUp(self):self.folder=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(4);app.repo.close()
        self.folder.cleanup()

    def create(self,mode='rework'):
        config=Config(Path(self.folder.name)/str(len(self.apps)),{'LLM_API_KEY':'offline-review-guard',
            'MAX_RETRIES':'1','PARALLEL_TASKS':'2'});config.apply_time_only_profile()
        plan=[{**task('Brief','research'),'outputContract':{'kind':'artifact','extensions':['.md']}},
              {**task('Risk assessment','reviewer',['Brief'],['review']),'outputContract':{'kind':'text'}},
              task('Consumer','coordinator',['Brief','Risk assessment'])]
        counts={};seen={}
        def brief(i,c,m):
            revised=bool(c['task'].get('feedback'));key=int(revised);n=counts.get(key,0);counts[key]=n+1
            if n==0:return tool('file.write',path='brief-v'+str(key)+'.md',content='Revised complete fixture' if revised else 'Incomplete fixture with explicit gap')
            if n==1:return {'type':'publish_artifact','path':'brief-v'+str(key)+'.md'}
            if not revised:
                if n==2:
                    current=model.app.state['sessions']['group']['run'];t=next(t for t in current['steps'] if t['id']==c['task']['id']);now=clock_ms()
                    t['stageControl'].update(allocatedAt=now-4000,synthesisAt=now-3000,handoffAt=now-2000,expectedHandoffBy=now-1000)
                    if mode=='owner_change':
                        # A restored admission from an earlier owner, before a
                        # fresh actual assignment. No authorization is taken
                        # from these obsolete fixture fields.
                        q=next(t for t in current['steps'] if t['title']=='Risk assessment')
                        q['_partialReviewAdmission']={'reviewerAgentId':'previous-owner','reviewerRevision':0}
                        q['_partialReviewCompletionClarification']={'reviewerAgentId':'previous-owner','reviewerRevision':0}
                return final('Required evidence remains missing in this initial fixture.',status='partial')
            return final('Revised fixture criteria fulfilled; no semantic verdict invented.')
        def risk(i,c,m):
            current=model.app.state['sessions']['group']['run'];t=next(t for t in current['steps'] if t['id']==c['task']['id'])
            target=next(d for d in c['dependencyResults'] if d['title']=='Brief')
            if i==0:
                self.assertEqual(c['partialDependencyReview']['targets'][0]['taskId'],target['id'])
                self.assertEqual(c['partialDependencyReview']['targets'][0]['revision'],0)
                seen['deadline']=current['budget']['deadlineAt']
                if mode=='final_only':
                    current['budget']['deadlineAt']=clock_ms()+50000
                    for row in current['steps']:
                        if control:=row.get('stageControl'):
                            control['runDeadlineAt']=current['budget']['deadlineAt']
                            for name in ('synthesisAt','handoffAt','expectedHandoffBy'):control[name]=min(control[name],control['runDeadlineAt'])
                    return {'type':'accept'}
                return final('Actual risk prose without an explicit review action.')
            if mode=='final_only':return final('Prose only in the original final-only window.')
            if i==1:
                payload=json.loads(m[-1]['content'].split('：',1)[1])
                self.assertEqual(payload['error']['code'],'partial_review_unresolved')
                self.assertEqual(payload['partialDependencyReview']['actionTemplate']['taskId'],target['id'])
                self.assertIn('protectedFeedback',current['calls'][-1]['contextCapacity'])
                seen['feedbackBinding']=copy.deepcopy(t['_partialReviewCompletionClarification'])
                if mode=='repeat_done':return final('Still no review action; must stay partial.')
                return {'type':'review','taskId':target['id'],'verdict':'rework' if mode=='rework' else 'pass',
                        'feedback':'Please repair the explicit missing evidence in the current brief.' if mode=='rework' else
                                   'Editorial assessment notes that required evidence remains missing; no completion asserted.'}
            if mode=='pass':return final('Recorded editorial assessment; producer remains partial.')
            if i==2:
                handoff=json.loads(next(x['content'] for x in reversed(m) if x['content'].startswith('动作的真实结果：')).split('：',1)[1])
                self.assertTrue(handoff['reworkCompleted']);self.assertEqual(handoff['revision'],1)
                seen['newArtifact']=handoff['authorizedArtifacts'][0]
                return tool('file.read',path=seen['newArtifact']['path'])
            if i==3:return {'type':'review','taskId':target['id'],'verdict':'pass','feedback':'Read the actual revised fixture via newly granted import; text assessment only.'}
            return final('Current revised output explicitly reviewed; not executable proof.')
        def consumer(i,c,m):
            dep=next(d for d in c['dependencyResults'] if d['title']=='Brief')
            self.assertEqual(dep['status'],'done');self.assertEqual(dep['revision'],1)
            if i==0:return tool('file.read',path=next(a['path'] for a in c['authorizedArtifacts'] if a['name']=='brief-v1.md'))
            receipt=json.loads(next(x['content'] for x in reversed(m) if x['content'].startswith('动作的真实结果：')).split('：',1)[1])
            self.assertEqual(receipt['content'],'Revised complete fixture')
            return final('Consumed only the actual revised done snapshot.')
        model=ScriptedProvider(plan,{'Brief':brief,'Risk assessment':risk,'Consumer':consumer})
        original=model.complete
        model.complete=lambda messages,max_tokens=None,deadline=None:original(messages)
        app=Platform(config,provider=model,tools=LocalFiles(config));model.app=app;self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['research','reviewer','coordinator'],
                    'text':'Independently review partial text before producing the current-version consumer output.'})
        self.assertTrue(app.wait_for_idle(6));return app,model,seen

    def test_one_feedback_then_explicit_rework_new_revision_read_review_and_consumer(self):
        app,model,seen=self.create();run=app.state['sessions']['group']['run'];by={t['title']:t for t in run['steps']}
        self.assertEqual([by[n]['status'] for n in ('Brief','Risk assessment','Consumer')],['done','done','done'],run.get('summary'))
        self.assertEqual(by['Brief']['revision'],1);self.assertEqual(run['budget']['deadlineAt'],seen['deadline'])
        self.assertEqual(len([e for e in run['events'] if e['type']=='partial_review_completion_feedback']),1)
        self.assertEqual(len([e for e in run['events'] if e['type']=='review_rework']),1)
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),2)
        self.assertTrue(any(c['tool']=='file.read' and c['agentId']=='reviewer' and c['args']['path']==seen['newArtifact']['path'] for c in run['calls']))
        self.assertFalse(by['Brief']['validation']['verified']);self.assertEqual(by['Brief']['validation']['kind'],'editorial_assessment')
        self.assertEqual(app.repo.get_run(run['id'])['steps'][2]['_partialReviewCompletionClarification'],seen['feedbackBinding'])
        self.assertEqual(run['budgetExtensions'],0);self.assertIsNone(run['budget']['agentTurns'])

    def test_editorial_pass_does_not_mark_an_incomplete_producer_done_or_unlock_consumer(self):
        app,model,_=self.create('pass');run=app.state['sessions']['group']['run'];by={t['title']:t for t in run['steps']}
        self.assertEqual(by['Brief']['status'],'partial');self.assertEqual(by['Brief']['revision'],0)
        self.assertEqual(by['Risk assessment']['status'],'done');self.assertEqual(by['Consumer']['status'],'skipped')
        self.assertEqual(model.counts.get('Consumer',0),0);self.assertFalse(by['Brief']['validation']['verified'])

    def test_repeated_done_without_review_closes_partial_after_one_feedback(self):
        app,model,_=self.create('repeat_done');run=app.state['sessions']['group']['run'];by={t['title']:t for t in run['steps']}
        self.assertEqual(by['Risk assessment']['status'],'partial');self.assertEqual(by['Risk assessment']['error']['code'],'partial_review_unresolved')
        self.assertEqual(model.counts['Risk assessment'],2);self.assertEqual(by['Consumer']['status'],'skipped')
        self.assertEqual(len([e for e in run['events'] if e['type']=='partial_review_completion_feedback']),1)
        self.assertFalse(any(e['type'].startswith('review_') for e in run['events']))

    def test_final_only_closes_unresolved_review_without_an_extra_feedback_turn(self):
        app,model,_=self.create('final_only');run=app.state['sessions']['group']['run'];by={t['title']:t for t in run['steps']}
        self.assertEqual(by['Risk assessment']['status'],'partial');self.assertEqual(model.counts['Risk assessment'],2)
        self.assertFalse(any(e['type']=='partial_review_completion_feedback' for e in run['events']))
        self.assertEqual(len([e for e in run['events'] if e['type']=='partial_review_completion_unresolved']),1)
        self.assertEqual(by['Consumer']['status'],'skipped')

    def test_fresh_same_revision_assignment_rebinds_owner_without_resetting_spent_feedback(self):
        app,model,_=self.create('owner_change');run=app.state['sessions']['group']['run'];q=next(t for t in run['steps'] if t['title']=='Risk assessment')
        self.assertEqual(q['_partialReviewAdmission']['reviewerAgentId'],'reviewer')
        self.assertEqual(q['partialReviewAdmissionHistory'],[{'reviewerAgentId':'previous-owner','reviewerRevision':0}])
        self.assertEqual(q['_partialReviewCompletionClarification'],{'reviewerAgentId':'previous-owner','reviewerRevision':0})
        self.assertEqual(q['status'],'partial');self.assertEqual(model.counts['Risk assessment'],1)
        self.assertFalse(any(e['type']=='partial_review_completion_feedback' for e in run['events']))
        self.assertEqual(len([e for e in run['events'] if e['type']=='partial_review_admitted' and e['actor']=='reviewer']),1)

    def test_sibling_waiting_human_does_not_drop_an_actual_parallel_admission_or_done_guard(self):
        config=Config(Path(self.folder.name)/'waiting',{'LLM_API_KEY':'offline-review-wait','MAX_RETRIES':'1','PARALLEL_TASKS':'2'})
        config.apply_time_only_profile()
        plan=[task('Unrelated question','coordinator'),
              {**task('Brief','research'),'outputContract':{'kind':'artifact','extensions':['.md']}},
              {**task('Review','reviewer',['Brief'],['review']),'outputContract':{'kind':'text'}}]
        def brief(i,c,m):
            if i==0:
                self.assertTrue(wait_until(lambda:any(a['status']=='pending' for a in app.state['sessions']['group']['run']['approvals'])))
                return tool('file.write',path='incomplete.md',content='Explicit gap in this actual fixture.')
            if i==1:return {'type':'publish_artifact','path':'incomplete.md'}
            if i==2:
                current=app.state['sessions']['group']['run'];t=next(t for t in current['steps'] if t['id']==c['task']['id']);now=clock_ms()
                t['stageControl'].update(allocatedAt=now-4000,synthesisAt=now-3000,handoffAt=now-2000,expectedHandoffBy=now-1000)
            return final('Explicit source gap remains.',status='partial')
        def review(i,c,m):
            self.assertEqual(app.state['sessions']['group']['run']['status'],'waiting_human')
            self.assertTrue(c['partialDependencyReview']['targets'])
            if i==0:return final('Prose-only assessment during unrelated human wait.')
            self.assertEqual(json.loads(m[-1]['content'].split('：',1)[1])['error']['code'],'partial_review_unresolved')
            return final('Cannot resolve the missing evidence; no verdict fabricated.',status='partial')
        model=ScriptedProvider(plan,{'Unrelated question':[{'type':'ask_human','question':'Unrelated offline fixture decision?','choices':[]},final('Actual fixture answer received.')],
                                    'Brief':brief,'Review':review})
        original=model.complete;model.complete=lambda messages,max_tokens=None,deadline=None:original(messages)
        app=Platform(config,provider=model,tools=LocalFiles(config));model.app=app;self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['coordinator','research','reviewer'],
                    'text':'An unrelated human wait must not bypass actual partial-review accounting.'})
        self.assertTrue(wait_until(lambda:any(t['title']=='Review' and t['status']=='partial' for t in app.snapshot()['sessions']['group']['run']['steps']),5))
        run=app.state['sessions']['group']['run'];deadline=run['budget']['deadlineAt']
        self.assertEqual(len([e for e in run['events'] if e['type']=='partial_review_admitted']),1)
        self.assertEqual(len([e for e in run['events'] if e['type']=='partial_review_completion_feedback']),1)
        approval=next(a for a in run['approvals'] if a['status']=='pending')
        app.action({'action':'approval','approvalId':approval['id'],'choiceId':'answer','answer':'Actual offline fixture reply.'})
        self.assertTrue(app.wait_for_idle(4));self.assertEqual(run['budget']['deadlineAt'],deadline)
        self.assertEqual(run['status'],'partial');self.assertEqual(model.counts['Review'],2)


if __name__=='__main__':unittest.main()
