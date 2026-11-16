"""Review schema feedback does not convert, replay, authorize or verify actions."""
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.feedback_context import latest_feedback_proof
from backend.runtime import compress_messages
from tests.test_engine import ScriptedProvider, final, task
from tests.test_execution_closure import LocalFiles, tool


def result_message(messages):
    return json.loads(next(row['content'] for row in reversed(messages)
                           if row['content'].startswith('动作的真实结果：')).split('：', 1)[1])


class ReviewSchemaFeedbackTests(unittest.TestCase):
    def setUp(self):self.temp = tempfile.TemporaryDirectory(); self.apps = []
    def tearDown(self):
        for app in self.apps:
            app.shutdown(); app.wait_for_idle(4); app.repo.close()
        self.temp.cleanup()

    def make_app(self, qa, *, actor='reviewer', html=False):
        config = Config(Path(self.temp.name)/str(len(self.apps)), {'LLM_API_KEY': 'offline-not-a-real-key',
            'MAX_RETRIES': '1', 'MAX_MODEL_CALLS': '50', 'MAX_AGENT_TURNS': '14',
            'MAX_RUN_TOKENS': '400000', 'MAX_CONTEXT_CHARS': '40000'})
        extension = '.html' if html else '.md'
        plan = [{**task('Build', 'executor'), 'outputContract': {'kind': 'artifact', 'extensions': [extension]}},
                task('Check', actor, ['Build'], ['review'] if actor == 'reviewer' else [])]
        def build(index, context, messages):
            revised = bool(context['task'].get('feedback'))
            path = 'output-v'+str(int(revised)+1)+extension
            body = '<html><body>Version 1</body></html>' if html else 'Version '+str(int(revised)+1)
            return [tool('file.write', path=path, content=body),
                    {'type': 'publish_artifact', 'path': path}, final('Published actual current output.')][index % 3]
        model = ScriptedProvider(plan, {'Build': build, 'Check': qa})
        app = Platform(config, provider=model, tools=LocalFiles(config)); model.app = app
        self.apps.append(app)
        return app, model, ['executor', actor]

    def send(self, app, participants):
        app.action({'action': 'send', 'mode': 'task', 'participants': participants,
                    'text': 'Review the actual published output under the unchanged evidence rules.'})
        self.assertTrue(app.wait_for_idle(7))
        return app.snapshot()['sessions']['group']['run']

    def test_only_explicit_corrected_review_changes_revision_and_real_evidence_binds_recheck(self):
        for invalid in ('nested_args', 'wrong_verdict'):
            with self.subTest(invalid=invalid):
                observed = {}
                def qa(index, context, messages):
                    current = app.state['sessions']['group']['run']
                    producer = next(t for t in current['steps'] if t['title'] == 'Build')
                    reviewer = next(t for t in current['steps'] if t['title'] == 'Check')
                    target = producer['id']
                    if index == 0:
                        fields = {'taskId': target, 'verdict': 'rework', 'feedback': 'Revise the actual text.'}
                        bad = {'type': 'review', 'args': fields} if invalid == 'nested_args' else {
                            'type': 'review', **fields, 'verdict': 'please_revise'}
                        observed['bad'] = bad
                        return bad
                    if index == 1:
                        receipt = result_message(messages)
                        self.assertFalse(receipt['ok']); self.assertEqual(receipt['error']['code'], 'protocol')
                        self.assertIn('顶层', receipt['error']['message'])
                        self.assertIn('args', receipt['error']['message'])
                        self.assertIn('未执行', receipt['error']['message'])
                        self.assertIn('ACTUAL_TARGET_TASK_ID', receipt['error']['message'])
                        self.assertEqual(producer['revision'], 0); self.assertEqual(producer['status'], 'done')
                        self.assertFalse(producer.get('validation')); self.assertFalse(producer.get('validationHistory'))
                        self.assertFalse(any(e['type'].startswith('review_') for e in current['events']))
                        self.assertEqual(model.counts['Build'], 3)
                        # Real original JSON remains unconverted in the audit and
                        # protected feedback, rather than an inferred executable action.
                        proposal = next(row for row in reversed(current['transcript']) if row.get('kind') == 'model_response'
                                        and row.get('taskId') == reviewer['id'])
                        self.assertEqual(json.loads(proposal['text']), observed['bad'])
                        proof = latest_feedback_proof(current, reviewer); self.assertIsNotNone(proof)
                        compressed = compress_messages(messages, 40000, trusted_feedback=proof)
                        self.assertEqual(result_message(compressed)['error']['code'], 'protocol')
                        return {'type': 'review', 'taskId': target, 'verdict': 'rework',
                                'feedback': 'Revise the actual text.'}
                    if index == 2:
                        self.assertEqual(producer['revision'], 1); self.assertEqual(producer['status'], 'done')
                        refreshed = result_message(messages)['authorizedArtifacts']
                        artifact = next(row for row in refreshed if row['name'] == 'output-v2.md')
                        return tool('file.read', path=artifact['path'])
                    if index == 3:
                        self.assertEqual(result_message(messages)['content'], 'Version 2')
                        read = next(c for c in reversed(current['calls']) if c.get('taskId') == reviewer['id']
                                    and c.get('tool') == 'file.read' and c.get('status') == 'success')
                        return {'type': 'review', 'taskId': target, 'verdict': 'pass',
                                'feedback': 'Read and assessed the current revised actual text; editorial assessment only.',
                                'evidenceCallIds': [read['id']]}
                    return final('Current revised output independently assessed; no executable verification claim.')
                app, model, participants = self.make_app(qa)
                run = self.send(app, participants); by = {t['title']: t for t in run['steps']}
                self.assertEqual(by['Build']['revision'], 1)
                self.assertEqual(model.counts['Build'], 6); self.assertEqual(model.counts['Check'], 5)
                reviews = [e for e in run['events'] if e['type'].startswith('review_')]
                self.assertEqual([e['type'] for e in reviews], ['review_rework', 'review_pass'])
                self.assertEqual(by['Build']['validation']['targetRevision'], 1)
                self.assertEqual(by['Build']['validation']['kind'], 'editorial_assessment')
                self.assertFalse(by['Build']['validation']['verified'])
                self.assertEqual(len(by['Build']['validation']['evidenceCallIds']), 1)
                persisted = app.repo.get_run(run['id'])
                self.assertEqual(next(t for t in persisted['steps'] if t['id'] == by['Build']['id'])['revision'], 1)

    def test_three_identical_nested_reviews_still_stop_without_any_review_or_revision(self):
        def qa(index, context, messages):
            return {'type': 'review', 'args': {'taskId': context['dependencyResults'][0]['id'],
                    'verdict': 'rework', 'feedback': 'A repeated malformed proposal.'}}
        app, model, participants = self.make_app(qa)
        run = self.send(app, participants); by = {t['title']: t for t in run['steps']}
        self.assertEqual(model.counts['Check'], 3)
        self.assertEqual(by['Check']['status'], 'failed'); self.assertEqual(by['Check']['error']['code'], 'protocol')
        self.assertIn('重复动作循环', by['Check']['error']['message'])
        self.assertEqual(by['Build']['revision'], 0); self.assertFalse(by['Build'].get('validationHistory'))
        self.assertFalse(any(e['type'].startswith('review_') for e in run['events']))
        errors = [json.loads(row['text']) for row in run['transcript'] if row.get('kind') == 'action_result'
                  and row.get('taskId') == by['Check']['id']]
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(e['error']['code'] == 'protocol' and '顶层' in e['error']['message'] for e in errors))

    def test_capability_gate_still_precedes_schema_help_and_rejects_corrected_unauthorized_review(self):
        def qa(index, context, messages):
            fields = {'taskId': context['dependencyResults'][0]['id'], 'verdict': 'rework', 'feedback': 'No permission.'}
            if index == 0:return {'type': 'review', 'args': fields}
            receipt = result_message(messages)
            self.assertEqual(receipt['error']['code'], 'protocol')
            self.assertIn('只有具有review能力', receipt['error']['message'])
            self.assertNotIn('ACTUAL_TARGET_TASK_ID', receipt['error']['message'])
            if index == 1:return {'type': 'review', **fields}
            return final('No review permission; no review was performed.', status='partial')
        app, model, participants = self.make_app(qa, actor='research')
        run = self.send(app, participants); by = {t['title']: t for t in run['steps']}
        self.assertEqual(model.counts['Check'], 3); self.assertEqual(by['Check']['status'], 'partial')
        self.assertEqual(by['Build']['revision'], 0); self.assertFalse(by['Build'].get('validation'))
        self.assertFalse(any(e['type'].startswith('review_') for e in run['events']))

    def test_correct_schema_does_not_let_a_fake_test_id_verify_html(self):
        def qa(index, context, messages):
            fields = {'taskId': context['dependencyResults'][0]['id'], 'verdict': 'pass',
                      'feedback': 'I claim the browser passed.', 'evidenceCallIds': ['invented-browser-test']}
            if index == 0:return {'type': 'review', 'args': fields}
            receipt = result_message(messages)
            if index == 1:
                self.assertEqual(receipt['error']['code'], 'protocol')
                self.assertFalse(any(e['type'].startswith('review_') for e in app.state['sessions']['group']['run']['events']))
                return {'type': 'review', **fields}
            self.assertFalse(receipt['ok']); self.assertEqual(receipt['error']['code'], 'verification')
            self.assertFalse(receipt['validation']['verified'])
            return final('The current HTML has no independent browser evidence.', status='partial')
        app, model, participants = self.make_app(qa, html=True)
        run = self.send(app, participants); by = {t['title']: t for t in run['steps']}
        self.assertEqual(by['Build']['revision'], 0); self.assertFalse(by['Build']['validation']['verified'])
        reviews = [e for e in run['events'] if e['type'].startswith('review_')]
        self.assertEqual(len(reviews), 1)
        self.assertFalse(reviews[0]['payload']['validation']['verified'])
        self.assertEqual(reviews[0]['payload']['validation']['evidenceCallIds'], [])
        self.assertFalse(any(c['tool'] == 'code.test' for c in run['calls']))


if __name__ == '__main__':unittest.main()
