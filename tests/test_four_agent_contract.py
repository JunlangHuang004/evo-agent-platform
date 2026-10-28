"""Explicit four-role planning and revision-bound independent acceptance contracts."""
import copy
from pathlib import Path
import tempfile
import unittest
from backend.agents import AGENTS
from backend.config import Config
from backend.engine import Platform
from backend.runtime import normalize_plan, ProtocolError


class InertTools:
    def capabilities(self): return {}
    def definitions(self, actor): return []
    def cancel(self, run_id): return None


class FourAgentContractTests(unittest.TestCase):
    @staticmethod
    def plan():
        return {'tasks': [
            {'id':'research','title':'Research sources','agentId':'research','dependsOn':[]},
            {'id':'execute','title':'Implement','agentId':'executor','dependsOn':['research']},
            {'id':'review','title':'Independent check','agentId':'reviewer','dependsOn':['execute']},
            {'id':'summary','title':'Synthesize','agentId':'coordinator','dependsOn':['review']},
        ]}

    def test_four_role_plans_are_complete_bound_and_end_in_real_synthesis(self):
        make_id=lambda value: value+'-'+str(next(self.counter))
        self.counter=iter(range(100))
        tasks=normalize_plan(self.plan(),list(AGENTS),make_id,6)
        self.assertTrue(tasks[-1]['finalSummary'])
        self.assertIn('synthesis',tasks[-1]['requiredCapabilities'])
        self.assertEqual([row['requiredCapabilities'][0] for row in tasks],['research','coding','review','planning'])
        for missing in range(4):
            plan=self.plan()
            removed=plan['tasks'].pop(missing)['id']
            for row in plan['tasks']:
                row['dependsOn']=[value for value in row['dependsOn'] if value!=removed]
            with self.subTest(missing=missing),self.assertRaises(ProtocolError):
                normalize_plan(plan,list(AGENTS),make_id,6)
        plan=self.plan();plan['tasks'][-1]['dependsOn']=[]
        with self.assertRaises(ProtocolError):
            normalize_plan(plan,list(AGENTS),make_id,6)
        unspecified={'tasks':[{'id':'generic','title':'general task'}]}
        generic=normalize_plan(unspecified,['coordinator','research'],make_id,3)
        self.assertEqual(generic[0]['requiredCapabilities'],[])
        self.assertIsNone(generic[0]['agentId'])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=Platform(Config(Path(self.temp.name),{'LLM_API_KEY':'fake-contract-key'}),tools=InertTools())
        self.run={'id':'run-contract','title':'code workflow','mode':'task','status':'running','participants':list(AGENTS),
                  'respondingAgentId':'coordinator','startedAt':0,'steps':[],'calls':[],'events':[],'approvals':[],
                  'artifacts':[{'id':'artifact-code','name':'solution.py','sha256':'a'*64,'ownerAgentId':'executor'}],
                  'memoryHits':[],'messages':[]}
        self.target=self.app._new_task('Implement','Implement','coordinator',list(AGENTS),'executor')
        self.target.update(status='done',result='code complete',outputs=['artifact-code'])
        self.reviewer=self.app._new_task('Independent check','Check','coordinator',list(AGENTS),'reviewer')
        self.reviewer.update(status='done',result='review complete',dependsOn=[self.target['id']])
        self.summary=self.app._new_task('Synthesize','Synthesize','human',list(AGENTS),'coordinator')
        self.summary.update(status='done',result='ACTUAL MODEL SYNTHESIS',finalSummary=True,dependsOn=[self.reviewer['id']])
        self.run['steps']=[self.target,self.reviewer,self.summary]
        self.app.state['sessions']['group']['run']=self.run

    def tearDown(self):
        self.app.repo.close()
        self.temp.cleanup()

    def evidence(self):
        return {'id':'call-review','taskId':self.reviewer['id'],'agentId':'reviewer','tool':'code.test','status':'success',
                'result':{'ok':True,'exitCode':0},'reviewTargets':{self.target['id']:self.app._review_snapshot(self.run,self.target)}}

    def review(self, verdict='pass', evidence=None):
        return self.app._review(self.run,self.reviewer,{'taskId':self.target['id'],'verdict':verdict,'feedback':'checked',
                                                       'evidenceCallIds':evidence or []})

    def test_no_foreign_stale_or_missing_evidence_can_accept_code(self):
        result=self.review()
        self.assertFalse(result['ok'])
        self.assertFalse(result['validation']['verified'])
        call=self.evidence();call['agentId']='executor';self.run['calls']=[call]
        self.assertFalse(self.review(evidence=['call-review'])['ok'])
        call=self.evidence();self.run['calls']=[call]
        accepted=self.review(evidence=['call-review'])
        self.assertTrue(accepted['ok'])
        self.assertEqual(accepted['validation']['targetRevision'],0)
        self.assertEqual(accepted['validation']['artifactHashes'],{'artifact-code':'a'*64})
        self.assertTrue(self.app._valid_review(self.run,self.target))
        self.run['artifacts'][0]['sha256']='b'*64
        self.assertFalse(self.app._valid_review(self.run,self.target))
        self.assertFalse(self.review(evidence=['call-review'])['ok'])
        self.app._finish('group',self.run['id'])
        self.assertEqual(self.run['status'],'partial')
        self.assertIn('最新代码',self.run['summary'])
        self.assertIn('ACTUAL MODEL SYNTHESIS',self.run['summary'])

    def test_rework_discards_validation_and_requires_new_revision_evidence(self):
        old=self.evidence();self.run['calls']=[old]
        self.assertTrue(self.review(evidence=['call-review'])['ok'])
        self.review('rework')
        self.assertEqual(self.target['revision'],1)
        self.assertNotIn('validation',self.target)
        self.assertEqual(self.target['outputs'],[])
        self.target.update(status='done',outputs=['artifact-code'])
        self.assertFalse(self.review(evidence=['call-review'])['ok'])
        new=self.evidence();new['id']='call-new';self.run['calls'].append(new)
        self.assertTrue(self.review(evidence=['call-new'])['ok'])
        self.app._finish('group',self.run['id'])
        self.assertEqual(self.run['status'],'done')

    def test_non_code_four_role_work_does_not_require_docker_test(self):
        self.run['artifacts'][0]['name']='notes.md'
        self.target['result']='documentary task done'
        self.app._finish('group',self.run['id'])
        self.assertEqual(self.run['status'],'done')
        self.assertFalse(self.run['acceptanceGaps'])


if __name__=='__main__': unittest.main()
