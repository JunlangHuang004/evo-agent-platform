import { AGENTS, AVAILABLE_PROFILES } from './data.js';
import { getState, subscribe, setTeam, createTeam, setChannel, setParticipants, setMode, setMemoryEnabled, sendMessage, resetChannel, pauseRun, resumeRun, cancelRun, recoverRun, retryTask, resolveApproval, refreshHistory, loadHistoryRun, setRolePrompts } from './store.js';
import { escapeHtml as h, formatDuration, badge, openDrawer, closeDrawer, downloadArtifact, isHtmlArtifact, runToMarkdown, failureSummaryHtml, taskOutputHtml } from './view.js';

const ACTIVE=['running','waiting_human','waiting_children','paused','interrupted'];
const drafts=new Map(),approvalDrafts=new Map();
let tab='tasks',messageKey='',approvalsKey='',rosterKey='',teamsKey='',displayedDraftKey='',displayedTeam='',historyLoad=0;
const session=()=>getState().sessions[getState().activeChannel] || {messages:[],run:null};
const draftKey=(team=getState().teamId,channel=getState().activeChannel)=>`${team}:${channel}`;
const agentName=id=>id==='human'?'你':AGENTS.find(agent=>agent.id===id)?.name || id || '待分配';
const initials=id=>(AGENTS.find(agent=>agent.id===id)?.name || id || 'Agent').split(/\s+/).slice(0,2).map(word=>word[0]).join('').toUpperCase();
const text=value=>typeof value==='string'?value:JSON.stringify(value ?? '',null,2);
const brief=value=>text(value).slice(0,240);
const stamp=value=>typeof value==='number'?value:Date.parse(value);
const elapsed=run=>run?Math.max(0,(run.finishedAt?stamp(run.finishedAt):Date.now())-stamp(run.startedAt)) || 0:0;
const LIVE_TASK=new Set(['running','assigned','retrying','waiting_children']);
const DONE_TASK=new Set(['done','partial','failed','cancelled','skipped','rejected']);
function mergedIntervals(intervals){
  const rows=intervals.filter(([start,end])=>Number.isFinite(start)&&Number.isFinite(end)&&end>start).sort((left,right)=>left[0]-right[0]);
  const merged=[];
  for(const row of rows){
    const last=merged.at(-1);
    if(!last || row[0]>last[1])merged.push([row[0],row[1]]);
    else last[1]=Math.max(last[1],row[1]);
  }
  return merged;
}
function overlapMs(start,end,intervals){
  let total=0;
  for(const [from,to] of intervals){
    const left=Math.max(start,from),right=Math.min(end,to);
    if(right>left)total+=right-left;
  }
  return total;
}
function humanWaitIntervals(run,task,now){
  const intervals=[];
  for(const approval of run?.approvals || []){
    if(approval?.taskId!==task.id)continue;
    const start=stamp(approval.createdAt);
    if(!Number.isFinite(start))continue;
    let end=approval.status==='pending'?now:stamp(approval.resolvedAt);
    if(!Number.isFinite(end))end=stamp(approval.clarificationReceipt?.receivedAt);
    if(!Number.isFinite(end))end=stamp(approval.lateResponse?.receivedAt);
    if(!Number.isFinite(end) && approval.status==='expired')end=stamp(approval.expiredAt ?? approval.expiresAt);
    if(!Number.isFinite(end)){
      const times=(run.events || []).filter(event=>event?.payload?.approvalId===approval.id).map(event=>stamp(event.time)).filter(Number.isFinite);
      if(times.length)end=Math.max(...times);
    }
    if(Number.isFinite(end) && end>start)intervals.push([start,end]);
  }
  return mergedIntervals(intervals);
}
export function taskTiming(run,task,now=Date.now()){
  const idle={text:'未开始',live:false,started:'',finished:'',wait:'0'};
  if(!run || !task || task.status==='pending')return idle;
  const marks=[];
  const push=value=>{const time=stamp(value);if(Number.isFinite(time))marks.push(time);};
  push(task.startedAt);
  for(const event of run.events || [])if(event?.payload?.taskId===task.id)push(event.time);
  for(const call of run.calls || []){
    if(call?.taskId!==task.id)continue;
    push(call.requestStartedAt);
    if(Number.isFinite(call.requestStartedAt) && Number.isFinite(call.durationMs))push(call.requestStartedAt+call.durationMs);
  }
  const started=Number.isFinite(task.startedAt)?task.startedAt:(marks.length?Math.min(...marks):null);
  if(!Number.isFinite(started))return idle;
  let finished=Number.isFinite(task.finishedAt)?task.finishedAt:null;
  if(finished==null && DONE_TASK.has(task.status) && marks.length)finished=Math.max(...marks);
  const waiting=task.status==='waiting_human';
  const live=LIVE_TASK.has(task.status) && !Number.isFinite(finished);
  const end=Number.isFinite(finished)?finished:(live || waiting?now:started);
  const wait=overlapMs(started,end,humanWaitIntervals(run,task,now));
  return {text:formatDuration(Math.max(0,end-started-wait)),live,started:String(started),finished:Number.isFinite(finished)?String(finished):'',wait:String(wait)};
}
function taskClockHtml(run,task){
  const timing=taskTiming(run,task);
  return `<time class="task-clock" data-task-clock data-live="${timing.live?'1':'0'}" data-started="${h(timing.started)}" data-wait="${h(timing.wait)}" data-finished="${h(timing.finished)}">${h(timing.text)}</time>`;
}
function tickTaskClocks(now=Date.now()){
  for(const node of document.querySelectorAll('[data-task-clock][data-live="1"]')){
    const started=Number(node.dataset.started),wait=Number(node.dataset.wait)||0;
    if(!Number.isFinite(started))continue;
    node.textContent=formatDuration(Math.max(0,now-started-wait));
  }
}
function timeText(value){const date=new Date(value);return Number.isNaN(date.getTime())?'':date.toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function findMemory(hit){const owner=hit.ownerAgentId || hit.agentId;return [...(AGENTS.find(agent=>agent.id===owner)?.memories || []),...(getState().learnedMemories[owner] || [])].find(memory=>memory.id===hit.memoryId);}
function remainingWallTime(run,now=Date.now()){
  const deadline=run?.budget?.deadlineAt;
  if(Number.isFinite(deadline))return Math.max(0,deadline-now);
  return Number.isFinite(run?.remainingMs)?Math.max(0,run.remainingMs):null;
}
export function runProgressText(run,now=Date.now()){
  if(!run)return '';
  const base=`${(run.steps || []).filter(step=>step.status==='done').length}/${run.steps?.length || 0} 步骤完成 · ${run.calls?.length || 0} 次调用`;
  if(run.budget?.profile!=='time_only')return base;
  const remaining=remainingWallTime(run,now),limit=value=>value===null?'不限':Number.isFinite(value)?String(value):'未记录';
  return `${base} · Token ${run.tokenUsage ?? 0}/${limit(run.budget.tokens)} · 模型调用 ${run.modelCalls ?? 0}/${limit(run.budget.modelCalls)} · 墙钟剩余 ${remaining===null?'未记录':formatDuration(remaining)}（含等待与暂停）`;
}
export function canRetryTask(run,task,now=Date.now()){
  if(!run || !task || !['partial','failed'].includes(run.status) || !['failed','partial'].includes(task.status) || task.outputs?.length)return false;
  if(Number.isFinite(task.progress?.turnLimit) && task.turns>=task.progress.turnLimit)return false;
  if(run.budget?.profile==='time_only' && remainingWallTime(run,now)===0)return false;
  return true;
}

export function mountWorkbench(){
  const options=document.getElementById('participantOptions'),input=document.getElementById('messageInput');
  options.addEventListener('change',()=>{if(!setParticipants([...options.querySelectorAll('input:checked')].map(element=>element.value)))renderParticipants();});
  document.getElementById('teamSelect').addEventListener('change',event=>{drafts.set(draftKey(),input.value);void setTeam(event.target.value);});
  const createTeamForm=document.getElementById('createTeamForm'),createTeamName=document.getElementById('createTeamName'),createTeamError=document.getElementById('createTeamError');
  document.getElementById('createTeam').addEventListener('click',()=>{createTeamError.textContent='';createTeamForm.hidden=false;createTeamName.focus();});
  document.getElementById('cancelCreateTeam').addEventListener('click',()=>{createTeamForm.hidden=true;createTeamName.value='';createTeamError.textContent='';});
  createTeamForm.addEventListener('submit',async event=>{
    event.preventDefault();
    const name=createTeamName.value.trim();
    if(!name){createTeamError.textContent='请填写团队名称。';return;}
    createTeamError.textContent='正在创建…';
    try{
      const team=await createTeam(name);
      createTeamForm.hidden=true;createTeamName.value='';createTeamError.textContent='';
      drafts.set(draftKey(),input.value);
      await setTeam(team.id);
    }catch(error){createTeamError.textContent=error.message || '新建团队失败。';}
  });
  document.getElementById('channelSelect').addEventListener('change',event=>{drafts.set(draftKey(),input.value);setChannel(event.target.value);});
  document.addEventListener('click',event=>{const menu=document.getElementById('participantsControl');if(menu.open && !menu.contains(event.target))menu.open=false;});
  document.getElementById('participantsControl').addEventListener('keydown',event=>{if(event.key==='Escape'){event.currentTarget.open=false;event.currentTarget.querySelector('summary').focus();}});
  document.getElementById('modeSelect').addEventListener('change',event=>setMode(event.target.value));
  document.getElementById('memoryEnabled').addEventListener('change',event=>setMemoryEnabled(event.target.checked));
  input.addEventListener('input',()=>drafts.set(draftKey(),input.value));
  document.getElementById('messageForm').addEventListener('submit',event=>{event.preventDefault();void submit();});
  input.addEventListener('keydown',event=>{if(event.key==='Enter' && !event.shiftKey && !event.isComposing){event.preventDefault();void submit();}});
  document.getElementById('resetConversation').addEventListener('click',async()=>{const team=getState().teamId,channel=getState().activeChannel;if(await resetChannel(channel)){drafts.delete(draftKey(team,channel));if(draftKey()===draftKey(team,channel))input.value='';}});
  document.getElementById('pauseRun').addEventListener('click',()=>{const status=session().run?.status;void(status==='paused'?resumeRun():status==='interrupted'?recoverRun():pauseRun());});
  document.getElementById('cancelRun').addEventListener('click',()=>{void cancelRun();});
  document.getElementById('refreshHistory').addEventListener('click',()=>{void refreshHistory();});
  document.getElementById('historyRunSelect').addEventListener('change',()=>{document.getElementById('openHistoryRun').disabled=!document.getElementById('historyRunSelect').value;});
  document.getElementById('openHistoryRun').addEventListener('click',async()=>{
    const id=document.getElementById('historyRunSelect').value;if(!id)return;
    const ticket=++historyLoad,run=await loadHistoryRun(id);if(!run || ticket!==historyLoad)return;
    openDrawer('历史任务 · '+(run.title || run.id),historyHtml(run));
  });
  document.getElementById('dialogBody').addEventListener('click',async event=>{
    const button=event.target.closest('[data-retry-task]');if(!button)return;
    if(await continueTask(button))closeDrawer();
  });
  document.getElementById('dialogBody').addEventListener('click',event=>{
    const button=event.target.closest('[data-export-history]'),state=getState(),run=state.history.selected;if(!button || !run)return;
    const json=button.dataset.exportHistory==='json';downloadArtifact({name:`${state.teamId}-${run.id}.${json?'json':'md'}`,content:json?JSON.stringify(run,null,2):runToMarkdown(run,state.teamName),mimeType:json?'application/json;charset=utf-8':'text/markdown;charset=utf-8'});
  });
  const tabs=document.getElementById('inspectorTabs');
  tabs.addEventListener('click',event=>{const button=event.target.closest('[data-inspector-tab]');if(button)selectTab(button.dataset.inspectorTab);});
  tabs.addEventListener('keydown',event=>{const buttons=[...tabs.querySelectorAll('[role=tab]')],index=buttons.indexOf(event.target);if(index<0 || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;selectTab(buttons[next].dataset.inspectorTab);buttons[next].focus();});
  document.getElementById('approvalArea').addEventListener('input',event=>{const id=event.target.dataset.approvalInput;if(!id)return;const key=`${getState().teamId}:${id}`,value=approvalDrafts.get(key)||{};value[event.target.dataset.field]=event.target.value;approvalDrafts.set(key,value);});
  document.getElementById('approvalArea').addEventListener('click',event=>{const button=event.target.closest('[data-approval-choice]');if(button)void replyToApproval(button);});
  document.getElementById('inspectorContent').addEventListener('click',async event=>{
    const retryButton=event.target.closest('[data-retry-task]');
    if(retryButton){event.preventDefault();await continueTask(retryButton);return;}
    const run=session().run;if(!run)return;
    const callButton=event.target.closest('[data-call]'),taskButton=event.target.closest('[data-task]'),artifactButton=event.target.closest('[data-artifact]'),memoryButton=event.target.closest('[data-memory-hit]');
    if(callButton){const call=run.calls?.[Number(callButton.dataset.call)];if(call)openDrawer(`调用 · ${call.tool}`,`<p>${badge(call.status)}</p><dl class="detail-list"><dt>任务 / Agent</dt><dd>${h(call.taskId)} / ${h(agentName(call.agentId))}</dd><dt>耗时 / 沙箱</dt><dd>${h(formatDuration(call.durationMs))} / ${h(call.sandboxId || '服务网关')}</dd></dl><h3>参数</h3><pre>${h(text(call.args))}</pre><h3>结构化结果</h3><pre>${h(text(call.result))}</pre>`);}
    if(taskButton){const task=run.steps?.find(item=>item.id===taskButton.dataset.task);if(task)openTask(task);}
    if(artifactButton){const artifact=run.artifacts?.find(item=>item.id===artifactButton.dataset.artifact);if(artifact){if(artifactButton.dataset.download || isHtmlArtifact(artifact))downloadArtifact(artifact);else openDrawer(`产物 · ${artifact.name}`,`<pre>${h(text(artifact.content))}</pre>`);}}
    if(memoryButton){const hit=run.memoryHits?.[Number(memoryButton.dataset.memoryHit)];if(hit){const memory=findMemory(hit);openDrawer('本次记忆',`<h3>${h(memory?.title || hit.memoryId)}</h3><p>${h(memory?.lesson || hit.reason || '')}</p><dl class="detail-list"><dt>归属 / 本次采用者</dt><dd>${h(agentName(hit.ownerAgentId || hit.agentId))} / ${h(agentName(hit.agentId))}</dd><dt>来源 / 关联任务</dt><dd>${h(memory?.sourceTaskId || '')} / ${h(hit.taskId)}</dd><dt>采用状态</dt><dd>${hit.applied?'已采用':'仅检索'}</dd><dt>依据</dt><dd>${h(text(memory?.evidence || hit.reason))}</dd></dl><p class="text-small muted">记忆参与执行不等于效果提升；效果须由独立对照验证。</p>`);}}
  });
  const unsubscribe=subscribe(render);render();
  const rolePromptsFields=document.getElementById('rolePromptsFields');
  rolePromptsFields.addEventListener('input',event=>{
    const agent=event.target.dataset.rolePromptAgent;if(!agent)return;
    writeEdit(rolePromptKey(getState().teamId,agent),'prompt',event.target.value);
    renderRolePrompts();
  });
  rolePromptsFields.addEventListener('change',event=>{
    const agent=event.target.dataset.rolePromptProfile;if(!agent)return;
    const boxes=[...rolePromptsFields.querySelectorAll(`[data-role-prompt-profile="${CSS.escape(agent)}"]`)];
    const selected=boxes.filter(box=>box.checked).map(box=>box.value);
    if(!selected.length){event.target.checked=true;rolePromptsStatus='每个 Agent 至少保留一个能力 profile。';rolePromptsStatusTeam=getState().teamId;renderRolePrompts();return;}
    writeEdit(rolePromptKey(getState().teamId,agent),'profile',selected);
    const container=document.getElementById('rolePromptsFields');container.dataset.renderKey='';
    renderRolePrompts();
  });
  document.getElementById('revertRolePrompts').addEventListener('click',()=>{
    const team=getState().teamId;for(const key of [...rolePromptEdits.keys()])if(key.startsWith(team+':'))rolePromptEdits.delete(key);
    const container=document.getElementById('rolePromptsFields');container.dataset.renderKey='';
    rolePromptsStatus='已撤销未保存的修改。';rolePromptsStatusTeam=team;renderRolePrompts();
  });
  document.getElementById('saveRolePrompts').addEventListener('click',async()=>{
    const state=getState(),team=state.teamId;
    const customizable=AGENTS.filter(agent=>agent.rolePromptCustomizable);
    if(!customizable.length)return;
    const payload={};let anyEdit=false;
    for(const agent of customizable){
      const key=rolePromptKey(team,agent.id);
      const edit=readEdit(key);
      if(!('prompt' in edit) && !('profile' in edit))continue;
      const entry={};
      if('prompt' in edit)entry.prompt=edit.prompt;
      if('profile' in edit)entry.profile=edit.profile;
      payload[agent.id]=entry;anyEdit=true;
    }
    if(!anyEdit){rolePromptsStatus='没有未保存的修改。';rolePromptsStatusTeam=team;renderRolePrompts();return;}
    const ok=await setRolePrompts(payload);
    if(ok){for(const agent of customizable)rolePromptEdits.delete(rolePromptKey(team,agent.id));rolePromptsStatus='已保存角色提示词与 profile；工具权限仍由后端 profile 表控制。';}
    else rolePromptsStatus=getState().client.error||'保存失败，请查看终端。';
    rolePromptsStatusTeam=team;
    const container=document.getElementById('rolePromptsFields');container.dataset.renderKey='';
    renderRolePrompts();
  });
  const timer=setInterval(()=>{const run=session().run;document.getElementById('runElapsed').textContent=run?formatDuration(elapsed(run)):'';document.getElementById('runProgress').textContent=runProgressText(run);tickTaskClocks();},1000);
  return()=>{unsubscribe();clearInterval(timer);};
}
async function submit(){
  const state=getState();if(state.client.pendingAction || ACTIVE.includes(session().run?.status))return;
  const input=document.getElementById('messageInput'),original=input.value,channel=state.activeChannel,key=draftKey();if(!original.trim())return;
  if(await sendMessage(original,channel)){if(drafts.get(key)===original || !drafts.has(key))drafts.delete(key);if(draftKey()===key && input.value===original)input.value='';}
}
function selectTab(id){tab=id;document.querySelectorAll('[data-inspector-tab]').forEach(button=>{const selected=button.dataset.inspectorTab===id;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});document.getElementById('inspectorContent').setAttribute('aria-labelledby',`tab-${id}`);renderInspector();}
function renderTeams(){
  const state=getState(),picker=document.getElementById('teamSelect'),next=JSON.stringify(state.teams);
  if(teamsKey!==next){picker.innerHTML=state.teams.length?state.teams.map(team=>`<option value="${h(team.id)}">${h(team.name)}</option>`).join(''):'<option value="default">连接后加载团队…</option>';teamsKey=next;}
  picker.value=state.teamId;picker.disabled=Boolean(state.client.pendingAction)||!state.teams.length;
  const createButton=document.getElementById('createTeam');if(createButton)createButton.disabled=Boolean(state.client.pendingAction)||!state.client.reachable;
  document.getElementById('teamDescription').textContent=(state.teamDescription?state.teamDescription+' ':'')+'同组后续任务保留上下文；不同团队的数据分别保存。';
  const nextRoster=JSON.stringify(AGENTS.map(agent=>[agent.id,agent.name,agent.label]));
  if(rosterKey!==nextRoster){
    document.getElementById('channelSelect').innerHTML='<option value="group">群聊 · 协作团队</option>'+AGENTS.map(agent=>`<option value="${h(agent.id)}">私聊 · ${h(agent.name)}</option>`).join('');
    document.getElementById('participantOptions').innerHTML=AGENTS.map(agent=>`<label class="participant-option"><input type="checkbox" name="participants" value="${h(agent.id)}"><span><strong>${h(agent.name)}</strong><small>${h(agent.label || agent.role)}</small></span></label>`).join('');rosterKey=nextRoster;
  }
  if(displayedTeam && displayedTeam!==state.teamId){closeDrawer();historyLoad+=1;}
  displayedTeam=state.teamId;
  const key=draftKey();if(key!==displayedDraftKey){document.getElementById('messageInput').value=drafts.get(key)||'';displayedDraftKey=key;}
  const history=state.history,past=document.getElementById('historyRunSelect'),previous=past.value;
  const html=history.runs.length?history.runs.map(run=>`<option value="${h(run.id)}">${h(run.title || run.id)} · ${h(run.status)}</option>`).join(''):'<option value="">暂无历史任务</option>';
  if(past.innerHTML!==html){past.innerHTML=html;if(history.runs.some(run=>run.id===previous))past.value=previous;}
  document.getElementById('openHistoryRun').disabled=!past.value || history.loading;
  document.getElementById('refreshHistory').disabled=history.loading || !state.client.reachable;
  document.getElementById('historyError').textContent=history.error || (history.loading?'正在读取本团队历史…':'查看以往任务的对话、产物和调用；导出 JSON / Markdown 供复核。');
  renderRolePrompts();
}
let rolePromptEdits=new Map(),rolePromptsStatus='',rolePromptsStatusTeam='';
function rolePromptKey(teamId,agentId){return `${teamId}:${agentId}`;}
function rolePromptsBusy(state){return Object.values(state.sessions || {}).some(session=>ACTIVE.includes(session?.run?.status));}
function readEdit(key){const current=rolePromptEdits.get(key);return current && typeof current==='object'?current:{};}
function writeEdit(key,field,value){const current=readEdit(key),next={...current,[field]:value};rolePromptEdits.set(key,next);}
function hasPendingEdit(team){return [...rolePromptEdits.entries()].some(([key,value])=>key.startsWith(team+':') && value && (('prompt' in value) || ('profile' in value)));}
function renderRolePrompts(){
  const state=getState(),control=document.getElementById('rolePromptsControl');if(!control)return;
  const customizable=AGENTS.filter(agent=>agent.rolePromptCustomizable);
  if(!customizable.length){control.hidden=true;control.open=false;return;}
  control.hidden=false;
  const injectedCount=customizable.filter(agent=>agent.rolePromptInjected).length;
  document.getElementById('rolePromptsBadge').textContent=`已注入 ${injectedCount}/${customizable.length}`;
  const container=document.getElementById('rolePromptsFields');
  const profileSig=AVAILABLE_PROFILES.map(p=>[p.id,p.label,p.description,p.tools.length]);
  const nextKey=JSON.stringify([state.teamId,customizable.map(a=>[a.id,a.name,a.rolePromptInjected,a.rolePrompt,a.profileIds||a.profileId]),profileSig]);
  if(container.dataset.renderKey!==nextKey){
    container.dataset.renderKey=nextKey;
    const chosen=agent=>{const edit=readEdit(rolePromptKey(state.teamId,agent.id));const raw=edit.profile!==undefined?edit.profile:(agent.profileIds||(agent.profileId?[agent.profileId]:[]));const ids=Array.isArray(raw)?raw:[raw];return ids.filter(id=>AVAILABLE_PROFILES.some(profile=>profile.id===id));};
    const unionTools=ids=>{const tools=[];for(const id of ids){const profile=AVAILABLE_PROFILES.find(item=>item.id===id);for(const tool of profile?.tools||[])if(!tools.includes(tool))tools.push(tool);}return tools;};
    container.innerHTML=customizable.map(agent=>{
      const stored=agent.rolePromptInjected?(agent.rolePrompt||''):'';
      const edit=readEdit(rolePromptKey(state.teamId,agent.id));
      const promptValue=edit.prompt!==undefined?edit.prompt:stored;
      const profileIds=chosen(agent);
      const toolsHint=unionTools(profileIds).join(', ');
      const injectedClass=agent.rolePromptInjected?' is-injected':'';
      const options=AVAILABLE_PROFILES.map(profile=>`<label><input type="checkbox" data-role-prompt-profile="${h(agent.id)}" value="${h(profile.id)}"${profileIds.includes(profile.id)?' checked':''}>${h(profile.label)}</label>`).join('');
      return `<div class="role-prompt-field${injectedClass}" data-agent="${h(agent.id)}">
        <label for="rolePrompt-${h(agent.id)}">${h(agent.name)} · ${h(agent.id)}</label>
        <div class="role-prompt-profile"><span class="text-small muted">能力 profile（可多选，工具取并集）</span>
          <div class="profile-options">${options || '<span class="text-small muted">等待后端加载 profile</span>'}</div></div>
        <p class="role-prompt-tools text-small muted">工具白名单：<code>${h(toolsHint||'（未知）')}</code></p>
        <textarea id="rolePrompt-${h(agent.id)}" data-role-prompt-agent="${h(agent.id)}" rows="6" placeholder="粘贴该 Agent 的角色提示词；留空即恢复占位。">${h(promptValue)}</textarea>
      </div>`;
    }).join('');
  }
  const status=document.getElementById('rolePromptsStatus');
  if(rolePromptsStatusTeam===state.teamId && rolePromptsStatus) status.textContent=rolePromptsStatus;
  else{status.textContent=rolePromptsBusy(state)?'任务进行中，暂不能修改。保存将在空闲时才被接受。':'每个 Agent 可勾选多个能力 profile，工具和能力取并集。再粘贴角色提示词。保存后立即生效。';rolePromptsStatus='';}
  const pending=Boolean(state.client.pendingAction);
  document.getElementById('saveRolePrompts').disabled=pending || rolePromptsBusy(state);
  document.getElementById('revertRolePrompts').disabled=pending || !hasPendingEdit(state.teamId);
}
function render(){
  const state=getState(),current=session(),run=current.run,channel=state.activeChannel,pending=Boolean(state.client.pendingAction),busy=ACTIVE.includes(run?.status),ready=state.client.reachable;
  renderTeams();renderParticipants();document.getElementById('channelSelect').value=channel;
  const mode=busy?run.mode:state.channelModes[channel];
  document.getElementById('modeSelect').value=mode || (channel==='group'?'task':'chat');document.getElementById('modeSelect').disabled=pending || busy;
  document.getElementById('memoryEnabled').checked=busy?run.memoryEnabled!==false:state.memoryEnabled;document.getElementById('memoryEnabled').disabled=pending || busy;
  document.getElementById('channelPrivacy').textContent=channel==='group'?'任务由所选团队协作；普通聊天由当前回复 Agent 处理。':'私聊仅由当前 Agent 处理，可选择聊天或单 Agent 工具任务。';
  document.getElementById('conversationTitle').textContent=channel==='group'?state.teamName+' · 群聊':'私聊 · '+agentName(channel);
  const participants=busy?(run.participants || []):state.selectedAgentIds;
  document.getElementById('conversationHint').textContent=channel==='group'?`${mode==='task'?'任务协作':'普通聊天'} · ${participants.map(agentName).join(' · ')}`:`${agentName(channel)} · ${mode==='task'?'单 Agent 执行任务':'普通聊天'}`;
  document.getElementById('sendMessage').disabled=!ready || !state.connection.configured || pending || busy;
  document.getElementById('sendMessage').textContent=pending && state.client.pendingAction==='send'?'提交中…':'发送 ↗';
  document.getElementById('messageInput').disabled=Boolean(pending && state.client.pendingChannel===channel) || busy;
  document.getElementById('resetConversation').disabled=!ready || pending;
  document.getElementById('composerHint').textContent=!ready?'后端未连接。':!state.connection.configured?'请先配置模型 API。':pending?'正在提交操作…':run?.status==='waiting_human'?'请回复上方人工请求。':run?.status==='paused'?'任务已暂停，可继续或取消。':run?.status==='interrupted'?'任务已中断，可恢复或取消。':busy?'任务执行中，可查看步骤与调用记录。':'Enter 发送 · Shift + Enter 换行';
  const nextKey=JSON.stringify([state.teamId,channel,current.messages]);
  if(nextKey!==messageKey){const list=document.getElementById('messageList'),follow=list.scrollHeight-list.scrollTop-list.clientHeight<120 || !messageKey;list.innerHTML=current.messages?.length?current.messages.map(renderMessage).join(''):`<div class="empty-state"><div class="empty-mark" aria-hidden="true">${channel==='group'?'↗':h(initials(channel))}</div><h3>开始${mode==='task'?'执行任务':'聊天'}</h3><p>${mode==='task'?'描述目标与验收条件。后续任务继续在同一团队提交。':'输入消息，与当前 Agent 进行普通对话。'}</p></div>`;if(follow)list.scrollTop=list.scrollHeight;messageKey=nextKey;}
  renderApprovals(run);
  document.getElementById('runMeta').textContent=run?`${run.id} · ${run.mode==='task'?'任务执行':'普通聊天'}`:'等待新任务';
  document.getElementById('runStatus').className=run?'':'tag muted';document.getElementById('runStatus').innerHTML=run?badge(run.status):'待开始';
  document.getElementById('runTitle').textContent=run?.title || '发送任务后，查看步骤、执行记录和成果。';
  const members=document.getElementById('runParticipants');members.hidden=!run;members.textContent=run?`本任务成员：${(run.participants || []).map(agentName).join(' · ')}`:'';
  document.getElementById('runElapsed').textContent=run?formatDuration(elapsed(run)):'';
  document.getElementById('runProgress').textContent=runProgressText(run);
  const issue=document.getElementById('runIssue'),issueHtml=failureSummaryHtml(run);issue.hidden=!issueHtml;if(issue.innerHTML!==issueHtml)issue.innerHTML=issueHtml;
  const pause=document.getElementById('pauseRun');pause.disabled=!ready || pending || !busy;pause.textContent=run?.status==='paused'?'继续':run?.status==='interrupted'?'恢复':'暂停';
  document.getElementById('cancelRun').disabled=!ready || pending || !busy;renderInspector();
}
function renderParticipants(){const state=getState(),control=document.getElementById('participantsControl');control.hidden=state.activeChannel!=='group';if(control.hidden)control.open=false;const locked=Boolean(state.client.pendingAction)||!state.client.reachable||ACTIVE.includes(state.sessions.group?.run?.status);const fieldset=document.getElementById('participantOptions');fieldset.disabled=locked;fieldset.querySelectorAll('input[name=participants]').forEach(input=>{input.checked=state.selectedAgentIds.includes(input.value);input.disabled=locked || (input.checked && state.selectedAgentIds.length===1);});document.getElementById('participantCount').textContent=`${state.selectedAgentIds.length}/${AGENTS.length}`;document.getElementById('participantHelp').textContent=locked?'执行期间成员锁定；请等待后端连接或当前任务结束。':'至少选择 1 个 Agent；只影响下次群聊任务。';}
function renderMessage(message){if(message.role==='system')return `<div class="message system">${h(message.text)}</div>`;const human=['human','user'].includes(message.role);return `<article class="message ${human?'human':'agent'}"><div class="avatar" aria-hidden="true">${human?'你':h(initials(message.agentId))}</div><div class="message-body"><div class="message-heading"><strong>${human?'你':h(agentName(message.agentId))}</strong><time>${h(timeText(message.time))}</time></div><p class="message-text">${h(message.text)}</p></div></article>`;}
function canReplyToApproval(approval,run){
  if(!approval || !ACTIVE.includes(run?.status))return false;
  if(approval.type==='clarification')return approval.canRespond===true && ['pending','expired'].includes(approval.status);
  return approval.status==='pending' && (!approval.expiresAt || stamp(approval.expiresAt)>Date.now());
}
function approvalTimingHtml(approval){
  const clarification=approval.type==='clarification',label=clarification?'原答复时间':'有效期至';
  const time=approval.expiresAt?`<p class="text-small muted">${label} ${h(new Date(approval.expiresAt).toLocaleString('zh-CN'))}</p>`:'';
  return time+(clarification && approval.canRespond===true && approval.acceptsLateResponse===true?'<p class="text-small muted">原答复时间已过，仍可补充真实澄清。迟答不是操作授权；若任务已暂停或中断，请手动继续或恢复。</p>':'');
}
function approvalResponse(approval){const late=approval.type==='clarification' && approval.lateResponse?.late===true,response=late?approval.lateResponse:approval.response || {},label=(approval.choices || []).find(choice=>choice.id===response.choiceId)?.label || response.choiceId || '尚未回答';return `<p class="text-small muted">回答${late?'（已记录迟答）':''}：${h(label)}</p>${response.answer?`<p class="message-text">${h(response.answer)}</p>`:''}${response.modifiedArgs?`<pre>${h(text(response.modifiedArgs))}</pre>`:''}`;}
function dialogueHtml(run){
  return `<section class="dialogue-section"><h3>Agent 间消息</h3>${(run.messages || []).map(message=>`<article class="dialogue-entry"><div class="text-small muted">${h(agentName(message.fromAgent))} → ${h(agentName(message.toAgent))} · ${h(timeText(message.time))}</div><p class="message-text">${h(message.text)}</p></article>`).join('') || '<p class="text-small muted">暂无成员交接消息。</p>'}</section><section class="dialogue-section"><h3>人工请求与回答</h3>${(run.approvals || []).map(approval=>`<article class="dialogue-entry"><div class="item-topline"><strong>${h(approval.title)}</strong>${badge(approval.status)}</div><p class="message-text">${h(approval.description)}</p>${approvalResponse(approval)}</article>`).join('') || '<p class="text-small muted">暂无人工请求。</p>'}</section>`;
}
function historyHtml(run){
  return `${deliveryHtml(run)}<p>${badge(run.status)} · ${h(run.id)}</p>${failureSummaryHtml(run)}<div class="export-actions"><button type="button" class="button small" data-export-history="json">导出 JSON</button><button type="button" class="button small" data-export-history="md">导出 Markdown</button></div><h3>本次对话</h3>${(run.conversation || []).map(message=>`<article class="dialogue-entry"><strong>${h(agentName(message.agentId || message.role))}</strong><p class="message-text">${h(message.text || message.content)}</p></article>`).join('') || '<p class="text-small muted">旧任务未保存独立对话，完整记录仍可导出。</p>'}${dialogueHtml(run)}<details><summary>子任务成果检查</summary>${(run.steps || []).map(task=>`<article class="dialogue-entry"><strong>${h(task.title)}</strong>${taskOutputHtml(task)}</article>`).join('') || '<p class="text-small muted">暂无子任务。</p>'}</details><details><summary>完整执行记录（含产物、工具和模型动作）</summary><pre>${h(text(run))}</pre></details>`;
}
function renderApprovals(run){
  const host=document.getElementById('approvalArea'),pending=ACTIVE.includes(run?.status)?(run.approvals || []).filter(approval=>approval.type==='clarification'?canReplyToApproval(approval,run):approval.status==='pending'):[],team=getState().teamId,nextKey=JSON.stringify([team,getState().activeChannel,run?.id,run?.status,pending]);
  if(nextKey!==approvalsKey){
    host.innerHTML=pending.map(approval=>{const draft=approvalDrafts.get(`${team}:${approval.id}`)||{},permission=approval.type==='permission',labels={permission:'操作审批',clarification:'需求澄清',budget:'资源预算',reconciliation:'状态核对',protocol_repair:'格式修复'},choices=(approval.choices || []).filter(choice=>choice.id!=='modify');return `<section class="approval-card" data-approval-card="${h(approval.id)}"><div class="item-topline"><h3>${h(approval.title)}</h3><span class="tag warning">${h(labels[approval.type] || approval.type)}</span></div><p>${h(approval.description)}</p>${approvalTimingHtml(approval)}${permission?`<details class="approval-parameters"><summary>操作参数与修改</summary><label class="text-small" for="args-${h(approval.id)}">修改后的参数（JSON）</label><textarea id="args-${h(approval.id)}" data-approval-input="${h(approval.id)}" data-field="args" rows="5">${h(draft.args ?? text(approval.args || {}))}</textarea><button type="button" class="button quiet small" data-approval="${h(approval.id)}" data-approval-choice="modify">提交修改并重新审批</button><p class="text-small muted">修改参数会生成新的审批请求，不会立即执行。</p></details>`:''}<label class="sr-only" for="answer-${h(approval.id)}">回复或补充说明</label><textarea id="answer-${h(approval.id)}" data-approval-input="${h(approval.id)}" data-field="answer" rows="2" placeholder="${permission?'补充说明（可选）':'输入答复…'}">${h(draft.answer || '')}</textarea><div class="approval-actions">${choices.map((choice,index)=>`<button type="button" class="button ${choice.id==='deny'?'quiet':index===0?'primary':'quiet'}" data-approval="${h(approval.id)}" data-approval-choice="${h(choice.id)}">${h(choice.label)}</button>`).join('')}${approval.type==='clarification'?`<button type="button" class="button primary" data-approval="${h(approval.id)}" data-approval-choice="answer">发送答复</button>`:''}</div><p class="approval-error" data-approval-error></p></section>`;}).join('');approvalsKey=nextKey;
  }
  for(const card of host.querySelectorAll('[data-approval-card]')){const approval=pending.find(item=>item.id===card.dataset.approvalCard);card.querySelectorAll('button,textarea').forEach(input=>input.disabled=Boolean(getState().client.pendingAction)||!getState().client.reachable||!canReplyToApproval(approval,run));}
}
async function replyToApproval(button){
  const channel=getState().activeChannel,id=button.dataset.approval,key=`${getState().teamId}:${id}`,run=session().run,approval=run?.approvals?.find(item=>item.id===id && canReplyToApproval(item,run));if(!approval)return;
  const card=button.closest('[data-approval-card]'),error=card.querySelector('[data-approval-error]');error.textContent='';
  const answer=card.querySelector('[data-field="answer"]').value.trim();let modifiedArgs;
  if(button.dataset.approvalChoice==='answer' && !answer){error.textContent='请先输入答复。';return;}
  if(button.dataset.approvalChoice==='modify'){try{modifiedArgs=JSON.parse(card.querySelector('[data-field="args"]').value);if(!modifiedArgs || typeof modifiedArgs!=='object' || Array.isArray(modifiedArgs))throw new Error();}catch{error.textContent='参数必须是有效的 JSON 对象。';return;}}
  if(await resolveApproval(id,button.dataset.approvalChoice,answer,modifiedArgs,channel))approvalDrafts.delete(key);
}
async function continueTask(button){
  button.disabled=true;
  const ok=await retryTask(button.dataset.retryTask,button.dataset.retryRun);
  if(!ok)button.disabled=false;
  return ok;
}
function retryTaskHtml(task){
  const run=session().run;
  if(!canRetryTask(run,task))return '';
  return `<p><button type="button" class="button small" data-retry-task="${h(task.id)}" data-retry-run="${h(run.id)}">从这里继续</button></p><p class="text-small muted">从这一步接着做。失败记录、已完成步骤和错误经验都保留，预算不增加。</p>`;
}
function openTask(task){openDrawer(`子任务 · ${task.title}`,`${retryTaskHtml(task)}<p>${badge(task.status)}</p><dl class="detail-list"><dt>编号 / 负责人</dt><dd>${h(task.id)} / ${h(agentName(task.agentId))}</dd><dt>派单者</dt><dd>${h(agentName(task.delegatedBy))}</dd><dt>依赖</dt><dd>${h((task.dependsOn || []).join('、') || '无')}</dd><dt>尝试 / 重试</dt><dd>${h(task.attempts || 0)} / ${h(task.retries || 0)}</dd><dt>已用轮数 / 上限</dt><dd>${h(task.turns ?? 0)} / ${task.progress?.turnLimit===null?'不限':h(task.progress?.turnLimit ?? '未记录')}</dd></dl><h3>目标与验收条件</h3><pre>${h(text({goal:task.goal,acceptanceCriteria:task.acceptanceCriteria}))}</pre>${taskOutputHtml(task)}<h3>输入 / 输出</h3><pre>${h(text({inputs:task.inputs,outputs:task.outputs}))}</pre><h3>结果与错误</h3><pre>${h(text({result:task.result,error:task.error,feedback:task.feedback}))}</pre><h3>独立验收</h3><pre>${h(text(task.validation || '尚无独立验收记录'))}</pre>`);}
function deliveryHtml(run){
  const team=run.teamId || getState().teamId;
  return (run.deliveries || []).map(delivery=>`<section class="panel"><div class="panel-body"><strong>${delivery.status==='candidate'?'候选产品包 · 非正式交付':'产品交付包'}</strong><p class="text-small">入口：${h(delivery.entrypoint)} · ${delivery.files?.length || 0} 个文件</p><p class="text-small muted">${delivery.status==='candidate'?'这是供检查的候选文件快照；实际审核状态请查看任务记录。':'已打包；是否通过验收请查看任务记录。'}包内包含文件清单及运行说明。</p><a class="button small" download="delivery.zip" href="/api/runs/${encodeURIComponent(run.id)}/deliveries/${encodeURIComponent(delivery.id)}?team=${encodeURIComponent(team)}">${delivery.status==='candidate'?'下载候选包':'下载产品包'}</a></div></section>`).join('');
}
function artifactHtml(artifact){const html=isHtmlArtifact(artifact);return `<div class="artifact-row">${html?`<span class="text-small">${h(artifact.name)} · HTML 仅下载</span>`:`<button type="button" class="button quiet small" data-artifact="${h(artifact.id)}">${h(artifact.name)}</button>`}<button type="button" class="button small" data-artifact="${h(artifact.id)}" data-download="true">下载</button></div>`;}
function renderInspector(){
  const host=document.getElementById('inspectorContent'),run=session().run;let html='';const eventsOpen=Boolean(host.querySelector('[data-events]')?.open);
  if(!run)html='<div class="empty-state"><p>开始任务后查看依赖、负责人、调用、记忆与产物。</p></div>';
  else if(tab==='dialogue')html=dialogueHtml(run);
  else if(tab==='calls')html=(run.calls || []).length?`<div class="record-list">${run.calls.map((call,index)=>`<button type="button" class="record-button" data-call="${index}"><div class="item-topline"><code>${h(call.tool)}</code>${badge(call.status)}</div><div class="record-meta"><span>${h(agentName(call.agentId))}</span><span>${h(formatDuration(call.durationMs))}</span></div><p>${h(brief(call.result || '等待返回'))}</p></button>`).join('')}</div>`:'<div class="empty-state"><p>暂无调用记录。</p></div>';
  else if(tab==='memory')html=(run.memoryHits || []).length?`<div class="record-list">${run.memoryHits.map((hit,index)=>`<button type="button" class="record-button" data-memory-hit="${index}"><div class="item-topline"><strong>${h(findMemory(hit)?.title || hit.memoryId)}</strong><span class="tag ${hit.applied?'info':'muted'}">${hit.applied?'已采用':'仅检索'}</span></div><div class="record-meta">${h(agentName(hit.agentId))} · ${h(hit.taskId)}</div><p>${h(hit.reason || '')}</p></button>`).join('')}</div><p class="text-small muted">使用记忆不代表已证明效果提升。</p>`:`<div class="empty-state"><p>${run.memoryEnabled===false?'本任务已关闭记忆。':'当前任务暂无记忆检索记录。'}</p></div>`;
  else html=`<ol class="task-list">${(run.steps || []).map(task=>`<li class="task-row"><button type="button" class="task-detail-button" data-task="${h(task.id)}"><span class="item-topline"><strong>${h(task.title)}</strong><span class="task-topline-status">${badge(task.status)}${taskClockHtml(run,task)}</span></span><span class="task-meta">${h(agentName(task.agentId))} · 派单：${h(agentName(task.delegatedBy))}</span><span class="task-meta">依赖：${h((task.dependsOn || []).map(id=>run.steps.find(step=>step.id===id)?.title || id).join('、') || '无')} · 输出 ${task.outputs?.length || 0} 项</span>${task.error?`<span class="task-error">${h(brief(task.error))}</span>`:''}</button>${canRetryTask(run,task)?`<button type="button" class="button small task-retry" data-retry-task="${h(task.id)}" data-retry-run="${h(run.id)}">从这里继续</button>`:''}</li>`).join('') || '<li class="text-small muted">正在建立任务计划…</li>'}</ol><section class="artifact-section">${deliveryHtml(run)}<h3>产物</h3>${(run.artifacts || []).map(artifactHtml).join('') || '<p class="text-small muted">暂无文本产物。</p>'}</section><details data-events ${eventsOpen?'open':''}><summary>执行事件 · ${run.events?.length || 0} 条</summary><ol class="event-list">${(run.events || []).map(event=>`<li><span class="muted">${h(timeText(event.time))}</span> <code>${h(event.type)}</code><p>${h(event.message)}</p></li>`).join('')}</ol></details>`;
  if(host.innerHTML!==html){const scrollTop=host.scrollTop;host.innerHTML=html;host.scrollTop=scrollTop;}
}
