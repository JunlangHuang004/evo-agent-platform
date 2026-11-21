import copy
import unittest
from benchmarks.package_review import package_review_coverage


class PackageReviewCoverageTests(unittest.TestCase):
    def fixture(self):
        validation={'verdict':'pass','assessed':True,'verified':False,'kind':'editorial_assessment',
                    'targetRevision':1,'reviewerRevision':0,'reviewerTaskId':'qa','artifactHashes':{'a':'hash'},'evidenceCallIds':['read']}
        return {'id':'run','steps':[{'id':'build','agentId':'writer','revision':1,'status':'done','outputs':['a'],'validation':validation},
                                   {'id':'qa','agentId':'checker','revision':0},
                                   {'id':'release','agentId':'writer','revision':0}],
                'artifacts':[{'id':'a','taskId':'build','runId':'run','path':'article.md','sha256':'hash'}],
                'calls':[{'id':'read','taskId':'qa','agentId':'checker','revision':0,'tool':'file.read','status':'success',
                          'result':{'sha256':'hash','truncated':False,'mode':'full'}}],
                'events':[{'type':'review_pass','agentId':'checker','payload':{'taskId':'build','validation':copy.deepcopy(validation)}}],
                'deliveries':[{'id':'package','taskId':'release','taskRevision':0,'status':'packaged',
                               'files':[{'path':'article.md','sha256':'hash'}]}]}

    def test_same_bytes_covered_but_changed_added_and_relocated_package_files_are_exposed(self):
        run=self.fixture()
        self.assertEqual(package_review_coverage(run)['packages'][0]['filesWithoutCurrentRecordedReview'],[])
        run['deliveries'][0]['files']=[{'path':'article.md','sha256':'edited'},
                                      {'path':'README.md','sha256':'new'},
                                      {'path':'relocated/article.md','sha256':'hash'}]
        before=copy.deepcopy(run)
        result=package_review_coverage(run)
        self.assertEqual(len(result['packages'][0]['filesWithoutCurrentRecordedReview']),3)
        self.assertEqual(result['qualityVerdict'],'unassessed')
        self.assertEqual(run,before)

    def test_stale_or_unbacked_or_partial_reads_never_cover_the_final_file(self):
        for mutate in (lambda r:r['steps'][0].update(revision=2),
                       lambda r:r['steps'][1].update(revision=1),
                       lambda r:r['events'].clear(),
                       lambda r:r['calls'][0].update(agentId='writer'),
                       lambda r:r['calls'][0]['result'].update(truncated=True),
                       lambda r:r['calls'][0]['result'].update(mode='find')):
            run=self.fixture();mutate(run)
            self.assertEqual(len(package_review_coverage(run)['packages'][0]['filesWithoutCurrentRecordedReview']),1)

    def test_candidates_are_not_formal_packages(self):
        run=self.fixture();run['deliveries'][0]['status']='candidate'
        self.assertEqual(package_review_coverage(run)['packages'],[])
