"""Actual Round7 flat action proposals receive feedback, never auto execution."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform, _action_not_tool_feedback
from backend.stage_control import stage_control
from tests import test_action_not_tool as prior
from tests.test_engine import task, final
from tests.test_execution_closure import LocalFiles, tool
from tests.test_time_only_runtime import Model

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/r7_flat_publish_proposals.json').read_text())


class FlatActionFeedbackTests(unittest.TestCase):
    setUp = prior.ActionNotToolRuntimeTests.setUp
    tearDown = prior.ActionNotToolRuntimeTests.tearDown
    execute = prior.ActionNotToolRuntimeTests.execute

    def test_all_actual_flat_proposals_have_nonexecuting_examples_and_no_mutation(self):
        for row in FIXTURE['proposals']:
            action = copy.deepcopy(row['exactProposal']); original = copy.deepcopy(action)
            feedback = _action_not_tool_feedback(action)
            self.assertEqual(feedback['error']['code'], 'action_not_tool')
            expected = {key: value for key, value in action.items() if key not in ('type', 'name')}
            expected['type'] = action['name']
            self.assertEqual(feedback['correctActionExample'], expected)
            self.assertTrue(feedback['notExecuted']); self.assertFalse(feedback['actionExecuted'])
            self.assertEqual(action, original)
            self.assertFalse(any(key in feedback for key in ('artifact', 'artifacts', 'delivery', 'callId')))

    def test_flat_publication_in_handoff_requires_a_new_explicit_action_and_registers_once(self):
        for index in (0, 2):
            proposal = FIXTURE['proposals'][index]['exactProposal']
            with self.subTest(name=proposal['name']):
                now = [1000000]
                config = Config(Path(self.tmp.name) / str(index), {'LLM_API_KEY': 'offline-flat', 'MAX_RETRIES': '0'})
                config.apply_time_only_profile()
                body = 'Actual local research notes; no fact verification is implied.\n'
                source = FIXTURE['successfulPrivateWrite']['path']

                def pin(run, current, stamp):
                    view = stage_control(run, current, stamp)
                    if view and not current.get('_testPinned'):
                        current['_testPinned'] = True
                        current['stageControl'].update(synthesisAt=stamp+10, handoffAt=stamp+20, expectedHandoffBy=stamp+30)
                        view = stage_control(run, current, stamp)
                    return view

                def response(turn, context, messages):
                    if turn == 0:
                        return tool('file.write', path=source, content=body)
                    if turn == 1:
                        self.assertEqual(context['stageControl']['phase'], 'handoff_final')
                        return copy.deepcopy(proposal)
                    if turn == 2:
                        feedback = prior.last_result(messages)
                        self.assertEqual(feedback['error']['code'], 'action_not_tool')
                        self.assertFalse(feedback['actionExecuted'])
                        self.assertEqual(context['ownArtifacts'], [], 'explanation cannot publish a file')
                        return feedback['correctActionExample']
                    return final('Published actual notes after a separate corrected proposal.')

                class AdvanceFiles(LocalFiles):
                    def execute(self, *args, **kwargs):
                        result = super().execute(*args, **kwargs)
                        if args[2] == 'file.write': now[0] += 30
                        return result

                model = Model(response); app = Platform(config, provider=model, tools=AdvanceFiles(config))
                model.app = app; self.apps.append(app)
                with patch('backend.engine.clock_ms', side_effect=lambda: now[0]), patch('backend.engine.stage_control', side_effect=pin):
                    app.action({'action': 'send', 'mode': 'task', 'participants': ['coordinator'], 'text': 'Publish actual existing notes.'})
                    self.assertTrue(app.wait_for_idle(4))
                run = app.state['sessions']['group']['run']
                self.assertEqual(run['status'], 'done')
                self.assertEqual(run['budget']['deadlineAt'], 2800000)
                self.assertEqual(len(run['artifacts']), 1)
                self.assertEqual(run['artifacts'][0]['sha256'], hashlib.sha256(body.encode()).hexdigest())
                self.assertEqual(sum(event['type'] == 'artifact_registered' for event in run['events']), 1)
                self.assertEqual(sum(event['type'] == 'action_protocol_rejected' for event in run['events']), 1)
                self.assertFalse(any(event['type'] == 'stage_action_blocked' for event in run['events']))
                self.assertFalse(any(call['tool'].startswith('publish') for call in run['calls']))

    def test_malformed_reserved_args_get_placeholders_without_running_the_tool(self):
        for value in (None, 'bad', [], True, 3):
            with self.subTest(args=value):
                wrong = {'type': 'tool', 'name': 'publish_artifact', 'args': value, 'path': 'ignored.md'}
                feedback = _action_not_tool_feedback(wrong)
                self.assertEqual(feedback['correctActionExample']['path'], '<existing-relative-file.md>')
                def response(turn, context, messages):
                    if turn == 0: return wrong
                    self.assertEqual(prior.last_result(messages)['error']['code'], 'action_not_tool')
                    return final('No publication performed.', status='partial')
                _, run, _ = self.execute([task('Check', 'coordinator')], {'Check': response}, ['coordinator'])
                self.assertEqual(run['status'], 'partial'); self.assertFalse(run['artifacts'])
                self.assertFalse(any(call['tool'] == 'publish_artifact' for call in run['calls']))

    def test_unknown_or_real_tool_with_flat_fields_is_not_auto_normalized(self):
        for name in ('publish_thing', 'file.write', 'git.push', 'review'):
            proposal = {'type': 'tool', 'name': name, 'path': 'no.md', 'content': 'Must not execute.'}
            self.assertIsNone(_action_not_tool_feedback(proposal))
        wrong = {'type': 'tool', 'name': 'file.write', 'path': 'no.md', 'content': 'Must not execute.'}
        _, run, _ = self.execute([task('Check', 'coordinator')], {'Check': [wrong, final('No write.', status='partial')]}, ['coordinator'])
        self.assertFalse(run['artifacts'])
        self.assertFalse(any(call['tool'] == 'file.write' for call in run['calls']))
        self.assertFalse(any(event['type'] == 'action_protocol_rejected' for event in run['events']))

    def test_flat_loop_and_final_only_boundaries_still_do_not_publish(self):
        wrong = FIXTURE['proposals'][0]['exactProposal']
        _, run, model = self.execute([task('Check', 'coordinator')], {'Check': [copy.deepcopy(wrong)]*5}, ['coordinator'])
        self.assertEqual(model.counts['Check'], 3); self.assertFalse(run['artifacts'])
        self.assertEqual(sum(event['type'] == 'action_protocol_rejected' for event in run['events']), 2)
        _, run, model = self.execute([task('Check', 'coordinator')], {'Check': [{'type': 'accept'}, copy.deepcopy(wrong)]}, ['coordinator'], turns=2)
        current = next(row for row in run['steps'] if not row.get('_planning'))
        self.assertEqual(current['finalizationFailure']['code'], 'final_action_required')
        self.assertFalse(run['artifacts'])
        self.assertFalse(any(event['type'] == 'action_protocol_rejected' for event in run['events']))


if __name__ == '__main__': unittest.main()
