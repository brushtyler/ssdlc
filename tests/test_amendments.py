import copy
import json
from pathlib import Path
import tempfile
import unittest

from ssdlc_amendments import fixture_impact, validate_amendment
from ssdlc_tasks import PlanError


class AmendmentTests(unittest.TestCase):
    def setUp(self):
        self.old = {'plan_revision': 1, 'tasks': [
            {'task_id': 'A', 'depends_on': []}, {'task_id': 'B', 'depends_on': ['A']},
            {'task_id': 'C', 'depends_on': ['B']}]}
        self.new = copy.deepcopy(self.old)
        self.new['plan_revision'] = 2
        self.report = json.dumps({'verdict': 'APPROVED', 'findings': []})

    def check(self, lineage=None):
        return validate_amendment(self.old, self.new, self.report, lambda p: p['tasks'], [], {}, lineage=lineage)

    def test_preserves_transitive_order(self):
        self.assertEqual(self.check()['ordered_task_ids'], ['A', 'B', 'C'])
        self.new['tasks'][2]['depends_on'] = ['A']
        with self.assertRaisesRegex(PlanError, 'transitive ordering'):
            self.check()

    def test_merge_preserves_remaining_dependencies(self):
        self.new['tasks'] = [{'task_id': 'AB', 'depends_on': []}, {'task_id': 'C', 'depends_on': ['AB']}]
        self.check({'A': ['AB'], 'B': ['AB'], 'C': ['C']})
        self.new['tasks'][1]['depends_on'] = []
        with self.assertRaises(PlanError):
            self.check({'A': ['AB'], 'B': ['AB'], 'C': ['C']})

    def test_split_requires_each_predecessor(self):
        self.new['tasks'] = [{'task_id': 'A1', 'depends_on': []}, {'task_id': 'A2', 'depends_on': []},
                             {'task_id': 'B', 'depends_on': ['A1', 'A2']}, {'task_id': 'C', 'depends_on': ['B']}]
        self.check({'A': ['A1', 'A2'], 'B': ['B'], 'C': ['C']})
        self.new['tasks'][2]['depends_on'].remove('A2')
        with self.assertRaises(PlanError):
            self.check({'A': ['A1', 'A2'], 'B': ['B'], 'C': ['C']})

    def test_missing_lineage_and_skipped_revision_refused(self):
        self.new['tasks'].pop()
        with self.assertRaises(PlanError):
            self.check()
        self.new = copy.deepcopy(self.old)
        with self.assertRaises(PlanError):
            self.check()

    def test_review_cannot_be_omitted(self):
        self.report = 'The model said it approved'
        with self.assertRaises(PlanError):
            self.check()

    def test_fixture_strings_and_imports_are_detected_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'test_fixture.py').write_text('from unittest.mock import patch\npatch("orchestrator._task_mode")\n')
            (root / 'broken.py').write_text('not valid python !!!')
            (root / 'ignored.py').symlink_to(root / 'test_fixture.py')
            report = fixture_impact(root, ['orchestrator.py'], ['_task_mode'])
            self.assertEqual(report['references'][0]['path'], 'test_fixture.py')
            self.assertIn('_task_mode', report['references'][0]['references'])
            self.assertEqual(report['parse_errors'], ['broken.py'])
            self.assertFalse(report['complete_dependency_proof'])
