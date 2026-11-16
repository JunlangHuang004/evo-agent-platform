"""Owner-only learning hooks in the real scheduler; scripted models, no external calls."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.tools import ToolGateway
from tests.test_engine import ScriptedProvider,task,final

TITLE='Inspect app.js workflow'
class Files(ToolGateway):
    def capabilities(self):return {}


def wait_until(fn):
    stop=time.monotonic()+3
    while time.monotonic()<stop:
        if fn():return True
        time.sleep(.01)
    return False


class LearningRuntimeHookTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.tmp.cleanup()
    def create(self,scripts,plan=None):
        config=Config(Path(self.tmp.name)/str(len(self.apps)),{'LLM_API_KEY':'offline-personal','MAX_RETRIES':'0','MAX_AGENT_TURNS':'8','MAX_RUN_TOKENS':'400000'})
        model=ScriptedProvider(plan or [task(TITLE,'coordinator')],scripts)
        app=Platform(config,provider=model,tools=Files(config));self.apps.append(app);return app,model
    def source(self,app,owner):
        target={'id':'source-task-'+owner,'agentId':owner,'revision':0,'title':TITLE,'goal':TITLE,
                'status':'failed','error':{'code':'budget'},'outputs':[]}
        run={'id':'source-run-'+owner,'teamId':'default','channelId':'group','status':'failed',
             'participants':[owner],'steps':[target],'calls':[]}
        text='PRIVATE_SOURCE_BODY_'+owner
        for index in range(3):
            run['calls'].append({'id':'source-read-'+owner+str(index),'agentId':owner,'taskId':target['id'],
                'revision':0,'tool':'file.read','status':'success','args':{'path':'app.js'},
                'result':{'ok':True,'path':'app.js','sha256':hashlib.sha256(text.encode()).hexdigest(),'content':text,'mode':'full'}})
        return app.learning.observe_failure(owner,run,target)['candidate']
    def send(self,app,participants=None,**extra):
        app.action({'action':'send','mode':'task','participants':participants or ['coordinator'],'text':TITLE,**extra})

    def test_actual_first_context_offers_only_own_candidate_and_final_ack_is_not_confirmation(self):
        own=other=None
        def answer(n,context,messages):
            self.assertEqual(context['memories'],[])
            personal=context['personalLearning'];encoded=json.dumps(personal,ensure_ascii=False)
            self.assertLessEqual(len(encoded),2400);self.assertNotIn('PRIVATE_SOURCE_BODY',encoded)
            self.assertIn(own['id'],encoded);self.assertNotIn(other['id'],encoded)
            self.assertTrue(all(item['verificationStatus']=='unverified' for item in personal['experimentalCandidates']))
            return final('A text result does not prove a workflow experiment.',reflection={'trialMemoryIds':[own['id'],other['id']]})
        app,model=self.create({TITLE:answer});own=self.source(app,'coordinator');other=self.source(app,'research')
        self.send(app);self.assertTrue(app.wait_for_idle(4));run=app.snapshot()['sessions']['group']['run'];target=run['steps'][-1]
        self.assertEqual(target['learningTrialIds'],[own['id']]);self.assertEqual(target['learningAcknowledgedIds'],[own['id']])
        self.assertFalse(run['memoryHits'],'unverified candidate offers must not be marked applied strategies')
        current=app.repo.get_memory(own['id'],'coordinator');self.assertEqual(current['status'],'candidate')
        self.assertEqual(current['learningTrials'][0]['assessment'],'unassessed')
        self.assertFalse(current['learningTrials'][0]['adoptionProven']);self.assertEqual(current['learningTrialCounts']['passed'],0)

    def test_verified_strategies_are_owner_only_and_not_duplicated_in_legacy_memories(self):
        app,model=self.create({TITLE:[final('A plain result.')]})
        owned=[]
        for owner in ('coordinator','research'):
            record=app.memory.propose(owner,TITLE+' use a targeted read',TITLE,'old-task','old-run',[],conditions='Only if current source remains available.')
            app.memory.confirm(record['id'],owner,True,{'type':'human','id':'fixture-'+owner});owned.append(record)
        self.send(app);self.assertTrue(app.wait_for_idle(3));context=model.records[0]['context']
        self.assertEqual(context['memories'],[])
        ids=[item['memoryId'] for item in context['personalLearning']['verifiedStrategies']]
        self.assertEqual(ids,[owned[0]['id']]);self.assertNotIn(owned[1]['id'],json.dumps(context))
        hits=app.snapshot()['sessions']['group']['run']['memoryHits'];self.assertEqual([item['memoryId'] for item in hits],[owned[0]['id']])

    def test_disabled_memory_does_not_offer_or_start_candidate_trial(self):
        app,model=self.create({TITLE:[final('No learning context used.')]});candidate=self.source(app,'coordinator')
        self.send(app,memoryEnabled=False);self.assertTrue(app.wait_for_idle(3));context=model.records[0]['context']
        self.assertEqual(context['personalLearning']['experimentalCandidates'],[])
        self.assertFalse(app.repo.get_memory(candidate['id'],'coordinator')['learningTrials'])
        self.assertFalse(app.snapshot()['sessions']['group']['run']['memoryHits'])

    def test_resume_filters_a_strategy_that_was_rejected_after_initial_injection(self):
        app,model=self.create({TITLE:[{'type':'ask_human','question':'Need a remaining real requirement?','choices':['A']}]})
        strategy=app.memory.propose('coordinator',TITLE,TITLE,'old-task','old-run',[],conditions='Applicable only to this workflow.')
        app.memory.confirm(strategy['id'],'coordinator',True,{'type':'human','id':'fixture-confirm'})
        self.send(app);self.assertTrue(wait_until(lambda:bool(app.state['sessions']['group']['run']['approvals'])))
        with app.changed:
            run=app.state['sessions']['group']['run'];target=run['steps'][-1]
            self.assertTrue(json.loads(target['_messages'][1]['content'])['personalLearning']['verifiedStrategies'])
            app.memory.confirm(strategy['id'],'coordinator',False,{'type':'human','id':'fixture-reject'})
            app._prepare_task('group',run['id'],target)
            self.assertEqual(json.loads(target['_messages'][1]['content'])['personalLearning']['verifiedStrategies'],[])

    def test_final_reviewer_rejection_updates_provisional_episode_without_duplicate_feedback(self):
        plan=[task('Build','coordinator'),task('Review','reviewer',['Build'],['review'])]
        def review(n,context,messages):
            if n==0:return {'type':'review','taskId':context['dependencyResults'][0]['id'],'verdict':'reject','feedback':'The text fails the stated requirement.'}
            return final('Recorded a genuine rejection; no successful acceptance.')
        app,model=self.create({'Build':[final('Provisional text result.')],'Review':review},plan)
        self.send(app,['coordinator','reviewer']);self.assertTrue(app.wait_for_idle(4));run=app.snapshot()['sessions']['group']['run']
        target=next(item for item in run['steps'] if item['title']=='Build');self.assertEqual(target['status'],'failed')
        episodes=[item for item in app.memory.list('coordinator') if item['kind']=='episodic' and item.get('sourceTaskId',item.get('source_task'))==target['id']]
        self.assertEqual(len(episodes),1);self.assertFalse(episodes[0]['success'])
        self.assertTrue(episodes[0].get('learningFacts') is not None)
        self.assertTrue(any(item['success'] is True for item in episodes[0]['outcomeHistory']))
        self.assertEqual(len([e for e in run['events'] if e['type']=='personal_failure_observed' and e['payload']['taskId']==target['id']]),1)


if __name__=='__main__':unittest.main()
