import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


class SchemaTests(unittest.TestCase):
    REQUIRED = [
        'id', 'severity', 'classification', 'evidence_kind', 'scenario', 'rationale',
        'task_attribution', 'file_refs', 'obligation_refs', 'evidence_refs',
        'violates_existing_obligation', 'introduces_regression',
        'necessary_for_requested_correctness',
    ]
    ARRAYS = ('task_attribution', 'file_refs', 'obligation_refs', 'evidence_refs')
    REFS = ('task_attribution', 'obligation_refs', 'evidence_refs')

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'schemas' / 'gate-findings.schema.json'
        cls.schema = json.loads(path.read_text())
        cls.validator = Draft202012Validator(cls.schema)

    def report(self):
        return {
            'schema_version': '1.0', 'reviewed_commit': 'a' * 40, 'source_gate': 'qa',
            'findings': [{
                'id': 'F1', 'severity': 'high',
                'classification': 'mandatory_violation_or_regression',
                'evidence_kind': 'observed', 'scenario': 'Observed failure',
                'rationale': 'Violates AC-1', 'task_attribution': ['T1'],
                'file_refs': ['src/example.py'], 'obligation_refs': ['AC-1'],
                'evidence_refs': ['ev/1'], 'violates_existing_obligation': True,
                'introduces_regression': False, 'necessary_for_requested_correctness': False,
            }],
        }

    def assert_error(self, report, keyword, path):
        errors = list(self.validator.iter_errors(report))
        self.assertTrue(any(error.validator == keyword and list(error.path) == path
                            for error in errors),
                        [(error.validator, list(error.path)) for error in errors])
        return errors

    def assert_valid(self, report):
        self.assertEqual(list(self.validator.iter_errors(report)), [])

    def test_schema_is_valid_draft_2020_12_and_closed(self):
        Draft202012Validator.check_schema(self.schema)
        self.assertEqual(self.schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        self.assertEqual(self.schema['properties']['schema_version'], {'const': '1.0'})
        self.assertEqual(self.schema['required'],
                         ['schema_version', 'reviewed_commit', 'source_gate', 'findings'])
        finding = self.schema['properties']['findings']['items']
        self.assertEqual(finding['required'], self.REQUIRED)
        self.assertEqual(set(finding['properties']), set(self.REQUIRED))

        def walk(node):
            if isinstance(node, dict):
                self.assertNotIn('$ref', node)
                self.assertNotIn('$id', node)
                if node.get('type') == 'object':
                    self.assertIs(node.get('additionalProperties'), False)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.schema)
        self.assert_valid(self.report())
        for field, value, keyword in (
            ('schema_version', '2.0', 'const'),
            ('reviewed_commit', 'a' * 41, 'maxLength'),
            ('reviewed_commit', 'A' * 40, 'pattern'),
            ('source_gate', 'unknown', 'enum'),
        ):
            with self.subTest(field=field, value=value):
                report = self.report()
                report[field] = value
                self.assert_error(report, keyword, [field])

    def test_commit_trailing_newline_rejected_by_schema_alone(self):
        report = self.report()
        report['reviewed_commit'] = 'a' * 40 + '\n'
        errors = self.assert_error(report, 'maxLength', ['reviewed_commit'])
        self.assertEqual([(error.validator, list(error.path)) for error in errors],
                         [('maxLength', ['reviewed_commit'])])

    def test_commit_width_bounds(self):
        for width, keyword in ((39, 'minLength'), (41, 'maxLength'), (40, None)):
            with self.subTest(width=width):
                report = self.report()
                report['reviewed_commit'] = 'a' * width
                if keyword:
                    self.assert_error(report, keyword, ['reviewed_commit'])
                else:
                    self.assert_valid(report)

    def test_each_required_field_missing_and_wrong_types(self):
        for field in self.REQUIRED:
            with self.subTest(missing=field):
                report = self.report()
                del report['findings'][0][field]
                errors = self.assert_error(report, 'required', ['findings', 0])
                self.assertEqual(len(errors), 1)
                self.assertIn(field, errors[0].validator_value)
                self.assertNotIn(field, errors[0].instance)
            with self.subTest(wrong_type=field):
                report = self.report()
                report['findings'][0][field] = None
                # D3's enum-only fields reject all non-members via enum.
                keyword = 'enum' if field in ('severity', 'classification', 'evidence_kind') else 'type'
                self.assert_error(report, keyword, ['findings', 0, field])
        for field in ('severity', 'classification', 'evidence_kind'):
            with self.subTest(enum=field):
                report = self.report()
                report['findings'][0][field] = 'unknown'
                self.assert_error(report, 'enum', ['findings', 0, field])
        for field in self.ARRAYS:
            with self.subTest(array_item_type=field):
                report = self.report()
                report['findings'][0][field] = [42]
                self.assert_error(report, 'type', ['findings', 0, field, 0])

    def test_limits_just_over_boundaries(self):
        report = self.report()
        report['findings'] = [copy.deepcopy(report['findings'][0]) for _ in range(201)]
        self.assert_error(report, 'maxItems', ['findings'])
        cases = [('id', 'a' * 65, 'maxLength'),
                 ('scenario', 'a' * 4001, 'maxLength'),
                 ('rationale', 'a' * 4001, 'maxLength')]
        cases.extend((field, ['ref'] * 51, 'maxItems') for field in self.ARRAYS)
        for field, value, keyword in cases:
            with self.subTest(field=field):
                report = self.report()
                report['findings'][0][field] = value
                self.assert_error(report, keyword, ['findings', 0, field])
        for field, width in [('file_refs', 513)] + [(field, 129) for field in self.REFS]:
            with self.subTest(item=field):
                report = self.report()
                report['findings'][0][field] = ['a' * width]
                self.assert_error(report, 'maxLength', ['findings', 0, field, 0])

    def test_limits_exact_boundaries(self):
        report = self.report()
        finding = report['findings'][0]
        finding.update(id='a' * 64, scenario='s' * 4000, rationale='r' * 4000)
        for field in self.REFS:
            finding[field] = ['r' * 128] * 50
        finding['file_refs'] = ['p' * 512] * 50
        report['findings'] = [copy.deepcopy(finding) for _ in range(200)]
        self.assert_valid(report)

    def test_minimum_bounds_and_id_pattern(self):
        for field in ('id', 'scenario', 'rationale'):
            with self.subTest(empty=field):
                report = self.report()
                report['findings'][0][field] = ''
                self.assert_error(report, 'minLength', ['findings', 0, field])
        for field in self.REFS:
            with self.subTest(empty_ref=field):
                report = self.report()
                report['findings'][0][field] = ['']
                self.assert_error(report, 'minLength', ['findings', 0, field, 0])
        report = self.report()
        report['findings'][0]['evidence_refs'] = []
        self.assert_error(report, 'minItems', ['findings', 0, 'evidence_refs'])
        for value in ('_bad', 'a/b', 'a b'):
            with self.subTest(id=value):
                report = self.report()
                report['findings'][0]['id'] = value
                self.assert_error(report, 'pattern', ['findings', 0, 'id'])
        report = self.report()
        report['findings'][0].update(id='a', scenario='s', rationale='r',
                                     task_attribution=[], obligation_refs=[], file_refs=[''])
        self.assert_valid(report)
        report['findings'] = []
        self.assert_valid(report)

    def test_unknown_fields_are_rejected(self):
        for path in ([], ['findings', 0]):
            with self.subTest(path=path):
                report = self.report()
                target = report if not path else report['findings'][0]
                target['unexpected'] = True
                self.assert_error(report, 'additionalProperties', path)
