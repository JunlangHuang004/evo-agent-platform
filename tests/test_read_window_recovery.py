"""Real action/result window retention and visibility-gated loop handling, without providers."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.runtime import compress_messages,retained_file_reads,ProtocolError
from tests.test_file_context_cache import tool,full,noise,actual_results


def base():
    return [{'role':'system','content':'S'*14866},{'role':'user','content':json.dumps({
        'task':{'id':'build','goal':'Extend the actual inherited system without losing records.'},
        'runGoal':'Implement, publish and independently check the requested extension.',
        'authorizedArtifacts':[{'id':'app','path':'imports/app/app.js','sha256':'a'*64}],
        'dependencyResults':[],'taskBoard':{'tasks':[]},'progressReceipt':{'turn':12,'turnLimit':24,
        'remainingAfterThisTurn':12,'privateFiles':[],'publishedOutputs':[],
        'instruction':'Redundant instruction '*60,'fileReads':[]}})}]


def window(path,source,start,end):
    lines=source.splitlines(keepends=True)
    return {'ok':True,'path':path,'sha256':hashlib.sha256(source.encode()).hexdigest(),
            'mode':'lines','startLine':start,'endLine':end,'totalLines':len(lines),
            'startColumn':1,'endColumn':len(lines[end-1])+1,'partialFirstLine':False,
            'partialLastLine':False,'truncated':False,'content':''.join(lines[start-1:end])}


class ReadWindowRecoveryTests(unittest.TestCase):
    def test_two_same_file_windows_survive_alternation_and_three_compactions_without_synthesized_full_read(self):
        source=''.join('line '+str(i)+' '+('x'*25)+'\n' for i in range(1,207));path='imports/app/app.js'
        first,last=window(path,source,1,120),window(path,source,120,206)
        messages=base()
        for _ in range(4):
            messages+=tool('file.read',{'path':path,'startLine':1,'endLine':120},first)
            messages+=tool('file.read',{'path':path,'startLine':120,'endLine':206},last)
        for _ in range(3):
            original=copy.deepcopy(messages);compressed=compress_messages(messages+noise(4,5000),40000)
            rows=[r for r in actual_results(compressed) if r.get('path')==path and 'content' in r]
            self.assertEqual({(r['startLine'],r['endLine']) for r in rows},{(1,120),(120,206)})
            self.assertEqual({r['content'] for r in rows},{first['content'],last['content']})
            self.assertTrue(all(r['mode']=='lines' for r in rows));self.assertEqual(messages,original)
            self.assertEqual(len(retained_file_reads(compressed)),2)
            self.assertLessEqual(sum(len(m['content']) for m in compressed),40000)
            messages=compressed

    def test_full_read_covers_same_sha_windows_without_merging_or_fabricating_body(self):
        source='first\nsecond\nthird\n';path='app.js'
        history=base()+tool('file.read',{'path':path,'startLine':1,'endLine':2},window(path,source,1,2))
        history+=tool('file.read',{'path':path},full(path,source))+noise(4,8000)
        rows=[r for r in actual_results(compress_messages(history,23000)) if r.get('path')==path and 'content' in r]
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['content'],source);self.assertEqual(rows[0]['mode'],'full')

    def test_changed_sha_omitted_or_invalid_full_read_invalidates_previous_version(self):
        for latest in ({**full('app.js','new\n'),'contentOmitted':True},
                       {**full('app.js','new\n'),'content':'partial','truncated':True},
                       {**full('app.js','new\n'),'content':'hash mismatch'}):
            latest=dict(latest)
            if latest.get('contentOmitted'):latest.pop('content')
            history=base()+tool('file.read',{'path':'app.js'},full('app.js','old\n'))
            history+=tool('file.read',{'path':'app.js'},latest)+noise(4,8000)
            with self.subTest(latest=latest):
                rows=actual_results(compress_messages(history,23000))
                self.assertFalse(any(r.get('content')=='old\n' for r in rows))
                self.assertFalse(retained_file_reads(compress_messages(history,23000)))

    def test_mutations_invalidate_all_windows_and_failed_patch_preserves_them(self):
        source='first\nsecond\nthird\n';path='app.js'
        start=base()+tool('file.read',{'path':path,'startLine':1,'endLine':2},window(path,source,1,2))
        start+=tool('file.read',{'path':path,'startLine':3,'endLine':3},window(path,source,3,3))
        for name,ok in [('file.write',True),('file.patch',True),('file.delete',True),('file.patch',False)]:
            result={'ok':ok,'path':path}
            if not ok:result['error']={'code':'parameter','message':'Unique patch target not found; nothing was changed.'}
            history=start+tool(name,{'path':path},result)+noise(4,8000)
            rows=retained_file_reads(compress_messages(history,23000))
            self.assertEqual(len(rows),0 if ok else 2)

    def test_final_only_constraint_survives_tool_and_prose_compaction_or_fails_explicitly(self):
        closure={'role':'user','content':'最后收尾约束：Return ONE final action only; never synthesize done.'}
        for tools in ([],tool('file.read',{'path':'app.js'},full('app.js','source\n'))):
            history=base()+tools+noise(4,8000)+[closure]
            compressed=compress_messages(history,23000)
            self.assertIn(closure,compressed);self.assertLessEqual(sum(len(m['content']) for m in compressed),23000)
        with self.assertRaises(ProtocolError):compress_messages(base()+[closure],14870)

    def test_potential_code_mutation_invalidates_private_cache_even_on_failure_but_not_immutable_imports(self):
        initial=base()+tool('file.read',{'path':'app.js'},full('app.js','private\n'))
        initial+=tool('file.read',{'path':'imports/shared/app.js'},full('imports/shared/app.js','immutable\n'))
        for language,ok in [('python',True),('javascript',False)]:
            history=initial+tool('code.test',{'language':language,'code':'potential mutation'},
                                 {'ok':ok,'exitCode':0 if ok else 1})+noise(4,8000)
            paths={r['path'] for r in retained_file_reads(compress_messages(history,23000))}
            self.assertEqual(paths,{'imports/shared/app.js'})
        # A real confined browser result includes the host-bound frozen manifest;
        # executed=True alone cannot establish a read-only source snapshot.
        entry={'path':'app.html','sha256':hashlib.sha256(b'<html></html>').hexdigest()}
        script={'path':'app.js','sha256':hashlib.sha256(b'private\n').hexdigest()}
        browser=initial+tool('code.test',{'language':'browser','path':'app.html'},
                              {'ok':True,'runtime':'browser','browser':{'executed':True,
                               'entry':entry,'testedFiles':[entry,script],'sourceManifest':[entry,script]}})+noise(4,8000)
        self.assertEqual({r['path'] for r in retained_file_reads(compress_messages(browser,23000))},
                         {'app.js','imports/shared/app.js'})

    def test_read_loop_is_rejected_only_with_real_current_body_not_a_historical_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Config(Path(directory),{'LLM_API_KEY':'offline-window','MAX_RETRIES':'0'})
            app=Platform(config)
            try:
                path='app.js';args={'path':path,'startLine':1,'endLine':2};source='one\ntwo\n'
                value=window(path,source,1,2)
                calls=[{'id':'read-'+str(i),'taskId':'build','tool':'file.read','args':dict(args),
                        'status':'success','result':copy.deepcopy(value)} for i in range(4)]
                run={'id':'run-window','calls':calls,'events':[]}
                task={'id':'build','agentId':'executor','_stagnantTurns':3,
                      '_messages':base()+tool('file.read',args,value)}
                before=copy.deepcopy(calls);result=app._read_loop_feedback(run,task,'file.read',args)
                self.assertFalse(result['ok']);self.assertEqual(result['error']['code'],'read_loop')
                self.assertFalse(result['actionExecuted']);self.assertEqual(calls,before)
                self.assertTrue(result['retainedReadWindows'][0]['bodyPresent'])
                task['_messages']=base()
                self.assertIsNone(app._read_loop_feedback(run,task,'file.read',args),
                                  'lost/omitted source must be recoverable despite historical reads')
                task['_messages']=base()+tool('file.read',args,value)
                self.assertIsNone(app._read_loop_feedback(run,task,'file.read',{'path':path,'startLine':3,'endLine':4}))
                task['_messages']=base()+tool('file.read',{'path':path},full(path,source))
                covered=app._read_loop_feedback(run,task,'file.read',args)
                self.assertEqual(covered['error']['code'],'read_loop')
                self.assertEqual(covered['retainedReadWindows'][0]['mode'],'full',
                                 'cite the real full receipt rather than fabricate a partial tool result')
            finally:app.shutdown();app.repo.close()


if __name__=='__main__':unittest.main()
