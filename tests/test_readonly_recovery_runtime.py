"""Engine rejects proven repeat reads as observations, never fabricated calls."""
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform,clock_ms
from tests.test_time_only_runtime import Model,Files


class PermanentReads(Files):
    def execute(self,agent,run,name,args,approved=False,deadline=None):
        if name=='web.fetch':return {'ok':False,'error':{'code':'permanent','retryable':False,'message':'HTTP404 confirmed by trusted offline fixture.'}}
        return super().execute(agent,run,name,args,approved,deadline)


class ResearchModel(Model):
    def complete(self,messages,max_tokens=None,deadline=None):
        response=super().complete(messages,max_tokens=max_tokens,deadline=deadline)
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            value=json.loads(response['content']);value['tasks'][0]['agentId']='research'
            response['content']=json.dumps(value)
        return response


class ReadonlyRecoveryRuntimeTests(unittest.TestCase):
    def setUp(self):self.directory=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.directory.cleanup()
    def create(self,phase=False):
        def action(n,context,messages):
            if n<3:
                if n==2 and phase:
                    run=model.app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
                    now=clock_ms();task['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-100,
                                                              handoffAt=now+5000,expectedHandoffBy=now+10000)
                return {'type':'tool','name':'web.fetch','args':{'url':'https://example.org/missing'}}
            if n==3:
                prefix='阶段动作结果：' if phase else '只读失败恢复：'
                self.assertTrue(any(m['content'].startswith(prefix) for m in messages))
                return {'type':'tool','name':'file.write','args':{'path':'brief.md','content':'Unavailable source explicitly identified. No claims of reading it.'}}
            if n==4:return {'type':'publish_artifact','path':'brief.md'}
            return {'type':'final','text':'Actual honest brief published with missing evidence identified.'}
        config=Config(Path(self.directory.name)/str(len(self.apps)),{'LLM_API_KEY':'offline-readonly','MAX_RETRIES':'0'})
        config.apply_time_only_profile();model=ResearchModel(action);app=Platform(config,provider=model,tools=PermanentReads(config))
        model.app=app;self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['research'],'text':'Use actual sources or report their unavailability.'})
        self.assertTrue(app.wait_for_idle(4));return app,app.state['sessions']['group']['run']

    def test_third_known_permanent_read_does_not_execute_or_crash_and_real_brief_can_be_published(self):
        app,run=self.create();self.assertEqual(run['status'],'done')
        fetches=[c for c in run['calls'] if c['tool']=='web.fetch']
        self.assertEqual(len(fetches),2);self.assertTrue(all(c['status']=='failed' for c in fetches))
        self.assertEqual(len([e for e in run['events'] if e['type']=='readonly_failure_recovery']),1)
        self.assertEqual(len(run['artifacts']),1)
        self.assertFalse(any(c.get('result',{}).get('error',{}).get('code')=='repeated_permanent_read_failure'
                             for c in fetches),'rejected proposal is not a fake tool call')
        self.assertEqual(app.repo.get_run(run['id'])['modelCalls'],run['modelCalls'])

    def test_stage_cutoff_has_priority_over_readonly_failure_recovery(self):
        app,run=self.create(phase=True);self.assertEqual(run['status'],'done')
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='web.fetch']),2)
        self.assertFalse(any(e['type']=='readonly_failure_recovery' for e in run['events']))
        self.assertEqual(len([e for e in run['events'] if e['type']=='stage_action_blocked']),1)

    def test_repeated_mutation_proposals_keep_the_existing_hard_loop_guard(self):
        class RefuseWrites(Files):
            def execute(self,agent,run,name,args,approved=False,deadline=None):
                return {'ok':False,'error':{'code':'parameter','retryable':False,'message':'No mutation performed by offline fixture.'}}
        config=Config(Path(self.directory.name),{'LLM_API_KEY':'offline-mutation','MAX_RETRIES':'0'});config.apply_time_only_profile()
        model=Model(lambda n,c,m:{'type':'tool','name':'file.write','args':{'path':'same.md','content':'same'}})
        app=Platform(config,provider=model,tools=RefuseWrites(config));model.app=app;self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Mutation loop should remain guarded.'})
        self.assertTrue(app.wait_for_idle(4));run=app.state['sessions']['group']['run']
        self.assertNotEqual(run['status'],'done');self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),2)
        task=next(t for t in run['steps'] if not t.get('_planning'));self.assertIn('重复动作循环',task['error']['message'])
        self.assertFalse(any(e['type']=='readonly_failure_recovery' for e in run['events']))


if __name__=='__main__':unittest.main()
