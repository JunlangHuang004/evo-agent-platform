"""A recorded budget refusal is shared, durable and never a new permission."""
import copy
import threading
import time
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform, BudgetExceeded, clock_ms


class NoTools:
    def capabilities(self):return {}
    def definitions(self,agent):return []
    def cancel(self,run_id):pass


class NoModel:
    def complete(self,*args,**kwargs):raise AssertionError('Budget tests never request a model')


class BudgetDeclineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.apps=[];self.threads=[]
        self.config=Config(Path(self.temp.name),{'LLM_API_KEY':'offline-budget','MAX_RUN_TOKENS':'100000'})
        self.app=self.create();a=self.app._new_task('One','One','human',['coordinator','executor'],'coordinator')
        b=self.app._new_task('Two','Two','human',['coordinator','executor'],'executor')
        self.run={'id':'run-budget','title':'Bounded work','mode':'task','status':'running','participants':['coordinator','executor'],
            'respondingAgentId':'coordinator','startedAt':clock_ms(),'finishedAt':None,'steps':[a,b],'calls':[],
            'events':[],'approvals':[],'artifacts':[],'messages':[],'memoryHits':[],'modelCalls':2,'tokenUsage':0,'activeMs':0,
            'budgetExtensions':0,'budget':{'modelCalls':2,'tokens':100000,'timeMs':600000,'tasks':12,'depth':3},'_planned':True}
        self.app.state['sessions']['group']['run']=self.run;self.app._persist();self.failures=[]
    def create(self):
        app=Platform(self.config,provider=NoModel(),tools=NoTools());self.apps.append(app);return app
    def tearDown(self):
        for app in self.apps:app.shutdown()
        for thread in self.threads:thread.join(3)
        for app in self.apps:app.repo.close()
        self.temp.cleanup()
    def worker(self,task):
        try:self.app._budget('group',self.run['id'],task,1)
        except Exception as exc:self.failures.append(exc)
    def start(self,task):
        thread=threading.Thread(target=self.worker,args=(task,));self.threads.append(thread);thread.start()
    def pending(self):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with self.app.lock:
                approvals=[a for a in self.run['approvals'] if a['status']=='pending']
                if approvals:return approvals[0]
            time.sleep(.005)
        self.fail('Expected an actual pending budget approval')
    def answer(self,approval,choice):
        self.app.action({'action':'approval','runId':self.run['id'],'approvalId':approval['id'],'choiceId':choice})
    def join(self):
        for thread in self.threads:thread.join(3);self.assertFalse(thread.is_alive())

    def test_concurrent_and_later_tasks_share_one_real_denial_without_changing_budget(self):
        original=copy.deepcopy(self.run['budget'])
        self.start(self.run['steps'][0]);approval=self.pending();self.start(self.run['steps'][1])
        self.answer(approval,'deny');self.join()
        self.assertEqual(len(self.failures),2);self.assertTrue(all(isinstance(e,BudgetExceeded) for e in self.failures))
        self.assertEqual(len(self.run['approvals']),1);self.assertEqual(self.run['budget'],original)
        decision=self.run['budgetExtensionDecision'];self.assertEqual(decision['decision'],'denied')
        self.assertEqual(decision['approvalId'],approval['id']);self.assertEqual(self.run['budgetExtensions'],0)
        with self.assertRaises(BudgetExceeded):self.app._budget('group',self.run['id'],self.run['steps'][1],1)
        self.assertEqual(len(self.run['approvals']),1)
        self.assertEqual(self.app.repo.get_run(self.run['id'])['budgetExtensionDecision'],decision)
        self.assertEqual(sum(e['type']=='budget_extension_declined' for e in self.run['events']),1)

    def test_restart_keeps_decision_but_an_action_inside_original_budget_is_allowed(self):
        self.start(self.run['steps'][0]);self.answer(self.pending(),'deny');self.join()
        before=copy.deepcopy(self.run['budget']);self.app.shutdown()
        restored=self.create();run=restored.state['sessions']['group']['run'];run['status']='running'
        with self.assertRaises(BudgetExceeded):restored._budget('group',run['id'],run['steps'][1],1)
        self.assertEqual(len(run['approvals']),1);run['modelCalls']=0
        restored._budget('group',run['id'],run['steps'][1],1)
        self.assertEqual(run['budget'],before);self.assertEqual(len(run['approvals']),1)

    def test_legacy_denial_and_stale_prechecked_ask_do_not_create_another_prompt(self):
        self.start(self.run['steps'][0]);self.answer(self.pending(),'deny');self.join()
        self.run.pop('budgetExtensionDecision')
        with self.assertRaises(BudgetExceeded):self.app._budget('group',self.run['id'],self.run['steps'][1],1)
        self.assertEqual(self.run['budgetExtensionDecision']['decision'],'denied')
        with self.assertRaises(BudgetExceeded):
            self.app._ask('group',self.run['id'],self.run['steps'][1],'budget','Budget','Same budget',
                          [{'id':'allow','label':'Allow'},{'id':'deny','label':'Deny'}],binding={'budget':copy.deepcopy(self.run['budget'])})
        self.assertEqual(len(self.run['approvals']),1)

    def test_expiry_is_not_misattributed_as_human_denial(self):
        self.start(self.run['steps'][0]);approval=self.pending()
        with self.app.changed:approval['expiresAt']=0;self.app.changed.notify_all()
        self.join();self.assertIsInstance(self.failures[0],BudgetExceeded)
        self.assertEqual(self.run['budgetExtensionDecision']['decision'],'expired')
        self.assertEqual(next(e for e in self.run['events'] if e['type']=='budget_extension_declined')['actor'],None)
        with self.assertRaises(BudgetExceeded):self.app._budget('group',self.run['id'],self.run['steps'][1],1)
        self.assertEqual(len(self.run['approvals']),1)

    def test_allow_remains_exactly_one_explicit_bounded_extension(self):
        original=copy.deepcopy(self.run['budget']);self.start(self.run['steps'][0]);self.answer(self.pending(),'allow');self.join()
        self.assertEqual(self.failures,[]);self.assertEqual(self.run['budgetExtensions'],1)
        self.assertEqual(self.run['budget']['modelCalls'],original['modelCalls']*2)
        self.assertEqual(self.run['budget']['tokens'],original['tokens']*2)
        self.assertNotIn('budgetExtensionDecision',self.run)
        self.run['modelCalls']=self.run['budget']['modelCalls']
        with self.assertRaises(BudgetExceeded):self.app._budget('group',self.run['id'],self.run['steps'][1],1)
        self.assertEqual(len(self.run['approvals']),1)


if __name__=='__main__':unittest.main()
