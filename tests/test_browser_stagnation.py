"""Replay immutable failures and exercise the real Platform decision loop offline."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.browser_failure_recovery import browser_failure_feedback
from backend.browser_stagnation import browser_stagnation_feedback, _failed_check
from backend.config import Config
from backend.engine import Platform
from tests.test_time_only_runtime import Files, Model

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/r9_browser_stagnation.json').read_text())


class BrowserStagnationTests(unittest.TestCase):
    def fixture(self):
        data = copy.deepcopy(FIXTURE)
        return data['run'], data['task'], data['proposal']

    def test_actual_r9_variations_share_failure_not_whole_args(self):
        run, task, action = self.fixture(); before = copy.deepcopy((run, task, action))
        self.assertNotEqual(run['calls'][0]['args'], run['calls'][1]['args'])
        self.assertIsNone(browser_failure_feedback(run, task, action))
        result = browser_stagnation_feedback(run, task, action)
        self.assertEqual(result['observedFailures']['count'], 2)
        failure = result['observedFailures']['failedAssertions'][0]
        self.assertEqual((failure['selector'], failure['expected'], failure['actual']), ('#dashCharts .chart-card:nth-child(1) .bar-value >> nth=3', '1', '0'))
        self.assertTrue(result['notExecuted']); self.assertFalse(result['actionExecuted'])
        self.assertFalse(result['rejectionScope']['allBrowserTestingBlocked'])
        self.assertFalse(result['rejectionScope']['newPermissionGranted'])
        self.assertFalse(result['recovery']['stopTask'])
        self.assertEqual((run, task, action), before)

    def test_irrelevant_assertions_and_later_steps_do_not_reset(self):
        for mode in ('prepend', 'append', 'later_state_change'):
            run, task, action = self.fixture()
            extra = {'action':'assert_count', 'selector':'#unrelated', 'count':42}
            if mode == 'prepend': action['args']['checks'].insert(0, extra)
            elif mode == 'append': action['args']['checks'].append(extra)
            else: action['args']['checks'].append({'action':'click', 'selector':'#later'})
            self.assertIsNotNone(browser_stagnation_feedback(run, task, action), mode)

    def test_corrected_check_or_stateful_setup_remains_executable(self):
        run, task, action = self.fixture()
        index = browser_stagnation_feedback(run, task, action)['rejectionScope']['proposalFailedCheckIndex']
        for field, value in [('selector', '.bar-value >> nth=4'), ('text', '2'), ('exact', True)]:
            changed = copy.deepcopy(action); changed['args']['checks'][index][field] = value
            self.assertIsNone(browser_stagnation_feedback(run, task, changed), field)
        for field, value in [('timeout', 30), ('storage', {'new-key':'value'}),
                             ('path', 'another.html'), ('resources', {'new.js':'new.js'})]:
            changed = copy.deepcopy(action); changed['args'][field] = value
            self.assertIsNone(browser_stagnation_feedback(run, task, changed), field)
        for prep in [{'action':'fill', 'selector':'#date', 'value':'2027-01-01'},
                     {'action':'click', 'selector':'#reset'}, {'action':'reload'},
                     {'action':'capture_canvas', 'selector':'canvas', 'name':'before'}]:
            changed = copy.deepcopy(action); changed['args']['checks'].insert(0, prep)
            self.assertIsNone(browser_stagnation_feedback(run, task, changed), prep)

    def test_source_version_and_owner_mutations_reset(self):
        for mode in ('sha', 'file.write', 'file.patch', 'file.delete', 'code.run', 'python', 'other_task'):
            run, task, action = self.fixture()
            if mode == 'sha':
                report = run['calls'][-1]['result']['browser']
                path = report['entry']['path']
                report['entry']['sha256'] = 'f' * 64
                for key in ('sourceManifest', 'testedFiles'):
                    for row in report[key]:
                        if row['path'] == path: row['sha256'] = 'f' * 64
            elif mode == 'other_task': run['calls'][-1]['taskId'] = 'other-task'
            else:
                run['calls'].insert(1, {'id':'mutation','agentId':task['agentId'],'taskId':'another-owner-task',
                    'revision':0,'tool':'code.test' if mode == 'python' else mode,'status':'failed',
                    'args':{'language':'python'} if mode == 'python' else {},'result':{'ok':False}})
            self.assertIsNone(browser_stagnation_feedback(run, task, action), mode)

    def test_untrusted_unknown_incomplete_and_wrongly_bound_evidence_does_not_block(self):
        modes = ('unknown','nested_unknown','success','missing_manifest','missing_steps','wrong_step',
                 'wrong_expected','wrong_step_index','wrong_page','duplicate','unexecuted','not_action',
                 'boolean_revision','other_agent','other_revision','other_run','pending')
        for mode in modes:
            run, task, action = self.fixture(); call = run['calls'][-1]; result = call['result']; report = result['browser']
            if mode == 'unknown': result['outcomeUnknown'] = True
            elif mode == 'nested_unknown': call['transport'] = {'outcomeUnknown':True}
            elif mode == 'success': call['status'] = 'success'; result['ok'] = True
            elif mode == 'missing_manifest': report.pop('sourceManifest')
            elif mode == 'missing_steps': report.pop('steps')
            elif mode == 'wrong_step': report['steps'][-1]['selector'] = '#foreign'
            elif mode == 'wrong_expected': report['assertions'][-1]['expected'] = 'bogus'
            elif mode == 'wrong_step_index': report['failureObservation']['stepIndex'] = 999
            elif mode == 'wrong_page': report['steps'][-1]['pageId'] = 'other'
            elif mode == 'duplicate': call['id'] = run['calls'][0]['id']
            elif mode == 'unexecuted': result['notExecuted'] = True
            elif mode == 'not_action': result['actionExecuted'] = False
            elif mode == 'boolean_revision': call['revision'] = False
            elif mode == 'other_agent': call['agentId'] = 'foreign'
            elif mode == 'other_revision': call['revision'] = 1
            elif mode == 'other_run': call['runId'] = 'foreign'
            else: run['steps'] = [{**task, '_pendingTool':{'name':'file.write'}}]
            self.assertIsNone(browser_stagnation_feedback(run, task, action), mode)

    def test_finite_escalation_is_persistent_scoped_and_never_adds_calls(self):
        run, task, action = self.fixture(); call_count = len(run['calls'])
        for number in (1, 2, 3):
            result = browser_stagnation_feedback(run, task, action)
            self.assertEqual(result['recovery']['blockedProposalCount'], number)
            self.assertEqual(result['recovery']['stopTask'], number == 3)
            self.assertEqual(result['observedFailures']['count'], 2)
            event = {'id':f'e{number}','type':'browser_failure_recovery','agentId':task['agentId'],
                'payload':{'taskId':task['id'],'failureSignature':result['failureSignature']}}
            run['events'].extend([event, copy.deepcopy(event)])  # Same persisted event is not two proposals.
            run['events'].append({**event,'id':f'foreign{number}','agentId':'other'})
        self.assertEqual(len(run['calls']), call_count)
        run['events'][0]['payload']['failureSignature'] = 'stale-source-or-revision'
        run['events'] = run['events'][:1]
        self.assertEqual(browser_stagnation_feedback(run, task, action)['recovery']['blockedProposalCount'], 1)

    def test_only_actual_executed_failures_count(self):
        run, task, action = self.fixture(); run['calls'] = run['calls'][:1]
        self.assertIsNone(browser_stagnation_feedback(run, task, action))
        run, task, action = self.fixture()
        success = copy.deepcopy(run['calls'][-1]); success['id'] = 'success'; success['status'] = 'success'; success['result']['ok'] = True
        run['calls'].append(success)
        self.assertIsNone(browser_stagnation_feedback(run, task, action))


class ExecutedPrefixFixtureTools(Files):
    """Offline executed-prefix fixture bound to real files; never claims live Chromium QA."""
    def execute(self, agent, run, name, args, approved=False, deadline=None):
        if name != 'code.test': return super().execute(agent, run, name, args, approved, deadline)
        body = super().execute(agent, run, 'file.read', {'path':args['path']}, approved, deadline)
        entry = {'path':args['path'], 'sha256':body['sha256']}; steps = []; assertions = []; good = True
        for check in args['checks']:
            actual = 'Real heading' if check['selector'] == 'h1' else 'unchanged'
            passed = check['text'] == actual
            assertion = {'action':'assert_text','pageId':'main','selector':check['selector'],
                         'expected':check['text'],'actual':actual,'passed':passed}
            assertions.append(assertion); steps.append({**assertion,'ok':passed})
            if not passed: good = False; break
        report = {'engine':'chromium','executed':True,'ok':good,'entry':entry,'testedFiles':[entry],
                  'sourceManifest':[entry],'assertions':assertions,'steps':steps}
        return {'ok':good,'runtime':'browser','exitCode':0 if good else 1,'browser':report,
                'error':None if good else 'Offline browser assertion fixture failed'}


class BrowserStagnationPlatformTests(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory(); self.apps = []
    def tearDown(self):
        for app in self.apps: app.shutdown(); app.wait_for_idle(3); app.repo.close()
        self.tmp.cleanup()

    def run_case(self, correct):
        config = Config(Path(self.tmp.name), {'LLM_API_KEY':'offline-stagnation','MAX_RETRIES':'0'})
        config.apply_time_only_profile(); counters = {'Build':0,'Check':0}
        def browser(path, wrong, variant):
            return {'type':'tool','name':'code.test','args':{'language':'browser','path':path,'checks':[
                {'action':'assert_text','selector':'h1','text':'Wrong expectation' if wrong else 'Real heading'},
                {'action':'assert_text','selector':f'#irrelevant-{variant}','text':'unchanged'}]}}
        def action(n, context, messages):
            title = context['task']['title']; i = counters[title]; counters[title] += 1
            if title == 'Check':
                if i == 0: return browser(context['authorizedArtifacts'][0]['path'], False, i)
                return {'type':'final','text':'Independent offline report fixture inspected granted HTML; live browser and semantic product acceptance unassessed.'}
            if i == 0: return {'type':'tool','name':'file.write','args':{'path':'app/index.html','content':'<h1>Real heading</h1>'}}
            if i <= 3 or not correct: return browser('app/index.html', True, i)
            if i == 4:
                feedback = [m for m in messages if m['content'].startswith('浏览器测试失败恢复：')]
                self.assertTrue(feedback)
                self.assertIn('same_failing_check_and_stateful_setup', feedback[-1]['content'])
                return browser('app/index.html', False, i)
            if i == 5: return {'type':'publish_artifact','path':'app/index.html'}
            return {'type':'final','text':'Actual HTML snapshot published for independent fixture inspection.'}
        class Plan(Model):
            def complete(self, messages, max_tokens=None, deadline=None):
                if messages[0]['content'].startswith('Plan an executable task DAG'):
                    return {'content':json.dumps({'tasks':[
                        {'id':'build','title':'Build','agentId':'executor','outputContract':{'kind':'artifact','extensions':['.html'],'minCount':1}},
                        {'id':'check','title':'Check','agentId':'reviewer','dependsOn':['build']}]}),
                        'finish_reason':'stop','usage':{'total_tokens':1}}
                return super().complete(messages,max_tokens,deadline)
        model = Plan(action); app = Platform(config, provider=model, tools=ExecutedPrefixFixtureTools(config)); model.app = app; self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['executor','reviewer'],'text':'Build and independently inspect actual artifact.'})
        self.assertTrue(app.wait_for_idle(8)); return app, app.state['sessions']['group']['run']

    def test_feedback_corrected_test_publish_and_independent_authorized_handoff(self):
        _, run = self.run_case(True); build = next(t for t in run['steps'] if t['title']=='Build'); qa = next(t for t in run['steps'] if t['title']=='Check')
        self.assertEqual((build['status'],qa['status']), ('done','done'))
        self.assertEqual([c['status'] for c in run['calls'] if c['tool']=='code.test' and c['taskId']==build['id']], ['failed','failed','success'])
        events = [e for e in run['events'] if e['type']=='browser_failure_recovery']
        self.assertEqual(len(events), 1); self.assertEqual(events[0]['payload']['recovery']['blockedProposalCount'],1)
        self.assertEqual(len(run['artifacts']),1)
        qa_call = next(c for c in run['calls'] if c['tool']=='code.test' and c['taskId']==qa['id'])
        self.assertTrue(qa_call['args']['path'].startswith('imports/')); self.assertEqual(qa_call['status'],'success')
        self.assertFalse(build.get('validation',{}).get('verified'))

    def test_ignored_feedback_ends_honestly_without_more_browser_execution_or_artifact(self):
        _, run = self.run_case(False); build = next(t for t in run['steps'] if t['title']=='Build')
        self.assertEqual(build['status'],'failed'); self.assertEqual(build['error']['code'],'protocol')
        self.assertEqual(sum(c['tool']=='code.test' for c in run['calls']),2)
        events = [e for e in run['events'] if e['type']=='browser_failure_recovery']
        self.assertEqual([e['payload']['recovery']['blockedProposalCount'] for e in events],[1,2,3])
        self.assertTrue(events[-1]['payload']['recovery']['stopTask'])
        self.assertFalse(run['artifacts']); self.assertFalse(build['outputs'])
        self.assertFalse(build.get('validation',{}).get('verified'))
        self.assertFalse(run.get('budgetExtensions'))


if __name__ == '__main__': unittest.main()
