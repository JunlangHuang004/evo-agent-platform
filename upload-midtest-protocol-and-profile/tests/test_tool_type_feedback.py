"""Recover the flat file.read seen in a live newsroom review, without executing it."""
import copy
import unittest
from backend.engine import _tool_type_feedback
from tests import test_action_not_tool as fixtures
from tests.test_engine import task,final
from tests.test_execution_closure import tool


class ToolTypeFeedbackTests(unittest.TestCase):
    def test_nested_and_flat_args_are_only_examples_and_input_is_unchanged(self):
        for proposal in ({'type':'file.read','path':'notes.md'},
                         {'type':'file.read','args':{'path':'notes.md'}}):
            before=copy.deepcopy(proposal)
            receipt=_tool_type_feedback(proposal,['file.read'])
            self.assertFalse(receipt['actionExecuted'])
            self.assertTrue(receipt['notExecuted'])
            self.assertEqual(receipt['correctActionExample'],tool('file.read',path='notes.md'))
            self.assertEqual(proposal,before)
        self.assertIsNone(_tool_type_feedback({'type':'web.fetch','url':'https://example.org'},['file.read']))
        self.assertIsNone(_tool_type_feedback({'type':[]},['file.read']))
        self.assertEqual(_tool_type_feedback({'type':'file.read','args':'bad'},['file.read'])['correctActionExample']['args'],{})
        self.assertEqual(_tool_type_feedback({'type':'file.write','content':'x'*2000},['file.write'])['correctActionExample']['args'],{})


class ToolTypeRecoveryRuntimeTests(unittest.TestCase):
    setUp=fixtures.ActionNotToolRuntimeTests.setUp
    tearDown=fixtures.ActionNotToolRuntimeTests.tearDown
    execute=fixtures.ActionNotToolRuntimeTests.execute
    def test_rejected_flat_read_executes_once_only_after_a_separate_model_proposal(self):
        seen={}
        def actor(i,c,m):
            if i==0:return tool('file.write',path='notes.md',content='Actual local evidence.')
            if i==1:return {'type':'file.read','path':'notes.md'}
            if i==2:
                receipt=fixtures.last_result(m)
                self.assertFalse(receipt['actionExecuted'])
                seen['receipt']=receipt
                return receipt['correctActionExample']
            self.assertEqual(fixtures.last_result(m)['content'],'Actual local evidence.')
            return final('Read actual bytes after explicitly correcting the proposal.')
        app,run,model=self.execute([task('Read','coordinator')],{'Read':actor},['coordinator'])
        self.assertEqual(run['status'],'done')
        reads=[call for call in run['calls'] if call['tool']=='file.read']
        self.assertEqual(len(reads),1)
        self.assertEqual(reads[0]['result']['content'],'Actual local evidence.')
        rejected=[event for event in run['events'] if event['type']=='action_rejected']
        self.assertEqual(len(rejected),1)
        self.assertEqual(model.counts['Read'],4)
