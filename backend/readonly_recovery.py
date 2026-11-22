"""Reject a known permanently failing read as an observation, not a task crash.

Independent standard-library implementation. Design reference: CrewAI fixed
commit bf6fd3a0972dd72fe1ff568c243e87e3b8408d35,
lib/crewai/src/crewai/utilities/agent_utils.py:1999-2071 (actual tool error/result
returned to the agent). This does not copy CrewAI's forced-finish policy, cache a
successful body, perform a request, grant permission or accept a deliverable.
"""
import copy
import json


def local_read_failure_feedback(run, task, action):
    """Bound alternating local failures, scoped to actual task/revision evidence.

    Successful tools, new grants and settled mutations invalidate the streak.
    No filesystem access, permission changes or synthetic tool calls occur here.
    """
    if (action.get('type') != 'tool' or action.get('name') not in ('file.read', 'file.list')
            or not isinstance(action.get('args'), dict)):
        return None
    path = action['args'].get('path', '')
    if not isinstance(path, str) or not path:
        return None
    events = run.get('events', [])
    last_grant = -1
    for index, event in enumerate(events):
        if (event.get('type') == 'artifact_granted'
                and event.get('payload', {}).get('toAgent') == task['agentId']):
            last_grant = index
    after_grant = {event.get('payload', {}).get('callId') for event in events[last_grant+1:]
                   if event.get('type') in ('tool_failed', 'tool_finished')}
    failures = []
    for call in run.get('calls', []):
        result = call.get('result')
        if not isinstance(result, dict):
            continue
        # A completed mutation can make a previously absent path readable.
        if (call.get('agentId') == task['agentId'] and call.get('status') == 'success'
                and result.get('ok') is True and call.get('tool') in ('file.write', 'file.patch', 'file.delete')):
            failures = []
        if (call.get('agentId') != task['agentId'] or call.get('taskId') != task['id']
                or type(call.get('revision')) is not int or call['revision'] != task['revision']
                or not str(call.get('tool', '')).startswith(('file.', 'code.'))):
            continue
        if last_grant >= 0 and call.get('id') not in after_grant:
            continue
        if call.get('status') == 'success' and result.get('ok') is True:
            failures = []
            continue
        error = result.get('error', {})
        if (call.get('tool') in ('file.read', 'file.list') and call.get('status') == 'failed'
                and result.get('ok') is False and not result.get('notExecuted')
                and not any(result.get(key) or call.get(key) for key in _UNKNOWN)
                and error.get('code') in ('permanent', 'permission', 'parameter')
                and error.get('retryable') is False):
            failures.append(call)
        else:
            failures = []
    if len(failures) < 4 or path not in {(c.get('args') or {}).get('path') for c in failures}:
        return None
    signature = [c['id'] for c in failures[-4:]]
    previous = sum(1 for event in events if event.get('type') == 'local_read_failure_recovery'
                   and event.get('payload', {}).get('taskId') == task['id']
                   and event['payload'].get('revision') == task['revision']
                   and event['payload'].get('observedCallIds') == signature)
    return {'ok': False, 'notExecuted': True, 'actionExecuted': False,
            'taskId': task['id'], 'revision': task['revision'], 'observedCallIds': signature,
            'failedPaths': sorted({c['args']['path'] for c in failures if c.get('args', {}).get('path')})[:8],
            'feedbackAttempt': previous+1, 'stopTask': previous >= 2,
            'instruction': 'Repeated local reads failed without new evidence. Read the authorizedArtifacts paths actually supplied in your current context; do not guess artifact IDs or retry inaccessible paths. If a required input is missing, request clarification or submit an evidenced review/rework decision where authorized; otherwise finish partial with the missing input identified. No read, new permission or acceptance was created. Two feedback opportunities are allowed before stopping unchanged failures.'}


_READS={'web.fetch','web.search'}
_UNKNOWN=('outcomeUnknown','deadlineExceeded','cancelled','timedOut')


def _canonical(value):
    try:
        return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    except (TypeError,ValueError,RecursionError):
        return None


def readonly_failure_feedback(run,task,action):
    """Return typed rejection after two real permanent failures of this read.

    Exact args, owner, task and integer revision must match. A matching success,
    unknown result or nonpermanent failure resets the history; interleaved reads
    of other URLs do not. No new tool call, result body or success is synthesized.
    Rejected proposals do not themselves count as another actual failed call.
    """
    if not all(isinstance(x,dict) for x in (run,task,action)):return None
    if action.get('type')!='tool' or action.get('name') not in _READS:return None
    name=action['name'];args=action.get('args');revision=task.get('revision')
    if (not isinstance(args,dict) or type(revision) is not int or revision<0
            or any(not isinstance(value,str) or not value for value in
                   (run.get('id'),task.get('id'),task.get('agentId')))):return None
    args_key=_canonical(args)
    if args_key is None or len(args_key)>12000:return None
    target=args.get('url' if name=='web.fetch' else 'query')
    if not isinstance(target,str) or not target.strip() or len(target)>4000:return None
    calls=run.get('calls')
    if not isinstance(calls,list) or any(not isinstance(call,dict) for call in calls):return None
    failures=[];seen=set();successful_reads={}
    for call in calls:
        if (call.get('taskId')!=task['id'] or call.get('agentId')!=task['agentId']
                or type(call.get('revision')) is not int or call['revision']!=revision):continue
        result=call.get('result');call_args=call.get('args')
        if (call.get('tool')=='web.fetch' and call.get('status')=='success' and isinstance(result,dict)
                and result.get('ok') is True and isinstance(result.get('content'),str) and result['content'].strip()
                and not any(result.get(key) or call.get(key) for key in _UNKNOWN) and isinstance(result.get('url'),str)
                and isinstance(call.get('id'),str)):
            successful_reads[result['url']]={'callId':call['id'],'url':result['url']}
        if call.get('tool')!=name or not isinstance(call_args,dict) or _canonical(call_args)!=args_key:continue
        call_id=call.get('id')
        if not isinstance(call_id,str) or not call_id or call_id in seen:return None
        seen.add(call_id)
        error=result.get('error') if isinstance(result,dict) else None
        permanent=(call.get('status')=='failed' and isinstance(result,dict) and result.get('ok') is False
                   and not any(result.get(key) for key in ('needsApproval','notExecuted','modelViewOnly'))
                   and result.get('actionExecuted') is not False
                   and not any(result.get(key) or call.get(key) for key in _UNKNOWN)
                   and isinstance(error,dict) and error.get('code')=='permanent'
                   and error.get('retryable') is False and isinstance(error.get('message'),str)
                   and bool(error['message'].strip()))
        if permanent:
            failures.append({'callId':call_id,'status':'failed','error':{
                'code':'permanent','message':error['message'][:600],'retryable':False},
                'errorMessageTruncated':len(error['message'])>600})
        else:
            failures=[]
    if len(failures)<2:return None
    reads=list(successful_reads.values())
    return {'ok':False,'actionExecuted':False,'notExecuted':True,
        'runId':run['id'],'taskId':task['id'],'agentId':task['agentId'],'revision':revision,
        'tool':name,'args':copy.deepcopy(args),'target':target,
        'error':{'code':'repeated_permanent_read_failure','retryable':False,
                 'message':'This exact read already returned at least two real permanent failures. '
                           'The identical proposed request was not sent; use the observed errors to change strategy.'},
        'observedFailures':{'count':len(failures),'callIds':[row['callId'] for row in failures[-8:]],
                            'callIdsTruncated':len(failures)>8,
                            'records':copy.deepcopy(failures[-3:]),'recordsTruncated':len(failures)>3},
        'historicalSuccessfulReadReferences':reads[-4:],'historicalReadReferenceCount':len(reads),
        'historicalReadReferencesTruncated':len(reads)>4,'bodyAvailability':'not_checked_by_this_helper',
        'instruction':'No new network request or tool call was executed and no source body was created. '
            'Use a different real link supported by already observed search/fetch links, or write/publish an honest '
            'deliverable from evidence whose actual readable content is retained. Historical successful-read references '
            'are audit metadata, not recovered bodies. Do not invent URLs, facts, citations or success. '
            'Existing stage/deadline/permission/output and independent-review requirements remain unchanged.'}
