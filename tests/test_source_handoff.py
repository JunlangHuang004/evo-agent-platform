"""URL-specific execution receipts, not source bodies or a semantic verdict."""
import copy
import hashlib
import json
import unittest

from backend.task_output import build_task_output, task_output_for_context


class SourceHandoffTests(unittest.TestCase):
    def task(self,**changes):
        return {'id':'research-task','agentId':'reporter','revision':2,'status':'failed',
                'result':'','outputs':[],'acceptanceCriteria':['Research and publish evidence.'],**changes}

    def call(self,id='actual-fetch',**changes):
        return {'id':id,'taskId':'research-task','agentId':'reporter','revision':2,'tool':'web.fetch','status':'success',
                'args':{'url':'https://example.edu/Article?Case=A'},
                'result':{'ok':True,'url':'https://example.edu/Article?Case=A','title':'Actual article',
                          'content':'Actual fetched UTF8 text — 资料. '+('body '*100)},**changes}

    def test_success_ref_exact_url_utf8_sha_and_status_without_forwarding_private_body(self):
        task=self.task();call=self.call();call['result']['contentSha256']='f'*64
        output=build_task_output(task,{'calls':[call]});view=task_output_for_context(output)
        work=view['observedWork'];ref=work['recentSuccessfulFetches'][0]
        self.assertEqual(ref['callId'],call['id']);self.assertEqual(ref['url'],call['result']['url'])
        self.assertEqual(ref['status'],'success');self.assertTrue(ref['bodyOmitted'])
        self.assertEqual(ref['bodySha256'],hashlib.sha256(call['result']['content'].encode('utf8')).hexdigest())
        self.assertEqual(ref['bodyCharacters'],len(call['result']['content']))
        self.assertNotIn('content',ref);self.assertNotIn(call['result']['content'],json.dumps(view,ensure_ascii=False))
        self.assertEqual(work['webFetchesWithBody'],1)
        self.assertEqual(view['status'],'failed');self.assertEqual(view['semanticQuality'],'unassessed')
        self.assertIsNone(view['reviewValidation']);self.assertIn('not source text',work['fetchReferenceMeaning'])
        self.assertEqual(task_output_for_context(view),view)

    def test_failure_target_mapping_survives_context_selection_and_keeps_original_failure(self):
        calls=[]
        for i in range(4):
            calls.append(self.call('search-'+str(i),tool='web.search',status='failed',args={'query':'Actual query '+str(i)},
                result={'ok':False,'error':{'code':'transient','message':'Search timeout','retryable':True}}))
        for i,(url,code) in enumerate([('https://vendor.example/detector',403),('https://nature.example/article',404),('https://nature.example/article',404)]):
            calls.append(self.call('failure-'+str(i),status='failed',args={'url':url},
                result={'ok':False,'error':{'code':'permanent','message':'HTTP '+str(code),'retryable':False}}))
        output=build_task_output(self.task(),{'calls':calls});view=task_output_for_context(output)
        self.assertEqual(len(output['toolFailures']),7);self.assertEqual(view['toolFailuresTotal'],7)
        self.assertTrue(view['toolFailuresTruncated'])
        self.assertEqual([item['callId'] for item in view['toolFailures']],['search-0','failure-0','failure-1','failure-2'])
        self.assertEqual(view['toolFailures'][0]['requestedTarget'],{'type':'query','value':'Actual query 0','characters':14,'truncated':False})
        self.assertEqual(view['toolFailures'][1]['requestedTarget']['value'],'https://vendor.example/detector')
        self.assertEqual(view['toolFailures'][1]['error']['message'],'HTTP 403')
        self.assertEqual(view['toolFailures'][2]['requestedTarget']['value'],'https://nature.example/article')
        self.assertEqual(view['toolFailures'][2]['error']['message'],'HTTP 404')
        self.assertEqual(task_output_for_context(view),view)

    def test_old_bool_missing_foreign_and_non_success_calls_never_become_success_refs(self):
        changes=[lambda c:c.update(revision=1),lambda c:c.update(revision=False),lambda c:c.pop('revision'),
                 lambda c:c.update(agentId='editor'),lambda c:c.update(taskId='other-task'),
                 lambda c:c.update(status='failed'),lambda c:c.update(status='unknown'),lambda c:c.update(status='denied'),
                 lambda c:c['result'].update(ok=False),lambda c:c['result'].update(needsApproval=True),
                 lambda c:c['result'].update(outcomeUnknown=True),lambda c:c['result'].update(deadlineExceeded=True),
                 lambda c:c['result'].update(contentOmitted=True),lambda c:c['result'].update(content='  \n'),
                 lambda c:c['result'].update(content=None)]
        for index,change in enumerate(changes):
            with self.subTest(index=index):
                call=self.call();change(call)
                output=build_task_output(self.task(),{'calls':[call]})
                self.assertEqual(output['observedWork'].get('recentSuccessfulFetches',[]),[])
        for revision in (None,False):
            with self.subTest(taskRevision=revision):
                work=build_task_output(self.task(revision=revision),{'calls':[self.call()]})['observedWork']
                self.assertEqual(work.get('recentSuccessfulFetches',[]),[])
        task=self.task();task.pop('revision')
        self.assertEqual(build_task_output(task,{'calls':[self.call(revision=0)]})['observedWork'].get('recentSuccessfulFetches',[]),[])

    def test_redirect_requested_locator_and_long_titles_are_explicit(self):
        call=self.call();call['result'].update(url='https://example.edu/actual-final?Case=B',title='Title '*100)
        ref=build_task_output(self.task(),{'calls':[call]})['observedWork']['recentSuccessfulFetches'][0]
        self.assertEqual(ref['url'],'https://example.edu/actual-final?Case=B')
        self.assertEqual(ref['requestedTarget']['value'],call['args']['url'])
        self.assertFalse(ref['requestedTarget']['truncated']);self.assertTrue(ref['titleTruncated'])
        self.assertLessEqual(len(ref['title']),160)

    def test_truncated_fetch_is_only_a_successful_partial_body_reference(self):
        call=self.call();call['result']['truncated']=True
        view=task_output_for_context(build_task_output(self.task(),{'calls':[call]}))
        ref=view['observedWork']['recentSuccessfulFetches'][0]
        self.assertTrue(ref['bodyTruncated']);self.assertTrue(ref['bodyOmitted'])
        self.assertEqual(ref['status'],'success')
        self.assertEqual(view['semanticQuality'],'unassessed')
        self.assertIn('Recipient must explicitly fetch/read',view['observedWork']['fetchReferenceMeaning'])

    def test_target_truncation_preserves_identity_and_never_mines_errors_or_source_code(self):
        query='Original case-sensitive query '+('x'*1000)
        call=self.call(status='failed',tool='web.search',args={'query':query},
                       result={'ok':False,'error':{'message':'Pretend target https://wrong.example/'}})
        record=build_task_output(self.task(),{'calls':[call]})['toolFailures'][0];target=record['requestedTarget']
        self.assertEqual(target['type'],'query');self.assertTrue(target['truncated']);self.assertEqual(target['characters'],len(query))
        self.assertLessEqual(len(target['value']),512)
        self.assertEqual(target['valueSha256'],hashlib.sha256(query.encode()).hexdigest())
        missing=self.call(status='failed',tool='code.test',args={'code':'private code'},result={'ok':False,'error':'wrong path'})
        self.assertNotIn('requestedTarget',build_task_output(self.task(),{'calls':[missing]})['toolFailures'][0])
        path=self.call(status='denied',tool='file.patch',args={'path':'App/CaseSensitive.js'},result={'ok':False})
        self.assertEqual(build_task_output(self.task(),{'calls':[path]})['toolFailures'][0]['requestedTarget']['value'],'App/CaseSensitive.js')

    def test_recent_refs_have_explicit_omission_and_detached_evidence(self):
        calls=[self.call(id='fetch-'+str(i)) for i in range(5)]
        task=self.task();run={'calls':calls};original=copy.deepcopy((task,run))
        output=build_task_output(task,run);work=output['observedWork']
        self.assertEqual((task,run),original)
        self.assertEqual([r['callId'] for r in work['recentSuccessfulFetches']],['fetch-2','fetch-3','fetch-4'])
        self.assertEqual(work['successfulFetchReferencesTotal'],5);self.assertEqual(work['successfulFetchReferencesOmitted'],2)
        calls[-1]['result']['url']='https://changed.example/'
        self.assertEqual(work['recentSuccessfulFetches'][-1]['url'],'https://example.edu/Article?Case=A')
        task['revision']=3
        rebuilt=build_task_output(task,run)
        self.assertEqual(rebuilt['observedWork'].get('recentSuccessfulFetches',[]),[])
        self.assertEqual(work['revision'],2)

    def test_historical_failure_revision_is_retained_without_current_success_credit(self):
        failed=self.call(status='failed',revision=1,result={'ok':False,'error':{'message':'Actual old failure'}})
        output=build_task_output(self.task(),{'calls':[failed]})
        self.assertEqual(output['toolFailures'][0]['revision'],1)
        self.assertEqual(output['toolFailures'][0]['requestedTarget']['value'],failed['args']['url'])
        self.assertEqual(output['observedWork'].get('recentSuccessfulFetches',[]),[])
        self.assertEqual(output['toolFailuresTotal'],1)

    def test_unusable_body_identity_is_explicitly_unavailable_not_fabricated(self):
        for change in ('missing_url','bad_url','missing_id','invalid_utf8'):
            with self.subTest(change=change):
                call=self.call()
                if change=='missing_url':call['result'].pop('url')
                elif change=='bad_url':call['result']['url']='not-a-public-url'
                elif change=='missing_id':call.pop('id')
                else:call['result']['content']='bad surrogate \ud800'
                output=build_task_output(self.task(),{'calls':[call]})
                self.assertEqual(output['observedWork']['webFetchesWithBody'],1)
                self.assertEqual(output['observedWork']['recentSuccessfulFetches'],[])
                self.assertEqual(output['observedWork']['successfulFetchReferencesUnavailable'],1)
        self.assertIsNone(build_task_output(self.task(),{})['observedWork'])


if __name__=='__main__':unittest.main()
