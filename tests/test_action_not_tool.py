"""Precise non-executed feedback for known top-level actions misnested as tools."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform,_action_not_tool_feedback
from backend.stage_control import stage_control
from tests.test_engine import ScriptedProvider,task,final
from tests.test_execution_closure import LocalFiles,tool
from tests.test_time_only_runtime import Model

# Exact accepted JSON text from Round4 Fund2 dialogue-7f5d9cdbada9415c.
# run-81c527d424444758 / export SHA eff78105a7c4fb683cbb2e21dda04fab462469c379d779c410d7a88f225790cd.
ACTUAL_FUND2_WRONG=json.loads('{"type":"tool","name":"publish_artifact","args":{"path":"research_findings.md"}}')


def last_result(messages):
    return json.loads(next(m['content'] for m in reversed(messages) if m['content'].startswith('动作的真实结果：')).split('：',1)[1])


class ActionNotToolSchemaTests(unittest.TestCase):
    def test_exact_known_schemas_only_with_no_implicit_conversion_or_permission(self):
        for name,args,expected in (
            ('publish_artifact',{'path':'research_findings.md'},{'type':'publish_artifact','path':'research_findings.md'}),
            ('publish_artifacts',{'paths':['one.md','two.md']},{'type':'publish_artifacts','paths':['one.md','two.md']}),
            ('package_delivery',{'artifactIds':['real-id'],'entrypoint':'app/index.html','instructions':'Serve these actual files.'},
             {'type':'package_delivery','artifactIds':['real-id'],'entrypoint':'app/index.html','instructions':'Serve these actual files.'})):
            action=tool(name,**args);before=copy.deepcopy(action);r=_action_not_tool_feedback(action)
            self.assertEqual(r['error']['code'],'action_not_tool');self.assertTrue(r['notExecuted']);self.assertFalse(r['actionExecuted'])
            self.assertEqual(r['correctActionExample'],expected);self.assertEqual(action,before)
            self.assertNotIn('callId',r);self.assertNotIn('artifact',r);self.assertNotIn('delivery',r)
        for name in ('publish_thing','git.push','file.write','review'):
            self.assertIsNone(_action_not_tool_feedback(tool(name)))
        self.assertIsNone(_action_not_tool_feedback({'type':'publish_artifact','path':'one.md'}))
        malformed=_action_not_tool_feedback({'type':'tool','name':'publish_artifact','args':'bad'})
        self.assertEqual(malformed['error']['code'],'action_not_tool')
        self.assertEqual(malformed['correctActionExample']['path'],'<existing-relative-file.md>')
        self.assertFalse(malformed['actionExecuted'])


class ActionNotToolRuntimeTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.tmp.cleanup()
    def execute(self,plan,scripts,participants,turns=8):
        config=Config(Path(self.tmp.name)/str(len(self.apps)),{'LLM_API_KEY':'offline-protocol','MAX_RETRIES':'0',
            'MAX_AGENT_TURNS':str(turns),'MAX_MODEL_CALLS':'30','MAX_RUN_TOKENS':'400000'})
        model=ScriptedProvider(plan,scripts);app=Platform(config,provider=model,tools=LocalFiles(config));self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':participants,'text':'Deliver actual notes and verify the immutable handoff.'})
        self.assertTrue(app.wait_for_idle(5));return app,app.state['sessions']['group']['run'],model
    def test_actual_fund2_wrong_publication_then_explicit_correct_action_registers_once_and_qa_reads_exact_sha(self):
        content='Actual research framework; source bodies remain unverified.\n';sha=hashlib.sha256(content.encode()).hexdigest()
        def research(i,c,m):
            if i==0:return tool('file.write',path='research_findings.md',content=content)
            if i==1:return copy.deepcopy(ACTUAL_FUND2_WRONG)
            if i==2:
                r=last_result(m);self.assertEqual(r['error']['code'],'action_not_tool');self.assertFalse(r['actionExecuted'])
                self.assertEqual(c['ownArtifacts'],[],'the rejected proposal did not publish or gain permission')
                return r['correctActionExample']  # An explicit, separate model response.
            return final('Published the actual research notes, no source/quality acceptance claim.')
        def qa(i,c,m):
            if i==0:
                self.assertEqual(len(c['authorizedArtifacts']),1);ref=c['authorizedArtifacts'][0]
                self.assertEqual(ref['sha256'],sha);self.assertTrue(ref['path'].startswith('imports/'))
                return tool('file.read',path=ref['path'])
            r=last_result(m);self.assertEqual(r['content'],content);self.assertEqual(r['sha256'],sha)
            return final('Read exact authorized immutable evidence; no fabricated code or financial validation.')
        app,run,model=self.execute([{**task('Research','research'),'outputContract':{'kind':'artifact','extensions':['.md']}},
            task('QA','reviewer',['Research'],['review'])],{'Research':research,'QA':qa},['research','reviewer'])
        self.assertEqual(run['status'],'done');self.assertEqual(len(run['artifacts']),1);self.assertEqual(run['artifacts'][0]['sha256'],sha)
        self.assertEqual(len([e for e in run['events'] if e['type']=='artifact_registered']),1)
        self.assertEqual(len([e for e in run['events'] if e['type']=='action_protocol_rejected']),1)
        self.assertFalse(any(c['tool']=='publish_artifact' for c in run['calls']))
        self.assertTrue(any(c['tool']=='file.read' and c['agentId']=='reviewer' and c['status']=='success' for c in run['calls']))
        self.assertEqual(model.counts['Research'],4)
    def test_batch_and_package_misnesting_do_not_execute_until_explicit_correct_proposal(self):
        def action(i,c,m):
            if i==0:return tool('file.write',path='app/index.html',content='<!doctype html><h1>Actual static fixture</h1>')
            if i==1:return tool('publish_artifacts',paths=['app/index.html'])
            if i in (2,4):
                r=last_result(m);self.assertEqual(r['error']['code'],'action_not_tool');return r['correctActionExample']
            if i==3:return tool('package_delivery',artifactIds=[last_result(m)['artifacts'][0]['id']],entrypoint='app/index.html',instructions='Open this actual static fixture.')
            return final('Actual static fixture packaged; semantic quality unassessed.')
        app,run,_=self.execute([task('Build','coordinator')],{'Build':action},['coordinator'])
        self.assertEqual(run['status'],'done');self.assertEqual(len(run['artifacts']),1);self.assertEqual(len(run['deliveries']),1)
        self.assertEqual(run['deliveries'][0]['semanticQuality'],'unassessed')
        self.assertEqual(len([e for e in run['events'] if e['type']=='action_protocol_rejected']),2)
        self.assertFalse(any(c['tool'] in ('publish_artifacts','package_delivery') for c in run['calls']))
    def test_unknown_and_real_disallowed_tool_still_return_permission_not_schema_mapping(self):
        def action(i,c,m):
            if i<2:
                if i:self.assertEqual(last_result(m)['error']['code'],'permission')
                return tool('unknown_publish' if i==0 else 'web.search',query='not permitted to coordinator')
            self.assertEqual(last_result(m)['error']['code'],'permission');return final('No disallowed tool executed.',status='partial')
        _,run,_=self.execute([task('Check','coordinator')],{'Check':action},['coordinator'])
        self.assertEqual(run['status'],'partial');self.assertFalse(any(e['type']=='action_protocol_rejected' for e in run['events']))
        self.assertFalse(any(c['tool'] in ('unknown_publish','web.search') for c in run['calls']))
    def test_repeated_wrong_action_keeps_existing_three_proposal_loop_boundary(self):
        _,run,model=self.execute([task('Check','coordinator')],{'Check':[copy.deepcopy(ACTUAL_FUND2_WRONG)]*5},['coordinator'])
        self.assertNotEqual(run['status'],'done');self.assertEqual(model.counts['Check'],3)
        self.assertEqual(len([e for e in run['events'] if e['type']=='action_protocol_rejected']),2)
        self.assertFalse(run['artifacts']);self.assertFalse(any(c['tool']=='publish_artifact' for c in run['calls']))
    def test_final_only_still_rejects_nonfinal_without_publication_or_extra_request(self):
        _,run,model=self.execute([task('Check','coordinator')],{'Check':[{'type':'accept'},copy.deepcopy(ACTUAL_FUND2_WRONG)]},['coordinator'],turns=2)
        t=next(t for t in run['steps'] if not t.get('_planning'))
        self.assertEqual(t['finalizationFailure']['code'],'final_action_required');self.assertEqual(model.counts['Check'],2)
        self.assertFalse(any(e['type']=='action_protocol_rejected' for e in run['events']));self.assertFalse(run['artifacts'])
    def test_handoff_can_explain_schema_but_only_later_correct_publication_executes(self):
        now=[1000000];config=Config(Path(self.tmp.name)/'handoff',{'LLM_API_KEY':'offline-handoff','MAX_RETRIES':'0'});config.apply_time_only_profile()
        def short(run,t,stamp):
            v=stage_control(run,t,stamp)
            if v and not t.get('_testPinned'):
                t['_testPinned']=True;t['stageControl'].update(synthesisAt=stamp+10,handoffAt=stamp+20,expectedHandoffBy=stamp+30);v=stage_control(run,t,stamp)
            return v
        def action(i,c,m):
            if i==0:return tool('file.write',path='research_findings.md',content='Real pre-handoff notes.')
            if i==1:
                self.assertEqual(c['stageControl']['phase'],'handoff_final');return copy.deepcopy(ACTUAL_FUND2_WRONG)
            if i==2:
                self.assertEqual(last_result(m)['error']['code'],'action_not_tool');return last_result(m)['correctActionExample']
            return final('Actual publication after explicit schema correction.')
        class AdvanceFiles(LocalFiles):
            def execute(self,*a,**k):
                result=super().execute(*a,**k)
                if a[2]=='file.write':now[0]+=30
                return result
        model=Model(action);app=Platform(config,provider=model,tools=AdvanceFiles(config));model.app=app;self.apps.append(app)
        with patch('backend.engine.clock_ms',side_effect=lambda:now[0]),patch('backend.engine.stage_control',side_effect=short):
            app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Publish only the actual local notes.'});self.assertTrue(app.wait_for_idle(4))
        r=app.state['sessions']['group']['run'];self.assertEqual(r['status'],'done');self.assertEqual(len(r['artifacts']),1)
        self.assertEqual(len([e for e in r['events'] if e['type']=='artifact_registered']),1)
        self.assertFalse(any(c['tool']=='publish_artifact' for c in r['calls']))


if __name__=='__main__':unittest.main()
