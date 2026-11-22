"""Dev redundant self-test short-circuit: unit tests at the pure-function layer.

These tests exercise the feedback function in isolation so they do not launch
Docker or require a running engine. They encode the intended policy:

* After this Dev has published and already passed the identical browser
  proposal on the currently published bytes, re-submitting it returns
  not-executed feedback that cites the cached passing call.
* Pre-publish runs, failed prior runs, unknown outcomes, non-browser code
  tests, Reviewer agents (no publish capability), file mutations after the
  passing call, and any change to args all keep the normal path open.
"""
import copy
import unittest

from backend.agents import AGENTS
from backend.dev_redundant_test import dev_redundant_test_feedback


def _passing_browser_result():
    entry = {'path': 'app/index.html', 'sha256': 'a' * 64}
    manifest = [entry,
                {'path': 'app/app.js', 'sha256': 'b' * 64},
                {'path': 'app/styles.css', 'sha256': 'c' * 64}]
    return {
        'ok': True,
        'runtime': 'browser',
        'exitCode': 0,
        'browser': {
            'engine': 'chromium',
            'executed': True,
            'ok': True,
            'entry': entry,
            'sourceManifest': manifest,
            'testedFiles': manifest,
            'assertions': [{'action': 'assert_text', 'selector': '#h',
                            'expected': 'ok', 'actual': 'ok', 'passed': True,
                            'pageId': 'main'}],
            'steps': [],
            'assertionCount': 1,
            'assertionsPassed': 1,
            'pageErrors': [],
            'blockedRequests': [],
            'httpErrors': [],
        },
    }


def _args():
    return {
        'language': 'browser',
        'path': 'app/index.html',
        'resources': {'app.js': 'app/app.js', 'styles.css': 'app/styles.css'},
        'checks': [{'action': 'assert_text', 'selector': '#h', 'text': 'ok'}],
        'timeout': 20,
    }


def _fixture(agent_id='executor', *, with_publish=True, prior_status='success',
             prior_ok=True):
    task = {'id': 'task-1', 'agentId': agent_id, 'revision': 0}
    call_args = _args()
    call = {
        'id': 'call-pass',
        'taskId': task['id'],
        'agentId': agent_id,
        'tool': 'code.test',
        'args': call_args,
        'revision': 0,
        'status': prior_status,
        'result': _passing_browser_result() if prior_ok else {
            'ok': False, 'runtime': 'browser', 'exitCode': 1,
            'browser': {'engine': 'chromium', 'executed': True, 'ok': False,
                        'entry': {'path': 'app/index.html', 'sha256': 'a' * 64},
                        'sourceManifest': [{'path': 'app/index.html', 'sha256': 'a' * 64}],
                        'testedFiles': [{'path': 'app/index.html', 'sha256': 'a' * 64}],
                        'assertions': [], 'steps': []}},
        'durationMs': 3200,
    }
    events = []
    if with_publish:
        events.append({'type': 'artifact_registered', 'agentId': agent_id,
                       'payload': {'taskId': task['id'],
                                   'artifactId': 'art-1',
                                   'sha256': 'a' * 64}})
    run = {'id': 'run-1', 'calls': [call], 'events': events}
    action = {'type': 'tool', 'name': 'code.test', 'args': copy.deepcopy(call_args)}
    return run, task, action


class DevRedundantSelfTestTests(unittest.TestCase):
    def test_identical_browser_proposal_short_circuits(self):
        run, task, action = _fixture()
        before = copy.deepcopy((run, task, action))
        feedback = dev_redundant_test_feedback(run, task, action, AGENTS)
        self.assertIsNotNone(feedback)
        self.assertEqual(feedback['error']['code'], 'redundant_browser_self_test')
        self.assertTrue(feedback['notExecuted'])
        self.assertFalse(feedback['actionExecuted'])
        self.assertEqual(feedback['cachedPassingCallId'], 'call-pass')
        self.assertEqual((run, task, action), before,
                         'feedback must be side-effect free')

    def test_pre_publish_proposal_is_not_short_circuited(self):
        run, task, action = _fixture(with_publish=False)
        self.assertIsNone(
            dev_redundant_test_feedback(run, task, action, AGENTS))

    def test_prior_failure_does_not_create_a_passing_cache(self):
        run, task, action = _fixture(prior_status='failed', prior_ok=False)
        self.assertIsNone(
            dev_redundant_test_feedback(run, task, action, AGENTS))

    def test_different_resources_bytes_bypass_the_cache(self):
        run, task, action = _fixture()
        action['args']['resources'] = {'app.js': 'app/other.js',
                                       'styles.css': 'app/styles.css'}
        self.assertIsNone(
            dev_redundant_test_feedback(run, task, action, AGENTS))

    def test_different_checks_bypass_the_cache(self):
        run, task, action = _fixture()
        action['args']['checks'].append(
            {'action': 'assert_count', 'selector': 'li', 'count': 3})
        self.assertIsNone(
            dev_redundant_test_feedback(run, task, action, AGENTS))

    def test_file_mutation_after_pass_invalidates_cache(self):
        for tool in ('file.write', 'file.patch', 'file.delete', 'code.run'):
            with self.subTest(tool=tool):
                run, task, action = _fixture()
                run['calls'].append({
                    'id': 'call-mut', 'taskId': task['id'],
                    'agentId': task['agentId'], 'tool': tool, 'args': {},
                    'revision': 0, 'status': 'success', 'result': {'ok': True},
                })
                self.assertIsNone(
                    dev_redundant_test_feedback(run, task, action, AGENTS))

    def test_non_browser_code_test_after_pass_invalidates_cache(self):
        run, task, action = _fixture()
        run['calls'].append({
            'id': 'call-other', 'taskId': task['id'],
            'agentId': task['agentId'], 'tool': 'code.test',
            'args': {'language': 'python', 'code': 'print(1)'},
            'revision': 0, 'status': 'success', 'result': {'ok': True},
        })
        self.assertIsNone(
            dev_redundant_test_feedback(run, task, action, AGENTS))

    def test_reviewer_agent_is_not_short_circuited(self):
        """QA/Reviewer never short-circuits even against the same bytes.

        Reviewer has no publish capability, so its calls land on a separate
        agent namespace and the cache simply does not match. This test guards
        against accidentally extending the short-circuit to independent QA.
        """
        run, task, action = _fixture(agent_id='reviewer')
        # Reviewer cannot actually publish via the engine; drop the publish event.
        run['events'] = []
        self.assertIsNone(
            dev_redundant_test_feedback(run, task, action, AGENTS))

    def test_other_revision_pass_does_not_short_circuit_current_revision(self):
        run, task, action = _fixture()
        run['calls'][0]['revision'] = 1
        self.assertIsNone(
            dev_redundant_test_feedback(run, task, action, AGENTS))

    def test_unknown_outcome_does_not_count_as_pass(self):
        run, task, action = _fixture()
        run['calls'][0]['result']['outcomeUnknown'] = True
        self.assertIsNone(
            dev_redundant_test_feedback(run, task, action, AGENTS))

    def test_timeout_default_is_normalised(self):
        """A proposal that omits timeout still matches the recorded default."""
        run, task, action = _fixture()
        action['args'].pop('timeout')
        feedback = dev_redundant_test_feedback(run, task, action, AGENTS)
        self.assertIsNotNone(feedback)
        self.assertEqual(feedback['error']['code'], 'redundant_browser_self_test')


if __name__ == '__main__':
    unittest.main()
