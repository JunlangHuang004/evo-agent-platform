"""HTML review requires actual browser observations of the current delivered bytes."""
import copy
from pathlib import Path
import tempfile
import unittest

from backend.agents import AGENTS
from backend.config import Config
from backend.engine import Platform


class NoTools:
    def capabilities(self):return {}
    def definitions(self,actor):return []
    def cancel(self,run_id):return None


class BrowserReviewBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=Platform(Config(Path(self.temp.name),{'LLM_API_KEY':'offline-review'}),tools=NoTools())
        self.target=self.app._new_task('Build product','Build','coordinator',list(AGENTS),'executor')
        self.target.update(status='done',result='Published implementation.',outputs=['html','script'])
        self.reviewer=self.app._new_task('Browser QA','Verify','coordinator',list(AGENTS),'reviewer')
        self.reviewer.update(status='done',result='Recorded QA.',dependsOn=[self.target['id']])
        self.summary=self.app._new_task('Synthesis','Summarize','human',list(AGENTS),'coordinator')
        self.summary.update(status='done',result='Recorded current evidence.',finalSummary=True,dependsOn=[self.reviewer['id']])
        self.run={'id':'browser-review-run','title':'Review actual HTML','status':'running','mode':'task',
                  'participants':list(AGENTS),'respondingAgentId':'coordinator','startedAt':0,
                  'steps':[self.target,self.reviewer,self.summary],'calls':[],'events':[],'approvals':[],
                  'artifacts':[{'id':'html','name':'index.html','sha256':'a'*64,'ownerAgentId':'executor'},
                               {'id':'script','name':'app.js','sha256':'b'*64,'ownerAgentId':'executor'}],
                  'memoryHits':[],'messages':[]}
        self.app.state['sessions']['group']['run']=self.run

    def tearDown(self):
        self.app.repo.close();self.temp.cleanup()

    def evidence(self,browser=True):
        result={'ok':True,'exitCode':0,'runtime':'python'}
        if browser:
            result.update(runtime='browser',test=True,browser={
                'engine':'chromium','executed':True,
                'entry':{'path':'imports/html/index.html','sha256':'a'*64},
                'testedFiles':[{'path':'imports/html/index.html','sha256':'a'*64},
                               {'path':'imports/script/app.js','sha256':'b'*64}],
                'assertions':[{'action':'assert_text','selector':'#status','expected':'Saved','actual':'Saved','passed':True}],
                'assertionCount':1,'assertionsPassed':1,'pageErrors':[],'httpErrors':[]})
        return {'id':'browser-call','taskId':self.reviewer['id'],'agentId':'reviewer','tool':'code.test',
                'status':'success','args':{'language':'browser'},'result':result,
                'reviewTargets':{self.target['id']:self.app._review_snapshot(self.run,self.target)}}

    def review(self,call):
        self.run['calls']=[call]
        return self.app._review(self.run,self.reviewer,{'taskId':self.target['id'],'verdict':'pass',
                                'feedback':'Independent checks recorded.','evidenceCallIds':[call['id']]})

    def test_python_success_and_claimed_browser_args_do_not_verify_html(self):
        result=self.review(self.evidence(browser=False))
        self.assertFalse(result['ok'])
        self.assertFalse(result['validation']['verified'])
        self.assertEqual(result['validation']['kind'],'browser_test')
        self.assertEqual(result['validation']['evidenceCallIds'],[])
        self.assertIn('Python',result['error']['message'])
        self.assertFalse(self.app._valid_review(self.run,self.target))
        self.app._finish('group',self.run['id'])
        self.assertEqual(self.run['status'],'partial')

    def test_real_browser_checks_match_html_and_loaded_script_hashes(self):
        call=self.evidence();result=self.review(call)
        self.assertTrue(result['ok'])
        self.assertTrue(result['validation']['verified'])
        self.assertEqual(result['validation']['kind'],'browser_test')
        self.assertTrue(self.app._valid_review(self.run,self.target))
        self.run['artifacts'][1]['sha256']='c'*64
        self.assertFalse(self.app._valid_review(self.run,self.target))
        # Merely rebinding the call to a new revision cannot change the bytes
        # actually observed by the trusted browser runner.
        call['reviewTargets'][self.target['id']]=self.app._review_snapshot(self.run,self.target)
        self.assertFalse(self.review(call)['ok'])

    def test_wrong_entry_missing_assets_failed_assertions_and_fake_execution_are_rejected(self):
        for change in ('entry','asset','assertion','empty_assertions','page_error','http_error','not_executed','wrong_runtime','foreign_actor'):
            with self.subTest(change=change):
                call=self.evidence();browser=call['result']['browser']
                if change=='entry':browser['entry']['sha256']='wrong'
                if change=='asset':browser['testedFiles']=browser['testedFiles'][:1]
                if change=='assertion':browser['assertions'][0]['passed']=False
                if change=='empty_assertions':browser.update(assertions=[],assertionCount=0,assertionsPassed=0)
                if change=='page_error':browser['pageErrors']=['JavaScript failed.']
                if change=='http_error':browser['httpErrors']=[{'path':'app.js','status':404}]
                if change=='not_executed':browser['executed']=False
                if change=='wrong_runtime':call['result']['runtime']='javascript'
                if change=='foreign_actor':call['agentId']='executor'
                result=self.review(call)
                self.assertFalse(result['ok'])
                self.assertFalse(result['validation']['verified'])

    def test_persisted_old_python_validation_does_not_bypass_html_revalidation(self):
        call=self.evidence(browser=False);self.run['calls']=[call]
        self.target['validation']={'verdict':'pass','verified':True,'reviewerTaskId':self.reviewer['id'],
                                   'evidenceCallIds':[call['id']],**self.app._review_snapshot(self.run,self.target)}
        self.assertFalse(self.app._valid_review(self.run,self.target))

    def test_pure_python_independent_test_remains_compatible(self):
        self.run['artifacts'][0]['name']='solution.py';self.target['outputs']=['html']
        result=self.review(self.evidence(browser=False))
        self.assertTrue(result['ok'])
        self.assertEqual(result['validation']['kind'],'code_test')
        self.assertTrue(self.app._valid_review(self.run,self.target))

    def test_changed_upstream_input_invalidates_current_review_and_transitive_delivery_without_replay(self):
        source=self.app._new_task('Source requirements','Source','coordinator',list(AGENTS),'research')
        source.update(status='done',result='Version one.',outputs=['source'])
        self.run['steps'].insert(0,source)
        self.run['artifacts'].append({'id':'source','name':'source.md','sha256':'c'*64,'ownerAgentId':'research'})
        self.target['dependsOn']=[source['id']]
        self.target['dependencySnapshots']={source['id']:{'status':'done',**self.app._review_snapshot(self.run,source)}}
        self.reviewer['dependencySnapshots']={self.target['id']:{'status':'done',**self.app._review_snapshot(self.run,self.target)}}
        self.summary['dependencySnapshots']={self.reviewer['id']:{'status':'done',**self.app._review_snapshot(self.run,self.reviewer)}}
        self.assertTrue(self.review(self.evidence())['ok'])
        calls=copy.deepcopy(self.run['calls'])
        source['revision']=1;self.run['artifacts'][-1]['sha256']='d'*64
        self.assertFalse(self.app._valid_review(self.run,self.target))
        self.assertFalse(self.review(self.evidence())['ok'],'test of old product bytes does not refresh its stale inputs')
        self.app._finish('group',self.run['id'])
        self.assertEqual(self.run['status'],'partial')
        for task in (self.target,self.reviewer,self.summary):
            self.assertEqual(task['status'],'partial')
            self.assertEqual(task['error']['code'],'stale_dependency')
            self.assertEqual(task['dependencyValidation']['status'],'stale')
        self.assertEqual(len(self.run['calls']),len(calls),'validation must never replay a tool')
        self.assertTrue(any('依赖 revision' in gap for gap in self.run['acceptanceGaps']))


if __name__=='__main__':unittest.main()
