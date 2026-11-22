"""Run the actual pure workbench formatting/eligibility functions in Node."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node is needed for this UI smoke')
class TimeOnlyUiTests(unittest.TestCase):
    def test_null_limits_are_unlimited_and_retry_obeys_wall_deadline(self):
        script = r'''
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
(async()=>{
 const context=vm.createContext({console,setTimeout,clearTimeout,setInterval,clearInterval,URL,Date});
 const modules=new Map();
 async function load(file){
  if(modules.has(file))return modules.get(file);
  const mod=new vm.SourceTextModule(fs.readFileSync(file,'utf8'),{context,identifier:file});
  modules.set(file,mod);
  await mod.link((name,ref)=>load(path.resolve(path.dirname(ref.identifier),name)));
  return mod;
 }
 const mod=await load(path.join(process.argv[1],'js/workbench.js'));await mod.evaluate();
 const {runProgressText,canRetryTask}=mod.namespace;
 const run={status:'partial',budget:{profile:'time_only',tokens:null,modelCalls:null,agentTurns:null,deadlineAt:200000},finishedAt:100000,modelCalls:4,tokenUsage:128,steps:[],calls:[]};
 const task={status:'failed',turns:24,progress:{turnLimit:null},outputs:[]};
 assert(runProgressText(run,100000).includes('Token 128/不限'));
 assert(runProgressText(run,100000).includes('模型调用 4/不限'));
 assert(runProgressText(run,100000).includes('含等待与暂停'));
 assert(canRetryTask(run,task,100000));
 assert(canRetryTask(run,{...task,status:'partial'},100000));
 assert(!canRetryTask(run,{...task,status:'done'},100000));
 assert(!canRetryTask(run,task,200001)); // partial's old finishedAt cannot freeze deadline.
 assert(!canRetryTask(run,{...task,progress:{turnLimit:24}},100000));
 assert(!canRetryTask(run,{...task,outputs:['published']},100000));
 assert.strictEqual(runProgressText({...run,budget:{tokens:1000}},100000),'0/0 步骤完成 · 0 次调用');
 console.log('10 time-only UI assertions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        completed = subprocess.run([shutil.which('node'), '--experimental-vm-modules', '-e', script,
            str(Path(__file__).resolve().parents[1])], text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('10 time-only UI assertions passed', completed.stdout)

    def test_subtask_clock_ticks_while_live_and_freezes_when_finished(self):
        script = r'''
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
(async()=>{
 const context=vm.createContext({console,setTimeout,clearTimeout,setInterval,clearInterval,URL,Date});
 const modules=new Map();
 async function load(file){
  if(modules.has(file))return modules.get(file);
  const mod=new vm.SourceTextModule(fs.readFileSync(file,'utf8'),{context,identifier:file});
  modules.set(file,mod);
  await mod.link((name,ref)=>load(path.resolve(path.dirname(ref.identifier),name)));
  return mod;
 }
 const mod=await load(path.join(process.argv[1],'js/workbench.js'));await mod.evaluate();
 const {taskTiming}=mod.namespace;
 const run={events:[{time:'2026-11-22T10:00:00.000Z',payload:{taskId:'write'}}],calls:[{taskId:'plan',requestStartedAt:Date.parse('2026-11-22T10:00:00.000Z'),durationMs:78171}]};
 const pending={id:'wait',status:'pending'};
 const live={id:'write',status:'running'};
 const done={id:'write',status:'done',startedAt:Date.parse('2026-11-22T10:00:00.000Z'),finishedAt:Date.parse('2026-11-22T10:00:12.500Z')};
 assert.strictEqual(taskTiming(run,pending,Date.parse('2026-11-22T10:00:05.000Z')).text,'未开始');
 assert.strictEqual(taskTiming(run,live,Date.parse('2026-11-22T10:00:05.000Z')).text,'5.0 秒');
 assert.strictEqual(taskTiming(run,live,Date.parse('2026-11-22T10:00:05.000Z')).live,true);
 assert.strictEqual(taskTiming(run,done,Date.parse('2026-11-22T10:05:00.000Z')).text,'12.5 秒');
 assert.strictEqual(taskTiming(run,done,Date.parse('2026-11-22T10:05:00.000Z')).live,false);
 assert.strictEqual(taskTiming(run,{id:'plan',status:'done'},Date.parse('2026-11-22T10:05:00.000Z')).text,'1 分 18 秒');
 const held={events:[],calls:[],approvals:[{taskId:'write',status:'pending',createdAt:Date.parse('2026-11-22T10:00:06.000Z')}]};
 const ask={id:'write',status:'waiting_human',startedAt:Date.parse('2026-11-22T10:00:00.000Z')};
 const asked=taskTiming(held,ask,Date.parse('2026-11-22T10:00:10.000Z'));
 assert.strictEqual(asked.text,'6.0 秒');
 assert.strictEqual(asked.live,false);
 const resumed={events:[],calls:[],approvals:[{taskId:'write',status:'resolved',createdAt:Date.parse('2026-11-22T10:00:06.000Z'),resolvedAt:Date.parse('2026-11-22T10:00:10.000Z')}]};
 const after=taskTiming(resumed,{id:'write',status:'running',startedAt:Date.parse('2026-11-22T10:00:00.000Z')},Date.parse('2026-11-22T10:00:15.000Z'));
 assert.strictEqual(after.text,'11.0 秒');
 assert.strictEqual(after.live,true);
 console.log('9 subtask clock assertions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        completed = subprocess.run([shutil.which('node'), '--experimental-vm-modules', '-e', script,
            str(Path(__file__).resolve().parents[1])], text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('9 subtask clock assertions passed', completed.stdout)
