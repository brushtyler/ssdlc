"""Engine-owned corrective records and pure recovery decisions.

Hashes detect partial tampering; they are not authentication against a writer
who can replace the registry and both complete journals.
"""
import copy
import math
import os
import tempfile
from pathlib import Path

from ssdlc_tasks import TaskError, TaskJournal, confined, digest, file_hash, validate_document

CODES = tuple('preview_not_candidates preview_tampered evidence_stale evidence_mutated evidence_missing diagnostic_only replan_required impact_ambiguous source_changed tasks_not_completed attempts_exhausted scope_changed protected_path_modified repair_failed dependent_revalidation_failed final_checkpoint_failed uncertain_dispatch budget_interrupted cancelled txn_record_conflict limits_exceeded gate_failure_not_repairable gate_attempts_exhausted journal_lineage_invalid'.split())


class CorrectiveError(TaskError):
    category = 'CORRECTIVE'

    def __init__(self, code, detail=''):
        if code not in CODES:
            raise ValueError('Unknown corrective code')
        self.code = code
        # Details are identifiers supplied by the engine, never finding prose.
        super().__init__(code + (': ' + str(detail)[:120] if detail else ''))


class TransactionJournal(TaskJournal):
    def __init__(self, directory):
        super().__init__(Path(directory))
        self.path = self.directory / 'txn-journal.jsonl'
        self.snapshot = self.directory / 'txn-state.json'


def txn_id_for(preview):
    return digest({k: preview[k] for k in ('phase', 'attempt', 'reviewed_commit', 'preview_revision')})[:32]


def frozen_manifest(preview, reader, controller_contract):
    reader = reader.get('source_hashes', reader)
    union = dict(reader)
    union[controller_contract['ref']] = controller_contract['sha256']
    if preview.get('source_hashes') != union:
        raise CorrectiveError('evidence_mutated')
    return copy.deepcopy(dict(reader=reader, controller_contract=controller_contract))


def build_record(preview_payload, **fields):
    preview = preview_payload
    if (preview.get('execution_authorized') is not False or preview.get('status') != 'candidates'
            or preview.get('blockers') or not preview.get('repair_roots')):
        raise CorrectiveError('preview_not_candidates')
    record = dict(schema_version='1.0', txn_id=txn_id_for(preview), created_by='engine', **fields)
    for key in ('phase', 'attempt', 'reviewed_commit', 'repair_roots', 'closure', 'revalidation_tasks'):
        record.setdefault(key, copy.deepcopy(preview.get(key)))
    verify_record(record)
    return record


def verify_record(record, expected_sha256=None):
    try:
        validate_document('corrective-transaction', record)
        if expected_sha256 is not None and digest(record) != expected_sha256:
            raise ValueError('hash')
    except (TaskError, ValueError, TypeError):
        raise CorrectiveError('txn_record_conflict') from None
    return record


def verify_evidence(record, root, registry, *, inputs_sha256=None,
                    controller_verifier=None, captured_diff_ref=None):
    """Verify frozen bytes read-only, with a separate controller assertion."""
    manifest = record['evidence_manifest']
    entries = dict(manifest['reader'])
    controller = manifest['controller_contract']
    entries[controller['ref']] = controller['sha256']
    for ref, sha in entries.items():
        actual_ref = captured_diff_ref if ref == '03-dev/implementation.diff' else ref
        if not actual_ref:
            raise CorrectiveError('evidence_missing')
        try:
            path = confined(Path(root), actual_ref, protect=False)
            registered = registry.get(actual_ref)
            if isinstance(registered, dict):
                registered = registered.get('sha256')
            if registered != sha or file_hash(path) != sha:
                raise CorrectiveError('evidence_mutated')
        except OSError:
            raise CorrectiveError('evidence_missing') from None
    if controller_verifier is None:
        raise CorrectiveError('evidence_missing')
    controller_verifier(controller)
    if inputs_sha256 != record['inputs_sha256']:
        raise CorrectiveError('evidence_mutated')
    return True


EVENT_KINDS = frozenset('TASKS_INVALIDATED ATTEMPT_RESTORED TASK_STARTED VERIFYING CHECK_FINISHED ATTEMPT_FAILED COMMIT_INTENT TASK_COMPLETED REVALIDATION_STARTED REVALIDATION_TASK_STARTED REVALIDATION_CHECK_STARTED REVALIDATION_CHECK_FINISHED REVALIDATION_TASK_PASSED REVALIDATION_TASK_FAILED REVALIDATION_TASK_BLOCKED REVALIDATION_ABORTED REVALIDATION_FINISHED REVALIDATION_INTERRUPTED PREFLIGHT_STARTED PREFLIGHT_PASSED PREFLIGHT_BLOCKED'.split())
FORBIDDEN_KINDS = frozenset('INITIALIZED PROTOCOL_RECOVERY_RESERVED PROTOCOL_RECOVERY_VERIFYING PROTOCOL_RECOVERY_FAILED PREFLIGHT_BOOTSTRAP_EXEMPT BOOTSTRAP_PROVEN'.split())
NATIVE_CAPTURE_KINDS = frozenset('DISPATCH_RESERVED DISPATCH_PROVEN_NOT_STARTED CAPTURE_INTEGRITY_REJECTED CANDIDATE_CAPTURED CAPTURE_REJECTION_RECOVERED CAPTURE_VERIFICATION_STARTED'.split())
EVENT_KINDS = EVENT_KINDS | NATIVE_CAPTURE_KINDS
TRANSITIONS = {}
for states, kind, target, effect in [
    ('G', 'TASKS_INVALIDATED', 'T_PRE', 'E1'),
    ('T_PRE', 'REVALIDATION_STARTED', 'T', 'E3'),
    ('T R_READY R_FAILED', 'TASK_STARTED', 'R_RUN', 'E5'),
    ('T', 'ATTEMPT_RESTORED', 'R_READY', 'E4'),
    ('R_FAILED', 'ATTEMPT_RESTORED', 'R_FAILED', 'E8r'),
    ('R_RUN', 'VERIFYING', 'R_VER', 'status'),
    ('R_VER', 'CHECK_FINISHED', 'R_VER', 'E9'),
    ('R_VER', 'ATTEMPT_FAILED', 'R_FAILED', 'E8'),
    ('R_VER', 'COMMIT_INTENT', 'R_INTENT', 'E10'),
    ('R_INTENT', 'TASK_COMPLETED', 'T', 'E11'),
    ('T', 'REVALIDATION_TASK_STARTED', 'D_RUN', 'E6'),
    ('T', 'REVALIDATION_TASK_BLOCKED', 'D_FAIL', 'terminal'),
    ('D_RUN', 'REVALIDATION_CHECK_STARTED', 'D_CHECK', 'cursor'),
    ('D_CHECK', 'REVALIDATION_CHECK_FINISHED', 'D_RUN', 'E12'),
    ('D_RUN', 'REVALIDATION_TASK_PASSED', 'T', 'E13'),
    ('D_RUN', 'REVALIDATION_TASK_FAILED', 'D_FAIL', 'terminal'),
    ('D_RUN D_CHECK', 'REVALIDATION_ABORTED', 'D_FAIL', 'terminal'),
    ('DONE', 'REVALIDATION_FINISHED', 'FIN_RUN', 'E14'),
    ('FIN_RUN F_OPEN', 'PREFLIGHT_STARTED', 'F_OPEN', 'open'),
    ('F_OPEN', 'CHECK_FINISHED', 'F_OPEN', 'E9p'),
    ('F_OPEN', 'PREFLIGHT_PASSED', 'F_PASSED', 'E15'),
    ('F_OPEN', 'PREFLIGHT_BLOCKED', 'F_BLOCKED', 'E16'),
]:
    for state in states.split():
        TRANSITIONS[state, kind] = (target, effect)
for state in ('T_PRE', 'T', 'D_RUN', 'D_CHECK', 'DONE'):
    TRANSITIONS[state, 'REVALIDATION_INTERRUPTED'] = ('T' if state.startswith('D_') else state, 'E7')


# Native isolation uses a reserved dispatch and immutable capture receipts.
# Recovery still stops in R_RUN/R_VER; none of these events permits redispatch.
for state in ('T', 'R_READY', 'R_FAILED'):
    TRANSITIONS[state, 'DISPATCH_RESERVED'] = ('R_RESERVED', 'E5')
TRANSITIONS['R_RESERVED', 'TASK_STARTED'] = ('R_RUN', 'native_start')
TRANSITIONS['R_RUN', 'DISPATCH_PROVEN_NOT_STARTED'] = ('R_RESERVED', 'capture')
for kind in NATIVE_CAPTURE_KINDS - {'DISPATCH_RESERVED', 'CAPTURE_VERIFICATION_STARTED', 'DISPATCH_PROVEN_NOT_STARTED'}:
    TRANSITIONS['R_RUN', kind] = ('R_RUN', 'capture')
TRANSITIONS['R_VER', 'CAPTURE_VERIFICATION_STARTED'] = ('R_VER', 'capture_verify')


def _require(condition):
    if not condition:
        raise CorrectiveError('journal_lineage_invalid')


def _checks(items):
    return bool(items) and all(isinstance(c.get('log_ref'), str) and c.get('sha256') for c in items)


def walk_lineage(anchor, events, closure, roots, limits):
    """Walk complete carried snapshots; compare effects to immediate predecessors.

    Anchor contains state, acknowledgment kind, position and txn_id. Physical
    ancestry is checked by verify_lineage before invoking this pure walker.
    """
    previous = copy.deepcopy(anchor['state'])
    initial = copy.deepcopy(previous)
    ack = anchor.get('event', 'TXN_CREATED')
    index = anchor.get('closure_index', -1) + 1
    mode = {'TXN_CREATED': 'G', 'INVALIDATION_APPLIED': 'T_PRE',
            'REPAIR_COMMITTED': 'T', 'DEPENDENT_VERIFIED': 'T',
            'REVALIDATION_RUN_FINISHED': 'FIN_RUN', 'FINAL_PREFLIGHT_PASSED': 'F_PASSED'}.get(ack)
    _require(mode is not None and len(set(closure)) == len(closure) and set(roots) <= set(closure))
    if mode == 'T' and index == len(closure):
        mode = 'DONE'
    counts = dict.fromkeys(closure, 0)
    rolled = False
    last = None
    for event in events:
        kind, current = event['event'], event['state']
        if 'sequence' in event and 'sequence' in anchor:
            _require(event['sequence'] == (last['sequence'] if last else anchor['sequence']) + 1)
            if 'previous' in event:
                _require(event['previous'] == (last['sha256'] if last else anchor['sha256']))
        _require((mode, kind) in TRANSITIONS)
        target, effect = TRANSITIONS[mode, kind]
        tasks, pred = current['tasks'], previous['tasks']
        for tid in tasks:
            _require(tid in pred)
            if tid not in closure:
                _require(tasks[tid] == initial['tasks'][tid])
            for key in ('attempts',):
                _require(tasks[tid].get(key, 0) >= pred[tid].get(key, 0))
            for key in ('failed_attempts', 'revalidations'):
                _require(len(tasks[tid].get(key, [])) >= len(pred[tid].get(key, [])))
            if pred[tid]['status'] == 'COMPLETED' and kind != 'TASKS_INVALIDATED':
                _require(tasks[tid]['status'] == 'COMPLETED')
        _require(set(tasks) == set(pred))
        for key in ('inputs_sha256', 'ignored_paths'):
            _require(current.get(key) == previous.get(key))
        if kind != 'TASK_COMPLETED':
            _require(current['expected_head'] == previous['expected_head'])
        growth = len(current.get('preflights', [])) - len(previous.get('preflights', []))
        _require(growth == (1 if kind == 'PREFLIGHT_STARTED' else 0))
        tid = closure[index] if index < len(closure) else None
        rec, old = tasks.get(tid, {}), pred.get(tid, {})
        if kind in ('TASK_STARTED', 'DISPATCH_RESERVED', 'ATTEMPT_RESTORED', 'VERIFYING', 'ATTEMPT_FAILED', 'COMMIT_INTENT', 'TASK_COMPLETED') or (kind == 'CHECK_FINISHED' and mode == 'R_VER'):
            _require(tid in roots)
            _require(all(tasks[t] == pred[t] for t in tasks if t != tid))
        if effect == 'E1':
            inv = current.get('invalidations', [])
            _require(len(inv) == len(previous.get('invalidations', [])) + 1)
            _require(inv[-1]['roots'] == list(roots) and inv[-1]['closure'] == list(closure))
            _require(inv[-1]['reason'] == 'corrective:' + anchor['txn_id'])
            _require(all(tasks[t]['status'] == 'INVALIDATED' for t in closure))
        elif effect == 'E3':
            run = current['revalidation_runs'][-1]
            _require(run['txn_id'] == anchor['txn_id'] and run['status'] == 'RUNNING')
        elif effect == 'E5':
            _require(rec['attempts'] == old['attempts'] + 1 <= limits['max_attempts'])
            _require(rec['status'] == ('DISPATCH_RESERVED' if kind == 'DISPATCH_RESERVED' else 'RUNNING') and rec['checks'] == [])
            if mode == 'R_FAILED':
                _require(rolled and old['failed_attempts'][-1]['category'] == 'CHECK_FAILURE')
            counts[tid] += 1
            rolled = False
        elif effect in ('E4', 'E8r'):
            _require(rec['attempts'] == old['attempts'] and rec.get('failed_attempts') == old.get('failed_attempts'))
            _require(len(rec.get('failed_attempts', [])) > rec.get('failed_floor', 0))
            rolled = effect == 'E8r'
        elif effect == 'native_start':
            _require(rec['attempts'] == old['attempts'] and rec['status'] == 'RUNNING' and rec['checks'] == [])
        elif effect in ('capture', 'capture_verify'):
            _require(rec['attempts'] == old['attempts'])
            _require(all(tasks[t] == pred[t] for t in tasks if t != tid))
            expected_status = ('VERIFYING' if effect == 'capture_verify' else
                               'DISPATCH_RESERVED' if kind == 'DISPATCH_PROVEN_NOT_STARTED' else 'CANDIDATE_CAPTURED')
            _require(rec['status'] == expected_status)
        elif effect == 'status':
            _require(rec['status'] == 'VERIFYING' and rec['attempts'] == old['attempts'])
        elif effect == 'E8':
            failures = rec.get('failed_attempts', [])
            _require(rec['status'] == 'FAILED' and rec['attempts'] == old['attempts'])
            _require(len(failures) == len(old.get('failed_attempts', [])) + 1)
            f = failures[-1]
            _require(f.get('diff_ref') and f.get('diff_sha256') and f.get('category'))
            _require(f['checks'] == rec['checks'])
            if f['category'] in ('CHECK_FAILURE', 'REPAIR_STALLED', 'ENVIRONMENT_DIAGNOSIS'):
                _require(_checks(rec['checks']))
            else:
                _require(rec['checks'] == [])
        elif effect in ('E9', 'E9p', 'E12'):
            a, b = rec, old
            if effect == 'E9p':
                a, b = current['preflights'][-1], previous['preflights'][-1]
            elif effect == 'E12':
                a, b = rec['revalidations'][-1], old['revalidations'][-1]
            _require(a['checks'][:-1] == b['checks'] and len(a['checks']) == len(b['checks']) + 1 and _checks(a['checks']))
        elif effect == 'E10':
            _require(rec['status'] == 'COMMIT_INTENT' and rec.get('candidate_tree') and rec.get('start_commit') and _checks(rec['checks']))
        elif effect == 'E11':
            _require(rec['status'] == 'COMPLETED' and rec['repairs'][-1]['txn_id'] == anchor['txn_id'] and rec.get('restored_by') is None and 'superseded' in rec)
        elif effect == 'E6':
            _require(all(tasks[t] == pred[t] for t in tasks if t != tid))
            _require(tid not in roots and len(rec['revalidations']) == len(old.get('revalidations', [])) + 1 and rec['revalidations'][-1]['status'] == 'RUNNING')
        elif effect == 'E13':
            _require(all(tasks[t] == pred[t] for t in tasks if t != tid))
            a = rec['revalidations'][-1]
            _require(rec['status'] == 'COMPLETED' and a['status'] == 'PASSED' and rec['restored_by'] == a['attempt'] and _checks(a['checks']))
        elif effect == 'E7':
            _require(current['revalidation_runs'][-1]['status'] == 'RUNNING')
            for task_id in tasks:
                a, b = copy.deepcopy(tasks[task_id]), copy.deepcopy(pred[task_id])
                for attempt in b.get('revalidations', []):
                    if attempt['status'] == 'RUNNING':
                        attempt['status'] = 'INTERRUPTED'
                _require(a == b)
        elif effect == 'cursor':
            _require(current['revalidation_runs'][-1]['cursor']['task_id'] == tid)
        elif effect == 'open':
            _require(all(tasks[t]['status'] == 'COMPLETED' for t in closure))
            _require(current['preflights'][-1]['status'] == 'RUNNING' and current['preflights'][-1]['checks'] == [])
        elif effect == 'E14':
            _require(current['revalidation_runs'][-1]['status'] == 'COMPLETE')
        elif effect in ('E15', 'E16'):
            p = current['preflights'][-1]
            if effect == 'E15':
                _require(p['status'] == 'PASSED' and p['head'] == current['expected_head'] and _checks(p['checks']) and all(c['exit_code'] == 0 and not c.get('empty_suite') for c in p['checks']))
                a, b = copy.deepcopy(current), copy.deepcopy(previous)
                a['preflights'][-1]['status'] = b['preflights'][-1]['status']
                _require(a == b)
            else:
                _require(p['status'] in ('FAILED', 'MUTATED'))
                _require(p['status'] == 'MUTATED' or any(c['exit_code'] != 0 or c.get('empty_suite') for c in p['checks']))
        if mode in ('FIN_RUN', 'F_OPEN'):
            _require(tasks == pred)
        if effect in ('E11', 'E13'):
            index += 1
            if index == len(closure):
                target = 'DONE'
        mode, previous, last = target, current, event
    for tid in closure:
        _require(previous['tasks'][tid]['attempts'] - initial['tasks'][tid]['attempts'] == counts[tid])
    return dict(state=mode, closure_index=index, rolled=rolled, snapshot=previous,
                last_event=last, starts=counts, txn_id=anchor['txn_id'])


def verify_lineage(path, anchor, closure, roots, limits):
    try:
        state, events = TaskJournal.replay_range(Path(path), anchor['sequence'], anchor['sha256'])
    except (TaskError, OSError):
        raise CorrectiveError('journal_lineage_invalid') from None
    return walk_lineage(dict(anchor, state=state), events, closure, roots, limits)


def derive_substate(automaton_state, record, git_obs):
    walk = automaton_state if isinstance(automaton_state, dict) else dict(state=automaton_state)
    mode = walk['state']
    mapping = dict(G='IDLE', T_PRE='IDLE', T='IDLE', DONE='IDLE', FIN_RUN='IDLE', R_READY='RESERVED', R_RESERVED='RESERVED', R_RUN='RUNNING', R_VER='VERIFYING', R_INTENT='COMMIT_INTENT', D_RUN='DEPENDENT_REVALIDATING', D_CHECK='DEPENDENT_REVALIDATING', D_FAIL='DEPENDENT_FAILED', F_OPEN='PREFLIGHT_OPEN', F_PASSED='PREFLIGHT_PASSED_UNACKED', F_BLOCKED='PREFLIGHT_FAILED')
    if mode == 'R_FAILED':
        category = record['failed_attempts'][-1]['category']
        sub = 'FAILED_UNROLLED' if not walk.get('rolled') else ('FAILED_ROLLED' if category == 'CHECK_FAILURE' else 'FAILED_UNVERIFIED')
    else:
        sub = mapping.get(mode)
    def conflict(ok):
        if not ok:
            raise CorrectiveError('txn_record_conflict')
    conflict(sub is not None)
    expected = git_obs['expected_head']
    dirty_allowed = mode in ('R_RUN', 'R_VER', 'R_FAILED', 'R_RESERVED') and sub != 'FAILED_UNVERIFIED' and not walk.get('rolled')
    if mode == 'R_INTENT':
        conflict(record.get('status') == 'COMMIT_INTENT' and record.get('start_commit') == expected)
        before = git_obs['head'] == expected and git_obs.get('index_tree') == record.get('candidate_tree') and not git_obs.get('unstaged') and set(git_obs.get('changed_files', [])) == set(record.get('changed_files', []))
        after = git_obs.get('parents') == [expected] and git_obs.get('tree') == record.get('candidate_tree') and git_obs.get('message') == record.get('commit_message') and git_obs.get('clean')
        conflict(before or after)
    else:
        conflict(git_obs['head'] == expected)
        mutated = (mode == 'F_BLOCKED' and record.get('preflights', [{}])[-1].get('status') == 'MUTATED'
                   or mode == 'F_OPEN' and record.get('preflights', [{}])[-1].get('mutation_observed') is True)
        conflict(git_obs.get('clean') or dirty_allowed or mutated)
        if dirty_allowed:
            conflict(set(git_obs.get('changed_files', [])) <= set(git_obs.get('target_files', [])))
    if mode == 'R_RESERVED':
        conflict(record.get('status') == 'DISPATCH_RESERVED' and record.get('checks') == [])
    if mode in ('R_RUN', 'R_VER'):
        conflict(record.get('status') in (('RUNNING', 'DISPATCH_RESERVED', 'CANDIDATE_CAPTURED') if mode == 'R_RUN' else ('VERIFYING',)))
        conflict(mode != 'R_RUN' or record.get('checks') == [])
    if mode in ('F_OPEN', 'F_PASSED', 'F_BLOCKED'):
        p = record['preflights'][-1]
        conflict(all(t['status'] == 'COMPLETED' for t in record['tasks'].values()))
        conflict(p['status'] in {'F_OPEN': ('RUNNING',), 'F_PASSED': ('PASSED',), 'F_BLOCKED': ('FAILED', 'MUTATED')}[mode])
        if mode == 'F_PASSED':
            conflict(p['head'] == expected and _checks(p['checks']) and all(c['exit_code'] == 0 and not c.get('empty_suite') for c in p['checks']))
    if mode == 'T' or mode == 'DONE':
        last = walk.get('last_event') or {}
        if last.get('event') == 'TASK_COMPLETED':
            conflict(record.get('status') == 'COMPLETED')
            sub = 'COMMITTED_UNACKED'
    return sub


def plan_next(txn_state, walk_result, observed):
    """Return an action only; never perform IO or dispatch a provider."""
    status = txn_state.get('status')
    if status in ('COMPLETED', 'APPROVED'):
        return 'noop'
    if status == 'FAILED':
        return 'refuse'
    sub = observed.get('substate')
    last = walk_result.get('last_event') or {}
    if last.get('event') == 'REVALIDATION_TASK_PASSED':
        return 'ack_dependent'
    if last.get('event') == 'TASK_COMPLETED':
        return 'ack_repair'
    actions = dict(RUNNING='stop_uncertain', VERIFYING='stop_uncertain', COMMIT_INTENT='complete_intent', COMMITTED_UNACKED='ack_repair', FAILED_UNROLLED='rollback_failed_attempt', FAILED_UNVERIFIED='fail_repair_blocked', DEPENDENT_REVALIDATING='verify_dependent', DEPENDENT_FAILED='refuse', PREFLIGHT_OPEN='final_preflight', PREFLIGHT_PASSED_UNACKED='ack_final_preflight', PREFLIGHT_FAILED='fail_final_preflight')
    if sub in actions:
        return actions[sub]
    if observed.get('unacked') == 'dependent':
        return 'ack_dependent'
    if observed.get('unacked') == 'repair':
        return 'ack_repair'
    if sub == 'FAILED_ROLLED' and observed['attempts'] >= observed['max_attempts']:
        return 'refuse'
    mode = walk_result['state']
    if mode == 'F_PASSED':
        return 'publish_step_' + str(observed.get('publish_step', 1)) if not observed.get('published') else 'complete'
    if mode == 'T':
        return 'reserve_repair' if observed.get('is_root') else 'verify_dependent'
    return dict(G='apply_invalidation', T_PRE='ack_invalidation' if status == 'INVALIDATING' else 'reserve_repair', R_READY='reserve_repair', R_RESERVED='reserve_repair', R_FAILED='reserve_repair', DONE='finalize_run', FIN_RUN='final_preflight').get(mode, 'refuse')


def effective_limits(env, txns, *, policy=None, combined_cap=None):
    # A bounded I2 run has explicit cumulative authority. Legacy tier defaults
    # apply only without that policy; environment settings may still tighten it.
    default_attempts = policy['development_attempts'] if policy is not None else 3
    attempts = int(env.get('SSDLC_MAX_DEV_RETRIES', str(default_attempts)))
    if policy is not None:
        attempts = min(attempts, default_attempts)
    tier = (env.get('risk_tier') or 'M').upper()
    raw = env.get('SSDLC_BUDGET_CAP_USD', '').strip()
    default_cap = (policy['metered_usd'] + policy['cli_estimate_usd'] if policy is not None
                   else {'S': 3., 'M': 6., 'L': 20.}.get(tier, 6.))
    cap = float(raw) if raw else default_cap
    caps = [cap] if cap > 0 else []
    if policy is not None:
        caps.append(default_cap)
    if combined_cap is not None:
        caps.append(combined_cap)
    rounds = attempts
    for txn in txns:
        limits = txn['limits']
        attempts = min(attempts, limits['max_attempts'])
        rounds = min(rounds, limits['gate_round_ceiling'])
        if limits['budget_cap_usd'] is not None:
            caps.append(limits['budget_cap_usd'])
    if any(not math.isfinite(c) or c <= 0 for c in caps):
        raise CorrectiveError('limits_exceeded')
    return dict(max_attempts=attempts, gate_round_ceiling=rounds, budget_cap_usd=min(caps) if caps else None)


def replace_atomic(path, data):
    """Durably replace a projection; caller validates repository confinement."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
