"""Planning output limits and transport diagnostics; no external requests."""
from pathlib import Path
import json
import socket
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform
from backend.provider import ModelClient, ProviderError


class NoTools:
    def capabilities(self):return {}
    def definitions(self,agent):return []
    def cancel(self,run_id):return None


class BudgetModel:
    def __init__(self):self.budgets=[]
    def complete(self,messages,max_tokens=None):
        self.budgets.append(max_tokens)
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            content=json.dumps({'tasks':[{'id':'one','title':'Assess request','agentId':'coordinator'}]})
        else:
            content=json.dumps({'type':'final','text':'Recorded result','status':'done'})
        return {'content':content,'finish_reason':'stop','usage':{'total_tokens':20},'model':'reported-e10'}


class PlanningBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=Config(Path(self.temp.name),{'LLM_API_KEY':'fake-private-test-key','MODEL_MAX_TOKENS':'3000',
                           'PLANNING_MAX_TOKENS':'1200','MAX_RUN_TOKENS':'200000','MAX_RETRIES':'2'})
        self.app=None

    def tearDown(self):
        if self.app:
            self.app.shutdown();self.app.wait_for_idle(3);self.app.repo.close()
        self.temp.cleanup()

    def run_model(self,model):
        self.app=Platform(self.config,provider=model,tools=NoTools())
        self.app.action({'action':'send','channelId':'group','mode':'task','participants':['coordinator'],'text':'Inspect this general request.'})
        self.assertTrue(self.app.wait_for_idle(3))
        return self.app.snapshot()['sessions']['group']['run']

    def test_planning_cap_is_independent_bounded_and_recorded(self):
        model=BudgetModel();run=self.run_model(model)
        self.assertEqual(run['status'],'done')
        self.assertEqual(model.budgets,[1200,3000])
        self.assertEqual([call['requestedMaxTokens'] for call in run['calls']],[1200,3000])
        self.assertEqual([call['args']['max_tokens'] for call in run['calls']],[1200,3000])
        self.assertTrue(all(call['finishReason']=='stop' and call['reportedModel']=='reported-e10' for call in run['calls']))
        self.config.max_completion_tokens=500
        self.assertEqual(self.config.planning_max_tokens,500)
        self.config.max_completion_tokens=3000
        self.assertEqual(self.config.planning_max_tokens,1200,'later harness overrides do not freeze an old completion limit')
        self.config.planning_max_tokens=5000
        self.assertEqual(self.config.planning_max_tokens,3000)

    def test_legacy_mock_signature_is_used_once_without_budget_keyword(self):
        class Legacy:
            def __init__(self):self.calls=0
            def complete(self,messages):
                self.calls+=1
                content={'tasks':[{'id':'one','title':'Assess','agentId':'coordinator'}]} if self.calls==1 else {'type':'final','text':'done'}
                return {'content':json.dumps(content)}
        model=Legacy();run=self.run_model(model)
        self.assertEqual(run['status'],'done');self.assertEqual(model.calls,2)

    def test_internal_typeerror_is_never_retried_as_signature_fallback(self):
        class Broken:
            calls=0
            def complete(self,messages,max_tokens=None):
                self.calls+=1
                raise TypeError('provider implementation failed internally')
        model=Broken();run=self.run_model(model)
        self.assertEqual(run['status'],'failed')
        self.assertEqual(model.calls,1)

    def test_length_truncation_has_explicit_error_usage_and_no_invalid_json_retry(self):
        class Truncated:
            calls=0
            def complete(self,messages,max_tokens=None):
                self.calls+=1
                return {'content':'{"tasks":[','finish_reason':'length','model':'reported-e10',
                        'usage':{'total_tokens':1234,'completion_tokens':1200}}
        model=Truncated();run=self.run_model(model)
        self.assertEqual(run['status'],'failed')
        self.assertEqual(run['error']['code'],'output_truncated')
        self.assertIn('1200',run['error']['message'])
        self.assertEqual(model.calls,1)
        self.assertEqual(run['tokenUsage'],1234)
        self.assertEqual(run['calls'][0]['finishReason'],'length')
        self.assertEqual(run['calls'][0]['usage']['completion_tokens'],1200)

    def test_provider_returns_finish_reason_and_enforces_request_cap(self):
        captured=[]
        payload={'model':'reported-e10','choices':[{'finish_reason':'length','message':{'content':None}}],
                 'usage':{'total_tokens':42,'completion_tokens_details':{'reasoning_tokens':40}}}
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,limit):return json.dumps(payload).encode()
        class Opener:
            def open(self,request,timeout):
                captured.append(json.loads(request.data));return Response()
        with patch('backend.provider.urllib.request.build_opener',return_value=Opener()):
            response=ModelClient(self.config).complete([{'role':'user','content':'fixture'}],max_tokens=9000)
        self.assertEqual(captured[0]['max_tokens'],3000)
        self.assertEqual(response['finish_reason'],'length')
        self.assertEqual(response['content'],'')
        self.assertEqual(response['usage']['total_tokens'],42)

    def test_timeout_is_distinct_from_dns_network_failure(self):
        for fault,code in [(TimeoutError('read timed out'),'timeout'),
                           (urllib.error.URLError(socket.timeout('timed out')),'timeout'),
                           (urllib.error.URLError(socket.gaierror('name resolution failed')),'network')]:
            with self.subTest(code=code),patch('backend.provider.urllib.request.build_opener') as build:
                build.return_value.open.side_effect=fault
                with self.assertRaises(ProviderError) as caught:
                    ModelClient(self.config).complete([{'role':'user','content':'fixture'}])
                self.assertEqual(caught.exception.code,code)
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn(self.config.api_key,str(caught.exception))


if __name__=='__main__':unittest.main()
