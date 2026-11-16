"""Observed progress and failure-aware final summaries; no external services."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.runtime import compress_messages
from backend.tools import ToolGateway


class LocalFiles(ToolGateway):
    def capabilities(self):return {}


class Scripted:
    def __init__(self,tasks,actions,finish_on_progress=False):
        self.tasks=tasks;self.actions=actions;self.messages=[];self.counts={}
        self.finish_on_progress=finish_on_progress

    def complete(self,messages,max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            result={'tasks':self.tasks}
        else:
            context=json.loads(messages[1]['content']);title=context['task']['title']
            self.messages.append((title,copy.deepcopy(messages)))
            count=self.counts.get(title,0);self.counts[title]=count+1
            actions=self.actions[title]
            if self.finish_on_progress and context.get('progressReceipt',{}).get('closingRequired'):
                result={'type':'final','text':'Draft remains private; requested complete delivery is blocked.','status':'partial'}
            else:result=actions[min(count,len(actions)-1)]
        return {'content':json.dumps(result),'finish_reason':'stop','usage':{'total_tokens':5}}


def tool(name,**args):return {'type':'tool','name':name,'args':args}
def final(text='Recorded actual work.',status='done'):return {'type':'final','text':text,'status':status}


class EngineDeliveryProgressTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.apps=[]
    def tearDown(self):
        for app in self.apps:
            app.shutdown();app.wait_for_idle(3);app.repo.close()
        self.temp.cleanup()

    def run_model(self,model,participants=None,**settings):
        config=Config(Path(self.temp.name)/str(len(self.apps)),{'LLM_API_KEY':'progress-fixture','MAX_RETRIES':'0',
                      'MAX_AGENT_TURNS':'12','MAX_MODEL_CALLS':'40','MAX_RUN_TOKENS':'400000',**settings})
        app=Platform(config,provider=model,tools=LocalFiles(config));self.apps.append(app)
        app.action({'action':'send','mode':'task','participants':participants or ['coordinator'],
                    'text':'Deliver a usable result and honestly describe what remains blocked.'})
        self.assertTrue(app.wait_for_idle(4))
        return app,app.snapshot()['sessions']['group']['run']

    def test_alternating_reads_receive_closing_feedback_without_auto_publication_or_budget_change(self):
        actions=[tool('file.write',path='draft.md',content='Actual draft.\n'),
                 tool('file.read',path='draft.md'),tool('file.list'),
                 tool('file.read',path='draft.md'),tool('file.list'),final()]
        model=Scripted([{'id':'a','title':'Draft','agentId':'coordinator'}],{'Draft':actions},finish_on_progress=True)
        app,run=self.run_model(model)
        task=next(t for t in run['steps'] if t.get('kind')!='planning')
        self.assertEqual(run['status'],'partial')
        self.assertEqual(task['turns'],6)
        self.assertEqual(task['progress']['turnsWithoutNewOutputOrTest'],3)
        self.assertEqual(task['progress']['completion'],'not_assessed')
        self.assertEqual(task['outputs'],[])
        self.assertEqual(run['artifacts'],[],'progress feedback must not auto-publish a private draft')
        self.assertEqual(run['budgetExtensions'],0)
        self.assertEqual(run['budget']['modelCalls'],40)
        self.assertEqual(run['modelCalls'],7)
        self.assertEqual([c['tool'] for c in run['calls'] if c['tool']!='llm.chat'],['file.write','file.read','file.list','file.read','file.list'])
        self.assertTrue(any('blocked/unread source is not evidence' in m['content'] for m in model.messages[-1][1]))
        self.assertTrue((app.tools.workspace('coordinator',run['id'])/'draft.md').is_file())

    def test_new_file_hash_resets_stagnation_and_old_feedback_is_not_left_as_current_advice(self):
        actions=[tool('file.write',path='a.md',content='First module.\n'),tool('file.read',path='a.md'),
                 tool('file.list'),tool('file.read',path='a.md'),tool('file.list'),
                 tool('file.write',path='b.md',content='Second actual module.\n'),tool('file.read',path='b.md'),final()]
        model=Scripted([{'id':'a','title':'Draft','agentId':'coordinator'}],{'Draft':actions})
        _,run=self.run_model(model)
        self.assertEqual(run['status'],'done')
        self.assertTrue(any(m['content'].startswith('执行进度收据：') for m in model.messages[5][1]))
        refreshed=json.loads(model.messages[6][1][1]['content'])['progressReceipt']
        self.assertFalse(refreshed['closingRequired'])
        self.assertEqual(refreshed['turnsWithoutNewOutputOrTest'],0)
        self.assertEqual(sum(m['content'].startswith('执行进度收据：') for m in model.messages[6][1]),0)
        self.assertTrue(model.messages[6][1][-1]['content'].startswith('动作的真实结果：'))
        task=next(t for t in run['steps'] if t.get('kind')!='planning')
        self.assertEqual(len(task['progress']['privateFiles']),2)
        self.assertEqual(task['progress']['turnsWithoutNewOutputOrTest'],0)

    def test_low_remaining_turns_have_explicit_truthful_delivery_hint(self):
        actions=[tool('file.write',path='a.md',content='A\n'),tool('file.write',path='b.md',content='B\n'),
                 tool('file.write',path='c.md',content='C\n'),final()]
        model=Scripted([{'id':'a','title':'Draft','agentId':'coordinator'}],{'Draft':actions})
        _,run=self.run_model(model,MAX_AGENT_TURNS='6')
        message=next(m['content'] for m in model.messages[2][1] if m['content'].startswith('执行进度收据：'))
        receipt=json.loads(message.split('：',1)[1])
        self.assertEqual(receipt['remainingAfterThisTurn'],3)
        self.assertEqual(json.loads(model.messages[2][1][1]['content'])['progressReceipt']['turnLimit'],6)
        self.assertIn('honest final status',receipt['instruction'])
        self.assertLess(len(message),800,'closing reminder must not duplicate the whole canonical ledger')
        self.assertEqual(run['budgetExtensions'],0)

    def test_every_turn_retains_actual_module_paths_and_hashes_without_copying_source_or_urging_early_final(self):
        sources={'data.js':'export const rooms = [1,2];\n','logic.js':'import {rooms} from "./data.js";\n',
                 'styles.css':'.room { color: blue; }\n','index.html':'<main id="rooms"></main>\n'}
        actions=[tool('file.write',path=path,content=source) for path,source in sources.items()]
        actions += [tool('file.read',path='data.js'),final()]
        model=Scripted([{'id':'a','title':'Modules','agentId':'coordinator'}],{'Modules':actions})
        _,run=self.run_model(model)
        self.assertEqual(run['status'],'done')
        import hashlib
        for turn,(_,messages) in enumerate(model.messages):
            context=json.loads(messages[1]['content']);receipt=context['progressReceipt']
            expected={path:hashlib.sha256(source.encode()).hexdigest() for path,source in list(sources.items())[:min(turn,4)]}
            self.assertEqual({item['path']:item['sha256'] for item in receipt['privateFiles']},expected)
            self.assertEqual(receipt['turn'],turn+1)
            self.assertFalse(receipt['closingRequired'])
            self.assertIn('Reuse the existing files',receipt['instruction'])
            self.assertIn('file.read',receipt['instruction'])
            self.assertNotIn('export const rooms',json.dumps(receipt))
            self.assertEqual(sum(m['content'].startswith('执行进度收据：') for m in messages),0)
            if turn:
                self.assertTrue(messages[-1]['content'].startswith('动作的真实结果：'))
                actual=json.loads(messages[-1]['content'][len('动作的真实结果：'):])
                self.assertEqual(actual['path'],list(sources)[turn-1] if turn<5 else 'data.js')
        messages=copy.deepcopy(model.messages[4][1]);messages += [{'role':'assistant','content':'old prose '*5000}]
        compressed=compress_messages(messages,18000)
        projected=json.loads(compressed[1]['content'])['progressReceipt']
        original=json.loads(messages[1]['content'])['progressReceipt']
        for field in ('privateFiles','publishedOutputs','turn','turnLimit','remainingAfterThisTurn'):
            self.assertEqual(projected[field],original[field])
        self.assertIn('fileReadReferences',projected)
        self.assertEqual(json.loads(model.messages[4][1][1]['content'])['progressReceipt'],original,
                         'model projection must not rewrite the original observed/audit receipt')

    def test_actual_browser_string_error_none_and_structured_errors_are_safe_progress_data(self):
        errors=['locator.click: strict mode violation: selector resolved to 12 elements',None,
                {'code':'transient','message':'A structured execution failure.','retryable':True}]
        for value in errors:
            with self.subTest(error=value):
                model=Scripted([{'id':'a','title':'Browser check','agentId':'coordinator'}],{'Browser check':[final()]})
                app,_=self.run_model(model)
                with app.lock:
                    run=app.state['sessions']['group']['run']
                    task=next(t for t in run['steps'] if not t.get('_planning'))
                    failed={'id':'actual-browser-failure','taskId':task['id'],'tool':'code.test','status':'failed',
                            'args':{'language':'browser','path':'index.html'},
                            'result':{'ok':False,'exitCode':1,'runtime':'browser','error':value}}
                    run['calls'].append(failed)
                    original=copy.deepcopy(failed)
                    app._progress_receipt(run,task)
                    receipt=task['progress']['recentToolFailures'][-1]
                    self.assertEqual(receipt['callId'],failed['id'])
                    self.assertEqual(receipt['target'],'index.html')
                    self.assertEqual(failed,original,'progress reporting must not rewrite a failed execution into success')
                    if isinstance(value,str):self.assertEqual(receipt['message'],value)
                    elif isinstance(value,dict):
                        self.assertEqual(receipt['code'],'transient');self.assertTrue(receipt['retryable'])
                        self.assertEqual(receipt['message'],value['message'])
                    else:self.assertIsNone(receipt['code']);self.assertFalse(receipt['retryable'])
                    self.assertEqual(sum(m['content'].startswith('执行进度收据：') for m in task['_messages']),0)

    def test_read_index_counts_exact_repeated_reads_without_copying_or_inventing_cache(self):
        actions=[tool('file.write',path='a.js',content='const apiA = 1;\n'),tool('file.write',path='b.js',content='const apiB = 2;\n'),
                 tool('file.read',path='a.js'),tool('file.read',path='b.js'),tool('file.read',path='a.js'),tool('file.read',path='b.js'),final()]
        model=Scripted([{'id':'a','title':'Read modules','agentId':'coordinator'}],{'Read modules':actions})
        app,run=self.run_model(model)
        self.assertEqual(run['status'],'done')
        first=json.loads(model.messages[3][1][1]['content'])['progressReceipt']
        self.assertEqual(first['fileReadCount'],1)
        self.assertEqual(first['fileReads'][0]['path'],'a.js')
        self.assertTrue(first['fileReads'][0]['bodyPresentWhenIndexed'])
        self.assertEqual(first['fileReads'][0]['requestedRange'],{})
        latest=json.loads(model.messages[-1][1][1]['content'])['progressReceipt']
        self.assertEqual(latest['fileReadCount'],4);self.assertEqual(latest['repeatedReadCount'],2)
        self.assertEqual({r['path']:r['readCount'] for r in latest['fileReads']},{'a.js':2,'b.js':2})
        self.assertNotIn('const apiA',json.dumps(latest))
        # A durable call receipt alone is not cached source in the model view.
        with app.lock:
            live=app.state['sessions']['group']['run'];task=next(t for t in live['steps'] if not t.get('_planning'))
            task['_messages']=[m for m in task['_messages'] if not m['content'].startswith('动作的真实结果：')]
            app._progress_receipt(live,task)
            self.assertTrue(all(not item['bodyPresentWhenIndexed'] for item in task['progress']['fileReads']))

    def test_read_index_keeps_requested_windows_distinct_and_is_bounded(self):
        model=Scripted([{'id':'a','title':'Read index','agentId':'coordinator'}],{'Read index':[final()]})
        app,_=self.run_model(model)
        with app.lock:
            live=app.state['sessions']['group']['run'];task=next(t for t in live['steps'] if not t.get('_planning'))
            for number in range(10):
                live['calls'].append({'id':'window-'+str(number),'taskId':task['id'],'tool':'file.read','status':'success',
                    'args':{'path':'app.js','start_line':number*10+1,'max_lines':10},
                    'result':{'ok':True,'path':'app.js','sha256':'a'*64,'content':'Actual returned window.','start_line':number*10+1,'end_line':number*10+10}})
            app._progress_receipt(live,task)
            receipt=task['progress']
            self.assertEqual(receipt['fileReadCount'],10);self.assertEqual(receipt['repeatedReadCount'],0)
            self.assertEqual(len(receipt['fileReads']),8);self.assertTrue(receipt['fileReadIndexTruncated'])
            self.assertEqual(receipt['fileReads'][-1]['requestedRange'],{'start_line':91,'max_lines':10})
            self.assertEqual(receipt['fileReads'][-1]['returnedRange'],{'start_line':91,'end_line':100})
            self.assertTrue(all(not item['bodyPresentWhenIndexed'] for item in receipt['fileReads']))

    def test_first_four_distinct_module_reads_are_progress_but_three_duplicate_windows_trigger_closing(self):
        paths=['app.js','store.js','index.html','styles.css']
        actions=[tool('file.write',path=path,content='Actual module '+path+'\n') for path in paths]
        actions += [tool('file.read',path=path) for path in paths]
        actions += [tool('file.read',path='app.js'),tool('file.read',path='store.js'),tool('file.read',path='app.js'),final()]
        model=Scripted([{'id':'a','title':'Inspect modules','agentId':'coordinator'}],{'Inspect modules':actions})
        _,run=self.run_model(model,MAX_AGENT_TURNS='16')
        self.assertEqual(run['status'],'done')
        for index in range(4,9):
            receipt=json.loads(model.messages[index][1][1]['content'])['progressReceipt']
            self.assertFalse(receipt['closingRequired'])
            self.assertEqual(receipt['turnsWithoutNewOutputOrTest'],0)
        for index,stagnant in ((9,1),(10,2),(11,3)):
            receipt=json.loads(model.messages[index][1][1]['content'])['progressReceipt']
            self.assertEqual(receipt['turnsWithoutNewOutputOrTest'],stagnant)
            self.assertEqual(receipt['closingRequired'],stagnant==3)
            self.assertEqual(receipt['observedReadWindowCount'],4)
        self.assertEqual(run['budgetExtensions'],0)

    def test_empty_find_results_are_not_new_read_progress(self):
        model=Scripted([{'id':'a','title':'Empty find','agentId':'coordinator'}],{'Empty find':[final()]})
        app,_=self.run_model(model)
        with app.lock:
            run=app.state['sessions']['group']['run'];task=next(t for t in run['steps'] if not t.get('_planning'))
            for number in range(3):
                run['calls'].append({'id':'empty-find-'+str(number),'taskId':task['id'],'tool':'file.read','status':'success',
                    'args':{'path':'app.js','find':'missing-'+str(number)},
                    'result':{'ok':True,'mode':'find','path':'app.js','sha256':'a'*64,'matches':[],'matchCount':0}})
                app._progress_receipt(run,task)
            self.assertEqual(task['progress']['observedReadWindowCount'],0)
            self.assertEqual(task['progress']['turnsWithoutNewOutputOrTest'],3)
            self.assertTrue(json.loads(task['_messages'][1]['content'])['progressReceipt']['closingRequired'])

    def test_final_summary_runs_after_terminal_failed_dependencies_but_production_consumers_stay_skipped(self):
        for outcome in ('partial','failed'):
            with self.subTest(outcome=outcome):
                tasks=[{'id':'research','title':'Source','agentId':'research'},
                       {'id':'build','title':'Build','agentId':'executor','dependsOn':['research']},
                       {'id':'consume','title':'Production consumer','agentId':'coordinator','dependsOn':['build']},
                       {'id':'qa','title':'QA','agentId':'reviewer','dependsOn':['build']},
                       {'id':'summary','title':'Synthesis','agentId':'coordinator','dependsOn':['research','build','consume','qa']}]
                actions={'Source':[final()], 'Build':[final('Blocked product work.',outcome)],
                         'Production consumer':[final('Must not run.')], 'QA':[final('Must not claim passed.')],
                         'Synthesis':[final('Source is complete; product, QA and packaging remain blocked.') ]}
                model=Scripted(tasks,actions);_,run=self.run_model(model,['coordinator','research','executor','reviewer'])
                by_title={t['title']:t for t in run['steps']}
                self.assertEqual(run['status'],'partial')
                self.assertEqual(by_title['Synthesis']['status'],'done')
                self.assertTrue(by_title['Synthesis']['finalSummary'])
                self.assertEqual(by_title['Production consumer']['status'],'skipped')
                self.assertEqual(by_title['QA']['status'],'skipped')
                self.assertNotIn('QA',model.counts);self.assertNotIn('Production consumer',model.counts)
                summary_messages=next(messages for title,messages in model.messages if title=='Synthesis')
                dependencies=json.loads(summary_messages[1]['content'])['dependencyResults']
                self.assertEqual(next(t for t in dependencies if t['title']=='Build')['status'],outcome)
                self.assertEqual(next(t for t in dependencies if t['title']=='QA')['status'],'skipped')


if __name__=='__main__':unittest.main()
