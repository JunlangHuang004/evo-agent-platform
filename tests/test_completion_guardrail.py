"""Real local artifacts and scripted decisions; no model/network/Docker calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.completion_guardrail import completion_clarification_feedback
from backend.config import Config
from backend.engine import Platform
from backend.feedback_context import latest_feedback_proof
from backend.provider import ProviderError
from backend.runtime import compress_messages
from backend.stage_control import stage_control
from backend.teams import get_team_agents
from backend.tools import ToolGateway
from tests.test_declared_partial_review import fixture
from tests.test_engine import ScriptedProvider, final, task


def candidate():
    run, producer, _, _ = fixture()
    run['status'] = 'running'
    run['budget'] = {'profile': 'time_only', 'deadlineAt': 9999999999999, 'timeMs': 1800000,
                     'tokens': None, 'modelCalls': None, 'agentTurns': None}
    producer.update(status='running', acceptanceCriteria=['Rate each claim, including uncertainty.',
                                                        'Flag weak or conflicting sources.'],
                    outputContract={'kind': 'artifact', 'extensions': ['.md'], 'minCount': 1})
    action = json.loads(run['transcript'][-1]['text'])
    validation = {'status': 'passed', 'kind': 'artifact', 'semanticQuality': 'unassessed',
                  'issues': [], 'checkedArtifactIds': ['findings'], 'matchedArtifactIds': ['findings']}
    return run, producer, action, validation


def feedback(run, producer, action, validation, **kwargs):
    return completion_clarification_feedback(run, producer, action, validation, now_ms=1000, **kwargs)


class CompletionGuardrailEligibilityTests(unittest.TestCase):
    def test_bound_feedback_is_nonmutating_and_not_a_semantic_pass(self):
        run, producer, action, validation = candidate()
        before = copy.deepcopy((run, action, validation))
        result = feedback(run, producer, action, validation)
        self.assertEqual(result['error']['code'], 'completion_scope_clarification')
        self.assertEqual(result['binding']['modelCallId'], 'final-model')
        self.assertEqual(result['binding']['artifactHashes'], {'findings': run['artifacts'][0]['sha256']})
        self.assertEqual(result['acceptanceCriteria'], producer['acceptanceCriteria'])
        self.assertEqual(result['semanticQuality'], 'unassessed')
        self.assertIn('You may keep partial or failed', result['instruction'])
        self.assertEqual((run, action, validation), before)

    def test_stage_final_summary_final_only_and_expired_deadline_never_get_extra_request(self):
        run, producer, action, validation = candidate()
        for options in ({'final_only': True}, {'final_only': True, 'stage_phase': 'handoff_final'}):
            with self.subTest(options=options):
                self.assertIsNone(feedback(run, producer, action, validation, **options))
        for changes in ({'finalSummary': True}, {'error': {'code': 'budget'}},
                        {'error': {'code': 'output_contract'}}, {'_recheckTargets': {'other': 0}}):
            r, p, a, v = candidate(); p.update(changes)
            with self.subTest(changes=changes):self.assertIsNone(feedback(r, p, a, v))
        run['budget']['deadlineAt'] = 1000
        self.assertIsNone(feedback(run, producer, action, validation))

    def test_soft_handoff_is_not_an_expired_wall_budget_or_a_second_deadline(self):
        run, producer, action, validation = candidate()
        original = copy.deepcopy(run['budget'])
        result = feedback(run, producer, action, validation, stage_phase='handoff_final')
        self.assertIsNotNone(result)
        self.assertEqual(result['observedStagePhase'], 'handoff_final')
        self.assertEqual(run['budget'], original)
        self.assertIsNone(producer.get('error'))

    def test_pending_unknown_effects_and_unknown_usage_fail_closed(self):
        for key in ('_pendingTool', '_uncertainCalls', '_approvalId', '_reworkWait'):
            r, p, a, v = candidate(); p[key] = {'pending': True}
            with self.subTest(key=key):self.assertIsNone(feedback(r, p, a, v))
        for changes in ({'status': 'running'}, {'status': 'unknown'}, {'usageUnknown': True},
                        {'result': {'outcomeUnknown': True}},
                        {'error': {'transport': {'requestMayContinueServerSide': True}}}):
            r, p, a, v = candidate(); r['calls'][-1].update(changes)
            with self.subTest(changes=changes):self.assertIsNone(feedback(r, p, a, v))
        r, p, a, v = candidate(); r['approvals'] = [{'taskId': p['id'], 'status': 'pending'}]
        self.assertIsNone(feedback(r, p, a, v))

    def test_closed_historical_generation_keeps_unknown_usage_without_becoming_unknown_tool_effect(self):
        r, p, a, v = candidate()
        failed = {'id': 'failed-generation', 'agentId': p['agentId'], 'taskId': p['id'], 'revision': 0,
                  'tool': 'llm.chat', 'status': 'failed', 'args': {}, 'usageUnknown': True,
                  'error': {'code': 'transport', 'phase': 'wait_headers', 'transport': {
                      'generationRetryEligible': True, 'retryScope': 'model_generation_only',
                      'upstreamRetryable': True, 'workerTerminated': True, 'workerReaped': True,
                      'modelResultAccepted': False, 'usageUnknown': True, 'requestMayContinueServerSide': True}}}
        r['calls'].insert(-1, failed)
        before = copy.deepcopy(r)
        self.assertIsNotNone(feedback(r, p, a, v))
        self.assertEqual(r, before)
        self.assertTrue(failed['usageUnknown'])
        mutations = [lambda c: c['error']['transport'].pop('workerTerminated'),
                     lambda c: c['error']['transport'].update(workerReaped=False),
                     lambda c: c['error']['transport'].update(modelResultAccepted=True),
                     lambda c: c['error']['transport'].update(upstreamRetryable=False),
                     lambda c: c['error']['transport'].update(httpStatus=401),
                     lambda c: c['error']['transport'].update(retryScope='task'),
                     lambda c: c['error'].update(code='permanent'),
                     lambda c: c.update(tool='web.fetch'),
                     lambda c: c.update(status='running'),
                     lambda c: c.update(revision=False),
                     lambda c: c.update(outcomeUnknown=True),
                     lambda c: c.update(result={'outcomeUnknown': True})]
        for mutation in mutations:
            changed = copy.deepcopy(before); call = changed['calls'][-2]; mutation(call)
            with self.subTest(mutation=mutation):
                self.assertIsNone(feedback(changed, changed['steps'][0], a, v))
        changed = copy.deepcopy(before)
        changed['calls'][-1]['usageUnknown'] = True
        self.assertIsNone(feedback(changed, changed['steps'][0], a, v),
                          'The current proposal itself must have a completed accepted response, not unknown usage.')

    def test_even_unflagged_failed_generation_needs_its_closed_worker_receipt(self):
        r, p, a, v = candidate()
        r['calls'].insert(-1, {'id': 'unclosed', 'agentId': p['agentId'], 'taskId': p['id'], 'revision': 0,
                              'tool': 'llm.chat', 'status': 'failed', 'args': {},
                              'error': {'code': 'network'}})
        self.assertIsNone(feedback(r, p, a, v))

    def test_current_owner_revision_actual_proposal_and_new_bytes_are_required(self):
        mutations = [lambda r, p: p.update(revision=False),
                     lambda r, p: r['calls'][-1].update(revision=1),
                     lambda r, p: r['calls'][-1].update(agentId='editor'),
                     lambda r, p: r['calls'].append(copy.deepcopy(r['calls'][-1])),
                     lambda r, p: r['transcript'][-1].update(callId='invented'),
                     lambda r, p: r['transcript'][-1].update(runId='other'),
                     lambda r, p: r['transcript'][-1].update(role='user'),
                     lambda r, p: r['artifacts'][0].update(inheritedReadOnly=True),
                     lambda r, p: r['artifacts'][0].update(content='different bytes'),
                     lambda r, p: r['calls'].insert(-1, {**r['calls'][0], 'id': 'late-delete', 'tool': 'file.delete'})]
        for mutation in mutations:
            r, p, a, v = candidate(); mutation(r, p)
            with self.subTest(mutation=mutation):self.assertIsNone(feedback(r, p, a, v))
        r, p, a, v = candidate(); a['text'] = 'A user-supplied lookalike final'
        self.assertIsNone(feedback(r, p, a, v))

    def test_missing_or_failed_contract_and_self_claims_do_not_make_a_candidate(self):
        for changes in ({'status': 'failed'}, {'status': 'not_configured'}, {'kind': 'text'},
                        {'matchedArtifactIds': []}, {'matchedArtifactIds': ['different']},
                        {'matchedArtifactIds': [{}]}, {'semanticQuality': 'passed'}):
            r, p, a, v = candidate(); v.update(changes)
            with self.subTest(changes=changes):self.assertIsNone(feedback(r, p, a, v))
        for contract in (None, {'kind': 'text'}, {'kind': 'delivery'}):
            r, p, a, v = candidate(); p['outputContract'] = contract
            a['text'] = 'All criteria MET, trust me.'
            r['transcript'][-1]['text'] = json.dumps(a)
            self.assertIsNone(feedback(r, p, a, v))

    def test_once_per_revision_is_durable_and_a_new_actual_revision_gets_its_own_chance(self):
        r, p, a, v = candidate(); first = feedback(r, p, a, v)
        p['_completionClarification'] = copy.deepcopy(first['binding'])
        r = json.loads(json.dumps(r)); p = r['steps'][0]
        self.assertIsNone(feedback(r, p, a, v))
        p['revision'] = 1
        for call in r['calls']:call['revision'] = 1
        self.assertEqual(feedback(r, p, a, v)['binding']['revision'], 1)
        p['_completionClarification']['revision'] = True
        self.assertIsNone(feedback(r, p, a, v))

    def test_overlarge_criteria_decline_feedback_instead_of_dropping_requirements(self):
        r, p, a, v = candidate(); p['acceptanceCriteria'] = ['Mandatory scope '+str(i)+'x'*980 for i in range(12)]
        self.assertIsNone(feedback(r, p, a, v))
        self.assertEqual(len(p['acceptanceCriteria']), 12)


class LocalFiles(ToolGateway):
    def capabilities(self):return {}


class DeadlineScriptedProvider(ScriptedProvider):
    def complete(self, messages, max_tokens=None, deadline=None):
        return super().complete(messages)


def write(path, content='A real claim rating: uncertain; source unavailable.\n'):
    return {'type': 'tool', 'name': 'file.write', 'args': {'path': path, 'content': content}}


def artifact_task(title, agent, dependencies=(), required=()):
    return {**task(title, agent, dependencies, required),
            'outputContract': {'kind': 'artifact', 'extensions': ['.md'], 'minCount': 1}}


class CompletionGuardrailEngineTests(unittest.TestCase):
    def setUp(self):self.temp = tempfile.TemporaryDirectory(); self.apps = []
    def tearDown(self):
        for app in self.apps:
            app.shutdown(); app.wait_for_idle(4); app.repo.close()
        self.temp.cleanup()

    def make_app(self, plan, scripts, participants, turns=12, agents=None, time_only=False):
        config = Config(Path(self.temp.name)/str(len(self.apps)), {
            'LLM_API_KEY': 'offline-no-real-key', 'MAX_RETRIES': '0', 'MAX_AGENT_TURNS': str(turns),
            'MAX_MODEL_CALLS': '60', 'MAX_RUN_TOKENS': '400000', 'MAX_CONTEXT_CHARS': '40000'})
        if time_only:config.apply_time_only_profile()
        model = DeadlineScriptedProvider(plan, scripts)
        app = Platform(config, provider=model, tools=LocalFiles(config, agents=agents), agents=agents)
        self.apps.append(app)
        return app, model, participants

    def send(self, app, participants):
        app.action({'action': 'send', 'mode': 'task', 'participants': participants,
                    'text': 'Complete the scoped output with explicit evidence and limitations.'})
        self.assertTrue(app.wait_for_idle(7))
        return app.snapshot()['sessions']['group']['run']

    def test_real_partial_can_stay_partial_once_and_feedback_survives_compression_and_persistence(self):
        original = 'Claim ratings and weak sources are documented; current product rates remain unknown.'
        captured = []
        def script(index, context, messages):
            if index == 0:return write('findings.md')
            if index == 1:return {'type': 'publish_artifact', 'path': 'findings.md'}
            if index == 2:return final(original, status='partial')
            self.assertEqual(index, 3)
            run = app.state['sessions']['group']['run']; producer = next(t for t in run['steps'] if t['title'] == 'Review scope')
            proof = latest_feedback_proof(run, producer)
            self.assertIsNotNone(proof)
            for _ in range(3):
                messages = compress_messages(messages, 40000, trusted_feedback=proof)
                actual = [json.loads(m['content'].split('：', 1)[1]) for m in messages
                          if m['content'].startswith('动作的真实结果：') and 'completion_scope_clarification' in m['content']]
                self.assertEqual(len(actual), 1)
                self.assertEqual(actual[0]['acceptanceCriteria'], context['task']['acceptanceCriteria'])
                self.assertEqual(actual[0]['binding']['modelCallId'], proof.model_call_id)
            captured.append(actual[0])
            return final('Required independent corroboration is still missing; keep partial.', status='partial')
        app, model, participants = self.make_app([artifact_task('Review scope', 'research')],
                                                {'Review scope': script}, ['research'])
        before = (app.config.max_agent_turns, app.config.max_run_tokens, app.config.max_model_calls)
        run = self.send(app, participants); producer = next(t for t in run['steps'] if t['title'] == 'Review scope')
        self.assertEqual(producer['status'], 'partial'); self.assertIsNone(producer['error'])
        self.assertEqual(model.counts['Review scope'], 4)
        self.assertEqual(sum(c['tool'] == 'file.write' for c in run['calls']), 1)
        self.assertEqual(sum(e['type'] == 'completion_scope_clarification' for e in run['events']), 1)
        self.assertFalse(any(e['type'].startswith('review_') for e in run['events']))
        self.assertEqual((app.config.max_agent_turns, app.config.max_run_tokens, app.config.max_model_calls), before)
        saved = app.repo.load_state()['sessions']['group']['run']
        current = next(t for t in saved['steps'] if t['id'] == producer['id'])
        self.assertEqual(current['_completionClarification']['modelCallId'], captured[0]['binding']['modelCallId'])
        raw_proposals = [json.loads(t['text']) for t in run['transcript'] if t.get('kind') == 'model_response'
                         and t.get('taskId') == producer['id']]
        self.assertIn({'type': 'final', 'status': 'partial', 'text': original}, raw_proposals)
        finishes = [e for e in run['events'] if e['type'] == 'task_finished' and e['payload'].get('taskId') == producer['id']]
        self.assertEqual([e['payload']['status'] for e in finishes], ['partial'])

    def test_author_may_choose_done_but_producer_stays_partial_and_real_review_stays_editorial(self):
        agents = get_team_agents('newsroom')
        plan = [task('Research', 'reporter'), artifact_task('Check', 'fact_checker', ['Research'], ['review']),
                task('Edit via check', 'editor', ['Check']), task('Direct consumer', 'editor', ['Research', 'Check'])]
        def check(index, context, messages):
            target = context['dependencyResults'][0]
            if index == 0:
                self.assertEqual(target['status'], 'partial')
                return {'type': 'tool', 'name': 'file.read', 'args': {'path': context['authorizedArtifacts'][0]['path']}}
            if index == 1:return {'type': 'review', 'taskId': target['id'], 'verdict': 'pass',
                                 'feedback': 'Actual read findings have explicit unknowns; usable as a bounded research brief, not verified current rates.'}
            if index == 2:return write('verification.md')
            if index == 3:return {'type': 'publish_artifact', 'path': 'verification.md'}
            if index == 4:return final('Ratings and limits supplied; partial because current rates remain unknown.', status='partial')
            self.assertEqual(index, 5)
            self.assertTrue(any('completion_scope_clarification' in m['content'] for m in messages))
            return final('Original scoped rating and weak-source criteria are met by the current note; current rates remain explicitly unverified.')
        scripts = {'Research': [write('findings.md'), {'type': 'publish_artifact', 'path': 'findings.md'},
                                final('Known limits are preserved.', status='partial')], 'Check': check,
                   'Edit via check': [final('Audience-facing draft based on the bounded note, retaining its explicit limitations.')]}
        app, model, participants = self.make_app(plan, scripts, ['reporter', 'fact_checker', 'editor'], agents=agents)
        run = self.send(app, participants); by = {t['title']: t for t in run['steps']}
        self.assertEqual(by['Research']['status'], 'partial'); self.assertEqual(by['Check']['status'], 'done')
        self.assertEqual(by['Edit via check']['status'], 'done'); self.assertEqual(by['Direct consumer']['status'], 'skipped')
        self.assertEqual(model.counts['Direct consumer'], 0); self.assertEqual(run['status'], 'partial')
        self.assertEqual(by['Research']['validation']['kind'], 'editorial_assessment')
        self.assertFalse(by['Research']['validation']['verified'])
        reviews = [e for e in run['events'] if e['type'] == 'review_pass']
        self.assertEqual(len(reviews), 1); self.assertEqual(reviews[0]['payload']['taskId'], by['Research']['id'])
        self.assertEqual(reviews[0]['payload']['validation']['targetRevision'], 0)
        self.assertEqual(reviews[0]['payload']['validation']['artifactHashes'],
                         {a['id']: a['sha256'] for a in run['artifacts'] if a['id'] in by['Research']['outputs']})

    def test_missing_artifact_or_contract_never_gets_a_completion_nudge(self):
        for contract in ({'kind': 'artifact', 'extensions': ['.html']}, None):
            with self.subTest(contract=contract):
                plan = [{**task('Missing', 'research'), 'outputContract': contract}]
                app, model, participants = self.make_app(plan, {'Missing': [final('MET by my assertion.', status='partial')]}, ['research'])
                run = self.send(app, participants)
                self.assertEqual(model.counts['Missing'], 1)
                self.assertFalse(any(e['type'] == 'completion_scope_clarification' for e in run['events']))

    def test_last_existing_final_only_turn_gets_no_completion_retry(self):
        app, model, participants = self.make_app([artifact_task('Limited', 'research')],
            {'Limited': [write('findings.md'), {'type': 'publish_artifact', 'path': 'findings.md'},
                         final('Current snapshot only; genuinely incomplete.', status='partial')]}, ['research'], turns=3)
        run = self.send(app, participants); producer = next(t for t in run['steps'] if t['title'] == 'Limited')
        self.assertEqual(model.counts['Limited'], 3); self.assertEqual(producer['closureReason'], 'turn_limit_final')
        self.assertEqual(producer['error']['code'], 'budget')
        self.assertFalse(any(e['type'] == 'completion_scope_clarification' for e in run['events']))

    def test_failed_generation_after_clarification_is_not_turned_into_success(self):
        app, model, participants = self.make_app([artifact_task('Fail later', 'research')],
            {'Fail later': [write('findings.md'), {'type': 'publish_artifact', 'path': 'findings.md'},
                            final('Limits remain.', status='partial'), ProviderError('offline simulated error', 'provider', False)]}, ['research'])
        run = self.send(app, participants); producer = next(t for t in run['steps'] if t['title'] == 'Fail later')
        self.assertEqual(producer['status'], 'failed'); self.assertEqual(producer['error']['code'], 'provider')
        self.assertEqual(sum(e['type'] == 'completion_scope_clarification' for e in run['events']), 1)
        self.assertFalse(any(e['type'].startswith('review_') for e in run['events']))
        self.assertEqual(sum(c['tool'] == 'file.write' for c in run['calls']), 1)

    def test_time_only_clarification_keeps_original_wall_deadline_and_pinned_stage(self):
        observed = {}
        def script(index, context, messages):
            current = app.state['sessions']['group']['run']
            producer = next(t for t in current['steps'] if t['title'] == 'Bounded brief')
            if index == 0:
                observed['budget'] = copy.deepcopy(current['budget'])
                observed['stage'] = copy.deepcopy(producer['stageControl'])
                return write('findings.md')
            self.assertEqual(current['budget'], observed['budget'])
            self.assertEqual(producer['stageControl'], observed['stage'])
            if index == 1:return {'type': 'publish_artifact', 'path': 'findings.md'}
            if index == 2:return final('Published rated claims but knowledge remains limited.', status='partial')
            self.assertEqual(index, 3)
            return final('The bounded note is complete; uncertainty remains labelled, not verified.')
        app, model, participants = self.make_app([artifact_task('Bounded brief', 'research')],
            {'Bounded brief': script}, ['research'], time_only=True)
        run = self.send(app, participants); producer = next(t for t in run['steps'] if t['title'] == 'Bounded brief')
        self.assertEqual(producer['status'], 'done')
        self.assertEqual(run['budget'], observed['budget'])
        self.assertEqual(producer['stageControl'], observed['stage'])
        self.assertEqual(run['budget']['profile'], 'time_only'); self.assertEqual(run['budgetExtensions'], 0)
        self.assertIsNone(run['budget']['tokens']); self.assertIsNone(run['budget']['agentTurns'])
        self.assertEqual(model.counts['Bounded brief'], 4)

    def test_real_soft_handoff_feedback_precedes_budget_annotation_without_reopening_tools(self):
        observed = {'handoff': False}
        def at_stage(run, producer, now):
            if observed['handoff'] and producer.get('stageControl'):
                now = producer['stageControl']['handoffAt'] + 1
            return stage_control(run, producer, now)
        def script(index, context, messages):
            current = app.state['sessions']['group']['run']
            producer = next(t for t in current['steps'] if t['title'] == 'Handoff brief')
            if index == 0:
                observed['budget'] = copy.deepcopy(current['budget'])
                observed['pinnedStage'] = copy.deepcopy(producer['stageControl'])
                return write('findings.md')
            if index == 1:return {'type': 'publish_artifact', 'path': 'findings.md'}
            if index == 2:
                observed['handoff'] = True
                return final('The note is complete but facts remain uncertain.', status='partial')
            self.assertEqual(index, 3)
            self.assertIsNone(producer.get('error'))
            self.assertNotIn('closureReason', producer)
            actual = next(json.loads(m['content'].split('：', 1)[1]) for m in reversed(messages)
                          if m['content'].startswith('动作的真实结果：') and 'completion_scope_clarification' in m['content'])
            self.assertEqual(actual['observedStagePhase'], 'handoff_final')
            # Keep partial deliberately; no automatic conversion or work restart.
            return final('One mandatory independent check is genuinely missing; keep partial.', status='partial')
        app, model, participants = self.make_app([artifact_task('Handoff brief', 'research')],
            {'Handoff brief': script}, ['research'], time_only=True)
        with patch('backend.engine.stage_control', at_stage):
            run = self.send(app, participants)
        producer = next(t for t in run['steps'] if t['title'] == 'Handoff brief')
        self.assertEqual(model.counts['Handoff brief'], 4)
        self.assertEqual(producer['status'], 'partial')
        self.assertEqual(producer['closureReason'], 'stage_handoff_final')
        self.assertEqual(producer['error']['code'], 'budget')
        self.assertEqual(run['budget'], observed['budget'])
        self.assertEqual(producer['stageControl'], observed['pinnedStage'])
        self.assertEqual(sum(c['tool'] == 'file.write' for c in run['calls']), 1)


if __name__ == '__main__':unittest.main()
