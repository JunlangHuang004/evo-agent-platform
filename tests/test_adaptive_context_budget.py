"""Offline projection decisions preserve real bodies before replacing history."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.context_budget import adapt_context, visible_bodies
from backend.runtime import ProtocolError
from tests.test_file_context_cache import tool,noise,padded
from tests.test_read_window_recovery import window
from tests.test_written_context_cache import base,write,written
from tests.test_time_only_runtime import Model,Files
from backend.engine import Platform


class AdaptiveContextBudgetTests(unittest.TestCase):
    def test_real_file_body_loss_expands_before_replace_and_highest_task_tier_does_not_fall(self):
        source=padded('actual source',33000);messages=base()+write('app.js',source)+noise(4,20000)
        original=copy.deepcopy(messages)
        projected,state=adapt_context(messages,40000)
        self.assertEqual(messages,original)
        self.assertEqual(state['activeChars'],60000);self.assertEqual(state['missingBodiesAfter'],0)
        self.assertEqual(len(visible_bodies(projected)),1)
        self.assertEqual(written(projected)['app.js']['action']['args']['content'],source)
        _,again=adapt_context(base(),state['activeChars'])
        self.assertEqual(again['activeChars'],60000)
        self.assertEqual(json.loads(projected[1]['content'])['contextCapacity']['activeChars'],60000)

    def test_web_url_sha_body_loss_expands_and_metadata_does_not_become_a_body(self):
        content='real fetched text '+('z'*33000)
        messages=base()+tool('web.fetch',{'url':'https://example.org/news'},
                 {'ok':True,'url':'https://example.org/news','content':content,'callId':'actual-fetch'})+noise(4,20000)
        projected,state=adapt_context(messages,40000)
        self.assertEqual(state['activeChars'],60000);self.assertEqual(state['missingBodiesAfter'],0)
        self.assertEqual(visible_bodies(projected)[0]['url'],'https://example.org/news')
        _,small=adapt_context(base()+[{'role':'user','content':json.dumps({'url':'https://example.org/news','sha256':'a'*64})}]+noise(4,20000),40000)
        self.assertEqual(small['activeChars'],40000);self.assertEqual(small['visibleBodiesBefore'],0)

    def test_whole_write_and_overlapping_same_sha_reads_do_not_trigger_duplicate_expansion(self):
        source=''.join('line '+str(i)+' '+('x'*38)+'\n' for i in range(1,401))
        messages=base()+write('app.js',source)
        for start,end in ((1,170),(160,300),(290,400)):
            args={'path':'app.js','startLine':start,'endLine':end}
            messages+=tool('file.read',args,window('app.js',source,start,end))
        projected,state=adapt_context(messages+noise(4,20000),40000)
        self.assertEqual(state['activeChars'],40000);self.assertEqual(state['visibleBodiesBefore'],1)
        self.assertEqual(state['missingBodiesAfter'],0)

    def test_ceiling_reports_missing_actual_body_without_faking_recovery_or_unbounded_growth(self):
        source=padded('too large',90000)
        projected,state=adapt_context(base()+write('huge.js',source),40000)
        self.assertEqual(state['activeChars'],80000);self.assertEqual(state['missingBodiesAfter'],1)
        self.assertEqual(state['missingBodyReferences'][0]['path'],'huge.js')
        self.assertFalse(visible_bodies(projected));self.assertLessEqual(state['characters'],80000)
        self.assertIn('split work',json.loads(projected[1]['content'])['contextCapacity']['nextStep'])

    def test_index_capacity_can_expand_but_other_protocol_errors_are_not_hidden(self):
        messages=[{'role':'system','content':'actual full protocol '+('x'*41000)},
                  {'role':'user','content':'Preserve the user request.'}]
        projected,state=adapt_context(messages,40000)
        self.assertEqual(state['activeChars'],60000);self.assertTrue(state['attempts'][0]['indexCapacityError'])
        self.assertEqual(projected[0],messages[0])
        with patch('backend.context_budget.compress_messages',side_effect=ProtocolError('invalid schema, not capacity')) as mocked:
            with self.assertRaisesRegex(ProtocolError,'invalid schema'):adapt_context(base(),40000)
            self.assertEqual(mocked.call_count,1)
        with self.assertRaises(ProtocolError):adapt_context([{'role':'system','content':'x'*81000}],40000)

    def test_model_hook_expands_in_same_call_budget_and_persists_real_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Config(Path(directory),{'LLM_API_KEY':'offline-adaptive','MAX_RETRIES':'0'})
            original=(config.max_completion_tokens,config.model_timeout)
            profile=config.apply_time_only_profile();self.assertEqual(profile['initialContextChars'],40000)
            source=padded('actual source',33000);observed={}
            def action(n,context,messages):
                run=model.app.state['sessions']['group']['run']
                task=next(item for item in run['steps'] if not item.get('_planning'))
                if n==0:
                    observed['deadline']=run['budget']['deadlineAt']
                    task['_messages']+=write('app.js',source)+noise(4,20000)
                    return {'type':'accept'}
                observed['capacity']=context['contextCapacity']
                observed['body']=written(messages).get('app.js',{}).get('action',{}).get('args',{}).get('content')==source
                return {'type':'final','text':'An evidence-neutral scripted response.'}
            model=Model(action);app=Platform(config,provider=model,tools=Files(config));model.app=app
            try:
                app.action({'action':'send','mode':'task','participants':['coordinator'],'text':'An offline context adaptation check.'})
                self.assertTrue(app.wait_for_idle(4));run=app.state['sessions']['group']['run']
                task=next(item for item in run['steps'] if not item.get('_planning'))
                self.assertEqual(run['status'],'done');self.assertEqual(run['modelCalls'],3)
                self.assertEqual(run['budget']['deadlineAt'],observed['deadline']);self.assertTrue(observed['body'])
                self.assertIn(task['contextBudget']['activeChars'],(60000,80000))
                self.assertEqual(app.repo.get_run(run['id'])['steps'][-1]['contextBudget'],task['contextBudget'])
                self.assertEqual(original,(config.max_completion_tokens,config.model_timeout))
                self.assertTrue(any(event['type']=='context_capacity' for event in run['events']))
                self.assertEqual(run['calls'][-1]['contextCapacity']['missingBodiesAfter'],0)
            finally:app.shutdown();app.wait_for_idle(2);app.repo.close()


if __name__=='__main__':unittest.main()
