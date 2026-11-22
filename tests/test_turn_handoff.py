import unittest
from backend.turn_handoff import window,feedback,publication_state

class TurnHandoffTests(unittest.TestCase):
    def test_text_review_and_readonly_summary_never_require_file_publication(self):
        r={'budget':{'taskTurnPolicy':{'initial':15,'extra':8}}}
        for t in [{'turns':20,'outputContract':{'kind':'text'}},
                  {'turns':20,'reviewOf':['producer'],'outputContract':{'kind':'text'}},
                  {'turns':20,'reviewOf':['producer'],'outputContract':{'kind':'artifact','extensions':['.md']}},
                  {'turns':20,'finalSummary':True,'outputContract':{'kind':'text'}},
                  {'turns':20,'outputContract':{'kind':'delivery'}}, {'turns':20}]:
            self.assertEqual(publication_state(r,t)['next'],'finish_original_contract')
            for action in [{'type':'tool','name':'file.read'},{'type':'review'},{'type':'final'}]:
                self.assertIsNone(feedback(r,t,action))

    def test_write_publish_final_and_later_change_require_new_publication(self):
        r={'calls':[],'artifacts':[]};t={'id':'t','agentId':'research','revision':0,'outputs':[],
                                      'outputContract':{'kind':'artifact','extensions':['.md'],'minCount':1}}
        self.assertEqual(publication_state(r,t)['next'],'write_required_files')
        c={'taskId':'t','agentId':'research','revision':0,'tool':'file.write','status':'success','result':{'ok':True,'path':'brief.md','sha256':'a'}}
        r['calls'].append(c)
        self.assertEqual(publication_state(r,t)['publicationExample'],{'type':'publish_artifact','path':'brief.md'})
        r['artifacts']=[{'id':'a','taskId':'t','path':'brief.md','sha256':'a'}];t['outputs']=['a']
        self.assertEqual(publication_state(r,t)['next'],'final')
        r['calls'].append(dict(c,result={'ok':True,'path':'brief.md','sha256':'b'}))
        self.assertEqual(publication_state(r,t)['next'],'publish_written_files')
        r.update(budget={'taskTurnPolicy':{'initial':15,'extra':8}});t['turns']=20
        self.assertIsNotNone(feedback(r,t,{'type':'tool','name':'file.read'}))
        self.assertIsNone(feedback(r,t,{'type':'publish_artifact','path':'brief.md'}))
    def test_default_off_and_before_boundary_do_not_block(self):
        self.assertIsNone(window({'budget':{}},{}))
        r={'budget':{'taskTurnPolicy':{'initial':15,'extra':8}}}
        self.assertIsNone(feedback(r,{'turns':19},{'type':'tool','name':'web.fetch'}))

    def test_exact_boundary_blocks_only_new_acquisition(self):
        r={'budget':{'taskTurnPolicy':{'initial':15,'extra':8}}};t={'turns':20}
        self.assertEqual(window(r,t)['finalTurn'],23)
        for a in [{'type':'tool','name':'web.search'},{'type':'tool','name':'web.fetch','evidenceGap':'Real missing evidence'},{'type':'delegate'}]:
            result=feedback(r,t,a);self.assertTrue(result['notExecuted']);self.assertFalse(result['ok'])
        for a in [{'type':'tool','name':'file.write'},{'type':'tool','name':'file.read'},{'type':'publish_artifact'},{'type':'package_delivery'},{'type':'review'},{'type':'final'}]:
            self.assertIsNone(feedback(r,t,a))

    def test_new_revision_uses_revision_relative_turns(self):
        r={'budget':{'taskTurnPolicy':{'initial':15,'extra':8}}}
        self.assertEqual(window(r,{'turns':42,'_revisionStartTurn':23})['used'],19)
        self.assertIsNone(feedback(r,{'turns':42,'_revisionStartTurn':23},{'type':'tool','name':'web.fetch'}))
