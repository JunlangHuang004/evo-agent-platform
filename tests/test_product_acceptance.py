"""Acceptance-contract regression fixtures only; these never count as real Mid-Test product scores."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.evaluation import RunEvidence, evaluate_case
from backend.product_acceptance import ProductVerifier, RUBRICS, evidence_file, usage_metrics, allocation_issues, platform_qa_evidence
from benchmarks.mid_test_acceptance import assess_exports, build_suite, applicable_requirements


class ProductAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.content = '<h1>UNIT TEST fixture, not a real product</h1>'
        self.digest = hashlib.sha256(self.content.encode()).hexdigest()
        self.artifact = {'id': 'artifact-unit', 'runId': 'run-unit', 'taskId': 'dev-task', 'ownerAgentId': 'dev',
                         'name': 'index.html', 'path': 'app/index.html', 'content': self.content,
                         'sha256': self.digest, 'size': len(self.content.encode())}
        self.task = {'taskId': 'dev-task', 'revision': 0, 'status': 'done', 'closed': True,
                     'ownerAgentId': 'dev', 'artifactIds': ['artifact-unit']}
        self.run = {'runId': 'run-unit', 'caseId': 'software-1', 'source': 'unit_fixture', 'status': 'done',
                    'startedAtMs': 100, 'finishedAtMs': 200, 'tasks': [self.task], 'exportSha256': 'e' * 64,
                    'roleBindings': {'reviewer': 'qa'}, 'rawRun': {'id': 'run-unit', 'status': 'done',
                     'steps': [{'id': 'dev-task', 'agentId': 'dev', 'revision': 0, 'outputs': ['artifact-unit']}],
                     'artifacts': [self.artifact], 'calls': [], 'deliveries': []}}
        self.artifacts = {'artifact-unit': self.artifact}
        (self.root / 'audit.json').write_text('{"scope":"UNIT TEST ONLY, not execution evidence"}')
        self.audit = {'path': 'audit.json', 'sha256': hashlib.sha256((self.root / 'audit.json').read_bytes()).hexdigest()}
        self.case = {'caseId': 'software-1', 'verifier': 'mid-test-product'}

    def tearDown(self): self.temp.cleanup()
    def evidence(self): return RunEvidence(self.run, self.artifacts.get)
    def binding(self): return {'taskId': 'dev-task', 'revision': 0, 'artifactId': 'artifact-unit', 'sha256': self.digest}
    def proof(self, check_id='S1.booking'):
        rule = next(item for item in RUBRICS[self.run['caseId']] if item['id'] == check_id)
        row = {'method': rule['method'], 'bindings': [self.binding()], 'evidenceFiles': [self.audit],
               'observations': {name: {'verdict': 'pass', 'detail': 'Explicit unit-fixture observation, not a real-run claim.'} for name in rule['observations']},
               'browser': {'engine': 'chromium', 'executed': True, 'testedFileHashes': [self.digest],
                           'assertions': [{'name': 'synthetic unit fixture assertion', 'passed': True}], 'pageErrors': []}}
        return {'schemaVersion': 1, 'provenance': 'independent_evaluator', 'assessor': 'UNIT TEST evaluator',
                'caseId': self.run['caseId'], 'runId': 'run-unit', 'exportSha256': 'e' * 64, 'checks': {check_id: row}}
    def assess(self, proof=None): return ProductVerifier(self.case, proof, self.root).evaluate(self.evidence())

    def test_artifact_http_or_keywords_alone_never_prove_business_success(self):
        rows = self.assess()
        self.assertEqual(rows['S1.delivery'].verdict, 'fail')
        self.assertEqual([rows[key].verdict for key in ('S1.booking','S1.cancel','S1.versioned_qa')], ['unassessed'] * 3)
        self.run['caseId'] = self.case['caseId'] = 'newsroom-2'
        self.artifact['content'] = 'detectors reliable false positives uncertainty academic claims ' * 50
        self.assertTrue(all(row.verdict == 'unassessed' for row in self.assess().values()))

    def test_independent_assessment_requires_all_exact_bindings_and_supporting_hashes(self):
        self.assertEqual(self.assess(self.proof())['S1.booking'].verdict, 'pass')
        mutations = [lambda p: p.update(runId='another-run'), lambda p: p.update(exportSha256='x' * 64),
                     lambda p: p['checks']['S1.booking']['bindings'][0].update(revision=1),
                     lambda p: p['checks']['S1.booking']['bindings'][0].update(sha256='0' * 64),
                     lambda p: p['checks']['S1.booking']['evidenceFiles'][0].update(sha256='0' * 64)]
        for mutate in mutations:
            proof = copy.deepcopy(self.proof()); mutate(proof)
            with self.subTest(mutation=mutate): self.assertEqual(self.assess(proof)['S1.booking'].verdict, 'unassessed')

    def test_every_required_observation_is_needed_and_actual_failed_assertions_fail(self):
        proof = self.proof(); del proof['checks']['S1.booking']['observations']['duplicate_slot_rejected']
        self.assertEqual(self.assess(proof)['S1.booking'].verdict, 'unassessed')
        for assertions, expected in [(['not a real assertion'], 'unassessed'), ([{'passed': 'true'}], 'unassessed'),
                                      ([{'name': 'observed assertion', 'passed': False}], 'fail')]:
            proof = self.proof(); proof['checks']['S1.booking']['browser']['assertions'] = assertions
            self.assertEqual(self.assess(proof)['S1.booking'].verdict, expected)

    def test_prototype_can_be_tested_but_cannot_be_delivered_or_use_stale_unknown_calls(self):
        source = self.root / 'snapshot.html'; source.write_text(self.content)
        ref = {'taskId': 'dev-task', 'revision': 0, 'path': 'app/index.html', 'sha256': self.digest,
               'snapshot': {'path': source.name, 'sha256': self.digest}}
        call = {'taskId': 'dev-task', 'agentId': 'dev', 'revision': 0, 'tool': 'file.write',
                'status': 'success', 'args': {'path': 'app/index.html'}, 'result': {'sha256': self.digest}}
        self.run['rawRun']['calls'] = [call]
        proof = self.proof(); proof['checks']['S1.booking']['bindings'] = [ref]
        self.assertEqual(self.assess(proof)['S1.booking'].verdict, 'pass')
        self.assertEqual(self.assess(proof)['S1.delivery'].verdict, 'fail')
        for later in [dict(call, tool='file.delete', result={}), dict(call, taskId='later-task', result={'sha256': 'n' * 64})]:
            self.run['rawRun']['calls'] = [call, later]
            self.assertEqual(self.assess(proof)['S1.booking'].verdict, 'unassessed')
        legacy = dict(call); legacy.pop('revision'); self.run['rawRun']['calls'] = [legacy]
        self.assertEqual(self.assess(proof)['S1.booking'].verdict, 'unassessed')

    def test_operator_pass_does_not_make_platform_qa_agent_task_pass(self):
        self.task['ownerAgentId'] = self.artifact['ownerAgentId'] = 'qa'
        verifier = ProductVerifier(self.case, self.proof(), self.root)
        result = evaluate_case(self.case, self.run, self.artifacts.get, verifier)
        self.assertEqual(result['checks'][1]['verdict'], 'pass')
        self.assertEqual(result['tasks'][0]['verdict'], 'unassessed')
        self.assertTrue(verifier.operator_assisted)
        self.assertEqual(platform_qa_evidence(self.run), [])

    def test_real_immutable_package_check_is_structural_and_not_overall_quality(self):
        self.run['tasks'].append({'taskId': 'release-task', 'revision': 0, 'ownerAgentId': 'release',
                                 'status': 'done', 'closed': True, 'artifactIds': []})
        self.run['rawRun']['steps'].append({'id': 'release-task', 'revision': 0, 'agentId': 'release', 'outputs': []})
        self.run['rawRun']['deliveries'] = [{'id': 'delivery-unit', 'runId': 'run-unit', 'taskId': 'release-task',
            'taskRevision': 0, 'agentId': 'release', 'entrypoint': 'app/index.html', 'instructions': 'Open app/index.html offline.',
            'files': [{'artifactId': 'artifact-unit', 'path': 'app/index.html', 'sha256': self.digest,
                       'size': self.artifact['size'], 'sourceTaskId': 'dev-task', 'sourceRevision': 0}]}]
        result = evaluate_case(self.case, self.run, self.artifacts.get, ProductVerifier(self.case))
        self.assertEqual(result['checks'][0]['verdict'], 'pass')
        self.assertEqual(result['goalVerdict'], 'unassessed')
        self.run['rawRun']['steps'][0]['revision'] = 1
        self.assertEqual(self.assess()['S1.delivery'].verdict, 'fail')

    def test_dashboard_recomputation_excludes_cancelled_and_respects_inclusive_dates(self):
        bookings = [{'id':'a','date':'2026-11-01','roomId':'r1','slot':0,'status':'active'},
                    {'id':'b','date':'2026-11-30','roomId':'r2','slot':'10:00','status':'active'},
                    {'id':'c','date':'2026-11-01','roomId':'r1','slot':0,'status':'cancelled'},
                    {'id':'d','date':'2026-12-01','roomId':'r1','slot':0,'status':'active'}]
        expected = usage_metrics(bookings, '2026-11-01', '2026-11-30', ['r1','r2','r3'])
        self.assertEqual(expected['activeCount'], 2); self.assertEqual(expected['totalHours'], 2)
        self.assertEqual(expected['perRoom'], {'r1':1,'r2':1,'r3':0})
        self.assertEqual(expected['cancelledCount'], 1); self.assertAlmostEqual(expected['cancellationRate'], 1/3)
        self.assertEqual(usage_metrics([], '2026-11-01','2026-11-30')['busiestHours'], [])
        with self.assertRaises(ValueError): usage_metrics(bookings + [bookings[0]], '2026-11-01','2026-11-30')

    def test_allocation_checks_real_totals_percentages_and_currency_not_keywords(self):
        calculation = {'capitalHKD':1000000,'fxHKDPerUSD':8,'allocations':[
            {'weightPct':60,'amountHKD':600000,'amountUSD':75000}, {'weightPct':40,'amountHKD':400000,'amountUSD':50000}]}
        self.assertEqual(allocation_issues(calculation), [])
        for key, value in [('amountHKD',500000), ('weightPct',80), ('amountUSD',1)]:
            changed = copy.deepcopy(calculation); changed['allocations'][0][key] = value
            self.assertTrue(allocation_issues(changed))

    def test_historical_preferences_require_actual_recorded_relay_and_do_not_hide_requirements(self):
        reply = 'A recorded operator relay, retained exactly.'
        requirement = {'caseId':'software-2','receiptRef':'historical-user-answer','answer':'Prior preferences',
            'deliveredToRun':'run-unit','deliveryEvidence':{'approvalId':'approval','receiptId':'receipt',
                'answerSha256':hashlib.sha256(reply.encode()).hexdigest()}}
        case = {'verifiedRequirements':[requirement]}
        self.assertIsNone(applicable_requirements(case, self.run)[0]['deliveredToRun'])
        self.run['rawRun']['approvals'] = [{'type':'clarification','id':'approval','clarificationReceipt':
            {'id':'receipt','runId':'run-unit','answer':reply}}]
        self.assertEqual(applicable_requirements(case,self.run)[0]['deliveredToRun'],'run-unit')
        self.run['caseId'] = self.case['caseId'] = 'software-2'
        proof = self.proof('S2.interaction')
        self.assertEqual(self.assess(proof)['S2.interaction'].verdict, 'pass')
        self.case['verifiedRequirements'] = [{'deliveredToRun':'run-unit'}]
        self.assertEqual(self.assess(proof)['S2.interaction'].verdict, 'unassessed', 'relayed extras need their actual observations; unrelayed extras do not fail original-scope checks')

    def test_external_evidence_cannot_escape_or_use_agent_workspace_or_symlink(self):
        (self.root/'link.json').symlink_to(self.root/'audit.json')
        for ref in [dict(self.audit,path='../audit.json'), dict(self.audit,path='link.json'),
                    dict(self.audit,path='workspaces/agent/report.json')]:
            with self.assertRaises(Exception): evidence_file(self.root, ref)

    def test_unrelated_published_file_cannot_make_a_prototype_or_nonhtml_binding_deliverable(self):
        source = self.root/'prototype.txt'; source.write_text(self.content)
        self.run['rawRun']['calls'] = [{'taskId':'dev-task','agentId':'dev','revision':0,'tool':'file.write',
            'status':'success','args':{'path':'prototype.txt'},'result':{'sha256':self.digest}}]
        self.run['caseId'] = self.case['caseId'] = 'fund-2'
        proof = self.proof('F2.slides')
        proof['checks']['F2.slides']['bindings'] = [{'taskId':'dev-task','revision':0,'path':'prototype.txt',
            'sha256':self.digest,'snapshot':{'path':'prototype.txt','sha256':self.digest}}]
        self.assertEqual(self.assess(proof)['F2.slides'].verdict, 'unassessed')
        self.run['caseId'] = self.case['caseId'] = 'fund-2'
        note = copy.deepcopy(self.artifact); note.update(id='notes',name='notes.txt',path='notes.txt')
        self.artifacts['notes'] = note; self.task['artifactIds'].append('notes')
        proof = self.proof('F2.slides'); proof['checks']['F2.slides']['bindings'][0]['artifactId']='notes'
        self.assertEqual(self.assess(proof)['F2.slides'].verdict, 'unassessed')

    def test_manuscript_can_bind_real_task_result_without_requiring_a_download(self):
        self.run['caseId'] = self.case['caseId'] = 'newsroom-1'
        self.task['result'] = 'UNIT TEST frozen article body, independently assessed in this fixture only.'
        self.task['artifactIds'] = []; self.run['rawRun']['artifacts'] = []
        proof = self.proof('N1.story')
        proof['checks']['N1.story']['bindings'] = [{'kind':'task_result','taskId':'dev-task','revision':0,
            'sha256':hashlib.sha256(self.task['result'].encode()).hexdigest()}]
        self.assertEqual(self.assess(proof)['N1.story'].verdict, 'pass')
        self.assertEqual(self.assess()['N1.story'].verdict, 'unassessed', 'frozen prose alone is not an independent factual/quality verdict')
        proof['checks']['N1.story']['bindings'][0]['sha256'] = '0' * 64
        self.assertEqual(self.assess(proof)['N1.story'].verdict, 'unassessed')


class ProductCliTests(unittest.TestCase):
    def test_six_case_manifest_and_missing_exports_keep_all_denominators_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); pack = root/'pack'; pack.mkdir()
            for prefix in ['scenario_1_software_engineering','scenario_2_newsroom','scenario_3_securities_fund']:
                folder = pack/prefix; folder.mkdir()
                roles='\n'.join('| `role'+str(i)+'` | fixture |\n```text\nRole fixture '+str(i)+'\n```' for i in range(4))
                (folder/'01_role_initialization.md').write_text(roles)
                for name in ['02_task_1.md','03_task_2.md']:
                    (folder/name).write_text('# Synthetic UNIT TEST input\n```text\nFixture prompt, never a real-run success.\n```')
            answers = root/'answers.json'; answers.write_text(json.dumps({'answers':[{'caseId':'software-1','receiptRef':'unit-fixture-receipt','answer':'Synthetic test answer'}]}))
            suite = build_suite(pack, answers)
            self.assertEqual(len(suite['cases']), 6); self.assertEqual(sum(len(c['checks']) for c in suite['cases']), 24)
            self.assertEqual(len(suite['packFilesSha256']), 9); self.assertFalse(suite['officialScore'])
            self.assertEqual(suite['cases'][0]['verifiedRequirements'][0]['receiptRef'], 'unit-fixture-receipt')
            report = assess_exports(suite, root/'no-exports')
            self.assertEqual(report['summary']['overallGoals']['total'], 6)
            self.assertEqual(report['summary']['fixedChecks']['total'], 24)
            self.assertTrue(all(row['independentProductVerdict'] == 'unassessed' and row['runId'] is None and row['elapsedMs'] is None for row in report['cases']))
            self.assertEqual(report['summary']['overallGoals']['passed'], 0)
            exports = root/'exports'; exports.mkdir()
            raw = {'id':'run-unit-export','teamId':'software','status':'done','mode':'task','title':'A different request',
                   'startedAt':100,'finishedAt':200,'steps':[],'artifacts':[],'calls':[],'events':[],'approvals':[]}
            (exports/'software-1.json').write_text(json.dumps(raw))
            rejected = assess_exports(suite, exports)['cases'][0]
            self.assertEqual(rejected['independentProductVerdict'], 'unassessed')
            self.assertIn('prompt', rejected['errorReason'])
            first = suite['cases'][0]
            (exports/'software-1.json').write_text(json.dumps({'case':'software-1','prompt':first['prompt'],
                'sourceSha256':first['sourceSha256'],'run':raw,'runId':raw['id']}))
            contradictory = assess_exports(suite, exports)['cases'][0]
            self.assertEqual(contradictory['independentProductVerdict'], 'unassessed')
            self.assertIn('Persisted run title', contradictory['errorReason'])
            raw['title'] = first['prompt']
            (exports/'software-1.json').write_text(json.dumps({'case':'software-1','prompt':first['prompt'],
                'sourceSha256':first['sourceSha256'],'run':raw,'runId':raw['id']}))
            actual = assess_exports(suite, exports)['cases'][0]
            self.assertEqual(actual['source'], 'frozen_export')
            self.assertEqual(actual['executionStatus'], 'done')
            self.assertEqual(actual['independentProductVerdict'], 'fail', 'terminal missing ZIP is a delivery failure, not business success')
            control = {'case':'software-1','task':{'id':'software-1','text':first['prompt'],'sha256':first['sourceSha256']},'run':raw}
            (exports/'software-1.json').write_text(json.dumps(control))
            self.assertEqual(assess_exports(suite, exports)['cases'][0]['source'], 'frozen_export')
            control['prompt'] = 'A contradictory outer wrapper'
            (exports/'software-1.json').write_text(json.dumps(control))
            self.assertEqual(assess_exports(suite, exports)['cases'][0]['independentProductVerdict'], 'unassessed')
            observations = next(rule for rule in RUBRICS['software-1'] if rule['id']=='S1.cancel')['observations']
            self.assertIn('cancellation_state_persists', observations)
            self.assertNotIn('cancelled_history_persists', observations)


if __name__ == '__main__': unittest.main()
