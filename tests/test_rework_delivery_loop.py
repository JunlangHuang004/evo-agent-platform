"""Bounded producer rework -> fresh authorized imports -> independent QA -> package."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from backend.config import Config
from backend.engine import Platform
from backend.tools import ToolGateway


class LocalBrowser(ToolGateway):
    def capabilities(self):return {}
    def execute(self,agent,run,name,args,approved=False):
        if name!='code.test':return super().execute(agent,run,name,args,approved)
        source=super().execute(agent,run,'file.read',{'path':args['path']})['content']
        passed='Version 2' in source
        entry={'path':args['path'],'sha256':hashlib.sha256(source.encode()).hexdigest()}
        return {'ok':passed,'exitCode':0 if passed else 1,'runtime':'browser','test':True,
                'browser':{'engine':'chromium','executed':True,'entry':entry,'testedFiles':[entry],
                           'assertions':[{'action':'assert_text','passed':passed}],
                           'assertionCount':1,'assertionsPassed':int(passed),'pageErrors':[],'httpErrors':[]}}


class ReworkModel:
    def __init__(self,fail_rework=False,block=None):
        self.counts={};self.messages=[];self.fail_rework=fail_rework;self.block=block
        self.entered=threading.Event()
    def complete(self,messages,max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            action={'tasks':[{'id':'r','title':'Requirements','agentId':'research'},
                {'id':'b','title':'Build','agentId':'executor','dependsOn':['r'],'outputContract':{'kind':'artifact','extensions':['.html']}},
                {'id':'q','title':'QA','agentId':'reviewer','dependsOn':['b']},
                {'id':'p','title':'Package','agentId':'coordinator','dependsOn':['q'],'outputContract':{'kind':'delivery'}}]}
        else:
            context=json.loads(messages[1]['content']);title=context['task']['title'];self.messages.append((title,copy.deepcopy(messages)))
            results=[json.loads(m['content'][len('动作的真实结果：'):]) for m in messages if m['content'].startswith('动作的真实结果：')]
            latest=results[-1] if results else {}
            if title=='Build':
                rework=bool(context['task'].get('feedback'));key=(title,rework);n=self.counts.get(key,0);self.counts[key]=n+1
                if rework and self.block and n==0:self.entered.set();self.block.wait(3)
                version=2 if rework else 1;path='app-v'+str(version)+'.html'
                if rework and self.fail_rework:action={'type':'final','text':'The requested fix could not be delivered.','status':'failed'}
                else:action=[{'type':'tool','name':'file.write','args':{'path':path,'content':'<h1>Version '+str(version)+'</h1>'}},
                             {'type':'publish_artifact','path':path},{'type':'final','text':'Published implementation.'}][n]
            elif title=='QA':
                target=next(d for d in context['dependencyResults'] if d['title']=='Build')
                if 'reworkCompleted' in latest and not latest['ok']:
                    action={'type':'final','status':'partial','text':'Producer rework failed; no acceptance claimed.'}
                elif 'reworkCompleted' in latest:
                    action={'type':'tool','name':'code.test','args':{'language':'browser','path':latest['authorizedArtifacts'][0]['path'],
                            'checks':[{'action':'assert_text','selector':'h1','text':'Version 2'}]}}
                elif 'browser' in latest:
                    action={'type':'review','taskId':target['id'],'verdict':'pass' if latest['ok'] else 'rework',
                            'feedback':'Require Version 2 and verify the actual browser page.', 'evidenceCallIds':[latest['callId']]}
                elif latest.get('validation',{}).get('verdict')=='pass':action={'type':'final','text':'New revision independently verified.'}
                else:action={'type':'tool','name':'code.test','args':{'language':'browser','path':context['authorizedArtifacts'][0]['path'],
                            'checks':[{'action':'assert_text','selector':'h1','text':'Version 2'}]}}
            elif title=='Package':
                if latest.get('delivery'):action={'type':'final','text':'Actual package '+latest['delivery']['id']}
                else:
                    build=next(d for d in context['dependencyResults'] if d['title']=='Build')
                    artifact=next(a for a in context['authorizedArtifacts'] if a['id'] in build['outputs'])
                    action={'type':'package_delivery','artifactIds':build['outputs'],'entrypoint':artifact['sourcePath'],
                            'instructions':'Open app-v2.html in a browser.'}
            else:action={'type':'final','text':'Requirements recorded.'}
        return {'content':json.dumps(action),'finish_reason':'stop','usage':{'total_tokens':5}}


class ReworkDeliveryLoopTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.apps=[];self.release=[]
    def tearDown(self):
        for event in self.release:event.set()
        for app in self.apps:
            app.shutdown();app.wait_for_idle(4);app.repo.close()
        self.temp.cleanup()
    def create(self,model):
        config=Config(Path(self.temp.name),{'LLM_API_KEY':'offline-rework','MAX_RETRIES':'1','MAX_AGENT_TURNS':'8',
                      'MAX_MODEL_CALLS':'40','MAX_RUN_TOKENS':'400000','MAX_CONTEXT_CHARS':'80000'})
        app=Platform(config,provider=model,tools=LocalBrowser(config));self.apps.append(app);return app
    def send(self,app):
        app.action({'action':'send','mode':'task','participants':['coordinator','research','executor','reviewer'],
                    'text':'Deliver an independently browser-tested version two product.'})
    def assert_current_delivery(self,app):
        self.assertTrue(app.wait_for_idle(5));run=app.snapshot()['sessions']['group']['run']
        self.assertEqual(run['status'],'done',run.get('summary'))
        build=next(t for t in run['steps'] if t['title']=='Build');qa=next(t for t in run['steps'] if t['title']=='QA')
        self.assertEqual(build['revision'],1);self.assertTrue(build['validation']['verified'])
        self.assertEqual(build['validation']['targetRevision'],1)
        self.assertEqual(build['progress']['turnLimit'],11,'one fixed eight-turn window after initial three turns')
        self.assertNotIn('_reworkWait',app._task(app.state['sessions']['group']['run'],qa['id']))
        self.assertFalse(app._task(app.state['sessions']['group']['run'],qa['id']).get('_recheckTargets'))
        self.assertEqual(run['deliveries'][0]['entrypoint'],'app-v2.html')
        self.assertEqual([f['sourceRevision'] for f in run['deliveries'][0]['files']],[1])
        self.assertEqual(len([e for e in run['events'] if e['type']=='rework_handoff']),1)
        calls=[c for c in run['calls'] if c['tool']=='code.test']
        self.assertEqual(len(calls),2);self.assertEqual(calls[0]['status'],'failed');self.assertEqual(calls[1]['status'],'success')
        self.assertNotEqual(calls[0]['args']['path'],calls[1]['args']['path'])
        return run
    def test_failed_browser_qa_reworks_producer_then_tests_fresh_imports_and_packages_only_v2(self):
        app=self.create(ReworkModel());self.send(app);self.assert_current_delivery(app)
    def test_failed_producer_rework_is_not_accepted_or_packaged(self):
        app=self.create(ReworkModel(fail_rework=True));self.send(app);self.assertTrue(app.wait_for_idle(5))
        run=app.snapshot()['sessions']['group']['run'];self.assertEqual(run['status'],'partial')
        self.assertFalse(run.get('deliveries'))
        build=next(t for t in run['steps'] if t['title']=='Build');self.assertEqual(build['status'],'failed')
        self.assertFalse(build.get('validation',{}).get('verified'))
        self.assertEqual(len([c for c in run['calls'] if c['tool']=='code.test']),1)
    def test_restart_during_exact_rework_wait_resumes_without_duplicate_producer_write_or_handoff(self):
        release=threading.Event();self.release.append(release)
        model=ReworkModel(block=release);app=self.create(model);self.send(app)
        self.assertTrue(model.entered.wait(4))
        app.shutdown();release.set();self.assertTrue(app.wait_for_idle(4));app.repo.close();self.apps.remove(app)
        restored=self.create(ReworkModel());self.assertEqual(restored.snapshot()['sessions']['group']['run']['status'],'interrupted')
        restored.action({'action':'recover','channelId':'group'})
        run=self.assert_current_delivery(restored)
        writes=[c for c in run['calls'] if c['tool']=='file.write' and c['args']['path']=='app-v2.html']
        self.assertEqual(len(writes),1)


if __name__=='__main__':unittest.main()
