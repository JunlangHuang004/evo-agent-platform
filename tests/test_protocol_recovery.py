"""Actual parser feedback and bounded recovery; no model/network/code execution."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform
from backend.runtime import ProtocolError, parse_object
from backend.tools import ToolGateway


class LocalFiles(ToolGateway):
    def __init__(self,config):
        super().__init__(config)
        self.executed=[]

    def capabilities(self):
        return {'dockerAvailable':False,'dockerImageReady':False,'hostFallback':False}

    def execute(self,agent_id,run_id,name,args,approved=False):
        self.executed.append((name,copy.deepcopy(args),approved))
        return super().execute(agent_id,run_id,name,args,approved)


class ProtocolModel:
    def __init__(self,responses):
        self.responses=responses
        self.action_messages=[]

    def complete(self,messages,max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            text=json.dumps({'tasks':[{'id':'one','title':'Execute a general task','agentId':'coordinator'}]})
        else:
            self.action_messages.append(copy.deepcopy(messages))
            text=self.responses[min(len(self.action_messages)-1,len(self.responses)-1)]
            if isinstance(text,dict):text=json.dumps(text)
        return {'content':text,'usage':{'total_tokens':5},'finish_reason':'stop'}


class ProtocolRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=None

    def tearDown(self):
        if self.app:
            self.app.shutdown();self.app.wait_for_idle(3);self.app.repo.close()
        self.temp.cleanup()

    def run_model(self,responses,**settings):
        config=Config(Path(self.temp.name),{'LLM_API_KEY':'fixture-key-must-not-leak','MAX_RETRIES':'0',
                     'MAX_MODEL_CALLS':'20','MAX_AGENT_TURNS':'8','MAX_RUN_TOKENS':'200000',**settings})
        model=ProtocolModel(responses);tools=LocalFiles(config)
        self.app=Platform(config,provider=model,tools=tools)
        self.app.action({'action':'send','channelId':'group','mode':'task','participants':['coordinator'],
                         'text':'Complete a general task and report only actual results.'})
        self.assertTrue(self.app.wait_for_idle(4),'format recovery must terminate')
        run=self.app.snapshot()['sessions']['group']['run']
        task=next(item for item in run['steps'] if item.get('kind')!='planning')
        return model,tools,run,task

    def test_bad_response_and_specific_error_are_returned_to_agent_and_audited(self):
        bad='{"type":"final",\n"text":}'
        model,_,run,task=self.run_model([bad,{'type':'final','text':'Corrected actual response.'}])
        self.assertEqual(run['status'],'done')
        messages=model.action_messages[1]
        self.assertTrue(any(item['role']=='assistant' and item['content']==bad for item in messages))
        feedback=json.loads(next(item['content'][len('动作协议错误：'):] for item in messages if item['content'].startswith('动作协议错误：')))
        self.assertEqual(feedback['error']['code'],'invalid_action_json')
        self.assertIn('JSON 行 2',feedback['error']['message'])
        self.assertIn('列',feedback['error']['message'])
        failure=task['protocolErrors'][0]
        self.assertEqual(failure['responseSha256'],hashlib.sha256(bad.encode()).hexdigest())
        call=next(item for item in run['calls'] if item['id']==failure['callId'])
        self.assertEqual(call['status'],'success','transport success stays distinct from protocol failure')
        self.assertEqual(call['protocolStatus'],'invalid_json')
        self.assertEqual(call['protocolErrorId'],failure['id'])
        self.assertTrue(any(event['type']=='protocol_parse_failed' and event['payload']['callId']==call['id'] for event in run['events']))
        self.assertTrue(any(item.get('kind')=='protocol_feedback' for item in run['transcript']))
        stored=self.app.repo.get_run(run['id'])
        stored_task=next(item for item in stored['steps'] if not item.get('_planning'))
        self.assertEqual(stored_task['protocolErrors'],task['protocolErrors'])
        self.assertTrue(any(item['content']==bad for item in stored_task['_messages']))

    def test_repeated_invalid_json_stops_after_two_repairs_with_protocol_failure(self):
        model,tools,run,task=self.run_model(['No JSON action was returned.'])
        self.assertEqual(len(model.action_messages),3)
        self.assertEqual(task['turns'],3)
        self.assertEqual(task['status'],'failed')
        self.assertEqual(task['error']['code'],'protocol')
        self.assertIn('2 次格式修复',task['error']['message'])
        self.assertEqual(run['status'],'failed')
        self.assertEqual([item['retrying'] for item in task['protocolErrors']],[True,True,False])
        self.assertEqual(len([event for event in run['events'] if event['type']=='protocol_parse_failed']),3)
        self.assertEqual(run['modelCalls'],4,'planning and three real fixture responses are all budgeted')
        self.assertEqual(run['tokenUsage'],20)
        self.assertEqual(self.app.state['sessions']['group']['run'].get('_reservedTokens',0),0)
        self.assertFalse(tools.executed)

    def test_parse_repair_never_replays_a_previously_successful_file_write(self):
        write={'type':'tool','name':'file.write','args':{'path':'receipt.md','content':'One actual write.\n'}}
        malformed=json.dumps(write)[:-1]
        model,tools,run,task=self.run_model([write,malformed,{'type':'final','text':'The existing file write is recorded.'}])
        self.assertEqual(run['status'],'done')
        self.assertEqual([name for name,_,_ in tools.executed],['file.write'])
        self.assertEqual(len([call for call in run['calls'] if call['tool']=='file.write']),1)
        self.assertEqual((tools.workspace('coordinator',run['id'])/'receipt.md').read_text(),'One actual write.\n')
        last=model.action_messages[-1]
        self.assertTrue(any(item['content']==malformed for item in last if item['role']=='assistant'))
        self.assertTrue(any('已有工具结果仍在上下文' in item['content'] for item in last))
        self.assertFalse(run['approvals'],'an automatic overwrite replay must not create a second approval')
        self.assertEqual(len(task['protocolErrors']),1)

    def test_agent_turn_budget_is_stricter_than_format_repair_limit(self):
        model,_,run,task=self.run_model(['invalid output'],MAX_AGENT_TURNS='2')
        self.assertEqual(len(model.action_messages),2)
        self.assertEqual(task['error']['code'],'budget')
        self.assertEqual(len(task['protocolErrors']),2)
        self.assertEqual(run['status'],'failed')

    def test_tool_name_used_as_type_is_only_feedback_until_corrected_tool_action(self):
        args={'path':'corrected.md','content':'Written only by the corrected tool action.\n'}
        wrong={'type':'file.write','args':args}
        corrected={'type':'tool','name':'file.write','args':args}
        model,tools,run,task=self.run_model([wrong,corrected,{'type':'final','text':'The corrected action wrote the file.'}])
        self.assertEqual(run['status'],'done')
        self.assertEqual([name for name,_,_ in tools.executed],['file.write'])
        feedback=json.loads(next(m['content'][len('动作的真实结果：'):] for m in model.action_messages[1]
                                 if m['content'].startswith('动作的真实结果：')))
        self.assertFalse(feedback['ok'])
        self.assertEqual(feedback['error']['code'],'protocol')
        self.assertIn('type="tool"',feedback['error']['message'])
        self.assertIn('name="file.write"',feedback['error']['message'])
        self.assertIn('此请求未执行',feedback['error']['message'])
        self.assertEqual(sum(e['type']=='action_rejected' for e in run['events']),1)
        self.assertEqual(len([call for call in run['calls'] if call['tool']=='file.write']),1)
        self.assertEqual(task['turns'],3)

    def test_repeated_tool_type_mistake_still_stops_at_existing_loop_limit(self):
        wrong={'type':'file.write','args':{'path':'never.md','content':'Must not be auto-converted.'}}
        model,tools,run,task=self.run_model([wrong])
        self.assertEqual(run['status'],'failed')
        self.assertEqual(task['error']['code'],'protocol')
        self.assertIn('重复动作循环',task['error']['message'])
        self.assertEqual(len(model.action_messages),3)
        self.assertFalse(tools.executed)

    def test_bad_response_secrets_are_scrubbed_in_working_context_and_audit(self):
        bad='not-json fixture-key-must-not-leak'
        model,_,run,_=self.run_model([bad,{'type':'final','text':'Recovered.'}])
        self.assertEqual(run['status'],'done')
        self.assertNotIn('fixture-key-must-not-leak',json.dumps(run))
        self.assertNotIn('fixture-key-must-not-leak',json.dumps(model.action_messages[-1]))
        self.assertIn('[REDACTED]',json.dumps(model.action_messages[-1]))

    def test_parser_reports_location_and_safe_depth_failure(self):
        with self.assertRaises(ProtocolError) as caught:parse_object('{"type":\n}')
        self.assertIn('JSON 行 2',str(caught.exception))
        self.assertEqual(caught.exception.code,'protocol')
        self.assertFalse(caught.exception.retryable)
        # Decoder depth limits differ across Python versions; inject the actual
        # exception instead of risking an enormous native-stack parse.
        with patch('backend.runtime.json.JSONDecoder.raw_decode',side_effect=RecursionError('decoder depth limit')):
            with self.assertRaises(ProtocolError) as nested:parse_object('{"x":0}')
        self.assertIn('嵌套过深',str(nested.exception))


if __name__=='__main__':unittest.main()
