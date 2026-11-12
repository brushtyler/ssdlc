"""Offline native in-scope return: real captures/accounting; simulated provider/host observations."""
import contextlib
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import orchestrator
import ssdlc_feedback_delegation as delegation
import ssdlc_feedback_return as repair
import ssdlc_feedback_workflow as flow
import test_reconciliation as fixtures
from test_feedback_delegation import grant
from test_feedback_workflow import config
from ssdlc_feedback import FeedbackStore, FeedbackError
from ssdlc_scope_amendments import inspect, prepare, activate
from ssdlc_recovery import LimitsStore
from ssdlc_tasks import TaskScheduler, TaskJournal, TaskError, atomic_json, digest
from ssdlc_transitions import runtime_root
from ssdlc_controller import execution_contract


class RepairReturnTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixtures.ReconciliationTests()
        f.setUp(); self.addCleanup(f.doCleanups)
        profile = f.request['profile']
        profile['development_attempts'] = 3
        profile['role_invocations'].update({'03-dev': 6, 'feedback-planner': 2, 'feedback-reviewer': 2, 'feedback-po': 1})
        profile['phase_invocations'].update({'03-dev': 6, 'feedback': 5})
        profile['feedback_delegation'] = grant()
        profile['feedback_workflow'] = config()
        f.identity['environment'] = dict(mode='role_container', image_id='offline')
        staged = f.stage(); f.select(staged)
        self.ticket, self.repo = f.target, f.repo
        self.root = runtime_root(self.ticket)
        self.dev_calls, self.role_calls = [], []
        self.route = 'IN_SCOPE_REPAIR'
        self.review_verdict = 'APPROVED'
        self.repair_files = ['next.py']
        def developer(task, attempt, prompt, workspace):
            self.dev_calls.append(attempt)
            store = LimitsStore(self.root)
            invocation = store.reserve('03-dev', '03-dev', 'codex'); store.finish(invocation, 'returned')
            (workspace / 'next.py').write_text('value = 2\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0, quiescent=True,
                        engine='codex', model='developer')
        with self.assertRaisesRegex(TaskError, 'scope hypothesis'):
            self.scheduler(self.root, developer).run(f.new_plan['tasks'])
        outer = json.loads((self.root / 'state.json').read_text())
        outer['status'] = 'ESCALATED_TO_SUPERVISOR'; atomic_json(self.root / 'state.json', outer)
        self.source = inspect(self.ticket, allow_host_hints=True)
        self.identity = self.source['journal']['candidate_runtime']
        self.snapshot = self.source['envelope']['binding']['controller']

    def verify(self, argv, cwd, timeout, workspace):
        if (workspace / 'next.py').exists() and (workspace / 'next.py').read_text() == 'value = 2\n':
            return ('ERROR: test_compat (app.Compatibility.test_compat)\n'
                    '----------------------------------------------------------------------\n'
                    'Traceback (most recent call last):\n'
                    '  File "app.py", line 1, in test_compat\n'
                    'AttributeError: missing helper\n\nRan 1 test in 0.1s\n\nFAILED (errors=1)\n', '', 1)
        return 'Ran 1 test in 0.1s\nOK\n', '', 0

    def scheduler(self, root, developer):
        envelope, _ = LimitsStore(root)._load(repair=False)
        state = TaskJournal(root / '03-dev/tasks').peek()
        return TaskScheduler(root / 'worktree', root / '03-dev/tasks',
            json.loads((root / '02-design/plan.json').read_text()),
            json.loads((root / '00-recon/quality-contract.json').read_text()), self.fixture.context,
            developer, self.verify, max_attempts=envelope['policy']['development_attempts'],
            isolate_candidates=True, recover_timeouts=True, feedback_enabled=True,
            runtime_identity=state['candidate_runtime'],
            historical_capture_authority=envelope['binding']['historical_capture_bindings_sha256'],
            historical_task_floors=envelope['binding'].get('task_floors'),
            scope_reconciliations=envelope['binding'].get('scope_reconciliations'))

    @contextlib.contextmanager
    def host(self):
        with (patch.object(delegation, 'assert_controller'),
              patch.object(orchestrator, 'assert_controller'),
              patch.object(delegation, 'observe_environment', return_value=self.identity['environment']),
              patch.object(delegation, 'docker_quiescent', return_value=True)):
            yield

    def runner(self, source, limits, prompt, effort):
        case = json.loads(prompt.split('UNTRUSTED_CASE_JSON:\n')[1])['case']
        def call(actor, invocation):
            self.role_calls.append(actor['identity'])
            if actor['identity'] == 'feedback-planner':
                response = dict(route=self.route, requested_files=[], reason_code='fix_owned_code')
                if self.route == 'IN_SCOPE_REPAIR': response['repair_files'] = self.repair_files
            else:
                response = dict(proposal_sha256=case['proposal_sha256'], verdict=self.review_verdict,
                                coverage_preserved=True, reason_code='preserve_existing_tests')
            return dict(response=response, usage=dict(metered_usd=None, cli_estimate_usd=0.01))
        return call

    def run_flow(self):
        with self.host():
            return flow.run_feedback(self.ticket, self.repo, Path(__file__).resolve().parents[1],
                                     self.snapshot, self.identity, runner_factory=self.runner)

    def test_real_capture_return_preserves_plan_scope_history_and_next_attempt(self):
        old_plan = copy.deepcopy(self.source['plan'])
        old_counters = copy.deepcopy(self.source['counters'])
        old_failure = copy.deepcopy(self.source['journal']['tasks']['T2']['failed_attempts'])
        original_app = (self.source['root'] / 'worktree/app.py').read_bytes()
        result = self.run_flow(); self.assertEqual(result['disposition'], 'APPLIED')
        root = runtime_root(self.ticket)
        self.assertEqual(json.loads((root / '02-design/plan.json').read_text()), old_plan)
        tasks = TaskJournal(root / '03-dev/tasks').peek()['tasks']
        self.assertEqual(tasks['T2']['attempts'], 1)
        self.assertEqual(tasks['T2']['failed_attempts'], old_failure)
        _, counters = LimitsStore(root)._load(repair=False)
        self.assertEqual(counters['deadline_at'], old_counters['deadline_at'])
        self.assertEqual(len(counters['invocations']), len(old_counters['invocations']) + 2)
        self.assertTrue(all(counters['invocations'][k] == v for k, v in old_counters['invocations'].items()))
        feedback = FeedbackStore(self.ticket).inspect()
        self.assertEqual(feedback['amendments'], [])
        self.assertEqual(len(feedback['repair_returns']), 1)
        public = FeedbackStore(self.ticket).public_projection()
        self.assertEqual(public['repair_returns'], 1)
        self.assertEqual(public['operator_repair_returns'], 0)
        def fixed(task, attempt, prompt, workspace):
            self.dev_calls.append(attempt)
            self.assertEqual((workspace / 'next.py').read_text(), 'value = 2\n')
            (workspace / 'next.py').write_text('value = 3\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0, quiescent=True)
        final = self.scheduler(root, fixed).run(old_plan['tasks'])
        self.assertEqual(final['tasks']['T2']['status'], 'COMPLETED')
        self.assertEqual(self.dev_calls, [1, 2])
        self.assertEqual((root / 'worktree/app.py').read_bytes(), original_app)

    def test_rejected_remains_terminal_and_does_not_authorize_retry(self):
        self.route = 'REJECTED'
        result = self.run_flow()
        self.assertEqual(result['disposition'], 'REJECTED_NO_RETRY')
        self.assertEqual(runtime_root(self.ticket), self.root)
        self.assertEqual(self.dev_calls, [1])
        self.assertNotIn('repair_returns', FeedbackStore(self.ticket).inspect())

    def test_reviewer_rejection_blocks_typed_return(self):
        self.review_verdict = 'REJECTED'
        self.assertEqual(self.run_flow()['disposition'], 'OPERATOR_DECISION')
        self.assertEqual(runtime_root(self.ticket), self.root)

    def test_outside_repair_files_refused_before_reviewer(self):
        self.repair_files = ['app.py']
        with self.assertRaises(FeedbackError): self.run_flow()
        self.assertEqual(self.role_calls, ['feedback-planner'])
        self.assertEqual(runtime_root(self.ticket), self.root)

    def operator_stage(self):
        self.route = 'REJECTED'
        result = self.run_flow()
        snapshot = {'upgraded_controller': 'explicit operator admission'}
        identity = dict(controller_sha256=digest(execution_contract(snapshot)), environment=self.identity['environment'])
        staged = prepare(self.ticket, self.repo, self.source['plan'], None, 'Repair existing targets; preserve rejected decision',
            runtime_identity=identity, controller_snapshot=snapshot,
            repair_return=dict(operator=True, request_id=result['request_id'], repair_files=['next.py']))
        p = staged['proposal']
        auth = dict(schema_version=1, transition_id=p['transition_id'], proposal_sha256=digest(p),
                    old_controller_sha256=p['old_controller_sha256'], new_controller_sha256=p['new_controller_sha256'],
                    repair_return_sha256=p['repair_return_sha256'], authorized=True, source_fenced=True)
        return staged, auth

    def test_exact_operator_recovery_preserves_rejection_and_upgrade_accounting(self):
        staged, auth = self.operator_stage()
        with self.assertRaises(TaskError): runtime_root(self.ticket)
        wrong = dict(auth, repair_return_sha256='0' * 64)
        with self.assertRaises(TaskError): activate(self.ticket, staged['preparation'], wrong)
        calls = list(self.role_calls)
        activate(self.ticket, staged['preparation'], auth)
        root = runtime_root(self.ticket)
        _, counters = LimitsStore(root)._load(repair=False)
        self.assertEqual(len(counters['invocations']), len(self.source['counters']['invocations']) + 2)
        self.assertEqual(self.role_calls, calls)
        state = FeedbackStore(self.ticket).inspect()
        row = next(iter(state['requests'].values()))
        self.assertEqual(row['proposal']['route'], 'REJECTED')
        self.assertTrue(state['repair_returns'][0]['operator'])
        before = (self.ticket / 'feedback/task-journal.jsonl').read_bytes()
        activate(self.ticket, staged['preparation'], auth)
        self.assertEqual((self.ticket / 'feedback/task-journal.jsonl').read_bytes(), before)

    def second_failure_after_controller_upgrade(self):
        staged, auth = self.operator_stage()
        activate(self.ticket, staged['preparation'], auth)
        self.root = runtime_root(self.ticket)
        recipe = json.loads((Path(staged['preparation']) / 'recipe.json').read_text())
        self.snapshot = recipe['envelope']['binding']['controller']
        self.identity = TaskJournal(self.root / '03-dev/tasks').peek()['candidate_runtime']
        old_verify = self.verify
        def next_verify(argv, cwd, timeout, workspace):
            path = workspace / 'next.py'
            if path.exists() and path.read_text() == 'value = 4\n':
                return ('ERROR: test_next_failure (app.Compatibility.test_next_failure)\n'
                        '----------------------------------------------------------------------\n'
                        'Traceback (most recent call last):\n'
                        '  File "app.py", line 1, in test_next_failure\n'
                        'AttributeError: another missing helper\n\n'
                        'Ran 1 test in 0.1s\n\nFAILED (errors=1)\n', '', 1)
            return old_verify(argv, cwd, timeout, workspace)
        self.verify = next_verify
        def developer(task, attempt, prompt, workspace):
            self.dev_calls.append(attempt)
            store = LimitsStore(self.root)
            invocation = store.reserve('03-dev', '03-dev', 'codex')
            store.finish(invocation, 'returned')
            (workspace / 'next.py').write_text('value = 4\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0, quiescent=True,
                        engine='codex', model='developer')
        with self.assertRaisesRegex(TaskError, 'scope hypothesis'):
            self.scheduler(self.root, developer).run(self.source['plan']['tasks'])
        outer = json.loads((self.root / 'state.json').read_text())
        outer['status'] = 'ESCALATED_TO_SUPERVISOR'; atomic_json(self.root / 'state.json', outer)
        return staged

    def test_next_workflow_after_operator_upgrade_uses_exact_admission(self):
        self.second_failure_after_controller_upgrade()
        before = copy.deepcopy(LimitsStore(self.root)._load(repair=False)[1])
        recording = (self.ticket / 'feedback/authority.json').read_bytes()
        calls = len(self.role_calls)
        self.assertEqual(self.run_flow()['disposition'], 'REJECTED_NO_RETRY')
        self.assertEqual(len(self.role_calls), calls + 2)
        after = LimitsStore(self.root)._load(repair=False)[1]
        self.assertEqual(len(after['invocations']), len(before['invocations']) + 2)
        self.assertTrue(all(after['invocations'][k] == v for k, v in before['invocations'].items()))
        self.assertEqual(after['deadline_at'], before['deadline_at'])
        self.assertEqual((self.ticket / 'feedback/authority.json').read_bytes(), recording)
        self.assertEqual(self.dev_calls, [1, 2])
        self.assertEqual(TaskJournal(self.root / '03-dev/tasks').peek()['tasks']['T2']['attempts'], 2)

    def test_next_workflow_refuses_mismatched_admission_before_consultation(self):
        staged = self.second_failure_after_controller_upgrade()
        store = FeedbackStore(self.ticket)
        original = store.inspect()
        calls = list(self.role_calls)
        counters = LimitsStore(self.root)._load(repair=False)[1]
        for field in ('limits_sha256', 'profile_sha256', 'projection_sha256'):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed['admitted_generations'][staged['proposal']['transition_id']][field] = '0' * 64
                store._append('TEST_ADMISSION_CHANGED', changed)
                with self.assertRaises(TaskError): self.run_flow()
                self.assertEqual(self.role_calls, calls)
                self.assertEqual(LimitsStore(self.root)._load(repair=False)[1], counters)
                store._append('TEST_ADMISSION_RESTORED', original)

    def test_selector_crash_fences_dispatch_and_replays_without_models(self):
        staged, auth = self.operator_stage()
        def crash(point):
            if point == 'repair_return_selector_published': raise OSError('lost reconciliation')
        with self.assertRaises(OSError): activate(self.ticket, staged['preparation'], auth, fault=crash)
        with self.assertRaisesRegex(TaskError, 'reconciliation pending'): runtime_root(self.ticket)
        calls = list(self.role_calls)
        activate(self.ticket, staged['preparation'], auth)
        self.assertEqual(runtime_root(self.ticket).name, staged['proposal']['transition_id'])
        self.assertEqual(self.role_calls, calls)

    def test_delegated_cold_resume_reconciles_without_repeated_roles(self):
        original = repair.activate
        def interrupted(owner, preparation):
            def crash(point):
                if point == 'after_activation': raise OSError('lost selector acknowledgment')
            return original(owner, preparation, fault=crash)
        with patch.object(repair, 'activate', side_effect=interrupted), self.assertRaises(OSError):
            self.run_flow()
        with self.assertRaises(TaskError): runtime_root(self.ticket)
        self.assertEqual(flow.observation_root(self.ticket, self.root), self.root)
        calls = list(self.role_calls)
        with self.host(), patch('ssdlc_controller.controller_snapshot', return_value=self.snapshot):
            self.assertTrue(flow.recover_preparation(self.repo, self.ticket.name))
        self.assertNotEqual(runtime_root(self.ticket), self.root)
        self.assertEqual(self.role_calls, calls)
        self.assertEqual(len(FeedbackStore(self.ticket).inspect()['repair_returns']), 1)

    def test_delegated_activation_requires_real_owner_and_untampered_proof(self):
        captured = {}
        def stopped(owner, preparation):
            captured['preparation'] = Path(preparation)
            raise OSError('stop before activation')
        with patch.object(repair, 'activate', side_effect=stopped), self.assertRaises(OSError):
            self.run_flow()
        preparation = captured['preparation']
        proposal = json.loads((preparation / 'proposal.json').read_text())
        recipe = json.loads((preparation / 'recipe.json').read_text())
        auth = repair.authorization(proposal, recipe['repair_return'])
        with self.assertRaisesRegex(TaskError, 'live native owner'):
            activate(self.ticket, preparation, auth)
        repair.validate_authorization(auth, proposal)
        other_parent = copy.deepcopy(proposal)
        other_parent['parent_selector']['transition_id'] = 'another-generation'
        changed_auth = dict(auth, proposal_sha256=digest(other_parent))
        with self.assertRaises(TaskError): repair.validate_authorization(changed_auth, other_parent)
        for path in ('scope', 'accounting', 'independence', 'fence', 'malformed'):
            changed = copy.deepcopy(auth)
            proof = changed['proof']
            if path == 'scope': proof['subject']['repair_files'] = ['app.py']
            if path == 'accounting': proof['counters']['deadline_at'] += 100
            if path == 'independence': proof['state']['requests'][proof['subject']['request_id']]['reviewer'] = proof['state']['requests'][proof['subject']['request_id']]['developer']
            if path == 'fence': proof['fence']['unresolved_invocations'] = 1
            if path == 'malformed': changed['proof'] = None
            forged_proposal = copy.deepcopy(proposal)
            if path != 'malformed':
                # Exercise semantic checks even if an attacker recomputes
                # unkeyed outer hashes, rather than just testing a checksum.
                if path == 'accounting':
                    proof['subject']['counters_sha256'] = digest(proof['counters'])
                    proof['source_binding']['counters_sha256'] = digest(proof['counters'])
                if path == 'independence': proof['subject']['feedback_state_sha256'] = digest(proof['state'])
                forged_proposal['repair_return_sha256'] = digest(proof)
                changed['proposal_sha256'] = digest(forged_proposal)
            with self.subTest(path=path), self.assertRaises(TaskError):
                repair.validate_authorization(changed, forged_proposal)

    def test_return_receipt_or_original_decision_tampering_fences_dispatch(self):
        self.run_flow()
        store = FeedbackStore(self.ticket)
        state = store.inspect()
        row = next(iter(state['requests'].values()))
        row['proposal']['reason_code'] = 'altered_after_return'
        store._append('TEST_PARTIAL_TAMPER', state)
        with self.assertRaisesRegex(TaskError, 'reconciliation pending'): runtime_root(self.ticket)

    def test_operator_cannot_change_targets_or_treat_rejection_as_delegated_authority(self):
        staged, auth = self.operator_stage()
        preparation = Path(staged['preparation'])
        recipe = json.loads((preparation / 'recipe.json').read_text())
        forged = copy.deepcopy(recipe['repair_return'])
        forged['subject']['operator'] = False
        with self.assertRaises(TaskError): repair.validate_proof(forged)
        forged = copy.deepcopy(recipe['repair_return'])
        forged['subject']['repair_files'] = ['app.py']
        with self.assertRaises(TaskError): repair.validate_proof(forged)

    def test_return_rejects_terminal_categories_and_literal_path_attacks(self):
        for files in ([], ['next.py', 'next.py'], ['../next.py'], ['.git/config'], [{}]):
            with self.subTest(files=files), self.assertRaises(TaskError):
                repair.validate_repair_files(self.source, files)
        changed = copy.deepcopy(self.source)
        changed['journal']['tasks']['T2']['failed_attempts'][-1]['category'] = 'ENVIRONMENT_BLOCKED'
        with self.assertRaises(TaskError): repair.validate_repair_files(changed, ['next.py'])

    def test_cli_routes_prepare_and_activate_without_generic_feedback_parser(self):
        import io
        for command in ('feedback-repair-prepare', 'feedback-repair-activate'):
            with self.subTest(command=command), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                orchestrator.main([command, '--help'])
            self.assertEqual(stopped.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
