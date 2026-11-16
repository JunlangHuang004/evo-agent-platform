"""Full human questions survive public UI/state and actual reply handoff; no API."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from backend.config import Config
from backend.engine import Platform


class NoTools:
    def capabilities(self): return {}
    def definitions(self, agent): return []
    def cancel(self, run_id): return None


class QuestionModel:
    def __init__(self, actions):
        self.actions = actions
        self.action_messages = []

    def complete(self, messages, max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            content = {'tasks':[{'id':'clarify','title':'Confirm actual requirements', 'agentId':'coordinator'}]}
        else:
            index = len(self.action_messages)
            self.action_messages.append(copy.deepcopy(messages))
            content = self.actions[index] if index < len(self.actions) else {'type':'final','text':'Recorded the actual answer.','status':'done'}
        return {'content':json.dumps(content,ensure_ascii=False), 'finish_reason':'stop','usage':{'total_tokens':4}}


class HumanQuestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.shutdown()
            app.wait_for_idle(3)
            app.repo.close()
        self.temp.cleanup()

    def start(self, actions):
        config = Config(Path(self.temp.name)/str(len(self.apps)),
                        {'LLM_API_KEY':'test-only-fixture','MAX_RETRIES':'0','MAX_MODEL_CALLS':'12',
                         'MAX_RUN_TOKENS':'200000','MAX_CONTEXT_CHARS':'80000'})
        model = QuestionModel(actions)
        app = Platform(config,provider=model,tools=NoTools())
        self.apps.append(app)
        app.action({'action':'send','channelId':'group','mode':'task',
                    'participants':['coordinator'],'text':'Clarify the real requirements and retain the human reply.'})
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            run = app.snapshot()['sessions']['group']['run']
            approval = next((item for item in run['approvals'] if item['status']=='pending'),None)
            if approval:
                return app,model,run,approval
            if run['status'] in ('failed','done','cancelled'):
                self.fail('Expected an actual pending human question: '+json.dumps(run,ensure_ascii=False))
            time.sleep(.01)
        self.fail('Human question was not raised within the local fixture deadline.')

    def answer(self, app, run, approval, text='实际答复：仅预约人可取消，晚间20点关闭。'):
        app.action({'action':'approval','channelId':'group','runId':run['id'],
                    'approvalId':approval['id'],'choiceId':'answer','answer':text})
        self.assertTrue(app.wait_for_idle(3))
        completed = app.snapshot()['sessions']['group']['run']
        self.assertEqual(completed['status'],'done')
        return completed

    def test_short_question_and_optional_description_keep_existing_behavior(self):
        question, detail = 'Which room schedule should we use?', 'Include opening and closing hours.'
        app,model,run,approval = self.start([{'type':'ask_human','question':question,
                                           'description':detail,'choices':['Weekday hours','All hours']}])
        self.assertEqual(approval['title'],question)
        self.assertEqual(approval['description'],detail)
        self.assertEqual(approval['binding']['question'],question)
        self.assertEqual([item['label'] for item in approval['choices']], ['Weekday hours','All hours'])
        self.answer(app,run,approval)
        result = next(message['content'] for message in model.action_messages[-1]
                      if message['content'].startswith('动作的真实结果：'))
        self.assertIn('实际答复',result)

    def test_long_question_and_supplement_are_public_persisted_and_answered_in_full(self):
        question = 'Please confirm: '+('room schedule and capacity; '*18)+'FINAL CONDITION: only the booking owner may cancel.'
        supplement = 'Additional requirement: keep cancellation history for the dashboard.'
        self.assertGreater(len(question),300)
        action = {'type':'ask_human','question':question,'description':supplement,'choices':['Confirm','Revise']}
        app,model,run,approval = self.start([action])
        self.assertLessEqual(len(approval['title']),300)
        self.assertIn(question,approval['description'])
        self.assertIn(supplement,approval['description'])
        self.assertEqual(approval['binding']['question'],question)
        public = app.snapshot()['sessions']['group']['run']['approvals'][0]
        stored = app.repo.get_run(run['id'])['approvals'][0]
        self.assertEqual(public['description'],stored['description'])
        self.assertEqual(public['description'].count(question),1)
        self.assertTrue(any(question in message['text'] and supplement in message['text']
                            for message in app.snapshot()['sessions']['group']['messages']))
        reply = '实际答复：采用标准10间会议室，08:00–20:00每小时，仅预约人可取消。'
        completed = self.answer(app,run,approval,reply)
        self.assertEqual(completed['approvals'][0]['response']['answer'],reply)
        self.assertEqual(app.repo.get_run(run['id'])['approvals'][0]['response']['answer'],reply)
        messages = [message for message in model.action_messages[-1]
                    if not message['content'].startswith('执行进度收据：')]
        self.assertEqual([message['role'] for message in messages],['system','user','assistant','user'])
        assistant = json.loads(messages[-2]['content'])
        response = json.loads(messages[-1]['content'].split('：',1)[1])
        self.assertEqual(assistant['question'],question)
        self.assertEqual(assistant['description'],supplement)
        self.assertEqual(response['answer'],reply)
        self.assertEqual(sum(message['content'].count(reply) for message in messages),1)

    def test_long_question_without_supplement_still_preserves_the_complete_question(self):
        question = 'Q'*300+' Crucial final condition must remain visible.'
        app,_,run,approval = self.start([{'type':'ask_human','question':question}])
        self.assertEqual(approval['description'],question)
        self.answer(app,run,approval)

    def test_over_limit_or_invalid_description_is_explicitly_rejected_not_silently_trimmed(self):
        invalid = [({'type':'ask_human','question':'Q'*4001},'4000'),
                   ({'type':'ask_human','question':'Short question','description':'D'*2001},'2000'),
                   ({'type':'ask_human','question':'Short question','description':{}},'必须是文本')]
        for bad,expected in invalid:
            with self.subTest(expected=expected):
                good = {'type':'ask_human','question':'Please answer this shorter complete question.'}
                app,model,run,approval = self.start([bad,good])
                self.assertEqual(approval['title'],good['question'])
                self.assertEqual(len(run['approvals']),1,'Invalid requests must not create a silently truncated approval.')
                rejected = next(event for event in run['events'] if event['type']=='action_rejected')
                self.assertIn(expected,rejected['message'])
                feedback = next(message['content'] for message in model.action_messages[1]
                                if message['content'].startswith('动作的真实结果：'))
                payload = json.loads(feedback.split('：',1)[1])
                self.assertFalse(payload['ok'])
                self.assertEqual(payload['error']['code'],'protocol')
                self.assertIn(expected,payload['error']['message'])
                self.answer(app,run,approval)


if __name__=='__main__':
    unittest.main()
