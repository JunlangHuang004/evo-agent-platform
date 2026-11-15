"""Structured evidence handoff, independently implemented with the standard library.

Design reference: CrewAI commit f4da03af1be0f61db71c8599a40c06dce98b9b48,
tasks/task_output.py (raw/expected_output/json_dict/agent/tool_failures) and
flow/persistence/sqlite.py (JSON state retained for restoration). No source or
dependency is copied. Evidence remains distinct from semantic task acceptance.
"""
import copy
import json


_MAX_RAW = 18000
_MAX_ARTIFACTS = 40
_MAX_RECORDS = 16
_MAX_DETAIL = 1000
_UNCERTAIN = {'pending', 'running', 'unknown', 'interrupted', 'cancelled', 'reconciled'}


def _clip(value, width):
    if not isinstance(value, str) or len(value) <= width:
        return copy.deepcopy(value)
    marker = '[TRUNCATED]'
    return value[:max(0, width - len(marker))] + marker


def _json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key.')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('Nonfinite JSON constant.')

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('A JSON object is required.')
    # JSON numbers such as 1e999 can overflow Python float; keep evidence safely
    # serializable by our strict persistence layer rather than injecting Infinity.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return value


def _tool_record(call, failure_type):
    result = call.get('result')
    record = {key: copy.deepcopy(call.get(key)) for key in ('taskId', 'agentId', 'tool', 'status')}
    record.update(callId=call.get('id'), type=failure_type)
    if isinstance(result, dict):
        error = result.get('error')
        if isinstance(error, dict):
            record['error'] = {key: _clip(error[key], _MAX_DETAIL if key == 'message' else 100)
                               for key in ('code', 'message', 'retryable') if key in error}
        elif isinstance(error, str):
            record['error'] = {'message': _clip(error, _MAX_DETAIL)}
        elif isinstance(result.get('stderr'), str) and result['stderr']:
            record['error'] = {'message': _clip(result['stderr'], _MAX_DETAIL)}
        for key in ('exitCode', 'timedOut', 'cancelled', 'denied'):
            if key in result:
                record[key] = copy.deepcopy(result[key])
    elif isinstance(result, str):
        record['error'] = {'message': _clip(result, _MAX_DETAIL)}
    if 'durationMs' in call:
        record['durationMs'] = call['durationMs']
    return record


def build_task_output(task, run):
    """Build a detached JSON-compatible output from actual server task/run data.

    Missing validation stays None. Missing/malformed call logs stay None, rather
    than becoming an invented empty failure history. Permission denial is a tool
    outcome, not a model failure; unknown effects remain explicitly uncertain.
    Rebuild after review/rework so a cached prior verdict cannot silently survive.
    """
    if not isinstance(task, dict) or not isinstance(run, dict):
        raise ValueError('Task and run records must be dictionaries.')
    raw = task.get('result')
    contract = copy.deepcopy(task.get('outputContract'))
    output_validation = copy.deepcopy(task.get('outputValidation')) if isinstance(task.get('outputValidation'), dict) else None
    review_validation = copy.deepcopy(task.get('validation')) if isinstance(task.get('validation'), dict) else None
    criteria = task.get('acceptanceCriteria')
    artifacts = task.get('outputs')
    calls = run.get('calls')
    calls_available = (isinstance(task.get('id'), str) and bool(task['id'])
                       and isinstance(calls, list) and all(isinstance(call, dict) for call in calls))
    output = {'schemaVersion': 1, 'taskId': task.get('id'), 'agentId': task.get('agentId'),
              'status': task.get('status'), 'revision': task.get('revision'),
              'expectedOutput': [_clip(item, 1000) for item in criteria[:20]] if isinstance(criteria, list) else None,
              'raw': _clip(raw, _MAX_RAW) if isinstance(raw, str) else None,
              'rawTruncated': isinstance(raw, str) and len(raw) > _MAX_RAW,
              'outputFormat': 'raw', 'jsonDict': None,
              'artifactIds': copy.deepcopy(artifacts[:_MAX_ARTIFACTS]) if isinstance(artifacts, list) else None,
              'artifactIdsTotal': len(artifacts) if isinstance(artifacts, list) else None,
              'artifactIdsTruncated': len(artifacts) > _MAX_ARTIFACTS if isinstance(artifacts, list) else None,
              'outputContract': contract, 'outputValidation': output_validation,
              'reviewValidation': review_validation,
              'toolFailures': None, 'toolFailuresTotal': None, 'toolFailuresTruncated': None,
              'toolUncertainties': None, 'toolUncertaintiesTotal': None, 'toolUncertaintiesTruncated': None,
              'evidenceAvailability': {'raw': isinstance(raw, str), 'calls': calls_available,
                                       'outputValidation': output_validation is not None,
                                       'reviewValidation': review_validation is not None},
              'semanticQuality': 'unassessed'}
    if isinstance(criteria, list):
        output['expectedOutputTotal'] = len(criteria)
        output['expectedOutputTruncated'] = len(criteria) > 20 or any(isinstance(item, str) and len(item) > 1000 for item in criteria)
    passed = output_validation is not None and output_validation.get('status') == 'passed'
    kind = contract.get('kind') if isinstance(contract, dict) else None
    if kind == 'json' and passed and isinstance(raw, str) and len(raw) <= _MAX_RAW:
        try:
            output['jsonDict'] = _json_object(raw)
        except (ValueError, RecursionError):
            output['structuredIssue'] = 'Declared JSON output could not be safely reconstructed; raw evidence retained.'
        else:
            output['outputFormat'] = 'json'
    elif kind == 'artifact' and passed:
        output['outputFormat'] = 'artifact'
    if calls_available:
        failures, uncertainties = [], []
        for call in calls:
            if call.get('taskId') != task.get('id') or not isinstance(call.get('tool'), str) or call['tool'] == 'llm.chat':
                continue
            status, result = call.get('status'), call.get('result')
            if status in _UNCERTAIN or isinstance(result, dict) and result.get('needsApproval'):
                uncertainties.append(_tool_record(call, 'outcome_uncertain'))
            elif status == 'denied':
                failures.append(_tool_record(call, 'permission_denied'))
            elif status == 'failed':
                failures.append(_tool_record(call, 'execution_failure'))
            elif status == 'success' and isinstance(result, dict) and result.get('ok') is False:
                record = _tool_record(call, 'reported_failure')
                record['statusConflictsWithResult'] = True
                failures.append(record)
        for key, records in (('toolFailures', failures), ('toolUncertainties', uncertainties)):
            output[key] = records[:_MAX_RECORDS]
            output[key + 'Total'] = len(records)
            output[key + 'Truncated'] = len(records) > _MAX_RECORDS
    return output


def _validation_for_context(value):
    if not isinstance(value, dict):
        return None
    scalar_keys = ('status', 'kind', 'semanticQuality', 'verdict', 'verified', 'assessed',
                   'targetRevision', 'reviewerTaskId')
    result = {key: copy.deepcopy(value[key]) for key in scalar_keys if key in value}
    if 'feedback' in value:
        result['feedback'] = _clip(value['feedback'], 320)
        result['feedbackTruncated'] = bool(value.get('feedbackTruncated')) or isinstance(value['feedback'], str) and len(value['feedback']) > 320
    for key in ('issues', 'checkedArtifactIds', 'matchedArtifactIds', 'evidenceCallIds'):
        if isinstance(value.get(key), list):
            items = value[key]
            result[key] = [_clip(item, 320) for item in items[:8]]
            result[key + 'Total'] = value.get(key + 'Total', len(items))
            result[key + 'Truncated'] = bool(value.get(key + 'Truncated')) or len(items) > 8 or any(isinstance(item, str) and len(item) > 320 for item in items)
    if isinstance(value.get('artifactHashes'), dict):
        hashes = value['artifactHashes']
        result['artifactHashes'] = dict(list(hashes.items())[:8])
        result['artifactHashesTotal'] = value.get('artifactHashesTotal', len(hashes))
        result['artifactHashesTruncated'] = bool(value.get('artifactHashesTruncated')) or len(hashes) > 8
    return result


def task_output_for_context(output):
    """Bounded prompt projection; dependencyResults.result already carries raw.

    Small jsonDict values remain exact. Large values are explicitly omitted, not
    clipped into fabricated structured facts. Original output objects are intact.
    """
    if not isinstance(output, dict):
        raise ValueError('A structured task output is required.')
    projected = copy.deepcopy(output)
    projected.pop('raw', None)
    projected['rawInDependencyResult'] = True
    for key in ('outputValidation', 'reviewValidation'):
        projected[key] = _validation_for_context(output.get(key))
    for key in ('toolFailures', 'toolUncertainties'):
        if isinstance(output.get(key), list):
            projected[key] = copy.deepcopy(output[key][:4])
            projected[key + 'Truncated'] = bool(output.get(key + 'Truncated')) or len(output[key]) > 4
            for record in projected[key]:
                if isinstance(record.get('error'), dict) and 'message' in record['error']:
                    message = record['error']['message']
                    record['error']['message'] = _clip(message, 320)
                    record['error']['messageTruncated'] = bool(record['error'].get('messageTruncated')) or isinstance(message, str) and len(message) > 320
    if isinstance(output.get('expectedOutput'), list):
        projected['expectedOutput'] = [_clip(item, 320) for item in output['expectedOutput'][:4]]
        projected['expectedOutputTruncated'] = bool(output.get('expectedOutputTruncated')) or len(output['expectedOutput']) > 4 or any(isinstance(item, str) and len(item) > 320 for item in output['expectedOutput'])
    if isinstance(output.get('artifactIds'), list):
        projected['artifactIds'] = copy.deepcopy(output['artifactIds'][:8])
        projected['artifactIdsTruncated'] = bool(output.get('artifactIdsTruncated')) or len(output['artifactIds']) > 8
    if output.get('jsonDict') is not None:
        raw_json = json.dumps(output['jsonDict'], ensure_ascii=False, allow_nan=False)
        if len(raw_json) > 4096:
            projected['jsonDict'] = None
            projected['jsonDictOmitted'] = True
            projected['jsonDictInDependencyResult'] = True
    return projected
