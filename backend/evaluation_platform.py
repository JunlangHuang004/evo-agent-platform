"""Adapt saved platform runs to the independent acceptance contract."""

from datetime import datetime

from .evaluation import EXECUTION_STATES


_TASK_TERMINAL = {'done', 'partial', 'failed', 'cancelled', 'skipped', 'rejected'}


def platform_record(binding, repository, artifact_store):
    run_id = binding['runId']
    platform = repository.get_run(run_id)
    if platform is None:
        raise ValueError(f'No saved platform run for {run_id}.')
    state = platform['status']
    error = platform.get('error') or {}
    if state in EXECUTION_STATES:
        if state == 'failed' and error.get('code') == 'runtime':
            state = 'crashed'
        elif state in ('failed', 'partial') and error.get('code') == 'timeout':
            state = 'timeout'
    elif binding.get('terminatedAtMs') is not None and state == 'interrupted':
        state = 'crashed'
    elif binding.get('deadlineAtMs') is not None:
        state = 'timeout'
    else:
        raise ValueError(f'Run {run_id} needs a recorded terminal state, termination, or deadline.')

    events = repository.events(run_id)
    modified_at = {event['payload']['approvalId']: int(datetime.fromisoformat(
        event['time'].replace('Z', '+00:00')).timestamp() * 1000)
        for event in events if event['type'] == 'approval_modified'}
    waits = []
    for approval in platform.get('approvals', []):
        ended = approval.get('resolvedAt')
        if approval['status'] == 'expired':
            ended = approval.get('expiresAt')
        if approval['status'] == 'modified' and approval['id'] in modified_at:
            ended = max(approval['createdAt'], modified_at[approval['id']])
        waits.append({'startedAtMs': approval['createdAt'], 'endedAtMs': ended})

    tasks = []
    for task in platform.get('steps', []):
        if task.get('_planning'):
            continue
        revision = task['revision']
        for prior in task.get('validationHistory', []):
            tasks.append({'taskId': task['id'], 'revision': prior['targetRevision'],
                          'status': 'rework', 'reviewEvidence': prior})
        # The shared baseline does not bind all memory hits to revisions.
        memory_ids = sorted({item['memoryId'] for item in platform.get('memoryHits', [])
                             if item['taskId'] == task['id'] and item.get('applied')
                             and item.get('ownerAgentId') == task.get('agentId')
                             and (item.get('revision') == revision
                                  or revision == 0 and 'revision' not in item)})
        tasks.append({
            'taskId': task['id'], 'revision': revision, 'ownerAgentId': task.get('agentId'),
            'status': task['status'], 'closed': task['status'] in _TASK_TERMINAL,
            'artifactIds': task.get('outputs', []), 'result': task.get('result', ''),
            'memoryIds': memory_ids, 'errorCode': (task.get('error') or {}).get('code'),
            'errorReason': (task.get('error') or {}).get('message'),
        })

    evidence = dict(binding.get('evidence', {}))
    calls = {call['id']: call for call in platform.get('calls', [])}
    if 'research' in evidence:
        ref = evidence['research']
        call = calls.get(ref.get('callId'), {})
        result = call_result(call)
        sources = result.get('sources', [])
        source_text = ' '.join(source.get('title', '') for source in sources if source['url'] == ref.get('sourceUrl'))
        if result.get('url') == ref.get('sourceUrl'):
            source_text += ' ' + result.get('content', '')
        valid = _matching_call(call, ref, 'research') and result.get('ok') is True
        evidence['research'] = {**ref, 'tool': call.get('tool'),
                                'status': call.get('status') if valid else None,
                                'returnedUrls': [source['url'] for source in sources], 'sourceText': source_text}
    if 'review' in evidence:
        evidence['review'] = platform_review(evidence['review'], calls, events, artifact_store, run_id)

    return {
        'runId': run_id, 'caseId': binding['caseId'], 'source': 'platform',
        'status': state, 'startedAtMs': platform['startedAt'], 'finishedAtMs': platform.get('finishedAt'),
        'terminatedAtMs': binding.get('terminatedAtMs'), 'deadlineAtMs': binding.get('deadlineAtMs'),
        'errorCode': error.get('code') or (state if state in ('timeout', 'crashed', 'cancelled') else None),
        'errorReason': error.get('message') or binding.get('errorReason'),
        'humanWaits': waits, 'tasks': tasks, 'evidence': evidence,
        'memoryEnabled': platform.get('memoryEnabled'),
        'memoryCondition': binding.get('memoryCondition'), 'memoryBoundary': binding.get('memoryBoundary'),
        'memoryChanges': [{key: event[key] for key in ('id', 'type', 'time', 'payload')}
                          for event in events if event['type'] in ('memory_applied', 'memory_feedback')],
    }


def _matching_call(call, ref, owner):
    return (call.get('agentId') == owner and call.get('taskId') == ref.get('taskId')
            and (call.get('revision') == ref.get('revision')
                 or 'revision' not in call and ref.get('revision') == 0))


def call_result(call):
    value = call.get('result')
    return value if isinstance(value, dict) else {}


def platform_review(ref, calls, events, artifacts, run_id):
    call = calls.get(ref.get('callId'), {})
    result = call_result(call)
    target = ref.get('target', {})
    snapshot = call.get('reviewTargets', {}).get(target.get('taskId'), {})
    output_id = ref.get('result', {}).get('artifactId')
    input_id = ref.get('inputArtifactId')

    finished = next((index for index, event in enumerate(events)
                     if event['type'] == 'tool_finished' and event['payload'].get('callId') == call.get('id')), None)

    def granted_path(artifact_id, name, digest):
        for event in events[:finished] if finished is not None else []:
            payload = event['payload']
            grant = payload.get('grant', {})
            if (event['type'] == 'artifact_granted' and payload.get('artifactId') == artifact_id
                    and payload.get('toAgent') == 'reviewer' and grant.get('readOnly') is True
                    and grant.get('sha256') == digest and grant.get('path') == f'{artifact_id}/{name}'):
                return f'imports/{artifact_id}/{name}'
        return None

    args = call.get('args', {})
    input_artifact = artifacts.get(input_id) if isinstance(input_id, str) else None
    script_path = granted_path(target.get('artifactId'), 'summarize_orders.py', target.get('sha256'))
    input_path = granted_path(input_id, 'review_orders.csv', input_artifact['sha256']) if input_artifact else None
    registered = finished is not None and any(
        event['type'] == 'artifact_registered' and event['payload'].get('artifactId') == output_id
        for event in events[finished + 1:])

    valid = (_matching_call(call, ref, 'reviewer') and result.get('ok') is True and registered
             and snapshot.get('targetRevision') == target.get('revision')
             and snapshot.get('artifactHashes', {}).get(target.get('artifactId')) == target.get('sha256')
             and bool(script_path and input_path and input_artifact)
             and input_artifact['runId'] == run_id and input_artifact['name'] == 'review_orders.csv'
             and input_artifact.get('ownerAgentId') == 'reviewer'
             and not any(event['type'] == 'artifact_granted' and event['payload'].get('artifactId') == input_id
                         and event['payload'].get('toAgent') != 'reviewer' for event in events)
             and args.get('path') == script_path and args.get('args') == ['/' + input_path, 'review_summary.json'])
    return {**ref, 'tool': call.get('tool'), 'status': call.get('status') if valid else None,
            'sandboxId': result.get('sandboxId'), 'exitCode': result.get('exitCode'),
            'inputSha256': input_artifact['sha256'] if valid else None, 'readOnly': bool(valid)}
