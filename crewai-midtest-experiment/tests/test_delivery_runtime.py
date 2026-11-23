"""Regress the measured failure modes without relaxing evidence gates."""
import copy,json,sys,unittest
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'vendor'))
from live.runtime import check_budget,compact_messages,proposed_action,receipt_view,repetitive,WorkBudgetExhausted,review_directive
from live.platform import task_artifacts
import test_live_platform as base


class ContextTests(unittest.TestCase):
    def test_linked_current_source_is_retained_while_it_fits(self):
        html='<form id="exact-booking-form"><input id="actual-title"></form>'
        body='Action: gateway\nAction Input: '+json.dumps({'operation':'file.write','arguments':{'path':'index.html','content':html*30}})+'\nObservation: {"ok":true,"sha256":"real"}'
        messages=[{'role':'system','content':'contract'},{'role':'user','content':'Build linked HTML/JS'},
                  {'role':'assistant','content':body},{'role':'user','content':'Next create app.js'},
                  {'role':'assistant','content':'Use the actual IDs above'}, {'role':'user','content':'continue'}]
        result=compact_messages(messages,limit=115000)
        self.assertEqual(result[2]['content'],body);self.assertIn('exact-booking-form',result[2]['content'])
    def test_identical_read_dedup_preserves_the_newest_actual_source(self):
        body='Action: gateway\nAction Input: {"operation":"file.read","arguments":{"path":"app.js"}}\nObservation: {"ok":true,"sha256":"current","content":"ACTUAL SOURCE"}'
        result=compact_messages([{'role':'system','content':'contract'},{'role':'user','content':'goal'},
                                 {'role':'assistant','content':body},{'role':'user','content':'reflect'}, {'role':'assistant','content':body}])
        self.assertNotIn('ACTUAL SOURCE',result[2]['content']);self.assertIn('ACTUAL SOURCE',result[-1]['content'])
    def test_old_file_proposals_shrink_but_recent_observation_survives(self):
        action='Thought: write\nAction: gateway\nAction Input: '+json.dumps({'operation':'file.write','arguments':{'path':'app.js','content':'old source '*7000}})
        messages=[{'role':'system','content':'PERMISSION CONTRACT'},{'role':'user','content':'CURRENT GOAL'},
                  {'role':'assistant','content':action},{'role':'user','content':'real receipt sha256=abc'},
                  {'role':'assistant','content':'Read current file'}, {'role':'user','content':'CURRENT ACTUAL FILE\n'+('current line\n'*1300)}]
        result=compact_messages(messages)
        self.assertLessEqual(sum(len(m['content']) for m in result),48000)
        self.assertIn('CURRENT ACTUAL FILE',result[-1]['content']);self.assertNotIn('old source '*100,json.dumps(result))
        self.assertEqual(messages[2]['content'],action)
    def test_approval_wait_and_raw_browser_evidence_remain_in_ledger(self):
        raw={'ok':False,'stdout':'browser JSON '*9000,'browser':{'ok':False,'assertions':9,'steps':[{'action':'assert_text','ok':False,'expected':'Booked','actual':'Error'}],'testedFiles':[{'sha256':'abc'}],'reportPath':'report.json'}}
        before=copy.deepcopy(raw);view=receipt_view('code.test',raw)
        self.assertNotIn('stdout',view);self.assertEqual(view['browser']['failedSteps'][0]['actual'],'Error')
        self.assertEqual(view['browser']['testedFiles'],raw['browser']['testedFiles']);self.assertEqual(before,raw)
    def test_crewai_combined_action_observation_keeps_actual_write_success(self):
        action='Thought: write\nAction: gateway\nAction Input: '+json.dumps({'operation':'file.write','arguments':{'path':'app.js','content':'old source '*7000}})
        action+='\nObservation: {"ok":true,"path":"app.js","sha256":"actual-sha"}'
        messages=[{'role':'system','content':'permissions'},{'role':'user','content':'goal'},
                  {'role':'assistant','content':action},{'role':'user','content':'reflect'},
                  {'role':'assistant','content':'next action'},{'role':'user','content':'recent observation'}]
        result=compact_messages(messages)
        rendered=json.dumps(result)
        self.assertIn('actual-sha',rendered);self.assertIn('Observation:',rendered)
        self.assertNotIn('old source '*100,rendered)
    def test_repetitive_promises_are_rejected_but_real_code_is_not(self):
        self.assertTrue(repetitive(('Let me call.\nThen write.\nNow.\nOK.\n')*30))
        self.assertFalse(repetitive('Thought: execute now\nAction: gateway\nAction Input: '+json.dumps({'operation':'file.write','arguments':{'path':'x.py','content':'print(1)\n'*60}})))
    def test_review_can_use_the_reserved_budget_after_production_stops(self):
        r={'limits':{'calls':100,'tokens':1000,'reviewReserveCalls':20,'reviewReserveTokens':300},'modelCalls':81,'tokens':701,'budgetUsage':{'work':{'calls':81,'tokens':701}}}
        with self.assertRaises(WorkBudgetExhausted):check_budget(r,{'agentId':'dev'})
        check_budget(r,{'reviewOf':'producer'});check_budget(r,{'acceptance':True})
        r['tokens']=1000
        with self.assertRaisesRegex(RuntimeError,'Token budget'):check_budget(r,{'acceptance':True})
    def test_only_complete_unquoted_terminal_action_is_recoverable(self):
        valid='Thought: next\nAction: gateway\nAction Input: {"operation":"file.write","arguments":{"path":"x.md","content":"x"}}'
        self.assertIsNotNone(proposed_action(valid))
        self.assertIsNone(proposed_action('Final Answer: example\n'+valid))
        self.assertIsNone(proposed_action('```\n'+valid));self.assertIsNone(proposed_action(valid[:-3]))


class DeliveryTests(unittest.TestCase):
    setUp=base.RuntimeTests.setUp
    tearDown=base.RuntimeTests.tearDown
    execute=base.RuntimeTests.execute
    start=base.RuntimeTests.start
    finish=base.RuntimeTests.finish
    def test_actual_written_files_reach_qa_when_model_omits_publication(self):
        original=self.p.executor
        def execute(p,ctx,s,d,e,tools):
            if s['id']=='write':
                p.tool(ctx,s,'file.write',{'path':'checklist.md','content':'# Checklist\n- Verify actual evidence\n'})
                return 'I still need to inspect the file before handing it off.'
            return original(p,ctx,s,d,e,tools)
        self.p.executor=execute;r=self.finish(self.start())
        self.assertEqual((r['status'],r['productVerdict']),('done','pass'))
        self.assertTrue(r['steps'][0]['runtimeCheckpointUsed']);self.assertEqual(r['reviews'][0]['reviewer'],'qa')
        self.assertTrue(any(c['tool']=='file.read' and c['agentId']=='qa' for c in r['calls']))
    def test_native_crewai_handoff_finishes_after_one_real_publication(self):
        from live.model import execute
        from backend.sandbox import write_text
        r=self.finish(self.start());ctx=self.p.contexts[r['id']]
        write_text(ctx['gateway'].workspace('dev',r['id']),'native.md','Actual file for handoff')
        step={'id':'native','agentId':'dev','requiresArtifacts':True,'revision':0,'dependsOn':[]}
        ctx['run']['steps'].append(step);bodies=[]
        class Response:
            def raise_for_status(self):pass
            def json(self):return {'model':'deepseek-flash','choices':[{'message':{'content':'Thought: Files ready.\nAction: handoff\nAction Input: {"paths":["native.md"],"summary":"Actual file ready for independent review"}'},'finish_reason':'stop'}],'usage':{'total_tokens':20}}
        class Session:
            trust_env=False
            def post(self,*args,**kwargs):
                bodies.append(kwargs['json'])
                if len(bodies)>1:raise AssertionError('A successful handoff must not ask the model to restate the product')
                return Response()
            def close(self):pass
        with patch('live.model.requests.Session',return_value=Session()):
            result=execute(self.p,ctx,step,'Publish native.md for independent checking','Actual immutable handoff')
        self.assertEqual(len(bodies),1);self.assertIn('pending independent review',result)
        self.assertTrue(any(a['path']=='native.md' for a in ctx['run']['artifacts']))
    def test_native_crewai_verdict_commits_real_review_without_an_extra_model_call(self):
        from live.model import execute
        r=self.finish(self.start());ctx=self.p.contexts[r['id']];step=ctx['run']['steps'][1];bodies=[]
        class Response:
            def raise_for_status(self):pass
            def json(self):return {'model':'deepseek-flash','choices':[{'message':{'content':'Thought: Evidence checked.\nAction: verdict\nAction Input: {"verdict":"pass","summary":"Independently checked the actual current Markdown file and its required checklist item"}'},'finish_reason':'stop'}],'usage':{'total_tokens':20}}
        class Session:
            trust_env=False
            def post(self,*args,**kwargs):
                bodies.append(kwargs['json'])
                if len(bodies)>1:raise AssertionError('A committed verdict must finish without a redundant final generation')
                return Response()
            def close(self):pass
        before=len(ctx['run']['reviews'])
        with patch('live.model.requests.Session',return_value=Session()):
            result=execute(self.p,ctx,step,'Independently check write and record the verdict','Actual current-version verdict')
        self.assertEqual(len(bodies),1);self.assertEqual(len(ctx['run']['reviews']),before+1)
        self.assertIn('pass',result)
    def test_republishing_unchanged_bytes_preserves_valid_review(self):
        r=self.finish(self.start());ctx=self.p.contexts[r['id']];s=ctx['run']['steps'][0]
        before=s['revision'];count=len(ctx['run']['artifacts'])
        result=self.p.tool(ctx,s,'artifact.publish',{'paths':['checklist.md'],'to':['qa']})
        self.assertEqual(len(ctx['run']['artifacts']),count);self.assertEqual(s['revision'],before)
        self.assertTrue(result['reusedUnchanged']);self.assertIsNotNone(self.p.current_review(ctx['run'],s))
    def test_review_control_requires_missing_read_and_does_not_choose_a_pass(self):
        r=self.finish(self.start());ctx=self.p.contexts[r['id']];s=ctx['run']['steps'][1]
        self.assertIn('REVIEW RECORDS COMMITTED',review_directive(ctx,s))
        ctx['run']['reviews']=[]
        directive=review_directive(ctx,s)
        self.assertIn('REAL pass or fail',directive);self.assertIn('NOT a product pass',directive)
        ctx['run']['calls']=[c for c in ctx['run']['calls'] if not(c['agentId']=='qa' and c['tool']=='file.read')]
        self.assertIn('NEXT REQUIRED REVIEW ACTION: file.read',review_directive(ctx,s))
    def test_only_review_capable_roles_are_advertised_the_verdict_operation(self):
        r=self.finish(self.start());ctx=self.p.contexts[r['id']]
        self.assertNotIn('review.submit',self.p.tool_description(ctx,'dev'))
        self.assertIn('review.submit',self.p.tool_description(ctx,'qa'))
    def test_final_changed_bytes_receive_a_new_independent_verdict(self):
        calls=[]
        def execute(p,ctx,s,d,e,tools):
            if s['id']=='planning':return json.dumps({'tasks':[
                {'id':'write','agentId':'dev','instructions':'Write first version','requiresArtifacts':True},
                {'id':'check','agentId':'qa','instructions':'Check first version','dependsOn':['write'],'reviewOf':'write'},
                {'id':'revision','agentId':'dev','instructions':'Revise product after review','dependsOn':['check'],'requiresArtifacts':True}]})
            if s['id']=='write':
                p.tool(ctx,s,'file.write',{'path':'result.md','content':'First version'})
                p.tool(ctx,s,'artifact.publish',{'paths':['result.md'],'to':['qa']});return 'Published first version'
            if s['id']=='revision':
                # Test actor makes a concurrent authorized update directly; production
                # edits still pass through the tested approval gateway.
                from backend.sandbox import write_text
                write_text(ctx['gateway'].workspace('dev',ctx['run']['id']),'result.md','Final corrected version',overwrite=True)
                p.tool(ctx,s,'artifact.publish',{'paths':['result.md'],'to':['qa']});return 'Published final version'
            targets=s.get('reviewTargets') or ['write']
            for tid in targets:
                for a in task_artifacts(ctx['run'],tid):p.tool(ctx,s,'file.read',{'path':'imports/'+a['id']+'/'+a['name']})
            p.tool(ctx,s,'review.submit',{'targetTaskIds':targets,'verdict':'pass','summary':'Independently read and checked the exact current document bytes'})
            calls.append((s['id'],targets));return 'Submitted actual review'
        self.p.executor=execute;r=self.finish(self.start())
        self.assertEqual((r['status'],r['productVerdict']),('done','pass'))
        self.assertEqual(len(r['reviews']),2);self.assertEqual(r['reviews'][-1]['targetTaskId'],'revision')
        self.assertTrue(any(s.get('acceptance') for s in r['steps']))
    def test_failed_review_triggers_bounded_real_repair_and_retest(self):
        def execute(p,ctx,s,d,e,tools):
            if s['id']=='planning':return json.dumps({'tasks':[{'id':'produce','agentId':'dev','instructions':'Write a checked note','requiresArtifacts':True}]})
            if s['agentId']=='dev':
                from backend.sandbox import write_text
                write_text(ctx['gateway'].workspace('dev',ctx['run']['id']),'note.md','correct' if s.get('repairOf') else 'defect',overwrite=bool(s.get('repairOf')))
                p.tool(ctx,s,'artifact.publish',{'paths':['note.md'],'to':['qa']});return 'Actual published note'
            ids=s['reviewTargets']
            for tid in ids:
                a=task_artifacts(ctx['run'],tid)[0];v=p.tool(ctx,s,'file.read',{'path':'imports/'+a['id']+'/'+a['name']})
                p.tool(ctx,s,'review.submit',{'targetTaskId':tid,'verdict':'pass' if v['content']=='correct' else 'fail','summary':'Actual content is correct' if v['content']=='correct' else 'Actual content says defect; replace it with the required correct note'})
            return 'Recorded pass/fail from actual bytes'
        self.p.executor=execute;r=self.finish(self.start())
        self.assertEqual((r['status'],r['productVerdict']),('done','pass'))
        self.assertEqual([v['verdict'] for v in r['reviews']],['fail','pass'])
        self.assertEqual(sum(bool(s.get('repairOf')) for s in r['steps']),1)
    def test_unexecuted_final_promise_cannot_be_counted_as_written(self):
        def execute(p,ctx,s,d,e,tools):
            if s['id']=='planning':return json.dumps({'tasks':[{'id':'produce','agentId':'dev','instructions':'Write the document','requiresArtifacts':True}]})
            return 'I will now write and publish the document.'
        self.p.executor=execute;r=self.finish(self.start())
        self.assertEqual(r['status'],'partial');self.assertEqual(r['productVerdict'],'unassessed');self.assertFalse(r['artifacts'])

if __name__=='__main__':unittest.main()
