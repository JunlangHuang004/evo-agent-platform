"""Group-task handoff cards. No network and no live model."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from backend.config import Config
from backend.engine import Platform
from backend.teams import get_team_agents
from tests.test_engine import ScriptedProvider, final, task


DIGEST = 'ab' * 32
NEWS_DIGEST = 'cd' * 32
DEV_REASON = 'booking-dev-schema-failure'


def ended_run(run_id, status, title, participants, artifacts, steps):
    return {
        'id': run_id, 'mode': 'task', 'status': status, 'title': title,
        'startedAt': 1, 'finishedAt': None,
        'participants': participants, 'steps': steps, 'artifacts': artifacts,
        'acceptanceGaps': [], 'calls': [], 'approvals': [], 'events': [],
        'conversation': [], 'transcript': [],
    }


class HandoffCardTests(unittest.TestCase):
    def test_software_page_names_hashes_and_existing_records(self):
        from backend.handoff import build_handoff
        card = build_handoff(ended_run(
            'run-software', 'partial', '会议室预约系统', ['lead', 'dev', 'qa', 'release'],
            [{'name': 'index.html', 'path': 'index.html', 'ownerAgentId': 'dev', 'sha256': DIGEST}],
            [{'id': 'implement', 'title': '实现预约页', 'agentId': 'dev', 'status': 'partial',
              'error': {'code': 'response_schema'}}]), 'software')
        self.assertIn(DIGEST, card['text'])
        self.assertIn('使用已有记录、不要另造数据', card['text'])
        self.assertEqual(card['files'][0]['readOnly'], True)
        self.assertLessEqual(len(card['text']), 2000)

    def test_news_report_keeps_sources_and_is_not_the_new_article(self):
        from backend.handoff import build_handoff
        card = build_handoff(ended_run(
            'run-news', 'done', '校园人工智能报道', ['chief_editor', 'reporter'],
            [{'name': 'campus-ai-report.md', 'path': 'campus-ai-report.md',
              'ownerAgentId': 'reporter', 'sha256': NEWS_DIGEST}],
            [{'id': 'report', 'title': '撰写报道', 'agentId': 'reporter', 'status': 'done'}]), 'newsroom')
        self.assertIn('沿用来源、范围和未定结论', card['text'])
        self.assertIn('旧文件不是新题目的正文', card['text'])
        self.assertIn(NEWS_DIGEST, card['text'])

    def test_failed_run_without_files_still_writes_a_short_card(self):
        from backend.handoff import build_handoff
        card = build_handoff(ended_run('run-empty', 'failed', '空任务', ['lead'], [], []), 'software')
        self.assertIn('没有可继承的产物', card['text'])

    def test_own_experience_stays_with_that_agent_and_run(self):
        from backend.handoff import own_experience
        records = [
            {'ownerAgentId': 'dev', 'kind': 'episodic', 'sourceRunId': 'run-software',
             'id': 'episode-dev', 'title': 'Implement booking', 'success': False,
             'reason': DEV_REASON, 'call_ids': ['call-book']},
            {'ownerAgentId': 'qa', 'kind': 'episodic', 'sourceRunId': 'run-software',
             'id': 'episode-qa', 'title': 'Check', 'success': True, 'reason': 'qa-only', 'call_ids': []},
            {'ownerAgentId': 'dev', 'kind': 'episodic', 'sourceRunId': 'run-other',
             'id': 'episode-old', 'title': 'Old', 'success': False, 'reason': 'other-run', 'call_ids': []},
        ]
        dev = own_experience(records, 'dev', 'run-software')
        self.assertEqual([item['reason'] for item in dev], [DEV_REASON])
        self.assertEqual(own_experience(records, 'qa', 'run-software')[0]['reason'], 'qa-only')
        self.assertEqual(own_experience(records, 'lead', 'run-software'), [])


class AnyTools:
    def capabilities(self):
        return {'dockerAvailable': False, 'dockerImageReady': False, 'gitInstalled': False}

    def definitions(self, agent):
        return [{'name': 'file.read', 'description': 'inert unit-test tool',
                 'parameters': {'type': 'object'}, 'idempotent': True}]

    def execute(self, *args, **kwargs):
        return {'ok': True}

    def cancel(self, run_id):
        return None


class RecordingProvider(ScriptedProvider):
    def __init__(self, plan, scripts=None):
        super().__init__(plan, scripts)
        self.seen = []

    def complete(self, messages):
        with self.lock:
            self.seen.append(copy.deepcopy(messages))
        return super().complete(messages)


class HandoffFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.shutdown()
        for app in self.apps:
            app.wait_for_idle(4)
            app.repo.close()
        self.temp.cleanup()

    def platform(self, team_id, provider):
        root = Path(self.temp.name) / team_id
        config = Config(root, {'LLM_API_KEY': 'unit-test-not-a-real-key', 'MAX_RETRIES': '0',
                               'MAX_MODEL_CALLS': '40', 'MAX_RUN_TOKENS': '200000',
                               'MAX_AGENT_TURNS': '8', 'PARALLEL_TASKS': '2'})
        app = Platform(config, provider=provider, tools=AnyTools(),
                       agents=get_team_agents(team_id), team_id=team_id)
        self.apps.append(app)
        return app

    def publish(self, app, run):
        with app.lock:
            app.state['sessions']['group']['run'] = run
            app._publish_handoff('group', run)
        return app.repo.load_handoff()

    def wait_records(self, provider, count, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with provider.lock:
                if len(provider.records) >= count:
                    return list(provider.records)
            time.sleep(0.02)
        self.fail('timed out waiting for task prompts')

    def planning_payload(self, provider):
        with provider.lock:
            seen = list(provider.seen)
        for messages in seen:
            if messages and 'Plan an executable task DAG' in messages[0].get('content', ''):
                return json.loads(messages[1]['content'])
        self.fail('planner was not called')

    def test_reset_keeps_software_card_for_the_next_plan_only_inside_the_team(self):
        provider = RecordingProvider([
            task('实现看板', 'dev'),
            task('核对看板', 'qa'),
        ])
        app = self.platform('software', provider)
        run = ended_run(
            'run-software', 'partial', '会议室预约系统', ['lead', 'dev', 'qa', 'release'],
            [{'name': 'index.html', 'path': 'index.html', 'ownerAgentId': 'dev', 'sha256': DIGEST}],
            [{'id': 'implement', 'title': '实现预约页', 'agentId': 'dev', 'status': 'partial',
              'error': {'code': 'response_schema', 'message': '模型没有返回有效的文本响应。'}}])
        card = self.publish(app, run)
        self.assertEqual(card['teamId'], 'software')
        self.assertTrue(any(item['role'] == 'system' and '交接卡' in item['text']
                            for item in app.state['sessions']['group']['messages']))
        app.memory.record_episode(
            'dev', run,
            {'id': 'implement', 'title': '实现预约页', 'agentId': 'dev', 'revision': 0},
            False, DEV_REASON, ['call-book'], 'group')
        app.action({'action': 'reset', 'channelId': 'group'})
        self.assertEqual(app.state['sessions']['group']['messages'], [])
        self.assertEqual(app.repo.load_handoff()['runId'], 'run-software')
        app.action({'action': 'send', 'channelId': 'group', 'mode': 'task',
                    'text': '做管理员看板，使用已有预约记录',
                    'participants': ['lead', 'dev', 'qa']})
        records = self.wait_records(provider, 2)
        payload = self.planning_payload(provider)
        handoff = payload['handoff']
        self.assertIn(DIGEST, json.dumps(handoff))
        self.assertIn('使用已有记录、不要另造数据', handoff['text'])
        self.assertNotIn(DEV_REASON, json.dumps(handoff.get('ownExperience', [])))
        by_title = {item['context']['task']['title']: item['context'] for item in records}
        self.assertIn(DEV_REASON, json.dumps(by_title['实现看板']['ownTaskExperience']))
        self.assertNotIn('ownTaskExperience', by_title['核对看板'])
        self.assertNotIn(DEV_REASON, json.dumps(by_title['核对看板']))
        app.wait_for_idle(4)

        other = RecordingProvider([task('核对旧报道', 'fact_checker')])
        news = self.platform('newsroom', other)
        news.action({'action': 'send', 'channelId': 'group', 'mode': 'task',
                     'text': '核查另一篇报道', 'participants': ['chief_editor', 'fact_checker']})
        self.wait_records(other, 1)
        other_plan = self.planning_payload(other)
        self.assertNotIn('handoff', other_plan)
        self.assertNotIn(DIGEST, json.dumps(other_plan))
        self.assertNotIn(DEV_REASON, json.dumps(other_plan))
        news.wait_for_idle(4)

    def test_news_card_survives_reset_and_does_not_become_the_new_article(self):
        provider = RecordingProvider([task('核查检测器说法', 'fact_checker')])
        app = self.platform('newsroom', provider)
        self.publish(app, ended_run(
            'run-news', 'done', '校园人工智能报道', ['chief_editor', 'reporter', 'fact_checker'],
            [{'name': 'campus-ai-report.md', 'path': 'campus-ai-report.md',
              'ownerAgentId': 'reporter', 'sha256': NEWS_DIGEST}],
            [{'id': 'report', 'title': '撰写报道', 'agentId': 'reporter', 'status': 'done'}]))
        app.action({'action': 'reset', 'channelId': 'group'})
        app.action({'action': 'send', 'channelId': 'group', 'mode': 'task',
                    'text': '把一年回本的说法做成事实核查幻灯片',
                    'participants': ['chief_editor', 'fact_checker']})
        self.wait_records(provider, 1)
        handoff = self.planning_payload(provider)['handoff']
        self.assertIn(NEWS_DIGEST, handoff['text'])
        self.assertIn('沿用来源、范围和未定结论', handoff['text'])
        self.assertIn('旧文件不是新题目的正文', handoff['text'])
        app.wait_for_idle(4)

    def test_cancel_writes_a_card_and_pause_does_not(self):
        app = self.platform('software', RecordingProvider([]))
        paused = ended_run('run-paused', 'running', '还在做', ['lead'], [], [])
        with app.lock:
            app.state['sessions']['group']['run'] = paused
        app.action({'action': 'pause', 'channelId': 'group', 'runId': 'run-paused'})
        self.assertIsNone(app.repo.load_handoff())
        app.action({'action': 'cancel', 'channelId': 'group', 'runId': 'run-paused'})
        card = app.repo.load_handoff()
        self.assertEqual(card['status'], 'cancelled')
        self.assertEqual(card['runId'], 'run-paused')

    def test_finished_group_task_writes_the_card_from_the_ledger(self):
        provider = RecordingProvider([task('交付页面', 'dev')])
        app = self.platform('software', provider)
        app.action({'action': 'send', 'channelId': 'group', 'mode': 'task',
                    'text': '做预约页', 'participants': ['lead', 'dev']})
        app.wait_for_idle(4)
        run = app.state['sessions']['group']['run']
        card = app.repo.load_handoff()
        self.assertEqual(card['runId'], run['id'])
        self.assertEqual(card['status'], run['status'])
        self.assertIn(run['status'], ('done', 'partial', 'failed'))
        self.assertTrue(any(item['role'] == 'system' and item['text'].startswith('交接卡')
                            for item in app.state['sessions']['group']['messages']))

    def test_private_channel_does_not_publish_a_team_card(self):
        app = self.platform('software', RecordingProvider([]))
        run = ended_run('run-private', 'done', '私聊', ['dev'], [], [])
        with app.lock:
            app.state['sessions']['dev']['run'] = run
            self.assertIsNone(app._publish_handoff('dev', run))
        self.assertIsNone(app.repo.load_handoff())


if __name__ == '__main__':
    unittest.main()
