import { mountWorkbench } from './workbench.js';
import { mountAgents, mountEvaluation } from './panels.js';
import { closeDrawer } from './view.js';
import { getState, subscribe, startClient, refreshState, stopClient } from './store.js';

const pages = {
  workspace: {title: '工作台', description: '提出任务，查看协作过程和执行结果。'},
  agents: {title: 'Agents 与记忆', description: '查看每个 Agent 的职责、工具权限和独立经验。'},
  evaluation: {title: '评测', description: '查看真实运行与独立验收状态。'}
};
const aliases = {overview: 'workspace', playground: 'workspace', task: 'workspace', workflow: 'workspace', trace: 'workspace', memory: 'agents'};

function route() {
  const hash = location.hash.slice(1);
  const owns = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const id = owns(pages, hash) ? hash : owns(aliases, hash) ? aliases[hash] : 'workspace';
  if (id !== hash) history.replaceState(null, '', `#${id}`);
  document.querySelectorAll('[data-page]').forEach(page => {page.hidden = page.dataset.page !== id;});
  document.querySelectorAll('[data-route]').forEach(link => {
    if (link.dataset.route === id) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  document.getElementById('pageTitle').textContent = pages[id].title;
  document.getElementById('pageDescription').textContent = pages[id].description;
  document.title = `${pages[id].title} · EvoMAC`;
  window.scrollTo({top: 0});
}

function renderConnection(){
  const {client,connection}=getState();
  const element=document.getElementById('connectionState');
  element.classList.toggle('is-connected',client.reachable);
  element.classList.toggle('is-disconnected',!client.loading && !client.reachable);
  const label=client.loading?'正在连接后端…':!client.reachable?'后端未连接':connection.configured?'后端已连接 · 模型已配置':'后端已连接 · 模型未配置';
  element.replaceChildren();
  const dot=document.createElement('span');dot.setAttribute('aria-hidden','true');element.append(dot,document.createTextNode(label));
  document.getElementById('modelState').textContent=client.reachable ? [connection.provider,connection.model,connection.dockerAvailable && connection.dockerImageReady?'Docker可执行':'代码执行不可用'].filter(Boolean).join(' · ') : '';
  const error=client.error || (client.reachable && !connection.configured?'请在后端环境配置模型 API 后再发送消息。':'');
  document.getElementById('connectionError').hidden=!error;
  document.getElementById('connectionErrorText').textContent=error;
  document.getElementById('retryConnection').disabled=Boolean(client.pendingAction);
}
subscribe(renderConnection);
renderConnection();
document.getElementById('retryConnection').addEventListener('click',()=>{void refreshState();});
await startClient();
mountWorkbench();
mountAgents(document.getElementById('agentsPage'));
mountEvaluation(document.getElementById('evaluationPage'));
window.addEventListener('pagehide',stopClient);
document.querySelector('.skip-link').addEventListener('click', event => {
  event.preventDefault();
  const main = document.getElementById('mainContent');
  main.tabIndex = -1;
  main.focus({preventScroll: true});
  main.scrollIntoView({block: 'start'});
});
document.getElementById('closeDialog').addEventListener('click', closeDrawer);
document.getElementById('detailDialog').addEventListener('click', event => {
  if (event.target === event.currentTarget) {
    const bounds = event.currentTarget.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeDrawer();
  }
});
window.addEventListener('hashchange', route);
route();
