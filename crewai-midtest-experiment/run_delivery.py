"""Isolated CrewAI Mid-Test pilot. Course API and real existing tool gateway only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from context_budget import bound_messages
from deepseek_protocol import normalize_dsml
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
    cfg = Config(args.config_root)
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
    report_path=args.output / args.team / 'report.json'
    restored=json.loads(report_path.read_text()) if args.resume and report_path.exists() else None
    if restored and (restored.get('provider',cfg.provider)!=cfg.provider or restored.get('modelRequested')!=cfg.model):
        raise ValueError('Cannot resume another provider/model run; select a fresh output directory.')
    run_id = restored['runId'] if restored else 'crew-' + uuid.uuid4().hex[:16]
    ledger_path=args.output / args.team / 'events.jsonl'
    prior_elapsed=max(0,time.time()-ledger_path.stat().st_birthtime) if restored and ledger_path.exists() else 0
    started = time.monotonic()-prior_elapsed
    # A user-requested continuation gets a new bounded execution allowance;
    # preserve the original measurement clock separately from this allowance.
    deadline = time.monotonic() + args.seconds
    case_id = None
    events = [json.loads(line) for line in ledger_path.read_text().splitlines()] if restored and ledger_path.exists() else []
    import requests
    session=requests.Session()
    session.trust_env=False

    def native_stop_complete(messages, use_stop=True):
        body={'model':cfg.model,'messages':messages,'temperature':0.2,'max_tokens':cfg.max_completion_tokens,'stream':False,'stop':['\nObservation:']}
        if not use_stop: body.pop('stop')
        if cfg.provider=='deepseek': body['thinking']={'type':'disabled'}
        elif cfg.thinking_mode=='disabled': body['chat_template_kwargs']={'enable_thinking':False}
        response=session.post(cfg.base_url+'/chat/completions',json=body,headers={'Authorization':'Bearer '+cfg.api_key},timeout=(8,min(45,max(1,deadline-time.monotonic()))),allow_redirects=False)
        response.raise_for_status()
        if response.status_code!=200: raise ValueError('Unexpected API response status')
        if len(response.content)>2_000_000: raise ValueError('Response size limit exceeded')
        value=response.json()
        choice=value['choices'][0]
        return cfg.scrub(dict(content=choice['message']['content'],usage=value.get('usage',{}),model=value.get('model'),finish_reason=choice.get('finish_reason'),transport={'transportMode':'standard','direct':args.direct,'nativeStop':['\nObservation:'],'provider':cfg.provider,'thinking':body.get('thinking',body.get('chat_template_kwargs'))}))

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
        def supports_stop_words(self): return True
        def get_context_window_size(self): return 65536
        def call(self, messages, tools=None, callbacks=None, available_functions=None, **kwargs):
            if time.monotonic() >= deadline: raise TimeoutError('Team wall deadline reached')
            if isinstance(messages,str): messages=[dict(role='user',content=messages)]
            original_chars=sum(len(str(m.get('content',''))) for m in messages)
            if original_chars>120000:
                messages=bound_messages(messages)
                event('context_compacted',agent=self.agent_id,beforeChars=original_chars,afterChars=sum(len(m['content']) for m in messages))
            begin=time.monotonic()
            capability=registry[self.agent_id]['primaryCapability']
            cfg.max_completion_tokens=2000 if capability=='planning' else 3000 if capability=='research' else 6000
            event('model_start',agent=self.agent_id,messages=messages,requestChars=sum(len(str(m.get('content',''))) for m in messages),maxTokens=cfg.max_completion_tokens)
            try:
                empty_seen=False
                for attempt in range(1,4):
                    try:
                        event('api_attempt',agent=self.agent_id,attempt=attempt)
                        result=native_stop_complete(messages,use_stop=not (empty_seen and attempt==3))
                        if cfg.provider=='deepseek' and isinstance(result.get('content'),str) and 'DSML' in result['content']:
                            event('provider_wire_response',agent=self.agent_id,result=result)
                            try: normalized=normalize_dsml(result['content'])
                            except ValueError:
                                messages=messages+[{'role':'user','content':'The last tool proposal could not be parsed. Return ONE compact complete ReAct action: Thought, Action: gateway, Action Input: valid JSON with operation and arguments. Do not emit DSML. Keep specifications short and split large files into smaller complete operations.'}]
                                raise
                            event('provider_protocol_normalized',agent=self.agent_id,protocol='DSML_to_ReAct',content=normalized)
                            result={**result,'content':normalized}
                        if not isinstance(result.get('content'),str) or not result['content'].strip():
                            empty_seen=True
                            event('empty_model_response',agent=self.agent_id,attempt=attempt,result=result)
                            messages=messages+[{'role':'user','content':'Your previous response had no usable content. Return a real Thought and ONE Action with valid JSON, or Final Answer. Do not start with Observation; only the tool runner supplies observations.'}]
                            raise ValueError('Empty model content at native stop boundary')
                        break
                    except Exception as exc:
                        code=getattr(getattr(exc,'response',None),'status_code',None)
                        retryable=isinstance(exc,(requests.Timeout,requests.ConnectionError)) or code in (429,500,502,503,504) or empty_seen and isinstance(exc,ValueError) or cfg.provider=='deepseek' and isinstance(exc,ValueError)
                        event('api_attempt_error',agent=self.agent_id,attempt=attempt,errorType=type(exc).__name__,httpStatus=code,seconds=round(time.monotonic()-begin,3),retryable=retryable,usageUnknown=not isinstance(exc,requests.ConnectTimeout))
                        if attempt==3 or not retryable or time.monotonic()+3>=deadline: raise
                        time.sleep(attempt)
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
                    view=result
                    if operation=='web.fetch':
                        view=dict(result)
                        if isinstance(view.get('links'),list): view['links']=view['links'][:8];view['linksTruncated']=len(result['links'])>8
                        if len(view.get('content',''))>6000: view['content']=view['content'][:6000];view['truncated']=True
                        view['modelViewNote']='Full original receipt is retained in the event ledger; use sourceSnapshot to read omitted content.'
                    return json.dumps(view,ensure_ascii=False)
                except Exception as exc:
                    event('tool_error',agent=agent_id,operation=operation,args=arguments,error=str(exc),seconds=round(time.monotonic()-begin,3))
                    return json.dumps({'ok':False,'error':str(exc)})
        return GatewayTool(description=description)

    agents={aid:Agent(role=registry[aid]['name'],goal='Complete your responsibility for the original user request with real tools and evidence.',backstory=prompt,
                      llm=CourseLLM(aid),tools=[make_tool(aid)],allow_delegation=False,max_iter=20,max_retry_limit=0,verbose=False)
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
    report={'team':args.team,'runId':run_id,'framework':'crewai','version':version('crewai'),'provider':cfg.provider,'modelRequested':cfg.model,'transportMode':cfg.transport_mode,'configSourceFile':str(cfg.root/'.env'),'cases':[],
            'assumptions':assumptions[args.team],'memoryEnabled':False,'maxIterationsPerRole':20,'deadlineSeconds':args.seconds,'directNetwork':args.direct,'reactBoundary':args.react_boundary,'nativeStop':args.native_stop,'transportClient':'requests.Session trust_env=False; connect=8s read=45s; 3 generation attempts; 3 stage attempts; planning=1500 others=6000 completion tokens'}
    if restored: report=restored
    previous=''
    event('team_start',model=cfg.model,frameworkVersion=version('crewai'))
    for item in team['tasks']:
        case_id=item['id']
        existing=next((c for c in report['cases'] if c['caseId']==case_id),None)
        if existing and existing['status']=='crew_returned':
            previous=existing.get('output','')[:12000]
            continue
        old_starts=[e['elapsedSeconds'] for e in events if e['kind']=='case_start' and e.get('caseId')==case_id]
        begin=started+min(old_starts) if old_starts else time.monotonic()
        row=existing or {'caseId':case_id,'sourceSha256':item['sha256'],'prompt':item['text'],'status':'running','qualityVerdict':'unassessed'}
        row['status']='running'
        if not existing: report['cases'].append(row)
        save(args.output / args.team / 'report.json',report)
        event('case_start')
        try:
            tasks=[]
            for index,(aid,agent) in enumerate(agents.items()):
                desc=(f"ORIGINAL USER REQUEST:\n{item['text']}\n\nYOUR STAGE:\n{instructions[args.team][index]}\n"
                      'Today is 2026-11-23, Asia/Hong_Kong. Use tools for files, research and execution. Tool error is not success. '
                      'Use ask_user for clarification. Local experimental revisions are approved and logged; remote pushes are unavailable. '
                      'Other agents cannot access your private files unless you publish named paths. Your workspace may contain checkpoint files; use file.list if needed but do not inspect nonexistent handoff or imports folders. Use exact published paths from previous stage reports. Planning role: ask_user once if needed, then give a SHORT specification under 500 words; do not spend turns browsing empty directories or writing lengthy specifications. Research role: use focused primary sources, save findings and hand off promptly instead of repeatedly expanding the research scope. '
                      'Keep execution focused; publish once ready and provide the exact handoff paths.\n'
                      f'PREVIOUS TASK CONTEXT (same team, same private workspaces):\n{previous}')
                task=Task(description=desc,expected_output='Evidence-based stage result with actual file paths, publication receipts and explicit unresolved issues.',agent=agent,context=list(tasks))
                tasks.append(task)
            stages=list(row.get('stages',[]))
            for task_index,task in enumerate(tasks):
                if task_index<len(stages): continue
                if args.team=='software' and task_index==2:
                    # Checkpoint publication is a host action, not a claimed
                    # agent action. Preserve exact bytes and real peer receipts.
                    base=gateway.workspace('dev',run_id)
                    paths=[p for p in ('app/index.html','app/app.js','app/styles.css','app/README.md') if (base/p).is_file() and not (base/p).is_symlink()]
                    if paths:
                        event('runtime_checkpoint_publication',agent='dev',paths=paths,reason='Publish completed private runtime before independent QA; do not let a missing final handoff hide existing code.')
                        published=json.loads(agents['dev'].tools[0]._run('publish',{'paths':paths}))
                        if not published.get('ok'): raise ValueError('Runtime checkpoint publication failed')
                        stages[1]['raw']+='\nHOST CHECKPOINT PUBLICATION (actual current files; supersedes older runtime handoffs):\n'+json.dumps(published,ensure_ascii=False)
                task.context=None
                task.description+='\nCOMPLETED STAGES AND EXACT HANDOFF PATHS:\n'+json.dumps(stages,ensure_ascii=False)
                if restored:
                    receipts=[e for e in events if e['kind']=='tool_result' and e.get('caseId')==case_id and e.get('operation')=='web.fetch' and e.get('result',{}).get('ok')]
                    notes=[{k:r['result'].get(k) for k in ('url','title','sourceSnapshot')} | {'excerpt':r['result'].get('content','')[:600]} for r in receipts[-10:]]
                    task.description+='\nALREADY FETCHED SOURCE RECEIPTS FROM THIS CASE (excerpts are incomplete; full snapshots remain available):\n'+json.dumps(notes,ensure_ascii=False)
                    task.description+='\nThis is a resumed stage. Inspect existing private files and finish the requested deliverable; do not restart the research from scratch.'
                if args.team=='fund' and task_index==1:
                    task.description+='\nDELIVERY BOUNDARY: You must SAVE and PUBLISH a concise research report, not merely describe future research. Use already-fetched source receipts where relevant and limit further background searches. Do not loop on tax/fee facts you cannot verify; disclose missing evidence. For the investment proposal, develop a concrete simulated allocation summing to 100%, rationale, risk/currency assumptions and verified sources. For semiconductors, provide evidence-backed judgments, scenarios, risks and investment implications for the HTML deck. Target a report under 1200 words and hand off promptly.'
                if case_id=='fund-2' and task_index==3:
                    task.description=(f'CURRENT ORIGINAL REQUEST:\n{item["text"]}\n'
                      'Create a NEW, compact, self-contained HTML slide deck at marketing/semiconductor_outlook_2026.html. '
                      'This task is about the SEMICONDUCTOR INDUSTRY OUTLOOK, not the prior personal HKD 1,000,000 investment plan. '
                      'Do NOT reuse marketing/slides.html: that is the wrong-topic personal investment introduction. '
                      'Use 8-10 concise slides, minimal inline CSS/JS, and keep the entire HTML under 11000 characters so file.write fits in one model response. '
                      'Include industry drivers, AI/datacenter demand, foundry/packaging or memory, scenarios, investment implications, risks, uncertainties and source links. '
                      'Use actual researched evidence below; distinguish reported estimates from observations and end-2026 scenarios. '
                      'Title or main heading must clearly identify semiconductors. Implement Prev/Next and ArrowRight/ArrowLeft navigation. '
                      'Write the actual file now, publish its exact path, then give a short delivery report. Do not browse old handoff directories; the relevant source material is supplied below.\n'
                      'RESEARCH STAGE OUTPUT (may contain an unexecuted draft file proposal; treat it as draft analysis, not proof of a saved file):\n'+stages[1]['raw']+
                      '\nRISK REVIEW:\n'+stages[2]['raw'])
                if case_id=='software-2' and task_index==1:
                    task.description+='\nREWORK REQUIREMENT: The previous attempt only wrote specifications; the dashboard was NOT implemented. You MUST modify actual runnable HTML/JavaScript now. Start by reading app/app.js and app/index.html or their current published equivalents, then implement the selected-date-range dashboard: per-room booking counts, total reserved hours, busiest days/times, cancellation frequency/rate, clear interactive charts from actual booking records. Preserve existing booking/cancellation behavior. Do not spend your turn budget rereading planning/release reports; make focused code changes and publish the runtime files. A specification-only response will be rejected.'
                if args.feedback and case_id=='software-2':
                    task.description+='\nCURRENT INDEPENDENT QA REWORK FEEDBACK (supersedes generic assumptions about missing code; current implementation exists):\n'+args.feedback.read_text(encoding='utf-8')
                crew=Crew(agents=[task.agent],tasks=[task],process=Process.sequential,memory=False,cache=False,verbose=False,tracing=False)
                for stage_attempt in range(1,4):
                    try:
                        stage_event_start=len(events)
                        result=crew.kickoff()
                        stage_text=str(result)
                        # CrewAI may return a pending Action as its forced final
                        # answer at max_iter. It is not an executed tool receipt.
                        # Recover at most one explicitly proposed local artifact
                        # action, through exactly the same permission gateway.
                        import re
                        pending=re.search(r'(?m)^Action:\s*gateway\s*\nAction Input:\s*',stage_text)
                        if pending and 'Final Answer:' not in stage_text[:pending.start()] and '```' not in stage_text[:pending.start()]:
                            try: proposed,_=json.JSONDecoder().raw_decode(stage_text[pending.end():].lstrip())
                            except ValueError: proposed={}
                            op=proposed.get('operation');arguments=proposed.get('arguments')
                            if op in ('file.write','file.patch','publish') and isinstance(arguments,dict):
                                aid=list(agents)[task_index]
                                event('terminal_action_recovery',agent=aid,operation=op,proposalSha256=hashlib.sha256(stage_text.encode()).hexdigest(),reason='Pending model action returned at Crew iteration boundary; not previously executed.')
                                receipt=json.loads(task.agent.tools[0]._run(op,arguments))
                                if not receipt.get('ok'): raise ValueError('Recovered artifact proposal failed: '+str(receipt.get('error')))
                                receipts=[receipt]
                                if op in ('file.write','file.patch'):
                                    receipts.append(json.loads(task.agent.tools[0]._run('publish',{'paths':[arguments['path']]})))
                                stage_text='Runtime recovered one pending local artifact action through the real gateway. Actual receipts: '+json.dumps(receipts,ensure_ascii=False)
                        if case_id=='software-2' and task_index==1:
                            writes=[e for e in events[stage_event_start:] if e['kind']=='tool_result' and e.get('operation') in ('file.write','file.patch') and e.get('result',{}).get('ok') and str(e.get('args',{}).get('path','')).endswith(('.html','.js'))]
                            if not writes:
                                task.description+='\nRejected prior stage attempt: no actual HTML/JS code was changed. You must implement and publish runnable dashboard code, not another plan.'
                                raise ValueError('Developer stage has no actual HTML/JavaScript writes for dashboard implementation')
                        if case_id=='fund-2' and task_index==3:
                            from backend.sandbox import read_text
                            html=read_text(gateway.workspace('marketing',run_id),'marketing/semiconductor_outlook_2026.html')
                            headings=' '.join(re.findall(r'<(?:title|h1)[^>]*>(.*?)</(?:title|h1)>',html,re.S|re.I))
                            if not re.search(r'semiconductor|chip industry|半导体',headings,re.I):
                                raise ValueError('Final HTML title/main heading does not address the semiconductor industry request')
                        break
                    except Exception as exc:
                        event('stage_attempt_error',agent=task.agent.role,attempt=stage_attempt,errorType=type(exc).__name__,error=cfg.scrub(str(exc)))
                        if stage_attempt==3 or time.monotonic()+5>=deadline: raise
                        time.sleep(2)
                stages.append({'agent':task.agent.role,'raw':stage_text})
                row['stages']=stages
                save(args.output / args.team / 'report.json',report)
                event('stage_end',agent=task.agent.role,stageNumber=len(stages))
            previous=stage_text[:12000]
            row.update(status='crew_returned',output=stage_text,stages=stages)
            row.pop('error',None);row.pop('errorType',None)
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
    p.add_argument('--config-root',type=Path,default=REPO,help='Local directory containing a provider-specific .env; credentials never enter argv.')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=900)
    p.add_argument('--team',choices=['software','newsroom','fund'])
    p.add_argument('--transport',choices=['standard','compatible'])
    p.add_argument('--direct',action='store_true')
    p.add_argument('--react-boundary',action='store_true')
    p.add_argument('--native-stop',action='store_true')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--feedback',type=Path,help='Local case-specific QA feedback for a separately recorded rework attempt.')
    args=p.parse_args(); args.output=args.output.resolve(); args.output.mkdir(parents=True,exist_ok=True)
    from backend.config import Config
    preflight=Config(args.config_root)
    if not preflight.api_key:
        p.error(f'LLM_API_KEY is not configured in {args.config_root / ".env"}; no model requests were sent.')
    if args.team:
        (args.output/args.team).mkdir(exist_ok=True)
        worker(args); return
    execution_started=time.monotonic()
    started=execution_started-(max(0,time.time()-args.output.stat().st_birthtime) if args.resume else 0); jobs=[]
    for team in ('software','newsroom','fund'):
        folder=args.output/team; folder.mkdir(exist_ok=True)
        log=(folder/'console.log').open('w')
        cmd=[sys.executable,__file__,'--team',team,'--suite',str(args.suite),'--config-root',str(args.config_root.resolve()),'--output',str(args.output),'--seconds',str(args.seconds)]
        if args.transport: cmd+=['--transport',args.transport]
        if args.direct: cmd+=['--direct']
        if args.react_boundary: cmd+=['--react-boundary']
        if args.native_stop: cmd+=['--native-stop']
        if args.resume: cmd+=['--resume']
        if args.feedback: cmd+=['--feedback',str(args.feedback.resolve())]
        jobs.append((team,subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT),log))
    while any(proc.poll() is None for _,proc,_ in jobs) and time.monotonic()-execution_started < args.seconds+15:
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
