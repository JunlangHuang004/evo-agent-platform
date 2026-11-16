"""Frozen evaluation evidence preserves real teams, history and failure boundaries."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.evaluation import EvidenceError, RunEvidence, evaluate_case, object_from_json, persist_result, summarize, timing
from backend.evaluation_platform import _matching_call, frozen_export_record, platform_review
from backend.order_acceptance import ExactJsonVerifier
from backend.teams import get_team_agents


class FrozenExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.path=self.root/'software-1.json'
        self.content='{"value":1}';digest=hashlib.sha256(self.content.encode()).hexdigest()
        self.artifact={'id':'artifact-one','runId':'real-run','taskId':'build','ownerAgentId':'dev',
                       'name':'output.json','path':'output.json','content':self.content,'sha256':digest,'size':len(self.content.encode())}
        self.run={'id':'real-run','teamId':'software','channelId':'group','status':'partial','startedAt':100,'finishedAt':1000,
                  'participants':['lead','dev','qa','release'],'steps':[
                      {'id':'plan','kind':'planning','revision':0,'status':'done','agentId':'lead'},
                      {'id':'build','revision':0,'status':'done','agentId':'dev','outputs':['artifact-one'],'result':'Actual output.',
                       'attemptHistory':[{'status':'failed','revision':0,'turns':12,'error':{'code':'runtime','message':'Original failure.'}}]}],
                  'calls':[],'events':[],'artifacts':[self.artifact],'memoryHits':[],
                  'approvals':[{'id':'clarify','type':'clarification','status':'expired','createdAt':200,'expiresAt':300,
                                'lateResponse':{'receivedAt':800,'answer':'Actual late answer.'},
                                'clarificationReceipt':{'receivedAt':800,'answer':'Actual late answer.'}}],
                  'continuationHistory':[{'id':'retry','previousStatus':'partial','modelCalls':12,'tokenUsage':1000}]}
        self.document={'case':'software-1','runId':'real-run','observedAt':'2026-11-15T12:00:00Z','observationStatus':'observed',
                       'elapsedSec':9.5,'stopReason':'Observed final state.','run':self.run}
        self.binding={'caseId':'software-1','runId':'real-run','teamId':'software'}
    def tearDown(self):self.temp.cleanup()
    def save(self):self.path.write_text(json.dumps(self.document,ensure_ascii=False))
    def adapt(self,binding=None):self.save();return frozen_export_record(binding or self.binding,self.path)

    def test_real_roles_export_hash_history_and_original_failure_survive_without_new_revision(self):
        run,load=self.adapt()
        self.assertEqual(run['source'],'frozen_export');self.assertEqual(run['teamId'],'software')
        self.assertEqual(run['roleBindings']['executor'],'dev');self.assertEqual(run['roleBindings']['reviewer'],'qa')
        self.assertEqual(len(run['tasks']),1,'public planning kind is excluded without relying on private _planning')
        task=run['tasks'][0];self.assertEqual(task['ownerAgentId'],'dev');self.assertEqual(task['revision'],0)
        self.assertEqual(task['attemptHistory'][0]['status'],'failed');self.assertEqual(task['attemptHistory'][0]['revision'],0)
        self.assertEqual(run['continuationHistory'],self.run['continuationHistory'])
        self.assertEqual(run['exportSha256'],hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(run['observedAt'],self.document['observedAt']);self.assertEqual(run['elapsedSec'],9.5)
        self.assertEqual(run['stopReason'],self.document['stopReason'])
        evidence=RunEvidence(run,load)
        _,item=evidence.artifact({'taskId':'build','revision':0,'artifactId':'artifact-one','sha256':self.artifact['sha256']},'output.json','executor')
        self.assertEqual(item['ownerAgentId'],'dev')
        run['rawRun']['artifacts'][0]['content']='caller mutation';item['content']='other caller mutation'
        self.assertEqual(load('artifact-one')['content'],self.content)
        self.assertEqual(json.loads(self.path.read_text())['run']['artifacts'][0]['content'],self.content)

    def test_overflow_constants_duplicate_keys_and_lone_surrogates_are_rejected(self):
        for value in ('{"x":1e999}','{"x":-2e999}','{"x":NaN}','{"x":Infinity}','{"x":1,"x":2}','{"x":"\\ud800"}'):
            with self.subTest(value=value),self.assertRaises(ValueError):object_from_json(value)

    def test_missing_task_revision_is_unassessed_not_invented_as_zero(self):
        self.run['steps'][1].pop('revision');run,load=self.adapt()
        task=run['tasks'][0];self.assertIsNone(task['revision']);self.assertFalse(task['revisionKnown'])
        with self.assertRaises(EvidenceError):RunEvidence(run,load).task({'taskId':'build','revision':0},'executor')
        (self.root/'expected.json').write_text(self.content)
        case={'caseId':'software-1','verifier':'exact-json','expectedPath':'expected.json','outputName':'output.json'}
        run['evidence']={'output':{'taskId':'build','revision':0,'artifactId':'artifact-one','sha256':self.artifact['sha256']}}
        result=evaluate_case(case,run,load,ExactJsonVerifier(case,self.root,'unused'))
        self.assertEqual(result['goalVerdict'],'unassessed');self.assertEqual(result['tasks'][0]['verdict'],'unassessed')
        self.assertEqual(result['attemptHistory']['build'][0]['status'],'failed')

    def test_late_clarification_wait_uses_actual_reply_but_critical_expiry_is_not_extended(self):
        self.run['approvals'].append({'id':'permission','type':'permission','status':'expired','createdAt':250,'expiresAt':350,
                                      'lateResponse':{'receivedAt':950,'answer':'Not an authorization.'}})
        run,_=self.adapt();self.assertEqual(timing(run),(900,600))
        waits={row['approvalId']:row for row in run['humanWaits']}
        self.assertEqual(waits['clarify']['endedAtMs'],800);self.assertEqual(waits['clarify']['expiresAtMs'],300)
        self.assertEqual(waits['permission']['endedAtMs'],350)
        self.assertEqual(self.run['approvals'][0]['status'],'expired')

    def test_wrong_hash_run_case_team_role_and_observation_binding_are_rejected(self):
        for changes in ({'exportSha256':'0'*64},{'runId':'another-run'},{'caseId':'another-case'},
                        {'teamId':'fund'},{'roleBindings':{'executor':'imaginary'}},{'roleBindings':{'reviewer':'dev'}},
                        {'observedAt':'changed-observation'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.adapt({**self.binding,**changes})

    def test_artifact_tampering_and_missing_content_never_fall_back_to_workspace(self):
        for change in ('content','sha256','size','runId','missing'):
            document=copy.deepcopy(self.document)
            item=document['run']['artifacts'][0]
            if change=='content':item['content']='forged'
            elif change=='sha256':item['sha256']='0'*64
            elif change=='size':item['size']=999
            elif change=='runId':item['runId']='foreign'
            else:item.pop('content')
            self.path.write_text(json.dumps(document));_,load=frozen_export_record(self.binding,self.path)
            with self.subTest(change=change),self.assertRaises(EvidenceError):load('artifact-one')

    def test_explicit_relative_artifact_files_are_hashed_and_hidden_or_escaping_paths_rejected(self):
        self.artifact.pop('content');(self.root/'snapshot.json').write_text(self.content)
        run,load=self.adapt({**self.binding,'artifactFiles':{'artifact-one':'snapshot.json'}})
        self.assertEqual(load('artifact-one')['content'],self.content)
        (self.root/'.env').write_text(self.content)
        (self.root/'linked.json').symlink_to(self.root/'snapshot.json')
        for name in ('.env','../snapshot.json','linked.json',str(self.root/'snapshot.json')):
            _,load=self.adapt({**self.binding,'artifactFiles':{'artifact-one':name}})
            with self.subTest(name=name),self.assertRaises(ValueError):load('artifact-one')

    def test_unfinished_observation_requires_recorded_deadline_and_preserves_raw_state(self):
        self.run.update(status='waiting_human',finishedAt=None)
        with self.assertRaises(ValueError):self.adapt()
        run,_=self.adapt({**self.binding,'deadlineAtMs':1200})
        self.assertEqual(run['status'],'timeout');self.assertEqual(run['rawRun']['status'],'waiting_human')

    def test_missing_timing_stays_explicit_and_does_not_become_measured_zero(self):
        row={'goalVerdict':'unassessed','executionStatus':'not_run','errorCode':None,'checks':[],'tasks':[],
             'elapsedMs':None,'humanWaitMs':None,'verifierMs':None}
        result=summarize([row]);self.assertEqual(result['overallGoals']['total'],1)
        self.assertIsNone(result['elapsedMs']);self.assertEqual(result['missingTimingCases'],1)
        stored=[]
        class Sink:
            def save_evaluation(self,item):stored.append(item)
        persist_result(Sink(),'suite','batch',{**row,'caseId':'not-observed','runId':None,'verifierType':'test-only'})
        self.assertIsNone(stored[0]['elapsedSec']);self.assertIsNone(stored[0]['runId'])
        self.assertFalse(stored[0]['verified']);self.assertEqual(stored[0]['status'],'unassessed')

    def test_missing_call_revision_is_not_accepted_as_known_revision_zero(self):
        call={'taskId':'qa-task','agentId':'qa'};ref={'taskId':'qa-task','revision':0}
        self.assertFalse(_matching_call(call,ref,'qa'))
        call['revision']=0;self.assertTrue(_matching_call(call,ref,'qa'))

    def test_newsroom_and_fund_bind_semantic_roles_without_renaming_historical_actors(self):
        for team,producer,reviewer in (('newsroom','editor','fact_checker'),('fund','research','risk')):
            with self.subTest(team=team):
                document=copy.deepcopy(self.document);run=document['run'];run['teamId']=team
                run['participants']=list(get_team_agents(team));run['steps']=run['steps'][1:]
                run['steps'][0]['agentId']=producer;run['artifacts'][0]['ownerAgentId']=producer
                self.path.write_text(json.dumps(document))
                normalized,load=frozen_export_record({**self.binding,'teamId':team,
                    'roleBindings':{'executor':producer,'reviewer':reviewer}},self.path)
                self.assertEqual(normalized['roleBindings']['executor'],producer)
                self.assertEqual(normalized['tasks'][0]['ownerAgentId'],producer)
                self.assertEqual(normalized['rawRun']['steps'][0]['agentId'],producer)
                self.assertEqual(RunEvidence(normalized,load).task({'taskId':'build','revision':0},'executor')['ownerAgentId'],producer)


if __name__=='__main__':unittest.main()
