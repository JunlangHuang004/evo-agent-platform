"""Plan visibility and bounded new scopes; scripted models and real local state."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform, PlannedTaskConflict
from backend.runtime import ProtocolError, compress_messages
from backend.tools import ToolGateway
from tests.test_rework_delivery_loop import ReworkModel, LocalBrowser


class LocalFiles(ToolGateway):
    def capabilities(self):return {}


class BoardModel:
    def __init__(self,distinct=False,unplanned=False):
        self.distinct=distinct;self.unplanned=unplanned;self.counts={};self.contexts=[];self.conflict=None
    def complete(self,messages,max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            tasks=[{'id':'design','title':'Design','agentId':'coordinator'}]
            if not self.unplanned:tasks.append({'id':'build','title':'Build','agentId':'executor','dependsOn':['design']})
            action={'tasks':tasks}
        else:
            context=json.loads(messages[1]['content']);title=context['task']['title'];self.contexts.append(copy.deepcopy(context))
            n=self.counts.get(title,0);self.counts[title]=n+1
            results=[json.loads(m['content'][len('动作的真实结果：'):]) for m in messages if m['content'].startswith('动作的真实结果：')]
            if title=='Design':
                if n==0:
                    spec={'title':'Different title does not justify duplicate work','agentId':'executor'}
                    if self.distinct:spec['distinctScope']='Investigate a separate feasibility question; do not implement the planned product.'
                    action={'type':'delegate','task':spec}
                elif n==1 and (self.distinct or self.unplanned):action={'type':'await_task','taskId':results[-1]['task']['id']}
                else:
                    if n==1:self.conflict=copy.deepcopy(results[-1])
                    action={'type':'final','text':'Own design is complete; scheduled work may now start.'}
            else:action={'type':'final','text':'Actual scheduled task completed.'}
        return {'content':json.dumps(action),'finish_reason':'stop','usage':{'total_tokens':5}}


class TaskBoardTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.app=None
    def tearDown(self):
        if self.app:self.app.shutdown();self.app.wait_for_idle(4);self.app.repo.close()
        self.temp.cleanup()
    def create(self,model,tools=None):
        config=Config(Path(self.temp.name),{'LLM_API_KEY':'offline-board','MAX_RETRIES':'1','MAX_AGENT_TURNS':'12',
            'MAX_MODEL_CALLS':'50','MAX_RUN_TOKENS':'500000','MAX_CONTEXT_CHARS':'80000'})
        self.app=Platform(config,provider=model,tools=tools or LocalFiles(config));return self.app
    def run_model(self,model):
        app=self.create(model);app.action({'action':'send','mode':'task','participants':['coordinator','executor'],
                                        'text':'Use the existing plan and delegate only genuinely uncovered work.'})
        self.assertTrue(app.wait_for_idle(4));return app.state['sessions']['group']['run']

    def test_existing_downstream_task_is_visible_and_duplicate_feedback_unblocks_original(self):
        model=BoardModel();run=self.run_model(model);self.assertEqual(run['status'],'done')
        business=[t for t in run['steps'] if t.get('kind')!='planning'];self.assertEqual(len(business),2)
        design=next(t for t in business if t['title']=='Design');build=next(t for t in business if t['title']=='Build')
        self.assertEqual(design['childIds'],[]);self.assertEqual(model.counts['Build'],1)
        first=model.contexts[0]['taskBoard'];self.assertEqual(first['runId'],run['id'])
        self.assertEqual(next(t for t in first['tasks'] if t['id']==build['id'])['dependsOn'],[design['id']])
        self.assertEqual(model.conflict['error']['code'],'already_planned')
        self.assertEqual(model.conflict['existingTaskId'],build['id']);self.assertTrue(model.conflict['finishParentFirst'])
        build_context=next(c for c in model.contexts if c['task']['title']=='Build')
        self.assertEqual(next(t for t in build_context['taskBoard']['tasks'] if t['id']==design['id'])['status'],'done')

    def test_legitimate_unplanned_child_and_explicit_distinct_scope_remain_possible(self):
        for distinct,unplanned in ((True,False),(False,True)):
            with self.subTest(distinct=distinct,unplanned=unplanned):
                if self.app:self.app.shutdown();self.app.wait_for_idle(3);self.app.repo.close();self.app=None
                # Each independent scenario needs its own persisted namespace.
                self.temp.cleanup();self.temp=tempfile.TemporaryDirectory()
                model=BoardModel(distinct,unplanned);run=self.run_model(model)
                self.assertEqual(run['status'],'done');child=next(t for t in run['steps'] if t.get('parentId'))
                design=next(t for t in run['steps'] if t['title']=='Design')
                parent_contexts=[c for c in model.contexts if c['task']['title']=='Design']
                self.assertIn(child['id'],parent_contexts[1]['task']['childIds'])
                self.assertEqual(next(t for t in parent_contexts[-1]['taskBoard']['tasks'] if t['id']==child['id'])['status'],'done')
                if distinct:
                    planned=next(t for t in run['steps'] if t['title']=='Build')
                    self.assertEqual(child['relatedPlannedTaskIds'],[planned['id']]);self.assertTrue(child['distinctScope'])
                    self.assertEqual(model.counts['Build'],1);self.assertEqual(child['parentId'],design['id'])

    def test_task_board_survives_multiple_compactions_without_truncating_indices(self):
        board={'runId':'run-current','tasks':[{'id':'task-'+str(i),'title':'Actual scope '+str(i),
                 'agentId':'executor','status':'pending','revision':i,'parentId':None,
                 'dependsOn':['task-0'] if i else [],'outputs':['artifact-'+str(i)]} for i in range(8)]}
        messages=[{'role':'system','content':'Original roles and protocol.'}, {'role':'user','content':json.dumps({
            'task':{'id':'task-0','goal':'Original own task'},'taskBoard':board,'groupHistory':[{'text':'bulk '*10000}]})}]
        for _ in range(3):
            messages=compress_messages(messages+[{'role':'assistant','content':'old prose '*4000}],5000)
            self.assertEqual(json.loads(messages[1]['content'])['taskBoard'],board)
        with self.assertRaisesRegex(ProtocolError,'上下文容量'):
            compress_messages(messages,500)

    def test_invalid_scope_does_not_override_the_existing_plan_guard(self):
        model=BoardModel();run=self.run_model(model);design=next(t for t in run['steps'] if t['title']=='Design')
        build=next(t for t in run['steps'] if t['title']=='Build');build['status']='pending'
        for value in (' ',123,'x'*1201):
            before=copy.deepcopy(run['steps'])
            with self.subTest(value=str(value)[:20]),self.assertRaises(ProtocolError):
                self.app._delegate(run,design,{'title':'Another spelling','agentId':'executor','distinctScope':value})
            self.assertEqual(run['steps'],before)

    def test_new_tool_and_model_calls_capture_own_revision_and_attempt_across_rework(self):
        config=Config(Path(self.temp.name),{'LLM_API_KEY':'offline-audit','MAX_RETRIES':'1','MAX_AGENT_TURNS':'12',
            'MAX_MODEL_CALLS':'50','MAX_RUN_TOKENS':'500000','MAX_CONTEXT_CHARS':'80000'})
        self.app=Platform(config,provider=ReworkModel(),tools=LocalBrowser(config))
        self.app.action({'action':'send','mode':'task','participants':['coordinator','research','executor','reviewer'],
                         'text':'Deliver and independently recheck the corrected product.'})
        self.assertTrue(self.app.wait_for_idle(5));run=self.app.state['sessions']['group']['run']
        self.assertEqual(run['status'],'done');build=next(t for t in run['steps'] if t['title']=='Build')
        writes=[c for c in run['calls'] if c.get('taskId')==build['id'] and c['tool']=='file.write']
        self.assertEqual([c['revision'] for c in writes],[0,1]);self.assertEqual([c['attempt'] for c in writes],[1,2])
        model_calls=[c for c in run['calls'] if c.get('taskId')==build['id'] and c['tool']=='llm.chat']
        self.assertEqual({c['revision'] for c in model_calls},{0,1})
        self.assertTrue(all(c['revision']==0 for c in run['calls'] if c['taskId']!=build['id']))

    def test_resuming_a_completed_legacy_call_does_not_backfill_revision_or_reexecute(self):
        run=self.run_model(BoardModel());run['status']='running'
        task=next(t for t in run['steps'] if t['title']=='Build');task.update(revision=3,attempts=4,status='running')
        run['continuationHistory']=[{'id':'operator-continuation-now'}]
        result={'ok':True,'path':'legacy.txt','content':'Observed historical bytes','sha256':'a'*64}
        legacy={'id':'legacy-call','taskId':task['id'],'agentId':task['agentId'],'tool':'file.read',
                'args':{'path':'legacy.txt'},'status':'success','result':copy.deepcopy(result)}
        run['calls'].append(legacy);before=copy.deepcopy(legacy)
        self.app.repo.reserve_effect('legacy-effect',{'runId':run['id'],'taskId':task['id'],'tool':'file.read','args':{'path':'legacy.txt'}})
        self.app.repo.complete_effect('legacy-effect',result)
        with patch.object(self.app.tools,'execute',side_effect=AssertionError('A completed tool must not run again')):
            restored=self.app._tool('group',run['id'],task,'file.read',{'path':'legacy.txt'},
                pending={'callId':'legacy-call','effectKey':'legacy-effect','phase':'executing'})
        self.assertEqual(restored['callId'],'legacy-call');self.assertEqual(legacy,before)
        for field in ('revision','attempt','continuationId'):self.assertNotIn(field,legacy)


if __name__=='__main__':unittest.main()
