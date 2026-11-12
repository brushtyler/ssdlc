"""Exact operator recovery of an exhausted owner-scope task in an active I2 run.

Only the next development attempt and the literal request's necessary scope
capacity can increase. Money, clocks, invocation caps and funded roles cannot.
The old recording authority and every request/consultation remain immutable.
"""
import copy

from ssdlc_tasks import TaskError, digest, validate_document


CONTRACT = 'operator-owner-recovery-v1'


def required_profile(source, paths, state):
    """Proposed data, not authorization: exactly one more exhausted attempt."""
    old = source['envelope']['policy']
    if source['journal']['tasks'][source['task_id']]['attempts'] != old['development_attempts']:
        raise TaskError('Operator recovery requires the exhausted development attempt limit')
    if not paths or len(set(paths)) != len(paths):
        raise TaskError('Operator recovery requires literal new scope paths')
    result = copy.deepcopy(old)
    result['development_attempts'] += 1
    grant = result.get('feedback_delegation')
    if not grant:
        raise TaskError('Operator recovery requires an existing feedback delegation')
    slots = list(state.get('amendments', [])) + list(state.get('owner_amendments', []))
    grant['allowed_files'] += sorted(set(paths) - set(grant['allowed_files']))
    grant['max_amendments'] = max(grant['max_amendments'], len(slots) + 1)
    grant['max_added_files'] = max(grant['max_added_files'], sum(len(r['paths']) for r in slots) + len(paths))
    validate_document('recovery-limits', result)
    return result


def subject(source, plan, reason, *, profile, feedback_request_id,
            reconcile_feedback_evidence=False, operator_owner_limit=None):
    from ssdlc_feedback import FeedbackStore
    from ssdlc_owner_revalidation import feedback_binding
    from ssdlc_scope_amendments import subject as scope_subject
    # Reuse the existing complete obligation/DAG/protected-path validation.
    value = scope_subject(source, plan, reason)
    if 'owner_revalidation' not in value or not feedback_request_id:
        raise TaskError('Operator recovery requires an exact owner feedback request')
    paths = value['scope_diff'][source['task_id']]['added']
    state = FeedbackStore(source['root'].parent.parent).inspect()
    validate_document('recovery-limits', profile)
    expected = required_profile(source, paths, state)
    if profile != expected:
        raise TaskError('Operator recovery profile differs from the exact next attempt and necessary scope capacity')
    value['owner_feedback'] = feedback_binding(source['root'].parent.parent, source, paths,
        feedback_request_id, reconcile_evidence=reconcile_feedback_evidence,
        operator_owner_limit=operator_owner_limit, grant_override=profile['feedback_delegation'])
    old = source['envelope']['policy']
    value['operator_recovery'] = dict(capability=CONTRACT,
        old_profile_sha256=digest(old), new_profile_sha256=digest(profile),
        task_id=source['task_id'], consumed_attempts=source['journal']['tasks'][source['task_id']]['attempts'],
        profile_delta={k: dict(old=copy.deepcopy(old[k]), new=copy.deepcopy(profile[k]))
                       for k in old if old[k] != profile[k]})
    return value


def validate_binding(proposal, recipe):
    """Prevent a profile-changing recipe from borrowing ordinary scope consent."""
    value = recipe['subject'].get('operator_recovery')
    if value is None:
        if proposal.get('authorization_contract') == CONTRACT or 'operator_recovery_sha256' in proposal:
            raise TaskError('Operator recovery capability is missing from its sealed subject')
        return
    if (proposal.get('authorization_contract') != CONTRACT
            or proposal.get('operator_recovery_sha256') != digest(value)
            or proposal.get('profile_sha256') != value['new_profile_sha256']
            or digest(recipe['envelope']['policy']) != value['new_profile_sha256']
            or 'owner_feedback_sha256' not in proposal):
        raise TaskError('Operator recovery profile/capability differs from its exact proposal')
