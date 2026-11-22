"""Actual scripted execution: duplicate review is not progress or completion."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.delivery import create_delivery,DeliveryError
from backend.engine import Platform,clock_ms
from backend.review_lifecycle import current_pass_receipt,review_context,record_duplicate,current_repeat_state,report_work_signature
from backend.teams import get_team_agents
from tests.test_engine import ScriptedProvider,task,final
from tests.test_execution_closure import LocalFiles,tool


class ReviewLifecycleRuntimeTests(unittest.TestCase):
    def run_case(self,qa,*,qa_contract=None,limit=None,source=False,package_names=None):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        config=Config(Path(folder.name),{'LLM_API_KEY':'offline-review-lifecycle','MAX_RETRIES':'0'})
        config.apply_time_only_profile()
        if limit is not None:config.max_agent_turns=limit
        plan=[];scripts={}
        if source:
            plan.append({**task('Source','coordinator'),'outputContract':{'kind':'artifact','extensions':['.md']}})
            scripts['Source']=[tool('file.write',path='source.md',content='Original source body, never implicitly shared.'),
                               {'type':'publish_artifact','path':'source.md'},final('Source produced.')]
        for title,agent in (('Proposal','research'),('Intro','executor')):
            plan.append({**task(title,agent,['Source'] if source else []),'outputContract':{'kind':'artifact','extensions':['.md']}})
            scripts[title]=[tool('file.write',path=title+'.md',content='Actual '+title+' material.'),
                            {'type':'publish_artifact','path':title+'.md'},final(title+' produced.')]
        plan.append({**task('QA','reviewer',['Proposal','Intro'],['review']),'reviewOf':['Proposal','Intro'],
                     **({'outputContract':qa_contract} if qa_contract else {})})
        plan.append(task('Release','coordinator',['Proposal','Intro','QA']))
        def release(i,c,m):
            if i==0:
                artifacts=c['authorizedArtifacts']
                if package_names is not None:artifacts=[a for a in artifacts if a['name'] in package_names]
                return {'type':'package_delivery','artifactIds':[a['id'] for a in artifacts],
                        'entrypoint':artifacts[0]['name'],'instructions':'Open the published Markdown documents.'}
            return final('Actual package created after explicit QA final.')
        box={}
        scripts.update(QA=lambda i,c,m:qa(i,c,m,box['app']),Release=release)
        model=ScriptedProvider(plan,scripts);original=model.complete
        model.complete=lambda messages,max_tokens=None,deadline=None:original(messages)
        app=Platform(config,provider=model,tools=LocalFiles(config));box['app']=app
        def clean():app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.addCleanup(clean)
        app.action({'action':'send','mode':'task','participants':['coordinator','research','executor','reviewer'],
                    'text':'Deliver two real text artifacts, review their exact current versions, then package them.'})
        self.assertTrue(app.wait_for_idle(8))
        return app,app.state['sessions']['group']['run'],model

    @staticmethod
    def decision(c,index=0,feedback='Actual editorial finding.'):
        return {'type':'review','taskId':c['task']['reviewOf'][index],'verdict':'pass','feedback':feedback}

    @staticmethod
    def qa_task(run):return next(t for t in run['steps'] if t['title']=='QA')

    def test_alternating_passes_keep_two_actual_decisions_then_explicit_final_releases_real_package(self):
        def qa(i,c,m,app):
            if i<2:return tool('file.read',path=c['authorizedArtifacts'][i]['path'])
            if i<4:return self.decision(c,i-2)
            progress=c['reviewProgress']
            self.assertEqual(len(progress['completedTargetIds']),2);self.assertFalse(progress['remainingTargetIds'])
            if i<6:return self.decision(c,i-4,'Different words '+str(i))
            self.assertTrue(progress['duplicateRecovery']['finalRequired'])
            self.assertTrue(any(x['content'].startswith('最后收尾约束：审核重复恢复。') for x in m))
            return final('Explicit QA final from actual editorial findings; no executable-test claim.')
        app,run,model=self.run_case(qa)
        self.assertEqual(run['status'],'done');self.assertEqual(model.counts['QA'],7)
        self.assertEqual(len([e for e in run['events'] if e['type']=='review_pass']),2)
        self.assertEqual(len(run['deliveries']),1);self.assertEqual(model.counts['Release'],2)
        self.assertEqual(run['budgetExtensions'],0)
        results=[json.loads(t['text']) for t in run['transcript'] if t.get('kind')=='action_result' and t.get('action')=='review']
        self.assertEqual(sum(bool(x.get('duplicateReview')) for x in results),2)
        for x in results[-2:]:
            self.assertFalse(x['actionExecuted']);self.assertFalse(x['validation']['verified'])
            self.assertEqual(x['validation']['feedback'],'Actual editorial finding.')
        self.assertEqual([r['context']['progressReceipt']['turnsWithoutNewOutputOrTest'] for r in model.records if r['title']=='QA'][-2:],[1,2])

    def test_ignoring_checkpoint_stops_before_tool_without_fabricating_final(self):
        def qa(i,c,m,app):
            if i<4:return self.decision(c,i%2,'Pass '+str(i))
            return tool('file.write',path='must-not-write.md',content='Should not execute.')
        app,run,model=self.run_case(qa)
        q=self.qa_task(run)
        self.assertEqual(model.counts['QA'],5);self.assertEqual(q['status'],'failed')
        self.assertEqual(q['finalizationFailure']['code'],'review_final_required');self.assertEqual(q['result'],'')
        self.assertNotEqual(run['status'],'done')
        self.assertTrue(all(d['semanticQuality']=='unassessed' for d in run['deliveries']))
        self.assertFalse((app.tools.workspace('reviewer',run['id'])/'must-not-write.md').exists())
        self.assertEqual(len([e for e in run['events'] if e['type']=='review_pass']),2)

    def test_missing_target_cannot_become_done_and_repeated_identical_pass_does_not_preempt_final(self):
        def qa(i,c,m,app):
            if i<4:return self.decision(c,0)
            self.assertEqual(len(c['reviewProgress']['remainingTargetIds']),1)
            self.assertTrue(c['reviewProgress']['duplicateRecovery']['finalRequired'])
            return final('Incorrect claim that both are complete.')
        _,run,model=self.run_case(qa)
        q=self.qa_task(run)
        self.assertEqual(model.counts['QA'],5);self.assertEqual(q['status'],'partial')
        self.assertEqual(q['error']['code'],'review_incomplete');self.assertNotEqual(run['status'],'done')
        self.assertEqual(len([e for e in run['events'] if e['type']=='review_pass']),1)
        other=next(t for t in run['steps'] if t['title']=='Intro');self.assertFalse(other.get('validation'))

    def test_review_pass_cannot_satisfy_missing_report_contract(self):
        def qa(i,c,m,app):
            if i<4:return self.decision(c,i%2)
            return final('I reviewed both, but never published the required report.')
        _,run,model=self.run_case(qa,qa_contract={'kind':'artifact','extensions':['.md']})
        q=self.qa_task(run);self.assertEqual(q['status'],'partial');self.assertEqual(q['error']['code'],'output_contract')
        self.assertFalse(q['outputs']);self.assertEqual(model.counts['QA'],5);self.assertNotEqual(run['status'],'done')

    def test_actual_report_work_resets_recovery_and_original_contract_still_checks_publication(self):
        def qa(i,c,m,app):
            if i<2:return self.decision(c,i)
            if i==2:return self.decision(c,0)
            if i==3:return tool('file.write',path='review.md',content='Actual assessment of the two current products.')
            if i==4:
                self.assertNotIn('duplicateRecovery',c['reviewProgress'])
                return self.decision(c,1)
            if i==5:return {'type':'publish_artifact','path':'review.md'}
            self.assertNotIn('duplicateRecovery',c['reviewProgress'])
            return final('Published the actual assessment and explicitly hand off.')
        _,run,model=self.run_case(qa,qa_contract={'kind':'artifact','extensions':['.md']})
        q=self.qa_task(run);self.assertEqual(q['status'],'done');self.assertEqual(q['outputValidation']['status'],'passed')
        self.assertEqual(model.counts['QA'],7);self.assertEqual(len([e for e in run['events'] if e['type']=='review_pass']),2)
        self.assertEqual(len(run['deliveries']),1)

    def test_ancestor_grant_and_denied_read_do_not_reset_duplicate_recovery(self):
        def qa(i,c,m,app):
            inputs=c['reviewProgress']['reviewInputs'];self.assertTrue(inputs)
            self.assertTrue(all(x['accessible'] is True for x in inputs))
            self.assertNotIn('Original source body',json.dumps(c['reviewProgress']))
            if i<2:return self.decision(c,i)
            if i==2:return self.decision(c,0)
            if i==3:return tool('file.read',path=c['authorizedArtifacts'][0]['path'])
            if i==4:return tool('file.read',path='../coordinator/source.md')
            if i==5:
                self.assertEqual(c['reviewProgress']['duplicateRecovery']['duplicatesWithoutProgress'],1)
                return self.decision(c,1)
            self.assertTrue(c['reviewProgress']['duplicateRecovery']['finalRequired'])
            return final('Editorial assessment handed off; underlying source was not accessible.',status='partial')
        app,run,model=self.run_case(qa,source=True)
        q=self.qa_task(run);self.assertEqual(model.counts['QA'],7);self.assertEqual(q['status'],'partial')
        source=next(a for a in run['artifacts'] if a['name']=='source.md')
        self.assertTrue(app.artifacts.can_access(source['id'],'reviewer',run['id']))
        self.assertTrue(any(e['type']=='artifact_granted' and e['payload'].get('artifactId')==source['id']
                             and e['payload'].get('toAgent')=='reviewer' for e in run['events']))

    def test_identical_report_republication_does_not_reset_final_handoff(self):
        def qa(i,c,m,app):
            if i==0:return tool('file.write',path='review.md',content='Actual assessment of the two current products.')
            if i==1:return {'type':'publish_artifact','path':'review.md'}
            if i<4:return self.decision(c,i-2)
            if i==4:return self.decision(c,0)
            if i==5:return tool('file.read',path='review.md')
            if i==6:return {'type':'publish_artifact','path':'review.md'}
            if i==7:
                self.assertEqual(c['reviewProgress']['duplicateRecovery']['duplicatesWithoutProgress'],1)
                return self.decision(c,1)
            self.assertTrue(c['reviewProgress']['duplicateRecovery']['finalRequired'])
            return final('Explicit handoff of the already published assessment.')
        # The release explicitly packages the two requested products. Including
        # both distinct review IDs at one archive path is a real package conflict,
        # unrelated to whether republishing is new reviewer work.
        app,run,model=self.run_case(qa,qa_contract={'kind':'artifact','extensions':['.md']},
                                  package_names={'Proposal.md','Intro.md'})
        q=self.qa_task(run)
        self.assertEqual(run['status'],'done')
        self.assertEqual(q['status'],'done');self.assertEqual(model.counts['QA'],9)
        reports=[a for a in run['artifacts'] if a.get('taskId')==q['id']]
        self.assertEqual(len(reports),2)
        self.assertNotEqual(reports[0]['id'],reports[1]['id'])
        self.assertEqual(reports[0]['sha256'],reports[1]['sha256'])
        self.assertEqual(len([e for e in run['events'] if e['type']=='review_pass']),2)
        self.assertEqual(len(run['deliveries']),1)
        self.assertEqual({f['path'] for f in run['deliveries'][0]['files']},{'Proposal.md','Intro.md'})
        release=next(t for t in run['steps'] if t['title']=='Release')
        with self.assertRaisesRegex(DeliveryError,'same product path'):
            create_delivery(run,release,app.artifacts,[reports[1]['id'],reports[0]['id']],
                            'review.md','Open the selected reports.')

    def test_report_progress_uses_bytes_and_path_but_preserves_new_publication_and_edits(self):
        files={'review.md':'a'*64}
        first={'id':'first','path':'review.md','name':'review.md','sha256':'a'*64}
        republished={**first,'id':'second','createdAt':12345}
        before=report_work_signature(files,[])
        published=report_work_signature(files,[first])
        self.assertNotEqual(before,published)
        self.assertEqual(published,report_work_signature(files,[first,republished]))
        self.assertEqual(published,report_work_signature(files,[first,{**republished,'name':'display-only-title'}]))
        self.assertEqual(published,report_work_signature(files,[republished,first]))
        moved={**first,'id':'moved','path':'archive/review.md'}
        self.assertNotEqual(published,report_work_signature(files,[first,moved]))
        changed={**first,'id':'changed','sha256':'b'*64}
        self.assertNotEqual(published,report_work_signature(files,[first,changed]))
        self.assertNotEqual(published,report_work_signature({'review.md':'b'*64},[first]))

    def test_original_deadline_final_gate_takes_precedence_and_adds_no_repair(self):
        def qa(i,c,m,app):
            if i<3:return self.decision(c,i%2)
            if i==3:
                run=app.state['sessions']['group']['run'];run['budget']['deadlineAt']=clock_ms()+50000
                for row in run['steps']:
                    if stage:=row.get('stageControl'):
                        stage['runDeadlineAt']=run['budget']['deadlineAt']
                        for key in ('synthesisAt','handoffAt','expectedHandoffBy'):stage[key]=min(stage[key],stage['runDeadlineAt'])
                return self.decision(c,1)
            self.assertEqual(c['progressReceipt']['executionPhase'],'final_only')
            self.assertFalse(any(x['content'].startswith('最后收尾约束：审核重复恢复。') for x in m))
            return tool('file.write',path='late.md',content='Do not execute.')
        app,run,model=self.run_case(qa)
        q=self.qa_task(run);self.assertEqual(model.counts['QA'],5);self.assertEqual(q['error']['code'],'budget')
        self.assertFalse((app.tools.workspace('reviewer',run['id'])/'late.md').exists());self.assertFalse(run['budgetExtensions'])

    def test_out_of_scope_duplicate_keeps_original_finite_loop_guard(self):
        seen={}
        def qa(i,c,m,app):
            if i==0:
                seen['target']=c['task']['reviewOf'][1]
                self.qa_task(app.state['sessions']['group']['run'])['reviewOf']=[c['task']['reviewOf'][0]]
            return {'type':'review','taskId':seen['target'],'verdict':'pass','feedback':'Same out-of-scope pass.'}
        _,run,model=self.run_case(qa)
        q=self.qa_task(run);self.assertEqual(q['status'],'failed');self.assertEqual(model.counts['QA'],3)
        self.assertEqual(q['error']['code'],'protocol');self.assertIn('重复动作',q['error']['message'])
        self.assertNotIn('_reviewRepeat',q)
        self.assertEqual(len([e for e in run['events'] if e['type']=='review_pass']),1)

    def test_new_target_revision_accepts_new_explicit_decision_without_reusing_old_pass(self):
        def qa(i,c,m,app):
            if i==2:
                target=next(t for t in app.state['sessions']['group']['run']['steps'] if t['id']==c['task']['reviewOf'][0])
                # Simulate the host observing an independently revised producer
                # while the reviewer is generating its next decision.
                target['revision']+=1
            if i<3:return self.decision(c,0)
            if i==3:
                self.assertNotIn('duplicateRecovery',c['reviewProgress'])
                return self.decision(c,1)
            return final('Explicit current-revision decisions delivered.')
        _,run,model=self.run_case(qa)
        q=self.qa_task(run);self.assertEqual(q['status'],'partial');self.assertEqual(model.counts['QA'],5)
        self.assertEqual(q['error']['code'],'stale_dependency')
        events=[e for e in run['events'] if e['type']=='review_pass']
        self.assertEqual(len(events),3);self.assertEqual(events[1]['payload']['validation']['targetRevision'],1)

    def test_malformed_final_at_checkpoint_is_not_repaired_or_coerced_into_completion(self):
        for malformed in ('not JSON',{'type':'final','status':'done','text':''}):
            with self.subTest(malformed=malformed):
                def qa(i,c,m,app):return self.decision(c,i%2) if i<4 else malformed
                _,run,model=self.run_case(qa)
                q=self.qa_task(run);self.assertEqual(q['status'],'failed');self.assertEqual(model.counts['QA'],5)
                self.assertEqual(q['result'],'');self.assertFalse(q.get('_protocolRepairs'))

    def test_checkpoint_invalidated_during_generation_does_not_leave_stale_final_constraint(self):
        def qa(i,c,m,app):
            if i<4:return self.decision(c,i%2)
            if i==4:
                self.assertTrue(any(x['content'].startswith('最后收尾约束：审核重复恢复。') for x in m))
                target=next(t for t in app.state['sessions']['group']['run']['steps'] if t['id']==c['task']['reviewOf'][0])
                target['revision']+=1
                return self.decision(c,0)
            self.assertFalse(any(x['content'].startswith('最后收尾约束：审核重复恢复。') for x in m))
            return final('New explicit decision recorded; old dependency version still requires reconciliation.',status='partial')
        _,run,model=self.run_case(qa)
        q=self.qa_task(run);self.assertEqual(model.counts['QA'],6);self.assertEqual(q['status'],'partial')
        self.assertFalse(q.get('finalizationFailure'))
        self.assertEqual(len([e for e in run['events'] if e['type']=='review_pass']),3)

    def test_actual_reject_receipt_remains_visible_with_source_access_diagnostics(self):
        def qa(i,c,m,app):
            if i==0:return {**self.decision(c,0),'verdict':'reject','feedback':'Actual rejection; source access is insufficient.'}
            receipt=json.loads(m[-1]['content'].split('：',1)[1])
            self.assertEqual(receipt['validation']['verdict'],'reject')
            self.assertFalse(receipt['validation']['verified']);self.assertEqual(receipt['status'],'failed')
            self.assertTrue(c['reviewProgress']['reviewInputs'])
            self.assertIn('protectedFeedback',app.state['sessions']['group']['run']['calls'][-1]['contextCapacity'])
            return final('Explicitly report the rejected draft and source-access gap.',status='partial')
        _,run,model=self.run_case(qa,source=True)
        self.assertEqual(model.counts['QA'],2);self.assertEqual(self.qa_task(run)['status'],'partial')
        self.assertEqual(len([e for e in run['events'] if e['type']=='review_reject']),1)


class ReviewLifecycleBindingTests(unittest.TestCase):
    def setUp(self):
        self.agents={'reviewer':{'capabilities':['review']},'producer':{'capabilities':[]}}
        self.q={'id':'qa','agentId':'reviewer','revision':0,'status':'running','dependsOn':['p'],'reviewOf':['p'],'_reviewWorkSignature':'work'}
        self.p={'id':'p','agentId':'producer','revision':0,'status':'done','outputs':['artifact']}
        self.artifact={'id':'artifact','runId':'run','taskId':'p','ownerAgentId':'producer','name':'p.md','sha256':'a'*64}
        self.validation={'verdict':'pass','feedback':'Actual finding.','verified':False,'kind':'editorial_assessment','assessed':True,
                         'reviewerTaskId':'qa','reviewerRevision':0,'targetRevision':0,'artifactHashes':{'artifact':'a'*64},'evidenceCallIds':[]}
        self.p['validation']=copy.deepcopy(self.validation)
        self.run={'id':'run','steps':[self.q,self.p],'artifacts':[self.artifact],
                  'events':[{'id':'event','type':'review_pass','agentId':'reviewer','actor':'reviewer',
                             'payload':{'taskId':'p','validation':copy.deepcopy(self.validation)}}]}

    def receipt(self,**kwargs):
        return current_pass_receipt(self.run,self.q,self.p,self.agents,kwargs.get('dependency',{'status':'passed'}),kwargs.get('verified',False))

    def test_current_receipt_is_read_only_and_requires_true_event_owner_revision_bytes(self):
        before=copy.deepcopy(self.run);self.assertIsNotNone(self.receipt());self.assertEqual(before,self.run)
        mutations=[lambda:self.q.update(revision=1),lambda:self.p.update(revision=1),
                   lambda:self.p.update(agentId='reviewer'),lambda:self.artifact.update(sha256='b'*64),
                   lambda:self.artifact.update(ownerAgentId='reviewer'),lambda:self.artifact.update(runId='another'),
                   lambda:self.run.update(events=[]),lambda:self.run['events'][0].update(actor='producer'),
                   lambda:self.p['validation'].pop('reviewerRevision'),lambda:self.p['validation'].update(reviewerRevision=False),
                   lambda:self.p['validation'].update(feedback='rewritten-without-event'),
                   lambda:self.p['validation'].update(verified=True,kind='code_test')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.setUp();mutate();self.assertIsNone(self.receipt())
        self.setUp();self.assertIsNone(self.receipt(dependency={'status':'stale'}))

    def test_scope_change_and_real_report_work_invalidate_checkpoint_but_unrelated_reads_do_not(self):
        def context():return review_context(self.run,self.q,self.agents,lambda t:self.receipt(),lambda *x:False)
        c=context();record_duplicate(self.q,c);record_duplicate(self.q,c)
        self.assertTrue(current_repeat_state(self.q,c)['finalRequired'])
        self.q['_progressSignature']='different_read_window'
        self.assertTrue(current_repeat_state(self.q,context())['finalRequired'])
        self.q['_reviewWorkSignature']='new_report_bytes';self.assertIsNone(current_repeat_state(self.q,context()))
        self.q['_reviewWorkSignature']='work';self.p['revision']=1
        self.assertIsNone(current_repeat_state(self.q,context()))

    def test_actual_r9_twenty_five_event_proposals_record_only_two_current_passes(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/r9-fund1-repeated-pass.json').read_text())
        run=fixture['run'];q=next(t for t in run['steps'] if t['id']==fixture['reviewerTaskId'])
        with tempfile.TemporaryDirectory() as folder:
            app=Platform(Config(Path(folder),{'LLM_API_KEY':'offline-r9-repeat'}),agents=get_team_agents('fund'),team_id='fund')
            try:
                results=[app._review(run,q,action) for action in fixture['actions']]
                self.assertEqual(len(results),25)
                self.assertTrue(all(row['ok'] for row in results))
                self.assertEqual(sum(bool(row.get('duplicateReview')) for row in results),23)
                self.assertEqual(len([e for e in run['events'] if e['type']=='review_pass']),2)
                self.assertEqual(q['status'],'running');self.assertNotIn('result',q)
                self.assertTrue(q['_reviewRepeat']['finalRequired'])
                self.assertFalse(any(e['type']=='artifact_granted' for e in run['events']))
                for target in run['steps'][1:]:
                    self.assertFalse(target['validation']['verified'])
                    self.assertEqual(target['validation']['kind'],'editorial_assessment')
            finally:app.shutdown();app.repo.close()


class ReviewLifecycleExecutableEvidenceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_browser_progress_coverage import BrowserProgressCoverageTests
        self.fixture=BrowserProgressCoverageTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        f=self.fixture;f.task['reviewOf']=[f.target['id']];f.run['calls']=[f.call()]
        self.action={'type':'review','taskId':f.target['id'],'verdict':'pass','feedback':'Actual current browser assertions.',
                     'evidenceCallIds':[f.run['calls'][0]['id']]}
        self.assertTrue(f.app._review(f.run,f.task,self.action)['ok'])

    def test_real_current_browser_pass_is_idempotent(self):
        f=self.fixture;result=f.app._review(f.run,f.task,self.action)
        self.assertTrue(result['duplicateReview']);self.assertTrue(result['validation']['verified'])
        self.assertEqual(len([e for e in f.run['events'] if e['type']=='review_pass']),1)

    def test_failed_unknown_bool_and_foreign_execution_never_becomes_current_receipt(self):
        f=self.fixture;original=copy.deepcopy(f.run['calls'][0])
        changes=[('result',k,True) for k in ('timedOut','notExecuted','outcomeUnknown','deadlineExceeded','cancelled','outputLimitExceeded','modelViewOnly')]
        changes += [('result','actionExecuted',False),('result','exitCode',False),('call','revision',False),('call','revision',1),
                    ('call','runId','foreign'),('call','agentId','executor'),('browser','error','Assertion failed'),
                    ('browser','ok',False),('browser','blockedRequests',['blocked resource']),('bound','targetRevision',False)]
        for where,key,value in changes:
            with self.subTest(where=where,key=key):
                call=copy.deepcopy(original)
                row={'call':call,'result':call['result'],'browser':call['result']['browser'],
                     'bound':call['reviewTargets'][f.target['id']]}[where]
                row[key]=value;f.run['calls']=[call]
                self.assertIsNone(f.app._current_review_receipt(f.run,f.task,f.target))

    def test_old_browser_success_cannot_be_combined_with_current_python_success(self):
        f=self.fixture;browser=copy.deepcopy(f.run['calls'][0]);browser['revision']=-1
        python=copy.deepcopy(browser);python.update(id='current-python',revision=0)
        python['result']={'ok':True,'runtime':'python','exitCode':0}
        f.run['calls']=[browser,python]
        # Preserve an event-backed validation which names both real call IDs;
        # no single current call proves current browser acceptance.
        v=f.target['validation'];v['evidenceCallIds'].append(python['id'])
        f.run['events'][0]['payload']['validation']=copy.deepcopy(v)
        self.assertIsNone(f.app._current_review_receipt(f.run,f.task,f.target))


if __name__=='__main__':unittest.main()
