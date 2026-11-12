"""Explicit frozen scope delegation; native staging, no inference or GH writes.

Recording approval is never an execution grant. This contract additionally
requires an opt-in native profile, captured funded decisions and live exclusive
ownership of the canonical ticket and scheduler. It preserves manual recovery.
"""
import contextlib
import copy
import hashlib
import json
from pathlib import Path

from ssdlc_controller import assert_controller, execution_contract
from ssdlc_feedback import (FeedbackError, FeedbackStore, class_disposition,
                            independence, literal_path, matches_source, participants)
from ssdlc_feedback_bindings import technical_subject, validate_accounting_advance
from ssdlc_locks import current_lock, transition_lock
from ssdlc_preparations import docker_quiescent
from ssdlc_tasks import digest, validate_document, ticket_lock
from ssdlc_transitions import _regular


CONTRACT = 'feedback-scope-delegation-v1'


def validate_grant(grant):
    fields = {'schema_version', 'capability', 'allowed_files', 'max_amendments',
              'max_amendments_per_task', 'max_added_files', 'require_strong_independence'}
    if isinstance(grant, dict) and 'owner_revalidation' in grant:
        fields.add('owner_revalidation')
        if grant['owner_revalidation'] is not True:
            raise FeedbackError('Owner revalidation delegation must be explicitly enabled')
    if (not isinstance(grant, dict) or set(grant) != fields
            or type(grant['schema_version']) is not int or grant['schema_version'] != 1
            or grant['capability'] != CONTRACT or grant['require_strong_independence'] is not True
            or not isinstance(grant['allowed_files'], list) or not grant['allowed_files']
            or len(grant['allowed_files']) > 100
            or len(set(grant['allowed_files'])) != len(grant['allowed_files'])):
        raise FeedbackError('Explicit frozen scope delegation grant required')
    for path in grant['allowed_files']:
        literal_path(path)
    for key in ('max_amendments', 'max_amendments_per_task', 'max_added_files'):
        if type(grant[key]) is not int or not 1 <= grant[key] <= 1000:
            raise FeedbackError('Invalid delegated scope quota')


def observe_environment():
    from orchestrator import EngineRunner, SSDLCOrchestrator
    if not EngineRunner._should_spawn_role_container():
        raise FeedbackError('Delegation requires contained role execution')
    return SSDLCOrchestrator._verification_environment(None, ['python3'])


class FencedSession:
    """An ephemeral owner object, never a serializable dispatch capability."""
    def __init__(self, ticket, root, controller, snapshot, runtime, leases):
        self.ticket, self.root, self.controller = ticket, root, controller
        self.snapshot, self.runtime, self.leases = snapshot, runtime, leases

    def check(self):
        paths = [self.ticket / 'ticket.lock', self.root / '03-dev/tasks/scheduler.lock']
        for path, lease in zip(paths, self.leases):
            if current_lock(path) is not lease:
                raise FeedbackError('Delegated transition lost native lock ownership')
        assert_controller(self.controller, self.snapshot)
        if (self.runtime['controller_sha256'] != digest(execution_contract(self.snapshot))
                or self.runtime['environment'].get('mode') != 'role_container'
                or not self.runtime['environment'].get('image_id')
                or observe_environment() != self.runtime['environment']):
            raise FeedbackError('Delegated controller or environment changed')
        if not docker_quiescent(dict(ticket_root=str(self.ticket), runtime='execution-runtimes',
                                     preparation='execution', source_root=str(self.root))):
            raise FeedbackError('Delegated writers are not proven quiescent')
        return dict(kind='native-exclusive-quiescence-v1',
                    locks=[dict(device=l.device, inode=l.inode, pid=l.pid, thread=l.thread) for l in self.leases],
                    controller_sha256=self.runtime['controller_sha256'],
                    environment_sha256=digest(self.runtime['environment']), unresolved_invocations=0)


@contextlib.contextmanager
def fenced(ticket, controller, snapshot, runtime_identity, *, held_locks=(), preparation=None):
    """Acquire/borrow real locks, observe quiescence, then inspect stopped source."""
    from ssdlc_scope_amendments import inspect
    ticket, controller = Path(ticket).absolute(), Path(controller).resolve()
    with transition_lock(ticket / 'ticket.lock', held_locks) as canonical:
        if preparation is None:
            source = inspect(ticket, allow_host_hints=True)
            root = source['root']
        else:
            preparation = Path(preparation).absolute()
            proposal = json.loads(_regular(preparation, 'proposal.json').read_text())
            recipe = json.loads(_regular(preparation, 'recipe.json').read_text())
            from ssdlc_feedback_return import CONTRACT as RETURN_CONTRACT, validate_authorization as validate_return
            if (preparation != ticket / 'execution' / ('prepared-' + proposal['transition_id'])
                    or proposal['authorization_contract'] not in {CONTRACT, RETURN_CONTRACT}):
                raise FeedbackError('Delegated replay requires its native preparation')
            if proposal['authorization_contract'] == RETURN_CONTRACT:
                from ssdlc_feedback_return import authorization as return_authorization
                validate_return(return_authorization(proposal, recipe['repair_return']), proposal)
            else:
                validate_authorization(authorization(proposal, recipe['review']), proposal)
            root = Path(recipe['source_root'])
            if root.parent != ticket / 'execution-runtimes' or root.is_symlink():
                raise FeedbackError('Delegated source is outside native generations')
            source = None
        with transition_lock(root / '03-dev/tasks/scheduler.lock', held_locks) as scheduler:
            owner = FencedSession(ticket, root, controller, snapshot,
                                  copy.deepcopy(runtime_identity), (canonical, scheduler))
            owner.check()
            expected = (source['journal']['candidate_runtime'] if source else recipe['runtime_identity'])
            if expected != runtime_identity:
                raise FeedbackError('Delegation cannot migrate controller or environment')
            yield owner


def build_proof(session, source, plan, reason, feedback, request_id):
    fence = session.check()
    authority, state = feedback._read()
    feedback._active()  # Preparing a new transition must precede recording expiry.
    row = state['requests'][request_id]
    if (feedback.ticket != session.ticket or not matches_source(state, row, source)
            or feedback.disposition(request_id) != 'NATIVE_AMENDMENT_REQUIRED'
            or row['proposal']['reason_code'] != reason):
        raise FeedbackError('Delegation requires the exact eligible recorded decision')
    grant = source['envelope']['policy'].get('feedback_delegation')
    validate_grant(grant)
    if authority['policy']['require_strong_independence'] is not True:
        raise FeedbackError('Delegation requires strong recording independence')
    calls = [c for c in state['role_calls'] if 'native' in c
             and c['native']['invocation_id'] not in row['accounting_anchor']['counters']['invocations']]
    advance = validate_accounting_advance(row['accounting_anchor'], source, calls)
    proof = dict(schema_version=1, grant=grant, envelope=source['envelope'],
                 recording_authority=authority, state=state, request_id=request_id,
                 source_files=source['files'], source_counters=source['counters'],
                 technical_subject=technical_subject(source, plan, reason),
                 accounting_advance=advance, fence=fence)
    validate_proof(proof)
    if proof['technical_subject']['scope_diff'] != {
            source['task_id']: {'added': sorted(row['proposal']['requested_files']), 'removed': []}}:
        raise FeedbackError('Delegated plan differs from funded proposal')
    return proof


def require_capacity(state, row, profile):
    """Do not spend a consultation on an exhausted eligible scope delegation."""
    grant = profile.get('feedback_delegation')
    if grant is None or row['request']['cause'] != 'MISSING_TARGET':
        return
    validate_grant(grant)
    paths = row['request']['requested_files']
    kinds = {'NEW_FILE', 'UNOWNED_SOURCE'}
    if grant.get('owner_revalidation'):
        kinds |= {'COMPLETED_SOURCE', 'COMPLETED_TEST'}
    if any(row['path_classes'][path]['kind'] not in kinds for path in paths):
        return  # Explicit consultations may still diagnose an operator-only case.
    if not set(paths) <= set(grant['allowed_files']):
        raise FeedbackError('Requested scope is outside frozen delegation; operator required')
    prior = list(state['amendments']) + list(state.get('owner_amendments', []))
    own = next((a for a in prior if a['request_id'] == row['request_id']), None)
    count, additions = (0, 0) if own else (1, len(paths))
    if (len(prior) + count > grant['max_amendments']
            or sum(a['task_id'] == row['binding']['task_id'] for a in prior) + count > grant['max_amendments_per_task']
            or sum(len(a['paths']) for a in prior) + additions > grant['max_added_files']):
        raise FeedbackError('Delegated cumulative amendment quota exhausted before consultation')


def validate_proof(proof):
    """Replay the sealed semantic proof, without claiming a historical lock live."""
    validate_document('recovery-limits', proof['envelope']['policy'])
    grant = proof['grant']; validate_grant(grant)
    if grant != proof['envelope']['policy'].get('feedback_delegation'):
        raise FeedbackError('Delegation differs from frozen native grant')
    state, identity = proof['state'], proof['request_id']
    row = state['requests'][identity]
    authority = proof['recording_authority']
    from ssdlc_feedback import validate_policy, validate_recording_authority
    validate_policy(authority['policy'])
    validate_recording_authority(authority['policy'], authority['authorization'])
    if (authority['status'] != 'ACTIVE' or state['policy_sha256'] != digest(authority['policy'])
            or authority['policy']['require_strong_independence'] is not True):
        raise FeedbackError('Delegation recording authority changed')
    if (row['status'] != 'REVIEWED' or row['request']['cause'] != 'MISSING_TARGET'
            or row.get('source_reconciliation_required', True)
            or class_disposition(row, owner_revalidation=grant.get('owner_revalidation', False))
            or row['proposal']['route'] != 'AMENDMENT' or row['review']['verdict'] != 'APPROVED'
            or row['review']['coverage_preserved'] is not True
            or row['review']['proposal_sha256'] != row['proposal_sha256']):
        raise FeedbackError('Delegated review does not approve an eligible amendment')
    proposal_sha = digest(dict(binding=row['binding'], proposal=row['proposal'], planner=row['planner'],
                               path_classes_sha256=row['path_classes_sha256']))
    quality = independence(*participants(state, row))
    if proposal_sha != row['proposal_sha256'] or not quality['distinct_roles'] or quality['reduced_independence']:
        raise FeedbackError('Delegation requires unchanged independent decisions')
    paths = row['proposal']['requested_files']
    if not paths or not set(paths) <= set(grant['allowed_files']):
        raise FeedbackError('Delegated paths exceed the frozen grant')
    amendments = list(state['amendments']) + list(state.get('owner_amendments', []))
    slots = [s for s in amendments if s['request_id'] == identity]
    if (len(slots) != 1 or slots[0]['status'] != 'RESERVED'
            or slots[0]['proposal_sha256'] != proposal_sha or slots[0]['paths'] != paths
            or len(amendments) > grant['max_amendments']
            or sum(s['task_id'] == row['binding']['task_id'] for s in amendments) > grant['max_amendments_per_task']
            or sum(len(s['paths']) for s in amendments) > grant['max_added_files']):
        raise FeedbackError('Delegated cumulative amendment quota exhausted or changed')
    current = proof['technical_subject']
    if 'owner_revalidation' in current:
        from ssdlc_owner_revalidation import validate_review
        validate_review(current['owner_revalidation'], row['review'].get('owner_coverage'))
    anchor = row['accounting_anchor']
    from ssdlc_feedback_bindings import ACCOUNTING_REFS
    if (current['accounting_authority_sha256'] != digest(proof['envelope'])
            or current['source_files_sha256'] != anchor['technical_files_sha256']
            or current['source_files_sha256'] != digest({k: v for k, v in proof['source_files'].items()
                                                        if k not in ACCOUNTING_REFS})
            or current['checkpoint_worktree_sha256'] != anchor['worktree_sha256']
            or current['old_plan_sha256'] != row['binding']['plan_sha256']
            or current['parent_selector'] != row['binding']['parent_selector']
            or current['task_id'] != row['binding']['task_id']
            or current['reason'] != row['proposal']['reason_code']
            or current['scope_diff'] != {row['binding']['task_id']: {'added': sorted(paths), 'removed': []}}):
        raise FeedbackError('Delegated technical subject differs from recorded proposal')
    advance = proof['accounting_advance']
    before, after = anchor['counters'], proof['source_counters']
    if (advance['current_counters_sha256'] != digest(after)
            or advance['envelope_sha256'] != digest(proof['envelope'])
            or advance['technical_files_sha256'] != anchor['technical_files_sha256']
            or {k: v for k, v in before.items() if k != 'invocations'}
                != {k: v for k, v in after.items() if k != 'invocations'}
            or any(after['invocations'].get(k) != v for k, v in before['invocations'].items())
            or sorted(set(after['invocations']) - set(before['invocations'])) != advance['charged_invocations']):
        raise FeedbackError('Delegated accounting advance changed historical facts')
    for call in state['role_calls']:
        native = call.get('native', {})
        invocation = native.get('invocation_id')
        if invocation not in advance['charged_invocations']:
            continue
        response = call.get('receipt', {}).get('response', {})
        accounted = after['invocations'][invocation]
        if (call['status'] != 'RETURNED' or digest(response) != call['receipt']['response_sha256']
                or native['limits_sha256'] != digest(proof['envelope'])
                or accounted['status'] != 'returned' or accounted['usage'] != response.get('usage')
                or accounted['role'] != 'feedback-' + call['role'] or accounted['phase'] != 'feedback'
                or accounted['engine'] != call['actor']['engine']):
            raise FeedbackError('Delegated consultation accounting differs from captured response')
    if set(advance['charged_invocations']) != {
            c['native']['invocation_id'] for c in state['role_calls'] if 'native' in c
            and c['native']['invocation_id'] not in before['invocations']}:
        raise FeedbackError('Delegated accounting contains an unrelated invocation')
    funded = []
    for role in ('planner', 'reviewer'):
        calls = [c for c in state['role_calls'] if c['request_id'] == identity and c['role'] == role]
        if len(calls) != 1:
            raise FeedbackError('Delegation requires exactly one funded decision per role')
        call = calls[0]
        response = call.get('receipt', {}).get('response')
        if (call['status'] != 'RETURNED' or not response or digest(response) != call['receipt']['response_sha256']
                or response['response'] != row['proposal' if role == 'planner' else 'review']
                or call['actor'] != row[role] or call['native']['limits_sha256'] != digest(proof['envelope'])
                or call['native']['invocation_id'] not in advance['charged_invocations']):
            raise FeedbackError('Delegated decision is not its captured funded response')
        funded.append(call['native']['invocation_id'])
    if len(set(funded)) != 2 or advance['anchor_sha256'] != digest(anchor):
        raise FeedbackError('Delegated accounting proof changed')
    fence = proof['fence']
    if (fence.get('kind') != 'native-exclusive-quiescence-v1' or len(fence.get('locks', [])) != 2
            or fence['unresolved_invocations'] != 0
            or fence['controller_sha256'] != digest(execution_contract(proof['envelope']['binding']['controller']))):
        raise FeedbackError('Delegated technical fencing receipt changed')


def authorization(proposal, proof):
    return dict(schema_version=1, contract=CONTRACT, transition_id=proposal['transition_id'],
                proposal_sha256=digest(proposal), proof=proof)


def validate_authorization(value, proposal):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'contract', 'transition_id', 'proposal_sha256', 'proof'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['contract'] != CONTRACT or value['transition_id'] != proposal['transition_id']
            or value['proposal_sha256'] != digest(proposal) or digest(value['proof']) != proposal['delegation_sha256']):
        raise FeedbackError('Exact delegated transition authorization required')
    proof = value['proof']; validate_proof(proof)
    # Source bytes are bound to the actual native envelope, never a loose grant.
    raw = (json.dumps(proof['envelope'], indent=2) + '\n').encode()
    if (hashlib.sha256(raw).hexdigest() != proposal['source_files']['limits/policy.json']
            or proof['source_files'] != proposal['source_files']
            or proposal['scope_diff'] != proof['technical_subject']['scope_diff']
            or proposal['parent_selector'] != proof['technical_subject']['parent_selector']
            or proposal['old_controller_sha256'] != proposal['new_controller_sha256']
            or proposal['new_controller_sha256'] != proof['fence']['controller_sha256']):
        raise FeedbackError('Delegated transition differs from frozen technical authority')


def prepare(session, repo, plan, request_id, *, fault=lambda point: None):
    from ssdlc_scope_amendments import prepare as native_prepare
    return native_prepare(session.ticket, repo, plan, None, None,
                          runtime_identity=session.runtime, controller_snapshot=session.snapshot,
                          guard=session.check, held_locks=session.leases, fault=fault,
                          delegation=(session, request_id))


def activate(session, preparation, *, fault=lambda point: None):
    from ssdlc_scope_amendments import activate as native_activate
    proposal = json.loads(_regular(Path(preparation), 'proposal.json').read_text())
    proof = json.loads(_regular(Path(preparation), 'recipe.json').read_text())['review']
    return native_activate(session.ticket, preparation, authorization(proposal, proof),
                           guard=session.check, held_locks=session.leases, fault=fault,
                           delegation=session)


def finalize(session, proposal, recipe, selected, *, fault=lambda point: None):
    """A published selector remains fenced until this durable quota receipt."""
    session.check()
    preparation = session.ticket / 'execution' / ('prepared-' + proposal['transition_id'])
    from ssdlc_tasks import file_hash
    for ref, sha in proposal['artifacts'].items():
        if file_hash(_regular(preparation, ref)) != sha:
            raise FeedbackError('Delegated preparation changed before quota reconciliation')
    proof = recipe['review']
    validate_authorization(authorization(proposal, proof), proposal)
    store = FeedbackStore(session.ticket)
    with ticket_lock(store.root / 'feedback.lock'):
        state = store.inspect()
        identity, transition = proof['request_id'], proposal['transition_id']
        row = state['requests'][identity]
        slot = next(s for s in state['amendments'] if s['request_id'] == identity)
        admission = dict(transition_id=transition, limits_sha256=digest(recipe['envelope']),
                         profile_sha256=digest(recipe['envelope']['policy']),
                         controller_sha256=proposal['new_controller_sha256'],
                         parent_selector=proposal['parent_selector'], projection_sha256=digest(selected))
        if slot['status'] == 'APPLIED':
            if (slot.get('transition_id') != transition
                    or state.get('admitted_generations', {}).get(transition) != admission):
                raise FeedbackError('Amendment quota was consumed by another transition')
            return
        if state != proof['state']:
            raise FeedbackError('Canonical feedback changed before quota reconciliation')
        from ssdlc_reconciliation import _files
        from ssdlc_candidates import inventory
        runtime = session.ticket / 'execution-runtimes' / transition
        seal = json.loads(_regular(preparation, 'runtime-seal.json').read_text())
        if (_files(runtime) != seal
                or digest(inventory(runtime / 'worktree')) != recipe['runtime_worktree_sha256']):
            raise FeedbackError('Unreconciled delegated runtime changed')
        slot.update(status='APPLIED', transition_id=transition)
        row.update(status='APPLIED', transition_id=transition, applied_at=store.clock(),
                   amendment_subject_sha256=digest(proof['technical_subject']),
                   new_plan_sha256=proof['technical_subject']['new_plan_sha256'])
        state.setdefault('admitted_generations', {})[transition] = admission
        fault('before_feedback_reconciliation')
        store._append('FEEDBACK_NATIVE_AMENDMENT_APPLIED', state)
        fault('after_feedback_reconciliation')


def ensure_reconciled(ticket, projection, proposal):
    """Read-only native entrypoint fence, including crash after selector replace."""
    if proposal.get('authorization_contract') in {'operator-verification-repair-v1', 'operator-verification-repair-deadline-v1'}:
        from ssdlc_verification_repair import ensure_reconciled as verification_reconciled
        verification_reconciled(ticket, projection, proposal)
        return
    from ssdlc_owner_revalidation import ensure_reconciled as owners_reconciled
    owners_reconciled(ticket, projection, proposal)
    if proposal.get('authorization_contract') != CONTRACT:
        from ssdlc_feedback_return import ensure_reconciled as return_reconciled
        return_reconciled(ticket, projection, proposal)
        return
    store = FeedbackStore(ticket)
    state = store.inspect()
    identity = projection['transition_id']
    proof = json.loads(_regular(Path(ticket) / 'execution' / identity, 'intent.json').read_text())['authorization']['proof']
    row = state['requests'].get(proof['request_id'], {})
    if (row.get('status') != 'APPLIED' or row.get('transition_id') != identity
            or state.get('admitted_generations', {}).get(identity, {}).get('projection_sha256') != digest(projection)):
        raise FeedbackError('Delegated quota reconciliation pending; dispatch is fenced')


def admitted(state, source):
    """Only a natively published and reconciled descendant can use old recording caps."""
    entry = state.get('admitted_generations', {}).get(source['projection']['transition_id'])
    return bool(entry and entry['limits_sha256'] == digest(source['envelope'])
                and entry['profile_sha256'] == digest(source['envelope']['policy'])
                and entry['projection_sha256'] == digest(source['projection']))


def descendant_of(ticket, root, ancestor, *, pointer=None):
    from ssdlc_transitions import TransitionStore
    ticket, root, ancestor = Path(ticket), Path(root), Path(ancestor)
    store = TransitionStore(ticket / 'execution')
    pointer = pointer or json.loads(_regular(store.root, 'active.json').read_text())
    seen = set()
    while root != ancestor:
        identity = pointer['transition_id']
        if identity in seen or len(seen) >= 1000 or root != ticket / 'execution-runtimes' / identity:
            return False
        seen.add(identity)
        projection = store.load_pointer(pointer)
        intent = json.loads(_regular(store.root / identity, 'intent.json').read_text())
        proposal = intent['proposal']
        from ssdlc_feedback_return import CONTRACTS as RETURN_CONTRACTS
        if (proposal.get('authorization_contract') not in {CONTRACT, *RETURN_CONTRACTS}
                and not (proposal.get('authorization_contract') == 'scope-amendment-v1'
                         and 'owner_feedback_sha256' in proposal)):
            return False
        ensure_reconciled(ticket, projection, proposal)
        recipe = json.loads(_regular(store.root / identity, 'recipe.json').read_text())
        root = Path(recipe['source_root'])
        pointer = proposal['parent_selector']
        parent = store.load_pointer(pointer)
        if root != ticket / 'execution-runtimes' / parent['transition_id']:
            return False
    return True
