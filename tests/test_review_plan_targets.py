"""Declared review targets bind an actual producer and existing handoff edge.

This validates plan structure, never the semantic completeness of the draft.
"""
import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from backend.agents import AGENTS
from backend.config import Config
from backend.engine import Platform
from backend.runtime import normalize_plan, ProtocolError, _compact_initial_context
from tests.test_engine import ScriptedProvider, task, final
from tests.test_execution_closure import LocalFiles, tool


class ReviewPlanTargetsTests(unittest.TestCase):
    def plan(self):
        return {'tasks':[
            task('Research','research'),
            {**task('Draft','executor',['Research']), 'outputContract':{'kind':'artifact','extensions':['.md']}},
            {**task('Review','reviewer',['Research','Draft']), 'reviewOf':['Draft']},
            task('Summary','coordinator',['Review'])]}

    def normalize(self, plan):
        counter=itertools.count()
        return normalize_plan(plan,list(AGENTS),lambda _: 'task-'+str(next(counter)),12)

    def test_targets_map_to_real_direct_dependencies_without_adding_access_or_success(self):
        plan=self.plan();before=copy.deepcopy(plan);rows=self.normalize(plan);by={t['title']:t for t in rows}
        review=by['Review'];draft=by['Draft']
        self.assertEqual(review['reviewOf'],[draft['id']])
        self.assertEqual(review['dependsOn'],[by['Research']['id'],draft['id']])
        self.assertIn('review',review['requiredCapabilities'])
        self.assertTrue(by['Summary']['finalSummary'])
        self.assertTrue(all(t['status']=='pending' and not t['outputs'] and 'validation' not in t for t in rows))
        self.assertEqual(plan,before)

    def test_legacy_tasks_without_declaration_are_preserved(self):
        plan=self.plan();plan['tasks'][2].pop('reviewOf');rows=self.normalize(plan)
        self.assertNotIn('reviewOf',rows[2])
        self.assertEqual(rows[2]['dependsOn'],[rows[0]['id'],rows[1]['id']])

    def test_malformed_unknown_duplicate_self_and_non_direct_targets_are_rejected(self):
        for targets in (None,[], 'Draft', [False], [{}], ['missing'], ['Draft','Draft'], ['Review'], ['Summary']):
            with self.subTest(targets=targets):
                plan=self.plan();plan['tasks'][2]['reviewOf']=targets
                with self.assertRaises(ProtocolError):self.normalize(plan)
        plan=self.plan();plan['tasks'][2]['dependsOn']=['Research']
        plan['tasks'][3]['dependsOn']=['Review','Draft']
        with self.assertRaisesRegex(ProtocolError,'direct dependsOn'):self.normalize(plan)

    def test_declared_review_requires_distinct_explicit_qualified_owners(self):
        for mode in ('same_owner','unknown_producer','unknown_reviewer','no_review_capability'):
            with self.subTest(mode=mode):
                # Three participants avoid the separate mandatory-four-role guard.
                plan={'tasks':[task('Draft','research'),{**task('Review','reviewer',['Draft']),'reviewOf':['Draft']}]}
                if mode=='same_owner':plan['tasks'][0]['agentId']='reviewer'
                elif mode=='unknown_producer':plan['tasks'][0].pop('agentId')
                elif mode=='unknown_reviewer':plan['tasks'][1].pop('agentId')
                else:plan['tasks'][1]['agentId']='coordinator'
                ids=itertools.count()
                with self.assertRaises(ProtocolError):
                    normalize_plan(plan,['research','reviewer','coordinator'],lambda _:str(next(ids)),12)

    def test_context_compaction_preserves_review_targets_exactly(self):
        rows=self.normalize(self.plan());review=rows[2]
        context={'runGoal':'Read current draft, then review','task':review,
                 'taskBoard':Platform._task_board({'id':'r','steps':rows}),
                 'groupHistory':[{'role':'agent','text':'history '*1000}]}
        compact=json.loads(_compact_initial_context(json.dumps(context),10000,minimal=True))
        self.assertEqual(compact['task']['reviewOf'],review['reviewOf'])
        self.assertEqual(compact['taskBoard']['tasks'][2]['reviewOf'],review['reviewOf'])


class ReviewPlanRuntimeTests(unittest.TestCase):
    def test_invalid_plan_is_corrected_before_draft_read_and_explicit_review(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Config(Path(directory),{'LLM_API_KEY':'offline-plan-test','MAX_RUN_SECONDS':'20'})
            good=[{**task('Draft','research'),'outputContract':{'kind':'artifact','extensions':['.md']}},
                  {**task('Review','reviewer',['Draft']),'reviewOf':['Draft']}]
            seen={}
            def review(index,context,messages):
                target=context['dependencyResults'][0]
                self.assertEqual(context['task']['reviewOf'],[target['id']])
                seen['target']=target['id']
                if index==0:
                    self.assertEqual(target['status'],'done')
                    self.assertFalse(any(e['type'].startswith('review_') for e in model.app.state['sessions']['group']['run']['events']))
                    return tool('file.read',path=context['authorizedArtifacts'][0]['path'])
                if index==1:
                    run=model.app.state['sessions']['group']['run']
                    read=next(c for c in reversed(run['calls']) if c['tool']=='file.read' and c['taskId']==context['task']['id'])
                    return {'type':'review','taskId':target['id'],'verdict':'pass',
                            'feedback':'Read the actual complete editorial fixture; no code-test claim.',
                            'evidenceCallIds':[read['id']]}
                return final('Current producer explicitly reviewed.')
            model=ScriptedProvider(good,{'Draft':[tool('file.write',path='report.md',content='Complete editorial fixture.'),
                {'type':'publish_artifact','path':'report.md'},final('Published actual draft.')],'Review':review})
            original=model.complete;plans=[]
            def complete(messages,max_tokens=None,deadline=None):
                if messages[0]['content'].startswith('Plan an executable task DAG'):
                    plans.append(copy.deepcopy(messages))
                    if len(plans)==1:
                        invalid=copy.deepcopy(good);invalid[1]['dependsOn']=[]
                        return {'content':json.dumps({'tasks':invalid}),'usage':{'total_tokens':1}}
                return original(messages)
            model.complete=complete
            app=Platform(config,provider=model,tools=LocalFiles(config));model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['research','reviewer'],'text':'Produce and independently review a report.'})
                self.assertTrue(app.wait_for_idle(6))
                run=app.state['sessions']['group']['run'];self.assertEqual(run['status'],'done')
                self.assertEqual(len(plans),2)
                self.assertIn('direct dependsOn',plans[1][-1]['content'])
                events=[e for e in run['events'] if e['type']=='review_pass']
                self.assertEqual(len(events),1);self.assertEqual(events[0]['payload']['taskId'],seen['target'])
                validation=events[0]['payload']['validation']
                self.assertEqual(validation['kind'],'editorial_assessment');self.assertFalse(validation['verified'])
                self.assertEqual(len(validation['evidenceCallIds']),1)
                self.assertEqual(len([e for e in run['events'] if e['type']=='dag_created']),1)
            finally:
                app.shutdown();app.wait_for_idle(3);app.repo.close()


if __name__=='__main__':unittest.main()
