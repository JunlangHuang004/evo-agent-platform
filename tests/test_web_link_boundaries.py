"""Discovery metadata must never become fetched target evidence or extra I/O."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.runtime import _working_tool_results, compress_messages
from backend.tools import ToolError, ToolGateway, _discovered_links
from backend.web_context import collect_web_records, project_web_record
from tests.test_file_context_cache import tool, noise


class WebLinkBoundaryTests(unittest.TestCase):
    def test_discovery_does_not_resolve_dns_or_follow_any_anchor(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=ToolGateway(SimpleNamespace(data_dir=Path(root),git_remote=''))
            response={'url':'https://example.edu/news/','contentType':'text/html','truncated':False,
                'content':'<p>Actual page body</p><a href="//elsewhere.example/story">A discovered story</a>'}
            with patch.object(gateway,'_fetch_url',return_value=response) as fetch, \
                    patch('socket.getaddrinfo',side_effect=AssertionError('discovery must not resolve DNS')):
                result=gateway._web_fetch('https://example.edu/original')
            fetch.assert_called_once_with('https://example.edu/original')
            self.assertEqual(result['links'][0]['url'],'https://elsewhere.example/story')
            self.assertTrue(result['linksAreUnfetched'])
            self.assertEqual(result['sources'],[{'title':response['url'],'url':response['url']}])
            self.assertNotIn('https://elsewhere.example/story',[source['url'] for source in result['sources']])

    def test_deadline_is_still_forwarded_and_expired_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=ToolGateway(SimpleNamespace(data_dir=Path(root),git_remote=''))
            response={'url':'https://example.edu/','contentType':'text/html','truncated':False,
                      'content':'<p>Read text</p><a href="/story">Target</a>'}
            with patch.object(gateway,'_fetch_url',return_value=response) as fetch:
                with self.assertRaises(ToolError):gateway._web_fetch(response['url'],deadline=0)
            fetch.assert_called_once_with(response['url'],deadline=0)

    def test_body_omission_also_omits_unfetched_links_without_promoting_them_to_sources(self):
        value={'ok':True,'url':'https://example.edu/listing','content':'Actual fetched body '+('x'*60000),
               'callId':'real-fetch','links':[{'title':'Discovered only','url':'https://example.edu/unread-target'}],
               'linkCount':87,'linksTruncated':True,'linksAreUnfetched':True}
        messages=[{'role':'system','content':'Protocol'},
                  {'role':'user','content':json.dumps({'task':{'id':'own-task'},'runGoal':'Investigate'})}]
        messages+=tool('web.fetch',{'url':value['url']},value)
        records,_=_working_tool_results(messages);record=collect_web_records(records)[0]
        marker=project_web_record(record,messages)
        projected=messages[:2]+marker+noise(4,20000)
        for _ in range(3):
            projected=compress_messages(projected,40000)
            records,_=_working_tool_results(projected);record=collect_web_records(records)[0]
            self.assertFalse(record['webIdentity']['bodyPresent'])
            omitted=record['value']
            self.assertTrue(omitted['linksOmitted']);self.assertTrue(omitted['linksAreUnfetched'])
            self.assertEqual(omitted['linkCount'],87)
            self.assertEqual(len(omitted['linksSha256']),64)
            self.assertNotIn('links',omitted);self.assertNotIn('sources',omitted)
            self.assertNotIn('https://example.edu/unread-target',json.dumps(omitted))
            self.assertLessEqual(sum(len(item['content']) for item in projected),40000)


if __name__=='__main__':unittest.main()
