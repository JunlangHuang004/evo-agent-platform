"""Generic browser guardrail coverage. Fixture apps are tests, never delivered products."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.browser_spec import validate_browser_args
from backend.config import Config
from backend.sandbox import ToolError
from backend.tools import ToolGateway
try:
    from . import test_browser_tools as legacy
except ImportError:
    import test_browser_tools as legacy


def application(*, transactional=True, fresh=True):
    # One generic exclusive resource, deliberately unrelated to meeting-room DOM.
    read = "JSON.parse(localStorage.getItem('reservations') || '[]')"
    before = 'let base = ' + (read if fresh else 'records') + ';'
    mutation = ("const next=[...base,{resource:'x',owner:document.querySelector('#who').value}]; try { localStorage.setItem('reservations',JSON.stringify(next)); records=next; render(); status('saved'); } catch(e) { status('storage-error'); }" if transactional else
                "records=base; records.push({resource:'x',owner:document.querySelector('#who').value}); try {localStorage.setItem('reservations',JSON.stringify(records));}catch(e){} render();status('saved');")
    return '''<!doctype html><input id="who"><button id="reserve">Reserve resource</button>
<button id="clear">Clear data</button><button id="remove">Remove data</button>
<p id="count"></p><p id="status"></p><script>
let records=''' + read + ''';
function status(s){document.querySelector('#status').textContent=s;}
function render(){document.querySelector('#count').textContent=String(records.length);}
document.querySelector('#who').oninput=()=>localStorage.setItem('name',document.querySelector('#who').value);
document.querySelector('#reserve').onclick=()=>{''' + before + '''if(base.length){status('conflict');return;}''' + mutation + '''};
document.querySelector('#clear').onclick=()=>{try{localStorage.clear();status('cleared');}catch(e){status('storage-error');}};
document.querySelector('#remove').onclick=()=>{try{localStorage.removeItem('reservations');status('removed');}catch(e){status('storage-error');}};
console.log('page fixture loaded');render();</script>'''


class BrowserFaultValidationTests(unittest.TestCase):
    def args(self, checks): return {'path':'index.html','checks':checks}
    def test_pages_faults_and_missing_value_have_explicit_bounded_schemas(self):
        good=[{'action':'new_page','pageId':'tab2'}, {'action':'storage_write_denied','pageId':'tab2','key':'records','enabled':True},
              {'action':'assert_storage_missing','pageId':'main','key':'records'}, {'action':'storage_write_denied','pageId':'tab2','enabled':False}]
        validate_browser_args(self.args(good))
        invalid=[good + [{'action':'new_page','pageId':'tab2'}],
                 [{'action':'assert_storage_missing','pageId':'unknown','key':'x'}],
                 [{'action':'new_page','pageId':'https://outside.example'},good[2]],
                 [{'action':'new_page','pageId':'b'},{'action':'new_page','pageId':'c'},{'action':'new_page','pageId':'d'},good[2]],
                 [{'action':'storage_write_denied','enabled':'true'},good[2]],
                 [{'action':'storage_write_denied','enabled':True,'key':''},good[2]],
                 [{'action':'assert_storage_missing','key':'x','value':'null'}]]
        for steps in invalid:
            with self.subTest(steps=steps),self.assertRaises(ToolError):validate_browser_args(self.args(steps))
        with tempfile.TemporaryDirectory() as root:
            definition=next(item for item in ToolGateway(Config(Path(root),{})).definitions('executor') if item['name']=='code.test')
            actions=definition['parameters']['properties']['checks']['items']['properties']['action']['enum']
            self.assertTrue({'new_page','storage_write_denied','assert_storage_missing'}.issubset(actions))
            self.assertIn('pageId',definition['parameters']['properties']['checks']['items']['properties'])

    def test_host_rejects_unexercised_or_mismatched_fault_report_even_when_runner_claims_ok(self):
        fixture=legacy.BrowserToolUnitTests();fixture.setUp()
        try:
            steps=[{'action':'storage_write_denied','key':'reservations','enabled':True},
                   {'action':'assert_text','selector':'#result','text':'Rendered'}]
            for epoch in [None,{'pageId':'main','key':'reservations','blockedWrites':0},
                          {'pageId':'main','key':'wrong','blockedWrites':1},
                          {'pageId':'main','key':'reservations','blockedWrites':True}]:
                def transform(report,spec):report['faults']=[] if epoch is None else [epoch]
                with self.subTest(epoch=epoch),patch('backend.sandbox.run_process',side_effect=fixture.fake_browser([],transform)):
                    result=fixture.call(fixture.args(checks=steps))
                self.assertFalse(result['ok']);self.assertFalse(result['browser']['faultCoverageComplete'])
        finally:fixture.tearDown()


@unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS')=='1','Explicit local pinned Docker browser tests')
class BrowserFaultDockerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.gateway=ToolGateway(Config(Path(self.temp.name),{}));self.run='fault-fixture'
    def tearDown(self):self.temp.cleanup()
    def check(self,name,html,steps,storage=None):
        self.gateway.execute('executor',self.run,'file.write',{'path':name+'.html','content':html})
        return self.gateway.execute('executor',self.run,'code.test',{'language':'browser','path':name+'.html','checks':steps,'storage':storage or {},'timeout':40})

    def test_targeted_fault_detects_ghost_state_and_good_transaction_restores_writes(self):
        steps=[{'action':'storage_write_denied','key':'reservations','enabled':True},
               {'action':'fill','selector':'#who','value':'A'},
               {'action':'click','selector':'#reserve'},
               {'action':'assert_storage_missing','key':'reservations'},
               {'action':'assert_text','selector':'#count','text':'0','exact':True},
               {'action':'assert_text','selector':'#status','text':'storage-error','exact':True},
               {'action':'storage_write_denied','enabled':False},
               {'action':'click','selector':'#reserve'},
               {'action':'assert_text','selector':'#count','text':'1','exact':True},
               {'action':'assert_storage','key':'reservations','value':'[{"resource":"x","owner":"A"}]'}]
        good=self.check('transaction-good',application(),steps)
        self.assertTrue(good['ok'],good)
        epoch=good['browser']['faults'][0]
        self.assertEqual(epoch['blockedWrites'],1);self.assertEqual(epoch['blockedKeys'],['reservations'])
        self.assertEqual(epoch['operations'],[{'operation':'setItem','key':'reservations'}])
        self.assertIsNotNone(epoch['restoredAtStep'])
        bad=self.check('transaction-bug',application(transactional=False),steps)
        self.assertFalse(bad['ok'],bad)
        self.assertEqual(bad['browser']['faults'][0]['blockedWrites'],1)
        self.assertEqual(bad['browser']['assertions'][-1]['actual'],'1','the buggy app exposes unsaved memory state')

    def test_two_preopened_pages_distinguish_stale_overwrite_from_fresh_conflict_check(self):
        steps=[{'action':'new_page','pageId':'tab2'},
               {'action':'fill','selector':'#who','value':'A'}, {'action':'click','selector':'#reserve'},
               {'action':'fill','pageId':'tab2','selector':'#who','value':'B'}, {'action':'click','pageId':'tab2','selector':'#reserve'},
               {'action':'assert_storage','pageId':'tab2','key':'reservations','value':'[{"resource":"x","owner":"A"}]'},
               {'action':'assert_text','pageId':'tab2','selector':'#status','text':'conflict','exact':True}]
        good=self.check('tabs-good',application(),steps)
        self.assertTrue(good['ok'],good);self.assertEqual([item['pageId'] for item in good['browser']['pages']],['main','tab2'])
        self.assertEqual(good['browser']['screenshotPageId'],'tab2')
        bad=self.check('tabs-bug',application(fresh=False),steps)
        self.assertFalse(bad['ok'],bad)
        self.assertIn('"owner":"B"',bad['browser']['assertions'][-1]['actual'])

    def test_new_page_does_not_reseed_and_each_call_has_a_fresh_context(self):
        steps=[{'action':'fill','selector':'#who','value':'A'},{'action':'click','selector':'#reserve'},
               {'action':'new_page','pageId':'tab2'},
               {'action':'assert_storage','pageId':'tab2','key':'reservations','value':'[{"resource":"x","owner":"A"}]'},
               {'action':'assert_text','pageId':'tab2','selector':'#count','text':'1','exact':True}]
        first=self.check('single-seed',application(),steps,storage={'reservations':'[]'})
        self.assertTrue(first['ok'],first)
        second=self.check('fresh-context',application(),[{'action':'assert_storage_missing','key':'reservations'},
               {'action':'assert_text','selector':'#count','text':'0','exact':True}])
        self.assertTrue(second['ok'],second)

    def test_untriggered_fault_never_passes_even_if_all_assertions_pass(self):
        result=self.check('unexercised',application(),[
            {'action':'storage_write_denied','key':'never-written','enabled':True},
            {'action':'fill','selector':'#who','value':'Other key write'},
            {'action':'assert_storage_missing','key':'never-written'}])
        self.assertFalse(result['ok'],result)
        self.assertTrue(result['browser']['assertions'][0]['passed'])
        self.assertEqual(result['browser']['faults'][0]['blockedWrites'],0)
        self.assertFalse(result['browser']['faultCoverageComplete'])

    def test_clear_remove_and_all_keys_faults_report_actual_target_operations(self):
        targeted=self.check('clear-remove',application(),[
            {'action':'storage_write_denied','key':'reservations','enabled':True},
            {'action':'click','selector':'#clear'}, {'action':'assert_text','selector':'#status','text':'storage-error','exact':True},
            {'action':'click','selector':'#remove'}, {'action':'assert_storage','key':'reservations','value':'[]'},
            {'action':'storage_write_denied','enabled':False},{'action':'click','selector':'#clear'},
            {'action':'assert_storage_missing','key':'reservations'}],storage={'reservations':'[]','other':'untouched'})
        self.assertTrue(targeted['ok'],targeted)
        self.assertEqual(targeted['browser']['faults'][0]['blockedWrites'],2)
        self.assertEqual([item['operation'] for item in targeted['browser']['faults'][0]['operations']],['clear','removeItem'])
        absent=self.check('clear-absent',application(),[
            {'action':'storage_write_denied','key':'absent','enabled':True},{'action':'click','selector':'#clear'},
            {'action':'assert_text','selector':'#status','text':'cleared','exact':True}])
        self.assertFalse(absent['ok'],'clear cannot count as exercising a target key which did not exist')
        self.assertEqual(absent['browser']['faults'][0]['blockedWrites'],0)
        all_keys=self.check('all-keys',application(),[
            {'action':'storage_write_denied','enabled':True},{'action':'click','selector':'#clear'},
            {'action':'assert_text','selector':'#status','text':'storage-error','exact':True}])
        self.assertTrue(all_keys['ok'],all_keys)
        self.assertIsNone(all_keys['browser']['faults'][0]['key'])
        self.assertEqual(all_keys['browser']['faults'][0]['operations'],[{'operation':'clear','key':'*'}])

    def test_secondary_page_errors_and_fault_locality_are_not_hidden(self):
        html=application()+'''<script>if(localStorage.getItem('name')==='trigger-error')throw new Error('second-page-visible-error');</script>'''
        result=self.check('secondary-error',html,[
            {'action':'fill','selector':'#who','value':'trigger-error'}, {'action':'new_page','pageId':'tab2'},
            {'action':'assert_text','pageId':'main','selector':'#count','text':'0','exact':True}])
        self.assertFalse(result['ok'],result)
        self.assertTrue(any(item['pageId']=='tab2' and 'second-page-visible-error' in item['message'] for item in result['browser']['pageErrors']))
        local=self.check('per-page-fault',application(),[
            {'action':'new_page','pageId':'tab2'}, {'action':'storage_write_denied','key':'reservations','enabled':True},
            {'action':'click','selector':'#remove'},
            {'action':'fill','pageId':'tab2','selector':'#who','value':'B'}, {'action':'click','pageId':'tab2','selector':'#reserve'},
            {'action':'assert_storage','pageId':'main','key':'reservations','value':'[{"resource":"x","owner":"B"}]'}])
        self.assertTrue(local['ok'],local)
        self.assertEqual(local['browser']['faults'][0]['pageId'],'main')
        self.assertEqual(local['browser']['faults'][0]['blockedWrites'],1)


if __name__=='__main__':unittest.main()
