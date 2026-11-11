"""Real failing test, captured native candidate, funded offline adjudication.

The developer introduces a genuine production bug. Feedback must preserve the
test rather than authorize changing its expectation to fit the broken code.
"""
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_continuations as continuations
import test_scope_amendments as fixtures
from ssdlc_controller import execution_contract
from ssdlc_feedback import FeedbackStore, source_binding
from ssdlc_feedback_analysis import failure_hint
from ssdlc_feedback_bindings import accounting_anchor, validate_accounting_advance
from ssdlc_feedback_roles import FundedFeedbackRoles
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import inspect
from ssdlc_tasks import DevelopmentBlocked, TaskError, digest, file_hash, git


CONTRACT = '''import importlib.util
from pathlib import Path
import unittest

class ContractTests(unittest.TestCase):
    def test_preserves_approved_value(self):
        path = Path("next.py")
        if path.exists():
            spec = importlib.util.spec_from_file_location("subject", path)
            subject = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(subject)
            self.assertEqual(subject.value, 1)
'''


class NativeHostHintTests(unittest.TestCase):
    def initialize(self):
        original_repo = continuations.RepositoryTests.setUp
        original_scheduler = continuations.TaskScheduler
        original_reconciliation = fixtures.fixtures.ReconciliationTests.setUp
        def repository(f):
            original_repo(f)
            (f.repo / 'tests').mkdir()
            (f.repo / 'tests/__init__.py').write_text('')
            (f.repo / 'tests/test_contract.py').write_text(CONTRACT)
            git(f.repo, 'add', 'tests')
            git(f.repo, '-c', 'core.hooksPath=/dev/null', 'commit', '-m', 'Approved independent contract test')
            f.base = git(f.repo, 'rev-parse', 'HEAD')
            f.plan['base_commit'] = f.base
            f.quality['base_commit'] = f.base
        def scheduler(*args, **kwargs):
            args[4]['01-po/requirements.md'] = (
                '# Requirements\nAC-1: If next.py is introduced its value must equal 1.\n'
                'SEC-1: Preserve the existing independent contract test.\n')
            return original_scheduler(*args, **kwargs)
        def funded(f):
            original_reconciliation(f)
            f.request['profile']['role_invocations'].update(
                {'feedback-diagnosis': 1, 'feedback-planner': 2, 'feedback-reviewer': 2})
            f.request['profile']['phase_invocations']['feedback'] = 5

        class ContractCase(fixtures.ScopeAmendmentTests):
            def verify(self, argv, cwd, timeout, workspace):
                result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                    timeout=timeout, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
                return result.stdout, result.stderr, result.returncode

            def scheduler(self, root, developer):
                engine = super().scheduler(root, developer)
                save = engine._save
                def pause_after_failure(kind):
                    save(kind)
                    task = engine.state['tasks'].get('T2', {})
                    if (kind == 'ATTEMPT_RESTORED' and task.get('failed_attempts')
                            and task['failed_attempts'][-1]['category'] == 'CHECK_FAILURE'):
                        raise DevelopmentBlocked('SCOPE_REQUIRED', 'offline host observation boundary')
                engine._save = pause_after_failure
                return engine

        case = ContractCase()
        # Stop at a native restored failure to inspect before a second developer
        # dispatch. Production scheduler/supervisor integration belongs to F3.
        with (patch.object(continuations.RepositoryTests, 'setUp', repository),
              patch.object(continuations, 'TaskScheduler', scheduler),
              patch.object(fixtures.fixtures.ReconciliationTests, 'setUp', funded),
              patch.object(fixtures, 'inspect', lambda ticket: inspect(ticket, allow_host_hints=True))):
            case.initialize()
        self.addCleanup(case.doCleanups)
        self.case, self.source = case, case.source

    def test_genuine_production_failure_rejects_test_change_without_redispatch(self):
        self.initialize()
        source, case = self.source, self.case
        failed = source['journal']['tasks']['T2']['failed_attempts'][-1]
        self.assertEqual(failed['category'], 'CHECK_FAILURE')
        self.assertEqual(case.calls, [1])
        hint = failure_hint(source, source['plan']['tasks'][1])
        self.assertEqual(hint['classification'], 'ALL_OUTSIDE')
        self.assertFalse(hint['authorization'])
        self.assertIn('tests/test_contract.py', [row['path'] for row in hint['failures']])
        # The existing manual transition has not been broadened implicitly.
        with self.assertRaisesRegex(TaskError, 'explicit SCOPE_REQUIRED'):
            inspect(case.ticket)

        baseline = source['root'] / 'worktree/tests/test_contract.py'
        original_test = baseline.read_bytes()
        check = failed['checks'][0]
        log = source['root'] / '03-dev/tasks' / check['log_ref']
        self.assertIn('2 != 1', log.read_text())
        feedback = FeedbackStore(case.ticket)
        policy = dict(schema_version=1, capability='record-only-v1', ticket=case.ticket.name,
            source_binding_sha256=digest(source_binding(source)),
            controller_sha256=digest(execution_contract(source['envelope']['binding']['controller'])),
            limits_sha256=digest(source['envelope']), allowed_files=['tests/test_contract.py'],
            max_requests=2, max_amendments=1, max_amendments_per_task=1, max_added_files=1,
            max_rounds=2, role_calls={'diagnosis': 1, 'planner': 1, 'reviewer': 1},
            require_strong_independence=True, expires_at=source['counters']['deadline_at'])
        feedback.authorize(policy, dict(schema_version=1, policy_sha256=digest(policy),
                                        authorized=True, source_fenced=True))
        ref = '03-dev/tasks/' + check['log_ref']
        request = dict(schema_version=1, cause='MISSING_TARGET', obligation_refs=['AC-1'],
            requested_files=['tests/test_contract.py'], evidence_refs={ref: source['files'][ref]})
        actor = lambda identity, engine: dict(identity=identity, context_id=identity, engine=engine, model=identity)
        row = feedback.submit(source, request, actor('captured-developer', 'codex'))
        self.assertEqual(row['origin'], 'HOST_HINT')
        anchor = accounting_anchor(source)
        roles = FundedFeedbackRoles(feedback, LimitsStore(source['root']), guard=lambda: None)
        proposal = roles.run(row['request_id'], 'planner', actor('planner', 'claude'),
            lambda metadata, invocation: dict(response=dict(route='REJECTED', requested_files=[],
                reason_code='production_bug_keep_contract_test'), usage=dict(metered_usd=None, cli_estimate_usd=0.01)))
        row = feedback.propose(row['request_id'], proposal, actor('planner', 'claude'),
                               inspect(case.ticket, allow_host_hints=True))
        review = roles.run(row['request_id'], 'reviewer', actor('reviewer', 'agy'),
            lambda metadata, invocation: dict(response=dict(proposal_sha256=row['proposal_sha256'],
                verdict='APPROVED', coverage_preserved=True, reason_code='independent_test_is_valid'),
                usage=dict(metered_usd=None, cli_estimate_usd=None)))
        # Reopen the canonical ledger after both paid consultations: replay uses
        # the captured receipts and exact accounting advance, not an old source
        # object or a rewritten model answer.
        feedback = FeedbackStore(case.ticket)
        feedback.review(row['request_id'], review, actor('reviewer', 'agy'),
                        inspect(case.ticket, allow_host_hints=True))
        self.assertEqual(feedback.disposition(row['request_id']), 'REJECTED_NO_RETRY')
        self.assertEqual(baseline.read_bytes(), original_test)
        self.assertEqual(case.calls, [1])
        fresh = inspect(case.ticket, allow_host_hints=True)
        proof = validate_accounting_advance(anchor, fresh, feedback.inspect()['role_calls'])
        self.assertEqual(len(proof['charged_invocations']), 2)
        self.assertEqual(fresh['journal']['tasks']['T2']['attempts'], 1)
        self.assertEqual(feedback.inspect()['amendments'], [])

    def test_repair_prompt_receives_only_a_bounded_non_authorizing_scope_hint(self):
        import json
        self.initialize()
        engine = self.case.scheduler(self.source['root'], lambda *a: self.fail('no developer dispatch'))
        engine.state = self.source['journal']
        task = next(t for t in self.source['plan']['tasks'] if t['task_id'] == 'T2')
        prompt = engine._developer_prompt(task, engine.state['tasks']['T2'])
        context = json.loads(prompt.split('REPAIR CONTEXT:\n', 1)[1])
        hint = context['scope_hint']
        self.assertEqual(hint['classification'], 'ALL_OUTSIDE')
        self.assertEqual(hint['origin'], 'HOST_HINT')
        self.assertTrue(hint['hypothesis_only'])
        self.assertFalse(hint['authorization'])
        self.assertLessEqual(len(hint['failures']), 8)
        self.assertEqual(self.case.calls, [1])


if __name__ == '__main__':
    unittest.main()
