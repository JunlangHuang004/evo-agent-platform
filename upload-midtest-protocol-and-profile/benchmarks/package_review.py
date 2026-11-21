"""Describe exact current review coverage, never a product-quality verdict."""


def package_review_coverage(run):
    tasks={task['id']:task for task in run.get('steps',[]) if isinstance(task.get('id'),str)}
    artifacts={item['id']:item for item in run.get('artifacts',[]) if isinstance(item.get('id'),str)}
    calls={call['id']:call for call in run.get('calls',[]) if isinstance(call.get('id'),str)}
    covered=set()
    for target in tasks.values():
        validation=target.get('validation') or {}
        reviewer=tasks.get(validation.get('reviewerTaskId'),{})
        current={aid:artifacts[aid].get('sha256') for aid in target.get('outputs',[]) if aid in artifacts}
        if (validation.get('verdict')!='pass' or not validation.get('assessed')
                or validation.get('targetRevision')!=target.get('revision')
                or validation.get('reviewerRevision')!=reviewer.get('revision')
                or not reviewer or reviewer.get('agentId')==target.get('agentId')
                or target.get('status') not in ('done','partial')
                or validation.get('artifactHashes')!=current
                or len(current)!=len(target.get('outputs',[]))):
            continue
        if not any(event.get('type')=='review_pass' and event.get('agentId')==reviewer.get('agentId')
                   and event.get('payload')=={'taskId':target['id'],'validation':validation}
                   for event in run.get('events',[])):
            continue
        evidence=[calls[cid] for cid in validation.get('evidenceCallIds',[]) if cid in calls
                  and calls[cid].get('taskId')==reviewer['id']
                  and calls[cid].get('agentId')==reviewer.get('agentId')
                  and calls[cid].get('revision')==reviewer.get('revision')
                  and calls[cid].get('status')=='success']
        snapshot={'targetRevision':target.get('revision'),'artifactHashes':current}
        for aid in current:
            artifact=artifacts[aid]
            if artifact.get('taskId')!=target['id'] or artifact.get('runId')!=run.get('id'):
                continue
            if validation.get('kind')=='editorial_assessment':
                observed=any(call.get('tool')=='file.read'
                             and (call.get('result') or {}).get('sha256')==artifact.get('sha256')
                             and (call.get('result') or {}).get('truncated') is False
                             and (call.get('result') or {}).get('mode')=='full' for call in evidence)
            else:
                observed=validation.get('verified') is True and any(
                    call.get('tool')=='code.test' and call.get('reviewTargets',{}).get(target['id'])==snapshot
                    and (call.get('result') or {}).get('ok') is True
                    and (call.get('result') or {}).get('exitCode')==0 for call in evidence)
            if observed:covered.add((artifact.get('path'),artifact.get('sha256')))
    rows=[]
    for delivery in run.get('deliveries',[]):
        if delivery.get('status')!='packaged':continue
        producer=tasks.get(delivery.get('taskId'),{})
        if producer.get('revision')!=delivery.get('taskRevision'):continue
        missing=[{'path':item.get('path'),'sha256':item.get('sha256')}
                 for item in delivery.get('files',[]) if (item.get('path'),item.get('sha256')) not in covered]
        rows.append({'deliveryId':delivery['id'],'fileCount':len(delivery.get('files',[])),
                     'filesWithoutCurrentRecordedReview':missing})
    return {'packages':rows,'qualityVerdict':'unassessed',
            'note':'Exact path/byte coverage of recorded current reviews only. New or relocated files require package review. Coverage is not semantic acceptance or an official grade.'}
