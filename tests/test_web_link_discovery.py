"""Actual page anchors aid navigation; never pretend their targets were read."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import tempfile
import unittest

from backend.tools import ToolGateway, _discovered_links


class WebLinkDiscoveryTests(unittest.TestCase):
    def test_fetch_resolves_actual_anchors_without_fetching_or_citing_targets(self):
        with tempfile.TemporaryDirectory() as root:
            gateway = ToolGateway(SimpleNamespace(data_dir=Path(root), git_remote=''))
            response = {'url':'https://example.edu/news/', 'contentType':'text/html', 'truncated':False,
                        'content':'<title>News</title><p>Event listing</p><a href="../story/ai?x=1&amp;y=2">AI event</a>'
                                  '<a href="../story/ai?x=1&amp;y=2#details">Long AI event title</a>'}
            with patch.object(gateway, '_fetch_url', return_value=response) as transport:
                result = gateway._web_fetch('https://example.edu/redirect')
            transport.assert_called_once_with('https://example.edu/redirect')
            self.assertEqual(result['links'], [{'url':'https://example.edu/story/ai?x=1&y=2', 'title':'Long AI event title'}])
            self.assertTrue(result['linksAreUnfetched'])
            self.assertEqual(result['sources'], [{'title':'News','url':'https://example.edu/news/'}])
            self.assertIn('Event listing', result['content'])

    def test_unsafe_empty_and_fragment_anchors_are_not_discovery_targets(self):
        bad = ['javascript:alert(1)', 'data:text/plain,secret', 'mailto:a@example.edu',
               'https://user:secret@example.edu/a', '#section', '', 'https://127.0.0.1/a',
               'http://[::1]/a', 'https://localhost/a', 'http://example.edu:1234/a',
               'https://exa\nmple.edu/a', '\\evil.test/a', 'http://[invalid/']
        result = _discovered_links([{'url':url,'title':'Bad'} for url in bad], 'https://example.edu/')
        self.assertEqual(result['links'], [])

    def test_links_are_bounded_and_omission_is_explicit(self):
        result = _discovered_links([{'url':'/article/'+str(i), 'title':'Article '+str(i)} for i in range(120)],
                                   'https://example.edu/', max_count=20, max_chars=700)
        self.assertLessEqual(len(result['links']), 20)
        self.assertLessEqual(len(json.dumps(result['links'], ensure_ascii=False)),700)
        self.assertEqual(result['linkCount'],120)
        self.assertTrue(result['linksTruncated'])

    def test_plain_text_keeps_the_existing_contract_without_invented_links(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=ToolGateway(SimpleNamespace(data_dir=Path(root), git_remote=''))
            response={'url':'https://example.edu/text','contentType':'text/plain','truncated':False,
                      'content':'An actual reference https://example.edu/x'}
            with patch.object(gateway,'_fetch_url',return_value=response):
                result=gateway._web_fetch(response['url'])
            self.assertNotIn('links', result)
            self.assertEqual(result['content'],response['content'])

    def test_empty_cover_anchors_keep_actual_accessible_labels_and_image_alt(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=ToolGateway(SimpleNamespace(data_dir=Path(root),git_remote=''))
            response={'url':'https://example.edu/news','contentType':'text/html','truncated':False,
                      'content':'<div>News listing</div><a href="/news/ai" class="cover-link" aria-label="AI campus event"></a>'
                                '<a href="/news/robot"><img alt="Robot demo" src="robot.png"></a>'
                                '<noscript><a href="/hidden" aria-label="Not rendered"></a></noscript>'}
            with patch.object(gateway,'_fetch_url',return_value=response):
                result=gateway._web_fetch(response['url'])
            self.assertEqual(result['links'],[{'title':'AI campus event','url':'https://example.edu/news/ai'},
                                               {'title':'Robot demo','url':'https://example.edu/news/robot'}])


if __name__=='__main__':
    unittest.main()
