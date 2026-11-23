"""Original Mid-Test order; human interaction stays in the live platform."""
import json,threading,time
from pathlib import Path
from benchmarks.mid_test_live import load_suite
from .platform import uid

class SuiteManager:
    def __init__(self,platform,source):
        self.platform=platform;self.source=Path(source);self.path=platform.root/'suite.json';self.lock=threading.RLock()
        self.state=json.loads(self.path.read_text()) if self.path.exists() else None
        if self.state and self.state['status']=='running':self.state['status']='interrupted';self.save()
    def save(self):
        with self.lock:
            tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(self.state,ensure_ascii=False));tmp.replace(self.path)
            if self.state and self.state.get('id'):
                folder=self.platform.root/'suites';folder.mkdir(exist_ok=True);p=folder/(self.state['id']+'.json');temp=p.with_suffix('.tmp');temp.write_text(json.dumps(self.state,ensure_ascii=False));temp.replace(p)
    def start(self,seconds=900,memory=True):
        if self.state and self.state['status']=='running':raise ValueError('A Mid-Test suite is already running')
        seconds=int(seconds)
        if not 60<=seconds<=3600:raise ValueError('Suite wall limit must be 60–3600 seconds')
        groups=load_suite(self.source)
        tasks=[{'team':g['team'],**t,'status':'not_started'} for g in groups for t in g['tasks']]
        # Use fresh scenario teams to avoid leakage from old demonstration tasks.
        teams={k:self.platform.create_team('Mid-Test · '+k,k)['id'] for k in ('software','newsroom','fund')}
        for group in groups:
            for aid,prompt in group['roles'].items():self.platform.update_agent(teams[group['team']],aid,{'rolePrompt':prompt})
        self.state={'id':uid('suite'),'status':'running','startedAt':time.time(),'deadline':time.time()+seconds,'memory':bool(memory),'cases':tasks,'teams':teams,'cancelRequested':False,'protocol':'Software 1→2, Newsroom 1→2, Fund 1→2; real human replies; no simulated assumptions'}
        self.save();threading.Thread(target=self.work,daemon=True).start();return self.state
    def work(self):
        try:
            for case in self.state['cases']:
                remaining=int(self.state['deadline']-time.time())
                if remaining<30 or self.state['cancelRequested']:break
                team=self.state['teams'][case['team']];run=self.platform.start(team,'group',case['text'],memory=self.state['memory'],seconds=min(1800,remaining))
                case.update(status='running',runId=run['id'],teamId=team);self.state['current']={'teamId':team,'runId':run['id']};self.save()
                while True:
                    r=self.platform.team(team)['repo'].get_run(run['id'])
                    if r['status'] not in ('queued','planning','running','waiting_human','pausing','paused','cancelling'):break
                    if self.state['cancelRequested'] or time.time()>self.state['deadline']:
                        if not r.get('cancelRequested'):self.platform.control(team,run['id'],'cancel')
                    time.sleep(.2)
                case.update(status=r['status'],productVerdict=r['productVerdict'],elapsedMs=r['elapsedMs'],executionComplete=bool(r['steps']) and all(s['status']=='done' for s in r['steps']));self.save()
            self.state['status']='cancelled' if self.state['cancelRequested'] else 'timed_out' if any(c['status']=='not_started' for c in self.state['cases']) else 'finished'
        except Exception as exc:self.state.update(status='failed',error=self.platform.config.scrub(str(exc)))
        finally:
            self.state['finishedAt']=time.time();self.state['elapsedMs']=round((self.state['finishedAt']-self.state['startedAt'])*1000)
            self.state['executionCompletionRate']=sum(bool(c.get('executionComplete')) for c in self.state['cases'])/6
            self.state['productPassRate']=sum(c.get('productVerdict')=='pass' for c in self.state['cases'])/6
            self.save()
    def cancel(self):
        if not self.state or self.state['status']!='running':raise ValueError('No suite is running')
        self.state['cancelRequested']=True;self.save()
