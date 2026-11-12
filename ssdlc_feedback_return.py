"""Native return to an unchanged task after an independently reviewed scope question.

An in-scope return does not amend a plan, reset attempts or refund consultations.
Operator recovery preserves an earlier rejected decision as an immutable fact.
"""
import copy
import json
from pathlib import Path

from ssdlc_feedback import (FeedbackError, FeedbackStore, independence, participants,
                            literal_path, matches_source, source_binding,
                            validate_recording_authority)
from ssdlc_tasks import digest, confined, validate_document
from ssdlc_transitions import _regular


CONTRACT = 'feedback-in-scope-return-v1'
OPERATOR_CONTRACT = 'feedback-operator-repair-return-v1'
CONTRACTS = {CONTRACT, OPERATOR_CONTRACT}


def validate_repair_files(source, files):
    task = next(t for t in source['plan']['tasks'] if t['task_id'] == source['task_id'])
    failure = source['journal']['tasks'][source['task_id']]['failed_attempts'][-1]
    if failure['category'] != 'CHECK_FAILURE':
        raise FeedbackError('In-scope repair requires a verified CHECK_FAILURE')
    if not isinstance(files, list) or not files or len(files) > 100:
        raise FeedbackError('Repair files must be nonempty existing task targets')
    for name in files:
        literal_path(name)
    if len(set(files)) != len(files) or not set(files) <= set(task['target_files']):
        raise FeedbackError('Repair files must be nonempty existing task targets')
    from ssdlc_grader import protected_paths
    protected = protected_paths(source['root'] / 'worktree', source['plan']['base_commit'])
    for name in files:
        literal_path(name)
        confined(source['root'] / 'worktree', name)
        if name in protected or name.startswith('tests/replay/'):
            raise FeedbackError('Repair cannot modify protected files')
    return task


def subject(source, plan, reason, request_id, repair_files, *, operator=False):
    if plan != source['plan'] or not isinstance(reason, str) or not reason.strip() or len(reason) > 1600:
        raise FeedbackError('A repair return must preserve the exact plan and state its reason')
    validate_repair_files(source, repair_files)
    ticket = source['root'].parent.parent
    store = FeedbackStore(ticket)
    authority, state = store._read()
    store._active()
    row = state['requests'].get(request_id)
    if (not row or row['status'] != 'REVIEWED' or row['request']['cause'] != 'MISSING_TARGET'
            or row['review']['verdict'] != 'APPROVED' or row['review']['coverage_preserved'] is not True):
        raise FeedbackError('Repair return requires a reviewed, coverage-preserving scope decision')
    current = source_binding(source)
    immutable = set(current) - {'source_files_sha256', 'counters_sha256'}
    if any(row['binding'].get(k) != current[k] for k in immutable):
        raise FeedbackError('Repair return refers to another task, failure, capture or plan')
    if any(r['request_id'] == request_id for r in state.get('repair_returns', [])):
        raise FeedbackError('The recorded failure already has a repair return')
    if operator:
        if row['proposal']['route'] not in {'REJECTED', 'IN_SCOPE_REPAIR'}:
            raise FeedbackError('Operator recovery cannot relabel another feedback outcome')
    else:
        if (not matches_source(state, row, source)
                or store.disposition(request_id) != 'NATIVE_REPAIR_RETURN_REQUIRED'
                or row['proposal']['repair_files'] != repair_files):
            raise FeedbackError('Delegated repair requires its exact eligible decision and source')
    return dict(schema_version=1, compatibility='feedback-in-scope-return-v1',
        parent_selector=source['parent_selector'], source_files_sha256=digest(source['files']),
        old_plan_sha256=digest(plan), new_plan_sha256=digest(plan), scope_diff={},
        task_id=source['task_id'], reason=reason, counters_sha256=digest(source['counters']),
        journal_sha256=source['files']['03-dev/tasks/task-journal.jsonl'],
        request_id=request_id, repair_files=copy.deepcopy(repair_files),
        feedback_head=_head(store),
        feedback_authority_sha256=digest(authority), feedback_state_sha256=digest(state),
        operator=operator)


def _head(store):
    from ssdlc_tasks import TaskJournal
    data = _regular(store.root, 'task-journal.jsonl').read_bytes()
    if not data or not data.endswith(b'\n'):
        raise FeedbackError('Incomplete feedback journal requires reconciliation')
    _, sequence, sha256 = TaskJournal._replay(data)
    return dict(sequence=sequence, sha256=sha256)


def proof(source, reviewed):
    store = FeedbackStore(source['root'].parent.parent)
    authority, state = store._read()
    value = dict(schema_version=1, subject=reviewed, recording_authority=authority,
                 state=state, envelope=source['envelope'], counters=source['counters'],
                 source_binding=source_binding(source), plan=source['plan'],
                 task_record=source['journal']['tasks'][source['task_id']],
                 runtime_identity=source['journal']['candidate_runtime'])
    validate_proof(value)
    return value


def validate_proof(value):
    try:
        _validate_proof(value)
    except FeedbackError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration):
        raise FeedbackError('Malformed repair-return proof') from None


def _validate_proof(value):
    reviewed, authority, state = value['subject'], value['recording_authority'], value['state']
    validate_recording_authority(authority['policy'], authority['authorization'])
    validate_document('recovery-limits', value['envelope']['policy'])
    if (authority['authorization'].get('contract') != 'native-feedback-recording-v1'
            or value['envelope']['policy'] != authority['authorization']['envelope']['policy']):
        raise FeedbackError('Repair return cannot migrate the frozen feedback grant or global profile')
    if (type(reviewed['operator']) is not bool
            or authority['status'] != 'ACTIVE' or state['policy_sha256'] != digest(authority['policy'])
            or reviewed['feedback_authority_sha256'] != digest(authority)
            or reviewed['feedback_state_sha256'] != digest(state)
            or reviewed['scope_diff'] != {} or reviewed['old_plan_sha256'] != reviewed['new_plan_sha256']
            or reviewed['counters_sha256'] != digest(value['counters'])):
        raise FeedbackError('Repair-return recording or unchanged scope proof changed')
    row = state['requests'][reviewed['request_id']]
    binding = value['source_binding']
    if (binding['parent_selector'] != reviewed['parent_selector']
            or binding['task_id'] != reviewed['task_id']
            or binding['plan_sha256'] != reviewed['old_plan_sha256']
            or binding['source_files_sha256'] != reviewed['source_files_sha256']
            or binding['counters_sha256'] != reviewed['counters_sha256']):
        raise FeedbackError('Repair proof is not bound to its exact native source')
    immutable = set(binding) - {'source_files_sha256', 'counters_sha256'}
    task = next(t for t in value['plan']['tasks'] if t['task_id'] == reviewed['task_id'])
    files = reviewed['repair_files']
    record = value['task_record']
    if (digest(value['plan']) != reviewed['old_plan_sha256'] or not files
            or len(files) != len(set(files)) or not set(files) <= set(task['target_files'])
            or record['failed_attempts'][-1]['category'] != 'CHECK_FAILURE'
            or digest(record['failed_attempts'][-1]) != binding['failure_sha256']
            or any(row['binding'].get(k) != binding[k] for k in immutable)):
        raise FeedbackError('Repair proof changes the original failure or target authority')
    for name in files:
        literal_path(name)
    if (row['status'] != 'REVIEWED' or row['request']['cause'] != 'MISSING_TARGET'
            or row['review']['verdict'] != 'APPROVED' or row['review']['coverage_preserved'] is not True
            or row['review']['proposal_sha256'] != row['proposal_sha256']):
        raise FeedbackError('Repair decision is not independently approved')
    expected = digest(dict(binding=row['binding'], proposal=row['proposal'], planner=row['planner'],
                           path_classes_sha256=row['path_classes_sha256']))
    quality = independence(*participants(state, row))
    if expected != row['proposal_sha256'] or not quality['distinct_roles'] or quality['reduced_independence']:
        raise FeedbackError('Repair-return decisions lost strong independence')
    if reviewed['operator']:
        if row['proposal']['route'] not in {'REJECTED', 'IN_SCOPE_REPAIR'}:
            raise FeedbackError('Operator recovery does not bind an eligible prior decision')
    elif (row['proposal']['route'] != 'IN_SCOPE_REPAIR' or row['proposal']['requested_files']
            or row['proposal']['repair_files'] != reviewed['repair_files']):
        raise FeedbackError('Delegated return must be typed as an unchanged-scope repair')
    anchor = row.get('accounting_anchor')
    if not anchor or anchor['envelope_sha256'] != digest(value['envelope']):
        raise FeedbackError('Repair return lacks its native accounting anchor')
    before, after = anchor['counters'], value['counters']
    calls = [c for c in state['role_calls'] if c['request_id'] == row['request_id']]
    charged = {c.get('native', {}).get('invocation_id') for c in calls}
    if (len(calls) != 2 or None in charged or len(charged) != len(calls)
            or {k: v for k, v in before.items() if k != 'invocations'}
                != {k: v for k, v in after.items() if k != 'invocations'}
            or any(after['invocations'].get(k) != v for k, v in before['invocations'].items())
            or set(after['invocations']) - set(before['invocations']) != charged):
        raise FeedbackError('Repair return changed historical accounting or unrelated invocations')
    for role in ('planner', 'reviewer'):
        calls = [c for c in state['role_calls'] if c['request_id'] == row['request_id'] and c['role'] == role]
        if len(calls) != 1:
            raise FeedbackError('Repair return requires exactly one funded decision per role')
        call = calls[0]
        response = call.get('receipt', {}).get('response', {})
        native = call.get('native', {})
        accounted = value['counters']['invocations'].get(native.get('invocation_id'), {})
        if (call['status'] != 'RETURNED' or digest(response) != call['receipt']['response_sha256']
                or response.get('response') != row['proposal' if role == 'planner' else 'review']
                or call['actor'] != row[role] or native.get('limits_sha256') != digest(value['envelope'])
                or accounted.get('status') != 'returned' or accounted.get('usage') != response.get('usage')
                or accounted.get('role') != 'feedback-' + role or accounted.get('phase') != 'feedback'
                or accounted.get('engine') != call['actor']['engine']):
            raise FeedbackError('Repair return lacks its captured funded decision')


def authorization(proposal, value):
    return dict(schema_version=1, contract=CONTRACT, transition_id=proposal['transition_id'],
                proposal_sha256=digest(proposal), proof=value)


def validate_authorization(auth, proposal):
    from ssdlc_controller import execution_contract
    if (not isinstance(auth, dict) or set(auth) != {'schema_version', 'contract', 'transition_id', 'proposal_sha256', 'proof'}
            or type(auth['schema_version']) is not int or auth['schema_version'] != 1 or auth['contract'] != CONTRACT
            or auth['transition_id'] != proposal['transition_id']
            or auth['proposal_sha256'] != digest(proposal)
            or proposal['old_controller_sha256'] != proposal['new_controller_sha256']
            or proposal['scope_diff'] != {} or not isinstance(auth.get('proof'), dict)
            or not isinstance(auth['proof'].get('subject'), dict)
            or auth['proof']['subject'].get('operator') is not False
            or digest(auth['proof']) != proposal['repair_return_sha256']):
        raise FeedbackError('Repair return lacks exact native unchanged-scope authority')
    validate_proof(auth['proof'])
    reviewed = auth['proof']['subject']
    if (reviewed['parent_selector'] != proposal['parent_selector']
            or reviewed['source_files_sha256'] != digest(proposal['source_files'])
            or digest(execution_contract(auth['proof']['envelope']['binding']['controller']))
                != proposal['old_controller_sha256']):
        raise FeedbackError('Repair-return authority belongs to another parent or controller')
    fence = auth['proof'].get('fence', {})
    if (fence.get('kind') != 'native-exclusive-quiescence-v1' or len(fence.get('locks', [])) != 2
            or fence.get('unresolved_invocations') != 0
            or fence.get('controller_sha256') != proposal['new_controller_sha256']
            or fence.get('environment_sha256') != digest(auth['proof']['runtime_identity']['environment'])):
        raise FeedbackError('Repair return lacks its native fencing receipt')


def finalize(ticket, proposal, recipe, projection):
    """Append a receipt after selection; preserve every prior decision and quota."""
    from ssdlc_tasks import ticket_lock
    store = FeedbackStore(ticket)
    reviewed = recipe['subject']
    identity = proposal['transition_id']
    with ticket_lock(store.root / 'feedback.lock'):
        authority, state = store._read()
        entry = dict(request_id=reviewed['request_id'], task_id=reviewed['task_id'],
                     failure_sha256=recipe['repair_return']['source_binding']['failure_sha256'],
                     transition_id=identity, proposal_sha256=digest(proposal),
                     operator=reviewed['operator'])
        prior = [r for r in state.get('repair_returns', []) if r['transition_id'] == identity]
        admitted = dict(limits_sha256=digest(recipe['envelope']),
                        profile_sha256=digest(recipe['envelope']['policy']), projection_sha256=digest(projection))
        if prior:
            if prior != [entry] or state.get('admitted_generations', {}).get(identity) != admitted:
                raise FeedbackError('Repair return reconciliation changed')
            return
        if digest(authority) != reviewed['feedback_authority_sha256'] or digest(state) != reviewed['feedback_state_sha256']:
            raise FeedbackError('Feedback history changed before repair-return reconciliation')
        state.setdefault('repair_returns', []).append(entry)
        state.setdefault('admitted_generations', {})[identity] = admitted
        store._append('FEEDBACK_REPAIR_RETURN_APPLIED', state)


def ensure_reconciled(ticket, projection, proposal):
    if proposal.get('authorization_contract') not in CONTRACTS:
        return
    state = FeedbackStore(ticket).inspect()
    identity = projection['transition_id']
    recipe = json.loads(_regular(Path(ticket) / 'execution' / identity, 'recipe.json').read_text())
    reviewed = recipe['subject']
    expected = dict(request_id=reviewed['request_id'], task_id=reviewed['task_id'],
                    failure_sha256=recipe['repair_return']['source_binding']['failure_sha256'],
                    transition_id=identity, proposal_sha256=digest(proposal), operator=reviewed['operator'])
    rows = [r for r in state.get('repair_returns', []) if r['transition_id'] == identity]
    if (rows != [expected]
            or state['requests'].get(reviewed['request_id'])
                != recipe['repair_return']['state']['requests'][reviewed['request_id']]
            or state.get('admitted_generations', {}).get(identity, {}).get('projection_sha256') != digest(projection)):
        raise FeedbackError('Repair-return reconciliation pending; dispatch is fenced')


def prepare(session, repo, request_id):
    from ssdlc_scope_amendments import prepare as native_prepare, inspect
    source = inspect(session.ticket, allow_host_hints=True)
    row = FeedbackStore(session.ticket).inspect()['requests'][request_id]
    return native_prepare(session.ticket, repo, source['plan'], None, row['proposal']['reason_code'],
        runtime_identity=session.runtime, controller_snapshot=session.snapshot,
        guard=session.check, held_locks=session.leases, repair_session=session,
        repair_return=dict(operator=False, request_id=request_id, repair_files=row['proposal']['repair_files']))


def activate(session, preparation, *, fault=lambda point: None):
    from ssdlc_scope_amendments import activate as native_activate
    preparation = Path(preparation)
    proposal = json.loads(_regular(preparation, 'proposal.json').read_text())
    recipe = json.loads(_regular(preparation, 'recipe.json').read_text())
    if proposal.get('authorization_contract') != CONTRACT:
        raise FeedbackError('Automatic recovery requires its delegated return contract')
    return native_activate(session.ticket, preparation, authorization(proposal, recipe['repair_return']),
        guard=session.check, held_locks=session.leases, repair_session=session, fault=fault)


def main(argv=None):
    import argparse
    import re
    import sys
    from ssdlc_controller import controller_snapshot, assert_controller, execution_contract
    from ssdlc_feedback_delegation import observe_environment
    from ssdlc_preparations import docker_quiescent
    from ssdlc_scope_amendments import prepare as native_prepare, activate as native_activate, inspect
    from ssdlc_tasks import TaskError
    parser = argparse.ArgumentParser(description='Exact operator in-scope repair return; no inference')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'activate'):
        p = commands.add_parser('feedback-repair-' + name)
        p.add_argument('--repo', required=True); p.add_argument('--ticket', required=True)
        if name == 'prepare':
            p.add_argument('--request-id', required=True)
            p.add_argument('--repair-files', required=True, help='JSON file containing literal current-task targets')
            p.add_argument('--reason', required=True)
        else:
            p.add_argument('--preparation', required=True); p.add_argument('--authorization', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.ticket) or args.ticket in {'.', '..'}:
        parser.error('Invalid ticket')
    repo = Path(args.repo).resolve(); ticket = repo / '.ssdlc' / args.ticket
    read = lambda path: json.loads(Path(path).read_text())
    try:
        controller = Path(__file__).resolve().parent; snapshot = controller_snapshot(controller)
        environment = observe_environment()
        identity = dict(controller_sha256=digest(execution_contract(snapshot)), environment=environment)
        def guard():
            assert_controller(controller, snapshot)
            if observe_environment() != environment or not docker_quiescent(dict(ticket_root=str(ticket),
                    runtime='execution-runtimes', preparation='execution', source_root=str(ticket))):
                raise FeedbackError('Repair-return environment or writer ownership changed')
        if args.command.endswith('prepare'):
            source = inspect(ticket, allow_host_hints=True)
            result = native_prepare(ticket, repo, source['plan'], None, args.reason,
                runtime_identity=identity, controller_snapshot=snapshot, guard=guard,
                repair_return=dict(operator=True, request_id=args.request_id, repair_files=read(args.repair_files)))
            result = dict(preparation=result['preparation'], proposal_sha256=digest(result['proposal']),
                          transition_id=result['proposal']['transition_id'], execution_authorized=False)
        else:
            preparation = Path(args.preparation).absolute()
            if (preparation.parent != ticket / 'execution'
                    or read(preparation / 'proposal.json')['authorization_contract'] != OPERATOR_CONTRACT
                    or read(preparation / 'recipe.json')['runtime_identity'] != identity):
                raise FeedbackError('Operator activation requires the canonical exact controller/environment proposal')
            selected = native_activate(ticket, preparation, read(args.authorization), guard=guard)
            result = dict(transition_id=selected['transition_id'], ready_for_signoff=False)
        print(json.dumps(dict(result, model_dispatches=0), sort_keys=True)); return 0
    except (TaskError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='FEEDBACK_REPAIR_REFUSED', reason=str(exc)[:240], model_dispatches=0)), file=sys.stderr)
        return 2
