"""Actual R7 write-loop feedback before the existing stop; no live API calls."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.feedback_context import latest_feedback_proof
from backend.runtime import compress_messages
from tests.test_engine import ScriptedProvider, task, final
from tests.test_execution_closure import LocalFiles, tool

FIXTURE=json.loads((Path(__file__).parent/'fixtures/r7_repeated_write.json').read_text())


class RepeatedActionWarningTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.temp.cleanup()

    def run_app(self,script,turns=8):
        config=Config(Path(self.temp.name)/str(len(self.apps)),{'LLM_API_KEY':'offline-repeat',
            'MAX_RETRIES':'0','MAX_AGENT_TURNS':str(turns),'MAX_MODEL_CALLS':'30',
            'MAX_RUN_TOKENS':'400000','MAX_CONTEXT_CHARS':'40000'})
        model=ScriptedProvider([{**task('Build','coordinator'),
            'outputContract':{'kind':'artifact','extensions':['.md']}}],{'Build':script})
        app=Platform(config,provider=model,tools=LocalFiles(config));model.app=app;self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['coordinator'],
                    'text':'Save and explicitly hand off the actual notes; do not claim semantic acceptance.'})
        until=time.monotonic()+6
        while time.monotonic()<until:
            run=app.snapshot()['sessions']['group']['run']
            for approval in run['approvals']:
                if approval.get('status')!='pending':continue
                self.assertEqual(approval['type'],'permission')
                binding=approval['binding'];self.assertEqual(binding['tool'],'file.write')
                self.assertEqual(binding['runId'],run['id'])
                args=binding['args'];self.assertEqual(args['path'],FIXTURE['actualRepeatedProposal']['args']['path'])
                self.assertEqual(args['content'],FIXTURE['actualRepeatedProposal']['args']['content'])
                self.assertEqual(args['expectedSha256'],hashlib.sha256(args['content'].encode()).hexdigest())
                app.action({'action':'approval','runId':run['id'],'approvalId':approval['id'],'choiceId':'allow',
                            'answer':'Test explicitly approves only this same-content, exact-SHA private overwrite.'})
            if app.wait_for_idle(.02):break
        self.assertTrue(app.wait_for_idle(.1))
        return app,app.state['sessions']['group']['run'],model

    def test_actual_repeated_write_warning_reaches_next_model_and_explicit_publish_once(self):
        proposal=FIXTURE['actualRepeatedProposal'];seen=[]
        def script(i,c,m):
            if i<2:return copy.deepcopy(proposal)
            if i==2:
                warning=json.loads(next(row['content'] for row in reversed(m)
                    if row['content'].startswith('重复动作警告：')).split('：',1)[1])
                seen.append(warning)
                self.assertEqual(warning['consecutiveIdenticalProposals'],2)
                self.assertTrue(warning['thirdProposalStopsIfExistingGuardsPermit'])
                self.assertEqual(warning['latestObservedResult']['sha256'],FIXTURE['calls'][0]['sha256'])
                self.assertEqual(c['ownArtifacts'],[])
                return {'type':'publish_artifact','path':proposal['args']['path']}
            return final('Actual notes published; no fabricated independent acceptance.')
        app,run,model=self.run_app(script)
        self.assertEqual(len(seen),1);self.assertEqual(model.counts['Build'],4)
        self.assertEqual(len(run['artifacts']),1)
        self.assertEqual(run['artifacts'][0]['sha256'],FIXTURE['calls'][0]['sha256'])
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),2)
        self.assertEqual(len([e for e in run['events'] if e['type']=='repeated_action_warning']),1)
        self.assertEqual(run['budgetExtensions'],0)
        self.assertFalse(run['artifacts'][0].get('verified'))

    def test_ignored_warning_keeps_three_proposal_stop_no_third_execution_or_auto_publication(self):
        proposal=FIXTURE['actualRepeatedProposal']
        _,run,model=self.run_app([copy.deepcopy(proposal)]*3)
        produced=next(t for t in run['steps'] if t['title']=='Build')
        self.assertEqual(produced['status'],'failed');self.assertIn('重复动作循环',produced['error']['message'])
        self.assertEqual(model.counts['Build'],3);self.assertEqual(run['artifacts'],[])
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),2)
        self.assertEqual(len([e for e in run['events'] if e['type']=='repeated_action_warning']),1)

    def test_no_warning_for_distinct_actions_or_no_second_result_at_final_boundary(self):
        for actions,turns in (([tool('file.list'),tool('file.write',path='one.md',content='one'),
                              {'type':'publish_artifact','path':'one.md'},final('Published actual text.')],8),
                            ([tool('file.list'),tool('file.list')],2),
                            ([final('Claims done without artifact.')]*3,8)):
            with self.subTest(turns=turns):
                _,run,model=self.run_app(actions,turns)
                self.assertFalse(any(e['type']=='repeated_action_warning' for e in run['events']))
                if turns==2:
                    self.assertEqual(model.counts['Build'],2)
                    self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.list']),1)

    def test_authentic_warning_survives_compaction_but_foreign_or_superseded_text_is_not_trusted(self):
        captured=[]
        def script(i,c,m):
            if i<2:return tool('file.list')
            # The actual provider's third request already passed runtime compaction.
            captured.extend(copy.deepcopy(m))
            return final('No required artifact was produced.',status='partial')
        app,run,model=self.run_app(script)
        target=next(t for t in run['steps'] if t['title']=='Build')
        warning=next(t for t in run['transcript'] if t.get('kind')=='repeated_action_warning')
        historic=copy.deepcopy(run)
        historic['transcript']=historic['transcript'][:historic['transcript'].index(warning)+1]
        proof=latest_feedback_proof(historic,target);self.assertIsNotNone(proof)
        self.assertEqual(proof.transcript_id,warning['id'])
        compact=compress_messages(captured,40000,trusted_feedback=proof)
        self.assertTrue(any(m['content'].startswith('重复动作警告：') for m in compact))
        foreign=copy.deepcopy(historic)
        foreign['transcript'][-1]['taskId']='foreign-task'
        foreign_proof=latest_feedback_proof(foreign,target)
        self.assertTrue(foreign_proof is None or foreign_proof.transcript_id!=warning['id'])
        for key,value in (('revision',False),('runId','foreign'),('proposalSha256','0'*64),
                          ('consecutiveIdenticalProposals',True)):
            with self.subTest(key=key):
                forged=copy.deepcopy(historic)
                payload=json.loads(forged['transcript'][-1]['text']);payload[key]=value
                forged['transcript'][-1]['text']=json.dumps(payload)
                invalid=latest_feedback_proof(forged,target)
                self.assertTrue(invalid is None or invalid.transcript_id!=warning['id'])
        current=latest_feedback_proof(run,target)
        self.assertTrue(current is None or current.transcript_id!=warning['id'])

    def test_existing_permanent_read_recovery_is_not_mislabeled_as_third_proposal_stop(self):
        from tests.test_readonly_recovery_runtime import ReadonlyRecoveryRuntimeTests
        existing=ReadonlyRecoveryRuntimeTests();existing.setUp()
        try:
            _,run=existing.create()
            self.assertEqual(run['status'],'done')
            self.assertEqual(len([c for c in run['calls'] if c['tool']=='web.fetch']),2)
            self.assertEqual(len([e for e in run['events'] if e['type']=='readonly_failure_recovery']),1)
            self.assertFalse(any(e['type']=='repeated_action_warning' for e in run['events']))
        finally:existing.tearDown()


if __name__=='__main__':unittest.main()
