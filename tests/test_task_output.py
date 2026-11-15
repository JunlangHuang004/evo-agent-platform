"""Structured handoff retains real evidence and missing-data distinctions; no API."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.persistence import Repository
from backend.task_output import build_task_output, task_output_for_context


class TaskOutputTests(unittest.TestCase):
    def task(self, **changes):
        return {'id':'task-producer', 'agentId':'dev', 'status':'done', 'revision':2,
                'acceptanceCriteria':['Publish the actual implementation.'],
                'result':'Actual delivery summary.', 'outputs':['artifact-code'], **changes}

    def call(self, call_id, status, tool='code.test', task_id='task-producer', result=None):
        return {'id':call_id, 'taskId':task_id, 'agentId':'dev', 'tool':tool,
                'status':status, 'result':result, 'durationMs':12}

    def test_raw_output_never_invents_contract_validation_or_semantic_success(self):
        task = self.task()
        result = build_task_output(task, {'calls':[]})
        self.assertEqual(result['raw'], task['result'])
        self.assertEqual(result['expectedOutput'], task['acceptanceCriteria'])
        self.assertEqual(result['artifactIds'], task['outputs'])
        self.assertEqual(result['agentId'], 'dev')
        self.assertEqual(result['outputFormat'], 'raw')
        self.assertIsNone(result['jsonDict'])
        self.assertIsNone(result['outputContract'])
        self.assertIsNone(result['outputValidation'])
        self.assertIsNone(result['reviewValidation'])
        self.assertEqual(result['semanticQuality'], 'unassessed')
        self.assertNotIn('passed', json.dumps(result))

    def test_failed_denied_and_uncertain_tools_stay_distinct_and_task_scoped(self):
        calls = [self.call('first-failure','failed',result={'ok':False,'exitCode':1,'stderr':'Actual assertion failure'}),
                 self.call('permission','denied',tool='git.push',result={'ok':False,'denied':True}),
                 self.call('unknown-effect','unknown',tool='git.commit'),
                 self.call('pending-approval','pending',tool='file.patch',result={'ok':False,'needsApproval':True}),
                 self.call('later-success','success',result={'ok':True,'exitCode':0}),
                 self.call('model-timeout','failed',tool='llm.chat'),
                 self.call('foreign-task','failed',task_id='other-task')]
        result = build_task_output(self.task(), {'calls':calls})
        self.assertEqual([item['callId'] for item in result['toolFailures']], ['first-failure','permission'])
        self.assertEqual([item['type'] for item in result['toolFailures']], ['execution_failure','permission_denied'])
        self.assertEqual([item['callId'] for item in result['toolUncertainties']], ['unknown-effect','pending-approval'])
        self.assertEqual(result['toolFailures'][0]['exitCode'], 1)
        self.assertEqual(result['toolFailuresTotal'], 2)
        self.assertEqual(result['toolUncertaintiesTotal'], 2)
        self.assertTrue(result['evidenceAvailability']['calls'])
        self.assertFalse(result['toolFailuresTruncated'])
        self.assertEqual(result['semanticQuality'], 'unassessed')

    def test_missing_or_malformed_calls_are_not_a_fake_empty_failure_history(self):
        for run in ({}, {'calls':None}, {'calls':'missing'}, {'calls':[None]}):
            with self.subTest(run=run):
                result = build_task_output(self.task(), run)
                self.assertIsNone(result['toolFailures'])
                self.assertIsNone(result['toolFailuresTotal'])
                self.assertIsNone(result['toolUncertainties'])
                self.assertFalse(result['evidenceAvailability']['calls'])
        known = build_task_output(self.task(), {'calls':[]})
        self.assertEqual(known['toolFailures'], [])
        self.assertEqual(known['toolFailuresTotal'], 0)
        self.assertTrue(known['evidenceAvailability']['calls'])
        unknown_task = build_task_output({}, {'calls':[]})
        self.assertIsNone(unknown_task['toolFailures'])

    def test_output_validation_and_independent_review_are_detached_and_rebuilt(self):
        task = self.task(outputContract={'kind':'artifact','extensions':['.py'],'minCount':1},
                         outputValidation={'status':'passed','kind':'artifact','semanticQuality':'unassessed',
                                           'checkedArtifactIds':['artifact-code'],'matchedArtifactIds':['artifact-code']},
                         validation={'verdict':'pass','verified':True,'targetRevision':2,
                                     'artifactHashes':{'artifact-code':'a'*64},'evidenceCallIds':['qa-test'],
                                     'reviewerTaskId':'task-review'})
        first = build_task_output(task, {'calls':[]})
        self.assertEqual(first['outputFormat'], 'artifact')
        self.assertEqual(first['outputValidation'], task['outputValidation'])
        self.assertEqual(first['reviewValidation'], task['validation'])
        task['validation']['verdict'] = 'rework'
        task['validation']['verified'] = False
        task['revision'] = 3
        task['outputValidation'] = None
        second = build_task_output(task, {'calls':[]})
        self.assertEqual(first['reviewValidation']['verdict'], 'pass')
        self.assertEqual(second['reviewValidation']['verdict'], 'rework')
        self.assertEqual(second['revision'], 3)
        self.assertIsNone(second['outputValidation'])
        self.assertEqual(second['outputFormat'], 'raw')

    def test_structured_json_is_actual_parsed_data_only_after_recorded_format_validation(self):
        task = self.task(result='{"answer":42,"sources":[]}', outputContract={'kind':'json','requiredKeys':['answer']},
                         outputValidation={'status':'passed','kind':'json','semanticQuality':'unassessed'})
        parsed = build_task_output(task, {'calls':[]})
        self.assertEqual(parsed['outputFormat'], 'json')
        self.assertEqual(parsed['jsonDict'], {'answer':42,'sources':[]})
        empty = build_task_output(dict(task,result='{}'), {'calls':[]})
        self.assertEqual(empty['jsonDict'], {})  # An empty object must not disappear by truthiness.
        undeclared = build_task_output(self.task(result=task['result']), {'calls':[]})
        self.assertIsNone(undeclared['jsonDict'])
        for text in ('I made the JSON report.', '{"x":1,"x":2}', '[1,2]', '{"x":NaN}', '{"x":1e999}', '{"x":"\\ud800"}'):
            with self.subTest(text=text):
                result = build_task_output(dict(task,result=text), {'calls':[]})
                self.assertIsNone(result['jsonDict'])
                self.assertEqual(result['outputFormat'], 'raw')
                self.assertIn('structuredIssue', result)
                json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')

    def test_prompt_projection_deduplicates_raw_and_keeps_bounded_failure_evidence(self):
        failures = [self.call(f'failed-{index}','failed',result={'ok':False,'error':{'code':'test','message':'x'*2000}})
                    for index in range(20)]
        task = self.task(result='actual ' * 5000, outputs=[f'artifact-{i}' for i in range(45)],
                         acceptanceCriteria=['criterion '*200]*25,
                         outputValidation={'status':'passed','kind':'artifact','semanticQuality':'unassessed',
                                           'checkedArtifactIds':[f'artifact-{i}' for i in range(20)]},
                         validation={'verdict':'pass','verified':True,'targetRevision':2,
                                     'artifactHashes':{f'artifact-{i}':'a'*64 for i in range(12)},
                                     'evidenceCallIds':[f'check-{i}' for i in range(12)]})
        original_task, original_run = copy.deepcopy(task), copy.deepcopy({'calls':failures})
        output = build_task_output(task, original_run)
        before_projection = copy.deepcopy(output)
        prompt = task_output_for_context(output)
        self.assertEqual(task, original_task)
        self.assertEqual(original_run, {'calls':failures})
        self.assertEqual(output, before_projection)
        self.assertNotIn('raw', prompt)
        self.assertTrue(prompt['rawInDependencyResult'])
        self.assertTrue(output['rawTruncated'])
        self.assertLessEqual(len(output['raw']), 18000)
        self.assertEqual(len(output['toolFailures']), 16)
        self.assertEqual(len(prompt['toolFailures']), 4)
        self.assertEqual(prompt['toolFailuresTotal'], 20)
        self.assertTrue(prompt['toolFailuresTruncated'])
        self.assertLessEqual(len(prompt['toolFailures'][0]['error']['message']), 320)
        self.assertTrue(prompt['toolFailures'][0]['error']['messageTruncated'])
        self.assertEqual(prompt['artifactIdsTotal'], 45)
        self.assertEqual(len(prompt['artifactIds']), 8)
        self.assertTrue(prompt['artifactIdsTruncated'])
        self.assertEqual(prompt['outputValidation']['status'], 'passed')
        self.assertEqual(prompt['outputValidation']['semanticQuality'], 'unassessed')
        self.assertEqual(prompt['reviewValidation']['targetRevision'], 2)
        self.assertEqual(prompt['reviewValidation']['artifactHashesTotal'], 12)
        self.assertTrue(prompt['reviewValidation']['artifactHashesTruncated'])
        self.assertEqual(task_output_for_context(prompt), prompt)

    def test_large_json_is_explicitly_omitted_from_prompt_not_changed_into_false_values(self):
        task = self.task(result=json.dumps({'payload':'x'*5000}), outputContract={'kind':'json'},
                         outputValidation={'status':'passed','kind':'json','semanticQuality':'unassessed'})
        output = build_task_output(task, {'calls':[]})
        prompt = task_output_for_context(output)
        self.assertEqual(output['jsonDict']['payload'], 'x'*5000)
        self.assertIsNone(prompt['jsonDict'])
        self.assertTrue(prompt['jsonDictOmitted'])
        self.assertTrue(prompt['jsonDictInDependencyResult'])
        self.assertEqual(prompt['outputFormat'], 'json')

    def test_structured_output_round_trips_actual_sqlite_state_without_losing_null_evidence(self):
        output = build_task_output(self.task(), {})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            repo = Repository(path)
            repo.save_run({'id':'run-handoff','taskOutput':output}, 'group')
            repo.close()
            repo = Repository(path)
            restored = repo.get_run('run-handoff')['taskOutput']
            repo.close()
        self.assertEqual(restored, output)
        self.assertIsNone(restored['toolFailures'])
        self.assertIsNone(restored['reviewValidation'])


if __name__ == '__main__':
    unittest.main()
