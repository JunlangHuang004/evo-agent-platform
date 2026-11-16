"""Opt-in JSON request formatting; all upstream responses are local fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from backend.config import Config
from backend.provider import ModelClient, ProviderError


class Response:
    status = 200

    def __init__(self, content='{"kind":"fixture"}'):
        self.raw = json.dumps({
            'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 7, 'total_tokens': 19},
            'model': 'fixture-reported-model',
        }).encode()
        self.headers = {'Content-Length': str(len(self.raw))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, limit):
        block, self.raw = self.raw[:limit], self.raw[limit:]
        return block


class JsonModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.messages = [{'role': 'user', 'content': 'Synthetic formatting test only.'}]

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **values):
        return Config(Path(self.temp.name), {'LLM_API_KEY': 'fixture-json-secret', **values})

    def request(self, config, method='complete_json', content='{"kind":"fixture"}'):
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.return_value = Response(content)
            result = getattr(ModelClient(config), method)(self.messages, max_tokens=128)
            self.assertEqual(build.return_value.open.call_count, 1)
            request = build.return_value.open.call_args.args[0]
        return json.loads(request.data), result

    def test_default_configuration_does_not_request_json_on_either_entry_point(self):
        config = self.config()
        self.assertEqual(config.json_mode, 'default')
        chat_body, chat_result = self.request(config, 'complete')
        protocol_body, protocol_result = self.request(config)
        self.assertEqual(chat_body, protocol_body)
        self.assertEqual(set(chat_body), {'model', 'messages', 'temperature', 'max_tokens', 'stream'})
        for result in (chat_result, protocol_result):
            self.assertIsNone(result['transport']['requestedResponseFormat'])

    def test_hkgai_json_object_applies_only_to_explicit_json_entry_point(self):
        config = self.config(LLM_JSON_MODE='json_object')
        chat_body, chat_result = self.request(config, 'complete')
        protocol_body, protocol_result = self.request(config)
        self.assertNotIn('response_format', chat_body)
        self.assertIsNone(chat_result['transport']['requestedResponseFormat'])
        self.assertEqual(protocol_body.pop('response_format'), {'type': 'json_object'})
        self.assertEqual(protocol_body, chat_body, 'no other request parameter changes')
        self.assertEqual(protocol_body['max_tokens'], 128)
        self.assertEqual(protocol_result['transport']['requestedResponseFormat'], {'type': 'json_object'})
        self.assertEqual(protocol_result['transport']['requestBytes'], len(json.dumps(
            {**protocol_body, 'response_format': {'type': 'json_object'}}, ensure_ascii=False).encode()))

    def test_json_mode_composes_with_verified_thinking_and_compatible_transport(self):
        body, result = self.request(self.config(LLM_JSON_MODE='json_object',
            LLM_THINKING_MODE='disabled', LLM_TRANSPORT_MODE='compatible'))
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(body['chat_template_kwargs'], {'enable_thinking': False})
        self.assertEqual(result['transport']['transportMode'], 'compatible')
        self.assertEqual(result['transport']['requestedThinkingMode'], 'disabled')
        self.assertTrue(result['transport']['wireComplete'])
        self.assertTrue(result['transport']['bodyComplete'])

    def test_configuration_rejects_bad_values_and_unverified_provider(self):
        for value in ('on', 'json_schema', '', None, 42, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config(LLM_JSON_MODE=value)
        with self.assertRaises(ValueError):
            self.config(LLM_PROVIDER='deepseek', LLM_JSON_MODE='json_object')
        config = self.config(LLM_JSON_MODE=' JSON_OBJECT ')
        self.assertEqual(config.json_mode, 'json_object')
        body, result = self.request(self.config(LLM_PROVIDER='deepseek'))
        self.assertNotIn('response_format', body)
        self.assertIsNone(result['transport']['requestedResponseFormat'])

    def test_runtime_override_cannot_send_invalid_or_unverified_json_mode(self):
        for invalid_mode, provider_name in [('json_schema', 'hkgai'), ('json_object', 'deepseek')]:
            with self.subTest(mode=invalid_mode, provider=provider_name):
                config = self.config(LLM_PROVIDER=provider_name)
                config.json_mode = invalid_mode
                with patch('backend.provider.urllib.request.build_opener') as build:
                    with self.assertRaises(ProviderError) as caught:
                        ModelClient(config).complete_json(self.messages)
                    build.assert_not_called()
                self.assertEqual(caught.exception.code, 'parameters')
                self.assertEqual(caught.exception.phase, 'prepare_request')
                self.assertFalse(caught.exception.transport['headersReceived'])

    def test_chat_does_not_read_or_send_json_runtime_override(self):
        config = self.config(LLM_PROVIDER='deepseek')
        config.json_mode = 'json_object'
        body, result = self.request(config, 'complete', content='Ordinary text reply.')
        self.assertNotIn('response_format', body)
        self.assertIsNone(result['transport']['requestedResponseFormat'])
        self.assertEqual(result['content'], 'Ordinary text reply.')

    def test_http_rejection_has_no_fallback_or_retry_and_keeps_requested_evidence(self):
        config = self.config(LLM_JSON_MODE='json_object')
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = urllib.error.HTTPError(
                config.base_url + '/chat/completions', 400, 'fixture rejected', None, None)
            with self.assertRaises(ProviderError) as caught:
                ModelClient(config).complete_json(self.messages)
            self.assertEqual(build.return_value.open.call_count, 1)
            body = json.loads(build.return_value.open.call_args.args[0].data)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(caught.exception.code, 'provider_http')
        self.assertEqual(caught.exception.transport['httpStatus'], 400)
        self.assertEqual(caught.exception.transport['requestedResponseFormat'], {'type': 'json_object'})
        self.assertNotIn(config.api_key, str(caught.exception) + json.dumps(caught.exception.transport))

    def test_requested_format_is_not_reported_as_verified_output_or_silent_repair(self):
        body, result = self.request(self.config(LLM_JSON_MODE='json_object'), content='not valid JSON')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(result['content'], 'not valid JSON', 'the protocol caller must validate actual content')
        self.assertEqual(result['transport']['requestedResponseFormat'], {'type': 'json_object'})
        self.assertNotIn('verified', result)
        self.assertNotIn('validJson', result)


if __name__ == '__main__':
    unittest.main()
