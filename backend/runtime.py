"""Structured model protocol and DAG validation, independent of HTTP or model vendor."""
import hashlib
import json
import re


class ProtocolError(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def parse_object(text):
    if not isinstance(text, str):
        raise ProtocolError('模型响应必须为文本 JSON。')
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()
    # Accept fenced JSON, but require exactly one object rather than evaluating model code.
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    decoder = json.JSONDecoder()
    start = text.find('{')
    if start < 0:
        raise ProtocolError('未返回结构化 JSON 对象。')
    try:
        value, end = decoder.raw_decode(text[start:])
    except ValueError:
        raise ProtocolError('模型 JSON 无法解析，请返回一个完整对象。') from None
    if not isinstance(value, dict) or text[start+end:].strip():
        raise ProtocolError('模型应只返回一个 JSON 对象。')
    return value


def strings(value, limit=12, width=1000):
    if isinstance(value, str):
        return [value[:width]] if value.strip() else []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProtocolError('此字段必须为字符串列表。')
    return [item[:width] for item in value[:limit] if item.strip()]


def validate_dag(tasks):
    ids = {task['id'] for task in tasks}
    if len(ids) != len(tasks):
        raise ProtocolError('任务 ID 重复。')
    edges = {task['id']: task.get('dependsOn', []) for task in tasks}
    visiting, done = set(), set()
    def visit(node):
        if node in visiting:
            raise ProtocolError('任务依赖形成循环。')
        if node in done:
            return
        visiting.add(node)
        for dependency in edges[node]:
            if dependency not in ids:
                raise ProtocolError('任务引用了不存在的依赖。')
            visit(dependency)
        visiting.remove(node)
        done.add(node)
    for node in ids:
        visit(node)


def normalize_plan(value, participants, make_id, max_tasks):
    source = value.get('tasks')
    if not isinstance(source, list) or not source or len(source) > max_tasks:
        raise ProtocolError(f'计划必须包含 1 至 {max_tasks} 个任务。')
    old_ids, mapping, tasks = [], {}, []
    role_capability = {'coordinator': 'planning', 'research': 'research', 'executor': 'coding', 'reviewer': 'review'}
    for i, task in enumerate(source):
        if not isinstance(task, dict):
            raise ProtocolError('计划中的任务必须为对象。')
        old_id = task.get('id', str(i+1))
        if not isinstance(old_id, str) or old_id in mapping or len(old_id) > 80:
            raise ProtocolError('计划中的任务 ID 无效或重复。')
        old_ids.append(old_id)
        mapping[old_id] = make_id('task')
    for old_id, item in zip(old_ids, source):
        title = item.get('title') or item.get('goal')
        if not isinstance(title, str) or not title.strip():
            raise ProtocolError('每个子任务都必须有目标。')
        agent_id = item.get('agentId')
        if agent_id is not None and agent_id not in participants:
            raise ProtocolError('计划不能分配给未选择的 Agent。')
        dependencies = item.get('dependsOn', [])
        if not isinstance(dependencies, list) or any(not isinstance(x, str) or x not in mapping for x in dependencies):
            raise ProtocolError('计划的依赖 ID 无效。')
        tasks.append({
            'id': mapping[old_id], 'title': title[:300], 'goal': str(item.get('goal', title))[:5000],
            'acceptanceCriteria': strings(item.get('acceptanceCriteria', ['输出可核对的结果与实际限制。'])),
            'agentId': agent_id, 'preferredAgentId': agent_id, 'candidateAgentIds': list(participants),
            'requiredTools': strings(item.get('requiredTools', [])),
            'requiredCapabilities': list(dict.fromkeys(strings(item.get('requiredCapabilities', [])) + ([role_capability[agent_id]] if agent_id in role_capability else []))),
            'dependsOn': [mapping[x] for x in dependencies], 'parentId': None, 'childIds': [],
            'delegatedBy': 'coordinator' if 'coordinator' in participants else participants[0],
            'inputs': item.get('inputs', {}), 'outputs': [], 'status': 'pending', 'result': '',
            'error': None, 'attempts': 0, 'retries': 0, 'revision': 0, 'depth': 0,
            'rejectedBy': [], 'feedback': [], 'turns': 0, 'tokenUsage': 0,
        })
    validate_dag(tasks)
    if set(participants) == set(role_capability):
        covered = {task['agentId'] for task in tasks}
        if not set(role_capability).issubset(covered):
            raise ProtocolError('Four-agent plans must include explicit real tasks for coordinator, research, executor and reviewer. Correct the plan; do not invent completed work.')
        by_id = {task['id']: task for task in tasks}
        def ancestors(task):
            found, queue = set(), list(task['dependsOn'])
            while queue:
                current = queue.pop()
                if current not in found:
                    found.add(current)
                    queue.extend(by_id[current]['dependsOn'])
            return found
        specialists = {task['id'] for task in tasks if task['agentId'] in ('research', 'executor', 'reviewer')}
        summaries = [task for task in tasks if task['agentId'] == 'coordinator' and specialists.issubset(ancestors(task))]
        if not summaries:
            raise ProtocolError('Add an explicit final Coordinator synthesis task that depends directly or transitively on every Research, Executor and Reviewer task.')
        summary = summaries[-1]
        summary['finalSummary'] = True
        summary['requiredCapabilities'] = list(dict.fromkeys(summary['requiredCapabilities'] + ['synthesis']))
        summary['acceptanceCriteria'].append('Synthesize actual research sources, execution evidence, reviewer verdicts and limitations; do not replace missing verification with prose.')
    return tasks


def planning_prompt(agents, limit):
    labels={'coordinator':'planning and summary','research':'public web and reports',
            'executor':'Python code/files/tests','reviewer':'independent tests'}
    roles=', '.join(agent['id']+' ('+labels.get(agent['id'],'general tasks')+')' for agent in agents)
    contract = ''
    if {agent['id'] for agent in agents} == set(labels):
        contract = (' Include real tasks for all 4 roles: Research searches and cites actual URLs; Executor implements/tests code when needed; '
                    'Reviewer independently checks delivered artifacts and records review with its own test evidence for code. '
                    'End with a Coordinator synthesis task depending directly/transitively on ALL specialist tasks. '
                    'Do not force code/tests for a purely non-code task.')
    return ('Plan an executable task DAG. Reply only JSON. Use only these agents: '+roles+'. '
            f'Max {min(limit,6)} tasks. Format: '+
            '{"summary":"brief","tasks":[{"id":"a","title":"goal","agentId":"executor","dependsOn":[],"acceptanceCriteria":["test"]}]}. '
            'Dependencies use task ids; no cycles. Keep response under 250 words.' + contract)


def action_prompt(agent, definitions):
    specs=[]
    for item in definitions:
        schema=item.get('parameters',item.get('schema',{}))
        fields=schema.get('required',[])
        if item['name'] in ('code.run','code.test'): fields=['code OR path']
        specs.append(item['name']+'('+','.join(fields)+')')
    return ('You are '+agent['name']+'. Execute the task. Reply one JSON action per turn, no prose. '
        'Tool action: {"type":"tool","name":"file.write","args":{"path":"x.py","content":"source"}}. '
        'Other actions: publish_artifact(path); final(text,status=done|partial|failed,reflection?); '
        'ask_human(question,choices); delegate(task:{title,agentId,dependsOn}); await_task(taskId); '
        'grant_artifact(artifactId,toAgent); message(toAgent,text); reject(reason); '
        'review(taskId,verdict=pass|rework|reject,feedback,evidenceCallIds) only Reviewer. '
        'Each is JSON with type and named fields. Allowed tools: '+ ';'.join(specs)+'. '
        'Run Python only via code.run/code.test; imports are read-only. Publish files before final. '
        'Never claim unexecuted work. Approval and permissions are enforced by the server.')


def _clip_context_text(text, limit):
    """Keep both the beginning and latest detail with an explicit truncation marker."""
    text = str(text)
    if len(text) <= limit:
        return text
    marker = '\n[TRUNCATED]\n'
    if limit <= len(marker):
        return '[TRUNCATED]'[:limit]
    room = limit - len(marker)
    prefix = (room * 2) // 3
    return text[:prefix] + marker + (text[-(room-prefix):] if room-prefix else '')


def _compact_initial_context(content, limit):
    """Preserve parseable task/dependency/artifact references, dropping bulk contents."""
    try:
        source = json.loads(content)
    except (ValueError, TypeError):
        return _clip_context_text(content, limit)
    if not isinstance(source, dict):
        return _clip_context_text(content, limit)

    def bounded(value, width, count):
        if isinstance(value, str):
            return _clip_context_text(value, width)
        if isinstance(value, list):
            return [bounded(item, width, count) for item in value[:count]]
        if isinstance(value, dict):
            return {key: bounded(item, width, count) for key, item in list(value.items())[:count]}
        return value

    def references(items, count):
        if not isinstance(items, list):
            return []
        return [{key: _clip_context_text(item[key], 240) for key in ('id', 'name', 'path', 'ownerAgentId') if key in item}
                for item in items[:count] if isinstance(item, dict)]

    def validation_ref(value, count):
        if not isinstance(value, dict):
            return value
        # These are acceptance state, never replace them with prose or clipped strings.
        result = {key: value[key] for key in ('verdict', 'verified', 'targetRevision', 'reviewerTaskId') if key in value}
        hashes = value.get('artifactHashes')
        if isinstance(hashes, dict):
            result['artifactHashes'] = dict(list(hashes.items())[:count])
            if len(hashes) > count:
                result['artifactHashesTruncated'] = True
        if isinstance(value.get('evidenceCallIds'), list):
            result['evidenceCallIds'] = value['evidenceCallIds'][:count]
        return result

    def source_refs(count):
        items = source.get('sourceReferences', [])
        if not isinstance(items, list):
            return []
        return [{key: _clip_context_text(item[key], 512 if key=='url' else 120) for key in ('url', 'title', 'callId', 'agentId') if key in item}
                for item in items[:count] if isinstance(item, dict)]

    for width, count in ((320, 4), (160, 3), (80, 2), (40, 1)):
        core = {'_contextTruncated': True, '_omitted': 'Bulk file contents, older results and excess list entries; read artifacts by reference.'}
        if 'runGoal' in source:
            core['runGoal'] = _clip_context_text(source['runGoal'], width)
        task = source.get('task')
        if isinstance(task, dict):
            core['task'] = {key: bounded(task[key], max(120, width) if key in ('id', 'dependsOn', 'childIds') else width, count) for key in ('id', 'title', 'goal', 'acceptanceCriteria', 'dependsOn', 'childIds', 'feedback') if key in task}
        if 'participants' in source:
            core['participants'] = bounded(source['participants'], 96, 4)
        dependencies = source.get('dependencyResults')
        if isinstance(dependencies, list):
            selected = dependencies[:count] + [item for item in dependencies[count:] if isinstance(item,dict) and isinstance(item.get('validation'),dict)]
            core['dependencyResults'] = []
            for item in selected:
                if not isinstance(item, dict):
                    continue
                row = {key: bounded(item[key], max(120, width) if key in ('id', 'outputs') else width, count)
                       for key in ('id', 'title', 'agentId', 'outputs', 'status', 'revision', 'result', 'acceptanceCriteria') if key in item}
                if 'validation' in item:
                    row['validation'] = validation_ref(item['validation'],count)
                core['dependencyResults'].append(row)
        if 'sourceReferences' in source:
            core['sourceReferences'] = source_refs(count)
        for field in ('ownArtifacts', 'authorizedArtifacts'):
            if field in source:
                core[field] = references(source[field], count)
        if isinstance(source.get('memories'), list):
            core['memories'] = [{key: bounded(item[key], width, count) for key in ('id', 'lesson', 'conditions') if key in item}
                                for item in source['memories'][:count] if isinstance(item, dict)]
        text = json.dumps(core, ensure_ascii=False, separators=(',', ':'))
        if len(text) <= limit:
            return text
    # Very small budgets still get valid JSON rather than a sliced JSON document.
    core = {'_contextTruncated': True}
    task = source.get('task')
    if isinstance(task, dict):
        core['task'] = {key: _clip_context_text(task[key], 40) for key in ('id', 'goal') if key in task}
    if 'runGoal' in source:
        core['runGoal'] = _clip_context_text(source['runGoal'], 40)
    dependencies = source.get('dependencyResults', [])
    if isinstance(dependencies,list):
        critical = [{ 'id': item.get('id'), 'validation': {key:item['validation'][key] for key in ('verdict','verified','targetRevision') if key in item['validation']}}
                    for item in dependencies if isinstance(item,dict) and isinstance(item.get('validation'),dict)]
        if critical:
            core['dependencyResults'] = critical
    if 'sourceReferences' in source:
        core['sourceReferences'] = source_refs(1)
    text = json.dumps(core, ensure_ascii=False, separators=(',', ':'))
    if len(text) <= limit:
        return text
    # Unusually tiny caller limits may fit only the explicit truncation marker.
    marker = '{"_contextTruncated":true}'
    return marker if len(marker) <= limit else ('{}' if limit >= 2 else '0'[:limit])


def compress_messages(messages, limit):
    """Pure, index-deduplicated compression with a strict total content-character bound."""
    limit = max(0, int(limit))
    copied = [{**item, 'content': str(item.get('content', ''))} for item in messages]
    if not copied or not limit:
        return []
    if sum(len(item['content']) for item in copied) <= limit:
        return copied
    # Preserve the protocol and initial objective, then the four latest unique turns.
    tail = copied[max(2, len(copied)-4):]
    system_limit = min(len(copied[0]['content']), max(limit//3, min(1800, limit//2)))
    first = {**copied[0], 'content': _clip_context_text(copied[0]['content'], system_limit)}
    result = [first]
    remaining = limit-len(first['content'])
    if len(copied) > 1 and remaining:
        initial_limit = remaining if not tail else remaining//2
        initial = {**copied[1], 'content': _compact_initial_context(copied[1]['content'], initial_limit)}
        result.append(initial)
        remaining -= len(initial['content'])
    for index, item in enumerate(tail):
        if not remaining:
            break
        allowance = remaining//(len(tail)-index)
        text = _clip_context_text(item['content'], allowance)
        result.append({**item, 'content': text})
        remaining -= len(text)
    return result
