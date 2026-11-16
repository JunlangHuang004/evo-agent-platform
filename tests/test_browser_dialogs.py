"""Explicit dialog replies test actual prompt/confirm user branches in Docker."""
import copy
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

HTML = '''<!doctype html><button id="cancel">Cancel reservation</button>
<button id="confirm">Confirm</button><button id="alert">Alert</button><p id="state">booked</p>
<script>
document.querySelector('#cancel').onclick=()=>{
 const who=prompt('Your name');
 document.querySelector('#state').textContent=who===null?'dismissed':who==='Alice'?'cancelled':'denied';
};
document.querySelector('#confirm').onclick=()=>{document.querySelector('#state').textContent=confirm('Sure?')?'yes':'no';};
document.querySelector('#alert').onclick=()=>{alert('Notice');document.querySelector('#state').textContent='acknowledged';};
</script>'''


class BrowserDialogValidationTests(unittest.TestCase):
    def test_explicit_bounded_dialog_schema(self):
        assertion={'action':'assert_text','selector':'#state','text':'cancelled'}
        good={'action':'respond_dialog','dialogType':'prompt','accept':True,'value':'Alice'}
        validate_browser_args({'path':'index.html','checks':[good,assertion]})
        for change in ({'accept':1},{'dialogType':'beforeunload'},{'value':'x'*2001},
                       {'accept':False},{'dialogType':'confirm'},{'execute':'arbitrary code'},
                       {'pageId':'missing'}):
            with self.subTest(change=change),self.assertRaises(ToolError):
                validate_browser_args({'path':'index.html','checks':[{**good,**change},assertion]})

    def test_host_does_not_accept_unhandled_wrong_page_or_wrong_response_evidence(self):
        fixture=legacy.BrowserToolUnitTests();fixture.setUp()
        try:
            step={'action':'respond_dialog','dialogType':'prompt','accept':True,'value':'Alice'}
            valid={'pageId':'main','dialogType':'prompt','accept':True,'value':'Alice','observedType':'prompt','handled':True}
            for change in ({'handled':False},{'pageId':'other'},{'value':'Bob'},{'observedType':'confirm'},{'accept':1}):
                def transform(report,spec):report['dialogResponses']=[{**valid,**change}]
                with self.subTest(change=change),patch('backend.sandbox.run_process',side_effect=fixture.fake_browser([],transform)):
                    result=fixture.call(fixture.args(checks=[step,{'action':'assert_text','selector':'#result','text':'Rendered'}]))
                self.assertFalse(result['ok']);self.assertFalse(result['browser']['dialogCoverageComplete'])
        finally:fixture.tearDown()


@unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS')=='1','Explicit real Docker browser tests')
class BrowserDialogDockerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.gateway=ToolGateway(Config(Path(self.temp.name),{}))
        self.gateway.execute('executor','dialogs','file.write',{'path':'index.html','content':HTML})
    def tearDown(self):self.temp.cleanup()
    def check(self,steps):
        return self.gateway.execute('executor','dialogs','code.test',{'language':'browser','path':'index.html','checks':steps,'timeout':40})
    def reply(self,kind,accept=True,**extra):return {'action':'respond_dialog','dialogType':kind,'accept':accept,**extra}
    def text(self,value,**extra):return {'action':'assert_text','selector':'#state','text':value,'exact':True,**extra}
    def click(self,selector,**extra):return {'action':'click','selector':selector,**extra}

    def test_prompt_owner_wrong_owner_dismiss_and_confirm_and_alert(self):
        result=self.check([self.reply('prompt',value='Bob'),self.click('#cancel'),self.text('denied'),
            self.reply('prompt',False),self.click('#cancel'),self.text('dismissed'),
            self.reply('prompt',value='Alice'),self.click('#cancel'),self.text('cancelled'),
            self.reply('confirm',False),self.click('#confirm'),self.text('no'),
            self.reply('confirm'),self.click('#confirm'),self.text('yes'),
            self.reply('alert'),self.click('#alert'),self.text('acknowledged')])
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(result['browser']['dialogResponses']),6)
        self.assertTrue(result['browser']['dialogCoverageComplete'])

    def test_unused_or_mismatched_response_never_passes(self):
        for steps in ([self.reply('prompt',value='Alice'),self.text('booked')],
                      [self.reply('confirm'),self.click('#cancel'),self.text('dismissed')],
                      [self.reply('prompt'),self.reply('prompt'),self.text('booked')]):
            result=self.check(steps);self.assertFalse(result['ok']);self.assertFalse(result['browser']['dialogCoverageComplete'])

    def test_responses_are_page_local_and_calls_do_not_reuse_old_response(self):
        result=self.check([{'action':'new_page','pageId':'second'},self.reply('prompt',value='Alice'),
            self.reply('prompt',value='Bob',pageId='second'),self.click('#cancel',pageId='second'),
            self.text('denied',pageId='second'),self.click('#cancel'),self.text('cancelled')])
        self.assertTrue(result['ok'],result)
        fresh=self.check([self.click('#cancel'),self.text('dismissed')])
        self.assertTrue(fresh['ok'],fresh);self.assertEqual(fresh['browser']['dialogResponses'],[])


if __name__=='__main__':unittest.main()
