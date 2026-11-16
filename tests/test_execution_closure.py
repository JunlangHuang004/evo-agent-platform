"""Bounded final-only closure and honest review of budget-limited artifacts; no network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.tools import ToolGateway
from tests.test_engine import ScriptedProvider, task, final


class LocalFiles(ToolGateway):
    def capabilities(self):return {}


def tool(name,**args):return {'type':'tool','name':name,'args':args}


class ExecutionClosureTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:
            app.shutdown();app.wait_for_idle(4);app.repo.close()
        self.tmp.cleanup()
    def run_app(self,plan,scripts,participants,turns=4,context=40000):
        config=Config(Path(self.tmp.name)/str(len(self.apps)),{'LLM_API_KEY':'offline-closure',
            'MAX_AGENT_TURNS':str(turns),'MAX_MODEL_CALLS':'40','MAX_RUN_TOKENS':'400000',
            'MAX_RETRIES':'0','MAX_CONTEXT_CHARS':str(context),'PARALLEL_TASKS':'2'})
        model=ScriptedProvider(plan,scripts);app=Platform(config,provider=model,tools=LocalFiles(config));self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':participants,'text':'Make an actual deliverable and report honest evidence.'})
        self.assertTrue(app.wait_for_idle(5));return app,app.snapshot()['sessions']['group']['run'],model

    def test_last_existing_turn_is_explicit_final_only_and_original_contract_still_checked(self):
        plan=[{**task('Build','coordinator'),'outputContract':{'kind':'artifact','extensions':['.md']}}]
        def close(index,context,messages):
            if index==0:return tool('file.write',path='result.md',content='Actual output.\n')
            if index==1:return {'type':'publish_artifact','path':'result.md'}
            if index==2:return tool('file.list')
            self.assertEqual(context['progressReceipt']['executionPhase'],'final_only')
            self.assertTrue(any(m['content'].startswith('最后收尾约束：') for m in messages))
            return final('Published the actual result; semantic acceptance remains separate.')
        _,run,model=self.run_app(plan,{'Build':close},['coordinator'])
        producer=next(t for t in run['steps'] if t.get('kind')!='planning')
        self.assertEqual(producer['status'],'done');self.assertEqual(producer['turns'],4)
        self.assertEqual(producer['outputValidation']['status'],'passed')
        self.assertEqual(model.counts['Build'],4);self.assertEqual(run['modelCalls'],5)
        self.assertEqual(run['budgetExtensions'],0)

    def test_last_nonfinal_never_executes_or_becomes_success_and_preserves_budget_error(self):
        plan=[{**task('Build','coordinator'),'outputContract':{'kind':'artifact','extensions':['.md']}}]
        actions=[tool('file.write',path='result.md',content='Current published result.\n'),
                 {'type':'publish_artifact','path':'result.md'},tool('file.list'),
                 tool('file.write',path='result.md',content='THIS LAST TURN MUST NOT EXECUTE')]
        app,run,model=self.run_app(plan,{'Build':actions},['coordinator'])
        producer=next(t for t in run['steps'] if t.get('kind')!='planning')
        self.assertEqual(producer['status'],'partial');self.assertEqual(producer['error']['code'],'budget')
        self.assertEqual(producer['finalizationFailure']['code'],'final_action_required')
        self.assertEqual(producer['partialOutput']['acceptance'],'unassessed')
        self.assertEqual((app.tools.workspace('coordinator',run['id'])/'result.md').read_text(),'Current published result.\n')
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='file.write']),1)
        self.assertEqual(model.counts['Build'],4);self.assertEqual(run['modelCalls'],5)
        self.assertEqual(len(producer['outputs']),1);self.assertFalse(producer.get('validation',{}).get('verified'))

    def test_final_done_without_required_artifact_remains_partial_without_extra_repair_turn(self):
        plan=[{**task('Build','coordinator'),'outputContract':{'kind':'artifact','extensions':['.md']}}]
        _,run,model=self.run_app(plan,{'Build':[tool('file.list'),final('I assert it is delivered')]},['coordinator'],turns=2)
        producer=next(t for t in run['steps'] if t.get('kind')!='planning')
        self.assertEqual(producer['status'],'partial');self.assertEqual(producer['outputValidation']['status'],'failed')
        self.assertEqual(producer['outputs'],[]);self.assertEqual(model.counts['Build'],2)
        self.assertEqual(run['modelCalls'],3);self.assertFalse(run['artifacts'])

    def test_partial_budget_producer_can_be_read_by_explicit_reviewer_but_release_still_skips(self):
        plan=[{**task('Build','executor'),'outputContract':{'kind':'artifact','extensions':['.md']}},
              task('QA','reviewer',['Build'],['review']),task('Release','coordinator',['Build','QA'])]
        def inspect(index,context,messages):
            dep=next(d for d in context['dependencyResults'] if d['title']=='Build')
            self.assertEqual(dep['status'],'partial');self.assertEqual(dep['error']['code'],'budget')
            self.assertEqual(dep['partialOutput']['acceptance'],'unassessed')
            if index==0:
                self.assertEqual(len(context['authorizedArtifacts']),1)
                return tool('file.read',path=context['authorizedArtifacts'][0]['path'])
            if index==1:
                latest=json.loads(next(m['content'] for m in reversed(messages) if m['content'].startswith('动作的真实结果：')).split('：',1)[1])
                self.assertEqual(latest['content'],'Inspectable real output.\n')
                return {'type':'review','taskId':dep['id'],'verdict':'pass',
                        'feedback':'Actual partial text snapshot read; editorial assessment only, producer is incomplete.',
                        'evidenceCallIds':[latest['callId']]}
            return final('Read surviving snapshot independently; producer is still incomplete.')
        actions=[tool('file.write',path='result.md',content='Inspectable real output.\n'),
                 {'type':'publish_artifact','path':'result.md'},tool('file.list')]
        _,run,model=self.run_app(plan,{'Build':actions,'QA':inspect},['coordinator','executor','reviewer'],turns=3)
        by={t['title']:t for t in run['steps']}
        self.assertEqual(by['Build']['status'],'partial');self.assertEqual(by['Build']['error']['code'],'budget')
        self.assertEqual(by['QA']['status'],'done');self.assertEqual(by['Release']['status'],'skipped')
        self.assertEqual(run['status'],'partial');self.assertEqual(model.counts['Build'],3)
        self.assertTrue(any(c['agentId']=='reviewer' and c['tool']=='file.read' and c['status']=='success' for c in run['calls']))
        self.assertFalse(by['Build'].get('validation',{}).get('verified'))

    def test_large_real_tool_result_is_valid_json_before_budget_compaction(self):
        source='line of real text\n'*1700
        def inspect(index,context,messages):
            if index==0:return tool('file.write',path='large.md',content=source)
            if index==1:return tool('file.read',path='large.md')
            receipt=json.loads(next(m['content'] for m in reversed(messages) if m['content'].startswith('动作的真实结果：')).split('：',1)[1])
            self.assertEqual(receipt['content'],source)
            return final('Read actual complete source, without truncating encoded JSON.')
        _,run,_=self.run_app([task('Read','coordinator')],{'Read':inspect},['coordinator'],turns=4,context=80000)
        self.assertEqual(run['status'],'done')

    def test_honest_partial_at_budget_closure_is_reviewable_without_changing_producer_success(self):
        plan=[{**task('Build','executor'),'outputContract':{'kind':'artifact','extensions':['.md']}},
              task('QA','reviewer',['Build'],['review']),task('Release','coordinator',['Build','QA'])]
        def inspect(index,context,messages):
            producer=context['dependencyResults'][0]
            self.assertEqual(producer['status'],'partial');self.assertEqual(producer['error']['code'],'budget')
            if index==0:return tool('file.read',path=context['authorizedArtifacts'][0]['path'])
            if index==1:
                latest=json.loads(next(m['content'] for m in reversed(messages) if m['content'].startswith('动作的真实结果：')).split('：',1)[1])
                self.assertEqual(latest['content'],'Real partial output.')
                return {'type':'review','taskId':producer['id'],'verdict':'pass',
                        'feedback':'Read the actual partial text; editorial assessment does not complete its producer.',
                        'evidenceCallIds':[latest['callId']]}
            return final('The partial published document is inspectable, not accepted business completion.')
        _,run,_=self.run_app(plan,{'Build':[tool('file.write',path='result.md',content='Real partial output.'),
            {'type':'publish_artifact','path':'result.md'},final('Published partial work; implementation is not fully finished.',status='partial')],
            'QA':inspect},['coordinator','executor','reviewer'],turns=3)
        by={t['title']:t for t in run['steps']}
        self.assertEqual(by['Build']['status'],'partial');self.assertEqual(by['Build']['closureReason'],'turn_limit_final')
        self.assertEqual(by['Build']['partialOutput']['executionStatus'],'partial')
        self.assertEqual(by['Build']['outputValidation']['status'],'passed')
        self.assertEqual(by['QA']['status'],'done');self.assertEqual(by['Release']['status'],'skipped')
        self.assertTrue(any(c['tool']=='file.read' and c['agentId']=='reviewer' and c['status']=='success' for c in run['calls']))
        self.assertFalse(by['Build']['validation']['verified'])

    def test_early_partial_allows_only_explicit_review_but_contract_error_remains_blocked(self):
        for kind in ('early','wrong_format'):
            plan=[{**task('Build','executor'),'outputContract':{'kind':'artifact','extensions':['.html']}},
                  task('QA','reviewer',['Build'],['review']),
                  task('Release','coordinator',['Build','QA'])]
            actions=[tool('file.write',path='draft.md',content='A document, not the requested HTML.'),
                     {'type':'publish_artifact','path':'draft.md'},final('Only a draft exists.',status='partial')]
            def inspect_draft(index,context,messages):
                if index==0:return tool('file.read',path=context['authorizedArtifacts'][0]['path'])
                latest=json.loads(next(m['content'] for m in reversed(messages) if m['content'].startswith('动作的真实结果：')).split('：',1)[1])
                if index==1:
                    self.assertEqual(latest['content'],'A document, not the requested HTML.')
                    return {'type':'review','taskId':context['dependencyResults'][0]['id'],'verdict':'rework',
                            'feedback':'The actual draft is Markdown, not the required HTML; produce the missing HTML deliverable.',
                            'evidenceCallIds':[latest['callId']]}
                self.assertFalse(latest['ok']);self.assertIn('返工次数上限',latest['error']['message'])
                return final('The missing HTML remains unresolved; the original zero-rework allowance was not expanded.',status='partial')
            _,run,model=self.run_app(plan,{'Build':actions,'QA':inspect_draft},['executor','reviewer','coordinator'],turns=4 if kind=='early' else 3)
            by={t['title']:t for t in run['steps']}
            self.assertEqual(by['Build']['status'],'partial');self.assertNotIn('partialOutput',by['Build'])
            self.assertEqual(by['Release']['status'],'skipped');self.assertEqual(model.counts['Release'],0)
            self.assertEqual(run['status'],'partial')
            self.assertFalse(by['Build'].get('validation',{}).get('verified'))
            if kind=='early':
                # Voluntarily published partial work is inspectable, never
                # producer completion and never the budget-only exception.
                self.assertIsNone(by['Build']['error'])
                # Missing HTML cannot get an editorial pass instead of browser
                # evidence. Explicit rework is denied by this fixture's original
                # MAX_RETRIES=0; the reviewer must honestly remain partial.
                self.assertEqual(by['QA']['status'],'partial');self.assertEqual(model.counts['QA'],3)
                self.assertEqual(by['Build']['revision'],0)
                self.assertTrue(any(c['tool']=='file.read' and c['agentId']=='reviewer' and c['status']=='success' for c in run['calls']))
                self.assertTrue(any(e['type']=='action_rejected' and e['payload'].get('action')=='review' and '返工次数上限' in e['message'] for e in run['events']))
                self.assertFalse(any(e['type']=='review_rework' for e in run['events']))
            else:
                self.assertEqual(by['Build']['error']['code'],'output_contract')
                self.assertEqual(by['QA']['status'],'skipped');self.assertEqual(model.counts['QA'],0)

    def test_rework_archives_budget_partial_evidence_without_leaking_it_into_new_revision(self):
        plan=[{**task('Build','executor'),'outputContract':{'kind':'artifact','extensions':['.md']}},
              task('QA','reviewer',['Build'],['review'])]
        def build(index,context,messages):
            revision=bool(context['task'].get('feedback'))
            path='result-v'+str(int(revision)+1)+'.md'
            return [tool('file.write',path=path,content='Version '+str(int(revision)+1)),
                    {'type':'publish_artifact','path':path},
                    final('Actual revised output.' if revision else 'Budget-limited first version.',status='done' if revision else 'partial')][index%3]
        def qa(index,context,messages):
            if index==0:
                target=context['dependencyResults'][0]
                return {'type':'review','taskId':target['id'],'verdict':'rework','feedback':'Please revise the actual document.'}
            return final('Revised version inspected; original budget failure remains historical.')
        # One bounded reviewer-requested revision is authorized by this fixture.
        config=Config(Path(self.tmp.name)/'rework',{'LLM_API_KEY':'offline-closure','MAX_AGENT_TURNS':'3',
            'MAX_RETRIES':'1','MAX_MODEL_CALLS':'40','MAX_RUN_TOKENS':'400000'})
        model=ScriptedProvider(plan,{'Build':build,'QA':qa});app=Platform(config,provider=model,tools=LocalFiles(config));self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':['executor','reviewer'],'text':'Revise a real partial document after independent review.'})
        self.assertTrue(app.wait_for_idle(5));run=app.snapshot()['sessions']['group']['run'];producer=next(t for t in run['steps'] if t['title']=='Build')
        self.assertEqual(producer['status'],'done');self.assertEqual(producer['revision'],1)
        self.assertNotIn('partialOutput',producer);self.assertNotIn('closureReason',producer)
        old=producer['partialOutputHistory'][0]
        self.assertEqual(old['revision'],0);self.assertEqual(old['error']['code'],'budget')
        self.assertEqual(old['partialOutput']['revision'],0);self.assertEqual(old['partialOutput']['executionStatus'],'partial')


if __name__=='__main__':unittest.main()
