"""Host-owned coverage and fresh owner checks for scope-only amendments.

No model dispatch or second executor. These checks run inside TaskScheduler's
existing captured-candidate verification and commit-intent recovery path.
"""
import ast
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from ssdlc_tasks import TaskError, digest, file_hash, git, confined


def contract(source, paths):
    from ssdlc_feedback_analysis import path_classes
    classes = path_classes(source, paths)
    selected = {p: c for p, c in classes.items()
                if c['owners'] or c['kind'] == 'BASELINE_TEST'}
    if not selected:
        return None
    tasks = {t['task_id']: t for t in source['plan']['tasks']}
    owners = sorted({tid for c in selected.values() for tid in c['owners']})
    head = source['journal']['expected_head']
    return dict(schema_version=1, capability='scope-owner-revalidation-v1',
        base_head=head, files={p: dict(c, sha256=(file_hash(source['root'] / 'worktree' / p)
                                                if (source['root'] / 'worktree' / p).is_file() else None))
                               for p, c in selected.items()},
        owners={tid: dict(task=copy.deepcopy(tasks[tid]),
                         commit=source['journal']['tasks'][tid]['commit'],
                         capture_sha256=source['journal']['tasks'][tid].get('capture_sha256'))
                for tid in owners})


def scope_contract(source, paths):
    inherited = source['journal']['tasks'][source['task_id']].get('scope_reconciliation', {}).get('owner_revalidation')
    combined = set(paths)
    if inherited:
        combined.update(inherited['contract']['files'])
    return contract(source, sorted(combined))


def validate_review(value, coverage):
    """A coverage exception is an exact reviewed future file, never a wildcard."""
    if (not isinstance(coverage, dict) or set(coverage) != {
            'contract_sha256', 'coverage_preserved', 'approved_test_changes'}
            or coverage['contract_sha256'] != digest(value)
            or coverage['coverage_preserved'] is not True
            or not isinstance(coverage['approved_test_changes'], list)):
        raise TaskError('Explicit owner coverage review must bind the exact owner contract')
    seen = set()
    patch_bytes = 0
    patches = 0
    for change in coverage['approved_test_changes']:
        fields = {'path', 'old_sha256', 'new_sha256', 'reason'}
        if isinstance(change, dict) and 'patch' in change:
            fields.add('patch')
        if (not isinstance(change, dict) or set(change) != fields
                or not isinstance(change['path'], str)
                or change['path'] in seen or change['path'] not in value['files']
                or value['files'][change['path']]['kind'] not in {'COMPLETED_TEST', 'BASELINE_TEST'}
                or change['old_sha256'] != value['files'][change['path']]['sha256']
                or change['new_sha256'] is not None and (not isinstance(change['new_sha256'], str)
                    or not re.fullmatch(r'[a-f0-9]{64}', change['new_sha256']))
                or not isinstance(change['reason'], str) or not change['reason'].strip()
                or len(change['reason']) > 1600):
            raise TaskError('Test coverage changes require an exact independently reviewed hash and reason')
        if 'patch' in change:
            patch = change['patch']
            if not isinstance(patch, str) or '\0' in patch or not patch:
                raise TaskError('Reviewed test patch must be bounded unified diff text')
            old = '/dev/null' if change['old_sha256'] is None else 'a/'+change['path']
            new = '/dev/null' if change['new_sha256'] is None else 'b/'+change['path']
            lines = patch.splitlines(keepends=True)
            if (lines[:2]!=['--- '+old+'\n','+++ '+new+'\n']
                    or any(line.startswith(('--- ','+++ ','diff --git ','GIT binary patch')) for line in lines[2:])):
                raise TaskError('Reviewed patch must affect only its exact declared test file')
            patch_bytes += len(json.dumps(patch).encode())
            patches += 1
        seen.add(change['path'])
    if patch_bytes > 4096 or patches > 4:
        raise TaskError('Reviewed test patch bundle exceeds its bounded prompt allowance')


def validate_patches(value, coverage, worktree):
    """Apply reviewed data in a disposable directory, never in the candidate."""
    validate_review(value, coverage)
    from ssdlc_candidates import git_environment
    for change in coverage['approved_test_changes']:
        if 'patch' not in change:
            continue
        path = confined(worktree, change['path'])
        old = path.read_bytes() if path.is_file() else None
        if (hashlib.sha256(old).hexdigest() if old is not None else None)!=change['old_sha256']:
            raise TaskError('Reviewed test patch base differs from its approved old hash')
        with tempfile.TemporaryDirectory(prefix='ssdlc-reviewed-patch-') as temporary:
            root = Path(temporary)
            target = root/change['path']
            if old is not None:
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(old)
            command = ['git','--literal-pathspecs','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false']
            initialized = subprocess.run(command+['init','--quiet','--template='],cwd=root,
                env=git_environment(),capture_output=True)
            if initialized.returncode:
                raise TaskError('Cannot validate reviewed test patch in a disposable repository')
            applied = subprocess.run(command+['apply','--whitespace=nowarn','-'],input=change['patch'],
                text=True,cwd=root,env=git_environment(),capture_output=True)
            actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
            if applied.returncode or actual!=change['new_sha256']:
                raise TaskError('Reviewed test patch does not produce its approved new hash')


def prompt_summary(authority):
    """The full sealed check/owner contracts are host authority, not prompt bulk."""
    value = authority['contract']
    owners, paths = sorted(value['owners']), sorted(value['files'])
    summary = dict(contract_sha256=digest(value), owner_tasks=owners[:12], files=paths[:12],
        omitted_owners=max(0,len(owners)-12), omitted_files=max(0,len(paths)-12),
        rule='Preserve owner obligations and tests. Host runs fresh owner checks before accepting this candidate.',
        reviewed_test_changes=[dict(path=c['path'],new_sha256=c['new_sha256'])
            for c in authority['coverage_review']['approved_test_changes'][:4]],
        omitted_test_changes=max(0,len(authority['coverage_review']['approved_test_changes'])-4))
    patches = [dict(path=c['path'],new_sha256=c['new_sha256'],patch=c['patch'])
               for c in authority['coverage_review']['approved_test_changes'] if 'patch' in c]
    if patches:
        summary['reviewed_patches'] = patches
        summary['patch_instruction'] = 'Apply these exact independently reviewed test patches unless the file already matches its new hash. Preserve their reviewed outcomes.'
    return summary


def _coverage_facts(text):
    """Conservative syntactic guard, not a semantic proof of equivalent tests."""
    tree = ast.parse(text)
    assertions, tests, skips = Counter(), Counter(), Counter()
    skip_names = {'skip', 'skipIf', 'skipUnless', 'skipif', 'skipunless', 'skipTest',
                  'xfail', 'expectedFailure', '__unittest_skip__', '__unittest_expecting_failure__'}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
            tests[node.name] += 1
        if isinstance(node, ast.Assert) or (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr.startswith('assert')):
            assertions[ast.dump(node, include_attributes=False)] += 1
        if isinstance(node, ast.Call):
            name = (node.func.attr if isinstance(node.func, ast.Attribute) else
                    node.func.id if isinstance(node.func, ast.Name) else '')
            if name in skip_names:
                skips[ast.dump(node, include_attributes=False)] += 1
        elif isinstance(node, (ast.Attribute, ast.Name)):
            name = node.attr if isinstance(node, ast.Attribute) else node.id
            if name in skip_names:
                skips[ast.dump(node, include_attributes=False)] += 1
    return assertions, tests, skips


def coverage_ok(scheduler, value, review, changed):
    validate_review(value, review)
    exceptions = {c['path']: c for c in review['approved_test_changes']}
    for name, entry in value['files'].items():
        if name not in changed or entry['kind'] not in {'COMPLETED_TEST', 'BASELINE_TEST'}:
            continue
        path = confined(scheduler.wt, name)
        actual = file_hash(path) if path.is_file() else None
        if name in exceptions and actual == exceptions[name]['new_sha256']:
            continue
        if actual is None:
            return False
        if entry['sha256'] is None:
            continue
        old = git(scheduler.wt, 'show', value['base_head'] + ':' + name)
        try:
            before, after = _coverage_facts(old), _coverage_facts(path.read_text())
        except (SyntaxError, UnicodeError, OSError):
            return False
        if before[0] - after[0] or before[1] - after[1] or after[2] - before[2]:
            return False
    return True


def verify(scheduler, task, record, directory, changed):
    authority = scheduler.scope_reconciliations.get(task['task_id'], {}).get('owner_revalidation')
    if not authority:
        return True
    value, review = authority['contract'], authority['coverage_review']
    validate_review(value, review)
    if not set(value['files']) & set(changed):
        return True
    owners = list(value['owners'])
    tasks = scheduler._plan_index()
    for tid in owners:
        if (tasks.get(tid) != value['owners'][tid]['task']
                or scheduler.state['tasks'][tid]['status'] != 'COMPLETED'
                or scheduler.state['tasks'][tid].get('commit') != value['owners'][tid]['commit']):
            raise TaskError('Owner contract or completed checkpoint changed')
    runs = record.setdefault('owner_revalidations', [])
    for previous in runs:
        if previous['status'] == 'RUNNING':
            previous['status'] = 'INTERRUPTED'
    run = dict(attempt=record['attempts'], status='RUNNING', contract_sha256=digest(value),
               head=record['start_commit'], capture_sha256=record.get('capture_sha256'),
               source_sha256=scheduler._source_signature(changed), owners={}, coverage_preserved=False)
    runs.append(run)
    scheduler._save('OWNER_REVALIDATION_STARTED')
    run['coverage_preserved'] = coverage_ok(scheduler, value, review, changed)
    success = run['coverage_preserved']
    # Only observations from this exact, unchanged captured candidate are
    # shared. Never reuse historical checkpoints or a previous verification.
    reuse = {digest(check): copy.deepcopy(result) for check, result in
             zip(scheduler._check_plan(task), record['checks'])}
    protected, ignored = scheduler._protected(), scheduler._ignored()
    def unchanged(*args):
        scheduler._assert_tree()
        if (scheduler._changed() != changed or scheduler._source_signature(changed) != run['source_sha256']
                or scheduler._protected() != protected or scheduler._ignored() != ignored):
            raise TaskError('Owner verifier changed candidate source or metadata')
    for tid in owners:
        scheduler.guard(); scheduler.budget()
        row = run['owners'][tid] = dict(checks=[])
        owner_dir = directory / 'owners' / tid
        owner_dir.mkdir(parents=True, exist_ok=False)
        scheduler._save('OWNER_CHECKS_STARTED')
        checks = scheduler._verify(tasks[tid], row, owner_dir, event='OWNER_CHECK_FINISHED',
                                   before_check=unchanged, after_check=unchanged, exclusive=True, reuse=reuse)
        unchanged()
        passed = ([c['check_id'] for c in checks] == scheduler._expected_check_ids(tid)
                  and all(c['exit_code'] == 0 and not c['empty_suite'] for c in checks))
        row['status'] = 'PASSED' if passed else 'FAILED'
        success = success and passed
        scheduler._save('OWNER_CHECKS_FINISHED')
    run['status'] = 'PASSED' if success else 'FAILED'
    scheduler._save('OWNER_REVALIDATION_FINISHED')
    if not success:
        # Even a coverage refusal is not permission to retry a developer or
        # silently repair historical completed tasks. I5a owns that recovery.
        closure, _ = scheduler._closure(set(owners))
        affected = [tid for tid in closure if tid != task['task_id']]
        for tid in affected:
            scheduler.state['tasks'][tid]['status'] = 'INVALIDATED'
        scheduler.state['owner_revalidation_blocked'] = dict(task_id=task['task_id'],
            owners=owners, closure=closure, attempt=record['attempts'],
            reason='owner_checks_failed' if run['coverage_preserved'] else 'coverage_review_required')
        scheduler._save('OWNER_TASKS_INVALIDATED')
    return success


def stamp(record, tree):
    for run in record.get('owner_revalidations', []):
        if run['attempt'] == record['attempts'] and run['status'] == 'PASSED':
            run['candidate_tree'] = tree


def evidence_problems(scheduler, task_id, record):
    problems = []
    for run in record.get('owner_revalidations', []):
        for owner, row in run['owners'].items():
            for check in row['checks']:
                _, reason = scheduler._resolve_evidence(check['log_ref'], check['sha256'])
                if reason:
                    problems.append(dict(task_id=task_id, check_id=check.get('check_id'),
                                         path=check['log_ref'], reason='owner_' + reason))
    authority = scheduler.scope_reconciliations.get(task_id, {}).get('owner_revalidation')
    if authority and record.get('status') in {'COMMIT_INTENT', 'COMPLETED'}:
        value = authority['contract']
        if set(value['files']) & set(record.get('changed_files', [])):
            passed = [r for r in record.get('owner_revalidations', [])
                      if r['attempt'] == record['attempts'] and r['status'] == 'PASSED'
                      and r['contract_sha256'] == digest(value)
                      and r.get('candidate_tree') == record.get('candidate_tree')
                      and r.get('capture_sha256') == record.get('capture_sha256')
                      and r['head'] == record['start_commit'] and r['coverage_preserved'] is True
                      and set(r['owners']) == set(value['owners'])
                      and all(row.get('status') == 'PASSED'
                              and [c['check_id'] for c in row['checks']] == scheduler._expected_check_ids(owner)
                              and all(c['exit_code'] == 0 and not c['empty_suite'] for c in row['checks'])
                              for owner, row in r['owners'].items())]
            if not passed:
                problems.append(dict(task_id=task_id, check_id=None, path=None,
                                     reason='missing_fresh_owner_revalidation'))
    return problems


def _operator_accounting(source, row, state):
    """Only exact captured consultations may advance a request's old counters.

    The operator separately reviews the changed registered-evidence inventory;
    this relation never waives a plan/task/candidate or accounting mutation.
    """
    from ssdlc_transitions import _regular
    from ssdlc_feedback_roles import _usage
    anchor = row.get('accounting_anchor', {})
    before, after = anchor.get('counters'), source['counters']
    if (not isinstance(before, dict) or anchor.get('envelope_sha256') != digest(source['envelope'])
            or anchor.get('worktree_sha256') != source['worktree_sha256']
            or row['binding']['counters_sha256'] != digest(before)
            or {k:v for k,v in before.items() if k!='invocations'}
                != {k:v for k,v in after.items() if k!='invocations'}
            or any(after['invocations'].get(k)!=v for k,v in before['invocations'].items())):
        raise TaskError('Operator reconciliation changed historical accounting; semantic inputs must match')
    raw = _regular(source['root'], 'limits/task-journal.jsonl').read_bytes()
    size = anchor.get('journal_size')
    if (type(size) is not int or size < 1 or size > len(raw)
            or not raw[:size].endswith(b'\n')
            or hashlib.sha256(raw[:size]).hexdigest() != anchor.get('journal_sha256')):
        raise TaskError('Operator accounting prefix changed')
    added = set(after['invocations']) - set(before['invocations'])
    calls = [c for c in state['role_calls'] if c['request_id']==row['request_id']
             and c.get('native', {}).get('invocation_id') not in before['invocations']]
    identities = [c.get('native', {}).get('invocation_id') for c in calls]
    if len(set(identities))!=len(calls) or set(identities)!=added:
        raise TaskError('Operator accounting includes unrelated or uncaptured invocations')
    for call in calls:
        native = call['native']; receipt = call.get('receipt', {})
        response = receipt.get('response', {}); accounted = after['invocations'][native['invocation_id']]
        if (call['status']!='RETURNED' or digest(response)!=receipt.get('response_sha256')
                or native.get('limits_sha256')!=digest(source['envelope'])
                or accounted.get('status')!='returned' or accounted.get('usage')!=_usage(response.get('usage'))
                or accounted.get('role')!='feedback-'+call['role'] or accounted.get('phase')!='feedback'
                or accounted.get('engine')!=call['actor']['engine']):
            raise TaskError('Operator accounting differs from captured funded consultations')
    return sorted(added)


def feedback_binding(ticket, source, paths, request_id, *, reconcile_evidence=False, operator_owner_limit=None,
                     grant_override=None):
    """Operator bridge: retain a pending/reviewed request as an immutable fact."""
    from ssdlc_feedback import FeedbackStore, matches_source, source_binding, validate_request
    from ssdlc_feedback_return import _head
    store = FeedbackStore(ticket)
    authority, state = store._read(); store._active()
    row = state['requests'].get(request_id)
    if (not row or row['status'] not in {'REQUESTED', 'PROPOSED', 'REVIEWED'}
            or row['request']['cause'] != 'MISSING_TARGET'
            or sorted(paths) != sorted(row['request']['requested_files'])
            or any(r['request_id'] == request_id for r in state.get('amendments', []))
            or any(r['request_id'] == request_id for r in state.get('owner_amendments', []))):
        raise TaskError('Owner amendment requires its exact unresolved feedback request')
    reconciliation = None
    if not matches_source(state, row, source):
        current = source_binding(source)
        mutable = {'source_files_sha256', 'counters_sha256'}
        if (reconcile_evidence is not True
                or {k:v for k,v in row['binding'].items() if k not in mutable}
                    != {k:v for k,v in current.items() if k not in mutable}):
            raise TaskError('Changed feedback evidence requires exact operator reconciliation; semantic inputs must match')
        charged = _operator_accounting(source, row, state)
        validate_request(source, row['request'])
        reconciliation = dict(old_source_files_sha256=row['binding']['source_files_sha256'],
            current_source_files_sha256=current['source_files_sha256'],
            unchanged_plan_task_capture_and_worktree=True,
            preserved_counter_prefix=True, captured_consultations=charged,
            interpretation='Current registered evidence is independently reviewed; old request/decisions are historical only')
    grant = (grant_override if grant_override is not None
             else source['envelope']['policy'].get('feedback_delegation'))
    if not grant or not set(paths) <= set(grant['allowed_files']):
        raise TaskError('Operator owner amendment is outside frozen feedback paths')
    allowance = _capacity(state, source['task_id'], paths, grant, operator_owner_limit=operator_owner_limit)
    value = dict(request_id=request_id, feedback_head=_head(store),
                authority_sha256=digest(authority), state_sha256=digest(state),
                request=copy.deepcopy(row), paths=sorted(paths))
    if reconciliation:
        value['source_reconciliation'] = reconciliation
    if allowance:
        value['operator_allowance'] = allowance
    return value


def _capacity(state, task_id, paths, grant, *, operator_owner_limit=None):
    slots = list(state.get('amendments', [])) + list(state.get('owner_amendments', []))
    own = sum(s['task_id']==task_id for s in slots)
    limit = grant['max_amendments_per_task']
    allowance = None
    if operator_owner_limit is not None:
        if (type(operator_owner_limit) is not int or operator_owner_limit<=limit
                or operator_owner_limit!=own+1 or operator_owner_limit>grant['max_amendments']):
            raise TaskError('Operator owner allowance must authorize exactly the next manual amendment')
        allowance = dict(capability='one-exact-manual-owner-amendment-v1', task_id=task_id,
            frozen_limit=limit, observed_amendments=own, manual_limit=operator_owner_limit,
            frozen_grant_sha256=digest(grant))
        limit = operator_owner_limit
    if (len(slots) >= grant['max_amendments']
            or own >= limit
            or sum(len(s['paths']) for s in slots) + len(paths) > grant['max_added_files']):
        raise TaskError('Cumulative owner/scope amendment allowance exhausted')
    return allowance


def validate_allowance_binding(proposal, binding):
    value = binding.get('operator_allowance')
    if (('operator_allowance' in binding)!=('owner_allowance_sha256' in proposal)
            or value is not None and digest(value)!=proposal['owner_allowance_sha256']):
        raise TaskError('Manual owner allowance differs from its exact operator proposal')


def finalize(ticket, proposal, recipe, projection):
    if 'owner_feedback_sha256' not in proposal:
        return
    from ssdlc_feedback import FeedbackStore
    from ssdlc_operator_recovery import validate_binding
    validate_binding(proposal, recipe)
    from ssdlc_tasks import ticket_lock
    store = FeedbackStore(ticket); binding = recipe['subject']['owner_feedback']
    validate_allowance_binding(proposal,binding)
    if digest(binding) != proposal['owner_feedback_sha256']:
        raise TaskError('Owner feedback binding changed')
    receipt = dict(request_id=binding['request_id'], task_id=recipe['subject']['task_id'],
        transition_id=proposal['transition_id'], proposal_sha256=digest(proposal), paths=binding['paths'])
    if 'operator_allowance' in binding:
        receipt['operator_allowance'] = binding['operator_allowance']
    if 'operator_recovery' in recipe['subject']:
        receipt['operator_recovery'] = recipe['subject']['operator_recovery']
    admitted = dict(limits_sha256=digest(recipe['envelope']),
        profile_sha256=digest(recipe['envelope']['policy']), projection_sha256=digest(projection))
    with ticket_lock(store.root / 'feedback.lock'):
        authority, state = store._read()
        prior = [r for r in state.get('owner_amendments', []) if r['transition_id']==proposal['transition_id']]
        if prior:
            if prior != [receipt] or state.get('admitted_generations', {}).get(proposal['transition_id']) != admitted:
                raise TaskError('Owner amendment reconciliation changed')
            return
        if digest(authority) != binding['authority_sha256'] or digest(state) != binding['state_sha256']:
            raise TaskError('Feedback changed before owner amendment reconciliation')
        state.setdefault('owner_amendments', []).append(receipt)
        state.setdefault('admitted_generations', {})[proposal['transition_id']] = admitted
        store._append('OWNER_AMENDMENT_APPLIED', state)


def ensure_reconciled(ticket, projection, proposal):
    if 'owner_feedback_sha256' not in proposal:
        return
    from ssdlc_feedback import FeedbackStore
    from ssdlc_transitions import _regular
    recipe = json.loads(_regular(ticket / 'execution' / proposal['transition_id'], 'recipe.json').read_text())
    from ssdlc_operator_recovery import validate_binding
    validate_binding(proposal, recipe)
    binding = recipe['subject']['owner_feedback']; state = FeedbackStore(ticket).inspect()
    validate_allowance_binding(proposal,binding)
    receipt = dict(request_id=binding['request_id'], task_id=recipe['subject']['task_id'],
        transition_id=proposal['transition_id'], proposal_sha256=digest(proposal), paths=binding['paths'])
    if 'operator_allowance' in binding:
        receipt['operator_allowance'] = binding['operator_allowance']
    if 'operator_recovery' in recipe['subject']:
        receipt['operator_recovery'] = recipe['subject']['operator_recovery']
    if (digest(binding) != proposal['owner_feedback_sha256']
            or [r for r in state.get('owner_amendments', []) if r['transition_id']==proposal['transition_id']] != [receipt]
            or state['requests'].get(binding['request_id']) != binding['request']
            or state.get('admitted_generations', {}).get(proposal['transition_id']) != dict(
                limits_sha256=digest(recipe['envelope']),profile_sha256=digest(recipe['envelope']['policy']),
                projection_sha256=digest(projection))):
        raise TaskError('Owner amendment reconciliation pending; dispatch is fenced')
