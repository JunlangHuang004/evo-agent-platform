"""Read failure observations, using both offline fixtures and a real saved N2."""
import copy
import json
from pathlib import Path
import unittest

from backend.readonly_recovery import readonly_failure_feedback


class ReadonlyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.task={'id':'task-read','agentId':'research','revision':0}
        self.action={'type':'tool','name':'web.fetch','args':{'url':'https://example.org/missing'}}
        self.run={'id':'run-read','calls':[]}

    def fail(self,number,**changes):
        call={'id':'call-'+str(number),'taskId':self.task['id'],'agentId':self.task['agentId'],'revision':0,
            'tool':self.action['name'],'args':copy.deepcopy(self.action['args']),'status':'failed',
            'result':{'ok':False,'error':{'code':'permanent','retryable':False,'message':'Public web server returned HTTP 404.'}}}
        call.update(changes);return call

    def ready(self):self.run['calls']=[self.fail(1),self.fail(2)]

    def feedback(self):return readonly_failure_feedback(self.run,self.task,self.action)

    def test_third_proposal_is_rejected_as_data_not_a_fake_execution_or_task_failure(self):
        self.assertIsNone(self.feedback());self.run['calls'].append(self.fail(1));self.assertIsNone(self.feedback())
        self.run['calls'].append(self.fail(2));before=copy.deepcopy((self.run,self.task,self.action))
        result=self.feedback();self.assertFalse(result['ok']);self.assertFalse(result['actionExecuted'])
        self.assertTrue(result['notExecuted']);self.assertNotIn('callId',result)
        self.assertEqual(result['error']['code'],'repeated_permanent_read_failure')
        self.assertEqual(result['target'],self.action['args']['url'])
        self.assertEqual(result['observedFailures']['callIds'],['call-1','call-2'])
        self.assertEqual((self.run,self.task,self.action),before)

    def test_interleaved_other_reads_do_not_hide_the_same_permanent_failure(self):
        self.ready();other=self.fail('other',status='success',args={'url':'https://example.org/valid'},
            result={'ok':True,'url':'https://example.org/valid','content':'Actually fetched data'})
        self.run['calls'].insert(1,other)
        result=self.feedback();self.assertEqual(result['observedFailures']['count'],2)
        self.assertEqual(result['historicalSuccessfulReadReferences'],[{'callId':'call-other','url':'https://example.org/valid'}])
        self.assertEqual(result['bodyAvailability'],'not_checked_by_this_helper')
        self.assertNotIn('Actually fetched data',json.dumps(result))

    def test_matching_success_unknown_or_nonpermanent_outcome_resets_old_failures(self):
        variants=[('success',{'ok':True,'url':self.action['args']['url'],'content':'Now available'}),
            ('unknown',{'ok':False,'outcomeUnknown':True}),('cancelled',None),('pending',None),
            ('failed',{'ok':False,'error':{'code':'transient','retryable':True,'message':'temporary timeout'}})]
        for status,result in variants:
            with self.subTest(status=status,result=result):
                self.ready();self.run['calls'].append(self.fail('later',status=status,result=result))
                self.assertIsNone(self.feedback())
                self.run['calls'].append(self.fail(3));self.assertIsNone(self.feedback())
                self.run['calls'].append(self.fail(4));self.assertEqual(self.feedback()['observedFailures']['count'],2)

    def test_exact_args_and_current_owner_task_integer_revision_are_required(self):
        variations=[{'agentId':'other'},{'taskId':'other'},{'revision':1},{'revision':False},
            {'revision':None},{'args':{'url':'https://example.org/different'}}]
        for changes in variations:
            with self.subTest(changes=changes):
                self.ready();self.run['calls'][1].update(changes);self.assertIsNone(self.feedback())
        self.ready();self.task['revision']=False;self.assertIsNone(self.feedback())

    def test_failed_record_must_be_explicit_permanent_false_retryable_and_known(self):
        mutations=[lambda c:c['result'].update(outcomeUnknown=True),lambda c:c['result'].update(cancelled=True),
            lambda c:c['result'].update(deadlineExceeded=True),lambda c:c['result'].update(timedOut=True),
            lambda c:c['result'].update(needsApproval=True),lambda c:c['result'].update(ok=True),
            lambda c:c.update(outcomeUnknown=True),lambda c:c['result'].update(notExecuted=True),
            lambda c:c['result'].update(actionExecuted=False),lambda c:c['result'].update(modelViewOnly='projected'),
            lambda c:c['result']['error'].update(retryable=None),lambda c:c['result']['error'].update(retryable=0),
            lambda c:c['result']['error'].update(code='permission'),lambda c:c.update(result='A raw error string'),
            lambda c:c['result'].update(error=None)]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                self.ready();mutation(self.run['calls'][1]);self.assertIsNone(self.feedback())

    def test_writes_git_code_and_malformed_actions_are_never_exempted(self):
        self.ready()
        for name in ('file.write','file.patch','file.delete','git.push','git.commit','code.run','code.test'):
            action={**self.action,'name':name};self.assertIsNone(readonly_failure_feedback(self.run,self.task,action))
        for action in (None,{}, {'type':'web.fetch','args':self.action['args']},
                       {'type':'tool','name':'web.fetch','args':[]},
                       {'type':'tool','name':'web.fetch','args':{'url':None}},
                       {'type':'tool','name':'web.fetch','args':{'url':'x','invalid':float('nan')}}):
            self.assertIsNone(readonly_failure_feedback(self.run,self.task,action))

    def test_search_uses_exact_query_args_not_a_canonicalized_different_request(self):
        self.action={'type':'tool','name':'web.search','args':{'query':'Actual topic','max_results':6}}
        self.ready();self.run['calls'][1]['args']={'max_results':6,'query':'Actual topic'}
        result=self.feedback();self.assertEqual(result['target'],'Actual topic')
        self.action['args']['max_results']=5;self.assertIsNone(self.feedback())

    def test_repeated_refusal_has_no_side_effect_count_inflation_or_budget_change(self):
        self.ready();self.run['budget']={'tokens':None,'modelCalls':None,'agentTurns':None,'deadlineAt':12345}
        before=copy.deepcopy(self.run)
        for _ in range(20):
            result=self.feedback();self.assertEqual(result['observedFailures']['count'],2)
        self.assertEqual(self.run,before)
        self.assertNotIn('status',self.task);self.assertNotIn('content',result)

    def test_duplicate_call_ids_do_not_turn_one_failure_into_two(self):
        self.run['calls']=[self.fail(1),self.fail(1)]
        self.assertIsNone(self.feedback())

    def test_receipt_preserves_real_error_and_bounds_history_with_explicit_omission(self):
        self.run['calls']=[self.fail(i) for i in range(30)]
        self.run['calls'][-1]['result']['error']['message']='Actual error '*200
        result=self.feedback();evidence=result['observedFailures']
        self.assertEqual(evidence['count'],30);self.assertEqual(len(evidence['callIds']),8)
        self.assertTrue(evidence['callIdsTruncated']);self.assertTrue(evidence['recordsTruncated'])
        self.assertEqual(len(evidence['records']),3)
        self.assertTrue(evidence['records'][-1]['errorMessageTruncated'])
        self.assertEqual(evidence['records'][-1]['error']['message'],('Actual error '*200)[:600])

    def test_saved_real_newsroom_nature404_after_three_arxiv_reads_is_recoverable(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/readonly_recovery_newsroom2.json').read_text())
        run,task,action=fixture['run'],fixture['task'],fixture['proposedAction']
        before=copy.deepcopy(fixture);result=readonly_failure_feedback(run,task,action)
        self.assertFalse(result['actionExecuted']);self.assertEqual(result['observedFailures']['count'],2)
        self.assertEqual(result['observedFailures']['callIds'],['call-0ad343cabe0d4536','call-394167b15aaf4e74'])
        self.assertEqual(len(result['historicalSuccessfulReadReferences']),3)
        self.assertTrue(all('arxiv.org/abs/' in ref['url'] for ref in result['historicalSuccessfulReadReferences']))
        self.assertEqual(task['error']['code'],'protocol','The historical failed run is not rewritten to success.')
        self.assertNotIn('content',result);self.assertEqual(fixture,before)
        self.assertEqual(len(run['calls']),5,'Rejected third proposal is not added as an executed request.')


if __name__=='__main__':unittest.main()
