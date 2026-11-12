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


def inspect(ticket, *, allow_host_hints=False, verification_repair=False):
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
        if not (failed and (allow_host_hints and failed[-1]['category'] == 'CHECK_FAILURE'
                            or verification_repair and failed[-1]['category'] == 'ENVIRONMENT_DIAGNOSIS')):
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
                        max_attempts=envelope['policy']['development_attempts'],
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
    if verification_repair:
        from ssdlc_verification_repair import require_timeout
        require_timeout(result)
    elif failed[-1]['category'] != 'SCOPE_REQUIRED':
        from ssdlc_feedback_analysis import failure_hint
        if failure_hint(result, task)['classification'] != 'ALL_OUTSIDE':
            raise TransitionError('Host scope hypothesis requires all failed checks localized outside the task')
    return result


def subject(source, plan, reason, *, feedback_request_id=None, reconcile_feedback_evidence=False,
            operator_owner_limit=None):
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
    result = dict(schema_version=1, compatibility='post-checkpoint-scope-v1',
                parent_selector=source['parent_selector'], source_files_sha256=digest(source['files']),
                old_plan_sha256=digest(old), new_plan_sha256=digest(plan),
                scope_diff=changes, task_id=source['task_id'], reason=reason,
                counters_sha256=digest(source['counters']), journal_sha256=source['files']['03-dev/tasks/task-journal.jsonl'])
    from ssdlc_owner_revalidation import scope_contract
    owners = scope_contract(source, changes[source['task_id']]['added'])
    if owners:
        result['owner_revalidation'] = owners
    if feedback_request_id is not None:
        if not owners:
            raise TransitionError('Owner feedback bridge requires completed ownership or baseline coverage')
        from ssdlc_owner_revalidation import feedback_binding
        result['owner_feedback'] = feedback_binding(source['root'].parent.parent, source,
            changes[source['task_id']]['added'], feedback_request_id, reconcile_evidence=reconcile_feedback_evidence,
            operator_owner_limit=operator_owner_limit)
    elif reconcile_feedback_evidence or operator_owner_limit is not None:
        raise TransitionError('Evidence reconciliation requires an explicit feedback request')
    return result


def prepare(ticket, repo, plan, review, reason, *, runtime_identity, controller_snapshot,
            guard=lambda: None, clock=time.time, fault=lambda point: None, held_locks=(), delegation=None, profile_update=None,
            repair_return=None, repair_session=None, feedback_request_id=None, reconcile_feedback_evidence=False,
            operator_owner_limit=None, operator_recovery_profile=None, verification_repair=None):
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
        source = inspect(ticket, allow_host_hints=delegation is not None or profile_update is not None or repair_return is not None,
                         verification_repair=verification_repair is not None)
        with transition_lock(source['root'] / '03-dev/tasks/scheduler.lock', held_locks):
            guard()
            if verification_repair is not None and (not isinstance(verification_repair, dict)
                    or set(verification_repair) not in ({'quality', 'profile'}, {'quality', 'profile', 'deadline_at'})
                    or any(x is not None for x in (delegation, profile_update, repair_return,
                        feedback_request_id, operator_owner_limit, operator_recovery_profile))
                    or reconcile_feedback_evidence):
                raise TransitionError('Verification repair requires its separate exact operator capability')
            if operator_recovery_profile is not None and (feedback_request_id is None or
                    any(x is not None for x in (delegation, profile_update, repair_return))):
                raise TransitionError('Operator recovery cannot borrow another transition capability')
            if operator_owner_limit is not None and feedback_request_id is None:
                raise TransitionError('Operator owner allowance requires its exact feedback request')
            if feedback_request_id is not None and any(x is not None for x in (delegation, profile_update, repair_return)):
                raise TransitionError('Operator owner feedback cannot be combined with another transition capability')
            if profile_update is not None and delegation is not None:
                raise TransitionError('Profile admission cannot use scope delegation')
            if repair_return is not None and (profile_update is not None or delegation is not None):
                raise TransitionError('An unchanged-scope return cannot amend grants or targets')
            return_proof = None
            if repair_return is not None:
                from ssdlc_feedback_return import subject as return_subject, proof as return_proof_for
                operator = repair_return['operator']
                if type(operator) is not bool:
                    raise TransitionError('Repair-return authorization mode must be explicit')
                if not operator:
                    from ssdlc_feedback_delegation import FencedSession
                    if (not isinstance(repair_session, FencedSession) or repair_session.ticket != ticket
                            or repair_session.root != source['root'] or repair_session.runtime != runtime_identity
                            or repair_session.snapshot != controller_snapshot):
                        raise TransitionError('Delegated repair return requires the live native owner')
                    repair_session.check()
            if delegation is not None:
                from ssdlc_feedback_delegation import build_proof, FencedSession
                from ssdlc_feedback import FeedbackStore
                session, request_id = delegation
                if (not isinstance(session, FencedSession) or session.ticket != ticket
                        or session.root != source['root'] or session.runtime != runtime_identity
                        or session.snapshot != controller_snapshot):
                    raise TransitionError('Delegation requires the live native owner session')
                feedback = FeedbackStore(ticket)
                reason = feedback.inspect()['requests'][request_id]['proposal']['reason_code']
                review = build_proof(session, source, plan, reason, feedback, request_id)
            if repair_return is not None:
                reviewed = return_subject(source, plan, reason, repair_return['request_id'],
                                          repair_return['repair_files'], operator=operator)
                return_proof = return_proof_for(source, reviewed)
                if not operator:
                    return_proof['fence'] = repair_session.check()
            elif verification_repair is not None:
                from ssdlc_verification_repair import subject as verification_subject
                reviewed = verification_subject(source, plan, verification_repair['quality'],
                                                verification_repair['profile'], reason,
                                                deadline_at=verification_repair.get('deadline_at'))
            elif operator_recovery_profile is not None:
                from ssdlc_operator_recovery import subject as recovery_subject
                reviewed = recovery_subject(source, plan, reason, profile=operator_recovery_profile,
                    feedback_request_id=feedback_request_id,
                    reconcile_feedback_evidence=reconcile_feedback_evidence,
                    operator_owner_limit=operator_owner_limit)
            elif profile_update is not None:
                from ssdlc_feedback_admission import subject as admission_subject
                reviewed = admission_subject(source, plan, profile_update, reason)
            else:
                reviewed = subject(source, plan, reason, feedback_request_id=feedback_request_id,
                                   reconcile_feedback_evidence=reconcile_feedback_evidence,
                                   operator_owner_limit=operator_owner_limit)
            review_fields = {'subject_sha256', 'reviewer', 'report', 'scope_diff'}
            if 'owner_revalidation' in reviewed:
                review_fields.add('owner_coverage')
            if delegation is None and profile_update is None and repair_return is None and (not isinstance(review, dict) or set(review) != review_fields
                    or not isinstance(review['reviewer'], str) or not review['reviewer'].strip()
                    or review['subject_sha256'] != digest(reviewed)):
                raise TransitionError('Independent review must bind the exact amendment subject')
            if 'owner_revalidation' in reviewed:
                from ssdlc_owner_revalidation import validate_review, validate_patches
                coverage = review['state']['requests'][review['request_id']]['review'].get('owner_coverage') if delegation else review.get('owner_coverage')
                validate_review(reviewed['owner_revalidation'], coverage)
                validate_patches(reviewed['owner_revalidation'], coverage, source['root'] / 'worktree')
            validator = TaskPlanValidator(source['root'] / 'worktree', source['context'][CONTEXT[0]],
                                          source['quality'], source['root'])
            if delegation is None and profile_update is None and repair_return is None:
                validate_amendment(source['plan'], plan, json.dumps(review['report']), validator.validate,
                                   validator.requirement_catalogue['required_ids'], source['quality'],
                                   declared_scope=review['scope_diff'])
            if runtime_identity['environment'] != source['journal']['candidate_runtime']['environment']:
                raise TransitionError('Scope-only recovery cannot change verification environment')
            if runtime_identity['controller_sha256'] != digest(execution_contract(controller_snapshot)):
                raise TransitionError('Controller snapshot does not match the proposed runtime')
            quality = verification_repair['quality'] if verification_repair else source['quality']
            limits = LimitsStore(source['root'], clock)
            attempt_profile = (verification_repair['profile'] if verification_repair else
                               operator_recovery_profile or source['envelope']['policy'])
            extension = reviewed.get('verification_repair', {}).get('deadline_extension')
            if extension is None:
                limits.check(source['counters'], source['envelope']['policy'])
                limits.require_capacity('03-dev', '03-dev')
            else:
                proposed_envelope = copy.deepcopy(source['envelope']); proposed_envelope['policy'] = attempt_profile
                proposed_counters = dict(source['counters'], deadline_at=extension['new_deadline_at'])
                limits.check_capacity(proposed_envelope, proposed_counters, '03-dev', '03-dev')
            if source['journal']['tasks'][source['task_id']]['attempts'] >= attempt_profile['development_attempts']:
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
                               inputs_sha256=digest(dict(plan=plan, quality=quality, context=source['context'])))
                row = current['tasks'][source['task_id']]
                if profile_update is None:
                    reconciliation = dict(task_id=source['task_id'], failure_sha256=digest(row['failed_attempts'][-1]),
                                          plan_sha256=digest(plan), subject_sha256=digest(reviewed), reason=reason)
                    if verification_repair is not None:
                        reconciliation['verification_repair'] = copy.deepcopy(reviewed['verification_repair'])
                    if 'owner_revalidation' in reviewed:
                        reconciliation['owner_revalidation'] = dict(contract=reviewed['owner_revalidation'],
                            coverage_review=copy.deepcopy(coverage))
                    elif 'owner_revalidation' in row.get('scope_reconciliation', {}):
                        # An unchanged-scope repair/controller return must not
                        # shed previously admitted owner obligations.
                        reconciliation['owner_revalidation'] = copy.deepcopy(
                            row['scope_reconciliation']['owner_revalidation'])
                    if 'scope_reconciliation' in row:
                        row.setdefault('scope_reconciliation_history', []).append(row['scope_reconciliation'])
                    row['scope_reconciliation'] = reconciliation
                scope = {tid: r['scope_reconciliation'] for tid, r in current['tasks'].items() if 'scope_reconciliation' in r}
                envelope, counters = copy.deepcopy(source['envelope']), copy.deepcopy(source['counters'])
                if profile_update is not None:
                    envelope['policy'] = copy.deepcopy(profile_update)
                    phases = envelope['binding'].setdefault('allowed_phases', [])
                    if 'feedback' not in phases:
                        phases.append('feedback')
                if operator_recovery_profile is not None:
                    envelope['policy'] = copy.deepcopy(operator_recovery_profile)
                    current['attempt_limit'] = operator_recovery_profile['development_attempts']
                if verification_repair is not None:
                    envelope['policy'] = copy.deepcopy(verification_repair['profile'])
                    current['attempt_limit'] = envelope['policy']['development_attempts']
                    if extension is not None:
                        envelope['binding'].setdefault('deadline_extensions', []).append(copy.deepcopy(extension))
                        counters['deadline_at'] = extension['new_deadline_at']
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
                if verification_repair is not None:
                    atomic_json(runtime / '00-recon/quality-contract.json', quality)
                atomic_json(runtime / '00-recon/controller-contract.json', controller_snapshot)
                outer = copy.deepcopy(source['state'])
                outer.update(status='SCOPE_AMENDMENT_PREPARED', current_phase='03-dev', current_step='awaiting-exact-authorization',
                             dev_branch=branch, gate_history=[], frozen_limits=envelope['policy'])
                outer.pop('failure', None); outer.pop('task_verified_head', None)
                outer['bounded_execution'] = dict(kind='bounded-i2', policy_sha256=digest(envelope))
                atomic_json(runtime / 'execution-mode.json', dict(outer['bounded_execution'], status='ACTIVE'))
                for ref in ('02-design/plan.json', '00-recon/controller-contract.json', '00-recon/quality-contract.json'):
                    outer['artifacts'][ref] = dict(path=ref, sha256=file_hash(runtime / ref))
                atomic_json(runtime / 'state.json', outer)
                store._publish_mode(envelope, outer)
                atomic_json(runtime / 'state.json', outer)
                scheduler = TaskScheduler(runtime / 'worktree', runtime / '03-dev/tasks', plan, quality, source['context'],
                    None, None, isolate_candidates=True, recover_timeouts=True, runtime_identity=runtime_identity,
                    max_attempts=envelope['policy']['development_attempts'],
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
                if return_proof is not None:
                    recipe['repair_return'] = return_proof
                atomic_json(prepared / 'recipe.json', recipe)
                atomic_json(prepared / 'projection.json', projected)
                atomic_json(prepared / 'runtime-seal.json', _files(runtime))
                proposal = dict(schema_version=1, compatibility='post-checkpoint-scope-v1', transition_id=identity,
                    authorization_contract='scope-amendment-v1', parent_selector=source['parent_selector'],
                    old_controller_sha256=digest(execution_contract(_read(source['root'], '00-recon/controller-contract.json'))),
                    new_controller_sha256=runtime_identity['controller_sha256'], scope_diff=reviewed['scope_diff'],
                    source_files=source['files'], projection_sha256=digest(projected),
                    artifacts={name: file_hash(prepared / name) for name in ('recipe.json', 'runtime-seal.json')})
                if delegation is not None:
                    from ssdlc_feedback_delegation import CONTRACT
                    proposal.update(authorization_contract=CONTRACT, delegation_sha256=digest(review))
                if profile_update is not None:
                    from ssdlc_feedback_admission import CONTRACT as PROFILE_CONTRACT
                    proposal.update(authorization_contract=PROFILE_CONTRACT, profile_sha256=digest(profile_update))
                if operator_recovery_profile is not None:
                    from ssdlc_operator_recovery import CONTRACT as RECOVERY_CONTRACT
                    proposal.update(authorization_contract=RECOVERY_CONTRACT,
                        profile_sha256=digest(operator_recovery_profile),
                        operator_recovery_sha256=digest(reviewed['operator_recovery']))
                if verification_repair is not None:
                    proposal.update(authorization_contract=reviewed['verification_repair']['capability'],
                        profile_sha256=digest(envelope['policy']), quality_contract_sha256=digest(quality),
                        verification_repair_sha256=digest(reviewed['verification_repair']))
                    if extension is not None:
                        proposal['deadline_extension_sha256'] = digest(extension)
                if return_proof is not None:
                    from ssdlc_feedback_return import CONTRACT as RETURN_CONTRACT, OPERATOR_CONTRACT
                    proposal.update(authorization_contract=OPERATOR_CONTRACT if operator else RETURN_CONTRACT,
                                    repair_return_sha256=digest(return_proof))
                if 'owner_feedback' in reviewed:
                    proposal['owner_feedback_sha256'] = digest(reviewed['owner_feedback'])
                    if 'operator_allowance' in reviewed['owner_feedback']:
                        proposal['owner_allowance_sha256'] = digest(reviewed['owner_feedback']['operator_allowance'])
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
             held_locks=(), delegation=None, repair_session=None):
    from ssdlc_candidates import inventory
    from ssdlc_preparations import update, clear_pending
    ticket, preparation = _ticket(ticket), _ticket(preparation)
    proposal, recipe = _read(preparation, 'proposal.json'), _read(preparation, 'recipe.json')
    from ssdlc_operator_recovery import CONTRACT as RECOVERY_CONTRACT, validate_binding as validate_recovery
    validate_recovery(proposal, recipe)
    from ssdlc_verification_repair import CONTRACTS as VERIFICATION_CONTRACTS, validate_binding as validate_verification
    validate_verification(proposal, recipe)
    if proposal.get('authorization_contract') in VERIFICATION_CONTRACTS:
        for ref, sha in proposal['artifacts'].items():
            if file_hash(_regular(preparation, ref)) != sha:
                raise TransitionError('Prepared verification repair changed')
    if 'owner_feedback' in recipe['subject'] or 'owner_allowance_sha256' in proposal:
        from ssdlc_owner_revalidation import validate_allowance_binding
        validate_allowance_binding(proposal, recipe['subject'].get('owner_feedback', {}))
    from ssdlc_feedback_delegation import CONTRACT, FencedSession, finalize
    delegated = proposal.get('authorization_contract') == CONTRACT
    profile_admission = proposal.get('authorization_contract') == 'feedback-profile-admission-v1'
    operator_recovery = proposal.get('authorization_contract') == RECOVERY_CONTRACT
    verification_repair = proposal.get('authorization_contract') in VERIFICATION_CONTRACTS
    from ssdlc_feedback_return import CONTRACTS as RETURN_CONTRACTS, CONTRACT as RETURN_CONTRACT
    repair_return = proposal.get('authorization_contract') in RETURN_CONTRACTS
    repair_delegated = proposal.get('authorization_contract') == RETURN_CONTRACT
    if (preparation != ticket / 'execution' / ('prepared-' + proposal['transition_id'])
            or proposal.get('authorization_contract') not in {'scope-amendment-v1', CONTRACT, RECOVERY_CONTRACT, *VERIFICATION_CONTRACTS, 'feedback-profile-admission-v1', *RETURN_CONTRACTS}
            or delegated and (not isinstance(delegation, FencedSession) or delegation.ticket != ticket
                              or delegation.root != Path(recipe['source_root']))
            or not delegated and delegation is not None):
        raise TransitionError('Activation requires the recorded scope preparation')
    if repair_delegated and (not isinstance(repair_session, FencedSession)
            or repair_session.ticket != ticket or repair_session.root != Path(recipe['source_root'])
            or repair_session.runtime != recipe['runtime_identity']):
        raise TransitionError('Delegated repair activation requires its live native owner')
    validate_authorization(authorization, proposal)
    with transition_lock(ticket / 'ticket.lock', held_locks):
        guard()
        if delegated:
            delegation.check()
        if repair_delegated:
            repair_session.check()
        store = TransitionStore(ticket / 'execution', fault)
        selected, projected = store.load(), _read(preparation, 'projection.json')
        if selected['transition_id'] == proposal['transition_id']:
            if selected != projected: raise TransitionError('Activated projection changed')
            _selected_execution_root(ticket, selected)
            if delegated:
                finalize(delegation, proposal, recipe, selected, fault=fault)
            if repair_return:
                from ssdlc_feedback_return import finalize as finalize_return
                finalize_return(ticket, proposal, recipe, selected)
            from ssdlc_owner_revalidation import finalize as finalize_owners
            finalize_owners(ticket, proposal, recipe, selected)
            if verification_repair:
                from ssdlc_verification_repair import finalize as finalize_verification
                finalize_verification(ticket, proposal, recipe, selected)
            update(ticket, proposal['transition_id'], namespace='execution', status='ACTIVE')
            clear_pending(ticket, proposal['transition_id'], namespace='execution')
            return selected
        source = inspect(ticket, allow_host_hints=delegated or profile_admission or repair_return,
                         verification_repair=verification_repair)
        with transition_lock(source['root'] / '03-dev/tasks/scheduler.lock', held_locks):
            if verification_repair:
                from ssdlc_verification_repair import subject as verification_subject
                runtime = ticket / 'execution-runtimes' / proposal['transition_id']
                if verification_subject(source, _read(runtime, '02-design/plan.json'),
                        _read(runtime, '00-recon/quality-contract.json'), recipe['envelope']['policy'],
                        recipe['subject']['reason'], deadline_at=recipe['subject']['verification_repair'].get(
                            'deadline_extension', {}).get('new_deadline_at')) != recipe['subject']:
                    raise TransitionError('Verification repair source or quality changed after review')
            if 'owner_feedback_sha256' in proposal:
                subject_builder = subject
                extra = {}
                if operator_recovery:
                    from ssdlc_operator_recovery import subject as subject_builder
                    extra['profile'] = recipe['envelope']['policy']
                expected = subject_builder(source, _read(ticket / 'execution-runtimes' / proposal['transition_id'],
                    '02-design/plan.json'), recipe['subject']['reason'],
                    feedback_request_id=recipe['subject']['owner_feedback']['request_id'],
                    reconcile_feedback_evidence='source_reconciliation' in recipe['subject']['owner_feedback'],
                    operator_owner_limit=recipe['subject']['owner_feedback'].get('operator_allowance', {}).get('manual_limit'), **extra)
                if expected != recipe['subject']:
                    raise TransitionError('Owner feedback changed after coverage review')
            if delegated:
                from ssdlc_feedback_delegation import build_proof
                from ssdlc_feedback import FeedbackStore
                # Persisted lock observations describe preparation. Reobserve
                # liveness now without rewriting the sealed AI response/proof.
                proof = build_proof(delegation, source, _read(ticket / 'execution-runtimes' / proposal['transition_id'],
                    '02-design/plan.json'), recipe['subject']['reason'], FeedbackStore(ticket),
                    recipe['review']['request_id'])
                proof['fence'] = recipe['review']['fence']
                if proof != recipe['review']:
                    raise TransitionError('Delegated feedback changed after preparation')
            if profile_admission:
                from ssdlc_feedback_admission import subject as admission_subject
                if admission_subject(source, source['plan'], recipe['envelope']['policy'], recipe['subject']['reason']) != recipe['subject']:
                    raise TransitionError('Profile admission source changed')
            if repair_return:
                from ssdlc_feedback_return import subject as return_subject, validate_proof
                reviewed = recipe['subject']
                if (return_subject(source, source['plan'], reviewed['reason'], reviewed['request_id'],
                                   reviewed['repair_files'], operator=reviewed['operator']) != reviewed
                        or digest(recipe['repair_return']) != proposal['repair_return_sha256']):
                    raise TransitionError('Repair-return source or feedback decision changed')
                validate_proof(recipe['repair_return'])
            if (source['parent_selector'] != proposal['parent_selector'] or source['files'] != proposal['source_files']
                    or str(source['root']) != recipe['source_root'] or source['worktree_sha256'] != recipe['source_worktree_sha256']):
                raise TransitionError('Parent generation changed after amendment review')
            for ref, sha in proposal['artifacts'].items():
                if file_hash(_regular(preparation, ref)) != sha: raise TransitionError('Prepared amendment changed')
            runtime = ticket / 'execution-runtimes' / proposal['transition_id']
            seal = _read(preparation, 'runtime-seal.json')
            if _files(runtime) != seal: raise TransitionError('Prepared runtime changed')
            envelope, counters = LimitsStore(runtime, clock)._load(repair=False)
            if (profile_admission or operator_recovery or verification_repair) and digest(envelope['policy']) != proposal['profile_sha256']:
                raise TransitionError('Admitted feedback profile changed')
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
            if delegated:
                delegation.check()
            if repair_delegated:
                repair_session.check()
            result = store.publish(source['root'], proposal, authorization, source['projection'], projected, artifacts)
            if verification_repair:
                from ssdlc_verification_repair import finalize as finalize_verification
                fault('verification_repair_selector_published')
                finalize_verification(ticket, proposal, recipe, result)
                fault('verification_repair_reconciled')
            if 'owner_feedback_sha256' in proposal:
                from ssdlc_owner_revalidation import finalize as finalize_owners
                fault('owner_selector_published')
                finalize_owners(ticket, proposal, recipe, result)
                fault('owner_feedback_reconciled')
            if delegated:
                finalize(delegation, proposal, recipe, result, fault=fault)
            if repair_return:
                from ssdlc_feedback_return import finalize as finalize_return
                fault('repair_return_selector_published')
                finalize_return(ticket, proposal, recipe, result)
                fault('repair_return_reconciled')
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
            command.add_argument('--feedback-request', help='Bind an existing owner-scope request without repeating inference')
            command.add_argument('--reconcile-feedback-evidence', action='store_true',
                help='Explicitly review changed registered evidence with identical plan/task/capture/counters/worktree')
            command.add_argument('--operator-owner-limit', type=int,
                help='Exact next manual owner amendment for this task; does not extend automatic/global/file allowances')
            command.add_argument('--operator-recovery-profile',
                help='Exact operator-reviewed next-attempt/scope profile; money, clock and invocation caps cannot change')
            command.add_argument('--verification-repair-quality',
                help='Exact quality contract with longer observed timed-out checks; requires a separate operator capability')
            command.add_argument('--verification-repair-profile',
                help='Exact profile granting one repair attempt; all other limits remain unchanged')
            command.add_argument('--verification-repair-deadline', type=float,
                help='Exact future Unix deadline; uses a separate explicit deadline-extension capability')
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
        verification = None
        if args.command.endswith(('preview', 'prepare')):
            if bool(args.verification_repair_quality) != bool(args.verification_repair_profile):
                raise TransitionError('Verification repair requires both quality and profile')
            if args.verification_repair_quality:
                if args.feedback_request or args.operator_owner_limit is not None or args.operator_recovery_profile or args.reconcile_feedback_evidence:
                    raise TransitionError('Verification repair cannot borrow owner-feedback authorization')
                verification = dict(quality=read(args.verification_repair_quality), profile=read(args.verification_repair_profile))
                if args.verification_repair_deadline is not None:
                    verification['deadline_at'] = args.verification_repair_deadline
            elif args.verification_repair_deadline is not None:
                raise TransitionError('Deadline extension requires the explicit verification repair quality and profile')
        if args.command in {'continuation-scope-inspect', 'continuation-scope-preview'}:
            with ticket_lock(ticket / 'ticket.lock'):
                source = inspect(ticket, verification_repair=verification is not None)
                with ticket_lock(source['root'] / '03-dev/tasks/scheduler.lock'):
                    if args.command.endswith('preview'):
                        subject_builder = subject
                        extra = {}
                        if args.operator_recovery_profile:
                            from ssdlc_operator_recovery import subject as subject_builder
                            extra['profile'] = read(args.operator_recovery_profile)
                        if verification is not None:
                            from ssdlc_verification_repair import subject as verification_subject
                            value = verification_subject(source, read(args.plan), verification['quality'], verification['profile'], args.reason,
                                                         deadline_at=verification.get('deadline_at'))
                        else:
                            value = subject_builder(source, read(args.plan), args.reason, feedback_request_id=args.feedback_request,
                                            reconcile_feedback_evidence=args.reconcile_feedback_evidence,
                                            operator_owner_limit=args.operator_owner_limit, **extra)
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
                                 runtime_identity=runtime_identity, controller_snapshot=snapshot, guard=guard,
                                 feedback_request_id=args.feedback_request,
                                 reconcile_feedback_evidence=args.reconcile_feedback_evidence,
                                 operator_owner_limit=args.operator_owner_limit,
                                 operator_recovery_profile=read(args.operator_recovery_profile) if args.operator_recovery_profile else None,
                                 verification_repair=verification)
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
