"""Contract guardrails in the actual scheduler; scripted model, real local files.

No model, network, Git or Docker execution is used. A format/evidence pass never
claims semantic task success.
"""
import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.runtime import ProtocolError, compress_messages, normalize_plan
from backend.tools import ToolGateway


class LocalFiles(ToolGateway):
    def capabilities(self):
        return {'dockerAvailable':False,'dockerImageReady':False,'hostFallback':False}


class ContractModel:
    def __init__(self, contract, actions):
        self.contract=contract
        self.actions=actions
        self.calls=0
        self.action_calls=0
        self.results=[]
        self.contexts=[]

    def complete(self,messages,max_tokens=None):
        self.calls+=1
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            task={'id':'delivery','title':'Deliver requested output','agentId':'coordinator',
                  'acceptanceCriteria':['Publish the requested form and disclose its limits.']}
            if self.contract is not None:task['outputContract']=copy.deepcopy(self.contract)
            content={'tasks':[task]}
        else:
            self.contexts.append(json.loads(messages[1]['content']))
            self.results=[json.loads(item['content'][len('动作的真实结果：'):]) for item in messages
                          if item.get('content','').startswith('动作的真实结果：')]
            content=self.actions[min(self.action_calls,len(self.actions)-1)]
            self.action_calls+=1
        return {'content':json.dumps(content),'finish_reason':'stop','usage':{'total_tokens':12}}


def final(text='Claimed completion'):
    return {'type':'final','status':'done','text':text}


class OutputContractRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.apps=[]

    def tearDown(self):
        for app in self.apps:
            app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.temp.cleanup()

    def run_model(self,contract,actions,**settings):
        config=Config(Path(self.temp.name)/str(len(self.apps)),
                      {'LLM_API_KEY':'fake-contract-runtime-key','MAX_RETRIES':'0','MAX_AGENT_TURNS':'8',
                       'MAX_MODEL_CALLS':'20','MAX_RUN_TOKENS':'200000',**settings})
        model=ContractModel(contract,actions)
        app=Platform(config,provider=model,tools=LocalFiles(config))
        self.apps.append(app)
        app.action({'action':'send','channelId':'group','mode':'task',
                    'participants':['coordinator'],'text':'Deliver an output matching the declared format.'})
        self.assertTrue(app.wait_for_idle(4),'guardrail repairs must terminate')
        run=app.snapshot()['sessions']['group']['run']
        task=next(item for item in run['steps'] if item.get('kind')!='planning')
        return app,model,run,task

    def test_missing_artifact_rejects_done_twice_then_preserves_partial(self):
        app,model,run,task=self.run_model({'kind':'artifact','extensions':['.html']},[final('The HTML file is ready.')])
        self.assertEqual(run['status'],'partial')
        self.assertEqual(task['status'],'partial')
        self.assertEqual(model.action_calls,3,'two repairs after the initial invalid final, then stop')
        self.assertFalse(run['artifacts'])
        self.assertEqual(task['outputValidation']['status'],'failed')
        self.assertEqual(task['outputValidation']['semanticQuality'],'unassessed')
        self.assertIn('交付契约未满足',task['result'])
        failures=[event for event in run['events'] if event['type']=='output_validation_failed']
        self.assertEqual(len(failures),3)
        self.assertEqual([item['repairAttempt'] for item in model.results if item.get('error',{}).get('code')=='output_contract'],[1,2])
        self.assertNotIn('检测到重复动作循环',task.get('result',''))
        self.assertEqual(app.repo.get_run(run['id'])['status'],'partial')

    def test_failed_final_can_repair_by_publishing_a_real_new_artifact(self):
        actions=[final('Only a prose claim so far.'),
                 {'type':'tool','name':'file.write','args':{'path':'delivery.html','content':'<!doctype html><html><body>Actual immutable delivery.</body></html>'}},
                 {'type':'publish_artifact','path':'delivery.html'},final('The actual HTML file is published.')]
        app,model,run,task=self.run_model({'kind':'artifact','extensions':['.html'],'minCount':1},actions)
        self.assertEqual(run['status'],'done',run.get('error'))
        artifact=next(item for item in run['artifacts'] if item['name']=='delivery.html')
        self.assertEqual(task['outputs'],[artifact['id']])
        self.assertEqual(task['outputValidation']['status'],'passed')
        self.assertEqual(task['outputValidation']['matchedArtifactIds'],[artifact['id']])
        self.assertEqual(task['outputValidation']['semanticQuality'],'unassessed')
        self.assertEqual(app.artifacts.get(artifact['id'])['content'],artifact['content'])
        self.assertTrue(any(event['type']=='output_validation_failed' for event in run['events']))
        self.assertTrue(any(event['type']=='output_validation_passed' for event in run['events']))
        self.assertEqual(model.contexts[0]['task']['outputContract'],{'kind':'artifact','extensions':['.html'],'minCount':1})
        self.assertTrue(all(call['tool'] not in ('code.run','code.test') for call in run['calls']))

    def test_json_contract_checks_delivered_text_and_legacy_text_stays_compatible(self):
        _,model,run,task=self.run_model({'kind':'json','requiredKeys':['result']},
                                     [final('I wrote JSON elsewhere.'),final('{"result":"actual returned value"}')])
        self.assertEqual(run['status'],'done')
        self.assertEqual(model.action_calls,2)
        self.assertEqual(json.loads(task['result']),{'result':'actual returned value'})
        self.assertEqual(task['outputValidation']['status'],'passed')
        _,legacy,run,task=self.run_model(None,[final('A legitimate plain-text result.')])
        self.assertEqual(run['status'],'done')
        self.assertEqual(legacy.action_calls,1)
        self.assertEqual(task['outputValidation']['status'],'not_configured')

    def test_overlong_json_is_not_validated_then_silently_cut(self):
        text=json.dumps({'result':'x'*19000})
        _,model,run,task=self.run_model({'kind':'json','requiredKeys':['result']},[final(text)])
        self.assertEqual(run['status'],'partial')
        self.assertEqual(model.action_calls,3)
        self.assertEqual(task['outputValidation']['status'],'failed')
        self.assertTrue(task['outputValidation']['issues'])

    def test_turn_budget_remains_a_hard_limit_on_contract_repairs(self):
        _,model,run,task=self.run_model({'kind':'artifact','extensions':['.html']},[final()],MAX_AGENT_TURNS='2')
        self.assertEqual(model.action_calls,2)
        self.assertEqual(task['status'],'failed')
        self.assertEqual(run['status'],'failed')
        self.assertEqual(task['error']['code'],'budget')

    def test_contracts_are_normalized_for_plans_and_delegation_and_retained_in_context(self):
        count=itertools.count()
        tasks=normalize_plan({'tasks':[{'id':'one','title':'Output','agentId':'coordinator',
                                      'outputContract':{'kind':'artifact','extensions':['.HTML']}}]},
                             ['coordinator'],lambda prefix:prefix+str(next(count)),4)
        self.assertEqual(tasks[0]['outputContract'],{'kind':'artifact','extensions':['.html'],'minCount':1})
        with self.assertRaises(ProtocolError):
            normalize_plan({'tasks':[{'id':'one','title':'Output','outputContract':{'kind':'artifact','minCount':0}}]},
                           ['coordinator'],lambda prefix:prefix+str(next(count)),4)
        app,_,run,_=self.run_model(None,[final()])
        parent=app._new_task('Delegate','Delegate','human',['coordinator','research'],'coordinator')
        parent['status']='running'
        run['participants']=['coordinator','research'];run['steps'].append(parent)
        delegated=app._delegate(run,parent,{'title':'Write source note','agentId':'research','outputContract':{'kind':'artifact','extensions':['.md']}})
        child=app._task(run,delegated['id'])
        self.assertEqual(child['outputContract'],{'kind':'artifact','extensions':['.md'],'minCount':1})
        before=len(run['steps'])
        with self.assertRaises(ProtocolError):
            app._delegate(run,parent,{'title':'Invalid output','agentId':'research','outputContract':{'kind':'shell'}})
        self.assertEqual(len(run['steps']),before)
        contract={'kind':'json','requiredKeys':['result','sources','limitations','evidence','status']}
        context={'task':{'id':'task-c','goal':'Bounded delivery','outputContract':contract},'ownArtifacts':[{'id':'artifact-a','content':'x'*40000}]}
        compressed=compress_messages([{'role':'system','content':'Protocol.'},{'role':'user','content':json.dumps(context)}],4000)
        self.assertLessEqual(sum(len(item['content']) for item in compressed),4000)
        self.assertEqual(json.loads(compressed[1]['content'])['task']['outputContract'],contract)

    def test_rework_discards_old_format_validation_and_resets_repair_allowance(self):
        app,_,run,target=self.run_model(None,[final('Text delivery.')])
        app.config.max_retries=1
        target.update(outputValidation={'status':'passed','kind':'artifact'},_outputContractRepairs=2)
        reviewer=app._new_task('Review','Review','human',['coordinator','reviewer'],'reviewer')
        run['participants']=['coordinator','reviewer'];run['steps'].append(reviewer)
        result=app._review(run,reviewer,{'taskId':target['id'],'verdict':'rework','feedback':'Revise the actual delivery.'})
        self.assertTrue(result['ok'])
        self.assertEqual(target['revision'],1)
        self.assertNotIn('outputValidation',target)
        self.assertNotIn('_outputContractRepairs',target)


if __name__=='__main__':unittest.main()
