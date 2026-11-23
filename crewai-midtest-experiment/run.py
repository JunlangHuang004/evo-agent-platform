"""Isolated CrewAI Mid-Test pilot. Course API and real existing tool gateway only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

os.environ['OTEL_SDK_DISABLED'] = 'true'
os.environ['CREWAI_TELEMETRY_DISABLED'] = 'true'
os.environ['CREWAI_TRACING_ENABLED'] = 'false'
ROOT = Path(__file__).resolve().parent
REPO = ROOT / 'config-deepseek'
sys.path.insert(0, str(ROOT / 'vendor'))

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)

def worker(args):
    from crewai import Agent, Task, Crew, Process, BaseLLM
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field
    from backend.config import Config
    from backend.provider import ModelClient
    from backend.teams import get_team_agents
    from backend.tools import ToolGateway
    from backend.sandbox import write_text
    from benchmarks.mid_test_live import load_suite
    from importlib.metadata import version

    team = next(t for t in load_suite(args.suite) if t['team'] == args.team)
    cfg = Config(REPO)
    if args.transport: cfg.transport_mode=args.transport
    if args.direct:
        for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
            os.environ.pop(key,None)
        # macOS urllib also discovers System Settings proxies without env vars.
        os.environ['NO_PROXY']='*'
        os.environ['no_proxy']='*'
    cfg.data_dir = args.output / args.team / 'runtime'
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    # Keep course model and local transport settings unchanged.
    cfg.max_completion_tokens = 8000
    cfg.model_timeout = 120
    registry = get_team_agents(args.team)
    gateway = ToolGateway(cfg, registry)
    client = ModelClient(cfg)
    run_id = 'crew-' + uuid.uuid4().hex[:16]
    started = time.monotonic()
    deadline = started + args.seconds
    case_id = None
    events = []

    def native_stop_complete(messages):
        import urllib.request
        from backend.provider import NoRedirect
        body={'model':cfg.model,'messages':messages,'temperature':0.2,'max_tokens':8000,'stream':False,'stop':['\nObservation:']}
        if cfg.thinking_mode=='disabled': body['chat_template_kwargs']={'enable_thinking':False}
        req=urllib.request.Request(cfg.base_url+'/chat/completions',data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg.api_key},method='POST')
        handlers=[NoRedirect()]
        if args.direct: handlers.append(urllib.request.ProxyHandler({}))
        with urllib.request.build_opener(*handlers).open(req,timeout=min(120,max(1,deadline-time.monotonic()))) as response:
            raw=response.read(2_000_001)
            if len(raw)>2_000_000: raise ValueError('Response size limit exceeded')
            value=json.loads(raw)
        choice=value['choices'][0]
        return cfg.scrub(dict(content=choice['message']['content'],usage=value.get('usage',{}),model=value.get('model'),finish_reason=choice.get('finish_reason'),transport={'transportMode':'standard','direct':args.direct,'nativeStop':['\nObservation:']}))

    def event(kind, **data):
        row = cfg.scrub(dict(kind=kind, caseId=case_id, elapsedSeconds=round(time.monotonic()-started,3), **data))
        events.append(row)
        with (args.output / args.team / 'events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False)+'\n')
        print(json.dumps({k:v for k,v in row.items() if k not in ('content','args','result','messages')},ensure_ascii=False),flush=True)

    class CourseLLM(BaseLLM):
        def __init__(self, agent_id):
            super().__init__(model=cfg.model, temperature=0.2)
            self.agent_id=agent_id
        def supports_function_calling(self): return False
        def supports_stop_words(self): return False
        def get_context_window_size(self): return 65536
        def call(self, messages, tools=None, callbacks=None, available_functions=None, **kwargs):
            if time.monotonic() >= deadline: raise TimeoutError('Team wall deadline reached')
            if isinstance(messages,str): messages=[dict(role='user',content=messages)]
            begin=time.monotonic()
            event('model_start',agent=self.agent_id,messages=messages,requestChars=sum(len(str(m.get('content',''))) for m in messages))
            try:
                result=native_stop_complete(messages) if args.native_stop else client.complete(messages,max_tokens=cfg.max_completion_tokens,deadline=deadline)
                event('model_result',agent=self.agent_id,seconds=round(time.monotonic()-begin,3),**result)
                content=result['content']
                if args.react_boundary and '\nObservation:' in content and 'Action:' in content.split('\nObservation:',1)[0] and 'Final Answer:' not in content.split('\nObservation:',1)[0]:
                    content=content.split('\nObservation:',1)[0].rstrip()
                    event('react_boundary',agent=self.agent_id,reason='Drop model-invented tool observations; execute the first actual tool call through CrewAI instead.')
                return content
            except Exception as exc:
                event('model_error',agent=self.agent_id,seconds=round(time.monotonic()-begin,3),error=str(exc),errorType=type(exc).__name__,transport=getattr(exc,'transport',None))
                raise

    class Input(BaseModel):
        operation: str = Field(description='Exact tool name from the documented allowlist, or publish, or ask_user')
        arguments: dict = Field(default_factory=dict, description='Exact arguments object matching the operation schema')

    assumptions = {
        'software': 'SIMULATED USER ASSUMPTIONS (not original requirements): a small internal team; three rooms A/B/C; Asia/Hong_Kong local time; browser-only localStorage demo is acceptable for this pilot, no multi-user server requirement. Preserve cancellation records, reject overlapping and past bookings, use real records for the dashboard. Explain demo limitations. Do not push to any remote.',
        'newsroom': 'SIMULATED USER ASSUMPTIONS (not original requirements): English university audience; choose a recent verifiable HKUST-campus AI story yourself; online article plus concise social/newsletter adaptations. Public primary sources only, no invented interviews or quotes. Detector fact-check must distinguish independent evidence from vendor claims and explain remaining uncertainty.',
        'fund': 'SIMULATED USER ASSUMPTIONS (not original requirements): educational simulation only; 5-year horizon, moderate risk, no leverage, keep an emergency reserve separate. Explain currency and drawdown risks. Semiconductor HTML slides are for fund colleagues; public primary sources; end-2026 is an analytical scenario, never invent future observed facts. Current date is 2026-11-23.'
    }

    def make_tool(agent_id):
        definitions=gateway.definitions(agent_id)
        allowed={d['name'] for d in definitions}
        # Remote publication/destruction is unnecessary for this experiment.
        allowed -= {'git.push','file.delete'}
        description='Execute ONE real tool. Use exact schemas below. No shell access. '+json.dumps([d for d in definitions if d['name'] in allowed],ensure_ascii=False)
        description+=' Additional operations: publish {"paths":["relative/path"]} copies explicit immutable snapshots to team members under handoff/'+agent_id+'/<snapshot-id>/; ask_user {"question":"..."} returns an operator-authorized simulated-user answer with recorded assumptions.'
        class GatewayTool(BaseTool):
            name: str = 'gateway'
            description: str = ''
            args_schema: type[BaseModel] = Input
            def _run(self, operation: str, arguments: dict):
                begin=time.monotonic()
                if time.monotonic()>=deadline: raise TimeoutError('Team wall deadline reached')
                try:
                    if operation=='ask_user':
                        result={'ok':True,'question':arguments.get('question'),'answer':assumptions[args.team], 'source':'operator_authorized_simulated_user'}
                    elif operation=='publish':
                        snapshot=uuid.uuid4().hex[:8]
                        receipts=[]
                        for path in arguments['paths']:
                            source=gateway.execute(agent_id,run_id,'file.read',{'path':path},deadline=deadline)
                            # Full bytes must be read from the confined source, not a truncated observation.
                            from backend.sandbox import read_text
                            text=read_text(gateway.workspace(agent_id,run_id),path)
                            for peer in registry:
                                if peer!=agent_id:
                                    target=f'handoff/{agent_id}/{snapshot}/{path}'
                                    write_text(gateway.workspace(peer,run_id),target,text,overwrite=False)
                                    receipts.append(dict(receiver=peer,path=target,sha256=hashlib.sha256(text.encode()).hexdigest()))
                        result={'ok':True,'receipts':receipts}
                    elif operation not in allowed:
                        result={'ok':False,'error':'Operation not allowed for this role'}
                    else:
                        result=gateway.execute(agent_id,run_id,operation,arguments,deadline=deadline)
                        if result.get('needsApproval') and operation in ('file.write','file.patch'):
                            bound=result['args']
                            event('simulated_approval',agent=agent_id,operation=operation,args=bound,reason='local experiment file revision only')
                            result=gateway.execute(agent_id,run_id,operation,bound,approved=True,deadline=deadline)
                    event('tool_result',agent=agent_id,operation=operation,args=arguments,result=result,seconds=round(time.monotonic()-begin,3))
                    return json.dumps(result,ensure_ascii=False)
                except Exception as exc:
                    event('tool_error',agent=agent_id,operation=operation,args=arguments,error=str(exc),seconds=round(time.monotonic()-begin,3))
                    return json.dumps({'ok':False,'error':str(exc)})
        return GatewayTool(description=description)

    agents={aid:Agent(role=registry[aid]['name'],goal='Complete your responsibility for the original user request with real tools and evidence.',backstory=prompt,
                      llm=CourseLLM(aid),tools=[make_tool(aid)],allow_delegation=False,max_iter=12,max_retry_limit=0,verbose=False)
            for aid,prompt in team['roles'].items()}
    instructions={
        'software':['Clarify assumptions and define a concise implementable specification. Hand off to developer. Do not implement yourself.',
                    'Implement the requested working product using file.write. Read previous-task files if extending. Publish all runtime files explicitly for QA and release. Deliver complete runnable files, not code in prose.',
                    'Independently inspect published files and execute real browser/code tests with gateway code.test. Explicitly state passed/failed/not tested. Do not claim a test you did not run. Publish a QA report.',
                    'Read the actual published implementation and QA results. Write release instructions and final delivery report. Identify unresolved issues. Never claim passing acceptance without evidence.'],
        'newsroom':['Clarify audience and establish editorial direction; choose a feasible angle. Hand off for research.',
                    'Research using real web.search AND web.fetch, record exact sources and supported claims. Save and publish a research file.',
                    'Independently check the research, fetch primary sources and flag unsupported assertions. Save and publish a fact-check report.',
                    'Produce actual publishable article files and channel adaptations using file.write, following research and fact-check findings. Cite fetched sources. Publish final files.'],
        'fund':['Clarify objectives and set the research mandate and decision criteria. Hand off for research.',
                'Research using real web.search AND web.fetch. Develop evidence-backed analysis relevant to the original request. For slides provide researched content. Save and publish research.',
                'Independently review sources, calculations, risks and assumptions. Save and publish a risk report with limitations.',
                'Create the actual requested deliverable files: an understandable investment proposal, or runnable HTML slide presentation with navigation. Use file.write and publish. Explain assumptions and uncertainties.']}
    report={'team':args.team,'runId':run_id,'framework':'crewai','version':version('crewai'),'modelRequested':cfg.model,'transportMode':cfg.transport_mode,'cases':[],
            'assumptions':assumptions[args.team],'memoryEnabled':False,'maxIterationsPerRole':12,'deadlineSeconds':args.seconds,'directNetwork':args.direct,'reactBoundary':args.react_boundary,'nativeStop':args.native_stop}
    previous=''
    event('team_start',model=cfg.model,frameworkVersion=version('crewai'))
    for item in team['tasks']:
        case_id=item['id']; begin=time.monotonic()
        row={'caseId':case_id,'sourceSha256':item['sha256'],'prompt':item['text'],'status':'running','qualityVerdict':'unassessed'}
        report['cases'].append(row); save(args.output / args.team / 'report.json',report)
        event('case_start')
        try:
            tasks=[]
            for index,(aid,agent) in enumerate(agents.items()):
                desc=(f"ORIGINAL USER REQUEST:\n{item['text']}\n\nYOUR STAGE:\n{instructions[args.team][index]}\n"
                      'Today is 2026-11-23, Asia/Hong_Kong. Use tools for files, research and execution. Tool error is not success. '
                      'Use ask_user for clarification. Local experimental revisions are approved and logged; remote pushes are unavailable. '
                      'Other agents cannot access your private files unless you publish named paths. Read file.list then handoff subdirectories for published sources. '
                      'Keep execution focused; publish once ready and provide the exact handoff paths.\n'
                      f'PREVIOUS TASK CONTEXT (same team, same private workspaces):\n{previous}')
                task=Task(description=desc,expected_output='Evidence-based stage result with actual file paths, publication receipts and explicit unresolved issues.',agent=agent,context=list(tasks))
                tasks.append(task)
            crew=Crew(agents=list(agents.values()),tasks=tasks,process=Process.sequential,memory=False,cache=False,verbose=False,tracing=False)
            result=crew.kickoff()
            previous=str(result)
            row.update(status='crew_returned',output=previous,stages=[dict(agent=t.agent,raw=t.raw) for t in result.tasks_output])
        except Exception as exc:
            row.update(status='error',error=cfg.scrub(str(exc)),errorType=type(exc).__name__)
        row['elapsedSeconds']=round(time.monotonic()-begin,3)
        row['modelCalls']=sum(e['kind']=='model_start' and e['caseId']==case_id for e in events)
        row['toolCalls']=sum(e['kind']=='tool_result' and e['caseId']==case_id for e in events)
        report['elapsedSeconds']=round(time.monotonic()-started,3)
        save(args.output / args.team / 'report.json',report)
        event('case_end',status=row['status'],seconds=row['elapsedSeconds'])
    manifest=[]
    for path in cfg.data_dir.rglob('*'):
        if path.is_file() and not path.is_symlink():
            manifest.append({'path':str(path.relative_to(args.output)),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    save(args.output / args.team / 'manifest.json',manifest)

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--suite',type=Path,default=ROOT/'fixtures/mid_test')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=900)
    p.add_argument('--team',choices=['software','newsroom','fund'])
    p.add_argument('--transport',choices=['standard','compatible'])
    p.add_argument('--direct',action='store_true')
    p.add_argument('--react-boundary',action='store_true')
    p.add_argument('--native-stop',action='store_true')
    args=p.parse_args(); args.output=args.output.resolve(); args.output.mkdir(parents=True,exist_ok=True)
    if args.team:
        (args.output/args.team).mkdir(exist_ok=True)
        worker(args); return
    started=time.monotonic(); jobs=[]
    for team in ('software','newsroom','fund'):
        folder=args.output/team; folder.mkdir(exist_ok=True)
        log=(folder/'console.log').open('w')
        cmd=[sys.executable,__file__,'--team',team,'--suite',str(args.suite),'--output',str(args.output),'--seconds',str(args.seconds)]
        if args.transport: cmd+=['--transport',args.transport]
        if args.direct: cmd+=['--direct']
        if args.react_boundary: cmd+=['--react-boundary']
        if args.native_stop: cmd+=['--native-stop']
        jobs.append((team,subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT),log))
    while any(proc.poll() is None for _,proc,_ in jobs) and time.monotonic()-started < args.seconds+15:
        time.sleep(1)
    for team,proc,log in jobs:
        if proc.poll() is None: proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        log.close()
    cases=[]
    for team,proc,_ in jobs:
        report_path=args.output/team/'report.json'
        report=json.loads(report_path.read_text()) if report_path.is_file() else {}
        existing={c['caseId']:c for c in report.get('cases',[])}
        for n in (1,2):
            row=existing.get(f'{team}-{n}',{'caseId':f'{team}-{n}','status':'not_started','qualityVerdict':'unassessed'})
            if row['status']=='running': row['status']='interrupted_at_deadline'
            cases.append(row)
        manifest=[]
        for path in (args.output/team/'runtime').rglob('*'):
            if path.is_file() and not path.is_symlink():
                manifest.append({'path':str(path.relative_to(args.output)),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        save(args.output/team/'manifest.json',manifest)
    save(args.output/'summary.json',{'elapsedSeconds':round(time.monotonic()-started,3),'teamConcurrency':3,'withinTeamTasks':'sequential','cases':cases,
         'deadlineSeconds':args.seconds,'exits':{team:proc.returncode for team,proc,_ in jobs},
         'comparisonLimit':'Pilot changes orchestration and concurrency, not a controlled framework-only speedup measurement. Historical baseline is contextual only.',
         'qualityVerdict':'unassessed; Crew return is not independent acceptance'})

if __name__=='__main__': main()
