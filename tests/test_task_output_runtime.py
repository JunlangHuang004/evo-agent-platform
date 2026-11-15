"""Structured handoffs in runtime/public history; no external requests or tools."""
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.runtime import compress_messages


class NoTools:
    def capabilities(self):return {}
    def definitions(self,agent):return []
    def cancel(self,run):return None


class TaskOutputRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=Platform(Config(Path(self.temp.name),{'LLM_API_KEY':'fixture-only'}),tools=NoTools())
        self.target=self.app._new_task('Actual result','Actual result','human',['research'],'research')
        self.target.update(status='done',result='{"score":12,"sources":[]}',
                           outputContract={'kind':'json','requiredKeys':['score','sources']},
                           outputValidation={'status':'passed','kind':'json','semanticQuality':'unassessed'})
        self.run={'id':'run-task-output','title':'Handoff test','status':'running','mode':'task',
                  'startedAt':1,'memoryEnabled':False,'respondingAgentId':'coordinator','participants':['coordinator','research','reviewer'],
                  'steps':[self.target],'calls':[
                    {'id':'call-denied','taskId':self.target['id'],'agentId':'research','tool':'file.delete','status':'denied',
                     'result':{'ok':False,'denied':True,'error':{'code':'permission','message':'The human denied deletion.'}}},
                    {'id':'call-unknown','taskId':self.target['id'],'agentId':'research','tool':'file.write','status':'unknown','result':None}],
                  'events':[],'approvals':[],'artifacts':[],'memoryHits':[],'messages':[]}
        self.app.state['sessions']['group']['run']=self.run

    def tearDown(self):
        self.app.shutdown();self.app.wait_for_idle(2);self.app.repo.close();self.temp.cleanup()

    def test_public_output_is_fresh_after_review_and_keeps_format_separate(self):
        planning=self.app._new_task('Plan','Plan','human',self.run['participants'],'coordinator')
        planning.update(_planning=True,kind='planning',status='done')
        unowned=self.app._new_task('Unassigned','Unassigned','human',self.run['participants'])
        self.run['steps'].extend([planning,unowned])
        first=self.app._public(self.run)
        output=first['steps'][0]['taskOutput']
        self.assertEqual(output['jsonDict'],{'score':12,'sources':[]})
        self.assertIsNone(output['reviewValidation'])
        self.assertEqual(output['semanticQuality'],'unassessed')
        self.assertEqual(output['toolFailures'][0]['type'],'permission_denied')
        self.assertEqual(output['toolUncertainties'][0]['callId'],'call-unknown')
        self.assertNotIn('taskOutput',first['steps'][1])
        self.assertNotIn('taskOutput',first['steps'][2])
        reviewer=self.app._new_task('Review','Review','human',self.run['participants'],'reviewer')
        self.run['steps'].append(reviewer)
        self.app._review(self.run,reviewer,{'taskId':self.target['id'],'verdict':'reject','feedback':'The cited result is not sufficiently supported.'})
        changed=self.app._public(self.app.repo.get_run(self.run['id']))['steps'][0]['taskOutput']
        self.assertEqual(changed['status'],'failed')
        self.assertEqual(changed['reviewValidation']['verdict'],'reject')
        self.assertFalse(changed['reviewValidation']['verified'])
        self.assertEqual(changed['outputValidation']['status'],'passed','format evidence does not override a review rejection')
        self.assertEqual(changed['semanticQuality'],'unassessed')
        self.assertNotIn('taskOutput',self.target,'derived public data must not be cached on the source task')

    def test_missing_call_log_is_unknown_not_an_invented_clean_history(self):
        self.run['calls']=None
        output=self.app._public(self.run)['steps'][0]['taskOutput']
        self.assertFalse(output['evidenceAvailability']['calls'])
        self.assertIsNone(output['toolFailures'])
        self.assertIsNone(output['toolUncertainties'])
        self.assertIsNone(output['reviewValidation'])

    def test_dependency_and_await_handoffs_have_projection_without_duplicate_raw(self):
        parent=self.app._new_task('Synthesize','Synthesize','human',self.run['participants'],'coordinator')
        parent.update(status='running',dependsOn=[self.target['id']],childIds=[self.target['id']])
        self.run['steps'].append(parent)
        self.app._prepare_task('group',self.run['id'],parent)
        context=json.loads(parent['_messages'][1]['content'])
        dependency=context['dependencyResults'][0]
        self.assertEqual(dependency['result'],self.target['result'])
        projection=dependency['taskOutput']
        self.assertNotIn('raw',projection)
        self.assertTrue(projection['rawInDependencyResult'])
        self.assertEqual(projection['jsonDict'],{'score':12,'sources':[]})
        self.assertEqual(projection['toolFailuresTotal'],1)
        self.assertEqual(projection['toolUncertaintiesTotal'],1)
        self.assertIsNone(projection['reviewValidation'])
        waited=self.app._await_task('group',self.run['id'],parent,self.target['id'])
        self.assertEqual(waited['result'],self.target['result'])
        self.assertNotIn('raw',waited['taskOutput'])
        self.assertEqual(waited['taskOutput']['toolFailures'][0]['callId'],'call-denied')

    def test_compression_preserves_missing_evidence_and_short_failure_receipts(self):
        parent=self.app._new_task('Summarize','Summarize','human',self.run['participants'],'coordinator')
        parent.update(status='running',dependsOn=[self.target['id']])
        self.run['steps'].append(parent)
        self.app._prepare_task('group',self.run['id'],parent)
        context=json.loads(parent['_messages'][1]['content'])
        context['ownArtifacts']=[{'id':'artifact-large','name':'source.txt','content':'x'*50000}]
        messages=[{'role':'system','content':'Protocol.'},{'role':'user','content':json.dumps(context)}]
        compact=compress_messages(messages,4000)
        self.assertLessEqual(sum(len(item['content']) for item in compact),4000)
        projection=json.loads(compact[1]['content'])['dependencyResults'][0]['taskOutput']
        self.assertIsNone(projection['reviewValidation'])
        self.assertFalse(projection['evidenceAvailability']['reviewValidation'])
        self.assertEqual(projection['outputValidation']['status'],'passed')
        self.assertEqual(projection['outputValidation']['semanticQuality'],'unassessed')
        self.assertEqual(projection['toolFailures'][0]['callId'],'call-denied')
        self.assertEqual(projection['toolFailures'][0]['type'],'permission_denied')
        self.assertIn('human denied',projection['toolFailures'][0]['error']['message'])
        self.assertEqual(projection['toolFailuresTotal'],1)
        self.assertFalse(projection['toolFailuresTruncated'])
        self.assertEqual(projection['toolUncertainties'][0]['callId'],'call-unknown')
        self.assertEqual(projection['toolUncertaintiesTotal'],1)
        self.assertTrue(projection['jsonDictOmitted'],'compaction omits structured facts instead of modifying their values')


if __name__=='__main__':unittest.main()
