"""Reject only demonstrably redundant reads, citing actual write provenance."""
import copy
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.runtime import compress_messages
from tests.test_file_context_cache import tool,noise
from tests.test_read_window_recovery import window
from tests.test_written_context_cache import base,write


class WrittenReadGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.app=Platform(Config(Path(self.directory.name),{'LLM_API_KEY':'offline-write-proof','MAX_RETRIES':'0'}))
        self.path='app.js';self.source='one\ntwo\nthree\n'
        self.args={'path':self.path,'startLine':1,'endLine':2}
        self.result=window(self.path,self.source,1,2)
        self.run={'id':'run-write-guard','calls':[
            {'id':'read-'+str(i),'taskId':'build','tool':'file.read','args':dict(self.args),
             'status':'success','result':copy.deepcopy(self.result)} for i in range(4)],'events':[]}
        self.task={'id':'build','agentId':'executor','_stagnantTurns':3,
                   '_messages':base()+write(self.path,self.source)}

    def tearDown(self):
        self.app.shutdown();self.app.repo.close();self.directory.cleanup()

    def guard(self):return self.app._read_loop_feedback(self.run,self.task,'file.read',self.args)

    def test_actual_full_write_covers_requested_window_without_fabricating_read_receipt(self):
        before=copy.deepcopy(self.run['calls']);answer=self.guard()
        self.assertEqual(answer['error']['code'],'read_loop')
        self.assertFalse(answer['actionExecuted']);self.assertEqual(self.run['calls'],before)
        self.assertEqual(answer['retainedReadWindows'],[])
        self.assertEqual(answer['retainedSourceBodies'][0]['sourceAction'],'file.write')
        self.assertTrue(answer['retainedSourceBodies'][0]['bodyPresent'])
        for _ in range(3):
            self.task['_messages']=compress_messages(self.task['_messages']+noise(4,10000),18000)
            self.assertEqual(self.guard()['retainedSourceBodies'][0]['sourceAction'],'file.write')

    def test_original_repeat_thresholds_still_apply_to_written_source(self):
        self.task['_stagnantTurns']=2;self.assertIsNone(self.guard())
        self.task['_stagnantTurns']=3;self.run['calls']=self.run['calls'][:3];self.assertIsNone(self.guard())
        self.run['calls'].append({'id':'different-read','taskId':'build','tool':'file.read',
                                 'status':'success','args':{'path':'other.js'},'result':{'ok':True}})
        self.run['calls'][0]['args']={'path':self.path,'startLine':3,'endLine':3}
        self.run['calls'][1]['args']={'path':self.path,'startLine':3,'endLine':3}
        self.assertIsNone(self.guard())

    def test_omitted_mismatched_mutated_unknown_and_changed_sources_do_not_block_needed_read(self):
        variants=[base(),base()+write(self.path,self.source,digest='a'*64),
                  base()+write(self.path,'different bytes')]
        for name,args,result in [('file.patch',{'path':self.path},{'ok':True}),
                                 ('file.write',{'path':self.path,'content':'maybe'},{'ok':False,'outcomeUnknown':True}),
                                 ('code.run',{'language':'javascript','code':'may write files'},{'ok':False})]:
            variants.append(base()+write(self.path,self.source)+tool(name,args,result))
        for messages in variants:
            with self.subTest(messages=messages[-1]['content'][:80]):
                self.task['_messages']=messages;self.assertIsNone(self.guard())
        self.task['_messages']=base()+write(self.path,self.source)
        self.run['calls'][-1]['result']['outcomeUnknown']=True
        self.assertIsNone(self.guard())


if __name__=='__main__':unittest.main()
