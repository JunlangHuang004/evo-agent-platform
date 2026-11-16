"""Real post-compaction receipt protection must fit the existing context ceiling."""
import copy
import json
import types
import unittest
from unittest.mock import patch

from backend.context_budget import _initial_status, adapt_context
from backend.engine import Platform
from backend.runtime import ProtocolError, compress_messages, fingerprint
from tests.test_file_context_cache import tool


class ContextProjectionBudgetTests(unittest.TestCase):
    def fixture(self, own=True, system_chars=9500, answer='The actual requester requirement.'):
        app=object.__new__(Platform)
        app.team_id='newsroom'
        app.config=types.SimpleNamespace(scrub=lambda value:value)
        source={'id':'chief-task','agentId':'chief_editor','revision':0}
        task=source if own else {'id':'reporter-task','agentId':'reporter','revision':0}
        binding={'question':'Which real event, audience and publication channels should this report use?'}
        receipt={'id':'real-reply','approvalId':'real-approval','runId':'run-projection','teamId':'newsroom',
                 'channelId':'group','taskId':source['id'],'agentId':source['agentId'],'revision':0,
                 'bindingHash':fingerprint(binding),'choiceId':'answer','answer':answer,'late':False,'receivedAt':1}
        approval={'id':'real-approval','type':'clarification','taskId':source['id'],'agentId':source['agentId'],
                  'taskRevision':0,'binding':binding,'bindingHash':fingerprint(binding),'choices':[],
                  'clarificationReceipt':receipt,'answerDelivery':{'receiptId':receipt['id'],'appliedAt':2}}
        initial={'task':{'id':task['id'],'goal':'Produce source-backed reporting'},'runGoal':'Original user request',
                 'humanClarifications':[],'taskBoard':{'tasks':[{'id':str(i),'agentId':'reporter','status':'pending'} for i in range(15)]}}
        ask={'role':'assistant','content':json.dumps({'type':'ask_human','question':binding['question']})}
        raw={'role':'user','content':app._clarification_message(receipt)}
        task['_messages']=[{'role':'system','content':'s'*system_chars},
                           {'role':'user','content':json.dumps(initial)},
                           *( [ask,raw] if own else [{'role':'assistant','content':'{"type":"accept"}'}] ),
                           *[{'role':'user','content':'ordinary history '+str(i)+' '+('x'*30000)} for i in range(4)]]
        run={'id':'run-projection','teamId':'newsroom','channelId':'group','steps':[source] if own else [source,task],
             'approvals':[approval],'events':[],'transcript':[{'kind':'action_result','text':raw['content']}]}
        app._protect_clarification_context(run,task,'group')
        return app,run,task,approval,ask,raw

    def protect(self,app,run,task,source):
        def callback(projected):
            app._protect_clarification_context(run,{**task,'_messages':projected},'group',source_messages=source)
            return projected
        return callback

    def test_actual_initial_reserialization_overhead_is_reserved_without_growing_capacity(self):
        app,run,task,approval,ask,raw=self.fixture(own=False)
        source=copy.deepcopy(task['_messages']);original=copy.deepcopy(source)
        current_protection=self.protect(app,run,task,source)
        def legacy_protection(projected):
            # Reproduce the former default-spaced serialization explicitly.
            # Production now uses compact JSON, but the budget fitter must
            # still handle real server-owned projection overhead correctly.
            projected=current_protection(projected)
            initial=json.loads(projected[1]['content'])
            projected[1]['content']=json.dumps(initial,ensure_ascii=False)
            return projected
        old=compress_messages(_initial_status(source,{'activeChars':40000}),40000)
        legacy_protection(old)
        self.assertGreater(sum(len(message['content']) for message in old),40000,'reproduce the prior over-cap projection')
        projected,state=adapt_context(source,40000,protect_projection=legacy_protection)
        self.assertEqual(state['activeChars'],40000)
        self.assertLessEqual(state['characters'],40000)
        self.assertGreater(state['attempts'][0]['projectionReserveChars'],0)
        self.assertGreater(state['attempts'][0]['projectionPasses'],1)
        self.assertEqual(json.loads(projected[1]['content'])['humanClarifications'][0]['answer'],approval['clarificationReceipt']['answer'])
        self.assertEqual(source,original)
        # The current compact production callback must independently retain
        # the same actual question/answer within the unchanged ceiling.
        current,current_state=adapt_context(source,40000,protect_projection=current_protection)
        self.assertEqual(current_state['activeChars'],40000)
        self.assertLessEqual(current_state['characters'],40000)
        expected=json.loads(projected[1]['content'])['humanClarifications']
        self.assertEqual(json.loads(current[1]['content'])['humanClarifications'],expected)
        self.assertEqual(source,original)
        self.assertEqual(run['events'],[])
        self.assertEqual(len(run['transcript']),1)

    def test_actual_answered_question_tail_is_restored_inside_budget_and_repeated_projection_is_stable(self):
        app,run,task,approval,ask,raw=self.fixture(answer='A genuine requirement with details. '*120)
        original_approval=copy.deepcopy(approval)
        for _ in range(3):
            source=copy.deepcopy(task['_messages'])
            projected,state=adapt_context(source,40000,protect_projection=self.protect(app,run,task,source))
            self.assertEqual(projected[-2:],[ask,raw])
            self.assertLessEqual(state['characters'],40000)
            self.assertEqual(sum(message['content']==raw['content'] for message in projected),1)
            task['_messages']=projected
        self.assertEqual(approval,original_approval)
        self.assertEqual(run['events'],[])
        self.assertEqual(len(run['transcript']),1)

    def test_insufficient_true_essentials_still_fail_at_ceiling_without_mutating_receipts(self):
        app,run,task,approval,ask,raw=self.fixture(system_chars=79000,answer='Actual answer '*300)
        source=copy.deepcopy(task['_messages']);original=copy.deepcopy(source);saved=copy.deepcopy(approval)
        with self.assertRaisesRegex(ProtocolError,'上下文容量不足'):
            adapt_context(source,80000,protect_projection=self.protect(app,run,task,source))
        self.assertEqual(source,original);self.assertEqual(approval,saved)

    def test_reserved_space_does_not_turn_omitted_source_metadata_into_a_body(self):
        app,run,task,approval,ask,raw=self.fixture(own=False)
        content='Actual fetched body '+('z'*70000)
        task['_messages']=task['_messages'][:2]+tool('web.fetch',{'url':'https://example.org/article'},
            {'ok':True,'url':'https://example.org/article','content':content,'callId':'actual-source'})
        source=copy.deepcopy(task['_messages'])
        projected,state=adapt_context(source,80000,protect_projection=self.protect(app,run,task,source))
        self.assertEqual(state['activeChars'],80000)
        self.assertLessEqual(state['characters'],80000)
        self.assertEqual(state['missingBodiesAfter'],1)
        self.assertEqual(state['missingBodyReferences'][0]['url'],'https://example.org/article')
        self.assertNotIn(content,''.join(message['content'] for message in projected))
        self.assertIn('contentOmitted',str(projected))

    def test_non_capacity_protection_errors_are_never_hidden_or_retried(self):
        callback=unittest.mock.Mock(side_effect=ProtocolError('receipt provenance mismatch'))
        with self.assertRaisesRegex(ProtocolError,'provenance mismatch'):
            adapt_context([{'role':'system','content':'protocol'},{'role':'user','content':'request'}],40000,protect_projection=callback)
        self.assertEqual(callback.call_count,1)

    def test_projection_retries_are_bounded_and_never_send_an_over_limit_view(self):
        def impossible(view):
            return view+[{'role':'user','content':'mandatory server protection '*4000}]
        with patch('backend.context_budget.compress_messages',wraps=compress_messages) as compressor:
            with self.assertRaisesRegex(ProtocolError,'上下文容量不足'):
                adapt_context([{'role':'system','content':'protocol'},{'role':'user','content':'request'}],80000,protect_projection=impossible)
            self.assertLessEqual(compressor.call_count,8)


if __name__=='__main__':unittest.main()
