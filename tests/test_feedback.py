"""Offline feedback decisions never grant execution by themselves."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from ssdlc_feedback import FeedbackError, FeedbackStore, literal_path, source_binding
from ssdlc_tasks import digest, file_hash, git


def a(name):
    return dict(identity=name, context_id=name, engine=name.split(':')[0], model=name.split(':')[0])


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.evidence = self.root / 'source'
        self.evidence.mkdir()
        worktree = self.evidence / 'worktree'
        (worktree / 'tests').mkdir(parents=True)
        (worktree / 'app.py').write_text('value = 1\n')
        (worktree / 'tests/test_reconciliation.py').write_text('def test_case():\n    assert True\n')
        git(worktree, 'init', '-q')
        git(worktree, 'config', 'user.name', 'Offline fixture')
        git(worktree, 'config', 'user.email', 'fixture@example.invalid')
        git(worktree, 'add', '.')
        git(worktree, '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Synthetic baseline')
        base = git(worktree, 'rev-parse', 'HEAD')
        (self.evidence / 'developer.log').write_text('SCOPE_REQUIRED: existing obligation needs test adaptation')
        self.source = dict(root=self.evidence, task_id='T08',
            parent_selector={'transition_id': 'parent'},
            plan={'base_commit': base, 'tasks': [{'task_id': 'T08', 'target_files': ['app.py'],
                                                'ac_refs': ['AC-37'], 'sec_refs': ['SEC-11']}]},
            files={'developer.log': file_hash(self.evidence / 'developer.log')}, counters={'invocations': 37},
            journal={'tasks': {'T08': {'status': 'FAILED', 'attempts': 2, 'failed_attempts': [
                {'category': 'SCOPE_REQUIRED', 'capture_sha256': 'a' * 64}]}}})
        self.ticket = self.root / 'ISSUE22'
        self.policy = dict(schema_version=1, capability='record-only-v1', ticket='ISSUE22',
            source_binding_sha256=digest(source_binding(self.source)), controller_sha256='b' * 64,
            limits_sha256='c' * 64, allowed_files=['tests/test_reconciliation.py'],
            max_requests=3, max_amendments=2, max_added_files=2, max_rounds=2,
            role_calls={'diagnosis': 1, 'planner': 2, 'reviewer': 2}, expires_at=200,
            max_amendments_per_task=2, require_strong_independence=True)
        self.authorization = dict(schema_version=1, policy_sha256=digest(self.policy),
                                  authorized=True, source_fenced=True)
        self.store = FeedbackStore(self.ticket, clock=lambda: 100)
        self.request = dict(schema_version=1, cause='MISSING_TARGET', obligation_refs=['AC-37', 'SEC-11'],
            requested_files=['tests/test_reconciliation.py'], evidence_refs=self.source['files'])

    def authorized(self):
        self.store.authorize(self.policy, self.authorization)
        return self.store.submit(self.source, self.request, a('developer:codex:invocation-37'))

    def proposed(self):
        row = self.authorized()
        return self.store.propose(row['request_id'], dict(route='AMENDMENT',
            requested_files=self.request['requested_files'], reason_code='existing_obligation_missing_test'),
            a('planner:separate-context'), self.source)

    def configure_new_files(self, paths):
        self.policy['allowed_files'] = list(paths)
        self.policy['source_binding_sha256'] = digest(source_binding(self.source))
        self.authorization['policy_sha256'] = digest(self.policy)

    def approve_file(self, path, reviewer=None):
        self.store.authorize(self.policy, self.authorization)
        request = dict(self.request, requested_files=[path])
        row = self.store.submit(self.source, request, a('developer'))
        row = self.store.propose(row['request_id'], dict(route='AMENDMENT', requested_files=[path],
            reason_code='missing_target'), a('planner'), self.source)
        review = dict(proposal_sha256=row['proposal_sha256'], verdict='APPROVED',
                      coverage_preserved=True, reason_code='preserved')
        return self.store.review(row['request_id'], review, reviewer or a('reviewer'), self.source)

    def test_t08_like_feedback_is_reviewed_but_not_execution_authority(self):
        row = self.proposed()
        result = self.store.review(row['request_id'], dict(proposal_sha256=row['proposal_sha256'],
            verdict='APPROVED', coverage_preserved=True, reason_code='preserve_history_and_stagnation'),
            a('reviewer:separate-context'), self.source)
        self.assertEqual(result['status'], 'REVIEWED')
        self.assertEqual(self.store.disposition(row['request_id']), 'OPERATOR_DECISION')
        self.assertEqual(result['path_classes']['tests/test_reconciliation.py']['kind'], 'BASELINE_TEST')
        self.assertFalse((self.ticket / 'execution/active.json').exists())
        self.assertEqual(self.source['journal']['tasks']['T08']['attempts'], 2)
        self.assertEqual(self.store.inspect()['amendments'], [])

    def test_duplicate_feedback_after_restart_does_not_consume_allowance(self):
        original = self.authorized()
        before = (self.store.root / 'task-journal.jsonl').read_bytes()
        replayed = FeedbackStore(self.ticket, clock=lambda: 100).submit(self.source, self.request, a('developer:retry'))
        self.assertEqual(original, replayed)
        self.assertEqual((self.store.root / 'task-journal.jsonl').read_bytes(), before)

    def test_equivalent_obligation_order_is_not_a_new_request(self):
        original = self.authorized()
        changed_order = dict(self.request, obligation_refs=list(reversed(self.request['obligation_refs'])))
        before = (self.store.root / 'task-journal.jsonl').read_bytes()
        duplicate = self.store.submit(self.source, changed_order, a('developer:retry'))
        self.assertEqual(duplicate, original)
        self.assertEqual((self.store.root / 'task-journal.jsonl').read_bytes(), before)

    def test_operator_authorization_is_exact_and_not_inferred_from_request(self):
        for key, value in [('authorized', False), ('source_fenced', False), ('policy_sha256', '0' * 64)]:
            auth = dict(self.authorization, **{key: value})
            with self.subTest(key=key), self.assertRaises(FeedbackError):
                self.store.authorize(self.policy, auth)
        self.assertFalse((self.store.root / 'authority.json').exists())

    def test_authorization_replays_both_initialization_crash_boundaries(self):
        for boundary in ('policy_intent', 'policy_journal'):
            with self.subTest(boundary=boundary):
                ticket = self.root / boundary / 'ISSUE22'
                store = FeedbackStore(ticket, clock=lambda: 100)
                def fault(point):
                    if point == boundary:
                        raise OSError('injected crash')
                with self.assertRaises(OSError):
                    store.authorize(self.policy, self.authorization, fault=fault)
                store.authorize(self.policy, self.authorization)
                self.assertEqual(store.inspect()['requests'], {})
                self.assertEqual(len((store.root / 'task-journal.jsonl').read_text().splitlines()), 1)

    def test_changed_policy_and_deleted_history_cannot_reset_counters(self):
        self.authorized()
        changed = dict(self.policy, max_requests=50)
        auth = dict(self.authorization, policy_sha256=digest(changed))
        with self.assertRaisesRegex(FeedbackError, 'Frozen'):
            self.store.authorize(changed, auth)
        (self.store.root / 'task-journal.jsonl').unlink()
        with self.assertRaisesRegex(FeedbackError, 'Missing feedback history'):
            self.store.authorize(self.policy, self.authorization)

    def test_partial_tail_is_preserved_and_refused(self):
        self.authorized()
        path = self.store.root / 'task-journal.jsonl'
        with path.open('ab') as out:
            out.write(b'{"unknown_role_result":')
        before = path.read_bytes()
        with self.assertRaisesRegex(FeedbackError, 'Incomplete'):
            self.store.submit(self.source, self.request, a('developer'))
        self.assertEqual(path.read_bytes(), before)

    def test_source_changed_after_request_requires_reconciliation(self):
        self.authorized()
        changed = copy.deepcopy(self.source)
        changed['counters']['invocations'] += 1
        with self.assertRaisesRegex(FeedbackError, 'changed source'):
            self.store.submit(changed, self.request, a('developer'))

    def test_forged_evidence_and_unknown_obligation_are_refused(self):
        self.store.authorize(self.policy, self.authorization)
        request = copy.deepcopy(self.request)
        request['evidence_refs']['developer.log'] = 'd' * 64
        with self.assertRaisesRegex(FeedbackError, 'Unregistered'):
            self.store.submit(self.source, request, a('developer'))
        request = dict(self.request, obligation_refs=['AC-NEW'])
        with self.assertRaisesRegex(FeedbackError, 'obligations'):
            self.store.submit(self.source, request, a('developer'))

    def test_path_aliases_and_symlink_authority_are_refused(self):
        for path in ('../x.py', './tests/a.py', 'tests//a.py', '/tmp/a', 'tests/../a.py', 'a\\b.py'):
            with self.subTest(path=path), self.assertRaises(FeedbackError):
                literal_path(path)
        self.ticket.mkdir()
        (self.ticket / 'feedback').symlink_to(self.evidence, target_is_directory=True)
        with self.assertRaisesRegex(FeedbackError, 'Symlink'):
            FeedbackStore(self.ticket)

    def test_developer_cannot_propose_or_review_own_change(self):
        row = self.authorized()
        proposal = dict(route='AMENDMENT', requested_files=self.request['requested_files'], reason_code='scope')
        with self.assertRaisesRegex(FeedbackError, 'Developer cannot'):
            self.store.propose(row['request_id'], proposal, row['developer'], self.source)
        row = self.store.propose(row['request_id'], proposal, a('planner'), self.source)
        review = dict(proposal_sha256=row['proposal_sha256'], verdict='APPROVED', coverage_preserved=True, reason_code='coverage')
        for reviewer in (a('planner'), row['developer']):
            with self.assertRaisesRegex(FeedbackError, 'Independent'):
                self.store.review(row['request_id'], review, reviewer, self.source)

    def test_stale_review_and_coverage_weakening_are_refused(self):
        row = self.proposed()
        review = dict(proposal_sha256='0' * 64, verdict='APPROVED', coverage_preserved=True, reason_code='coverage')
        with self.assertRaisesRegex(FeedbackError, 'exact proposal'):
            self.store.review(row['request_id'], review, a('reviewer'), self.source)
        review.update(proposal_sha256=row['proposal_sha256'], coverage_preserved=False)
        with self.assertRaisesRegex(FeedbackError, 'weaken'):
            self.store.review(row['request_id'], review, a('reviewer'), self.source)

    def test_non_amendment_routes_never_dispatch_or_authorize_files(self):
        for route, disposition in [('PREREQUISITE', 'BLOCKED_PREREQUISITE'), ('ADVISORY', 'BACKLOG_ONLY'),
                                   ('REJECTED', 'REJECTED_NO_RETRY'), ('INCONCLUSIVE', 'OPERATOR_DECISION')]:
            with self.subTest(route=route):
                store = FeedbackStore(self.root / route / 'ISSUE22', clock=lambda: 100)
                store.authorize(self.policy, self.authorization)
                request = dict(self.request, cause='IMPROVEMENT') if route == 'ADVISORY' else self.request
                row = store.submit(self.source, request, a('developer'))
                row = store.propose(row['request_id'], dict(route=route, requested_files=[], reason_code='triaged'), a('planner'), self.source)
                store.review(row['request_id'], dict(proposal_sha256=row['proposal_sha256'], verdict='APPROVED',
                    coverage_preserved=True, reason_code='reviewed'), a('reviewer'), self.source)
                self.assertEqual(store.disposition(row['request_id']), disposition)
                self.assertFalse((store.ticket / 'execution').exists())

    def test_expired_policy_stops_without_mutation(self):
        self.authorized()
        expired = FeedbackStore(self.ticket, clock=lambda: 201)
        before = (expired.root / 'task-journal.jsonl').read_bytes()
        with self.assertRaisesRegex(FeedbackError, 'expired'):
            expired.submit(self.source, self.request, a('developer'))
        self.assertEqual((expired.root / 'task-journal.jsonl').read_bytes(), before)

    def test_public_projection_excludes_paths_evidence_and_role_output(self):
        self.proposed()
        text = json.dumps(self.store.public_projection())
        self.assertNotIn('developer.log', text)
        self.assertNotIn('test_reconciliation.py', text)
        self.assertNotIn('source', text)
        self.assertEqual(json.loads(text)['requests'][0]['disposition'], 'OPERATOR_DECISION')

    def test_role_intent_prevents_duplicate_dispatch_after_restart(self):
        row = self.authorized()
        call = self.store.reserve_role(row['request_id'], 'planner', a('planner'))
        restarted = FeedbackStore(self.ticket, clock=lambda: 100)
        with self.assertRaisesRegex(FeedbackError, 'already reserved'):
            restarted.reserve_role(row['request_id'], 'planner', a('another-planner'))
        with self.assertRaisesRegex(FeedbackError, 'Unknown feedback outcome'):
            restarted.reserve_role(row['request_id'], 'diagnosis', a('diagnosis'))
        response = dict(route='AMENDMENT', requested_files=self.request['requested_files'], reason_code='scope')
        receipt = restarted.record_role_result(call['operation_id'], response)
        self.assertEqual(receipt['status'], 'RETURNED')
        before = (self.store.root / 'task-journal.jsonl').read_bytes()
        self.assertEqual(restarted.record_role_result(call['operation_id'], response), receipt)
        self.assertEqual((self.store.root / 'task-journal.jsonl').read_bytes(), before)
        with self.assertRaisesRegex(FeedbackError, 'Conflicting'):
            restarted.record_role_result(call['operation_id'], dict(response, reason_code='changed'))

    def test_named_role_allowance_and_reduced_independence_stop_before_intent(self):
        policy = copy.deepcopy(self.policy)
        policy['role_calls']['planner'] = 0
        auth = dict(self.authorization, policy_sha256=digest(policy))
        self.store.authorize(policy, auth)
        row = self.store.submit(self.source, self.request, a('developer'))
        with self.assertRaisesRegex(FeedbackError, 'allowance exhausted'):
            self.store.reserve_role(row['request_id'], 'planner', a('planner'))
        self.assertEqual(self.store.inspect()['role_calls'], [])
        row = self.store.propose(row['request_id'], dict(route='AMENDMENT',
            requested_files=self.request['requested_files'], reason_code='scope'), a('planner'), self.source)
        reviewer = dict(a('reviewer'), engine='developer', model='developer')
        with self.assertRaisesRegex(FeedbackError, 'Reduced independence'):
            self.store.reserve_role(row['request_id'], 'reviewer', reviewer)
        self.assertEqual(self.store.inspect()['role_calls'], [])

    def test_returned_response_is_preserved_after_deadline_but_never_grants_dispatch(self):
        row = self.authorized()
        call = self.store.reserve_role(row['request_id'], 'planner', a('planner'))
        expired = FeedbackStore(self.ticket, clock=lambda: 201)
        expired.record_role_result(call['operation_id'], dict(route='INCONCLUSIVE'))
        self.assertEqual(expired.inspect()['role_calls'][0]['status'], 'RETURNED')
        with self.assertRaisesRegex(FeedbackError, 'expired'):
            expired.propose(row['request_id'], dict(route='INCONCLUSIVE', requested_files=[], reason_code='uncertain'),
                            a('planner'), self.source)

    def test_integer_truth_values_cannot_forge_operator_authorization(self):
        for key in ('authorized', 'source_fenced', 'schema_version'):
            auth = dict(self.authorization, **{key: 1 if key != 'schema_version' else True})
            with self.subTest(key=key), self.assertRaises(FeedbackError):
                self.store.authorize(self.policy, auth)

    def test_blocking_request_cannot_be_downgraded_to_optional_backlog(self):
        row = self.authorized()
        with self.assertRaisesRegex(FeedbackError, 'downgraded'):
            self.store.propose(row['request_id'], dict(route='ADVISORY', requested_files=[], reason_code='optional'),
                               a('planner'), self.source)
        self.assertEqual(self.store.inspect()['requests'][row['request_id']]['status'], 'REQUESTED')

    def test_changed_classification_cannot_reuse_an_existing_optional_decision(self):
        request = dict(self.request, cause='IMPROVEMENT')
        self.store.authorize(self.policy, self.authorization)
        row = self.store.submit(self.source, request, a('developer'))
        with self.assertRaisesRegex(FeedbackError, 'changed classification'):
            self.store.submit(self.source, self.request, a('developer'))
        self.assertEqual(len(self.store.inspect()['requests']), 1)
        self.assertEqual(self.store.inspect()['requests'][row['request_id']]['request']['cause'], 'IMPROVEMENT')

    def test_each_amendment_quota_is_enforced_before_native_disposition(self):
        for quota in ('max_amendments', 'max_amendments_per_task', 'max_added_files'):
            case = FeedbackTests(); case.setUp(); self.addCleanup(case.doCleanups)
            with self.subTest(quota=quota):
                case.policy[quota] = 1
                case.configure_new_files(['first.py', 'second.py'])
                first = case.approve_file('first.py')
                self.assertEqual(case.store.disposition(first['request_id']), 'NATIVE_AMENDMENT_REQUIRED')
                second = case.approve_file('second.py')
                self.assertEqual(case.store.disposition(second['request_id']), 'LIMIT_REACHED')
                state = case.store.inspect()
                self.assertEqual(len(state['amendments']), 1)
                self.assertEqual(state['added_files'], ['first.py'])

    def test_approved_recording_quota_replays_without_double_reservation(self):
        self.configure_new_files(['new.py'])
        row = self.approve_file('new.py')
        before = (self.store.root / 'task-journal.jsonl').read_bytes()
        same = self.store.review(row['request_id'], row['review'], row['reviewer'], self.source)
        self.assertEqual(row, same)
        self.assertEqual((self.store.root / 'task-journal.jsonl').read_bytes(), before)
        restarted = FeedbackStore(self.ticket, clock=lambda: 100)
        self.assertEqual(restarted.disposition(row['request_id']), 'NATIVE_AMENDMENT_REQUIRED')
        self.assertEqual(len(restarted.inspect()['amendments']), 1)

    def test_completed_production_owner_requires_native_revalidation(self):
        self.source['journal']['tasks']['T01'] = dict(status='COMPLETED', attempts=1, changed_files=['app.py'])
        self.configure_new_files(['app.py'])
        row = self.approve_file('app.py')
        self.assertEqual(self.store.disposition(row['request_id']), 'OWNER_REVALIDATION_REQUIRED')
        self.assertEqual(row['path_classes']['app.py']['owners'], ['T01'])
        self.assertEqual(self.store.inspect()['amendments'], [])

    def test_completed_test_cannot_be_delegated_by_a_coverage_boolean(self):
        self.source['journal']['tasks']['T01'] = dict(status='COMPLETED', attempts=1,
            changed_files=['tests/test_reconciliation.py'])
        self.configure_new_files(['tests/test_reconciliation.py'])
        row = self.approve_file('tests/test_reconciliation.py')
        self.assertEqual(self.store.disposition(row['request_id']), 'OPERATOR_DECISION')
        self.assertEqual(row['path_classes']['tests/test_reconciliation.py']['kind'], 'COMPLETED_TEST')
        self.assertEqual(self.store.inspect()['amendments'], [])

    def test_planner_and_diagnosis_independence_fail_before_reservation(self):
        row = self.authorized()
        for role in ('planner', 'diagnosis'):
            metadata = dict(a(role), engine=row['developer']['engine'], model=row['developer']['model'])
            with self.subTest(role=role), self.assertRaisesRegex(FeedbackError, 'Reduced independence'):
                self.store.reserve_role(row['request_id'], role, metadata)
        unknown = dict(a('planner'), model='UNKNOWN')
        with self.assertRaisesRegex(FeedbackError, 'Reduced independence'):
            self.store.reserve_role(row['request_id'], 'planner', unknown)
        self.assertEqual(self.store.inspect()['role_calls'], [])

    def test_diagnosis_participates_in_final_independence_assessment(self):
        self.configure_new_files(['new.py'])
        self.store.authorize(self.policy, self.authorization)
        row = self.store.submit(self.source, dict(self.request, requested_files=['new.py']), a('developer'))
        diagnosis = self.store.reserve_role(row['request_id'], 'diagnosis', a('diagnosis'))
        self.store.record_role_result(diagnosis['operation_id'], dict(reason_code='missing_target'))
        row = self.approve_file('new.py', reviewer=dict(a('reviewer'), engine='diagnosis', model='diagnosis'))
        self.assertEqual(self.store.disposition(row['request_id']), 'OPERATOR_DECISION')
        self.assertTrue(row['independence']['reduced_independence'])
        self.assertEqual(self.store.inspect()['amendments'], [])

    def test_failed_test_set_is_derived_not_chosen_by_the_caller(self):
        self.store.authorize(self.policy, self.authorization)
        with self.assertRaisesRegex(FeedbackError, 'host-derived'):
            self.store.submit(self.source, self.request, a('developer'), failed_tests=['invented.Case.test_one'])
        self.assertEqual(self.store.inspect()['requests'], {})
        row = self.store.submit(self.source, self.request, a('developer'), failed_tests=[])
        self.assertEqual(row['failed_tests'], [])

    def test_requirement_conflict_is_always_an_operator_product_decision(self):
        request = dict(self.request, cause='REQUIREMENT_CONFLICT')
        self.store.authorize(self.policy, self.authorization)
        row = self.store.submit(self.source, request, a('developer'))
        self.assertEqual(self.store.disposition(row['request_id']), 'OPERATOR_DECISION')
        for route, files in [('AMENDMENT', request['requested_files']), ('ADVISORY', [])]:
            with self.subTest(route=route), self.assertRaises(FeedbackError):
                self.store.propose(row['request_id'], dict(route=route, requested_files=files,
                    reason_code='new_requirement'), a('planner'), self.source)

    def test_host_outside_failure_opens_only_a_distinct_non_authorizing_hypothesis(self):
        ref = '03-dev/tasks/T08/attempt-2/verification-1/check-0.log'
        path = self.evidence / ref; path.parent.mkdir(parents=True)
        path.write_text('FAIL: test_case (test_reconciliation.Case.test_case)\n'
                        'Traceback (most recent call last):\nAssertionError\n'
                        'Ran 1 test in 0.1s\nFAILED (failures=1)\n')
        self.source['files'][ref] = file_hash(path)
        failure = self.source['journal']['tasks']['T08']['failed_attempts'][-1]
        failure.update(category='CHECK_FAILURE', tree='1' * 40,
            checks=[dict(check_id='CHECK-TESTS', exit_code=1, empty_suite=False,
                         log_ref=ref.removeprefix('03-dev/tasks/'), sha256=file_hash(path))])
        self.policy['source_binding_sha256'] = digest(source_binding(self.source))
        self.authorization['policy_sha256'] = digest(self.policy)
        row = self.authorized()
        self.assertEqual(row['origin'], 'HOST_HINT')
        self.assertEqual(row['failure_hint']['classification'], 'ALL_OUTSIDE')
        self.assertEqual(row['failed_tests'], ['test_reconciliation.Case.test_case'])
        self.assertFalse(row['failure_hint']['authorization'])
        later = copy.deepcopy(self.source)
        later['journal']['tasks']['T08']['attempts'] = 3
        later['journal']['tasks']['T08']['failed_attempts'].append(dict(failure,
            category='SCOPE_REQUIRED', capture_sha256='e' * 64, checks=[]))
        self.assertEqual(source_binding(later)['origin'], 'DEVELOPER')
        later_row = self.store.submit(later, self.request, a('developer'))
        self.assertEqual(later_row['cause_id'], row['cause_id'])
        self.assertNotEqual(later_row['request_id'], row['request_id'])
        self.assertTrue(later_row['source_reconciliation_required'])
        self.assertEqual(self.store.disposition(later_row['request_id']), 'OPERATOR_DECISION')
        with self.assertRaisesRegex(FeedbackError, 'native reconciliation'):
            self.store.reserve_role(later_row['request_id'], 'planner', a('planner'))
        self.assertEqual(len(self.store.inspect()['requests']), 2)


if __name__ == '__main__':
    unittest.main()
