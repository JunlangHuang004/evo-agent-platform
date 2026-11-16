"""Producer QA coverage is bound to all delivered web assets, including CSS."""
import copy
import unittest

from tests import test_browser_progress_coverage as coverage


class ReviewBundleBindingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=coverage.BrowserProgressCoverageTests()
        self.fixture.setUp()
        self.app=self.fixture.app;self.run=self.fixture.run
        self.target=self.fixture.target;self.reviewer=self.fixture.task
        self.run['artifacts'].append({'id':'style','runId':self.run['id'],'taskId':self.target['id'],
            'ownerAgentId':'executor','name':'styles.css','path':'styles.css','sha256':'c'*64})
        self.target['outputs'].append('style')

    def tearDown(self):
        self.fixture.tearDown()

    def evidence(self,css_sha='c'*64):
        call=self.fixture.call()
        # QA may reconstruct an authorized bundle in its own workspace. The
        # host-observed bytes must still equal the producer's published bytes.
        css={'path':'qa/styles.css','sha256':css_sha}
        call['args']['resources']['styles.css']=css['path']
        for field in ('sourceManifest','testedFiles'):
            call['result']['browser'][field]=call['result']['browser'][field][:2]+[copy.deepcopy(css)]
        self.run['calls']=[call]
        self.target['validation']={'verdict':'pass','verified':True,'reviewerTaskId':self.reviewer['id'],
            'evidenceCallIds':[call['id']],**self.app._review_snapshot(self.run,self.target)}
        return call

    def test_qa_owned_modified_css_cannot_validate_or_count_as_producer_coverage(self):
        call=self.evidence(css_sha='d'*64)
        self.assertFalse(self.app._browser_review_evidence(self.run,self.target,call))
        self.assertFalse(self.app._valid_review(self.run,self.target))
        self.assertEqual(self.app._browser_progress_coverage(self.run,self.reviewer),(set(),{}))
        self.assertEqual(self.target['status'],'done','evidence rejection does not rewrite completed work')

    def test_byte_identical_authorized_css_copy_remains_usable_evidence(self):
        call=self.evidence()
        self.assertTrue(self.app._browser_review_evidence(self.run,self.target,call))
        self.assertTrue(self.app._valid_review(self.run,self.target))
        progress,summary=self.app._browser_progress_coverage(self.run,self.reviewer)
        self.assertEqual(len(progress),2)
        self.assertEqual(summary['successfulBrowserCallIds'],[call['id']])
        self.assertEqual(summary['semanticQuality'],'unassessed')

    def test_merely_declared_unserved_current_css_is_not_product_coverage(self):
        call=self.evidence()
        call['result']['browser']['testedFiles']=call['result']['browser']['testedFiles'][:2]
        self.assertFalse(self.app._browser_review_evidence(self.run,self.target,call))
        self.assertFalse(self.app._valid_review(self.run,self.target))
        self.assertEqual(self.app._browser_progress_coverage(self.run,self.reviewer),(set(),{}))


if __name__=='__main__':unittest.main()
