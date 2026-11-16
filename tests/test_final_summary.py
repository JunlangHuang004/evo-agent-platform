"""Text synthesis must not redo work; actual output contracts remain binding."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.final_summary import final_summary_feedback
from backend.teams import get_team_agents
from tests.test_action_not_tool import last_result
from tests.test_engine import ScriptedProvider, task as planned_task, final
from tests.test_execution_closure import LocalFiles


def summary_task(**updates):
    task = {
        'id': 'summary', 'agentId': 'chief_editor', 'status': 'running',
        'revision': 0, 'finalSummary': True, 'outputContract': {'kind': 'text'},
        'outputs': [], 'dependsOn': ['reviewed-product'],
        'acceptanceCriteria': ['Report actual outputs, verification and remaining gaps.'],
    }
    task.update(updates)
    return task


def tool(name, **args):
    return {'type': 'tool', 'name': name, 'args': args}


class FinalSummaryFeedbackTests(unittest.TestCase):
    def test_product_mutations_and_fresh_execution_receive_nonexecuting_feedback(self):
        actions = [tool(name) for name in (
            'file.write', 'file.patch', 'file.delete', 'code.run', 'code.test', 'git.commit', 'git.push')]
        actions += [{'type': kind} for kind in (
            'publish_artifact', 'publish_artifacts', 'package_delivery', 'delegate',
            'grant_artifact', 'review', 'message', 'reject')]
        for action in actions:
            with self.subTest(action=action):
                task = summary_task(); before = copy.deepcopy((task, action))
                feedback = final_summary_feedback(task, action)
                self.assertFalse(feedback['ok'])
                self.assertFalse(feedback['actionExecuted'])
                self.assertTrue(feedback['notExecuted'])
                self.assertEqual(feedback['error']['code'], 'final_summary_read_only')
                self.assertEqual((task, action), before)
                self.assertIn('final.text', feedback['instruction'])
                self.assertIn('partial/failed', feedback['instruction'])
                self.assertFalse(set(feedback) & {
                    'artifacts', 'delivery', 'validation', 'callId', 'status', 'text', 'correctActionExample'})

    def test_r10_n1_identical_byte_overwrite_still_requires_explicit_text_final(self):
        # R10 N1 used this task/path for two byte-identical writes:
        # call-636abf9f61e64aec and call-552d5abf1e744ffe, original body SHA
        # e3a6365a6d5ca76466bdd2b4a75855d4e60becd763c75bdfcb5a81120578a857.
        # The body is shortened here; the actual same-bytes/expected-SHA shape
        # is preserved, without depending on external live-run files.
        body = '# Final Editorial Synthesis\nExisting reviewed outputs and explicit scope gaps.\n'
        digest = hashlib.sha256(body.encode()).hexdigest()
        task = summary_task(id='task-4000b0fba9aa4a64')
        first = tool('file.write', path='final_editorial_synthesis.md', content=body)
        overwrite = tool('file.write', path=first['args']['path'], content=body, expectedSha256=digest)
        before = copy.deepcopy(task)
        for proposal in (first, overwrite, overwrite):
            feedback = final_summary_feedback(task, proposal)
            self.assertFalse(feedback['actionExecuted'])
            self.assertEqual(task, before, 'the pure guard must not consume retries or manufacture a final')
        self.assertEqual(hashlib.sha256(overwrite['args']['content'].encode()).hexdigest(),
                         overwrite['args']['expectedSha256'])
        self.assertIsNone(final_summary_feedback(task, {
            'type': 'final', 'status': 'partial', 'text': 'Existing product; recency evidence remains missing.'}))

    def test_r10_n2_artifact_summary_keeps_its_actual_write_and_publish_contract(self):
        task = summary_task(id='task-feb1bb134ac94f41',
                            outputContract={'extensions': ['.md'], 'kind': 'artifact', 'minCount': 1})
        for action in (
            tool('file.write', path='final_editorial_synthesis.md', content='Required actual report.'),
            {'type': 'publish_artifact', 'path': 'final_editorial_synthesis.md'},
            {'type': 'publish_artifacts', 'paths': ['final_editorial_synthesis.md']},
            {'type': 'package_delivery', 'artifactIds': ['published-report'], 'entrypoint': 'report.md'},
        ):
            with self.subTest(action=action):
                self.assertIsNone(final_summary_feedback(task, action))

    def test_readonly_tools_keep_original_permission_and_phase_decisions(self):
        for name in ('file.read', 'file.list', 'web.search', 'web.fetch', 'git.status'):
            with self.subTest(name=name):
                action = tool(name, path='imports/not-granted/source.md')
                before = copy.deepcopy(action)
                self.assertIsNone(final_summary_feedback(summary_task(), action))
                self.assertEqual(action, before, 'None is not a grant or argument repair')

    def test_final_is_never_synthesized_validated_or_promoted_by_this_guard(self):
        for status in ('done', 'partial', 'failed', 'invented', None):
            for text in ('Actual findings.', '', None):
                action = {'type': 'final', 'status': status, 'text': text}
                before = copy.deepcopy(action)
                self.assertIsNone(final_summary_feedback(summary_task(), action))
                self.assertEqual(action, before)

    def test_legacy_and_non_text_contracts_are_untouched(self):
        for contract in (None, {}, {'kind': 'delivery'}, {'kind': 'json'}, {'kind': 'artifact'},
                         'text', [], {'kind': 'Text'}):
            with self.subTest(contract=contract):
                self.assertIsNone(final_summary_feedback(summary_task(outputContract=contract), tool('file.write')))
        legacy = summary_task(); legacy.pop('outputContract')
        self.assertIsNone(final_summary_feedback(legacy, {'type': 'delegate'}))

    def test_foreign_tasks_are_not_inferred_from_title_leader_or_proposal(self):
        for marker in (None, False, 0, 1, 'true', [], {}):
            with self.subTest(marker=marker):
                task = summary_task(finalSummary=marker, title='Final summary', agentId='chief_editor')
                self.assertIsNone(final_summary_feedback(task, tool('file.write')))
        task = summary_task(); task.pop('finalSummary')
        self.assertIsNone(final_summary_feedback(task, {'type': 'delegate', 'finalSummary': True}))

    def test_finished_and_not_running_tasks_are_not_reopened(self):
        for status in ('done', 'partial', 'failed', 'skipped', 'cancelled', 'rejected',
                       'pending', 'assigned', 'retrying', 'waiting_human', 'waiting_children', None):
            task = summary_task(status=status); before = copy.deepcopy(task)
            with self.subTest(status=status):
                self.assertIsNone(final_summary_feedback(task, tool('file.write')))
                self.assertEqual(task, before)

    def test_unknown_or_malformed_protocol_stays_with_original_validator(self):
        for action in (None, [], True, 4, 'final', {}, {'type': []}, {'type': {}},
                       {'type': 'tool', 'name': []}, {'type': 'tool', 'name': {}},
                       {'type': 'tool', 'name': 'publish_artifact'},
                       {'type': 'tool', 'name': 'unknown.future_tool'},
                       {'type': 'file.write'}, {'type': 'unknown.action'}):
            with self.subTest(action=action):
                self.assertIsNone(final_summary_feedback(summary_task(), action))
        for task in (None, [], True, 'summary'):
            self.assertIsNone(final_summary_feedback(task, tool('file.write')))

    def test_existing_conversation_and_wait_actions_are_not_reinterpreted_as_product_work(self):
        for kind in ('accept', 'ask_human', 'await_task'):
            self.assertIsNone(final_summary_feedback(summary_task(), {'type': kind}))

    def test_feedback_is_bounded_and_has_no_mutable_input_aliases(self):
        task = summary_task()
        action = tool('file.write', path='new.md', content='x' * 200000)
        before = copy.deepcopy((task, action))
        feedback = final_summary_feedback(task, action)
        self.assertLess(len(str(feedback)), 2000)
        feedback['error']['message'] = 'changed'
        feedback['rejectedAction']['type'] = 'changed'
        self.assertEqual((task, action), before)
        self.assertEqual(final_summary_feedback(task, action)['rejectedAction']['type'], 'tool')


class FinalSummaryRuntimeTests(unittest.TestCase):
    body = '# Existing editorial notes\nThe example has no external factual claims.\n'
    path = 'final_editorial_synthesis.md'

    def run_summary(self, script, *, artifact_contract=False):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        config = Config(Path(folder.name), {
            'LLM_API_KEY': 'offline-summary', 'MAX_RETRIES': '0', 'MAX_AGENT_TURNS': '12',
            'MAX_MODEL_CALLS': '50', 'MAX_RUN_TOKENS': '400000', 'MAX_CONTEXT_CHARS': '40000',
        })
        agents = get_team_agents('newsroom')
        contract = ({'kind': 'artifact', 'extensions': ['.md'], 'minCount': 1}
                    if artifact_contract else {'kind': 'text'})
        plan = [
            planned_task('Prepare', 'chief_editor'),
            planned_task('Research', 'reporter', ['Prepare']),
            {**planned_task('Draft', 'editor', ['Research']),
             'outputContract': {'kind': 'artifact', 'extensions': ['.md'], 'minCount': 1}},
            {**planned_task('Review', 'fact_checker', ['Draft'], ['review']), 'reviewOf': ['Draft']},
            {**planned_task('Summary', 'chief_editor', ['Prepare', 'Research', 'Draft', 'Review']),
             'outputContract': contract},
        ]
        prepare_path = 'initial-notes.md' if artifact_contract else self.path

        def review(index, context, messages):
            if index == 0:
                return tool('file.read', path=context['authorizedArtifacts'][0]['path'])
            if index == 1:
                self.assertEqual(last_result(messages)['content'], self.body)
                return {'type': 'review', 'taskId': context['task']['reviewOf'][0],
                        'verdict': 'pass', 'feedback': 'Read the actual current fixture; no external factual claims.'}
            return final('Actual editorial review completed; no browser/code-test claim.')

        scripts = {
            'Prepare': [tool('file.write', path=prepare_path, content=self.body),
                        final('Stored actual local notes; no product acceptance claim.')],
            'Research': [final('This offline example contains no external factual assertions.')],
            'Draft': [tool('file.write', path='article.md', content=self.body),
                      {'type': 'publish_artifact', 'path': 'article.md'}, final('Published the actual fixture.')],
            'Review': review, 'Summary': script,
        }
        model = ScriptedProvider(plan, scripts)
        app = Platform(config, provider=model, tools=LocalFiles(config, agents=agents),
                       agents=agents, team_id='newsroom')

        def close():
            app.shutdown(); app.wait_for_idle(3); app.repo.close()

        self.addCleanup(close)
        app.action({'action': 'send', 'mode': 'task', 'participants': list(agents),
                    'text': 'Review existing example notes, then report their actual outcome and limitations.'})
        self.assertTrue(app.wait_for_idle(8))
        run = app.state['sessions']['group']['run']
        summary = next(row for row in run['steps'] if row.get('title') == 'Summary')
        self.assertTrue(summary['finalSummary'])
        return app, run, summary, model

    def test_actual_read_then_same_byte_write_feedback_then_real_partial_final(self):
        digest = hashlib.sha256(self.body.encode()).hexdigest()

        def summary(index, context, messages):
            if index == 0:
                return tool('file.read', path=self.path)
            if index == 1:
                self.assertEqual(last_result(messages)['sha256'], digest)
                return tool('file.write', path=self.path, content=self.body, expectedSha256=digest)
            feedback = last_result(messages)
            self.assertEqual(feedback['error']['code'], 'final_summary_read_only')
            self.assertFalse(feedback['actionExecuted'])
            return final('Actual notes and editorial review exist; independent external evidence is not provided.',
                         status='partial')

        app, run, task, model = self.run_summary(summary)
        self.assertEqual(model.counts['Summary'], 3)
        self.assertEqual(task['status'], 'partial')
        self.assertIn('independent external evidence', task['result'])
        calls = [row for row in run['calls'] if row.get('taskId') == task['id']]
        self.assertEqual([row['tool'] for row in calls if row['tool'] != 'llm.chat'], ['file.read'])
        self.assertFalse(any(row.get('taskId') == task['id'] for row in run['approvals']))
        self.assertFalse(task['outputs'])
        self.assertEqual((app.tools.workspace('chief_editor', run['id']) / self.path).read_text(), self.body)
        self.assertEqual(sum(row['tool'] == 'file.write' and row.get('agentId') == 'chief_editor'
                             for row in run['calls']), 1, 'only the actual upstream write executed')
        self.assertEqual(len([event for event in run['events'] if event['type'] == 'review_pass']), 1)
        self.assertEqual(run['budgetExtensions'], 0)

    def test_three_identical_write_proposals_use_original_loop_stop_without_fake_final(self):
        proposal = tool('file.write', path=self.path, content=self.body,
                        expectedSha256=hashlib.sha256(self.body.encode()).hexdigest())
        app, run, task, model = self.run_summary([copy.deepcopy(proposal)] * 4)
        self.assertEqual(model.counts['Summary'], 3)
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['result'], '')
        self.assertIn('重复动作循环', task['error']['message'])
        self.assertFalse(any(row.get('taskId') == task['id'] and row['tool'] == 'file.write'
                             for row in run['calls']))
        results = [json.loads(row['text']) for row in run['transcript']
                   if row.get('taskId') == task['id'] and row.get('kind') == 'action_result']
        self.assertEqual(len(results), 2)
        self.assertTrue(all(row['error']['code'] == 'final_summary_read_only' for row in results))
        proposals = [json.loads(row['text']) for row in run['transcript']
                     if row.get('taskId') == task['id'] and row.get('kind') == 'model_response']
        self.assertTrue(all(row['type'] == 'tool' for row in proposals))
        self.assertEqual((app.tools.workspace('chief_editor', run['id']) / self.path).read_text(), self.body)

    def test_artifact_summary_still_writes_publishes_and_explicitly_finishes(self):
        _, run, task, model = self.run_summary([
            tool('file.write', path=self.path, content=self.body),
            {'type': 'publish_artifact', 'path': self.path},
            final('Published the required actual summary with the editorial evidence and scope limits.'),
        ], artifact_contract=True)
        self.assertEqual(task['status'], 'done')
        self.assertEqual(model.counts['Summary'], 3)
        artifacts = [row for row in run['artifacts'] if row.get('taskId') == task['id']]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]['content'], self.body)
        self.assertEqual(task['outputs'], [artifacts[0]['id']])
        self.assertTrue(any(row.get('taskId') == task['id'] and row['tool'] == 'file.write'
                            and row['status'] == 'success' for row in run['calls']))

    def test_artifact_summary_private_file_without_publication_remains_partial(self):
        app, run, task, _ = self.run_summary([
            tool('file.write', path=self.path, content=self.body),
            *[final('Incorrect claim that the private draft was delivered.')] * 4,
        ], artifact_contract=True)
        self.assertEqual(task['status'], 'partial')
        self.assertEqual(task['error']['code'], 'output_contract')
        self.assertFalse(task['outputs'])
        self.assertFalse(any(row.get('taskId') == task['id'] for row in run['artifacts']))
        self.assertEqual((app.tools.workspace('chief_editor', run['id']) / self.path).read_text(), self.body)


if __name__ == '__main__':
    unittest.main()
