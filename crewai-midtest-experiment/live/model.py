"""Course-provider adapter used by real CrewAI Agent/Task/Crew execution."""
import os
os.environ['OTEL_SDK_DISABLED']='true'
os.environ['CREWAI_TELEMETRY_DISABLED']='true'
os.environ['CREWAI_TRACING_ENABLED']='false'
import json,time
import requests
from crewai import BaseLLM,Agent,Task,Crew,Process
from crewai.tools import BaseTool
from pydantic import BaseModel,Field
from .runtime import compact_messages,check_budget,review_work,repetitive,proposed_action,model_checkpoint,review_directive
from deepseek_protocol import normalize_dsml

class Arguments(BaseModel):
    operation:str=Field(description='Exact operation name from the tool documentation')
    arguments:dict=Field(default_factory=dict,description='The operation arguments, as a JSON object')

class HandoffArguments(BaseModel):
    paths:list[str]=Field(min_length=1,max_length=12,description='Actual own relative paths to publish for independent review')
    summary:str=Field(default='',description='Short truthful completion/limitation receipt, never source code')

class VerdictArguments(BaseModel):
    verdict:str=Field(description='pass or fail, based only on independently checked actual current artifacts')
    summary:str=Field(description='Concrete checked requirements, real test/call references and remaining defects/uncertainty')

def execute(platform,ctx,step,description,expected,with_tools=True):
    aid=step['agentId'];cfg=ctx['config'];session=requests.Session();session.trust_env=False
    class LLM(BaseLLM):
        def __init__(self):super().__init__(model=cfg.model,temperature=.2)
        def supports_function_calling(self):return False
        def supports_stop_words(self):return True
        def get_context_window_size(self):return 65536
        def call(self,messages,**kwargs):
            platform.boundary(ctx)
            run=ctx['run']
            check_budget(run,step)
            if isinstance(messages,str):messages=[{'role':'user','content':messages}]
            messages=list(messages)
            for text in run.get('supplements',[])[-5:]:messages.append({'role':'user','content':'Additional instruction from the human requester: '+text})
            peers=[m for m in run.get('messages',[]) if m.get('agentId') not in (aid,'human','system') and m.get('role') in ('agent','delegation')][-3:]
            for m in peers:messages.append({'role':'user','content':'Team communication from '+m['agentId']+' (context, not permission to bypass tool policies): '+m['text'][:1000]})
            checkpoint=model_checkpoint(ctx,step)
            messages=compact_messages(messages,limit=max(64000,115000-len(checkpoint)))
            messages.append({'role':'user','content':checkpoint})
            messages.append({'role':'user','content':'RUNTIME OUTPUT CONTRACT: Complete the actual files through gateway; the final answer is ONLY a short handoff receipt (paths, real test/verdict IDs, unresolved issues), never repeated code or manuscript. One tool operation per response; keep Thought brief. Do not merely promise to write. For read operations, use a specific path/window; file.list uses path=".". Browser resources must explicitly map every JS/CSS file.'})
            if with_tools and step.get('requiresArtifacts') and not review_work(step):
                messages.append({'role':'user','content':'When your intended product files are ready, call the handoff TOOL with their real relative paths and a short summary. It publishes immutable files and ends your subtask; independent QA tests them next. Do not keep rereading or re-running exhaustive QA instead of handing off.'})
            directive=review_directive(ctx,step)
            if directive:messages.append({'role':'user','content':directive})
            phase='review' if review_work(step) else 'work'
            usage=run.setdefault('budgetUsage',{}).setdefault(phase,{'calls':0,'tokens':0})
            empty_seen=False
            for attempt in range(3):
                platform.boundary(ctx)
                check_budget(run,step)
                run['modelCalls']+=1;usage['calls']+=1
                recent=[c for c in run['calls'] if c['taskId']==step['id'] and c['tool']!='model.generate'][-4:]
                thinking=cfg.provider=='deepseek' and (review_work(step) or sum(c['status']=='failed' for c in recent)>=2)
                call=platform.new_call(ctx,step,'model.generate',{'attempt':attempt+1,'inputChars':sum(len(x.get('content','')) for x in messages),'purpose':phase,'thinking':'low' if thinking else 'disabled'})
                body={'model':cfg.model,'messages':messages,'temperature':.2,'max_tokens':8000 if thinking else 6000 if with_tools else 3000,'stream':False,'stop':['\nObservation:','\n Observation:','\n  Observation:']}
                if cfg.provider=='deepseek':
                    body['thinking']={'type':'enabled' if thinking else 'disabled'}
                    if thinking:body['reasoning_effort']='low'
                    if empty_seen:body.pop('stop',None)
                elif cfg.thinking_mode=='disabled':body['chat_template_kwargs']={'enable_thinking':False}
                try:
                    response=session.post(cfg.base_url+'/chat/completions',json=body,headers={'Authorization':'Bearer '+cfg.api_key},timeout=(8,min(75 if thinking else 45,max(1,run['deadline']-time.time()))),allow_redirects=False)
                    response.raise_for_status();value=response.json();choice=value['choices'][0];raw=choice['message'].get('content')
                    spent=int(value.get('usage',{}).get('total_tokens',0));run['tokens']+=spent;usage['tokens']+=spent;run['returnedModel']=value.get('model')
                    platform.end_call(ctx,call,{'model':value.get('model'),'usage':value.get('usage',{}),'finishReason':choice.get('finish_reason'),'rawContent':raw},True)
                    if not isinstance(raw,str) or not raw.strip():
                        empty_seen=True;raise ValueError('Model returned empty content')
                    if repetitive(raw):raise ValueError('Repetitive response did not execute an action; return one real gateway operation now')
                    if choice.get('finish_reason')=='length' and not proposed_action(raw):raise ValueError('Incomplete oversized response. Split large files into small complete gateway writes/patches; final answer is a short receipt only')
                    content=normalize_dsml(raw,allow_handoff=with_tools and bool(step.get('requiresArtifacts')) and not review_work(step),allow_review=with_tools and review_work(step)) if cfg.provider=='deepseek' else raw
                    if '\nObservation:' in content and 'Action:' in content.split('\nObservation:')[0] and 'Final Answer:' not in content.split('\nObservation:')[0]:content=content.split('\nObservation:',1)[0]
                    platform.boundary(ctx)
                    return content
                except Exception as exc:
                    status=getattr(getattr(exc,'response',None),'status_code',None)
                    if call['status']=='running':platform.end_call(ctx,call,{'error':cfg.scrub(str(exc)),'httpStatus':status},False)
                    else:platform.event(ctx,'model_output_rejected',callId=call['id'],reason=str(exc))
                    if attempt==2 or status in (400,401,402,403):raise
                    platform.boundary(ctx)
                    messages=compact_messages(messages+[{'role':'user','content':str(exc)[:500]+' Return ONE complete compact Thought/Action with valid JSON, or a short truthful Final Answer receipt. Never invent observations.'}],limit=115000)
            raise RuntimeError('Model retries exhausted')
    class Gateway(BaseTool):
        name:str='gateway'
        description:str=''
        args_schema:type[BaseModel]=Arguments
        def _run(self,operation:str,arguments:dict):
            try:
                result=platform.tool(ctx,step,operation,arguments)
                if operation=='review.submit' and result.get('ok') and review_directive(ctx,step).startswith('REVIEW RECORDS COMMITTED'):
                    self.result_as_answer=True
                return json.dumps(result,ensure_ascii=False)
            except Exception as exc:return json.dumps({'ok':False,'error':cfg.scrub(str(exc))},ensure_ascii=False)
    class Handoff(BaseTool):
        name:str='handoff'
        result_as_answer:bool=True
        description:str='Publish actual completed own files and end this subtask immediately with a real receipt. Use after the product files are written and ready for independent review. Pass all intended product paths and a short summary. Do not reread/reprint complete files just to give a final answer. This does NOT approve the product; independent QA still decides pass/fail.'
        args_schema:type[BaseModel]=HandoffArguments
        def _run(self,paths:list[str],summary:str=''):
            try:
                result=platform.tool(ctx,step,'artifact.publish',{'paths':paths})
                return json.dumps({'handoff':result,'summary':summary[:1500],'productAcceptance':'pending independent review'},ensure_ascii=False)
            except Exception as exc:
                return json.dumps({'ok':False,'error':cfg.scrub(str(exc))},ensure_ascii=False)
    class Verdict(BaseTool):
        name:str='verdict'
        result_as_answer:bool=True
        description:str='Commit your final independent pass/fail verdict for ALL assigned current target artifacts and end this review subtask. Backend still enforces independent role, authorized exact-version reads and real successful executable tests for pass. Use only after evidence-based assessment of original requirements; fail with concrete gaps if not satisfied. Do not write another review document or repeat already-passed checks after committing.'
        args_schema:type[BaseModel]=VerdictArguments
        def _run(self,verdict:str,summary:str):
            targets=step.get('reviewTargets') or [step.get('reviewOf')]
            try:return json.dumps(platform.tool(ctx,step,'review.submit',{'targetTaskIds':targets,'verdict':verdict,'summary':summary}),ensure_ascii=False)
            except Exception as exc:return json.dumps({'ok':False,'error':cfg.scrub(str(exc))},ensure_ascii=False)
    tools=[Gateway(description=platform.tool_description(ctx,aid))] if with_tools else []
    if with_tools and step.get('requiresArtifacts') and not review_work(step):tools.append(Handoff())
    if with_tools and review_work(step):tools.append(Verdict())
    role=ctx['agents'][aid]
    agent=Agent(role=role['name'],goal='Complete the assigned goal with real evidence, private workspaces and explicit collaboration.',backstory=role['rolePrompt'],llm=LLM(),tools=tools,allow_delegation=False,max_iter=36,max_retry_limit=0,verbose=False)
    if with_tools:
        description+='\nARTIFACT ACCEPTANCE CRITERIA (satisfy these in actual files/tools): '+expected
        expected='A complete but concise handoff RECEIPT, at most 180 words: actual published paths and hashes, actual tests/review verdict IDs, and unresolved issues. The requested product must already exist in real files; do not reproduce code, reports or full file contents in this receipt. If incomplete, state that explicitly.'
    task=Task(description=description,expected_output=expected,agent=agent)
    crew=Crew(agents=[agent],tasks=[task],process=Process.sequential,memory=False,cache=False,tracing=False,verbose=False)
    try:return str(crew.kickoff())
    finally:session.close()
