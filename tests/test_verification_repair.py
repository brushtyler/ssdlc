"""Timeout repair uses real captured attempts, cumulative journals and owners."""
import copy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_reconciliation as fixtures
from ssdlc_controller import execution_contract
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import inspect, prepare, activate
from ssdlc_tasks import TaskError, TaskJournal, TaskScheduler, atomic_json, digest, file_hash
from ssdlc_transitions import runtime_root
from ssdlc_verification_repair import subject


class VerificationRepairTests(unittest.TestCase):
    def setUp(self):
        self.initialize()

    def initialize(self):
        class OwnedFixture(fixtures.ReconciliationTests):
            def before_import(self):
                path = self.snapshot / 'tests/test_extra.py'
                path.parent.mkdir(); path.write_text('def test_extra():\n    assert 1 == 1\n')
                self.manifest['source_files'].append(dict(path='tests/test_extra.py', role='acceptance_test',
                    rationale='Independent existing owner fixture', sha256=file_hash(path), executable=False))
                self.new_plan['tasks'][0]['target_files'].append('tests/test_extra.py')
        self.f = f = OwnedFixture()
        f.setUp(); self.addCleanup(f.doCleanups)
        f.request['profile']['development_attempts'] = 1
        f.request['profile']['role_invocations']['03-dev'] = 6
        f.request['profile']['phase_invocations']['03-dev'] = 6
        f.identity['environment'] = dict(mode='role_container', image_id='offline')
        f.select(f.stage())
        self.ticket, self.repo = f.target, f.repo
        self.root = runtime_root(self.ticket)
        self.calls = []
        def developer(task, attempt, prompt, workspace):
            self.calls.append(attempt)
            store = LimitsStore(self.root)
            identity = store.reserve('03-dev', '03-dev', 'codex'); store.finish(identity, 'returned')
            (workspace / 'next.py').write_text('value = 2\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0, quiescent=True,
                        engine='codex', model='offline')
        def timeout(argv, cwd, seconds, workspace):
            if (workspace / 'next.py').exists():
                return '', 'Verification timeout', 124
            return f.verify(argv, cwd, seconds, workspace)
        with self.assertRaisesRegex(TaskError, 'ENVIRONMENT_DIAGNOSIS'):
            self.scheduler(self.root, developer, timeout).run(f.new_plan['tasks'])
        outer = json.loads((self.root / 'state.json').read_text()); outer['status'] = 'FAILED'
        atomic_json(self.root / 'state.json', outer)
        self.source = inspect(self.ticket, verification_repair=True)
        self.plan = copy.deepcopy(self.source['plan']); self.plan['plan_revision'] += 1
        self.plan['tasks'][1]['target_files'].append('tests/test_extra.py')
        self.quality = copy.deepcopy(self.source['quality']); self.quality['checks'][0]['timeout_sec'] *= 2
        self.profile = copy.deepcopy(self.source['envelope']['policy']); self.profile['development_attempts'] += 1
        self.reason = 'Repair a reviewed owner fixture after a captured verification timeout.'
        self.updated_fixture = 'def test_extra():\n    assert 2 == 2\n'
        self.controller = dict(verification_repair_controller='frozen')
        self.identity = dict(controller_sha256=digest(execution_contract(self.controller)),
                             environment=f.identity['environment'])

    def scheduler(self, root, developer, verifier=None):
        envelope, _ = LimitsStore(root)._load(repair=False)
        state = TaskJournal(root / '03-dev/tasks').peek()
        return TaskScheduler(root / 'worktree', root / '03-dev/tasks',
            json.loads((root / '02-design/plan.json').read_text()),
            json.loads((root / '00-recon/quality-contract.json').read_text()), self.f.context,
            developer, verifier or self.f.verify, isolate_candidates=True, recover_timeouts=True,
            max_attempts=envelope['policy']['development_attempts'], runtime_identity=state['candidate_runtime'],
            historical_capture_authority=envelope['binding']['historical_capture_bindings_sha256'],
            historical_task_floors=envelope['binding'].get('task_floors'),
            scope_reconciliations=envelope['binding'].get('scope_reconciliations'))

    def value(self, quality=None, profile=None, plan=None, deadline_at=None):
        return subject(self.source, plan or self.plan, quality or self.quality,
                       profile or self.profile, self.reason, deadline_at=deadline_at)

    def stage(self, deadline_at=None, **kwargs):
        value = self.value(deadline_at=deadline_at)
        changes = []
        if 'tests/test_extra.py' in value['owner_revalidation']['files']:
            changes = [dict(path='tests/test_extra.py',
                old_sha256=value['owner_revalidation']['files']['tests/test_extra.py']['sha256'],
                new_sha256=hashlib.sha256(self.updated_fixture.encode()).hexdigest(),
                reason='Reviewed equivalent owner fixture assertion')]
        review = dict(subject_sha256=digest(value), reviewer='independent offline reviewer',
            report=dict(verdict='APPROVED', findings=[]), scope_diff=value['scope_diff'],
            owner_coverage=dict(contract_sha256=digest(value['owner_revalidation']),
                                coverage_preserved=True, approved_test_changes=changes))
        verification = dict(quality=self.quality, profile=self.profile)
        if deadline_at is not None: verification['deadline_at'] = deadline_at
        return prepare(self.ticket, self.repo, self.plan, review, self.reason,
            runtime_identity=self.identity, controller_snapshot=self.controller,
            verification_repair=verification, **kwargs)

    def auth(self, staged):
        p = staged['proposal']
        value = dict(schema_version=1, transition_id=p['transition_id'], proposal_sha256=digest(p),
            authorized=True, source_fenced=True, authorize_extensions=True,
            old_controller_sha256=p['old_controller_sha256'], new_controller_sha256=p['new_controller_sha256'],
            scope_diff_sha256=digest(p['scope_diff']), profile_sha256=p['profile_sha256'],
            quality_contract_sha256=p['quality_contract_sha256'],
            verification_repair_sha256=p['verification_repair_sha256'])
        if 'deadline_extension_sha256' in p:
            value.update(deadline_extension_sha256=p['deadline_extension_sha256'], authorize_deadline_extension=True)
        return value

    def test_native_development_restores_candidate_and_revalidates_owners(self):
        self.native_development()

    def test_native_development_after_expiry_uses_the_extended_bound(self):
        self.native_development(extend_deadline=True)

    def native_development(self, extend_deadline=False):
        deadline = self.source['counters']['deadline_at']
        if extend_deadline:
            self.profile['wall_seconds'] += 3600
            options = dict(deadline_at=deadline + 3600, clock=lambda: deadline + 1)
        else:
            options = {}
        original = {ref: (self.root / ref).read_bytes() for ref in self.source['files']}
        staged = self.stage(**options)
        activate(self.ticket, staged['preparation'], self.auth(staged),
                 **({'clock': options['clock']} if extend_deadline else {}))
        root = runtime_root(self.ticket)
        envelope, counters = LimitsStore(root)._load(repair=False)
        self.assertEqual(envelope['policy'], self.profile)
        self.assertEqual(counters, dict(self.source['counters'],
            deadline_at=deadline + (3600 if extend_deadline else 0), policy_sha256=digest(envelope)))
        self.assertEqual(TaskJournal(root / '03-dev/tasks').peek()['tasks']['T2']['attempts'], 1)
        import orchestrator
        dispatches = []; timeouts = []
        def provider(argv, **kwargs):
            dispatches.append(1)
            workspace = Path(kwargs['cwd'])
            self.assertEqual((workspace / 'next.py').read_text(), 'value = 2\n')
            (workspace / 'next.py').write_text('value = 3\n')
            (workspace / 'tests/test_extra.py').write_text(self.updated_fixture)
            return subprocess.CompletedProcess(argv, 0, 'TASK_DEV_STATUS: COMPLETE', '')
        def verifier(argv, cwd, timeout, **kwargs):
            timeouts.append(timeout)
            return 'Ran 1 test\nOK', '', 0
        original_init = LimitsStore.__init__
        def init_with_clock(store, root, *args, **kwargs):
            return original_init(store, root, clock=options['clock'])
        timed = patch.object(LimitsStore, '__init__', init_with_clock) if extend_deadline else contextlib.nullcontext()
        with (timed, patch.object(orchestrator.ProjectRuntimeIsolation, 'activate', return_value={'project_id':'offline'}),
              patch.object(orchestrator.ProjectRuntimeIsolation, 'ensure_trusted_path'),
              patch.object(orchestrator, 'assert_controller'),
              patch.object(orchestrator.SSDLCOrchestrator, '_verification_environment', return_value=self.identity['environment']),
              patch.object(orchestrator.EngineRunner, '_should_spawn_role_container', return_value=True),
              patch.object(orchestrator.EngineRunner, '_wrap_in_role_container', side_effect=lambda argv, **kw: argv),
              patch.object(orchestrator.EngineRunner, '_run_role_command', side_effect=provider),
              patch.object(orchestrator.EngineRunner, 'run_sandboxed_cmd', side_effect=verifier),
              patch.object(orchestrator.SquadDispatcher, 'dispatch_role', return_value=('codex','offline','low','fixture'))):
            native = orchestrator.SSDLCOrchestrator(self.repo, self.ticket.name, sync_remote=False)
            native.run_phase_dev()
        self.assertEqual(dispatches, [1])
        row = TaskJournal(root / '03-dev/tasks').peek()['tasks']['T2']
        self.assertEqual(row['attempts'], 2); self.assertEqual(row['status'], 'COMPLETED')
        self.assertEqual(row['owner_revalidations'][-1]['status'], 'PASSED')
        self.assertEqual(row['failed_attempts'], self.source['journal']['tasks']['T2']['failed_attempts'])
        self.assertIn(self.quality['checks'][0]['timeout_sec'], timeouts)
        after = LimitsStore(root)._load(repair=False)[1]
        self.assertEqual(len(after['invocations']), len(counters['invocations']) + 1)
        self.assertEqual(after['deadline_at'], counters['deadline_at'])
        self.assertTrue(all(after['invocations'][k] == v for k, v in counters['invocations'].items()))
        self.assertEqual({ref: (self.root / ref).read_bytes() for ref in original}, original)

    def test_quality_changes_cannot_remove_or_weaken_checks(self):
        for key, value in [('argv', ['true']), ('cwd', 'other'), ('required', False), ('timeout_sec', 1)]:
            quality = copy.deepcopy(self.quality); quality['checks'][0][key] = value
            with self.subTest(key=key), self.assertRaises(TaskError): self.value(quality=quality)
        quality = copy.deepcopy(self.quality); quality['checks'].pop()
        with self.assertRaises(TaskError): self.value(quality=quality)
        quality = copy.deepcopy(self.quality); quality['checks'][0]['timeout_sec'] = 3600
        with self.assertRaises(TaskError): self.value(quality=quality)

    def test_no_other_profile_field_or_obligation_can_change(self):
        for key in ('invocations', 'wall_seconds', 'metered_usd', 'cli_estimate_usd',
                    'invocation_timeout_seconds', 'transport_retries', 'development_attempts'):
            profile = copy.deepcopy(self.profile); profile[key] += 1
            with self.subTest(key=key), self.assertRaises(TaskError): self.value(profile=profile)
        plan = copy.deepcopy(self.plan); plan['tasks'][1]['title'] = 'different'
        with self.assertRaises(TaskError): self.value(plan=plan)
        plan = copy.deepcopy(self.plan); plan['tasks'][1]['target_files'][-1] = 'production.py'
        with self.assertRaises(TaskError): self.value(plan=plan)
        profile = copy.deepcopy(self.profile); profile['role_invocations']['03-dev'] += 1
        with self.assertRaises(TaskError): self.value(profile=profile)

    def test_ordinary_scope_cannot_borrow_timeout_failure(self):
        with self.assertRaisesRegex(TaskError, 'SCOPE_REQUIRED'): inspect(self.ticket)
        changed = copy.deepcopy(self.source)
        changed['journal']['tasks']['T2']['failed_attempts'][-1]['checks'][0]['exit_code'] = 1
        with self.assertRaises(TaskError): subject(changed, self.plan, self.quality, self.profile, self.reason)
        changed = copy.deepcopy(self.source); changed['journal']['tasks']['T2']['producer']['quiescent'] = False
        with self.assertRaises(TaskError): subject(changed, self.plan, self.quality, self.profile, self.reason)

    def test_exact_authorization_and_prepared_quality_tampering(self):
        staged = self.stage(); auth = self.auth(staged)
        for key in ('profile_sha256', 'quality_contract_sha256', 'verification_repair_sha256', 'authorize_extensions'):
            wrong = dict(auth); wrong.pop(key)
            with self.subTest(key=key), self.assertRaises(TaskError): activate(self.ticket, staged['preparation'], wrong)
        runtime = self.ticket / 'execution-runtimes' / staged['proposal']['transition_id']
        quality = copy.deepcopy(self.quality); quality['checks'][0]['argv'] = ['true']
        atomic_json(runtime / '00-recon/quality-contract.json', quality)
        with self.assertRaises(TaskError): activate(self.ticket, staged['preparation'], auth)
        self.assertEqual(json.loads((self.ticket / 'execution/active.json').read_text()), self.source['parent_selector'])

    def test_replay_after_selector_crash_preserves_attempts_and_counters(self):
        staged = self.stage(); auth = self.auth(staged)
        def crash(point):
            if point == 'after_activation': raise OSError('injected selector crash')
        with self.assertRaises(OSError): activate(self.ticket, staged['preparation'], auth, fault=crash)
        selected = activate(self.ticket, staged['preparation'], auth)
        self.assertEqual(activate(self.ticket, staged['preparation'], auth), selected)
        root = runtime_root(self.ticket)
        self.assertEqual(TaskJournal(root / '03-dev/tasks').peek()['tasks']['T2']['attempts'], 1)
        self.assertEqual(LimitsStore(root)._load(repair=False)[1]['invocations'], self.source['counters']['invocations'])
        self.assertEqual(self.calls, [1])

    def test_expired_or_changed_environment_cannot_be_prepared(self):
        with self.assertRaises(TaskError): self.stage(clock=lambda: self.source['counters']['deadline_at'] + 1)
        self.identity['environment'] = dict(mode='role_container', image_id='different')
        with self.assertRaises(TaskError): self.stage()

    def test_existing_recording_authority_is_preserved_and_selector_receipt_is_fenced(self):
        import test_owner_revalidation as owners
        from ssdlc_feedback import FeedbackStore
        from ssdlc_feedback_delegation import admitted
        case = owners.OwnerFeedbackTests(); case.initialize(False, attempt_cap=3)
        self.addCleanup(case.doCleanups)
        first, _, store = case.operator_bridge()
        activate(case.f.ticket, first['preparation'], case.f.auth(first))
        root = runtime_root(case.f.ticket)
        def developer(task, attempt, prompt, workspace):
            store = LimitsStore(root); invocation = store.reserve('03-dev', '03-dev', 'codex')
            store.finish(invocation, 'returned'); (workspace / 'next.py').write_text('value = 4\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0, quiescent=True)
        original_verify = case.f.verify
        def timeout(argv, cwd, seconds, workspace):
            if (workspace / 'next.py').exists(): return '', 'Verification timeout', 124
            return original_verify(argv, cwd, seconds, workspace)
        case.f.verify = timeout
        with self.assertRaisesRegex(TaskError, 'ENVIRONMENT_DIAGNOSIS'):
            case.f.scheduler(root, developer).run(case.f.plan['tasks'])
        outer = json.loads((root / 'state.json').read_text()); outer['status'] = 'FAILED'
        atomic_json(root / 'state.json', outer)
        self.root, self.ticket, self.repo = root, case.f.ticket, case.f.repo
        self.source = inspect(self.ticket, verification_repair=True)
        self.plan = copy.deepcopy(self.source['plan']); self.plan['plan_revision'] += 1
        self.plan['tasks'][1]['target_files'].append('tests/new_fixture.py')
        self.quality = copy.deepcopy(self.source['quality']); self.quality['checks'][0]['timeout_sec'] *= 2
        self.profile = copy.deepcopy(self.source['envelope']['policy']); self.profile['development_attempts'] += 1
        self.identity['environment'] = case.f.identity['environment']
        original_authority = (store.root / 'authority.json').read_bytes()
        before = store.inspect()
        staged = self.stage(); auth = self.auth(staged)
        def crash(point):
            if point == 'verification_repair_selector_published': raise OSError('lost receipt')
        with self.assertRaisesRegex(OSError, 'lost receipt'):
            activate(self.ticket, staged['preparation'], auth, fault=crash)
        with self.assertRaisesRegex(TaskError, 'reconciliation pending'):
            runtime_root(self.ticket)
        activate(self.ticket, staged['preparation'], auth)
        root = runtime_root(self.ticket)
        envelope, counters = LimitsStore(root)._load(repair=False)
        state = store.inspect()
        self.assertEqual((store.root / 'authority.json').read_bytes(), original_authority)
        self.assertEqual(state['requests'], before['requests'])
        self.assertEqual(state['role_calls'], before['role_calls'])
        self.assertEqual(state['amendments'], before['amendments'])
        self.assertEqual(envelope['policy']['feedback_delegation'], self.source['envelope']['policy']['feedback_delegation'])
        self.assertEqual(counters['invocations'], self.source['counters']['invocations'])
        projection = json.loads((Path(staged['preparation'])/'projection.json').read_text())
        self.assertTrue(admitted(state, dict(projection=projection, envelope=envelope)))
        self.assertEqual(store.public_projection()['operator_verification_repairs'], 1)
        prefix = (store.root / 'task-journal.jsonl').read_bytes()
        activate(self.ticket, staged['preparation'], auth)
        self.assertEqual((store.root / 'task-journal.jsonl').read_bytes(), prefix)

    def test_cli_preview_binds_the_same_subject_without_dispatch_or_preparation(self):
        from ssdlc_scope_amendments import main
        paths = []
        for name, value in [('plan', self.plan), ('quality', self.quality), ('profile', self.profile)]:
            path = Path(self.f.tmp.name) / (name + '.json'); atomic_json(path, value); paths.append(path)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(['continuation-scope-preview', '--repo', str(self.repo), '--ticket', self.ticket.name,
                '--plan', str(paths[0]), '--reason', self.reason,
                '--verification-repair-quality', str(paths[1]), '--verification-repair-profile', str(paths[2])])
        self.assertEqual(rc, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['subject_sha256'], digest(self.value()))
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['model_dispatches'], 0)
        self.assertFalse((self.ticket / 'execution/pending.json').exists())

    def test_expired_source_admits_exact_deadline_without_resetting_elapsed_history(self):
        old = self.source['counters']; extension = 3600
        now = old['deadline_at'] + 1
        self.profile['wall_seconds'] += extension
        with self.assertRaisesRegex(TaskError, 'wall-time exhausted'):
            LimitsStore(self.root, clock=lambda: now).require_capacity('03-dev', '03-dev')
        staged = self.stage(deadline_at=old['deadline_at'] + extension, clock=lambda: now)
        original = {ref: (self.root / ref).read_bytes() for ref in self.source['files']}
        selected = activate(self.ticket, staged['preparation'], self.auth(staged), clock=lambda: now)
        root = runtime_root(self.ticket); store = LimitsStore(root, clock=lambda: now)
        envelope, counters = store._load(repair=False)
        store.require_capacity('03-dev', '03-dev')
        self.assertEqual(counters['started_at'], old['started_at'])
        self.assertEqual(counters['deadline_at'], old['deadline_at'] + extension)
        self.assertEqual(envelope['binding']['accounting_anchor'], self.source['envelope']['binding']['accounting_anchor'])
        self.assertEqual(counters, dict(old, deadline_at=old['deadline_at'] + extension, policy_sha256=digest(envelope)))
        self.assertEqual({ref: (self.root / ref).read_bytes() for ref in original}, original)
        self.assertEqual(TaskJournal(root/'03-dev/tasks').peek()['tasks']['T2']['attempts'], 1)
        self.assertEqual(activate(self.ticket, staged['preparation'], self.auth(staged), clock=lambda: now), selected)
        self.assertEqual(self.calls, [1])

    def test_deadline_requires_extra_exact_authorization_and_future_capacity(self):
        old = self.source['counters']['deadline_at']; self.profile['wall_seconds'] += 3600
        staged = self.stage(deadline_at=old + 3600, clock=lambda: old + 1)
        auth = self.auth(staged)
        for field in ('deadline_extension_sha256', 'authorize_deadline_extension'):
            wrong = dict(auth); wrong.pop(field)
            with self.subTest(field=field), self.assertRaises(TaskError):
                activate(self.ticket, staged['preparation'], wrong, clock=lambda: old + 1)
        with self.assertRaisesRegex(TaskError, 'wall-time exhausted'):
            activate(self.ticket, staged['preparation'], auth, clock=lambda: old + 3601)
        self.assertEqual(json.loads((self.ticket/'execution/active.json').read_text()), self.source['parent_selector'])

    def test_deadline_cannot_borrow_v1_or_hide_other_limit_changes(self):
        old = self.source['counters']['deadline_at']; self.profile['wall_seconds'] += 3600
        with self.assertRaises(TaskError): self.value()
        for deadline in (old, old - 1, old + 1800, old + 3600.5, True, float('inf')):
            with self.subTest(deadline=deadline), self.assertRaises(TaskError): self.value(deadline_at=deadline)
        self.profile['metered_usd'] += 1
        with self.assertRaises(TaskError): self.value(deadline_at=old + 3600)

    def test_native_deadline_counter_and_extension_chain_tampering_are_refused(self):
        old = self.source['counters']['deadline_at']; self.profile['wall_seconds'] += 3600
        staged = self.stage(deadline_at=old + 3600)
        activate(self.ticket, staged['preparation'], self.auth(staged))
        root = runtime_root(self.ticket); store = LimitsStore(root)
        _, counters = store._load(repair=False)
        store.journal.load(); store.journal.append('TAMPERED_DEADLINE', dict(counters, deadline_at=old + 7200))
        with self.assertRaisesRegex(TaskError, 'anchor changed'): store._load(repair=False)
        from ssdlc_deadline_extensions import accounting_anchor
        envelope = copy.deepcopy(self.source['envelope'])
        envelope['binding']['deadline_extensions'] = [self.value(deadline_at=old + 3600)['verification_repair']['deadline_extension']]
        envelope['policy'] = self.profile
        self.assertEqual(accounting_anchor(envelope)['deadline_at'], old + 3600)
        envelope['binding']['deadline_extensions'][0]['started_at'] += 1
        with self.assertRaises(TaskError): accounting_anchor(envelope)
