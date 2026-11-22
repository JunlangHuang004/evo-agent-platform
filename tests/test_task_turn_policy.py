import tempfile
import unittest
from pathlib import Path
from backend.config import Config
from backend.engine import Platform
from tests.test_engine import ScriptedProvider,task,final
from tests.test_execution_closure import LocalFiles,tool

class TaskTurnPolicyTests(unittest.TestCase):
    def execute(self,finish,script=None,contract=None):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        c=Config(Path(temp.name),{'LLM_API_KEY':'offline-policy','MAX_RETRIES':'0','AGENT_INITIAL_TURNS':'15','AGENT_EXTRA_TURNS':'8'})
        c.apply_time_only_profile()
        def action(i,context,messages):
            if script:return script(i,context,messages)
            if i==finish:return final('Actual current output and known gaps.',status='partial')
            return tool('file.write',path=f'note-{i}.md',content=f'Actual note {i}')
        class DeadlineScript(ScriptedProvider):
            def complete(self,messages,max_tokens=None,deadline=None):return super().complete(messages)
        plan=task('Work','coordinator')
        if contract:plan['outputContract']=contract
        m=DeadlineScript([plan],{'Work':action})
        app=Platform(c,provider=m,tools=LocalFiles(c))
        self.addCleanup(app.repo.close);self.addCleanup(app.shutdown)
        app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Publish concise work with explicit gaps.'})
        self.assertTrue(app.wait_for_idle(20))
        return app.state['sessions']['group']['run'],m

    def test_valid_final_before_fifteen_has_no_extension(self):
        r,m=self.execute(2)
        self.assertEqual(m.counts['Work'],3)
        self.assertFalse(any(e['type']=='task_turn_extension' for e in r['events']))

    def test_final_on_fifteenth_turn_has_no_extension(self):
        r,m=self.execute(14)
        self.assertEqual(m.counts['Work'],15)
        self.assertFalse(any(e['type']=='task_turn_extension' for e in r['events']))

    def test_one_extension_then_twenty_third_turn_is_final_only(self):
        r,m=self.execute(22)
        self.assertEqual(m.counts['Work'],23)
        self.assertEqual(len([e for e in r['events'] if e['type']=='task_turn_extension']),1)
        self.assertEqual(len([c for c in r['calls'] if c['tool']=='file.write']),22)
        t=next(t for t in r['steps'] if t['title']=='Work')
        self.assertEqual(t['progress']['executionPhase'],'final_only')
        self.assertEqual(t['progress']['turnLimit'],23)
        self.assertEqual(r['budget']['timeMs'],1800000)
        self.assertEqual(r['status'],'partial')

    def test_nonfinal_last_proposal_not_executed_or_extended(self):
        r,m=self.execute(99)
        self.assertEqual(m.counts['Work'],23)
        self.assertEqual(len([c for c in r['calls'] if c['tool']=='file.write']),22)
        self.assertEqual(len([e for e in r['events'] if e['type']=='task_turn_extension']),1)
        self.assertNotEqual(r['status'],'done')

    def test_late_fetch_is_not_executed_and_brief_can_be_written_and_published(self):
        def script(i,c,m):
            if i<19:return tool('file.write',path=f'note-{i}.md',content='Existing retained research.')
            if i==19:return tool('web.fetch',url='https://example.org/report')
            if i==20:return tool('file.write',path='brief.md',content='Known findings; required evidence gaps explicitly retained.')
            if i==21:return {'type':'publish_artifact','path':'brief.md'}
            return final('Published partial brief, mandatory evidence still missing.',status='partial')
        r,m=self.execute(22,script)
        self.assertEqual(m.counts['Work'],23)
        self.assertFalse(any(c['tool']=='web.fetch' for c in r['calls']))
        self.assertTrue(any(a['name']=='brief.md' for a in r['artifacts']))
        self.assertTrue(any(e['type']=='turn_handoff_blocked' for e in r['events']))
        self.assertEqual(r['status'],'partial')

    def test_reserved_turns_allow_write_malformed_publish_correction_then_final(self):
        def script(i,c,m):
            if i<19:return {'type':'accept','sequence':i}
            state=c['progressReceipt']['turnHandoff']['publicationState']
            if i==19:
                self.assertEqual(state['next'],'write_required_files')
                return tool('file.write',path='brief.md',content='Supported findings, explicit unresolved gaps.')
            if i==20:
                self.assertEqual(state['next'],'publish_written_files')
                return tool('publish_artifact',path='brief.md')
            if i==21:return state['publicationExample']
            self.assertEqual(state['next'],'final')
            return final('Brief published, evidence gaps remain.',status='partial')
        r,m=self.execute(22,script,{'kind':'artifact','extensions':['.md'],'minCount':1})
        self.assertEqual(m.counts['Work'],23)
        self.assertEqual(len([a for a in r['artifacts'] if a['name']=='brief.md']),1)
        self.assertEqual(r['status'],'partial')
