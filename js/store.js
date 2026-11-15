import { AGENTS, updateMetadata } from './data.js';

const ACTIVE=['running','waiting_human','waiting_children','paused','interrupted'];
const emptyConnection=()=>({configured:false,provider:null,model:null,dockerAvailable:false,dockerImageReady:false});
const emptyHistory=()=>({loading:false,runs:[],selected:null,error:''});
const defaults=()=>({activeChannel:'group',selectedAgentIds:[],memoryEnabled:true,channelModes:{group:'task'},selectedLocally:false});
const state={teamId:'default',teamName:'默认团队',teamDescription:'',teams:[],...defaults(),
  sessions:{group:{channelId:'group',messages:[],run:null}},learnedMemories:{},connection:emptyConnection(),
  evaluation:{mode:'real',runs:[]},history:emptyHistory(),
  client:{loading:true,reachable:false,error:'',errorSource:'',pendingAction:null,pendingChannel:null,lastSynced:null}};
const teams=new Map();
const listeners=new Set();
const polling=new Set();
const historyRequests=new Map();
let generation=0,tail=Promise.resolve(),pollTimer,initialized=false,lifecycle=0;
const publish=()=>{for(const fn of [...listeners]){try{fn(state);}catch(error){console.error('UI subscriber failed',error);}}};
const queue=task=>{const result=tail.then(task,task);tail=result.catch(()=>{});return result;};
const current=(team,version)=>state.teamId===team && generation===version;
const api=(path,team)=>`${path}?team=${encodeURIComponent(team)}`;
const entry=team=>{if(!teams.has(team))teams.set(team,{snapshot:null,preferences:defaults(),history:emptyHistory(),historyStamp:''});return teams.get(team);};
function savePreferences(){entry(state.teamId).preferences={activeChannel:state.activeChannel,selectedAgentIds:[...state.selectedAgentIds],memoryEnabled:state.memoryEnabled,channelModes:{...state.channelModes},selectedLocally:state.selectedLocally};}
function displaySnapshot(snapshot){
  const preference=entry(state.teamId).preferences;
  updateMetadata(snapshot);
  state.teamName=snapshot.teamName || state.teams.find(team=>team.id===state.teamId)?.name || state.teamId;
  state.teamDescription=snapshot.teamDescription || state.teams.find(team=>team.id===state.teamId)?.description || '';
  state.sessions=snapshot.sessions;
  state.learnedMemories=snapshot.learnedMemories || {};
  state.connection=snapshot.connection || emptyConnection();
  state.evaluation=snapshot.evaluation || {mode:'real',runs:[]};
  const ids=AGENTS.map(agent=>agent.id);
  if(!preference.selectedLocally && Array.isArray(snapshot.selectedAgentIds))state.selectedAgentIds=snapshot.selectedAgentIds.filter(id=>ids.includes(id));
  else state.selectedAgentIds=preference.selectedAgentIds.filter(id=>ids.includes(id));
  if(!state.selectedAgentIds.length)state.selectedAgentIds=[...ids];
  for(const id of Object.keys(state.sessions))if(!state.channelModes[id])state.channelModes[id]=id==='group'?'task':'chat';
  state.activeChannel=state.sessions[state.activeChannel]?state.activeChannel:'group';
  state.client.reachable=true;state.client.loading=false;state.client.lastSynced=Date.now();
  if(state.client.errorSource!=='action'){state.client.error='';state.client.errorSource='';}
  savePreferences();
}
function applySnapshot(snapshot,team,version){
  if(!snapshot || !snapshot.sessions || typeof snapshot.sessions!=='object' || Array.isArray(snapshot.sessions))throw new Error('后端返回了无效状态。');
  if(snapshot.teamId && snapshot.teamId!==team)throw new Error('后端返回了其他团队的状态，已拒绝显示。');
  entry(team).snapshot=snapshot;
  if(current(team,version))displaySnapshot(snapshot);
}
async function request(url,options={}){
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
  try{
    const response=await fetch(url,{cache:'no-store',...options,signal:controller.signal});
    let body;try{body=await response.json();}catch{throw new Error('后端未连接。请使用项目后端服务启动，不能仅用静态文件服务。');}
    if(!response.ok)throw new Error(body.error || `请求失败（${response.status}）`);
    return body;
  }catch(error){if(error.name==='AbortError')throw new Error('连接超时，请重试。');throw error;}
  finally{clearTimeout(timeout);}
}
export const getState=()=>state;
export function subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);}
export function setChannel(id){if(state.sessions[id]){state.activeChannel=id;savePreferences();publish();}}
export function setMode(mode,channelId=state.activeChannel){if(['task','chat'].includes(mode)){state.channelModes[channelId]=mode;savePreferences();publish();}}
export function setMemoryEnabled(value){state.memoryEnabled=Boolean(value);savePreferences();publish();}
export function setParticipants(agentIds){
  const valid=[...new Set(agentIds)].filter(id=>AGENTS.some(agent=>agent.id===id));
  if(!valid.length || state.client.pendingAction || ACTIVE.includes(state.sessions.group?.run?.status))return false;
  state.selectedAgentIds=valid;state.selectedLocally=true;savePreferences();publish();return true;
}
export async function setTeam(teamId){
  if(state.client.pendingAction || !state.teams.some(team=>team.id===teamId))return false;
  if(teamId===state.teamId)return true;
  savePreferences();generation+=1;state.teamId=teamId;
  const cached=entry(teamId);Object.assign(state,cached.preferences);state.history=cached.history;
  state.client={loading:true,reachable:false,error:'',errorSource:'',pendingAction:null,pendingChannel:null,lastSynced:null};
  if(cached.snapshot){displaySnapshot(cached.snapshot);state.client.loading=true;state.client.reachable=false;}
  else{
    updateMetadata({agents:[],evaluation:{mode:'real',runs:[]}});
    state.sessions={group:{channelId:'group',messages:[],run:null}};state.learnedMemories={};state.connection=emptyConnection();state.evaluation={mode:'real',runs:[]};
    state.teamName=state.teams.find(team=>team.id===teamId)?.name || teamId;state.teamDescription=state.teams.find(team=>team.id===teamId)?.description || '';
  }
  publish();
  const result=await refreshState();void refreshHistory();return result;
}
export async function refreshTeams(){
  try{const result=await request('/api/teams');if(!Array.isArray(result.teams))throw new Error('后端未提供团队列表。');state.teams=result.teams;publish();return true;}
  catch(error){state.client.loading=false;state.client.error=error.message;state.client.errorSource='connection';publish();return false;}
}
export async function refreshState(){
  const team=state.teamId,version=generation,key=`${team}:${version}`;
  if(polling.has(key) || state.client.pendingAction)return false;
  polling.add(key);
  try{
    await queue(async()=>applySnapshot(await request(api('/api/state',team)),team,version));
    const stamp=JSON.stringify(Object.values(entry(team).snapshot.sessions).map(session=>[session.run?.id,session.run?.status]));
    if(current(team,version) && stamp!==entry(team).historyStamp){entry(team).historyStamp=stamp;void refreshHistory();}
    return true;
  }catch(error){if(current(team,version)){state.client.reachable=false;state.client.loading=false;state.client.error=error.message || '连接后端失败。';state.client.errorSource='connection';}return false;}
  finally{polling.delete(key);if(current(team,version))publish();}
}
export async function refreshHistory(){
  const team=state.teamId,version=generation,key=`${team}:${version}`;
  if(historyRequests.has(key))return historyRequests.get(key);
  const history=entry(team).history;history.loading=true;if(current(team,version))publish();
  const result=(async()=>{
    try{const body=await queue(()=>request(api('/api/runs',team)));if(body.teamId && body.teamId!==team)throw new Error('历史记录团队不匹配。');history.runs=body.runs || [];history.error='';return true;}
    catch(error){history.error=error.message;return false;}
    finally{history.loading=false;historyRequests.delete(key);if(current(team,version)){state.history=history;publish();}}
  })();historyRequests.set(key,result);return result;
}
export async function loadHistoryRun(runId){
  const team=state.teamId,version=generation;
  try{const run=await queue(()=>request(api('/api/runs/'+encodeURIComponent(runId),team)));if(!current(team,version))return null;if(run.teamId && run.teamId!==team)throw new Error('历史任务团队不匹配。');state.history.selected=run;return run;}
  catch(error){if(current(team,version)){state.history.error=error.message;publish();}return null;}
}
export async function startClient(){
  if(initialized)return;initialized=true;const version=++lifecycle;
  await refreshTeams();if(!initialized || lifecycle!==version)return;
  await refreshState();if(!initialized || lifecycle!==version)return;
  pollTimer=setInterval(()=>{void refreshState();},800);
}
export function stopClient(){clearInterval(pollTimer);pollTimer=undefined;initialized=false;lifecycle+=1;}
async function action(actionName,payload={},channelId=state.activeChannel){
  if(state.client.pendingAction || !state.client.reachable)return false;
  const team=state.teamId,version=generation;
  const runId=actionName!=='memory_confirm'?state.sessions[channelId]?.run?.id:null;
  state.client.pendingAction=actionName;state.client.pendingChannel=channelId;state.client.error='';state.client.errorSource='';publish();
  try{
    await queue(async()=>applySnapshot(await request('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-EvoMAC-Client':'1'},body:JSON.stringify({action:actionName,team,channelId,...(runId?{runId}:{}),...payload})}),team,version));
    if(current(team,version))void refreshHistory();return true;
  }catch(error){if(current(team,version)){state.client.error=error.message || '操作失败。';state.client.errorSource='action';}return false;}
  finally{if(current(team,version)){state.client.pendingAction=null;state.client.pendingChannel=null;publish();}}
}
export function sendMessage(text,channelId=state.activeChannel){const value=text.trim();if(!value || !state.connection.configured || ACTIVE.includes(state.sessions[channelId]?.run?.status))return Promise.resolve(false);const participants=channelId==='group'?[...state.selectedAgentIds]:[channelId];return action('send',{text:value,participants,memoryEnabled:state.memoryEnabled,mode:state.channelModes[channelId] || (channelId==='group'?'task':'chat')},channelId);}
export const pauseRun=(channelId=state.activeChannel)=>action('pause',{},channelId);
export const resumeRun=(channelId=state.activeChannel)=>action('resume',{},channelId);
export const resetChannel=(channelId=state.activeChannel)=>action('reset',{},channelId);
export const cancelRun=(channelId=state.activeChannel)=>action('cancel',{},channelId);
export const recoverRun=(channelId=state.activeChannel)=>action('recover',{},channelId);
export const resolveApproval=(approvalId,choiceId,answer='',modifiedArgs,channelId=state.activeChannel)=>action('approval',{approvalId,choiceId,answer,...(modifiedArgs?{modifiedArgs}:{})},channelId);
export const confirmMemory=(agentId,memoryId,confirmed)=>action('memory_confirm',{agentId,memoryId,confirmed});
