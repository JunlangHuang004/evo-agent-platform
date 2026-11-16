"""Generic storage-read failures in real Docker; fixture apps are not products."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.browser_spec import validate_browser_args,read_fault_coverage
from backend.config import Config
from backend.sandbox import ToolError
from backend.tools import ToolGateway
try:
    from . import test_browser_tools as legacy
except ImportError:
    import test_browser_tools as legacy


ORIGINAL='[{"id":"old"}]'


def application(good=True):
    read=("function read(){return JSON.parse(localStorage.getItem('records')||'[]');}" if good else
          "function read(){try{return JSON.parse(localStorage.getItem('records')||'[]');}catch(e){return [];}}")
    return '''<!doctype html><button id="read">Read</button><button id="save">Save</button>
<button id="other">Other key</button><button id="session">Session</button><button id="writeOther">Write other</button><button id="pretend">Pretend fault</button>
<p id="status"></p><p id="count"></p><script>'''+read+'''
const status=s=>document.querySelector('#status').textContent=s;
function load(){try{const records=read();document.querySelector('#count').textContent=String(records.length);status('ready:'+records.length);}catch(e){status('read-error');}}
document.querySelector('#read').onclick=load;
document.querySelector('#save').onclick=()=>{let records;try{records=read();}catch(e){status('read-error');return;}records.push({id:'new'});try{localStorage.setItem('records',JSON.stringify(records));status('saved');}catch(e){status('write-error');}};
document.querySelector('#other').onclick=()=>{try{status(localStorage.getItem('other'));}catch(e){status('read-error');}};
document.querySelector('#session').onclick=()=>{sessionStorage.setItem('records','session-ok');status(sessionStorage.getItem('records'));};
document.querySelector('#writeOther').onclick=()=>{try{localStorage.setItem('other','changed');status('written');}catch(e){status('write-error');}};
document.querySelector('#pretend').onclick=()=>{const c=JSON.parse(sessionStorage.getItem('__evoHarnessStorageReadFault_v1'));window.__evoReportStorageReadDenied({epoch:c.epoch,token:c.token,operation:'getItem',key:'records',harnessRead:false});status('pretended');};
load();</script>'''


class ReadFaultUnitTests(unittest.TestCase):
    def test_schema_and_tool_definition_expose_read_failure_without_arbitrary_code(self):
        valid=[{'action':'storage_read_denied','enabled':True,'key':'records'},
               {'action':'assert_text','selector':'#status','text':'read-error'},
               {'action':'storage_read_denied','enabled':False},
               {'action':'assert_storage','key':'records','value':ORIGINAL}]
        validate_browser_args({'path':'index.html','checks':valid})
        for bad in ({'action':'storage_read_denied','enabled':'true'}, {'action':'storage_read_denied','enabled':True,'key':''},
                    {'action':'storage_read_denied','enabled':True,'pageId':'missing'},
                    {'action':'storage_read_denied','enabled':True,'script':'arbitrary()'}):
            with self.subTest(bad=bad),self.assertRaises(ToolError):validate_browser_args({'path':'index.html','checks':[bad,valid[1]]})
        with tempfile.TemporaryDirectory() as root:
            d=next(d for d in ToolGateway(Config(Path(root),{})).definitions('executor') if d['name']=='code.test')
            self.assertIn('storage_read_denied',d['parameters']['properties']['checks']['items']['properties']['action']['enum'])
            self.assertIn('assertions do not count',d['description'])
    def test_host_refuses_missing_zero_bool_wrong_page_key_operation_and_unexecuted_report(self):
        fixture=legacy.BrowserToolUnitTests();fixture.setUp()
        steps=[{'action':'storage_read_denied','key':'records','enabled':True},
               {'action':'assert_text','selector':'#result','text':'Rendered'},
               {'action':'storage_read_denied','enabled':False}]
        epoch={'id':'read-fault-1','pageId':'main','key':'records','enabledAtStep':1,'restoredAtStep':3,
               'blockedReads':1,'blockedHarnessReads':0,'blockedKeys':['records'],'operations':[{'operation':'getItem','key':'records'}]}
        variants=[None,('blockedReads',0),('blockedReads',True),('pageId','tab2'),('key','other'),
                  ('operations',[]),('operations',[{'operation':'setItem','key':'records'}]),
                  ('operations',[{'operation':'getItem','key':'other'}]),('enabledAtStep',True),('restoredAtStep',None)]
        try:
            for change in variants:
                def transform(report,spec):
                    e=copy.deepcopy(epoch)
                    if change:e[change[0]]=change[1]
                    report.update(readFaults=[] if change is None else [e],readFaultCoverageComplete=True)
                with self.subTest(change=change),patch('backend.sandbox.run_process',side_effect=fixture.fake_browser([],transform)):
                    result=fixture.call(fixture.args(checks=steps))
                self.assertFalse(result['ok']);self.assertFalse(result['browser']['readFaultCoverageComplete'])
                saved=json.loads((fixture.workspace/result['browser']['reportPath']).read_text())
                self.assertFalse(saved['ok']);self.assertFalse(saved['readFaultCoverageComplete'])
            def valid(report,spec):report.update(readFaults=[copy.deepcopy(epoch)],readFaultCoverageComplete=False)
            with patch('backend.sandbox.run_process',side_effect=fixture.fake_browser([],valid)):
                result=fixture.call(fixture.args(checks=steps))
            self.assertTrue(result['ok']);self.assertTrue(result['browser']['readFaultCoverageComplete'])
            def not_executed(report,spec):valid(report,spec);report['executed']=False
            with patch('backend.sandbox.run_process',side_effect=fixture.fake_browser([],not_executed)):
                result=fixture.call(fixture.args(checks=steps))
            self.assertFalse(result['ok']);self.assertFalse(result['browser']['readFaultCoverageComplete'])
        finally:fixture.tearDown()


@unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS')=='1','Explicit local pinned Docker browser tests')
class ReadFaultDockerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.gateway=ToolGateway(Config(Path(self.temp.name),{}));self.run='read-fault-fixture'
    def tearDown(self):self.temp.cleanup()
    def check(self,name,steps,good=True):
        html=application(good)
        self.gateway.execute('executor',self.run,'file.write',{'path':name+'.html','content':html})
        args={'language':'browser','path':name+'.html','checks':steps,'storage':{'records':ORIGINAL,'other':'other-ok'},'timeout':40}
        result=self.gateway.execute('executor',self.run,'code.test',args)
        if os.environ.get('BROWSER_READ_FAULT_EVIDENCE_DIR'):
            root=Path(os.environ['BROWSER_READ_FAULT_EVIDENCE_DIR']);root.mkdir(parents=True,exist_ok=True)
            (root/(name+'.json')).write_text(json.dumps({'fixture':True,'notGeneratedProduct':True,'name':name,
                'htmlSHA256':hashlib.sha256(html.encode()).hexdigest(),'html':html,'request':args,'result':result},ensure_ascii=False,indent=2))
        return result
    def test_good_read_error_stops_write_while_bad_empty_fallback_overwrites_original_records(self):
        steps=[{'action':'storage_read_denied','key':'records','enabled':True}, {'action':'reload'},
               {'action':'click','selector':'#save'}, {'action':'storage_read_denied','enabled':False},
               {'action':'assert_storage','key':'records','value':ORIGINAL}, {'action':'reload'},
               {'action':'assert_text','selector':'#count','text':'1','exact':True}]
        good=self.check('guarded',steps);self.assertTrue(good['ok'],good)
        self.assertTrue(good['browser']['readFaultCoverageComplete']);self.assertEqual(good['browser']['faults'],[])
        e=good['browser']['readFaults'][0];self.assertGreaterEqual(e['blockedReads'],2);self.assertEqual(e['blockedKeys'],['records'])
        self.assertTrue(all(o['operation']=='getItem' for o in e['operations']));self.assertEqual(e['restoredAtStep'],4)
        bad=self.check('empty-fallback',steps,good=False);self.assertFalse(bad['ok'],bad)
        self.assertTrue(bad['browser']['readFaultCoverageComplete']);self.assertEqual(bad['browser']['assertions'][-1]['actual'],'[{"id":"new"}]')
        self.assertEqual(bad['browser']['assertions'][-1]['expected'],ORIGINAL)
    def test_page_and_key_scope_reload_persistence_and_native_session_storage(self):
        steps=[{'action':'new_page','pageId':'tab2'}, {'action':'storage_read_denied','key':'records','enabled':True},
               {'action':'click','selector':'#session'}, {'action':'assert_text','selector':'#status','text':'session-ok','exact':True},
               {'action':'click','selector':'#other'}, {'action':'assert_text','selector':'#status','text':'other-ok','exact':True},
               {'action':'click','selector':'#read','pageId':'tab2'}, {'action':'assert_text','selector':'#status','text':'ready:1','exact':True,'pageId':'tab2'},
               {'action':'click','selector':'#read'}, {'action':'assert_text','selector':'#status','text':'read-error','exact':True},
               {'action':'reload'}, {'action':'assert_text','selector':'#status','text':'read-error','exact':True},
               {'action':'storage_read_denied','enabled':False}, {'action':'assert_storage','key':'records','value':ORIGINAL},
               {'action':'reload'}, {'action':'assert_text','selector':'#status','text':'ready:1','exact':True}]
        r=self.check('scope',steps);self.assertTrue(r['ok'],r);e=r['browser']['readFaults'][0]
        self.assertEqual(e['pageId'],'main');self.assertEqual(e['blockedKeys'],['records']);self.assertEqual(e['blockedHarnessReads'],0)
        self.assertEqual(r['browser']['pageErrors'],[])
    def test_read_and_write_faults_remain_independent_with_untargeted_read_mode(self):
        steps=[{'action':'storage_write_denied','key':'other','enabled':True},
               {'action':'storage_read_denied','enabled':True}, {'action':'click','selector':'#writeOther'},
               {'action':'assert_text','selector':'#status','text':'write-error','exact':True},
               {'action':'click','selector':'#session'}, {'action':'assert_text','selector':'#status','text':'session-ok','exact':True},
               {'action':'click','selector':'#other'}, {'action':'assert_text','selector':'#status','text':'read-error','exact':True},
               {'action':'storage_read_denied','enabled':False}, {'action':'storage_write_denied','enabled':False},
               {'action':'assert_storage','key':'other','value':'other-ok'}, {'action':'assert_storage','key':'records','value':ORIGINAL}]
        r=self.check('both',steps);self.assertTrue(r['ok'],r)
        self.assertEqual(r['browser']['faults'][0]['blockedWrites'],1)
        self.assertEqual(r['browser']['readFaults'][0]['key'],None);self.assertEqual(r['browser']['readFaults'][0]['blockedKeys'],['other'])
        self.assertTrue(r['browser']['faultCoverageComplete']);self.assertTrue(r['browser']['readFaultCoverageComplete'])
    def test_unexercised_fault_and_harness_only_getitem_never_count_as_application_coverage(self):
        untouched=[{'action':'storage_read_denied','key':'records','enabled':True}, {'action':'click','selector':'#other'},
                   {'action':'assert_text','selector':'#status','text':'other-ok','exact':True},
                   {'action':'storage_read_denied','enabled':False}, {'action':'assert_storage','key':'records','value':ORIGINAL}]
        r=self.check('not-exercised',untouched);self.assertFalse(r['ok']);self.assertFalse(r['browser']['readFaultCoverageComplete'])
        self.assertEqual(r['browser']['readFaults'][0]['blockedReads'],0)
        harness=[{'action':'storage_read_denied','key':'records','enabled':True}, {'action':'assert_storage','key':'records','value':ORIGINAL}]
        r=self.check('harness-not-app',harness);self.assertFalse(r['ok']);self.assertFalse(r['browser']['readFaultCoverageComplete'])
        self.assertEqual(r['browser']['readFaults'][0]['blockedReads'],0)
        self.assertGreater(r['browser']['readFaults'][0]['blockedHarnessReads'],0)
    def test_application_cannot_claim_fault_coverage_from_visible_harness_configuration(self):
        steps=[{'action':'storage_read_denied','key':'records','enabled':True}, {'action':'click','selector':'#pretend'},
               {'action':'assert_text','selector':'#status','text':'pretended','exact':True},
               {'action':'storage_read_denied','enabled':False}, {'action':'assert_storage','key':'records','value':ORIGINAL}]
        r=self.check('cannot-spoof-read',steps);self.assertFalse(r['ok']);self.assertFalse(r['browser']['readFaultCoverageComplete'])
        self.assertEqual(r['browser']['readFaults'][0]['blockedReads'],0)
        self.assertTrue(all(a['passed'] for a in r['browser']['assertions']),'only the missing real fault coverage rejects this fixture')


if __name__=='__main__':unittest.main()
