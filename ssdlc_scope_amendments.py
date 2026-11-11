"""Explicit scope-only recovery of a fenced I2 run, without resetting history.

Preparation never executes candidate code or dispatches a provider. The next
native scheduler preflight verifies the retained checkpoint before dispatch.
Completed receipts remain historical proof bound to their original inputs.
"""
import copy
import json
from pathlib import Path
import time
import uuid

from ssdlc_amendments import scope_diff, validate_amendment
from ssdlc_controller import execution_contract
from ssdlc_reconciliation import _files, _read, _selected_execution_root, CONTEXT
from ssdlc_recovery import LimitsStore, RecoveryStop
from ssdlc_tasks import (TaskError, TaskJournal, TaskScheduler, TaskPlanValidator,
                         atomic_json, digest, file_hash, git, ticket_lock, validate_document)
from ssdlc_transitions import (TransitionError, TransitionStore, _regular,
                               source_binding, sync_tree, validate_authorization)
from ssdlc_locks import transition_lock


def _ticket(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in [path, *path.parents]):
        raise TransitionError('Symlink in scope transition root')
    return path.resolve()


def _git_read(root, *args):
    return git(root, '--no-optional-locks', '-c', 'core.fsmonitor=false', *args)


def inspect(ticket, *, allow_host_hints=False):
    """Inspect the selected stopped generation; no repairing loads or writers."""
    from ssdlc_candidates import CandidateStore, inventory
    ticket = _ticket(ticket)
    _ticket(ticket / 'execution')
    store = TransitionStore(ticket / 'execution')
    projection = store.load()
    _ticket(ticket / 'execution-runtimes' / projection['transition_id'])
    root = _selected_execution_root(ticket, projection)
    outer = _read(root, 'state.json')
    if outer.get('status') not in {'FAILED', 'ESCALATED_TO_SUPERVISOR'}:
        raise TransitionError('Scope amendment requires a stopped failed task run')
    raw = _regular(root, '03-dev/tasks/task-journal.jsonl').read_bytes()
    if not raw.endswith(b'\n'):
        raise TransitionError('Interrupted journal tail requires reconciliation')
    state, _, _ = TaskJournal._replay(raw)
    envelope, counters = LimitsStore(root)._load(repair=False)
    if any(row['status'] not in {'historical', 'returned', 'terminated', 'not_dispatched'}
           for row in counters['invocations'].values()):
        raise TransitionError('Unresolved invocation forbids scope amendment')
    plan, quality = _read(root, '02-design/plan.json'), _read(root, '00-recon/quality-contract.json')
    validate_document('plan', plan)
    validate_document('quality-contract', quality)
    if (plan['ticket_id'] != ticket.name or plan['base_commit'] != outer['base_commit']
            or plan['base_commit'] != envelope['binding']['base_commit']):
        raise TransitionError('Source plan identity differs from its execution authority')
    context = {ref: _regular(root, ref).read_text() if (root / ref).exists() else '(Skipped in Tier S)'
               for ref in CONTEXT}
    if state['inputs_sha256'] != digest(dict(plan=plan, quality=quality, context=context)):
        raise TransitionError('Source scheduler inputs changed')
    if set(state['tasks']) != {task['task_id'] for task in plan['tasks']}:
        raise TransitionError('Source task identities changed')
    selected = [tid for tid, row in state['tasks'].items() if row['status'] == 'FAILED']
    if len(selected) != 1:
        raise TransitionError('Scope amendment requires exactly one failed task')
    task_id = selected[0]
    failed = state['tasks'][task_id].get('failed_attempts', [])
    if not failed or failed[-1]['category'] != 'SCOPE_REQUIRED':
        if not (allow_host_hints and failed and failed[-1]['category'] == 'CHECK_FAILURE'):
            raise TransitionError('Only an explicit SCOPE_REQUIRED failure is supported')
    if any(row['status'] not in {'FAILED', 'COMPLETED', 'PENDING'} for row in state['tasks'].values()):
        raise TransitionError('Active or invalidated work requires separate reconciliation')
    for tid, row in state['tasks'].items():
        if row['status'] == 'PENDING' and row['attempts']:
            raise TransitionError('Pending task has unexplained consumed attempts')
        if row.get('reconciliation_required'):
            raise TransitionError('Earlier reconciliation gate remains active')
    worktree = root / 'worktree'
    if worktree.is_symlink(): raise TransitionError('Symlink in source worktree')
    if _git_read(worktree, 'status', '--porcelain') or _git_read(worktree, 'rev-parse', 'HEAD') != state['expected_head']:
        raise TransitionError('Source checkpoint is dirty or moved')
    for meta in outer['artifacts'].values():
        if file_hash(_regular(root, meta['path'])) != meta['sha256']:
            raise TransitionError('Registered source evidence changed')
    old = TaskScheduler(worktree, root / '03-dev/tasks', plan, quality, context, None, None,
                        isolate_candidates=True, recover_timeouts=True, runtime_identity=state['candidate_runtime'],
                        historical_capture_authority=envelope['binding'].get('historical_capture_bindings_sha256'),
                        historical_task_floors=envelope['binding'].get('task_floors'),
                        scope_reconciliations=envelope['binding'].get('scope_reconciliations'))
    old.state = state
    old._assert_candidate_runtime(state)
    bindings = copy.deepcopy(state.get('historical_capture_bindings', {}))
    for task in plan['tasks']:
        tid, row = task['task_id'], state['tasks'][task['task_id']]
        if row['status'] == 'COMPLETED' and not old._evidence_valid(row, tid):
            raise TransitionError('Completed checkpoint evidence changed')
        records = [row] if row.get('capture_sha256') else []
        records += [dict(row, attempts=f['attempt'], start_commit=f['base_commit'],
                         invocation_id=f['invocation_id'], capture_sha256=f['capture_sha256'])
                    for f in row.get('failed_attempts', []) if f.get('capture_sha256')]
        for record in records:
            binding = old._capture_binding(task, record)
            CandidateStore(root / '03-dev/tasks' / tid / f"attempt-{record['attempts']}").load(
                binding, record['capture_sha256'])
            bindings.setdefault(tid, {})[str(record['attempts'])] = binding
    if not failed[-1].get('capture_sha256'):
        raise TransitionError('Scope failure has no durable candidate receipt')
    task = next(t for t in plan['tasks'] if t['task_id'] == task_id)
    if not all(state['tasks'][dep]['status'] == 'COMPLETED' for dep in task['depends_on']):
        raise TransitionError('Failed task dependencies are incomplete')
    result = dict(root=root, projection=projection, state=outer, journal=state, plan=plan,
                quality=quality, context=context, envelope=envelope, counters=counters,
                task_id=task_id, capture_bindings=bindings, files=_files(root),
                worktree_sha256=digest(inventory(worktree)),
                parent_selector=_read(ticket / 'execution', 'active.json'))
    if failed[-1]['category'] != 'SCOPE_REQUIRED':
        from ssdlc_feedback_analysis import failure_hint
        if failure_hint(result, task)['classification'] != 'ALL_OUTSIDE':
            raise TransitionError('Host scope hypothesis requires all failed checks localized outside the task')
    return result


def subject(source, plan, reason):
    """Pure host-calculated review subject; this grants no execution authority."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1600:
        raise TransitionError('A bounded nonempty scope reason is required')
    validate_document('plan', plan)
    old = source['plan']
    changes = scope_diff(old, plan)
    if (set(changes) != {source['task_id']} or changes[source['task_id']]['removed']
            or not changes[source['task_id']]['added']):
        raise TransitionError('Only target additions to the selected failed task are supported')
    normalized = copy.deepcopy(plan)
    normalized['plan_revision'] = old['plan_revision']
    for before, after in zip(old['tasks'], normalized['tasks']):
        if after.get('task_id') == source['task_id']:
            after['target_files'] = copy.deepcopy(before['target_files'])
    if normalized != old or plan['plan_revision'] != old['plan_revision'] + 1:
        raise TransitionError('Scope amendment cannot change obligations, checks, identity or ordering')
    validator = TaskPlanValidator(source['root'] / 'worktree', source['context'][CONTEXT[0]],
                                  source['quality'], source['root'])
    validator.validate(plan)
    # Manifest extensions obey the same protected-path rules as execution.
    from ssdlc_tasks import confined
    from ssdlc_grader import protected_paths
    protected = protected_paths(source['root'] / 'worktree', old['base_commit'])
    for path in changes[source['task_id']]['added']:
        confined(source['root'] / 'worktree', path)
        current = source['root'] / 'worktree'
        for part in Path(path).parts:
            current = current / part
            if current.is_symlink(): raise TransitionError('Symlink in amended target')
        if path in protected or path.startswith('tests/replay/'):
            raise TransitionError('Protected acceptance paths cannot be amended')
    return dict(schema_version=1, compatibility='post-checkpoint-scope-v1',
                parent_selector=source['parent_selector'], source_files_sha256=digest(source['files']),
                old_plan_sha256=digest(old), new_plan_sha256=digest(plan),
                scope_diff=changes, task_id=source['task_id'], reason=reason,
                counters_sha256=digest(source['counters']), journal_sha256=source['files']['03-dev/tasks/task-journal.jsonl'])


def prepare(ticket, repo, plan, review, reason, *, runtime_identity, controller_snapshot,
            guard=lambda: None, clock=time.time, fault=lambda point: None, held_locks=()):
    """Stage a reviewed amendment, retaining all prior generations and counters."""
    from ssdlc_preparations import unfinished, register, update, pending, clear_pending
    from ssdlc_candidates import inventory
    validate_document('plan', plan)
    ticket, repo = _ticket(ticket), Path(repo).resolve()
    if ticket != repo / '.ssdlc' / plan.get('ticket_id', ''):
        raise TransitionError('Scope target must be the canonical ticket of the target repository')
    with transition_lock(ticket / 'ticket.lock', held_locks):
        if unfinished(ticket, namespace='execution'):
            raise TransitionError('Earlier preparation requires native discard')
        source = inspect(ticket)
        with transition_lock(source['root'] / '03-dev/tasks/scheduler.lock', held_locks):
            guard()
            reviewed = subject(source, plan, reason)
            if (not isinstance(review, dict) or set(review) != {'subject_sha256', 'reviewer', 'report', 'scope_diff'}
                    or not isinstance(review['reviewer'], str) or not review['reviewer'].strip()
                    or review['subject_sha256'] != digest(reviewed)):
                raise TransitionError('Independent review must bind the exact amendment subject')
            validator = TaskPlanValidator(source['root'] / 'worktree', source['context'][CONTEXT[0]],
                                          source['quality'], source['root'])
            validate_amendment(source['plan'], plan, json.dumps(review['report']), validator.validate,
                               validator.requirement_catalogue['required_ids'], source['quality'],
                               declared_scope=review['scope_diff'])
            if runtime_identity['environment'] != source['journal']['candidate_runtime']['environment']:
                raise TransitionError('Scope-only recovery cannot change verification environment')
            if runtime_identity['controller_sha256'] != digest(execution_contract(controller_snapshot)):
                raise TransitionError('Controller snapshot does not match the proposed runtime')
            limits = LimitsStore(source['root'], clock)
            limits.check(source['counters'], source['envelope']['policy'])
            limits.require_capacity('03-dev', '03-dev')
            if source['journal']['tasks'][source['task_id']]['attempts'] >= source['envelope']['policy']['development_attempts']:
                raise RecoveryStop('LIMIT_REACHED', 'Scope amendment cannot grant another development attempt')
            identity = uuid.uuid4().hex
            register(ticket, repo, source['root'], identity, source['journal']['expected_head'], namespace='execution')
            branch = f"ssdlc/{plan['ticket_id']}/execution-{identity}"
            update(ticket, identity, namespace='execution', branch=branch)
            prepared, runtime = ticket / 'execution' / ('prepared-' + identity), ticket / 'execution-runtimes' / identity
            try:
                prepared.mkdir(mode=0o700); runtime.mkdir(mode=0o700)
                for ref in source['files']:
                    dest = runtime / ref
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(_regular(source['root'], ref).read_bytes())
                git(repo, 'worktree', 'add', '-b', branch, str(runtime / 'worktree'), source['journal']['expected_head'])
                update(ticket, identity, namespace='execution', worktree_created=True)
                fault('worktree_created')
                current = copy.deepcopy(source['journal'])
                current.update(candidate_runtime=runtime_identity, historical_capture_bindings=source['capture_bindings'],
                               inputs_sha256=digest(dict(plan=plan, quality=source['quality'], context=source['context'])))
                row = current['tasks'][source['task_id']]
                reconciliation = dict(task_id=source['task_id'], failure_sha256=digest(row['failed_attempts'][-1]),
                                      plan_sha256=digest(plan), subject_sha256=digest(reviewed), reason=reason)
                if 'scope_reconciliation' in row:
                    row.setdefault('scope_reconciliation_history', []).append(row['scope_reconciliation'])
                row['scope_reconciliation'] = reconciliation
                scope = {tid: r['scope_reconciliation'] for tid, r in current['tasks'].items() if 'scope_reconciliation' in r}
                envelope, counters = copy.deepcopy(source['envelope']), copy.deepcopy(source['counters'])
                envelope['binding'].update(controller=controller_snapshot, reconciliation_id=identity,
                    historical_capture_bindings_sha256=digest(source['capture_bindings']), scope_reconciliations=scope,
                    historical_invocations=copy.deepcopy(counters['invocations']),
                    task_floors={tid: {key: copy.deepcopy(r[key]) for key in
                        ('attempts', 'failed_attempts', 'capture_verifications', 'reconciliation_required') if key in r}
                        for tid, r in current['tasks'].items()})
                counters['policy_sha256'] = digest(envelope)
                store = LimitsStore(runtime, clock)
                atomic_json(store.policy_path, envelope)
                store.journal.load()
                store.journal.append('SCOPE_AMENDMENT_LIMITS_BOUND', counters)
                atomic_json(runtime / '02-design/plan.json', plan)
                atomic_json(runtime / '00-recon/controller-contract.json', controller_snapshot)
                outer = copy.deepcopy(source['state'])
                outer.update(status='SCOPE_AMENDMENT_PREPARED', current_phase='03-dev', current_step='awaiting-exact-authorization',
                             dev_branch=branch, gate_history=[], frozen_limits=envelope['policy'])
                outer.pop('failure', None); outer.pop('task_verified_head', None)
                outer['bounded_execution'] = dict(kind='bounded-i2', policy_sha256=digest(envelope))
                atomic_json(runtime / 'execution-mode.json', dict(outer['bounded_execution'], status='ACTIVE'))
                for ref in ('02-design/plan.json', '00-recon/controller-contract.json'):
                    outer['artifacts'][ref] = dict(path=ref, sha256=file_hash(runtime / ref))
                atomic_json(runtime / 'state.json', outer)
                store._publish_mode(envelope, outer)
                atomic_json(runtime / 'state.json', outer)
                scheduler = TaskScheduler(runtime / 'worktree', runtime / '03-dev/tasks', plan, source['quality'], source['context'],
                    None, None, isolate_candidates=True, recover_timeouts=True, runtime_identity=runtime_identity,
                    historical_capture_authority=digest(source['capture_bindings']), scope_reconciliations=scope)
                current['ignored_paths'] = scheduler._ignored()
                for record in current['tasks'].values():
                    if 'protected_snapshot' in record: record['protected_snapshot'] = scheduler._protected()
                scheduler.state = current
                scheduler.journal.load(); scheduler._save('SCOPE_AMENDMENT_STAGED')
                marker = dict(transition_id=identity, source_generation=source['projection']['transition_id'],
                              runtime_identity=runtime_identity, policy_sha256=digest(envelope),
                              model_dispatch_authorized=True, entrypoint='run', from_phase='03-dev')
                atomic_json(runtime / 'generation.json', marker)
                projected = copy.deepcopy(source['projection'])
                projected.update(transition_id=identity, runtime_binding=marker)
                recipe = dict(source_root=str(source['root']), source_worktree_sha256=source['worktree_sha256'],
                              runtime_worktree_sha256=digest(inventory(runtime / 'worktree')), runtime_identity=runtime_identity,
                              counters=counters, envelope=envelope, journal_state=current, subject=reviewed, review=review,
                              ledger=outer['ledger'], old_policy_sha256=digest(source['envelope']))
                atomic_json(prepared / 'recipe.json', recipe)
                atomic_json(prepared / 'projection.json', projected)
                atomic_json(prepared / 'runtime-seal.json', _files(runtime))
                proposal = dict(schema_version=1, compatibility='post-checkpoint-scope-v1', transition_id=identity,
                    authorization_contract='scope-amendment-v1', parent_selector=source['parent_selector'],
                    old_controller_sha256=digest(execution_contract(_read(source['root'], '00-recon/controller-contract.json'))),
                    new_controller_sha256=runtime_identity['controller_sha256'], scope_diff=reviewed['scope_diff'],
                    source_files=source['files'], projection_sha256=digest(projected),
                    artifacts={name: file_hash(prepared / name) for name in ('recipe.json', 'runtime-seal.json')})
                atomic_json(prepared / 'proposal.json', proposal)
                guard(); sync_tree(runtime); sync_tree(prepared)
                pending(ticket, identity, namespace='execution')
                fault('prepared')
                return dict(preparation=str(prepared), proposal=proposal, model_dispatches=0)
            except BaseException as exc:
                update(ticket, identity, namespace='execution', status='REJECTED', error_type=type(exc).__name__)
                clear_pending(ticket, identity, namespace='execution')
                raise


def activate(ticket, preparation, authorization, *, guard=lambda: None, clock=time.time, fault=lambda point: None,
             held_locks=()):
    from ssdlc_candidates import inventory
    from ssdlc_preparations import update, clear_pending
    ticket, preparation = _ticket(ticket), _ticket(preparation)
    proposal, recipe = _read(preparation, 'proposal.json'), _read(preparation, 'recipe.json')
    if (preparation != ticket / 'execution' / ('prepared-' + proposal['transition_id'])
            or proposal.get('authorization_contract') != 'scope-amendment-v1'):
        raise TransitionError('Activation requires the recorded scope preparation')
    validate_authorization(authorization, proposal)
    with transition_lock(ticket / 'ticket.lock', held_locks):
        guard()
        store = TransitionStore(ticket / 'execution', fault)
        selected, projected = store.load(), _read(preparation, 'projection.json')
        if selected['transition_id'] == proposal['transition_id']:
            if selected != projected: raise TransitionError('Activated projection changed')
            _selected_execution_root(ticket, selected)
            update(ticket, proposal['transition_id'], namespace='execution', status='ACTIVE')
            clear_pending(ticket, proposal['transition_id'], namespace='execution')
            return selected
        source = inspect(ticket)
        with transition_lock(source['root'] / '03-dev/tasks/scheduler.lock', held_locks):
            if (source['parent_selector'] != proposal['parent_selector'] or source['files'] != proposal['source_files']
                    or str(source['root']) != recipe['source_root'] or source['worktree_sha256'] != recipe['source_worktree_sha256']):
                raise TransitionError('Parent generation changed after amendment review')
            for ref, sha in proposal['artifacts'].items():
                if file_hash(_regular(preparation, ref)) != sha: raise TransitionError('Prepared amendment changed')
            runtime = ticket / 'execution-runtimes' / proposal['transition_id']
            seal = _read(preparation, 'runtime-seal.json')
            if _files(runtime) != seal: raise TransitionError('Prepared runtime changed')
            envelope, counters = LimitsStore(runtime, clock)._load(repair=False)
            tasks = TaskJournal(runtime / '03-dev/tasks').peek()
            if (envelope != recipe['envelope'] or counters != recipe['counters'] or tasks != recipe['journal_state']
                    or _read(runtime, 'state.json')['ledger'] != recipe['ledger']
                    or digest(inventory(runtime / 'worktree')) != recipe['runtime_worktree_sha256']
                    or _git_read(runtime / 'worktree', 'status', '--porcelain')
                    or _git_read(runtime / 'worktree', 'rev-parse', 'HEAD') != tasks['expected_head']):
                raise TransitionError('Native amendment projection changed')
            limits = LimitsStore(runtime, clock)
            limits.check(counters, envelope['policy']); limits.require_capacity('03-dev', '03-dev')
            artifacts = {name: _regular(preparation, name).read_bytes() for name in proposal['artifacts']}
            result = store.publish(source['root'], proposal, authorization, source['projection'], projected, artifacts)
            update(ticket, proposal['transition_id'], namespace='execution', status='ACTIVE')
            clear_pending(ticket, proposal['transition_id'], namespace='execution')
            return result


def main(argv=None):
    """Offline native amendment commands; activation itself dispatches nothing."""
    import argparse
    import re
    import sys
    from orchestrator import EngineRunner, SSDLCOrchestrator
    from ssdlc_controller import controller_snapshot, assert_controller
    from ssdlc_preparations import discard, docker_quiescent
    parser = argparse.ArgumentParser(description='Explicit scope-only recovery after task checkpoints')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'preview', 'prepare', 'activate', 'discard'):
        command = commands.add_parser('continuation-scope-' + name)
        command.add_argument('--repo', '-r', default='.')
        command.add_argument('--ticket', required=True)
        if name in {'preview', 'prepare'}:
            command.add_argument('--plan', required=True)
            command.add_argument('--reason', required=True)
        if name == 'prepare': command.add_argument('--review', required=True)
        if name in {'activate', 'discard'}: command.add_argument('--preparation', required=True)
        if name == 'activate': command.add_argument('--authorization', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.ticket) or args.ticket in {'.', '..'}:
        parser.error('Invalid ticket')
    repo = Path(args.repo).resolve()
    ticket = repo / '.ssdlc' / args.ticket
    read = lambda path: json.loads(Path(path).read_text())
    try:
        if args.command in {'continuation-scope-inspect', 'continuation-scope-preview'}:
            with ticket_lock(ticket / 'ticket.lock'):
                source = inspect(ticket)
                with ticket_lock(source['root'] / '03-dev/tasks/scheduler.lock'):
                    if args.command.endswith('preview'):
                        value = subject(source, read(args.plan), args.reason)
                        result = dict(subject=value, subject_sha256=digest(value), execution_authorized=False)
                    else:
                        result = dict(task_id=source['task_id'], source_generation=source['projection']['transition_id'],
                                      source_sha256=digest(source['files']),
                                      attempts={tid: r['attempts'] for tid, r in source['journal']['tasks'].items()},
                                      invocations=len(source['counters']['invocations']),
                                      deadline_at=source['counters']['deadline_at'])
        elif args.command == 'continuation-scope-discard':
            identity = Path(args.preparation).name.removeprefix('prepared-')
            if Path(args.preparation).resolve() != ticket / 'execution' / ('prepared-' + identity):
                raise TransitionError('Discard requires the recorded preparation path')
            record = discard(ticket, identity, namespace='execution', writers_quiescent=docker_quiescent)
            result = dict(transition_id=identity, status=record['status'])
        else:
            if not EngineRunner._should_spawn_role_container():
                raise TransitionError('Scope recovery requires Docker role execution; no local fallback')
            controller = Path(__file__).resolve().parent
            snapshot = controller_snapshot(controller)
            environment = SSDLCOrchestrator._verification_environment(None, ['python3'])
            if environment.get('mode') != 'role_container' or not environment.get('image_id'):
                raise TransitionError('Cannot bind verification image')
            runtime_identity = dict(controller_sha256=digest(execution_contract(snapshot)), environment=environment)
            def guard():
                if not docker_quiescent(dict(ticket_root=str(ticket), runtime='execution-runtimes',
                                              preparation='execution', source_root=str(ticket))):
                    raise TransitionError('Source/staged writers are not proven quiescent')
                assert_controller(controller, snapshot)
                if SSDLCOrchestrator._verification_environment(None, ['python3']) != environment:
                    raise TransitionError('Verification environment changed')
            if args.command == 'continuation-scope-prepare':
                result = prepare(ticket, repo, read(args.plan), read(args.review), args.reason,
                                 runtime_identity=runtime_identity, controller_snapshot=snapshot, guard=guard)
                result = dict(preparation=result['preparation'], proposal_sha256=digest(result['proposal']),
                              transition_id=result['proposal']['transition_id'], execution_authorized=False)
            else:
                preparation = Path(args.preparation).resolve()
                if preparation.parent != ticket / 'execution':
                    raise TransitionError('Activation requires the recorded preparation path')
                if _read(preparation, 'recipe.json')['runtime_identity'] != runtime_identity:
                    raise TransitionError('Prepared controller/environment differs from executing runtime')
                selected = activate(ticket, preparation, read(args.authorization), guard=guard)
                result = dict(transition_id=selected['transition_id'], model_dispatch_authorized=True,
                              ready_for_signoff=False)
        result['model_dispatches'] = 0
        print(json.dumps(result, sort_keys=True))
        return 0
    except (TaskError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='SCOPE_AMENDMENT_REFUSED', reason=str(exc)[:240], model_dispatches=0)), file=sys.stderr)
        return 2
