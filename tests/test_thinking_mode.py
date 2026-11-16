"""Explicit provider-specific thinking requests; no model or network calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform
from backend.provider import ModelClient, ProviderError


class Response:
    status=200
    def __init__(self):
        self.raw=json.dumps({'model':'fixture-reported-model','choices':[{'finish_reason':'stop','message':{'content':'Fixture reply.'}}],
                            'usage':{'prompt_tokens':99,'completion_tokens':123,'total_tokens':222,
                                     'completion_tokens_details':{'reasoning_tokens':17}}}).encode()
        self.headers={'Content-Length':str(len(self.raw))}
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read1(self,limit):
        block=self.raw[:limit];self.raw=self.raw[limit:];return block


class NoTools:
    def capabilities(self):return {}
    def definitions(self,agent):return []
    def cancel(self,run):return None


class ThinkingModeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=None
    def tearDown(self):
        if self.app:
            self.app.shutdown();self.app.wait_for_idle(2);self.app.repo.close()
        self.temp.cleanup()
    def config(self,**values):
        return Config(Path(self.temp.name),{'LLM_API_KEY':'fixture-thinking-secret','MAX_RETRIES':'0',**values})
    def complete(self,config):
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.return_value=Response()
            result=ModelClient(config).complete([{'role':'user','content':'fixture'}],max_tokens=128)
            self.assertEqual(build.return_value.open.call_count,1)
            request=build.return_value.open.call_args.args[0]
        return json.loads(request.data),result

    def test_default_keeps_wire_request_unchanged_and_existing_output_budgets(self):
        config=self.config()
        self.assertEqual(config.thinking_mode,'default')
        self.assertEqual((config.planning_max_tokens,config.max_completion_tokens),(3000,6000))
        body,result=self.complete(config)
        self.assertEqual(set(body),{'model','messages','temperature','max_tokens','stream'})
        self.assertNotIn('chat_template_kwargs',body)
        self.assertEqual(result['transport']['requestedThinkingMode'],'default')

    def test_hkgai_disabled_sends_only_the_verified_template_option(self):
        body,result=self.complete(self.config(LLM_THINKING_MODE='disabled'))
        self.assertEqual(body['chat_template_kwargs'],{'enable_thinking':False})
        self.assertIs(body['chat_template_kwargs']['enable_thinking'],False)
        self.assertNotIn('thinking_token_budget',body)
        self.assertEqual(body['max_tokens'],128)
        self.assertEqual(result['transport']['requestedThinkingMode'],'disabled')
        self.assertEqual(result['usage']['completion_tokens_details']['reasoning_tokens'],17,
                         'the request setting does not override actual provider-reported reasoning usage')

    def test_invalid_or_unverified_provider_configuration_is_rejected(self):
        for value in ('on','false','',None,42):
            with self.subTest(value=value),self.assertRaises(ValueError):self.config(LLM_THINKING_MODE=value)
        with self.assertRaises(ValueError):self.config(LLM_PROVIDER='deepseek',LLM_THINKING_MODE='disabled')
        body,result=self.complete(self.config(LLM_PROVIDER='deepseek'))
        self.assertNotIn('chat_template_kwargs',body)
        self.assertEqual(result['transport']['requestedThinkingMode'],'default')

    def test_runtime_override_cannot_send_unverified_option_or_silently_fallback(self):
        config=self.config(LLM_PROVIDER='deepseek')
        config.thinking_mode='disabled'
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.side_effect=AssertionError('unverified mode must not open a connection')
            with self.assertRaises(ProviderError) as caught:ModelClient(config).complete([{'role':'user','content':'fixture'}])
            build.assert_not_called()
        self.assertEqual(caught.exception.code,'parameters')
        self.assertEqual(caught.exception.phase,'prepare_request')
        self.assertEqual(caught.exception.transport['requestedThinkingMode'],'disabled')

    def test_request_mode_and_actual_reasoning_are_persisted_as_separate_evidence(self):
        config=self.config(LLM_THINKING_MODE='disabled')
        self.app=Platform(config,provider=ModelClient(config),tools=NoTools())
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.return_value=Response()
            self.app.action({'action':'send','channelId':'coordinator','mode':'chat','text':'fixture'})
            self.assertTrue(self.app.wait_for_idle(2))
        run=self.app.snapshot()['sessions']['coordinator']['run']
        self.assertEqual(run['status'],'done')
        call=run['calls'][0]
        self.assertEqual(call['transport']['requestedThinkingMode'],'disabled')
        self.assertEqual(call['usage']['completion_tokens_details']['reasoning_tokens'],17)
        self.assertEqual(self.app.repo.get_run(run['id'])['calls'][0]['transport'],call['transport'])
        self.assertNotIn(config.api_key,json.dumps(run))


if __name__=='__main__':unittest.main()
