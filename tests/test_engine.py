"""Independent scheduler tests with scripted JSON and inert tools; no API/Docker."""
from collections import defaultdict
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.agents import AGENTS
from backend.config import Config
from backend.engine import Platform, ActionError
from backend.provider import ProviderError
from backend.runtime import fingerprint


def final(text='delivered', **extra):
    return {'type': 'final', 'status': 'done', 'text': text, **extra}


def task(title, agent, dependencies=(), required=()):
    return {'id': title, 'title': title, 'goal': title, 'agentId': agent,
            'dependsOn': list(dependencies), 'requiredCapabilities': list(required),
            'acceptanceCriteria': ['return an observable result']}


class ScriptedProvider:
    def __init__(self, plan, scripts=None):
        self.plan = {'summary': 'scripted plan', 'tasks': plan}
        self.scripts = scripts or {}
        self.counts = defaultdict(int)
        self.records = []
        self.lock = threading.Lock()

    def complete(self, messages):
        if 'Plan an executable task DAG' in messages[0]['content']:
            return {'content': json.dumps(self.plan), 'usage': {'total_tokens': 1}}
        context = None
        for message in messages:
            try:
                value = json.loads(message['content'])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and isinstance(value.get('task'), dict):
                context = value
        if context is None:
            return {'content': 'chat reply', 'usage': {'total_tokens': 1}}
        title = context['task']['title']
        with self.lock:
            index = self.counts[title]
            self.counts[title] += 1
            self.records.append({'title': title, 'index': index, 'time': time.monotonic(), 'context': copy.deepcopy(context)})
        script = self.scripts.get(title, [])
        if callable(script):
            value = script(index, context, messages)
        else:
            value = script[index] if index < len(script) else final(title + ' complete')
            if callable(value):
                value = value(index, context, messages)
        if isinstance(value, Exception):
            raise value
        return {'content': value if isinstance(value, str) else json.dumps(value), 'usage': {'total_tokens': 1}}


class FakeTools:
    def __init__(self, handler=None):
        self.handler = handler
        self.calls = []
        self.cancelled = []
        self.lock = threading.Lock()

    def capabilities(self):
        return {'dockerAvailable': False, 'dockerImageReady': False, 'gitInstalled': False}

    def definitions(self, agent):
        return [{'name': name, 'description': 'inert unit-test tool',
                 'parameters': {'type': 'object'}, 'idempotent': name in ('file.read', 'file.write')}
                for name in AGENTS[agent]['tools']]

    def execute(self, agent, run_id, name, args, approved=False):
        assert name in AGENTS[agent]['tools'], 'engine must preserve actor tool permissions'
        with self.lock:
            self.calls.append({'agent': agent, 'runId': run_id, 'name': name, 'args': copy.deepcopy(args), 'approved': approved})
        if self.handler:
            return self.handler(agent, run_id, name, args, approved)
        if name in ('file.delete', 'file.patch', 'git.push') and not approved:
            return {'ok': False, 'needsApproval': True, 'args': copy.deepcopy(args),
                    'approval': {'title': 'Approve inert write?', 'description': 'No real side effect.'}}
        return {'ok': True, 'exitCode': 0, 'text': 'inert result'}

    def cancel(self, run_id):
        self.cancelled.append(run_id)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name), {'LLM_API_KEY': 'unit-test-not-a-real-key',
                            'MAX_RETRIES': '0', 'MAX_MODEL_CALLS': '60', 'MAX_RUN_TOKENS': '200000',
                            'MAX_AGENT_TURNS': '12', 'PARALLEL_TASKS': '2'})
        self.apps = []
        self.release_events = []

    def tearDown(self):
        for release in self.release_events:
            release.set()
        for app in self.apps:
            app.shutdown()
        for app in self.apps:
            app.wait_for_idle(4)
            app.repo.close()
        self.temp.cleanup()

    def app(self, provider, tools=None):
        app = Platform(self.config, provider=provider, tools=tools or FakeTools())
        self.apps.append(app)
        return app

    def send(self, app, participants=None, **extra):
        # Generic scheduler cases select their actual roles; all-four completion is
        # independently covered by the dedicated four-agent contract/integration suite.
        if participants is None:
            planned={item.get('agentId') for item in app.provider.plan.get('tasks',[])}
            planned.add('research')  # permits the explicit child handoff and recovery candidate cases
            participants=[agent for agent in AGENTS if agent in planned]
        return app.action({'action': 'send', 'channelId': 'group', 'text': 'scheduler test',
                           'participants': participants, 'mode': 'task', **extra})

    def current_run(self, app):
        return app.snapshot()['sessions']['group']['run']

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.01)
        self.fail('timed out waiting for scheduler condition')

    def pending(self, app, kind=None):
        def find():
            run = self.current_run(app)
            if run:
                return next((item for item in run['approvals'] if item['status'] == 'pending' and (kind is None or item['type'] == kind)), None)
        return self.wait(find)

    def answer(self, app, approval, choice, **extra):
        return app.action({'action': 'approval', 'channelId': 'group', 'runId': self.current_run(app)['id'],
                           'approvalId': approval['id'], 'choiceId': choice, **extra})

    def test_independent_dag_tasks_overlap_and_dependency_waits_for_both(self):
        barrier = threading.Barrier(2)
        entered = []
        lock = threading.Lock()
        def parallel(index, context, messages):
            with lock:
                entered.append(context['task']['title'])
            barrier.wait(timeout=2)
            return final(context['task']['title'] + ' done')
        plan = [task('research-first', 'research', required=['research']),
                task('execute-first', 'executor', required=['coding']),
                task('merge-after', 'reviewer', ['research-first', 'execute-first'], ['review'])]
        provider = ScriptedProvider(plan, {'research-first': parallel, 'execute-first': parallel})
        app = self.app(provider)
        self.send(app)
        self.assertTrue(app.wait_for_idle(4))
        run = self.current_run(app)
        self.assertEqual(run['status'], 'done')
        self.assertEqual(set(entered), {'research-first', 'execute-first'})
        merge = next(item for item in provider.records if item['title'] == 'merge-after')
        self.assertEqual(len(merge['context']['dependencyResults']), 2)
        self.assertTrue(all(item['result'].endswith(' done') for item in merge['context']['dependencyResults']))
        self.assertTrue(all(step['status'] == 'done' for step in run['steps']))

    def test_failed_dependency_is_skipped_without_running_dependent_agent(self):
        provider = ScriptedProvider([task('fails', 'executor', required=['coding']),
                                     task('must-not-start', 'reviewer', ['fails'], ['review'])],
                                    {'fails': [ProviderError('invalid course key', 'authentication', False)]})
        app = self.app(provider)
        self.send(app)
        self.assertTrue(app.wait_for_idle(3))
        run = self.current_run(app)
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(next(step for step in run['steps'] if step['title'] == 'must-not-start')['status'], 'skipped')
        self.assertEqual(provider.counts['must-not-start'], 0)
        self.assertFalse(run['artifacts'])
        self.assertTrue(any(call['tool'] == 'llm.chat' and call['status'] == 'failed' for call in run['calls']))

    def test_permission_is_bound_and_denial_never_executes_approved_tool(self):
        tools = FakeTools()
        provider = ScriptedProvider([task('remove', 'executor', required=['coding'])],
                                    {'remove': [{'type': 'tool', 'name': 'file.delete', 'args': {'path': 'old.txt'}}, final('request denied; retained local state', status='partial')]})
        app = self.app(provider, tools)
        self.send(app)
        approval = self.pending(app, 'permission')
        with self.assertRaises(ActionError):
            app.action({'action': 'approval', 'channelId': 'research', 'approvalId': approval['id'], 'choiceId': 'allow'})
        with self.assertRaises(ActionError):
            app.action({'action': 'approval', 'channelId': 'group', 'runId': 'wrong-run', 'approvalId': approval['id'], 'choiceId': 'allow'})
        reason='Keep the existing data.js API; read its current interface before extending it. '+self.config.api_key
        self.answer(app, approval, 'deny', answer=reason)
        self.assertTrue(app.wait_for_idle(3))
        self.assertFalse(any(call['approved'] for call in tools.calls))
        denied=next(call for call in self.current_run(app)['calls'] if call['tool'] == 'file.delete')
        self.assertEqual(denied['status'], 'denied')
        self.assertEqual(denied['result']['approvalFeedback'],self.config.scrub(reason))
        self.assertNotIn(self.config.api_key,denied['result']['approvalFeedback'])
        saved_task=next(step for step in app.state['sessions']['group']['run']['steps'] if step['title']=='remove')
        actual_results=[json.loads(m['content'][len('动作的真实结果：'):]) for m in saved_task['_messages']
                        if m['content'].startswith('动作的真实结果：')]
        self.assertEqual(actual_results[0]['approvalFeedback'],self.config.scrub(reason))
        self.assertTrue(actual_results[0]['denied']);self.assertFalse(actual_results[0]['ok'])
        with self.assertRaises(ActionError):
            self.answer(app, approval, 'allow')

    def test_modified_permission_requires_a_new_approval_before_side_effect(self):
        tools = FakeTools()
        provider = ScriptedProvider([task('remove', 'executor', required=['coding'])],
                                    {'remove': [{'type': 'tool', 'name': 'file.delete', 'args': {'path': 'original.txt'}}, final()]})
        app = self.app(provider, tools)
        self.send(app)
        old = self.pending(app, 'permission')
        self.answer(app, old, 'modify', modifiedArgs={'path': 'modified.txt'})
        new = self.pending(app, 'permission')
        self.assertNotEqual(old['id'], new['id'])
        self.assertEqual(new['binding']['args']['path'], 'modified.txt')
        self.assertFalse(any(call['approved'] for call in tools.calls))
        with self.assertRaises(ActionError):
            self.answer(app, old, 'allow')
        self.answer(app, new, 'allow')
        self.assertTrue(app.wait_for_idle(3))
        approved = [call for call in tools.calls if call['approved']]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]['args']['path'], 'modified.txt')

    def test_reset_discards_late_tool_result_without_resurrecting_task(self):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        def blocking(agent, run_id, name, args, approved):
            entered.set(); release.wait(3)
            return {'ok': True, 'text': 'late side-effect result'}
        tools = FakeTools(blocking)
        provider = ScriptedProvider([task('slow-tool', 'executor', required=['coding'])],
                                    {'slow-tool': [{'type': 'tool', 'name': 'file.read', 'args': {'path': 'input.txt'}}, final()]})
        app = self.app(provider, tools)
        self.send(app)
        self.assertTrue(entered.wait(2))
        old_id = self.current_run(app)['id']
        app.action({'action': 'reset', 'channelId': 'group'})
        release.set()
        self.assertTrue(app.wait_for_idle(3))
        self.assertIsNone(self.current_run(app))
        self.assertEqual(app.snapshot()['sessions']['group']['messages'], [])
        self.assertEqual(app.repo.get_run(old_id)['status'], 'cancelled')
        self.assertIn(old_id, tools.cancelled)
        self.assertEqual(provider.counts['slow-tool'], 1, 'no next model action after cancellation')

    def test_pause_holds_new_tool_and_model_actions_until_resumed(self):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        def first(index, context, messages):
            entered.set(); release.wait(3)
            return {'type': 'tool', 'name': 'file.read', 'args': {'path': 'input.txt'}}
        provider = ScriptedProvider([task('pause-target', 'executor', required=['coding'])],
                                    {'pause-target': [first, final()]})
        tools = FakeTools()
        app = self.app(provider, tools)
        self.send(app)
        self.assertTrue(entered.wait(2))
        app.action({'action': 'pause', 'channelId': 'group'})
        release.set()
        time.sleep(.1)
        self.assertEqual(self.current_run(app)['status'], 'paused')
        self.assertEqual(tools.calls, [])
        self.assertEqual(provider.counts['pause-target'], 1)
        app.action({'action': 'resume', 'channelId': 'group'})
        self.assertTrue(app.wait_for_idle(3))
        self.assertEqual(len(tools.calls), 1)
        self.assertEqual(self.current_run(app)['status'], 'done')

    def test_delegate_then_await_runs_child_without_parent_deadlock(self):
        def parent(index, context, messages):
            if index == 0:
                return {'type': 'delegate', 'task': {'title': 'child-research', 'goal': 'child-research', 'agentId': 'research', 'requiredCapabilities': ['research']}}
            if index == 1:
                for message in reversed(messages):
                    prefix = '动作的真实结果：'
                    if message['content'].startswith(prefix):
                        payload = json.loads(message['content'][len(prefix):])
                        return {'type': 'await_task', 'taskId': payload['task']['id']}
            return final('parent merged child result')
        provider = ScriptedProvider([task('parent', 'executor', required=['coding'])], {'parent': parent})
        app = self.app(provider)
        self.send(app)
        self.assertTrue(app.wait_for_idle(4))
        run = self.current_run(app)
        self.assertEqual(run['status'], 'done')
        parent_task = next(step for step in run['steps'] if step['title'] == 'parent')
        child = next(step for step in run['steps'] if step['title'] == 'child-research')
        self.assertEqual(child['parentId'], parent_task['id'])
        self.assertEqual(child['status'], 'done')
        self.assertIn(child['id'], parent_task['childIds'])

    def test_cyclic_plan_is_rejected_without_executing_tools(self):
        provider = ScriptedProvider([task('a', 'research', ['b']), task('b', 'executor', ['a'])])
        tools = FakeTools()
        app = self.app(provider, tools)
        self.send(app)
        self.assertTrue(app.wait_for_idle(3))
        self.assertEqual(self.current_run(app)['status'], 'failed')
        self.assertEqual(tools.calls, [])
        self.assertTrue(any(event['type'] == 'run_failed' for event in self.current_run(app)['events']))

    def test_model_reflection_remains_candidate_until_explicit_confirmation(self):
        reflection = {'trigger': 'offline dependency', 'lesson': 'Use Python csv standard library.', 'conditions': 'network off', 'kind': 'procedural'}
        provider = ScriptedProvider([task('memory-case', 'executor', required=['coding'])],
                                    {'memory-case': [final(reflection=reflection)]})
        app = self.app(provider)
        self.send(app)
        self.assertTrue(app.wait_for_idle(3))
        memories = app.memory.list('executor')
        candidates = [memory for memory in memories if memory['kind'] == 'procedural']
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['status'], 'candidate')
        self.assertEqual(app.memory.recall('executor', 'offline dependency Python'), [])
        app.action({'action': 'memory_confirm', 'agentId': 'executor', 'memoryId': candidates[0]['id'], 'confirmed': True})
        self.assertEqual(len(app.memory.recall('executor', 'offline dependency Python')), 1)
        self.assertFalse(self.current_run(app)['memoryHits'])

    def test_restart_checkpoint_requires_reconciliation_before_uncertain_call_replay(self):
        provider = ScriptedProvider([task('resume-target', 'executor', required=['coding'])])
        old = self.app(provider)
        self.send(old)
        self.assertTrue(old.wait_for_idle(3))
        state = old.repo.load_state()
        run = state['sessions']['group']['run']
        target = next(step for step in run['steps'] if step['title'] == 'resume-target')
        target.update(status='running', turns=0)
        target.pop('_episodeRecordedRevision', None)
        run.update(status='running', finishedAt=None)
        call = {'id': 'unknown-external-call', 'taskId': target['id'], 'agentId': 'executor', 'tool': 'git.push',
                'args': {'remote': 'https://example.invalid/repo.git', 'branch': 'demo', 'commit': 'a'*40},
                'status': 'running', 'result': None, 'durationMs': 0}
        run['calls'].append(call)
        target['_pendingTool'] = {'name': call['tool'], 'args': call['args'], 'callId': call['id'], 'effectKey': 'uncertain-effect', 'phase': 'executing'}
        old.repo.reserve_effect('uncertain-effect', {'tool': call['tool'], 'args': call['args']})
        old.repo.save_state(state)
        tools = FakeTools()
        restored = self.app(ScriptedProvider([]), tools)
        self.assertEqual(self.current_run(restored)['status'], 'interrupted')
        self.assertEqual(tools.calls, [])
        restored.action({'action': 'recover', 'channelId': 'group'})
        approval = self.pending(restored, 'reconciliation')
        self.assertEqual(approval['binding']['callId'], call['id'])
        self.assertEqual(tools.calls, [])
        self.answer(restored, approval, 'executed')
        self.assertTrue(restored.wait_for_idle(3))
        self.assertEqual(tools.calls, [], 'existing external effect is not replayed')
        reconciled = next(item for item in self.current_run(restored)['calls'] if item['id'] == call['id'])
        self.assertEqual(reconciled['status'], 'reconciled')
        self.assertFalse(reconciled['result']['ok'], 'human recollection is not a verified tool success')
        self.assertEqual(restored.repo.effect('uncertain-effect')['status'], 'reserved')

    def test_concurrent_model_requests_atomically_reserve_last_call_budget(self):
        """Both threads pass the initial budget check before either can reserve."""
        release = threading.Event()
        self.release_events.append(release)
        def hold(index, context, messages):
            release.wait(3)
            return final('one permitted response')
        provider = ScriptedProvider([
            task('budget-research', 'research', required=['research']),
            task('budget-executor', 'executor', required=['coding'])],
            {'budget-research': hold, 'budget-executor': hold})
        self.config.max_model_calls = 2  # Planning uses one; only one remains.
        app = self.app(provider)
        initial_check = app._budget
        checked = set()
        checks_lock = threading.Lock()
        barrier = threading.Barrier(2)
        def synchronized_check(channel, run_id, current_task, estimated_tokens=0):
            initial_check(channel, run_id, current_task, estimated_tokens)
            if current_task.get('_planning'):
                return
            with checks_lock:
                first = current_task['id'] not in checked
                checked.add(current_task['id'])
            if first:
                barrier.wait(timeout=2)
        app._budget = synchronized_check
        self.send(app)
        approval = self.pending(app, 'budget')
        run = self.current_run(app)
        self.assertEqual(run['modelCalls'], 2)
        self.assertEqual(len(provider.records), 1, 'only one worker may consume the remaining upstream call')
        self.assertEqual(sum(call['tool'] == 'llm.chat' for call in run['calls']), 2)
        self.assertEqual(sum(item['type'] == 'budget' and item['status'] == 'pending' for item in run['approvals']), 1)
        with app.lock:
            self.assertGreater(app.state['sessions']['group']['run']['_reservedTokens'], 0)
        self.answer(app, approval, 'deny')
        release.set()
        self.assertTrue(app.wait_for_idle(3))
        self.assertEqual(self.current_run(app)['modelCalls'], 2)
        with app.lock:
            self.assertEqual(app.state['sessions']['group']['run']['_reservedTokens'], 0)

    def test_restored_pending_tool_keeps_original_owner_despite_cheaper_preference(self):
        original = self.app(ScriptedProvider([task('owner-target', 'reviewer', required=['review'])]))
        self.send(original)
        self.assertTrue(original.wait_for_idle(3))
        state = original.repo.load_state()
        run = state['sessions']['group']['run']
        current_task = next(step for step in run['steps'] if step['title'] == 'owner-target')
        current_task.update(status='waiting_human', turns=0, preferredAgentId='research',
                            requiredCapabilities=[], requiredTools=['file.read'],
                            candidateAgentIds=['research', 'reviewer'])
        current_task.pop('_episodeRecordedRevision', None)
        run.update(status='waiting_human', finishedAt=None)
        args = {'path': 'reviewer-owned.txt'}
        call_id, approval_id, effect_key = 'owned-call', 'owned-approval', 'owned-effect'
        binding = {'callId': call_id, 'tool': 'file.delete', 'args': args,
                   'agentId': 'reviewer', 'runId': run['id']}
        run['calls'].append({'id': call_id, 'taskId': current_task['id'], 'agentId': 'reviewer',
                             'tool': 'file.delete', 'args': args, 'status': 'pending',
                             'result': 'waiting approval', 'durationMs': 0})
        run['approvals'].append({'id': approval_id, 'type': 'permission', 'status': 'pending',
                                 'title': 'Resume this exact owned action', 'description': 'inert test',
                                 'choices': [{'id': 'allow', 'label': 'allow'}, {'id': 'deny', 'label': 'deny'}],
                                 'createdAt': int(time.time()*1000), 'expiresAt': int(time.time()*1000)+600000,
                                 'agentId': 'reviewer', 'taskId': current_task['id'], 'callId': call_id,
                                 'args': args, 'binding': binding, 'bindingHash': fingerprint(binding)})
        current_task['_pendingTool'] = {'name': 'file.delete', 'args': args, 'callId': call_id,
                                        'effectKey': effect_key, 'phase': 'approval'}
        original.repo.reserve_effect(effect_key, {'runId': run['id'], 'taskId': current_task['id'],
                                                  'tool': 'file.delete', 'args': args})
        original.repo.save_state(state)
        tools = FakeTools()
        restored = self.app(ScriptedProvider([]), tools)
        restored.action({'action': 'recover', 'channelId': 'group'})
        approval = self.pending(restored, 'permission')
        self.assertEqual(approval['id'], approval_id)
        self.answer(restored, approval, 'allow')
        self.assertTrue(restored.wait_for_idle(3))
        approved = [call for call in tools.calls if call['approved']]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]['agent'], 'reviewer')
        self.assertEqual(approved[0]['args']['path'], 'reviewer-owned.txt')
        resumed_task = next(step for step in self.current_run(restored)['steps'] if step['title'] == 'owner-target')
        self.assertEqual(resumed_task['agentId'], 'reviewer')

    def test_expired_permission_cannot_execute_side_effect(self):
        tools = FakeTools()
        provider = ScriptedProvider([task('expire-target', 'executor', required=['coding'])],
                                    {'expire-target': [{'type': 'tool', 'name': 'file.delete', 'args': {'path': 'keep.txt'}},
                                                       final('expired action was not executed', status='partial')]})
        app = self.app(provider, tools)
        self.send(app)
        approval = self.pending(app, 'permission')
        with app.changed:
            live_run = app.state['sessions']['group']['run']
            live_approval = next(item for item in live_run['approvals'] if item['id'] == approval['id'])
            live_approval['expiresAt'] = 0
            app._persist()
        with self.assertRaises(ActionError):
            self.answer(app, approval, 'allow')
        self.assertTrue(app.wait_for_idle(3))
        self.assertFalse(any(call['approved'] for call in tools.calls))
        run = self.current_run(app)
        self.assertEqual(next(item for item in run['approvals'] if item['id'] == approval['id'])['status'], 'expired')
        self.assertEqual(next(item for item in run['calls'] if item['tool'] == 'file.delete')['status'], 'denied')

    def test_failed_and_cancelled_model_elapsed_time_is_accounted_once(self):
        simulated_time = [1000]
        app = self.app(ScriptedProvider([]))
        class FailedRequest:
            def complete(self, messages):
                simulated_time[0] += 90000
                raise ProviderError('simulated upstream timeout', 'network', False)
        class ResetRequest:
            def complete(self, messages):
                simulated_time[0] += 7000
                app.action({'action': 'reset', 'channelId': 'coordinator'})
                return {'content': 'late response after reset', 'usage': {'total_tokens': 1}}
        class CancelAfterResponse:
            def complete(self, messages):
                simulated_time[0] += 5000
                app.action({'action': 'cancel', 'channelId': 'coordinator'})
                return {'content': 'completed upstream request for a cancelled run', 'usage': {'total_tokens': 1}}
        with patch('backend.engine.clock_ms', side_effect=lambda: simulated_time[0]):
            app.provider = FailedRequest()
            app.action({'action': 'send', 'channelId': 'coordinator', 'mode': 'chat', 'text': 'failure time'})
            self.assertTrue(app.wait_for_idle(2))
            failed = app.snapshot()['sessions']['coordinator']['run']
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(failed['activeMs'], 90000)
            self.assertEqual(failed['calls'][0]['durationMs'], 90000)
            app.provider = ResetRequest()
            started = app.action({'action': 'send', 'channelId': 'coordinator', 'mode': 'chat', 'text': 'reset time'})
            old_id = started['sessions']['coordinator']['run']['id']
            self.assertTrue(app.wait_for_idle(2))
            archived = app.repo.get_run(old_id)
            self.assertEqual(archived['status'], 'cancelled')
            self.assertEqual(archived['activeMs'], 7000)
            self.assertIsNone(app.snapshot()['sessions']['coordinator']['run'])
            app.provider = CancelAfterResponse()
            app.action({'action': 'send', 'channelId': 'coordinator', 'mode': 'chat', 'text': 'cancel time'})
            self.assertTrue(app.wait_for_idle(2))
            cancelled = app.snapshot()['sessions']['coordinator']['run']
            self.assertEqual(cancelled['status'], 'cancelled')
            self.assertEqual(cancelled['activeMs'], 5000, 'success accounting followed by cancellation must not count twice')

    def test_runtime_owner_only_memory_preserves_historical_explicit_share_without_injection(self):
        title = 'offline Python dependency'
        plan = [task(title, 'executor', required=['coding'])]
        scripts = {title: [{'type': 'tool', 'name': 'code.test', 'args': {'code': 'assert True'}}, final('tested output')]}
        app = self.app(ScriptedProvider(plan, scripts))
        candidate = app.memory.propose('research', 'Use Python csv for offline dependency processing.',
                                       'offline Python dependency', 'source-task', 'source-run', ['source-call'])
        app.memory.confirm(candidate['id'], 'research', True, {'type': 'human', 'id': 'confirm-shared'})
        app.memory.share(candidate['id'], 'research', {'type': 'human', 'id': 'explicit-publish'})
        self.send(app, participants=['executor'])
        self.assertTrue(app.wait_for_idle(3))
        run = self.current_run(app)
        self.assertEqual(run['status'], 'done')
        self.assertFalse(any(item['memoryId'] == candidate['id'] for item in run['memoryHits']))
        self.assertTrue(app.provider.records)
        self.assertTrue(all(not any(item.get('id') == candidate['id'] for item in record['context']['memories']) for record in app.provider.records))
        source = next(item for item in app.memory.list('research') if item['id'] == candidate['id'])
        self.assertEqual(source['uses'], 0, 'consumer cannot mutate original owner counters')
        self.assertEqual(source['success_count'], 0)
        self.assertEqual(source['visibility'], 'shared', 'historical explicit publication is preserved, not copied or deleted')
        self.assertEqual(source['shareConfirmation']['id'], 'explicit-publish')
        self.assertFalse(any(event['type'] in ('memory_feedback', 'memory_feedback_skipped') and event['payload'].get('memoryId') == candidate['id'] for event in run['events']))
        own_records = app.memory.list('executor')
        self.assertTrue(all(item['owner'] == 'executor' and item['kind'] == 'episodic' for item in own_records))
        self.assertFalse(any(item.get('source_task') == 'source-task' for item in own_records), 'no foreign strategy is copied into personal memory')
        explicit = app.memory.recall('executor', title, include_shared=True)
        self.assertEqual([item['id'] for item in explicit], [candidate['id']], 'legacy administrative opt-in remains explicit')

    def test_owner_feedback_updates_normally_and_feedback_failure_is_nonfatal(self):
        title = 'offline Python dependency'
        plan = [task(title, 'executor', required=['coding'])]
        scripts = {title: [{'type': 'tool', 'name': 'code.test', 'args': {'code': 'assert True'}}, final('tested output')]}
        app = self.app(ScriptedProvider(plan, scripts))
        candidate = app.memory.propose('executor', 'Use Python csv for offline dependency processing.',
                                       'offline Python dependency', 'source-task', 'source-run', ['source-call'])
        app.memory.confirm(candidate['id'], 'executor', True, {'type': 'tool', 'id': 'source-verification'})
        self.send(app, participants=['executor'])
        self.assertTrue(app.wait_for_idle(3))
        run = self.current_run(app)
        self.assertEqual(run['status'], 'done')
        owner_memory = next(item for item in app.memory.list('executor') if item['id'] == candidate['id'])
        self.assertEqual((owner_memory['uses'], owner_memory['success_count']), (1, 1))
        self.assertTrue(any(event['type'] == 'memory_feedback' for event in run['events']))
        app.provider = ScriptedProvider(plan, scripts)
        with patch.object(app.memory, 'feedback', side_effect=ValueError('simulated feedback bookkeeping failure')):
            self.send(app, participants=['executor'])
            self.assertTrue(app.wait_for_idle(3))
        run = self.current_run(app)
        self.assertEqual(run['status'], 'done', 'feedback error must not replace completed task status')
        self.assertTrue(any(event['type'] == 'memory_feedback_failed' for event in run['events']))
        self.assertTrue(all(step['status'] == 'done' for step in run['steps']))
        owner_memory = next(item for item in app.memory.list('executor') if item['id'] == candidate['id'])
        self.assertEqual(owner_memory['uses'], 1)


if __name__ == '__main__':
    unittest.main()
