"""Transport phase/evidence regressions; all upstream connections are test doubles."""
import http.client
import gzip
import json
from pathlib import Path
import tempfile
import threading
import time
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import Mock, patch

from backend.config import Config
from backend.engine import Platform
from backend.provider import ModelClient, ProviderError, NoRedirect, _CompatibleHTTPSConnection, _CompatibleHTTPSHandler, _MAX_RESPONSE_BYTES


PAYLOAD=json.dumps({'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}],
                    'usage':{'total_tokens':8},'model':'fixture-model'}).encode()


class Response:
    status=200
    def __init__(self,chunks,headers=None):
        self.chunks=list(chunks)
        self.headers={'Content-Length':str(len(PAYLOAD)),**(headers or {})}
        self.reads=0
        self.closed=False
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True;return False
    def read1(self,limit):
        self.reads+=1
        if not self.chunks:return b''
        item=self.chunks.pop(0)
        if isinstance(item,Exception):raise item
        return item


class NoTools:
    def capabilities(self):return {}
    def definitions(self,agent):return []
    def cancel(self,run):return None


class ProviderTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=Config(Path(self.temp.name),{'LLM_API_KEY':'fixture-provider-secret','MAX_RETRIES':'0'})
        self.messages=[{'role':'user','content':'检查 UTF-8 请求'}]
        self.app=None

    def tearDown(self):
        if self.app:
            self.app.shutdown();self.app.wait_for_idle(2);self.app.repo.close()
        self.temp.cleanup()

    def caught(self,response=None,error=None,client=None):
        client=client or ModelClient(self.config)
        with patch('backend.provider.urllib.request.build_opener') as build:
            if error is not None:build.return_value.open.side_effect=error
            else:build.return_value.open.return_value=response
            with self.assertRaises(ProviderError) as caught:client.complete(self.messages)
        return caught.exception

    def test_waiting_headers_timeout_is_distinct_and_contains_no_secret_or_body(self):
        error=self.caught(error=TimeoutError('read operation timed out '+self.config.api_key))
        self.assertEqual(error.code,'timeout')
        self.assertEqual(error.phase,'wait_headers')
        self.assertFalse(error.transport['headersReceived'])
        self.assertEqual(error.transport['receivedBytes'],0)
        self.assertFalse(error.transport['bodyComplete'])
        self.assertIn('requestBytes',error.transport)
        self.assertIn('slotWaitMs',error.transport)
        self.assertNotIn(self.config.api_key,str(error)+json.dumps(error.transport))

    def test_timeout_after_partial_body_keeps_observed_byte_count_and_never_parses_it(self):
        response=Response([PAYLOAD[:17],TimeoutError('read operation timed out')])
        with patch('backend.provider.json.loads',side_effect=AssertionError('partial data must not be decoded')):
            error=self.caught(response=response)
        self.assertEqual(error.code,'timeout')
        self.assertEqual(error.phase,'read_body')
        self.assertTrue(error.transport['headersReceived'])
        self.assertEqual(error.transport['receivedBytes'],17)
        self.assertEqual(error.transport['expectedBytes'],len(PAYLOAD))
        self.assertFalse(error.transport['bodyComplete'])
        self.assertTrue(response.closed)

    def test_incomplete_read_is_transport_failure_even_when_partial_bytes_form_valid_json(self):
        response=Response([http.client.IncompleteRead(PAYLOAD,25)],{'Content-Length':str(len(PAYLOAD)+25)})
        with patch('backend.provider.json.loads',side_effect=AssertionError('IncompleteRead must not be decoded')):
            error=self.caught(response=response)
        self.assertEqual(error.code,'transport_incomplete')
        self.assertEqual(error.phase,'read_body')
        self.assertEqual(error.transport['receivedBytes'],len(PAYLOAD))
        self.assertEqual(error.transport['exceptionType'],'IncompleteRead')
        self.assertFalse(error.transport['bodyComplete'])
        self.assertTrue(error.retryable)

    def test_early_eof_with_declared_length_is_not_a_complete_json_response(self):
        error=self.caught(response=Response([PAYLOAD,b''],{'Content-Length':str(len(PAYLOAD)+5)}))
        self.assertEqual(error.code,'transport_incomplete')
        self.assertEqual(error.transport['receivedBytes'],len(PAYLOAD))
        self.assertFalse(error.transport['bodyComplete'])

    def test_complete_http_body_with_invalid_json_is_decode_error_not_network_failure(self):
        raw=b'<html>gateway response</html>'
        error=self.caught(response=Response([raw],{'Content-Length':str(len(raw))}))
        self.assertEqual(error.phase,'decode_json')
        self.assertEqual(error.code,'response_json')
        self.assertTrue(error.transport['bodyComplete'])
        self.assertEqual(error.transport['receivedBytes'],len(raw))
        self.assertFalse(error.retryable)

    def test_http_protocol_exception_is_redacted_transport_failure(self):
        error=self.caught(error=http.client.BadStatusLine('bad '+self.config.api_key))
        self.assertEqual(error.phase,'wait_headers')
        self.assertEqual(error.code,'transport')
        self.assertTrue(error.retryable)
        self.assertEqual(error.transport['exceptionType'],'BadStatusLine')
        self.assertNotIn(self.config.api_key,str(error)+json.dumps(error.transport))

    def test_slot_wait_is_bounded_and_does_not_open_connection(self):
        self.config.model_timeout=.02
        client=ModelClient(self.config)
        client._slots=threading.BoundedSemaphore(0)
        started=time.monotonic()
        with patch('backend.provider.urllib.request.build_opener') as build:
            with self.assertRaises(ProviderError) as caught:client.complete(self.messages)
            build.assert_not_called()
        self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(caught.exception.code,'provider_busy')
        self.assertEqual(caught.exception.phase,'wait_slot')
        self.assertFalse(caught.exception.transport['headersReceived'])

    def test_failure_releases_slot_and_subsequent_chunked_read_succeeds(self):
        client=ModelClient(self.config);client._slots=threading.BoundedSemaphore(1)
        self.config.model_timeout=.05
        response=Response([PAYLOAD[:15],PAYLOAD[15:],b''])
        response.headers={'Transfer-Encoding':'chunked'}
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.side_effect=[http.client.RemoteDisconnected('fixture disconnect'),response]
            with self.assertRaises(ProviderError):client.complete(self.messages)
            result=client.complete(self.messages)
        self.assertEqual(result['content'],'{"ok":true}')
        self.assertEqual(response.reads,3)
        self.assertTrue(response.closed)

    def test_utf8_request_bytes_and_default_proxy_handlers_are_preserved(self):
        captured=[]
        response=Response([PAYLOAD])
        class Opener:
            def open(inner,request,timeout):
                captured.append(request);return response
        with patch('backend.provider.urllib.request.build_opener',return_value=Opener()) as build:
            ModelClient(self.config).complete(self.messages,max_tokens=128)
        request=captured[0]
        body=json.loads(request.data)
        self.assertEqual(body['messages'],self.messages)
        self.assertEqual(body['max_tokens'],128)
        self.assertFalse(body['stream'])
        self.assertNotIn('Content-encoding',request.headers)
        self.assertNotIn('Transfer-encoding',request.headers)
        self.assertNotIn('Proxy-authorization',request.headers)
        self.assertEqual(len(build.call_args.args),1,'NoRedirect stays the only custom handler; proxy policy is unchanged')

    def test_engine_persists_failure_phase_and_transport_in_public_history(self):
        self.app=Platform(self.config,provider=ModelClient(self.config),tools=NoTools())
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.side_effect=TimeoutError('fixture read timeout')
            self.app.action({'action':'send','channelId':'coordinator','mode':'chat','text':'fixture'})
            self.assertTrue(self.app.wait_for_idle(2))
        run=self.app.snapshot()['sessions']['coordinator']['run']
        error=run['calls'][0]['error']
        self.assertEqual(error['phase'],'wait_headers')
        self.assertFalse(error['transport']['headersReceived'])
        self.assertEqual(error['transport']['receivedBytes'],0)
        saved=self.app._public(self.app.repo.get_run(run['id']))
        self.assertEqual(saved['calls'][0]['error'],error)
        self.assertNotIn(self.config.api_key,json.dumps(saved))

    def test_transport_mode_is_explicit_validated_and_budget_defaults_are_updated(self):
        self.assertEqual(self.config.transport_mode,'standard')
        self.assertEqual(self.config.max_completion_tokens,6000)
        self.assertEqual(self.config.planning_max_tokens,3000)
        for mode in ('direct','gzip','',None,42):
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                Config(Path(self.temp.name)/'invalid-mode',{'LLM_TRANSPORT_MODE':mode})
        compatible=Config(Path(self.temp.name)/'compatible',{'LLM_TRANSPORT_MODE':'compatible'})
        self.assertEqual(compatible.transport_mode,'compatible')

    def test_compatible_mode_preserves_request_body_and_default_verified_tls_proxy_policy(self):
        requests=[];handlers=[]
        class Opener:
            def open(inner,request,timeout):
                requests.append(request);return Response([PAYLOAD])
        def build(*items):
            handlers.append(items);return Opener()
        with patch('backend.provider.urllib.request.build_opener',side_effect=build):
            standard=ModelClient(self.config).complete(self.messages,max_tokens=128)
            self.config.transport_mode='compatible'
            compatible=ModelClient(self.config).complete(self.messages,max_tokens=128)
        self.assertEqual(requests[0].data,requests[1].data,'compatibility does not compress or alter the prompt/model request')
        self.assertNotIn('Content-encoding',requests[1].headers)
        self.assertEqual(requests[1].get_header('Accept-encoding'),'gzip')
        self.assertEqual(len(handlers[0]),1)
        self.assertEqual(len(handlers[1]),2)
        self.assertIsInstance(handlers[1][0],NoRedirect)
        handler=handlers[1][1]
        self.assertIsInstance(handler,_CompatibleHTTPSHandler)
        self.assertEqual(handler._context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)
        self.assertFalse(any(isinstance(item,urllib.request.ProxyHandler) for item in handlers[1]),'no custom proxy handler overrides the default route')
        self.assertIsNone(handlers[1][0].redirect_request(requests[1],None,302,'redirect',{},'https://unrelated.invalid/'))
        self.assertEqual(standard['transport']['transportMode'],'standard')
        self.assertEqual(compatible['transport']['transportMode'],'compatible')
        self.assertEqual(compatible['transport']['requestEncoding'],'identity')
        self.assertEqual(compatible['transport']['configuredWriteChunkBytes'],512)

    def test_compatible_tls_bytes_are_paced_in_512_byte_writes_including_send_boundaries(self):
        connection=_CompatibleHTTPSConnection('course-api.example')
        tls=Mock(spec=ssl.SSLSocket);connection.sock=tls
        content=b'x'*1500
        with patch('backend.provider.time.sleep') as sleep:
            connection.send(content)
            connection.send(b'end')
        chunks=[call.args[0] for call in tls.sendall.call_args_list]
        self.assertEqual([len(item) for item in chunks],[512,512,476,3])
        self.assertEqual(b''.join(chunks),content+b'end')
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[.03,.03,.03])

    def test_connect_tunnel_plain_socket_does_not_use_tls_pacing(self):
        connection=_CompatibleHTTPSConnection('course-api.example')
        plain=Mock(spec=socket.socket);connection.sock=plain
        connect=b'CONNECT course-api.example:443 HTTP/1.1\r\n'+b'x'*1400
        with patch('backend.provider.time.sleep') as sleep:connection.send(connect)
        plain.sendall.assert_called_once_with(connect)
        sleep.assert_not_called()

    def test_gzip_response_is_bounded_and_reports_wire_and_decoded_bytes(self):
        wire=gzip.compress(PAYLOAD,mtime=0)
        self.config.transport_mode='compatible'
        response=Response([wire[:9],wire[9:]],{'Content-Encoding':'gzip','Content-Length':str(len(wire))})
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.return_value=response
            result=ModelClient(self.config).complete(self.messages)
        self.assertEqual(result['content'],'{"ok":true}')
        details=result['transport']
        self.assertEqual(details['receivedBytes'],len(wire))
        self.assertEqual(details['decodedBytes'],len(PAYLOAD))
        self.assertEqual(details['contentEncoding'],'gzip')
        self.assertTrue(details['wireComplete'])
        self.assertTrue(details['bodyComplete'])
        self.assertNotIn(self.config.api_key,json.dumps(details))

    def test_invalid_truncated_crc_and_extra_gzip_data_are_never_decoded_as_json(self):
        good=gzip.compress(PAYLOAD,mtime=0)
        crc=bytearray(good);crc[-8]^=1
        cases=[(good[:-1],'transport_incomplete'),(bytes(crc),'response_decompression'),
               (b'not a gzip stream','response_decompression'),
               (good+b'trailing garbage','response_decompression'),
               (good+gzip.compress(b'{}',mtime=0),'response_decompression')]
        self.config.transport_mode='compatible'
        for wire,code in cases:
            with self.subTest(code=code),patch('backend.provider.json.loads',side_effect=AssertionError('invalid gzip must not be parsed')):
                error=self.caught(response=Response([wire],{'Content-Encoding':'gzip','Content-Length':str(len(wire))}))
            self.assertEqual(error.code,code)
            self.assertEqual(error.phase,'decompress_body')
            self.assertTrue(error.transport['wireComplete'])
            self.assertFalse(error.transport['bodyComplete'])
            self.assertEqual(error.transport['receivedBytes'],len(wire))

    def test_wire_and_decompressed_limits_are_both_enforced(self):
        oversized=Response([],{'Content-Length':str(_MAX_RESPONSE_BYTES+1)})
        error=self.caught(response=oversized)
        self.assertEqual(error.code,'response_too_large')
        self.assertEqual(oversized.reads,0)
        bomb=gzip.compress(b'x'*(_MAX_RESPONSE_BYTES+1),mtime=0)
        with patch('backend.provider.json.loads',side_effect=AssertionError('oversized output must not be parsed')):
            error=self.caught(response=Response([bomb],{'Content-Encoding':'gzip','Content-Length':str(len(bomb))}))
        self.assertEqual(error.code,'response_too_large')
        self.assertEqual(error.phase,'decompress_body')
        self.assertLessEqual(error.transport['decodedBytes'],_MAX_RESPONSE_BYTES+1)
        self.assertFalse(error.transport['bodyComplete'])

    def test_unsupported_content_encoding_fails_without_mode_fallback(self):
        self.config.transport_mode='compatible'
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.return_value=Response([PAYLOAD],{'Content-Encoding':'br'})
            with self.assertRaises(ProviderError) as caught:ModelClient(self.config).complete(self.messages)
            self.assertEqual(build.return_value.open.call_count,1)
        self.assertEqual(caught.exception.code,'response_encoding')
        self.assertEqual(caught.exception.phase,'decompress_body')

    def test_successful_transport_diagnostics_are_saved_without_breaking_legacy_mocks(self):
        self.config.transport_mode='compatible'
        self.app=Platform(self.config,provider=ModelClient(self.config),tools=NoTools())
        wire=gzip.compress(PAYLOAD,mtime=0)
        with patch('backend.provider.urllib.request.build_opener') as build:
            build.return_value.open.return_value=Response([wire],{'Content-Encoding':'gzip','Content-Length':str(len(wire))})
            self.app.action({'action':'send','channelId':'coordinator','mode':'chat','text':'fixture'})
            self.assertTrue(self.app.wait_for_idle(2))
        run=self.app.snapshot()['sessions']['coordinator']['run']
        self.assertEqual(run['status'],'done')
        details=run['calls'][0]['transport']
        self.assertEqual(details['transportMode'],'compatible')
        self.assertEqual(details['receivedBytes'],len(wire))
        self.assertEqual(self.app.repo.get_run(run['id'])['calls'][0]['transport'],details)
        class Legacy:
            def complete(inner,messages):return {'content':'fixture plain reply'}
        self.app.provider=Legacy()
        self.app.action({'action':'send','channelId':'coordinator','mode':'chat','text':'legacy fixture'})
        self.assertTrue(self.app.wait_for_idle(2))
        run=self.app.snapshot()['sessions']['coordinator']['run']
        self.assertEqual(run['status'],'done')
        self.assertNotIn('transport',run['calls'][0])


if __name__=='__main__':unittest.main()
