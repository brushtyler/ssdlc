"""Read-only legacy-run inventory and explicit continuation preparation.

No provider is called here. Historical producer metadata is never reconstructed
from a model's completion prose. The journal, not tasks-state.json, is authority.
"""
import copy
import hashlib
import json
from pathlib import Path
import uuid

from ssdlc_tasks import TaskError, TaskJournal, TaskScheduler, TaskPlanValidator, atomic_json, digest, file_hash, git, ticket_lock, test_suite_is_empty, validate_document, confined
from ssdlc_transitions import TransitionError, TransitionStore, _regular, runtime_root, source_binding


STOPPED = {'ESCALATED_TO_SUPERVISOR', 'HALTED_BUDGET_EXCEEDED', 'FAILED', 'AWAITING_SUPERVISOR_SIGNOFF'}


def inspect_source(root):
    """Validate a sealed, stopped task run without repairing any source file."""
    root = Path(root)
    state_path = _regular(root, 'state.json')
    state = json.loads(state_path.read_text())
    if state.get('status') not in STOPPED or not state.get('task_breakdown'):
        raise TransitionError('Continuation requires a stopped task-mode run')
    journal_path = _regular(root, '03-dev/tasks/task-journal.jsonl')
    raw = journal_path.read_bytes()
    if not raw.endswith(b'\n'):
        raise TransitionError('Interrupted journal tail requires explicit reconciliation first')
    journal, sequence, last = TaskJournal._replay(raw)
    if journal is None:
        raise TransitionError('Missing authoritative task state')
    refs = ['state.json', '03-dev/tasks/task-journal.jsonl']
    for name, meta in state.get('artifacts', {}).items():
        path = _regular(root, meta['path'])
        if file_hash(path) != meta['sha256']:
            raise TransitionError('Registered source evidence changed: ' + name)
        refs.append(meta['path'])
    required_refs = ['02-design/plan.json', '00-recon/quality-contract.json', '01-po/requirements.md',
                     '00-recon/controller-contract.json']
    required_refs.extend(ref for ref in ('02-design/adr.md', '02-design/threat-model.md') if (root / ref).exists())
    for ref in required_refs:
        if state.get('artifacts', {}).get(ref, {}).get('path') != ref:
            raise TransitionError('Unregistered upstream input: ' + ref)
    controller_ref = '00-recon/controller-contract.json'
    if controller_ref not in state.get('artifacts', {}):
        raise TransitionError('No registered historical controller identity')
    plan = json.loads(_regular(root, '02-design/plan.json').read_text())
    quality = json.loads(_regular(root, '00-recon/quality-contract.json').read_text())
    if any(obj.get('schema_version') != '1.0' for obj in (plan, quality, journal)):
        raise TransitionError('Unsupported historical schema; explicit compatibility rule required')
    validate_document('plan', plan)
    validate_document('quality-contract', quality)
    if len({t['task_id'] for t in plan['tasks']}) != len(plan['tasks']):
        raise TransitionError('Ambiguous historical task identities')
    if plan['base_commit'] != state['base_commit'] or plan['ticket_id'] != state['ticket_id']:
        raise TransitionError('Historical plan identity mismatch')
    context = {}
    for ref in ('01-po/requirements.md', '02-design/adr.md', '02-design/threat-model.md'):
        path = root / ref
        context[ref] = _regular(root, ref).read_text() if path.exists() else ''
    if journal['inputs_sha256'] != digest({'plan': plan, 'quality': quality, 'context': context}):
        raise TransitionError('Historical scheduler input binding changed')
    if set(journal['tasks']) != {t['task_id'] for t in plan['tasks']}:
        raise TransitionError('Historical plan and journal task sets differ')
    for tid, record in journal['tasks'].items():
        for attempt in confined(root / '03-dev/tasks', tid, protect=False).glob('attempt-*'):
            if attempt.is_symlink():
                raise TransitionError('Symlink in historical attempts')
            # Available logs/manifests from every charged attempt are historical,
            # including earlier failures; disposable source trees are not proofs.
            suffix = attempt.name.removeprefix('attempt-')
            if not suffix.isdigit() or not 1 <= int(suffix) <= record.get('attempts', 0):
                continue
            for path in attempt.iterdir():
                if path.name in ('manifest.json', 'implementation.diff') or path.suffix == '.log':
                    refs.append(path.relative_to(root).as_posix())
    counters = {}
    for ref in refs:
        if ref.endswith('/session.json') and ref.startswith('02-design/'):
            # Preserve complete design/planning sessions, not a guessed count.
            counters[ref] = json.loads(_regular(root, ref).read_text())
    facts = {'ledger': copy.deepcopy(state.get('ledger', [])),
             'limits': copy.deepcopy(state.get('frozen_limits')),
             'counters': counters, 'tasks': copy.deepcopy(journal['tasks'])}
    binding = source_binding(root, refs)
    return {'schema_version': 1, 'source_files': binding, 'facts': facts,
            'state': state, 'journal': journal, 'plan': plan, 'quality': quality,
            'context': context, 'journal_sequence': sequence, 'journal_last_sha256': last,
            'old_controller_sha256': state['artifacts'][controller_ref]['sha256'],
            'execution_authorized': False}


def inspect_stopped_source(root):
    """Fence cooperating controllers while collecting a consistent inventory."""
    root = Path(root)
    with ticket_lock(root / 'ticket.lock'), ticket_lock(root / '03-dev/tasks/scheduler.lock'):
        first = inspect_source(root)
        if source_binding(root, first['source_files']) != first['source_files']:
            raise TransitionError('Source changed during continuation inventory')
        return first


def import_projection(source, *, plan, quality, context, runtime_identity, task_id):
    """Convert a pre-checkpoint run; historical implementation proof is inert."""
    old = source['journal']
    if old.get('candidate_isolation'):
        raise TransitionError('Capture-version migration needs a separate compatibility rule; verification allowances cannot reset')
    if any(t['status'] == 'COMPLETED' for t in old['tasks'].values()):
        raise TransitionError('Completed-checkpoint migration requires a separate compatibility rule')
    if set(old['tasks']) != {t['task_id'] for t in plan['tasks']}:
        raise TransitionError('Execution migration cannot change task identities or reset their allowances')
    record = old['tasks'].get(task_id)
    selected = next((t for t in plan['tasks'] if t['task_id'] == task_id), None)
    if selected and selected['depends_on']:
        raise TransitionError('Pre-checkpoint import requires an independent root task')
    if not record or record.get('attempts', 0) < 1:
        raise TransitionError('Import needs an attributed, already consumed development attempt')
    other_active = [tid for tid, t in old['tasks'].items()
                    if tid != task_id and (t.get('attempts', 0) or t['status'] != 'PENDING')]
    if other_active:
        raise TransitionError('Multiple active candidates need explicit reconciliation')
    result = copy.deepcopy(old)
    result.update(inputs_sha256=digest({'plan': plan, 'quality': quality, 'context': context}),
                  expected_head=plan['base_commit'], candidate_isolation=1,
                  candidate_runtime=runtime_identity)
    result['historical_preflights'] = result.pop('preflights', [])
    result['preflights'] = []
    for tid, t in result['tasks'].items():
        result['tasks'][tid] = {'status': 'PENDING', 'attempts': t['attempts'],
                                'origin_record': t}
    result['tasks'][task_id].update(status='CANDIDATE_CAPTURED', start_commit=plan['base_commit'],
                                    invocation_id=uuid.uuid4().hex, checks=[])
    return result


def impact_report(repo, snapshot_root, manifest):
    from ssdlc_amendments import fixture_impact, changed_symbols
    validate_document('continuation-snapshot', manifest)
    for entry in manifest['source_files']:
        confined(Path(repo), entry['path'])
        if not entry.get('deleted'):
            _regular(Path(snapshot_root), entry['path'])
    return fixture_impact(repo, [e['path'] for e in manifest['source_files']],
                          changed_symbols(repo, snapshot_root, manifest))


def amendment_subject(source, plan, quality, context, manifest, impact):
    return {'schema_version': 1, 'old_plan_sha256': digest(source['plan']),
            'new_plan': plan, 'quality_policy': {k: v for k, v in quality.items() if k != 'baseline'},
            'context_sha256': digest(context), 'snapshot': manifest, 'fixture_impact': impact}


def prepare_generation(source_root, ticket_root, repo, **kwargs):
    """Serialize preparation against both controllers, even for in-place imports."""
    import contextlib
    source_root, ticket_root = Path(source_root).resolve(), Path(ticket_root).resolve()
    locks = {source_root / 'ticket.lock', source_root / '03-dev/tasks/scheduler.lock', ticket_root / 'ticket.lock'}
    with contextlib.ExitStack() as stack:
        for lock in sorted(locks):
            stack.enter_context(ticket_lock(lock))
        if (ticket_root / 'continuations/active.json').exists():
            raise TransitionError('A continuation is already selected')
        return _prepare_generation(source_root, ticket_root, repo, **kwargs)


def _prepare_generation(source_root, ticket_root, repo, *, task_id, plan, quality, context,
                       runtime_identity, controller_snapshot, snapshot_root, snapshot_manifest,
                       review, verifier, fault=lambda point: None):
    """Prepare a reviewable generation without publishing it or calling models.

    A changed source base requires a separately resolved snapshot explicitly
    naming that new base. This function never overlays whole legacy files onto
    newer source and calls that a rebase.
    """
    from ssdlc_amendments import validate_amendment
    from ssdlc_candidates import CandidateStore, checkout, git_identity, git_metadata, inventory, signature, _path
    from ssdlc_tasks import PROTECTED
    source_root, ticket_root, repo = map(Path, (source_root, ticket_root, repo))
    source = inspect_source(source_root)
    validate_document('continuation-snapshot', snapshot_manifest)
    if context != source['context']:
        raise TransitionError('Changed requirements/design need a separate input authorization')
    if quality['base_commit'] != plan['base_commit']:
        raise TransitionError('Quality contract and target plan have different bases')
    if source['state']['ticket_id'] != plan['ticket_id']:
        raise TransitionError('Continuation must preserve ticket identity')
    if git(repo, 'status', '--porcelain'):
        raise TransitionError('Target repository must be clean before preparation')
    if git(repo, 'rev-parse', 'HEAD') != plan['base_commit']:
        raise TransitionError('Target repository does not match the proposed base')
    if snapshot_manifest.get('base_commit') != plan['base_commit']:
        raise TransitionError('Resolved snapshot must explicitly name the new base commit')
    impact = impact_report(repo, snapshot_root, snapshot_manifest)
    subject = amendment_subject(source, plan, quality, context, snapshot_manifest, impact)
    if (not isinstance(review, dict) or set(review) != {'subject_sha256', 'reviewer', 'report'}
            or not isinstance(review['reviewer'], str) or not review['reviewer'].strip()
            or review['subject_sha256'] != digest(subject)):
        raise TransitionError('Independent amendment review does not bind the proposed inputs, snapshot and impact')
    identity = uuid.uuid4().hex
    store_root = ticket_root / 'continuations'
    store_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    preparation = store_root / ('prepared-' + identity)
    preparation.mkdir(mode=0o700)
    runtime = ticket_root / 'runtime-generations' / identity
    runtime.mkdir(parents=True, mode=0o700)
    # Copy only registered upstream inputs. Execution evidence and source
    # snapshots stay historical rather than acquiring a new controller binding.
    state = copy.deepcopy(source['state'])
    state['artifacts'] = {}
    historical = runtime / 'history'
    historical.mkdir()
    for ref in source['source_files']:
        data = _regular(source_root, ref).read_bytes()
        target = historical / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if ref.startswith(('00-recon/', '01-po/', '02-design/')) and ref.endswith('.md'):
            target = runtime / ref
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    # A migration may change the base, not silently weaken baseline obligations.
    def obligations(q):
        return {k: v for k, v in q.items() if k not in ('base_commit', 'baseline')}
    if obligations(quality) != obligations(source['quality']):
        raise TransitionError('Quality policy changes require a separate authorization')
    quality = copy.deepcopy(quality)
    quality['baseline'] = []
    worktree = runtime / 'worktree'
    branch = f"ssdlc/{plan['ticket_id']}/continuation-{identity}"
    git(repo, 'worktree', 'add', '-b', branch, str(worktree.resolve()), plan['base_commit'])
    validate_document('quality-contract', quality)
    validate_document('plan', plan)
    for check in quality['checks']:
        worker = preparation / ('baseline-' + check['check_id'])
        checkout(repo, worker, plan['base_commit'])
        before = inventory(worker)
        metadata = git_identity(worker)
        cwd = confined(worker, check['cwd'])
        if not cwd.is_dir():
            raise TransitionError('Baseline cwd does not exist')
        out, err, code = verifier(check['argv'], cwd, check['timeout_sec'], worker)
        if inventory(worker) != before:
            # Scratch additions are disposable; existing source must be stable.
            after = inventory(worker)
            if any(after.get(p) != entry for p, entry in before.items()):
                raise TransitionError('Baseline verification changed source')
        if git_metadata(worker) != metadata['metadata'] or git_identity(worker) != metadata:
            raise TransitionError('Baseline verification changed checkout metadata')
        if code or test_suite_is_empty(out, err):
            raise TransitionError('New-base baseline failed; generation remains unselected')
        ref = f"00-recon/baseline/continuation-{check['check_id']}.log"
        log = runtime / ref
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(out + '\n' + err)
        quality['baseline'].append({'check_id': check['check_id'], 'exit_code': code,
                                    'log_ref': ref, 'sha256': file_hash(log)})
    validator = TaskPlanValidator(repo, context['01-po/requirements.md'], quality, runtime)
    amendment = validate_amendment(source['plan'], plan, json.dumps(review['report']), validator.validate,
                                   validator.requirement_catalogue['required_ids'], quality,
                                   history=source['facts']['counters'].get('02-design/task-planning/session.json', {}).get('attempts', []))
    scheduler = TaskScheduler(worktree, runtime / '03-dev/tasks', plan, quality, context,
                              lambda *a: (_ for _ in ()).throw(TransitionError('No model dispatch in I1')),
                              verifier, isolate_candidates=True, runtime_identity=runtime_identity)
    task_state = import_projection(source, plan=plan, quality=quality, context=context,
                                   runtime_identity=runtime_identity, task_id=task_id)
    task_state['ignored_paths'] = scheduler._ignored()
    scheduler.state = task_state
    record = task_state['tasks'][task_id]
    record['protected_snapshot'] = scheduler._protected()
    task = next(t for t in plan['tasks'] if t['task_id'] == task_id)
    attempt_dir = scheduler._attempt_directory(task, record)
    candidate = attempt_dir / 'developer-workspace'
    checkout(repo, candidate, plan['base_commit'])
    initial, metadata = inventory(candidate), git_identity(candidate)
    names = []
    for entry in snapshot_manifest['source_files']:
        name = entry['path']
        if name in names or name not in task['target_files']:
            raise TransitionError('Duplicate or out-of-scope snapshot path')
        if entry.get('deleted') is True:
            path = _path(candidate, name)
            if path.exists():
                path.unlink()
            names.append(name)
            continue
        path = _regular(Path(snapshot_root), name)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry['sha256'] or type(entry.get('executable')) is not bool:
            raise TransitionError('Snapshot changed or exceeds approved scope')
        target = _path(candidate, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if entry['executable'] else 0o644)
        names.append(name)
    recipe = {'schema_version': 1, 'compatibility': 'pre-checkpoint-import-v1',
              'invalidated_proofs': ['historical baseline', 'historical task checks', 'historical global gates'],
              'transition_id': identity, 'source_root': str(source_root.resolve()),
              'source_files': source['source_files'],
              'plan_sha256': digest(plan), 'quality_sha256': digest(quality),
              'snapshot': snapshot_manifest, 'runtime_identity': runtime_identity}
    recipe_sha = digest(recipe)
    record['import_proposal_sha256'] = recipe_sha
    producer = {'output': '', 'exit_code': None, 'engine': None, 'model': None,
                'quiescent': True, 'termination': 'imported', 'origin_proposal_sha256': recipe_sha}
    capture = CandidateStore(attempt_dir)
    receipt = capture.capture(candidate, initial, initial, metadata, scheduler._capture_binding(task, record),
                              producer, set(task['target_files']), PROTECTED)
    if receipt['issues']:
        raise TransitionError('Imported snapshot has unsafe or out-of-scope changes')
    record.update(capture_sha256=signature(receipt), producer=receipt['producer'])
    # Retain the entire original journal prefix; append the conversion instead
    # of inventing a clean history or resetting failed/consumed attempts.
    scheduler.journal.path.write_bytes(_regular(source_root, '03-dev/tasks/task-journal.jsonl').read_bytes())
    scheduler.journal.load()
    scheduler._save('CONTINUATION_IMPORTED')
    state.update(status='CONTINUATION_IMPORTED', current_phase='03-dev',
                 current_step='awaiting-explicit-continuation-activation', base_commit=plan['base_commit'],
                 dev_branch=branch, task_breakdown=True, gate_history=[],
                 continuation={'transition_id': identity, 'model_dispatch_authorized': False,
                               'source_files': source['source_files']})
    state.pop('failure', None)
    state.pop('task_verified_head', None)
    atomic_json(runtime / '00-recon/controller-contract.json', controller_snapshot)
    atomic_json(runtime / '00-recon/quality-contract.json', quality)
    atomic_json(runtime / '02-design/plan.json', plan)
    atomic_json(runtime / '02-design/amendment-review.json', {'review': review, 'validation': amendment, 'subject': subject})
    atomic_json(runtime / '02-design/fixture-impact.json', impact)
    for folder in ('00-recon', '01-po', '02-design'):
        for path in (runtime / folder).rglob('*'):
            if path.is_file():
                ref = path.relative_to(runtime).as_posix()
                state['artifacts'][ref] = {'path': ref, 'sha256': file_hash(path), 'size_bytes': path.stat().st_size}
    atomic_json(runtime / 'state.json', state)
    binding = {'transition_id': identity, 'runtime_identity': runtime_identity,
               'model_dispatch_authorized': False}
    atomic_json(runtime / 'generation.json', binding)
    new_facts = copy.deepcopy(source['facts'])
    new_facts.update(transition_id=identity, runtime_binding=binding)
    proposal = {'schema_version': 1, 'compatibility': 'pre-checkpoint-import-v1', 'transition_id': identity, 'old_controller_sha256': source['old_controller_sha256'],
                'new_controller_sha256': runtime_identity['controller_sha256'],
                'projection_sha256': digest(new_facts), 'source_files': source['source_files'],
                'artifacts': {}}
    atomic_json(preparation / 'recipe.json', recipe)
    proposal['artifacts']['recipe.json'] = file_hash(preparation / 'recipe.json')
    # Prepared runtime bytes are sealed separately; active runtime bytes may
    # later change only through the native journal/verification path.
    seal = source_binding(runtime, [p.relative_to(runtime).as_posix() for p in runtime.rglob('*')
                                   if p.is_file() and not p.is_symlink() and 'worktree' not in p.relative_to(runtime).parts
                                   and 'developer-workspace' not in p.relative_to(runtime).parts])
    atomic_json(preparation / 'runtime-seal.json', seal)
    proposal['artifacts']['runtime-seal.json'] = file_hash(preparation / 'runtime-seal.json')
    validate_document('continuation-proposal', proposal)
    atomic_json(preparation / 'proposal.json', proposal)
    atomic_json(preparation / 'projection.json', new_facts)
    from ssdlc_transitions import sync_tree
    sync_tree(runtime)
    sync_tree(preparation)
    fault('prepared')
    return {'preparation': str(preparation), 'proposal': proposal, 'execution_authorized': False}


def activate_generation(ticket_root, preparation, authorization, *, fault=lambda point: None):
    """Select a fully staged generation under source/target cooperative locks."""
    import contextlib
    preparation, ticket_root = Path(preparation), Path(ticket_root)
    proposal = json.loads(_regular(preparation, 'proposal.json').read_text())
    validate_document('continuation-proposal', proposal)
    recipe = json.loads(_regular(preparation, 'recipe.json').read_text())
    projection = json.loads(_regular(preparation, 'projection.json').read_text())
    source_root = Path(recipe['source_root'])
    runtime = ticket_root / 'runtime-generations' / proposal['transition_id']
    locks = {source_root / 'ticket.lock', source_root / '03-dev/tasks/scheduler.lock', ticket_root / 'ticket.lock'}
    with contextlib.ExitStack() as stack:
        for lock in sorted(locks):
            stack.enter_context(ticket_lock(lock))
        source = inspect_source(source_root)
        if source['source_files'] != proposal['source_files']:
            raise TransitionError('Source changed after preparation')
        # A retry after activation validates immutable history, not mutable
        # verification progress. It cannot select a different generation.
        store = TransitionStore(ticket_root / 'continuations', fault)
        if (store.root / 'active.json').exists():
            selected = store.load()
            if selected != projection:
                raise TransitionError('Another continuation is active')
            from ssdlc_transitions import validate_authorization
            validate_authorization(authorization, proposal)
            return selected
        from ssdlc_transitions import validate_authorization
        validate_authorization(authorization, proposal)
        if file_hash(_regular(preparation, 'recipe.json')) != proposal['artifacts']['recipe.json']:
            raise TransitionError('Prepared recipe changed')
        if file_hash(_regular(preparation, 'runtime-seal.json')) != proposal['artifacts']['runtime-seal.json']:
            raise TransitionError('Prepared runtime seal changed')
        seal = json.loads(_regular(preparation, 'runtime-seal.json').read_text())
        if source_binding(runtime, seal) != seal:
            raise TransitionError('Prepared runtime changed before activation')
        artifacts = {name: _regular(preparation, name).read_bytes() for name in proposal['artifacts']}
        return store.publish(source_root, proposal, authorization, source['facts'], projection, artifacts)


def verify_import(repo, ticket, *, verifier, runtime_identity, controller_guard):
    """Verify captured imports only; pending/reserved tasks never call a model."""
    repo = Path(repo)
    root = runtime_root(repo / '.ssdlc' / ticket)
    if root == repo / '.ssdlc' / ticket:
        raise TransitionError('No activated continuation')
    with ticket_lock(repo / '.ssdlc' / ticket / 'ticket.lock'):
        state = json.loads(_regular(root, 'state.json').read_text())
        plan = json.loads(_regular(root, '02-design/plan.json').read_text())
        quality = json.loads(_regular(root, '00-recon/quality-contract.json').read_text())
        context = {ref: _regular(root, ref).read_text() if (root / ref).exists() else ''
                   for ref in ('01-po/requirements.md', '02-design/adr.md', '02-design/threat-model.md')}
        validator = TaskPlanValidator(repo, context['01-po/requirements.md'], quality, root)
        ordered = validator.validate(plan)
        def guard():
            controller_guard()
            for name, meta in state['artifacts'].items():
                if file_hash(_regular(root, meta['path'])) != meta['sha256']:
                    raise TransitionError('Continuation input artifact changed')
        scheduler = TaskScheduler(root / 'worktree', root / '03-dev/tasks', plan, quality, context,
                                  lambda *a: (_ for _ in ()).throw(TransitionError('Model dispatch forbidden in I1')),
                                  verifier, guard, isolate_candidates=True, runtime_identity=runtime_identity)
        guard()
        try:
            result = scheduler.run(ordered, verify_captured_only=True)
        except Exception as exc:
            state.update(status='CONTINUATION_BLOCKED', current_step='continuation-verification',
                         failure={'type': type(exc).__name__, 'category': getattr(exc, 'category', 'INTEGRITY')})
            atomic_json(root / 'state.json', state)
            raise
        state.pop('failure', None)
        completed = [tid for tid, record in result['tasks'].items() if record['status'] == 'COMPLETED']
        state.update(tasks_status=result['tasks'], current_phase='03-dev',
                     current_step='continuation-verification', status='CONTINUATION_VERIFIED' if completed else 'CONTINUATION_CHECKS_FAILED')
        state.pop('task_verified_head', None)
        atomic_json(root / 'state.json', state)
        return {'status': state['status'], 'completed': completed,
                'pending': [tid for tid in result['tasks'] if tid not in completed],
                'model_dispatches': 0, 'ready_for_signoff': False}


def main(argv=None):
    """Native offline continuation commands; deliberately no run/model adapter."""
    import argparse
    import sys
    from orchestrator import EngineRunner, SSDLCOrchestrator
    from ssdlc_controller import assert_controller, controller_snapshot, execution_contract
    parser = argparse.ArgumentParser(description='Prepare/activate/verify a stopped pre-checkpoint continuation')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('inspect', 'preview', 'prepare', 'activate', 'verify'):
        p = sub.add_parser('continuation-' + command)
        p.add_argument('--repo', '-r', default='.')
        p.add_argument('--ticket', required=True)
        if command in ('inspect', 'preview', 'prepare'):
            p.add_argument('--source', required=True, help='Original ticket artifact directory')
        if command in ('preview', 'prepare'):
            p.add_argument('--task', required=True)
            p.add_argument('--plan', required=True)
            p.add_argument('--quality', required=True)
            p.add_argument('--snapshot', required=True, help='Resolved source root')
            p.add_argument('--manifest', required=True)
            if command == 'prepare':
                p.add_argument('--review', required=True, help='Independent review envelope bound to the preview subject')
        if command == 'activate':
            p.add_argument('--preparation', required=True)
            p.add_argument('--authorization', required=True)
    a = parser.parse_args(argv)
    import re
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', a.ticket) or a.ticket in ('.', '..'):
        parser.error('Invalid ticket')
    repo = Path(a.repo).resolve()
    ticket_root = repo / '.ssdlc' / a.ticket
    read = lambda path: json.loads(Path(path).read_text())
    try:
        if a.command == 'continuation-inspect':
            source = inspect_stopped_source(a.source)
            if source['state']['ticket_id'] != a.ticket:
                raise TransitionError('Ticket identity differs from source')
            # Do not print prompts, ledger contents or private evidence.
            result = {k: source[k] for k in ('journal_sequence', 'journal_last_sha256', 'old_controller_sha256', 'execution_authorized')}
            result['source_sha256'] = digest(source['source_files'])
        elif a.command == 'continuation-preview':
            source = inspect_stopped_source(a.source)
            manifest = read(a.manifest)
            validate_document('continuation-snapshot', manifest)
            subject = amendment_subject(source, read(a.plan), read(a.quality), source['context'], manifest,
                                        impact_report(repo, Path(a.snapshot), manifest))
            result = {'subject_sha256': digest(subject), 'subject': subject, 'execution_authorized': False}
        else:
            if not EngineRunner._should_spawn_role_container():
                raise TransitionError('Continuation requires Docker verification; local fallback is refused')
            controller_root = Path(__file__).resolve().parent
            snapshot = controller_snapshot(controller_root)
            # The environment helper has no constructor/config/provider side effects.
            environment = SSDLCOrchestrator._verification_environment(None, ['python3'])
            if environment.get('mode') != 'role_container' or not environment.get('image_id'):
                raise TransitionError('Cannot bind the verification image')
            identity = {'controller_sha256': digest(execution_contract(snapshot)), 'environment': environment}
            def guard():
                assert_controller(controller_root, snapshot)
                if SSDLCOrchestrator._verification_environment(None, ['python3']) != environment:
                    raise TransitionError('Verification environment changed')
            def verify(argv, cwd, timeout, workspace):
                guard()
                result = EngineRunner.run_sandboxed_cmd(argv, cwd, timeout, verification_worktree=workspace)
                guard()
                return result
            if a.command == 'continuation-prepare':
                source = inspect_stopped_source(a.source)
                plan = read(a.plan)
                if plan['ticket_id'] != a.ticket:
                    raise TransitionError('Target ticket differs from plan')
                result = prepare_generation(a.source, ticket_root, repo, task_id=a.task, plan=plan,
                        quality=read(a.quality), context=source['context'], runtime_identity=identity,
                        controller_snapshot=snapshot, snapshot_root=Path(a.snapshot),
                        snapshot_manifest=read(a.manifest), review=read(a.review), verifier=verify)
                result = {'preparation': result['preparation'], 'transition_id': result['proposal']['transition_id'],
                          'proposal_sha256': digest(result['proposal']), 'execution_authorized': False}
            elif a.command == 'continuation-activate':
                proposal = read(Path(a.preparation) / 'proposal.json')
                if proposal['new_controller_sha256'] != identity['controller_sha256']:
                    raise TransitionError('Prepared controller differs from executing controller')
                recipe = read(Path(a.preparation) / 'recipe.json')
                if recipe['runtime_identity'] != identity:
                    raise TransitionError('Prepared environment differs from executing environment')
                guard()
                projection = activate_generation(ticket_root, a.preparation, read(a.authorization))
                result = {'transition_id': projection['transition_id'], 'model_dispatch_authorized': False}
            else:
                root = runtime_root(ticket_root)
                expected = read(root / '00-recon/controller-contract.json')
                assert_controller(controller_root, expected)
                result = verify_import(repo, a.ticket, verifier=verify, runtime_identity=identity, controller_guard=guard)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (TaskError, OSError, ValueError, KeyError, TypeError) as exc:
        # Bounded refusal: no raw logs, absolute source paths or provider text.
        recorded = False
        try:
            # A refusal is durable without rewriting historical state or
            # creating an active/pending generation. Never persist raw logs.
            atomic_json(ticket_root / 'continuation-refusals' / (uuid.uuid4().hex + '.json'),
                        {'schema_version': 1, 'operation': a.command, 'error_type': type(exc).__name__,
                         'category': getattr(exc, 'category', 'INTEGRITY'), 'model_dispatches': 0})
            recorded = True
        except OSError:
            pass
        print(json.dumps({'status': 'CONTINUATION_REFUSED', 'reason': str(exc)[:240], 'audit_recorded': recorded}), file=sys.stderr)
        return 2
