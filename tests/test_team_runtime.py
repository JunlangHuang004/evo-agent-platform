"""Team registries and consecutive-task runtime; no model/network/Docker calls."""
from pathlib import Path
import copy
import itertools
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.agents import AGENTS
from backend.config import Config
from backend.engine import ActionError, Platform
from backend.runtime import ProtocolError, action_prompt, normalize_plan, planning_prompt
from backend.teams import get_team_agents, list_teams
from backend.tools import ToolGateway


class ScriptedTeam:
    """Only a test model; workflow is not installed in the product registry."""
    def __init__(self, agents, clarify=False):
        self.agents=agents
        self.lead=next(key for key,value in agents.items() if 'planning' in value['capabilities'])
        self.roles=[key for key in agents if key!=self.lead]+[self.lead]
        self.round=0
        self.counts={}
        self.contexts=[]
        self.planning_systems=[]
        self.action_systems=[]
        self.clarify=clarify

    def complete(self,messages):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            self.round+=1
            self.planning_systems.append(messages[0]['content'])
            self.contexts.append(json.loads(messages[1]['content']))
            tasks=[{'id':role,'title':str(self.round)+':'+role,'agentId':role,
                    'dependsOn':self.roles[:-1] if role==self.lead else [],
                    'goal':'Contribute an independently recorded text result.'} for role in self.roles]
            action={'summary':'Model-fixture team plan','tasks':tasks}
        else:
            context=json.loads(messages[1]['content'])
            title=context['task']['title'];role=title.split(':',1)[1]
            self.action_systems.append((role,messages[0]['content']))
            self.contexts.append(context)
            index=self.counts.get(title,0);self.counts[title]=index+1
            results=[json.loads(item['content'].split('：',1)[1]) for item in messages
                     if item.get('content','').startswith('动作的真实结果：')]
            if role!=self.roles[0]:
                action={'type':'final','text':role+' text contribution','status':'done'}
            elif title.startswith('1:'):
                actions=[]
                if self.clarify:
                    actions.append({'type':'ask_human','question':'Which audience should this draft serve?', 'choices':['Students','Staff']})
                actions.extend([
                    {'type':'tool','name':'file.write','args':{'path':'note.md','content':'# Opaque prior material\ncontinuity-token-9371\n'}},
                    {'type':'publish_artifact','path':'note.md'},
                    {'type':'message','toAgent':self.lead,'text':'Published the actual source note.'},
                    {'type':'final','text':'Published the note.','status':'done'}])
                action=actions[index]
            else:
                imported=next(item['path'] for item in context['authorizedArtifacts'] if item['name']=='note.md')
                if index==0:
                    action={'type':'tool','name':'file.read','args':{'path':imported}}
                elif index==1:
                    actual=next(item['content'] for item in results if 'content' in item)
                    action={'type':'tool','name':'file.write','args':{'path':'continued.md','content':actual+'\nContinued from the imported immutable snapshot.\n'}}
                elif index==2:
                    action={'type':'publish_artifact','path':'continued.md'}
                else:
                    action={'type':'final','text':'Read and extended the actual prior artifact.','status':'done'}
        return {'content':json.dumps(action),'usage':{'total_tokens':4},'model':'fixture-reported-model'}


class TeamRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.apps=[]

    def tearDown(self):
        for app in self.apps:
            app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.temp.cleanup()

    def app(self,team='newsroom',clarify=False):
        agents=get_team_agents(team)
        root=Path(self.temp.name)/team
        config=Config(root,{'LLM_API_KEY':'fake-test-key','MAX_RETRIES':'0','MAX_MODEL_CALLS':'60',
                            'MAX_AGENT_TURNS':'16','MAX_RUN_TOKENS':'500000','MAX_CONTEXT_CHARS':'80000'})
        model=ScriptedTeam(agents,clarify)
        app=Platform(config,provider=model,agents=agents,team_id=team)
        self.apps.append(app)
        return app,model

    def send(self,app,text):
        app.action({'action':'send','channelId':'group','mode':'task','text':text,'participants':list(app.agents)})

    def wait(self,app):
        self.assertTrue(app.wait_for_idle(4))
        run=app.snapshot()['sessions']['group']['run']
        self.assertEqual(run['status'],'done',json.dumps(run.get('error',run['steps']),ensure_ascii=False))
        return run

    def test_all_teams_expose_independent_registries_and_full_role_contract(self):
        self.assertEqual([item['id'] for item in list_teams()],['default','software','newsroom','fund'])
        with self.assertRaises(ValueError):get_team_agents('unknown')
        for team in ('software','newsroom','fund'):
            with self.subTest(team=team):
                agents=get_team_agents(team);model=ScriptedTeam(agents)
                tasks=[{'id':role,'title':role,'agentId':role,
                        'dependsOn':model.roles[:-1] if role==model.lead else []} for role in model.roles]
                normalized=normalize_plan({'tasks':tasks},list(agents),lambda prefix:prefix+str(next(self.ids)),8,agents=agents)
                self.assertTrue(normalized[-1]['finalSummary'])
                for task in normalized:
                    self.assertEqual(task['candidateAgentIds'],[task['agentId']])
                    self.assertIn(agents[task['agentId']]['primaryCapability'],task['requiredCapabilities'])
                missing=copy.deepcopy(tasks);missing.pop(0)
                for row in missing:row['dependsOn']=[]
                with self.assertRaises(ProtocolError):
                    normalize_plan({'tasks':missing},list(agents),lambda prefix:prefix+str(next(self.ids)),8,agents=agents)
                system=planning_prompt([{'id':key,**value} for key,value in agents.items()],8)
                for role,agent in agents.items():
                    self.assertIn(agent['rolePrompt'],system)
                    definitions=ToolGateway(Config(Path(self.temp.name)/('schema-'+team),{'LLM_API_KEY':'fake'}),agents=agents).definitions(role)
                    prompt=action_prompt(agent,definitions)
                    self.assertIn(agent['rolePrompt'],prompt)
                    self.assertIn('expectedSha256',prompt)
                    self.assertIn('Python 3.11',prompt)
                    if 'code.test' in agent['tools']:
                        self.assertIn('"timeout"',prompt)
                        self.assertIn('"args"',prompt)
        self.assertEqual(set(AGENTS),{'coordinator','research','executor','reviewer'})

    ids=iter(range(1000))

    def test_two_group_tasks_keep_human_replies_context_artifacts_and_full_transcript(self):
        app,model=self.app(clarify=True)
        self.send(app,'Create an initial collaborative draft.')
        deadline=time.monotonic()+3
        approval=None
        while time.monotonic()<deadline:
            run=app.snapshot()['sessions']['group']['run']
            approval=next((item for item in run['approvals'] if item['status']=='pending'),None)
            if approval:break
            time.sleep(.01)
        self.assertIsNotNone(approval)
        self.assertEqual(approval['type'],'clarification')
        app.action({'action':'approval','channelId':'group','approvalId':approval['id'],'choiceId':'answer','answer':'Actual human context: faculty audience.'})
        first=self.wait(app)
        first_note=next(item for item in first['artifacts'] if item['name']=='note.md')
        self.send(app,'Continue the existing draft with the earlier material.')
        second=self.wait(app)
        self.assertEqual(second['previousRunId'],first['id'])
        self.assertEqual(second['teamId'],'newsroom')
        self.assertEqual(second['channelId'],'group')
        self.assertTrue(any('Actual human context' in item['text'] for item in second['groupHistory']))
        inherited=next(item for item in second['artifacts'] if item.get('sourceArtifactId')==first_note['id'])
        self.assertNotEqual(inherited['id'],first_note['id'])
        self.assertTrue(inherited['inheritedReadOnly'])
        continued=next(item for item in second['artifacts'] if item['name']=='continued.md')
        self.assertIn('continuity-token-9371',continued['content'])
        self.assertEqual(app.artifacts.get(first_note['id'])['content'],first_note['content'])
        self.assertTrue(second['previousArtifacts'])
        second_context=next(item for item in model.contexts if item.get('task',{}).get('title')=='2:reporter')
        self.assertTrue(any(item['id']==inherited['id'] and item['path'].startswith('imports/') for item in second_context['authorizedArtifacts']))
        self.assertFalse(any(item['id']==inherited['id'] for item in second_context['ownArtifacts']))
        self.assertTrue(any(item.get('kind')=='model_response' for item in second['transcript']))
        self.assertTrue(any(item.get('kind')=='action_result' for item in second['transcript']))
        self.assertTrue(first['messages'])
        self.assertTrue(any(item.get('kind')=='agent_message' for item in first['transcript']))
        self.assertTrue(any(call.get('reportedModel')=='fixture-reported-model' for call in second['calls']))
        self.assertEqual(set(item['agentId'] for item in second['steps'] if item.get('kind')!='planning'),set(app.agents))
        self.assertTrue(any('Actual human context' in item['text'] for item in app.snapshot()['transcript']))

    def test_carry_failure_restores_previous_session_instead_of_phantom_running_run(self):
        app,_=self.app()
        self.send(app,'Initial draft.');first=self.wait(app)
        before=copy.deepcopy(app.state['sessions']['group'])
        with patch.object(app.artifacts,'carry_forward',side_effect=ValueError('snapshot hash mismatch')):
            with self.assertRaises(ActionError):self.send(app,'Extend draft.')
        self.assertEqual(app.state['sessions']['group'],before)
        self.assertEqual(app.state['sessions']['group']['run']['id'],first['id'])

    def test_text_review_is_recorded_without_fabricating_code_verification(self):
        for team,review_role in [('newsroom','fact_checker'),('fund','risk')]:
            app,_=self.app(team)
            target=app._new_task('Text proposal','Assess evidence','human',list(app.agents),next(key for key in app.agents if key!=review_role))
            target.update(status='done',result='Text delivered')
            reviewer=app._new_task('Assess','Assess','human',list(app.agents),review_role)
            run={'id':'run-'+team,'status':'running','steps':[target,reviewer],'calls':[],'events':[],
                 'artifacts':[],'approvals':[],'memoryHits':[],'messages':[],'participants':list(app.agents)}
            app.state['sessions']['group']['run']=run
            result=app._review(run,reviewer,{'taskId':target['id'],'verdict':'pass','feedback':'The citations and stated uncertainty were checked.'})
            self.assertTrue(result['ok'])
            self.assertTrue(result['validation']['assessed'])
            self.assertFalse(result['validation']['verified'])
            self.assertEqual(result['validation']['kind'],'editorial_assessment')
            self.assertFalse(result['validation']['evidenceCallIds'])

    def test_registry_is_not_shared_and_session_history_is_not_truncated(self):
        first,_=self.app('software');second,_=self.app('fund')
        first.agents['lead']['name']='Changed only this instance'
        self.assertEqual(get_team_agents('software')['lead']['name'],'Technical Lead')
        self.assertNotIn('lead',second.agents)
        self.assertNotIn('lead',second.snapshot()['sessions'])
        session=first.state['sessions']['group']
        for index in range(205):first._message(session,'human',str(index))
        self.assertEqual(len(first.snapshot()['transcript']),205)
        self.assertEqual(first.snapshot()['transcript'][0]['text'],'0')

    def test_qa_review_is_capability_bound_and_rejects_a_developer_test_receipt(self):
        app,_=self.app('software')
        target=app._new_task('Implementation','Check delivered code','lead',list(app.agents),'dev')
        target.update(status='done',result='provisional delivery',outputs=['code-artifact'])
        qa=app._new_task('Independent check','Check','lead',list(app.agents),'qa')
        run={'id':'run-qa-evidence','status':'running','steps':[target,qa],'calls':[],'events':[],
             'artifacts':[{'id':'code-artifact','name':'solution.py','sha256':'a'*64,'ownerAgentId':'dev'}],
             'approvals':[],'memoryHits':[],'messages':[],'participants':list(app.agents)}
        app.state['sessions']['group']['run']=run
        call={'id':'actual-test','taskId':qa['id'],'agentId':'dev','tool':'code.test','status':'success',
              'result':{'ok':True,'exitCode':0},'reviewTargets':{target['id']:app._review_snapshot(run,target)}}
        run['calls'].append(call)
        action={'taskId':target['id'],'verdict':'pass','feedback':'Independently checked the actual output.',
                'evidenceCallIds':[call['id']]}
        self.assertFalse(app._review(run,qa,action)['ok'])
        call['agentId']='qa'
        accepted=app._review(run,qa,action)
        self.assertTrue(accepted['ok'])
        self.assertTrue(accepted['validation']['verified'])
        self.assertEqual(run['events'][-1]['agentId'],'qa')
        parent=app._new_task('Delegate','Delegate','human',list(app.agents),'lead')
        parent['status']='running';run['steps'].append(parent)
        run['budget']={'tasks':12,'depth':3}
        delegated=app._delegate(run,parent,{'title':'Inspect the delivery','agentId':'qa'})
        child=app._task(run,delegated['id'])
        self.assertEqual(child['candidateAgentIds'],['qa'])
        self.assertIn('review',child['requiredCapabilities'])

    def test_memory_feedback_waits_for_review_and_counts_actual_failure(self):
        app,_=self.app('software')
        candidate=app.memory.propose('dev','Use the checked technique.','code validation','source-task','source-run',['source-call'])
        app.memory.confirm(candidate['id'],'dev',True,{'type':'human','id':'fixture-confirmation'})
        task=app._new_task('Deliver code','Validate code','lead',list(app.agents),'dev')
        task.update(status='done',result='Self-reported done',outputs=['source'])
        qa=app._new_task('Check','Check','lead',list(app.agents),'qa')
        run={'id':'run-final-feedback','status':'running','steps':[task,qa],
             'artifacts':[{'id':'source','name':'source.py','sha256':'a'*64}],
             'events':[],'approvals':[],'messages':[],'participants':list(app.agents),
             'memoryHits':[{'memoryId':candidate['id'],'taskId':task['id'],'agentId':'dev','ownerAgentId':'dev'}],
             'calls':[{'id':'self-test','taskId':task['id'],'agentId':'dev','tool':'code.test','status':'success','result':{'ok':True,'exitCode':0}}]}
        app.state['sessions']['group']['run']=run
        app._record_memory(run,task,'group',None)
        self.assertEqual(app.repo.get_memory(candidate['id'],'dev')['uses'],0,'provisional done is not positive feedback')
        app._review(run,qa,{'taskId':task['id'],'verdict':'reject','feedback':'A real counterexample disproved the output.'})
        app._feedback_memories(run,task)
        memory=app.repo.get_memory(candidate['id'],'dev')
        self.assertEqual((memory['success_count'],memory['failure_count']),(0,1))
        app._feedback_memories(run,task)
        self.assertEqual(app.repo.get_memory(candidate['id'],'dev')['failure_count'],1)
        another=app._new_task('Single-role test failure','Validate code','human',['dev'],'dev')
        another.update(status='failed',result='Test failed')
        run['participants']=['dev'];run['steps']=[another]
        run['memoryHits']=[{'memoryId':candidate['id'],'taskId':another['id'],'agentId':'dev','ownerAgentId':'dev'}]
        run['calls']=[{'id':'failed-check','taskId':another['id'],'agentId':'dev','tool':'code.test','status':'failed','result':{'ok':False,'exitCode':1}}]
        app._feedback_memories(run,another)
        self.assertEqual(app.repo.get_memory(candidate['id'],'dev')['failure_count'],2,'an all-failing test still supplies negative evidence')


if __name__=='__main__':unittest.main()
