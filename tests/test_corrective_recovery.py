"""Persistence-boundary faults with real journals and offline providers."""
import copy
import json
import os
import threading
from unittest.mock import patch

import orchestrator
import ssdlc_corrective as corrective
from ssdlc_tasks import TaskError, TaskJournal, atomic_json, digest, file_hash, git, ticket_lock
import test_corrective_executor as executor_fixtures
from test_corrective_executor import Crash


class RecoveryTests(executor_fixtures.ExecutorFixture):
    def crash(self, point, preview):
        def fault(name):
            if name == point:
                raise Crash(point)
        with self.fakes(), patch('orchestrator._corrective_fault', side_effect=fault):
            with self.assertRaises(Crash):
                self.execute(preview)

    def recovered(self, point, *, uncertain=False, dependent=False, failed=False, blocker=False):
        if dependent:
            self.add_dependent()
        self.check_failure = failed
        if blocker:
            self.developer_status = 'ENVIRONMENT_BLOCKED'
        preview = self.preview()
        before = self.journal.path.read_bytes()
        self.crash(point, preview)
        persisted = self.journal.path.read_bytes()
        dispatched = len(self.dispatches)
        self.check_failure = False
        with self.fakes():
            if uncertain or blocker:
                with self.assertRaises(corrective.CorrectiveError) as error:
                    self.resume()
                self.assertEqual(error.exception.code, 'uncertain_dispatch' if uncertain else 'repair_failed')
                self.assertEqual(len(self.dispatches), dispatched)
                self.assertEqual(self.txn().peek()['status'], 'INTERRUPTED' if uncertain else 'FAILED')
            else:
                # Before-record crash has no transaction to reconcile.
                result = self.resume() or self.execute(preview)
                self.assertEqual(result['status'], 'COMPLETED')
                self.assertEqual(len(self.dispatches), 2 if failed and dispatched > 0 else 1)
                self.assertNotEqual(before, self.journal.path.read_bytes())
                journal = self.txn()
                paths = [journal.path, journal.snapshot, self.journal.path, self.journal.snapshot]
                frozen = [p.read_bytes() for p in paths]
                self.execute(preview)
                self.assertEqual(frozen, [p.read_bytes() for p in paths])
        self.assertEqual(self.invalidations(), 1)
        if point == 'invalidate.after_call_before_ack':
            additions = self.events()[len(persisted.splitlines()):]
            self.assertNotIn('TASKS_INVALIDATED', [e['event'] for e in additions])
        if point == 'repair.after_commit_before_ack':
            additions = self.events()[len(persisted.splitlines()):]
            self.assertNotIn('TASK_STARTED', [e['event'] for e in additions])

    def test_two_concurrent_resumes_have_one_invalidation_and_repair(self):
        preview = self.preview()
        self.crash('invalidate.after_intent_before_call', preview)
        errors = []
        entered = threading.Event()
        release = threading.Event()
        original = self.develop
        def develop(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(20))
            return original(*args, **kwargs)
        def first():
            try:
                self.resume()
            except BaseException as exc:
                errors.append(exc)
        with self.fakes(), patch.object(self.orch, '_make_task_scheduler', wraps=self.orch._make_task_scheduler), \
             patch('orchestrator.EngineRunner.run_codex', side_effect=develop):
            worker = threading.Thread(target=first)
            worker.start()
            self.assertTrue(entered.wait(20))
            try:
                with self.assertRaisesRegex(TaskError, 'Another process'):
                    self.resume()
            finally:
                release.set()
                worker.join(30)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.invalidations(), 1)
        self.assertEqual(len(self.dispatches), 1)

    def test_extra_commit_conflicts_without_dispatch(self):
        preview = self.preview()
        self.crash('repair.after_commit_intent_before_git_commit', preview)
        git(self.orch.worktree_dir, 'commit', '-m', 'unexpected commit')
        before = len(self.dispatches)
        with self.fakes(), self.assertRaises(corrective.CorrectiveError) as error:
            self.resume()
        self.assertEqual(error.exception.code, 'txn_record_conflict')
        self.assertEqual(before, len(self.dispatches))

    def test_wrong_tree_conflicts_without_dispatch(self):
        preview = self.preview()
        self.crash('repair.after_commit_intent_before_git_commit', preview)
        (self.orch.worktree_dir / 'app.py').write_text('wrong tree\n')
        git(self.orch.worktree_dir, 'add', 'app.py')
        with self.fakes(), self.assertRaises(corrective.CorrectiveError) as error:
            self.resume()
        self.assertEqual(error.exception.code, 'txn_record_conflict')
        self.assertEqual(len(self.dispatches), 1)

    def test_dirty_outside_target_conflicts_on_uncertain_dispatch(self):
        preview = self.preview()
        self.crash('repair.after_task_started', preview)
        (self.orch.worktree_dir / 'outside.py').write_text('unrelated\n')
        with self.fakes(), self.assertRaises(corrective.CorrectiveError) as error:
            self.resume()
        self.assertEqual(error.exception.code, 'txn_record_conflict')
        self.assertEqual(self.dispatches, [])

    def test_terminal_preflight_failure_is_not_rerun(self):
        preview = self.preview()
        opened = []
        original = self.verify_cmd
        def fault(name):
            if name == 'final.after_preflight_started': opened.append(True)
        def verify(*args, **kwargs):
            if opened:
                return 'Ran 1 test\nFAILED', '', 1
            return original(*args, **kwargs)
        with self.fakes(), patch('orchestrator._corrective_fault', side_effect=fault), \
             patch('orchestrator.EngineRunner.run_sandboxed_cmd', side_effect=verify):
            with self.assertRaises(corrective.CorrectiveError) as error:
                self.execute(preview)
        self.assertEqual(error.exception.code, 'final_checkpoint_failed')
        self.assertEqual(self.txn().peek()['status'], 'FAILED')
        before = self.journal.path.read_bytes()
        with self.fakes(), self.assertRaises(corrective.CorrectiveError): self.resume()
        self.assertEqual(before, self.journal.path.read_bytes())

    def test_passed_preflight_ack_does_not_repeat_checks(self):
        preview = self.preview()
        self.crash('final.after_preflight_passed_before_ack', preview)
        with self.fakes(), patch('ssdlc_tasks.TaskScheduler._verify', side_effect=AssertionError('repeat')):
            self.assertEqual(self.resume()['status'], 'COMPLETED')

    def test_cancel_boundaries_preserve_frozen_limits(self):
        self.add_dependent()
        preview = self.preview()
        self.crash('invalidate.after_call_before_ack', preview)
        before = len(self.dispatches)
        with self.fakes(), patch.object(self.orch, '_cancel_requested', return_value=True):
            with self.assertRaises(corrective.CorrectiveError) as error:
                self.resume()
        self.assertEqual(error.exception.code, 'cancelled')
        self.assertEqual(self.txn().peek()['status'], 'INTERRUPTED')
        self.assertEqual(len(self.dispatches), before)
        record = json.loads((self.txn().directory / 'transaction.json').read_text())
        with self.fakes(), patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '100'}):
            self.assertEqual(self.orch._corrective_limits()['max_attempts'], record['limits']['max_attempts'])
            self.assertEqual(self.resume()['status'], 'COMPLETED')

    def test_native_candidate_isolation_is_preserved(self):
        # Native runs use the same receipt/capture mechanism on corrective dispatch.
        self.checkpoint['candidate_isolation'] = 1
        with self.fakes():
            contract = self.orch._ensure_controller_contract()
            self.checkpoint['candidate_runtime'] = dict(controller_sha256=digest(contract),
                environment=self.orch._verification_environment(self.quality['checks'][0]['argv']))
            self.journal.append('TASK_COMPLETED', self.checkpoint)
            preview = self.preview()
            self.assertEqual(self.execute(preview)['status'], 'COMPLETED')
        kinds = [e['event'] for e in self.events()]
        self.assertIn('DISPATCH_RESERVED', kinds)
        self.assertIn('CANDIDATE_CAPTURED', kinds)
        self.assertTrue(self.journal.peek()['candidate_isolation'])


# Each row has a separately named unittest case and checks actual durable state.
_ROWS = {
    'txn.before_record_write': {}, 'txn.after_record_before_registry': {},
    'txn.after_registry_before_journal': {}, 'txn.after_created': {},
    'invalidate.after_intent_before_call': {}, 'invalidate.after_call_before_ack': {},
    'invalidate.after_ack': {}, 'revalidate.after_run_started': {},
    'repair.after_reserved_before_dispatch': {},
    'repair.after_task_started': {'uncertain': True},
    'repair.after_dispatch_before_verify': {'uncertain': True},
    'repair.after_verifying': {'uncertain': True},
    'repair.after_check_1': {'uncertain': True}, 'repair.after_check_2': {'uncertain': True},
    'repair.after_attempt_failed_before_rollback': {'failed': True},
    'repair.after_rollback_before_next': {'failed': True},
    'repair.after_second_attempt_started': {'failed': True, 'uncertain': True},
    'repair.after_blocker_attempt_failed_before_rollback': {'blocker': True},
    'repair.after_blocker_rollback_before_terminal': {'blocker': True},
    'repair.after_commit_intent_before_git_commit': {},
    'repair.after_git_commit_before_complete': {}, 'repair.after_commit_before_ack': {},
    'revalidate.after_task_started': {'dependent': True},
    'revalidate.after_check_started': {'dependent': True},
    'revalidate.after_check_finished': {'dependent': True},
    'revalidate.after_task_passed_before_ack': {'dependent': True},
    'revalidate.after_last_passed_before_finished': {'dependent': True},
    'revalidate.after_finished_before_preflight': {'dependent': True},
    'final.after_preflight_started': {}, 'final.after_preflight_check': {},
    'final.after_preflight_passed_before_ack': {},
    'publish.after_archive': {}, 'publish.after_intent': {}, 'publish.after_diff_replace': {},
    'publish.after_registry': {}, 'publish.after_state': {}, 'publish.after_checkpoint_event': {},
    'gates.after_completed_before_rerun': {},
}
for _point, _options in _ROWS.items():
    def case(self, point=_point, options=_options):
        self.recovered(point, **options)
    setattr(RecoveryTests, 'test_fault_' + _point.replace('.', '_'), case)

class RecoveryEdgeTests(executor_fixtures.ExecutorFixture):
    def test_wrong_parent_conflicts(self):
        preview = self.preview()
        def fault(point):
            if point == 'repair.after_commit_intent_before_git_commit': raise Crash()
        with self.fakes(), patch('orchestrator._corrective_fault', side_effect=fault), self.assertRaises(Crash):
            self.execute(preview)
        tree = git(self.orch.worktree_dir, 'write-tree')
        commit = git(self.orch.worktree_dir, 'commit-tree', tree, '-p', self.base, '-m', 'wrong parent')
        git(self.orch.worktree_dir, 'reset', '--hard', commit)
        with self.fakes(), self.assertRaises(corrective.CorrectiveError) as error: self.resume()
        self.assertEqual(error.exception.code, 'txn_record_conflict')
        self.assertEqual(len(self.dispatches), 1)

    def later_root(self, point):
        self.add_dependent()
        self.add_dependent('T3', ['T2'])
        self.plan['tasks'][-1]['ac_refs'] = ['AC-1']
        self.plan['tasks'][-1]['verification']['checks'][0]['ac_refs'] = ['AC-1']
        atomic_json(self.orch.dir_design / 'plan.json', self.plan)
        self.orch.record_artifact('02-design/plan.json', self.orch.dir_design / 'plan.json')
        self.checkpoint['inputs_sha256'] = digest(dict(plan=self.plan, quality=self.quality,
            context=orchestrator.build_task_context(self.orch.ssdlc_root)))
        self.journal.append('TASK_COMPLETED', self.checkpoint)
        from test_gate_reports import finding
        from test_full_checkpoint_review import framed
        self.run_review(lambda *a, **k: (framed('review', self.orch, [finding(),
            finding(id='F2', task_attribution=['T3'], file_refs=['t3.py'])]), 0, 0, 0, 'fake'))
        preview = self.orch.preview_bounce('04-review', 1)
        self.assertEqual(preview['plan']['repair_roots'], ['T1', 'T3'])
        original = self.develop
        def develop(prompt, *args, **kwargs):
            self.edit_path = 't3.py' if '"task_id": "T3"' in prompt.split('PROJECT QUALITY CONTRACT:')[0] else 'app.py'
            return original(prompt, *args, **kwargs)
        def fault(name):
            if name == point and len(self.dispatches) == 2: raise Crash()
        with self.fakes(), patch('orchestrator.EngineRunner.run_codex', side_effect=develop), \
             patch('orchestrator._corrective_fault', side_effect=fault), self.assertRaises(Crash):
            self.execute(preview)
        with self.fakes(): self.assertEqual(self.resume()['status'], 'COMPLETED')
        self.assertEqual(len(self.dispatches), 2)
        self.assertEqual(self.journal.peek()['tasks']['T2']['attempts'], 1)
        self.assertEqual(self.invalidations(), 1)

    def test_later_root_intent_before_commit(self):
        self.later_root('repair.after_commit_intent_before_git_commit')

    def test_later_root_intent_after_commit(self):
        self.later_root('repair.after_git_commit_before_complete')

    def preflight_failure(self, point, mutated=False):
        preview = self.preview()
        final = []
        original = self.verify_cmd
        def fault(name):
            if name == 'final.after_preflight_started': final.append(True)
            if name == point: raise Crash()
        def verify(*args, **kwargs):
            if final:
                if mutated:
                    (self.orch.worktree_dir / 'app.py').write_text('mutated by verification\n')
                    return 'Ran 1 test\nOK', '', 0
                return 'Ran 1 test\nFAILED', '', 1
            return original(*args, **kwargs)
        with self.fakes(), patch('orchestrator.EngineRunner.run_sandboxed_cmd', side_effect=verify), \
             patch('orchestrator._corrective_fault', side_effect=fault), self.assertRaises(Crash):
            self.execute(preview)
        snapshot = self.journal.peek()
        self.assertEqual(snapshot['preflights'][-1]['status'], 'MUTATED' if mutated else 'FAILED')
        with self.fakes(), patch('ssdlc_tasks.TaskScheduler._verify', side_effect=AssertionError('rerun')), \
             self.assertRaises(corrective.CorrectiveError) as error:
            self.resume()
        self.assertEqual(error.exception.code, 'final_checkpoint_failed')
        self.assertEqual(self.txn().peek()['status'], 'FAILED')
        self.assertEqual(self.invalidations(), 1)
        self.assertEqual(len(self.dispatches), 1)
        if mutated:
            self.assertEqual((self.orch.worktree_dir / 'app.py').read_text(), 'mutated by verification\n')

    def test_failed_preflight_fault_is_terminal(self):
        self.preflight_failure('final.after_preflight_blocked_before_record')

    def test_mutated_preflight_fault_is_terminal(self):
        self.preflight_failure('final.after_preflight_mutated_before_record', mutated=True)

    def interruption(self, point, budget=False):
        self.add_dependent()
        armed = []
        original = self.orch.check_budget_circuit_breaker
        def fault(name):
            if name == point: armed.append(True)
        def guard():
            if armed:
                if budget:
                    if not any(x.get('phase') == 'fixture' for x in self.orch.state['ledger']):
                        from test_gate_reports import add_ledger_cost
                        add_ledger_cost(self.orch, 1000)
                else:
                    raise corrective.CorrectiveError('cancelled')
            return original()
        # A positive cap freezes the same limit across interruption and resume.
        with patch.dict(os.environ, {'SSDLC_BUDGET_CAP_USD': '8'}), self.fakes(), \
             patch.object(self.orch, 'run_phase_dev'), \
             patch.object(self.orch, 'check_budget_circuit_breaker', side_effect=guard), \
             patch('orchestrator._corrective_fault', side_effect=fault), self.assertRaises(TaskError):
            self.orch.run_pipeline('fix', from_phase='03-dev')
        self.assertTrue(armed)
        dispatched = len(self.dispatches)
        self.assertEqual(self.txn().peek()['status'], 'INTERRUPTED')
        self.orch.state['ledger'] = []
        self.orch._save_state()
        with self.fakes(), patch.dict(os.environ, {'SSDLC_BUDGET_CAP_USD': '100', 'SSDLC_MAX_DEV_RETRIES': '100'}):
            self.assertEqual(self.orch._corrective_limits()['budget_cap_usd'], 8)
            if point == 'repair.after_task_started':
                with self.assertRaises(corrective.CorrectiveError) as error: self.resume()
                self.assertEqual(error.exception.code, 'uncertain_dispatch')
                self.assertEqual(len(self.dispatches), dispatched)
            else:
                self.assertEqual(self.resume()['status'], 'COMPLETED')
                self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.invalidations(), 1)


for _point in ('invalidate.after_intent_before_call', 'repair.after_task_started',
               'revalidate.after_task_started', 'gates.after_completed_before_rerun'):
    for _budget in (False, True):
        def case(self, point=_point, budget=_budget):
            self.interruption(point, budget)
        setattr(RecoveryEdgeTests, 'test_' + ('budget_' if _budget else 'cancel_') + _point.replace('.', '_'), case)


def _mutated_evidence_case(suffix):
    def case(self):
        preview = self.preview()
        def fault(point):
            if point == 'invalidate.after_call_before_ack': raise Crash()
        with self.fakes(), patch('orchestrator._corrective_fault', side_effect=fault), self.assertRaises(Crash):
            self.execute(preview)
        record = json.loads((self.txn().directory / 'transaction.json').read_text())
        refs = record['evidence_manifest']['reader']
        ref = next(ref for ref in refs if ('/evidence/' in ref if suffix == 'captured' else ref.endswith(suffix)))
        path = self.orch.ssdlc_root / ref
        path.write_bytes(path.read_bytes() + b' changed')
        with self.fakes(), self.assertRaises(corrective.CorrectiveError) as error: self.resume()
        self.assertEqual(error.exception.code, 'evidence_mutated')
        self.assertEqual(self.dispatches, [])
        self.assertEqual(self.invalidations(), 1)
    return case


for _suffix in ('report.json', 'response-1.md', 'input.json', 'captured'):
    setattr(RecoveryEdgeTests, 'test_evidence_after_invalidation_' + _suffix.replace('.', '_'), _mutated_evidence_case(_suffix))


def _native_fault_case(point, options):
    def case(self):
        self.enable_native()
        self.recovered(point, **options)
    return case


for _point in ('repair.after_reserved_before_dispatch', 'repair.after_task_started',
               'repair.after_verifying', 'repair.after_check_1',
               'repair.after_commit_intent_before_git_commit', 'repair.after_git_commit_before_complete',
               'repair.after_commit_before_ack', 'repair.after_attempt_failed_before_rollback',
               'repair.after_second_attempt_started'):
    setattr(RecoveryTests, 'test_native_fault_' + _point.replace('.', '_'),
            _native_fault_case(_point, _ROWS[_point]))


def _active_i2_defaults_case(self):
    from ssdlc_recovery import LimitsStore
    from test_recovery_limits import policy
    store = LimitsStore(self.orch.ssdlc_root)
    store.authorize(policy(development_attempts=5, metered_usd=15, cli_estimate_usd=15),
                    {'ticket': self.orch.ticket_id})
    reserved = store.reserve('03-dev', '03-dev', 'codex')
    store.finish(reserved, 'returned', {'metered_usd': None, 'cli_estimate_usd': 7})
    frozen = copy.deepcopy(store._load(repair=False)[1])
    with patch.object(orchestrator._dispatch_local, 'limits', store, create=True), patch.dict(os.environ):
        os.environ.pop('SSDLC_MAX_DEV_RETRIES', None)
        os.environ.pop('SSDLC_BUDGET_CAP_USD', None)
        self.assertEqual(self.orch._corrective_limits(),
            dict(max_attempts=5, gate_round_ceiling=5, budget_cap_usd=30))
        self.orch.check_budget_circuit_breaker()
        txn = dict(limits=dict(max_attempts=4, gate_round_ceiling=4, budget_cap_usd=25))
        self.assertEqual(self.orch._corrective_limits([txn]),
            dict(max_attempts=4, gate_round_ceiling=4, budget_cap_usd=25))
        with patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '2', 'SSDLC_BUDGET_CAP_USD': '8'}):
            self.assertEqual(self.orch._corrective_limits([txn]),
                dict(max_attempts=2, gate_round_ceiling=2, budget_cap_usd=8))
        with patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '100', 'SSDLC_BUDGET_CAP_USD': '0'}):
            self.assertEqual(self.orch._corrective_limits([txn]),
                dict(max_attempts=4, gate_round_ceiling=4, budget_cap_usd=25))
    self.assertEqual(store._load(repair=False)[1], frozen)
    self.assertIsNone(frozen['invocations'][reserved]['usage']['metered_usd'])


RecoveryEdgeTests.test_active_i2_defaults_use_authorized_profile = _active_i2_defaults_case


def _active_i2_case(self):
    from ssdlc_recovery import LimitsStore
    from test_recovery_limits import policy
    self.enable_native()
    self.checkpoint['bounded_recovery'] = 1
    self.journal.append('TASK_COMPLETED', self.checkpoint)
    preview = self.preview()
    store = LimitsStore(self.orch.ssdlc_root)
    store.authorize(policy(development_attempts=2), {'ticket': self.orch.ticket_id})
    first = store.reserve('03-dev', '03-dev', 'codex')
    store.finish(first, 'returned', {'metered_usd': 1, 'cli_estimate_usd': 1})
    frozen = copy.deepcopy(store._load(repair=False)[1])
    def run_codex(prompt, cwd, **kwargs):
        return self.develop(prompt, cwd, **kwargs)
    with self.fakes(), patch.object(orchestrator._dispatch_local, 'limits', store, create=True), \
         patch('orchestrator.EngineRunner.run_codex', side_effect=orchestrator._supervised_dispatch(run_codex)):
        self.assertEqual(self.orch._corrective_limits()['max_attempts'], 2)
        self.assertEqual(self.execute(preview)['status'], 'COMPLETED')
        self.assertEqual(self.journal.peek()['tasks']['T1']['attempts'], 2)
    after = store._load(repair=False)[1]
    self.assertEqual(len(after['invocations']), 2)
    self.assertEqual(after['invocations'][first], frozen['invocations'][first])
    for key in ('started_at', 'deadline_at', 'transports', 'diagnoses'):
        self.assertEqual(after[key], frozen[key])
    with patch.object(orchestrator._dispatch_local, 'limits', store, create=True), \
         patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '100'}):
        self.assertEqual(self.orch._corrective_limits()['max_attempts'], 2)
    self.assertEqual(self.invalidations(), 1)


RecoveryEdgeTests.test_active_i2_limits_remain_cumulative = _active_i2_case


def _native_reserved_case(failed=False):
    def case(self):
        self.enable_native()
        self.check_failure = failed
        ceiling = 3 if failed else 2
        preview = self.preview()
        def fault(point):
            if (point == 'task.after_dispatch_reserved'
                    and self.journal.peek()['tasks']['T1']['attempts'] == ceiling):
                raise Crash()
        with patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': str(ceiling)}), self.fakes(), \
             patch('orchestrator._corrective_fault', side_effect=fault), self.assertRaises(Crash):
            self.execute(preview)
        self.assertEqual(self.journal.peek()['tasks']['T1']['status'], 'DISPATCH_RESERVED')
        self.assertEqual(len(self.dispatches), int(failed))
        self.check_failure = False
        with self.fakes():
            self.assertEqual(self.resume()['status'], 'COMPLETED')
        self.assertEqual(len(self.dispatches), int(failed) + 1)
        self.assertEqual(self.journal.peek()['tasks']['T1']['attempts'], ceiling)
        self.assertEqual(self.invalidations(), 1)
    return case


RecoveryEdgeTests.test_native_reserved_at_attempt_limit = _native_reserved_case()
RecoveryEdgeTests.test_native_reserved_retry_at_attempt_limit = _native_reserved_case(True)


def _i2_quota_stop_case(self):
    from ssdlc_recovery import LimitsStore, RecoveryStop
    from test_recovery_limits import policy
    self.enable_native()
    self.checkpoint['bounded_recovery'] = 1
    self.journal.append('TASK_COMPLETED', self.checkpoint)
    preview = self.preview()
    store = LimitsStore(self.orch.ssdlc_root)
    store.authorize(policy(development_attempts=2, cli_estimate_usd=1), {'ticket': self.orch.ticket_id})
    def run_codex(prompt, cwd, **kwargs):
        result = self.develop(prompt, cwd, **kwargs)
        # Model usage is charged by the real dispatch wrapper. A Claude-shaped
        # response supplies an observed CLI estimate without changing the policy.
        return result[0], result[1], 1, 0, 0
    run_codex.__name__ = 'run_claude'
    frozen = copy.deepcopy(store._load(repair=False)[1])
    with self.fakes(), patch.object(orchestrator._dispatch_local, 'limits', store, create=True), \
         patch('orchestrator.EngineRunner.run_claude', side_effect=orchestrator._supervised_dispatch(run_codex)), \
         patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('claude', 'fake', 'low', 'test')):
        with self.assertRaises(RecoveryStop):
            self.execute(preview)
        self.assertEqual(self.txn().peek()['status'], 'INTERRUPTED')
        consumed = copy.deepcopy(store._load(repair=False)[1])
        self.assertEqual(len(consumed['invocations']), 1)
        self.assertEqual(next(iter(consumed['invocations'].values()))['usage']['cli_estimate_usd'], 1)
        with patch.dict(os.environ, {'SSDLC_BUDGET_CAP_USD': '100', 'SSDLC_MAX_DEV_RETRIES': '100'}):
            with self.assertRaises(RecoveryStop):
                self.resume()
        self.assertEqual(store._load(repair=False)[1], consumed)
        self.assertEqual(consumed['deadline_at'], frozen['deadline_at'])
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.invalidations(), 1)


RecoveryEdgeTests.test_i2_quota_stop_interrupts_without_refunding_reservation = _i2_quota_stop_case
