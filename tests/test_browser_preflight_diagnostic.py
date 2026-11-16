"""Actual R7 missing-resource diagnostics; no fallback or permission changes."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.sandbox import ToolError,write_text
from backend.tools import ToolGateway

FIXTURE=Path(__file__).parent/'fixtures/browser-preflight-r7-software2.json'

class BrowserPreflightDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.config=Config(Path(self.tmp.name),{});self.g=ToolGateway(self.config)
        self.manager=self.g.sandbox;self.manager.docker='/fake/docker'
        self.ws=self.g.workspace('executor','run1');self.imp=self.manager.imports('executor','run1')
        self.ready=patch.object(self.manager,'capabilities',return_value={'dockerAvailable':True,'browserImageReady':True});self.ready.start()
    def tearDown(self):self.ready.stop();self.tmp.cleanup()
    def call(self,path='dashboard.html',resources=None):
        return self.g.execute('executor','run1','code.test',{'language':'browser','path':path,'resources':resources or {},'checks':[{'action':'assert_text','selector':'h1','text':'Fixture'}]})
    def assert_no_container(self,path='dashboard.html',resources=None):
        with patch('backend.sandbox.run_process') as process:
            with self.assertRaises(ToolError) as caught:self.call(path,resources)
            self.assertFalse(any(c.args and len(c.args[0])>1 and c.args[0][1]=='run' for c in process.call_args_list),
                             'Preflight may clean up its reserved random name, but must never create/run a container')
        self.assertFalse(list((self.config.data_dir/'browser-inputs'/'executor'/'run1').iterdir()))
        return caught.exception
    def test_exact_r7_mapping_reports_missing_private_resource_without_automatic_import_fallback(self):
        fixture=json.loads(FIXTURE.read_text());f=fixture['failedPreflight'];self.assertEqual(f['error']['code'],'permanent');self.assertFalse(f['browserExecuted'])
        write_text(self.ws,'dashboard.html','<h1>Fixture</h1><script src="app.js"></script>')
        for name in f['resources']:
            if name!='app.js':write_text(self.ws,name,'/* bounded fixture */')
        # A readable owner import exists, but the caller did NOT request it.
        write_text(self.imp,'artifact-old/app.js','/* IMPORT_SECRET_MARKER */')
        e=self.assert_no_container(f['path'],f['resources'])
        self.assertEqual(e.code,'permanent');self.assertFalse(e.retryable);self.assertFalse(e.outcome_unknown)
        self.assertIn('resource URL "app.js"',e.message);self.assertIn('source path "app.js"',e.message)
        self.assertIn('this agent/run private workspace',e.message);self.assertIn('No browser/container was executed',e.message)
        self.assertNotIn(str(self.config.data_dir),e.message);self.assertNotIn('IMPORT_SECRET_MARKER',e.message)
        self.assertFalse((self.ws/'app.js').exists())
    def test_unreadable_entry_identifies_only_the_requested_entry(self):
        e=self.assert_no_container('missing.html');self.assertIn('entry page URL "missing.html"',e.message);self.assertEqual(e.code,'permanent')
    def test_missing_import_remains_missing_and_discloses_no_alternative_owner(self):
        write_text(self.ws,'dashboard.html','<h1>Fixture</h1>')
        other=self.manager.imports('reviewer','run1');write_text(other,'artifact-only/app.js','UNSHARED_OTHER_OWNER')
        e=self.assert_no_container(resources={'app.js':'imports/artifact-only/app.js'})
        self.assertIn('read-only imports',e.message);self.assertIn('source path "imports/artifact-only/app.js"',e.message)
        self.assertNotIn('UNSHARED_OTHER_OWNER',e.message);self.assertNotIn(str(other),e.message)
    def test_symlink_and_linked_resources_still_fail_closed_without_target_content(self):
        write_text(self.ws,'dashboard.html','<h1>Fixture</h1>');outside=Path(self.tmp.name)/'outside.txt';outside.write_text('PRIVATE_TARGET_MARKER')
        for name,create in [('linked.js',lambda p:p.symlink_to(outside)),('hard.js',lambda p:os.link(outside,p))]:
            with self.subTest(name=name):
                create(self.ws/name);e=self.assert_no_container(resources={'app.js':name});self.assertEqual(e.code,'permission')
                self.assertNotIn('PRIVATE_TARGET_MARKER',e.message);self.assertNotIn(str(outside),e.message)
    def test_traversal_hidden_and_other_agent_capabilities_are_still_rejected(self):
        write_text(self.ws,'dashboard.html','<h1>Fixture</h1>')
        with patch('backend.sandbox.run_process') as process:
            for source in ['../reviewer/app.js','.env','/etc/passwd','imports/../app.js']:
                with self.subTest(source=source),self.assertRaises(ToolError) as error:self.call(resources={'app.js':source})
                self.assertEqual(error.exception.code,'permission')
            with self.assertRaises(ToolError):self.g.execute('research','run1','code.test',{'language':'browser','path':'dashboard.html','checks':[{'action':'assert_count','selector':'h1','count':1}]})
            self.assertFalse(any(c.args and len(c.args[0])>1 and c.args[0][1]=='run' for c in process.call_args_list),
                             'Preflight may clean up its reserved random name, but must never create/run a container')

@unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS')=='1','explicit local Docker opt-in required')
class BrowserPreflightCorrectedMappingDockerTests(unittest.TestCase):
    def test_only_a_new_explicit_readonly_import_mapping_runs_real_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            gateway=ToolGateway(Config(Path(folder),{}));ws=gateway.workspace('executor','run1');imp=gateway.sandbox.imports('executor','run1')
            write_text(ws,'dashboard.html','<h1 id="result">Waiting</h1><script src="app.js"></script>')
            body='document.querySelector("#result").textContent="Imported fixture ran";';write_text(imp,'artifact-approved/app.js',body)
            args={'language':'browser','path':'dashboard.html','resources':{'app.js':'app.js'},'checks':[{'action':'assert_text','selector':'#result','text':'Imported fixture ran'}]}
            with self.assertRaises(ToolError) as first:gateway.execute('executor','run1','code.test',args)
            self.assertIn('resource URL "app.js"',first.exception.message)
            args['resources']['app.js']='imports/artifact-approved/app.js'
            result=gateway.execute('executor','run1','code.test',args)
            self.assertTrue(result['ok']);self.assertTrue(result['browser']['executed']);self.assertEqual(result['browser']['assertionsPassed'],1)
            self.assertEqual({f['path'] for f in result['browser']['testedFiles']},{'dashboard.html','imports/artifact-approved/app.js'})
            self.assertEqual((imp/'artifact-approved/app.js').read_text(),body);self.assertFalse((ws/'app.js').exists())

if __name__=='__main__':unittest.main()
