import { AGENTS } from './data.js';
import { getState, subscribe, setChannel, setParticipants, setMode, setMemoryEnabled, sendMessage, resetChannel, pauseRun, resumeRun, cancelRun, recoverRun, resolveApproval } from './store.js';
import { escapeHtml as h, formatDuration, badge, openDrawer, downloadArtifact } from './view.js';

const ACTIVE=['running','waiting_human','paused','interrupted'];
const INITIALS={coordinator:'C',research:'R',executor:'E',reviewer:'V'};
const drafts=new Map();
const approvalDrafts=new Map();
let tab='tasks';let messageKey='';let approvalsKey='';
const session=()=>getState().sessions[getState().activeChannel] || {messages:[],run:null};
const agentName=id=>id==='human'?'你':AGENTS.find(agent=>agent.id===id)?.name || id || '待分配';
const text=value=>typeof value==='string'?value:JSON.stringify(value ?? '',null,2);
const brief=value=>text(value).slice(0,240);
const stamp=value=>typeof value==='number'?value:Date.parse(value);
const elapsed=run=>run?Math.max(0,(run.finishedAt?stamp(run.finishedAt):Date.now())-stamp(run.startedAt)) || 0:0;
function timeText(value){const date=new Date(value);return Number.isNaN(date.getTime())?'':date.toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function findMemory(hit){const owner=hit.ownerAgentId || hit.agentId;return [...(AGENTS.find(agent=>agent.id===owner)?.memories || []),...(getState().learnedMemories[owner] || [])].find(memory=>memory.id===hit.memoryId);}

export function mountWorkbench(){
  const options=document.getElementById('participantOptions');
  options.innerHTML=AGENTS.map(agent=>`<label class="participant-option"><input type="checkbox" name="participants" value="${h(agent.id)}"><span><strong>${h(agent.name)}</strong><small>${h(agent.label)}</small></span></label>`).join('');
  options.addEventListener('change',()=>{if(!setParticipants([...options.querySelectorAll('input:checked')].map(input=>input.value))) renderParticipants();});
  document.addEventListener('click',event=>{const menu=document.getElementById('participantsControl');if(menu.open && !menu.contains(event.target)) menu.open=false;});
  document.getElementById('participantsControl').addEventListener('keydown',event=>{if(event.key==='Escape'){event.currentTarget.open=false;event.currentTarget.querySelector('summary').focus();}});
  const input=document.getElementById('messageInput');
  document.getElementById('channelSelect').addEventListener('change',event=>{drafts.set(getState().activeChannel,input.value);setChannel(event.target.value);input.value=drafts.get(event.target.value) || '';});
  document.getElementById('modeSelect').addEventListener('change',event=>setMode(event.target.value));
  document.getElementById('memoryEnabled').addEventListener('change',event=>setMemoryEnabled(event.target.checked));
  input.addEventListener('input',()=>drafts.set(getState().activeChannel,input.value));
  document.getElementById('messageForm').addEventListener('submit',event=>{event.preventDefault();void submit();});
  input.addEventListener('keydown',event=>{if(event.key==='Enter' && !event.shiftKey && !event.isComposing){event.preventDefault();void submit();}});
  document.getElementById('resetConversation').addEventListener('click',async()=>{const channel=getState().activeChannel;if(await resetChannel(channel)){drafts.delete(channel);if(getState().activeChannel===channel) input.value='';}});
  document.getElementById('pauseRun').addEventListener('click',()=>{const status=session().run?.status;void(status==='paused'?resumeRun():status==='interrupted'?recoverRun():pauseRun());});
  document.getElementById('cancelRun').addEventListener('click',()=>{void cancelRun();});
  const tabs=document.getElementById('inspectorTabs');
  tabs.addEventListener('click',event=>{const button=event.target.closest('[data-inspector-tab]');if(button) selectTab(button.dataset.inspectorTab);});
  tabs.addEventListener('keydown',event=>{const buttons=[...tabs.querySelectorAll('[role=tab]')];const index=buttons.indexOf(event.target);if(index<0 || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;selectTab(buttons[next].dataset.inspectorTab);buttons[next].focus();});
  document.getElementById('approvalArea').addEventListener('input',event=>{const id=event.target.dataset.approvalInput;if(!id)return;const value=approvalDrafts.get(id) || {};value[event.target.dataset.field]=event.target.value;approvalDrafts.set(id,value);});
  document.getElementById('approvalArea').addEventListener('click',event=>{const button=event.target.closest('[data-approval-choice]');if(button) void replyToApproval(button);});
  document.getElementById('inspectorContent').addEventListener('click',event=>{
    const run=session().run;if(!run)return;
    const callButton=event.target.closest('[data-call]');
    const taskButton=event.target.closest('[data-task]');
    const artifactButton=event.target.closest('[data-artifact]');
    const memoryButton=event.target.closest('[data-memory-hit]');
    if(callButton){const call=run.calls?.[Number(callButton.dataset.call)];if(call)openDrawer(`调用 · ${call.tool}`,`<p>${badge(call.status)}</p><dl class="detail-list"><dt>任务 / Agent</dt><dd>${h(call.taskId)} / ${h(agentName(call.agentId))}</dd><dt>耗时 / 沙箱</dt><dd>${h(formatDuration(call.durationMs))} / ${h(call.sandboxId || '服务网关')}</dd></dl><h3>参数</h3><pre>${h(text(call.args))}</pre><h3>结构化结果</h3><pre>${h(text(call.result))}</pre>`);}
    if(taskButton){const task=run.steps?.find(item=>item.id===taskButton.dataset.task);if(task)openTask(task);}
    if(artifactButton){const artifact=run.artifacts?.find(item=>item.id===artifactButton.dataset.artifact);if(artifact){if(artifactButton.dataset.download)downloadArtifact(artifact);else openDrawer(`产物 · ${artifact.name}`,`<pre>${h(text(artifact.content))}</pre>`);}}
    if(memoryButton){const hit=run.memoryHits?.[Number(memoryButton.dataset.memoryHit)];if(hit){const memory=findMemory(hit);openDrawer('本次记忆',`<h3>${h(memory?.title || hit.memoryId)}</h3><p>${h(memory?.lesson || hit.reason || '')}</p><dl class="detail-list"><dt>归属 / 本次采用者</dt><dd>${h(agentName(hit.ownerAgentId || hit.agentId))} / ${h(agentName(hit.agentId))}</dd><dt>来源 / 关联任务</dt><dd>${h(memory?.sourceTaskId || '')} / ${h(hit.taskId)}</dd><dt>采用状态</dt><dd>${hit.applied?'已采用':'仅检索'}</dd><dt>依据</dt><dd>${h(text(memory?.evidence || hit.reason))}</dd></dl><p class="text-small muted">记忆参与执行不等于效果提升；效果须由独立对照验证。</p>`);}}
  });
  const unsubscribe=subscribe(render);render();
  const clock=setInterval(()=>{const run=session().run;document.getElementById('runElapsed').textContent=run?formatDuration(elapsed(run)):'';},1000);
  return ()=>{unsubscribe();clearInterval(clock);};
}

async function submit(){const state=getState();if(state.client.pendingAction || ACTIVE.includes(session().run?.status))return;const input=document.getElementById('messageInput');const original=input.value;const channel=state.activeChannel;if(!original.trim())return;if(await sendMessage(original,channel)){if(drafts.get(channel)===original || !drafts.has(channel))drafts.delete(channel);if(getState().activeChannel===channel && input.value===original)input.value='';}}
function selectTab(id){tab=id;document.querySelectorAll('[data-inspector-tab]').forEach(button=>{const selected=button.dataset.inspectorTab===id;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});document.getElementById('inspectorContent').setAttribute('aria-labelledby',`tab-${id}`);renderInspector();}
function render(){
  const state=getState(),current=session(),run=current.run,channel=state.activeChannel;
  const pending=Boolean(state.client.pendingAction),busy=ACTIVE.includes(run?.status),ready=state.client.reachable;
  renderParticipants();document.getElementById('channelSelect').value=channel;
  const mode=busy?run.mode:state.channelModes[channel];
  document.getElementById('modeSelect').value=mode || (channel==='group'?'task':'chat');document.getElementById('modeSelect').disabled=pending || busy;
  document.getElementById('memoryEnabled').checked=busy?run.memoryEnabled!==false:state.memoryEnabled;document.getElementById('memoryEnabled').disabled=pending || busy;
  document.getElementById('channelPrivacy').textContent=channel==='group'?'群聊任务由所选成员协作；普通聊天由当前回复 Agent 处理。':'私聊仅由当前 Agent 处理，可选择聊天或单 Agent 工具任务。';
  document.getElementById('conversationTitle').textContent=channel==='group'?'群聊':'私聊 · '+agentName(channel);
  const participants=busy?(run.participants || []):state.selectedAgentIds;
  document.getElementById('conversationHint').textContent=channel==='group'?`${mode==='task'?'任务协作':'普通聊天'} · ${participants.map(agentName).join(' · ')}`:`${agentName(channel)} · ${mode==='task'?'单 Agent 执行任务':'普通聊天'}`;
  document.getElementById('sendMessage').disabled=!ready || !state.connection.configured || pending || busy;
  document.getElementById('sendMessage').textContent=pending && state.client.pendingAction==='send'?'提交中…':'发送 ↗';
  document.getElementById('messageInput').disabled=Boolean(pending && state.client.pendingChannel===channel) || busy;
  document.getElementById('resetConversation').disabled=!ready || pending;
  document.getElementById('composerHint').textContent=!ready?'后端未连接。':!state.connection.configured?'请先配置模型 API。':pending?'正在提交操作…':run?.status==='waiting_human'?'请回复上方人工请求。':run?.status==='paused'?'任务已暂停，可继续或取消。':run?.status==='interrupted'?'任务已中断，可恢复或取消。':busy?'任务执行中，可查看步骤与调用记录。':'Enter 发送 · Shift + Enter 换行';
  const nextKey=JSON.stringify([channel,current.messages]);
  if(nextKey!==messageKey){const list=document.getElementById('messageList');const follow=list.scrollHeight-list.scrollTop-list.clientHeight<120 || !messageKey.includes(`"${channel}"`);list.innerHTML=current.messages?.length?current.messages.map(renderMessage).join(''):`<div class="empty-state"><div class="empty-mark" aria-hidden="true">${channel==='group'?'↗':INITIALS[channel] || 'A'}</div><h3>开始${mode==='task'?'执行任务':'聊天'}</h3><p>${mode==='task'?'描述目标与验收条件。执行过程、人工请求与产物会在这里更新。':'输入消息，与当前 Agent 进行普通对话。'}</p></div>`;if(follow)list.scrollTop=list.scrollHeight;messageKey=nextKey;}
  renderApprovals(run);
  document.getElementById('runMeta').textContent=run?`${run.id} · ${run.mode==='task'?'任务执行':'普通聊天'}`:'等待新任务';
  document.getElementById('runStatus').className=run?'':'tag muted';document.getElementById('runStatus').innerHTML=run?badge(run.status):'待开始';
  document.getElementById('runTitle').textContent=run?.title || '发送任务后，查看步骤、执行记录和成果。';
  const members=document.getElementById('runParticipants');members.hidden=!run;members.textContent=run?`本任务成员：${(run.participants || []).map(agentName).join(' · ')}`:'';
  document.getElementById('runElapsed').textContent=run?formatDuration(elapsed(run)):'';
  document.getElementById('runProgress').textContent=run?`${(run.steps || []).filter(step=>step.status==='done').length}/${run.steps?.length || 0} 步骤完成 · ${run.calls?.length || 0} 次调用`:'';
  const pause=document.getElementById('pauseRun');pause.disabled=!ready || pending || !busy;pause.textContent=run?.status==='paused'?'继续':run?.status==='interrupted'?'恢复':'暂停';
  document.getElementById('cancelRun').disabled=!ready || pending || !busy;
  renderInspector();
}
function renderParticipants(){const state=getState(),control=document.getElementById('participantsControl');control.hidden=state.activeChannel!=='group';if(control.hidden)control.open=false;const locked=Boolean(state.client.pendingAction) || ACTIVE.includes(state.sessions.group?.run?.status);const fieldset=document.getElementById('participantOptions');fieldset.disabled=locked;fieldset.querySelectorAll('input[name=participants]').forEach(input=>{input.checked=state.selectedAgentIds.includes(input.value);input.disabled=locked || (input.checked && state.selectedAgentIds.length===1);});document.getElementById('participantCount').textContent=`${state.selectedAgentIds.length}/${AGENTS.length}`;document.getElementById('participantHelp').textContent=locked?'当前任务成员已锁定。':'至少选择 1 个 Agent；只影响下次群聊任务。';}
function renderMessage(message){if(message.role==='system')return `<div class="message system">${h(message.text)}</div>`;const human=['human','user'].includes(message.role);return `<article class="message ${human?'human':'agent'}"><div class="avatar ${h(message.agentId || '')}" aria-hidden="true">${human?'你':INITIALS[message.agentId] || 'A'}</div><div class="message-body"><div class="message-heading"><strong>${human?'你':h(agentName(message.agentId))}</strong><time>${h(timeText(message.time))}</time></div><p class="message-text">${h(message.text)}</p></div></article>`;}
function renderApprovals(run){
  const host=document.getElementById('approvalArea');const pending=(run?.approvals || []).filter(approval=>approval.status==='pending');
  const nextKey=JSON.stringify([getState().activeChannel,pending]);
  if(nextKey!==approvalsKey){
    host.innerHTML=pending.map(approval=>{const draft=approvalDrafts.get(approval.id) || {};const permission=approval.type==='permission';const labels={permission:'操作审批',clarification:'需求澄清',budget:'资源预算',reconciliation:'状态核对'};const choices=(approval.choices || []).filter(choice=>choice.id!=='modify');return `<section class="approval-card" data-approval-card="${h(approval.id)}"><div class="item-topline"><h3>${h(approval.title)}</h3><span class="tag warning">${h(labels[approval.type] || approval.type)}</span></div><p>${h(approval.description)}</p>${approval.expiresAt?`<p class="text-small muted">有效期至 ${h(new Date(approval.expiresAt).toLocaleString('zh-CN'))}</p>`:''}${permission?`<details class="approval-parameters"><summary>操作参数与修改</summary><label class="text-small" for="args-${h(approval.id)}">修改后的参数（JSON）</label><textarea id="args-${h(approval.id)}" data-approval-input="${h(approval.id)}" data-field="args" rows="5">${h(draft.args ?? text(approval.args || {}))}</textarea><button type="button" class="button quiet small" data-approval="${h(approval.id)}" data-approval-choice="modify">提交修改并重新审批</button><p class="text-small muted">修改参数会生成新的审批请求，不会立即执行。</p></details>`:''}<label class="sr-only" for="answer-${h(approval.id)}">回复或补充说明</label><textarea id="answer-${h(approval.id)}" data-approval-input="${h(approval.id)}" data-field="answer" rows="2" placeholder="${permission?'补充说明（可选）':'输入答复…'}">${h(draft.answer || '')}</textarea><div class="approval-actions">${choices.map((choice,index)=>`<button type="button" class="button ${choice.id==='deny'?'quiet':index===0?'primary':'quiet'}" data-approval="${h(approval.id)}" data-approval-choice="${h(choice.id)}">${h(choice.label)}</button>`).join('')}${approval.type==='clarification'?`<button type="button" class="button primary" data-approval="${h(approval.id)}" data-approval-choice="answer">发送答复</button>`:''}</div><p class="approval-error" data-approval-error></p></section>`;}).join('');approvalsKey=nextKey;
  }
  for(const card of host.querySelectorAll('[data-approval-card]')){const approval=pending.find(item=>item.id===card.dataset.approvalCard);const expired=approval?.expiresAt && stamp(approval.expiresAt)<=Date.now();card.querySelectorAll('button,textarea').forEach(input=>input.disabled=Boolean(getState().client.pendingAction) || !getState().client.reachable || Boolean(expired));}
}
async function replyToApproval(button){
  const channel=getState().activeChannel,id=button.dataset.approval;const approval=session().run?.approvals?.find(item=>item.id===id && item.status==='pending');if(!approval)return;
  const card=button.closest('[data-approval-card]'),error=card.querySelector('[data-approval-error]');error.textContent='';
  const answer=card.querySelector('[data-field="answer"]').value.trim();let modifiedArgs;
  if(button.dataset.approvalChoice==='answer' && !answer){error.textContent='请先输入答复。';return;}
  if(button.dataset.approvalChoice==='modify'){try{modifiedArgs=JSON.parse(card.querySelector('[data-field="args"]').value);if(!modifiedArgs || typeof modifiedArgs!=='object' || Array.isArray(modifiedArgs))throw new Error();}catch{error.textContent='参数必须是有效的 JSON 对象。';return;}}
  if(await resolveApproval(id,button.dataset.approvalChoice,answer,modifiedArgs,channel))approvalDrafts.delete(id);
}
function openTask(task){openDrawer(`子任务 · ${task.title}`,`<p>${badge(task.status)}</p><dl class="detail-list"><dt>编号 / 负责人</dt><dd>${h(task.id)} / ${h(agentName(task.agentId))}</dd><dt>派单者</dt><dd>${h(agentName(task.delegatedBy))}</dd><dt>依赖</dt><dd>${h((task.dependsOn || []).join('、') || '无')}</dd><dt>尝试 / 重试</dt><dd>${h(task.attempts || 0)} / ${h(task.retries || 0)}</dd></dl><h3>目标与验收条件</h3><pre>${h(text({goal:task.goal,acceptanceCriteria:task.acceptanceCriteria}))}</pre><h3>输入 / 输出</h3><pre>${h(text({inputs:task.inputs,outputs:task.outputs}))}</pre><h3>结果与错误</h3><pre>${h(text({result:task.result,error:task.error,feedback:task.feedback}))}</pre>`);}
function renderInspector(){
  const host=document.getElementById('inspectorContent'),run=session().run;let html='';const eventsOpen=Boolean(host.querySelector('[data-events]')?.open);
  if(!run)html='<div class="empty-state"><p>开始任务后查看依赖、负责人、调用、记忆与产物。</p></div>';
  else if(tab==='calls')html=(run.calls || []).length?`<div class="record-list">${run.calls.map((call,index)=>`<button type="button" class="record-button" data-call="${index}"><div class="item-topline"><code>${h(call.tool)}</code>${badge(call.status)}</div><div class="record-meta"><span>${h(agentName(call.agentId))}</span><span>${h(formatDuration(call.durationMs))}</span></div><p>${h(brief(call.result || '等待返回'))}</p></button>`).join('')}</div>`:'<div class="empty-state"><p>暂无调用记录。</p></div>';
  else if(tab==='memory')html=(run.memoryHits || []).length?`<div class="record-list">${run.memoryHits.map((hit,index)=>`<button type="button" class="record-button" data-memory-hit="${index}"><div class="item-topline"><strong>${h(findMemory(hit)?.title || hit.memoryId)}</strong><span class="tag ${hit.applied?'info':'muted'}">${hit.applied?'已采用':'仅检索'}</span></div><div class="record-meta">${h(agentName(hit.agentId))} · ${h(hit.taskId)}</div><p>${h(hit.reason || '')}</p></button>`).join('')}</div><p class="text-small muted" style="margin-top:14px">仅采用已确认且符合当前范围的私有或显式共享记忆。使用记录不代表效果提升。</p>`:`<div class="empty-state"><p>${run.memoryEnabled===false?'本任务已关闭记忆。':'当前任务暂无记忆检索记录。'}</p></div>`;
  else html=`<ol class="task-list">${(run.steps || []).map(task=>`<li class="task-row"><button type="button" class="task-detail-button" data-task="${h(task.id)}"><span class="item-topline"><strong>${h(task.title)}</strong>${badge(task.status)}</span><span class="task-meta">${h(agentName(task.agentId))} · 派单：${h(agentName(task.delegatedBy))}</span><span class="task-meta">依赖：${h((task.dependsOn || []).map(id=>run.steps.find(step=>step.id===id)?.title || id).join('、') || '无')} · 输出 ${task.outputs?.length || 0} 项</span>${task.error?`<span class="task-error">${h(brief(task.error))}</span>`:''}</button></li>`).join('') || '<li class="text-small muted">正在建立任务计划…</li>'}</ol><section class="artifact-section"><h3>产物</h3>${(run.artifacts || []).map(artifact=>`<div class="artifact-row"><button type="button" class="button quiet small" data-artifact="${h(artifact.id)}">${h(artifact.name)}</button><button type="button" class="button small" data-artifact="${h(artifact.id)}" data-download="true">下载</button></div>`).join('') || '<p class="text-small muted">暂无文本产物。</p>'}</section><details data-events ${eventsOpen?'open':''}><summary>执行事件 · ${run.events?.length || 0} 条</summary><ol class="event-list">${(run.events || []).map(event=>`<li><span class="muted">${h(timeText(event.time))}</span> <code>${h(event.type)}</code><p>${h(event.message)}</p></li>`).join('')}</ol></details>`;
  if(host.innerHTML!==html){const scrollTop=host.scrollTop;host.innerHTML=html;host.scrollTop=scrollTop;}
}
