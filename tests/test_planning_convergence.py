import copy
import json
import tempfile
import unittest
from pathlib import Path

from ssdlc_controller import assert_controller, controller_snapshot
from ssdlc_planning import PlanRepair, validate_plan_review
from ssdlc_tasks import PlanError, TaskError, git
from test_task_bootstrap import RepositoryTests


def finding(identifier='F1', **kwargs):
    return {'finding_id': identifier, 'status': 'OPEN', 'severity': 'BLOCKING',
            'category': 'PLAN_DEFECT', 'requirement_refs': ['AC-1'], 'task_ids': ['T1'],
            'message': 'Missing recovery assertion', 'evidence': 'T1 does not assert recovery after snapshot failure',
            'requested_change': 'Inject failure after journal append and assert recovered state',
            'new_issue_reason': 'INITIAL_REVIEW', **kwargs}


def report(findings):
    blockers = [f for f in findings if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
    return json.dumps({'verdict': 'REVISION_REQUIRED' if blockers else 'APPROVED', 'findings': findings})


class StructuredReviewTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'tasks': [{'task_id': 'T1'}]}

    def validate(self, raw, history=None):
        return validate_plan_review(raw, self.plan, ['AC-1', 'SEC-10'], history or [])

    def test_optional_improvement_cannot_block_or_become_mandatory(self):
        advice = finding(severity='ADVISORY', category='IMPROVEMENT', requirement_refs=[])
        self.assertEqual(self.validate(report([advice]))['verdict'], 'APPROVED')
        with self.assertRaises(PlanError):
            self.validate(report([finding(category='IMPROVEMENT')]))
        with self.assertRaises(PlanError):
            self.validate(report([finding(requirement_refs=[])]))

    def test_history_requires_explicit_disposition_even_after_structural_failure(self):
        history = [{'assessment': self.validate(report([finding()]))}, {'diagnostics': 'missing check'}]
        with self.assertRaisesRegex(PlanError, 'omitted prior'):
            self.validate(report([]), history)
        resolved = finding(status='RESOLVED', evidence='T1 now asserts journal replay after durable append')
        self.assertEqual(self.validate(report([resolved]), history)['verdict'], 'APPROVED')

    def test_old_finding_cannot_be_relabelled_to_bypass_a_blocker(self):
        history = [{'assessment': self.validate(report([finding()]))}]
        with self.assertRaisesRegex(PlanError, 'reassigned'):
            self.validate(report([finding(severity='ADVISORY')]), history)
        with self.assertRaisesRegex(PlanError, 'reassigned'):
            self.validate(report([finding(requirement_refs=['SEC-10'])]), history)

    def test_new_findings_require_reason_and_valid_references(self):
        history = [{'assessment': self.validate(report([finding()]))}]
        with self.assertRaisesRegex(PlanError, 'explain'):
            self.validate(report([finding(), finding('F2')]), history)
        revised = finding('F2', new_issue_reason='INTRODUCED_BY_REVISION')
        self.validate(report([finding(), revised]), history)
        for kwargs in ({'task_ids': ['T99']}, {'requirement_refs': ['AC-404']}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(PlanError, 'unknown'):
                self.validate(report([finding(**kwargs)]))

    def test_duplicate_ids_contradictory_verdict_and_marker_only_are_invalid(self):
        for raw in (report([finding(), finding()]),
                    json.dumps({'verdict': 'APPROVED', 'findings': [finding()]}),
                    'TASK_PLAN_STATUS: APPROVED'):
            with self.subTest(raw=raw), self.assertRaises(PlanError):
                self.validate(raw)

    def test_approved_quality_obligations_can_block_without_inventing_ac_ids(self):
        quality = {'standards': [{'standard_id': 'STYLE-1'}], 'checks': [{'check_id': 'LINT'}]}
        for refs in ({'standard_refs': ['STYLE-1']}, {'quality_check_refs': ['LINT']}):
            raw = report([finding(requirement_refs=[], **refs)])
            verdict = validate_plan_review(raw, self.plan, ['AC-1'], [], quality)
            self.assertEqual(verdict['verdict'], 'REVISION_REQUIRED')
            with self.assertRaisesRegex(PlanError, 'unknown standards or quality checks'):
                validate_plan_review(raw, self.plan, ['AC-1'], [])


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.generations = []
        self.review_histories = []
        self.inputs = {'context': {'requirement_catalogue': {'required_ids': ['AC-1', 'SEC-10']}}}
        self.plan = {'tasks': [{'task_id': 'T1'}]}

    def repair(self, answers, **kwargs):
        responses = iter(answers)
        def generate(attempt, inputs, previous):
            self.generations.append(previous)
            return json.dumps(self.plan)
        def review(plan, history):
            self.review_histories.append(copy.deepcopy(history))
            return next(responses)
        return PlanRepair(self.root, self.inputs, lambda _: None, generate, review, lambda: None,
                          structured_reviews=True, **kwargs)

    def session(self):
        return json.loads((self.root/'task-planning/session.json').read_text())

    def test_both_roles_receive_all_feedback_and_keep_explicit_closures(self):
        first = finding()
        second = finding('F2', new_issue_reason='MISSED_EARLIER')
        closed = finding(status='RESOLVED', evidence='T1 now contains the required assertions')
        final = {**second, 'status': 'RESOLVED', 'evidence': 'Additional edge case is covered'}
        repair = self.repair([report([first]), report([closed, second]), report([closed, final])])
        self.assertEqual(repair.run(), self.plan)
        self.assertEqual([len(h) for h in self.review_histories], [0, 1, 2])
        self.assertEqual(len(self.generations[-1]['history']), 2)
        self.assertEqual(self.generations[-1]['history'][0]['assessment']['findings'][0]['finding_id'], 'F1')
        self.assertEqual(self.session()['limits']['review_repairs'], 4)
        # A resumed approval validates the saved review and performs no new model calls.
        self.assertEqual(self.repair([]).run(), self.plan)
        self.assertEqual(len(self.generations), 3)

    def test_stagnation_is_persistent_and_never_implies_approval(self):
        answers = [report([finding()])] * 3
        for _ in range(2):
            with self.assertRaisesRegex(PlanError, 'STAGNATION'):
                self.repair(answers).run()
        self.assertEqual(len(self.generations), 3)
        self.assertEqual(self.session()['attempts'][-1]['failure_kind'], 'STAGNATION')

    def test_environment_prerequisite_is_not_sent_to_developer_or_blindly_replanned(self):
        problem = finding(category='ENVIRONMENT_PREREQUISITE', requirement_refs=['SEC-10'],
                          evidence='Runner lacks the declared execution prerequisite')
        for _ in range(2):
            with self.assertRaisesRegex(PlanError, 'ENVIRONMENT_PREREQUISITE'):
                self.repair([report([problem])]).run()
        self.assertEqual(len(self.generations), 1)
        self.assertEqual(self.session()['attempts'][-1]['status'], 'REJECTED')

    def test_invalid_reviewer_output_is_not_misclassified_as_a_plan_defect(self):
        with self.assertRaisesRegex(PlanError, 'Invalid review protocol'):
            self.repair(['TASK_PLAN_STATUS: APPROVED'] * 2).run()
        with self.assertRaisesRegex(PlanError, 'REVIEW_PROTOCOL'):
            self.repair([]).run()
        self.assertEqual(len(self.generations), 1)

    def test_protocol_repair_keeps_same_candidate_and_preserves_all_responses(self):
        self.repair(['TASK_PLAN_STATUS: APPROVED', report([])]).run()
        self.assertEqual(len(self.generations), 1)
        self.assertEqual(len(self.review_histories), 2)
        self.assertIn('review_protocol_errors', self.review_histories[-1][-1])
        record = self.session()['attempts'][0]
        self.assertEqual(record['review_invocations'], 2)
        response = self.root/'task-planning'/record['review-response-1']
        self.assertEqual(response.read_text(), 'TASK_PLAN_STATUS: APPROVED')
        response.write_text('tampered invalid response')
        with self.assertRaisesRegex(PlanError, 'evidence changed'):
            self.repair([]).run()

    def test_persisted_lower_limit_is_not_silently_extended(self):
        f1, f2 = finding(), finding('F2')
        f1_closed = finding(status='RESOLVED')
        f2['new_issue_reason'] = 'INTRODUCED_BY_REVISION'
        with self.assertRaisesRegex(PlanError, 'Semantic.*exhausted'):
            self.repair([report([f1]), report([f1_closed, f2])], review_repairs=1).run()
        with self.assertRaisesRegex(PlanError, 'Semantic.*exhausted'):
            self.repair([]).run()
        self.assertEqual(self.session()['limits']['review_repairs'], 1)
        self.assertEqual(len(self.generations), 2)

    def test_progress_can_use_four_repairs_but_never_an_unbounded_fifth(self):
        reports, seen = [], []
        for index in range(5):
            seen = [{**item, 'status': 'RESOLVED', 'evidence': 'Current plan now exercises this assertion'} for item in seen]
            seen.append(finding(f'F{index}', new_issue_reason='INITIAL_REVIEW' if index == 0 else 'INTRODUCED_BY_REVISION'))
            reports.append(report(seen))
        with self.assertRaisesRegex(PlanError, 'Semantic.*exhausted'):
            self.repair(reports).run()
        self.assertEqual(len(self.generations), 5)
        self.assertEqual(self.session()['attempts'][-1]['status'], 'REJECTED')


class ControllerIntegrityTests(RepositoryTests):
    def test_committed_change_clean_checkout_is_detected(self):
        snapshot = controller_snapshot(self.repo)
        assert_controller(self.repo, snapshot)
        (self.repo/'app.py').write_text('value = 2\n')
        git(self.repo, 'add', 'app.py')
        git(self.repo, 'commit', '-m', 'controller changed')
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        with self.assertRaisesRegex(TaskError, 'controller changed'):
            assert_controller(self.repo, snapshot)

    def test_dirty_deleted_and_new_controller_files_are_detected(self):
        snapshot = controller_snapshot(self.repo)
        for change in ('dirty', 'deleted', 'untracked'):
            with self.subTest(change=change):
                if change == 'dirty':
                    (self.repo/'app.py').write_text('value = 99\n')
                elif change == 'deleted':
                    (self.repo/'app.py').unlink()
                else:
                    (self.repo/'extra.py').write_text('new runtime code\n')
                with self.assertRaises(TaskError):
                    assert_controller(self.repo, snapshot)
                git(self.repo, 'restore', 'app.py')
                if (self.repo/'extra.py').exists():
                    (self.repo/'extra.py').unlink()
        assert_controller(self.repo, snapshot)
