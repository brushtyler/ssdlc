"""Exact owner-verification deltas within the corrective scheduler journal."""
import copy
import re
from ssdlc_tasks import digest

KINDS = frozenset('OWNER_REVALIDATION_STARTED OWNER_CHECKS_STARTED OWNER_CHECK_FINISHED OWNER_CHECKS_FINISHED OWNER_REVALIDATION_FINISHED OWNER_TASKS_INVALIDATED'.split())


def verify_event(kind, current, previous, tid, require):
    """Accept owner checks, never an arbitrary same-state task mutation."""
    tasks, before = current['tasks'], previous['tasks']
    rec, old = tasks[tid], before[tid]
    authority = old.get('scope_reconciliation', {}).get('owner_revalidation')
    require(isinstance(authority, dict))
    value = authority['contract']
    require(rec.get('scope_reconciliation') == old.get('scope_reconciliation'))
    require(rec['status'] == old['status'] == 'VERIFYING')
    a, b = copy.deepcopy(rec), copy.deepcopy(old)
    runs, prior = a.pop('owner_revalidations', []), b.pop('owner_revalidations', [])
    require(a == b)
    require(bool(runs))
    run = runs[-1]
    require(run['attempt'] == rec['attempts'] and run['contract_sha256'] == digest(value))
    require(run['head'] == rec['start_commit'] and run.get('capture_sha256') == rec.get('capture_sha256'))
    require(isinstance(run.get('source_sha256'), str) and re.fullmatch('[a-f0-9]{64}', run['source_sha256']))
    owners = list(value['owners'])
    require(list(run['owners']) == owners[:len(run['owners'])])
    if kind == 'OWNER_REVALIDATION_STARTED':
        require(len(runs) == len(prior) + 1)
        for entry in prior:
            if entry['status'] == 'RUNNING': entry['status'] = 'INTERRUPTED'
        require(runs[:-1] == prior and run['status'] == 'RUNNING')
        require(run['owners'] == {} and run['coverage_preserved'] is False)
    else:
        require(len(runs) == len(prior) and runs[:-1] == prior[:-1])
        previous_run = prior[-1]
        require(type(run['coverage_preserved']) is bool)
        x, y = copy.deepcopy(run), copy.deepcopy(previous_run)
        if kind == 'OWNER_CHECKS_STARTED':
            require(run['status'] == previous_run['status'] == 'RUNNING')
            require(len(run['owners']) == len(previous_run['owners']) + 1)
            new_owner = list(run['owners'])[-1]
            require(run['owners'][new_owner] == {'checks': []})
            x['owners'].pop(new_owner)
            if not previous_run['owners']: x['coverage_preserved'] = y['coverage_preserved']
        elif kind in {'OWNER_CHECK_FINISHED', 'OWNER_CHECKS_FINISHED'}:
            require(list(run['owners']) == list(previous_run['owners']) and bool(run['owners']))
            owner = list(run['owners'])[-1]
            row, prev = x['owners'][owner], y['owners'][owner]
            if kind == 'OWNER_CHECK_FINISHED':
                require(row['checks'][:-1] == prev['checks'] and len(row['checks']) == len(prev['checks']) + 1)
                check = row['checks'].pop()
                require(isinstance(check.get('log_ref'), str) and bool(check.get('sha256')))
                require(type(check.get('exit_code')) is int and type(check.get('empty_suite')) is bool)
            else:
                require(bool(row['checks']))
                passed = all(c['exit_code'] == 0 and not c['empty_suite'] for c in row['checks'])
                require(row.pop('status') == ('PASSED' if passed else 'FAILED'))
        elif kind == 'OWNER_REVALIDATION_FINISHED':
            require(list(run['owners']) == owners and previous_run['status'] == 'RUNNING')
            success = run['coverage_preserved'] and all(row.get('status') == 'PASSED' for row in run['owners'].values())
            require(run['status'] == ('PASSED' if success else 'FAILED'))
            x['status'] = y['status']
            if not owners: x['coverage_preserved'] = y['coverage_preserved']
        elif kind == 'OWNER_TASKS_INVALIDATED':
            require(run == previous_run and run['status'] == 'FAILED')
        require(x == y)
    if kind == 'OWNER_TASKS_INVALIDATED':
        block = current.get('owner_revalidation_blocked', {})
        require(block.get('task_id') == tid and block.get('owners') == owners)
        require(block.get('attempt') == rec['attempts'])
        require(block.get('reason') == ('owner_checks_failed' if run['coverage_preserved'] else 'coverage_review_required'))
        affected = set(block.get('closure', [])) - {tid}
        require(set(owners) <= set(block.get('closure', [])) and affected <= set(tasks))
        for task_id in tasks:
            expected = copy.deepcopy(before[task_id])
            if task_id in affected: expected['status'] = 'INVALIDATED'
            require(tasks[task_id] == expected)
    else:
        require(all(tasks[t] == before[t] for t in tasks if t != tid))
        require(current.get('owner_revalidation_blocked') == previous.get('owner_revalidation_blocked'))
