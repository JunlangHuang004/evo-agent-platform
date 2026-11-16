"""Read-only review of voluntary partials; scripted models and local bytes only."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.partial_review import (budget_partial_receipt, declared_partial_receipt,
                                    review_dependency_exceptions)
from backend.tools import ToolGateway
from backend.teams import get_team_agents
from tests.test_engine import ScriptedProvider, final, task


def fixture():
    content = 'Draft findings: the signing date remains to be checked.\n'
    sha = hashlib.sha256(content.encode()).hexdigest()
    producer = {'id': 'report', 'agentId': 'reporter', 'revision': 0, 'status': 'partial',
                'error': None, 'result': 'Published actual findings, with an unresolved date.',
                'outputs': ['findings'], 'finishedAt': 1794770399774}
    reviewer = {'id': 'check', 'agentId': 'fact_checker', 'requiredCapabilities': ['review'],
                'dependsOn': ['report'], 'status': 'pending'}
    run = {'id': 'run-current', 'steps': [producer, reviewer],
           'artifacts': [{'id': 'findings', 'ownerAgentId': 'reporter', 'runId': 'run-current',
                          'taskId': 'report', 'path': 'findings.md', 'name': 'findings.md',
                          'content': content, 'sha256': sha, 'size': len(content.encode())}],
           'calls': [{'id': 'write', 'agentId': 'reporter', 'taskId': 'report', 'revision': 0,
                      'tool': 'file.write', 'status': 'success', 'args': {'path': 'findings.md'},
                      'result': {'ok': True, 'sha256': sha}},
                     {'id': 'final-model', 'agentId': 'reporter', 'taskId': 'report', 'revision': 0,
                      'tool': 'llm.chat', 'status': 'success', 'args': {}}],
           'events': [{'id': 'registered', 'actor': 'reporter', 'agentId': 'reporter',
                       'type': 'artifact_registered', 'time': '2026-11-15T19:19:47Z',
                       'payload': {'taskId': 'report', 'artifactId': 'findings', 'sha256': sha}},
                      {'id': 'finished', 'actor': 'reporter', 'agentId': 'reporter',
                       'type': 'task_finished', 'time': '2026-11-15T19:19:59Z',
                       'payload': {'taskId': 'report', 'status': 'partial'}}],
           'transcript': [{'kind': 'model_response', 'role': 'agent', 'agentId': 'reporter',
                           'runId': 'run-current', 'taskId': 'report', 'callId': 'final-model',
                           'time': '2026-11-15T19:19:59Z',
                           'text': json.dumps({'type': 'final', 'status': 'partial', 'text': producer['result']})}]}
    agents = {'reporter': {'capabilities': ['research']},
              'fact_checker': {'capabilities': ['review']}, 'editor': {'capabilities': ['editing']}}
    return run, producer, reviewer, agents


class DeclaredPartialTests(unittest.TestCase):
    def test_only_explicit_independent_review_gets_unassessed_read_only_receipt(self):
        run, producer, reviewer, agents = fixture()
        before = copy.deepcopy(run)
        receipt = declared_partial_receipt(run, producer)
        self.assertEqual(receipt['closure'], {'modelCallId': 'final-model', 'taskFinishedEventId': 'finished'})
        self.assertEqual(receipt['acceptance'], 'unassessed')
        self.assertEqual(receipt['executionStatus'], 'partial')
        self.assertIsNone(budget_partial_receipt(run, producer))
        self.assertEqual(review_dependency_exceptions(run, reviewer, agents), {'report': receipt})
        for changes in ({'agentId': 'editor'}, {'agentId': 'reporter'}, {'requiredCapabilities': []},
                        {'dependsOn': []}, {'finalSummary': True}):
            with self.subTest(changes=changes):
                self.assertEqual(review_dependency_exceptions(run, {**reviewer, **changes}, agents), {})
        self.assertEqual(run, before)

    def test_status_alone_or_a_failed_generation_cannot_forge_voluntary_closure(self):
        mutations = [lambda r: r['steps'][0].update(error={'code': 'runtime'}),
                     lambda r: r['steps'][0].update(error=''),
                     lambda r: r['steps'][0].update(status='failed'),
                     lambda r: r['steps'][0].update(result='I changed the stored claim'),
                     lambda r: r.update(transcript=[]),
                     lambda r: r.update(events=[]),
                     lambda r: r['calls'][-1].update(status='failed'),
                     lambda r: r['calls'][-1].update(revision=False),
                     lambda r: r['steps'][0].update(revision=1),
                     lambda r: r['transcript'][-1].update(callId='other'),
                     lambda r: r['transcript'][-1].update(agentId='editor'),
                     lambda r: r['transcript'][-1].update(runId='old-run'),
                     lambda r: r['events'][-1].update(actor='editor'),
                     lambda r: r['events'][-1]['payload'].update(revision=False),
                     lambda r: r['transcript'][-1].update(time='2026-11-15T19:20:00Z'),
                     lambda r: r['events'][-1].update(time='invalid'),
                     lambda r: r['steps'][0].update(finishedAt=1794770400000),
                     lambda r: r.update(finishedAt=1794770399000),
                     lambda r: r['events'].append({'type': 'task_failed', 'payload': {'taskId': 'report'}})]
        for mutation in mutations:
            run, producer, _, _ = fixture()
            mutation(run)
            with self.subTest(mutation=mutation):
                self.assertIsNone(declared_partial_receipt(run, producer))
        for status in ('done', 'failed'):
            run, producer, _, _ = fixture()
            action = json.loads(run['transcript'][-1]['text']); action['status'] = status
            run['transcript'][-1]['text'] = json.dumps(action)
            self.assertIsNone(declared_partial_receipt(run, producer))

    def test_published_current_owner_bytes_are_required_not_an_inherited_or_stale_file(self):
        mutations = [lambda r: r['steps'][0].update(outputs=[]),
                     lambda r: r['artifacts'][0].update(content='not the registered bytes'),
                     lambda r: r['artifacts'][0].update(size=True),
                     lambda r: r['artifacts'][0].update(ownerAgentId='editor'),
                     lambda r: r['artifacts'][0].update(taskId='other'),
                     lambda r: r['artifacts'][0].update(sourceRunId='previous'),
                     lambda r: r['artifacts'][0].update(inheritedReadOnly=True),
                     lambda r: r['events'][0]['payload'].update(sha256='0' * 64),
                     lambda r: r['events'][0].update(time='2026-11-15T19:20:00Z'),
                     lambda r: r['events'].pop(0),
                     lambda r: r['calls'][0].update(revision=None),
                     lambda r: r['artifacts'].append({**r['artifacts'][0], 'id': 'new-registration'})]
        for mutation in mutations:
            run, producer, _, _ = fixture(); mutation(run)
            with self.subTest(mutation=mutation):
                self.assertIsNone(declared_partial_receipt(run, producer))

    def test_unfinished_and_unknown_effects_or_later_mutations_fail_closed(self):
        for key in ('_pendingTool', '_uncertainCalls', '_reworkWait', '_approvalId'):
            run, producer, _, _ = fixture(); producer[key] = {'unfinished': True}
            self.assertIsNone(declared_partial_receipt(run, producer))
        for status in ('pending', 'running', 'uncertain', 'unknown', 'failed', 'reconciled'):
            run, producer, _, _ = fixture()
            run['calls'].insert(-1, {**run['calls'][0], 'id': 'later-mutation', 'status': status,
                                      'result': {'ok': False}})
            with self.subTest(status=status):
                self.assertIsNone(declared_partial_receipt(run, producer))
        run, producer, _, _ = fixture()
        run['approvals'] = [{'id': 'waiting', 'taskId': 'report', 'status': 'pending'}]
        self.assertIsNone(declared_partial_receipt(run, producer))
        run, producer, _, _ = fixture()
        run['calls'].insert(-1, {'id': 'unknown-web', 'agentId': 'reporter', 'taskId': 'report',
                                 'tool': 'web.fetch', 'status': 'failed', 'result': {'outcomeUnknown': True}})
        self.assertIsNone(declared_partial_receipt(run, producer))

    def test_known_failed_web_fetch_and_incomplete_contract_do_not_prevent_inspection(self):
        run, producer, _, _ = fixture()
        producer['outputContract'] = {'kind': 'artifact', 'extensions': ['.html']}
        producer['outputValidation'] = {'status': 'failed', 'issues': ['Only reporting notes exist.']}
        run['calls'].insert(-1, {'id': 'blocked-public-page', 'agentId': 'reporter', 'taskId': 'report',
                                 'revision': 0, 'tool': 'web.fetch', 'status': 'failed',
                                 'result': {'ok': False, 'error': {'code': 'permanent', 'message': 'HTTP 403'}}})
        receipt = declared_partial_receipt(run, producer)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt['acceptance'], 'unassessed')
        self.assertEqual(producer['outputValidation']['status'], 'failed')

    def test_valid_finished_at_before_next_second_event_is_not_rejected(self):
        run, producer, _, _ = fixture()
        run['events'][-1]['time'] = '2026-11-15T19:20:00Z'
        # Worker stores finishedAt before emitting its event; these operations
        # can legitimately straddle the second boundary.
        producer['finishedAt'] = 1794770399999
        self.assertIsNotNone(declared_partial_receipt(run, producer))

    def test_malformed_records_and_ambiguous_final_call_are_not_trusted(self):
        for field in ('calls', 'artifacts', 'transcript', 'events', 'approvals'):
            run, producer, _, _ = fixture(); run[field] = [None]
            self.assertIsNone(declared_partial_receipt(run, producer))
        run, producer, _, _ = fixture(); run['calls'].insert(-1, copy.deepcopy(run['calls'][-1]))
        self.assertIsNone(declared_partial_receipt(run, producer))
        run, producer, _, _ = fixture(); run['transcript'][-1]['text'] = '{bad JSON'
        self.assertIsNone(declared_partial_receipt(run, producer))


class OfflinePrimaryFiles(ToolGateway):
    """Actual file/artifact operations; synthetic primary-page bytes, no HTTP."""
    def capabilities(self):
        return {}

    def _web_fetch(self, url, deadline=None):
        return {'ok': True, 'url': url, 'content': 'Official fixture: signing occurred on 14 October; announcement on 9 November.',
                'truncated': False}


class DeclaredPartialWorkflowTests(unittest.TestCase):
    def test_read_only_assessment_does_not_complete_partial_or_open_normal_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp), {'LLM_API_KEY': 'offline-not-a-real-key', 'MAX_AGENT_TURNS': '8',
                'MAX_MODEL_CALLS': '30', 'MAX_RUN_TOKENS': '400000'})
            plan = [task('Research', 'reporter'), task('Fact check', 'fact_checker', ['Research'], ['review']),
                    task('Edit', 'editor', ['Research', 'Fact check'])]
            def assess(index,context,messages):
                if index==0:
                    return {'type':'tool','name':'file.read','args':{'path':context['authorizedArtifacts'][0]['path']}}
                if index==1:
                    return {'type':'review','taskId':context['dependencyResults'][0]['id'],'verdict':'pass',
                            'feedback':'Actual incomplete source list inspected; editorial assessment only, no business completion claimed.'}
                return final('Incomplete source list inspected; no business acceptance claimed.')
            scripts = {'Research': [
                {'type': 'tool', 'name': 'file.write', 'args': {'path': 'findings.md', 'content': 'Real incomplete notes.\n'}},
                {'type': 'publish_artifact', 'path': 'findings.md'}, final('One source remains missing.', status='partial')],
                'Fact check': assess}
            agents = get_team_agents('newsroom'); model = ScriptedProvider(plan, scripts)
            app = Platform(config, provider=model, tools=OfflinePrimaryFiles(config, agents=agents),
                           agents=agents, team_id='newsroom')
            try:
                app.action({'action': 'send', 'mode': 'task', 'participants': ['reporter', 'fact_checker', 'editor'],
                            'text': 'Inspect incomplete findings without inventing completion.'})
                self.assertTrue(app.wait_for_idle(5))
                run = app.snapshot()['sessions']['group']['run']; by = {t['title']: t for t in run['steps']}
                self.assertEqual(by['Research']['status'], 'partial')
                self.assertIsNone(by['Research']['error'])
                self.assertEqual(by['Fact check']['status'], 'done')
                self.assertEqual(by['Edit']['status'], 'skipped')
                self.assertEqual(model.counts['Edit'], 0)
                self.assertEqual(run['status'], 'partial')
                self.assertFalse(by['Research'].get('validation', {}).get('verified'))
            finally:
                app.shutdown(); app.wait_for_idle(4); app.repo.close()

    def test_review_reads_bytes_reworks_then_original_producer_redelivers_before_editor(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp), {'LLM_API_KEY': 'offline-not-a-real-key', 'MAX_AGENT_TURNS': '12',
                'MAX_RETRIES': '1', 'MAX_MODEL_CALLS': '40', 'MAX_RUN_TOKENS': '400000'})
            plan = [task('Research', 'reporter'), task('Fact check', 'fact_checker', ['Research'], ['review']),
                    task('Edit', 'editor', ['Fact check'])]
            reads = []

            def research(index, context, messages):
                revised = bool(context['task'].get('feedback'))
                path = 'findings-v2.md' if revised else 'findings-v1.md'
                body = 'Signing: 14 October; announcement: 9 November.\n' if revised else 'Signing: 9 November.\n'
                return [{'type': 'tool', 'name': 'file.write', 'args': {'path': path, 'content': body}},
                        {'type': 'publish_artifact', 'path': path},
                        final('Corrected current findings.' if revised else 'Published draft with a date requiring verification.',
                              status='done' if revised else 'partial')][index % 3]

            def inspect(index, context, messages):
                target = context['dependencyResults'][0]
                if index in (0, 3):
                    if index == 3:
                        real = next(m for m in reversed(messages) if m['content'].startswith('动作的真实结果：'))
                        rows = json.loads(real['content'].split('：', 1)[1])['authorizedArtifacts']
                    else:
                        rows = context['authorizedArtifacts']
                    entry = next((a for a in rows if a['name'] == ('findings-v2.md' if index == 3 else 'findings-v1.md')), None)
                    self.assertIsNotNone(entry)
                    if index == 0:
                        self.assertEqual(target['status'], 'partial')
                        self.assertIsNone(target['error'])
                    return {'type': 'tool', 'name': 'file.read', 'args': {'path': entry['path']}}
                if index in (1, 4):
                    real = next(m for m in reversed(messages) if m['content'].startswith('动作的真实结果：'))
                    receipt = json.loads(real['content'].split('：', 1)[1]); reads.append(receipt['content'])
                    if index == 1:
                        self.assertEqual(receipt['content'], 'Signing: 9 November.\n')
                        return {'type': 'tool', 'name': 'web.fetch', 'args': {'url': 'https://example.org/official-fixture'}}
                    self.assertIn('14 October', receipt['content'])
                    return {'type': 'review', 'taskId': target['id'], 'verdict': 'pass',
                            'feedback': 'Current revised document agrees with the independently read primary fixture; editorial assessment only.'}
                if index == 2:
                    real = next(m for m in reversed(messages) if m['content'].startswith('动作的真实结果：'))
                    primary = json.loads(real['content'].split('：', 1)[1])
                    self.assertIn('14 October', primary['content'])
                    return {'type': 'review', 'taskId': target['id'], 'verdict': 'rework',
                            'feedback': 'Primary fixture gives 14 October signing versus 9 November announcement. Correct the date and republish.'}
                return final('Independently read and assessed corrected current findings; no executable verification claim.')

            model = ScriptedProvider(plan, {'Research': research, 'Fact check': inspect})
            agents = get_team_agents('newsroom')
            app = Platform(config, provider=model, tools=OfflinePrimaryFiles(config, agents=agents),
                           agents=agents, team_id='newsroom')
            try:
                app.action({'action': 'send', 'mode': 'task', 'participants': ['reporter', 'fact_checker', 'editor'],
                            'text': 'Produce reporting notes, independently check them, then edit.'})
                self.assertTrue(app.wait_for_idle(6))
                run = app.snapshot()['sessions']['group']['run']; by = {t['title']: t for t in run['steps']}
                self.assertEqual(by['Research']['revision'], 1, json.dumps({'tasks': [{k:t.get(k) for k in ('title','status','error','revision','result')} for t in run['steps']], 'calls': [{k:c.get(k) for k in ('tool','agentId','status','result')} for c in run['calls'] if c.get('tool')!='llm.chat']}, ensure_ascii=False))
                self.assertEqual(by['Research']['agentId'], 'reporter')
                self.assertEqual(by['Research']['status'], 'done')
                self.assertEqual(by['Fact check']['status'], 'done', json.dumps(by['Fact check'], ensure_ascii=False))
                self.assertEqual(by['Edit']['status'], 'done')
                self.assertEqual(reads, ['Signing: 9 November.\n', 'Signing: 14 October; announcement: 9 November.\n'])
                self.assertEqual(by['Research']['validationHistory'][0]['verdict'], 'rework')
                self.assertEqual(by['Research']['validation']['kind'], 'editorial_assessment')
                self.assertFalse(by['Research']['validation']['verified'])
                self.assertEqual(by['Research']['validation']['evidenceCallIds'], [])
                writes = [c for c in run['calls'] if c['tool'] == 'file.write' and c['taskId'] == by['Research']['id']]
                self.assertEqual([(c['agentId'], c['revision']) for c in writes], [('reporter', 0), ('reporter', 1)])
                self.assertTrue(any(e['type'] == 'task_finished' and e['payload'].get('taskId') == by['Research']['id']
                                    and e['payload'].get('status') == 'partial' for e in run['events']))
                self.assertEqual(model.counts['Research'], 6)
            finally:
                app.shutdown(); app.wait_for_idle(4); app.repo.close()


if __name__ == '__main__':
    unittest.main()
