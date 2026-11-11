import copy
import json
from pathlib import Path
import tempfile
import unittest

from replay.repository_fixtures import finding, review
from ssdlc_design import DesignSession
from ssdlc_recovery import RecoveryStop, progress
from ssdlc_tasks import PlanError


class DesignRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.arch_calls = []; self.review_calls = []; self.diagnoses = []
        self.inputs = {'context': {'requirement_catalogue': {'required_ids': ['AC-1', 'SEC-2']}, 'quality_contract': {}}}

    def make(self, *, resolve=True, diagnosis=None, **overrides):
        def arch(rnd, previous, history):
            self.arch_calls.append((rnd, history)); return f'# Rewritten prose, round {rnd}'
        def reviewer(rnd, adr, history):
            self.review_calls.append(rnd)
            return review([finding(status='RESOLVED' if resolve and rnd >= 4 else 'OPEN')])
        def diagnose(subject, adr, history):
            self.diagnoses.append(subject); return 'Bounded diagnosis: enumerate archived failed check logs.'
        args = dict(directory=self.root, inputs=self.inputs, architect=arch, reviewer=reviewer,
                    budget=lambda: None, progress_policy=True, diagnose=diagnosis or diagnose)
        args.update(overrides); return DesignSession(**args)

    def state(self): return json.loads((self.root / 'design-session/session.json').read_text())

    def test_rewording_does_not_reset_stagnation_diagnosis_then_next_round_repairs(self):
        self.make().run()
        self.assertEqual(len(self.arch_calls), 4); self.assertEqual(len(self.review_calls), 4)
        self.assertEqual(len(self.diagnoses), 1)
        self.assertIn('diagnosis', self.arch_calls[-1][1][-1])
        self.assertEqual(self.state()['rounds'][-1]['status'], 'APPROVED')
        before = len(self.arch_calls), len(self.review_calls), len(self.diagnoses)
        self.make().run(); self.assertEqual(before, (len(self.arch_calls), len(self.review_calls), len(self.diagnoses)))

    def test_no_progress_after_diagnosis_is_a_persistent_registered_stop(self):
        for _ in range(2):
            with self.assertRaisesRegex(RecoveryStop, 'did not progress'):
                self.make(resolve=False).run()
        self.assertEqual(len(self.arch_calls), 4); self.assertEqual(len(self.review_calls), 4)
        self.assertEqual(len(self.diagnoses), 1)

    def test_lost_diagnosis_response_does_not_dispatch_another_architect_or_diagnosis(self):
        def crash(*a): self.diagnoses.append(1); raise RuntimeError('lost diagnosis')
        with self.assertRaises(RuntimeError): self.make(diagnosis=crash).run()
        for _ in range(2):
            with self.assertRaisesRegex(RecoveryStop, 'without a captured result'):
                self.make().run()
        self.assertEqual(len(self.arch_calls), 3); self.assertEqual(len(self.diagnoses), 1)

    def test_zero_authorized_diagnosis_stops_without_an_implicit_call(self):
        def refuse(*a): raise RecoveryStop('DESIGN_STALLED', 'No explicitly authorized diagnosis allowance remains')
        with self.assertRaisesRegex(RecoveryStop, 'No explicitly authorized'):
            self.make(diagnosis=refuse).run()
        self.assertEqual(len(self.arch_calls), 3); self.assertEqual(self.diagnoses, [])

    def test_diagnosis_does_not_allocate_a_sixth_round_or_call_without_repair_room(self):
        with self.assertRaisesRegex(RecoveryStop, 'No remaining authorized design round'):
            self.make(initial_limit=3).run()
        self.assertEqual(self.diagnoses, []); self.assertEqual(len(self.arch_calls), 3)

    def test_finding_id_and_evidence_rewording_alone_are_not_progress(self):
        previous = {'findings': [finding()]}
        renamed = {'findings': [finding('renamed', message='new words', evidence='new words')]}
        self.assertFalse(progress(previous, renamed)['material_progress'])
        closed = {'findings': [finding(status='RESOLVED')]}
        self.assertTrue(progress(previous, closed)['material_progress'])

    def test_new_progress_policy_cannot_be_attached_to_an_existing_session(self):
        self.make().run()
        with self.assertRaisesRegex(PlanError, 'policy changed'):
            self.make(progress_policy=False).run()

    def test_crash_after_review_before_progress_replays_without_model_calls(self):
        session = self.make()
        original = session._progress
        def crash(*a): raise OSError('interrupted progress evaluation')
        session._progress = crash
        with self.assertRaises(OSError): session.run()
        self.assertEqual(len(self.arch_calls), 1); self.assertEqual(len(self.review_calls), 1)
        self.make().run()
        self.assertEqual(len(self.arch_calls), 4); self.assertEqual(len(self.review_calls), 4)
        self.assertEqual(len(self.diagnoses), 1)

    def test_material_evidence_must_be_host_verified_and_cannot_be_relabelled(self):
        proof = {'artifact_ref': 'host-proof.json', 'sha256': 'a' * 64, 'obligation_refs': ['SEC-2']}
        session = self.make(verify_evidence=lambda item: item == proof)
        session.root.mkdir(exist_ok=True)
        state = {'round_limit': 5, 'rounds': [{'round': 1, 'status': 'REJECTED', 'stagnant_rounds': 0},
                                            {'round': 2, 'status': 'REJECTED'}]}
        history = [{'assessment': {'findings': [finding()]}}]
        assessment = {'findings': [finding()], 'progress_evidence': [proof]}
        session._progress(state, state['rounds'][-1], history, assessment, 'ADR')
        self.assertTrue(state['rounds'][-1]['progress']['material_progress'])
        state['rounds'].append({'round': 3, 'status': 'REJECTED'})
        session._progress(state, state['rounds'][-1], history, assessment, 'rewritten ADR')
        self.assertFalse(state['rounds'][-1]['progress']['material_progress'])

    def test_crash_after_derived_delta_write_reconciles_without_new_architect(self):
        session = self.make()
        save = session._save
        def crash(state):
            if state['rounds'] and 'adr_delta' in state['rounds'][-1]:
                raise OSError('delta written before metadata')
            save(state)
        session._save = crash
        with self.assertRaises(OSError): session.run()
        self.assertEqual(len(self.arch_calls), 1)
        self.make().run()
        self.assertEqual(len(self.arch_calls), 4); self.assertEqual(len(self.review_calls), 4)

    def scenario(self, name):
        from types import SimpleNamespace
        from orchestrator import SSDLCOrchestrator
        from ssdlc_tasks import atomic_json, file_hash
        owner = SimpleNamespace(ssdlc_root=self.root, repo_root=self.root,
                                state={'base_commit': 'fixture', 'artifacts': {}})
        source = self.root / 'source.py'; source.write_text('value = 1\n')
        proof = {}
        def arch(rnd, previous, history):
            self.arch_calls.append(rnd)
            return '' if name == 'design_empty_after_diagnosis' and rnd == 4 else '# ADR ' + str(rnd)
        def reviewer(rnd, adr, history):
            self.review_calls.append(rnd)
            if name == 'design_oscillation':
                return review([finding('F1', requirement_refs=['AC-1'], status='OPEN' if rnd % 2 else 'RESOLVED'),
                               finding('F2', requirement_refs=['SEC-2'], status='RESOLVED' if rnd % 2 else 'OPEN')])
            result = json.loads(review([finding()]))
            if proof: result['progress_evidence'] = [proof.copy()]
            return json.dumps(result)
        def diagnosis(subject, adr, history):
            self.diagnoses.append(subject)
            ref = '02-design/design-session/source-inspection-' + subject + '.json'
            path = self.root / ref
            atomic_json(path, {'kind': 'source_inspection', 'purpose': 'diagnosis_context',
                              'base_commit': 'fixture', 'files': [{'path': 'source.py', 'sha256': file_hash(source)}]})
            owner.state['artifacts'][ref] = {'path': ref, 'sha256': file_hash(path)}
            proof.update(artifact_ref=ref, sha256=file_hash(path), obligation_refs=['SEC-2'])
            return 'Model diagnosis is advice, not proof'
        return self.make(architect=arch, reviewer=reviewer, diagnose=diagnosis,
                         verify_evidence=lambda item: SSDLCOrchestrator._valid_design_progress_evidence(owner, item))

    def test_oscillating_obligations_do_not_count_as_new_resolutions(self):
        for _ in range(2):
            with self.assertRaises(RecoveryStop) as stop: self.scenario('design_oscillation').run()
            self.assertEqual(stop.exception.category, 'DESIGN_STALLED')
        self.assertEqual(len(self.arch_calls), 5)
        self.assertEqual(self.state()['rounds'][3]['progress']['resolved_obligations'], [])
        self.assertEqual(self.state()['rounds'][4]['progress']['resolved_obligations'], [])

    def test_diagnosis_report_cannot_be_cited_as_material_progress(self):
        with self.assertRaises(RecoveryStop) as stop: self.scenario('design_diagnosis_citation').run()
        self.assertEqual(stop.exception.category, 'DESIGN_STALLED')
        self.assertEqual(len(self.arch_calls), 4)
        self.assertEqual(self.state()['rounds'][-1]['progress']['material_evidence'], [])
        self.assertEqual(len(self.diagnoses), 1)

    def test_empty_adr_after_diagnosis_stops_without_another_round(self):
        for _ in range(2):
            with self.assertRaises(RecoveryStop) as stop: self.scenario('design_empty_after_diagnosis').run()
            self.assertEqual(stop.exception.category, 'DESIGN_STALLED')
        self.assertEqual(len(self.arch_calls), 4); self.assertEqual(len(self.review_calls), 3)
        self.assertEqual(len(self.diagnoses), 1)

    def test_empty_adr_before_diagnosis_does_not_reset_streak(self):
        def arch(rnd, previous, history):
            self.arch_calls.append(rnd)
            return '' if rnd == 3 else '# ADR ' + str(rnd)
        with self.assertRaisesRegex(PlanError, 'empty ADR'): self.make(resolve=False, architect=arch, initial_limit=8).run()
        with self.assertRaises(RecoveryStop) as stop: self.make(resolve=False, architect=arch, initial_limit=8).run()
        self.assertEqual(stop.exception.category, 'DESIGN_STALLED')
        self.assertEqual(len(self.diagnoses), 1)
        self.assertEqual(len(self.arch_calls), 5)

    def test_round_exhaustion_with_progress_policy_is_registered(self):
        with self.assertRaises(RecoveryStop) as stop: self.make(resolve=False, initial_limit=1).run()
        self.assertEqual(stop.exception.category, 'DESIGN_STALLED')

    def test_invalid_review_after_diagnosis_is_a_registered_stop_without_next_round(self):
        def reviewer(rnd, adr, history):
            self.review_calls.append(rnd)
            return review([finding()]) if rnd < 4 else 'not a valid review'
        for _ in range(2):
            with self.assertRaises(RecoveryStop) as stop: self.make(resolve=False, reviewer=reviewer).run()
            self.assertEqual(stop.exception.category, 'DESIGN_STALLED')
        self.assertEqual(len(self.arch_calls), 4); self.assertEqual(len(self.review_calls), 5)
        self.assertEqual(len(self.diagnoses), 1)

    def test_interrupted_architect_after_diagnosis_cannot_reserve_another_round(self):
        def architect(rnd, previous, history):
            self.arch_calls.append(rnd)
            if rnd == 4: raise RuntimeError('unknown ADR outcome')
            return '# ADR ' + str(rnd)
        with self.assertRaises(RuntimeError): self.make(resolve=False, architect=architect).run()
        with self.assertRaises(RecoveryStop) as stop: self.make(resolve=False, architect=architect).run()
        self.assertEqual(stop.exception.category, 'DESIGN_STALLED')
        self.assertEqual(self.arch_calls, [1, 2, 3, 4])
        self.assertEqual(len(self.diagnoses), 1)

    def test_overlapping_findings_do_not_spend_the_first_real_resolution_early(self):
        def reviewer(rnd, adr, history):
            self.review_calls.append(rnd)
            return review([
                finding('F1', status='RESOLVED', requirement_refs=['AC-1']),
                finding('F2', status='RESOLVED' if rnd >= 2 else 'OPEN', requirement_refs=['AC-1']),
                finding('F3', status='RESOLVED' if rnd >= 4 else 'OPEN', requirement_refs=['SEC-2']),
            ])
        self.make(reviewer=reviewer).run()
        rounds = self.state()['rounds']
        self.assertEqual(rounds[1]['progress']['resolved_obligations'], ['AC-1'])
        self.assertTrue(rounds[1]['progress']['material_progress'])
        self.assertEqual(self.diagnoses, [])
        self.assertEqual(len(self.arch_calls), 4)
