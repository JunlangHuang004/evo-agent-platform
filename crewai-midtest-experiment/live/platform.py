"""Durable team runtime with real CrewAI planning, HITL and confined tools."""
import copy,hashlib,io,json,re,threading,time,uuid,zipfile
from pathlib import Path
from backend.config import Config
from backend.teams import get_team_agents
from backend.tools import ToolGateway
from backend.artifacts import ArtifactStore
from backend.persistence import Repository
from backend.memory import MemoryService
from .runtime import receipt_view,short_handoff,proposed_action,review_work,WorkBudgetExhausted

ACTIVE={'queued','planning','running','waiting_human','pausing','paused','interrupted','cancelling'}
RUNTIME_VERSION='delivery-v8'
def uid(prefix):return prefix+'-'+uuid.uuid4().hex[:16]
def now():return time.time()
def public_artifact(a):return {k:v for k,v in a.items() if k!='content'}
def task_artifacts(run,task_id):
    ids={task_id}
    for _ in range(4):ids.update(s['id'] for s in run['steps'] if s.get('parentId') in ids)
    return [a for a in run['artifacts'] if a['taskId'] in ids and not a.get('superseded') and not a.get('carriedForward')]

class Platform:
    runtime_version=RUNTIME_VERSION
    def __init__(self,root,config_root,executor=None):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.config_root=Path(config_root).resolve();self.config=Config(self.config_root)
        self.lock=threading.RLock();self.condition=threading.Condition(self.lock);self.contexts={};self.executor=executor
        self.teams={}
        index=self.root/'teams.json'
        metadata=json.loads(index.read_text()) if index.exists() else [{'id':x,'name':n,'template':x} for x,n in [('software','软件开发团队'),('newsroom','新闻编辑室'),('fund','证券投资团队')]]
        for item in metadata:self._open_team(item)
        self._index()
    def _index(self):
        p=self.root/'teams.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps([t['meta'] for t in self.teams.values()],ensure_ascii=False));tmp.replace(p)
    def _open_team(self,item):
        repo=Repository(self.root/'teams'/item['id']);state=repo.load_state()
        if not state:state={'agents':get_team_agents(item.get('template','default')),'channels':{},'runs':[],'settings':{'memory':True,'gitRemote':''}}
        for channel in ['group',*state['agents']]:state['channels'].setdefault(channel,{'messages':[],'runId':None})
        t={'meta':item,'repo':repo,'memory':MemoryService(repo),'state':state};self.teams[item['id']]=t
        for rid in state['runs']:
            run=repo.get_run(rid)
            if run and run['status'] in ACTIVE:
                run['status']='interrupted';run['recoveryNote']='Server restarted. Inspect uncertain operations before resuming; no automatic replay.'
                run['uncertainCalls']=[c['id'] for c in run['calls'] if c['status']=='running' and c['tool']!='model.generate']
                repo.save_run(run,run['channelId'])
        repo.save_state(state)
    def team(self,team):
        if team not in self.teams:raise ValueError('Unknown team')
        return self.teams[team]
    def create_team(self,name,template='default'):
        if template not in ('default','software','newsroom','fund'):raise ValueError('Unknown template')
        if not isinstance(name,str) or not name.strip() or len(name)>80:raise ValueError('A short team name is required')
        with self.lock:
            item={'id':uid('team'),'name':name.strip(),'template':template};self._open_team(item);self._index();return item
    def save(self,ctx):
        with self.lock:
            ctx['team']['repo'].save_run(ctx['run'],ctx['run']['channelId']);ctx['team']['repo'].save_state(ctx['team']['state'])
            self.condition.notify_all()
    def event(self,ctx,kind,**data):
        with self.lock:
            e={'id':uid('event'),'kind':kind,'at':now(),**ctx['config'].scrub(data)}
            ctx['team']['repo'].append_event(ctx['run']['id'],e);self.save(ctx);return e
    def message(self,ctx,aid,text,kind='agent',to=None):
        with self.lock:
            item={'id':uid('msg'),'agentId':aid,'role':kind,'text':str(text)[:20000],'at':now(),'runId':ctx['run']['id'],'to':to}
            ctx['team']['state']['channels'][ctx['run']['channelId']]['messages'].append(item);ctx['run'].setdefault('messages',[]).append(item);self.save(ctx);return item
    def new_call(self,ctx,step,name,args):
        with self.lock:
            call={'id':uid('call'),'taskId':step['id'],'agentId':step['agentId'],'tool':name,'args':ctx['config'].scrub(copy.deepcopy(args)),'status':'running','startedAt':now()}
            ctx['run']['calls'].append(call);self.save(ctx);return call
    def end_call(self,ctx,call,result,ok):
        with self.lock:
            call.update(status='success' if ok else 'failed',result=ctx['config'].scrub(result),finishedAt=now());call['durationMs']=round((call['finishedAt']-call['startedAt'])*1000)
            self.save(ctx)
    def boundary(self,ctx):
        with self.condition:
            run=ctx['run']
            if now()>=run['deadline']:raise TimeoutError('Original wall deadline reached, including human waiting and pauses')
            if run.get('cancelRequested'):raise InterruptedError('Cancelled by requester')
            if run.get('pauseRequested'):
                run['status']='paused';self.save(ctx)
                while run.get('pauseRequested') and not run.get('cancelRequested'):
                    remaining=run['deadline']-now()
                    if remaining<=0:raise TimeoutError('Wall deadline reached during pause')
                    self.condition.wait(min(remaining,1))
                if run.get('cancelRequested'):raise InterruptedError('Cancelled by requester')
                run['status']='running';self.save(ctx)
    def wait_human(self,ctx,step,kind,title,args,choices=None):
        with self.condition:
            pending={'id':uid('question'),'taskId':step['id'],'agentId':step['agentId'],'kind':kind,'title':str(title)[:3000],'args':copy.deepcopy(args),'choices':choices or [],'status':'pending','createdAt':now()}
            ctx['run']['pending'].append(pending);ctx['run']['status']='waiting_human';self.message(ctx,step['agentId'],title,'question')
            while pending['status']=='pending':
                self.boundary(ctx)
                if ctx['run']['status']!='paused':ctx['run']['status']='waiting_human'
                self.save(ctx);self.condition.wait(min(1,max(.01,ctx['run']['deadline']-now())))
            ctx['run']['status']='running';self.save(ctx)
            return copy.deepcopy(pending)
    def answer(self,team,rid,qid,answer):
        with self.condition:
            ctx=self.contexts.get(rid)
            if not ctx or ctx['team']['meta']['id']!=team:raise ValueError('This run is not waiting in the current server')
            p=next((p for p in ctx['run']['pending'] if p['id']==qid and p['status']=='pending'),None)
            if not p:raise ValueError('Question is no longer pending')
            if p['kind']=='approval' and type(answer) is not bool:raise ValueError('Approval requires true or false')
            if p['kind']=='clarification' and (not isinstance(answer,str) or not answer.strip() or len(answer)>10000):raise ValueError('A nonempty answer is required')
            p.update(status='answered',answer=answer,answeredAt=now());self.message(ctx,'human',str(answer),'human');self.event(ctx,'human_answer',questionId=qid,answer=answer)
    def control(self,team,rid,action,reason=''):
        with self.condition:
            t=self.team(team);ctx=self.contexts.get(rid);run=ctx['run'] if ctx else t['repo'].get_run(rid)
            if not run or run['teamId']!=team:raise ValueError('Unknown run')
            if action=='cancel' and not ctx and run['status']=='interrupted':
                run.update(status='cancelled',cancelRequested=True,finishedAt=now())
                run['elapsedMs']=round((run['finishedAt']-run['startedAt'])*1000)
                t['repo'].save_run(run,run['channelId']);return
            if action=='resume' and not ctx:
                if run['status']!='interrupted':raise ValueError('Only interrupted runs can be recovered')
                if run.get('uncertainCalls'):raise ValueError('Reconcile uncertain operations before resuming')
                if now()>=run['deadline']:raise ValueError('Original deadline expired; start a new task without claiming resumed timing')
                for s in run['steps']:
                    if s['status']=='running':s['status']='queued'
                run['pending']=[p for p in run['pending'] if p['status']!='pending'];run['pauseRequested']=False;run['cancelRequested']=False
                if run.get('runtimeVersion')!=RUNTIME_VERSION:
                    run.setdefault('runtimeHistory',[]).append({'from':run.get('runtimeVersion'),'to':RUNTIME_VERSION,'at':now(),'reason':'Explicit recovery with current server; original deadline and timing unchanged'})
                    run['runtimeVersion']=RUNTIME_VERSION
                ctx=self._context(t,run);self._launch(ctx);return
            if action=='reconcile' and not ctx:
                if not reason.strip():raise ValueError('Record the actual observed external/file effect before recovery')
                ids=run.get('uncertainCalls',[])
                run.setdefault('reconciliations',[]).append({'calls':ids,'reason':reason[:3000],'at':now()})
                for c in run['calls']:
                    if c['id'] in ids:c.update(status='reconciled',result={'operatorNote':reason[:3000],'machineVerified':False})
                run['uncertainCalls']=[];t['repo'].save_run(run,run['channelId']);return
            if not ctx or run['status'] not in ACTIVE:raise ValueError('Run is not active')
            if action=='pause':run['pauseRequested']=True;run['status']='pausing'
            elif action=='resume':run['pauseRequested']=False;run['status']='running'
            elif action=='cancel':run['cancelRequested']=True;run['pauseRequested']=False;run['status']='cancelling'
            else:raise ValueError('Unsupported control')
            self.save(ctx)
    def _context(self,t,run):
        cfg=copy.copy(self.config);cfg.data_dir=self.root/'teams'/t['meta']['id']/'tools';cfg.git_remote=t['state']['settings']['gitRemote']
        agents=copy.deepcopy(run['agents']);ctx={'team':t,'run':run,'config':cfg,'agents':agents}
        ctx['gateway']=ToolGateway(cfg,agents);ctx['artifacts']=ArtifactStore(cfg.data_dir,t['state']['agents']);self.contexts[run['id']]=ctx;return ctx
    def _launch(self,ctx):
        th=threading.Thread(target=self._work,args=(ctx,),daemon=True);ctx['thread']=th;th.start()
    def start(self,team,channel,prompt,participants=None,mode='task',memory=True,seconds=900):
        with self.lock:
            t=self.team(team);state=t['state']
            if channel not in state['channels']:raise ValueError('Unknown channel')
            if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>20000:raise ValueError('Task text is required (max 20000 characters)')
            old=state['channels'][channel]['runId'];previous=t['repo'].get_run(old) if old else None
            if previous and previous['status'] in ACTIVE:raise ValueError('This channel already has an active task')
            ids=list(state['agents']) if participants is None else participants
            if channel!='group':ids=[channel]
            if not isinstance(ids,list) or not ids or len(set(ids))!=len(ids) or any(i not in state['agents'] for i in ids):raise ValueError('Select known distinct participants')
            if sum(c['run']['status'] in ACTIVE for c in self.contexts.values())>=3:raise ValueError('Three runs are already active')
            seconds=int(seconds)
            if not 30<=seconds<=1800:raise ValueError('Wall deadline must be 30–1800 seconds')
            run={'id':uid('run'),'teamId':team,'channelId':channel,'mode':mode,'title':prompt.strip(),'participants':ids,'agents':{i:copy.deepcopy(state['agents'][i]) for i in ids},
                 'status':'queued','startedAt':now(),'deadline':now()+seconds,'finishedAt':None,'steps':[],'calls':[],'artifacts':[],'grants':[],'reviews':[],'pending':[],'memoryHits':[],
                 'memoryEnabled':bool(memory),'modelCalls':0,'tokens':0,'messages':[],'supplements':[],'limits':{'calls':320,'tokens':2500000,'reviewReserveCalls':80,'reviewReserveTokens':750000,'seconds':seconds},'runtimeVersion':RUNTIME_VERSION,'budgetUsage':{},'thinkingPolicy':'review and repeated tool errors: low; other work: disabled','productVerdict':'unassessed','previousRunId':old if channel=='group' and previous and previous['mode']=='task' else None}
            ctx=self._context(t,run)
            if run['previousRunId']:
                # Artifact ownership/grants remain explicit and read-only across tasks.
                try:
                    inherited=ctx['artifacts'].carry_forward(previous,run,ids)
                    run['artifacts']=[public_artifact(a) for a in inherited['artifacts']];run['grants']=inherited['grants']
                except ValueError as exc:run['inheritanceNote']=str(exc)
            state['runs'].append(run['id']);state['channels'][channel]['runId']=run['id'];self.message(ctx,'human',prompt,'human');self._launch(ctx);return copy.deepcopy(run)
    def _execute(self,ctx,step,description,expected,tools=True):
        if self.executor:return self.executor(self,ctx,step,description,expected,tools)
        from .model import execute
        return execute(self,ctx,step,description,expected,tools)
    def _work(self,ctx):
        run=ctx['run']
        try:
            if not run['steps']:
                run['status']='planning';self.save(ctx)
                planner=next((i for i,a in ctx['agents'].items() if a['primaryCapability']=='planning'),run['participants'][0])
                p={'id':'planning','agentId':planner,'title':'自主任务规划','status':'running','revision':0}
                if run['mode']=='chat' or run['channelId']!='group':
                    plan={'tasks':[{'id':'reply','agentId':run['participants'][0],'title':'回应与执行','instructions':run['title'],'dependsOn':[],'requiresArtifacts':False,'expectedOutput':'An accurate response with real tool evidence when execution is needed.'}]}
                else:
                    roster=[{'id':i,'name':a['name'],'capabilities':a['capabilities']} for i,a in ctx['agents'].items()]
                    prompt='Plan the original request as a SMALL task dependency graph. Choose boundaries yourself; do not use a fixed four-stage script. Return ONLY JSON {"tasks":[{"id":"t1","agentId":"known ID","title":"short title","instructions":"concrete work","dependsOn":[],"requiresArtifacts":true,"expectedOutput":"verifiable FILE/tool outcome","reviewOf":null}]}. IDs unique; dependencies precede the task. Prefer 2–5 useful tasks; combine work by the same role where appropriate. Include independent review by a different participant with review capability and reviewOf set to the producer ID. Review tasks finish with the verdict tool (which records review.submit); set requiresArtifacts=false for review tasks (their durable verdict is not a recursively reviewed document). Do not schedule unconditional repair or elaborate final summaries: host enforces final current-version acceptance and schedules repair only on actual failed review. Automatic ZIP export already packages published paths: release should write brief usage/version notes, not duplicate unchanged runtime source into another agent workspace merely for packaging. Producers do focused build/smoke checks; independent QA covers original acceptance criteria, avoiding two exhaustive identical test plans. An internal brief should be concise (<=350 words); product code/reports belong in files, never in final chat. Ask only genuinely unresolved questions; do not ask again about decisions already answered. Documents .md/.txt; explicit HTML and software source permitted. No invented facts, preferences or human replies.\nROSTER: '+json.dumps(roster,ensure_ascii=False)+'\nREQUEST: '+run['title']
                    for attempt in range(2):
                        raw=self._execute(ctx,p,prompt,'Only a valid JSON dependency graph',False)
                        try:
                            match=re.search(r'\{[\s\S]*\}',raw)
                            if not match:raise ValueError('Planner did not return a JSON task graph')
                            plan=json.loads(match.group());self.validate_plan(plan,ctx['agents']);break
                        except ValueError as exc:
                            if attempt:raise
                            prompt+='\nYour previous plan was rejected: '+str(exc)+'. Return a corrected graph using the listed capabilities.'
                run['steps']=self.validate_plan(plan,ctx['agents']);self.message(ctx,planner,'已拆解为 '+str(len(run['steps']))+' 个子任务，开始按依赖执行。','plan')
            run['status']='running';self.save(ctx)
            while any(s['status']=='queued' for s in run['steps']):
                self.boundary(ctx)
                ready=next((s for s in run['steps'] if s['status']=='queued' and all(next(x for x in run['steps'] if x['id']==d)['status']=='done' for d in s['dependsOn'])),None)
                if not ready:
                    for s in run['steps']:
                        if s['status']=='queued':s['status']='blocked'
                    break
                try:self.run_step(ctx,ready)
                except InterruptedError:raise
                except Exception as exc:
                    ready.update(status='failed',result=ctx['config'].scrub(str(exc)),finishedAt=now())
                    ready['elapsedMs']=round((ready['finishedAt']-ready.get('startedAt',ready['finishedAt']))*1000)
                    ctx['team']['memory'].record_episode(ready['agentId'],run,ready,False,ready['result'],[c['id'] for c in run['calls'] if c['taskId']==ready['id']],run['channelId'])
                    self.message(ctx,ready['agentId'],ready['title']+'：'+ready['result'],'error');self.save(ctx)
                    if isinstance(exc,WorkBudgetExhausted):self.checkpoint_files(ctx,ready)
            self.close_acceptance(ctx)
            successful=all(s['status']=='done' for s in run['steps'])
            required=self.delivery_targets(run)
            verdicts=[]
            for s in required:
                current=task_artifacts(run,s['id'])
                reviews=[r for r in run['reviews'] if r['targetTaskId']==s['id'] and r['revision']==s['revision'] and r['artifacts']==sorted(a['id'] for a in current)]
                verdicts.append('pass' if current and reviews and reviews[-1]['verdict']=='pass' else 'fail' if reviews and reviews[-1]['verdict']=='fail' else 'unassessed')
            run['productVerdict']='fail' if 'fail' in verdicts else 'unassessed' if 'unassessed' in verdicts or not required else 'pass'
            run['status']='done' if successful and (not required or run['productVerdict']=='pass') else 'partial'
        except InterruptedError:run['status']='cancelled'
        except Exception as exc:run.update(status='failed',error=ctx['config'].scrub(str(exc)))
        finally:
            run['finishedAt']=now();run['elapsedMs']=round((run['finishedAt']-run['startedAt'])*1000)
            self.message(ctx,'system',f'执行结束：{run["status"]}；成果验收：{run["productVerdict"]}。已保存 {len(run["artifacts"])} 项产物。','system')
            self.save(ctx)
    @staticmethod
    def validate_plan(plan,agents):
        tasks=plan.get('tasks') if isinstance(plan,dict) else None
        if not isinstance(tasks,list) or not 1<=len(tasks)<=12:raise ValueError('Plan must contain 1–12 tasks')
        out=[];seen=set()
        for s in tasks:
            if not isinstance(s,dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',str(s.get('id',''))) or s['id'] in seen:raise ValueError('Invalid or repeated task ID')
            if s.get('agentId') not in agents:raise ValueError('Plan selected an absent agent')
            deps=s.get('dependsOn',[])
            if not isinstance(deps,list) or any(d not in seen for d in deps):raise ValueError('Dependency must refer to an earlier task')
            if not isinstance(s.get('instructions'),str) or not s['instructions'].strip():raise ValueError('Task instructions are required')
            target=s.get('reviewOf')
            if target:
                producer=next((x for x in out if x['id']==target),None)
                if not producer or producer['agentId']==s['agentId'] or target not in deps:raise ValueError('Review needs an independent producer dependency')
                if 'review' not in agents[s['agentId']]['capabilities']:raise ValueError('Assign reviewOf only to a review-capable participant')
            seen.add(s['id']);out.append({**s,'title':str(s.get('title',s['id']))[:200],'dependsOn':deps,'status':'queued','revision':0,'requiresArtifacts':bool(s.get('requiresArtifacts',False)) and not bool(target),'result':''})
        return out
    @staticmethod
    def delivery_targets(run):
        targets=[]
        for s in run['steps']:
            if review_work(s):continue
            current=task_artifacts(run,s['id'])
            ever=any(a['taskId']==s['id'] and not a.get('carriedForward') for a in run['artifacts'])
            if current or s.get('requiresArtifacts') and not ever:targets.append(s)
        return targets
    @staticmethod
    def current_review(run,target):
        artifacts=task_artifacts(run,target['id'])
        matches=[r for r in run['reviews'] if r['targetTaskId']==target['id'] and r['revision']==target['revision'] and r['artifacts']==sorted(a['id'] for a in artifacts)]
        return matches[-1] if artifacts and matches else None
    def checkpoint_files(self,ctx,step):
        run=ctx['run']
        written={c['args']['path']:c.get('result',{}).get('sha256') for c in run['calls'] if c['taskId']==step['id'] and c['agentId']==step['agentId'] and c['status']=='success' and c['tool'] in ('file.write','file.patch')}
        intended={path for c in run['calls'] if c['taskId']==step['id'] and c['tool']=='artifact.publish' for path in c['args'].get('paths',[])}
        if intended:written={p:h for p,h in written.items() if p in intended}
        paths=[p for p,h in written.items() if not any(a['path']==p and a['ownerAgentId']==step['agentId'] and a['taskId']==step['id'] and a['sha256']==h and not a.get('superseded') and not a.get('carriedForward') for a in run['artifacts'])]
        if paths:
            self.event(ctx,'runtime_checkpoint_publication',taskId=step['id'],paths=paths,reason='Preserve actual written bytes for independent review; this is not a success verdict')
            self.tool(ctx,step,'artifact.publish',{'paths':paths[:12]})
            step['runtimeCheckpointUsed']=True
    def close_acceptance(self,ctx):
        """Cover current product bytes, including revisions after planned review."""
        run=ctx['run']
        if run['mode']!='task' or run['channelId']!='group':return
        for cycle in range(3):
            pending=[s for s in self.delivery_targets(run) if task_artifacts(run,s['id']) and not self.current_review(run,s)]
            groups={}
            for target in pending:
                owners={a['ownerAgentId'] for a in task_artifacts(run,target['id'])}
                reviewer=next((aid for aid,a in ctx['agents'].items() if 'review' in a['capabilities'] and aid not in owners),None)
                if reviewer:groups.setdefault(reviewer,[]).append(target)
            for reviewer,targets in groups.items():
                ids=[s['id'] for s in targets]
                step={'id':uid('acceptance'),'agentId':reviewer,'title':'Independent acceptance of current artifacts','instructions':'Independently assess the ORIGINAL USER GOAL using all current artifacts below, not merely whether files exist. Read every current artifact with file.read (full contents in line windows), execute meaningful code/browser tests for executable files, and submit actual review.submit verdicts for targetTaskIds='+json.dumps(ids)+'. Batch targetTaskIds may share one verdict ONLY if you actually checked each. If the original goal is incomplete, key facts lack sources, behavior fails, or only planning notes exist, submit fail with actionable defects. Do not invent test passes. You may use matching prior successful test evidence for unchanged hashes; repeat only affected tests. A review is the durable tool record; do not write/review your own review report.','expectedOutput':'Actual hash-bound independent review.submit records with pass/fail and evidence.','dependsOn':ids,'reviewTargets':ids,'acceptance':True,'requiresArtifacts':False,'status':'queued','revision':0}
                run['steps'].append(step)
                try:self.run_step(ctx,step)
                except (TimeoutError,InterruptedError):raise
                except Exception as exc:step.update(status='failed',result=ctx['config'].scrub(str(exc)),finishedAt=now());self.save(ctx)
            failed=[s for s in self.delivery_targets(run) if (v:=self.current_review(run,s)) and v['verdict']=='fail']
            if not failed or cycle==2:break
            for target in failed:
                verdict=self.current_review(run,target)
                repair={'id':uid('repair'),'agentId':target['agentId'],'title':'Repair independently observed defects','instructions':'Fix only confirmed defects in your original task: '+target['instructions']+'\nINDEPENDENT DEFECT REPORT: '+verdict['summary']+'\nUse your own current paths, publish the corrected files, and run affected checks. No changes to peer imports or to test expectations merely to hide a defect. Your corrected version will be independently reviewed again.','expectedOutput':'Actual corrected published files and a short receipt identifying fixes and unresolved issues.','dependsOn':[target['id']],'requiresArtifacts':True,'repairOf':target['id'],'status':'queued','revision':0}
                run['steps'].append(repair)
                try:self.run_step(ctx,repair)
                except (TimeoutError,InterruptedError):raise
                except Exception as exc:repair.update(status='failed',result=ctx['config'].scrub(str(exc)),finishedAt=now());self.save(ctx)
    def run_step(self,ctx,step,depth=0):
        self.boundary(ctx);run=ctx['run'];aid=step['agentId'];step.update(status='running',startedAt=now(),depth=depth);self.message(ctx,aid,'开始执行：'+step['title'],'task');self.save(ctx)
        memory=ctx['team']['memory'];hits=memory.recall(aid,run['title']+' '+step['instructions'],run['channelId']) if run['memoryEnabled'] else []
        hits=[h for h in hits if h.get('channelId')==run['channelId']]
        if run['memoryEnabled']:
            from backend.persistence import tokens
            terms=tokens(run['title']+' '+step['instructions'])
            owned=[m for m in memory.list(aid) if m.get('channelId')==run['channelId'] and m['status'] in ('candidate','episodic') and terms & tokens(m.get('title','')+' '+m.get('lesson','')+' '+m.get('trigger','')+' '+m.get('reason',''))]
            owned.sort(key=lambda m:str(m.get('createdAt','')),reverse=True)
            # Candidates are exposed as trials, never as trusted strategies.
            hits=hits[:3]+[m for m in owned if m['status']=='candidate'][:1]+[m for m in owned if m['status']=='episodic'][:2]
        step['recalled']=[h['id'] for h in hits]
        for h in hits:run['memoryHits'].append({'agentId':aid,'taskId':step['id'],'memoryId':h['id'],'title':h.get('title'),'lesson':h.get('lesson') or h.get('reason'),'status':h['status'],'applied':False})
        deps=[{'taskId':s['id'],'agentId':s['agentId'],'result':short_handoff(s.get('result',''),1200)} for s in run['steps'] if s['id'] in step['dependsOn']]
        current_ids={a['id'] for a in run['artifacts'] if not a.get('superseded')}
        grants=[{k:g[k] for k in ('id','ownerAgentId','taskId','sha256','size') if k in g}|{'path':'imports/'+g['path']} for g in run['grants'] if g['toAgentId']==aid and g['id'] in current_ids]
        previous=ctx['team']['repo'].get_run(run['previousRunId']) if run.get('previousRunId') else None
        prior=(previous.get('title','')[:500]+'\n'+json.dumps([{'title':s['title'],'result':short_handoff(s.get('result',''),400)} for s in previous['steps']],ensure_ascii=False))[:2200] if previous else ''
        memory_context=[{k:h.get(k) for k in ('id','status','kind','sourceRunId')}|{'lesson':str(h.get('lesson') or h.get('reason') or '')[:1000]} for h in hits]
        description='CURRENT REQUEST:\n'+run['title']+'\nYOUR SUBTASK:\n'+step['instructions']+'\nUse actual tools. Ask the real human for ambiguities or approvals; never simulate answers. Tool errors are not success. Do not invent observations. Write documents only as Markdown/TXT; code/HTML may be code artifacts. Publish exact paths via artifact.publish so peers can read imports. Imported files are read-only; copy to your own path before fixing. Report unresolved issues. A review task must read current artifacts, execute appropriate tests, then finish with the verdict tool (which commits the actual review.submit record). Use memory.propose for reusable lessons and memory.use only when actually applying a recalled lesson. Finish focused work and hand off promptly.\nDEPENDENCIES: '+json.dumps(deps,ensure_ascii=False)+'\nAUTHORIZED IMPORTS: '+json.dumps(grants,ensure_ascii=False)+'\nYOUR OWN MEMORIES (candidates are unverified): '+json.dumps(memory_context,ensure_ascii=False)+'\nPRIOR GROUP TASK CONTEXT: '+prior
        result=''
        targets=step.get('reviewTargets') or ([step['reviewOf']] if step.get('reviewOf') else [])
        for attempt in range(2):
            result=self._execute(ctx,step,description,step.get('expectedOutput','Evidence-based result with actual artifact paths'))
            pending=proposed_action(result)
            if pending and pending['operation'] in ('file.write','file.patch','artifact.publish','review.submit','code.test'):
                duplicate=any(c['taskId']==step['id'] and c['tool']==pending['operation'] and c['args']==pending['arguments'] and c['status']=='success' for c in run['calls'])
                if not duplicate:
                    self.event(ctx,'pending_action_recovered',taskId=step['id'],operation=pending['operation'],reason='Unexecuted complete terminal proposal; actual permission gateway receipt required')
                    self.tool(ctx,step,pending['operation'],pending['arguments'])
            if step.get('requiresArtifacts'):self.checkpoint_files(ctx,step)
            missing_files=step.get('requiresArtifacts') and not task_artifacts(run,step['id'])
            missing_reviews=[tid for tid in targets if not self.current_review(run,next(s for s in run['steps'] if s['id']==tid))]
            if not missing_files and not missing_reviews:break
            if attempt==0:
                self.event(ctx,'subtask_continuation',taskId=step['id'],missingArtifacts=bool(missing_files),missingReviews=missing_reviews)
                recent=[{'tool':c['tool'],'status':c['status'],'result':receipt_view(c['tool'],c.get('result',{}))} for c in run['calls'] if c['taskId']==step['id'] and c['tool'] not in ('model.generate','file.read')][-4:]
                description+='\nCONTINUATION: The task is not complete. Already executed receipts (do not replay): '+json.dumps(recent,ensure_ascii=False)[:6500]+'\nYour actual file/review outcome is missing. Immediately write and publish the required artifact, or submit the missing independent review for '+json.dumps(missing_reviews)+'. Do not merely promise, repeat analysis, or paste product content into Final Answer.'
        step.update(result=short_handoff(result,2400),status='done',finishedAt=now());step['elapsedMs']=round((step['finishedAt']-step['startedAt'])*1000)
        if missing_files or missing_reviews:step['status']='partial'

        memory.record_episode(aid,run,step,step['status']=='done',step['result'][:2400],[c['id'] for c in run['calls'] if c['taskId']==step['id']],run['channelId'])
        for mid in set(step.get('adopted',[])):
            m=ctx['team']['repo'].get_memory(mid,aid)
            if m and m.get('verified'):memory.feedback(aid,mid,step['status']=='done',step['id']+':'+run['id'])
            elif m:
                m.setdefault('candidateTrials',[]).append({'runId':run['id'],'taskId':step['id'],'executionSuccess':step['status']=='done','productAcceptance':'unassessed','at':now()})
                ctx['team']['repo'].update_memory(aid,m)
        self.message(ctx,aid,step['result']);self.save(ctx);return {'taskId':step['id'],'status':step['status'],'result':step['result'],'artifacts':[a for a in run['artifacts'] if a['taskId']==step['id']]}
    def tool_description(self,ctx,aid):
        extra={'ask_user':{'question':'text','choices':['optional choices']},'collab.message':{'text':'message to group; private channels remain private'},'collab.delegate':{'agentId':'participating specialist','title':'short title','instructions':'work','expectedOutput':'criterion'},'artifact.publish':{'paths':['own relative paths'],'to':['participating recipient IDs, default all']},'artifact.list':{},'memory.propose':{'lesson':'reusable candidate','trigger':'when applicable'},'memory.use':{'id':'recalled own memory ID','reason':'how applied'},'review.submit':{'targetTaskId':'one producer task, OR targetTaskIds:[IDs] for a fully checked batch','verdict':'pass or fail','summary':'evidence based assessment and actual test/call references'}}
        if 'review' not in ctx['agents'][aid]['capabilities']:extra.pop('review.submit')
        return 'Execute ONE operation with arguments matching these schemas. No shell commands.\n'+json.dumps(ctx['gateway'].definitions(aid),ensure_ascii=False)+'\nCollaboration operations: '+json.dumps(extra,ensure_ascii=False)
    def review_record(self,ctx,aid,target_id,args):
        run=ctx['run']
        target=next((s for s in run['steps'] if s['id']==target_id),None)
        if not target or target['agentId']==aid or 'review' not in ctx['agents'][aid]['capabilities']:raise ValueError('An independent review-capable participant must submit the verdict')
        artifacts=task_artifacts(run,target['id'])
        if any(a['ownerAgentId']==aid for a in artifacts):raise ValueError('An artifact producer cannot independently review their own delegated output')
        if not artifacts or any(not ctx['artifacts'].can_access(a['id'],aid,run['id']) for a in artifacts):raise ValueError('Read authorization is required for every current artifact')
        reads=[c for c in run['calls'] if c['agentId']==aid and c['tool']=='file.read' and c['status']=='success']
        if any(not any(c.get('result',{}).get('sha256')==a['sha256'] for c in reads) for a in artifacts):raise ValueError('Read every exact current artifact before review')
        if args.get('verdict') not in ('pass','fail'):raise ValueError('Verdict must be pass or fail')
        if args['verdict']=='pass' and any(Path(a['name']).suffix in ('.html','.js','.py') for a in artifacts):
            hashes={x['sha256'] for c in run['calls'] if c['agentId']==aid and c['tool'] in ('code.test','code.run') and c['status']=='success' and c.get('result',{}).get('exitCode')==0 for x in c['result'].get('browser',{}).get('testedFiles',[])}
            if any(a['sha256'] not in hashes for a in artifacts if Path(a['name']).suffix in ('.html','.js')):raise ValueError('Executable artifact pass needs current hash-bound browser test evidence')
            if any(Path(a['name']).suffix=='.py' for a in artifacts) and not any(c['agentId']==aid and c['tool']=='code.test' and c['status']=='success' and c.get('result',{}).get('exitCode')==0 for c in run['calls']):raise ValueError('Python artifacts require an executed successful test')
        review={'id':uid('review'),'reviewer':aid,'targetTaskId':target['id'],'revision':target['revision'],'artifacts':sorted(a['id'] for a in artifacts),'verdict':args['verdict'],'summary':str(args.get('summary',''))[:5000],'at':now()}
        return review
    def tool(self,ctx,step,op,args):
        self.boundary(ctx)
        if not isinstance(args,dict):raise ValueError('Arguments must be an object')
        aid=step['agentId'];run=ctx['run'];call=self.new_call(ctx,step,op,args)
        try:
            if op=='ask_user':
                answer=self.wait_human(ctx,step,'clarification',args['question'],{},args.get('choices'));result={'ok':True,'answer':answer['answer'],'receiptId':answer['id']}
            elif op=='collab.message':self.message(ctx,aid,args['text']);result={'ok':True}
            elif op=='collab.delegate':
                target=args.get('agentId')
                if target not in ctx['agents'] or target==aid:raise ValueError('Delegate to a different participating agent')
                if step.get('depth',0)>=3 or len(run['steps'])>=20:raise ValueError('Delegation depth/task budget reached')
                child={'id':uid('subtask'),'agentId':target,'title':str(args['title'])[:200],'instructions':str(args['instructions'])[:10000],'expectedOutput':str(args.get('expectedOutput','Verified result')),'dependsOn':[],'status':'queued','revision':0,'parentId':step['id'],'requiresArtifacts':bool(args.get('requiresArtifacts',False))}
                run['steps'].append(child);self.message(ctx,aid,'委派给 '+ctx['agents'][target]['name']+'：'+child['title'],'delegation');result=self.run_step(ctx,child,step.get('depth',0)+1)
            elif op=='artifact.publish':
                targets=args.get('to',[i for i in run['participants'] if i!=aid])
                if not isinstance(targets,list) or any(i not in ctx['agents'] for i in targets):raise ValueError('Artifact recipient is not participating')
                paths=args.get('paths')
                if not isinstance(paths,list) or not 1<=len(paths)<=12:raise ValueError('Publish 1–12 own text/code paths')
                produced=[];grants=[];changed=False;reused=[]
                for path in paths:
                    text=ctx['gateway'].execute(aid,run['id'],'file.read',{'path':path})
                    from backend.sandbox import read_text
                    raw=read_text(ctx['gateway'].workspace(aid,run['id']),path)
                    if ctx['config'].scrub(raw)!=raw:raise ValueError('Known credentials cannot be published')
                    digest=hashlib.sha256(raw.encode()).hexdigest()
                    existing=next((a for a in run['artifacts'] if a['ownerAgentId']==aid and a['path']==path and a['taskId']==step['id'] and a['sha256']==digest and not a.get('superseded') and not a.get('carriedForward')),None)
                    if existing:a=existing;reused.append(a['id'])
                    else:
                        a=ctx['artifacts'].register(aid,run['id'],path,step['id']);changed=True
                        for old in run['artifacts']:
                            if old['ownerAgentId']==aid and old['path']==a['path'] and not old.get('superseded'):old['superseded']=True
                        run['artifacts'].append(public_artifact(a))
                    produced.append(public_artifact(a))
                    for recipient in targets:
                        g=ctx['artifacts'].grant(a['id'],recipient,run['id'],aid);run['grants'].append(g);grants.append(g)
                step['revision']+=int(changed)
                parent=step.get('parentId')
                while parent:
                    ancestor=next(s for s in run['steps'] if s['id']==parent);ancestor['revision']+=int(changed);parent=ancestor.get('parentId')
                result={'ok':True,'artifacts':produced,'reusedUnchanged':reused,'imports':[{'agentId':g['toAgentId'],'path':'imports/'+g['path'],'sha256':g['sha256']} for g in grants]}
            elif op=='artifact.list':result={'ok':True,'artifacts':[a for a in run['artifacts'] if ctx['artifacts'].can_access(a['id'],aid,run['id']) and not a.get('superseded')]}
            elif op=='memory.propose':
                m=ctx['team']['memory'].propose(aid,args['lesson'],args['trigger'],step['id'],run['id'],[{'type':'result','id':step['id']}],channel_id=run['channelId']);result={'ok':True,'memory':m}
            elif op=='memory.use':
                if args.get('id') not in step.get('recalled',[]):raise ValueError('Only own recalled memories may be marked applied')
                step.setdefault('adopted',[]).append(args['id'])
                for h in run['memoryHits']:
                    if h['taskId']==step['id'] and h['memoryId']==args['id']:h.update(applied=True,reason=str(args.get('reason',''))[:2000])
                result={'ok':True}
            elif op=='review.submit':
                ids=args.get('targetTaskIds') or [args.get('targetTaskId')]
                if not isinstance(ids,list) or not 1<=len(ids)<=12 or len(set(ids))!=len(ids):raise ValueError('Review targets must be 1–12 distinct task IDs')
                reviews=[self.review_record(ctx,aid,target_id,args) for target_id in ids]
                run['reviews'].extend(reviews)
                result={'ok':True,'reviews':reviews,'review':reviews[0]}
            else:
                result=ctx['gateway'].execute(aid,run['id'],op,args,deadline=time.monotonic()+max(0,run['deadline']-now()))
                if result.get('needsApproval'):
                    bound=copy.deepcopy(result['args']);answer=self.wait_human(ctx,step,'approval',result['approval']['title'],{'operation':op,'arguments':bound})
                    result=ctx['gateway'].execute(aid,run['id'],op,bound,approved=True,deadline=time.monotonic()+max(0,run['deadline']-now())) if answer['answer'] else {'ok':False,'deniedByHuman':True,'error':'The requester denied this exact operation'}
            self.end_call(ctx,call,result,result.get('ok',True) is not False)
            return receipt_view(op,result)
        except Exception as exc:self.end_call(ctx,call,{'error':ctx['config'].scrub(str(exc))},False);raise
    def snapshot(self,team,channel='group',run_id=None):
        with self.lock:
            t=self.team(team);s=t['state']
            if channel not in s['channels']:raise ValueError('Unknown channel')
            rid=run_id or s['channels'][channel]['runId'];run=t['repo'].get_run(rid) if rid else None
            if run and (run['teamId']!=team or run['channelId']!=channel):raise ValueError('Run is outside this channel')
            recent=[t['repo'].get_run(r) for r in reversed(s['runs'][-60:])]
            history=[{k:r.get(k) for k in ('id','title','channelId','status','productVerdict','startedAt','finishedAt','elapsedMs')} for r in recent if r]
            memories={aid:t['memory'].list(aid) for aid in s['agents']}
            clean=copy.deepcopy(run)
            if clean:
                for c in clean['calls']:
                    if isinstance(c.get('result'),dict):c['result'].pop('rawContent',None)
                    if len(json.dumps(c.get('result',{}),ensure_ascii=False))>10000:c['result']={'detailAvailable':True,'summary':'完整回执请点击查看或导出记录。'}
                    if len(json.dumps(c.get('args',{}),ensure_ascii=False))>6000:c['args']={'detailAvailable':True}
                clean.pop('agents',None)
            return self.config.scrub({'teams':[t['meta'] for t in self.teams.values()],'teamId':team,'team':t['meta'],'agents':s['agents'],'channels':[{'id':k,'label':'群聊' if k=='group' else s['agents'][k]['name']} for k in s['channels']],
                   'channelId':channel,'messages':copy.deepcopy((run.get('messages',[]) if run_id and run else s['channels'][channel]['messages'])[-150:]),'run':clean,'history':history,'memories':memories,
                   'model':{'provider':self.config.provider,'name':self.config.model,'configured':bool(self.config.api_key)},'settings':s['settings'],'evaluation':self.metrics(recent)})
    @staticmethod
    def metrics(runs):
        runs=[r for r in runs if r and r['mode']=='task'];finished=[r for r in runs if r['status'] not in ACTIVE]
        steps=[s for r in finished for s in r['steps']]
        return {'runs':len(finished),'executionCompletionRate':sum(all(s['status']=='done' for s in r['steps']) and bool(r['steps']) for r in finished)/len(finished) if finished else None,
                'productPassRate':sum(r['productVerdict']=='pass' for r in finished)/len(finished) if finished else None,'subtaskCompletionRate':sum(s['status']=='done' for s in steps)/len(steps) if steps else None,
                'averageMs':sum(r.get('elapsedMs',0) for r in finished)/len(finished) if finished else None,'rows':[{k:r.get(k) for k in ('id','title','status','productVerdict','elapsedMs','memoryEnabled','modelCalls','tokens')} for r in finished]}
    def update_agent(self,team,aid,values):
        with self.lock:
            t=self.team(team)
            if aid not in t['state']['agents']:raise ValueError('Unknown agent')
            if any(r['status'] in ACTIVE for r in [t['repo'].get_run(i) for i in t['state']['runs']] if r):raise ValueError('Edit roles after active team tasks finish')
            from backend.tools import _SCHEMAS
            a=t['state']['agents'][aid]
            for key in ('name','rolePrompt'):
                if key in values:
                    if not isinstance(values[key],str) or not values[key].strip() or len(values[key])>8000:raise ValueError('Invalid role text')
                    a[key]=values[key].strip()
            if 'tools' in values:
                if not isinstance(values['tools'],list) or any(x not in _SCHEMAS for x in values['tools']):raise ValueError('Unknown tool permission')
                a['tools']=values['tools']
            t['repo'].save_state(t['state'])
    def settings(self,team,values):
        with self.lock:
            t=self.team(team)
            if 'gitRemote' in values:
                remote=str(values['gitRemote']).strip()
                if remote:ToolGateway._check_remote_format(remote)
                t['state']['settings']['gitRemote']=remote
            if 'memory' in values:t['state']['settings']['memory']=bool(values['memory'])
            t['repo'].save_state(t['state'])
    def confirm_memory(self,team,aid,mid,confirmed):
        if type(confirmed) is not bool:raise ValueError('Memory confirmation requires a boolean')
        t=self.team(team);m=t['repo'].get_memory(mid,aid)
        if not m:raise ValueError('Unknown owned memory')
        # A human confirms a candidate; this is not proof of improved delivery.
        return t['memory'].confirm(mid,aid,bool(confirmed),{'type':'human','id':uid('human-memory-review')})
    def artifact(self,team,rid,artifact_id):
        t=self.team(team);run=t['repo'].get_run(rid)
        if not run or not any(a['id']==artifact_id for a in run['artifacts']):raise ValueError('Artifact is outside this run')
        store=ArtifactStore(self.root/'teams'/team/'tools',t['state']['agents']);a=store.get(artifact_id)
        if a['runId']!=rid:raise ValueError('Artifact is outside this run')
        return a
    def bundle(self,team,rid):
        t=self.team(team);run=t['repo'].get_run(rid)
        if not run:raise ValueError('Unknown run')
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
            for a in run['artifacts']:
                if a.get('superseded'):continue
                item=self.artifact(team,rid,a['id']);z.writestr(item['ownerAgentId']+'/'+item['path'],item['content'])
            z.writestr('artifact-manifest.json',json.dumps(run['artifacts'],ensure_ascii=False,indent=2))
            z.writestr('run-report.json',json.dumps(self.config.scrub(run),ensure_ascii=False,indent=2))
        return buf.getvalue()

    def supplement(self,team,channel,text):
        t=self.team(team);rid=t['state']['channels'].get(channel,{}).get('runId');ctx=self.contexts.get(rid)
        if not ctx or ctx['run']['status'] not in ACTIVE:raise ValueError('No active task in this channel')
        if not isinstance(text,str) or not text.strip() or len(text)>10000:raise ValueError('A short supplement is required')
        ctx['run']['supplements'].append(text.strip());self.message(ctx,'human',text,'human')

    def reset(self,team,channel):
        with self.lock:
            t=self.team(team);c=t['state']['channels'][channel];r=t['repo'].get_run(c['runId']) if c['runId'] else None
            if r and r['status'] in ACTIVE:raise ValueError('Finish or cancel the active task before starting a new conversation')
            c['messages']=[];c['runId']=None;t['repo'].save_state(t['state'])
