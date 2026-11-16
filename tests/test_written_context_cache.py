"""Preserve actual successful source writes, never invented read receipts."""
import copy
import hashlib
import json
import unittest

from backend.runtime import _working_tool_results, compress_messages, retained_file_reads, retained_written_files
from tests.test_file_context_cache import actual_results, full, noise, padded, tool
from tests.test_read_window_recovery import window


def base():
    return [{'role':'system','content':'Fixed protocol '+('S'*13000)},
            {'role':'user','content':json.dumps({'runGoal':'Build and actually test the product.',
             'task':{'id':'build','goal':'Preserve existing current sources.'},
             'authorizedArtifacts':[],'ownArtifacts':[],'dependencyResults':[],
             'progressReceipt':{'turn':12,'privateFiles':[], 'publishedOutputs':[]}})}]


def write(path, content, *, ok=True, digest=None):
    digest=digest or hashlib.sha256(content.encode()).hexdigest()
    return tool('file.write',{'path':path,'content':content},
                {'ok':ok,'path':path,'sha256':digest,'size':len(content.encode())})


def written(messages):
    _,cached=_working_tool_results(messages)
    return {record['action']['args']['path']:record for record in cached.values() if record.get('writtenBody')}


class WrittenContextCacheTests(unittest.TestCase):
    def test_visible_full_write_covers_same_sha_windows_without_second_body_cache(self):
        path='app.js';source=''.join('line '+str(i)+' '+('x'*35)+'\n' for i in range(1,181))
        history=base()+write(path,source)
        for start,end in ((1,70),(65,130),(125,180)):
            history+=tool('file.read',{'path':path,'startLine':start,'endLine':end},window(path,source,start,end))
        _,cache=_working_tool_results(history)
        self.assertEqual(len(cache),1)
        self.assertTrue(next(iter(cache.values()))['writtenBody'])
        self.assertEqual(retained_file_reads(history),[])
        self.assertEqual(retained_written_files(history)[0]['sourceAction'],'file.write')
        for _ in range(3):
            history=compress_messages(history+noise(4,10000),30000)
            self.assertEqual(written(history)[path]['action']['args']['content'],source)
            self.assertEqual(len(_working_tool_results(history)[1]),1)
            self.assertEqual(len(retained_written_files(history)),1)
            self.assertFalse(retained_file_reads(history))

    def test_changed_partial_read_invalidates_full_write_and_unknown_write_never_supplies_source(self):
        path='app.js';old='one\ntwo\n';new='changed\ntwo\n'
        history=base()+write(path,old)+tool('file.read',{'path':path,'startLine':1,'endLine':1},window(path,new,1,1))
        self.assertFalse(retained_written_files(history))
        self.assertEqual(retained_file_reads(history)[0]['sha256'],hashlib.sha256(new.encode()).hexdigest())
        for flag in ('outcomeUnknown','deadlineExceeded'):
            pair=write(path,new)
            result=json.loads(pair[1]['content'].split('：',1)[1]);result[flag]=True
            pair[1]['content']='动作的真实结果：'+json.dumps(result)
            with self.subTest(flag=flag):
                self.assertFalse(retained_written_files(base()+write(path,old)+pair))

    def test_three_current_written_modules_survive_repeated_compression_as_original_writes(self):
        source={path:padded(path,size) for path,size in [('app/index.html',1700),('app/styles.css',3600),('app/app.js',8800)]}
        history=base();pairs={}
        for path,body in source.items():
            pairs[path]=write(path,body);history+=pairs[path]
        for _ in range(3):
            before=copy.deepcopy(history)
            compact=compress_messages(history+noise(4,10000),40000)
            self.assertEqual(history,before)
            self.assertLessEqual(sum(len(m['content']) for m in compact),40000)
            cached=written(compact)
            self.assertEqual(set(cached),set(source))
            for path,record in cached.items():
                self.assertEqual(record['action']['args']['content'],source[path])
                self.assertIn(pairs[path][0],compact)
                self.assertIn(pairs[path][1],compact)
            self.assertEqual(retained_file_reads(compact),[], 'writes must not be forged into file.read evidence')
            history=compact

    def test_latest_successful_version_replaces_older_source_and_read_body(self):
        history=base()+write('app.js','OLD')+tool('file.read',{'path':'app.js'},full('app.js','OLD'))
        history+=write('app.js','NEW')+noise(4,10000)
        compact=compress_messages(history,18000)
        self.assertEqual(written(compact)['app.js']['action']['args']['content'],'NEW')
        self.assertFalse(retained_file_reads(compact))
        self.assertFalse(any(row.get('content')=='OLD' for row in actual_results(compact)))

    def test_patch_delete_and_possible_code_mutation_invalidate_written_source(self):
        for name,args,result in [('file.patch',{'path':'app.js'},{'ok':True,'path':'app.js','sha256':'a'*64}),
                                 ('file.delete',{'path':'app.js'},{'ok':True,'path':'app.js'}),
                                 ('code.run',{'code':'changes the workspace'},{'ok':False})]:
            history=base()+write('app.js','SOURCE')+tool(name,args,result)+noise(4,10000)
            with self.subTest(tool=name):
                self.assertEqual(written(compress_messages(history,18000)),{})

    def test_failed_or_mismatched_write_cannot_be_current_source_cache(self):
        for pair in (write('app.js','PROPOSED',ok=False),write('app.js','HASH_MISMATCH',digest='a'*64)):
            history=base()+pair+tool('file.list',{}, {'ok':True,'files':[]})+noise(4,10000)
            self.assertEqual(written(compress_messages(history,18000)),{})

    def test_reading_new_version_invalidates_write_and_full_same_version_supersedes_without_duplication(self):
        for content in ('WRITTEN','CHANGED'):
            history=base()+write('app.js','WRITTEN')+tool('file.read',{'path':'app.js'},full('app.js',content))+noise(4,10000)
            compact=compress_messages(history,18000)
            self.assertEqual(written(compact),{})
            self.assertEqual(len(retained_file_reads(compact)),1)
            self.assertEqual(retained_file_reads(compact)[0]['sha256'],hashlib.sha256(content.encode()).hexdigest())

    def test_unknown_mutation_cannot_leave_old_source_current_but_explicit_rejection_can(self):
        for result in ({'ok':False,'error':{'code':'uncertain'}}, {'ok':False,'reconciled':True},
                       {'ok':False,'outcomeUnknown':True}, {'ok':False}, None):
            history=base()+write('app.js','OLD')+tool('file.write',{'path':'app.js','content':'MAYBE'},result)+noise(4,10000)
            with self.subTest(result=result):
                self.assertEqual(written(compress_messages(history,18000)),{})
        history=base()+write('app.js','OLD')+tool('file.patch',{'path':'app.js','old':'not found','new':'unused'},
                      {'ok':False,'error':{'code':'parameter','message':'No unique match; not executed'}})+noise(4,10000)
        self.assertEqual(written(compress_messages(history,18000))['app.js']['action']['args']['content'],'OLD')

    def test_capacity_omits_body_explicitly_instead_of_fabricating_partial_source(self):
        source=padded('oversized source',40000)
        compact=compress_messages(base()+write('big.js',source)+noise(4,10000),18000)
        marker=next(row for row in actual_results(compact) if row.get('writeBodyOmitted'))
        self.assertEqual(marker['path'],'big.js')
        self.assertEqual(marker['sha256'],hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(marker['sourceAction'],'file.write')
        self.assertNotIn('content',marker)
        self.assertIn('Read only the needed',marker['rangeReadHint'])
        self.assertFalse(written(compact));self.assertFalse(retained_file_reads(compact))
        for _ in range(3):
            compact=compress_messages(compact+tool('file.list',{}, {'ok':True,'files':['big.js']})+noise(4,10000),18000)
            self.assertIn(marker,actual_results(compact))
            self.assertFalse(written(compact));self.assertFalse(retained_file_reads(compact))
        history=base()+write('big.js','OLD')+[{'role':'user','content':'动作的真实结果：'+json.dumps(marker)}]
        self.assertFalse(written(history), 'A newer omitted write version invalidates an older visible body')


if __name__=='__main__':unittest.main()
