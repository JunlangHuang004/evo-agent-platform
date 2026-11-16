"""Length-limited generations are never executed; repair turns remain budgeted."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.tools import ToolGateway


class LocalFiles(ToolGateway):
    def __init__(self,config):
        super().__init__(config)
        self.executed=[]

    def capabilities(self): return {}

    def execute(self,agent_id,run_id,name,args,approved=False):
        self.executed.append((name,copy.deepcopy(args),approved))
        return super().execute(agent_id,run_id,name,args,approved)


def response(value,finish='stop',tokens=7):
    return {'content':json.dumps(value) if isinstance(value,dict) else value,
            'finish_reason':finish,'usage':{'total_tokens':tokens,'completion_tokens':tokens}}


class ScriptedModel:
    def __init__(self,responses):
        self.responses=responses
        self.messages=[]

    def complete(self,messages,max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            return response({'tasks':[{'id':'implement','title':'Implement a general deliverable','agentId':'coordinator'}]},tokens=3)
        self.messages.append(copy.deepcopy(messages))
        return self.responses[min(len(self.messages)-1,len(self.responses)-1)]


class TruncationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=None

    def tearDown(self):
        if self.app:
            self.app.shutdown();self.app.wait_for_idle(3);self.app.repo.close()
        self.temp.cleanup()

    def run_model(self,responses,**settings):
        config=Config(Path(self.temp.name),{'LLM_API_KEY':'secret-truncation-fixture','MAX_RETRIES':'2',
                     'MAX_MODEL_CALLS':'20','MAX_AGENT_TURNS':'8','MAX_RUN_TOKENS':'200000',**settings})
        model=ScriptedModel(responses);tools=LocalFiles(config)
        self.app=Platform(config,provider=model,tools=tools)
        self.app.action({'action':'send','channelId':'group','mode':'task','participants':['coordinator'],
                         'text':'Implement a general deliverable in small steps.'})
        self.assertTrue(self.app.wait_for_idle(4),'truncation repair must be bounded')
        run=self.app.snapshot()['sessions']['group']['run']
        task=next(t for t in run['steps'] if t.get('kind')!='planning')
        return model,tools,run,task

    def test_truncated_even_parseable_tool_action_is_not_executed_then_short_action_succeeds(self):
        rejected={'type':'tool','name':'file.write','args':{'path':'unexecuted.md','content':'Never execute this truncated action.'}}
        small={'type':'tool','name':'file.write','args':{'path':'small.md','content':'A complete small file.\n'}}
        model,tools,run,task=self.run_model([response(rejected,'length',41),response(small),
                                           response({'type':'final','text':'The small file was actually written.'})])
        self.assertEqual(run['status'],'done')
        self.assertEqual([args['path'] for name,args,_ in tools.executed],['small.md'])
        self.assertFalse((tools.workspace('coordinator',run['id'])/'unexecuted.md').exists())
        raw=json.dumps(rejected)
        next_messages=model.messages[1]
        self.assertTrue(any(m['role']=='assistant' and m['content']==raw for m in next_messages))
        feedback=json.loads(next(m['content'][len('输出截断修复：'):] for m in next_messages if m['content'].startswith('输出截断修复：')))
        self.assertEqual(feedback['error']['code'],'output_truncated')
        self.assertIn('small modular files',feedback['instruction'])
        failure=task['truncationErrors'][0]
        self.assertEqual(failure['responseSha256'],hashlib.sha256(raw.encode()).hexdigest())
        call=next(c for c in run['calls'] if c['id']==failure['callId'])
        self.assertEqual(call['status'],'failed')
        self.assertEqual(call['finishReason'],'length')
        self.assertEqual(call['usage']['total_tokens'],41)
        self.assertEqual(run['tokenUsage'],3+41+7+7)
        self.assertEqual(run['modelCalls'],4)
        stored=self.app.repo.get_run(run['id'])
        self.assertTrue(any(t.get('callId')==call['id'] and t.get('text')==raw for t in stored['transcript']))
        self.assertEqual(task['turns'],3)

    def test_repeated_length_responses_stop_after_two_repairs_with_all_usage_preserved(self):
        raw='{"type":"tool","name":"file.write","args":{"path":"partial.py","content":"'
        model,tools,run,task=self.run_model([response(raw,'length',29)])
        self.assertEqual(len(model.messages),3,'no implicit provider retries or unbounded repair')
        self.assertEqual(task['turns'],3)
        self.assertEqual(task['error']['code'],'output_truncated')
        self.assertIn('2 次截断输出修复',task['error']['message'])
        self.assertEqual(run['status'],'failed')
        self.assertEqual([e['retrying'] for e in task['truncationErrors']],[True,True,False])
        self.assertEqual(run['modelCalls'],4)
        self.assertEqual(run['tokenUsage'],3+29*3)
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='llm.chat' and c['status']=='failed']),3)
        self.assertEqual(self.app.state['sessions']['group']['run'].get('_reservedTokens',0),0)
        self.assertFalse(tools.executed)

    def test_repair_does_not_replay_a_previously_successful_write(self):
        write={'type':'tool','name':'file.write','args':{'path':'existing.md','content':'Exactly once.\n'}}
        model,tools,run,task=self.run_model([response(write),response(write,'length'),
                                           response({'type':'final','text':'Existing write retained.'})])
        self.assertEqual(run['status'],'done')
        self.assertEqual(len(tools.executed),1)
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),1)
        self.assertEqual((tools.workspace('coordinator',run['id'])/'existing.md').read_text(),'Exactly once.\n')
        self.assertFalse(run['approvals'],'the old write must not be replayed as an overwrite')
        self.assertTrue(any('do not replay completed tools' in m['content'] for m in model.messages[-1]))
        self.assertEqual(len(task['truncationErrors']),1)

    def test_agent_turn_budget_remains_stricter_than_repair_allowance(self):
        model,tools,run,task=self.run_model([response('', 'length')],MAX_AGENT_TURNS='2')
        self.assertEqual(len(model.messages),2)
        self.assertEqual(task['turns'],2)
        self.assertEqual(task['error']['code'],'budget')
        self.assertEqual(run['modelCalls'],3)
        self.assertEqual(len(task['truncationErrors']),2)
        self.assertFalse(tools.executed)

    def test_repair_raw_output_is_scrubbed_before_working_context_and_audit(self):
        model,_,run,_=self.run_model([response('unfinished secret-truncation-fixture','length'),
                                    response({'type':'final','text':'Recovered.'})])
        self.assertEqual(run['status'],'done')
        self.assertNotIn('secret-truncation-fixture',json.dumps(run))
        self.assertNotIn('secret-truncation-fixture',json.dumps(model.messages[-1]))
        self.assertIn('[REDACTED]',json.dumps(model.messages[-1]))


if __name__=='__main__':unittest.main()
