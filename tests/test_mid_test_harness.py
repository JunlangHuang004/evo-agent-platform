import copy
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

from benchmarks import mid_test_live
from benchmarks.mid_test_live import SCENARIOS, evidence_checks, load_suite, run_case


class ObservedApp:
    """Inert model-free app with a UI replacement and a real runId control gate."""
    def __init__(self, replace_on_snapshot=False, replace_on_control=False, status='running', approvals=None):
        self.agents = {'a': {}, 'b': {}}
        self.config = SimpleNamespace(model_timeout=1, provider='test', model='test', scrub=lambda value: value)
        self.lock = threading.RLock()
        self.actions, self.started = [], False
        self.replace_on_snapshot, self.replace_on_control = replace_on_snapshot, replace_on_control
        self.original = {'id':'run-owned', 'title':'Official task', 'status':status,
                         'steps':[], 'calls':[], 'artifacts':[], 'approvals':approvals or [],
                         'conversation':[{'role':'human','text':'Original conversation'}],
                         '_messages':[{'content':'Private context must not be exported'}]}
        self.saved_original = dict(copy.deepcopy(self.original), status='cancelled')
        self.replacement = {'id':'run-user', 'title':'Another user task', 'status':'done',
                            'steps':[{'id':'foreign','agentId':'a','status':'done','turns':1}],
                            'calls':[], 'artifacts':[{'id':'artifact-user','name':'user.txt','content':'Unrelated user material'}],
                            'approvals':[], 'conversation':[{'text':'Do not copy new user dialogue'}]}
        self.state = {'sessions':{'group':{'run':None,'messages':[]}}}
        self.repo = SimpleNamespace(get_run=self.get_run)
        self.requested_runs = []

    def get_run(self, run_id):
        self.requested_runs.append(run_id)
        return copy.deepcopy(self.saved_original) if run_id == 'run-owned' else None

    def replace(self):
        self.state['sessions']['group'] = {'run':copy.deepcopy(self.replacement),
                                            'messages':[{'text':'Do not copy new user dialogue'}]}

    def snapshot(self):
        if self.started and self.replace_on_snapshot:
            self.replace_on_snapshot = False
            self.replace()
        return copy.deepcopy(self.state)

    def action(self, payload):
        self.actions.append(copy.deepcopy(payload))
        if payload['action'] == 'send':
            self.started = True
            self.state['sessions']['group'] = {'run':copy.deepcopy(self.original),
                                                'messages':copy.deepcopy(self.original['conversation'])}
            return copy.deepcopy(self.state)
        if self.replace_on_control:
            self.replace_on_control = False
            self.replace()
        current = self.state['sessions']['group']['run']
        if payload.get('runId') != current['id']:
            raise ValueError('Stale run ID: current user run must not be controlled.')
        current['status'] = 'paused' if payload['action'] == 'pause' else 'cancelled'
        self.saved_original = copy.deepcopy(current)
        return copy.deepcopy(self.state)

    def wait_for_idle(self, _):
        return True


class MidTestHarnessTests(unittest.TestCase):
    task = {'id':'software-1', 'title':'Official task', 'text':'Official task', 'sha256':'0'*64}

    def make_pack(self, root):
        for _, prefix in SCENARIOS:
            folder = root / (prefix + ' 2')
            folder.mkdir()
            roles = '\n'.join(f'| `{agent}` | Role |' for agent in ('a','b','c','d'))
            roles += '\n' + '\n'.join(f'```text\nOriginal role {agent}.\n```' for agent in ('a','b','c','d'))
            (folder / '01_role_initialization.md').write_text(roles)
            for idx, name in enumerate(('02_task_1.md', '03_task_2.md'),1):
                (folder / name).write_text(f'# Task {idx}\n\n```text\nOriginal task {idx}: keep context.\n```\n')

    def test_external_pack_is_six_ordered_tasks_with_exact_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_pack(root)
            suite = load_suite(root)
            self.assertEqual([s['team'] for s in suite], ['software','newsroom','fund'])
            self.assertEqual(sum(len(s['tasks']) for s in suite),6)
            self.assertEqual(suite[0]['tasks'][1]['text'],'Original task 2: keep context.')
            self.assertEqual(suite[1]['roles']['c'],'Original role c.')
            self.assertEqual(len(suite[2]['tasks'][0]['sha256']),64)

    def test_ambiguous_or_malformed_pack_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_pack(root)
            (root / SCENARIOS[0][1]).mkdir()
            with self.assertRaises(ValueError):
                load_suite(root)

    def test_agent_done_and_exit_zero_are_not_quality_verdicts(self):
        run = {'steps':[{'agentId':a,'status':'done','turns':1} for a in ('a','b','c','d')],
               'calls':[{'tool':'code.test','status':'success','result':{'exitCode':0}},
                        {'tool':'web.fetch','status':'failed','result':{}}], 'artifacts':[]}
        check = evidence_checks(run, ['a','b','c','d'])
        self.assertTrue(check['allRolesDeclaredDone'])
        self.assertEqual(check['realCodeCallsSucceeded'],1)
        self.assertEqual(check['realWebCallsSucceeded'],0)
        self.assertEqual(check['qualityVerdict'],'unassessed')
        self.assertNotIn('passed',check)

    def test_planning_and_inherited_outputs_do_not_count_as_new_work(self):
        run={'steps':[{'id':'plan','agentId':'lead','status':'done','turns':1,'_planning':True},
                      {'id':'dev','agentId':'dev','status':'done','turns':2}],
             'calls':[], 'artifacts':[{'id':'old','sourceRunId':'old-run'}, {'id':'new'}]}
        result=evidence_checks(run,['lead','dev'])
        self.assertEqual(result['plannedSubtasks'],1)
        self.assertFalse(result['allRolesDeclaredDone'])
        self.assertEqual(result['newPublishedArtifacts'],1)
        self.assertEqual(result['inheritedArtifacts'],1)

    def test_replacement_run_is_not_counted_or_exported_as_the_official_case(self):
        app = ObservedApp(replace_on_snapshot=True)
        with tempfile.TemporaryDirectory() as temp:
            result = run_case(app, self.task, Path(temp), 600, 180, emit=lambda *args, **kwargs: None)
            saved = json.loads((Path(temp)/'software-1.json').read_text())
        self.assertEqual(result['runId'], 'run-owned')
        self.assertEqual(result['run']['id'], 'run-owned')
        self.assertEqual(result['observationStatus'], 'external_run_changed')
        self.assertEqual(result['executionStatus'], 'cancelled')
        self.assertEqual(result['checks']['newPublishedArtifacts'], 0)
        self.assertEqual(app.requested_runs, ['run-owned'])
        self.assertEqual([action['action'] for action in app.actions], ['send'])
        self.assertEqual(app.state['sessions']['group']['run'], app.replacement)
        self.assertNotIn('Unrelated user material', json.dumps(saved))
        self.assertNotIn('Do not copy new user dialogue', json.dumps(saved))
        self.assertNotIn('_messages', saved['run'])

    def test_cancel_race_is_bound_to_old_run_and_does_not_cancel_replacement(self):
        app = ObservedApp(replace_on_control=True)
        with tempfile.TemporaryDirectory() as temp:
            result = run_case(app, self.task, Path(temp), -1, 180, emit=lambda *args, **kwargs: None)
        controls = [action for action in app.actions if action['action'] != 'send']
        self.assertEqual(controls, [{'action':'cancel','channelId':'group','runId':'run-owned'}])
        self.assertEqual(app.state['sessions']['group']['run']['id'], 'run-user')
        self.assertEqual(app.state['sessions']['group']['run']['status'], 'done')
        self.assertEqual(result['observationStatus'], 'external_run_changed')
        self.assertEqual(result['run']['id'], 'run-owned')

    def test_pause_and_deadline_controls_always_include_the_bound_run_id(self):
        cases = [('running', [], -1, 180, 'cancel'),
                 ('waiting_human', [{'id':'approval-1','status':'pending'}], 600, -1, 'pause')]
        for status, approvals, deadline, human_wait, control in cases:
            with self.subTest(control=control), tempfile.TemporaryDirectory() as temp:
                app = ObservedApp(status=status, approvals=approvals)
                result = run_case(app, self.task, Path(temp), deadline, human_wait, emit=lambda *args, **kwargs: None)
                self.assertEqual(app.actions[-1], {'action':control,'channelId':'group','runId':'run-owned'})
                self.assertEqual(result['runId'], 'run-owned')
                self.assertEqual(result['observationStatus'], 'observed')

    def test_cli_time_only_profile_uses_effective_limits_and_requested_deadline(self):
        for time_only in (False, True):
            with self.subTest(time_only=time_only), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                server = mock.Mock()
                hub = mock.Mock()
                argv = ['mid_test_live.py', '--live', '--suite-root', str(root / 'suite'),
                        '--config-root', str(root), '--data-dir', str(root / 'state'),
                        '--output', str(root / 'output'), '--deadline', '123']
                if time_only:
                    argv.append('--time-only')
                with mock.patch('sys.argv', argv), mock.patch.object(mid_test_live, 'load_suite', return_value=[]), \
                        mock.patch('backend.server.TeamHub', return_value=hub) as factory, \
                        mock.patch('backend.server.make_handler', return_value=object()), \
                        mock.patch.object(mid_test_live, 'ThreadingHTTPServer', return_value=server), \
                        mock.patch('builtins.print'):
                    self.assertEqual(mid_test_live.main(), 0)
                config = factory.call_args.args[0]
                self.assertEqual(config.max_run_seconds, 123)
                self.assertEqual(config.execution_profile, 'time_only' if time_only else 'bounded')
                self.assertEqual(config.max_model_calls, None if time_only else 48)
                self.assertEqual(config.max_agent_turns, None if time_only else 16)
                self.assertEqual(config.max_run_tokens, None if time_only else 200000)

    def test_main_does_not_post_task_two_over_even_a_finished_replacement_user_run(self):
        app = ObservedApp(replace_on_snapshot=True)
        suite = [{'team':'software', 'roles':{'a':'Role A','b':'Role B'},
                  'tasks':[self.task, dict(self.task, id='software-2', text='Official follow-up')]}]
        hub = SimpleNamespace(get=lambda team: app, shutdown=lambda: None)
        server = mock.Mock()
        server.serve_forever.return_value = None
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            argv = ['mid_test_live.py','--live','--suite-root',str(root/'suite'),
                    '--config-root',str(root),'--data-dir',str(root/'state'),'--output',str(root/'output')]
            with mock.patch('sys.argv', argv), mock.patch.object(mid_test_live, 'load_suite', return_value=suite), \
                    mock.patch('backend.config.Config', return_value=app.config), \
                    mock.patch('backend.server.TeamHub', return_value=hub), \
                    mock.patch('backend.server.make_handler', return_value=object()), \
                    mock.patch('backend.teams.get_team_agents', return_value={'a':{'rolePrompt':'Role A'},'b':{'rolePrompt':'Role B'}}), \
                    mock.patch.object(mid_test_live, 'ThreadingHTTPServer', return_value=server), \
                    mock.patch('builtins.print'):
                self.assertEqual(mid_test_live.main(), 0)
            followup = json.loads((root/'output'/'software-2.json').read_text())
            summary = json.loads((root/'output'/'suite-results.json').read_text())
        self.assertEqual(followup['executionStatus'], 'not_run')
        self.assertEqual(len(summary['cases']), 2)
        self.assertEqual(len([action for action in app.actions if action['action'] == 'send']), 1)
        self.assertEqual(app.state['sessions']['group']['run']['id'], 'run-user')


if __name__ == '__main__':
    unittest.main()
