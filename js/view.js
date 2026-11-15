export function escapeHtml(value = '') {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

export function formatDuration(ms = 0) {
  const seconds = Math.max(0, Number(ms) || 0) / 1000;
  return seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
}

export function badge(status) {
  const states = {
    running: ['进行中', 'info'], waiting_human: ['等待你回复', 'warning'], paused: ['已暂停', 'warning'],
    pending: ['待执行', 'muted'], done: ['已完成', 'success'], success: ['成功', 'success'],
    passed: ['通过', 'success'], failed: ['失败', 'danger'], partial: ['部分完成', 'warning'],
    skipped: ['已跳过', 'muted'], cancelled: ['已取消', 'muted'], denied: ['已拒绝', 'danger'],
    assigned:['已分配','info'], retrying:['重试中','warning'], waiting_children:['等待子任务','warning'], rejected:['已拒收','danger'], interrupted:['已中断','warning'], unknown:['结果待核对','warning'], expired:['已过期','muted'], confirmed:['已确认可检索','info'], unassessed:['未评估','muted'], candidate: ['待确认经验', 'warning'], verified: ['已验证', 'success'], resolved: ['已回复', 'success']
  };
  const [label, tone] = states[status] || [status || '待开始', 'muted'];
  return `<span class="tag ${tone}">${escapeHtml(label)}</span>`;
}

export function runFailureSummary(run) {
  if(!run || !['failed','partial','paused','interrupted','cancelled'].includes(run.status))return null;
  const calls=Array.isArray(run.calls)?run.calls:[],tasks=Array.isArray(run.steps)?run.steps:[];
  const rootError=run.error,gaps=Array.isArray(run.acceptanceGaps)?run.acceptanceGaps.filter(item=>typeof item==='string'):[];
  const hasRootError=Boolean(typeof rootError==='string'?rootError.trim():rootError && (rootError.message || rootError.code));
  const acceptanceOnly=run.status==='partial' && !hasRootError && gaps.length>0;
  // A recovered retry belonging to a completed task is historical evidence,
  // not the cause of another task's current failure or missing acceptance.
  const call=acceptanceOnly?null:[...calls].reverse().find(item=>['failed','denied','unknown','interrupted'].includes(item.status) && tasks.find(task=>task.id===item.taskId)?.status!=='done');
  const task=acceptanceOnly?null:tasks.find(item=>item.id===call?.taskId) || [...tasks].reverse().find(item=>item.status!=='done' && item.error);
  const result=call?.result,taskError=task?.error;
  const error=typeof rootError==='object' && rootError?rootError:typeof taskError==='object' && taskError?taskError:{};
  const resultError=typeof result==='object' && result?.error?result.error:{};
  const callError=call?.error || {};
  const code=error.code || callError.code || resultError.code || '';
  let message=acceptanceOnly?gaps.join('\n'):error.message || (typeof rootError==='string'?rootError:'') || (typeof taskError==='string'?taskError:'') || resultError.message || (typeof resultError==='string'?resultError:'') || (typeof result==='string'?result:'') || gaps.join('\n');
  let next='先核对失败调用、已保存产物与外部影响，修正后在本团队提交新任务；不会重放旧任务。';
  if(run.status==='paused'){message='已暂停派发新动作；已经发出的请求可能仍在结束。';next='点击“继续”从保存的位置继续当前任务。';}
  else if(run.status==='interrupted'){message=message || '执行已中断，现场和调用记录已保存。';next='点击“恢复”继续；无法确认的外部操作仍须先人工核对，不会直接重放。';}
  else if(run.status==='cancelled'){message='任务已取消；已发生的操作不会被撤销。';}
  else if(code==='authorization'){next='检查本机课程密钥、权限与额度，核对已有产物后再提交新任务；不会重放旧任务。';}
  else if(['network','provider_http'].includes(code)){next='检查模型服务、网络或限流状态，核对已有产物后再提交新任务；不会重放旧任务。';}
  else if(code==='budget'){next='核对已有成果与预算，调整任务范围后提交新任务。';}
  else if(gaps.length || ['verification','output_contract'].includes(code)){next='查看未满足的验收条件和成果检查问题，修正后提交后续任务。';}
  if(!message)message=run.status==='partial'?'部分任务未完成，请查看子任务结果与验收条件。':'未保存详细错误，请查看调用记录和执行事件。';
  return {status:run.status,phase:acceptanceOnly?'成果验收':run.status==='paused'?'人工暂停':run.status==='cancelled'?'人工取消':call?(call.tool==='llm.chat'?'模型请求':'工具执行 · '+call.tool):task?'子任务执行':'任务调度',
    code,message:String(message),taskId:task?.id || call?.taskId || '',taskTitle:task?.title || '',agentId:call?.agentId || task?.agentId || '',callId:call?.id || '',
    retryable:typeof callError.retryable==='boolean'?callError.retryable:typeof resultError.retryable==='boolean'?resultError.retryable:null,
    savedArtifacts:Array.isArray(run.artifacts)?run.artifacts.length:0,canResume:['paused','interrupted'].includes(run.status),next};
}

export function failureSummaryHtml(run) {
  const summary=runFailureSummary(run);if(!summary)return '';
  const detail=[summary.taskTitle || summary.taskId,summary.agentId,summary.callId].filter(Boolean).join(' · ');
  return `<section class="run-issue"><strong>${escapeHtml(summary.phase)}${summary.code?' · '+escapeHtml(summary.code):''}</strong>${detail?`<p class="text-small muted">${escapeHtml(detail)}</p>`:''}<p class="message-text">${escapeHtml(summary.message)}</p><p class="text-small">${escapeHtml(summary.next)}</p><p class="text-small muted">已保存产物 ${summary.savedArtifacts} 项；执行状态与成果验收分别记录。</p></section>`;
}

export function taskOutputHtml(task) {
  const contract=task?.outputContract,validation=task?.outputValidation;
  const configured=Boolean(contract && typeof contract==='object' && ['text','artifact','json'].includes(contract.kind));
  const validIssues=Array.isArray(validation?.issues) && validation.issues.every(issue=>typeof issue==='string');
  const issues=validIssues?validation.issues:[];
  const status=configured && validIssues && ['passed','failed'].includes(validation?.status)?validation.status:'not_configured';
  const label=status==='passed' && !issues.length?'格式/文件检查通过':status==='failed' || (status==='passed' && issues.length)?'格式/文件检查未通过':'未配置或尚未检查';
  const tone=label==='格式/文件检查通过'?'info':label==='格式/文件检查未通过'?'warning':'muted';
  const kind=configured?({text:'文本',artifact:'文件产物',json:'JSON'})[contract.kind]:'未配置';
  return `<section class="task-output-contract"><h3>成果合同与格式检查</h3><p class="text-small">类型：${escapeHtml(kind)} · <span class="tag ${tone}">${label}</span></p>${configured?`<details><summary>结构化成果要求</summary><pre>${escapeHtml(JSON.stringify(contract,null,2))}</pre></details>`:''}${issues.length?`<ul>${issues.map(issue=>`<li>${escapeHtml(issue)}</li>`).join('')}</ul>`:''}<p class="text-small muted">仅检查格式、字段或文件；不代表语义正确、独立验收或任务完成。</p></section>`;
}

export function openDrawer(title, html) {
  const dialog = document.getElementById('detailDialog');
  document.getElementById('dialogTitle').textContent = title;
  document.getElementById('dialogBody').innerHTML = html;
  if (!dialog.open) dialog.showModal();
}

export function closeDrawer() {
  document.getElementById('detailDialog').close();
}

export function isHtmlArtifact(artifact) {
  return /\.html?$/i.test(artifact.name || '') || /^text\/html\b/i.test(artifact.mimeType || '');
}

export function downloadArtifact(artifact) {
  // Generated HTML is a download, never a same-origin live document or iframe.
  const type=isHtmlArtifact(artifact)?'application/octet-stream':artifact.mimeType || 'text/plain;charset=utf-8';
  const blob = new Blob([artifact.content], {type});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = artifact.name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function runToMarkdown(run, teamName='') {
  const lines=[`# ${run.title || run.id}`, '', `团队：${teamName || run.teamId || ''}`, `任务：${run.id}`, `状态：${run.status}`, ''];
  for(const [label, items] of [['团队前序上下文',run.groupHistory],['本次对话',run.conversation]]){
    if(!items?.length)continue;
    lines.push(`## ${label}`,'');
    for(const message of items)lines.push(`### ${message.agentId || message.role || '消息'}${message.time?' · '+message.time:''}`, '', String(message.text || message.content || ''), '');
  }
  if(run.messages?.length){lines.push('## Agent 间消息','');for(const message of run.messages)lines.push(`### ${message.fromAgent} → ${message.toAgent}`, '', String(message.text || ''),'');}
  if(run.approvals?.length){lines.push('## 人工请求与回答','');for(const approval of run.approvals)lines.push(`### ${approval.title}`, '', String(approval.description || ''), '', `状态：${approval.status}`, `回答：${JSON.stringify(approval.response || {},null,2)}`, '');}
  lines.push('## 完整执行记录（含调用、产物和执行对话）','');
  const serialized=JSON.stringify(run,null,2),maxTicks=Math.max(2,...(serialized.match(/`+/g)||[]).map(match=>match.length)),fence='`'.repeat(maxTicks+1);
  lines.push(`${fence}json`,serialized,fence,'');return lines.join('\n');
}
