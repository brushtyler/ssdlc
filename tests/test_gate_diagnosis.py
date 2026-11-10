"""Offline lifecycle, crash recovery and serialized diagnosis dispatch."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import ssdlc_gate_reports as cr
from ssdlc_tasks import TaskError


class Crash(BaseException):
    pass


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = {}
        self.entries = []
        self.gates = []
        self.invoke = Mock(return_value=('APPROVED ' * 1200, 0.0, 1, 2, ''))
        self.provider = dict(engine='engine', model='first', reasoning_effort='medium')

    def register(self, ref, path):
        digest = cr._digest(path.read_bytes())
        if ref in self.artifacts:
            self.assertEqual(digest, self.artifacts[ref]['sha256'])
        self.artifacts[ref] = dict(sha256=digest)

    def run_diag(self, finding='F1', identity='A', **changes):
        args = dict(gate='review', finding=dict(id=finding, task_attribution=['T1'], scenario='untrusted'),
                    reviewed_commit='a'*40, tree='b'*40, evaluation_identity=identity, attempt=1,
                    candidate_records=['record-A'], provider=self.provider, invoke=self.invoke,
                    register=self.register, artifacts=self.artifacts, ledger=self.entries.append,
                    record_gate=lambda *args: self.gates.append(args))
        args.update(changes)
        return cr.run_diagnosis(self.root, **args)

    def files(self, pattern):
        return sorted((self.root / 'diagnosis').glob(pattern))

    def test_load_provenance_bounds_and_immutable_separate_records(self):
        scheduler = self.root / '03-dev/tasks'
        scheduler.mkdir(parents=True)
        for name in ('tasks-state.json', 'task-journal.jsonl', 'tasks_status'):
            (scheduler / name).write_bytes(b'unchanged')
        before = {p: p.read_bytes() for p in scheduler.iterdir()}
        result = self.run_diag()
        self.assertEqual(result['trust'], 'untrusted_data')
        self.assertEqual(len(result['text']), 8000)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['text_sha256'], cr._digest(result['text'].encode()))
        self.assertEqual(result['producer']['model'], 'first')
        self.assertEqual(result['finding_id'], 'F1')
        self.assertEqual(result['source']['commit'], 'a'*40)
        self.assertEqual(before, {p: p.read_bytes() for p in scheduler.iterdir()})
        for p in self.files('**/*.json'):
            self.assertEqual(p.read_bytes(), cr.canon(json.loads(p.read_bytes())))
            self.assertEqual(self.artifacts[p.relative_to(self.root).as_posix()]['sha256'], cr._digest(p.read_bytes()))
        path = self.files('requests/*/request.json')[0]
        original = path.read_bytes()
        with self.assertRaises(TaskError):
            cr.write_or_verify(path, b'{}', root=self.root)
        self.assertEqual(original, path.read_bytes())
        self.assertTrue(all('identity' not in e for e in self.entries))
        # Advisory prose cannot alter the host's strict decision predicate.
        assessment = dict(source_gate='code_review', correction_roots=[], impact_assessment_required=[],
                          replanning_required=[], diagnosis_required=['F1'], findings=[])
        approved, reasons = cr.decide('APPROVED', assessment, {'candidate_checks': 'passed'}, True)
        self.assertFalse(approved)
        self.assertIn('assessment', reasons)

    def test_different_evaluations_reuse_frozen_provider(self):
        first = self.run_diag()
        path = self.files('requests/*/request.json')[0]
        original = path.read_bytes()
        second = self.run_diag(identity='B', candidate_records=['record-B'],
                               provider=dict(engine='engine', model='second', reasoning_effort='high'))
        self.assertEqual(first, second)
        self.assertEqual(original, path.read_bytes())
        associations = [json.loads(p.read_bytes()) for p in self.files('requests/*/associations/*.json')]
        self.assertEqual(len(associations), 2)
        self.assertEqual(sum(a['provider_differs'] for a in associations), 1)
        self.assertEqual(len(self.files('reservations/*')), 1)
        self.assertEqual(len(self.files('requests/*/consumption.json')), 1)
        self.invoke.assert_called_once()

    def pair(self, call_a, call_b):
        outcomes = []
        def run(call):
            try:
                outcomes.append(call())
            except BaseException as exc:
                outcomes.append(exc)
        threads = [threading.Thread(target=run, args=(call,)) for call in (call_a, call_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        return outcomes

    def test_event_serialized_same_and_different_requests(self):
        entered, release, starting = threading.Event(), threading.Event(), threading.Event()
        outcomes = []
        def fault(point):
            if point == cr.DIAGNOSIS_POINTS[2]:
                entered.set()
                if not release.wait(10):
                    raise AssertionError('release timeout')
        def first():
            outcomes.append(self.run_diag(fault=fault))
        def second():
            starting.set()
            outcomes.append(self.run_diag(identity='B'))
        a = threading.Thread(target=first)
        a.start()
        self.assertTrue(entered.wait(10))
        b = threading.Thread(target=second)
        b.start()
        self.assertTrue(starting.wait(10))
        self.assertEqual(self.invoke.call_count, 0)
        release.set()
        a.join(15)
        b.join(15)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(self.invoke.call_count, 1)
        self.run_diag('F2')
        self.assertEqual(json.loads(self.files('reservations/slot-2.json')[0].read_bytes())['slot'], 2)
        with self.assertRaises(cr.GateTerminalError) as caught:
            self.run_diag('F3')
        self.assertEqual(caught.exception.outcome_class, 'diagnosis_exhausted')

    def test_different_request_waits_for_dispatch_then_gets_second_slot(self):
        entered, release, started = threading.Event(), threading.Event(), threading.Event()
        outcomes = []
        def invoke(system, user):
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise AssertionError('release timeout')
            return 'analysis', 0.0, 0, 0, ''
        self.invoke.side_effect = invoke
        def first():
            outcomes.append(self.run_diag())
        def second():
            started.set()
            outcomes.append(self.run_diag('F2'))
        a = threading.Thread(target=first)
        a.start()
        self.assertTrue(entered.wait(10))
        b = threading.Thread(target=second)
        b.start()
        self.assertTrue(started.wait(10))
        self.assertEqual(self.invoke.call_count, 1)
        self.assertEqual(len(self.files('reservations/*')), 1)
        release.set()
        a.join(15)
        b.join(15)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(self.invoke.call_count, 2)
        self.assertEqual([json.loads(p.read_bytes())['slot'] for p in self.files('reservations/*')], [1, 2])

    def test_unchanged_candidate_records_and_scheduler_state(self):
        from test_candidate_evidence import CandidateTests
        fixture = CandidateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.orch.dir_recon = fixture.store / '00-recon'
        fixture.orch.dir_recon.mkdir()
        contract = fixture.orch.dir_recon / 'quality-contract.json'
        contract.write_bytes(cr.canon(fixture.quality))
        fixture.artifacts['00-recon/quality-contract.json']['sha256'] = cr._digest(contract.read_bytes())
        candidate = fixture.collect()
        self.root = fixture.store
        self.artifacts = fixture.artifacts
        original = {p: p.read_bytes() for p in (self.root / 'candidate-evidence').rglob('*') if p.is_file()}
        status = dict(tasks=['T1'], attempts=1)
        fixture.orch.state['tasks_status'] = status
        status_bytes = cr.canon(status)
        task_root = self.root / '03-dev/tasks'
        task_root.mkdir(parents=True)
        for name in ('tasks-state.json', 'task-journal.jsonl'):
            (task_root / name).write_bytes(b'prior scheduler state')
        self.run_diag(candidate_records=[r['record_id'] for r in candidate.records])
        fixture.orch._verify_candidate_evidence(candidate, fixture.wt)
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        self.assertEqual(status_bytes, cr.canon(fixture.orch.state['tasks_status']))
        for p in task_root.iterdir():
            self.assertEqual(p.read_bytes(), b'prior scheduler state')

    def test_orchestrator_hook_binds_evaluator_and_sorted_read_only_dispatch(self):
        from types import SimpleNamespace
        from orchestrator import SSDLCOrchestrator, EngineRunner, request_identity
        from ssdlc_findings import TrustedContext
        orch = SSDLCOrchestrator.__new__(SSDLCOrchestrator)
        orch.ssdlc_root = self.root
        orch.state = dict(artifacts=self.artifacts)
        orch._register_new = self.register
        orch.project_id, orch.ticket_id = 'project', 'ticket'
        orch.check_budget_circuit_breaker = Mock()
        orch.record_gate = Mock()
        orch.record_ledger = Mock()
        request = cr.EvaluationRequest('review', 'engine', 'model', 1, 1, 'a'*40, 'system', 'user',
            TrustedContext('a'*40, ['T1'], [], []), [], 'fingerprint', None, 'high')
        findings = [SimpleNamespace(id=name, to_dict=lambda name=name: dict(id=name, task_attribution=['T1']))
                    for name in ('Z', 'A')]
        result = cr.EvaluationResult('rejected_substantive', False,
            assessment=SimpleNamespace(findings=findings, diagnosis_required=['Z', 'A']))
        candidate = cr.CandidateEvidence(quality=dict(checks=[dict(required=True, candidate_proof=True)]), block='catalogue')
        with patch('orchestrator.git', return_value='tree'), \
             patch.object(EngineRunner, 'invoke_role', return_value=('advisory', 0.0, 0, 0, '')) as invoke:
            orch._diagnose_gate([request], [result], candidate, self.root, '04-review')
        self.assertEqual(invoke.call_count, 2)
        for call, finding_id in zip(invoke.call_args_list, ('A', 'Z')):
            self.assertEqual(json.loads(call.args[3].split('\n')[0])['untrusted_data']['id'], finding_id)
            self.assertEqual(call.args[7], '04-review-diag')
            self.assertEqual(call.kwargs, dict(sandbox='read-only', reasoning_effort='high'))
        for p in self.files('requests/*/associations/*.json'):
            self.assertEqual(json.loads(p.read_bytes())['source_evaluation_identity'], request_identity(request)['identity'])
        self.assertEqual(result.assessment.diagnosis_required, ['Z', 'A'])

    def test_persisted_allowance_governs(self):
        cr.write_exclusive(self.root / 'diagnosis/allowance.json',
            cr.canon(dict(schema_version='1.0', kind='allowance', allowance=1)), root=self.root)
        self.run_diag()
        with self.assertRaises(cr.GateTerminalError) as caught:
            self.run_diag('F2')
        self.assertEqual(caught.exception.outcome_class, 'diagnosis_exhausted')
        self.assertIn('Allowance 1', str(caught.exception))

    def test_all_crash_boundaries_concurrent_restart(self):
        for index, point in enumerate(cr.DIAGNOSIS_POINTS):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                self.artifacts = {}
                self.invoke.reset_mock()
                def fault(actual):
                    if actual == point:
                        raise Crash()
                with self.assertRaises(Crash):
                    self.run_diag(fault=fault)
                if index == 5:
                    result_path = self.files('requests/*/result.json')[0]
                    self.assertNotIn(result_path.relative_to(self.root).as_posix(), self.artifacts)
                cr.reconcile_candidate_store(self.root, self.artifacts, self.register)
                outcomes = self.pair(lambda: self.run_diag(), lambda: self.run_diag(identity='B'))
                if index in (3, 4):
                    self.assertTrue(all(isinstance(o, cr.GateTerminalError) and o.outcome_class == 'diagnosis_uncertain' for o in outcomes))
                    self.assertFalse(self.files('requests/*/result.json'))
                else:
                    self.assertTrue(all(isinstance(o, dict) for o in outcomes), outcomes)
                self.assertEqual(len(self.files('reservations/*')), 1)
                self.assertEqual(len(self.files('requests/*/consumption.json')), 1)
                self.assertLessEqual(self.invoke.call_count, 1)

    def test_dispatch_exception_no_message_or_retry(self):
        self.invoke.side_effect = ValueError('secret provider error')
        for identity in ('A', 'B'):
            with self.assertRaises(cr.GateTerminalError) as caught:
                self.run_diag(identity=identity)
            self.assertEqual(caught.exception.outcome_class, 'diagnosis_uncertain')
        self.invoke.assert_called_once()
        obj = json.loads(self.files('requests/*/uncertain.json')[0].read_bytes())
        self.assertEqual(obj['exc_type'], 'ValueError')
        self.assertNotIn('secret', json.dumps(obj))
        self.assertFalse(self.files('requests/*/result.json'))

    def test_exhaustion_snapshot_changes_without_reset(self):
        # Reserve slot 1 without consuming it; consume slot 2.
        def fault(point):
            if point == cr.DIAGNOSIS_POINTS[2]:
                raise Crash()
        with self.assertRaises(Crash):
            self.run_diag(fault=fault)
        self.run_diag('F2')
        for expected in (1, 2):
            with self.assertRaises(cr.GateTerminalError) as caught:
                self.run_diag('F3')
            message = str(caught.exception)
            for fragment in ('Allowance 2', 'reserved 2', f'consumed {expected}', 'uncertain 0', 'consumption.json', 'Operator', 'reject', 'new ticket'):
                self.assertIn(fragment, message)
            if expected == 1:
                self.run_diag()
        self.assertEqual(len(self.files('exhaustion-*.json')), 2)
        self.assertEqual(len(self.gates), 2)
        self.assertEqual(len(self.files('reservations/*')), 2)

    def test_tamper_and_unregistered_request_identity_rejected(self):
        self.run_diag()
        path = self.files('requests/*/request.json')[0]
        body = json.loads(path.read_bytes())
        body['tree'] = 'changed'
        path.write_bytes(cr.canon(body))
        for registered in (True, False):
            if not registered:
                self.artifacts.clear()
            with self.assertRaises(cr.GateTerminalError) as caught:
                cr.reconcile_candidate_store(self.root, self.artifacts, self.register)
            self.assertIn('diagnosis_state_inconsistent', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
