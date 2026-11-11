"""Offline delegated native activation with real journals, worktrees and flocks.

Only external controller/Docker observations and role responses are simulated.
No paid provider is contacted and no human activation attestation is fabricated.
"""
import contextlib
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_scope_amendments as fixtures
import ssdlc_feedback_delegation as delegated
from ssdlc_controller import execution_contract
from ssdlc_feedback import FeedbackError, FeedbackStore, source_binding
from ssdlc_feedback_roles import FundedFeedbackRoles
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import inspect
from ssdlc_tasks import TaskError, TaskJournal, atomic_json, digest
from ssdlc_transitions import TransitionStore, runtime_root, validate_authorization
from test_recovery_limits import policy as limit_profile


def actor(name, engine):
    return dict(identity=name, context_id=name, engine=engine, model=name)


def grant():
    return dict(schema_version=1, capability=delegated.CONTRACT, allowed_files=['helper.py', 'other.py'],
                max_amendments=2, max_amendments_per_task=2, max_added_files=2,
                require_strong_independence=True)


class DelegatedScopeTests(unittest.TestCase):
    def setUp(self):
        self.initialize()

    def initialize(self, attempt_cap=3, grant_enabled=True, grant_cap=2):
        original = fixtures.fixtures.ReconciliationTests.setUp
        def funded(f):
            original(f)
            if grant_enabled:
                f.request['profile']['feedback_delegation'] = grant()
                f.request['profile']['feedback_delegation']['max_amendments'] = grant_cap
            f.request['profile']['role_invocations'].update({'feedback-planner': 2, 'feedback-reviewer': 2})
            f.request['profile']['phase_invocations']['feedback'] = 4

        class HelperScope(fixtures.ScopeAmendmentTests):
            def scheduler(self, root, developer):
                def wrapped(*args):
                    result = developer(*args)
                    result['output'] = result['output'].replace('app.py to the manifest', 'helper.py to the manifest')
                    return result
                return super().scheduler(root, wrapped)

        self.case = f = HelperScope()
        with patch.object(fixtures.fixtures.ReconciliationTests, 'setUp', funded):
            f.initialize(attempt_cap=attempt_cap)
        self.addCleanup(f.doCleanups)
        self.source = f.source
        self.snapshot = self.source['envelope']['binding']['controller']
        self.runtime = self.source['journal']['candidate_runtime']
        self.plan = copy.deepcopy(self.source['plan'])
        self.plan['plan_revision'] += 1
        self.plan['tasks'][1]['target_files'].append('helper.py')
        self.feedback = FeedbackStore(f.ticket)
        policy = dict(schema_version=1, capability='record-only-v1', ticket=f.ticket.name,
            source_binding_sha256=digest(source_binding(self.source)),
            controller_sha256=digest(execution_contract(self.snapshot)), limits_sha256=digest(self.source['envelope']),
            allowed_files=['helper.py', 'other.py'], max_requests=3, max_amendments=2, max_amendments_per_task=2,
            max_added_files=2, max_rounds=2, role_calls={'diagnosis': 0, 'planner': 2, 'reviewer': 2},
            require_strong_independence=True, expires_at=self.source['counters']['deadline_at'])
        self.feedback.authorize(policy, dict(schema_version=1, policy_sha256=digest(policy),
                                            authorized=True, source_fenced=True))
        ref = next(r for r in self.source['files'] if r.endswith('/developer.log'))
        task = self.source['plan']['tasks'][1]
        request = dict(schema_version=1, cause='MISSING_TARGET', requested_files=['helper.py'],
            obligation_refs=task['ac_refs'] + task['sec_refs'], evidence_refs={ref: self.source['files'][ref]})
        self.row = self.feedback.submit(self.source, request, actor('developer', 'codex'))
        self.roles = FundedFeedbackRoles(self.feedback, LimitsStore(self.source['root']), guard=lambda: None)
        self.role_calls = []

    @contextlib.contextmanager
    def session(self, preparation=None, quiescent=True):
        with (patch.object(delegated, 'assert_controller'),
              patch.object(delegated, 'observe_environment', return_value=self.runtime['environment']),
              patch.object(delegated, 'docker_quiescent', return_value=quiescent)):
            with delegated.fenced(self.case.ticket, Path(__file__).parent.parent, self.snapshot,
                                  self.runtime, preparation=preparation) as session:
                yield session

    def decide(self):
        def runner(value):
            def run(metadata, invocation):
                self.role_calls.append(invocation)
                return dict(response=value, usage=dict(metered_usd=None, cli_estimate_usd=0.01))
            return run
        planner = actor('planner', 'claude')
        proposal = dict(route='AMENDMENT', requested_files=self.row['request']['requested_files'],
                        reason_code='add_required_helper')
        response = self.roles.run(self.row['request_id'], 'planner', planner, runner(proposal))
        row = self.feedback.propose(self.row['request_id'], response, planner, inspect(self.case.ticket))
        reviewer = actor('reviewer', 'agy')
        review = dict(proposal_sha256=row['proposal_sha256'], verdict='APPROVED',
                      coverage_preserved=True, reason_code='preserves_existing_coverage')
        response = self.roles.run(self.row['request_id'], 'reviewer', reviewer, runner(review))
        self.feedback.review(row['request_id'], response, reviewer, inspect(self.case.ticket))

    def stage(self, session, **kwargs):
        return delegated.prepare(session, self.case.repo, self.plan, self.row['request_id'], **kwargs)

    def test_funded_delegation_preserves_history_and_native_repair(self):
        self.decide()
        source = inspect(self.case.ticket)
        original = {r: (source['root'] / r).read_bytes() for r in source['files']}
        with self.session() as session:
            staged = self.stage(session)
            recipe = json.loads((Path(staged['preparation']) / 'recipe.json').read_text())
            auth = delegated.authorization(staged['proposal'], recipe['review'])
            self.assertNotIn('source_fenced', auth)
            self.assertNotIn('authorized', auth)
            selected = delegated.activate(session, staged['preparation'])
            self.assertEqual(delegated.activate(session, staged['preparation']), selected)
            old_accounting = LimitsStore(source['root'])._load(repair=False)[1]
            with self.assertRaisesRegex(TaskError, 'currently selected native generation'):
                self.roles.run(self.row['request_id'], 'reviewer', actor('extra-reviewer', 'agy'),
                               lambda *args: self.fail('must not dispatch through old accounting'))
            operation = self.feedback.inspect()['role_calls'][0]['operation_id']
            with self.assertRaisesRegex(TaskError, 'currently selected native generation'):
                self.roles.resume_response(operation)
            self.assertEqual(LimitsStore(source['root'])._load(repair=False)[1], old_accounting)
        root = runtime_root(self.case.ticket)
        envelope, counters = LimitsStore(root)._load(repair=False)
        self.assertEqual(envelope['policy'], source['envelope']['policy'])
        self.assertEqual(counters, dict(source['counters'], policy_sha256=digest(envelope)))
        self.assertEqual({r: (source['root'] / r).read_bytes() for r in original}, original)
        self.assertEqual(self.feedback.disposition(self.row['request_id']), 'APPLIED')
        self.assertEqual(len(self.feedback.inspect()['amendments']), 1)
        calls = []
        def develop(task, attempt, prompt, workspace):
            calls.append(attempt)
            self.assertIn('scope_amendment', prompt)
            store = LimitsStore(root); reservation = store.reserve('03-dev', '03-dev', 'codex')
            store.finish(reservation, 'returned')
            (workspace / 'helper.py').write_text('value = 1\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0, quiescent=True, engine='codex', model='offline')
        result = self.case.scheduler(root, develop).run(self.plan['tasks'])
        self.assertEqual(calls, [3])
        self.assertEqual(result['tasks']['T2']['status'], 'COMPLETED')
        self.assertEqual(len(self.role_calls), 2)
        self.assertEqual(len(LimitsStore(root)._load(repair=False)[1]['invocations']), len(counters['invocations']) + 1)

    def test_activation_crashes_replay_without_double_quota_or_dispatch(self):
        self.decide()
        for boundary in ('intent_published', 'generation_staged', 'before_activation', 'after_activation',
                         'before_feedback_reconciliation', 'after_feedback_reconciliation'):
            case = self if boundary == 'intent_published' else DelegatedScopeTests()
            if case is not self:
                case.setUp(); self.addCleanup(case.doCleanups); case.decide()
            with self.subTest(boundary=boundary):
                with case.session() as session:
                    staged = case.stage(session)
                    def crash(point):
                        if point == boundary:
                            raise OSError('injected publication interruption')
                    with self.assertRaises(OSError):
                        delegated.activate(session, staged['preparation'], fault=crash)
                if boundary in {'after_activation', 'before_feedback_reconciliation'}:
                    with self.assertRaisesRegex(TaskError, 'reconciliation pending'):
                        runtime_root(case.case.ticket)
                with case.session(preparation=staged['preparation']) as session:
                    selected = delegated.activate(session, staged['preparation'])
                    self.assertEqual(delegated.activate(session, staged['preparation']), selected)
                self.assertEqual(runtime_root(case.case.ticket).name, staged['proposal']['transition_id'])
                state = case.feedback.inspect()
                self.assertEqual(len(state['amendments']), 1)
                self.assertEqual(state['amendments'][0]['status'], 'APPLIED')
                self.assertEqual(len(case.role_calls), 2)
                self.assertEqual(case.case.calls, [1, 2])

    def test_unfunded_recorded_approval_cannot_activate(self):
        planner, reviewer = actor('planner', 'claude'), actor('reviewer', 'agy')
        row = self.feedback.propose(self.row['request_id'],
            dict(route='AMENDMENT', requested_files=['helper.py'], reason_code='add_helper'), planner, self.source)
        self.feedback.review(row['request_id'], dict(proposal_sha256=row['proposal_sha256'], verdict='APPROVED',
            coverage_preserved=True, reason_code='approved'), reviewer, self.source)
        with self.session() as session, self.assertRaisesRegex(TaskError, 'funded decision'):
            self.stage(session)
        self.assertEqual(self.role_calls, [])

    def test_writer_or_lost_lock_refuses_before_preparation(self):
        self.decide()
        with self.assertRaisesRegex(TaskError, 'writers'):
            with self.session(quiescent=False):
                self.fail('must refuse')
        with self.session() as session:
            pass
        with self.assertRaisesRegex(TaskError, 'ownership'):
            self.stage(session)

    def test_recording_and_role_caps_without_execution_grant_cannot_activate(self):
        self.initialize(grant_enabled=False)
        self.decide()
        before = inspect(self.case.ticket)['parent_selector']
        with self.session() as session, self.assertRaisesRegex(TaskError, 'frozen scope delegation grant'):
            self.stage(session)
        self.assertEqual(inspect(self.case.ticket)['parent_selector'], before)

    def test_descendant_keeps_canonical_caps_and_requires_native_admission(self):
        self.initialize(attempt_cap=4)
        self.descendant_request()
        self.decide()
        with self.session() as session:
            second = self.stage(session)
            delegated.activate(session, second['preparation'])
        state = self.feedback.inspect()
        self.assertEqual(len(state['amendments']), 2)
        self.assertTrue(all(s['status'] == 'APPLIED' for s in state['amendments']))
        self.assertEqual(len(state['admitted_generations']), 2)
        self.assertEqual(len(self.role_calls), 4)
        final = TaskJournal(runtime_root(self.case.ticket) / '03-dev/tasks').peek()
        self.assertEqual(final['tasks']['T2']['attempts'], 3)

    def test_delegation_quota_stops_before_another_consultation(self):
        self.initialize(attempt_cap=4, grant_cap=1)
        self.descendant_request()
        before = LimitsStore(self.source['root'])._load(repair=False)[1]
        with self.assertRaisesRegex(TaskError, 'quota exhausted before consultation'):
            self.decide()
        self.assertEqual(LimitsStore(self.source['root'])._load(repair=False)[1], before)
        self.assertEqual(len(self.role_calls), 2)

    def descendant_request(self):
        self.decide()
        with self.session() as session:
            staged = self.stage(session)
            delegated.activate(session, staged['preparation'])
        root = runtime_root(self.case.ticket)
        def still_blocked(task, attempt, prompt, workspace):
            store = LimitsStore(root); reservation = store.reserve('03-dev', '03-dev', 'codex')
            store.finish(reservation, 'returned')
            return dict(output='Add other.py to the manifest.\nTASK_DEV_STATUS: SCOPE_REQUIRED',
                        exit_code=0, quiescent=True, engine='codex', model='offline')
        with self.assertRaises(TaskError):
            self.case.scheduler(root, still_blocked).run(self.plan['tasks'])
        outer = json.loads((root / 'state.json').read_text()); outer['status'] = 'ESCALATED_TO_SUPERVISOR'
        atomic_json(root / 'state.json', outer)
        self.source = inspect(self.case.ticket)
        self.assertEqual(self.source['journal']['tasks']['T2']['attempts'], 3)
        self.plan = copy.deepcopy(self.source['plan']); self.plan['plan_revision'] += 1
        self.plan['tasks'][1]['target_files'].append('other.py')
        ref = next(r for r in self.source['files'] if r.endswith('attempt-3/developer.log'))
        task = self.source['plan']['tasks'][1]
        request = dict(schema_version=1, cause='MISSING_TARGET', requested_files=['other.py'],
            obligation_refs=task['ac_refs'] + task['sec_refs'], evidence_refs={ref: self.source['files'][ref]})
        self.row = self.feedback.submit(self.source, request, actor('developer', 'codex'))
        self.assertFalse(self.row['source_reconciliation_required'])
        self.roles = FundedFeedbackRoles(self.feedback, LimitsStore(root), guard=lambda: None)

    def test_review_source_and_grant_tampering_are_rejected(self):
        self.decide()
        with self.session() as session:
            staged = self.stage(session)
            proof = json.loads((Path(staged['preparation']) / 'recipe.json').read_text())['review']
            auth = delegated.authorization(staged['proposal'], proof)
            manual = dict(auth, authorized=True, source_fenced=True)
            with self.assertRaises(TaskError):
                validate_authorization(manual, staged['proposal'])
            for target in ('grant', 'old_cost', 'response'):
                bad = copy.deepcopy(proof)
                if target == 'grant':
                    bad['grant']['max_amendments'] += 1
                elif target == 'old_cost':
                    previous = next(iter(bad['state']['requests'][self.row['request_id']]['accounting_anchor']['counters']['invocations']))
                    bad['source_counters']['invocations'][previous]['usage']['cli_estimate_usd'] = 999
                else:
                    bad['state']['role_calls'][-1]['receipt']['response']['response']['verdict'] = 'REJECTED'
                with self.subTest(target=target), self.assertRaises(TaskError):
                    delegated.validate_proof(bad)

    def test_crash_after_prepare_reuses_sealed_decisions(self):
        self.decide()
        def crash(point):
            if point == 'prepared':
                raise OSError('lost prepare response')
        with self.session() as session, self.assertRaises(OSError):
            self.stage(session, fault=crash)
        preparations = list((self.case.ticket / 'execution').glob('prepared-*'))
        preparation = next(p for p in preparations if json.loads((p / 'proposal.json').read_text()).get(
            'authorization_contract') == delegated.CONTRACT)
        with self.session(preparation=preparation) as session:
            delegated.activate(session, preparation)
        self.assertEqual(len(self.role_calls), 2)
        self.assertEqual(len(self.feedback.inspect()['amendments']), 1)

    def test_environment_or_prepared_runtime_change_cannot_publish(self):
        self.decide()
        with self.session() as session:
            with patch.object(delegated, 'observe_environment', return_value={'mode': 'local'}):
                with self.assertRaisesRegex(TaskError, 'environment changed'):
                    self.stage(session)
            staged = self.stage(session)
            runtime = self.case.ticket / 'execution-runtimes' / staged['proposal']['transition_id']
            (runtime / 'worktree/app.py').write_text('tampered = True\n')
            with self.assertRaises(TaskError):
                delegated.activate(session, staged['preparation'])
        self.assertEqual(self.feedback.inspect()['amendments'][0]['status'], 'RESERVED')
        self.assertEqual(TransitionStore(self.case.ticket / 'execution').load()['transition_id'],
                         self.source['projection']['transition_id'])

    def test_invalid_delegation_cannot_initialize_native_limits(self):
        for change in ('unfunded', 'alias', 'weak_independence'):
            p = limit_profile(feedback_delegation=grant())
            if change == 'alias':
                p['feedback_delegation']['allowed_files'] = ['../helper.py']
            elif change == 'weak_independence':
                p['feedback_delegation']['require_strong_independence'] = False
            root = self.case.repo / ('invalid-' + change)
            with self.subTest(change=change), self.assertRaises(TaskError):
                LimitsStore(root).authorize(p, {})
            self.assertFalse((root / 'execution-mode.json').exists())


if __name__ == '__main__':
    unittest.main()
