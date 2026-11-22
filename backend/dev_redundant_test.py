"""Skip Dev's redundant browser self-tests on unchanged published bytes.

After the task's own agent has already published an artifact AND an earlier
identical browser `code.test` proposal (same `resources` SHA map, same
normalised `checks`, same `timeout`) passed in the current task revision,
re-submitting the same proposal returns a non-executed receipt that cites the
cached passing call. No Docker is launched and no wall clock is consumed.

Scope is narrow and policy-safe:

* Only affects agents whose declared tool list is allowed to `publish_artifact`
  via the existing engine protocol (Dev/Executor-style roles). Independent
  reviewers (QA) have their own `agentId` namespace, so their first run against
  the same (resources, checks) pair never hits this cache.
* Requires an actual `publish_artifacts`/`publish_artifact` event for this
  task+revision+agent. Pre-publish iteration is untouched.
* Never upgrades a failure to success, never hides unknown outcomes, never
  changes acceptance records. A re-submission simply receives the earlier
  real receipt annotated `shortCircuited=True`.
* Any `file.write` / `file.patch` / `file.delete` / `code.run` or non-browser
  `code.test` after the cached pass invalidates the cache for that signature.
"""
import copy
import json


def _encode(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None


def _signature(args):
    """Normalised fingerprint for a browser test proposal."""
    if not isinstance(args, dict) or args.get('language') != 'browser':
        return None
    if 'code' in args:
        return None
    checks = args.get('checks')
    if not isinstance(checks, list) or not checks:
        return None
    effective = copy.deepcopy(args)
    # ToolGateway records the same default timeout; keep signatures aligned.
    effective.setdefault('timeout', 20)
    encoded = _encode(effective)
    if encoded is None or len(encoded) > 64000:
        return None
    return encoded


def _has_publish_event(run, task):
    """Only engage after this agent actually published for this revision."""
    events = run.get('events')
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get('type') != 'artifact_registered':
            continue
        if event.get('agentId') != task.get('agentId'):
            continue
        payload = event.get('payload') or {}
        if payload.get('taskId') != task.get('id'):
            continue
        return True
    return False


def _is_mutation(tool, call_args):
    if tool in ('file.write', 'file.patch', 'file.delete', 'code.run'):
        return True
    if tool == 'code.test':
        if not isinstance(call_args, dict) or call_args.get('language') != 'browser':
            return True
    return False


def _last_passed_call(run, task, signature, revision):
    """Return the most recent passing call matching this signature, or None."""
    calls = run.get('calls')
    if not isinstance(calls, list):
        return None
    passing = None
    for call in calls:
        if not isinstance(call, dict):
            continue
        if call.get('agentId') != task.get('agentId'):
            continue
        tool = call.get('tool')
        call_args = call.get('args')
        if _is_mutation(tool, call_args):
            passing = None
            continue
        if tool != 'code.test':
            continue
        if (call.get('taskId') != task.get('id')
                or type(call.get('revision')) is not int
                or call.get('revision') != revision):
            continue
        if call.get('status') != 'success':
            # Explicit failure / unknown invalidates the streak for this signature.
            if _encode(call_args) == signature:
                passing = None
            continue
        if _encode(call_args) != signature:
            continue
        result = call.get('result')
        if not isinstance(result, dict) or result.get('ok') is not True:
            continue
        if result.get('outcomeUnknown') or result.get('deadlineExceeded'):
            continue
        browser = result.get('browser')
        if (not isinstance(browser, dict)
                or browser.get('engine') != 'chromium'
                or browser.get('executed') is not True
                or browser.get('ok') is not True):
            continue
        passing = call
    return passing


def dev_redundant_test_feedback(run, task, action, agents):
    """Reject a redundant browser self-test and cite the prior passing receipt.

    Returns None when the proposal must flow through normal execution.
    """
    if not all(isinstance(value, dict) for value in (run, task, action, agents)):
        return None
    if action.get('type') != 'tool' or action.get('name') != 'code.test':
        return None
    revision = task.get('revision')
    if (type(revision) is not int or revision < 0
            or any(not isinstance(v, str) or not v for v in
                   (run.get('id'), task.get('id'), task.get('agentId')))):
        return None
    actor = agents.get(task.get('agentId'))
    if not isinstance(actor, dict):
        return None
    tool_list = actor.get('tools')
    if not isinstance(tool_list, list) or 'code.test' not in tool_list:
        return None
    # QA / independent reviewers deliberately do not publish; keep them exempt.
    # The Executor role is the one that both publishes and loops self-tests.
    if 'git.commit' not in tool_list and 'code.run' not in tool_list:
        return None
    signature = _signature(action.get('args'))
    if signature is None:
        return None
    if not _has_publish_event(run, task):
        return None
    cached = _last_passed_call(run, task, signature, revision)
    if cached is None:
        return None
    browser = (cached.get('result') or {}).get('browser') or {}
    tested = browser.get('testedFiles')
    manifest = browser.get('sourceManifest')
    feedback = {
        'ok': False,
        'notExecuted': True,
        'actionExecuted': False,
        'runId': run['id'],
        'taskId': task['id'],
        'agentId': task['agentId'],
        'revision': revision,
        'tool': 'code.test',
        'error': {
            'code': 'redundant_browser_self_test',
            'retryable': False,
            'message': ('This identical browser proposal already passed on the '
                        'currently published bytes; it was not re-executed. Hand '
                        'off to independent QA, add new assertions, change the '
                        'tested resources, or publish new bytes before retesting.'),
        },
        'cachedPassingCallId': cached.get('id'),
        'cachedDurationMs': cached.get('durationMs'),
        'sourceManifest': manifest if isinstance(manifest, list) else None,
        'testedFiles': tested if isinstance(tested, list) else None,
        'instruction': (
            'The cached receipt stays authoritative; no Docker ran this turn. '
            'Any change to resources, checks, or published bytes creates a new '
            'signature and a fresh test will execute normally. This observation '
            'does not pass, publish, package, accept, extend time, or grant any '
            'new permission.'),
    }
    encoded = _encode(feedback)
    if encoded is None or len(encoded) > 24000:
        return None
    return feedback
