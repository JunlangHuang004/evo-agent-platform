"""Real scheduler/watch threads with blocked scripted calls and a fake wall clock."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform
from tests.test_engine import FakeTools, ScriptedProvider, final, task


class BarrierProvider(ScriptedProvider):
    def __init__(self, actions):
        self.actions = actions
        self.entered = [threading.Event() for _ in range(12)]
        self.release = [threading.Event() for _ in range(12)]
        self.messages = {}
        super().__init__([task('Work', 'coordinator')], {'Work': self.respond})

    def respond(self, index, context, messages):
        self.messages[index] = copy.deepcopy(messages)
        self.entered[index].set()
        if not self.release[index].wait(15):
            raise AssertionError('test did not release scripted model barrier')
        return self.actions[index] if index < len(self.actions) else final()

    def complete(self, messages, max_tokens=None, deadline=None):
        result = super().complete(messages)
        result['finish_reason'] = 'stop'
        return result


class DeadlineTools(FakeTools):
    def execute(self, agent, run_id, name, args, approved=False, deadline=None):
        return super().execute(agent, run_id, name, args, approved)


class WatchdogEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.apps = []
        self.models = []
        self.now = int(time.time() * 1000)
        timer = patch('backend.engine.clock_ms', side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)

    def tearDown(self):
        for model in self.models:
            for release in model.release:
                release.set()
        for app in self.apps:
            app.shutdown()
        for app in self.apps:
            self.assertTrue(app.wait_for_idle(5), 'scheduler/watch thread leaked')
            self.assertFalse(app.progress_workers)
            app.repo.close()
        self.tmp.cleanup()

    def create(self, actions, profile='time_only', tools=None):
        config = Config(Path(self.tmp.name) / str(len(self.apps)), {
            'LLM_API_KEY': 'offline-watchdog-test', 'MAX_RETRIES': '0',
            'MAX_MODEL_CALLS': '30', 'MAX_AGENT_TURNS': '12',
            'MAX_RUN_TOKENS': '200000', 'MAX_RUN_SECONDS': '1800'})
        if profile == 'time_only':
            config.apply_time_only_profile()
        model = BarrierProvider(actions)
        app = Platform(config, provider=model, tools=tools or FakeTools())
        self.models.append(model)
        self.apps.append(app)
        self.send(app)
        self.at(model, 0)
        run = self.run_of(app)
        self.wait(lambda: run['id'] in app.progress_workers)
        self.assertTrue(app.progress_workers[run['id']].is_alive())
        self.assertEqual(run['budget'].get('profile', 'bounded'), profile)
        return app, model

    @staticmethod
    def send(app):
        app.action({'action': 'send', 'mode': 'task', 'channelId': 'group',
                    'participants': ['coordinator'], 'text': 'Return an observable plain-text result.'})

    @staticmethod
    def run_of(app):
        return app.state['sessions']['group']['run']

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail('scheduler condition not reached')

    def at(self, model, index):
        self.assertTrue(model.entered[index].wait(5), 'missing normal model request')

    def advance(self, app, milliseconds):
        with app.changed:
            self.now += milliseconds
            expected = self.now
            app.changed.notify_all()
        self.wait(lambda: (self.run_of(app).get('progressWatchdog') or {}).get('lastCheckAt') == expected)

    def event_rows(self, app, kind):
        return [row for row in self.run_of(app)['events'] if row['type'] == kind]

    @staticmethod
    def notices(messages):
        return [row for row in messages if row.get('content', '').startswith('定时进展诊断：')]

    def queue_notice(self, app, model):
        # Establish the task's baseline with its first request still active.
        self.advance(app, 90000)
        for index in (0, 1):
            model.release[index].set()
            self.at(model, index + 1)
        # Two genuine model responses settled, no product evidence changed.
        self.advance(app, 180000)
        self.assertEqual(len(self.event_rows(app, 'progress_watchdog_observed')), 1)
        self.assertFalse(self.event_rows(app, 'progress_watchdog_delivered'))
        self.assertFalse(self.notices(model.messages[2]))

    def test_active_call_only_queues_then_next_boundary_delivers_once_without_extra_calls(self):
        app, model = self.create([{'type': 'accept', 'sequence': n} for n in range(4)] + [final()])
        budget = copy.deepcopy(self.run_of(app)['budget'])
        self.queue_notice(app, model)
        self.assertEqual(model.counts['Work'], 3)
        self.assertEqual(self.run_of(app)['modelCalls'], 4)  # plan plus normal requests
        model.release[2].set()
        self.at(model, 3)
        self.assertEqual(len(self.notices(model.messages[3])), 1)
        self.assertEqual(len(self.event_rows(app, 'progress_watchdog_delivered')), 1)
        model.release[3].set()
        self.at(model, 4)
        self.advance(app, 90000)
        self.assertEqual(len(self.notices(model.messages[4])), 1)
        self.assertEqual(len(self.event_rows(app, 'progress_watchdog_observed')), 1)
        self.assertEqual(len(self.event_rows(app, 'progress_watchdog_delivered')), 1)
        model.release[4].set()
        self.assertTrue(app.wait_for_idle(5))
        run = self.run_of(app)
        self.assertEqual(run['status'], 'done')
        self.assertEqual(run['modelCalls'], 6)
        self.assertEqual(model.counts['Work'], 5)
        self.assertEqual(run['budget'], budget)
        self.assertEqual(run['budgetExtensions'], 0)

    def test_real_new_tool_evidence_invalidates_queued_notice_before_next_request(self):
        body = 'newly observed source'
        digest = hashlib.sha256(body.encode()).hexdigest()
        tools = DeadlineTools(lambda a, r, n, args, approved: {
            'ok': True, 'path': args['path'], 'text': body, 'sha256': digest})
        app, model = self.create([{'type': 'accept', 'sequence': 0}, {'type': 'accept', 'sequence': 1},
            {'type': 'tool', 'name': 'file.read', 'args': {'path': 'source.txt'}}, final()], tools=tools)
        self.queue_notice(app, model)
        model.release[2].set()
        self.at(model, 3)
        self.assertFalse(self.notices(model.messages[3]))
        self.assertFalse(self.event_rows(app, 'progress_watchdog_delivered'))
        self.assertEqual(len(tools.calls), 1)
        self.advance(app, 90000)
        task_row = next(row for row in self.run_of(app)['steps'] if not row.get('_planning'))
        self.assertIsNone(self.run_of(app)['progressWatchdog']['tasks'][task_row['id']]['notice'])
        model.release[3].set()
        self.assertTrue(app.wait_for_idle(5))
        self.assertEqual(self.run_of(app)['status'], 'done')
        self.assertEqual(self.run_of(app)['modelCalls'], 5)

    def test_long_model_and_real_pending_approval_are_waits_not_stagnation_or_completion(self):
        app, model = self.create([{'type': 'ask_human', 'question': 'Choose output language?',
                                  'choices': ['English', 'Chinese']}, final()])
        budget = copy.deepcopy(self.run_of(app)['budget'])
        self.advance(app, 90000)
        self.advance(app, 270000)
        self.assertEqual(model.counts['Work'], 1)
        self.assertFalse(self.event_rows(app, 'progress_watchdog_observed'))
        self.assertEqual(self.run_of(app)['status'], 'running')
        model.release[0].set()
        self.wait(lambda: any(a['status'] == 'pending' for a in self.run_of(app)['approvals']))
        self.advance(app, 270000)
        self.assertEqual(model.counts['Work'], 1)
        self.assertFalse(self.event_rows(app, 'progress_watchdog_observed'))
        approval = next(a for a in self.run_of(app)['approvals'] if a['status'] == 'pending')
        self.assertNotIn('clarificationReceipt', approval)
        app.action({'action': 'approval', 'channelId': 'group', 'approvalId': approval['id'],
                    'choiceId': 'option-0'})
        self.at(model, 1)
        self.assertFalse(self.notices(model.messages[1]))
        model.release[1].set()
        self.assertTrue(app.wait_for_idle(5))
        self.assertEqual(self.run_of(app)['status'], 'done')
        self.assertEqual(self.run_of(app)['budget'], budget)

    def test_both_profiles_start_watcher_and_terminal_replacement_shutdown_release_it(self):
        for profile in ('bounded', 'time_only'):
            with self.subTest(profile=profile):
                app, model = self.create([final(), final()], profile)
                old_run = self.run_of(app)
                old_watcher = app.progress_workers[old_run['id']]
                if profile == 'bounded':
                    model.release[0].set()
                    self.assertTrue(app.wait_for_idle(5))
                    self.assertEqual(old_run['status'], 'done')
                    self.assertFalse(old_watcher.is_alive())
                else:
                    # Real reset/replacement while the old model request is active.
                    app.action({'action': 'reset', 'channelId': 'group'})
                    self.wait(lambda: not old_watcher.is_alive())
                    model.release[0].set()
                    self.assertTrue(app.wait_for_idle(5))
                    self.send(app)
                    self.at(model, 1)
                    new_run = self.run_of(app)
                    self.assertNotEqual(new_run['id'], old_run['id'])
                    self.wait(lambda: not old_watcher.is_alive())
                    self.wait(lambda: new_run['id'] in app.progress_workers)
                    new_watcher = app.progress_workers[new_run['id']]
                    app.shutdown()
                    self.wait(lambda: not new_watcher.is_alive())
                    self.assertEqual(new_run['status'], 'interrupted')
                    model.release[0].set()
                    model.release[1].set()
                    self.assertTrue(app.wait_for_idle(5))
                self.assertFalse(app.progress_workers)
                self.assertFalse(app.workers)


if __name__ == '__main__':
    unittest.main()
