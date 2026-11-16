"""Independent phase-boundary review: offline only, never a real model/tool run."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.config import Config
from backend.engine import Platform, clock_ms
from backend.runtime import ProtocolError
from backend.stage_control import stage_control, stage_action_feedback
from tests.test_stage_control import fixture
from tests.test_rework_delivery_loop import ReworkModel, LocalBrowser


class StageControlBoundaryTests(unittest.TestCase):
    def test_same_revision_json_restore_keeps_original_pinned_times_and_budget(self):
        run,task=fixture();control=stage_control(run,task,10000)
        original=copy.deepcopy(task['stageControl']);budget=copy.deepcopy(run['budget'])
        restored=json.loads(json.dumps(run));target=restored['steps'][0]
        restored['steps'][1]['status']='done'
        actual=stage_control(restored,target,control['expectedHandoffBy']+1000)
        self.assertEqual(target['stageControl'],original)
        self.assertEqual(actual['phase'],'handoff_final')
        self.assertEqual(restored['budget'],budget)

    def test_saved_stage_deadline_must_match_actual_run_deadline(self):
        for delta in (-1000,1000):
            run,task=fixture();stage_control(run,task,10000)
            saved=copy.deepcopy(task['stageControl']);run['budget']['deadlineAt']+=delta
            with self.subTest(delta=delta),self.assertRaises(ProtocolError):
                stage_control(run,task,11000)
            self.assertEqual(task['stageControl'],saved,'invalid binding must not silently allocate a replacement')

    def test_bool_revision_in_current_or_saved_record_is_not_revision_zero(self):
        for field in ('task','saved'):
            run,task=fixture();stage_control(run,task,10000)
            if field=='task':task['revision']=False
            else:task['stageControl']['revision']=False
            with self.subTest(field=field),self.assertRaises(ProtocolError):
                stage_control(run,task,11000)

    def test_synthesis_feedback_does_not_execute_mutate_or_certify_any_tool(self):
        run,task=fixture();first=stage_control(run,task,10000)
        control=stage_control(run,task,first['synthesisAt']);before=copy.deepcopy(run)
        result=stage_action_feedback(control,{'type':'tool','name':'web.fetch','args':{'url':'https://example.org'}})
        self.assertFalse(result['ok']);self.assertFalse(result['actionExecuted'])
        self.assertNotIn('callId',result);self.assertNotIn('verified',result)
        self.assertEqual(run,before)
        self.assertIsNone(stage_action_feedback(control,{'type':'tool','name':'file.write'}))
        self.assertIsNone(stage_action_feedback(control,{'type':'publish_artifact'}))

    def test_rework_finishes_after_reviewer_old_checkpoint_and_still_performs_fresh_qa(self):
        class LateReviewer(ReworkModel):
            def complete(self,messages,max_tokens=None,deadline=None):
                if not messages[0]['content'].startswith('Plan an executable task DAG'):
                    context=json.loads(messages[1]['content'])
                    if context['task']['title']=='Build' and context['task'].get('feedback'):
                        time.sleep(.07)  # fake provider delay crosses the old QA checkpoint
                response=super().complete(messages,max_tokens=max_tokens)
                action=json.loads(response['content'])
                if action.get('type')=='review' and action.get('verdict')=='rework':
                    run=self.app.state['sessions']['group']['run'];self.original_deadline=run['budget']['deadlineAt']
                    reviewer=next(t for t in run['steps'] if t['title']=='QA');now=clock_ms()
                    reviewer['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-200,
                        handoffAt=now+30,expectedHandoffBy=now+50)
                    self.old_reviewer_control=copy.deepcopy(reviewer['stageControl'])
                return response
        class OfflineBrowser(LocalBrowser):
            def execute(self,agent,run,name,args,approved=False,deadline=None):
                return super().execute(agent,run,name,args,approved)
        with tempfile.TemporaryDirectory() as directory:
            config=Config(Path(directory),{'LLM_API_KEY':'offline-boundary','MAX_RETRIES':'1'})
            config.apply_time_only_profile();model=LateReviewer()
            app=Platform(config,provider=model,tools=OfflineBrowser(config));model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['coordinator','research','executor','reviewer'],
                    'text':'Deliver the exact current second revision after independent re-verification.'})
                self.assertTrue(app.wait_for_idle(6));run=app.state['sessions']['group']['run']
                build=next(t for t in run['steps'] if t['title']=='Build')
                qa=next(t for t in run['steps'] if t['title']=='QA')
                self.assertEqual(build['revision'],1)
                self.assertTrue(any(c['tool']=='file.write' and c.get('revision')==1 and c['status']=='success' for c in run['calls']))
                self.assertTrue(any(c['tool']=='code.test' and c.get('reviewTargets',{}).get(build['id'],{}).get('targetRevision')==1
                                    and c['status']=='success' for c in run['calls']),run.get('summary'))
                self.assertTrue(build['validation']['verified']);self.assertEqual(run['status'],'done')
                self.assertTrue(run['deliveries']);self.assertEqual(run['budget']['deadlineAt'],model.original_deadline)
                self.assertLessEqual(qa['stageControl']['expectedHandoffBy'],model.original_deadline)
                self.assertGreater(qa['stageControl']['allocatedAt'],model.old_reviewer_control['handoffAt'])
            finally:
                app.shutdown();app.wait_for_idle(3);app.repo.close()


if __name__=='__main__':unittest.main()
