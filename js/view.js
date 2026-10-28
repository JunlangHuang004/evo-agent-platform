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

export function openDrawer(title, html) {
  const dialog = document.getElementById('detailDialog');
  document.getElementById('dialogTitle').textContent = title;
  document.getElementById('dialogBody').innerHTML = html;
  if (!dialog.open) dialog.showModal();
}

export function closeDrawer() {
  document.getElementById('detailDialog').close();
}

export function downloadArtifact(artifact) {
  const blob = new Blob([artifact.content], {type: artifact.mimeType || 'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = artifact.name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
