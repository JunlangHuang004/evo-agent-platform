// Fallback role labels only. Tools, memories and results come from the backend.
export const AGENTS = [
  {id:'coordinator',name:'Coordinator',label:'协调员',role:'任务拆解、依赖管理、结果汇总'},
  {id:'research',name:'Research',label:'研究员',role:'资料搜集、来源整理、信息交接'},
  {id:'executor',name:'Executor',label:'执行员',role:'文件处理、代码运行、测试与修复'},
  {id:'reviewer',name:'Reviewer',label:'审核员',role:'独立验证、需求核对、失败反馈'}
].map(agent => ({...agent, tools:[], memories:[], sandbox:{id:'未连接',policy:'等待后端配置',workspace:'未连接',memory:'未连接'}}));

export const EVALUATION = {mode:'real',dataset:'实际任务运行记录',runs:[]};

export function updateMetadata(snapshot) {
  if (Array.isArray(snapshot.agents)) {
    AGENTS.splice(0, AGENTS.length, ...snapshot.agents.map(agent => ({
      ...agent, tools:agent.tools || [], memories:agent.memories || [], sandbox:agent.sandbox || {}
    })));
  }
  if (snapshot.evaluation) Object.assign(EVALUATION, snapshot.evaluation, {runs:snapshot.evaluation.runs || []});
}
