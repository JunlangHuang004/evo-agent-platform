"""Existing tool-type schema feedback remains visible after soft checkpoints."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform
from backend.stage_control import stage_control
from tests.test_execution_closure import LocalFiles, tool
from tests.test_engine import final, task, ScriptedProvider
from tests.test_action_not_tool import last_result


class ToolEnvelopeStageTests(unittest.TestCase):
    def test_schema_feedback_after_checkpoint_never_executes_or_reopens_corrected_network_action(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg=Config(Path(folder),{'LLM_API_KEY':'offline','MAX_RETRIES':'0'});cfg.apply_time_only_profile()
            now=[1000000]
            def fixed_stage(run,task,stamp):
                value=stage_control(run,task,stamp)
                if value and not task.get('_testPinned'):
                    task['_testPinned']=True
                    task['stageControl'].update(synthesisAt=stamp,handoffAt=stamp+1000,expectedHandoffBy=stamp+2000)
                    value=stage_control(run,task,stamp)
                return value
            def script(i,c,m):
                self.assertEqual(c['stageControl']['phase'],'synthesis')
                if i==0:return {'type':'web.fetch','args':{'url':'https://example.com/'}}
                if i==1:
                    reply=last_result(m);self.assertEqual(reply['error']['code'],'protocol')
                    self.assertIn('type="tool"',reply['error']['message'])
                    self.assertIn('name="web.fetch"',reply['error']['message'])
                    return tool('web.fetch',url='https://example.com/')
                return final('No network request executed after evidence acquisition closed.',status='partial')
            class DeadlineModel(ScriptedProvider):
                def complete(self,messages,max_tokens=None,deadline=None):
                    return super().complete(messages)
            model=DeadlineModel([task('Work','research')],{'Work':script})
            app=Platform(cfg,provider=model,tools=LocalFiles(cfg))
            try:
                with patch('backend.engine.clock_ms',side_effect=lambda:now[0]),patch('backend.engine.stage_control',side_effect=fixed_stage):
                    app.action({'action':'send','mode':'task','participants':['research'],'text':'Use only retained evidence.'})
                    self.assertTrue(app.wait_for_idle(5))
                run=app.state['sessions']['group']['run']
                self.assertEqual(run['status'],'partial',[(t['title'],t.get('status'),t.get('error')) for t in run['steps']])
                self.assertFalse(any(call['tool']=='web.fetch' for call in run['calls']))
                self.assertEqual(sum(event['type']=='action_rejected' for event in run['events']),1)
                self.assertEqual(sum(event['type']=='stage_action_blocked' for event in run['events']),1)
                self.assertFalse(run['artifacts']);self.assertFalse(run['approvals'])
                self.assertEqual(run['budget']['deadlineAt'],1000000+1800000)
            finally:
                app.shutdown();app.wait_for_idle(3);app.repo.close()


if __name__=='__main__':unittest.main()
