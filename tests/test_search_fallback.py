"""Search discovery remains bounded, truthful and distinct from source reading."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote, parse_qs, urlsplit

from backend.config import Config
from backend.sandbox import ToolError
from backend.search_results import _YahooOrganic, yahoo_search_results, bing_search_results
from backend.tools import ToolGateway


def organic(url='https://example.edu/paper', title='Actual paper &amp; evidence'):
    return ('<div class="dd algo relsrch Sr"><div class="compTitle options-toggle">'
            '<a href="'+url+'"><div>Navigation text not the title</div><h3><span>'+title+'</span></h3></a>'
            '</div><div class="compText aAbs"><p>Search excerpt, not the source body.</p></div></div>')


class SearchExtractionTests(unittest.TestCase):
    def test_bing_direct_organic_heading_links_exclude_ads_answers_and_wrappers(self):
        row = '<li class="b_algo"><div><a href="https://label.example/">Display URL</a></div><h2><a href="https://hkust.edu.hk/news/item">Actual <strong>campus</strong> event</a></h2><p>Unfetched excerpt</p></li>'
        page = '<li class="b_ad">'+row+'</li><div class="b_ans">'+row+'</div>'+row
        page += '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=wrapped">Wrapped</a></h2></li>'
        page += '<li class="b_algo"><h2><a href="http://127.0.0.1/">Unsafe</a></h2></li>'
        self.assertEqual(bing_search_results(page,'https://www.bing.com/search?q=AI'),
                         [{'title':'Actual campus event','url':'https://hkust.edu.hk/news/item'}])
        self.assertEqual(bing_search_results('<li class="b_algo"><h2>Unlinked heading</h2><a href="https://wrong.example/">Other link</a></li>', 'https://www.bing.com/search'), [])

    def test_fragmented_long_title_stops_retaining_fragments_at_label_bound(self):
        parser = _YahooOrganic()
        parser.feed('<div class="algo relsrch"><div class="compTitle"><a href="https://example.edu/paper"><h3>')
        parser.feed('<span>X</span>' * 20000)
        self.assertEqual(parser.active['titleChars'], 1200)
        self.assertEqual(len(parser.active['title']), 1200)
        parser.feed('</h3></a></div></div>')
        self.assertEqual(parser.rows, [{'url': 'https://example.edu/paper', 'title': 'X' * 1200}])

    def test_pathological_page_depth_fails_closed_without_an_unbounded_stack(self):
        page='<div>'*1000+organic()+'</div>'*1000
        self.assertEqual(yahoo_search_results(page,'https://search.yahoo.com/search'),[])

    def test_explicit_ad_or_ai_ancestors_cannot_masquerade_as_organic(self):
        page='<div class="ad">'+organic('https://ad.example/')+'</div>'
        page+='<div class="genaipaa">'+organic('https://ai.example/')+'</div>'+organic()
        self.assertEqual(yahoo_search_results(page,'https://search.yahoo.com/search'),
                         [{'title':'Actual paper & evidence','url':'https://example.edu/paper'}])

    def test_title_must_be_inside_its_corresponding_anchor_not_a_sibling(self):
        wrong='<div class="algo relsrch"><div class="compTitle"><a href="https://wrong.example/">Icon</a><h3>Unrelated heading</h3></div></div>'
        valid='<div class="algo relsrch"><div class="compTitle"><a href="https://icon.example/">Icon</a><a href="https://right.example/"><h3>Real title</h3></a></div></div>'
        self.assertEqual(yahoo_search_results(wrong+valid,'https://search.yahoo.com/search'),
                         [{'title':'Real title','url':'https://right.example/'}])

    def test_only_complete_organic_title_links_not_ai_answers_adverts_or_navigation(self):
        page='<a href="https://nav.example/">Navigation</a>'
        page+='<div class="genaipaa"><h3><a href="https://ai.example/">AI generated answer</a></h3></div>'
        page+='<div class="ad"><div class="compTitle"><a href="https://ad.example/"><h3>Advert</h3></a></div></div>'
        page+=organic()+organic('https://example.edu/paper#another','Duplicate')
        page+='<div class="relsrch"><div class="compTitle"><a href="https://broken.example/"><h3>Incomplete'
        self.assertEqual(yahoo_search_results(page,'https://search.yahoo.com/search'),
                         [{'title':'Actual paper & evidence','url':'https://example.edu/paper'}])

    def test_wrapped_targets_decode_once_and_unsafe_targets_are_not_returned(self):
        wrapped='https://r.search.yahoo.com/path/RU='+quote('https://example.edu/Case?Q=A',safe='')+'/RK=2/RS=token'
        bad=['javascript:alert(1)','https://name:secret@example.edu/','http://127.0.0.1/',
             'http://169.254.169.254/','http://localhost/','https://machine.local/',
             'https://search.yahoo.com/scout/chat','//search.yahoo.com/search', 'https://example.edu/\\bad']
        page=organic(wrapped,'资料')+''.join(organic(url)for url in bad)
        self.assertEqual(yahoo_search_results(page,'https://search.yahoo.com/search'),
                         [{'title':'资料','url':'https://example.edu/Case?Q=A'}])

    def test_bounded_titles_results_and_hidden_script_text(self):
        page=''.join(organic('https://example.edu/'+str(i),'A'*1500+'<script>NOT A TITLE</script>')for i in range(20))
        results=yahoo_search_results(page,'https://search.yahoo.com/search',3)
        self.assertEqual(len(results),3);self.assertTrue(all(len(r['title'])==300 for r in results))
        self.assertTrue(all('NOT A TITLE' not in r['title']for r in results))
        self.assertEqual(yahoo_search_results(organic(title='   '),'https://search.yahoo.com/search'),[])


class SearchFallbackTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.gateway=ToolGateway(Config(Path(self.directory.name),{}))
        self.query='大学 AI 证据 & original?'

    def response(self,url,body):
        return {'url':url,'content':body,'contentType':'text/html','truncated':False}

    def call(self,**kwargs):
        return self.gateway.execute('research','search-test','web.search',{'query':self.query,'max_results':5},**kwargs)

    def test_primary_success_does_not_query_second_provider_or_claim_source_text(self):
        def fetch(url,deadline=None):
            return self.response(url,'<a class="result-link" href="https://example.edu/primary">Primary result</a>')
        with patch.object(self.gateway,'_fetch_url',side_effect=fetch)as mock:
            result=self.call()
        self.assertEqual(mock.call_count,1);self.assertEqual(result['provider'],'DuckDuckGo Lite')
        self.assertEqual(result['providerAttempts'][0]['status'],'success');self.assertTrue(result['resultsAreUnfetched'])
        self.assertNotIn('content',result);self.assertNotIn('snippet',result['results'][0])

    def test_actual_http_202_error_is_retained_when_fallback_returns_exact_links(self):
        seen=[];before=time.monotonic();deadline=before+4
        def fetch(url,deadline=None):
            seen.append((url,deadline))
            if 'duckduckgo.com' in url:raise ToolError('permanent','Public web server returned HTTP 202.',False)
            return self.response(url,organic())
        with patch.object(self.gateway,'_fetch_url',side_effect=fetch):result=self.call(deadline=deadline)
        self.assertEqual(len(seen),2);self.assertEqual(result['provider'],'Yahoo Search')
        self.assertEqual(parse_qs(urlsplit(seen[0][0]).query)['q'],[self.query])
        self.assertEqual(parse_qs(urlsplit(seen[1][0]).query)['p'],[self.query])
        self.assertEqual(result['providerAttempts'][0]['error']['message'],'Public web server returned HTTP 202.')
        self.assertEqual([r['status']for r in result['providerAttempts']],['failed','success'])
        self.assertLessEqual(seen[0][1],before+2.1);self.assertLessEqual(seen[1][1],deadline)
        self.assertEqual(result['results'][0]['url'],'https://example.edu/paper')

    def test_no_parseable_primary_results_uses_real_fallback_not_fabricated_links(self):
        def fetch(url,deadline=None):return self.response(url,'<html>Challenge or no results</html>' if 'duckduckgo' in url else organic())
        with patch.object(self.gateway,'_fetch_url',side_effect=fetch):result=self.call()
        self.assertEqual(result['providerAttempts'][0]['status'],'failed');self.assertEqual(len(result['results']),1)

    def test_two_unavailable_providers_can_reach_actual_bing_links_in_same_budget(self):
        def fetch(url, deadline=None):
            if 'bing.com' not in url:
                raise ToolError('transient', 'Actual provider timeout.', True)
            return self.response(url, '<li class="b_algo"><h2><a href="https://hkust.edu.hk/news">Campus news</a></h2></li>')
        with patch.object(self.gateway, '_fetch_url', side_effect=fetch) as mock:
            result = self.call()
        self.assertEqual(mock.call_count, 3)
        self.assertEqual(result['provider'], 'Bing Search')
        self.assertEqual([a['status'] for a in result['providerAttempts']], ['failed','failed','success'])
        self.assertTrue(result['resultsAreUnfetched'])
        self.assertNotIn('content', result)

    def test_all_provider_failures_remain_failed_and_preserve_both_messages(self):
        failures=[ToolError('permanent','HTTP 202'),ToolError('transient','HTTP 429',True),ToolError('transient','HTTP 503',True)]
        with patch.object(self.gateway,'_fetch_url',side_effect=failures)as mock:
            with self.assertRaises(ToolError)as caught:self.call()
        self.assertEqual(mock.call_count,3);self.assertEqual(caught.exception.code,'search_unavailable')
        self.assertIn('HTTP 202',str(caught.exception));self.assertIn('HTTP 429',str(caught.exception))

    def test_original_deadline_expiring_during_first_attempt_never_launches_second(self):
        def fetch(url,deadline=None):time.sleep(.03);raise ToolError('transient','First timed out',True)
        with patch.object(self.gateway,'_fetch_url',side_effect=fetch)as mock:
            with self.assertRaises(ToolError)as caught:self.call(deadline=time.monotonic()+.01)
        self.assertEqual(mock.call_count,1);self.assertEqual(caught.exception.code,'deadline')

    def test_attempt_deadlines_exhaust_search_budget_without_expiring_caller(self):
        clock = [100.0]
        def fetch(url, deadline=None):
            clock[0] = deadline + .001
            raise ToolError('deadline', 'Provider attempt timed out.', True)
        with patch('time.monotonic', side_effect=lambda: clock[0]), patch.object(self.gateway, '_fetch_url', side_effect=fetch) as mock:
            with self.assertRaises(ToolError) as caught:
                self.call(deadline=1000.0)
        self.assertEqual(mock.call_count, 3)
        self.assertEqual(caught.exception.code, 'search_unavailable')
        self.assertTrue(caught.exception.retryable)
        self.assertIn('DuckDuckGo Lite', str(caught.exception))
        self.assertIn('Yahoo Search', str(caught.exception))
        self.assertIn('Bing Search', str(caught.exception))
        self.assertLess(clock[0], 121)

    def test_same_attempt_timeout_at_actual_caller_deadline_remains_deadline(self):
        clock = [100.0]
        def fetch(url, deadline=None):
            clock[0] = 101.0
            raise ToolError('deadline', 'Caller expired.', True)
        with patch('time.monotonic', side_effect=lambda: clock[0]), patch.object(self.gateway, '_fetch_url', side_effect=fetch) as mock:
            with self.assertRaises(ToolError) as caught:
                self.call(deadline=101.0)
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(caught.exception.code, 'deadline')

    def test_security_and_unknown_outcomes_are_not_relabelled_as_search_unavailability(self):
        for error in [ToolError('permission','Blocked target'),ToolError('parameter','Invalid URL'),
                      ToolError('transient','Unknown outcome',True,outcome_unknown=True)]:
            with self.subTest(code=error.code),patch.object(self.gateway,'_fetch_url',side_effect=error)as mock:
                with self.assertRaises(ToolError)as caught:self.call()
                self.assertEqual(caught.exception.code,error.code)
                self.assertEqual(str(caught.exception),str(error))
                self.assertEqual(caught.exception.outcome_unknown,error.outcome_unknown)
                self.assertEqual(mock.call_count,1)


if __name__=='__main__':unittest.main()
