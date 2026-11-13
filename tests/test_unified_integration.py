"""Offline acceptance journeys through CLI, pipeline and scheduler entry points.

Provider processes and sandbox commands are deterministic fakes. Existing journal,
validation and fault cases are selected explicitly by load_tests, so this focused
suite reuses their assertions without importing TestCase classes into discovery.
No documentation word matching is used: examples below execute their behavior.
"""
import contextlib
import copy
import inspect
import io
import itertools
import json
import os
import unittest
import uuid
from contextlib import ExitStack
from unittest.mock import patch

import orchestrator as o
import ssdlc_corrective as corrective
from ssdlc_tasks import TaskError, TaskJournal, atomic_json, git
import test_unified_execution as unified
import test_corrective_executor as executor
import test_corrective_recovery as recovery
import test_corrective_creation as creation
import test_bootstrap as bootstrap_fixtures
import test_legacy_compat as legacy
from test_full_checkpoint_review import framed


@contextlib.contextmanager
def existing_host(host):
    """Reuse fixture initialization and preserve the parser's real choices."""
    phases = type(host).PHASE_ORDER
    with patch.object(o, 'SSDLCOrchestrator', return_value=host) as constructor:
        constructor.PHASE_ORDER = phases
        yield


class StartupJourneys(unified.UnifiedExecutionTests):
    make_orchestrator = unified.UnifiedExecutionTests.make_orchestrator
    seed_inputs = unified.UnifiedExecutionTests.seed_inputs
    setUp = unified.UnifiedExecutionTests.setUp

    def new_run(self, flag=None, repair=False, bootstrap=False, outcome=None):
        if bootstrap:
            atomic_json(self.repo / '.ssdlc/bootstrap-profile.json', dict(
                schema_version='1.0', profile_id='python-unittest', window_tasks=1))
        else:
            tests = self.repo / 'tests'
            tests.mkdir()
            (tests / 'test_app.py').write_text('import unittest\nclass TestApp(unittest.TestCase):\n    def test_value(self): self.assertTrue(True)\n')
            git(self.repo, 'add', 'tests/test_app.py')
            git(self.repo, 'commit', '-m', 'test convention')
        orch = self.make_orchestrator()
        self.active_orch = orch
        orch.state['specialists_triggered'] = ['APPSEC_SUPPLY_CHAIN_REDTEAM']
        orch._save_state()
        planned = copy.deepcopy(self.plan)
        planned['base_commit'] = orch.state['base_commit']
        planned['tasks'][0]['verification']['quality_check_refs'] = ['CHECK-TESTS']
        if bootstrap:
            planned['tasks'][0]['target_files'].append('tests/test_app.py')
        calls = []
        developers = []
        def dispatch(role, *args, **kwargs):
            return ('codex' if role == '03-dev' else 'claude'), ('reviewer' if role.endswith('sec') else 'planner'), 'low', 'fixture'
        def invoke(engine, model, system, user, *args, **kwargs):
            phase = args[-1]
            calls.append(phase)
            if phase == '00-recon':
                out = 'Small scoped change\nRISK_TIER: S\nDEV_PROFILE: ALGORITHMIC'
            elif phase == '01-po':
                out = 'AC-1: Correct behavior\nSEC-1: Secure behavior\n'
            elif phase == '02-design-task-plan':
                self.assertIn('Tier S requires exactly one plan node', system)
                candidate = copy.deepcopy(planned)
                if repair and calls.count(phase) == 1:
                    candidate['tasks'].append(self.task('T2', [], [], ['other.py']))
                    candidate['tasks'][1]['verification']['quality_check_refs'] = ['CHECK-TESTS']
                out = json.dumps(candidate)
            elif phase == '02-design-task-plan-review':
                out = json.dumps(dict(verdict='APPROVED', findings=[]))
            elif phase in ('06-ops', '07-docs'):
                out = 'Verified candidate documentation'
            else:
                gate = {'04-review': 'review', '05-qa': 'qa', '05b-appsec': 'appsec'}[orch.state['current_phase']]
                out = framed(gate, orch)
            return out, 0, 0, 0, 'fixture'
        def develop(prompt, **kwargs):
            stack = inspect.stack()
            self.assertTrue(any(f.function in ('_run', '_execute_task') and isinstance(f.frame.f_locals.get('self'), o.TaskScheduler) for f in stack))
            developers.append(prompt)
            (kwargs['cwd'] / 'app.py').write_text('value = 1\n')
            if bootstrap:
                tests = kwargs['cwd'] / 'tests'
                tests.mkdir(exist_ok=True)
                (tests / 'test_app.py').write_text('import unittest\nclass TestApp(unittest.TestCase):\n    def test_value(self): self.assertTrue(True)\n')
            return 'TASK_DEV_STATUS: COMPLETE', 0
        def check(argv, *args, **kwargs):
            if argv == ['id', '-u']:
                return '1000', '', 0
            if outcome is not None:
                return outcome
            return 'Ran 1 test\nOK', '', 0
        def claude(prompt, **kwargs):
            # Exercise invoke_role's real routing, telemetry and error boundary.
            frame = next(f for f in inspect.stack() if f.function == 'invoke_role')
            phase = frame.frame.f_locals['phase']
            out, cost, tin, tout, _ = invoke('claude', 'fake', prompt, '', phase)
            return out, 0, cost, tin, tout
        env = dict(os.environ, SSDLC_EXECUTION_MODE='local', GIT_AUTHOR_DATE='2026-01-01T00:00:00Z', GIT_COMMITTER_DATE='2026-01-01T00:00:00Z')
        env.pop('SSDLC_ENABLE_TASK_BREAKDOWN', None)
        if flag is not None:
            env['SSDLC_ENABLE_TASK_BREAKDOWN'] = flag
        invocation_ids = itertools.count(1)
        with patch('ssdlc_tasks.uuid.uuid4', side_effect=lambda: uuid.UUID(int=next(invocation_ids))), \
             patch.dict(os.environ, env, clear=True), patch.object(o, '_OBSOLETE_FLAG_WARNED', False), \
             existing_host(orch), \
             patch.object(o.SquadDispatcher, 'dispatch_role', side_effect=dispatch), \
             patch.object(o.SquadDispatcher, 'resolve_review_models', return_value=(['claude:reviewer'], 'low')), \
             patch.object(o.EngineRunner, 'run_claude', side_effect=claude), \
             patch.object(o.EngineRunner, 'run_codex', side_effect=develop), \
             patch.object(o.EngineRunner, 'run_sandboxed_cmd', side_effect=check), \
             patch.object(orch, '_run_appsec_scans', return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()):
            logs = io.StringIO()
            import logging
            handler = logging.StreamHandler(logs)
            logger = logging.getLogger(o.__name__)
            logger.addHandler(handler)
            try:
                self.assertEqual(o._foreground_main(['run', '--repo', str(self.repo), '--ticket', 'TKT-1', '--goal', 'Scoped change', '--tier', 'S', '--no-sync-remote', '--test-cmd', 'printf shell-smoke']), 0)
            finally:
                logger.removeHandler(handler)
        self.assertEqual(orch.state['status'], 'AWAITING_SUPERVISOR_SIGNOFF')
        self.assertEqual(len(developers), 1)
        self.assertTrue((orch.dir_design / 'plan.json').is_file())
        self.assertEqual(len(json.loads((orch.dir_design / 'plan.json').read_text())['tasks']), 1)
        journal = (orch.dir_dev / 'tasks/task-journal.jsonl').read_bytes()
        state, sequence, previous = TaskJournal._replay(journal)
        self.assertEqual(state['tasks']['T1']['status'], 'COMPLETED')
        self.assertGreater(sequence, 1)
        self.assertTrue(any(g['gate'].startswith('CODE_REVIEW') and g['passed'] for g in orch.state['gate_history']))
        self.assertTrue(any(g['gate'].startswith('QA_') and g['passed'] for g in orch.state['gate_history']))
        self.assertTrue(any(g['gate'].startswith('APPSEC_') and g['passed'] for g in orch.state['gate_history']))
        quality = json.loads((orch.dir_recon / 'quality-contract.json').read_text())
        if bootstrap:
            self.assertEqual(quality['baseline'], [])
            self.assertEqual(quality['bootstrap']['baseline_status'], 'absent')
            self.assertEqual(state['bootstrap']['status'], 'PROVEN')
        self.assertTrue(all(c['candidate_proof'] for c in quality['checks'] if c['required']))
        self.assertNotIn('printf shell-smoke', json.dumps(quality['checks']))
        qa = (orch.dir_qa / 'deterministic-checks-a1.md').read_text()
        self.assertIn('shell_command', qa)
        self.assertNotIn('equivalent', qa)
        self.assertIn('shell_command', (orch.ssdlc_root / 'EVIDENCE_PACK.md').read_text())
        return journal, logs.getvalue(), calls


    def test_documented_default_cli_and_tier_s(self):
        self.new_run()

    def test_documented_bootstrap_profile(self):
        self.new_run(bootstrap=True)

    def test_bootstrap_empty_checkpoint_and_restart(self):
        with patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '1'}):
            with self.assertRaises(TaskError):
                self.new_run(bootstrap=True, outcome=('Ran 0 tests\nOK', '', 0))
        self.assert_bootstrap_restart_blocked()

    def test_bootstrap_failing_checkpoint_and_restart(self):
        with patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '1'}):
            with self.assertRaises(TaskError):
                self.new_run(bootstrap=True, outcome=('Ran 1 test\nFAILED', '', 1))
        self.assert_bootstrap_restart_blocked()

    def test_existing_task_cli_resume_preserves_seals_attempts_and_budget(self):
        self.new_run()
        orch = self.active_orch
        contract = orch.dir_recon / 'quality-contract.json'
        sealed = contract.read_bytes()
        journal = orch.dir_dev / 'tasks/task-journal.jsonl'
        prefix = journal.read_bytes()
        previous = TaskJournal(orch.dir_dev / 'tasks').peek()
        ledger = copy.deepcopy(orch.state['ledger'])
        with patch.dict(os.environ, {'SSDLC_EXECUTION_MODE': 'local'}), existing_host(orch), \
             patch.object(o.EngineRunner, 'run_claude', side_effect=AssertionError('new provider dispatch')), \
             patch.object(o.EngineRunner, 'run_codex', side_effect=AssertionError('new developer dispatch')), \
             patch.object(o.EngineRunner, 'run_sandboxed_cmd', side_effect=lambda argv, *a, **k:
                 ('1000' if argv == ['id', '-u'] else 'Ran 1 test\nOK', '', 0)), \
             patch.object(o.SquadDispatcher, 'dispatch_role', side_effect=lambda role, *a, **k:
                 ('codex' if role == '03-dev' else 'claude', 'reviewer' if role.endswith('sec') else 'planner', 'low', 'fixture')), \
             patch.object(o.SquadDispatcher, 'resolve_review_models', return_value=(['claude:reviewer'], 'low')), \
             patch.object(orch, '_run_appsec_scans', return_value=[]):
            self.assertEqual(o._foreground_main(['run', '--repo', str(self.repo), '--ticket', 'TKT-1',
                '--goal', 'Scoped change', '--from-phase', '03-dev', '--no-sync-remote',
                '--test-cmd', 'printf shell-smoke']), 0)
        self.assertEqual(contract.read_bytes(), sealed)
        self.assertTrue(journal.read_bytes().startswith(prefix))
        self.assertEqual(TaskJournal(orch.dir_dev / 'tasks').peek()['tasks']['T1']['attempts'],
                         previous['tasks']['T1']['attempts'])
        self.assertEqual(orch.state['ledger'], ledger)

    def assert_bootstrap_restart_blocked(self):
        orch = self.active_orch
        state = TaskJournal(orch.dir_dev / 'tasks').peek()
        self.assertEqual(state['bootstrap']['status'], 'CHECKPOINT_STARTED')
        contract = (orch.dir_recon / 'quality-contract.json').read_bytes()
        with existing_host(orch), \
             patch.object(o.EngineRunner, 'run_claude', side_effect=AssertionError('provider')), \
             patch.object(o.EngineRunner, 'run_codex', side_effect=AssertionError('developer')), \
             patch.object(o.EngineRunner, 'run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)), \
             self.assertRaisesRegex(TaskError, 'interrupted'):
            o._foreground_main(['run', '--repo', str(self.repo), '--ticket', 'TKT-1',
                '--goal', 'Scoped change', '--from-phase', '03-dev', '--no-sync-remote'])
        self.assertEqual(contract, (orch.dir_recon / 'quality-contract.json').read_bytes())


class CorrectiveJourneys(executor.ExecutorFixture):
    def setUp(self):
        super().setUp()
        self.enable_native()

    def fakes(self):
        stack = ExitStack()
        stack.enter_context(patch.object(o.EngineRunner, '_should_spawn_role_container', return_value=True))
        stack.enter_context(patch.object(o.SquadDispatcher, 'dispatch_role', side_effect=lambda role, *a, **k: ('codex' if role == '03-dev' else 'claude', 'fake', 'low', 'test')))
        stack.enter_context(patch.object(o.SquadDispatcher, 'resolve_review_models', return_value=(['claude:reviewer'], 'low')))
        def claude(prompt, cwd, **kwargs):
            output, cost, tin, tout, _ = self.provider('claude', 'fake', '', prompt, cwd)
            return output, 0, cost, tin, tout
        stack.enter_context(patch.object(o.EngineRunner, 'run_claude', side_effect=claude))
        stack.enter_context(patch.object(o.EngineRunner, 'run_codex', side_effect=self.develop))
        stack.enter_context(patch.object(o.EngineRunner, 'run_sandboxed_cmd', side_effect=self.verify_cmd))
        stack.enter_context(patch.object(self.orch, '_run_deterministic_scanners', return_value=(True, 'checks passed')))
        stack.enter_context(patch.object(self.orch, '_run_appsec_scans', return_value=[]))
        stack.enter_context(patch.object(self.orch, 'run_phase_ops'))
        stack.enter_context(patch.object(self.orch, 'run_phase_docs'))
        return stack

    def cli(self):
        with self.fakes(), existing_host(self.orch):
            return o._foreground_main(['run', '--repo', str(self.repo), '--ticket', 'TKT-1',
                '--goal', 'Scoped change', '--from-phase', '03-dev', '--no-sync-remote'])

    def assert_approved(self, gate):
        self.reject = gate
        self.orch.state['risk_tier'] = 'L'
        self.orch._save_state()
        old = self.orch.state['task_verified_head']
        self.assertEqual(self.cli(), 0)
        head = self.orch.state['task_verified_head']
        self.assertNotEqual(old, head)
        self.assertEqual(self.invalidations(), 1)
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual([g for g, a, h in self.gates if a == 2], ['review', 'qa', 'appsec'])
        self.assertTrue(all(h == head for g, a, h in self.gates if a == 2))
        self.assertEqual(self.txn().peek()['status'], 'APPROVED')
        self.assertEqual(self.orch.state['status'], 'AWAITING_SUPERVISOR_SIGNOFF')

    def test_review_root_repair_dependent_revalidation_and_fresh_gates(self):
        self.add_dependent()
        self.assert_approved('review')
        dependent = self.journal.peek()['tasks']['T2']
        self.assertEqual(dependent['attempts'], 1)
        self.assertEqual(dependent['revalidations'][-1]['status'], 'PASSED')

    def test_documented_bounce_preview_cli(self):
        with self.fakes():
            self.orch.run_phase_review(attempt=1)
        before = self.journal.path.read_bytes()
        with self.fakes(), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(o._foreground_main(['bounce-preview', '--repo', str(self.repo),
                '--ticket', 'TKT-1', '--phase', '04-review', '--attempt', '1']), 0)
        self.assertEqual(before, self.journal.path.read_bytes())
        self.assertEqual(self.dispatches, [])
        previews = list((self.orch.ssdlc_root / 'bounce-preview').rglob('plan.json'))
        self.assertEqual(len(previews), 1)
        self.assertIs(json.loads(previews[0].read_text())['execution_authorized'], False)

    def test_qa_rejection(self):
        self.assert_approved('qa')

    def test_appsec_rejection(self):
        self.assert_approved('appsec')

    def test_attempts_exhausted_through_pipeline(self):
        self.checkpoint['tasks']['T1']['attempts'] = 2
        self.journal.append('TASK_COMPLETED', self.checkpoint)
        with patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '2'}), \
             self.assertRaises(corrective.CorrectiveError) as error:
            self.cli()
        self.assertEqual(error.exception.code, 'attempts_exhausted')
        self.assertEqual(self.invalidations(), 0)
        self.assertEqual(self.dispatches, [])
        self.assertEqual(self.journal.peek()['tasks']['T1']['attempts'], 2)

    def test_stale_evidence_refused_through_pipeline(self):
        original = self.orch._execute_corrective
        def stale(phase, attempt, preview):
            self.journal.append('TEST_STALE', self.journal.peek())
            return original(phase, attempt, preview)
        source = (self.orch.worktree_dir / 'app.py').read_bytes()
        with patch.object(self.orch, '_execute_corrective', side_effect=stale), self.assertRaises(corrective.CorrectiveError):
            self.cli()
        self.assertEqual(self.invalidations(), 0)
        self.assertEqual(self.dispatches, [])
        self.assertEqual(source, (self.orch.worktree_dir / 'app.py').read_bytes())

    def test_source_mutation_refused_through_pipeline(self):
        original = self.orch._execute_corrective
        def mutate(phase, attempt, preview):
            (self.orch.worktree_dir / 'app.py').write_text('external mutation\n')
            return original(phase, attempt, preview)
        with patch.object(self.orch, '_execute_corrective', side_effect=mutate), self.assertRaises(corrective.CorrectiveError):
            self.cli()
        self.assertEqual(self.invalidations(), 0)
        self.assertEqual(self.dispatches, [])
        self.assertEqual((self.orch.worktree_dir / 'app.py').read_text(), 'external mutation\n')

    def recover_pipeline(self, point):
        def fault(name):
            if name == point:
                raise executor.Crash(point)
        with patch('orchestrator._corrective_fault', side_effect=fault), self.assertRaises(executor.Crash):
            self.cli()
        before = self.journal.path.read_bytes()
        calls = len(self.dispatches)
        self.assertEqual(self.cli(), 0)
        self.assertTrue(self.journal.path.read_bytes().startswith(before))
        additions = self.events()[len(before.splitlines()):]
        self.assertNotIn('TASKS_INVALIDATED', [e['event'] for e in additions])
        self.assertEqual(self.invalidations(), 1)
        self.assertEqual(len(self.dispatches), 1)
        if calls:
            self.assertEqual([e['event'] for e in additions], [
                'REVALIDATION_FINISHED', 'PREFLIGHT_STARTED',
                'CHECK_FINISHED', 'PREFLIGHT_PASSED'])
        else:
            kinds = [e['event'] for e in additions]
            for event in ('REVALIDATION_STARTED', 'TASK_STARTED', 'TASK_COMPLETED',
                          'REVALIDATION_FINISHED', 'PREFLIGHT_STARTED', 'PREFLIGHT_PASSED'):
                self.assertEqual(kinds.count(event), 1, event)
        self.assertEqual(self.txn().peek()['status'], 'APPROVED')

    def test_cli_recovery_after_invalidation(self):
        self.recover_pipeline('invalidate.after_call_before_ack')

    def test_cli_recovery_after_repair(self):
        self.recover_pipeline('repair.after_commit_before_ack')


def load_tests(loader, standard_tests, pattern):
    # Explicit selection keeps focused acceptance coverage stable and avoids
    # executing imported TestCases accidentally through unittest discovery.
    suite = unittest.TestSuite()
    for cls, names in (
        (StartupJourneys, [
            'test_documented_default_cli_and_tier_s', 'test_documented_bootstrap_profile',
            'test_bootstrap_empty_checkpoint_and_restart', 'test_bootstrap_failing_checkpoint_and_restart',
            'test_obsolete_values_have_identical_journals_and_one_bounded_warning',
            'test_tier_s_two_nodes_are_repaired_before_dispatch',
            'test_historical_scheduler_resume_preserves_contract_and_journal_prefix',
            'test_existing_task_cli_resume_preserves_seals_attempts_and_budget']),
        (CorrectiveJourneys, [name for name in CorrectiveJourneys.__dict__ if name.startswith('test_')]),
        (legacy.CompatibilityTests, ['test_refusals_before_every_constructor',
            'test_read_only_status_and_monitor_without_supervisor_records', 'test_history_import_confined_and_read_only']),
        (bootstrap_fixtures.BootstrapTests, ['test_existing_tests', 'test_dirty', 'test_two_commits', 'test_invalid_profiles']),
        (executor.ExecutorTests, ['test_completed_and_approved_replay_are_zero_write',
            'test_attempt_exhaustion_precedes_invalidation', 'test_every_append_is_verified_from_each_acknowledgment_anchor']),
        (creation.CreationTests, ['test_changed_journal_head', 'test_moved_head_dirty_tree_and_protected_path']),
        (recovery.RecoveryTests, ['test_fault_invalidate_after_call_before_ack',
            'test_fault_repair_after_commit_before_ack', 'test_fault_repair_after_task_started']),
        (recovery.RecoveryEdgeTests, [name for name in recovery.RecoveryEdgeTests.__dict__
            if name.startswith(('test_budget_', 'test_cancel_'))]),
    ):
        for name in names:
            suite.addTest(cls(name))
    return suite
