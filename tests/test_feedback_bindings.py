"""A paid review may advance its own ledger, never the reviewed technical state."""
import copy
import unittest
from unittest.mock import patch

import test_scope_amendments as fixtures
from ssdlc_controller import execution_contract
from ssdlc_feedback import FeedbackError, FeedbackStore, source_binding
from ssdlc_feedback_bindings import accounting_anchor, technical_subject, validate_accounting_advance
from ssdlc_feedback_roles import FundedFeedbackRoles
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import inspect
from ssdlc_tasks import digest


class FeedbackBindingTests(unittest.TestCase):
    def setUp(self):
        original = fixtures.fixtures.ReconciliationTests.setUp
        def funded(f):
            original(f)
            f.request['profile']['role_invocations'].update(
                {'feedback-diagnosis': 1, 'feedback-planner': 2, 'feedback-reviewer': 2})
            f.request['profile']['phase_invocations']['feedback'] = 5
        self.fixture = f = fixtures.ScopeAmendmentTests()
        with patch.object(fixtures.fixtures.ReconciliationTests, 'setUp', funded):
            f.initialize()
        self.addCleanup(f.doCleanups)
        self.source = f.source
        self.limits = LimitsStore(self.source['root'])
        ref = next(ref for ref in self.source['files'] if ref.endswith('/developer.log'))
        task = next(t for t in self.source['plan']['tasks'] if t['task_id'] == self.source['task_id'])
        request = dict(schema_version=1, cause='MISSING_TARGET', requested_files=['app.py'],
            obligation_refs=task['ac_refs'] + task['sec_refs'], evidence_refs={ref: self.source['files'][ref]})
        self.feedback = FeedbackStore(f.ticket)
        policy = dict(schema_version=1, capability='record-only-v1', ticket=f.ticket.name,
            source_binding_sha256=digest(source_binding(self.source)),
            controller_sha256=digest(execution_contract(self.source['envelope']['binding']['controller'])),
            limits_sha256=digest(self.source['envelope']), allowed_files=['app.py'],
            max_requests=3, max_amendments=2, max_amendments_per_task=2, max_added_files=2,
            max_rounds=2, role_calls={'diagnosis': 1, 'planner': 2, 'reviewer': 2},
            require_strong_independence=True, expires_at=self.source['counters']['deadline_at'])
        self.feedback.authorize(policy, dict(schema_version=1, policy_sha256=digest(policy),
                                            authorized=True, source_fenced=True))
        actor = lambda identity, engine: dict(identity=identity, context_id=identity, engine=engine, model=identity)
        self.row = self.feedback.submit(self.source, request, actor('developer', 'codex'))
        self.metadata = actor('planner', 'claude')
        self.roles = FundedFeedbackRoles(self.feedback, self.limits, guard=lambda: None)
        self.anchor = accounting_anchor(self.source)
        self.subject = technical_subject(self.source, f.plan, f.reason)

    def consult(self):
        return self.roles.run(self.row['request_id'], 'planner', self.metadata,
            lambda actor, invocation: dict(response={'route': 'AMENDMENT'},
                usage={'metered_usd': None, 'cli_estimate_usd': 0.1}))

    def test_own_consultation_cost_does_not_change_technical_review_subject(self):
        self.consult()
        fresh = inspect(self.fixture.ticket)
        calls = self.feedback.inspect()['role_calls']
        proof = validate_accounting_advance(self.anchor, fresh, calls)
        self.assertEqual(len(proof['charged_invocations']), 1)
        self.assertEqual(technical_subject(fresh, self.fixture.plan, self.fixture.reason), self.subject)
        self.assertNotEqual(source_binding(fresh), source_binding(self.source))
        self.assertEqual(self.fixture.calls, [1, 2])
        self.assertEqual(fresh['journal']['tasks']['T2']['attempts'], 2)

    def test_unrelated_invocation_or_mutated_historical_usage_is_not_accounting_advance(self):
        self.consult()
        calls = self.feedback.inspect()['role_calls']
        fresh = inspect(self.fixture.ticket)
        changed = copy.deepcopy(fresh)
        old = next(iter(self.anchor['counters']['invocations']))
        changed['counters']['invocations'][old]['usage']['metered_usd'] = 0
        with self.assertRaisesRegex(FeedbackError, 'Historical invocation'):
            validate_accounting_advance(self.anchor, changed, calls)
        unrelated = self.limits.reserve('03-dev', '03-dev', 'codex')
        self.limits.finish(unrelated, 'returned')
        with self.assertRaisesRegex(FeedbackError, 'Unrelated invocation'):
            validate_accounting_advance(self.anchor, inspect(self.fixture.ticket), calls)

    def test_code_policy_and_response_mutations_cannot_hide_in_the_review_cost(self):
        self.consult()
        calls = self.feedback.inspect()['role_calls']
        fresh = inspect(self.fixture.ticket)
        changed = copy.deepcopy(fresh)
        changed['files']['02-design/plan.json'] = '0' * 64
        with self.assertRaisesRegex(FeedbackError, 'Technical inputs'):
            validate_accounting_advance(self.anchor, changed, calls)
        changed = copy.deepcopy(fresh)
        changed['envelope']['policy']['invocations'] += 1
        with self.assertRaisesRegex(FeedbackError, 'accounting authority'):
            validate_accounting_advance(self.anchor, changed, calls)
        bad = copy.deepcopy(calls)
        bad[0]['receipt']['response']['response']['route'] = 'ADVISORY'
        with self.assertRaisesRegex(FeedbackError, 'response changed'):
            validate_accounting_advance(self.anchor, fresh, bad)

    def test_accounting_advance_after_inspection_is_not_a_fresh_snapshot(self):
        self.consult()
        calls = self.feedback.inspect()['role_calls']
        fresh = inspect(self.fixture.ticket)
        other = self.limits.reserve('03-dev', '03-dev', 'codex')
        self.limits.finish(other, 'returned')
        with self.assertRaisesRegex(FeedbackError, 'Current native accounting changed'):
            validate_accounting_advance(self.anchor, fresh, calls)

    def test_worktree_mutation_is_not_hidden_by_matching_cached_source_metadata(self):
        self.consult()
        calls = self.feedback.inspect()['role_calls']
        fresh = inspect(self.fixture.ticket)
        (fresh['root'] / 'worktree/app.py').write_text('value = 999\n')
        with self.assertRaisesRegex(FeedbackError, 'Checkpoint worktree changed'):
            validate_accounting_advance(self.anchor, fresh, calls)


if __name__ == '__main__':
    unittest.main()
