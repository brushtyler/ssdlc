"""Offline verified Ops/Docs context and structured measurement checks."""
import json
from pathlib import Path
from ssdlc_controller import controller_snapshot
from unittest.mock import patch

import ssdlc_gate_reports as cr
from ssdlc_tasks import TaskError, atomic_json
from test_task_bootstrap import RepositoryTests
import test_full_checkpoint_review as checkpoint


class OpsDocsTests(RepositoryTests):
    seed = checkpoint.FullCheckpointReviewTests.seed

    def enabled(self):
        orch, diff = self.seed()
        quality = dict(self.quality)
        quality['checks'] = [dict(c, candidate_proof=True) for c in quality['checks']]
        atomic_json(orch.dir_recon / 'quality-contract.json', quality)
        orch.record_artifact('00-recon/quality-contract.json', orch.dir_recon / 'quality-contract.json')
        return orch, diff

    def test_verified_cwd_full_diff_disclosure_and_protected_files(self):
        orch, diff = self.enabled()
        (orch.dir_design / 'adr.md').write_text('x' * 60001)
        controller_root = Path(__file__).resolve().parent.parent
        snapshot = controller_snapshot(controller_root)
        protected = {p: p.read_bytes() for p in orch.dir_recon.rglob('*') if p.is_file()}
        def invoke(engine, model, system, user, cwd, *args, **kwargs):
            self.assertEqual(cwd, orch.worktree_dir)
            self.assertNotEqual(cwd, orch.repo_root)
            self.assertIn('FINAL_DIFF_SENTINEL', user)
            self.assertIn('verified candidate context', user)
            self.assertIn('must not claim complete audit coverage', system)
            return 'output', 0, 0, 0, 'test'
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
            orch.run_phase_ops()
            orch.run_phase_docs()
        path = orch.dir_ops / 'context-disclosure.json'
        disclosure = json.loads(path.read_text())
        self.assertEqual(disclosure['inputs'], [dict(name='ADR', included_chars=60000, total_chars=60001, truncated=True)])
        self.assertFalse(disclosure['full_audit_coverage_permitted'])
        self.assertEqual(disclosure['diff']['included_chars'], len(diff))
        self.assertLess(path.stat().st_size, 2000)
        self.assertEqual(cr._digest(path.read_bytes()), orch.state['artifacts']['06-ops/context-disclosure.json']['sha256'])
        self.assertEqual(protected, {p: p.read_bytes() for p in protected})
        self.assertEqual(snapshot, controller_snapshot(controller_root))
        del orch.state['artifacts']['06-ops/context-disclosure.json']
        cr.reconcile_candidate_store(orch.ssdlc_root, orch.state['artifacts'], orch._register_new)
        self.assertIn('06-ops/context-disclosure.json', orch.state['artifacts'])
        path.write_text('{}')
        with self.assertRaises(TaskError):
            cr.reconcile_candidate_store(orch.ssdlc_root, orch.state['artifacts'], orch._register_new)

    def test_oversize_and_dirty_refused_before_dispatch(self):
        orch, diff = self.enabled()
        with patch('orchestrator.EngineRunner.invoke_role') as invoke:
            with patch.dict('os.environ', {'SSDLC_REVIEW_MAX_DIFF_CHARS': '60000'}):
                with self.assertRaisesRegex(TaskError, 'no truncation performed'):
                    orch.run_phase_ops()
            self.assertFalse((orch.dir_ops / 'context-disclosure.json').exists())
            (orch.worktree_dir / 'app.py').write_text('dirty')
            with self.assertRaisesRegex(TaskError, 'clean verified'):
                orch.run_phase_docs()
            invoke.assert_not_called()

    def test_mutation_during_invocation(self):
        orch, _ = self.enabled()
        def mutate(*args, **kwargs):
            (orch.worktree_dir / 'app.py').write_text('changed')
            return 'output', 0, 0, 0, 'test'
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=mutate):
            with self.assertRaisesRegex(TaskError, 'clean verified'):
                orch.run_phase_ops()
        self.assertFalse((orch.dir_ops / 'release-and-rollback.md').exists())

    def test_legacy_ops_docs(self):
        orch, diff = self.seed()
        def invoke(engine, model, system, user, cwd, *args, **kwargs):
            self.assertEqual(cwd, orch.repo_root)
            self.assertIn(diff[:50000], user)
            self.assertNotIn('FINAL_DIFF_SENTINEL', user)
            self.assertNotIn('context disclosure', user)
            return 'legacy', 0, 0, 0, 'test'
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
            orch.run_phase_ops()
            orch.run_phase_docs()
        self.assertFalse((orch.dir_ops / 'context-disclosure.json').exists())
        self.assertFalse((orch.dir_docs / 'context-disclosure.json').exists())

    def test_metrics_mixed_read_only(self):
        root = self.repo / 'metrics'
        artifacts = {
            'candidate-evidence/decisions/a.json': dict(decision='executed', considered=[json.dumps(dict(code='stale_source_commit'))], declined='environment_identity_unavailable'),
            'candidate-evidence/decisions/c.json': dict(decision='invalidated', considered=[]),
            'candidate-evidence/decisions/b.json': dict(decision='reused', considered=[]),
            'candidate-evidence/probes/a.json': dict(kind='privilege_probe'),
            'candidate-evidence/probes/b.json': dict(kind='privilege_probe'),
            'diagnosis/requests/a/request.json': {}, 'diagnosis/requests/b/request.json': {},
            'diagnosis/reservations/slot-1.json': {}, 'diagnosis/requests/a/result.json': {},
            'diagnosis/requests/a/consumption.json': {}, 'diagnosis/requests/b/uncertain.json': {},
            'history/review-attempt-2.json': {},
            '06-ops/context-disclosure.json': dict(full_audit_coverage_permitted=False),
            '07-docs/context-disclosure.json': dict(full_audit_coverage_permitted=True),
        }
        for ref, obj in artifacts.items():
            path = root / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(cr.canon(obj))
        before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(cr.convergence_metrics(root), dict(checks_run=2, checks_reused=1, reuse_declined=1, probes=2,
                         rejections_by_code={'stale_source_commit': 1}, diagnosis_requests=2, reservations=1,
                         results=1, consumed=1, uncertain=1, history_snapshots=1, truncated_contexts=1))
        self.assertEqual(before, {p: p.read_bytes() for p in before})
