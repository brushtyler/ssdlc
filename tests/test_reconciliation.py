"""Offline cumulative admission replay: real worktrees/journals, no inference."""
import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_continuations import ContinuationTests
from ssdlc_controller import execution_contract
from ssdlc_historical import project_accounting, reservation_catalogue
from ssdlc_reconciliation import prepare, activate, inspect_i1, resolve_execution
from ssdlc_recovery import LimitsStore, RecoveryStop
from ssdlc_tasks import TaskJournal, TaskScheduler, TaskError, atomic_json, digest, file_hash, git
from ssdlc_transitions import TransitionError, runtime_root


class ReconciliationTests(ContinuationTests):
    def setUp(self):
        super().setUp()
        self.target = self.artifacts
        self.controller = {'new_controller': 'frozen'}
        self.identity['controller_sha256'] = digest(execution_contract(self.controller))
        self.before_import()
        imported = super().prepare()
        super().activate(imported)
        self.original = inspect_i1(self.target)
        self.original_bytes = {ref: (self.original['root'] / ref).read_bytes() for ref in self.original['files']}
        roles = {'03-dev': 3, '02-design-task-plan': 6, '04-review': 3}
        self.request = {'schema_version': 1, 'profile': {
            'schema_version': 1, 'profile_id': 'historical-replay', 'invocations': 20,
            'wall_seconds': 10000, 'role_invocations': roles,
            'phase_invocations': {'03-dev': 3, '02-design': 6, '04-review': 3},
            'transport_retries': 0, 'design_diagnoses': 1, 'metered_usd': 10,
            'cli_estimate_usd': 10, 'development_attempts': 2, 'design_rounds': 6,
            'invocation_timeout_seconds': 1800},
            'ledger_mapping': [{'index': 0, 'sha256': digest(self.outer['ledger'][0]),
                'role': '02-design-task-plan', 'phase': '02-design',
                'origins': ['02-design/task-planning/session.json:opaque:0']}],
            'opaque_floors': {'02-design/task-planning/session.json': {
                'count': 5, 'role': '02-design-task-plan', 'phase': '02-design',
                'reason': 'Conservative allocation of five historically charged calls'}},
            'supplemental_invocations': [
                {'origin': '02-design/task-planning/session.json:opaque:' + str(i),
                 'role': '02-design-task-plan', 'phase': '02-design', 'engine': 'codex',
                 'reason': 'Charged reservation with absent usage row'} for i in range(1, 5)] + [
                {'origin': 'task:T1:1', 'role': '03-dev', 'phase': '03-dev', 'engine': 'codex',
                 'reason': 'Preserved developer attempt has unknown usage'}],
            'limit_sources': [], 'combined_reported_usd_cap': 10,
            'time': {'started_at': time.time() - 1000, 'basis': 'operator_attestation',
                     'reason': 'Test fixture has no created_at'}}

    def before_import(self):
        pass

    def stage(self, **kwargs):
        return prepare(self.target, self.repo, self.request, runtime_identity=self.identity,
                       controller_snapshot=self.controller, verifier=self.verify, **kwargs)

    def auth(self, staged):
        p = staged['proposal']
        result = {'schema_version': 1, 'transition_id': p['transition_id'], 'proposal_sha256': digest(p),
                'profile_sha256': p['profile_sha256'], 'decisions_sha256': digest(p['decisions']),
                'authorized': True, 'source_fenced': True, 'accept_unknown_history': True,
                'authorize_extensions': True, 'old_controller_sha256': p['old_controller_sha256'],
                'new_controller_sha256': p['new_controller_sha256']}
        if any(v['added'] for v in p.get('scope_diff', {}).values()):
            result['scope_diff_sha256'] = digest(p['scope_diff'])
        return result

    def select(self, staged):
        return activate(self.target, staged['preparation'], self.auth(staged))

    def test_cumulative_activation_preserves_source_and_skips_verified_developer(self):
        staged = self.stage()
        with self.assertRaisesRegex(TransitionError, 'pending'):
            runtime_root(self.target)
        selected = self.select(staged)
        root = runtime_root(self.target)
        self.assertEqual(root, resolve_execution(self.target))
        self.assertEqual(self.select(staged), selected)
        envelope, counters = LimitsStore(root)._load(repair=False)
        self.assertEqual(len(counters['invocations']), 6)
        self.assertEqual(sum(r['usage']['cli_estimate_usd'] or 0 for r in counters['invocations'].values()), 9.26)
        self.assertIsNone(next(r for r in counters['invocations'].values() if r['engine'] == 'codex')['usage']['metered_usd'])
        state = json.loads((root / 'state.json').read_text())
        self.assertEqual(state['ledger'], self.outer['ledger'])
        self.assertEqual(state['gate_history'], [])
        self.assertNotIn('task_verified_head', state)
        journal = TaskJournal(root / '03-dev/tasks').peek()
        self.assertEqual(journal['tasks']['T1']['attempts'], 1)
        self.assertEqual(journal['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual(journal['tasks']['T2']['attempts'], 0)
        self.assertTrue((root / '03-dev/tasks/task-journal.jsonl').read_bytes().startswith(self.prefix))
        self.assertEqual({ref: (self.original['root'] / ref).read_bytes() for ref in self.original_bytes}, self.original_bytes)
        calls = []
        def developer(task, attempt, prompt, workspace):
            calls.append(task['task_id'])
            (workspace / 'next.py').write_text('value = 2\n')
            return {'output': 'done', 'exit_code': 0, 'quiescent': True, 'engine': 'codex', 'model': 'offline'}
        # Inspect the real adapter signature rather than hiding dispatch behavior.
        scheduler = TaskScheduler(root / 'worktree', root / '03-dev/tasks', self.new_plan,
            json.loads((root / '00-recon/quality-contract.json').read_text()), self.context,
            developer, self.verify, isolate_candidates=True, runtime_identity=self.identity,
            recover_timeouts=True, max_attempts=2,
            historical_capture_authority=envelope['binding']['historical_capture_bindings_sha256'])
        result = scheduler.run(self.new_plan['tasks'])
        self.assertEqual(calls, ['T2'])
        self.assertEqual(result['tasks']['T1']['attempts'], 1)

    def test_native_constructor_dispatch_compatibility_and_supervisor_read_same_generation(self):
        from orchestrator import SSDLCOrchestrator, ProjectRuntimeIsolation, EngineRunner, SquadDispatcher, resume_compatibility
        from ssdlc_controller import controller_snapshot
        from ssdlc_supervisor import canonical_root, peek_journal
        self.controller = controller_snapshot(Path(__file__).resolve().parents[1])
        self.identity['controller_sha256'] = digest(execution_contract(self.controller))
        staged = self.stage(); self.select(staged)
        root = runtime_root(self.target)
        self.assertTrue(resume_compatibility(self.repo, 'TKT-1', 'run', '03-dev')['ok'])
        self.assertFalse(resume_compatibility(self.repo, 'TKT-1', 'run', '00-recon')['ok'])
        self.assertFalse(resume_compatibility(self.repo, 'TKT-1', 'design')['ok'])
        self.assertEqual(canonical_root(self.repo, 'TKT-1'), root)
        calls = []
        def provider(argv, **kwargs):
            calls.append(kwargs['cwd'])
            (Path(kwargs['cwd']) / 'next.py').write_text('value = 2\n')
            return __import__('subprocess').CompletedProcess(argv, 0, 'done', '')
        with patch.object(ProjectRuntimeIsolation, 'activate', return_value={'project_id': 'offline'}), \
             patch.object(ProjectRuntimeIsolation, 'ensure_trusted_path'), \
             patch.object(SSDLCOrchestrator, '_verification_environment', return_value=self.identity['environment']), \
             patch.object(EngineRunner, '_should_spawn_role_container', return_value=True), \
             patch.object(SquadDispatcher, 'dispatch_role', return_value=('codex', 'offline', 'low', 'test')), \
             patch.object(EngineRunner, '_run_role_command', side_effect=provider), \
             patch.object(EngineRunner, 'run_sandboxed_cmd', side_effect=lambda argv,cwd,timeout,verification_worktree: self.verify(argv,cwd,timeout,verification_worktree)):
            native = SSDLCOrchestrator(self.repo, 'TKT-1', sync_remote=False)
            self.assertEqual(native.ssdlc_root, root)
            native.run_phase_dev()
        self.assertEqual(len(calls), 1)
        counters = LimitsStore(root)._load(repair=False)[1]
        self.assertEqual(len(counters['invocations']), 7)
        tasks = TaskJournal(root / '03-dev/tasks').peek()['tasks']
        self.assertEqual(tasks['T1']['attempts'], 1)
        self.assertEqual(tasks['T2']['status'], 'COMPLETED')
        gates = json.loads((root / 'state.json').read_text())['gate_history']
        self.assertEqual([g['gate'] for g in gates], ['TASK_EXECUTION_GATE'])

    def test_authorization_requires_unknown_and_extension_decisions(self):
        staged = self.stage()
        auth = self.auth(staged); auth['accept_unknown_history'] = False
        with self.assertRaisesRegex(TaskError, 'Unknown/ambiguous'):
            activate(self.target, staged['preparation'], auth)
        auth = self.auth(staged); auth['profile_sha256'] = '0' * 64
        with self.assertRaisesRegex(TaskError, 'stale'):
            activate(self.target, staged['preparation'], auth)
        self.assertFalse((self.target / 'execution/active.json').exists())

    def test_duplicate_and_missing_consumption_refused(self):
        self.request['supplemental_invocations'].append(copy.deepcopy(self.request['supplemental_invocations'][0]))
        with self.assertRaisesRegex(TaskError, 'duplicates'):
            self.stage()
        self.request['supplemental_invocations'] = []
        with self.assertRaisesRegex(TaskError, 'unmapped'):
            self.stage()

    def test_exhausted_caps_refuse_dispatch_and_explicit_extension_works(self):
        self.request['profile']['cli_estimate_usd'] = 9
        staged = self.stage()
        with self.assertRaisesRegex(RecoveryStop, 'cap exhausted'):
            self.select(staged)
        self.assertFalse((self.target / 'execution/active.json').exists())

    def test_actual_runtime_attempt_reset_and_stale_source_refused(self):
        staged = self.stage()
        root = self.target / 'execution-runtimes' / staged['proposal']['transition_id']
        journal = TaskJournal(root / '03-dev/tasks'); state = journal.load()
        state['tasks']['T1']['attempts'] = 0
        journal.append('INVALID_RESET', state)
        with self.assertRaisesRegex(TaskError, 'runtime changed'):
            self.select(staged)

    def test_planning_entrypoints_and_phases_cannot_reset_sessions(self):
        staged = self.stage(); self.select(staged)
        limits = LimitsStore(runtime_root(self.target))
        limits.guard_entrypoint('compile_evidence_pack')  # deterministic final evidence, including stop reports
        for name in ('run_phase_design', 'run_phase_recon', 'run_phase_po'):
            with self.assertRaisesRegex(TaskError, 'earlier'):
                limits.guard_entrypoint(name)
        with self.assertRaisesRegex(TaskError, 'scope'):
            limits.require_capacity('02-design-task-plan', '02-design')
        identity = limits.reserve('03-dev', '03-dev', 'codex')
        limits.finish(identity, 'not_dispatched')
        with self.assertRaisesRegex(TaskError, 'no confirmed outcome'):
            self.uncertain(limits)

    def uncertain(self, limits):
        limits.reserve('03-dev', '03-dev', 'codex')
        limits.require_capacity('03-dev', '03-dev')

    def test_activation_crashes_are_idempotent(self):
        staged = self.stage()
        with self.assertRaises(OSError):
            activate(self.target, staged['preparation'], self.auth(staged),
                     fault=lambda p: (_ for _ in ()).throw(OSError('crash')) if p == 'after_activation' else None)
        selected = self.select(staged)
        self.assertEqual(selected['transition_id'], staged['proposal']['transition_id'])
        self.assertEqual(len(LimitsStore(runtime_root(self.target))._load(repair=False)[1]['invocations']), 6)

    def test_preparation_failure_discard_and_second_prepare(self):
        from ssdlc_preparations import unfinished, discard
        with self.assertRaisesRegex(RecoveryStop, 'baseline failed'):
            prepare(self.target, self.repo, self.request, runtime_identity=self.identity,
                    controller_snapshot=self.controller, verifier=lambda *a: ('Ran 1 test\nFAILED', '', 1))
        self.assertEqual(runtime_root(self.target), self.original['root'])
        ids = unfinished(self.target, namespace='execution')
        self.assertEqual(len(ids), 1)
        with self.assertRaisesRegex(TaskError, 'native discard'): self.stage()
        discard(self.target, ids[0], namespace='execution', writers_quiescent=lambda r: True)
        self.assertEqual(unfinished(self.target, namespace='execution'), [])
        self.select(self.stage())

    def test_all_publication_crash_boundaries_replay_without_reset(self):
        staged = self.stage()
        for boundary in ('intent_published', 'generation_staged', 'before_activation'):
            with self.subTest(boundary=boundary):
                with self.assertRaises(OSError):
                    activate(self.target, staged['preparation'], self.auth(staged),
                        fault=lambda point: (_ for _ in ()).throw(OSError('crash')) if point == boundary else None)
                self.assertFalse((self.target / 'execution/active.json').exists())
                with self.assertRaisesRegex(TaskError, 'pending'): runtime_root(self.target)
        self.select(staged)
        self.assertEqual(len(LimitsStore(runtime_root(self.target))._load(repair=False)[1]['invocations']), 6)

    def test_effective_scheduler_floor_cannot_be_reset_by_valid_append(self):
        staged = self.stage(); self.select(staged)
        root = runtime_root(self.target)
        envelope = LimitsStore(root)._load(repair=False)[0]
        journal = TaskJournal(root / '03-dev/tasks'); current = journal.load()
        current['tasks']['T1']['capture_verifications'] = []
        journal.append('INVALID_RESET', current)
        scheduler = TaskScheduler(root / 'worktree', root / '03-dev/tasks', self.new_plan,
            json.loads((root / '00-recon/quality-contract.json').read_text()), self.context,
            lambda *a: self.fail('No dispatch'), self.verify, isolate_candidates=True,
            runtime_identity=self.identity, recover_timeouts=True, max_attempts=2,
            historical_capture_authority=envelope['binding']['historical_capture_bindings_sha256'],
            historical_task_floors=envelope['binding']['task_floors'])
        # The original source had no checks yet. The transition itself also
        # needs to freeze its newly consumed verification collection.
        self.assertTrue(envelope['binding']['task_floors']['T1']['capture_verifications'])
        with self.assertRaisesRegex(TaskError, 'allowance reset'): scheduler.run(self.new_plan['tasks'])

    def test_source_change_and_concurrent_writer_block_activation(self):
        from ssdlc_tasks import ticket_lock
        staged = self.stage()
        with ticket_lock(self.target / 'ticket.lock'):
            with self.assertRaises(TaskError): self.select(staged)
        source = self.original['root'] / '03-dev/tasks/T1/attempt-1/developer.log'
        source.write_text('changed historical transcript')
        with self.assertRaisesRegex(TaskError, 'source changed'): self.select(staged)

    def test_supervisor_is_pinned_and_routes_only_authorized_phases(self):
        from ssdlc_supervisor import pinned_root, route_child, SupervisorIntegrityError, peek_journal
        old_record = {'authorized': {}}
        staged = self.stage(); self.select(staged)
        with self.assertRaises(SupervisorIntegrityError): pinned_root(self.repo, 'TKT-1', old_record)
        record = {'authorized': {'runtime_root': str(runtime_root(self.target).relative_to(self.repo))}}
        self.assertEqual(pinned_root(self.repo, 'TKT-1', record), runtime_root(self.target))
        self.assertFalse(peek_journal(self.repo, 'TKT-1', record)[1])
        with patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('codex', 'offline', 'low', 'test')):
            routes = route_child(self.repo, 'run', 'M', 'TKT-1')['routes']
        self.assertIn('03-dev', routes)
        self.assertFalse(any(k.startswith(('00-', '01-', '02-')) for k in routes))

    def test_staged_candidate_source_change_invalidates_authorization(self):
        staged = self.stage()
        root = self.target / 'execution-runtimes' / staged['proposal']['transition_id']
        (root / 'worktree/next.py').write_text('unauthorized = True\n')
        with self.assertRaisesRegex(TaskError, 'source changed'): self.select(staged)

    def test_known_extension_must_be_explicit_and_native_caps_still_apply(self):
        self.request['profile']['development_attempts'] = 3
        staged = self.stage()
        auth = self.auth(staged); auth['authorize_extensions'] = False
        with self.assertRaisesRegex(TaskError, 'extensions'): activate(self.target, staged['preparation'], auth)
        self.select(staged)
        limits = LimitsStore(runtime_root(self.target))
        self.assertEqual(limits._load(repair=False)[0]['policy']['development_attempts'], 3)
        self.assertEqual(limits._load(repair=False)[1]['historical']['tasks']['T1']['attempts'], 1)

    def test_native_cli_inspect_prepare_activate_keeps_controller_offline(self):
        import contextlib
        import io
        from orchestrator import main as cli, SSDLCOrchestrator, EngineRunner
        from ssdlc_controller import execution_contract
        directory = Path(self.tmp.name) / 'request-files'; directory.mkdir()
        atomic_json(directory / 'request.json', self.request)
        common = ['--repo', str(self.repo), '--ticket', 'TKT-1']
        def invoke(words):
            output = io.StringIO()
            with contextlib.redirect_stdout(output): code = cli(words + common)
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue().splitlines()[-1])
        with patch.object(SSDLCOrchestrator, '__init__', side_effect=AssertionError('No model controller')):
            result = invoke(['continuation-limits-inspect'])
            self.assertEqual(len(result['ledger']), 1)
            self.assertNotIn('review-response-1', json.dumps(result))
            environment = self.identity['environment'] | {'mode': 'role_container'}
            with patch.object(EngineRunner, '_should_spawn_role_container', return_value=True), \
                 patch.object(SSDLCOrchestrator, '_verification_environment', return_value=environment), \
                 patch('ssdlc_controller.controller_snapshot', return_value=self.controller), \
                 patch('ssdlc_controller.assert_controller'), patch('ssdlc_preparations.docker_quiescent', return_value=True), \
                 patch.object(EngineRunner, 'run_sandboxed_cmd', side_effect=lambda argv,cwd,timeout,verification_worktree: self.verify(argv,cwd,timeout,verification_worktree)):
                result = invoke(['continuation-limits-prepare', '--request', str(directory / 'request.json')])
                preparation = Path(result['preparation'])
                proposal = json.loads((preparation / 'proposal.json').read_text())
                atomic_json(directory / 'auth.json', self.auth({'proposal': proposal}))
                result = invoke(['continuation-limits-activate', '--preparation', str(preparation),
                                 '--authorization', str(directory / 'auth.json')])
                self.assertTrue(result['model_dispatch_authorized'])
                self.assertEqual(result['model_dispatches'], 0)

    def test_effective_limit_anchor_cannot_be_reset_after_activation(self):
        staged = self.stage(); self.select(staged)
        limits = LimitsStore(runtime_root(self.target))
        for field, value in [('deadline_at', time.time() + 100000), ('combined_reported_usd_cap', 100)]:
            original = limits.journal.load(); changed = copy.deepcopy(original)
            changed[field] = value
            limits.journal.append('INVALID_RESET', changed)
            with self.assertRaisesRegex(TaskError, 'anchor changed'): limits.require_capacity('03-dev', '03-dev')
            limits.journal.append('RESTORE_TEST', original)

    def test_sigkill_after_atomic_selection_does_not_repeat_developer_or_accounting(self):
        import multiprocessing
        import os
        import signal
        staged = self.stage()
        def child():
            activate(self.target, staged['preparation'], self.auth(staged),
                     fault=lambda point: os.kill(os.getpid(), signal.SIGKILL) if point == 'after_activation' else None)
        process = multiprocessing.get_context('fork').Process(target=child)
        process.start(); process.join(20)
        if process.is_alive():
            process.kill(); process.join(); self.fail('Activation did not reach crash boundary')
        self.assertEqual(process.exitcode, -signal.SIGKILL)
        selected = self.select(staged)
        self.assertEqual(selected['transition_id'], staged['proposal']['transition_id'])
        root = runtime_root(self.target)
        self.assertEqual(len(LimitsStore(root)._load(repair=False)[1]['invocations']), 6)
        self.assertEqual(TaskJournal(root / '03-dev/tasks').peek()['tasks']['T1']['attempts'], 1)

    def test_already_verified_i1_checkpoint_gets_fresh_checks_without_new_commit(self):
        from ssdlc_continuations import verify_import
        verify_import(self.repo, 'TKT-1', verifier=self.verify, runtime_identity=self.identity,
                      controller_guard=lambda: None)
        previous = inspect_i1(self.target)
        record = previous['journal']['tasks']['T1']
        self.assertEqual(record['status'], 'COMPLETED')
        staged = self.stage(); self.select(staged)
        current = TaskJournal(runtime_root(self.target) / '03-dev/tasks').peek()['tasks']['T1']
        self.assertEqual(current['commit'], record['commit'])
        self.assertEqual(current['attempts'], 1)
        self.assertEqual(len(current['capture_verifications']), len(record['capture_verifications']) + 1)
        self.assertNotEqual(current['checks'], record['checks'])


class FailureHistoryTests(ReconciliationTests):
    def before_import(self):
        worktree = self.wt
        (worktree / 'app.py').write_text('value = 1\n')
        git(worktree, 'add', '--', 'app.py')
        tree = git(worktree, 'write-tree')
        diff = git(worktree, 'diff', '--cached', '--binary')
        git(worktree, 'reset', '--hard', self.base)
        directory = self.artifacts / '03-dev/tasks/T1/attempt-1'
        (directory / 'implementation.diff').write_text(diff)
        checks = []
        for index, check_id in enumerate(('CHECK-T1', 'CHECK-GLOBAL')):
            path = directory / ('check-' + str(index) + '.log')
            path.write_text('Ran 1 test\nFAILED')
            checks.append({'check_id': check_id, 'exit_code': 1, 'empty_suite': False,
                           'log_ref': 'T1/attempt-1/' + path.name, 'sha256': file_hash(path)})
        failure = {'attempt': 1, 'category': 'CHECK_FAILURE', 'base_commit': self.base,
                   'tree': tree, 'changed_files': ['app.py'], 'diff_ref': 'T1/attempt-1/implementation.diff',
                   'diff_sha256': file_hash(directory / 'implementation.diff'), 'checks': checks,
                   'developer_output': 'Historical check failed'}
        journal = TaskJournal(self.artifacts / '03-dev/tasks'); current = journal.load()
        current['tasks']['T1']['failed_attempts'] = [failure]
        journal.append('HISTORICAL_CHECK_FAILURE', current)

    def test_old_i1_lost_failure_prefix_is_restored_and_checked(self):
        root = self.original['root']
        journal = TaskJournal(root / '03-dev/tasks'); current = journal.load()
        current['tasks']['T1'].pop('failed_attempts')
        journal.append('OLD_I1_LOST_ACTIVE_HISTORY', current)
        self.assertTrue(inspect_i1(self.target)['journal']['tasks']['T1']['failed_attempts'])
        staged = self.stage(); self.select(staged)
        tasks = TaskJournal(runtime_root(self.target) / '03-dev/tasks').peek()['tasks']
        self.assertEqual(tasks['T1']['failed_attempts'][0]['category'], 'CHECK_FAILURE')
        self.assertTrue(tasks['T1']['failed_attempts'][0]['diff_ref'].startswith('origin-history/'))

    def test_prior_reconciliation_gate_never_disappears_during_import(self):
        # Simulate the old I1 history-only bug with a non-code stop.
        root = self.original['root']
        projection = self.original['projection']
        # Immutable original facts cannot be patched to invent a different
        # category. The import projection unit below exercises real source data.
        from ssdlc_continuations import import_projection, inspect_source
        source = inspect_source(self.artifacts)
        source['journal']['tasks']['T1']['failed_attempts'][0]['category'] = 'REPAIR_STALLED'
        projected = import_projection(source, plan=self.new_plan, quality=self.quality, context=self.context,
                                      runtime_identity=self.identity, task_id='T1')
        self.assertEqual(projected['tasks']['T1']['reconciliation_required'], ['REPAIR_STALLED'])
        self.assertEqual(projected['tasks']['T1']['failed_attempts'][0]['category'], 'REPAIR_STALLED')

    def test_failed_partial_candidate_repairs_next_attempt_and_stagnation_remains_active(self):
        def checks(argv, cwd, timeout, workspace):
            return ('Ran 1 test\nFAILED', '', 1) if (workspace / 'app.py').read_text() == 'value = 1\n' else ('Ran 1 test\nOK', '', 0)
        staged = prepare(self.target, self.repo, self.request, runtime_identity=self.identity,
                         controller_snapshot=self.controller, verifier=checks)
        self.select(staged)
        root = runtime_root(self.target)
        envelope = LimitsStore(root)._load(repair=False)[0]
        calls = []
        def same_repair(task, attempt, prompt, workspace):
            calls.append(attempt)
            self.assertIn('origin-history/T1/attempt-1/implementation.diff', prompt)
            return {'output': 'unchanged repair', 'exit_code': 0, 'quiescent': True}
        scheduler = TaskScheduler(root / 'worktree', root / '03-dev/tasks', self.new_plan,
            json.loads((root / '00-recon/quality-contract.json').read_text()), self.context,
            same_repair, checks, isolate_candidates=True, runtime_identity=self.identity,
            recover_timeouts=True, max_attempts=2,
            historical_capture_authority=envelope['binding']['historical_capture_bindings_sha256'],
            historical_task_floors=envelope['binding']['task_floors'])
        with self.assertRaisesRegex(TaskError, 'REPAIR_STALLED'): scheduler.run(self.new_plan['tasks'])
        self.assertEqual(calls, [2])
        self.assertEqual(TaskJournal(root / '03-dev/tasks').peek()['tasks']['T1']['attempts'], 2)


class BlockedHistoryTests(ReconciliationTests):
    def before_import(self):
        FailureHistoryTests.before_import(self)
        journal = TaskJournal(self.artifacts / '03-dev/tasks'); current = journal.load()
        current['tasks']['T1']['failed_attempts'][0]['category'] = 'REPAIR_STALLED'
        journal.append('HISTORICAL_RECONCILIATION_GATE', current)

    def test_native_admission_refuses_unresolved_historical_gate(self):
        with self.assertRaisesRegex(TaskError, 'REPAIR_STALLED'): self.stage()
        self.assertFalse((self.target / 'execution/active.json').exists())
        self.assertEqual(inspect_i1(self.target)['journal']['tasks']['T1']['reconciliation_required'], ['REPAIR_STALLED'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (ReconciliationTests, FailureHistoryTests, BlockedHistoryTests)
                              for name in cls.__dict__ if name.startswith('test_'))
