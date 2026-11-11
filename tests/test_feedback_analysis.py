"""Host scope hints never classify a failing test as an incorrect test."""
import unittest

from ssdlc_feedback_analysis import (classify_failures, unittest_failures,
                                     independence, impact_attribution)
from ssdlc_tasks import TaskError, digest


def log(*modules):
    return '\n'.join('FAIL: test_case (' + module + '.Case.test_case)\nTraceback (most recent call last):\n'
                     '  File "/malicious/arbitrary.py", line 3, in test_case\nAssertionError' for module in modules) + (
                     '\nRan 5 tests in 0.1s\nFAILED (failures=' + str(len(modules)) + ')\n')


class FeedbackAnalysisTests(unittest.TestCase):
    def classify(self, text, targets=(), protected=()):
        return classify_failures([dict(check_id='CHECK-TESTS', exit_code=1, empty_suite=False)],
                                 lambda c: text, ['tests/test_a.py', 'tests/test_b.py'], targets, protected)

    def test_all_outside_is_only_a_scope_hint(self):
        result = self.classify(log('test_a'))
        self.assertEqual(result['classification'], 'ALL_OUTSIDE')
        self.assertTrue(result['scope_candidate'])
        self.assertFalse(result['authorization'])
        self.assertEqual(result['failures'][0]['path'], 'tests/test_a.py')

    def test_mixed_inside_protected_and_unknown_are_distinct(self):
        cases = [(log('test_a', 'test_b'), ['tests/test_a.py'], [], 'MIXED'),
                 (log('test_a'), ['tests/test_a.py'], [], 'INSIDE'),
                 (log('test_a'), [], ['tests/test_a.py'], 'PROTECTED'),
                 (log('unknown'), [], [], 'UNRESOLVED'), ('truncated', [], [], 'UNRESOLVED')]
        for text, targets, protected, expected in cases:
            with self.subTest(expected=expected):
                result = self.classify(text, targets, protected)
                self.assertEqual(result['classification'], expected)
                self.assertFalse(result['scope_candidate'])

    def test_ambiguous_module_and_summary_mismatch_are_not_scope_proof(self):
        self.assertIsNone(unittest_failures(log('test_a'), ['one/test_a.py', 'two/test_a.py']))
        self.assertIsNone(unittest_failures(log('test_a').replace('failures=1', 'failures=2'), ['tests/test_a.py']))
        self.assertIsNone(unittest_failures(log('test_a').replace('test_a.Case.test_case', '../test_a'), ['tests/test_a.py']))

    def test_unresolved_check_prevents_all_outside_classification(self):
        checks = [dict(check_id=n, exit_code=1, empty_suite=False) for n in ('unit', 'lint')]
        result = classify_failures(checks, lambda c: log('test_a') if c['check_id'] == 'unit' else 'syntax error',
                                   ['tests/test_a.py'], [], [])
        self.assertEqual(result['classification'], 'UNRESOLVED')
        self.assertEqual(result['unresolved_checks'], ['lint'])

    def test_model_metadata_tracks_reduced_independence(self):
        def a(name, model):
            return dict(identity=name, context_id=name, engine='codex', model=model)
        result = independence(a('dev', 'same'), a('planner', 'other'), a('reviewer', 'same'))
        self.assertTrue(result['distinct_roles'])
        self.assertTrue(result['reduced_independence'])
        self.assertFalse(independence(a('dev', 'x'), a('dev', 'y'))['distinct_roles'])
        self.assertTrue(independence(a('dev', 'unknown'), a('reviewer', 'y'))['reduced_independence'])

    def test_impact_report_absence_is_unknown_not_unpredictable(self):
        result = impact_attribution(None, None, ['tests/test_a.py'])
        self.assertEqual(result['status'], 'UNKNOWN')
        report = dict(references=[dict(path='tests/test_a.py', references=['changed_symbol'])])
        result = impact_attribution(report, digest(report), ['tests/test_a.py', 'other.py'])
        self.assertEqual(result['status'], 'DETECTABLE')
        self.assertEqual(result['unmatched_files'], ['other.py'])
        self.assertFalse(result['complete_dependency_proof'])
        with self.assertRaises(TaskError):
            impact_attribution(report, '0' * 64, ['tests/test_a.py'])


if __name__ == '__main__':
    unittest.main()
