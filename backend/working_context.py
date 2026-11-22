"""Shrink the next model request without deleting the ledger.

The shared board is status, hashes, gaps, clarifications and source indexes.
Each agent still receives only their own experience beside that board.
Older file, browser and fetched-page bodies become indexes once a newer
receipt for the same target exists. The latest body stays. Omitted text is
not a read and not a passing test.
"""
import copy
import json

_RECEIPT = '动作的真实结果：'
_NOTE = '正文已省略。按路径和 SHA256 用 file.read 再读；省略不是已读，也不是测试通过。'


def build_team_board(run, clarifications):
    """One ledger board for every participant. No private transcript or lesson."""
    tasks = []
    for task in run.get('steps') or []:
        if not isinstance(task, dict) or task.get('_planning') or task.get('kind') == 'planning':
            continue
        error = task.get('error') if isinstance(task.get('error'), dict) else {}
        tasks.append({
            'id': task.get('id'), 'title': task.get('title'), 'agentId': task.get('agentId'),
            'status': task.get('status'), 'revision': task.get('revision', 0),
            'errorCode': error.get('code') or None,
        })
    files = []
    for artifact in run.get('artifacts') or []:
        if not isinstance(artifact, dict):
            continue
        files.append({
            'name': artifact.get('name'), 'path': artifact.get('path'),
            'ownerAgentId': artifact.get('ownerAgentId') or artifact.get('owner'),
            'sha256': artifact.get('sha256'), 'readOnly': True,
        })
    sources = []
    for call in run.get('calls') or []:
        if not isinstance(call, dict) or call.get('status') != 'success' or call.get('tool') not in ('web.search', 'web.fetch'):
            continue
        result = call.get('result') if isinstance(call.get('result'), dict) else {}
        if call.get('tool') == 'web.search':
            for item in result.get('results') or []:
                if isinstance(item, dict) and isinstance(item.get('url'), str):
                    sources.append({'kind': 'search', 'url': item['url'], 'title': item.get('title') or '', 'callId': call.get('id')})
            continue
        snapshot = result.get('sourceSnapshot') if isinstance(result.get('sourceSnapshot'), dict) else {}
        sources.append({
            'kind': 'fetch', 'url': result.get('url'), 'title': result.get('title') or '',
            'callId': call.get('id'), 'sha256': snapshot.get('sha256'), 'path': snapshot.get('path'),
            'reread': '同一次运行的其他成员可用 file.read 读此公开网页快照，并核对 SHA256。引用和调用编号不是正文。',
        })
    return {
        'runId': run.get('id'), 'goal': run.get('title'), 'tasks': tasks, 'files': files,
        'gaps': [item for item in (run.get('acceptanceGaps') or []) if isinstance(item, str)][:12],
        'clarificationReceiptIds': [item.get('receiptId') for item in clarifications or []
                                    if isinstance(item, dict) and item.get('receiptId')],
        'clarificationNote': '人类答复在同一请求的 humanClarifications，只保留一份原文。',
        'sources': sources[:12],
        'note': '全队共用的状态索引，不含私有对话和个人经验。文件正文按路径和 SHA256 读取。',
    }


def call_index(call):
    """A receipt pointer. The body stays in the call ledger, not in this row."""
    result = call.get('result') if isinstance(call.get('result'), dict) else {}
    row = {'id': call.get('id'), 'tool': call.get('tool'), 'status': call.get('status')}
    for key in ('path', 'sha256', 'url', 'ok'):
        if key in result:
            row[key] = result[key]
    snapshot = result.get('sourceSnapshot') if isinstance(result.get('sourceSnapshot'), dict) else None
    if snapshot and isinstance(snapshot.get('path'), str):
        row['sourceSnapshot'] = {'path': snapshot['path'], 'sha256': snapshot.get('sha256'), 'callId': call.get('id')}
    error = result.get('error') if isinstance(result.get('error'), dict) else None
    if error and error.get('code'):
        row['errorCode'] = error['code']
    return row


def _receipt(message):
    content = message.get('content') if isinstance(message, dict) else None
    if message.get('role') != 'user' or not isinstance(content, str) or not content.startswith(_RECEIPT):
        return None
    try:
        body = json.loads(content[len(_RECEIPT):])
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _has_file_body(body):
    if not isinstance(body.get('path'), str) or body.get('contentOmitted') is True:
        return False
    if isinstance(body.get('content'), str) and body['content']:
        return True
    matches = body.get('matches')
    return isinstance(matches, list) and any(isinstance(item, dict) and isinstance(item.get('content'), str) and item['content'] for item in matches)


def _browser_key(body):
    browser = body.get('browser')
    if not isinstance(browser, dict):
        return None
    files = browser.get('testedFiles')
    parts = []
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict) and isinstance(item.get('path'), str) and isinstance(item.get('sha256'), str):
                parts.append(item['path'] + '\0' + item['sha256'])
    if not parts:
        entry = browser.get('entry')
        if isinstance(entry, dict) and isinstance(entry.get('path'), str) and isinstance(entry.get('sha256'), str):
            parts.append(entry['path'] + '\0' + entry['sha256'])
    return tuple(sorted(parts)) if parts else None


def _index_file(body):
    slim = {key: value for key, value in body.items() if key not in ('content', 'matches')}
    matches = body.get('matches')
    if isinstance(matches, list):
        slim['matches'] = [{key: value for key, value in item.items() if key != 'content'} | {'contentOmitted': True}
                           if isinstance(item, dict) else item for item in matches]
    slim['contentOmitted'] = True
    slim['readNote'] = _NOTE
    return slim


def _index_web(body):
    snapshot = body.get('sourceSnapshot') if isinstance(body.get('sourceSnapshot'), dict) else {}
    kept = {key: body[key] for key in ('ok', 'url', 'title', 'callId', 'truncated', 'sources', 'sourceSnapshot', 'sourceSnapshotUnavailable') if key in body}
    kept['contentOmitted'] = True
    kept['readNote'] = '抓取正文已省略。用 file.read 读取 sourceSnapshot.path，并核对 sha256 与 callId。引用不是正文。'
    if not snapshot.get('path'):
        kept['readNote'] = '抓取正文已省略。用 callId 在调用记录中找到快照；没有快照路径时不能把网址当成正文。'
    return kept


def _index_browser(body):
    slim = {key: value for key, value in body.items() if key not in ('stdout', 'stderr')}
    browser = body.get('browser')
    if isinstance(browser, dict):
        slim['browser'] = {key: value for key, value in browser.items() if key not in ('console', 'stdout', 'pageErrors', 'steps')}
    slim['contentOmitted'] = True
    slim['readNote'] = '较早的浏览器输出已省略。最新一次回执仍保留。省略不是测试通过。'
    return slim


def compact_working_receipts(messages):
    """Replace older bodies with indexes. Return the same list when nothing changes."""
    if not isinstance(messages, list):
        return messages
    parsed = [_receipt(message) for message in messages]
    last_file, last_web, last_browser = {}, {}, {}
    for index, body in enumerate(parsed):
        if not isinstance(body, dict):
            continue
        if _has_file_body(body):
            last_file[body['path']] = index
        if isinstance(body.get('url'), str) and isinstance(body.get('content'), str) and body['content'] and body.get('contentOmitted') is not True:
            last_web[body['url']] = index
        key = _browser_key(body)
        if key and body.get('contentOmitted') is not True:
            last_browser[key] = index
    changed = False
    updated = []
    for index, message in enumerate(messages):
        body = parsed[index]
        replacement = None
        if isinstance(body, dict) and _has_file_body(body) and last_file.get(body['path']) != index:
            replacement = _index_file(body)
        elif isinstance(body, dict) and isinstance(body.get('url'), str) and isinstance(body.get('content'), str) and last_web.get(body['url']) != index and body.get('contentOmitted') is not True:
            replacement = _index_web(body)
        elif isinstance(body, dict):
            key = _browser_key(body)
            if key and last_browser.get(key) != index and body.get('contentOmitted') is not True:
                replacement = _index_browser(body)
        if replacement is None:
            updated.append(message)
            continue
        changed = True
        updated.append({**message, 'content': _RECEIPT + json.dumps(replacement, ensure_ascii=False)})
    return updated if changed else messages
