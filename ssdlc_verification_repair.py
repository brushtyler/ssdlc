"""Exact operator admission after a captured verifier timeout.

This grants one repair attempt, literal test scope and longer timed-out quality
checks. A separate capability may extend the deadline. Test commands,
acceptance criteria, money and consumed history stay unchanged.
"""
import copy

from ssdlc_tasks import TaskError, digest, validate_document


CONTRACT = 'operator-verification-repair-v1'
CLOCK_CONTRACT = 'operator-verification-repair-deadline-v1'
CONTRACTS = {CONTRACT, CLOCK_CONTRACT}


def require_timeout(source):
    row = source['journal']['tasks'][source['task_id']]
    failure = row['failed_attempts'][-1]
    checks = failure.get('checks', [])
    if (failure['category'] != 'ENVIRONMENT_DIAGNOSIS'
            or not any(c['exit_code'] == 124 for c in checks)
            or any(c['exit_code'] not in (0, 124) for c in checks)
            or not row.get('capture_sha256')
            or failure.get('capture_sha256') != row['capture_sha256']
            or row.get('producer', {}).get('quiescent') is not True):
        raise TaskError('Verification repair requires a quiescent captured verifier timeout')


def subject(source, plan, quality, profile, reason, *, deadline_at=None):
    from ssdlc_scope_amendments import subject as scope_subject
    require_timeout(source)
    validate_document('quality-contract', quality)
    validate_document('recovery-limits', profile)
    old_profile = source['envelope']['policy']
    row = source['journal']['tasks'][source['task_id']]
    expected = copy.deepcopy(old_profile)
    expected['development_attempts'] += 1
    extension = None
    if deadline_at is not None:
        from ssdlc_deadline_extensions import specification
        extension = specification(source, profile, deadline_at)
        expected['wall_seconds'] = profile['wall_seconds']
    if row['attempts'] != old_profile['development_attempts'] or profile != expected:
        raise TaskError('Verification repair permits exactly one exhausted development attempt; other limits stay frozen')
    value = scope_subject(source, plan, reason)
    paths = value['scope_diff'][source['task_id']]['added']
    if len(paths) > 4 or any(not p.startswith('tests/') or not p.endswith('.py') for p in paths):
        raise TaskError('Verification repair scope additions must be literal Python test files')
    old = source['quality']
    normalized = copy.deepcopy(quality)
    timed_out = {c['check_id'] for c in row['failed_attempts'][-1]['checks'] if c['exit_code'] == 124}
    changes = {}
    if len(quality['checks']) != len(old['checks']):
        raise TaskError('Verification repair cannot add or remove quality checks')
    for before, after, reset in zip(old['checks'], quality['checks'], normalized['checks']):
        if after['timeout_sec'] != before['timeout_sec']:
            if (before['check_id'] not in timed_out
                    or not before['timeout_sec'] < after['timeout_sec'] <= profile['invocation_timeout_seconds']):
                raise TaskError('Only observed timed-out checks may receive a longer bounded timeout')
            changes[before['check_id']] = dict(old=before['timeout_sec'], new=after['timeout_sec'])
            reset['timeout_sec'] = before['timeout_sec']
    if normalized != old or not changes:
        raise TaskError('Verification repair may only increase observed quality-check timeouts')
    value['verification_repair'] = dict(capability=CONTRACT,
        failure_sha256=digest(row['failed_attempts'][-1]),
        capture_sha256=row['capture_sha256'],
        old_quality_sha256=digest(old), new_quality_sha256=digest(quality),
        old_profile_sha256=digest(old_profile), new_profile_sha256=digest(profile),
        timeout_changes=changes, consumed_attempts=row['attempts'])
    if extension is not None:
        value['verification_repair'].update(capability=CLOCK_CONTRACT, deadline_extension=extension)
    from ssdlc_feedback import FeedbackStore
    store = FeedbackStore(source['root'].parent.parent)
    if store.root.exists():
        authority, state = store._read()
        from ssdlc_feedback_delegation import admitted
        if state is None or authority['status'] != 'ACTIVE' or not admitted(state, source):
            raise TaskError('Verification repair feedback source is outside admitted lineage')
        value['verification_repair']['feedback_binding'] = dict(
            authority_sha256=digest(authority), state_sha256=digest(state))
    return value


def validate_binding(proposal, recipe):
    value = recipe['subject'].get('verification_repair')
    if value is None:
        if proposal.get('authorization_contract') in CONTRACTS:
            raise TaskError('Verification repair capability is missing')
        return
    capability = value.get('capability')
    if (capability not in CONTRACTS or proposal.get('authorization_contract') != capability
            or proposal.get('verification_repair_sha256') != digest(value)
            or proposal.get('quality_contract_sha256') != value['new_quality_sha256']
            or proposal.get('profile_sha256') != value['new_profile_sha256']
            or digest(recipe['envelope']['policy']) != value['new_profile_sha256']):
        raise TaskError('Verification repair differs from its exact quality/profile capability')
    extension = value.get('deadline_extension')
    if ((extension is not None) != (capability == CLOCK_CONTRACT)
            or extension is not None and proposal.get('deadline_extension_sha256') != digest(extension)
            or extension is None and 'deadline_extension_sha256' in proposal):
        raise TaskError('Deadline extension requires its separate exact capability')


def _receipt(proposal, recipe, projection):
    value = recipe['subject']['verification_repair']
    return dict(request_id='operator-verification-repair:' + proposal['transition_id'],
        task_id=recipe['subject']['task_id'], transition_id=proposal['transition_id'],
        proposal_sha256=digest(proposal), paths=recipe['subject']['scope_diff'][recipe['subject']['task_id']]['added'],
        verification_repair=value), dict(limits_sha256=digest(recipe['envelope']),
            profile_sha256=digest(recipe['envelope']['policy']), projection_sha256=digest(projection))


def finalize(ticket, proposal, recipe, projection):
    """Count manual scope and admit lineage without changing recording grants."""
    value = recipe['subject']['verification_repair']
    if 'feedback_binding' not in value:
        return
    from ssdlc_feedback import FeedbackStore
    from ssdlc_tasks import ticket_lock
    validate_binding(proposal, recipe)
    store = FeedbackStore(ticket)
    receipt, admission = _receipt(proposal, recipe, projection)
    with ticket_lock(store.root / 'feedback.lock'):
        authority, state = store._read()
        prior = [r for r in state.get('owner_amendments', []) if r['transition_id'] == proposal['transition_id']]
        if prior:
            if prior != [receipt] or state.get('admitted_generations', {}).get(proposal['transition_id']) != admission:
                raise TaskError('Verification repair reconciliation changed')
            return
        if value['feedback_binding'] != dict(authority_sha256=digest(authority), state_sha256=digest(state)):
            raise TaskError('Feedback changed before verification repair reconciliation')
        # This exact manual operation may exceed recording caps. It consumes a
        # slot and its paths; automatic grants stay frozen and cannot reuse it.
        state.setdefault('owner_amendments', []).append(receipt)
        state.setdefault('admitted_generations', {})[proposal['transition_id']] = admission
        store._append('OPERATOR_VERIFICATION_REPAIR_APPLIED', state)


def ensure_reconciled(ticket, projection, proposal):
    from ssdlc_transitions import _regular
    import json
    recipe = json.loads(_regular(ticket / 'execution' / proposal['transition_id'], 'recipe.json').read_text())
    validate_binding(proposal, recipe)
    if 'feedback_binding' not in recipe['subject']['verification_repair']:
        return
    from ssdlc_feedback import FeedbackStore
    receipt, admission = _receipt(proposal, recipe, projection)
    state = FeedbackStore(ticket).inspect()
    if ([r for r in state.get('owner_amendments', []) if r['transition_id'] == proposal['transition_id']] != [receipt]
            or state.get('admitted_generations', {}).get(proposal['transition_id']) != admission):
        raise TaskError('Verification repair reconciliation pending; dispatch is fenced')
