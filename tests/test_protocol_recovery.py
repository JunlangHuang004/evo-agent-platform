"""Actual parser feedback and bounded recovery; no model/network/code execution."""
import copy
import hashlib
import json
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.engine import Platform
from backend.runtime import ProtocolError, parse_object, rewrite_single_action, rewrite_single_tool_markup
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

    def run_model(self,responses,wait=True,**settings):
        config=Config(Path(self.temp.name),{'LLM_API_KEY':'fixture-key-must-not-leak','MAX_RETRIES':'0',
                     'MAX_MODEL_CALLS':'20','MAX_AGENT_TURNS':'8','MAX_RUN_TOKENS':'200000',**settings})
        model=ProtocolModel(responses);tools=LocalFiles(config)
        self.app=Platform(config,provider=model,tools=tools)
        self.app.action({'action':'send','channelId':'group','mode':'task','participants':['coordinator'],
                         'text':'Complete a general task and report only actual results.'})
        if not wait:
            return model,tools,None,None
        self.assertTrue(self.app.wait_for_idle(4),'format recovery must terminate')
        run=self.app.snapshot()['sessions']['group']['run']
        task=next(item for item in run['steps'] if item.get('kind')!='planning')
        return model,tools,run,task

    def pending_repair(self):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            run=self.app.snapshot()['sessions']['group'].get('run')
            approval=next((item for item in (run or {}).get('approvals',[])
                           if item.get('type')=='protocol_repair' and item.get('status')=='pending'),None)
            if approval:
                return run,approval
            time.sleep(.02)
        self.fail('expected a pending protocol repair approval')

    def finish(self):
        self.assertTrue(self.app.wait_for_idle(4),'format recovery must terminate')
        run=self.app.snapshot()['sessions']['group']['run']
        task=next(item for item in run['steps'] if item.get('kind')!='planning')
        return run,task

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

    def test_repeated_invalid_json_asks_before_failing_and_denial_stops_the_task(self):
        model,tools,_,_=self.run_model(['No JSON action was returned.'],wait=False)
        run,approval=self.pending_repair()
        self.assertEqual(run['status'],'waiting_human')
        self.assertEqual(len(model.action_messages),3)
        self.app.action({'action':'approval','channelId':'group','runId':run['id'],'approvalId':approval['id'],'choiceId':'deny'})
        run,task=self.finish()
        self.assertEqual(task['turns'],3)
        self.assertEqual(task['status'],'failed')
        self.assertEqual(task['error']['code'],'protocol')
        self.assertIn('2 次自动格式修复',task['error']['message'])
        self.assertIn('人工未批准再次格式修复',task['error']['message'])
        self.assertEqual(run['status'],'failed')
        self.assertEqual([item['retrying'] for item in task['protocolErrors']],[True,True,False])
        self.assertEqual(len([event for event in run['events'] if event['type']=='protocol_parse_failed']),3)
        self.assertFalse(any(event['type']=='protocol_repair_granted' for event in run['events']))
        self.assertEqual(run['modelCalls'],4,'planning and three real fixture responses are all budgeted')
        self.assertEqual(run['tokenUsage'],20)
        self.assertEqual(self.app.state['sessions']['group']['run'].get('_reservedTokens',0),0)
        self.assertFalse(tools.executed)

    def test_human_can_grant_one_more_format_repair(self):
        model,tools,_,_=self.run_model(['No JSON action was returned.','No JSON action was returned.','No JSON action was returned.',
                                        {'type':'final','text':'Recovered after one granted repair.'}],wait=False)
        run,approval=self.pending_repair()
        self.app.action({'action':'approval','channelId':'group','runId':run['id'],'approvalId':approval['id'],'choiceId':'allow'})
        run,task=self.finish()
        self.assertEqual(run['status'],'done')
        self.assertEqual(task['status'],'done')
        self.assertEqual(len(model.action_messages),4)
        self.assertTrue(any(event['type']=='protocol_repair_granted' for event in run['events']))
        self.assertTrue(any('humanGranted' in item['content'] for item in model.action_messages[-1] if item['role']=='user'))
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

    def test_single_tool_markup_is_rewritten_and_executed_without_another_repair(self):
        markup='<tool><name>file.write</name><args>{"path":"rewritten.md","content":"from markup\\n"}</args></tool>'
        model,tools,run,task=self.run_model([markup,{'type':'final','text':'Wrote the rewritten file.'}])
        self.assertEqual(run['status'],'done')
        self.assertEqual([name for name,_,_ in tools.executed],['file.write'])
        self.assertEqual((tools.workspace('coordinator',run['id'])/'rewritten.md').read_text(),'from markup\n')
        self.assertEqual(len(model.action_messages),2)
        self.assertFalse(task.get('protocolErrors'))
        event=next(item for item in run['events'] if item['type']=='protocol_format_rewritten')
        self.assertEqual(event['payload']['original'],markup)
        self.assertEqual(event['payload']['tool'],'file.write')
        self.assertTrue(any(item.get('kind')=='model_response' and item.get('text')==markup for item in run['transcript']))
        call=next(item for item in run['calls'] if item['id']==event['payload']['callId'])
        self.assertEqual(call['protocolStatus'],'format_rewritten')
        accepted=next(item['content'] for item in model.action_messages[1] if item['role']=='assistant')
        self.assertEqual(json.loads(accepted),{'type':'tool','name':'file.write','args':{'path':'rewritten.md','content':'from markup\n'}})
        self.assertNotIn('<tool>',accepted)

    def test_ambiguous_or_disallowed_markup_is_not_rewritten_or_executed(self):
        prose='use this <tool><name>file.write</name><args>{"path":"no.md","content":"x"}</args></tool>'
        _,tools,run,task=self.run_model([prose,{'type':'final','text':'No file was written.'}])
        self.assertEqual(run['status'],'done')
        self.assertFalse(tools.executed)
        self.assertEqual(len(task['protocolErrors']),1)
        self.assertFalse(any(item['type']=='protocol_format_rewritten' for item in run['events']))

    def test_disallowed_tool_markup_stays_on_the_model_repair_path(self):
        markup='<tool><name>web.fetch</name><args>{"url":"https://example.com/source"}</args></tool>'
        _,tools,_,_=self.run_model([markup],wait=False)
        run,approval=self.pending_repair()
        self.assertEqual(approval['type'],'protocol_repair')
        self.app.action({'action':'approval','channelId':'group','runId':run['id'],'approvalId':approval['id'],'choiceId':'deny'})
        run,task=self.finish()
        self.assertEqual(task['status'],'failed')
        self.assertFalse(tools.executed)
        self.assertFalse(any(item['type']=='protocol_format_rewritten' for item in run['events']))
        self.assertEqual(len(task['protocolErrors']),3)

    def test_one_extra_closer_restores_the_original_final(self):
        final={'type':'final','text':'Closed from one extra brace.'}
        broken=json.dumps(final)+'}'
        model,_,run,task=self.run_model([broken])
        self.assertEqual(run['status'],'done')
        self.assertEqual(task['status'],'done')
        self.assertFalse(task.get('protocolErrors'))
        self.assertEqual(len(model.action_messages),1)
        event=next(item for item in run['events'] if item['type']=='protocol_format_rewritten')
        self.assertEqual(event['payload']['action'],'final')
        self.assertEqual(event['payload']['original'],broken)
        self.assertNotIn('tool',event['payload'])

    def test_one_mismatched_closer_still_writes_the_original_file(self):
        action={'type':'tool','name':'file.write','args':{'path':'brief.md','content':'see } and ] now\n'}}
        broken=json.dumps(action,ensure_ascii=False)[:-1]+']'
        _,tools,run,task=self.run_model([broken,{'type':'final','text':'Wrote the repaired brief.'}])
        self.assertEqual(run['status'],'done')
        self.assertEqual([name for name,_,_ in tools.executed],['file.write'])
        self.assertEqual((tools.workspace('coordinator',run['id'])/'brief.md').read_text(),'see } and ] now\n')
        self.assertFalse(task.get('protocolErrors'))
        event=next(item for item in run['events'] if item['type']=='protocol_format_rewritten')
        self.assertEqual(event['payload']['tool'],'file.write')


class ToolMarkupRewriteTests(unittest.TestCase):
    def rewrite(self, text, allowed=('file.read','file.write','web.fetch')):
        return rewrite_single_tool_markup(text, set(allowed))

    def test_live_shapes_become_one_tool_action(self):
        fetch=self.rewrite('<tool><name>web.fetch</name><args>{"url":"https://hkust.edu.hk/news/item"}</args></tool>')
        self.assertEqual(fetch,{'type':'tool','name':'web.fetch','args':{'url':'https://hkust.edu.hk/news/item'}})
        spaced=self.rewrite('<think>plan</think>\n<tool>\n<name>file.read</name>\n<args>{"path":"brief.md","find":"a}b"}</args>\n</tool>\n')
        self.assertEqual(spaced['name'],'file.read')
        self.assertEqual(spaced['args']['find'],'a}b')
        embedded=self.rewrite('<tool><name>file.write</name><args>{"path":"a.md","content":"see </args> now"}</args></tool>')
        self.assertEqual(embedded['args']['content'],'see </args> now')

    def test_ambiguous_markup_is_left_untouched(self):
        samples=[
            'Please fetch <tool><name>web.fetch</name><args>{"url":"https://example.com"}</args></tool>',
            '<tool><name>file.read</name><args>{"path":"a.md"}</args></tool><tool><name>file.list</name><args>{"path":"."}</args></tool>',
            '<tool><name>file.read</name><args>["a.md"]</args></tool>',
            '<tool><name>file.read</name><args>not-json</args></tool>',
            '<tool><args>{"path":"a.md"}</args><name>file.read</name></tool>',
            '<tool><name>file.read</name><args>{"path":"a.md"}{"path":"b.md"}</args></tool>',
            '<think><tool><name>file.read</name><args>{"path":"secret.md"}</args></tool></think>',
            '<tool><name>git.push</name><args>{"remote":"origin","branch":"main","commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}</args></tool>',
            '{"type":"tool","name":"file.read","args":{"path":"a.md"}}',
        ]
        for sample in samples:
            self.assertIsNone(self.rewrite(sample),sample)


class SingleActionRewriteTests(unittest.TestCase):
    def rewrite(self, text, allowed=('file.read','file.write','web.fetch')):
        return rewrite_single_action(text, set(allowed))

    def test_one_structural_difference_restores_the_same_action(self):
        final={'type':'final','status':'done','text':'Confirmed brief.'}
        self.assertEqual(self.rewrite(json.dumps(final)+'}'), final)
        self.assertEqual(self.rewrite(json.dumps(final)+']'), final)
        self.assertEqual(self.rewrite('<text>\n'+json.dumps(final)+'\n</text>'), final)
        self.assertEqual(self.rewrite(json.dumps(final)[:-1]+',}'), final)
        write={'type':'tool','name':'file.write','args':{'path':'a.md','content':'see } and ]\n'}}
        self.assertEqual(self.rewrite(json.dumps(write)[:-1]+']'), write)
        fenced='```json\n'+json.dumps(final)+'}\n```'
        self.assertEqual(self.rewrite(fenced), final)

    def test_ambiguous_or_prose_replies_stay_unparsed(self):
        samples=[
            '<text>I will inspect the artifact.</text>',
            'use this '+json.dumps({'type':'final','text':'ok'}),
            json.dumps({'type':'final','text':'ok'})+' thanks',
            json.dumps({'type':'final','text':'ok'})+'}}',
            '{"type":"final","text":"ok",,}',
            '{"a":[1}]',
            '{"text":"unterminated}',
            '<text>'+json.dumps({'type':'final','text':'ok'})+'}</text>',
            '{"type":"file.write","args":{"path":"a.md","content":"x"}}',
        ]
        for sample in samples:
            self.assertIsNone(self.rewrite(sample),sample)


if __name__=='__main__':unittest.main()
