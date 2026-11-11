"""Explicit cumulative-limit transition from a fenced I1 generation to I2 execution.

Staging never calls a model. Original generations stay immutable. Only an atomic,
hash-bound selector grants the native run-from-03 path permission to dispatch.
"""
import copy
import json
from pathlib import Path
import time
import uuid

from ssdlc_tasks import (DevelopmentBlocked, TaskError, TaskJournal, TaskScheduler, TaskPlanValidator, atomic_json,
                         digest, file_hash, git, ticket_lock, confined, validate_document, test_suite_is_empty)
from ssdlc_transitions import TransitionError, TransitionStore, _regular, source_binding, sync_tree
from ssdlc_recovery import LimitsStore, RecoveryStop
from ssdlc_historical import project_accounting, limit_sources, validate_decisions


ALLOWED_ENTRYPOINTS = ['run_pipeline', 'run_phase_dev', 'run_phase_review', 'run_phase_qa',
                       'run_phase_appsec', 'run_phase_ops', 'run_phase_docs', 'compile_evidence_pack']
ALLOWED_PHASES = ['03-dev', '04-review', '05-qa', '05b-appsec', '06-ops', '07-docs']
CONTEXT = ('01-po/requirements.md', '02-design/adr.md', '02-design/threat-model.md')


def _read(root, ref): return json.loads(_regular(Path(root), ref).read_text())


def i1_root(ticket):
    projection = TransitionStore(Path(ticket) / 'continuations').load()
    root = Path(ticket) / 'runtime-generations' / projection['transition_id']
    if _read(root, 'generation.json') != projection['runtime_binding']:
        raise TransitionError('I1 runtime marker changed')
    return root, projection


def _files(root):
    refs = []
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(part in ('worktree', 'developer-workspace', '.verification') for part in relative.parts): continue
        if path.name.endswith('.lock'): continue
        if path.is_symlink(): raise TransitionError('Unsafe historical runtime entry')
        if path.is_file(): refs.append(relative.as_posix())
    return source_binding(root, refs)


def inspect_i1(ticket):
    """No repairing source journal or fabricating old producer metadata."""
    from ssdlc_candidates import inventory
    root, projection = i1_root(ticket)
    state = _read(root, 'state.json')
    if state.get('status') not in {'CONTINUATION_IMPORTED', 'CONTINUATION_VERIFIED', 'CONTINUATION_CHECKS_FAILED', 'CONTINUATION_BLOCKED'}:
        raise TransitionError('Reconciliation requires a stopped I1 continuation')
    if (root / 'limits/policy.json').exists(): raise TransitionError('An existing I2 profile needs another explicit transition')
    raw = _regular(root, '03-dev/tasks/task-journal.jsonl').read_bytes()
    if not raw.endswith(b'\n'): raise TransitionError('Interrupted source journal requires native reconciliation first')
    journal, sequence, last = TaskJournal._replay(raw)
    plan, quality = _read(root, '02-design/plan.json'), _read(root, '00-recon/quality-contract.json')
    if plan['ticket_id'] != state['ticket_id'] or plan['ticket_id'] != Path(ticket).name:
        raise TransitionError('I1 ticket identity differs from reconciliation target')
    context = {ref: _regular(root, ref).read_text() if (root / ref).exists() else '' for ref in CONTEXT}
    if journal['inputs_sha256'] != digest({'plan': plan, 'quality': quality, 'context': context}):
        raise TransitionError('I1 execution input binding changed')
    for meta in state.get('artifacts', {}).values():
        if file_hash(_regular(root, meta['path'])) != meta['sha256']:
            raise TransitionError('I1 registered input evidence changed')
    if state.get('ledger') != projection['ledger'] or state.get('frozen_limits') != projection['limits']:
        raise TransitionError('I1 ledger/limits differ from preserved source facts')
    for ref, counter in projection['counters'].items():
        if _read(root / 'history', ref) != counter: raise TransitionError('I1 historical counters changed')
    if set(journal['tasks']) != set(projection['tasks']): raise TransitionError('I1 task identities changed')
    for tid, record in journal['tasks'].items():
        if record['attempts'] != projection['tasks'][tid]['attempts']:
            raise TransitionError('I1 consumed attempts changed without development authority')
    captures = [tid for tid, record in journal['tasks'].items() if record.get('capture_sha256')]
    if len(captures) != 1 or any(record['status'] != 'PENDING' for tid, record in journal['tasks'].items() if tid not in captures):
        raise TransitionError('Unsupported I1 execution topology')
    # Older I1 controllers retained failed attempts only inside origin_record.
    # Restore the scheduler-visible prefix from immutable original facts, using
    # archived evidence rather than trusting stale/missing live references.
    history_evidence = {}
    for tid, record in journal['tasks'].items():
        original = projection['tasks'][tid]
        failures = original.get('failed_attempts', [])
        live = record.setdefault('failed_attempts', [])
        restored = []
        if failures:
            for failed in copy.deepcopy(failures):
                for item, ref_key, sha_key in [(failed, 'diff_ref', 'diff_sha256')] + [
                        (check, 'log_ref', 'sha256') for check in failed.get('checks', [])]:
                    if ref_key not in item: continue
                    old_ref = 'history/03-dev/tasks/' + item[ref_key]
                    if file_hash(_regular(root, old_ref)) != item[sha_key]:
                        raise TransitionError('Historical failure evidence changed')
                    new_ref = 'origin-history/' + item[ref_key]
                    history_evidence['03-dev/tasks/' + new_ref] = old_ref
                    item[ref_key] = new_ref
                restored.append(failed)
            if not live:
                live.extend(restored)
            elif live[:len(restored)] != restored:
                raise TransitionError('Partially lost failure history requires explicit reconciliation')
        blockers = [failure['category'] for failure in failures if failure['category'] != 'CHECK_FAILURE']
        if blockers:
            record['reconciliation_required'] = list(dict.fromkeys(record.get('reconciliation_required', []) + blockers))
    worktree = root / 'worktree'
    if git(worktree, 'rev-parse', 'HEAD') != journal['expected_head']:
        raise TransitionError('I1 worktree head changed')
    source = {'root': root, 'projection': projection, 'state': state, 'journal': journal,
              'plan': plan, 'quality': quality, 'context': context, 'sequence': sequence,
              'last_sha256': last, 'files': _files(root), 'worktree_sha256': digest(inventory(worktree)),
              'capture_task': captures[0], 'history_evidence': history_evidence}
    return source


def _authorization(authorization, proposal):
    validate_document('reconciliation-authorization', authorization)
    required = {'schema_version', 'transition_id', 'proposal_sha256', 'profile_sha256', 'decisions_sha256',
                'authorized', 'source_fenced', 'old_controller_sha256', 'new_controller_sha256',
                'accept_unknown_history', 'authorize_extensions'}
    if any(v['added'] for v in proposal.get('scope_diff', {}).values()):
        required.add('scope_diff_sha256')
    if not isinstance(authorization, dict) or set(authorization) != required:
        raise TransitionError('Reconciliation needs exact explicit authorization fields')
    if (type(authorization['schema_version']) is not int or authorization['schema_version'] != 1
            or authorization['authorized'] is not True or authorization['source_fenced'] is not True
            or authorization['transition_id'] != proposal['transition_id']
            or authorization['proposal_sha256'] != digest(proposal)
            or authorization['profile_sha256'] != proposal['profile_sha256']
            or any(authorization[key] != proposal[key] for key in ('old_controller_sha256', 'new_controller_sha256'))):
        raise TransitionError('Reconciliation authorization is stale or incompatible')
    if 'scope_diff_sha256' in required and authorization['scope_diff_sha256'] != digest(proposal['scope_diff']):
        raise TransitionError('Authorization does not bind expanded task scope')
    validate_decisions(authorization, proposal)
    return authorization



def prepare(ticket, repo, request, **kwargs):
    from ssdlc_preparations import unfinished, update, clear_pending
    ticket = Path(ticket).resolve()
    with ticket_lock(ticket / 'ticket.lock'):
        if unfinished(ticket, namespace='execution'):
            raise TransitionError('An earlier reconciliation requires native discard')
        try:
            return _prepare(ticket, repo, request, **kwargs)
        except BaseException as exc:
            for identity in unfinished(ticket, namespace='execution'):
                update(ticket, identity, namespace='execution', status='REJECTED', error_type=type(exc).__name__)
                clear_pending(ticket, identity, namespace='execution')
            raise

def _prepare(ticket, repo, request, *, runtime_identity, controller_snapshot, verifier, guard=lambda: None,
            clock=time.time, fault=lambda point: None):
    """Stage actual native runtime and cumulative journal for operator review."""
    from ssdlc_candidates import CandidateStore, checkout, inventory, git_identity, git_metadata
    from ssdlc_controller import execution_contract
    ticket, repo = Path(ticket).resolve(), Path(repo).resolve()
    validate_document('reconciliation-request', request)
    if (ticket / 'execution/active.json').exists(): raise TransitionError('An execution generation is already selected')
    source = inspect_i1(ticket)
    root = source['root']
    with ticket_lock(root / '03-dev/tasks/scheduler.lock'):
        guard()
        effective, external, payloads = limit_sources(source['projection'], request['limit_sources'])
        profile, initial, decisions, origins = project_accounting(effective, request, state=source['state'], now=clock())
        # The immutable historical facts remain literal, even when external
        # sources supply previously unknown cap provenance.
        initial['historical'] = copy.deepcopy(source['projection'])
        identity = uuid.uuid4().hex
        from ssdlc_preparations import register, update
        register(ticket, repo, root, identity, source['journal']['expected_head'], namespace='execution')
        update(ticket, identity, namespace='execution', branch=f"ssdlc/{source['plan']['ticket_id']}/execution-{identity}")
        prepared = ticket / 'execution' / ('prepared-' + identity)
        prepared.mkdir(parents=True, mode=0o700)
        runtime = ticket / 'execution-runtimes' / identity
        runtime.mkdir(parents=True, mode=0o700)
        for ref in source['files']:
            data = _regular(root, ref).read_bytes()
            destination = runtime / ref
            destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(data)
            archive = runtime / 'history/i1-generation' / ref
            archive.parent.mkdir(parents=True, exist_ok=True); archive.write_bytes(data)
        for destination_ref, source_ref in source['history_evidence'].items():
            destination = runtime / destination_ref
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_regular(root, source_ref).read_bytes())
        for ref, data in payloads.items():
            path = runtime / ref; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        branch = f"ssdlc/{source['plan']['ticket_id']}/execution-{identity}"
        git(repo, 'worktree', 'add', '-b', branch, str(runtime / 'worktree'), source['journal']['expected_head'])
        update(ticket, identity, namespace='execution', worktree_created=True)
        fault('worktree_created')
        quality = copy.deepcopy(source['quality']); quality['baseline'] = []
        for check in quality['checks']:
            worker = prepared / ('baseline-' + check['check_id'])
            checkout(repo, worker, source['plan']['base_commit'])
            before, metadata = inventory(worker), git_identity(worker)
            out, err, code = verifier(check['argv'], confined(worker, check['cwd']), check['timeout_sec'], worker)
            after = inventory(worker)
            if (any(after.get(p) != value for p, value in before.items()) or git_identity(worker) != metadata
                    or git_metadata(worker) != metadata['metadata']):
                raise TransitionError('Reconciliation baseline mutated source/metadata')
            if code or test_suite_is_empty(out, err): raise RecoveryStop('ENVIRONMENT_DIAGNOSIS', 'Fresh baseline failed')
            ref = '00-recon/baseline/reconciliation-' + check['check_id'] + '.log'
            path = runtime / ref; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(out + '\n' + err)
            quality['baseline'].append({'check_id': check['check_id'], 'exit_code': 0, 'log_ref': ref, 'sha256': file_hash(path)})
            guard()
        if runtime_identity['controller_sha256'] != digest(execution_contract(controller_snapshot)):
            raise TransitionError('New controller identity does not match its snapshot')
        old_identity = source['journal']['candidate_runtime']
        old_scheduler = TaskScheduler(root / 'worktree', root / '03-dev/tasks', source['plan'], source['quality'],
            source['context'], None, None, isolate_candidates=True, runtime_identity=old_identity)
        old_scheduler.state = copy.deepcopy(source['journal'])
        bindings = {}
        for tid, record in source['journal']['tasks'].items():
            if not record.get('capture_sha256'): continue
            bindings[tid] = {str(record['attempts']): old_scheduler._capture_binding(source['plan']['tasks'][next(i for i,t in enumerate(source['plan']['tasks']) if t['task_id']==tid)], record)}
            task = next(t for t in source['plan']['tasks'] if t['task_id'] == tid)
            CandidateStore(root / '03-dev/tasks' / tid / ('attempt-' + str(record['attempts']))).load(bindings[tid][str(record['attempts'])], record['capture_sha256'])
            for failed in record.get('failed_attempts', []):
                if failed.get('capture_sha256'):
                    old_record = dict(record, attempts=failed['attempt'], start_commit=failed['base_commit'], invocation_id=failed['invocation_id'])
                    bindings[tid][str(failed['attempt'])] = old_scheduler._capture_binding(task, old_record)
        current = copy.deepcopy(source['journal'])
        current.update(candidate_runtime=runtime_identity, bounded_recovery=1, attempt_limit=profile['development_attempts'],
                       historical_capture_bindings=bindings,
                       inputs_sha256=digest({'plan': source['plan'], 'quality': quality, 'context': source['context']}))
        current['historical_preflights'] = current.get('historical_preflights', []) + current.get('preflights', [])
        current['preflights'] = []
        binding = {'ticket': source['plan']['ticket_id'], 'base_commit': source['plan']['base_commit'],
                   'controller': controller_snapshot, 'reconciliation_id': identity,
                   'historical_capture_bindings_sha256': digest(bindings),
                   'historical_sha256': digest(initial['historical']),
                   'accounting_anchor': {key: copy.deepcopy(initial[key]) for key in
                        ('started_at', 'deadline_at', 'combined_reported_usd_cap', 'diagnoses')},
                   'historical_invocations': copy.deepcopy(initial['invocations']),
                   'task_floors': {tid: {key: copy.deepcopy(row[key]) for key in
                        ('attempts', 'failed_attempts', 'reconciliation_required', 'capture_verifications') if key in row}
                        for tid, row in current['tasks'].items()},
                   'allowed_entrypoints': ALLOWED_ENTRYPOINTS, 'allowed_phases': ALLOWED_PHASES}
        envelope = {'policy': profile, 'binding': binding}
        initial['policy_sha256'] = digest(envelope)
        store = LimitsStore(runtime, clock)
        atomic_json(store.policy_path, envelope); store.journal.append('LIMITS_RECONCILED', initial)
        atomic_json(runtime / '00-recon/quality-contract.json', quality)
        atomic_json(runtime / '00-recon/controller-contract.json', controller_snapshot)
        outer = copy.deepcopy(source['state'])
        outer.update(status='RECONCILIATION_PREPARED', current_phase='03-dev', current_step='awaiting-exact-authorization',
                     dev_branch=branch, frozen_limits=profile, gate_history=[],
                     execution_reconciliation={'transition_id': identity, 'source_generation': source['projection']['transition_id']})
        outer.pop('task_verified_head', None); outer.pop('failure', None); outer.pop('po_validation_ref', None)
        for ref in ('00-recon/quality-contract.json', '00-recon/controller-contract.json'):
            outer['artifacts'][ref] = {'path': ref, 'sha256': file_hash(runtime / ref)}
        for baseline in quality['baseline']:
            outer['artifacts'][baseline['log_ref']] = {'path': baseline['log_ref'], 'sha256': baseline['sha256']}
        atomic_json(runtime / 'state.json', outer)
        def verify_guard():
            guard()
            for meta in outer['artifacts'].values():
                if file_hash(_regular(runtime, meta['path'])) != meta['sha256']:
                    raise TransitionError('Reconciliation input changed')
        def budget():
            data, counters = store._load(repair=False)
            store.check(counters, data['policy'], for_dispatch=False)
        scheduler = TaskScheduler(runtime / 'worktree', runtime / '03-dev/tasks', source['plan'], quality, source['context'],
            lambda *a: (_ for _ in ()).throw(TransitionError('No models during reconciliation')),
            verifier, verify_guard, budget, max_attempts=profile['development_attempts'], isolate_candidates=True,
            runtime_identity=runtime_identity, recover_timeouts=True, historical_capture_authority=digest(bindings), historical_task_floors=binding['task_floors'])
        scheduler.state = current
        current['ignored_paths'] = scheduler._ignored()
        for record in current['tasks'].values():
            if 'protected_snapshot' in record: record['protected_snapshot'] = scheduler._protected()
        scheduler.journal.load(); scheduler._save('ACCOUNTING_RECONCILIATION_STAGED')
        task_id = source['capture_task']; record = current['tasks'][task_id]
        task = next(t for t in source['plan']['tasks'] if t['task_id'] == task_id)
        if record['status'] == 'COMPLETED':
            if not old_scheduler._evidence_valid(source['journal']['tasks'][task_id], task_id):
                raise TransitionError('Historical checkpoint evidence changed')
            runs = record.setdefault('capture_verifications', [])
            if len([r for r in runs if r['attempt'] == record['attempts']]) >= 3:
                raise RecoveryStop('LIMIT_REACHED', 'Capture verification allowance exhausted')
            ref = task_id + '/reconciliation-' + identity
            runs.append({'attempt': record['attempts'], 'dir': ref})
            directory = runtime / '03-dev/tasks' / ref; directory.mkdir()
            record['historical_checks'] = copy.deepcopy(record['checks'])
            scheduler._save('RECONCILIATION_CHECK_RESERVED')
            results = scheduler._verify(task, record, directory, exclusive=True)
            if any(r['exit_code'] or r['empty_suite'] for r in results):
                raise RecoveryStop('ENVIRONMENT_DIAGNOSIS', 'Fresh checkpoint checks failed; historical proof cannot authorize execution')
            scheduler._save('RECONCILIATION_CHECKPOINT_VERIFIED')
        else:
            record['status'] = 'CANDIDATE_CAPTURED'
            scheduler._save('RECONCILIATION_CAPTURE_VERIFICATION_RESERVED')
            scheduler.run(TaskPlanValidator(repo, source['context']['01-po/requirements.md'], quality, runtime).validate(source['plan']), verify_captured_only=True)
        for tid, row in scheduler.state['tasks'].items():
            if row.get('reconciliation_required'):
                raise DevelopmentBlocked(row['reconciliation_required'][-1], 'Historical recovery gate remains active')
        # Include verification and failure reservations consumed while staging:
        # those allowances also survive activation, not just the source floors.
        binding['task_floors'] = {tid: {key: copy.deepcopy(row[key]) for key in
            ('attempts', 'failed_attempts', 'reconciliation_required', 'capture_verifications') if key in row}
            for tid, row in scheduler.state['tasks'].items()}
        initial['policy_sha256'] = digest(envelope)
        atomic_json(store.policy_path, envelope)
        store.journal.append('RECONCILIATION_VERIFICATION_ACCOUNTED', initial)
        outer['tasks_status'] = copy.deepcopy(scheduler.state['tasks'])
        outer.update(status='RECONCILIATION_READY', current_step='awaiting-exact-authorization')
        store._publish_mode(envelope, outer)
        atomic_json(runtime / 'state.json', outer)
        update(ticket, identity, namespace='execution', staged_head=scheduler.state['expected_head'])
        runtime_binding = {'transition_id': identity, 'source_generation': source['projection']['transition_id'],
                           'runtime_identity': runtime_identity, 'policy_sha256': digest(envelope),
                           'model_dispatch_authorized': True, 'entrypoint': 'run', 'from_phase': '03-dev'}
        atomic_json(runtime / 'generation.json', runtime_binding)
        projected = copy.deepcopy(source['projection'])
        projected.update(transition_id=identity, runtime_binding=runtime_binding)
        old_snapshot = _read(root, '00-recon/controller-contract.json')
        from ssdlc_amendments import scope_diff
        scope = scope_diff(_read(root, 'history/02-design/plan.json'), source['plan'])
        recipe = {'schema_version': 1, 'source_root': str(root), 'source_files': source['files'],
                  'source_worktree_sha256': source['worktree_sha256'],
                  'runtime_worktree_sha256': digest(inventory(runtime / 'worktree')), 'request': request,
                  'limit_sources': external, 'runtime_identity': runtime_identity, 'initial_limits': initial,
                  'historical_capture_bindings': bindings, 'scope_diff': scope}
        atomic_json(prepared / 'recipe.json', recipe)
        atomic_json(prepared / 'projection.json', projected)
        seal = _files(runtime); atomic_json(prepared / 'runtime-seal.json', seal)
        proposal = {'schema_version': 1, 'compatibility': 'i1-cumulative-limits-v1', 'transition_id': identity,
                    'old_controller_sha256': digest(execution_contract(old_snapshot)),
                    'new_controller_sha256': runtime_identity['controller_sha256'],
                    'controller_identity_scheme': 'execution-contract-json-sha256',
                    'historical_controller_artifact_sha256': file_hash(root / '00-recon/controller-contract.json'),
                    'source_files': source['files'], 'projection_sha256': digest(projected),
                    'profile_sha256': digest(profile), 'decisions': decisions, 'scope_diff': scope, 'authorization_contract': 'cumulative-limits-v1',
                    'artifacts': {'recipe.json': file_hash(prepared / 'recipe.json'), 'runtime-seal.json': file_hash(prepared / 'runtime-seal.json')}}
        validate_document('reconciliation-proposal', proposal)
        atomic_json(prepared / 'proposal.json', proposal)
        sync_tree(runtime); sync_tree(prepared)
        from ssdlc_preparations import pending
        pending(ticket, identity, namespace='execution')
        fault('prepared')
        return {'preparation': str(prepared), 'proposal': proposal, 'model_dispatches': 0}


def validate_execution_projection(source, runtime, proposal, recipe):
    """Validate the journals and scope that native readers will actually consume."""
    from ssdlc_candidates import inventory
    outer = _read(runtime, 'state.json')
    tasks = TaskJournal(runtime / '03-dev/tasks').peek()
    envelope, counters = LimitsStore(runtime)._load(repair=False)
    initial = recipe['initial_limits']
    if counters != initial or digest(envelope['policy']) != proposal['profile_sha256']:
        raise TransitionError('Effective native limit counters differ from the reviewed projection')
    if outer['ledger'] != source['projection']['ledger'] or outer['frozen_limits'] != envelope['policy']:
        raise TransitionError('Executed ledger/limits do not preserve reviewed historical accounting')
    if counters['historical'] != source['projection']:
        raise TransitionError('Native historical counter projection differs from source')
    if set(tasks['tasks']) != set(source['journal']['tasks']): raise TransitionError('Runtime task set changed')
    for tid, old in source['journal']['tasks'].items():
        row = tasks['tasks'][tid]
        if row['attempts'] != old['attempts']:
            raise TransitionError('Runtime resets development attempts')
        previous = old.get('failed_attempts', [])
        if row.get('failed_attempts', [])[:len(previous)] != previous:
            raise TransitionError('Runtime resets failure history/stagnation semantics')
        for key in ('capture_verifications',):
            prefix = old.get(key, [])
            if row.get(key, [])[:len(prefix)] != prefix:
                raise TransitionError('Runtime resets capture verification allowance')
        if old.get('reconciliation_required') and row.get('reconciliation_required') != old['reconciliation_required']:
            raise TransitionError('Runtime drops an earlier reconciliation gate')
    if tasks['historical_capture_bindings'] != recipe['historical_capture_bindings']:
        raise TransitionError('Runtime capture provenance changed')
    if (digest(inventory(runtime / 'worktree')) != recipe['runtime_worktree_sha256']
            or git(runtime / 'worktree', 'status', '--porcelain')):
        raise TransitionError('Prepared runtime source changed before activation')
    if git(runtime / 'worktree', 'rev-parse', 'HEAD') != tasks['expected_head']:
        raise TransitionError('Runtime checkpoint changed')
    return envelope, counters, tasks


def activate(ticket, preparation, authorization, *, guard=lambda: None, clock=time.time,
             fault=lambda point: None):
    """No model dispatch before the fully materialized execution selector exists."""
    from ssdlc_candidates import inventory
    ticket, preparation = Path(ticket).resolve(), Path(preparation).resolve()
    proposal, recipe = _read(preparation, 'proposal.json'), _read(preparation, 'recipe.json')
    if preparation != ticket / 'execution' / ('prepared-' + proposal['transition_id']):
        raise TransitionError('Activation requires the recorded preparation path')
    validate_document('reconciliation-proposal', proposal)
    normalized = _authorization(authorization, proposal)
    projection = _read(preparation, 'projection.json')
    runtime = ticket / 'execution-runtimes' / proposal['transition_id']
    store = TransitionStore(ticket / 'execution', fault)
    with ticket_lock(ticket / 'ticket.lock'):
        guard()
        if (store.root / 'active.json').exists():
            selected = store.load()
            if selected != projection: raise TransitionError('Another execution generation is already selected')
            resolve_execution(ticket)
            from ssdlc_preparations import update, clear_pending
            update(ticket, proposal['transition_id'], namespace='execution', status='ACTIVE')
            clear_pending(ticket, proposal['transition_id'], namespace='execution')
            return selected
        source = inspect_i1(ticket)
        with ticket_lock(source['root'] / '03-dev/tasks/scheduler.lock'):
            if (source['files'] != proposal['source_files'] or source['worktree_sha256'] != recipe['source_worktree_sha256']
                    or str(source['root']) != recipe['source_root']):
                raise TransitionError('Reconciliation source changed after preparation')
            for info in recipe['limit_sources']:
                path = Path(info['path']).absolute()
                if file_hash(_regular(path.parent, path.name)) != info['sha256']:
                    raise TransitionError('Historical limit source changed after authorization')
            for name, sha in proposal['artifacts'].items():
                if file_hash(_regular(preparation, name)) != sha:
                    raise TransitionError('Prepared reconciliation artifact changed')
            seal = _read(preparation, 'runtime-seal.json')
            if source_binding(runtime, seal) != seal: raise TransitionError('Prepared execution runtime changed')
            envelope, counters, tasks = validate_execution_projection(source, runtime, proposal, recipe)
            limits = LimitsStore(runtime, clock)
            limits.check(counters, envelope['policy'])
            needed = '03-dev' if any(t['status'] != 'COMPLETED' for t in tasks['tasks'].values()) else '04-review'
            limits.require_capacity(needed, needed)
            for record in tasks['tasks'].values():
                if record['status'] != 'COMPLETED' and record['attempts'] >= envelope['policy']['development_attempts']:
                    raise RecoveryStop('LIMIT_REACHED', 'Historical task attempts exhausted; explicit extension required')
            artifacts = {name: _regular(preparation, name).read_bytes() for name in proposal['artifacts']}
            result = store.publish(source['root'], proposal, normalized, source['projection'], projection, artifacts)
            from ssdlc_preparations import update, clear_pending
            update(ticket, proposal['transition_id'], namespace='execution', status='ACTIVE')
            clear_pending(ticket, proposal['transition_id'], namespace='execution')
            return result


def resolve_execution(ticket):
    """Read-only resolver shared by orchestrator, CLI, status and supervisor."""
    ticket = Path(ticket)
    store = TransitionStore(ticket / 'execution')
    projection = store.load()
    identity = projection['transition_id']
    root = ticket / 'execution-runtimes' / identity
    marker = _read(root, 'generation.json')
    if marker != projection['runtime_binding'] or marker.get('model_dispatch_authorized') is not True:
        raise TransitionError('Execution generation binding changed')
    envelope, counters = LimitsStore(root)._load(repair=False)
    if digest(envelope) != marker['policy_sha256']:
        raise TransitionError('Execution policy differs from authorized generation')
    state = _read(root, 'state.json')
    if state['ledger'][:len(projection['ledger'])] != projection['ledger']:
        raise TransitionError('Executed state lost its historical ledger prefix')
    return root


def authorized(ticket, root):
    ticket, root = Path(ticket), Path(root)
    if not (ticket / 'execution/active.json').exists(): return False
    return resolve_execution(ticket) == root


def main(argv=None):
    """Offline admission commands; executing development remains the native run CLI."""
    import argparse
    import re
    import sys
    from orchestrator import EngineRunner, SSDLCOrchestrator
    from ssdlc_controller import assert_controller, controller_snapshot, execution_contract
    parser = argparse.ArgumentParser(description='Reconcile a stopped I1 generation with cumulative I2 limits')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'prepare', 'activate', 'discard'):
        command = commands.add_parser('continuation-limits-' + name)
        command.add_argument('--repo', '-r', default='.')
        command.add_argument('--ticket', required=True)
        if name == 'prepare': command.add_argument('--request', required=True)
        if name in ('activate', 'discard'): command.add_argument('--preparation', required=True)
        if name == 'activate': command.add_argument('--authorization', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.ticket) or args.ticket in ('.', '..'):
        parser.error('Invalid ticket')
    repo = Path(args.repo).resolve()
    ticket = repo / '.ssdlc' / args.ticket
    read = lambda path: json.loads(Path(path).read_text())
    try:
        if args.command == 'continuation-limits-inspect':
            source = inspect_i1(ticket)
            # Hashes and accounting descriptors, never raw prompts or transcripts.
            result = {'source_generation': source['projection']['transition_id'],
                      'source_sha256': digest(source['files']), 'journal_sequence': source['sequence'],
                      'journal_sha256': source['last_sha256'], 'limits': source['projection']['limits'],
                      'ledger': [{'index': i, 'sha256': digest(row), 'engine': row.get('engine'),
                                  'cost_usd': row.get('cost_usd')} for i, row in enumerate(source['projection']['ledger'])],
                      'counters': {ref: {'sha256': digest(counter),
                          'rounds': len(counter['rounds']) if isinstance(counter.get('rounds'), list) else None,
                          'attempts': len(counter['attempts']) if isinstance(counter.get('attempts'), list) else None,
                          'used': counter.get('used')} for ref, counter in source['projection']['counters'].items()},
                      'task_attempts': {tid: row['attempts'] for tid, row in source['journal']['tasks'].items()},
                      'model_dispatches': 0}
        elif args.command == 'continuation-limits-discard':
            from ssdlc_preparations import discard, docker_quiescent
            identity = Path(args.preparation).name.removeprefix('prepared-')
            expected = ticket / 'execution' / ('prepared-' + identity)
            if Path(args.preparation).resolve() != expected.resolve():
                raise TransitionError('Discard requires the recorded preparation path')
            record = discard(ticket, identity, namespace='execution', writers_quiescent=docker_quiescent)
            result = {'status': record['status'], 'transition_id': identity, 'model_dispatches': 0}
        else:
            if not EngineRunner._should_spawn_role_container():
                raise TransitionError('Reconciliation requires Docker verification; local fallback is refused')
            controller = Path(__file__).resolve().parent
            snapshot = controller_snapshot(controller)
            environment = SSDLCOrchestrator._verification_environment(None, ['python3'])
            if environment.get('mode') != 'role_container' or not environment.get('image_id'):
                raise TransitionError('Cannot bind the verification image')
            identity = {'controller_sha256': digest(execution_contract(snapshot)), 'environment': environment}
            def guard():
                from ssdlc_preparations import docker_quiescent
                source, _ = i1_root(ticket)
                if not docker_quiescent({'ticket_root': str(ticket), 'runtime': 'execution-runtimes',
                        'preparation': 'execution', 'source_root': str(source)}):
                    raise TransitionError('Historical/staged writers are not proven quiescent')
                assert_controller(controller, snapshot)
                if SSDLCOrchestrator._verification_environment(None, ['python3']) != environment:
                    raise TransitionError('Verification environment changed')
            def verify(argv, cwd, timeout, workspace):
                guard()
                result = EngineRunner.run_sandboxed_cmd(argv, cwd, timeout, verification_worktree=workspace)
                guard()
                return result
            if args.command == 'continuation-limits-prepare':
                result = prepare(ticket, repo, read(args.request), runtime_identity=identity,
                                 controller_snapshot=snapshot, verifier=verify, guard=guard)
                result = {'preparation': result['preparation'], 'transition_id': result['proposal']['transition_id'],
                          'proposal_sha256': digest(result['proposal']), 'execution_authorized': False,
                          'model_dispatches': 0}
            else:
                preparation = Path(args.preparation)
                expected = ticket / 'execution' / preparation.name
                if preparation.resolve() != expected.resolve():
                    raise TransitionError('Activation requires the recorded preparation path')
                recipe = read(preparation / 'recipe.json')
                if recipe['runtime_identity'] != identity:
                    raise TransitionError('Prepared controller/environment differs from executing runtime')
                selected = activate(ticket, preparation, read(args.authorization), guard=guard)
                result = {'transition_id': selected['transition_id'], 'model_dispatch_authorized': True,
                          'model_dispatches': 0, 'ready_for_signoff': False}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (TaskError, OSError, ValueError, KeyError, TypeError) as exc:
        atomic_json(ticket / 'reconciliation-refusals' / (uuid.uuid4().hex + '.json'),
                    {'schema_version': 1, 'operation': args.command, 'error_type': type(exc).__name__,
                     'category': getattr(exc, 'category', 'INTEGRITY'), 'model_dispatches': 0})
        print(json.dumps({'status': 'RECONCILIATION_REFUSED', 'reason': str(exc)[:240]}), file=sys.stderr)
        return 2
