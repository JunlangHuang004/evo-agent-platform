"""Browser model projection only: full calls remain the acceptance/audit source."""
import copy
import hashlib
import json
import unittest

from backend.tool_context import compact_browser_result


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def browser_result(click_failure=False):
    files = [{'path': name, 'sha256': letter * 64} for name, letter in
             [('app/index.html', 'a'), ('app/styles.css', 'b'), ('app/app.js', 'c')]]
    assertion = {'action': 'assert_count', 'selector': 'button.slot.free', 'expected': 120,
                 'actual': 30, 'passed': False, 'pageId': 'main'}
    failure = {'action': 'click', 'selector': 'button.slot[data-room="r1"][data-slot="0"]',
               'pageId': 'main', 'ok': False,
               'error': 'locator.click: Timeout 2500ms exceeded; element is disabled.\n' + 'repeated call log\n' * 60}
    browser = {'engine': 'chromium', 'executed': True, 'ok': False,
        'entry': copy.deepcopy(files[0]), 'testedFiles': files, 'sourceManifest': copy.deepcopy(files),
        'assertions': [] if click_failure else [assertion], 'assertionCount': 0 if click_failure else 1,
        'assertionsPassed': 0, 'steps': [failure] if click_failure else [{**assertion, 'ok': False, 'error': 'Assertion failed'}],
        'console': [{'text': 'large log ' * 500}], 'pageErrors': [], 'httpErrors': [], 'blockedRequests': [],
        'reportPath': 'browser-evidence/actual-case/report.json',
        'screenshot': {'path': 'browser-evidence/actual-case/screenshot.png', 'sha256': 'd' * 64, 'bytes': 9000}}
    return {'ok': False, 'exitCode': 1, 'runtime': 'browser', 'test': True, 'timedOut': False,
            'error': failure['error'] if click_failure else 'Browser step failed: Assertion failed',
            'stdout': json.dumps(browser), 'stderr': '', 'browser': browser}


class ToolContextTests(unittest.TestCase):
    def test_success_projection_points_to_existing_full_receipt_without_accepting_it(self):
        source = browser_result(); source.update(ok=True, callId='actual-call', exitCode=0)
        source['browser'].update(ok=True)
        before = copy.deepcopy(source)
        projection = compact_browser_result(source)
        self.assertIn('review.evidenceCallIds', projection['receiptAccess'])
        self.assertIn('actual-call', projection['callId'])
        self.assertTrue(projection['fullResultRequiredForAcceptance'])
        self.assertFalse(projection['evidenceComplete'])
        self.assertEqual(source, before)

    def test_real_failure_values_source_binding_and_provenance_survive_without_mutating_audit(self):
        original = browser_result(); before = copy.deepcopy(original)
        projected = compact_browser_result(original)
        self.assertEqual(original, before)
        self.assertLessEqual(len(json.dumps(projected, ensure_ascii=False)), 2600)
        self.assertEqual(projected['sourceResultSHA256'], canonical_hash(original))
        self.assertEqual(projected['sourceBrowserSHA256'], canonical_hash(original['browser']))
        failed = projected['browser']['failedAssertions'][0]
        self.assertEqual((failed['expected'], failed['actual'], failed['selector']), (120, 30, 'button.slot.free'))
        self.assertTrue(failed['alsoFailedStep'])
        self.assertEqual(projected['browser']['entry'], original['browser']['entry'])
        self.assertEqual(projected['browser']['testedFiles'], original['browser']['testedFiles'])
        self.assertEqual(projected['browser']['reportPath'], original['browser']['reportPath'])
        self.assertTrue(projected['sourceBindingsComplete']); self.assertTrue(projected['readonlyBrowserSource'])
        self.assertTrue(projected['fullResultRequiredForAcceptance']); self.assertFalse(projected['evidenceComplete'])
        self.assertNotIn('stdout', projected); self.assertNotIn('steps', projected['browser'])
        self.assertEqual(projected['omissions']['console'], 1)
        self.assertNotEqual(projected['sourceBrowserSHA256'], original['browser']['screenshot']['sha256'])
        self.assertIn('not-report-file-bytes', projected['hashEncoding'])

    def test_failed_click_keeps_exact_selector_and_explicit_original_error_preview(self):
        source = browser_result(click_failure=True); projected = compact_browser_result(source)
        failed = projected['browser']['failedStep']
        self.assertEqual(failed['action'], 'click')
        self.assertEqual(failed['selector'], source['browser']['steps'][0]['selector'])
        self.assertIsInstance(failed['error'], dict)
        self.assertTrue(failed['error']['truncated']); self.assertIn('disabled', failed['error']['preview'])
        self.assertEqual(failed['error']['sha256'], hashlib.sha256(source['browser']['steps'][0]['error'].encode()).hexdigest())
        self.assertFalse(projected['ok']); self.assertEqual(projected['exitCode'], 1)
        self.assertLessEqual(len(json.dumps(projected, ensure_ascii=False)), 2600)

    def test_repeated_projection_does_not_rehash_the_view_or_reset_original_omissions(self):
        original = browser_result(click_failure=True); projection = compact_browser_result(original)
        digest = projection['sourceResultSHA256']; totals = copy.deepcopy(projection['sourceTotals'])
        for _ in range(3):
            again = compact_browser_result(projection)
            self.assertEqual(again, projection)
            self.assertEqual(again['sourceResultSHA256'], digest)
            self.assertEqual(again['sourceTotals'], totals)
            projection = again

    def test_unknown_unexecuted_or_missing_binding_does_not_gain_readonly_exemption(self):
        for case in ('not_executed', 'missing', 'unknown', 'cancelled', 'no_manifest', 'unbound_entry', 'unbound_asset'):
            source = browser_result()
            if case == 'not_executed': source['browser']['executed'] = False
            elif case == 'missing': source['browser'].pop('executed')
            elif case == 'unknown': source['outcomeUnknown'] = True
            elif case == 'cancelled': source['cancelled'] = True
            elif case == 'no_manifest': source['browser'].pop('sourceManifest')
            elif case == 'unbound_entry': source['browser']['entry']['sha256'] = 'e' * 64
            elif case == 'unbound_asset': source['browser']['testedFiles'][-1]['sha256'] = 'e' * 64
            projected = compact_browser_result(source)
            with self.subTest(case=case):
                self.assertFalse(projected['readonlyBrowserSource'])
                self.assertEqual(projected['browser'].get('executed'), source['browser'].get('executed'))
                self.assertFalse(projected['ok'])

    def test_large_source_indexes_are_omitted_whole_and_never_claim_complete_evidence(self):
        source = browser_result()
        source['browser']['testedFiles'] += [{'path': 'assets/' + ('long-name-' * 18) + str(i) + '.js',
            'sha256': 'e' * 64} for i in range(35)]
        source['browser']['sourceManifest'] = copy.deepcopy(source['browser']['testedFiles'])
        projected = compact_browser_result(source, max_chars=1800)
        self.assertLessEqual(len(json.dumps(projected, ensure_ascii=False)), 1800)
        self.assertFalse(projected['sourceBindingsComplete'])
        self.assertGreater(projected['omissions']['testedFiles'], 0)
        self.assertFalse(projected['evidenceComplete'])
        original_rows = source['browser']['testedFiles']
        self.assertTrue(all(row in original_rows for row in projected['browser'].get('testedFiles', [])))
        self.assertEqual(projected['sourceResultSHA256'], canonical_hash(source))

    def test_passing_details_are_counted_as_omitted_not_an_invented_complete_test(self):
        source = browser_result(); source['ok'] = True; source['exitCode'] = 0
        source['browser'].update(ok=True, assertions=[{'action': 'assert_count', 'expected': 1, 'actual': 1, 'passed': True}] * 20,
                                 assertionCount=20, assertionsPassed=20, steps=[])
        source.pop('error')
        projected = compact_browser_result(source)
        self.assertTrue(projected['ok']); self.assertEqual(projected['browser']['assertionCount'], 20)
        self.assertEqual(projected['omissions']['assertions'], 20)
        self.assertEqual(projected['browser']['failedAssertions'], [])
        self.assertFalse(projected['evidenceComplete']); self.assertTrue(projected['fullResultRequiredForAcceptance'])

    def test_nonbrowser_malformed_and_nonfinite_results_fail_closed(self):
        for source in (None, {'runtime': 'python', 'ok': True}, {'runtime': 'browser', 'x': float('nan')},
                       {'runtime': 'browser', 'modelViewOnly': 'browser-tool-context-v1', 'sourceResultSHA256': 'a' * 64}):
            self.assertIsNone(compact_browser_result(source))
        source = {'runtime': 'browser', 'browser': None, 'ok': False}
        projected = compact_browser_result(source)
        self.assertIsNone(projected['sourceTotals']['assertions'])
        self.assertIsNone(projected['omissions']['assertions'])
        self.assertNotIn('executed', projected['browser']); self.assertFalse(projected['readonlyBrowserSource'])
        for limit in (True, 100, 'large'):
            with self.assertRaises(ValueError): compact_browser_result(source, max_chars=limit)

    def test_read_fault_coverage_survives_projection_without_claiming_full_evidence(self):
        source = browser_result()
        source['browser'].update(readFaultCoverageComplete=False, readFaults=[
            {'id': 'read-fault-1', 'blockedReads': 0, 'blockedHarnessReads': 1}])
        before = copy.deepcopy(source)
        projected = compact_browser_result(source)
        self.assertFalse(projected['browser']['readFaultCoverageComplete'])
        self.assertEqual(projected['sourceTotals']['readFaults'], 1)
        self.assertEqual(projected['omissions']['readFaults'], 1)
        self.assertFalse(projected['evidenceComplete'])
        self.assertEqual(compact_browser_result(projected), projected)
        self.assertEqual(source, before)
        self.assertLessEqual(len(json.dumps(projected, ensure_ascii=False)), 2600)


if __name__ == '__main__':
    unittest.main()
