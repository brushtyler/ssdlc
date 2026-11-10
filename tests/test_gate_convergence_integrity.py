"""Observed budget and environment regressions; every provider is a local fake."""
from dataclasses import replace
import os
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_candidate_evidence as candidates
import test_gate_diagnosis as diagnoses
import ssdlc_gate_reports as cr
from orchestrator import EngineRunner, SSDLCOrchestrator
from ssdlc_findings import TrustedContext
from ssdlc_tasks import TaskError


class DiagnosisAccountingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = diagnoses.DiagnosisTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ledger = []
        self.engine = patch.object(EngineRunner, 'invoke_role', return_value=('diagnosis', .25, 13, 17, 'fake'))
        self.invoke = self.engine.start()
        self.addCleanup(self.engine.stop)
        self.git = patch('orchestrator.git', return_value='b' * 40)
        self.git.start()
        self.addCleanup(self.git.stop)
        self.request = cr.EvaluationRequest('review', 'first-engine', 'first-model', 1, 1, 'a'*40,
            'system', 'user', TrustedContext('a'*40, ['T1'], [], []), [], 'fingerprint', None, 'medium')
        finding = SimpleNamespace(id='F1', to_dict=lambda: dict(id='F1', task_attribution=['T1']))
        self.result = cr.EvaluationResult('rejected_substantive', False,
            assessment=SimpleNamespace(findings=[finding], diagnosis_required=['F1']))
        self.candidate = cr.CandidateEvidence(quality=dict(checks=[dict(required=True, candidate_proof=True)]))
        self.orch = self.new_orchestrator()

    def new_orchestrator(self):
        orch = SSDLCOrchestrator.__new__(SSDLCOrchestrator)
        orch.ssdlc_root = self.fixture.root
        orch.state = dict(artifacts=self.fixture.artifacts, ledger=self.ledger, risk_tier='M')
        orch._register_new = self.fixture.register
        orch.project_id, orch.ticket_id = 'offline', 'offline'
        orch._save_state = Mock()
        orch.record_gate = Mock()
        return orch

    def diagnose(self, orch=None, request=None):
        (orch or self.orch)._diagnose_gate([request or self.request], [self.result],
            self.candidate, self.fixture.root, '04-review')

    def charged(self):
        return [e for e in self.ledger if e['details']['transition'] == 'result']

    def test_nonzero_reported_usage_charged_once_with_original_producer(self):
        with patch.dict(os.environ, SSDLC_BUDGET_CAP_USD='1'):
            self.diagnose()
            self.diagnose(request=replace(self.request, engine='other-engine', model='other-model', attempt=2))
        self.assertEqual(self.invoke.call_count, 1)
        self.assertEqual(len(self.charged()), 1)
        entry = self.charged()[0]
        self.assertEqual((entry['cost_usd'], entry['tokens_in'], entry['tokens_out']), (.25, 13, 17))
        self.assertEqual((entry['engine'], entry['model']), ('first-engine', 'first-model'))
        self.assertGreaterEqual(entry['duration_sec'], 0)
        self.assertTrue(entry['details']['result_sha256'])

    def test_crash_before_ledger_backfills_persisted_usage_without_another_inference(self):
        def crash(point):
            if point == cr.DIAGNOSIS_POINTS[6]:
                raise diagnoses.Crash()
        self.orch._diagnosis_fault = crash
        with patch.dict(os.environ, SSDLC_BUDGET_CAP_USD='1'):
            with self.assertRaises(diagnoses.Crash):
                self.diagnose()
            self.assertEqual(self.ledger, [])
            # A fresh controller object reconciles the durable result.
            self.diagnose(self.new_orchestrator())
            self.diagnose(self.new_orchestrator())
        self.assertEqual(self.invoke.call_count, 1)
        self.assertEqual(len(self.charged()), 1)
        self.assertEqual(sum(e['cost_usd'] for e in self.ledger), .25)

    def test_existing_ledger_precision_does_not_double_charge_on_replay(self):
        self.invoke.return_value = ('diagnosis', .123456789, 13, 17, 'fake')
        with patch.dict(os.environ, SSDLC_BUDGET_CAP_USD='1'):
            self.diagnose()
            self.diagnose()
        self.assertEqual(self.invoke.call_count, 1)
        self.assertEqual(len(self.charged()), 1)
        self.assertEqual(self.charged()[0]['cost_usd'], .123457)

    def test_reported_cost_enforces_cap_after_result_without_marking_call_uncertain(self):
        with patch.dict(os.environ, SSDLC_BUDGET_CAP_USD='.2'):
            with self.assertRaisesRegex(TaskError, 'budget cap'):
                self.diagnose()
        self.assertEqual(self.invoke.call_count, 1)
        self.assertEqual(len(self.charged()), 1)
        self.assertEqual(self.orch.state['status'], 'HALTED_BUDGET_EXCEEDED')
        self.assertEqual(len(list(self.fixture.root.glob('diagnosis/requests/*/result.json'))), 1)
        self.assertEqual(list(self.fixture.root.glob('diagnosis/requests/*/uncertain.json')), [])

    def test_known_pre_dispatch_limit_is_not_an_uncertain_or_consumed_inference(self):
        self.orch.check_budget_circuit_breaker = Mock(side_effect=TaskError('known budget refusal'))
        with self.assertRaisesRegex(TaskError, 'known budget refusal'):
            self.diagnose()
        self.invoke.assert_not_called()
        for name in ('consumption.json', 'uncertain.json', 'result.json'):
            self.assertEqual(list(self.fixture.root.glob('diagnosis/requests/*/' + name)), [])
        self.orch.check_budget_circuit_breaker = Mock()
        self.diagnose()
        self.assertEqual(self.invoke.call_count, 1)
        self.assertEqual(len(list(self.fixture.root.glob('diagnosis/reservations/slot-*.json'))), 1)

    def test_existing_ledger_cannot_hide_mismatched_reported_usage(self):
        with patch.dict(os.environ, SSDLC_BUDGET_CAP_USD='1'):
            self.diagnose()
            self.charged()[0]['cost_usd'] = 0
            with self.assertRaisesRegex(TaskError, 'ledger differs'):
                self.diagnose()
        self.assertEqual(self.invoke.call_count, 1)


class MutableLocalDependencyTests(unittest.TestCase):
    def test_changed_dependency_at_same_path_is_executed_again(self):
        fixture = candidates.CandidateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.runner.stop()
        fixture.orch._verification_environment = MethodType(SSDLCOrchestrator._verification_environment, fixture.orch)
        deps = fixture.root / 'external-dependencies'
        deps.mkdir()
        dependency = deps / 'mutable_dep.py'
        dependency.write_text('enabled=True\n')
        argv = ['python3', '-B', '-c', f'import sys; sys.path.insert(0,{str(deps)!r}); '
                'import mutable_dep; sys.exit(0 if mutable_dep.enabled else 7)']
        fixture.quality['checks'][0]['argv'] = argv
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False):
            initial = fixture.collect()
            identity = fixture.orch._verification_environment(argv)
            dependency.write_text('enabled=False\n')
            self.assertEqual(identity, fixture.orch._verification_environment(argv))
            current = fixture.collect('qa')
        self.assertFalse(identity['reuse_eligible'])
        self.assertEqual(identity['reason'], 'local_dependencies_unpinned')
        self.assertEqual(initial.records[0]['result']['exit_code'], 0)
        self.assertEqual(current.records[0]['result']['exit_code'], 7)
        self.assertNotEqual(current.decisions[0]['decision'], 'reused')
        self.assertEqual(current.controls['candidate_checks'], 'failed')
