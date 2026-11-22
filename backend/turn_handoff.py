"""Reserve final action turns for producing files; never manufacture artifacts."""
def publication_state(run,task):
    contract=task.get('outputContract') or {}
    if contract.get('kind')!='artifact' or task.get('reviewOf') or task.get('finalSummary'):
        return {'next':'finish_original_contract'}
    extensions=contract.get('extensions',[])
    files={}
    for c in run.get('calls',[]):
        if (c.get('taskId')!=task['id'] or c.get('agentId')!=task['agentId']
                or c.get('revision')!=task.get('revision',0) or c.get('status')!='success'):continue
        result=c.get('result') or {};args=c.get('args') or {}
        if not isinstance(result,dict) or result.get('ok') is not True:continue
        path=result.get('path',args.get('path'))
        if not isinstance(path,str):continue
        if c.get('tool')=='file.delete':files.pop(path,None)
        elif c.get('tool') in ('file.write','file.patch') and result.get('sha256'):files[path]=result['sha256']
    files={p:h for p,h in files.items() if not extensions or p.lower().endswith(tuple(extensions))}
    published={(a.get('path'),a.get('sha256')) for a in run.get('artifacts',[])
               if a.get('taskId')==task['id'] and a.get('id') in task.get('outputs',[])}
    pending=[p for p,h in files.items() if (p,h) not in published]
    enough=len(files)>=contract.get('minCount',1)
    state={'next':'write_required_files' if not enough else 'publish_written_files' if pending else 'final',
           'writtenPaths':list(files),'unpublishedPaths':pending,
           'meaning':'Historical write/publication receipts only; normal current-byte/contract validation still applies.'}
    if pending:
        state['publicationExample']=({'type':'publish_artifact','path':pending[0]} if len(pending)==1
                                     else {'type':'publish_artifacts','paths':pending[:12]})
    return state

def window(run,task):
    policy=run.get('budget',{}).get('taskTurnPolicy')
    if not policy:return None
    total=policy['initial']+policy['extra']
    result={'used':task.get('turns',0)-task.get('_revisionStartTurn',0),
            'stopAcquisitionAt':max(1,total-3),'finalTurn':total,
            'publicationState':publication_state(run,task),
            'instruction':'Reserve the last four turns for local file work, publication/packaging and final. '
                'At stopAcquisitionAt no web.search/web.fetch or new delegation executes, even for a necessary source. '
                'Follow publicationState: write required files first, then publish current files, then final. '
                'Publication is a TOP-LEVEL action: {"type":"publish_artifact","path":"actual.md"}, never type=tool or args wrapping. '
                'Include required assets in the actual publication. Use the first reserved turn for output, not more inspection; keep a turn for format correction. '
                'Do not leave required files only in final.text. Missing mandatory evidence stays partial; no content is invented.'}
    if result['publicationState']['next']=='finish_original_contract':
        result['instruction']='No artifact-file obligation is added. Follow the original text, review or delivery contract; do not create files merely to close a text/review task. Necessary local reads and review actions remain available under original permissions. At stopAcquisitionAt new web search/fetch and delegation stop; finalTurn remains final only. No new acceptance is implied.'
    return result

def feedback(run,task,action):
    w=window(run,task)
    if not w or w['used']<w['stopAcquisitionAt']:return None
    blocked=action.get('type')=='delegate' or action.get('type')=='tool' and action.get('name') in ('web.search','web.fetch')
    state=w['publicationState']
    if state['next'] in ('write_required_files','publish_written_files'):
        blocked=blocked or action.get('type')=='tool' and action.get('name') in ('file.read','file.list','code.test','code.run')
    if not blocked:return None
    return {'ok':False,'actionExecuted':False,'notExecuted':True,
            'error':{'code':'turn_handoff_required','retryable':False,'message':'Final turns are reserved for the required output and handoff; the proposed action was not executed.'},
            'turnHandoff':w}
