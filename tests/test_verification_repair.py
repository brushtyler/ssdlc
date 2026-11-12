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

    def value(self, quality=None, profile=None, plan=None):
        return subject(self.source, plan or self.plan, quality or self.quality,
                       profile or self.profile, self.reason)

    def stage(self, **kwargs):
        value = self.value()
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
        return prepare(self.ticket, self.repo, self.plan, review, self.reason,
            runtime_identity=self.identity, controller_snapshot=self.controller,
            verification_repair=dict(quality=self.quality, profile=self.profile), **kwargs)

    def auth(self, staged):
        p = staged['proposal']
        return dict(schema_version=1, transition_id=p['transition_id'], proposal_sha256=digest(p),
            authorized=True, source_fenced=True, authorize_extensions=True,
            old_controller_sha256=p['old_controller_sha256'], new_controller_sha256=p['new_controller_sha256'],
            scope_diff_sha256=digest(p['scope_diff']), profile_sha256=p['profile_sha256'],
            quality_contract_sha256=p['quality_contract_sha256'],
            verification_repair_sha256=p['verification_repair_sha256'])

    def test_native_development_restores_candidate_and_revalidates_owners(self):
        original = {ref: (self.root / ref).read_bytes() for ref in self.source['files']}
        staged = self.stage(); activate(self.ticket, staged['preparation'], self.auth(staged))
        root = runtime_root(self.ticket)
        envelope, counters = LimitsStore(root)._load(repair=False)
        self.assertEqual(envelope['policy'], self.profile)
        self.assertEqual(counters, dict(self.source['counters'], policy_sha256=digest(envelope)))
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
        with (patch.object(orchestrator.ProjectRuntimeIsolation, 'activate', return_value={'project_id':'offline'}),
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
