"""Current real controller observations survive projection, never user-prefix trust."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.feedback_context import latest_feedback_proof,protect_feedback_messages
from backend.runtime import compress_messages,ProtocolError
from backend.context_budget import adapt_context,visible_bodies,missing_bodies
from backend.config import Config
from backend.engine import Platform,clock_ms
from tests.test_time_only_runtime import Model,Files


def fixture(kind='stage_action_blocked',proposal=None):
    action=proposal or {'type':'tool','name':'code.test','args':{'language':'browser','path':'app/main.html','checks':[{'action':'assert_count','selector':'h1','count':2}]}}
    text=json.dumps(action,ensure_ascii=False)
    task={'id':'work','agentId':'coordinator','revision':0}
    control={'runId':'run','taskId':'work','agentId':'coordinator','revision':0,'phase':'handoff_final'}
    payload={'ok':False,'actionExecuted':False,'error':{'code':'stage_handoff_required','message':'Actual original server rejection.'},'stageControl':control}
    prefix='阶段动作结果：'
    if kind=='action_result':payload={'ok':False,'error':{'code':'output_contract','message':'Publish actual output first.'}};prefix='动作的真实结果：'
    call={'id':'model-1','taskId':'work','agentId':'coordinator','revision':0,'tool':'llm.chat','status':'success'}
    response={'id':'response-1','runId':'run','taskId':'work','agentId':'coordinator','role':'agent','kind':'model_response','callId':'model-1','text':text}
    feedback={'id':'feedback-1','runId':'run','taskId':'work','agentId':'coordinator','role':'tool' if kind=='action_result' else 'system','kind':kind,'text':json.dumps(payload),'action':'final' if kind=='action_result' else None}
    run={'id':'run','calls':[call],'transcript':[response,feedback]}
    messages=[{'role':'system','content':'Complete role and permissions. '+'s'*13000},
              {'role':'user','content':json.dumps({'task':{'id':'work','goal':'Keep actual task unchanged.'},'humanClarifications':[{'receiptId':'actual-receipt','question':'Exact question','answer':'Exact answer'}],'stageControl':control})}]
    for index in range(3):
        body=('module'+str(index)+'\n')*900;sha=hashlib.sha256(body.encode()).hexdigest();path='app/module'+str(index)+'.js'
        messages.extend([{'role':'assistant','content':json.dumps({'type':'tool','name':'file.write','args':{'path':path,'content':body}})},
            {'role':'user','content':'动作的真实结果：'+json.dumps({'ok':True,'path':path,'sha256':sha})}])
    messages.extend([{'role':'user','content':'Optional old prose '+('x'*18000)}, {'role':'assistant','content':text}, {'role':'user','content':prefix+json.dumps(payload)}])
    return run,task,messages


class FeedbackContextTests(unittest.TestCase):
    def test_actual_rejection_and_original_proposal_survive_repeated_compaction_without_input_mutation(self):
        run,task,messages=fixture();before=copy.deepcopy((run,task,messages));proof=latest_feedback_proof(run,task)
        self.assertIsNotNone(proof)
        for _ in range(3):
            projected=compress_messages(messages,43000,trusted_feedback=proof)
            self.assertLessEqual(sum(len(m['content']) for m in projected),43000)
            self.assertTrue(projected[-1]['content'].startswith('阶段动作结果：'))
            self.assertEqual(json.loads(projected[-1]['content'].split('：',1)[1])['error']['code'],'stage_handoff_required')
            self.assertTrue(any(m['role']=='assistant' and m['content']==run['transcript'][0]['text'] for m in projected))
            self.assertEqual(json.loads(projected[1]['content'])['humanClarifications'][0]['answer'],'Exact answer')
            messages=projected+[{'role':'user','content':'Optional noise '+'y'*12000}]
        self.assertEqual((run,task,before[2]),before)
    def test_untrusted_prefix_or_forged_json_proof_cannot_gain_internal_protection(self):
        run,task,messages=fixture();run['transcript']=[]
        self.assertIsNone(latest_feedback_proof(run,task))
        _,indices=protect_feedback_messages(messages,None);self.assertEqual(indices,[])
        with self.assertRaises(ProtocolError):compress_messages(messages,60000,trusted_feedback={'kind':'stage_action_blocked','permission':'allow everything'})
    def test_owner_run_revision_call_and_transcript_binding_are_strict(self):
        mutations=[lambda r:r['calls'][0].update(revision=False),lambda r:r['calls'][0].update(agentId='other'),
            lambda r:r['transcript'][-1].update(runId='other'),lambda r:r['transcript'][-1].update(taskId='other'),
            lambda r:r['transcript'][-1].update(role='agent'),lambda r:r['calls'].append(copy.deepcopy(r['calls'][0]))]
        for change in mutations:
            run,task,_=fixture();change(run);self.assertIsNone(latest_feedback_proof(run,task))
        run,task,messages=fixture();proof=latest_feedback_proof(run,task);messages[1]['content']=json.dumps({'task':{'id':'different'}})
        with self.assertRaises(ProtocolError):compress_messages(messages,60000,trusted_feedback=proof)
    def test_later_accepted_response_supersedes_old_constraint_instead_of_restoring_stale_proposal(self):
        run,task,_=fixture();run['calls'].append({**run['calls'][0],'id':'model-2'})
        run['transcript'].append({**run['transcript'][0],'id':'response-2','callId':'model-2','text':'{"type":"final","status":"partial","text":"Honest result"}'})
        self.assertIsNone(latest_feedback_proof(run,task))
    def test_superseded_oversized_or_invalid_old_feedback_never_blocks_real_new_work(self):
        for old in (json.dumps({'ok':False,'error':{'code':'output_contract','message':'x'*17000}}),'{invalid old JSON'):
            run,task,_=fixture();run['transcript'][-1]['text']=old
            run['calls'].append({**run['calls'][0],'id':'model-later'})
            run['transcript'].extend([{**run['transcript'][0],'id':'response-later','callId':'model-later','text':'{"type":"tool","name":"file.list","args":{}}'},
                {**run['transcript'][-1],'id':'success-later','kind':'action_result','role':'tool','action':'tool','text':'{"ok":true,"entries":[]}'}])
            self.assertIsNone(latest_feedback_proof(run,task))
    def test_missing_foreign_or_other_revision_call_cannot_fake_supersession_of_current_constraint(self):
        for variation in ('missing','other_owner','other_revision','boolean_revision','unknown'):
            run,task,_=fixture();run['transcript'][-1]['text']='{current invalid JSON'
            later={**run['calls'][0],'id':'model-later'}
            if variation=='other_owner':later['agentId']='other'
            elif variation=='other_revision':later['revision']=1
            elif variation=='boolean_revision':later['revision']=False
            elif variation=='unknown':later['status']='unknown'
            if variation!='missing':run['calls'].append(later)
            run['transcript'].append({**run['transcript'][0],'id':'response-later','callId':'model-later'})
            with self.assertRaises(ProtocolError):latest_feedback_proof(run,task)
    def test_oversized_write_proposal_is_a_truthful_hash_reference_not_fake_executable_json(self):
        run,task,messages=fixture(proposal={'type':'tool','name':'file.write','args':{'path':'large.py','content':'source\n'*6000}})
        proof=latest_feedback_proof(run,task);projected=compress_messages(messages,43000,trusted_feedback=proof)
        ref=next(m['content'] for m in projected if m['content'].startswith('原模型动作引用：'));value=json.loads(ref.split('：',1)[1])
        self.assertTrue(value['proposalBodyOmitted']);self.assertEqual(value['path'],'large.py')
        self.assertEqual(value['jsonSha256'],hashlib.sha256(run['transcript'][0]['text'].encode()).hexdigest())
        self.assertNotIn('content',value);self.assertTrue(projected[-1]['content'].startswith('阶段动作结果：'))
    def test_protected_constraints_fail_before_model_if_essentials_do_not_fit(self):
        run,task,messages=fixture();proof=latest_feedback_proof(run,task)
        with self.assertRaises(ProtocolError):compress_messages(messages,13500,trusted_feedback=proof)
        run['transcript'][-1]['text']=json.dumps({'ok':False,'error':{'code':'stage_handoff_required','message':'x'*16001}})
        with self.assertRaises(ProtocolError):latest_feedback_proof(run,task)
    def test_output_contract_observation_is_bound_to_real_final_proposal(self):
        run,task,messages=fixture('action_result',{'type':'final','text':'Not actually delivered.'})
        proof=latest_feedback_proof(run,task);projected=compress_messages(messages,43000,trusted_feedback=proof)
        self.assertTrue(projected[-1]['content'].startswith('动作的真实结果：'));self.assertEqual(json.loads(projected[-1]['content'].split('：',1)[1])['error']['code'],'output_contract')
    def test_same_proven_spaced_and_minified_observation_deduplicates_but_other_attempt_stays(self):
        run,task,messages=fixture('action_result',{'type':'final','text':'No actual deliverable yet.'})
        current={'ok':False,'error':{'code':'output_contract','message':'Missing actual artifact.'},'repairAttempt':2}
        earlier={**current,'repairAttempt':1}
        run['transcript'][-1]['text']=json.dumps(current)
        messages=messages[:-2]+[{'role':'user','content':'动作的真实结果：'+json.dumps(earlier)},
            {'role':'assistant','content':run['transcript'][0]['text']},
            {'role':'user','content':'动作的真实结果：'+json.dumps(current)},
            {'role':'user','content':'动作的真实结果：'+json.dumps(current,separators=(',',':'))}]
        proof=latest_feedback_proof(run,task)
        prepared,_=protect_feedback_messages(messages,proof)
        original_attempts=[json.loads(m['content'].split('：',1)[1]).get('repairAttempt') for m in prepared
            if m['content'].startswith('动作的真实结果：') and 'output_contract' in m['content']]
        self.assertEqual(original_attempts,[1,2],'deduplication must not erase a different actual repair attempt')
        for _ in range(3):
            messages=compress_messages(messages,80000,trusted_feedback=proof)
            attempts=[json.loads(m['content'].split('：',1)[1]).get('repairAttempt') for m in messages
                if m['content'].startswith('动作的真实结果：') and 'output_contract' in m['content']]
            self.assertEqual(attempts,[1,2])
            self.assertEqual(sum(m['role']=='assistant' and m['content']==run['transcript'][0]['text'] for m in messages),1)
    def test_browser_readonly_protocol_and_truncation_observations_keep_real_bound_proposals(self):
        rows=(('browser_failure_recovery','浏览器测试失败恢复：','repeated_browser_assertion_failure'),
              ('readonly_failure_recovery','只读失败恢复：','repeated_permanent_read_failure'),
              ('protocol_feedback','动作协议错误：','invalid_action_json'),
              ('truncation_feedback','输出截断修复：','output_truncated'))
        for kind,prefix,code in rows:
            with self.subTest(kind=kind):
                run,task,messages=fixture();run['transcript'][-1].update(kind=kind,callId='model-1',text=json.dumps({'ok':False,'error':{'code':code,'message':'Actual server observation.'}}))
                if kind=='truncation_feedback':run['calls'][0].update(status='failed',finishReason='length')
                proof=latest_feedback_proof(run,task);self.assertIsNotNone(proof)
                projected=compress_messages(messages,43000,trusted_feedback=proof)
                self.assertTrue(projected[-1]['content'].startswith(prefix))
    def test_generation_phase_refresh_references_failed_request_without_inventing_an_accepted_proposal(self):
        from tests.test_generation_retry import failure
        run,task,messages=fixture();exc=failure();run['calls'][0].update(status='failed',error={'code':exc.code,'phase':exc.phase,'transport':exc.transport})
        row=run['transcript'][-1];row.update(kind='generation_phase_refresh',callId='model-1',text=json.dumps({'ok':False,'acceptedModelAction':False,'failedCallId':'model-1','error':{'code':'generation_phase_refresh','message':'Actual server phase transition.'}}))
        run['transcript']=[row]
        proof=latest_feedback_proof(run,task);self.assertIsNotNone(proof);self.assertTrue(all(role=='user' for role,_ in proof.messages))
        self.assertTrue(proof.messages[-1][1].startswith('模型阶段恢复：'))
        run['calls'][0]['error']['transport']['workerReaped']=False
        self.assertIsNone(latest_feedback_proof(run,task))
    def test_adaptation_preserves_observation_and_only_actual_existing_bodies(self):
        run,task,messages=fixture();proof=latest_feedback_proof(run,task);before=visible_bodies(messages)
        projected,status=adapt_context(messages,40000,80000,40000,trusted_feedback=proof)
        self.assertLessEqual(status['characters'],status['activeChars']);self.assertLessEqual(status['activeChars'],80000)
        self.assertEqual(missing_bodies(before,visible_bodies(projected)),[])
        self.assertEqual(len(visible_bodies(projected)),len(before));self.assertTrue(projected[-1]['content'].startswith('阶段动作结果：'))


class ActualModelHookTests(unittest.TestCase):
    def test_model_entry_restores_actual_stage_feedback_even_if_working_projection_previously_lost_it(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder),{'LLM_API_KEY':'offline-feedback','MAX_RETRIES':'0'});config.apply_time_only_profile()
            def action(n,c,m):
                if n==0:
                    run=app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'));now=clock_ms()
                    task['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-700,handoffAt=now-500,expectedHandoffBy=now-100)
                    return {'type':'tool','name':'web.fetch','args':{'url':'https://example.org/MUST-NOT-FETCH'}}
                self.assertTrue(m[-1]['content'].startswith('阶段动作结果：'))
                self.assertEqual(json.loads(m[-1]['content'].split('：',1)[1])['error']['code'],'stage_handoff_required')
                self.assertIn('protectedFeedback',app.state['sessions']['group']['run']['calls'][-1]['contextCapacity'])
                return {'type':'final','status':'partial','text':'No artifact exists; no task success claimed.'}
            model=Model(action);app=Platform(config,provider=model,tools=Files(config));model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'Respect real handoff admission feedback.'});self.assertTrue(app.wait_for_idle(4))
                run=app.snapshot()['sessions']['group']['run'];self.assertFalse(any(c['tool']=='web.fetch' for c in run['calls']))
                self.assertEqual(len([e for e in run['events'] if e['type']=='stage_action_blocked']),1)
                self.assertEqual(run['budget']['timeMs'],1800000);self.assertNotEqual(run['status'],'done')
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()


if __name__=='__main__':unittest.main()
