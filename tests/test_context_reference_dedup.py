"""Model-view aliases preserve exact evidence; no source bytes are recovered."""
import copy
import hashlib
import json
import unittest

from backend.context_budget import visible_bodies
from backend.runtime import (
    ProtocolError, _compact_initial_context, _expand_context_references,
    _deduplicate_context_references, compress_messages,
)


class ContextReferenceDedupTests(unittest.TestCase):
    def context(self):
        ids=['artifact-'+str(i)+'x'*40 for i in range(8)]
        meaning='Call evidence only; private versions may change. No source body or acceptance. '*4
        feedback='PASS within scope; market source could NOT be checked. '+('Limit remains. '*35)
        validation={'verdict':'pass','verified':False,'targetRevision':0,
                    'artifactHashes':{ids[0]:'a'*64},'feedback':feedback}
        rows=[]
        for i in range(6):
            criterion=['Use actual evidence and state the remaining unknowns. '*7]
            checked={'status':'passed','kind':'artifact','semanticQuality':'unassessed',
                     'checkedArtifactIds':ids,'matchedArtifactIds':ids,'issues':[]}
            rows.append({'id':'task-'+str(i),'revision':i,'agentId':'research','status':'done',
                'acceptanceCriteria':criterion,'outputs':ids,'result':'',
                'validation':copy.deepcopy(validation) if i==0 else None,
                'taskOutput':{'schemaVersion':1,'taskId':'task-'+str(i),'revision':i,
                    'expectedOutput':copy.deepcopy(criterion),'artifactIds':list(ids),
                    'outputValidation':checked,'reviewValidation':copy.deepcopy(validation) if i==0 else None,
                    'evidenceAvailability':{'calls':True,'artifacts':True,'review':False},
                    'observedWork':{'fileWrites':1,'meaning':meaning},
                    'toolFailures':None,'toolUncertainties':None,'executionError':None}})
        return {'task':{'id':'summary','goal':'Keep original goal and all limitations.'},
                'runGoal':'Original complete goal','dependencyResults':rows,
                'humanClarifications':[{'receiptId':'real-reply','answer':'Use explicit simulated assumptions.'}],
                'authorizedArtifacts':[{'id':ids[0],'path':'imports/input.md','sha256':'a'*64}],
                'ownArtifacts':[],'groupHistory':[]}

    def compact(self,context):
        return json.loads(_compact_initial_context(json.dumps(context,ensure_ascii=False),80000,minimal=True))

    def test_exact_duplicates_are_pinned_and_expand_to_original_unique_evidence(self):
        source=self.context();original=copy.deepcopy(source);view=self.compact(source)
        self.assertIn('_modelContextReferences',view)
        expanded=_expand_context_references(view)
        self.assertEqual(source,original)
        for before,after in zip(source['dependencyResults'],expanded['dependencyResults']):
            self.assertEqual(after,before)
        self.assertEqual(view['task'],source['task'])
        self.assertEqual(view['humanClarifications'],source['humanClarifications'])
        self.assertEqual(view['authorizedArtifacts'],source['authorizedArtifacts'])
        self.assertEqual(view['dependencyResults'][0]['validation'],source['dependencyResults'][0]['validation'])
        self.assertLess(len(json.dumps(view)),len(json.dumps(source)))

    def test_partial_feedback_is_not_removed_because_the_verdict_is_pass(self):
        source=self.context()
        source['dependencyResults'][0]['taskOutput']['reviewValidation']['feedback']='PASS, but original source unverified and risk remains.'
        view=self.compact(source);expanded=_expand_context_references(view)
        before=source['dependencyResults'][0];after=expanded['dependencyResults'][0]
        self.assertEqual(after['validation']['feedback'],before['validation']['feedback'])
        self.assertEqual(after['taskOutput']['reviewValidation']['feedback'],before['taskOutput']['reviewValidation']['feedback'])
        self.assertNotEqual(after['validation']['feedback'],after['taskOutput']['reviewValidation']['feedback'])

    def test_three_compactions_preserve_pins_and_every_original_value(self):
        source=self.context();view=self.compact(source)
        for _ in range(3):
            view=self.compact(view)
            self.assertEqual(_expand_context_references(view)['dependencyResults'],source['dependencyResults'])
            self.assertLessEqual(len(json.dumps(view,ensure_ascii=False)),80000)

    def test_missing_changed_or_malformed_reference_fails_explicitly(self):
        view=self.compact(self.context());reference=view['_modelContextReferences']['references'][0]
        corrupt=copy.deepcopy(view);reference=corrupt['_modelContextReferences']['references'][0]
        reference['sha256']='0'*64
        with self.assertRaisesRegex(ProtocolError,'已变化'):_expand_context_references(corrupt)
        corrupt=copy.deepcopy(view);corrupt['_modelContextReferences']['references'][0]['path']='/dependencyResults/99/outputs'
        with self.assertRaisesRegex(ProtocolError,'目标缺失'):_expand_context_references(corrupt)
        corrupt=copy.deepcopy(view);corrupt['dependencyResults'][0]['taskOutput']['expectedOutput']={'$contextRef':False}
        with self.assertRaisesRegex(ProtocolError,'索引越界'):_expand_context_references(corrupt)
        corrupt=copy.deepcopy(view);corrupt['_modelContextReferences']['version']=True
        with self.assertRaisesRegex(ProtocolError,'索引无效'):_expand_context_references(corrupt)
        corrupt=copy.deepcopy(view);corrupt['dependencyResults'][0]['taskOutput']['observedWork']['meaning']='A changed anchor.'
        with self.assertRaisesRegex(ProtocolError,'已变化'):_expand_context_references(corrupt)

    def test_different_order_bool_and_nonversioned_values_are_not_inferred_equal(self):
        source=self.context()
        source['dependencyResults'][0]['taskOutput']['artifactIds'].reverse()
        source['dependencyResults'][1]['taskOutput']['evidenceAvailability']['review']=0
        source['dependencyResults'][2]['taskOutput']['schemaVersion']=True
        source['dependencyResults'][3]['taskOutput']['observedWork']['meaning']+=' $contextRef is ordinary quoted text.'
        expanded=_expand_context_references(self.compact(source))
        self.assertEqual(expanded['dependencyResults'],source['dependencyResults'])
        self.assertIs(type(expanded['dependencyResults'][0]['taskOutput']['evidenceAvailability']['review']),bool)
        self.assertIs(type(expanded['dependencyResults'][1]['taskOutput']['evidenceAvailability']['review']),int)

    def test_fixed_28k_and_80k_keep_actual_body_and_questions_without_retrieval(self):
        context=self.context();body='An actual current source body.\n'*120
        sha=hashlib.sha256(body.encode()).hexdigest()
        action={'type':'tool','name':'file.read','args':{'path':'imports/input.md'}}
        result={'ok':True,'path':'imports/input.md','sha256':sha,'content':body,'mode':'full','truncated':False,'callId':'real-read'}
        messages=[{'role':'system','content':'Exact original action/tool protocol.'},
                  {'role':'user','content':json.dumps(context)},
                  {'role':'user','content':'old optional prose '*6000},
                  {'role':'assistant','content':json.dumps(action)},
                  {'role':'user','content':'动作的真实结果：'+json.dumps(result)}]
        original=copy.deepcopy(messages)
        for limit in (28000,80000):
            view=compress_messages(messages,limit)
            self.assertLessEqual(sum(len(m['content']) for m in view),limit)
            self.assertEqual(len(visible_bodies(view)),1)
            self.assertEqual(visible_bodies(view)[0]['sha256'],sha)
            initial=_expand_context_references(json.loads(view[1]['content']))
            self.assertEqual(initial['humanClarifications'],context['humanClarifications'])
            self.assertEqual(initial['dependencyResults'],context['dependencyResults'])
        self.assertEqual(messages,original)

    def test_no_duplicate_does_not_invent_a_reference_or_silently_fit_too_small(self):
        source={'task':{'id':'task-one','goal':'unique goal'},'dependencyResults':[],
                'humanClarifications':[{'answer':'This full requirement must stay. '*100}]}
        self.assertNotIn('_modelContextReferences',self.compact(source))
        with self.assertRaisesRegex(ProtocolError,'未静默丢弃'):
            _compact_initial_context(json.dumps(source),100,minimal=True)

    def test_literal_marker_collision_input_immutability_and_width_retries(self):
        source=self.context();original=copy.deepcopy(source)
        view=_deduplicate_context_references(source)
        self.assertEqual(source,original)
        self.assertEqual(_expand_context_references(view),original)
        source['task']['inputs']={'literal':{'$contextRef':0}}
        original=copy.deepcopy(source)
        view=_deduplicate_context_references(source)
        self.assertNotIn('_modelContextReferences',view)
        self.assertEqual(view,original)
        self.assertEqual(source,original)
        source['groupHistory']=[{'role':'human','text':'optional old prose '*10000}]*4
        raw=json.dumps(source,ensure_ascii=False)
        minimum=_compact_initial_context(raw,200000,minimal=True)
        view=json.loads(_compact_initial_context(raw,len(minimum)+10))
        self.assertEqual(view['task'],source['task'])
        self.assertEqual(view['dependencyResults'],source['dependencyResults'])
        self.assertNotIn('_modelContextReferences',view)


if __name__=='__main__':
    unittest.main()
