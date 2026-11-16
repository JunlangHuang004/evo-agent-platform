"""Task-only structured generation routing; scripted providers, no HTTP/model."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.provider import ProviderError


class NoTools:
    def capabilities(self): return {}
    def definitions(self,agent): return []
    def cancel(self,run_id): return None


def task_response(messages,contract=None):
    if messages[0]['content'].startswith('Plan an executable task DAG'):
        task={'id':'one','title':'Produce the requested result','agentId':'coordinator'}
        if contract is not None:task['outputContract']=contract
        action={'tasks':[task]}
    else:
        action={'type':'final','text':'A syntactically valid final response.','status':'done'}
    return {'content':json.dumps(action),'finish_reason':'stop','usage':{'total_tokens':7}}


class DualProvider:
    def __init__(self,contract=None):
        self.calls=[]
        self.contract=contract

    def complete(self,messages,max_tokens=None):
        self.calls.append(('complete',copy.deepcopy(messages),max_tokens))
        return {'content':'Ordinary conversation response.','finish_reason':'stop','usage':{'total_tokens':5}}

    def complete_json(self,messages,max_tokens=None):
        self.calls.append(('complete_json',copy.deepcopy(messages),max_tokens))
        return task_response(messages,self.contract)


class JsonRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=None

    def tearDown(self):
        if self.app:
            self.app.shutdown();self.app.wait_for_idle(3);self.app.repo.close()
        self.temp.cleanup()

    def run_provider(self,provider,chat=False):
        config=Config(Path(self.temp.name),{'LLM_API_KEY':'offline-routing-fixture','MAX_RETRIES':'2',
                     'MODEL_MAX_TOKENS':'3000','PLANNING_MAX_TOKENS':'1200','MAX_MODEL_CALLS':'20',
                     'MAX_RUN_TOKENS':'200000','MAX_AGENT_TURNS':'8'})
        self.app=Platform(config,provider=provider,tools=NoTools())
        channel='coordinator' if chat else 'group'
        self.app.action({'action':'send','channelId':channel,'mode':'chat' if chat else 'task',
                         'participants':['coordinator'],'text':'Give the requested result.'})
        self.assertTrue(self.app.wait_for_idle(4))
        return self.app.snapshot()['sessions'][channel]['run']

    def test_planning_and_agent_actions_use_available_json_entrypoint(self):
        provider=DualProvider();run=self.run_provider(provider)
        self.assertEqual(run['status'],'done')
        self.assertEqual([name for name,_,_ in provider.calls],['complete_json','complete_json'])
        self.assertTrue(provider.calls[0][1][0]['content'].startswith('Plan an executable task DAG'))
        self.assertFalse(provider.calls[1][1][0]['content'].startswith('Plan an executable task DAG'))
        self.assertEqual([budget for _,_,budget in provider.calls],[1200,3000])
        self.assertEqual(run['modelCalls'],2)
        self.assertEqual(run['tokenUsage'],14)

    def test_ordinary_chat_uses_only_complete(self):
        provider=DualProvider();run=self.run_provider(provider,chat=True)
        self.assertEqual(run['status'],'done')
        self.assertEqual([name for name,_,_ in provider.calls],['complete'])
        self.assertEqual(run['steps'][0]['result'],'Ordinary conversation response.')
        self.assertEqual(run['modelCalls'],1)

    def test_legacy_complete_only_provider_without_budget_keyword_stays_compatible(self):
        class Legacy:
            def __init__(self):self.calls=0
            def complete(self,messages):
                self.calls+=1
                return task_response(messages)
        provider=Legacy();run=self.run_provider(provider)
        self.assertEqual(run['status'],'done')
        self.assertEqual(provider.calls,2)

    def test_internal_typeerror_from_json_entrypoint_never_replays_with_complete(self):
        class Broken(DualProvider):
            def complete_json(self,messages,max_tokens=None):
                self.calls.append(('complete_json',copy.deepcopy(messages),max_tokens))
                raise TypeError('Actual JSON provider implementation error.')
        provider=Broken();run=self.run_provider(provider)
        self.assertEqual(run['status'],'failed')
        self.assertEqual([name for name,_,_ in provider.calls],['complete_json'])
        self.assertEqual(run['modelCalls'],1)

    def test_json_mode_rejection_never_falls_back_or_retries_nonretryable_error(self):
        class Unsupported(DualProvider):
            def complete_json(self,messages,max_tokens=None):
                self.calls.append(('complete_json',copy.deepcopy(messages),max_tokens))
                raise ProviderError('Endpoint rejected the requested response format.','parameters',False)
        provider=Unsupported();run=self.run_provider(provider)
        self.assertEqual(run['status'],'failed')
        self.assertEqual([name for name,_,_ in provider.calls],['complete_json'])
        self.assertEqual(run['error']['code'],'parameters')
        self.assertEqual(run['modelCalls'],1)

    def test_valid_json_syntax_does_not_satisfy_missing_artifact_contract(self):
        provider=DualProvider(contract={'kind':'artifact','extensions':['.md'],'minCount':1})
        run=self.run_provider(provider)
        task=next(t for t in run['steps'] if t.get('kind')!='planning')
        self.assertEqual(run['status'],'partial')
        self.assertEqual(task['status'],'partial')
        self.assertEqual(task['outputValidation']['status'],'failed')
        self.assertEqual(task['outputValidation']['semanticQuality'],'unassessed')
        self.assertFalse(task['outputs'])
        self.assertEqual([name for name,_,_ in provider.calls],['complete_json']*4)
        self.assertEqual(len([e for e in run['events'] if e['type']=='output_validation_failed']),3)


if __name__=='__main__':unittest.main()
