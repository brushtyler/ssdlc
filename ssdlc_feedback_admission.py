"""Operator-only feedback grant admission to a stopped I2 generation.

This does not amend scope, reconcile a failed task or extend global limits.
It uses the existing native staging/activation and exact operator authorization.
"""
import copy
from pathlib import Path

from ssdlc_feedback import FeedbackError
from ssdlc_feedback_workflow import workflow
from ssdlc_tasks import digest, validate_document


CONTRACT = 'feedback-profile-admission-v1'


def subject(source, plan, profile, reason):
    validate_document('recovery-limits', profile)
    if workflow(profile) is None:
        raise FeedbackError('Admission requires the explicit feedback workflow grant')
    if plan != source['plan'] or not isinstance(reason, str) or not reason.strip() or len(reason) > 1600:
        raise FeedbackError('Profile admission cannot amend a plan or omit its reason')
    old = source['envelope']['policy']
    normalized = copy.deepcopy(profile)
    for field in ('feedback_workflow', 'feedback_delegation'):
        normalized.pop(field, None)
        if field in old:
            normalized[field] = old[field]
    for field, allowed in [('role_invocations', lambda k: k.startswith('feedback-')),
                           ('phase_invocations', lambda k: k == 'feedback')]:
        for key in list(normalized[field]):
            if allowed(key):
                normalized[field].pop(key)
        normalized[field].update({k: v for k, v in old[field].items() if allowed(k)})
    if normalized != old:
        raise FeedbackError('Feedback admission cannot change global limits, existing roles, time or usage reserves')
    ticket = source['root'].parent.parent
    if (ticket / 'feedback/authority.json').exists():
        raise FeedbackError('Existing feedback authority requires a separate ledger reconciliation')
    return dict(schema_version=1, compatibility=CONTRACT, parent_selector=source['parent_selector'],
                source_files_sha256=digest(source['files']), old_plan_sha256=digest(plan), new_plan_sha256=digest(plan),
                scope_diff={}, task_id=source['task_id'], reason=reason,
                counters_sha256=digest(source['counters']), journal_sha256=source['files']['03-dev/tasks/task-journal.jsonl'],
                profile_sha256=digest(profile), old_profile_sha256=digest(old))


def prepare(ticket, repo, profile, reason, **kwargs):
    from ssdlc_scope_amendments import inspect, prepare as native_prepare
    from ssdlc_locks import transition_lock
    with transition_lock(Path(ticket) / 'ticket.lock', kwargs.get('held_locks', ())) as lease:
        source = inspect(ticket, allow_host_hints=True)
        held = kwargs.get('held_locks', ())
        held = held if any(value is lease for value in held) else (*held, lease)
        return native_prepare(ticket, repo, source['plan'], None, reason, profile_update=profile,
                              **dict(kwargs, held_locks=held))


def activate(ticket, preparation, authorization, **kwargs):
    import json
    from ssdlc_transitions import _regular
    proposal = json.loads(_regular(Path(preparation), 'proposal.json').read_text())
    if proposal.get('authorization_contract') != CONTRACT:
        raise FeedbackError('Feedback profile activation requires its own admission contract')
    from ssdlc_scope_amendments import activate as native_activate
    return native_activate(ticket, preparation, authorization, **kwargs)


def main(argv=None):
    import argparse
    import json
    import re
    import sys
    from ssdlc_controller import controller_snapshot, assert_controller, execution_contract
    from ssdlc_feedback_delegation import observe_environment
    from ssdlc_preparations import docker_quiescent
    from ssdlc_tasks import TaskError
    parser = argparse.ArgumentParser(description='Explicit feedback profile admission; no model calls')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'activate'):
        p = commands.add_parser('feedback-profile-' + name)
        p.add_argument('--repo', required=True); p.add_argument('--ticket', required=True)
        if name == 'prepare':
            p.add_argument('--profile', required=True); p.add_argument('--reason', required=True)
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
                raise FeedbackError('Feedback admission environment or writer ownership changed')
        if args.command.endswith('prepare'):
            result = prepare(ticket, repo, read(args.profile), args.reason, runtime_identity=identity,
                             controller_snapshot=snapshot, guard=guard)
            result = dict(preparation=result['preparation'], proposal_sha256=digest(result['proposal']),
                          transition_id=result['proposal']['transition_id'], execution_authorized=False)
        else:
            preparation = Path(args.preparation).absolute()
            if preparation.parent != ticket / 'execution':
                raise FeedbackError('Activation requires the canonical preparation')
            if read(preparation / 'recipe.json')['runtime_identity'] != identity:
                raise FeedbackError('Prepared controller or environment differs from the executing runtime')
            result = activate(ticket, preparation, read(args.authorization), guard=guard)
            result = dict(transition_id=result['transition_id'], ready_for_signoff=False)
        print(json.dumps(dict(result, model_dispatches=0), sort_keys=True)); return 0
    except (TaskError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='FEEDBACK_PROFILE_REFUSED', reason=str(exc)[:240], model_dispatches=0)), file=sys.stderr)
        return 2
