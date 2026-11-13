"""Task pipeline sequencing and durable candidate continuation, entirely offline."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_candidate_evidence as fixtures
import test_full_checkpoint_review as checkpoint
import ssdlc_gate_reports as cr
from ssdlc_tasks import TaskError, atomic_json


class SequenceTests(checkpoint.FullCheckpointReviewTests):
    def test_legacy_resume_without_quality_contract_reaches_development(self):
        def develop(**kwargs):
            raise RuntimeError('reached_dev')

        legacy = SimpleNamespace(
            ssdlc_root=self.repo / '.ssdlc/legacy', state={},
            PHASE_ORDER=checkpoint.SSDLCOrchestrator.PHASE_ORDER,
            _ensure_task_plan=lambda: None, _reconcile_open_txn=lambda: None, run_phase_dev=develop)
        with patch('orchestrator.candidate_reports.reconcile_candidate_store') as reconcile:
            with self.assertRaisesRegex(RuntimeError, 'reached_dev'):
                checkpoint.SSDLCOrchestrator._run_pipeline(legacy, 'goal', from_phase='03-dev')
            reconcile.assert_not_called()
        self.assertFalse((legacy.ssdlc_root / 'candidate-evidence').exists())

    def test_real_pipeline_collects_before_review_and_qa(self):
        orch, diff = self.seed()
        quality = copy.deepcopy(self.quality)
        quality['checks'][0]['candidate_proof'] = True
        atomic_json(orch.dir_recon / 'quality-contract.json', quality)
        orch.record_artifact('00-recon/quality-contract.json', orch.dir_recon / 'quality-contract.json')
        order = []
        def runner(argv, *args):
            if argv == ['id', '-u']:
                return '1000', '', 0
            order.append('check')
            return 'passed', '', 0
        def invoke(*args, **kwargs):
            order.append('review')
            self.assertIn(cr.CANDIDATE_EVIDENCE_NOTE, args[3])
            self.assertFalse((orch.dir_qa / 'deterministic-checks-a1.md').exists())
            return checkpoint.framed('review', orch), 0, 0, 0, 'test'
        def qa(**kwargs):
            order.append('qa')
            candidate = orch._collect_candidate_evidence('qa', 1, orch.worktree_dir, quality)
            self.assertEqual(candidate.decisions[0]['decision'], 'reused')
            return True, 'passed'
        def appsec(**kwargs):
            order.append('appsec')
            candidate = orch._collect_candidate_evidence('appsec', 1, orch.worktree_dir, quality)
            self.assertEqual(candidate.decisions[0]['decision'], 'reused')
            return True, 'passed'
        with patch.object(orch, '_controller_revision', return_value=dict(head='controller', files_sha256='b'*64)), \
             patch.object(orch, '_verification_environment', return_value=dict(
                 mode='role_container', platform='fixture', image_id='sha256:' + '1'*64, reuse_eligible=True)), \
             patch.object(orch, '_ensure_task_plan', return_value=self.plan), patch.object(orch, '_run_task_development'), \
             patch.object(orch, 'run_phase_qa', side_effect=qa), \
             patch.object(orch, 'run_phase_appsec', side_effect=appsec), \
             patch.object(orch, 'run_phase_ops'), patch.object(orch, 'run_phase_docs'), \
             patch.object(orch, 'compile_evidence_pack', side_effect=lambda **kw: kw), \
             patch('orchestrator.EngineRunner.run_sandboxed_cmd', side_effect=runner), \
             patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:reviewer'], 'low')), \
             patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
            result = orch._run_pipeline('goal', from_phase='03-dev')
        self.assertTrue(result['ready_for_signoff'])
        self.assertEqual(order, ['check', 'review', 'qa', 'appsec'])

    def test_pipeline_faults_restart_without_losing_snapshot(self):
        for point in ('before_check', 'after_check_before_log', 'after_log_before_record',
                      'after_record_before_register', 'after_register_before_decision',
                      'after_decision_before_snapshot', 'after_snapshot_before_dispatch'):
            with self.subTest(point=point):
                # A separate ticket store per boundary keeps attempts independent.
                self.setUp()
                orch, diff = self.seed()
                quality = copy.deepcopy(self.quality)
                quality['checks'][0]['candidate_proof'] = True
                atomic_json(orch.dir_recon / 'quality-contract.json', quality)
                orch.record_artifact('00-recon/quality-contract.json', orch.dir_recon / 'quality-contract.json')
                def runner(argv, *args):
                    return ('1000' if argv == ['id', '-u'] else 'passed'), '', 0
                def invoke(*args, **kwargs):
                    return checkpoint.framed('review', orch), 0, 0, 0, 'test'
                def fault(actual):
                    if actual == 'evidence.' + point:
                        raise fixtures.Crash()
                with patch.object(orch, '_controller_revision', return_value=dict(head='controller', files_sha256='b'*64)), \
                     patch.object(orch, '_ensure_task_plan', return_value=self.plan), \
                     patch.object(orch, '_run_task_development'), patch.object(orch, 'run_phase_qa', return_value=(True, 'passed')), \
                     patch.object(orch, 'run_phase_appsec', return_value=(True, 'passed')) as appsec, \
                     patch.object(orch, 'run_phase_ops'), patch.object(orch, 'run_phase_docs'), \
                     patch.object(orch, 'compile_evidence_pack', side_effect=lambda **kw: kw), \
                     patch('orchestrator.EngineRunner.run_sandboxed_cmd', side_effect=runner), \
                     patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:reviewer'], 'low')), \
                     patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
                    orch._candidate_fault = fault
                    with self.assertRaises(fixtures.Crash):
                        orch._run_pipeline('goal', from_phase='03-dev')
                    snapshot = orch.ssdlc_root / 'candidate-evidence/catalogue/review-attempt-1.json'
                    before = snapshot.read_bytes() if snapshot.exists() else None
                    orch._candidate_fault = lambda point: None
                    self.assertTrue(orch._run_pipeline('goal', from_phase='03-dev')['ready_for_signoff'])
                    appsec.assert_called_once_with(attempt=1)
                    if before is not None:
                        self.assertEqual(before, snapshot.read_bytes())

    def test_evaluation_reverifies_candidate_record(self):
        orch, diff = self.seed()
        quality = copy.deepcopy(self.quality)
        quality['checks'][0]['candidate_proof'] = True
        atomic_json(orch.dir_recon / 'quality-contract.json', quality)
        orch.record_artifact('00-recon/quality-contract.json', orch.dir_recon / 'quality-contract.json')
        def mutate(*args, **kwargs):
            record = next((orch.ssdlc_root / 'candidate-evidence/records').glob('*.json'))
            record.write_text('{}')
            return checkpoint.framed('review', orch), 0, 0, 0, 'test'
        with patch.object(orch, '_controller_revision', return_value=dict(head='controller', files_sha256='b'*64)), \
             patch('orchestrator.EngineRunner.run_sandboxed_cmd', side_effect=lambda argv, *args: ('1000' if argv == ['id', '-u'] else 'passed', '', 0)), \
             patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:reviewer'], 'low')), \
             patch('orchestrator.EngineRunner.invoke_role', side_effect=mutate):
            with self.assertRaises(TaskError):
                orch.run_phase_review()


class RecoveryTests(fixtures.CandidateTests):
    def test_all_fault_boundaries_resume(self):
        for index, point in enumerate(('before_check', 'after_check_before_log', 'after_log_before_record',
                                      'after_record_before_register', 'after_register_before_decision',
                                      'after_decision_before_snapshot', 'after_snapshot_before_dispatch'), 1):
            with self.subTest(point=point):
                self.env['env'] = {'PATH': str(index)}
                def fault(actual):
                    if actual == 'evidence.' + point:
                        raise fixtures.Crash()
                self.orch._candidate_fault = fault
                with self.assertRaises(fixtures.Crash):
                    self.collect(attempt=index)
                self.orch._candidate_fault = lambda point: None
                result = self.collect(attempt=index)
                self.assertEqual(result.controls['candidate_checks'], 'passed')

    def test_mutating_check_records_invalidation_only(self):
        self.orch.record_gate = lambda *args: None
        def runner(argv, *args):
            if argv == ['id', '-u']:
                return '1000', '', 0
            (self.wt / 'source').write_text('mutated')
            return 'passed', '', 0
        with patch('orchestrator.EngineRunner.run_sandboxed_cmd', side_effect=runner):
            with self.assertRaisesRegex(TaskError, 'Source changed'):
                self.collect()
        self.assertFalse(list((self.store / 'candidate-evidence/records').glob('*')))
        decision = json.loads(next((self.store / 'candidate-evidence/decisions').glob('*')).read_text())
        self.assertEqual(decision['decision'], 'invalidated')

    def test_nonreusable_snapshot_is_continuation(self):
        self.env['reuse_eligible'] = False
        first = self.collect()
        self.calls.clear()
        second = self.collect()
        self.assertEqual(first.block, second.block)
        self.assertEqual(self.calls, [['id', '-u']])
        self.collect('qa')
        self.assertEqual(len(self.calls), 3)

    def test_unregistered_tampered_snapshot_refused(self):
        self.collect()
        ref = 'candidate-evidence/catalogue/review-attempt-1.json'
        del self.artifacts[ref]
        path = self.store / ref
        obj = json.loads(path.read_text())
        obj['block'] += 'tampered'
        path.write_bytes(cr.canon(obj))
        with self.assertRaises(TaskError):
            cr.reconcile_candidate_store(self.store, self.artifacts, self.orch._register_new)

    def test_binding_changes_execute_again(self):
        dimensions = ('source_commit', 'source_tree', 'controller', 'contract', 'command', 'environment', 'privilege')
        for dimension in dimensions:
            with self.subTest(dimension=dimension):
                self.setUp()
                self.collect()
                self.calls.clear()
                if dimension == 'source_commit':
                    fixtures.git(self.wt, 'commit', '--allow-empty', '-m', 'new commit')
                    self.orch.state['task_verified_head'] = fixtures.git(self.wt, 'rev-parse', 'HEAD')
                elif dimension == 'source_tree':
                    (self.wt / 'source').write_text('new tree')
                    fixtures.git(self.wt, 'add', 'source')
                    fixtures.git(self.wt, 'commit', '-m', 'new tree')
                    self.orch.state['task_verified_head'] = fixtures.git(self.wt, 'rev-parse', 'HEAD')
                elif dimension == 'controller':
                    self.orch._controller_revision = lambda: dict(head='new controller', files_sha256='b'*64)
                elif dimension == 'contract':
                    self.artifacts['00-recon/quality-contract.json']['sha256'] = 'e'*64
                elif dimension == 'command':
                    self.quality['checks'][0]['argv'].append('--changed')
                elif dimension == 'environment':
                    self.env['env'] = {'PATH': 'changed'}
                elif dimension == 'privilege':
                    self.orch._candidate_privilege = lambda wt: 'root'
                result = self.collect('qa')
                self.assertEqual(result.decisions[0]['decision'], 'executed')
                self.assertEqual(sum(argv != ['id', '-u'] for argv in self.calls), 1)

    def test_unregistered_tampered_decision_refused(self):
        self.collect()
        path = next((self.store / 'candidate-evidence/decisions').glob('*.json'))
        del self.artifacts[str(path.relative_to(self.store))]
        obj = json.loads(path.read_text())
        obj['decision'] = 'reused'
        path.write_bytes(cr.canon(obj))
        with self.assertRaises(TaskError):
            cr.reconcile_candidate_store(self.store, self.artifacts, self.orch._register_new)
