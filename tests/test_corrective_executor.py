"""Corrective execution through real pipeline, scheduler and durable journals."""
import copy
import json
import os
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import orchestrator
import ssdlc_corrective as corrective
from ssdlc_tasks import TaskError, TaskJournal, atomic_json, digest, file_hash, git, ticket_lock
import test_gate_reports as gate_fixtures
from test_gate_reports import finding
from test_full_checkpoint_review import framed


class Crash(BaseException):
    pass


class ExecutorFixture(gate_fixtures.GateFixture):
    def setUp(self):
        super().setUp()
        self.orch._ensure_controller_contract()
        self.directory = self.orch.dir_dev / 'tasks'
        self.journal = TaskJournal(self.directory)
        proof = self.directory / 'initial.log'
        proof.write_text('Ran 1 test\nOK\n')
        self.check = dict(check_id='CHECK-T1', log_ref='initial.log', sha256=file_hash(proof),
                          exit_code=0, empty_suite=False)
        self.checkpoint = dict(inputs_sha256=digest(dict(plan=self.plan, quality=self.quality,
            context=orchestrator.build_task_context(self.orch.ssdlc_root))),
            expected_head=self.orch.state['task_verified_head'], ignored_paths=[],
            tasks={'T1': dict(status='COMPLETED', attempts=1, checks=[self.check],
                              commit=self.orch.state['task_verified_head'])})
        self.journal.append('TASK_COMPLETED', self.checkpoint)
        self.dispatches = []
        self.prompts = []
        self.gates = []
        self.reject = 'review'
        self.reject_rounds = {1}
        self.developer_status = 'COMPLETE'
        self.edit_path = 'app.py'
        self.check_failure = False
        self.environment = patch.dict(os.environ, {'SSDLC_EXECUTION_MODE': 'local',
            'SSDLC_MAX_DEV_RETRIES': '4', 'SSDLC_BUDGET_CAP_USD': '0', 'T12_SECRET_TOKEN': 'secret-never-in-prompts'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def enable_native(self):
        self.checkpoint['candidate_isolation'] = 1
        with self.fakes():
            contract = self.orch._ensure_controller_contract()
            self.checkpoint['candidate_runtime'] = dict(controller_sha256=digest(contract),
                environment=self.orch._verification_environment(self.quality['checks'][0]['argv']))
        self.journal.append('TASK_COMPLETED', self.checkpoint)

    def provider(self, engine, model, system, user, cwd, *args, **kwargs):
        phase = self.orch.state['current_phase']
        gate = {'04-review': 'review', '05-qa': 'qa', '05b-appsec': 'appsec'}[phase]
        directory = {'review': self.orch.dir_review, 'qa': self.orch.dir_qa, 'appsec': self.orch.dir_appsec}[gate]
        inputs = list(directory.glob('evaluations/*/input.json'))
        metadata = json.loads(max(inputs, key=lambda p: p.stat().st_mtime_ns).read_text())
        attempt = metadata['attempt']
        self.gates.append((gate, attempt, self.orch.state['task_verified_head']))
        fs = [finding(scenario='secret-never-in-prompts')] if gate == self.reject and attempt in self.reject_rounds else []
        return framed(gate, self.orch, fs), 0, 0, 0, 'fake'

    def develop(self, prompt, cwd, **kwargs):
        self.dispatches.append(str(cwd))
        self.prompts.append(prompt)
        target = Path(cwd) / self.edit_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('value = ' + str(len(self.dispatches) + 1) + '\n')
        return 'TASK_DEV_STATUS: ' + self.developer_status, 0

    def verify_cmd(self, argv, cwd, timeout, **kwargs):
        if argv == ['id', '-u']:
            return '1000', '', 0
        failed = self.check_failure and self.dispatches and len(self.dispatches) == 1
        return 'Ran 1 test\n' + ('FAILED' if failed else 'OK'), '', int(failed)

    def fakes(self):
        stack = ExitStack()
        stack.enter_context(patch('orchestrator.EngineRunner._should_spawn_role_container', return_value=True))
        stack.enter_context(patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('codex', 'fake', 'low', 'test')))
        stack.enter_context(patch('orchestrator.EngineRunner.run_codex', side_effect=self.develop))
        stack.enter_context(patch('orchestrator.EngineRunner.run_sandboxed_cmd', side_effect=self.verify_cmd))
        stack.enter_context(patch('orchestrator.EngineRunner.invoke_role', side_effect=self.provider))
        stack.enter_context(patch.object(self.orch, '_run_deterministic_scanners', return_value=(True, 'checks passed')))
        stack.enter_context(patch.object(self.orch, '_run_appsec_scans', return_value=[]))
        stack.enter_context(patch.object(self.orch, 'run_phase_ops'))
        stack.enter_context(patch.object(self.orch, 'run_phase_docs'))
        return stack

    def preview(self):
        self.run_review(lambda *a, **k: (framed('review', self.orch, [finding()]), 0, 0, 0, 'fake'))
        return self.orch.preview_bounce('04-review', 1)

    def execute(self, preview):
        with ticket_lock(self.orch.ticket_lock_path):
            return self.orch._execute_corrective('04-review', 1, preview)

    def resume(self):
        with ticket_lock(self.orch.ticket_lock_path):
            return self.orch._reconcile_open_txn()

    def txn(self):
        directory = next((self.orch.ssdlc_root / 'bounce-txn').iterdir())
        return corrective.TransactionJournal(directory)

    def events(self):
        return [json.loads(x) for x in self.journal.path.read_text().splitlines()]

    def invalidations(self):
        return sum(e['event'] == 'TASKS_INVALIDATED' for e in self.events())

    def add_dependent(self, tid='T2', deps=None, root=False):
        task = self.task(tid, [], [], [tid.lower() + '.py'], deps or ['T1'])
        self.plan['tasks'].append(task)
        atomic_json(self.orch.dir_design / 'plan.json', self.plan)
        self.orch.record_artifact('02-design/plan.json', self.orch.dir_design / 'plan.json')
        self.checkpoint['tasks'][tid] = dict(status='COMPLETED', attempts=1, checks=[self.check],
                                             commit=self.checkpoint['expected_head'])
        self.checkpoint['inputs_sha256'] = digest(dict(plan=self.plan, quality=self.quality,
            context=orchestrator.build_task_context(self.orch.ssdlc_root)))
        self.journal.append('TASK_COMPLETED', self.checkpoint)


class ExecutorTests(ExecutorFixture):
    def pipeline(self):
        with self.fakes(), patch.object(self.orch, 'run_phase_dev') as dev:
            result = self.orch.run_pipeline('fix', from_phase='03-dev')
        dev.assert_called_once_with()
        return result

    def assert_pipeline(self, gate):
        self.reject = gate
        if gate == 'appsec':
            self.orch.state['risk_tier'] = 'L'
            self.orch._save_state()
        before = self.orch.state['task_verified_head']
        self.pipeline()
        after = self.orch.state['task_verified_head']
        self.assertNotEqual(before, after)
        self.assertEqual(self.invalidations(), 1)
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual([g for g, a, h in self.gates if a == 2], ['review', 'qa', 'appsec'])
        self.assertTrue(all(h == after for g, a, h in self.gates if a == 2))
        self.assertEqual(self.txn().peek()['status'], 'APPROVED')
        self.assertEqual(self.orch.state['status'], 'AWAITING_SUPERVISOR_SIGNOFF')
        for phase in ('04-review', '05-qa', '05b-appsec'):
            roster = json.loads((self.orch.ssdlc_root / phase / 'provenance/attempt-2/roster.json').read_text())
            self.assertEqual(roster['reviewed_commit'], after)

    def test_review_rejection(self): self.assert_pipeline('review')
    def test_qa_rejection(self): self.assert_pipeline('qa')
    def test_appsec_rejection(self): self.assert_pipeline('appsec')

    def test_tier_s_always_reruns_appsec_after_repair(self):
        self.orch.state['risk_tier'] = 'S'
        self.orch._save_state()
        self.assert_pipeline('review')

    def test_dependent_revalidated_without_provider_dispatch(self):
        self.add_dependent()
        self.assert_pipeline('review')
        state = self.journal.peek()
        self.assertEqual(state['tasks']['T2']['attempts'], 1)
        self.assertEqual(state['tasks']['T2']['revalidations'][-1]['status'], 'PASSED')
        self.assertEqual(state['revalidation_runs'][-1]['status'], 'COMPLETE')

    def test_repeated_rejection_reenters_corrective_with_preserved_attempts(self):
        self.reject_rounds = {1, 2}
        self.pipeline()
        self.assertEqual(self.invalidations(), 2)
        self.assertEqual(len(self.dispatches), 2)
        self.assertEqual(self.journal.peek()['tasks']['T1']['attempts'], 3)
        self.assertEqual([g for g, a, h in self.gates if a == 3], ['review', 'qa', 'appsec'])

    def test_completed_and_approved_replay_are_zero_write(self):
        preview = self.preview()
        with self.fakes():
            outcome = self.execute(preview)
        self.assertEqual(outcome['status'], 'COMPLETED')
        for approved in (False, True):
            if approved:
                with self.fakes(), ticket_lock(self.orch.ticket_lock_path):
                    self.orch._approve_corrective_candidate(2)
            journal = self.txn()
            paths = [journal.path, journal.snapshot, self.journal.path, self.journal.snapshot,
                     self.orch.state_file]
            before = [p.read_bytes() for p in paths]
            with patch.object(self.orch, '_make_task_scheduler', side_effect=AssertionError('dispatch')):
                result = self.execute(preview)
            self.assertTrue(result['replayed'])
            self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_attempt_exhaustion_precedes_invalidation(self):
        preview = self.preview()
        with patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '1'}), self.fakes():
            with self.assertRaises(corrective.CorrectiveError) as error:
                self.execute(preview)
        self.assertEqual(error.exception.code, 'attempts_exhausted')
        self.assertEqual(self.invalidations(), 0)
        self.assertEqual(self.dispatches, [])

    def test_assembled_prompt_is_bounded_and_scrubbed(self):
        preview = self.preview()
        with self.fakes(): self.execute(preview)
        prompt = self.prompts[0]
        self.assertNotIn('secret-never-in-prompts', prompt)
        context = prompt.split('\nREPAIR CONTEXT:\n')[1]
        self.assertLessEqual(len(context), 12000)
        self.assertIn('reviewed_commit', context)
        self.assertNotIn('scenario', context)
        self.assertNotIn('developer_output', context)

    def test_scope_controller_gate_and_contract_changes_fail_closed(self):
        preview = self.preview()
        # A single transaction cannot be retried after terminal failure.
        self.edit_path = 'outside.py'
        with self.fakes(), self.assertRaises(corrective.CorrectiveError) as error:
            self.execute(preview)
        self.assertEqual(error.exception.code, 'scope_changed')
        self.assertFalse((self.orch.worktree_dir / 'outside.py').exists())
        self.assertEqual(self.txn().peek()['status'], 'FAILED')

    def test_approval_refuses_open_failed_and_interrupted_transactions(self):
        preview = self.preview()
        with ticket_lock(self.orch.ticket_lock_path):
            self.orch._create_corrective('04-review', 1, preview)
        for status in ('INVALIDATED', 'INTERRUPTED', 'FAILED'):
            journal = self.txn(); journal.load()
            journal.append(status, dict(journal.peek(), status=status))
            self.orch.state['status'] = 'AWAITING_SUPERVISOR_SIGNOFF'
            with self.assertRaises(corrective.CorrectiveError):
                self.orch._approve_and_merge()

    def test_every_append_is_verified_from_each_acknowledgment_anchor(self):
        self.add_dependent()
        preview = self.preview()
        seen = []
        def fault(point):
            # The driver calls this only after layer E, lineage, substate and
            # inventory verification have all succeeded for the durable append.
            if point.startswith('task.after_'):
                seen.append(point.removeprefix('task.after_').upper())
        start = len(self.events())
        with self.fakes(), patch('orchestrator._corrective_fault', side_effect=fault):
            self.execute(preview)
        expected = [e['event'] for e in self.events()[start:] if e['event'] != 'TASKS_INVALIDATED']
        self.assertEqual(seen, expected)
        self.assertIn('REVALIDATION_TASK_PASSED', seen)
        self.assertIn('PREFLIGHT_PASSED', seen)


def _protected_case(path):
    def case(self):
        preview = self.preview()
        self.edit_path = path
        with self.fakes(), self.assertRaises(corrective.CorrectiveError) as error:
            self.execute(preview)
        self.assertEqual(error.exception.code, 'protected_path_modified')
        self.assertFalse((self.orch.worktree_dir / path).exists())
        self.assertEqual(self.txn().peek()['status'], 'FAILED')
    return case

for _path in ('ssdlc_controller.py', 'scripts/gate-check.py', 'quality-contract.json'):
    setattr(ExecutorTests, 'test_protected_' + _path.replace('/', '_').replace('.', '_'), _protected_case(_path))


def _candidate_proof_case(self):
    self.quality['checks'][0]['candidate_proof'] = True
    atomic_json(self.orch.dir_recon / 'quality-contract.json', self.quality)
    self.orch.record_artifact('00-recon/quality-contract.json', self.orch.dir_recon / 'quality-contract.json')
    self.checkpoint['inputs_sha256'] = digest(dict(plan=self.plan, quality=self.quality,
        context=orchestrator.build_task_context(self.orch.ssdlc_root)))
    self.journal.append('TASK_COMPLETED', self.checkpoint)
    with self.fakes():
        self.orch.run_phase_review(attempt=1)
        preview = self.orch.preview_bounce('04-review', 1)
        self.execute(preview)
    context = json.loads(self.prompts[0].split('\nREPAIR CONTEXT:\n')[1])
    self.assertTrue(context['candidate_proof'])
    self.assertTrue(context['findings'][0]['evidence_refs'])
    self.assertNotIn('log_excerpt', json.dumps(context))


ExecutorTests.test_real_flow_retains_registered_candidate_proof = _candidate_proof_case


def _native_pipeline_case(gate):
    def case(self):
        self.enable_native()
        self.assert_pipeline(gate)
        kinds = [e['event'] for e in self.events()]
        self.assertIn('DISPATCH_RESERVED', kinds)
        self.assertIn('CANDIDATE_CAPTURED', kinds)
    return case


for _gate in ('review', 'qa', 'appsec'):
    setattr(ExecutorTests, 'test_native_pipeline_' + _gate, _native_pipeline_case(_gate))


def _controls_case(self):
    import ssdlc_bounce as bounce
    for gate, base in (('review', {}), ('qa', {'det_ok': True}), ('appsec', {'scanners': True})):
        scanner = {'scanners': [{'status': 'passed'}]} if gate == 'appsec' else None
        for status in ('passed', 'failed', 'unmet'):
            controls = dict(base, candidate_checks=status)
            self.assertEqual(bounce.validate_controls(gate, controls, scanner), controls)
        self.assertEqual(bounce.validate_controls(gate, base, scanner), base)
        for bad in (True, False, None, 'not_applicable', 'unknown'):
            with self.assertRaises(bounce.BounceError):
                bounce.validate_controls(gate, dict(base, candidate_checks=bad), scanner)
        with self.assertRaises(bounce.BounceError):
            bounce.validate_controls(gate, dict(base, unknown=True), scanner)
        for name in base:
            with self.assertRaises(bounce.BounceError):
                bounce.validate_controls(gate, dict(base, **{name: 'passed'}), scanner)
    self.preview()
    evidence = bounce.read_evidence(root=self.orch.ssdlc_root, state=self.orch.state,
        repo=self.repo, worktree=self.orch.worktree_dir, phase='04-review', attempt=1)
    self.assertEqual(evidence['status'], 'verified')
    for status in ('passed', 'failed', 'unmet'):
        evaluations = copy.deepcopy(evidence['evaluations'])
        evaluations[0]['controls']['candidate_checks'] = status
        proposal = bounce.plan_bounce(repo=self.repo, plan=self.plan, quality=self.quality,
            requirements_text=evidence['requirements_text'], reviewed_commit=evidence['reviewed_commit'],
            evaluations=evaluations, phase='04-review', attempt=1, artifacts=self.orch.ssdlc_root)
        codes = {b['code'] for b in proposal['blockers']}
        self.assertEqual('control_failed' in codes, status != 'passed')
        self.assertNotIn('malformed_record', codes)


ExecutorTests.test_native_controls_are_strict_and_semantic = _controls_case
