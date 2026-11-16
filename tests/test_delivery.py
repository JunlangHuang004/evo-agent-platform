"""Real immutable-file packaging, authorization and download checks; no model API."""
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

from backend.artifacts import ArtifactStore
from backend.config import Config
from backend.contracts import normalize_output_contract, validate_task_output
from backend.delivery import create_delivery, delivery_zip, validate_delivery, DeliveryError, _path
from backend.engine import Platform
from backend.sandbox import SandboxManager
from backend.server import make_handler


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=Config(Path(self.temp.name),{'LLM_API_KEY':'fixture-only','MAX_MODEL_CALLS':'15','MAX_RUN_TOKENS':'200000'})
        self.store=ArtifactStore(self.config.data_dir)
        self.sandbox=SandboxManager(self.config)
        self.dev={'id':'task-dev','agentId':'executor','revision':0,'outputs':[]}
        self.release={'id':'task-release','agentId':'coordinator','revision':0,'outputs':[], 'outputContract':{'kind':'delivery'}}
        self.run={'id':'run-product','steps':[self.dev,self.release],'artifacts':[]}

    def tearDown(self):self.temp.cleanup()

    def publish(self,path,content,owner='executor'):
        workspace=self.sandbox.workspace(owner,self.run['id']);target=workspace/path
        target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content)
        artifact=self.store.register(owner,self.run['id'],path,self.dev['id'])
        self.dev['outputs'].append(artifact['id']);self.run['artifacts'].append(artifact)
        return artifact

    def package(self,artifacts):
        for a in artifacts:self.store.grant(a['id'],'coordinator',self.run['id'],'orchestrator:task-release')
        delivery=create_delivery(self.run,self.release,self.store,[a['id'] for a in artifacts],artifacts[0]['path'],'Open app/index.html in a browser. No server or network is required.')
        self.run.setdefault('deliveries',[]).append(delivery)
        return delivery

    def test_authorization_immutable_bytes_layout_and_explicit_contract(self):
        html=self.publish('app/index.html','<html><script src="app.js"></script></html>')
        js=self.publish('app/app.js','window.productLoaded = true;')
        with self.assertRaises(DeliveryError):
            create_delivery(self.run,self.release,self.store,[html['id'],js['id']],html['path'],'Open the HTML.')
        self.assertEqual(validate_task_output(self.release,self.run,self.store,'Files written')['status'],'failed')
        delivery=self.package([html,js])
        (self.sandbox.workspace('executor',self.run['id'])/'app/app.js').write_text('LATER UNPUBLISHED CONTENT')
        with zipfile.ZipFile(io.BytesIO(delivery_zip(delivery,self.run,self.store))) as z:
            self.assertEqual(set(z.namelist()),{'app/index.html','app/app.js','RUN.txt','DELIVERY.json'})
            self.assertEqual(z.read('app/app.js').decode(),js['content'])
            self.assertEqual(json.loads(z.read('DELIVERY.json'))['semanticQuality'],'unassessed')
        result=validate_task_output(self.release,self.run,self.store,'Download the packaged files.')
        self.assertEqual(result['status'],'passed');self.assertEqual(result['deliveryId'],delivery['id'])
        self.assertEqual(result['semanticQuality'],'unassessed')

    def test_stale_source_or_packager_revision_cannot_satisfy_current_delivery(self):
        html=self.publish('app/index.html','<html>v1</html>');delivery=self.package([html])
        replacement=self.publish('app/index.html','<html>v2</html>')
        with self.assertRaises(DeliveryError):validate_delivery(delivery,self.run,self.store)
        with self.assertRaises(DeliveryError):self.package([html])
        # Original archive remains an honest historical v1 snapshot.
        with zipfile.ZipFile(io.BytesIO(delivery_zip(delivery,self.run,self.store))) as z:
            self.assertIn('v1',z.read('app/index.html').decode())
        fresh=self.package([replacement]);self.release['revision']=1
        with self.assertRaises(DeliveryError):validate_delivery(fresh,self.run,self.store)

    def test_invalid_utf8_instructions_never_become_downloadable(self):
        html=self.publish('index.html','<html>valid</html>')
        self.store.grant(html['id'],self.release['agentId'],self.run['id'],'orchestrator:task-release')
        for instructions in ('bad'+chr(0xd800),'x'*6001,'bad'+chr(0)):
            with self.subTest(instructions=repr(instructions)[:30]),self.assertRaises(DeliveryError):
                create_delivery(self.run,self.release,self.store,[html['id']],html['path'],instructions)
        delivery=self.package([html]);delivery['instructions']='bad'+chr(0xd800)
        self.assertEqual(validate_task_output(self.release,self.run,self.store,'Ready')['status'],'failed')
        with self.assertRaises(DeliveryError):delivery_zip(delivery,self.run,self.store)

    def test_new_task_same_owner_path_and_unicode_collisions(self):
        first=self.publish('app/index.html','<html>old</html>')
        delivery=self.package([first])
        new_task={**self.dev,'id':'task-new','outputs':[]}
        self.run['steps'].append(new_task)
        self.dev=new_task
        self.publish('app/index.html','<html>new</html>')
        with self.assertRaises(DeliveryError):validate_delivery(delivery,self.run,self.store)
        with self.assertRaises(DeliveryError):self.package([first])
        composed=self.publish('app/café.txt','first')
        decomposed=self.publish('app/cafe\u0301.txt','second')
        with self.assertRaises(DeliveryError):self.package([composed,decomposed])

    def test_real_inheritance_cannot_package_superseded_entrypoint_with_new_notes(self):
        original=self.publish('app/index.html','<html>Inherited version 1</html>')
        self.store.grant(original['id'],'coordinator',self.run['id'],'orchestrator:old-release')
        self.run.update(teamId='default',channelId='group',mode='task',status='done',
                        participants=['executor','coordinator'])
        previous=self.run
        self.dev={'id':'task-new-dev','agentId':'executor','revision':0,'outputs':[]}
        self.release={'id':'task-new-release','agentId':'coordinator','revision':0,'outputs':[],
                      'outputContract':{'kind':'delivery'}}
        self.run={'id':'run-continued','teamId':'default','channelId':'group','mode':'task','status':'running',
                  'participants':['executor','coordinator'],'previousRunId':previous['id'],
                  'steps':[self.dev,self.release],'artifacts':[]}
        carried=self.store.carry_forward(previous,self.run,self.run['participants'])
        self.run['artifacts'].extend(carried['artifacts']);inherited=carried['artifacts'][0]
        notes=self.publish('README.md','Actual new release notes.')
        old_package=self.package([inherited,notes])
        self.assertEqual(validate_task_output(self.release,self.run,self.store,'Ready')['status'],'passed')
        replacement=self.publish('app/index.html','<html>New version 2</html>')
        with self.assertRaisesRegex(DeliveryError,'latest published revision'):
            self.package([inherited,notes])
        with self.assertRaisesRegex(DeliveryError,'latest published revision'):
            validate_delivery(old_package,self.run,self.store)
        self.assertEqual(validate_task_output(self.release,self.run,self.store,'Ready')['status'],'failed')
        # Historical downloads remain exact; only current acceptance is invalid.
        with zipfile.ZipFile(io.BytesIO(delivery_zip(old_package,self.run,self.store))) as archive:
            self.assertEqual(archive.read('app/index.html').decode(),original['content'])
        fresh=self.package([replacement,notes])
        self.assertEqual(validate_task_output(self.release,self.run,self.store,'Ready')['status'],'passed')
        self.assertEqual(fresh['entrypoint'],'app/index.html')
        self.assertEqual(fresh['semanticQuality'],'unassessed')

    def test_reject_collisions_wrong_run_missing_entry_and_manifest_tamper(self):
        a=self.publish('app/index.html','<html>A</html>');b=self.publish('app/INDEX.html','<html>B</html>')
        with self.assertRaises(DeliveryError):self.package([a,b])
        delivery=self.package([b]);bad=copy.deepcopy(delivery);bad['files'][0]['sha256']='0'*64
        with self.assertRaises(DeliveryError):delivery_zip(bad,self.run,self.store)
        with self.assertRaises(DeliveryError):validate_delivery(delivery,{**self.run,'id':'run-other'},self.store)
        with self.assertRaises(DeliveryError):create_delivery(self.run,self.release,self.store,[a['id']],'missing.html','Open it.')
        for path in ('../x.html','/x.html','a//x.html','a/./x.html','C:/x.html','a\\b.html','CON.txt','RUN.txt','delivery.JSON','run.txt/file','DELIVERY.json/file'):
            with self.subTest(path=path),self.assertRaises(DeliveryError):_path(path)
        self.assertEqual(normalize_output_contract({'kind':'delivery'}),{'kind':'delivery'})


class ProductModel:
    def __init__(self,broken=False):self.app=None;self.turn=0;self.broken=broken;self.receipts=[]
    def complete(self,messages,max_tokens=None):
        if messages[0]['content'].startswith('Plan an executable task DAG'):
            action={'tasks':[{'id':'build','title':'Deliver a small browser product','agentId':'executor','outputContract':{'kind':'delivery'}}]}
        else:
            self.turn+=1
            if self.turn==1:action={'type':'tool','name':'file.write','args':{'path':'app/index.html','content':'<html><body>Real product file</body></html>'}}
            elif self.turn==2:action={'type':'publish_artifacts','paths':['app/index.html']+(['missing.js'] if self.broken else [])}
            elif self.turn==3:
                actual=next(m['content'] for m in reversed(messages) if m['content'].startswith('动作的真实结果：'))
                self.receipts.append(json.loads(actual.split('：',1)[1]))
                if self.broken:action={'type':'final','status':'partial','text':'Only one file was published; missing.js failed. No full package.'}
                else:
                    run=self.app.state['sessions']['group']['run']
                    action={'type':'package_delivery','artifactIds':[a['id'] for a in run['artifacts']], 'entrypoint':'app/index.html','instructions':'Open app/index.html in your browser.'}
            else:action={'type':'final','status':'done','text':'The real download package is ready; semantic QA is still unassessed.'}
        return {'content':json.dumps(action),'finish_reason':'stop','usage':{'total_tokens':10}}


class DeliveryIntegrationTests(unittest.TestCase):
    def test_engine_package_download_and_cross_team_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Config(Path(tmp),{'LLM_API_KEY':'fixture-only','MAX_RUN_TOKENS':'200000','MAX_MODEL_CALLS':'12'})
            model=ProductModel();app=Platform(config,provider=model);model.app=app
            server=None
            try:
                app.action({'action':'send','channelId':'group','mode':'task','participants':['executor'],'text':'Make a downloadable tiny browser product.'})
                self.assertTrue(app.wait_for_idle(5))
                run=app.snapshot()['sessions']['group']['run'];self.assertEqual(run['status'],'done')
                self.assertEqual(len(run['deliveries']),1)
                self.assertEqual(model.receipts[0]['artifacts'][0]['contentOmitted'],True)
                server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
                threading.Thread(target=server.serve_forever,daemon=True).start()
                route=f"/api/runs/{run['id']}/deliveries/{run['deliveries'][0]['id']}"
                url=f'http://127.0.0.1:{server.server_port}'+route
                with urllib.request.urlopen(url) as response:
                    self.assertEqual(response.headers['Content-Type'],'application/zip')
                    self.assertIn('attachment',response.headers['Content-Disposition'])
                    with zipfile.ZipFile(io.BytesIO(response.read())) as z:self.assertIn('app/index.html',z.namelist())
                with self.assertRaises(urllib.error.HTTPError) as failure:urllib.request.urlopen(url+'?team=software')
                self.assertEqual(failure.exception.code,404)
            finally:
                if server:server.shutdown();server.server_close()
                app.shutdown();app.wait_for_idle(3);app.repo.close()

    def test_partial_batch_failure_keeps_truthful_published_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Config(Path(tmp),{'LLM_API_KEY':'fixture-only','MAX_RUN_TOKENS':'200000'})
            model=ProductModel(broken=True);app=Platform(config,provider=model);model.app=app
            try:
                app.action({'action':'send','channelId':'group','mode':'task','participants':['executor'],'text':'Publish explicit files.'})
                self.assertTrue(app.wait_for_idle(5));run=app.snapshot()['sessions']['group']['run']
                self.assertEqual(run['status'],'partial');self.assertEqual(len(run['artifacts']),1)
                self.assertFalse(model.receipts[0]['ok']);self.assertEqual(model.receipts[0]['failedPath'],'missing.js')
                self.assertEqual(len(model.receipts[0]['artifacts']),1)
                self.assertFalse(run.get('deliveries'))
            finally:app.shutdown();app.wait_for_idle(3);app.repo.close()


if __name__=='__main__':unittest.main()
