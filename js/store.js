import { AGENTS, updateMetadata } from './data.js';

const ids = AGENTS.map(agent => agent.id);
const state = {
  activeChannel:'group', selectedAgentIds:[...ids], memoryEnabled:true, channelModes:Object.fromEntries(['group',...ids].map(id=>[id,id==='group'?'task':'chat'])),
  sessions:Object.fromEntries(['group', ...ids].map(channelId => [channelId,{channelId,messages:[],run:null}])),
  learnedMemories:Object.fromEntries(ids.map(id => [id,[]])),
  connection:{configured:false,provider:null,model:null,dockerAvailable:false,dockerImageReady:false},
  evaluation:{mode:'real',runs:[]},
  client:{loading:true,reachable:false,error:'',errorSource:'',pendingAction:null,pendingChannel:null,lastSynced:null}
};
const ACTIVE=['running','waiting_human','paused','interrupted'];
const listeners = new Set();
let tail = Promise.resolve();
let pollPending = false;
let pollTimer;
let initialized = false;
let selectedLocally = false;
const publish = () => {for (const fn of [...listeners]) {try {fn(state);} catch (error) {console.error('UI subscriber failed',error);}}};
const queue = task => {const result=tail.then(task,task);tail=result.catch(()=>{});return result;};

function applySnapshot(snapshot) {
  if (!snapshot || typeof snapshot.sessions !== 'object') throw new Error('后端返回了无效状态。');
  const activeChannel=state.activeChannel;
  updateMetadata(snapshot);
  state.sessions=snapshot.sessions;
  state.learnedMemories=snapshot.learnedMemories || {};
  state.connection=snapshot.connection || {};
  state.evaluation=snapshot.evaluation || {mode:'real',runs:[]};
  if (!selectedLocally && Array.isArray(snapshot.selectedAgentIds)) state.selectedAgentIds=[...snapshot.selectedAgentIds];
  state.activeChannel=state.sessions[activeChannel] ? activeChannel : 'group';
  state.client.reachable=true;
  state.client.loading=false;
  if(state.client.errorSource!=='action'){state.client.error='';state.client.errorSource='';}
  state.client.lastSynced=Date.now();
}

async function request(url, options={}) {
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),15000);
  try {
    const response=await fetch(url,{cache:'no-store',...options,signal:controller.signal});
    let body;
    try {body=await response.json();} catch {throw new Error('后端未连接。请使用项目后端服务启动，不能仅用静态文件服务。');}
    if (!response.ok) throw new Error(body.error || `请求失败（${response.status}）`);
    return body;
  } catch(error) {
    if (error.name==='AbortError') throw new Error('连接超时，请重试。');
    throw error;
  } finally {clearTimeout(timeout);}
}

export const getState=()=>state;
export function subscribe(fn) {listeners.add(fn);return ()=>listeners.delete(fn);}
export function setChannel(id) {if(state.sessions[id]) {state.activeChannel=id;publish();} }
export function setMode(mode,channelId=state.activeChannel) {if(['task','chat'].includes(mode)){state.channelModes[channelId]=mode;publish();}}
export function setMemoryEnabled(value) {state.memoryEnabled=Boolean(value);publish();}
export function setParticipants(agentIds) {
  const valid=[...new Set(agentIds)].filter(id=>AGENTS.some(agent=>agent.id===id));
  if (!valid.length || state.client.pendingAction || ACTIVE.includes(state.sessions.group?.run?.status)) return false;
  state.selectedAgentIds=valid;selectedLocally=true;publish();return true;
}
export async function refreshState() {
  if (pollPending || state.client.pendingAction) return false;
  pollPending=true;
  try {
    await queue(async()=>applySnapshot(await request('/api/state')));
    return true;
  } catch(error) {
    state.client.reachable=false;state.client.loading=false;state.client.error=error.message || '连接后端失败。';state.client.errorSource='connection';
    return false;
  } finally {pollPending=false;publish();}
}
export async function startClient() {
  if (initialized) return;
  initialized=true;
  await refreshState();
  pollTimer=setInterval(()=>{void refreshState();},800);
}
export function stopClient() {clearInterval(pollTimer);initialized=false;}

async function action(actionName,payload={},channelId=state.activeChannel) {
  if (state.client.pendingAction || !state.client.reachable) return false;
  const runId=actionName!=='memory_confirm'?state.sessions[channelId]?.run?.id:null;
  state.client.pendingAction=actionName;state.client.pendingChannel=channelId;state.client.error='';state.client.errorSource='';publish();
  try {
    await queue(async()=>applySnapshot(await request('/api/action',{
      method:'POST',headers:{'Content-Type':'application/json','X-EvoMAC-Client':'1'},
      body:JSON.stringify({action:actionName,channelId,...(runId?{runId}:{}),...payload})
    })));
    return true;
  } catch(error) {
    state.client.error=error.message || '操作失败。';state.client.errorSource='action';
    // Preserve the server state and user's draft. Never replace a failed action with a simulated result.
    return false;
  } finally {state.client.pendingAction=null;state.client.pendingChannel=null;publish();}
}
export function sendMessage(text,channelId=state.activeChannel) {
  const value=text.trim();
  if (!value || !state.connection.configured || ACTIVE.includes(state.sessions[channelId]?.run?.status)) return Promise.resolve(false);
  const participants=channelId==='group' ? [...state.selectedAgentIds] : [channelId];
  return action('send',{text:value,participants,memoryEnabled:state.memoryEnabled,mode:state.channelModes[channelId] || (channelId==='group'?'task':'chat')},channelId);
}
export const pauseRun=(channelId=state.activeChannel)=>action('pause',{},channelId);
export const resumeRun=(channelId=state.activeChannel)=>action('resume',{},channelId);
export const resetChannel=(channelId=state.activeChannel)=>action('reset',{},channelId);
export const cancelRun=(channelId=state.activeChannel)=>action('cancel',{},channelId);
export const recoverRun=(channelId=state.activeChannel)=>action('recover',{},channelId);
export const resolveApproval=(approvalId,choiceId,answer='',modifiedArgs,channelId=state.activeChannel)=>action('approval',{approvalId,choiceId,answer,...(modifiedArgs?{modifiedArgs}:{})},channelId);
export const confirmMemory=(agentId,memoryId,confirmed)=>action('memory_confirm',{agentId,memoryId,confirmed});
