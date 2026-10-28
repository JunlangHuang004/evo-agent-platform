"""Persistent task DAG, bounded agent runtimes, human gates and explicit handoffs."""
import copy
import json
import threading
import time
import uuid

from .agents import AGENTS
from .artifacts import ArtifactStore
from .memory import MemoryService
from .persistence import Repository
from .provider import ModelClient, ProviderError
from .runtime import ProtocolError, action_prompt, fingerprint, normalize_plan, parse_object, planning_prompt, strings, validate_dag, compress_messages
from .tools import ToolGateway, ToolError

CHANNELS = ['group', *AGENTS]
TERMINAL = {'done', 'partial', 'failed', 'cancelled', 'skipped', 'rejected'}
ACTIVE = {'running', 'waiting_human', 'waiting_children', 'paused', 'interrupted'}
AGENT_ROLES = {key: (value['name'], value['label'], value['role']) for key, value in AGENTS.items()}


def uid(prefix): return prefix + '-' + uuid.uuid4().hex[:16]
def clock_ms(): return int(time.time()*1000)
def iso_time(): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class ActionError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Stopped(Exception): pass
class BudgetExceeded(Exception): pass


class Platform:
    def __init__(self, config, provider=None, tools=None, artifacts=None, memory=None):
        self.config = config
        self.provider = provider or ModelClient(config)
        self.repo = Repository(config.data_dir)
        self.tools = tools or ToolGateway(config)
        self.artifacts = artifacts or ArtifactStore(config.data_dir)
        self.memory = memory or MemoryService(self.repo)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.workers = set()
        self.runners = {}
        self.task_workers = {}
        self.closing = False
        self._capabilities = self.tools.capabilities()
        self._capability_at = time.monotonic()
        self.state = self.repo.load_state() or {'activeChannel': 'group', 'selectedAgentIds': list(AGENTS), 'sessions': {}}
        for channel in CHANNELS:
            session = self.state['sessions'].setdefault(channel, {'channelId': channel, 'messages': [], 'run': None})
            run = session.get('run')
            if not run or run.get('status') not in ACTIVE:
                continue
            run['status'] = 'interrupted'
            run['userPaused'] = False
            # No upstream requests remain owned by this process after restart. Preserve
            # the unknown request cost conservatively, rather than a phantom reservation.
            reserved=run.pop('_reservedTokens',0)
            if reserved:
                run['tokenUsage']=run.get('tokenUsage',0)+reserved
                run['unconfirmedTokenEstimate']=run.get('unconfirmedTokenEstimate',0)+reserved
            run.setdefault('approvals', [])
            for task in run.get('steps', []):
                if task.get('status') not in TERMINAL:
                    task['status'] = 'pending'
            for call in run.get('calls', []):
                if call.get('status') in ('running', 'pending') and call.get('tool') != 'llm.chat':
                    if not any(a.get('callId') == call['id'] and a['status'] == 'pending' for a in run['approvals']):
                        call['status'] = 'unknown'
                        task = self._task(run, call.get('taskId'))
                        if task:
                            task.setdefault('_uncertainCalls', []).append(call['id'])
                elif call.get('status') == 'running':
                    call.update(status='interrupted', result='服务中断，模型请求不会自动视为成功。')
            self._event(run, 'checkpoint_restored', None, '已恢复执行现场；请点击恢复。未确认的外部写入不会自动重放。')
            self._message(session, 'system', '服务重启，执行现场已保留。点击恢复继续；未确认的副作用会要求人工核对。')
        self._persist()

    @staticmethod
    def _task(run, task_id):
        return next((task for task in run.get('steps', []) if task['id'] == task_id), None)

    def _current(self, channel, run_id):
        run = self.state['sessions'][channel].get('run')
        if not run or run['id'] != run_id:
            raise Stopped()
        return run

    def _message(self, session, role, text, agent_id=None):
        item = {'id': uid('msg'), 'role': role, 'text': self.config.scrub(str(text)), 'time': iso_time(), 'runId': (session.get('run') or {}).get('id')}
        if agent_id: item['agentId'] = agent_id
        session['messages'].append(item)
        session['messages'] = session['messages'][-200:]

    def _event(self, run, kind, agent_id, message, payload=None):
        event = {'id': uid('evt'), 'time': iso_time(), 'type': kind, 'agentId': agent_id, 'actor': agent_id,
                 'message': self.config.scrub(str(message)), 'payload': self.config.scrub(payload or {})}
        run.setdefault('events', []).append(event)
        run['events'] = run['events'][-1500:]
        self.repo.append_event(run['id'], event)
        return event

    def _persist(self):
        self.repo.save_state(self.config.scrub(self.state))
        for channel, session in self.state['sessions'].items():
            run = session.get('run')
            if run:
                clean = self.config.scrub(run)
                self.repo.save_run(clean, channel)
                self.repo.save_checkpoint(run['id'], clean)
        self.changed.notify_all() if getattr(self.lock, '_is_owned', lambda: False)() else None

    def _public(self, value):
        if isinstance(value, dict): return {k: self._public(v) for k, v in value.items() if not k.startswith('_')}
        if isinstance(value, list): return [self._public(v) for v in value]
        return value

    def snapshot(self):
        if time.monotonic()-self._capability_at > 15:
            self._capability_at = time.monotonic()
            self._capabilities = self.tools.capabilities()
        with self.lock:
            state = self._public(copy.deepcopy(self.state))
        state['learnedMemories'] = {agent: self.memory.list(agent) for agent in AGENTS}
        state['agents'] = []
        for agent_id, agent in AGENTS.items():
            meta = copy.deepcopy(agent)
            meta['id'] = agent_id
            meta['tools'] = self.tools.definitions(agent_id)
            meta['memories'] = []
            state['agents'].append(meta)
        caps = self._capabilities
        state['connection'] = {'configured': bool(self.config.api_key), 'backend': 'connected',
            'provider': self.config.provider, 'model': self.config.model, 'baseUrl': self.config.base_url,
            **caps, 'features': {'multiAgent': True, 'memory': True, 'tools': True,
                                'sandbox': bool(caps.get('dockerAvailable') and caps.get('dockerImageReady'))}}
        state['evaluation'] = {'mode': 'real', 'runs': self.repo.evaluation_runs()}
        return self.config.scrub(state)

    @staticmethod
    def _participants(value):
        if not isinstance(value, list) or not value or any(not isinstance(x, str) or x not in AGENTS for x in value):
            raise ActionError('至少选择一个有效 Agent。')
        return [agent for agent in AGENTS if agent in value]

    def action(self, payload):
        if not isinstance(payload, dict): raise ActionError('请求必须为 JSON 对象。')
        action = payload.get('action')
        if action == 'refresh_capabilities':
            self._capabilities = self.tools.capabilities()
            return self.snapshot()
        with self.changed:
            if action in ('memory_confirm','memory_share'):
                agent = payload.get('agentId')
                memory_id = payload.get('memoryId')
                if agent not in AGENTS or not isinstance(memory_id, str) or not isinstance(payload.get('confirmed'), bool):
                    raise ActionError('经验确认参数无效。')
                receipt={'type':'human','id':uid('confirmation')}
                try:
                    if action=='memory_share':
                        if not payload['confirmed']: raise ValueError('共享知识需要明确批准。')
                        result=self.memory.share(memory_id,agent,receipt)
                    else:
                        result=self.memory.confirm(memory_id, agent, payload['confirmed'], receipt)
                    source_run=result.get('sourceRunId',result.get('source_run','memory-confirmations'))
                    self.repo.append_event(source_run,{'id':receipt['id'],'time':iso_time(),'type':action,
                        'agentId':agent,'actor':'human','message':'用户明确确认记忆操作。',
                        'payload':{'memoryId':memory_id,'confirmed':payload['confirmed']}})
                except (ValueError,KeyError) as exc:
                    raise ActionError(str(exc)) from None
                return self.snapshot()
            if action == 'participants':
                if (self.state['sessions']['group'].get('run') or {}).get('status') in ACTIVE:
                    raise ActionError('任务进行中不能变更参与成员。', 409)
                self.state['selectedAgentIds'] = self._participants(payload.get('agentIds'))
                self._persist()
                return self.snapshot()
            channel = payload.get('channelId', 'group')
            if not isinstance(channel, str) or channel not in CHANNELS: raise ActionError('无效频道。')
            session = self.state['sessions'][channel]
            run = session.get('run')
            if payload.get('runId') and (not run or payload['runId'] != run['id']):
                raise ActionError('任务已改变，请刷新后重试。', 409)
            if action in ('cancel', 'reset'):
                if run and run.get('status') in ACTIVE:
                    run.update(status='cancelled', finishedAt=clock_ms())
                    for task in run['steps']:
                        if task.get('status') not in TERMINAL: task['status'] = 'cancelled'
                    for approval in run['approvals']:
                        if approval['status'] == 'pending': approval['status'] = 'cancelled'
                    for call in run['calls']:
                        if call['status'] in ('running','pending'):
                            call.update(status='cancelled',result='用户已取消；已发出请求的实际影响需要按原调用核对。')
                    self._event(run, 'run_cancelled', 'human', '用户取消任务；已发生的外部操作不会被撤回。')
                    self.tools.cancel(run['id'])
                    self.repo.save_run(self.config.scrub(run), channel)
                if action == 'reset':
                    self.state['sessions'][channel] = {'channelId': channel, 'messages': [], 'run': None}
                self._persist()
                return self.snapshot()
            if action == 'pause':
                if not run or run['status'] not in ('running', 'waiting_human'):
                    raise ActionError('当前任务不能暂停。', 409)
                run.update(userPaused=True, status='paused')
                self._event(run, 'run_paused', 'human', '已暂停派发新动作；已经发出的请求可能仍在结束。')
                self._persist()
                return self.snapshot()
            if action in ('resume', 'recover'):
                if not run or run['status'] not in ('paused', 'interrupted'):
                    raise ActionError('没有可恢复的任务。', 409)
                run.update(userPaused=False, status='running')
                self._event(run, 'run_resumed', 'human', '从已保存的执行位置继续，不重跑已完成子任务。')
                self._persist()
                self._start(channel, run['id'])
                return self.snapshot()
            if action == 'approval':
                self._resolve_approval(channel, run, payload)
                return self.snapshot()
            if action != 'send': raise ActionError('不支持此操作。', 400)
            text = payload.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                raise ActionError('消息不能为空，最多 16000 个字符。')
            if not self.config.api_key: raise ActionError('请先配置本地 LLM_API_KEY。', 503)
            if run and run['status'] in ACTIVE: raise ActionError('当前频道有未完成任务，请先取消或恢复。', 409)
            if any(t.name == 'run:'+channel and t.is_alive() for t in self.workers):
                raise ActionError('旧请求尚未结束，请稍后重试。', 409)
            participants = self._participants(payload.get('participants', self.state['selectedAgentIds'])) if channel == 'group' else [channel]
            mode = payload.get('mode', 'task' if channel == 'group' else 'chat')
            if mode not in ('task', 'chat'): raise ActionError('mode 必须为 task 或 chat。')
            if channel == 'group': self.state['selectedAgentIds'] = participants
            actor = 'coordinator' if 'coordinator' in participants else participants[0]
            run = {'id': uid('run'), 'title': text.strip(), 'mode': mode, 'status': 'running',
                   'startedAt': clock_ms(), 'finishedAt': None, 'participants': participants, 'respondingAgentId': actor,
                   'memoryEnabled': bool(payload.get('memoryEnabled', True)), 'userPaused': False,
                   'steps': [], 'calls': [], 'memoryHits': [], 'events': [], 'approvals': [], 'artifacts': [],
                   'messages': [], 'modelCalls': 0, 'tokenUsage': 0, 'activeMs': 0, 'budgetExtensions': 0,
                   'budget': {'tokens': self.config.max_run_tokens, 'modelCalls': self.config.max_model_calls,
                              'timeMs': self.config.max_run_seconds*1000, 'tasks': self.config.max_tasks,
                              'depth': self.config.max_depth}, '_planned': mode == 'chat'}
            if mode == 'chat':
                run['steps'] = [self._new_task('基础对话', text.strip(), actor, participants, agent=actor)]
            session['run'] = run
            self._message(session, 'human', text.strip())
            self._event(run, 'run_created', 'human', '创建任务并保存参与者和资源预算。', {'mode': mode, 'participants': participants})
            self._persist()
            self._start(channel, run['id'])
            return self.snapshot()

    def _start(self, channel, run_id):
        previous = self.runners.get(run_id)
        if previous and previous.is_alive():
            self.changed.notify_all()
            return
        worker = threading.Thread(target=self._drive, args=(channel, run_id), name='run:'+channel, daemon=True)
        self.workers.add(worker)
        self.runners[run_id] = worker
        worker.start()

    def _new_task(self, title, goal, delegated_by, participants, agent=None, depth=0, parent=None):
        return {'id': uid('task'), 'title': title[:300], 'goal': goal[:8000], 'acceptanceCriteria': ['交付可核对的结果并说明限制。'],
                'agentId': agent, 'preferredAgentId': agent, 'candidateAgentIds': list(participants),
                'requiredTools': [], 'requiredCapabilities': [], 'dependsOn': [], 'parentId': parent, 'childIds': [],
                'delegatedBy': delegated_by, 'inputs': {}, 'outputs': [], 'status': 'pending', 'result': '', 'error': None,
                'attempts': 0, 'retries': 0, 'revision': 0, 'depth': depth, 'rejectedBy': [], 'feedback': [], 'turns': 0, 'tokenUsage': 0}

    def _gate(self, channel, run_id):
        with self.changed:
            while True:
                run = self._current(channel, run_id)
                if self.closing or run['status'] in TERMINAL or run['status'] == 'interrupted': raise Stopped()
                if not run.get('userPaused'): return run
                self.changed.wait(.25)

    def _budget(self, channel, run_id, task, estimated_tokens=0):
        run = self._gate(channel, run_id)
        with self.lock:
            over = (run['modelCalls'] >= run['budget']['modelCalls'] or
                    run['tokenUsage'] + run.get('_reservedTokens',0) + estimated_tokens > run['budget']['tokens'] or run['activeMs'] >= run['budget']['timeMs'])
            if not over: return
            if run['budgetExtensions'] >= 1: raise BudgetExceeded('已达到人工扩展后的资源预算。')
            existing=next((a for a in run['approvals'] if a['type']=='budget' and a['status']=='pending'),None)
        result = self._ask(channel, run_id, task, 'budget', '任务即将超过资源预算',
                           '允许本任务的模型调用、Token 和执行时间预算增加一次（最多原预算的两倍）？',
                           [{'id': 'allow', 'label': '扩展一次并继续'}, {'id': 'deny', 'label': '停止并保留结果'}],
                           binding={'budget': copy.deepcopy(run['budget'])},existing=existing)
        if result.get('choiceId') != 'allow': raise BudgetExceeded('用户未批准扩展资源预算。')
        with self.lock:
            if run['budgetExtensions'] == 0:
                for key in ('tokens', 'modelCalls', 'timeMs'): run['budget'][key] *= 2
                run['budgetExtensions'] = 1
                self._event(run, 'budget_extended', 'human', '用户批准本任务预算扩展一次。')
                self._persist()

    def _model(self, channel, run_id, task, messages):
        # Full tool/audit records remain persisted; the model receives bounded working context.
        if sum(len(m.get('content', '')) for m in messages) > self.config.max_context_chars:
            messages[:] = compress_messages(messages, self.config.max_context_chars)
            with self.lock:
                self._event(self._current(channel, run_id), 'context_compressed', task.get('agentId'),
                            '压缩工作上下文；大内容已明确截断，完整工具结果仍在调用记录中。',
                            {'characters': sum(len(item['content']) for item in messages)})
        estimate = sum(len(m.get('content','').encode('utf-8')) for m in messages)//2 + self.config.max_completion_tokens + 128
        while True:
            self._budget(channel, run_id, task, estimate)
            with self.lock:
                run = self._current(channel, run_id)
                # Atomically reserve both a request and its context/completion token estimate.
                if (run['modelCalls'] >= run['budget']['modelCalls'] or
                        run['tokenUsage']+run.get('_reservedTokens',0)+estimate > run['budget']['tokens']):
                    continue
                run['modelCalls'] += 1
                run['_reservedTokens'] = run.get('_reservedTokens',0)+estimate
                call = {'id': uid('call'), 'taskId': task['id'], 'agentId': task['agentId'], 'tool': 'llm.chat',
                        'args': {'provider': self.config.provider, 'model': self.config.model}, 'status': 'running',
                        'result': '等待模型响应。', 'durationMs': 0, 'sandboxId': 'model-gateway'}
                run['calls'].append(call)
                self._persist()
                break
        started = clock_ms()
        settled=False
        time_accounted=False
        try:
            response = self.provider.complete(messages)
            if not isinstance(response, dict) or not isinstance(response.get('content'), str) or not response['content'].strip():
                raise ProviderError('模型响应格式无效。', 'protocol')
            with self.lock:
                current = self._current(channel, run_id)
                usage = response.get('usage') if isinstance(response.get('usage'), dict) else {}
                tokens = usage.get('total_tokens')
                used = tokens if isinstance(tokens, int) and tokens >= 0 else estimate
                current['tokenUsage'] += used
                current['_reservedTokens'] = max(0,current.get('_reservedTokens',0)-estimate)
                settled=True
                current['activeMs'] += max(0,clock_ms()-started)
                time_accounted=True
                task['tokenUsage'] += used
                call.update(status='success', result='已收到真实模型响应。', usage=usage, durationMs=clock_ms()-started)
                self._persist()
            self._gate(channel, run_id)
            return self.config.scrub(response['content'])
        except Exception as exc:
            with self.lock:
                # Failed/aborted upstream requests consumed real execution time too.
                # A successful response followed by cancellation already settled it.
                if not time_accounted:
                    run['activeMs'] = run.get('activeMs',0)+max(0,clock_ms()-started)
                    time_accounted=True
                if not settled:
                    run['_reservedTokens']=max(0,run.get('_reservedTokens',0)-estimate)
                call.update(status='cancelled' if isinstance(exc,Stopped) else 'failed', result=self.config.scrub(str(exc))[:700], durationMs=clock_ms()-started,
                            error={'code': getattr(exc,'code','provider'), 'retryable': bool(getattr(exc,'retryable',False))})
                if self.state['sessions'][channel].get('run') is not run:
                    # Reset removed this run from live state; keep its audit accurate
                    # without ever attaching a late result to the new conversation.
                    self.repo.save_run(self.config.scrub(run),channel)
                self._persist()
            raise

    def _model_retry(self, channel, run_id, task, messages):
        for attempt in range(self.config.max_retries+1):
            try: return self._model(channel, run_id, task, messages)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self.config.max_retries: raise
                with self.changed:
                    self._event(self._current(channel, run_id), 'provider_retry', task['agentId'], '临时模型网络错误，有限退避后重试。', {'attempt': attempt+1, 'code': exc.code})
                    self.changed.wait(min(4, .5*(2**attempt)))
                self._gate(channel, run_id)

    def _drive(self, channel, run_id):
        try:
            with self.lock: run = self._current(channel, run_id)
            if run['mode'] == 'chat':
                self._chat(channel, run_id)
                return
            if not run.get('_planned'):
                self._plan(channel, run_id)
            while True:
                run = self._gate(channel, run_id)
                with self.changed:
                    tasks = [task for task in run['steps'] if not task.get('_planning')]
                    for task in tasks:
                        if task['status'] != 'pending': continue
                        blocked = [self._task(run, dep) for dep in task['dependsOn']]
                        if any(dep is None or dep['status'] in ('failed','cancelled','rejected','skipped','partial') for dep in blocked):
                            task.update(status='skipped', error={'code':'dependency','message':'前置任务未完成。'})
                            self._event(run, 'dependency_blocked', task.get('agentId'), '前置任务失败，保留未执行子任务。', {'taskId':task['id']})
                    if tasks and all(task['status'] in TERMINAL for task in tasks): break
                    active = [task for task in tasks if task['status'] in ('running','assigned','retrying')]
                    available = max(0, self.config.parallel_tasks-len(active))
                    for task in tasks:
                        if not available: break
                        if task['status'] != 'pending' or not all(self._task(run, dep)['status'] == 'done' for dep in task['dependsOn']): continue
                        agent = self._choose(run, task)
                        if agent is None:
                            continue
                        task.update(agentId=agent, status='assigned')
                        task['attempts'] += 1
                        self._event(run, 'task_assigned', agent, '根据能力、权限、负载与成本分配子任务。', {'taskId':task['id'], 'delegatedBy':task['delegatedBy']})
                        thread = threading.Thread(target=self._task_worker, args=(channel,run_id,task['id']), name='task:'+task['id'],daemon=True)
                        self.task_workers[task['id']] = thread
                        self.workers.add(thread)
                        thread.start()
                        available -= 1
                    pending = [task for task in tasks if task['status'] not in TERMINAL]
                    if pending and all(task['status']=='pending' for task in pending) and not any(t['id'] in self.task_workers and self.task_workers[t['id']].is_alive() for t in tasks):
                        for task in pending:
                            task.update(status='failed',error={'code':'deadlock','message':'依赖或能力约束无法满足。'})
                        self._event(run, 'deadlock_detected', None, '未找到可执行的任务，已停止无效循环。')
                    self._aggregate_status(run)
                    self._persist()
                    self.changed.wait(.15)
            self._finish(channel, run_id)
        except Stopped:
            pass
        except Exception as exc:
            with self.changed:
                try: run = self._current(channel, run_id)
                except Stopped: return
                if run['status'] not in TERMINAL:
                    run.update(status='partial' if any(t.get('status')=='done' for t in run['steps']) else 'failed', finishedAt=clock_ms())
                    run['error'] = {'code': getattr(exc,'code','budget' if isinstance(exc,BudgetExceeded) else 'runtime'), 'message':self.config.scrub(str(exc))[:1000]}
                    for task in run['steps']:
                        if task['status'] not in TERMINAL:
                            task.update(status='failed' if task['status'] in ('running','assigned','retrying','waiting_human','waiting_children') else 'skipped',error=copy.deepcopy(run['error']))
                    for approval in run['approvals']:
                        if approval['status']=='pending': approval['status']='cancelled'
                    self._event(run, 'run_failed', None, run['error']['message'], run['error'])
                    self._message(self.state['sessions'][channel], 'system', '任务停止：'+run['error']['message'])
                    self.tools.cancel(run_id)
                    self._persist()
        finally:
            with self.changed:
                self.workers.discard(threading.current_thread())
                self.changed.notify_all()

    def _aggregate_status(self, run):
        if run['status'] in TERMINAL or run['status']=='interrupted': return
        run['status'] = 'paused' if run.get('userPaused') else 'waiting_human' if any(a['status']=='pending' for a in run['approvals']) else 'running'

    def _choose(self, run, task):
        candidates = []
        busy_agents = {t['agentId'] for t in run['steps'] if t['status'] in ('running','assigned','retrying')}
        for agent_id in task.get('candidateAgentIds',run['participants']):
            if agent_id not in run['participants'] or agent_id in task.get('rejectedBy',[]): continue
            # A resumed task's private workspace, pending approval and working context belong
            # to its original owner. Reassignment is only valid before any actions were taken.
            if (task.get('_pendingTool') or task.get('_uncertainCalls') or task.get('_messages')) and task.get('agentId') and agent_id!=task['agentId']:
                continue
            agent = AGENTS[agent_id]
            if not set(task.get('requiredTools',[])).issubset(set(agent['tools'])): continue
            if not set(task.get('requiredCapabilities',[])).issubset(set(agent.get('capabilities',[]))): continue
            if agent_id in busy_agents: continue
            load = sum(1 for session in self.state['sessions'].values() for t in (session.get('run') or {}).get('steps',[]) if t.get('agentId')==agent_id and t.get('status') in ('running','assigned','retrying'))
            cost = float(agent.get('cost',1)) + load*3 - (2 if task.get('preferredAgentId')==agent_id else 0)
            candidates.append((cost,agent_id))
        return min(candidates)[1] if candidates else None

    def _plan(self, channel, run_id):
        with self.lock:
            run = self._current(channel,run_id)
            actor = run['respondingAgentId']
            planning = next((t for t in run['steps'] if t.get('_planning')),None)
            if not planning:
                planning = self._new_task('任务拆解与依赖规划',run['title'], 'human',run['participants'],actor)
                planning.update(_planning=True,status='running')
                run['steps'].append(planning)
            agents=[{'id':agent,**AGENTS[agent]} for agent in run['participants']]
            messages = [{'role':'system','content':planning_prompt(agents,self.config.max_tasks-1)}, {'role':'user','content':run['title']}]
        for attempt in range(3):
            content = self._model_retry(channel,run_id,planning,messages)
            try:
                plan=parse_object(content)
                tasks=normalize_plan(plan,run['participants'],uid,self.config.max_tasks-1)
                break
            except ProtocolError as exc:
                if attempt==2: raise
                messages.extend([{'role':'assistant','content':content},{'role':'user','content':str(exc)+' 请修正并只返回JSON。'}])
        with self.lock:
            self._gate(channel,run_id)
            planning.update(status='done',result=str(plan.get('summary','结构化计划已建立。'))[:2000])
            run['steps'].extend(tasks)
            run['_planned']=True
            self._event(run,'dag_created',actor,'已创建并验证无循环的任务DAG。',{'taskIds':[task['id'] for task in tasks]})
            self._message(self.state['sessions'][channel],'agent',planning['result'],actor)
            self._persist()

    def _chat(self, channel, run_id):
        with self.lock:
            run=self._current(channel,run_id)
            task=run['steps'][0]
            actor=task['agentId']
            task['status']='running'
            history=self.state['sessions'][channel]['messages'][-16:]
            messages=[{'role':'system','content':f'你是{AGENTS[actor]["name"]}，职责：{AGENTS[actor]["role"]}。当前是普通对话模式，不启动工具或多Agent调度。直接回答用户；不要声称已经执行工具。'}]
            messages.extend({'role':'user' if item['role']=='human' else 'assistant','content':item['text']} for item in history if item['role'] in ('human','agent'))
        try:
            content=self._model_retry(channel,run_id,task,messages)
            with self.lock:
                self._gate(channel,run_id)
                task.update(status='done',result=content)
                run.update(status='done',finishedAt=clock_ms())
                self._message(self.state['sessions'][channel],'agent',content,actor)
                self._event(run,'chat_completed',actor,'普通模型对话结束，未执行工具。')
                self._persist()
        except Stopped: raise
        except Exception as exc:
            with self.lock:
                task.update(status='failed',error={'code':getattr(exc,'code','provider'),'message':str(exc)})
            raise

    def _task_worker(self, channel, run_id, task_id):
        try:
            with self.lock:
                run=self._current(channel,run_id)
                task=self._task(run,task_id)
                task['status']='running'
                self._event(run,'task_accepted',task['agentId'],'Agent 接受结构化子任务。',{'taskId':task_id})
                self._persist()
            self._prepare_task(channel,run_id,task)
            result=self._agent_loop(channel,run_id,task)
            if result is not None:
                with self.lock:
                    self._gate(channel,run_id)
                    task.update(status=result['status'],result=result['text'],finishedAt=clock_ms())
                    self._event(run,'task_finished',task['agentId'],'子任务结束。',{'taskId':task_id,'status':task['status']})
                    self._message(self.state['sessions'][channel],'agent',task['result'],task['agentId'])
                    self._record_memory(run,task,channel,result.get('reflection'))
                    self._persist()
        except Stopped:
            pass
        except Exception as exc:
            with self.lock:
                try: run=self._current(channel,run_id)
                except Stopped: return
                task=self._task(run,task_id)
                if run['status'] in TERMINAL or self.closing: return
                retryable=bool(getattr(exc,'retryable',False))
                if retryable and task['retries']<self.config.max_retries:
                    task['retries']+=1
                    task.update(status='pending',error={'code':getattr(exc,'code','transient'),'message':str(exc)[:1000]})
                    self._event(run,'task_retry',task['agentId'],'暂时性失败后保留现场并重新调度。',{'taskId':task_id,'retry':task['retries']})
                else:
                    task.update(status='failed',error={'code':getattr(exc,'code','budget' if isinstance(exc,BudgetExceeded) else 'runtime'),'message':self.config.scrub(str(exc))[:1200]})
                    self._event(run,'task_failed',task['agentId'],task['error']['message'],{'taskId':task_id,'error':task['error']})
                    self._record_memory(run,task,channel,None)
                self._persist()
        finally:
            with self.changed:
                self.workers.discard(threading.current_thread())
                self.changed.notify_all()

    def _prepare_task(self, channel, run_id, task):
        run=self._gate(channel,run_id)
        dependency_ids=set(task.get('dependsOn',[]))
        if task.get('finalSummary'):
            pending=list(dependency_ids)
            while pending:
                parent=self._task(run,pending.pop())
                for earlier in (parent or {}).get('dependsOn',[]):
                    if earlier not in dependency_ids:
                        dependency_ids.add(earlier)
                        pending.append(earlier)
        for dep_id in dependency_ids:
            dep=self._task(run,dep_id)
            if not dep: continue
            for artifact_id in dep.get('outputs',[]):
                self._grant(run,artifact_id,task['agentId'],'orchestrator:'+task['id'])
        for call_id in list(task.get('_uncertainCalls',[])):
            call=next(c for c in run['calls'] if c['id']==call_id)
            result=self._ask(channel,run_id,task,'reconciliation','核对中断前的工具操作',
                '无法确定此调用是否已产生外部影响。请核对目标后确认；系统不会自动重放。',
                [{'id':'executed','label':'确认已执行，继续核对结果'},{'id':'not_executed','label':'确认未执行，允许重新尝试'},{'id':'stop','label':'停止此任务'}],
                binding={'callId':call_id,'tool':call['tool'],'args':call['args']})
            if result.get('choiceId')=='stop': raise BudgetExceeded('用户停止无法确认的中断操作。')
            with self.lock:
                if result.get('choiceId')=='not_executed':
                    call['status']='not_executed'
                    call['result']={'ok':False,'reconciled':True,'message':'人工确认原调用未执行；可通过新的工具动作重新尝试。'}
                else:
                    call['status']='reconciled'
                    call['result']={'ok':False,'reconciled':True,'message':'人工确认曾执行，结果仍需工具读取核验；不自动标记成功。'}
                pending=task.get('_pendingTool')
                if pending and pending.get('callId')==call_id:
                    task.pop('_pendingTool',None)
                task['_uncertainCalls'].remove(call_id)
                self._persist()
        with self.lock:
            actor=task['agentId']
            memories=self.memory.recall(actor,task['goal'],channel) if run['memoryEnabled'] else []
            for memory in memories:
                owner=memory.get('ownerAgentId',memory.get('owner',actor))
                existing_hit=next((hit for hit in run['memoryHits'] if hit['memoryId']==memory['id'] and hit['taskId']==task['id']),None)
                if existing_hit is not None:
                    existing_hit.setdefault('ownerAgentId',owner)
                else:
                    run['memoryHits'].append({'agentId':actor,'ownerAgentId':owner,'memoryId':memory['id'],'taskId':task['id'],'applied':True,
                        'reason':'作为已确认且适用的执行策略注入本任务上下文。'})
                    self._event(run,'memory_applied',actor,'检索并采用已确认经验。',{'taskId':task['id'],'memoryId':memory['id']})
            deps=[{'id':d['id'],'title':d['title'],'agentId':d.get('agentId'),'acceptanceCriteria':d.get('acceptanceCriteria',[]),
                   'revision':d.get('revision',0),'validation':copy.deepcopy(d.get('validation')),
                   'result':d.get('result',''),'outputs':d.get('outputs',[])} for d in run['steps'] if d['id'] in dependency_ids]
            source_refs=[]
            for call in run['calls']:
                if call.get('taskId') not in dependency_ids or call.get('status')!='success' or call.get('tool') not in ('web.search','web.fetch'):
                    continue
                receipt=call.get('result') or {}
                for source in receipt.get('sources',receipt.get('results',[])):
                    if isinstance(source,dict) and source.get('url') and not any(ref['url']==source['url'] for ref in source_refs):
                        source_refs.append({'url':source['url'],'title':source.get('title',''),'callId':call['id'],'agentId':call['agentId']})
                if receipt.get('url') and not any(ref['url']==receipt['url'] for ref in source_refs):
                    source_refs.append({'url':receipt['url'],'title':receipt.get('title',''),'callId':call['id'],'agentId':call['agentId']})
            context={'runGoal':run['title'],'task':{key:task[key] for key in ('id','title','goal','acceptanceCriteria','feedback','inputs','childIds','finalSummary') if key in task},'participants':run['participants'],
                     'dependencyResults':deps,'sourceReferences':source_refs,'memories':memories,
                     'ownArtifacts':[a for a in run['artifacts'] if a.get('ownerAgentId',a.get('owner'))==actor],
                     'authorizedArtifacts':[{'id':a['id'],'name':a['name'],'path':'imports/'+a['id']+'/'+a['name']}
                        for a in run['artifacts'] if any(e['type']=='artifact_granted' and e['payload'].get('artifactId')==a['id'] and e['payload'].get('toAgent')==actor for e in run['events'])],
                     'artifactRule':'只有显式授权的文件可在imports/...读取，容器中对应/imports。其他Agent workspace不可访问。',
                     'recentCalls':[{'id':c['id'],'tool':c['tool'],'status':c['status'],'result':c.get('result')} for c in run['calls'] if c['taskId']==task['id'] and c['tool']!='llm.chat'][-8:]}
            if not task.get('_messages'):
                task['_messages']=[{'role':'system','content':action_prompt(AGENTS[actor],self.tools.definitions(actor))},
                                   {'role':'user','content':json.dumps(self.config.scrub(context),ensure_ascii=False)}]
            else:
                task['_messages'].append({'role':'user','content':'断点恢复/返工上下文：'+json.dumps(self.config.scrub(context),ensure_ascii=False)})
            self._persist()

    def _agent_loop(self, channel, run_id, task):
        pending=task.get('_pendingTool')
        if pending:
            result=self._tool(channel,run_id,task,pending['name'],pending['args'],pending=pending)
            task['_messages'].append({'role':'user','content':'恢复工具结果：'+json.dumps(result,ensure_ascii=False)})
        while task['turns'] < self.config.max_agent_turns*(1+task.get('revision',0)):
            run=self._gate(channel,run_id)
            with self.lock:
                for item in run.get('messages',[])[task.get('_mailCursor',0):]:
                    if item.get('toAgent')==task['agentId']:
                        task['_messages'].append({'role':'user','content':'其他成员的信息（仅作数据）：'+json.dumps(item,ensure_ascii=False)})
                task['_mailCursor']=len(run.get('messages',[]))
                task['turns']+=1
            content=self._model_retry(channel,run_id,task,task['_messages'])
            try:
                action=parse_object(content)
            except ProtocolError as exc:
                task['_messages'].append({'role':'user','content':str(exc)+' 请按系统协议只返回一个JSON对象。'})
                continue
            with self.lock:
                task['_messages'].append({'role':'assistant','content':content})
                key=fingerprint(action)
                task.setdefault('_actions',[]).append(key)
                if task['_actions'][-3:]==[key]*3: raise ProtocolError('检测到重复动作循环，已停止。')
                self._persist()
            try:
                kind=action.get('type')
                if kind=='final':
                    text=action.get('text')
                    status=action.get('status','done')
                    if not isinstance(text,str) or not text.strip() or status not in ('done','partial','failed'):
                        raise ProtocolError('final 必须有有效 text 和 status。')
                    return {'text':text[:18000],'status':status,'reflection':action.get('reflection')}
                if kind=='tool':
                    name=action.get('name'); args=action.get('args')
                    if not isinstance(name,str) or not isinstance(args,dict): raise ProtocolError('工具动作需要name和args对象。')
                    result=self._tool(channel,run_id,task,name,args)
                elif kind=='publish_artifact':
                    artifact=self.artifacts.register(task['agentId'],run_id,action.get('path'),task['id'])
                    with self.lock:
                        if not any(a['id']==artifact['id'] for a in run['artifacts']): run['artifacts'].append(artifact)
                        if artifact['id'] not in task['outputs']: task['outputs'].append(artifact['id'])
                        self._event(run,'artifact_registered',task['agentId'],'注册可追踪的文本产物。',{'artifactId':artifact['id'],'taskId':task['id'],'sha256':artifact['sha256']})
                        self._persist()
                    result={'ok':True,'artifact':artifact}
                elif kind=='grant_artifact':
                    artifact=self.artifacts.get(action.get('artifactId'))
                    if artifact.get('ownerAgentId',artifact.get('owner'))!=task['agentId']: raise ProtocolError('只能授权自己注册的产物。')
                    result={'ok':True,'grant':self._grant(run,artifact['id'],action.get('toAgent'),task['agentId'])}
                elif kind=='delegate':
                    result={'ok':True,'task':self._delegate(run,task,action.get('task'))}
                elif kind=='await_task':
                    result=self._await_task(channel,run_id,task,action.get('taskId'))
                elif kind=='message':
                    target=action.get('toAgent'); text=action.get('text')
                    if target not in run['participants'] or target==task['agentId'] or not isinstance(text,str) or not text.strip():
                        raise ProtocolError('只能向本任务的其他成员发送非空消息。')
                    with self.lock:
                        record={'id':uid('handoff'),'fromAgent':task['agentId'],'toAgent':target,'text':self.config.scrub(text[:5000]),'taskId':task['id'],'time':iso_time()}
                        run['messages'].append(record)
                        self._event(run,'agent_message',task['agentId'],'向其他成员交接信息。',record)
                        self._persist()
                    result={'ok':True,'messageId':record['id']}
                elif kind=='ask_human':
                    question=action.get('question')
                    if not isinstance(question,str) or not question.strip(): raise ProtocolError('澄清问题不能为空。')
                    choices=strings(action.get('choices',[]),limit=5,width=200)
                    result=self._ask(channel,run_id,task,'clarification',question[:300],str(action.get('description',''))[:2000],
                                     [{'id':'option-'+str(i),'label':label} for i,label in enumerate(choices)],binding={'question':question})
                elif kind=='reject':
                    reason=action.get('reason')
                    if not isinstance(reason,str) or not reason.strip(): raise ProtocolError('拒绝任务必须说明原因。')
                    with self.lock:
                        task['rejectedBy'].append(task['agentId'])
                        task.update(status='pending',agentId=None)
                        task['_messages']=[]
                        self._event(run,'task_rejected',task['rejectedBy'][-1],reason,{'taskId':task['id']})
                        self._persist()
                    return None
                elif kind=='review':
                    result=self._review(run,task,action)
                elif kind=='accept':
                    result={'ok':True,'message':'任务已接受，请执行获准动作。'}
                else:
                    raise ProtocolError('未知动作类型。')
            except (ToolError,ProtocolError,ValueError,KeyError,TypeError) as exc:
                result={'ok':False,'error':{'code':getattr(exc,'code','parameters'),'message':self.config.scrub(str(exc))[:1200],
                                          'retryable':bool(getattr(exc,'retryable',False))}}
                with self.lock:
                    self._event(run,'action_rejected',task['agentId'],result['error']['message'],{'taskId':task['id'],'action':action.get('type')})
            with self.lock:
                task['_messages'].append({'role':'user','content':'动作的真实结果：'+json.dumps(self.config.scrub(result),ensure_ascii=False)[:24000]})
                self._persist()
        raise BudgetExceeded('该Agent达到动作轮数预算，保留当前产物与记录。')

    def _grant(self, run, artifact_id, to_agent, authorized_by):
        if to_agent not in run['participants']: raise ProtocolError('Artifact只能授权给本run参与者。')
        artifact=self.artifacts.get(artifact_id)
        if artifact.get('runId')!=run['id']: raise ProtocolError('禁止跨run隐式共享Artifact。')
        grant=self.artifacts.grant(artifact_id,to_agent,run['id'],authorized_by)
        with self.lock:
            if not any(event['type']=='artifact_granted' and event['payload'].get('artifactId')==artifact_id and event['payload'].get('toAgent')==to_agent for event in run['events']):
                self._event(run,'artifact_granted',authorized_by,'显式授权只读Artifact副本。',{'artifactId':artifact_id,'toAgent':to_agent,'grant':grant})
                self._persist()
        return grant

    def _delegate(self, run, parent, spec):
        if not isinstance(spec,dict): raise ProtocolError('委派任务必须是结构化对象。')
        with self.lock:
            if len(run['steps'])>=run['budget']['tasks']: raise ProtocolError('已达到子任务数量上限。')
            if parent['depth']>=run['budget']['depth']: raise ProtocolError('已达到委派深度上限。')
            title=spec.get('title') or spec.get('goal')
            if not isinstance(title,str) or not title.strip(): raise ProtocolError('委派目标不能为空。')
            target=spec.get('agentId')
            if target is not None and (target not in run['participants'] or target==parent['agentId']):
                raise ProtocolError('委派对象必须为本任务的其他参与者。')
            if any(t.get('parentId')==parent['id'] and t['goal']==spec.get('goal',title) for t in run['steps']):
                raise ProtocolError('拒绝重复委派相同目标。')
            task=self._new_task(title,str(spec.get('goal',title)),parent['agentId'],run['participants'],target,parent['depth']+1,parent['id'])
            task['acceptanceCriteria']=strings(spec.get('acceptanceCriteria',task['acceptanceCriteria']))
            task['requiredTools']=strings(spec.get('requiredTools',[]))
            task['requiredCapabilities']=strings(spec.get('requiredCapabilities',[]))
            dependencies=spec.get('dependsOn',[])
            if not isinstance(dependencies,list) or any(not isinstance(x,str) or not self._task(run,x) for x in dependencies):
                raise ProtocolError('委派依赖不存在。')
            if parent['id'] in dependencies: raise ProtocolError('子任务不能等待尚未交付的父任务。')
            task['dependsOn']=dependencies
            task['candidateAgentIds']=[a for a in run['participants'] if a!=parent['agentId']]
            validate_dag([*run['steps'],task])
            run['steps'].append(task)
            parent['childIds'].append(task['id'])
            self._event(run,'task_delegated',parent['agentId'],'Agent自主创建子任务。',{'taskId':task['id'],'parentId':parent['id'],'preferredAgentId':target})
            self._persist()
            return self._public(task)

    def _await_task(self, channel, run_id, task, child_id):
        with self.changed:
            run=self._current(channel,run_id)
            child=self._task(run,child_id)
            if not child or child_id not in task['childIds']: raise ProtocolError('只能等待当前任务直接委派的子任务。')
            def reaches(node,target,seen=None):
                seen=set() if seen is None else seen
                if node in seen: return False
                seen.add(node)
                current=self._task(run,node)
                return bool(current and (target in current['dependsOn'] or any(reaches(dep,target,seen) for dep in current['dependsOn'])))
            if reaches(child_id,task['id']): raise ProtocolError('检测到父子任务循环等待。')
            task['status']='waiting_children'
            self._persist()
            started=time.monotonic()
            while child['status'] not in TERMINAL:
                self._gate(channel,run_id)
                if time.monotonic()-started>self.config.max_run_seconds: raise BudgetExceeded('等待子任务超时。')
                self.changed.wait(.2)
            task['status']='running'
            for artifact_id in child['outputs']: self._grant(run,artifact_id,task['agentId'],'orchestrator:'+task['id'])
            self._persist()
            return {'ok':child['status']=='done','taskId':child_id,'status':child['status'],'result':child['result'],'outputs':child['outputs']}

    @staticmethod
    def _review_snapshot(run, task):
        artifact_ids=set(task.get('outputs',[]))
        return {'targetRevision':task.get('revision',0),
                'artifactHashes':{artifact['id']:artifact['sha256'] for artifact in run.get('artifacts',[])
                                  if artifact['id'] in artifact_ids and artifact.get('sha256')}}

    def _valid_review(self, run, task):
        validation=task.get('validation') or {}
        snapshot=self._review_snapshot(run,task)
        return bool(validation.get('verified') and validation.get('verdict')=='pass'
                    and validation.get('targetRevision')==snapshot['targetRevision']
                    and validation.get('artifactHashes')==snapshot['artifactHashes'])

    @staticmethod
    def _has_code_work(run, task):
        if set(task.get('requiredTools',[])) & {'code.run','code.test'}:
            return True
        if any(call.get('taskId')==task['id'] and call.get('tool') in ('code.run','code.test') for call in run.get('calls',[])):
            return True
        return any(artifact['id'] in task.get('outputs',[]) and artifact.get('name','').lower().endswith(('.py','.js','.mjs','.ts','.tsx','.jsx','.c','.cpp','.java','.rs','.go','.sh'))
                   for artifact in run.get('artifacts',[]))

    def _review(self, run, reviewer, action):
        if reviewer['agentId']!='reviewer': raise ProtocolError('只有Reviewer可提交审查决定。')
        verdict=action.get('verdict')
        if verdict not in ('pass','rework','reject'): raise ProtocolError('审查决定无效。')
        evidence_ids=strings(action.get('evidenceCallIds',[]))
        with self.lock:
            target=self._task(run,action.get('taskId'))
            if not target or target['id']==reviewer['id'] or target['status'] not in ('done','partial'):
                raise ProtocolError('审查对象必须是其他已交付子任务。')
            if verdict=='rework' and target['revision']>=self.config.max_retries:
                raise ProtocolError('该子任务已达到返工次数上限。')
            snapshot=self._review_snapshot(run,target)
            evidence=[call for call in run['calls'] if call['id'] in evidence_ids and call['taskId']==reviewer['id']
                      and call.get('agentId')=='reviewer' and call['tool']=='code.test' and call['status']=='success'
                      and isinstance(call.get('result'),dict) and call['result'].get('ok') is True and call['result'].get('exitCode')==0
                      and call.get('reviewTargets',{}).get(target['id'])==snapshot]
            validation={'verdict':verdict,'feedback':str(action.get('feedback',''))[:3000],
                        'verified':bool(evidence) and verdict=='pass','evidenceCallIds':[call['id'] for call in evidence],
                        'reviewerTaskId':reviewer['id'],**snapshot}
            if verdict=='rework':
                target.setdefault('validationHistory',[]).append(validation)
                target['revision']+=1
                target['feedback'].append(validation['feedback'])
                target.update(status='pending',result='',error=None,outputs=[])
                target.pop('validation',None)
                target['_messages']=[]
            else:
                target['validation']=validation
                if verdict=='reject':
                    target.update(status='failed',error={'code':'review','message':validation['feedback']})
            self._event(run,'review_'+verdict,'reviewer','审核员给出审查决定。',{'taskId':target['id'],'validation':validation})
            self._persist()
            accepted=verdict!='pass' or validation['verified']
            result={'ok':accepted,'validation':validation,'status':target['status']}
            if not accepted:
                result['error']={'code':'verification','message':'独立验收未通过：需要审核员本任务成功的 code.test 证据，且目标 revision 与产物 SHA256 必须匹配。'}
            return result

    def _ask(self, channel, run_id, task, kind, title, description, choices, binding=None, existing=None):
        with self.changed:
            run=self._current(channel,run_id)
            approval=existing
            if approval is None and kind=='budget':
                approval=next((item for item in run['approvals'] if item['type']=='budget' and item['status']=='pending'),None)
            if approval is None:
                approval={'id':uid('approval'),'type':kind,'title':title,'description':description,'choices':choices,
                          'status':'pending','createdAt':clock_ms(),'expiresAt':clock_ms()+self.config.approval_ttl*1000,
                          'agentId':task['agentId'],'taskId':task['id'],'binding':copy.deepcopy(binding or {})}
                approval['args']=copy.deepcopy((binding or {}).get('args'))
                approval['callId']=(binding or {}).get('callId')
                approval['bindingHash']=fingerprint(approval['binding'])
                run['approvals'].append(approval)
                self._event(run,'human_requested',task['agentId'],title,{'approvalId':approval['id'],'taskId':task['id'],'type':kind})
                self._message(self.state['sessions'][channel],'agent',title+'：'+description,task['agentId'])
            previous=task['status']
            task.update(status='waiting_human',_approvalId=approval['id'])
            self._aggregate_status(run)
            self._persist()
            while approval['status']=='pending':
                self._gate(channel,run_id)
                if clock_ms()>approval['expiresAt']:
                    approval.update(status='expired',response={'choiceId':'deny','answer':'授权已过期，未执行动作。'})
                    self._event(run,'approval_expired',task['agentId'],'审批已过期，不执行受控动作。',{'approvalId':approval['id']})
                    break
                self.changed.wait(.25)
            self._gate(channel,run_id)
            task['status']='running' if previous in ('waiting_human','pending','assigned') else previous
            task.pop('_approvalId',None)
            if kind=='permission': approval['_consumed']=True
            self._aggregate_status(run)
            self._persist()
            return copy.deepcopy(approval.get('response',{'choiceId':'deny'}))

    def _resolve_approval(self, channel, run, payload):
        if not run or run['status'] not in ACTIVE: raise ActionError('没有可回复的人工请求。',409)
        approval=next((a for a in run['approvals'] if a['id']==payload.get('approvalId') and a['status']=='pending'),None)
        if not approval: raise ActionError('请求已结束、已回复或不属于当前任务。',409)
        if clock_ms()>approval['expiresAt']:
            approval.update(status='expired',response={'choiceId':'deny'})
            self._persist()
            raise ActionError('授权已过期，请重新请求。',409)
        if fingerprint(approval['binding'])!=approval['bindingHash']:
            raise ActionError('审批动作参数发生变化，拒绝复用授权。',409)
        choice=payload.get('choiceId')
        answer=payload.get('answer','')
        if not isinstance(answer,str) or len(answer)>8000: raise ActionError('人工答复无效。')
        allowed={item['id'] for item in approval['choices']}
        if choice=='answer' and approval['type']=='clarification' and answer.strip():
            pass
        elif choice=='modify' and approval['type']=='permission':
            modified=payload.get('modifiedArgs')
            if not isinstance(modified,dict) or len(json.dumps(modified,ensure_ascii=False))>40000:
                raise ActionError('修改参数必须为有效 JSON 对象。')
            # CAS is part of the authorized snapshot and cannot be removed by an edit.
            original=approval['binding'].get('args',{})
            if 'expectedSha256' in original: modified['expectedSha256']=original['expectedSha256']
            approval.update(status='modified',response={'choiceId':'modify','modifiedArgs':self.config.scrub(copy.deepcopy(modified))})
            self._event(run,'approval_modified','human','用户修改了动作参数；必须重新审批后才会执行。',{'approvalId':approval['id']})
            self._persist()
            return
        elif choice not in allowed:
            raise ActionError('请选择此请求允许的操作。')
        approval.update(status='resolved',resolvedAt=clock_ms(),response={'choiceId':choice,'answer':answer})
        self._event(run,'human_replied','human','用户回复人工请求。',{'approvalId':approval['id'],'choiceId':choice})
        self._aggregate_status(run)
        self._persist()

    def _tool(self, channel, run_id, task, name, args, pending=None):
        self._gate(channel,run_id)
        self._budget(channel,run_id,task)
        if name not in AGENTS[task['agentId']]['tools']:
            raise ToolError('permission','该Agent没有此工具权限。')
        with self.lock:
            run=self._current(channel,run_id)
            args=copy.deepcopy(args)
            if not pending and name in ('code.run','code.test'):
                remaining=max(1,(run['budget']['timeMs']-run['activeMs'])/1000)
                requested=args.get('timeout',20)
                if isinstance(requested,(int,float)) and not isinstance(requested,bool):
                    args['timeout']=min(requested,remaining,60)
            if pending:
                call=next(c for c in run['calls'] if c['id']==pending['callId'])
            else:
                call={'id':uid('call'),'taskId':task['id'],'agentId':task['agentId'],'tool':name,'args':args,
                      'status':'running','result':None,'durationMs':0,'sandboxId':f'{task["agentId"]}/{run_id}'}
                if task['agentId']=='reviewer' and name=='code.test':
                    call['reviewTargets']={target['id']:self._review_snapshot(run,target) for target in run['steps']
                                           if target['id']!=task['id'] and target.get('status') in ('done','partial')}
                run['calls'].append(call)
            effect_key=pending.get('effectKey') if pending else fingerprint({'run':run_id,'task':task['id'],'revision':task['revision'],'turn':task['turns'],'name':name,'args':args})
            effect=self.repo.effect(effect_key)
            if effect and effect['status']=='completed':
                call.update(status='success' if effect['result'].get('ok') else 'failed',result=effect['result'])
                return {**effect['result'],'callId':call['id']}
            if not pending:
                effect=self.repo.reserve_effect(effect_key,{'runId':run_id,'taskId':task['id'],'tool':name,'args':args})
                if not effect.get('new'):
                    raise ToolError('uncertain','发现未确认的同一工具调用，拒绝自动重放。')
            task['_pendingTool']={'name':name,'args':args,'callId':call['id'],'effectKey':effect_key,'phase':'executing'}
            self._persist()
        started=clock_ms()
        approval_wait_ms=0
        approved=False
        try:
            if pending and pending.get('phase')=='approval':
                result={'ok':False,'needsApproval':True,'args':args,'approval':{'title':'恢复待审批动作','description':'继续保存的具体工具动作。'}}
            else:
                result=self._execute_retry(channel,run_id,task,name,args,False)
            while result.get('needsApproval'):
                args=copy.deepcopy(result.get('args',args))
                binding={'callId':call['id'],'tool':name,'args':args,'agentId':task['agentId'],'runId':run_id}
                with self.lock:
                    call.update(status='pending',args=args,result='等待具体动作审批。')
                    task['_pendingTool'].update(args=args,phase='approval')
                    existing=next((a for a in reversed(run['approvals']) if a.get('callId')==call['id'] and not a.get('_consumed')
                                   and a['status'] in ('pending','resolved','modified','expired') and a.get('bindingHash')==fingerprint(binding)),None)
                    self._persist()
                details=result.get('approval',{})
                wait_started=clock_ms()
                decision=self._ask(channel,run_id,task,'permission',details.get('title','审批工具动作'),
                    details.get('description',name+' 将产生受控变更，请核对具体参数。'),
                    [{'id':'allow','label':'仅批准此动作'},{'id':'deny','label':'拒绝此动作'}],binding,existing)
                approval_wait_ms+=clock_ms()-wait_started
                if decision.get('choiceId')=='modify':
                    args=decision['modifiedArgs']
                    result={'needsApproval':True,'args':args,'approval':{'title':'重新审批修改后的动作','description':'参数已修改；确认以下动作后才会执行。'}}
                    continue
                if decision.get('choiceId')!='allow':
                    result={'ok':False,'denied':True,'error':{'code':'permission','message':'用户拒绝或授权过期，未执行此动作。'}}
                    break
                self._gate(channel,run_id)
                with self.lock:
                    task['_pendingTool'].update(args=args,phase='executing')
                    call.update(status='running',args=args)
                    self._persist()
                result=self._execute_retry(channel,run_id,task,name,args,True)
                approved=True
            with self.lock:
                current=self._current(channel,run_id)
                clean=self.config.scrub(result)
                call.update(status='denied' if result.get('denied') else 'success' if result.get('ok') else 'failed',result=clean,durationMs=clock_ms()-started)
                current['activeMs']+=max(0,clock_ms()-started-approval_wait_ms)
                task.pop('_pendingTool',None)
                self.repo.complete_effect(effect_key,clean)
                self._event(current,'tool_finished',task['agentId'],name+'：'+call['status'],{'callId':call['id'],'taskId':task['id'],'approved':approved})
                self._persist()
            self._gate(channel,run_id)
            return {**clean,'callId':call['id']}
        except Stopped:
            raise
        except Exception as exc:
            with self.lock:
                call.update(status='failed',result={'ok':False,'error':{'code':getattr(exc,'code','tool'),'message':self.config.scrub(str(exc))[:1200],
                    'retryable':bool(getattr(exc,'retryable',False))}},durationMs=clock_ms()-started)
                task.pop('_pendingTool',None)
                self.repo.complete_effect(effect_key,call['result'])
                self._event(run,'tool_failed',task['agentId'],name+'：'+str(exc),{'callId':call['id'],'taskId':task['id']})
                self._persist()
            if isinstance(exc,ToolError): return call['result']
            raise

    def _execute_retry(self, channel, run_id, task, name, args, approved):
        definition=next(d for d in self.tools.definitions(task['agentId']) if d['name']==name)
        for attempt in range(self.config.max_retries+1):
            self._gate(channel,run_id)
            try:
                return self.tools.execute(task['agentId'],run_id,name,args,approved=approved)
            except ToolError as exc:
                if not exc.retryable or not definition.get('idempotent') or attempt>=self.config.max_retries: raise
                with self.changed:
                    self._event(self._current(channel,run_id),'tool_retry',task['agentId'],'幂等工具临时失败，有限退避重试。',{'tool':name,'attempt':attempt+1})
                    self.changed.wait(min(4,.5*2**attempt))

    def _record_memory(self, run, task, channel, reflection):
        if task.get('_episodeRecordedRevision')==task['revision']: return
        task['_episodeRecordedRevision']=task['revision']
        actor=task.get('agentId')
        if actor not in AGENTS: return
        calls=[call for call in run['calls'] if call['taskId']==task['id'] and call['tool']!='llm.chat']
        success=task['status']=='done'
        reason=task.get('result') or (task.get('error') or {}).get('message','没有完成')
        self.memory.record_episode(actor,run,task,success,str(reason)[:2500],[c['id'] for c in calls],channel)
        # Reflection is a proposal, never proof of success. It remains candidate until confirmed.
        if isinstance(reflection,dict) and isinstance(reflection.get('lesson'),str) and reflection['lesson'].strip():
            try:
                memory=self.memory.propose(actor,reflection['lesson'][:3000],str(reflection.get('trigger',task['goal']))[:1000],task['id'],run['id'],
                    {'callIds':[c['id'] for c in calls],'resultStatus':task['status']},conditions=str(reflection.get('conditions',''))[:1500],
                    kind=reflection.get('kind','procedural') if reflection.get('kind') in ('procedural','semantic') else 'procedural',channel_id=channel,
                    supersedes=reflection.get('supersedes'))
                self._event(run,'memory_candidate',actor,'反思已保存为候选经验，需要真实结果或人工确认后才能长期检索。',{'memoryId':memory['id'],'taskId':task['id']})
            except (ValueError,KeyError,TypeError) as exc:
                self._event(run,'reflection_rejected',actor,'不采用无效的反思，不影响已完成产物。',{'taskId':task['id'],'reason':str(exc)[:500]})
        validated=any(c['tool']=='code.test' and c['status']=='success' and isinstance(c.get('result'),dict) and c['result'].get('exitCode')==0 for c in calls)
        if validated:
            for hit in run['memoryHits']:
                if hit['taskId']==task['id']:
                    owner=hit.get('ownerAgentId',actor)
                    if owner!=actor:
                        self._event(run,'memory_feedback_skipped',actor,'记录共享经验的实际采用；不代替原所有者修改其私有反馈计数。',
                                    {'memoryId':hit['memoryId'],'ownerAgentId':owner,'taskId':task['id'],
                                     'adopted':True,'reason':'shared_memory_owner_boundary','success':success})
                        continue
                    try:
                        feedback=self.memory.feedback(actor,hit['memoryId'],success,task['id'])
                    except Exception as exc:
                        # Strategy bookkeeping must not invalidate a delivered/tested result.
                        self._event(run,'memory_feedback_failed',actor,'记忆反馈未更新，保留已完成任务与验证结果。',
                                    {'memoryId':hit['memoryId'],'ownerAgentId':owner,'taskId':task['id'],
                                     'error':self.config.scrub(str(exc))[:500]})
                    else:
                        self._event(run,'memory_feedback',actor,'根据实际测试结果更新已采用经验的置信度。',
                                    {'memoryId':hit['memoryId'],'ownerAgentId':owner,'success':success,
                                     'feedbackApplied':feedback.get('feedbackApplied',True)})

    def _finish(self, channel, run_id):
        with self.lock:
            run=self._current(channel,run_id)
            tasks=[t for t in run['steps'] if not t.get('_planning')]
            done=[t for t in tasks if t['status']=='done']
            status='done' if len(done)==len(tasks) else 'partial' if done or any(t['status']=='partial' or t.get('outputs') for t in tasks) else 'failed'
            gaps=[]
            four_role=run.get('mode')=='task' and set(run.get('participants',[]))==set(AGENTS)
            if four_role:
                for task in tasks:
                    if task.get('agentId')=='executor' and task.get('status') in ('done','partial') and self._has_code_work(run,task) and not self._valid_review(run,task):
                        gaps.append(task['title']+'：最新代码 revision / 产物尚无匹配的 Reviewer 独立验收。')
                if gaps and status=='done': status='partial'
            summaries=[f"- {t['title']} [{t['status']}]：{t.get('result') or (t.get('error') or {}).get('message','未交付')}" for t in tasks]
            synthesis=next((task for task in reversed(tasks) if task.get('finalSummary') and task['status']=='done'),None)
            output=(synthesis['result']+'\n\n执行记录：\n' if synthesis else '任务执行汇总：\n')+'\n'.join(summaries)
            if gaps:
                output='验收未完成（部分完成）：\n'+'\n'.join('- '+gap for gap in gaps)+'\n\n'+output
            if run['artifacts']:
                output+='\n\n可下载产物：'+ '、'.join(a['name'] for a in run['artifacts'])
            output+='\n\n执行状态由真实调用记录产生；任务验收与记忆策略有效性需要独立验证。'
            run.update(status=status,finishedAt=clock_ms(),summary=output[:20000],acceptanceGaps=gaps)
            self._event(run,'run_finished',run['respondingAgentId'],'所有可调度子任务已结束。',{'status':status,'done':len(done),'total':len(tasks),'acceptanceGaps':gaps})
            self._message(self.state['sessions'][channel],'agent',run['summary'],run['respondingAgentId'])
            self.memory.maintenance()
            self._persist()

    def wait_for_idle(self, timeout=10):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            with self.lock: workers=[w for w in self.workers if w.is_alive()]
            if not workers: return True
            for worker in workers: worker.join(timeout=max(0,min(.1,deadline-time.monotonic())))
        return False

    def shutdown(self):
        with self.changed:
            self.closing=True
            for session in self.state['sessions'].values():
                run=session.get('run')
                if run and run['status'] in ACTIVE:
                    run.update(status='interrupted',userPaused=False)
                    self._event(run,'service_stopping',None,'服务停止并保存执行现场，未确认的动作需要恢复时核对。')
                    self.tools.cancel(run['id'])
            self._persist()
            self.changed.notify_all()
