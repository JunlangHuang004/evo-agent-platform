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
 assert(!canRetryTask(run,task,200001)); // partial's old finishedAt cannot freeze deadline.
 assert(!canRetryTask(run,{...task,progress:{turnLimit:24}},100000));
 assert(!canRetryTask(run,{...task,outputs:['published']},100000));
 assert.strictEqual(runProgressText({...run,budget:{tokens:1000}},100000),'0/0 步骤完成 · 0 次调用');
 console.log('8 time-only UI assertions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        completed = subprocess.run([shutil.which('node'), '--experimental-vm-modules', '-e', script,
            str(Path(__file__).resolve().parents[1])], text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('8 time-only UI assertions passed', completed.stdout)
