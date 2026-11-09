"""Supplemental integration regressions; every provider is a local fake."""
from dataclasses import replace
import json
import re
from unittest.mock import patch

import ssdlc_gate_reports as gates
from test_full_checkpoint_review import framed
from test_gate_reports import GateFixture, finding


class GateReportRegressions(GateFixture):
    def run_verification(self, gate, invoke, *, det=True):
        with patch.object(self.orch, '_run_deterministic_scanners', return_value=(det, 'same log')), \
             patch.object(self.orch, '_run_appsec_scans', return_value=[]), \
             patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke) as engine:
            return getattr(self.orch, 'run_phase_' + gate)(), engine

    def test_wrong_nonce_cannot_hide_a_reported_mandatory_finding(self):
        calls = []
        def invoke(*args, **kwargs):
            calls.append(1)
            text = framed('review', self.orch, [finding()] if len(calls) == 1 else [])
            if len(calls) == 1:
                text = re.sub(r'(SSDLC GATE REPORT )[0-9a-f]{16}', r'\1stale', text)
            return text, 0, 0, 0, 'fake'
        with self.assertRaises(gates.GateTerminalError):
            self.run_review(invoke)
        self.assertLessEqual(len(calls), 2)
        self.assertFalse(self.orch.state['gate_history'][-1]['passed'])
        self.assertTrue(any((slot/'response-1.md').is_file() for slot in self.slots()))

    def test_ambiguous_delimiters_stop_without_discarding_findings(self):
        for attempt, fault in enumerate(('different_nonces', 'duplicate', 'inline', 'unterminated'), 1):
            with self.subTest(fault=fault):
                def invoke(*args, **kwargs):
                    text = framed('review', self.orch, [finding()])
                    if fault == 'different_nonces':
                        text = re.sub(r'(BEGIN SSDLC GATE REPORT )[0-9a-f]{16}', r'\1stale', text)
                    elif fault == 'duplicate':
                        text += '\n' + text
                    elif fault == 'inline':
                        text = 'inline ' + text
                    else:
                        text = text.split('-----END SSDLC GATE REPORT')[0]
                    return text, 0, 0, 0, 'fake'
                with patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke) as engine:
                    with self.assertRaisesRegex(gates.GateTerminalError, 'correction_not_preservable'):
                        self.orch.run_phase_review(attempt=attempt)
                engine.assert_called_once()

    def test_nonce_repair_preserves_optional_and_mandatory_claims(self):
        for attempt, classification in enumerate(('optional_improvement', 'mandatory_violation_or_regression'), 1):
            with self.subTest(classification=classification):
                calls = []
                def invoke(*args, **kwargs):
                    calls.append(1)
                    text = framed('review', self.orch, [finding(classification)])
                    if len(calls) == 1:
                        text = re.sub(r'(SSDLC GATE REPORT )[0-9a-f]{16}', r'\1stale', text)
                    return text, 0, 0, 0, 'fake'
                result, engine = self.run_review(invoke, attempt)
                self.assertEqual(result[0], classification == 'optional_improvement')
                self.assertEqual(engine.call_count, 2)

    def test_cached_approval_cannot_override_a_current_failed_control(self):
        original = gates.GateEvaluator.evaluate
        state = {'passed': True}
        calls = []
        def evaluate(evaluator, request):
            return original(evaluator, replace(request, controls={'required_check': state['passed']}))
        def invoke(*args, **kwargs):
            calls.append(1)
            return framed('review', self.orch), 0, 0, 0, 'fake'
        with patch.object(gates.GateEvaluator, 'evaluate', evaluate):
            self.assertTrue(self.run_review(invoke)[0][0])
            state['passed'] = False
            with self.assertRaises(gates.GateTerminalError):
                self.run_review(invoke)
        self.assertEqual(len(calls), 1)

    def test_qa_cache_cannot_override_changed_deterministic_result(self):
        def invoke(*args, **kwargs):
            return framed('qa', self.orch), 0, 0, 0, 'fake'
        result, first = self.run_verification('qa', invoke)
        self.assertTrue(result[0])
        first.assert_called_once()
        result, replay = self.run_verification('qa', invoke)
        self.assertTrue(result[0])
        replay.assert_not_called()
        with patch.object(self.orch, '_run_deterministic_scanners', return_value=(False, 'same log')), \
             patch('orchestrator.EngineRunner.invoke_role') as engine:
            self.assertFalse(self.orch.run_phase_qa()[0])
        engine.assert_not_called()
        self.assertEqual(self.orch._gate_terminal, ('05-qa', 'integrity_blocked'))
        with self.assertRaises(gates.GateTerminalError):
            self.orch._stop_on_terminal_gate()

    def test_changed_prompt_effort_or_context_cannot_reuse_approval(self):
        original = gates.GateEvaluator.evaluate
        for attempt, field in enumerate(('user_prompt', 'reasoning_effort', 'context'), 1):
            with self.subTest(field=field):
                self.assertTrue(self.run_review(attempt=attempt)[0][0])
                def evaluate(evaluator, request):
                    value = {'user_prompt': request.user_prompt + '\nChanged policy',
                             'reasoning_effort': 'high',
                             'context': replace(request.context, obligation_ids=request.context.obligation_ids | {'AC-extra'})}[field]
                    return original(evaluator, replace(request, **{field: value}))
                with patch.object(gates.GateEvaluator, 'evaluate', evaluate), \
                     patch('orchestrator.EngineRunner.invoke_role') as engine:
                    with self.assertRaisesRegex(gates.GateTerminalError, 'different inputs'):
                        self.orch.run_phase_review(attempt=attempt)
                engine.assert_not_called()

    def test_control_mutation_during_invocation_blocks_approval(self):
        original = gates.GateEvaluator.evaluate
        controls = {'required_check': True}
        def evaluate(evaluator, request):
            return original(evaluator, replace(request, controls=controls))
        def invoke(*args, **kwargs):
            controls['required_check'] = False
            return framed('review', self.orch), 0, 0, 0, 'fake'
        with patch.object(gates.GateEvaluator, 'evaluate', evaluate), self.assertRaises(gates.GateTerminalError):
            self.run_review(invoke)
        self.assertFalse(self.orch.state['gate_history'][-1]['passed'])

    def test_provider_receives_the_approved_plan_schema_and_actual_gate(self):
        quality = json.loads((self.orch.dir_recon / 'quality-contract.json').read_text())
        for gate in ('review', 'qa', 'appsec'):
            with self.subTest(gate=gate):
                def invoke(engine, model, system, user, *args, **kwargs):
                    records = {}
                    for line in user.splitlines():
                        try:
                            value = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(value, dict) and 'label' in value and 'untrusted_data' in value:
                            records[value['label']] = value['untrusted_data']
                    self.assertEqual(records['approved task plan'], self.plan)
                    self.assertEqual(records['quality contract'], quality)
                    self.assertEqual(records['trusted context']['source_gate'],
                                     'code_review' if gate == 'review' else gate)
                    schema = records['gate findings schema']
                    self.assertIn('classification', schema['properties']['findings']['items']['properties'])
                    return framed(gate, self.orch), 0, 0, 0, 'fake'
                result, engine = self.run_review(invoke) if gate == 'review' else self.run_verification(gate, invoke)
                self.assertTrue(result[0])
                engine.assert_called_once()
