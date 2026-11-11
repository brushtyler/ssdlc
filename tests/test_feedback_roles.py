"""Consultation crashes use native reservations; no provider is contacted."""
import copy
import unittest

import test_feedback as records
from test_recovery_limits import policy
from ssdlc_controller import execution_contract
from ssdlc_feedback import FeedbackError
from ssdlc_feedback_roles import FundedFeedbackRoles
from ssdlc_recovery import LimitsStore, RecoveryStop
from ssdlc_tasks import TaskJournal, atomic_json, digest


class FundedFeedbackRoleTests(unittest.TestCase):
    def setUp(self):
        self.initialize()

    def initialize(self, attempts=3):
        self.fixture = f = records.FeedbackTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.limits = LimitsStore(f.root / 'native', clock=lambda: 100)
        self.profile = policy(development_attempts=attempts,
            role_invocations={'03-dev': 3, 'feedback-planner': 2, 'feedback-reviewer': 2, 'feedback-diagnosis': 1},
            phase_invocations={'03-dev': 3, 'feedback': 5})
        self.limits.authorize(self.profile, dict(ticket='ISSUE22', controller={'offline': 'controller'},
            allowed_phases=['03-dev', 'feedback']))
        journal = TaskJournal(self.limits.root / '03-dev/tasks')
        journal.load(); journal.append('SYNTHETIC_STOPPED_SOURCE', f.source['journal'])
        atomic_json(self.limits.root / '02-design/plan.json', f.source['plan'])
        envelope = self.limits._load(repair=False)[0]
        f.policy.update(limits_sha256=digest(envelope),
                        controller_sha256=digest(execution_contract(envelope['binding']['controller'])))
        f.authorization['policy_sha256'] = digest(f.policy)
        self.row = f.authorized()
        self.guard_calls = []
        self.roles = FundedFeedbackRoles(f.store, self.limits, guard=lambda: self.guard_calls.append('guard'))
        self.metadata = records.a('planner')
        self.calls = []
        self.response = dict(response={'route': 'INCONCLUSIVE'},
                             usage={'metered_usd': None, 'cli_estimate_usd': None})

    def runner(self, actor, invocation):
        self.assertEqual(actor, self.metadata)
        row = self.limits._load(repair=False)[1]['invocations'][invocation]
        self.assertEqual(row['status'], 'reserved')
        self.calls.append(invocation)
        return copy.deepcopy(self.response)

    def run_role(self, **kwargs):
        return self.roles.run(self.row['request_id'], 'planner', self.metadata, self.runner, **kwargs)

    def test_named_role_is_funded_inside_total_caps_without_charging_developer(self):
        self.assertEqual(self.run_role(), self.response['response'])
        rows = list(self.limits._load(repair=False)[1]['invocations'].values())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['role'], 'feedback-planner')
        self.assertEqual(rows[0]['phase'], 'feedback')
        self.assertEqual(rows[0]['status'], 'returned')
        self.assertIsNone(rows[0]['usage']['cli_estimate_usd'])
        self.assertEqual(self.fixture.source['journal']['tasks']['T08']['attempts'], 2)
        self.assertGreaterEqual(len(self.guard_calls), 3)

    def test_captured_response_reconciles_after_crash_without_redispatch(self):
        def fault(point):
            if point == 'response_captured':
                raise OSError('crash')
        with self.assertRaises(OSError):
            self.run_role(fault=fault)
        self.assertEqual(len(self.calls), 1)
        operation = self.fixture.store.inspect()['role_calls'][0]['operation_id']
        self.assertEqual(self.roles.resume_response(operation), self.response['response'])
        self.assertEqual(self.roles.resume_response(operation), self.response['response'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.limits._load(repair=False)[1]['invocations']), 1)
        with self.assertRaisesRegex(FeedbackError, 'already reserved'):
            self.run_role()

    def test_crash_between_reservation_and_binding_remains_uncertain(self):
        def fault(point):
            if point == 'native_reservation':
                raise OSError('crash')
        with self.assertRaises(OSError):
            self.run_role(fault=fault)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.limits._load(repair=False)[1]['invocations']), 1)
        with self.assertRaises(RecoveryStop):
            self.run_role()
        operation = self.fixture.store.inspect()['role_calls'][0]['operation_id']
        with self.assertRaisesRegex(FeedbackError, 'Unconfirmed'):
            self.roles.resume_response(operation)

    def test_missing_role_capacity_fails_before_intent_or_model(self):
        other = LimitsStore(self.fixture.root / 'unfunded', clock=lambda: 100)
        other.authorize(policy(development_attempts=3), dict(ticket='ISSUE22', controller={'offline': 'controller'}))
        envelope = other._load(repair=False)[0]
        # A different authority cannot be substituted for the frozen allocation.
        roles = FundedFeedbackRoles(self.fixture.store, other, guard=lambda: None)
        with self.assertRaisesRegex(FeedbackError, 'frozen native authority'):
            roles.run(self.row['request_id'], 'planner', self.metadata, self.runner)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.fixture.store.inspect()['role_calls'], [])

    def test_unknown_exception_stays_charged_and_blocks_further_calls(self):
        def failed(actor, invocation):
            raise RuntimeError('transport result unknown')
        with self.assertRaises(RuntimeError):
            self.roles.run(self.row['request_id'], 'planner', self.metadata, failed)
        row = next(iter(self.limits._load(repair=False)[1]['invocations'].values()))
        self.assertEqual(row['status'], 'uncertain')
        with self.assertRaises(RecoveryStop):
            self.run_role()

    def test_malformed_return_is_charged_but_never_used_for_a_blind_retry(self):
        self.response['usage']['cli_estimate_usd'] = -1
        with self.assertRaisesRegex(FeedbackError, 'Malformed returned'):
            self.run_role()
        row = next(iter(self.limits._load(repair=False)[1]['invocations'].values()))
        self.assertEqual(row['status'], 'returned')
        self.assertIsNone(row['usage']['cli_estimate_usd'])
        self.assertEqual(self.fixture.store.inspect()['role_calls'][0]['status'], 'INTENT')
        with self.assertRaisesRegex(FeedbackError, 'already reserved'):
            self.run_role()
        self.assertEqual(len(self.calls), 1)

    def test_stale_completed_task_and_partial_accounting_fail_before_consultation(self):
        journal = TaskJournal(self.limits.root / '03-dev/tasks')
        state = journal.load(); state['tasks']['T08']['status'] = 'COMPLETED'
        journal.append('EXTERNAL_TASK_ADVANCE', state)
        with self.assertRaisesRegex(FeedbackError, 'Task or plan changed'):
            self.run_role()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.fixture.store.inspect()['role_calls'], [])
        path = self.limits.journal.path
        with path.open('ab') as output:
            output.write(b'{"partial":')
        before = path.read_bytes()
        with self.assertRaisesRegex(FeedbackError, 'Incomplete native accounting'):
            self.run_role()
        self.assertEqual(path.read_bytes(), before)

    def test_exhausted_development_attempts_stop_before_any_role_intent(self):
        case = FundedFeedbackRoleTests()
        case.initialize(attempts=2)
        self.addCleanup(case.doCleanups)
        with self.assertRaisesRegex(FeedbackError, 'No useful development attempt'):
            case.run_role()
        self.assertEqual(case.calls, [])
        self.assertEqual(case.fixture.store.inspect()['role_calls'], [])
        self.assertEqual(case.limits._load(repair=False)[1]['invocations'], {})


if __name__ == '__main__':
    unittest.main()
