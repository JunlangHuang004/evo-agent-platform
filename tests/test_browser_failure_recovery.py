"""Deterministic browser failure observations; no real model or generated host execution."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.browser_failure_recovery import browser_failure_feedback
from backend.config import Config
from backend.engine import Platform,clock_ms
from tests.test_time_only_runtime import Files,Model

# Exact last two failed test calls from immutable actual Round3 software-1 export.
# They are evidence, not a successful browser fixture or a generated answer.
ACTUAL_ROUND3 = json.loads(r'''{"sourceExportSha256":"ee0cc34d28d2c1a5ab503df257dfe2920d2c24b2ee4620fe9038ebc3405289e8","run":{"id":"run-53ef1b11557546d4","calls":[{"id":"call-5975acd8df924101","taskId":"task-f30da8398c8e41ae","agentId":"dev","tool":"code.test","args":{"checks":[{"action":"assert_count","count":10,"selector":"#gridBody tr"},{"action":"assert_count","count":12,"selector":"#gridBody tr:first-child td.cell"},{"action":"fill","selector":"#nameInput","value":"Alice Chan"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"assert_text","selector":"#status","text":"Booked Meeting Room 1 on 2026-11-16 at 08:00–09:00 for Alice Chan."},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#status","text":"Only Alice Chan can cancel this booking. Enter the same name to cancel."},{"action":"fill","selector":"#nameInput","value":"Bob Lee"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#status","text":"Only Alice Chan can cancel this booking. Enter the same name to cancel."},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"fill","selector":"#nameInput","value":"Alice Chan"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Free"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"reload"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"assert_text","selector":"#status","text":"Booked Meeting Room 1 on 2026-11-16 at 08:00–09:00 for Alice Chan."}],"language":"browser","path":"app/index.html","resources":{"app.js":"app/app.js","styles.css":"app/styles.css"},"timeout":20},"revision":0,"status":"failed","result":{"browser":{"assertionCount":3,"assertions":[{"action":"assert_count","actual":10,"expected":10,"pageId":"main","passed":true,"selector":"#gridBody tr"},{"action":"assert_count","actual":12,"expected":12,"pageId":"main","passed":true,"selector":"#gridBody tr:first-child td.cell"},{"action":"assert_text","actual":"Free","expected":"Alice Chan","pageId":"main","passed":false,"selector":"#gridBody tr:first-child td.cell:nth-child(2) .who"}],"assertionsPassed":2,"blockedRequests":[],"chromiumSandbox":false,"console":[],"dialogCoverageComplete":true,"dialogResponses":[],"engine":"chromium","entry":{"path":"app/index.html","sha256":"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65"},"error":"Browser step failed: Assertion failed","executed":true,"faultCoverageComplete":true,"faults":[],"httpErrors":[],"ok":false,"pageErrors":[],"pages":[{"entryPath":"app/index.html","pageId":"main"}],"reportPath":"browser-evidence/browser-39014593ee624888823081d5fb56ecd7/report.json","screenshot":{"bytes":85870,"path":"browser-evidence/browser-39014593ee624888823081d5fb56ecd7/screenshot.png","sha256":"85d9a97399aee2900a8611e7f8667862aba88c5a8895a8b70e31053a174ed4c1"},"screenshotCaptured":true,"screenshotPageId":"main","sourceManifest":[{"path":"app/index.html","sha256":"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65"},{"path":"app/styles.css","sha256":"bc467cd1e1a5fce289c46988d9b6b523f18f0a4155ec83cc52676a32cdc25a3b"},{"path":"app/app.js","sha256":"1c3935aa3aeb4585863322240d061e4e5965bd650d8cf85df40ca39a0de3ac7f"}],"steps":[{"action":"assert_count","actual":10,"expected":10,"ok":true,"pageId":"main","passed":true,"selector":"#gridBody tr"},{"action":"assert_count","actual":12,"expected":12,"ok":true,"pageId":"main","passed":true,"selector":"#gridBody tr:first-child td.cell"},{"action":"fill","ok":true,"pageId":"main","selector":"#nameInput"},{"action":"click","ok":true,"pageId":"main","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","actual":"Free","error":"Assertion failed","expected":"Alice Chan","ok":false,"pageId":"main","passed":false,"selector":"#gridBody tr:first-child td.cell:nth-child(2) .who"}],"testedFiles":[{"path":"app/index.html","sha256":"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65"},{"path":"app/styles.css","sha256":"bc467cd1e1a5fce289c46988d9b6b523f18f0a4155ec83cc52676a32cdc25a3b"},{"path":"app/app.js","sha256":"1c3935aa3aeb4585863322240d061e4e5965bd650d8cf85df40ca39a0de3ac7f"}],"version":"156.0.8078.4"},"durationMs":3877,"error":"Browser step failed: Assertion failed","exitCode":1,"isolation":{"containerId":"evo-6d13a90e7a804805a8d6ae104cd872b5","hostFallback":false,"imports":"explicit_read_only","namespaceId":"ns-bbe2052b6e55b50283a4","network":"none","ownerAgentId":"dev","resourceScope":"per_invocation","runId":"run-53ef1b11557546d4","workspace":"private_per_agent_run"},"ok":false,"outputLimitExceeded":false,"runtime":"browser","sandboxId":"evo-6d13a90e7a804805a8d6ae104cd872b5","stderr":"","stdout":"{\"engine\":\"chromium\",\"executed\":true,\"entry\":{\"path\":\"app/index.html\",\"sha256\":\"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65\"},\"testedFiles\":[{\"path\":\"app/index.html\",\"sha256\":\"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65\"},{\"path\":\"app/styles.css\",\"sha256\":\"bc467cd1e1a5fce289c46988d9b6b523f18f0a4155ec83cc52676a32cdc25a3b\"},{\"path\":\"app/app.js\",\"sha256\":\"1c3935aa3aeb4585863322240d061e4e5965bd650d8cf85df40ca39a0de3ac7f\"}],\"assertions\":[{\"action\":\"assert_count\",\"pageId\":\"main\",\"selector\":\"#gridBody tr\",\"expected\":10,\"actual\":10,\"passed\":true},{\"action\":\"assert_count\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell\",\"expected\":12,\"actual\":12,\"passed\":true},{\"action\":\"assert_text\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell:nth-child(2) .who\",\"expected\":\"Alice Chan\",\"actual\":\"Free\",\"passed\":false}],\"assertionCount\":3,\"assertionsPassed\":2,\"steps\":[{\"action\":\"assert_count\",\"pageId\":\"main\",\"selector\":\"#gridBody tr\",\"expected\":10,\"actual\":10,\"passed\":true,\"ok\":true},{\"action\":\"assert_count\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell\",\"expected\":12,\"actual\":12,\"passed\":true,\"ok\":true},{\"action\":\"fill\",\"pageId\":\"main\",\"selector\":\"#nameInput\",\"ok\":true},{\"action\":\"click\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell:nth-child(2) button\",\"ok\":true},{\"action\":\"assert_text\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell:nth-child(2) .who\",\"expected\":\"Alice Chan\",\"actual\":\"Free\",\"passed\":false,\"ok\":false,\"error\":\"Assertion failed\"}],\"console\":[],\"pageErrors\":[],\"blockedRequests\":[],\"httpErrors\":[],\"chromiumSandbox\":false,\"pages\":[{\"pageId\":\"main\",\"entryPath\":\"app/index.html\"}],\"faults\":[],\"dialogResponses\":[],\"version\":\"156.0.8078.4\",\"error\":\"Browser step failed: Assertion failed\",\"faultCoverageComplete\":true,\"dialogCoverageComplete\":true,\"screenshotCaptured\":true,\"screenshotPageId\":\"main\",\"ok\":false}","test":true,"timedOut":false}},{"id":"call-559cdef81fe249b8","taskId":"task-f30da8398c8e41ae","agentId":"dev","tool":"code.test","args":{"checks":[{"action":"assert_count","count":10,"selector":"#gridBody tr"},{"action":"assert_count","count":12,"selector":"#gridBody tr:first-child td.cell"},{"action":"fill","selector":"#nameInput","value":"Alice Chan"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"assert_text","selector":"#status","text":"Booked Meeting Room 1 on 2026-11-16 at 08:00–09:00 for Alice Chan."},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#status","text":"Only Alice Chan can cancel this booking. Enter the same name to cancel."},{"action":"fill","selector":"#nameInput","value":"Bob Lee"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#status","text":"Only Alice Chan can cancel this booking. Enter the same name to cancel."},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"fill","selector":"#nameInput","value":"Alice Chan"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Free"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"reload"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"assert_text","selector":"#status","text":"Booked Meeting Room 1 on 2026-11-16 at 08:00–09:00 for Alice Chan."}],"language":"browser","path":"app/index.html","resources":{"app.js":"app/app.js","styles.css":"app/styles.css"},"timeout":20},"revision":0,"status":"failed","result":{"browser":{"assertionCount":3,"assertions":[{"action":"assert_count","actual":10,"expected":10,"pageId":"main","passed":true,"selector":"#gridBody tr"},{"action":"assert_count","actual":12,"expected":12,"pageId":"main","passed":true,"selector":"#gridBody tr:first-child td.cell"},{"action":"assert_text","actual":"Free","expected":"Alice Chan","pageId":"main","passed":false,"selector":"#gridBody tr:first-child td.cell:nth-child(2) .who"}],"assertionsPassed":2,"blockedRequests":[],"chromiumSandbox":false,"console":[],"dialogCoverageComplete":true,"dialogResponses":[],"engine":"chromium","entry":{"path":"app/index.html","sha256":"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65"},"error":"Browser step failed: Assertion failed","executed":true,"faultCoverageComplete":true,"faults":[],"httpErrors":[],"ok":false,"pageErrors":[],"pages":[{"entryPath":"app/index.html","pageId":"main"}],"reportPath":"browser-evidence/browser-c1da864870a24c8ca32f1f5a72f55820/report.json","screenshot":{"bytes":85870,"path":"browser-evidence/browser-c1da864870a24c8ca32f1f5a72f55820/screenshot.png","sha256":"85d9a97399aee2900a8611e7f8667862aba88c5a8895a8b70e31053a174ed4c1"},"screenshotCaptured":true,"screenshotPageId":"main","sourceManifest":[{"path":"app/index.html","sha256":"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65"},{"path":"app/styles.css","sha256":"bc467cd1e1a5fce289c46988d9b6b523f18f0a4155ec83cc52676a32cdc25a3b"},{"path":"app/app.js","sha256":"1c3935aa3aeb4585863322240d061e4e5965bd650d8cf85df40ca39a0de3ac7f"}],"steps":[{"action":"assert_count","actual":10,"expected":10,"ok":true,"pageId":"main","passed":true,"selector":"#gridBody tr"},{"action":"assert_count","actual":12,"expected":12,"ok":true,"pageId":"main","passed":true,"selector":"#gridBody tr:first-child td.cell"},{"action":"fill","ok":true,"pageId":"main","selector":"#nameInput"},{"action":"click","ok":true,"pageId":"main","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","actual":"Free","error":"Assertion failed","expected":"Alice Chan","ok":false,"pageId":"main","passed":false,"selector":"#gridBody tr:first-child td.cell:nth-child(2) .who"}],"testedFiles":[{"path":"app/index.html","sha256":"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65"},{"path":"app/styles.css","sha256":"bc467cd1e1a5fce289c46988d9b6b523f18f0a4155ec83cc52676a32cdc25a3b"},{"path":"app/app.js","sha256":"1c3935aa3aeb4585863322240d061e4e5965bd650d8cf85df40ca39a0de3ac7f"}],"version":"156.0.8078.4"},"durationMs":4204,"error":"Browser step failed: Assertion failed","exitCode":1,"isolation":{"containerId":"evo-02374be1406040f2bce12e3ccce8f494","hostFallback":false,"imports":"explicit_read_only","namespaceId":"ns-bbe2052b6e55b50283a4","network":"none","ownerAgentId":"dev","resourceScope":"per_invocation","runId":"run-53ef1b11557546d4","workspace":"private_per_agent_run"},"ok":false,"outputLimitExceeded":false,"runtime":"browser","sandboxId":"evo-02374be1406040f2bce12e3ccce8f494","stderr":"","stdout":"{\"engine\":\"chromium\",\"executed\":true,\"entry\":{\"path\":\"app/index.html\",\"sha256\":\"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65\"},\"testedFiles\":[{\"path\":\"app/index.html\",\"sha256\":\"cb3f43fdff93193fda189f71f5385db36b4eaa3a32144e0ce09c73b088963e65\"},{\"path\":\"app/styles.css\",\"sha256\":\"bc467cd1e1a5fce289c46988d9b6b523f18f0a4155ec83cc52676a32cdc25a3b\"},{\"path\":\"app/app.js\",\"sha256\":\"1c3935aa3aeb4585863322240d061e4e5965bd650d8cf85df40ca39a0de3ac7f\"}],\"assertions\":[{\"action\":\"assert_count\",\"pageId\":\"main\",\"selector\":\"#gridBody tr\",\"expected\":10,\"actual\":10,\"passed\":true},{\"action\":\"assert_count\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell\",\"expected\":12,\"actual\":12,\"passed\":true},{\"action\":\"assert_text\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell:nth-child(2) .who\",\"expected\":\"Alice Chan\",\"actual\":\"Free\",\"passed\":false}],\"assertionCount\":3,\"assertionsPassed\":2,\"steps\":[{\"action\":\"assert_count\",\"pageId\":\"main\",\"selector\":\"#gridBody tr\",\"expected\":10,\"actual\":10,\"passed\":true,\"ok\":true},{\"action\":\"assert_count\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell\",\"expected\":12,\"actual\":12,\"passed\":true,\"ok\":true},{\"action\":\"fill\",\"pageId\":\"main\",\"selector\":\"#nameInput\",\"ok\":true},{\"action\":\"click\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell:nth-child(2) button\",\"ok\":true},{\"action\":\"assert_text\",\"pageId\":\"main\",\"selector\":\"#gridBody tr:first-child td.cell:nth-child(2) .who\",\"expected\":\"Alice Chan\",\"actual\":\"Free\",\"passed\":false,\"ok\":false,\"error\":\"Assertion failed\"}],\"console\":[],\"pageErrors\":[],\"blockedRequests\":[],\"httpErrors\":[],\"chromiumSandbox\":false,\"pages\":[{\"pageId\":\"main\",\"entryPath\":\"app/index.html\"}],\"faults\":[],\"dialogResponses\":[],\"version\":\"156.0.8078.4\",\"error\":\"Browser step failed: Assertion failed\",\"faultCoverageComplete\":true,\"dialogCoverageComplete\":true,\"screenshotCaptured\":true,\"screenshotPageId\":\"main\",\"ok\":false}","test":true,"timedOut":false}}]},"task":{"id":"task-f30da8398c8e41ae","agentId":"dev","revision":0},"action":{"type":"tool","name":"code.test","args":{"checks":[{"action":"assert_count","count":10,"selector":"#gridBody tr"},{"action":"assert_count","count":12,"selector":"#gridBody tr:first-child td.cell"},{"action":"fill","selector":"#nameInput","value":"Alice Chan"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"assert_text","selector":"#status","text":"Booked Meeting Room 1 on 2026-11-16 at 08:00–09:00 for Alice Chan."},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#status","text":"Only Alice Chan can cancel this booking. Enter the same name to cancel."},{"action":"fill","selector":"#nameInput","value":"Bob Lee"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#status","text":"Only Alice Chan can cancel this booking. Enter the same name to cancel."},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"fill","selector":"#nameInput","value":"Alice Chan"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Free"},{"action":"click","selector":"#gridBody tr:first-child td.cell:nth-child(2) button"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"reload"},{"action":"assert_text","selector":"#gridBody tr:first-child td.cell:nth-child(2) .who","text":"Alice Chan"},{"action":"assert_text","selector":"#status","text":"Booked Meeting Room 1 on 2026-11-16 at 08:00–09:00 for Alice Chan."}],"language":"browser","path":"app/index.html","resources":{"app.js":"app/app.js","styles.css":"app/styles.css"},"timeout":20}}}''')

class BrowserFailureFeedbackTests(unittest.TestCase):
    def fixture(self):
        f=copy.deepcopy(ACTUAL_ROUND3);return f['run'],f['task'],f['action']
    def test_actual_round3_wrong_test_replay_rejects_proposal_without_altering_history(self):
        run,task,action=self.fixture();before=copy.deepcopy((run,task,action))
        result=browser_failure_feedback(run,task,action)
        self.assertEqual(result['error']['code'],'repeated_browser_assertion_failure')
        self.assertFalse(result['actionExecuted']);self.assertTrue(result['notExecuted'])
        self.assertEqual(len(result['observedFailures']['callIds']),2)
        assertion=result['observedFailures']['failedAssertions'][0]
        self.assertEqual(assertion['expected'],'Alice Chan');self.assertEqual(assertion['actual'],'Free')
        self.assertEqual(result['pageDate']['status'],'not_recorded_in_this_browser_report')
        self.assertFalse(result['rejectionScope']['allBrowserTestingBlocked'])
        self.assertTrue(result['rejectionScope']['correctedProposalRequiresNormalChecks'])
        self.assertFalse(result['rejectionScope']['newPermissionGranted'])
        corrected=copy.deepcopy(action);corrected['args']['checks'][0]['count']=9
        self.assertIsNone(browser_failure_feedback(run,task,corrected),'a different proposed test must reach normal checks, never inherit a blanket prohibition')
        self.assertEqual((run,task,action),before)
    def test_changed_manifest_mutation_or_different_test_resets_streak(self):
        for variation in ('manifest','write','patch','code','different'):
            with self.subTest(variation=variation):
                run,task,action=self.fixture()
                if variation=='manifest':run['calls'][-1]['result']['browser']['sourceManifest'][0]['sha256']='f'*64
                elif variation=='different':run['calls'][-1]['args']['checks'][0]['count']=11
                else:run['calls'].insert(1,{'id':'mutation','agentId':task['agentId'],'taskId':task['id'],
                    'revision':0,'tool':{'write':'file.write','patch':'file.patch','code':'code.run'}[variation],
                    'status':'failed','args':{},'result':{'ok':False}})
                self.assertIsNone(browser_failure_feedback(run,task,action))
    def test_unknown_success_malformed_or_duplicate_call_is_not_evidence(self):
        for variation in ('unknown','success','manifest','duplicate','unexecuted','actionNotExecuted','booleanRevision'):
            with self.subTest(variation=variation):
                run,task,action=self.fixture();c=run['calls'][-1]
                if variation=='unknown':c['result']['outcomeUnknown']=True
                elif variation=='success':c['status']='success';c['result']['ok']=True
                elif variation=='manifest':c['result']['browser'].pop('sourceManifest')
                elif variation=='duplicate':c['id']=run['calls'][0]['id']
                elif variation=='unexecuted':c['result']['notExecuted']=True
                elif variation=='actionNotExecuted':c['result']['actionExecuted']=False
                else:c['revision']=False
                self.assertIsNone(browser_failure_feedback(run,task,action))
    def test_python_javascript_and_new_browser_setup_keep_original_guard(self):
        for language in ('python','javascript'):
            run,task,action=self.fixture();action['args']['language']=language
            self.assertIsNone(browser_failure_feedback(run,task,action))
        run,task,action=self.fixture();action['args']['checks'][0]['count']=9
        self.assertIsNone(browser_failure_feedback(run,task,action))


class BrowserFixtureTools(Files):
    """Trustworthy offline report fixture; does not claim to run Chromium."""
    def execute(self,agent,run,name,args,approved=False,deadline=None):
        if name!='code.test':return super().execute(agent,run,name,args,approved,deadline)
        body=super().execute(agent,run,'file.read',{'path':args['path']},approved,deadline);sha=body['sha256']
        good=args['checks'][0]['text']=='Real heading';entry={'path':args['path'],'sha256':sha}
        assertion={'action':'assert_text','selector':'h1','pageId':'main',
                   'expected':args['checks'][0]['text'],'actual':'Real heading','passed':good}
        step={**assertion,'ok':good}
        if not good:step['error']='Assertion failed'
        report={'engine':'chromium','executed':True,'ok':good,'entry':entry,'testedFiles':[entry],
                'sourceManifest':[entry],'assertions':[assertion],'steps':[step],
                'assertionCount':1,'assertionsPassed':int(good),'pageErrors':[],
                'blockedRequests':[],'httpErrors':[]}
        return {'ok':good,'runtime':'browser','exitCode':0 if good else 1,'browser':report,'test':True,
                'isolation':{'runId':run,'ownerAgentId':agent},
                'error':None if good else 'Browser step failed: Assertion failed'}


class BrowserFailureRuntimeTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.tmp.cleanup()
    def run_case(self,phase=False):
        config=Config(Path(self.tmp.name),{'LLM_API_KEY':'offline-browser-recovery','MAX_RETRIES':'0'})
        config.apply_time_only_profile();counter={'Build':0,'Check':0}
        def test(path,text):return {'type':'tool','name':'code.test','args':{'language':'browser','path':path,
            'checks':[{'action':'assert_text','selector':'h1','text':text}]}}
        def action(n,context,messages):
            name=context['task']['title'];i=counter[name];counter[name]+=1
            if name=='Check':
                if i==0:
                    self.assertEqual(len(context['dependencyResults']),1)
                    ref=context['authorizedArtifacts'][0]
                    return test(ref['path'],'Real heading')
                return {'type':'final','text':'Independent fixture checked actual granted snapshot; semantic quality unassessed.'}
            if i==0:return {'type':'tool','name':'file.write','args':{'path':'app/index.html','content':'<h1>Real heading</h1>'}}
            if i in (1,2,3):
                if i==3 and phase:
                    run=model.app.state['sessions']['group']['run'];t=next(t for t in run['steps'] if t.get('title')=='Build');now=clock_ms()
                    t['stageControl'].update(allocatedAt=now-1000,synthesisAt=now-500,handoffAt=now-1,expectedHandoffBy=now+10000)
                return test('app/index.html','Wrong fixture expectation')
            if i==4 and phase:
                self.assertTrue(any(m['content'].startswith('浏览器测试失败恢复：') for m in messages))
                self.assertFalse(any(m['content'].startswith('阶段动作结果：') for m in messages))
                return {'type':'final','status':'partial','text':'No artifact was published before the handoff; requirements unmet.'}
            if i==4:
                self.assertTrue(any(m['content'].startswith('浏览器测试失败恢复：') for m in messages))
                return test('app/index.html','Real heading')
            if i==5:return {'type':'publish_artifact','path':'app/index.html'}
            return {'type':'final','text':'Actual HTML snapshot published; independent QA follows.'}
        class Plan(Model):
            def complete(self,messages,max_tokens=None,deadline=None):
                if messages[0]['content'].startswith('Plan an executable task DAG'):
                    return {'content':json.dumps({'tasks':[{'id':'build','title':'Build','agentId':'executor',
                        'outputContract':{'kind':'artifact','extensions':['.html'],'minCount':1}},
                        {'id':'check','title':'Check','agentId':'reviewer','dependsOn':['build']}]}),
                        'finish_reason':'stop','usage':{'total_tokens':1}}
                return super().complete(messages,max_tokens,deadline)
        model=Plan(action);app=Platform(config,provider=model,tools=BrowserFixtureTools(config));model.app=app;self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['executor','reviewer'],'text':'Build an actual artifact and independently inspect it.'})
        self.assertTrue(app.wait_for_idle(5));return app,app.state['sessions']['group']['run']
    def test_error_observation_then_corrected_test_publish_and_authorized_independent_handoff(self):
        app,run=self.run_case();build=next(t for t in run['steps'] if t['title']=='Build');qa=next(t for t in run['steps'] if t['title']=='Check')
        self.assertEqual(build['status'],'done');self.assertEqual(qa['status'],'done')
        self.assertEqual(len(run['artifacts']),1);self.assertEqual(len(build['outputs']),1)
        calls=[c for c in run['calls'] if c['tool']=='code.test' and c['taskId']==build['id']]
        self.assertEqual([c['status'] for c in calls],['failed','failed','success'])
        self.assertEqual(len([e for e in run['events'] if e['type']=='browser_failure_recovery']),1)
        qa_call=next(c for c in run['calls'] if c['tool']=='code.test' and c['taskId']==qa['id'])
        self.assertEqual(qa_call['status'],'success');self.assertTrue(qa_call['args']['path'].startswith('imports/'))
        self.assertFalse(build.get('validation',{}).get('verified'),'fixture handoff is not fabricated formal acceptance')
    def test_handoff_keeps_repeat_failure_guard_and_missing_output_stays_unmet(self):
        app,run=self.run_case(phase=True)
        self.assertEqual(sum(e['type']=='browser_failure_recovery' for e in run['events']),1)
        self.assertFalse(any(e['type']=='stage_action_blocked' for e in run['events']))
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='code.test']),2)
        build=next(t for t in run['steps'] if t['title']=='Build');self.assertNotEqual(build['status'],'done')
        self.assertFalse(build['outputs']);self.assertEqual(build['error']['code'],'output_contract')


if __name__=='__main__':unittest.main()
