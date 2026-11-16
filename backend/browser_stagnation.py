"""Bound repeated *executed failures*, rather than whole browser proposals.

Read-only assertions before/after a failing check are not evidence that the
check was corrected. Keep state-changing preparation, declared input setup,
the failing check, and host-bound source versions in the identity instead.
This never edits a test, attributes a product defect, or creates acceptance.
"""
import copy
import hashlib

from .browser_failure_recovery import _key, _report
from .browser_observation import failure_observation_view
from .browser_spec import validate_browser_args
from .sandbox import ToolError


MAX_BLOCKED_PROPOSALS = 3


def _step(value):
    value = copy.deepcopy(value)
    value.setdefault('pageId', 'main')
    if value.get('action') == 'assert_text':
        value.setdefault('exact', False)
    return value


def _setup(args, index):
    """Only assertions are observational; all other prior steps remain exact."""
    setup = copy.deepcopy(args)
    checks = setup.pop('checks')
    setup.setdefault('timeout', 20)
    setup.setdefault('resources', {})
    setup.setdefault('storage', {})
    return {'inputSetup': setup,
            'preparation': [_step(s) for s in checks[:index] if not s['action'].startswith('assert_')],
            'assertion': _step(checks[index])}


def _valid_args(args):
    if (not isinstance(args, dict) or args.get('language') != 'browser' or 'code' in args):
        return False
    try:
        validate_browser_args(args)
    except (ToolError, TypeError, ValueError, KeyError):
        return False
    encoded = _key(args)
    return encoded is not None and len(encoded) <= 131072


def _failed_check(call):
    parsed = _report(call)
    args = call.get('args')
    if parsed is None or not _valid_args(args):
        return None
    manifest, report, failures = parsed
    steps = report.get('steps')
    if (not isinstance(steps, list) or not 1 <= len(steps) <= len(args['checks'])
            or any(not isinstance(s, dict) for s in steps) or len(failures) != 1):
        return None
    index = len(steps) - 1
    failed = failures[0]
    for i, observed in enumerate(steps):
        requested = _step(args['checks'][i])
        if (observed.get('action') != requested['action']
                or observed.get('pageId', 'main') != requested['pageId']
                or any(observed.get(k) != requested.get(k) for k in ('selector', 'key'))
                or observed.get('ok') is not (i != index)):
            return None
    check = _step(args['checks'][index])
    if not check['action'].startswith('assert_') or steps[-1].get('passed') is not False:
        return None
    if any(failed.get(k) != steps[-1].get(k) for k in ('action', 'pageId', 'selector', 'key', 'name', 'expected', 'actual', 'passed')):
        return None
    expected_field = {'assert_text': 'text', 'assert_count': 'count', 'assert_value': 'value',
                      'assert_attribute': 'value', 'assert_storage': 'value'}.get(check['action'])
    if expected_field and failed.get('expected') != check[expected_field]:
        return None
    if check['action'] == 'assert_visible' and failed.get('expected') is not True:
        return None
    if check['action'] == 'assert_storage_missing' and failed.get('expected') is not None:
        return None
    if check['action'] == 'assert_attribute' and failed.get('name') != check['name']:
        return None
    observation = report.get('failureObservation')
    if isinstance(observation, dict) and observation.get('stepIndex', index) != index:
        return None
    return manifest, report, failed, index, _setup(args, index)


def browser_stagnation_feedback(run, task, action):
    """Reject a still-unchanged failing check despite surrounding check changes.

    Two actual matching failures are required. Every possible owner workspace
    mutation or untrustworthy browser result invalidates the history; every
    corrected check/setup remains eligible for normal tool execution. A real
    successful browser execution clears the failure history. Rejections are
    not fake tool calls and never inflate the observed failure count.
    """
    if not all(isinstance(v, dict) for v in (run, task, action)):
        return None
    args = action.get('args')
    if (action.get('type') != 'tool' or action.get('name') != 'code.test' or not _valid_args(args)
            or any(not isinstance(v, str) or not v for v in (run.get('id'), task.get('id'), task.get('agentId')))
            or type(task.get('revision')) is not int or task['revision'] < 0):
        return None
    calls = run.get('calls')
    events = run.get('events', [])
    if (not isinstance(calls, list) or any(not isinstance(c, dict) for c in calls)
            or not isinstance(events, list) or any(not isinstance(e, dict) for e in events)):
        return None
    from .stage_control import _unresolved_owner_effects
    if _unresolved_owner_effects(run, task):
        return None
    proposed = {}
    for index, step in enumerate(args['checks']):
        if step['action'].startswith('assert_'):
            key = _key(_setup(args, index))
            if key is not None:
                proposed[key] = index
    groups = {}
    source_version = None
    seen = set()
    for call in calls:
        if call.get('runId', run['id']) != run['id']:
            return None
        if call.get('agentId') != task['agentId']:
            continue
        tool, call_args = call.get('tool'), call.get('args')
        mutation = tool in ('file.write', 'file.patch', 'file.delete', 'code.run') or (
            tool == 'code.test' and (not isinstance(call_args, dict) or call_args.get('language') != 'browser'))
        if mutation:
            groups = {}; source_version = None
            continue
        if tool != 'code.test':
            continue
        identifier = call.get('id')
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            return None
        seen.add(identifier)
        if (call.get('taskId') != task['id'] or type(call.get('revision')) is not int
                or call['revision'] != task['revision']):
            groups = {}; source_version = None
            continue
        parsed = _failed_check(call)
        if parsed is None:
            # Success, preflight/infra errors and malformed/unexecuted evidence
            # cannot prove an unchanged failing browser attempt.
            groups = {}; source_version = None
            continue
        manifest, report, failed, index, setup = parsed
        if source_version != manifest:
            groups = {}
        source_version = manifest
        key = _key(setup)
        if key is not None:
            groups.setdefault(key, []).append((identifier, report, failed, index))
    matches = [(key, rows) for key, rows in groups.items() if key in proposed and len(rows) >= 2]
    if not matches:
        return None
    key, rows = max(matches, key=lambda item: len(item[1]))
    latest_id, report, failed, actual_index = rows[-1]
    identity = {'runId': run['id'], 'taskId': task['id'], 'agentId': task['agentId'],
                'revision': task['revision'], 'sourceManifest': source_version, 'failureSetup': key}
    signature = hashlib.sha256(_key(identity).encode()).hexdigest()
    # Persisted host events are the rejection ledger. They count proposals, not
    # executions. A restart does not grant another endless set of warnings.
    prior = len({e['id'] for e in events
                 if isinstance(e.get('id'), str) and e['id']
                 and e.get('type') == 'browser_failure_recovery'
                 and e.get('agentId') == task['agentId']
                 and isinstance(e.get('payload'), dict)
                 and e['payload'].get('failureSignature') == signature
                 and e['payload'].get('taskId') == task['id']})
    blocked = prior + 1
    diagnostic = {k: copy.deepcopy(failed[k]) for k in ('action', 'pageId', 'selector', 'key', 'name', 'expected', 'actual', 'passed') if k in failed}
    for k, v in list(diagnostic.items()):
        if isinstance(v, str) and len(v) > 800:
            diagnostic[k] = v[:800]; diagnostic[k + 'Truncated'] = True
    return {'ok': False, 'actionExecuted': False, 'notExecuted': True,
        'runId': run['id'], 'taskId': task['id'], 'agentId': task['agentId'], 'revision': task['revision'],
        'tool': 'code.test', 'failureSignature': signature,
        'error': {'code': 'repeated_browser_assertion_failure', 'retryable': False,
                  'message': 'This still-unchanged failing check is rejected. Changing surrounding read-only assertions or later steps does not correct the observed failure.'},
        'rejectionScope': {'kind': 'same_failing_check_and_stateful_setup_on_unchanged_sources',
            'allBrowserTestingBlocked': False, 'correctedProposalRequiresNormalChecks': True,
            'newPermissionGranted': False, 'proposalFailedCheckIndex': proposed[key],
            'ignoredDifferences': 'Other read-only assertions and steps after the failed check only.'},
        'observedFailures': {'count': len(rows), 'callIds': [row[0] for row in rows[-4:]],
            'callIdsTruncated': len(rows) > 4, 'latestCallId': latest_id,
            'failedAssertions': [diagnostic], 'failedAssertionsOmitted': 0,
            'latestActualFailedCheckIndex': actual_index},
        'sourceManifest': [{'path': p, 'sha256': h} for p, h in source_version.items()],
        'failureObservation': failure_observation_view(report.get('failureObservation')),
        'recovery': {'blockedProposalCount': blocked, 'maxBlockedProposals': MAX_BLOCKED_PROPOSALS,
                     'stopTask': blocked >= MAX_BLOCKED_PROPOSALS,
                     'nextUnchangedProposalStopsTask': blocked == MAX_BLOCKED_PROPOSALS - 1},
        'instruction': 'Compare the failed assertion with actual page observations and the relevant source, including selector/row identity, '
            'dates and generated IDs. Diagnose test error versus product defect before retrying. Correct the failing check or its '
            'state-changing setup only when evidence supports it; do not weaken requirements or merely shuffle other assertions. '
            'A corrected check/setup or a real source edit can be proposed under the original schema, stage, access and deadline checks. '
            'Alternatively submit a bounded review rework with a demonstrated defect, or an honest final partial with missing coverage. '
            'After two rejected proposals for this failure, a third still-unchanged proposal stops this task without accepting it. '
            'Observed failure count excludes these rejected proposals. No test, product edit, review, publication or extra time is created.'}
