"""Offline publication crash recovery and frozen dispatch limits."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import orchestrator
import ssdlc_corrective as corrective
from ssdlc_tasks import TaskError


class Crash(BaseException):
    pass


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.orch = object.__new__(orchestrator.SSDLCOrchestrator)
        self.orch.ssdlc_root = self.root
        self.orch.worktree_dir = self.root
        self.orch._registry_lock = threading.RLock()
        self.orch.state = dict(artifacts={}, ledger=[], base_commit='c' * 40,
                               task_verified_head='a' * 40, risk_tier='M')
        self.orch._save_state = lambda: None
        self.orch.record_gate = lambda *a: None
        self.path = self.root / '03-dev/implementation.diff'
        self.path.parent.mkdir()
        self.path.write_bytes(b'old\n')
        self.orch.record_artifact('03-dev/implementation.diff', self.path)
        directory = self.root / '03-dev/tasks'
        directory.mkdir()
        for name in ('tasks-state.json', 'task-journal.jsonl'):
            (directory / name).write_text('{}\n')
        self.journal = corrective.TransactionJournal(self.root / 'bounce-txn/test')
        self.journal.append('FINAL_PREFLIGHT_PASSED', dict(status='REVALIDATED'))
        self.record = dict(reviewed_commit='a' * 40, next_gate_attempt=2)
        self.checkpoint = dict(expected_head='b' * 40, tasks={'T1': {'status': 'COMPLETED'}})
        self.git = patch('orchestrator.git', side_effect=lambda *a: 'new' if '--binary' in a else 'auth.py')
        self.git.start()
        self.addCleanup(self.git.stop)
        self.env = patch.dict('os.environ', {'SSDLC_BUDGET_CAP_USD': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def publish(self):
        return self.orch._publish_corrective(self.record, self.journal, self.checkpoint, lambda: None)

    def recover(self, point):
        def fault(name):
            if name == point:
                raise Crash()
        with patch('orchestrator._corrective_fault', side_effect=fault):
            with self.assertRaises(Crash):
                self.publish()
        result = self.publish()
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(result['next_gate_attempt'], 2)
        self.assertEqual(self.path.read_bytes(), b'new\n')
        self.assertEqual(self.orch.state['task_verified_head'], 'b' * 40)
        self.assertEqual(self.orch.state['artifacts']['03-dev/implementation.diff']['sha256'],
                         hashlib.sha256(b'new\n').hexdigest())
        entries = [json.loads(x) for x in self.journal.path.read_text().splitlines()]
        for event in ('PUBLISH_INTENT', 'CHECKPOINT_PUBLISHED', 'TXN_COMPLETED'):
            self.assertEqual(sum(e['event'] == event for e in entries), 1)
        before = self.journal.path.read_bytes()
        self.publish()
        self.assertEqual(before, self.journal.path.read_bytes())

    def test_after_archive(self): self.recover('publish.after_archive')
    def test_after_intent(self): self.recover('publish.after_intent')
    def test_after_diff_replace(self): self.recover('publish.after_diff_replace')
    def test_after_registry(self): self.recover('publish.after_registry')
    def test_after_state(self): self.recover('publish.after_state')
    def test_after_checkpoint_event(self): self.recover('publish.after_checkpoint_event')

    def test_hand_modified_diff(self):
        self.path.write_bytes(b'hand modified')
        with self.assertRaises(corrective.CorrectiveError) as error:
            self.publish()
        self.assertEqual(error.exception.code, 'txn_record_conflict')

    def test_archive_symlink_escape(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / '03-dev/archive').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(corrective.CorrectiveError):
            self.publish()
        self.assertEqual(list(outside.iterdir()), [])

    def test_archive_traversal_in_intent(self):
        sha = hashlib.sha256(b'old\n').hexdigest()
        intent = dict(old=dict(head='a'*40, sha256=sha, archive_ref='../escape'),
                      new=dict(head='b'*40, sha256=hashlib.sha256(b'new\n').hexdigest(), archive_ref='../escape'))
        self.journal.append('PUBLISH_INTENT', dict(status='PUBLISHING', publish_intent=intent))
        with self.assertRaises(corrective.CorrectiveError):
            self.publish()

    def test_no_transaction_historical_budget(self):
        with patch.dict('os.environ', {'SSDLC_BUDGET_CAP_USD': '8'}):
            self.orch.state['ledger'] = [{'cost_usd': 8}]
            with self.assertRaises(TaskError): self.orch.check_budget_circuit_breaker()
        with patch.dict('os.environ', {'SSDLC_BUDGET_CAP_USD': '0'}):
            self.orch.check_budget_circuit_breaker()

    def test_every_nested_dispatch_uses_frozen_guard(self):
        frozen = [{'limits': dict(max_attempts=3, gate_round_ceiling=3, budget_cap_usd=8)}]
        with patch.object(self.orch, '_open_or_any_txns', return_value=frozen), \
             patch.dict('os.environ', {'SSDLC_BUDGET_CAP_USD': '100'}):
            for dispatch in ('second_reviewer', 'protocol_correction', 'diagnosis'):
                with self.subTest(dispatch=dispatch):
                    self.orch.state['ledger'] = [{'cost_usd': 7}]
                    self.orch.check_budget_circuit_breaker()
                    self.orch.state['ledger'].append({'cost_usd': 2})
                    invoked = []
                    with self.assertRaises(TaskError):
                        self.orch.check_budget_circuit_breaker()
                        invoked.append(dispatch)
                    self.assertEqual(invoked, [])
            with patch.object(self.orch, '_cancel_requested', return_value=True):
                with self.assertRaises(corrective.CorrectiveError) as error:
                    self.orch.check_budget_circuit_breaker()
                self.assertEqual(error.exception.code, 'cancelled')

    def test_interruption_is_durable_and_idempotent(self):
        self.journal.append('PUBLISH_INTENT', dict(status='PUBLISHING'))
        with patch.object(self.orch, '_open_or_any_txns', return_value=[{'txn_id': 'test'}]):
            self.orch._mark_open_txn_interrupted('cancelled')
            state = self.journal.load()
            self.assertEqual(state['status'], 'INTERRUPTED')
            self.assertEqual(state['resume_status'], 'PUBLISHING')
            self.assertEqual(state['code'], 'cancelled')
            before = self.journal.path.read_bytes()
            self.orch._mark_open_txn_interrupted('cancelled')
            self.assertEqual(before, self.journal.path.read_bytes())

    def test_interruption_recording_does_not_mask_integrity_error(self):
        with patch.object(self.orch, '_open_or_any_txns', side_effect=corrective.CorrectiveError('txn_record_conflict')):
            self.orch._mark_open_txn_interrupted('cancelled')


class LimitsTests(unittest.TestCase):
    def test_gate_setup_preserves_no_transaction_path(self):
        with tempfile.TemporaryDirectory() as directory:
            for artifacts in ({}, {'02-design/plan.json': {}}):
                with self.subTest(artifacts=artifacts):
                    pipeline = SimpleNamespace(
                        ssdlc_root=Path(directory), state={'artifacts': artifacts},
                        PHASE_ORDER=orchestrator.SSDLCOrchestrator.PHASE_ORDER,
                        _ensure_task_plan=lambda: None,
                        run_phase_dev=Mock(side_effect=RuntimeError('reached_dev')))
                    with patch.dict('os.environ', {'SSDLC_MAX_DEV_RETRIES': '3',
                                                  'SSDLC_BUDGET_CAP_USD': 'invalid'}), \
                         patch.object(orchestrator._dispatch_local, 'limits', None, create=True):
                        with self.assertRaisesRegex(RuntimeError, 'reached_dev'):
                            orchestrator.SSDLCOrchestrator._run_pipeline(
                                pipeline, 'goal', from_phase='03-dev')
                    pipeline.run_phase_dev.assert_called_once_with()

    def test_gate_setup_with_transaction_uses_frozen_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = SimpleNamespace(
                ssdlc_root=Path(directory),
                state={'artifacts': {'bounce-txn/test/transaction.json': {}}},
                PHASE_ORDER=orchestrator.SSDLCOrchestrator.PHASE_ORDER,
                _ensure_task_plan=lambda: None,
                _corrective_limits=Mock(return_value={'gate_round_ceiling': 1}),
                run_phase_dev=Mock(side_effect=RuntimeError('reached_dev')))
            with patch.dict('os.environ', {'SSDLC_MAX_DEV_RETRIES': '0'}), \
                 patch.object(orchestrator._dispatch_local, 'limits', None, create=True):
                with self.assertRaisesRegex(RuntimeError, 'reached_dev'):
                    orchestrator.SSDLCOrchestrator._run_pipeline(
                        pipeline, 'goal', from_phase='03-dev')
            pipeline._corrective_limits.assert_called_once_with()

    def test_raised_disabled_and_lowered_limits(self):
        txns = [{'limits': dict(max_attempts=3, gate_round_ceiling=2, budget_cap_usd=8)}]
        original = copy.deepcopy(txns)
        for cap in ('100', '0'):
            result = corrective.effective_limits({'SSDLC_MAX_DEV_RETRIES': '100', 'SSDLC_BUDGET_CAP_USD': cap}, txns)
            self.assertEqual(result, dict(max_attempts=3, gate_round_ceiling=2, budget_cap_usd=8))
        self.assertEqual(corrective.effective_limits({'SSDLC_MAX_DEV_RETRIES': '1', 'SSDLC_BUDGET_CAP_USD': '4'}, txns),
                         dict(max_attempts=1, gate_round_ceiling=1, budget_cap_usd=4))
        self.assertEqual(original, txns)

    def test_budget_environment_read_only_in_helper(self):
        for name in ('orchestrator.py', 'ssdlc_corrective.py'):
            tree = ast.parse(Path(name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    literals = [n for n in ast.walk(node) if isinstance(n, ast.Constant) and n.value == 'SSDLC_BUDGET_CAP_USD']
                    if literals:
                        self.assertEqual(node.name, 'effective_limits')

    def test_dispatch_constructions_keep_shared_guard(self):
        source = Path('orchestrator.py').read_text()
        tree = ast.parse(source)
        names = {'TaskScheduler', 'GateEvaluator', 'PlanRepair', 'DesignSession', 'run_diagnosis'}
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, 'attr', '')
            if name not in names:
                continue
            text = ast.get_source_segment(source, call)
            if name == 'DesignSession' and len(call.args) >= 4 and all(
                    isinstance(a, ast.Constant) and a.value is None for a in call.args[2:4]):
                continue  # Read-only import has no provider callable.
            # Scheduler verification/dispatch wrappers preserve the existing
            # resource-limit distinction, both delegate to the shared guard.
            if name == 'TaskScheduler' and 'verification_budget' in text:
                for wrapper in ('verification_budget', 'dispatch_budget'):
                    definitions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == wrapper]
                    self.assertTrue(definitions)
                    self.assertTrue(all('self.check_budget_circuit_breaker()' in ast.get_source_segment(source, n) for n in definitions))
            else:
                self.assertIn('self.check_budget_circuit_breaker', text)
