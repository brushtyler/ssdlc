"""Real I1/I2 checkpoints and scope recovery, with offline semantic developers."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_reconciliation as fixtures
from ssdlc_controller import execution_contract
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import inspect, subject, prepare, activate
from ssdlc_tasks import TaskScheduler, TaskJournal, TaskError, atomic_json, digest
from ssdlc_transitions import TransitionError, runtime_root


class ScopeAmendmentTests(unittest.TestCase):
    def setUp(self):
        self.initialize()

    def initialize(self, attempt_cap=3):
        self.fixture = f = fixtures.ReconciliationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        # These are explicit synthetic initial limits, not a recovery extension.
        f.request['profile']['development_attempts'] = attempt_cap
        f.request['profile']['role_invocations']['03-dev'] = 6
        f.request['profile']['phase_invocations']['03-dev'] = 6
        staged = f.stage(); f.select(staged)
        self.ticket, self.repo = f.target, f.repo
        self.root = runtime_root(self.ticket)
        self.calls = []
        def blocked(task, attempt, prompt, workspace):
            self.calls.append(attempt)
            store = LimitsStore(self.root)
            identity = store.reserve('03-dev', '03-dev', 'codex')
            store.finish(identity, 'returned')
            (workspace / 'next.py').write_text('value = 2\n')
            output = ('TASK_DEV_STATUS: COMPLETE' if attempt == 1 else
                      'Add app.py to the manifest.\nTASK_DEV_STATUS: SCOPE_REQUIRED')
            return dict(output=output,
                        exit_code=0, quiescent=True, engine='codex', model='offline')
        scheduler = self.scheduler(self.root, blocked)
        with self.assertRaisesRegex(TaskError, 'SCOPE_REQUIRED'):
            scheduler.run(f.new_plan['tasks'])
        outer = json.loads((self.root / 'state.json').read_text())
        outer['status'] = 'ESCALATED_TO_SUPERVISOR'
        atomic_json(self.root / 'state.json', outer)
        self.source = inspect(self.ticket)
        self.plan = copy.deepcopy(self.source['plan'])
        self.plan['plan_revision'] += 1
        self.plan['tasks'][1]['target_files'].append('app.py')
        self.reason = 'Adapt the existing fixture; preserve its no-dispatch assertions.'
        self.new_controller = {'scope_controller': 'frozen'}
        self.identity = dict(controller_sha256=digest(execution_contract(self.new_controller)),
                             environment=f.identity['environment'])
        self.review = dict(subject_sha256=digest(subject(self.source, self.plan, self.reason)),
                           reviewer='independent offline reviewer', report=dict(verdict='APPROVED', findings=[]),
                           scope_diff={'T2': {'added': ['app.py'], 'removed': []}})

    def scheduler(self, root, developer):
        envelope, _ = LimitsStore(root)._load(repair=False)
        state = TaskJournal(root / '03-dev/tasks').peek()
        return TaskScheduler(root / 'worktree', root / '03-dev/tasks',
            json.loads((root / '02-design/plan.json').read_text()),
            json.loads((root / '00-recon/quality-contract.json').read_text()), self.fixture.context,
            developer, self.verify, max_attempts=envelope['policy']['development_attempts'], isolate_candidates=True,
            runtime_identity=state['candidate_runtime'], recover_timeouts=True,
            historical_capture_authority=envelope['binding']['historical_capture_bindings_sha256'],
            historical_task_floors=envelope['binding'].get('task_floors'),
            scope_reconciliations=envelope['binding'].get('scope_reconciliations'))

    def verify(self, argv, cwd, timeout, workspace):
        if (workspace / 'next.py').exists() and self.calls == [1]:
            return 'Ran 1 test\nFAILED (failures=1)', '', 1
        return self.fixture.verify(argv, cwd, timeout, workspace)

    def stage(self, **kwargs):
        return prepare(self.ticket, self.repo, self.plan, self.review, self.reason,
                       runtime_identity=self.identity, controller_snapshot=self.new_controller, **kwargs)

    def auth(self, staged):
        p = staged['proposal']
        return dict(schema_version=1, transition_id=p['transition_id'], proposal_sha256=digest(p),
                    old_controller_sha256=p['old_controller_sha256'], new_controller_sha256=p['new_controller_sha256'],
                    scope_diff_sha256=digest(p['scope_diff']), authorized=True, source_fenced=True)

    def test_checkpoint_history_counters_and_native_repair(self):
        original = {ref: (self.root / ref).read_bytes() for ref in self.source['files']}
        staged = self.stage()
        with self.assertRaisesRegex(TransitionError, 'pending'):
            runtime_root(self.ticket)
        selected = activate(self.ticket, staged['preparation'], self.auth(staged))
        root = runtime_root(self.ticket)
        self.assertNotEqual(root, self.root)
        envelope, counters = LimitsStore(root)._load(repair=False)
        before = dict(self.source['counters'], policy_sha256=digest(envelope))
        self.assertEqual(counters, before)
        self.assertEqual(envelope['policy'], self.source['envelope']['policy'])
        current = TaskJournal(root / '03-dev/tasks').peek()
        self.assertEqual(current['tasks']['T1']['commit'], self.source['journal']['tasks']['T1']['commit'])
        self.assertEqual(current['tasks']['T2']['failed_attempts'], self.source['journal']['tasks']['T2']['failed_attempts'])
        self.assertTrue((root / '03-dev/tasks/task-journal.jsonl').read_bytes().startswith(original['03-dev/tasks/task-journal.jsonl']))
        self.assertEqual({ref: (self.root / ref).read_bytes() for ref in original}, original)
        repaired = []
        def develop(task, attempt, prompt, workspace):
            repaired.append(attempt)
            store = LimitsStore(root)
            reservation = store.reserve('03-dev', '03-dev', 'codex')
            store.finish(reservation, 'returned')
            self.assertEqual((workspace / 'next.py').read_text(), 'value = 2\n')
            self.assertIn('scope_amendment', prompt)
            (workspace / 'app.py').write_text('value = 3\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0, quiescent=True, engine='codex', model='offline')
        result = self.scheduler(root, develop).run(self.plan['tasks'])
        self.assertEqual(repaired, [3])
        self.assertEqual(result['tasks']['T1']['attempts'], 1)
        self.assertEqual(result['tasks']['T2']['attempts'], 3)
        self.assertEqual(result['tasks']['T2']['status'], 'COMPLETED')
        self.assertEqual(len(LimitsStore(root)._load(repair=False)[1]['invocations']), len(before['invocations']) + 1)
        self.assertEqual(activate(self.ticket, staged['preparation'], self.auth(staged)), selected)

    def test_changed_obligations_checks_or_other_task_scope_refused(self):
        for field, value in [('depends_on', []), ('ac_refs', []), ('title', 'changed')]:
            plan = copy.deepcopy(self.plan)
            plan['tasks'][1][field] = value
            with self.subTest(field=field), self.assertRaises(TaskError):
                subject(self.source, plan, self.reason)
        plan = copy.deepcopy(self.plan); plan['tasks'][0]['target_files'].append('extra.py')
        with self.assertRaises(TaskError): subject(self.source, plan, self.reason)

    def test_review_and_authorization_bind_expanded_scope(self):
        self.review['subject_sha256'] = '0' * 64
        with self.assertRaisesRegex(TaskError, 'Independent review'):
            self.stage()
        self.review['subject_sha256'] = digest(subject(self.source, self.plan, self.reason))
        staged = self.stage()
        auth = self.auth(staged); auth['scope_diff_sha256'] = '0' * 64
        with self.assertRaises(TaskError): activate(self.ticket, staged['preparation'], auth)
        self.assertEqual(json.loads((self.ticket / 'execution/active.json').read_text()), self.source['parent_selector'])

    def test_scope_authority_cannot_be_forged_in_task_state(self):
        staged = self.stage(); activate(self.ticket, staged['preparation'], self.auth(staged))
        root = runtime_root(self.ticket)
        scheduler = self.scheduler(root, lambda *a: self.fail('must not dispatch'))
        scheduler.scope_reconciliations = {}
        with self.assertRaisesRegex(TaskError, 'Scope reconciliation has no transition authority'):
            scheduler.run(self.plan['tasks'])

    def test_activation_faults_replay_without_counter_or_dispatch_reset(self):
        # A preparation can be retried across every publication boundary. Each
        # subcase gets its own full source to retain an independent parent.
        for boundary in ('intent_published', 'generation_staged', 'before_activation', 'after_activation'):
            case = self if boundary == 'intent_published' else ScopeAmendmentTests()
            if case is not self:
                case.setUp(); self.addCleanup(case.doCleanups)
            with self.subTest(boundary=boundary):
                staged = case.stage()
                def fault(point):
                    if point == boundary: raise OSError('injected publication interruption')
                with self.assertRaises(OSError):
                    activate(case.ticket, staged['preparation'], case.auth(staged), fault=fault)
                activate(case.ticket, staged['preparation'], case.auth(staged))
                root = runtime_root(case.ticket)
                counters = LimitsStore(root)._load(repair=False)[1]
                self.assertEqual(counters['invocations'], case.source['counters']['invocations'])
                self.assertEqual(case.calls, [1, 2])
                self.assertEqual(TaskJournal(root / '03-dev/tasks').peek()['tasks']['T2']['attempts'], 2)

    def test_stale_source_and_prepared_source_are_not_activated(self):
        staged = self.stage()
        state_path = self.root / 'state.json'
        original = state_path.read_bytes()
        state = json.loads(original); state['operator_note'] = 'changed'
        atomic_json(state_path, state)
        with self.assertRaisesRegex(TaskError, 'Parent generation changed'):
            activate(self.ticket, staged['preparation'], self.auth(staged))
        state_path.write_bytes(original)
        runtime = self.ticket / 'execution-runtimes' / staged['proposal']['transition_id']
        (runtime / 'unexpected.json').write_text('{}')
        with self.assertRaisesRegex(TaskError, 'Prepared runtime changed'):
            activate(self.ticket, staged['preparation'], self.auth(staged))
        self.assertEqual(json.loads((self.ticket / 'execution/active.json').read_text()), self.source['parent_selector'])

    def test_budget_deadline_and_environment_cannot_be_extended(self):
        with self.assertRaisesRegex(TaskError, 'wall-time exhausted'):
            self.stage(clock=lambda: self.source['counters']['deadline_at'] + 1)
        self.identity['environment'] = {'image_id': 'different'}
        with self.assertRaisesRegex(TaskError, 'verification environment'):
            self.stage()
        self.assertFalse((self.ticket / 'execution/pending.json').exists())

    def test_exhausted_attempts_cannot_be_granted_by_amendment(self):
        case = ScopeAmendmentTests()
        case.initialize(attempt_cap=2)
        self.addCleanup(case.doCleanups)
        with self.assertRaisesRegex(TaskError, 'cannot grant another development attempt'):
            case.stage()
        self.assertEqual(case.source['journal']['tasks']['T2']['attempts'], 2)
        self.assertFalse((case.ticket / 'execution/pending.json').exists())

    def test_expired_activation_leaves_parent_selected(self):
        staged = self.stage()
        with self.assertRaisesRegex(TaskError, 'wall-time exhausted'):
            activate(self.ticket, staged['preparation'], self.auth(staged),
                     clock=lambda: self.source['counters']['deadline_at'] + 1)
        self.assertEqual(json.loads((self.ticket / 'execution/active.json').read_text()), self.source['parent_selector'])

    def test_protected_path_and_undeclared_scope_are_refused(self):
        with patch('ssdlc_grader.protected_paths', return_value={'app.py'}):
            with self.assertRaises(TaskError): subject(self.source, self.plan, self.reason)
        self.review['scope_diff'] = {}
        with self.assertRaisesRegex(TaskError, 'Scope expansion'):
            self.stage()

    def test_failed_preparation_is_owned_and_discardable(self):
        from ssdlc_preparations import unfinished, discard
        def fault(point):
            if point == 'worktree_created': raise OSError('injected preparation interruption')
        with self.assertRaises(OSError): self.stage(fault=fault)
        identities = unfinished(self.ticket, namespace='execution')
        self.assertEqual(len(identities), 1)
        self.assertEqual(runtime_root(self.ticket), self.root)
        result = discard(self.ticket, identities[0], namespace='execution', writers_quiescent=lambda _: True)
        self.assertEqual(result['status'], 'ABORTED')
        self.assertEqual(inspect(self.ticket)['files'], self.source['files'])
        self.assertEqual(unfinished(self.ticket, namespace='execution'), [])

    def test_receipts_and_completed_proof_are_checked_before_amending(self):
        completed = self.source['journal']['tasks']['T1']
        check = completed['checks'][0]
        path = self.root / '03-dev/tasks' / check['log_ref']
        path.write_text('changed completed proof')
        with self.assertRaises(TaskError): inspect(self.ticket)

    def test_unknown_outcome_and_partial_journal_refuse_before_preparation(self):
        original = (self.root / '03-dev/tasks/task-journal.jsonl').read_bytes()
        (self.root / '03-dev/tasks/task-journal.jsonl').write_bytes(original + b'{')
        with self.assertRaisesRegex(TaskError, 'Interrupted journal tail'):
            inspect(self.ticket)
        (self.root / '03-dev/tasks/task-journal.jsonl').write_bytes(original)
        LimitsStore(self.root).reserve('03-dev', '03-dev', 'codex')
        with self.assertRaisesRegex(TaskError, 'Unresolved invocation'):
            inspect(self.ticket)

    def test_cli_preview_and_prepare_are_offline_and_do_not_dispatch(self):
        import contextlib
        import io
        import orchestrator as o
        plan, review = self.repo / 'amendment-plan.json', self.repo / 'amendment-review.json'
        atomic_json(plan, self.plan); atomic_json(review, self.review)
        base = ['--repo', str(self.repo), '--ticket', self.ticket.name]
        with patch.object(o.EngineRunner, 'invoke_role', side_effect=AssertionError('provider')), \
             patch.object(o, 'SSDLCOrchestrator', side_effect=AssertionError('constructor')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(o.main(['continuation-scope-preview', *base, '--plan', str(plan), '--reason', self.reason]), 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['model_dispatches'], 0)


    def test_native_development_entrypoint_uses_authorized_scope_and_last_attempt(self):
        import subprocess
        import orchestrator as o
        from ssdlc_controller import controller_snapshot
        self.new_controller = controller_snapshot(Path(__file__).resolve().parents[1])
        self.identity['controller_sha256'] = digest(execution_contract(self.new_controller))
        staged = self.stage(); activate(self.ticket, staged['preparation'], self.auth(staged))
        root = runtime_root(self.ticket)
        self.assertTrue(o.resume_compatibility(self.repo, self.ticket.name, 'run', '03-dev')['ok'])
        self.assertFalse(o.resume_compatibility(self.repo, self.ticket.name, 'run', '00-recon')['ok'])
        calls = []
        def provider(argv, **kwargs):
            calls.append(kwargs['cwd'])
            self.assertEqual((Path(kwargs['cwd']) / 'next.py').read_text(), 'value = 2\n')
            (Path(kwargs['cwd']) / 'app.py').write_text('value = 3\n')
            return subprocess.CompletedProcess(argv, 0, 'TASK_DEV_STATUS: COMPLETE', '')
        with patch.object(o.ProjectRuntimeIsolation, 'activate', return_value={'project_id': 'offline'}), \
             patch.object(o.ProjectRuntimeIsolation, 'ensure_trusted_path'), \
             patch.object(o.SSDLCOrchestrator, '_verification_environment', return_value=self.identity['environment']), \
             patch.object(o.EngineRunner, '_should_spawn_role_container', return_value=True), \
             patch.object(o.SquadDispatcher, 'dispatch_role', return_value=('codex', 'offline', 'low', 'test')), \
             patch.object(o.EngineRunner, '_run_role_command', side_effect=provider), \
             patch.object(o.EngineRunner, 'run_sandboxed_cmd', side_effect=lambda argv,cwd,timeout,verification_worktree:
                          self.verify(argv,cwd,timeout,verification_worktree)):
            native = o.SSDLCOrchestrator(self.repo, self.ticket.name, sync_remote=False)
            native.run_phase_dev()
        self.assertEqual(len(calls), 1)
        tasks = TaskJournal(root / '03-dev/tasks').peek()['tasks']
        self.assertEqual(tasks['T1']['attempts'], 1)
        self.assertEqual(tasks['T2']['attempts'], 3)
        self.assertEqual(tasks['T2']['status'], 'COMPLETED')
        self.assertEqual(len(LimitsStore(root)._load(repair=False)[1]['invocations']), len(self.source['counters']['invocations']) + 1)

    def test_symlinked_execution_store_and_ticket_are_refused(self):
        alias = self.ticket.parent / 'ALIAS'
        alias.symlink_to(self.ticket, target_is_directory=True)
        with self.assertRaisesRegex(TaskError, 'Symlink'):
            inspect(alias)


class ImportedEvidenceMetadataTests(unittest.TestCase):
    def test_failure_pack_accepts_undated_imported_metadata_without_fabricating_time(self):
        import os
        import test_task_bootstrap as fixtures
        case = fixtures.OrchestratorBootstrapTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            owner = case.make_orchestrator()
            owner.state['goal'] = 'offline evidence refusal'
            failure = dict(type='DevelopmentBlocked', category='SCOPE_REQUIRED', message='Original failure')
            owner.state['failure'] = copy.deepcopy(failure)
            owner.state['artifacts']['imported.txt'] = dict(path='imported.txt', sha256='a' * 64)
            owner._save_state()
            pack = owner.compile_evidence_pack(False)
        self.assertIn('not recorded', pack.read_text())
        self.assertNotIn('recorded_at', owner.state['artifacts']['imported.txt'])
        self.assertEqual(owner.state['failure'], failure)
        self.assertEqual(owner.state['status'], 'ESCALATED_TO_SUPERVISOR')
