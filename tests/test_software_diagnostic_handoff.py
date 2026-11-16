"""Real browser failures through the scheduler, rework, review and immutable ZIPs.

Synthetic products exist only inside temporary test directories. Positive browser
evidence comes from the pinned local Docker runner, never a fabricated pass.
"""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from backend.config import Config
from backend.delivery import delivery_zip
from backend.engine import Platform
from backend.tools import ToolGateway
from tests.test_engine import ScriptedProvider


PREFIX = '浏览器测试失败恢复：'
CONTRACT = {'kind': 'artifact', 'extensions': ['.html'], 'candidateDelivery': True}


class DeadlineScriptedProvider(ScriptedProvider):
    def complete(self, messages, max_tokens=None, deadline=None):
        if deadline is None:
            raise AssertionError('The scripted provider must receive the original absolute deadline.')
        return {**super().complete(messages), 'finish_reason': 'stop'}


def final(status='done'):
    return {'type': 'final', 'status': status,
            'text': 'Report only the actual retained outputs, tests and unresolved failures.'}


def results(messages, prefix='动作的真实结果：'):
    return [json.loads(m['content'][len(prefix):]) for m in messages
            if m.get('content', '').startswith(prefix)]


def write(path, body):
    return {'type': 'tool', 'name': 'file.write', 'args': {'path': path, 'content': body}}


def publish(path):
    return {'type': 'publish_artifacts', 'paths': [path],
            'delivery': {'entrypoint': path, 'instructions': 'Open ' + path + ' in a browser.'}}


def storage_page(fixed=False):
    operation = "localStorage.setItem('records','saved'); document.querySelector('#state').textContent='saved';"
    if fixed:
        operation = "try {" + operation + "} catch (e) {document.querySelector('#state').textContent='storage-error';}"
    return ('<!doctype html><button id="save">Save</button><p id="state">ready</p>'
            '<ul id="records"></ul><script>document.querySelector("#save").onclick=()=>{'
            + operation + '};</script>')


def fault_test(path, fixed=False):
    checks = [{'action': 'storage_write_denied', 'key': 'records', 'enabled': True},
              {'action': 'click', 'selector': '#save'},
              {'action': 'assert_storage_missing', 'key': 'records'},
              {'action': 'assert_count', 'selector': '#records li', 'count': 0}]
    if fixed:
        checks.extend([{'action': 'assert_text', 'selector': '#state', 'text': 'storage-error', 'exact': True},
                       {'action': 'storage_write_denied', 'enabled': False},
                       {'action': 'click', 'selector': '#save'},
                       {'action': 'assert_storage', 'key': 'records', 'value': 'saved'},
                       {'action': 'assert_text', 'selector': '#state', 'text': 'saved', 'exact': True}])
    return {'type': 'tool', 'name': 'code.test', 'args': {
        'language': 'browser', 'path': path, 'checks': checks, 'timeout': 30}}


SELECTOR_PAGE = '''<!doctype html><table><tbody id="items"><tr><th>Item</th>
<td><button data-item="one">Run</button></td></tr></tbody></table><p id="result">idle</p>
<script>document.querySelector('[data-item="one"]').onclick=()=>{
document.querySelector('#result').textContent='ready';};</script>'''


def selector_test(path, correct=False):
    return {'type': 'tool', 'name': 'code.test', 'args': {
        'language': 'browser', 'path': path, 'timeout': 30, 'checks': [
            {'action': 'click', 'selector': '[data-item="one"]' if correct
             else '#items tr:nth-child(1) td:nth-child(1) button'},
            {'action': 'assert_text', 'selector': '#result', 'text': 'ready', 'exact': True}]}}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.shutdown(); app.wait_for_idle(5); app.repo.close()
        self.tmp.cleanup()

    def run_model(self, plan, scripts, participants=None, tools=None, holder=None, values=None, run_seconds=None):
        config = Config(Path(self.tmp.name) / str(len(self.apps)), {
            'LLM_API_KEY': 'offline-diagnostic-fixture', 'MAX_RETRIES': '0', **(values or {})})
        config.apply_time_only_profile()
        if run_seconds is not None:
            config.max_run_seconds = run_seconds
        model = DeadlineScriptedProvider(plan, scripts)
        app = Platform(config, provider=model, tools=(tools or ToolGateway)(config))
        self.apps.append(app)
        if holder is not None:
            holder['app'] = app
        app.action({'action': 'send', 'mode': 'task', 'participants': participants or ['executor'],
                    'text': 'Deliver the explicitly declared temporary browser fixture with honest independent evidence.'})
        self.assertTrue(app.wait_for_idle(60), 'Local scripted test must settle without a live model or approval.')
        run = app.snapshot()['sessions']['group']['run']
        self.assertEqual(run['budgetExtensions'], 0)
        self.assertTrue(all(run['budget'][key] is None for key in ('tokens', 'modelCalls', 'agentTurns')))
        self.assertFalse(any(a['status'] == 'pending' for a in run['approvals']))
        return app, model, run

    def diagnostic_events(self, run):
        return [e for e in run['events'] if e['type'] == 'browser_failure_recovery'
                and e['payload'].get('recovery', {}).get('kind') == 'browser_execution_diagnostic']

    def actual_browser_calls(self, run):
        return [c for c in run['calls'] if c['tool'] == 'code.test']

    def verify_zip(self, app, run, delivery, path, body):
        with zipfile.ZipFile(io.BytesIO(delivery_zip(delivery, run, app.artifacts))) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read(path), body.encode())
            self.assertEqual(json.loads(archive.read('DELIVERY.json'))['status'], delivery['status'])


@unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS') == '1',
                     'Explicit local pinned Docker browser tests')
class SoftwareDiagnosticBrowserTests(Harness):
    def test_repeated_page_error_hands_off_real_rework_fresh_review_and_formal_zip(self):
        plan = [{'id': 'r', 'title': 'Requirements', 'agentId': 'research'},
                {'id': 'b', 'title': 'Build', 'agentId': 'executor', 'dependsOn': ['r'],
                 'outputContract': copy.deepcopy(CONTRACT)},
                {'id': 'q', 'title': 'QA', 'agentId': 'reviewer', 'dependsOn': ['b']},
                {'id': 'p', 'title': 'Package', 'agentId': 'coordinator', 'dependsOn': ['q'],
                 'outputContract': {'kind': 'delivery'}}]
        counts = {}; holder = {}; diagnostics_seen = []

        def build(index, context, messages):
            revised = bool(context['task'].get('feedback'))
            n = counts.get(revised, 0); counts[revised] = n + 1
            path = 'app-v2.html' if revised else 'app-v1.html'
            if revised and n == 0:
                task = next(t for t in holder['app'].state['sessions']['group']['run']['steps']
                            if t['id'] == context['task']['id'])
                diagnostic = task['reworkDiagnostics']['lastTest']
                self.assertEqual(diagnostic['kind'], 'page_errors')
                self.assertEqual(diagnostic['callId'], diagnostics_seen[-1]['diagnostic']['callId'])
                self.assertEqual(diagnostic['actualTestStatus'], 'failed')
            return [write(path, storage_page(revised)), publish(path), final()][n]

        def qa(index, context, messages):
            latest = results(messages)[-1] if results(messages) else {}
            target = next(t for t in context['dependencyResults'] if t['title'] == 'Build')
            if index < 3:
                return fault_test(context['authorizedArtifacts'][0]['path'])
            if index == 3:
                diagnostic = results(messages, PREFIX)[-1]
                diagnostics_seen.append(diagnostic)
                self.assertEqual(diagnostic['error']['code'], 'repeated_browser_execution_failure')
                self.assertFalse(latest['ok'])
                return {'type': 'tool', 'name': 'file.read',
                        'args': {'path': context['authorizedArtifacts'][0]['path']}}
            if index == 4:
                self.assertIn("localStorage.setItem('records'", latest['content'])
                self.assertEqual(context['lastTestDiagnostic']['kind'], 'page_errors')
                self.assertEqual(context['lastTestDiagnostic']['callId'], diagnostics_seen[-1]['diagnostic']['callId'])
                return {'type': 'review', 'taskId': target['id'], 'verdict': 'rework',
                        'feedback': 'The actual denied write throws an uncaught page error. Handle the failure without changing stored records.',
                        'evidenceCallIds': [diagnostics_seen[-1]['diagnostic']['callId']]}
            if 'reworkCompleted' in latest:
                self.assertTrue(latest['ok'], latest)
                return fault_test(latest['authorizedArtifacts'][0]['path'], fixed=True)
            if 'browser' in latest:
                self.assertTrue(latest['ok'], latest)
                return {'type': 'review', 'taskId': target['id'], 'verdict': 'pass',
                        'feedback': 'The new authorized version handles denied writes and normal saving in the real browser.',
                        'evidenceCallIds': [latest['callId']]}
            self.assertEqual(latest.get('validation', {}).get('verdict'), 'pass', latest)
            return final()

        def package(index, context, messages):
            if index:
                self.assertTrue(results(messages)[-1].get('delivery'))
                return final()
            target = next(t for t in context['dependencyResults'] if t['title'] == 'Build')
            artifact = next(a for a in context['authorizedArtifacts'] if a['id'] in target['outputs'])
            return {'type': 'package_delivery', 'artifactIds': target['outputs'],
                    'entrypoint': artifact['sourcePath'], 'instructions': 'Open app-v2.html in a browser.'}

        app, _, run = self.run_model(plan, {'Requirements': [final()], 'Build': build,
            'QA': qa, 'Package': package}, ['coordinator', 'research', 'executor', 'reviewer'], holder=holder,
            values={'MAX_RETRIES': '1'})
        self.assertEqual(run['status'], 'done', run.get('summary'))
        build_task = next(t for t in run['steps'] if t['title'] == 'Build')
        calls = self.actual_browser_calls(run)
        self.assertEqual([c['status'] for c in calls], ['failed', 'failed', 'success'])
        for call in calls[:2]:
            browser = call['result']['browser']
            self.assertTrue(browser['pageErrors'])
            self.assertTrue(all(a['passed'] for a in browser['assertions']))
        self.assertEqual(len(diagnostics_seen), 1)
        self.assertEqual(len(self.diagnostic_events(run)), 1)
        self.assertEqual(build_task['revision'], 1)
        self.assertTrue(build_task['validation']['verified'])
        self.assertEqual(build_task['validation']['targetRevision'], 1)
        self.assertEqual(len([e for e in run['events'] if e['type'] == 'rework_handoff']), 1)
        self.assertTrue(calls[-1]['args']['path'].startswith('imports/'))
        self.assertNotEqual(calls[0]['args']['path'], calls[-1]['args']['path'])
        candidates = [d for d in run['deliveries'] if d['status'] == 'candidate']
        formal = [d for d in run['deliveries'] if d['status'] == 'packaged']
        self.assertEqual((len(candidates), len(formal)), (2, 1))
        for delivery, path, fixed in [(candidates[0], 'app-v1.html', False),
                                      (candidates[1], 'app-v2.html', True),
                                      (formal[0], 'app-v2.html', True)]:
            self.verify_zip(app, run, delivery, path, storage_page(fixed))
        self.assertEqual({f['artifactId'] for f in formal[0]['files']}, set(build_task['outputs']))
        self.assertTrue(all(f['sourceRevision'] == 1 for f in formal[0]['files']))
        self.assertFalse(any(d.get('semanticQuality') == 'passed' for d in candidates))

    def test_selector_timeout_source_read_corrected_selector_and_actual_candidate(self):
        plan = [{'id': 'b', 'title': 'Build', 'agentId': 'executor', 'outputContract': copy.deepcopy(CONTRACT)}]
        captured = {}
        def script(index, context, messages):
            if index == 0:
                return write('app.html', SELECTOR_PAGE)
            if index <= 3:
                return selector_test('app.html')
            if index == 4:
                captured['diagnostic'] = results(messages, PREFIX)[-1]
                return {'type': 'tool', 'name': 'file.read', 'args': {'path': 'app.html'}}
            if index == 5:
                latest = results(messages)[-1]
                self.assertIn('data-item="one"', latest['content'])
                captured['read'] = latest
                self.assertEqual(context['lastTestDiagnostic']['kind'], 'step_execution_error')
                self.assertEqual(context['lastTestDiagnostic']['callId'], captured['diagnostic']['diagnostic']['callId'])
                return selector_test('app.html', correct=True)
            if index == 6:
                self.assertTrue(results(messages)[-1]['ok'])
                return publish('app.html')
            return final()
        app, _, run = self.run_model(plan, {'Build': script})
        self.assertEqual(run['status'], 'done', run.get('summary'))
        calls = self.actual_browser_calls(run)
        self.assertEqual([c['status'] for c in calls], ['failed', 'failed', 'success'])
        for call in calls[:2]:
            report = call['result']['browser']
            self.assertFalse(any(a.get('passed') is False for a in report['assertions']))
            self.assertFalse(report['steps'][-1]['ok'])
            self.assertTrue(report['steps'][-1]['error'])
        self.assertEqual(len(self.diagnostic_events(run)), 1)
        self.assertEqual(captured['diagnostic']['error']['code'], 'repeated_browser_execution_failure')
        self.assertTrue(captured['read']['sha256'])
        self.assertEqual(calls[-1]['args']['checks'][0]['selector'], '[data-item="one"]')
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate'])
        self.verify_zip(app, run, run['deliveries'][0], 'app.html', SELECTOR_PAGE)
        self.assertFalse(any(e['type'] == 'review_pass' for e in run['events']))
        self.assertFalse(run['steps'][-1].get('validation', {}).get('verified'))

    def disregarding_model(self, closing='partial'):
        plan = [{'id': 'b', 'title': 'Build', 'agentId': 'executor', 'outputContract': copy.deepcopy(CONTRACT)}]
        final_requests = []
        def script(index, context, messages):
            if index == 0:
                return write('bad.html', storage_page())
            if index == 1:
                return publish('bad.html')
            feedback = results(messages, PREFIX)
            if feedback and feedback[-1].get('recovery', {}).get('requireFinal'):
                final_requests.append(copy.deepcopy(messages))
                if closing == 'tool':
                    return write('must-not-exist.txt', 'Forbidden extra work')
                if closing == 'malformed':
                    return {'type': 'final', 'status': 'partial', 'text': ''}
                if closing == 'done':
                    return {'type': 'final', 'status': 'done',
                            'text': 'All required behavior is complete and all browser tests pass.'}
                return {'type': 'final', 'status': 'partial',
                        'text': 'The published bad.html candidate is retained. Browser failures '
                        + ', '.join(feedback[-1]['observedFailures']['callIds'])
                        + ' show an uncaught denied-write error despite passing assertions. '
                        'The fault remains unresolved; no independent acceptance or formal release is claimed.'}
            return fault_test('bad.html')
        return plan, script, final_requests

    def test_bounded_disregard_requires_actual_partial_final_and_keeps_candidate(self):
        plan, script, final_requests = self.disregarding_model()
        app, model, run = self.run_model(plan, {'Build': script})
        self.assertEqual(run['status'], 'partial')
        self.assertEqual(len(final_requests), 1)
        self.assertEqual(len(self.actual_browser_calls(run)), 2)
        events = self.diagnostic_events(run)
        self.assertEqual(len(events), 3)
        self.assertTrue(events[-1]['payload']['recovery']['requireFinal'])
        self.assertLess(model.counts['Build'], 12)
        task = next(t for t in run['steps'] if t['title'] == 'Build')
        self.assertEqual(task['closureReason'], 'browser_diagnostic_unresolved')
        self.assertNotIn('partialOutput', task)
        self.assertFalse(any(e['type'] == 'completion_scope_clarification' for e in run['events']))
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate'])
        self.verify_zip(app, run, run['deliveries'][0], 'bad.html', storage_page())
        self.assertFalse(any(e['type'] == 'review_pass' for e in run['events']))

    def test_nonfinal_after_bounded_disregard_is_not_executed_or_fabricated_done(self):
        plan, script, final_requests = self.disregarding_model(closing='tool')
        app, model, run = self.run_model(plan, {'Build': script})
        self.assertEqual(len(final_requests), 1)
        self.assertIn(run['status'], ('partial', 'failed'))
        self.assertEqual(len(self.actual_browser_calls(run)), 2)
        self.assertEqual(len(self.diagnostic_events(run)), 3)
        self.assertLess(model.counts['Build'], 12)
        self.assertFalse((app.tools.workspace('executor', run['id']) / 'must-not-exist.txt').exists())
        self.assertFalse(any(c['tool'] == 'file.write' and c['args']['path'] == 'must-not-exist.txt'
                             for c in run['calls']))
        self.assertFalse(any(e['type'] == 'review_pass' for e in run['events']))
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate'])

    def test_malformed_final_after_diagnostic_checkpoint_stops_without_another_request(self):
        plan, script, final_requests = self.disregarding_model(closing='malformed')
        _, model, run = self.run_model(plan, {'Build': script})
        self.assertEqual(len(final_requests), 1)
        self.assertEqual(run['status'], 'partial')
        self.assertEqual(len(self.actual_browser_calls(run)), 2)
        self.assertEqual(len(self.diagnostic_events(run)), 3)
        self.assertLess(model.counts['Build'], 12)
        task = next(t for t in run['steps'] if t['title'] == 'Build')
        self.assertEqual(task['status'], 'failed')
        self.assertFalse(task.get('result'))
        self.assertFalse(any(e['type'] == 'review_pass' for e in run['events']))
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate'])

    def test_false_done_after_diagnostic_checkpoint_preserves_actual_failure_and_partial(self):
        plan, script, final_requests = self.disregarding_model(closing='done')
        _, model, run = self.run_model(plan, {'Build': script})
        self.assertEqual(len(final_requests), 1)
        self.assertEqual(run['status'], 'partial')
        self.assertEqual(len(self.actual_browser_calls(run)), 2)
        self.assertEqual(len(self.diagnostic_events(run)), 3)
        self.assertLess(model.counts['Build'], 12)
        task = next(t for t in run['steps'] if t['title'] == 'Build')
        self.assertEqual(task['status'], 'partial')
        self.assertEqual(task['closureReason'], 'browser_diagnostic_unresolved')
        self.assertNotIn('partialOutput', task)
        self.assertFalse(any(e['type'] == 'completion_scope_clarification' for e in run['events']))
        self.assertFalse(task.get('validation', {}).get('verified'))
        self.assertFalse(any(e['type'] == 'review_pass' for e in run['events']))
        self.assertEqual([d['status'] for d in run['deliveries']], ['candidate'])


class NotExecutedTools(ToolGateway):
    """Only negative unknown/timeout receipts; never returns browser success."""
    def capabilities(self):
        return {}

    def execute(self, agent, run, name, args, approved=False, deadline=None):
        if name != 'code.test':
            return super().execute(agent, run, name, args, approved, deadline)
        return {'ok': False, 'test': True, 'runtime': 'browser', 'exitCode': 1,
                'notExecuted': True, 'timedOut': True,
                'browser': {'engine': 'chromium', 'executed': False, 'ok': False},
                'error': 'Explicit negative fixture: no browser execution evidence.'}


class SoftwareDiagnosticBoundaryTests(Harness):
    def test_unexecuted_timeout_does_not_become_product_diagnosis_or_acceptance(self):
        plan = [{'id': 'b', 'title': 'Build', 'agentId': 'executor'}]
        actions = [write('app.html', storage_page()), fault_test('app.html'), fault_test('app.html'), final('partial')]
        _, _, run = self.run_model(plan, {'Build': actions}, tools=NotExecutedTools)
        self.assertEqual(run['status'], 'partial')
        self.assertEqual(len(self.actual_browser_calls(run)), 2)
        self.assertFalse(self.diagnostic_events(run))
        self.assertFalse(run.get('deliveries'))
        self.assertFalse(any(e['type'] in ('review_pass', 'rework_handoff') for e in run['events']))

    def test_unknown_browser_outcome_stays_unsettled_without_diagnosis_or_automatic_replay(self):
        class UnknownTools(NotExecutedTools):
            actual_calls = 0
            def execute(self, agent, run, name, args, approved=False, deadline=None):
                if name == 'code.test':
                    self.actual_calls += 1
                    return {'ok': False, 'outcomeUnknown': True,
                            'error': 'Explicit unknown receipt; no executed-browser result is available.'}
                return super().execute(agent, run, name, args, approved, deadline)
        config = Config(Path(self.tmp.name), {'LLM_API_KEY': 'offline-unknown-diagnostic', 'MAX_RETRIES': '0'})
        config.apply_time_only_profile()
        model = DeadlineScriptedProvider([{'id': 'b', 'title': 'Build', 'agentId': 'executor'}],
                                 {'Build': lambda n, c, m: fault_test('unknown.html')})
        gateway = UnknownTools(config)
        app = Platform(config, provider=model, tools=gateway); self.apps.append(app)
        app.action({'action': 'send', 'mode': 'task', 'participants': ['executor'],
                    'text': 'Preserve the unknown browser outcome without claiming a test result.'})
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if app.snapshot()['sessions']['group']['run']['status'] == 'interrupted':
                break
            time.sleep(.02)
        run = app.state['sessions']['group']['run']
        self.assertEqual(run['status'], 'interrupted')
        calls = self.actual_browser_calls(run)
        self.assertEqual(gateway.actual_calls, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['status'], 'unknown')
        task = next(t for t in run['steps'] if t['title'] == 'Build')
        self.assertEqual(task['_uncertainCalls'], [calls[0]['id']])
        self.assertEqual(app.repo.effect(task['_pendingTool']['effectKey'])['status'], 'reserved')
        self.assertFalse(self.diagnostic_events(run))
        self.assertFalse(run.get('deliveries'))
        self.assertEqual(run['budgetExtensions'], 0)

    def test_original_global_final_only_still_forbids_browser_test_execution(self):
        plan = [{'id': 'b', 'title': 'Build', 'agentId': 'executor'}]
        def script(index, context, messages):
            self.assertEqual(context['progressReceipt']['executionPhase'], 'final_only')
            return fault_test('never-created.html')
        _, _, run = self.run_model(plan, {'Build': script}, tools=NotExecutedTools, run_seconds=45)
        self.assertIn(run['status'], ('partial', 'failed'))
        self.assertFalse(self.actual_browser_calls(run))
        self.assertFalse(self.diagnostic_events(run))
        task = next(t for t in run['steps'] if t['title'] == 'Build')
        self.assertEqual(task['finalizationFailure']['code'], 'final_action_required')
        self.assertFalse(run.get('deliveries'))


if __name__ == '__main__':
    unittest.main()
