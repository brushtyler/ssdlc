"""Review integration and disk-boundary recovery; no provider is contacted."""
import copy
import inspect
import json
import os
from pathlib import Path
from unittest.mock import patch

import ssdlc_gate_reports as gates
from orchestrator import SSDLCOrchestrator, CostAndTelemetryEntry
from ssdlc_tasks import TaskError
import test_full_checkpoint_review as checkpoints
from test_full_checkpoint_review import framed
from test_task_bootstrap import RepositoryTests

GATE_PHASE = {'review': '04-review', 'qa': '05-qa', 'appsec': '05b-appsec'}


class Crash(BaseException):
    pass


def finding(classification='mandatory_violation_or_regression', **changes):
    value = dict(id='F1', severity='high', classification=classification,
        evidence_kind='observed', scenario='A concrete defect', rationale='Behavior violates the contract',
        task_attribution=['T1'], file_refs=['app.py'], obligation_refs=['AC-1'],
        evidence_refs=['ev/diff'], violates_existing_obligation=classification == 'mandatory_violation_or_regression',
        introduces_regression=False, necessary_for_requested_correctness=classification == 'necessary_gap')
    value.update(changes)
    return value


def add_ledger_cost(orch, usd):
    orch.record_ledger(CostAndTelemetryEntry('fixture', 'fixture', 'fake', 'fake', 'COMPLETED', 0, usd))
    orch._save_state()


def selective_breaker(orch, gate, *, post_call=False):
    real = orch.check_budget_circuit_breaker
    seen = []
    def breaker():
        phase = orch.state['current_phase']
        delegated = phase == GATE_PHASE[gate]
        seen.append((phase, delegated))
        if delegated:
            return real()
    return breaker, seen


def assert_terminal_failure(test, orch, gate, captured, error_type='GateTerminalError',
                            status='ESCALATED_TO_SUPERVISOR'):
    failure = orch.state['failure']
    test.assertEqual(captured[0], GATE_PHASE[gate])
    test.assertEqual(failure['phase'], captured[0])
    test.assertEqual(failure['type'], error_type)
    test.assertTrue(failure['category'])
    test.assertTrue(failure['message'])
    test.assertEqual(orch.state['current_phase'], captured[0])
    test.assertEqual(orch.state['current_step'], captured[1])
    test.assertEqual(orch.state['status'], status)
    test.assertTrue(any(g['gate'] == 'TASK_EXECUTION_GATE' and not g['passed'] for g in orch.state['gate_history']))
    reloaded = orch._load_or_init_state()
    test.assertEqual(reloaded['failure'], failure)
    test.assertEqual(reloaded['current_phase'], captured[0])
    test.assertEqual(reloaded['current_step'], captured[1])
    test.assertEqual(reloaded['status'], status)
    pack = orch.ssdlc_root / 'EVIDENCE_PACK.md'
    test.assertTrue(pack.is_file())
    test.assertIn('TASK_EXECUTION_GATE', pack.read_text())


class GateFixture(RepositoryTests):
    seed = checkpoints.FullCheckpointReviewTests.seed

    def setUp(self):
        super().setUp()
        self.orch, self.diff = self.seed()
        self.panel = patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:reviewer'], 'low'))
        self.panel.start()
        self.addCleanup(self.panel.stop)

    def valid(self, *args, **kwargs):
        return framed('review', self.orch), 0, 0, 0, 'fake'

    def run_review(self, engine=None, attempt=1):
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=engine or self.valid) as invoke:
            result = self.orch.run_phase_review(attempt=attempt)
        return result, invoke

    def slots(self):
        return sorted((self.orch.dir_review / 'evaluations').glob('*'))


class ReviewTests(GateFixture):
    def test_constructor_initializes_terminal_hook(self):
        self.assertIsNone(self.orch._gate_terminal)
        self.orch._stop_on_terminal_gate()

    def test_empty_optional_and_all_blocking_assessments(self):
        cases = [(None, True), ('optional_improvement', True), ('mandatory_violation_or_regression', False),
                 ('necessary_gap', False), ('uncertain_needs_diagnosis', False)]
        for attempt, (classification, expected) in enumerate(cases, 1):
            with self.subTest(classification=classification):
                fs = [] if classification is None else [finding(classification)]
                result, engine = self.run_review(lambda *a, **k: (framed('review', self.orch, fs), 0, 0, 0, 'fake'), attempt)
                self.assertEqual(result[0], expected)
                self.assertEqual(engine.call_count, 1)
        for path in self.slots():
            assessment = json.loads((path/'assessment.json').read_text())
            self.assertFalse(assessment['execution_authorized'])

    def test_unattributed_and_cross_task_findings_preserved(self):
        self.plan['tasks'].append(self.task('T2', [], [], ['second.py'], ['T1']))
        from ssdlc_tasks import atomic_json
        atomic_json(self.orch.dir_design/'plan.json', self.plan)
        self.orch.record_artifact('02-design/plan.json', self.orch.dir_design/'plan.json')
        for attempt, attribution in enumerate(([], ['T1', 'T2']), 1):
            f = finding(task_attribution=attribution)
            result, _ = self.run_review(lambda *a, **k: (framed('review', self.orch, [f]), 0, 0, 0, 'fake'), attempt)
            self.assertFalse(result[0])
        for path in self.slots():
            assessment = json.loads((path/'assessment.json').read_text())
            self.assertEqual(assessment['impact_assessment_required'], ['F1'])

    def test_all_reviewers_evaluated_and_terminal_is_not_laundered(self):
        with patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:A', 'mock:B'], 'low')):
            def invoke(engine, model, *a, **k):
                return self.valid() if model == 'A' else ('No frame', 0, 0, 0, 'fake')
            with self.assertRaises(gates.GateTerminalError), patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke) as engine:
                self.orch.run_phase_review()
            self.assertEqual(engine.call_count, 3)
            self.assertEqual(len(self.slots()), 2)
            self.assertFalse(self.orch.state['gate_history'][-1]['passed'])

    def test_stale_binding_and_unknown_references(self):
        cases = [('reviewed_commit', 'a'*40), ('source_gate', 'qa'), ('task_attribution', ['unknown']),
                 ('obligation_refs', ['AC-unknown']), ('evidence_refs', ['ev/missing'])]
        for attempt, (field, value) in enumerate(cases, 1):
            with self.subTest(field=field):
                def invoke(*a, **k):
                    if field in ('source_gate', 'reviewed_commit'):
                        text = framed('review', self.orch, **{field: value})
                    else:
                        text = framed('review', self.orch, [finding(**{field: value})])
                    return text, 0, 0, 0, 'fake'
                with self.assertRaises(gates.GateTerminalError):
                    self.run_review(invoke, attempt)

    def test_successful_exhausted_and_contradictory_correction(self):
        calls = []
        def fixed(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                correction = json.loads(a[3].splitlines()[-1])['untrusted_data']
                self.assertEqual(set(correction), {'instruction', 'original_response', 'diagnostics'})
                self.assertLessEqual(len(correction['diagnostics']), 4000)
            return ('No frame' if len(calls) == 1 else framed('review', self.orch)), 0, 0, 0, 'fake'
        result, engine = self.run_review(fixed)
        self.assertTrue(result[0])
        self.assertEqual(engine.call_count, 2)
        with self.assertRaises(gates.GateTerminalError):
            self.run_review(lambda *a, **k: ('No frame', 0, 0, 0, 'fake'), 2)
        calls.clear()
        def contradiction(*a, **k):
            calls.append(1)
            return framed('review', self.orch, [finding()] if len(calls) == 1 else [],
                          source_gate='qa' if len(calls) == 1 else 'code_review'), 0, 0, 0, 'fake'
        with self.assertRaises(gates.GateTerminalError) as caught:
            self.run_review(contradiction, 3)
        self.assertIn('correction_contradiction', str(caught.exception))

    def test_engine_exception_and_baseexception_reservations_survive(self):
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=RuntimeError('provider down')):
            with self.assertRaises(gates.GateTerminalError) as caught:
                self.orch.run_phase_review()
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        with patch('orchestrator.EngineRunner.invoke_role') as engine, self.assertRaises(gates.GateTerminalError):
            self.orch.run_phase_review()
        engine.assert_not_called()
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=Crash()), self.assertRaises(Crash):
            self.orch.run_phase_review(attempt=2)
        with patch('orchestrator.EngineRunner.invoke_role') as engine, self.assertRaises(gates.GateTerminalError):
            self.orch.run_phase_review(attempt=2)
        engine.assert_not_called()
        self.assertEqual(sum(len(list(p.glob('reservation-*.json'))) for p in self.slots()), 2)

    def test_mutation_of_source_evidence_and_input_blocks_without_correction(self):
        for index, kind in enumerate(('source', 'evidence', 'input'), 1):
            with self.subTest(kind=kind):
                source = self.orch.worktree_dir/'app.py'
                evidence = self.orch.dir_po/'requirements.md'
                old_source, old_evidence = source.read_bytes(), evidence.read_bytes()
                def invoke(*a, **k):
                    text = framed('review', self.orch)
                    if kind == 'source':
                        source.write_text('tampered')
                    elif kind == 'evidence':
                        evidence.write_text('AC-1: changed\nSEC-1: changed')
                    else:
                        current = max((self.orch.dir_review/'evaluations').glob('*/input.json'), key=lambda p:p.stat().st_mtime_ns)
                        current.write_text('{}')
                    return text, 0, 0, 0, 'fake'
                with patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke) as engine, self.assertRaises(gates.GateTerminalError):
                    self.orch.run_phase_review(attempt=index)
                self.assertEqual(engine.call_count, 1)
                source.write_bytes(old_source)
                evidence.write_bytes(old_evidence)

    def test_slot_reuse_changed_fingerprint_and_distinct_attempt_archives(self):
        self.run_review()
        before = {str(p): p.read_bytes() for p in self.slots()[0].rglob('*') if p.is_file()}
        self.run_review(attempt=2)
        self.assertEqual(len(self.slots()), 2)
        self.assertTrue(all(Path(p).read_bytes() == data for p, data in before.items()))
        with patch('orchestrator.EngineRunner.invoke_role') as engine:
            self.assertTrue(self.orch.run_phase_review()[0])
        engine.assert_not_called()
        (self.orch.dir_design/'adr.md').write_text('new design')
        with self.assertRaisesRegex(gates.GateTerminalError, 'different inputs'):
            self.run_review()

    def test_registered_plan_guard_restores_phase_even_on_validation_error(self):
        self.orch.state.update(current_phase='04-review', current_step='test')
        with patch.object(self.orch, '_ensure_task_plan', side_effect=TaskError('invalid')):
            with self.assertRaises(TaskError):
                self.orch._gate_plan()
        self.assertEqual(self.orch.state['current_phase'], '04-review')
        self.assertEqual(self.orch.state['current_step'], 'test')
        del self.orch.state['artifacts']['02-design/plan.json']
        with patch.object(self.orch, '_ensure_task_plan') as generator, self.assertRaisesRegex(TaskError, 'registered task plan'):
            self.orch._gate_plan()
        generator.assert_not_called()

    def test_s3_s4_tampered_missing_and_stale_checkpoint(self):
        attempt = 0
        for state in ('S3', 'S4'):
            for fault in ('tampered', 'missing', 'stale'):
                attempt += 1
                with self.subTest(state=state, fault=fault):
                    if state == 'S3':
                        original = gates.write_exclusive
                        def write(path, data, **kwargs):
                            if Path(path).name == 'outcome.json':
                                raise Crash()
                            return original(path, data, **kwargs)
                        with patch.object(gates, 'write_exclusive', side_effect=write), self.assertRaises(Crash):
                            self.run_review(attempt=attempt)
                    else:
                        self.run_review(attempt=attempt)
                    directory = max(self.slots(), key=lambda p:(p/'input.json').stat().st_mtime_ns)
                    if fault == 'tampered':
                        (directory/'response-1.md').write_text('changed')
                    elif fault == 'missing':
                        (directory/'response-1.md').unlink()
                    else:
                        # The source verifier rejects a live checkpoint substitution.
                        original_head = self.orch.state['task_verified_head']
                        self.orch.state['task_verified_head'] = self.base
                    with patch('orchestrator.EngineRunner.invoke_role') as engine, self.assertRaises(TaskError):
                        self.orch.run_phase_review(attempt=attempt)
                    engine.assert_not_called()
                    if fault == 'stale':
                        self.orch.state['task_verified_head'] = original_head

    def test_path_confinement_and_permissions(self):
        self.run_review()
        for path in self.slots()[0].rglob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(TaskError):
            gates.write_exclusive(self.orch.ssdlc_root/'../escape', b'bad', root=self.orch.ssdlc_root)
        link = self.orch.dir_review/'link'
        link.symlink_to(self.repo)
        with self.assertRaises(TaskError):
            gates.write_exclusive(link/'escape', b'bad', root=self.orch.ssdlc_root)

    def test_fault_boundaries_resume_without_repeating_invocation(self):
        # FP4/4b are covered separately; FP13/14 have real budget tests below.
        cases = [('FP1', 'input.json', False, False), ('FP2', 'reservation-1.json', False, False),
                 ('FP3', 'reservation-1.json', True, True), ('FP5', 'response-1.md', False, True),
                 ('FP7', 'prompt-2.json', False, False), ('FP8', 'reservation-2.json', True, True),
                 ('FP9', 'outcome.json', False, False), ('FP12', 'outcome.json', False, False)]
        for attempt, (label, suffix, after, terminal) in enumerate(cases, 1):
            with self.subTest(boundary=label):
                original = gates.write_exclusive
                calls = []
                correction = label in ('FP7', 'FP8', 'FP12')
                def invoke(*a, **k):
                    calls.append(1)
                    return ('No frame' if correction and len(calls) == 1 else framed('review', self.orch)), 0, 0, 0, 'fake'
                def write(path, data, **kwargs):
                    if Path(path).name == suffix:
                        if after:
                            original(path, data, **kwargs)
                        raise Crash(label)
                    return original(path, data, **kwargs)
                with patch.object(gates, 'write_exclusive', side_effect=write), self.assertRaises(Crash):
                    self.run_review(invoke, attempt)
                consumed = len(calls)
                if terminal:
                    with self.assertRaises(gates.GateTerminalError):
                        self.run_review(invoke, attempt)
                    self.assertEqual(len(calls), consumed)
                else:
                    self.assertTrue(self.run_review(invoke, attempt)[0][0])
                    expected = 2 if correction else 1
                    self.assertEqual(len(calls), expected)

    def test_callback_faults_resume_idempotently(self):
        for attempt, (label, callback, suffix) in enumerate((
                ('FP6', 'record_ledger', None), ('FP10', 'record_gate', None),
                ('FP11', 'record_artifact', 'response-1.md')), 1):
            with self.subTest(boundary=label):
                real = getattr(self.orch, callback)
                def fault(*args, **kwargs):
                    if suffix is None or str(args[0]).endswith(suffix):
                        raise Crash(label)
                    return real(*args, **kwargs)
                with patch.object(self.orch, callback, side_effect=fault), self.assertRaises(Crash):
                    self.run_review(attempt=attempt)
                with patch('orchestrator.EngineRunner.invoke_role') as engine:
                    self.assertTrue(self.orch.run_phase_review(attempt=attempt)[0])
                engine.assert_not_called()
                entries = [e for e in self.orch.state['ledger'] if e.get('details', {}).get('attempt') == attempt]
                self.assertEqual(len(entries), 1)

    def test_fp13_budget_before_correction_retains_allowance(self):
        count = []
        def budget():
            count.append(1)
            if len(count) == 3:
                raise TaskError('budget')
        with patch.object(self.orch, 'check_budget_circuit_breaker', side_effect=budget), self.assertRaisesRegex(TaskError, 'budget'):
            self.run_review(lambda *a, **k: ('No frame', 0, 0, 0, 'fake'))
        directory = self.slots()[0]
        self.assertTrue((directory/'reservation-1.json').exists())
        self.assertFalse((directory/'reservation-2.json').exists())
        self.assertTrue(self.run_review()[0][0])
        self.assertEqual(len(list(directory.glob('reservation-*.json'))), 2)


def _pipeline(self, engine, *, outer=False, breaker=None, retries='2'):
    orch = self.orch
    with patch.dict(os.environ, {'SSDLC_EXECUTION_MODE': 'local', 'SSDLC_MAX_DEV_RETRIES': retries}), \
         patch.object(orch, 'run_phase_dev') as developer, \
         patch.object(orch, '_ensure_task_plan', return_value=self.plan), \
         patch('orchestrator.EngineRunner.invoke_role', side_effect=engine) as invoke:
        self.developer = developer
        self.engine = invoke
        method = orch.run_pipeline if outer else orch._run_pipeline
        if breaker is None:
            return method('test goal', from_phase='03-dev')
        with patch.object(orch, 'check_budget_circuit_breaker', side_effect=breaker):
            return method('test goal', from_phase='03-dev')


class PipelineBoundaryTests(GateFixture):
    pipeline = _pipeline

    def test_layer_a_protocol_propagation_no_developer_retry(self):
        with self.assertRaises(gates.GateTerminalError):
            self.pipeline(lambda *a, **k: ('No frame', 0, 0, 0, 'fake'))
        self.developer.assert_called_once_with(feedback='', attempt=1)
        self.assertEqual(self.engine.call_count, 2)

    def test_layer_a_engine_exception_cause(self):
        def invoke(*a, **k):
            raise RuntimeError('provider failed')
        with self.assertRaises(gates.GateTerminalError) as caught:
            self.pipeline(invoke)
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.developer.assert_called_once_with(feedback='', attempt=1)

    def test_layer_b_protocol_and_engine_error_failure_metadata(self):
        captured = []
        def invoke(*a, **k):
            captured[:] = [self.orch.state['current_phase'], self.orch.state.get('current_step')]
            return 'No frame', 0, 0, 0, 'fake'
        with self.assertRaises(gates.GateTerminalError):
            self.pipeline(invoke, outer=True)
        assert_terminal_failure(self, self.orch, 'review', captured)
        self.developer.assert_called_once_with(feedback='', attempt=1)

    def test_layer_b_engine_exception(self):
        captured = []
        def invoke(*a, **k):
            captured[:] = [self.orch.state['current_phase'], self.orch.state.get('current_step')]
            raise RuntimeError('provider failed')
        with self.assertRaises(gates.GateTerminalError):
            self.pipeline(invoke, outer=True)
        assert_terminal_failure(self, self.orch, 'review', captured)

    def test_layer_b_interrupted_reservation_no_recall(self):
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=Crash()), self.assertRaises(Crash):
            self.orch.run_phase_review()
        captured = []
        real = gates.GateEvaluator.evaluate
        def evaluate(evaluator, request):
            captured[:] = [self.orch.state['current_phase'], self.orch.state.get('current_step')]
            return real(evaluator, request)
        with patch.object(gates.GateEvaluator, 'evaluate', evaluate), self.assertRaises(gates.GateTerminalError):
            self.pipeline(self.valid, outer=True)
        self.engine.assert_not_called()
        assert_terminal_failure(self, self.orch, 'review', captured)

    def test_budget_gate_pre_reservation_and_phase_selective_boundary(self):
        add_ledger_cost(self.orch, 2)
        breaker, seen = selective_breaker(self.orch, 'review')
        with patch.dict(os.environ, {'SSDLC_BUDGET_CAP_USD': '1'}), self.assertRaises(TaskError):
            self.pipeline(self.valid, outer=True, breaker=breaker)
        self.assertEqual(seen, [('INIT', False), ('04-review', True)])
        self.engine.assert_not_called()
        self.assertFalse(list(self.orch.dir_review.glob('evaluations/*/reservation-1.json')))
        assert_terminal_failure(self, self.orch, 'review', (seen[-1][0], self.orch.state.get('current_step')),
                                'TaskError', 'HALTED_BUDGET_EXCEEDED')
        self.developer.assert_called_once_with(feedback='', attempt=1)
        self.assertTrue(any(g['gate'] == 'BUDGET_CAP_GATE' and not g['passed'] for g in self.orch.state['gate_history']))

    def test_budget_gate_postcall_retains_response_s3(self):
        breaker, seen = selective_breaker(self.orch, 'review')
        def invoke(*a, **k):
            text, _, tin, tout, trace = self.valid()
            return text, 2, tin, tout, trace
        with patch.dict(os.environ, {'SSDLC_BUDGET_CAP_USD': '1'}), self.assertRaises(TaskError):
            self.pipeline(invoke, outer=True, breaker=breaker)
        self.assertEqual(seen, [('INIT', False), ('04-review', True), ('04-review', True)])
        self.assertEqual(self.engine.call_count, 1)
        directory = self.slots()[0]
        self.assertTrue((directory/'response-1.md').exists())
        self.assertFalse((directory/'outcome.json').exists())
        self.assertTrue(any('budget exhausted; evaluation' in str(g['details']) for g in self.orch.state['gate_history']))
        assert_terminal_failure(self, self.orch, 'review', ('04-review', self.orch.state.get('current_step')),
                                'TaskError', 'HALTED_BUDGET_EXCEEDED')
        with patch('orchestrator.EngineRunner.invoke_role') as engine:
            self.assertTrue(self.orch.run_phase_review()[0])
        engine.assert_not_called()

    def test_budget_inherited_control_fails_at_init_before_review(self):
        add_ledger_cost(self.orch, 2)
        seen = []
        real = self.orch.check_budget_circuit_breaker
        def breaker():
            seen.append(self.orch.state['current_phase'])
            return real()
        with patch.dict(os.environ, {'SSDLC_BUDGET_CAP_USD': '1'}), \
             patch.object(self.orch, 'run_phase_review') as review, self.assertRaises(TaskError):
            self.pipeline(self.valid, outer=True, breaker=breaker)
        self.assertEqual(seen, ['INIT'])
        review.assert_not_called()
        self.developer.assert_called_once_with(feedback='', attempt=1)
        self.assertEqual(self.orch.state['failure']['phase'], seen[0])
        self.assertEqual(self.orch.state['status'], 'HALTED_BUDGET_EXCEEDED')
        self.assertEqual(self.orch._load_or_init_state()['failure']['phase'], seen[0])

    def test_substantive_rejection_preserves_developer_feedback(self):
        def invoke(*a, **k):
            return framed('review', self.orch, [finding()], verdict='CHANGES_REQUESTED'), 0, 0, 0, 'fake'
        self.pipeline(invoke, outer=True)
        self.assertEqual(self.developer.call_count, 2)
        self.assertEqual(self.developer.call_args.kwargs['attempt'], 2)
        self.assertIn('CHANGES_REQUESTED', self.developer.call_args.kwargs['feedback'])
        self.assertNotIn('failure', self.orch.state)
        self.assertEqual(self.orch.state['status'], 'ESCALATED_TO_SUPERVISOR')
        self.assertEqual(self.orch.state['current_phase'], '08-supervisor')

    def test_reviewer_substantive_rejection_cannot_hide_terminal_peer(self):
        def invoke(engine, model, *a, **k):
            if model == 'A':
                return framed('review', self.orch, [finding()], verdict='CHANGES_REQUESTED'), 0, 0, 0, 'fake'
            return 'No frame', 0, 0, 0, 'fake'
        with patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:A', 'mock:B'], 'low')), self.assertRaises(gates.GateTerminalError):
            self.pipeline(invoke)
        self.developer.assert_called_once_with(feedback='', attempt=1)
        self.assertEqual(self.engine.call_count, 3)

    def test_qa_mutation_terminal_hook_layer_a(self):
        def invoke(*a, **k):
            phase = self.orch.state['current_phase']
            if phase == '04-review':
                return self.valid()
            (self.orch.worktree_dir/'app.py').write_text('mutated during QA')
            return 'QA_STATUS: PASSED', 0, 0, 0, 'fake'
        with patch.object(self.orch, '_run_deterministic_scanners', return_value=(True, 'passed')), self.assertRaises(gates.GateTerminalError):
            self.pipeline(invoke)
        self.developer.assert_called_once_with(feedback='', attempt=1)

    def test_qa_mutation_terminal_hook_layer_b(self):
        captured = []
        def invoke(*a, **k):
            if self.orch.state['current_phase'] == '04-review':
                return self.valid()
            captured[:] = [self.orch.state['current_phase'], self.orch.state.get('current_step')]
            (self.orch.worktree_dir/'app.py').write_text('mutated during QA')
            return 'QA_STATUS: PASSED', 0, 0, 0, 'fake'
        with patch.object(self.orch, '_run_deterministic_scanners', return_value=(True, 'passed')), self.assertRaises(gates.GateTerminalError):
            self.pipeline(invoke, outer=True)
        assert_terminal_failure(self, self.orch, 'qa', captured)
        self.developer.assert_called_once_with(feedback='', attempt=1)

    def test_source_level_exception_and_budget_guards(self):
        pipeline = inspect.getsource(SSDLCOrchestrator._run_pipeline)
        self.assertNotIn("['failure']", pipeline)
        self.assertNotIn('["failure"]', pipeline)
        pack = inspect.getsource(SSDLCOrchestrator.compile_evidence_pack)
        self.assertIn('"AWAITING_SUPERVISOR_SIGNOFF" if ready_for_signoff else "ESCALATED_TO_SUPERVISOR"', pack)
        handler = inspect.getsource(SSDLCOrchestrator.run_pipeline)
        self.assertLess(handler.index('except TaskError'), handler.index("failed_phase = self.state.get('current_phase')"))
        for gate, phase in GATE_PHASE.items():
            source = inspect.getsource(getattr(SSDLCOrchestrator, 'run_phase_' + gate))
            self.assertIn(f'self.state["current_phase"] = "{phase}"', source)
        dev = pipeline.index('self.run_phase_dev(')
        review = pipeline.index('self.run_phase_review(')
        self.assertIn('self.check_budget_circuit_breaker()', pipeline[dev:review])
        self.assertNotIn('current_phase', inspect.getsource(SSDLCOrchestrator.check_budget_circuit_breaker))
        self.assertEqual(pipeline.count('self._stop_on_terminal_gate()'), 3)


class VerificationGateTests(GateFixture):
    def call_gate(self, gate, *, det=True, scanners=None, mutate=False, attempt=1):
        def invoke(engine, model, system, user, cwd, *args, **kwargs):
            self.assertEqual(cwd, self.orch.worktree_dir)
            self.assertEqual(kwargs['sandbox'], 'read-only')
            self.assertIn(self.diff, user)
            self.assertIn('FINAL_DIFF_SENTINEL', user)
            response = framed(gate, self.orch)
            if mutate:
                (cwd / 'app.py').write_text('changed during gate')
            return response, 0, 0, 0, 'fake'
        with patch.object(self.orch, '_run_deterministic_scanners', return_value=(det, 'deterministic log')), \
             patch.object(self.orch, '_run_appsec_scans', return_value=scanners or []), \
             patch.object(self.orch, 'run_phase_dev') as dev, \
             patch('orchestrator.TaskScheduler') as scheduler, \
             patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
            result = getattr(self.orch, 'run_phase_' + gate)(attempt=attempt)
        dev.assert_not_called()
        scheduler.assert_not_called()
        return result

    def test_qa_deterministic_failure_cannot_be_overridden(self):
        self.assertFalse(self.call_gate('qa', det=False)[0])
        self.assertTrue(self.call_gate('qa', attempt=2)[0])

    def test_qa_checkpoint_precedes_scanners(self):
        (self.orch.worktree_dir / 'app.py').write_text('dirty')
        with patch.object(self.orch, '_run_deterministic_scanners') as scan, self.assertRaises(TaskError):
            self.orch.run_phase_qa()
        scan.assert_not_called()

    def test_appsec_complete_diff_controls_and_full_logs(self):
        for attempt, status in enumerate(('failed', 'unknown', 'not_applicable', 'passed'), 1):
            log = 'L' * 10000 + 'LOG_END'
            scan = gates.ScannerResult('npm:.', status, status != 'not_applicable', log=log)
            self.assertEqual(self.call_gate('appsec', scanners=[scan], attempt=attempt)[0],
                             status in ('passed', 'not_applicable'))
            self.assertIn(log, (self.orch.dir_appsec / f'supply-chain-sca-a{attempt}.md').read_text())
        for path in self.orch.dir_appsec.glob('evaluations/*/scanner-status.json'):
            self.assertIn('LOG_END', path.read_text())

    def test_appsec_mutation_returns_false_and_terminal_flag(self):
        self.assertFalse(self.call_gate('appsec', mutate=True)[0])
        self.assertEqual(self.orch.state['ledger'][-1]['status'], 'INPUT_CHANGED')
        with self.assertRaises(gates.GateTerminalError):
            self.orch._stop_on_terminal_gate()

    def test_scanner_manifest_scopes_and_tools(self):
        from ssdlc_tasks import git
        wt = self.orch.worktree_dir
        cases = [({}, 'not_applicable'), ({'name': 'empty'}, 'not_applicable')]
        cases += [({key: {'dep': '1'}}, 'unknown') for key in
                  ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies')]
        cases += [('malformed', 'unknown'), ([], 'unknown')]
        for obj, expected in cases:
            (wt / 'package.json').write_text(obj if isinstance(obj, str) else json.dumps(obj))
            git(wt, 'add', 'package.json')
            with patch('orchestrator.EngineRunner.run_sandboxed_cmd') as run:
                results = self.orch._run_appsec_scans(wt)
            run.assert_not_called()
            npm = next(r for r in results if r.id == 'npm:.')
            self.assertEqual(npm.status, expected)
            self.assertEqual(npm.required, expected == 'unknown')
        (wt / 'package.json').write_text('{}')
        (wt / 'package-lock.json').write_text('{}')
        nested = wt / 'packages/a'
        nested.mkdir(parents=True)
        (nested / 'package.json').write_text('{"dependencies":{"a":"1"}}')
        (wt / 'requirements-dev.txt').write_text('dep')
        git(wt, 'add', '.')
        with patch('orchestrator.shutil.which', return_value=None), \
             patch('orchestrator.EngineRunner._should_spawn_role_container', return_value=False):
            results = self.orch._run_appsec_scans(wt)
        self.assertTrue(all(r.status == 'unknown' for r in results))
        self.assertIn('npm:packages/a', [r.id for r in results])
        for rc, expected in ((0, 'passed'), (1, 'failed')):
            with patch('orchestrator.shutil.which', return_value='/tool'), \
                 patch('orchestrator.EngineRunner.run_sandboxed_cmd', return_value=('X' * 9000, 'TAIL', rc)) as run:
                results = self.orch._run_appsec_scans(wt)
            root = next(r for r in results if r.id == 'npm:.')
            self.assertEqual(root.status, expected)
            self.assertEqual(root.log, 'X' * 9000 + 'TAIL')
            self.assertTrue(all('install' not in call.args[0] for call in run.call_args_list))
        with patch('orchestrator.shutil.which', return_value='/tool'), \
             patch('orchestrator.EngineRunner.run_sandboxed_cmd', side_effect=TimeoutError('timeout')):
            self.assertTrue(all(r.status == 'unknown' for r in self.orch._run_appsec_scans(wt)))

    def test_required_npm_without_lock_blocks_secure_gate(self):
        from ssdlc_tasks import git
        wt = self.orch.worktree_dir
        for attempt, key in enumerate(('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'), 1):
            path = wt / ('packages/a/package.json' if key == 'peerDependencies' else 'package.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({key: {'dep': '1'}}))
            git(wt, 'add', '.')
            git(wt, 'commit', '-m', 'dependency manifest')
            self.orch.state['task_verified_head'] = git(wt, 'rev-parse', 'HEAD')
            diff = git(wt, 'diff', '--binary', self.orch.state['base_commit'], 'HEAD') + '\n'
            target = self.orch.dir_dev / 'implementation.diff'
            target.write_text(diff)
            self.orch.record_artifact('03-dev/implementation.diff', target)
            with patch('orchestrator.EngineRunner.run_sandboxed_cmd') as scanner, \
                 patch('orchestrator.EngineRunner.invoke_role', side_effect=lambda *a, **k: (framed('appsec', self.orch), 0, 0, 0, 'fake')):
                self.assertFalse(self.orch.run_phase_appsec(attempt=attempt)[0])
            scanner.assert_not_called()

    def test_scanner_mutation_is_terminal_without_model_call(self):
        def scan(*a):
            (self.orch.worktree_dir / 'app.py').write_text('scanner changed source')
            return True, 'log'
        with patch.object(self.orch, '_run_deterministic_scanners', side_effect=scan), \
             patch('orchestrator.EngineRunner.invoke_role') as engine:
            self.assertFalse(self.orch.run_phase_qa()[0])
        engine.assert_not_called()
        self.assertEqual(self.orch.state['ledger'][-1]['status'], 'INPUT_CHANGED')
        with self.assertRaises(gates.GateTerminalError):
            self.orch._stop_on_terminal_gate()

    def test_absent_manifests_not_applicable(self):
        self.assertTrue(all(r.status == 'not_applicable' and not r.required
                            for r in self.orch._run_appsec_scans(self.orch.worktree_dir)))

    def test_legacy_gates_need_no_frame_and_hook_is_noop(self):
        self.orch.state['task_breakdown'] = False
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'false'}), \
             patch.object(self.orch, '_run_deterministic_scanners', return_value=(True, 'passed')), \
             patch('orchestrator.EngineRunner.invoke_role', return_value=('QA_STATUS: PASSED', 0, 0, 0, 'fake')):
            self.assertTrue(self.orch.run_phase_qa()[0])
            self.orch._stop_on_terminal_gate()
        with patch('orchestrator.EngineRunner.invoke_role', return_value=('APPSEC_STATUS: SECURE', 0, 0, 0, 'fake')):
            self.assertTrue(self.orch.run_phase_appsec()[0])
        with patch('orchestrator.EngineRunner.invoke_role', return_value=('REVIEW_STATUS: APPROVED', 0, 0, 0, 'fake')):
            self.assertTrue(self.orch.run_phase_review()[0])
        self.assertFalse(list(self.orch.ssdlc_root.glob('*/evaluations/*/input.json')))


class VerificationPipelineTests(GateFixture):
    pipeline = _pipeline

    def run_terminal(self, gate, outer):
        if gate == 'appsec':
            self.orch.state['risk_tier'] = 'L'
            self.orch._save_state()
        captured = []
        def invoke(*args, **kwargs):
            phase = self.orch.state['current_phase']
            current = next(g for g, p in GATE_PHASE.items() if p == phase)
            if current == gate:
                captured[:] = [phase, self.orch.state.get('current_step')]
                return 'No frame', 0, 0, 0, 'fake'
            return framed(current, self.orch), 0, 0, 0, 'fake'
        with patch.object(self.orch, '_run_deterministic_scanners', return_value=(True, 'passed')), \
             patch.object(self.orch, '_run_appsec_scans', return_value=[]), self.assertRaises(gates.GateTerminalError):
            self.pipeline(invoke, outer=outer)
        self.developer.assert_called_once_with(feedback='', attempt=1)
        self.assertEqual(self.engine.call_count, 3 if gate == 'qa' else 4)
        if outer:
            assert_terminal_failure(self, self.orch, gate, captured)

    def test_qa_layer_a(self):
        self.run_terminal('qa', False)

    def test_qa_layer_b(self):
        self.run_terminal('qa', True)

    def test_appsec_layer_a(self):
        self.run_terminal('appsec', False)

    def test_appsec_layer_b(self):
        self.run_terminal('appsec', True)
