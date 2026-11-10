"""Read-only provenance checks through real bridges and temporary Git repos."""
import copy
import json
import re
from pathlib import Path
from unittest.mock import patch

import orchestrator
import ssdlc_bounce as bounce
import ssdlc_gate_reports as gates
from ssdlc_tasks import git
from test_full_checkpoint_review import framed
from test_gate_reports import GateFixture, Crash, finding


class EvidenceTests(GateFixture):
    def read(self, phase='04-review', attempt=1):
        # Restart from the persisted state, never a fixture-produced assessment.
        state = self.orch._load_or_init_state()
        return bounce.read_evidence(root=self.orch.ssdlc_root, state=state,
            repo=self.orch.repo_root, worktree=self.orch.worktree_dir, phase=phase, attempt=attempt)

    def assert_blocked(self, code=None, phase='04-review', attempt=1):
        value = self.read(phase, attempt)
        self.assertIn(value['status'], ('blocked', 'rejected-legacy'))
        self.assertNotIn('plan', value)
        self.assertFalse(value['execution_authorized'])
        if code:
            self.assertEqual(value['blockers'], [{'code': code}])
        return value

    def verify_gate(self, gate, det=True, scans=(), invoke=None, attempt=1):
        with patch.object(self.orch, '_run_deterministic_scanners', return_value=(det, 'det_ok=false; arbitrary output')), \
             patch.object(self.orch, '_run_appsec_scans', return_value=list(scans)), \
             patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke or (
                 lambda *a, **k: (framed(gate, self.orch), 0, 0, 0, 'fake'))) as engine:
            result = getattr(self.orch, 'run_phase_' + gate)(attempt=attempt)
        return result, engine

    def proposal(self, value):
        self.assertEqual(value['status'], 'verified', value)
        return bounce.plan_bounce(repo=self.orch.repo_root, artifacts=self.orch.ssdlc_root,
            plan=value['plan'], requirements_text=value['requirements_text'], quality=value['quality'],
            reviewed_commit=value['reviewed_commit'], evaluations=value['evaluations'],
            phase=value['phase'], attempt=value['attempt'])

    def restart(self):
        self.orch.state = self.orch._load_or_init_state()

    def rewrite(self, path, value):
        path.write_bytes(gates.canon(value) if not isinstance(value, str) else value.encode())
        self.orch.record_artifact(str(path.relative_to(self.orch.ssdlc_root)), path)

    def reseal(self, directory):
        outcome_path = directory / 'outcome.json'
        outcome = json.loads(outcome_path.read_text())
        outcome['manifest'] = {str(p.relative_to(directory)): bounce._sha(p.read_bytes())
            for p in directory.rglob('*') if p.is_file() and p.name != 'outcome.json'}
        self.rewrite(outcome_path, outcome)
        phase = directory.parent.parent.name
        for path in (self.orch.ssdlc_root / phase / 'provenance').glob('attempt-*/completion.json'):
            completion = json.loads(path.read_text())
            for member in completion['evaluators']:
                if member['outcome_ref'] == str(outcome_path.relative_to(self.orch.ssdlc_root)):
                    member['outcome_sha256'] = bounce._sha(outcome_path.read_bytes())
            self.rewrite(path, completion)

    def test_all_bridges_and_read_only_counters(self):
        self.assertTrue(self.run_review()[0][0])
        self.assertTrue(self.verify_gate('qa')[0][0])
        self.assertTrue(self.verify_gate('appsec')[0][0])
        before = copy.deepcopy(self.orch.state)
        raw = {str(p): p.read_bytes() for p in self.orch.ssdlc_root.rglob('*') if p.is_file()}
        for phase in bounce.PHASES:
            value = self.read(phase)
            plan = self.proposal(value)
            self.assertEqual(plan['status'], 'empty')
            self.assertFalse(plan['execution_authorized'])
            self.assertEqual(value['evaluations'][0]['response_ordinal'], 1)
            self.assertTrue(value['evaluations'][0]['telemetry_present'])
        self.assertEqual(self.orch.state, before)
        self.assertEqual(raw, {str(p): p.read_bytes() for p in self.orch.ssdlc_root.rglob('*') if p.is_file()})

    def test_mixed_panel_and_f1_collisions(self):
        with patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:a', 'mock:b', 'mock:c'], 'low')):
            def invoke(engine, model, *a, **k):
                return framed('review', self.orch, [] if model == 'c' else [finding()]), 0, 0, 0, 'fake'
            result, _ = self.run_review(invoke)
        self.assertFalse(result[0])
        value = self.read()
        self.assertEqual(len(value['evaluations']), 3)
        plan = self.proposal(value)
        self.assertEqual(len(plan['findings']), 2)
        self.assertNotEqual(plan['findings'][0]['identity'], plan['findings'][1]['identity'])
        self.assertEqual(plan['repair_roots'], ['T1'])

    def test_legacy_and_latest_incomplete_no_fallback(self):
        self.assert_blocked('rejected-legacy')
        self.run_review()
        original = self.orch._phase_roster
        def crash(*a):
            original(*a)
            raise Crash()
        with patch.object(self.orch, '_phase_roster', side_effect=crash), self.assertRaises(Crash):
            self.run_review(attempt=2)
        self.assert_blocked('superseded_attempt')
        self.assert_blocked('incomplete_evidence', attempt=2)
        self.restart()
        self.run_review(attempt=2)
        self.assertEqual(self.read(attempt=2)['status'], 'verified')

    def test_crash_after_roster_resume_and_after_evaluation_resume(self):
        for attempt, boundary in ((1, '_phase_roster'), (2, '_phase_completion')):
            original = getattr(self.orch, boundary)
            def crash(*args):
                if boundary == '_phase_roster':
                    original(*args)
                raise Crash()
            with patch.object(self.orch, boundary, side_effect=crash), self.assertRaises(Crash):
                self.run_review(attempt=attempt)
            self.assert_blocked('incomplete_evidence', attempt=attempt)
            self.restart()
            _, engine = self.run_review(attempt=attempt)
            if boundary == '_phase_completion':
                engine.assert_not_called()
            self.assertEqual(self.read(attempt=attempt)['status'], 'verified')

    def test_missing_and_mismatched_completion_and_partial_roster(self):
        self.run_review()
        base = self.orch.dir_review / 'provenance/attempt-1'
        path = base / 'completion.json'
        saved = path.read_bytes()
        path.unlink()
        self.assert_blocked('incomplete_evidence')
        path.write_bytes(saved)
        value = json.loads(saved)
        value['evaluators'][0]['evaluator']['identity'] = 'a' * 64
        self.rewrite(path, value)
        self.assert_blocked('contradictory_evidence')
        path.write_bytes(saved)
        self.orch.record_artifact(str(path.relative_to(self.orch.ssdlc_root)), path)
        roster = json.loads((base / 'roster.json').read_text())
        roster['evaluators'] = []
        self.rewrite(base / 'roster.json', roster)
        value = json.loads(saved)
        value['roster_sha256'] = bounce._sha(gates.canon(roster))
        self.rewrite(path, value)
        self.assert_blocked('incomplete_evidence')

    def test_stale_dirty_and_moved_checkpoint(self):
        self.run_review()
        app = self.orch.worktree_dir / 'app.py'
        original = app.read_bytes()
        app.write_text('dirty')
        self.assert_blocked('stale_checkpoint')
        app.write_bytes(original)
        old = self.orch.state['task_verified_head']
        self.orch.state['task_verified_head'] = self.base
        self.orch._save_state()
        self.assert_blocked('stale_checkpoint')
        self.orch.state['task_verified_head'] = old
        self.orch._save_state()
        git(self.orch.worktree_dir, 'checkout', '--detach', self.base)
        self.assert_blocked('stale_checkpoint')

    def test_tamper_every_evidence_layer(self):
        self.run_review()
        slot = self.slots()[0]
        files = [slot / name for name in ('input.json', 'report.json', 'outcome.json', 'prompt-1.json')]
        files += list((slot / 'evidence').glob('*'))
        files += [self.orch.dir_design / 'plan.json', self.orch.dir_recon / 'quality-contract.json']
        for path in files:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b' ')
                self.assert_blocked()
                path.write_bytes(original)
        outcome = json.loads((slot / 'outcome.json').read_text())
        outcome['manifest']['report.json'] = '0' * 64
        self.rewrite(slot / 'outcome.json', outcome)
        self.assert_blocked('integrity_mismatch')

    def test_registry_and_missing_registered_file(self):
        self.run_review()
        path = self.slots()[0] / 'report.json'
        ref = str(path.relative_to(self.orch.ssdlc_root))
        self.orch.state['artifacts'][ref]['sha256'] = '0' * 64
        self.orch._save_state()
        self.assert_blocked('integrity_mismatch')
        self.orch.record_artifact(ref, path)
        path.unlink()
        self.assert_blocked('integrity_mismatch')

    def test_forged_assessment_ignored_even_when_resealed(self):
        self.run_review(lambda *a, **k: (framed('review', self.orch, [finding()]), 0, 0, 0, 'fake'))
        slot = self.slots()[0]
        self.rewrite(slot / 'assessment.json', {'correction_roots': [], 'approved': True})
        self.reseal(slot)
        self.assertEqual(self.proposal(self.read())['repair_roots'], ['T1'])

    def test_f20_f21_response_crashes_resume_without_provider_call(self):
        original = gates.GateEvaluator._write
        for attempt, n in ((1, 1), (2, 2)):
            calls = []
            def invoke(*a, **k):
                calls.append(1)
                text = 'protocol failure' if n == 2 and len(calls) == 1 else framed('review', self.orch, [finding()] if n == 2 else [])
                return text, 0, 0, 0, 'fake'
            def write(evaluator, name, value):
                if name == f'invocation-{n}.json':
                    raise Crash()
                return original(evaluator, name, value)
            with patch.object(gates.GateEvaluator, '_write', write), self.assertRaises(Crash):
                self.run_review(invoke, attempt=attempt)
            self.assert_blocked('incomplete_evidence', attempt=attempt)
            self.restart()
            _, provider = self.run_review(attempt=attempt)
            provider.assert_not_called()
            value = self.read(attempt=attempt)
            self.assertEqual(value['status'], 'verified', value)
            record = value['evaluations'][0]
            self.assertEqual(record['response_ordinal'], n)
            self.assertFalse(record['telemetry_present'])
            self.assertEqual(record['informational'], ['telemetry_missing'])
            self.assertEqual(self.proposal(value)['repair_roots'], ['T1'] if n == 2 else [])

    def test_f22_f23_response_companion_contradictions(self):
        self.run_review()
        slot = self.slots()[0]
        for name in ('prompt-2.json', 'correction-record.json'):
            path = slot / name
            self.rewrite(path, {})
            self.reseal(slot)
            self.assert_blocked('contradictory_evidence')
            path.unlink()
            del self.orch.state['artifacts'][str(path.relative_to(self.orch.ssdlc_root))]
            self.orch._save_state()
            self.reseal(slot)
        (slot / 'response-1.md').rename(slot / 'response-2.md')
        del self.orch.state['artifacts'][str((slot / 'response-1.md').relative_to(self.orch.ssdlc_root))]
        self.orch.record_artifact(str((slot / 'response-2.md').relative_to(self.orch.ssdlc_root)), slot / 'response-2.md')
        self.reseal(slot)
        self.assert_blocked('contradictory_evidence')

    def test_malformed_telemetry(self):
        self.run_review()
        slot = self.slots()[0]
        self.rewrite(slot / 'invocation-1.json', [])
        self.reseal(slot)
        self.assert_blocked('malformed_record')

    def test_controls_empty_reports_and_qa_log_is_not_authority(self):
        for attempt, det, verdict in ((1, True, 'PASSED'), (2, False, 'PASSED'), (3, False, 'FAILED')):
            result, _ = self.verify_gate('qa', det=det, attempt=attempt, invoke=lambda *a, **k: (
                framed('qa', self.orch, verdict=verdict), 0, 0, 0, 'fake'))
            self.assertEqual(result[0], det)
            value = self.read('05-qa', attempt)
            plan = self.proposal(value)
            codes = {b['code'] for b in plan['blockers']}
            self.assertEqual('control_failed' in codes, not det)
            self.assertEqual('negative_verdict_without_roots' in codes, verdict == 'FAILED')
            self.assertFalse(plan['execution_authorized'])

    def test_f24_control_fingerprint_tamper(self):
        self.verify_gate('qa', det=False)
        slot = next((self.orch.dir_qa / 'evaluations').iterdir())
        data = json.loads((slot / 'input.json').read_text())
        data['decision_inputs']['controls'] = {'det_ok': True}
        self.rewrite(slot / 'input.json', data)
        self.reseal(slot)
        self.assert_blocked('contradictory_evidence', phase='05-qa')

    def test_f25_malformed_control_records(self):
        self.verify_gate('qa')
        slot = next((self.orch.dir_qa / 'evaluations').iterdir())
        data = json.loads((slot / 'input.json').read_text())
        for controls in ({}, {'det_ok': 'passed'}, {'det_ok': 1}, {'det_ok': True, 'x': True}, None):
            data['decision_inputs']['controls'] = controls
            self.rewrite(slot / 'input.json', data)
            self.reseal(slot)
            self.assert_blocked('malformed_record', phase='05-qa')

    def test_f26_appsec_contradictory_controls(self):
        self.verify_gate('appsec', scans=[gates.ScannerResult('fake', 'failed', True)])
        slot = next((self.orch.dir_appsec / 'evaluations').iterdir())
        data = json.loads((slot / 'input.json').read_text())
        data['decision_inputs']['controls'] = {'scanners': True}
        self.rewrite(slot / 'input.json', data)
        self.reseal(slot)
        self.assert_blocked('contradictory_evidence', phase='05b-appsec')

    def test_protocol_and_provider_outcomes_remain_diagnostics(self):
        for attempt, invoke in ((1, lambda *a, **k: ('bad', 0, 0, 0, 'fake')),
                                (2, lambda *a, **k: (_ for _ in ()).throw(RuntimeError('provider')))):
            with self.assertRaises(gates.GateTerminalError):
                self.run_review(invoke, attempt=attempt)
            value = self.read(attempt=attempt)
            plan = self.proposal(value)
            self.assertTrue(plan['diagnosis'])
            self.assertEqual(plan['repair_roots'], [])
            self.assertFalse(plan['execution_authorized'])

    def test_unknown_outcome_cannot_pass(self):
        self.run_review()
        slot = self.slots()[0]
        data = json.loads((slot / 'outcome.json').read_text())
        data.update(outcome_class='unknown', approved=False)
        self.rewrite(slot / 'outcome.json', data)
        self.reseal(slot)
        self.assert_blocked('contradictory_evidence')

    def test_binding_and_outcome_contradictions(self):
        self.run_review()
        slot = self.slots()[0]
        report = json.loads((slot / 'report.json').read_text())
        report['source_gate'] = 'qa'
        self.rewrite(slot / 'report.json', report)
        self.reseal(slot)
        self.assert_blocked('contradictory_evidence')

    def test_parity_with_live_integrity_and_no_reconciliation(self):
        captured = []
        real = gates.GateEvaluator.evaluate
        def evaluate(evaluator, request):
            result = real(evaluator, request)
            evaluator._integrity()
            captured.append(evaluator)
            return result
        with patch.object(gates.GateEvaluator, 'evaluate', evaluate):
            self.run_review()
        self.assertEqual(self.read()['status'], 'verified')
        evaluator = captured[0]
        path = evaluator.directory / 'prompt-1.json'
        path.write_text('{}')
        with self.assertRaises(Exception):
            evaluator._integrity()
        self.assert_blocked('integrity_mismatch')

    def test_d12_errors_translate_but_baseexception_escapes(self):
        self.run_review()
        with patch.object(bounce.TaskPlanValidator, 'validate', side_effect=ValueError('secret')):
            self.assert_blocked('malformed_record')
        with patch.object(bounce.TaskPlanValidator, 'validate', side_effect=Crash()), self.assertRaises(Crash):
            self.read()
        path = self.orch.dir_review / 'provenance/attempt-1/completion.json'
        self.rewrite(path, '{broken')
        self.assert_blocked('malformed_record')

    def test_malformed_outcome_preserves_wrapper_terminal_error(self):
        self.run_review()
        outcome = self.slots()[0] / 'outcome.json'
        outcome.write_text('{broken')
        with self.assertRaises(gates.GateTerminalError):
            self.run_review()
        self.assert_blocked('integrity_mismatch')

    def test_verdict_parser_parity(self):
        for gate, parser, marker, verdict in (
            ('review', orchestrator.parse_review_verdict, 'REVIEW_STATUS', 'APPROVED'),
            ('qa', orchestrator.parse_qa_verdict, 'QA_STATUS', 'PASSED'),
            ('appsec', orchestrator.parse_appsec_verdict, 'APPSEC_STATUS', 'SECURE')):
            for text in ('', f'{marker}: {verdict}', f'{marker}: {verdict}\nnoise',
                         f'{marker}: {verdict}\n{marker}: {verdict}', f'human\n {marker}: {verdict}\n'):
                self.assertEqual(bounce.parse_final_verdict(gate, text), parser(text))

    def test_selector_and_control_schemas(self):
        for invocations in range(3):
            records = {'response-1.md': '', 'diagnostics-1.json': []}
            records.update({f'invocation-{n}.json': {} for n in range(1, invocations + 1)})
            self.assertEqual(bounce.select_response(records), (1, 'response-1.md'))
            records.update({'response-2.md': '', 'prompt-2.json': {}, 'correction-record.json': {},
                            'diagnostics-1.json': [{}], 'diagnostics-2.json': []})
            self.assertEqual(bounce.select_response(records), (2, 'response-2.md'))
        for names in ({}, {'response-2.md': ''}, {'response-3.md': ''}, {'response-1.md.bak': ''}):
            with self.assertRaises(bounce.BounceError):
                bounce.select_response(names)
        self.assertEqual(bounce.validate_controls('review', {}), {})
        with self.assertRaises(bounce.BounceError):
            bounce.validate_controls('review', {'x': True})
