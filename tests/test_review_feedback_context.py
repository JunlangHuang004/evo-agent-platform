"""Latest real review decisions remain readable without protecting arbitrary success."""
import copy
import json
import unittest

from backend.feedback_context import latest_feedback_proof
from backend.runtime import compress_messages,ProtocolError
from tests.test_feedback_context import fixture


def review_fixture(verdict='pass',duplicate=False):
    action={'type':'review','taskId':'producer','verdict':verdict,'feedback':'Actual recorded assessment.'}
    run,q,messages=fixture('action_result',action)
    validation={'verdict':verdict,'feedback':action['feedback'],'reviewerTaskId':q['id'],'reviewerRevision':0,
                'targetRevision':0,'artifactHashes':{'output':'a'*64},'kind':'editorial_assessment',
                'assessed':True,'verified':False,'evidenceCallIds':[]}
    target={'id':'producer','agentId':'executor','revision':0,'status':'failed' if verdict=='reject' else 'done',
            'outputs':['output'],'validation':copy.deepcopy(validation)}
    run.update(steps=[q,target],artifacts=[{'id':'output','runId':'run','taskId':'producer',
        'ownerAgentId':'executor','sha256':'a'*64}],events=[{'id':'review-event','type':'review_'+verdict,
        'agentId':q['agentId'],'actor':q['agentId'],'payload':{'taskId':target['id'],'validation':copy.deepcopy(validation)}}])
    payload={'ok':True,'validation':validation,'status':target['status'],'targetTaskId':target['id'],'targetAgentId':'executor'}
    if duplicate:payload.update(duplicateReview=True,actionExecuted=False,newDecision=False,existingReviewEventId='review-event')
    else:payload['reviewEventId']='review-event'
    run['transcript'][-1].update(action='review',text=json.dumps(payload))
    messages[-1]['content']='动作的真实结果：'+json.dumps(payload)
    return run,q,messages,payload


class ReviewFeedbackContextTests(unittest.TestCase):
    def test_pass_reject_and_duplicate_receipts_survive_pressure_exactly_and_without_new_events(self):
        for verdict,duplicate in (('pass',False),('reject',False),('pass',True)):
            with self.subTest(verdict=verdict,duplicate=duplicate):
                run,q,messages,payload=review_fixture(verdict,duplicate);before=copy.deepcopy(run)
                proof=latest_feedback_proof(run,q);self.assertIsNotNone(proof)
                for _ in range(3):
                    messages=compress_messages(messages,23000,trusted_feedback=proof)
                    self.assertLessEqual(sum(len(m['content']) for m in messages),23000)
                    actual=json.loads(messages[-1]['content'].split('：',1)[1])
                    self.assertEqual(actual,payload);self.assertFalse(actual['validation']['verified'])
                    messages.append({'role':'user','content':'Optional historical prose '+'x'*30000})
                self.assertEqual(run,before)
                with self.assertRaises(ProtocolError):compress_messages(messages,13500,trusted_feedback=proof)

    def test_current_response_event_owner_revision_artifact_and_target_bindings_are_required(self):
        mutations=[lambda r,q,p:r['calls'][0].update(revision=False),
            lambda r,q,p:r['calls'][0].update(agentId='foreign'),
            lambda r,q,p:r['calls'][0].update(runId='foreign-run'),
            lambda r,q,p:r['transcript'][-1].update(role='system'),
            lambda r,q,p:r['transcript'][-1].update(runId='foreign'),
            lambda r,q,p:r['steps'][1].update(revision=1),
            lambda r,q,p:r['steps'][1].update(agentId='other-producer'),
            lambda r,q,p:r['artifacts'][0].update(sha256='b'*64),
            lambda r,q,p:r['artifacts'][0].update(ownerAgentId='other-producer'),
            lambda r,q,p:r['artifacts'][0].update(runId='other-run'),
            lambda r,q,p:r.update(events=[]),
            lambda r,q,p:r['events'][0].update(actor='foreign'),
            lambda r,q,p:r['events'].append(copy.deepcopy(r['events'][0])),
            lambda r,q,p:p.update(reviewEventId='invented'),
            lambda r,q,p:p.update(targetAgentId='other-producer'),
            lambda r,q,p:p.update(targetTaskId='other-target'),
            lambda r,q,p:p.pop('targetTaskId')]
        for change in mutations:
            with self.subTest(change=change):
                run,q,_,payload=review_fixture();change(run,q,payload)
                run['transcript'][-1]['text']=json.dumps(payload)
                self.assertIsNone(latest_feedback_proof(run,q))
        run,q,_,_=review_fixture();self.assertIsNone(latest_feedback_proof(run,copy.deepcopy(q)))

    def test_recorded_upstream_revision_or_bytes_must_remain_current(self):
        run,q,_,_=review_fixture();target=run['steps'][1]
        source={'id':'source','revision':0,'status':'done','outputs':[]}
        run['steps'].append(source)
        target['dependencySnapshots']={'source':{'status':'done','targetRevision':0,'artifactHashes':{}}}
        self.assertIsNotNone(latest_feedback_proof(run,q))
        source['revision']=1;self.assertIsNone(latest_feedback_proof(run,q))
        source['revision']=0;source['outputs']=['new-source']
        run['artifacts'].append({'id':'new-source','sha256':'b'*64})
        self.assertIsNone(latest_feedback_proof(run,q))
        target.pop('dependencySnapshots')
        self.assertIsNotNone(latest_feedback_proof(run,q),'missing legacy snapshots are not treated as recorded mismatches')

    def test_next_actual_response_supersedes_success_but_untrusted_prefix_never_protects_it(self):
        run,q,messages,_=review_fixture();run['calls'].append({**run['calls'][0],'id':'model-2'})
        run['transcript'].append({**run['transcript'][0],'id':'response-2','callId':'model-2',
                                 'text':'{"type":"final","status":"partial","text":"Actual handoff"}'})
        self.assertIsNone(latest_feedback_proof(run,q))
        run,q,_,_=review_fixture();run['transcript']=[]
        self.assertIsNone(latest_feedback_proof(run,q))
        # Keeping a matching prefix/body in working messages does not restore it.
        self.assertTrue(any(m['content'].startswith('动作的真实结果：') for m in messages))

    def test_other_success_and_false_duplicate_metadata_do_not_gain_review_protection(self):
        for action in ('tool','publish_artifact','final','accept'):
            run,q,_,_=review_fixture();run['transcript'][-1]['action']=action
            self.assertIsNone(latest_feedback_proof(run,q))
        for field,value in (('existingReviewEventId','missing'),('newDecision',True),('actionExecuted',True)):
            run,q,_,payload=review_fixture(duplicate=True);payload[field]=value
            run['transcript'][-1]['text']=json.dumps(payload)
            self.assertIsNone(latest_feedback_proof(run,q))

    def test_unaccepted_review_keeps_original_false_validation_and_error_protection(self):
        run,q,messages,payload=review_fixture()
        payload.update(ok=False,error={'code':'verification','message':'No actual independent test evidence.'})
        payload['validation'].update(kind='code_test',verified=False,assessed=True)
        run['transcript'][-1]['text']=json.dumps(payload)
        messages[-1]['content']='动作的真实结果：'+json.dumps(payload)
        proof=latest_feedback_proof(run,q);self.assertIsNotNone(proof)
        projected=compress_messages(messages,23000,trusted_feedback=proof)
        self.assertEqual(json.loads(projected[-1]['content'].split('：',1)[1]),payload)


if __name__=='__main__':unittest.main()
