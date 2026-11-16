"""Failure summaries distinguish unpublished work from work never performed."""
import copy
import unittest
from backend.task_output import build_task_output, task_output_for_context


class ObservedWorkTests(unittest.TestCase):
    def setUp(self):
        self.task={'id':'task1','agentId':'dev','revision':0,'status':'failed','outputs':[],
                   'result':'','error':{'code':'protocol','message':'Actual context capacity error'}}

    def call(self,name,result,**changes):
        return {'id':name+'-call','taskId':'task1','agentId':'dev','revision':0,'tool':name,'status':'success','result':result,**changes}

    def test_failed_task_keeps_runtime_error_and_real_unpublished_work(self):
        calls=[self.call('web.fetch',{'ok':True,'content':'Actual source text'}),
               self.call('file.write',{'ok':True,'path':'draft.md','sha256':'a'*64}),
               self.call('code.test',{'ok':True,'exitCode':0}),
               self.call('file.patch',{'ok':False,'error':{'code':'parameter','message':'Earlier failed patch'}},status='failed')]
        before=copy.deepcopy(calls);out=build_task_output(self.task,{'calls':calls})
        self.assertEqual(out['executionError'],self.task['error'])
        self.assertEqual(out['observedWork']['webFetchesWithBody'],1)
        self.assertEqual(out['observedWork']['fileWrites'],1)
        self.assertEqual(out['observedWork']['codeTestsExitedZero'],1)
        self.assertEqual(out['artifactIds'],[])
        self.assertEqual(out['semanticQuality'],'unassessed')
        self.assertEqual(task_output_for_context(out)['observedWork'],out['observedWork'])
        self.assertEqual(calls,before)

    def test_foreign_old_revision_unknown_and_bodyless_calls_do_not_count(self):
        good={'ok':True,'content':'actual'}
        calls=[self.call('web.fetch',good,agentId='other'),self.call('web.fetch',good,revision=1),
               self.call('web.fetch',good,revision=False),self.call('web.fetch',good,status='unknown'),
               self.call('web.fetch',{'ok':True,'content':'actual','outcomeUnknown':True}),
               self.call('web.fetch',{'ok':True,'content':'omitted','contentOmitted':True}),
               self.call('web.fetch',{'ok':True,'url':'https://example.com/'})]
        out=build_task_output(self.task,{'calls':calls})
        self.assertEqual(out['observedWork']['webFetchesWithBody'],0)
        self.assertIsNone(build_task_output(self.task,{})['observedWork'])

    def test_recent_versions_are_bounded_without_claiming_they_still_exist(self):
        calls=[self.call('file.write',{'ok':True,'path':str(i)+'.md','sha256':'a'*64},id=str(i)) for i in range(10)]
        calls.append(self.call('file.delete',{'ok':True,'deleted':True,'path':'9.md'}))
        out=build_task_output(self.task,{'calls':calls})['observedWork']
        self.assertEqual(out['fileWrites'],10)
        self.assertEqual(out['writtenVersionReferencesOmitted'],7)
        self.assertEqual(len(out['recentWrittenVersions']),3)
        self.assertIn('deleted',out['meaning'])


if __name__=='__main__':unittest.main()
