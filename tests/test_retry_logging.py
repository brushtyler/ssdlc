import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import SSDLCOrchestrator
from ssdlc_tasks import log_retry_reason
from ssdlc_planning import PlanRepair


class RetryLoggingTests(unittest.TestCase):
    def make_design(self, root):
        dispatcher = patch('orchestrator.SquadDispatcher.dispatch_role', side_effect=lambda role, *args, **kwargs:
                           ('mock', 'architect' if role.endswith('arch') else 'reviewer', 'low', 'test'))
        dispatcher.start()
        self.addCleanup(dispatcher.stop)
        orch = SSDLCOrchestrator.__new__(SSDLCOrchestrator)
        orch.repo_root = root
        orch.ssdlc_root = root
        orch.project_id = 'test'
        orch.ticket_id = 'TKT-LOG'
        orch.state = {'risk_tier': 'M', 'dev_profile': 'ALGORITHMIC', 'base_commit': 'a' * 40}
        for attr, name in [('dir_design', '02-design'), ('dir_recon', '00-recon'), ('dir_po', '01-po')]:
            path = root/name
            path.mkdir(exist_ok=True)
            setattr(orch, attr, path)
        (orch.dir_recon/'context-and-blast-radius.md').write_text('Recon')
        (orch.dir_po/'requirements.md').write_text('AC-1 SEC-1')
        orch._design_sources = MagicMock(return_value=[])
        orch.check_budget_circuit_breaker = MagicMock()
        orch._save_state = MagicMock()
        orch.record_artifact = MagicMock()
        orch.record_ledger = MagicMock()
        orch.record_gate = MagicMock()
        return orch

    def test_design_logs_critique_before_next_round_and_saves_incremental_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = self.make_design(Path(tmp))
            output = io.StringIO()
            finding = {'finding_id': 'D1', 'status': 'OPEN', 'severity': 'BLOCKING', 'category': 'PLAN_DEFECT',
                       'requirement_refs': ['SEC-1'], 'task_ids': [], 'message': 'Missing crash recovery proof',
                       'evidence': 'ADR omits snapshot failure', 'requested_change': 'Specify replay proof', 'new_issue_reason': 'INITIAL_REVIEW'}
            rejected = json.dumps({'verdict': 'REVISION_REQUIRED', 'analysis': 'STRIDE', 'findings': [finding]})
            approved = json.dumps({'verdict': 'APPROVED', 'analysis': 'STRIDE', 'findings': [{**finding, 'status': 'RESOLVED'}]})
            replies = iter(['ADR one', rejected, 'ADR two', approved])
            def invoke(*args, **kwargs):
                if args[-1] == '02-design-arch-r2':
                    self.assertIn('Missing crash recovery proof', output.getvalue())
                    self.assertTrue(list(orch.dir_design.glob('design-session/round-1-review.txt')))
                return next(replies), 0, 0, 0, 'trace'
            with patch.dict('os.environ', {'SSDLC_MAX_DESIGN_ROUNDS': '2'}), patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke), contextlib.redirect_stdout(output):
                orch.run_phase_pair_design()
            self.assertIn('next round 2/2', output.getvalue())
            self.assertIn('Full report:', output.getvalue())
            self.assertEqual(len(list(orch.dir_design.glob('design-session/*-adr.txt'))), 2)

    def test_exhaustion_does_not_claim_another_retry_and_keeps_old_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = self.make_design(Path(tmp))
            finding = {'finding_id': 'D1', 'status': 'OPEN', 'severity': 'BLOCKING', 'category': 'PLAN_DEFECT',
                       'requirement_refs': ['SEC-1'], 'task_ids': [], 'message': 'Unresolved lock boundary',
                       'evidence': 'Lock is released early', 'requested_change': 'Hold lock', 'new_issue_reason': 'INITIAL_REVIEW'}
            report = json.dumps({'verdict': 'REVISION_REQUIRED', 'analysis': 'STRIDE', 'findings': [finding]})
            output = io.StringIO()
            with patch.dict('os.environ', {'SSDLC_MAX_DESIGN_ROUNDS': '1'}), patch('orchestrator.EngineRunner.invoke_role', side_effect=[('ADR', 0, 0, 0, 'trace'), (report, 0, 0, 0, 'trace')]) as invoke, contextlib.redirect_stdout(output):
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'round limit exhausted'):
                        orch.run_phase_pair_design()
                self.assertEqual(invoke.call_count, 2)
            self.assertIn('attempt limit exhausted; no next round', output.getvalue())
            self.assertNotIn('next round 2/1', output.getvalue())
            self.assertIn('Unresolved lock boundary', output.getvalue())
            self.assertEqual(len(list(orch.dir_design.glob('design-session/round-1-review.txt'))), 1)

    def test_semantic_plan_rejection_logs_actual_report_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            verdicts = iter(['Missing behavioral lock test.\nTASK_PLAN_STATUS: REVISION_REQUIRED', 'TASK_PLAN_STATUS: APPROVED'])
            output = io.StringIO()
            repair = PlanRepair(Path(tmp), {}, lambda _: None, lambda *args: '{}', lambda _: next(verdicts), lambda: None)
            with contextlib.redirect_stdout(output):
                repair.run()
            self.assertIn('Missing behavioral lock test.', output.getvalue())
            self.assertIn('attempt-1-review.txt', output.getvalue())
            self.assertIn('semantic review requested changes', output.getvalue())

    def test_long_report_excerpt_keeps_pointer_and_does_not_truncate_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'review.md'
            report = 'Long critique ' * 1000
            path.write_text(report)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                log_retry_reason('phase', report, path, max_chars=100)
            self.assertIn('Excerpt truncated', output.getvalue())
            self.assertLess(len(output.getvalue()), 500)
            self.assertEqual(path.read_text(), report)
