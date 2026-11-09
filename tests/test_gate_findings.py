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


import ast
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from itertools import product
from types import MappingProxyType
from unittest.mock import patch

import ssdlc_findings as gf


def context():
    return gf.TrustedContext('a' * 40, {'T1', 'T2'}, {'AC-1', 'SEC-1'}, {'ev/1'})


def report(classification='mandatory_violation_or_regression', **changes):
    result = SchemaTests().report()
    f = result['findings'][0]
    f.update(classification=classification, violates_existing_obligation=False,
             necessary_for_requested_correctness=classification == 'necessary_gap')
    if classification == 'mandatory_violation_or_regression':
        f['violates_existing_obligation'] = True
    f.update(changes)
    return result


class BehaviorCase(unittest.TestCase):
    def errors(self, value, ctx=None):
        with self.assertRaises(gf.FindingsError) as caught:
            gf.assess_report(value, context() if ctx is None else ctx)
        return caught.exception.diagnostics

    def codes(self, value):
        return {d['code'] for d in self.errors(value)}


class ValidationTests(BehaviorCase):
    def test_required_and_type_diagnostic_codes(self):
        for name in SchemaTests.REQUIRED:
            r = report()
            del r['findings'][0][name]
            self.assertIn('missing_field', self.codes(r))
        self.assertIn('invalid_type', self.codes(report(scenario=5)))
        self.assertIn('invalid_value', self.codes(report(severity='wrong')))

    def test_unknown_field_report_and_finding_level(self):
        r = report(extra=True)
        r['extra'] = True
        self.assertEqual({d['path'] for d in self.errors(r)}, {'/extra', '/findings/0/extra'})

    def test_duplicate_ids(self):
        r = report()
        r['findings'] += [report(id='other')['findings'][0], copy.deepcopy(r['findings'][0])]
        d = next(d for d in self.errors(r) if d['code'] == 'duplicate_id')
        self.assertEqual(d['indices'], [0, 2])
        self.assertEqual(d['finding_id'], 'F1')

    def test_colliding_display_paths_preserve_each_unknown_field_diagnostic(self):
        r = report()
        names = ('a' * 127 + '/', 'a' * 127 + '~')
        for name in names:
            r[name] = True
        diagnostics = self.errors(r)
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual({d['code'] for d in diagnostics}, {'unknown_field'})
        self.assertEqual({d['value'] for d in diagnostics}, set(names))
        reordered = dict(reversed(list(r.items())))
        self.assertEqual(diagnostics, self.errors(reordered))

    def test_stale_commit(self):
        r = report()
        r['reviewed_commit'] = 'b' * 40
        self.assertEqual(self.codes(r), {'stale_commit'})

    def test_malformed_commit_not_reported_as_stale(self):
        for commit in ('a' * 40 + '\n', 'bad', 5):
            r = report()
            r['reviewed_commit'] = commit
            self.assertNotIn('stale_commit', self.codes(r))
        self.assertIn('invalid_value', self.codes(report(id='abc\n')))

    def test_unknown_references_one_diagnostic_each(self):
        ds = self.errors(report(task_attribution=['T9'], obligation_refs=['AC-9'], evidence_refs=['secret']))
        self.assertEqual(len(ds), 3)
        for d in ds:
            self.assertEqual(d['finding_id'], 'F1')
            self.assertIn('field', d)
            self.assertIn('value', d)

    def test_empty_evidence_refs_rejected(self):
        self.assertEqual(self.codes(report(evidence_refs=[])), {'missing_evidence'})

    def test_optional_with_each_claim_at_each_severity(self):
        for claim, severity in product(gf._CLAIMS, ('critical', 'high', 'medium', 'low', 'info')):
            with self.subTest(claim=claim, severity=severity):
                self.assertIn('misclassified_optional', self.codes(report('optional_improvement', severity=severity, **{claim: True})))

    def test_mandatory_requires_violation_or_regression(self):
        self.assertIn('inconsistent_mandatory', self.codes(report(violates_existing_obligation=False)))

    def test_gap_rules(self):
        for changes in ({'necessary_for_requested_correctness': False},
                        {'violates_existing_obligation': True}, {'introduces_regression': True}):
            self.assertIn('inconsistent_gap', self.codes(report('necessary_gap', **changes)))

    def test_hypothesis_matrix(self):
        for classification in ('mandatory_violation_or_regression', 'necessary_gap', 'optional_improvement', 'uncertain_needs_diagnosis'):
            r = report(classification, evidence_kind='hypothesis')
            if classification in ('mandatory_violation_or_regression', 'necessary_gap'):
                self.assertIn('hypothesis_classification', self.codes(r))
            else:
                gf.validate_report(r, context())

    def test_uncertain_accepts_all_claim_combinations(self):
        for values in product((False, True), repeat=3):
            r = report('uncertain_needs_diagnosis', **dict(zip(gf._CLAIMS, values)))
            self.assertEqual(gf.validate_report(r, context()).to_dict(), r)

    def test_malformed_input_and_context(self):
        class Hostile(Mapping):
            def __iter__(self):
                raise RuntimeError('secret')
            def __getitem__(self, key):
                raise RuntimeError('secret')
            def __len__(self):
                return 1
        class HostileGet(Hostile):
            def __iter__(self):
                return iter(['key'])
        for value in (None, [], '', 3, Hostile(), HostileGet()):
            self.errors(value)
        gf.validate_report(MappingProxyType(report()), context())
        for field in ('known_task_ids', 'obligation_ids', 'evidence_refs'):
            for value in ('T1', None, (v for v in ['T1']), [5]):
                args = dict(expected_commit='a' * 40, known_task_ids=['T1'], obligation_ids=['AC-1'], evidence_refs=['ev/1'])
                args[field] = value
                ds = self.errors(Hostile(), gf.TrustedContext(**args))
                self.assertEqual({d['code'] for d in ds}, {'malformed_context'})
        self.assertEqual(self.errors(report(), object())[0]['code'], 'malformed_context')
        self.assertEqual(self.errors(report(), gf.TrustedContext('A' * 40, [], [], []))[0]['code'], 'malformed_context')

    def test_non_json_types(self):
        class Foreign:
            def __repr__(self):
                raise AssertionError('foreign method called')
        class String(str):
            pass
        cycle = []
        cycle.append(cycle)
        for value in (float('nan'), float('inf'), b'x', (), set(), Foreign(), String('x'), {1: 'x'}, cycle):
            self.assertTrue(self.codes(report(scenario=value)) <= {'malformed_input', 'limit_exceeded'})

    def test_limits_exact_boundaries(self):
        for field, value in [('id', 'x' * 65), ('scenario', 'x' * 4001), ('rationale', 'x' * 4001),
                             ('file_refs', ['x' * 513]), ('evidence_refs', ['x' * 129])]:
            self.assertIn('limit_exceeded', self.codes(report(**{field: value})))
        for field in SchemaTests.ARRAYS:
            self.assertIn('limit_exceeded', self.codes(report(**{field: ['x'] * 51})))
        refs = ['r' * 125 + str(i).zfill(3) for i in range(50)]
        ctx = gf.TrustedContext('a' * 40, refs, refs, refs)
        r = report(id='i' * 64, scenario='s' * 4000, rationale='r' * 4000,
                   task_attribution=refs, obligation_refs=refs, evidence_refs=refs,
                   file_refs=['p' * 509 + str(i).zfill(3) for i in range(50)])
        f = r['findings'][0]
        r['findings'] = [dict(copy.deepcopy(f), id=str(i).zfill(64)) for i in range(200)]
        self.assertEqual(len(gf.validate_report(r, ctx).findings), 200)
        r['findings'].append(copy.deepcopy(f))
        self.assertIn('limit_exceeded', self.codes(r))

    def test_oversized_rejected_before_schema(self):
        for r in (report(scenario='x' * 1000000), dict(report(), findings=[{}] * 1000000)):
            with patch.object(Draft202012Validator, 'iter_errors', side_effect=AssertionError('schema reached')):
                self.assertEqual(self.codes(r), {'limit_exceeded'})

    def test_diagnostics_bounded_and_escaped(self):
        ds = self.errors(report(id='SECRET\n' * 2000, scenario='SECRET\x00' * 2000))
        for d in ds:
            self.assertNotIn('SECRET', d['message'])
            for name in ('value', 'finding_id'):
                if name in d:
                    self.assertLessEqual(len(d[name]), 128)
                    self.assertNotIn('\n', d[name])
                    self.assertNotIn('\x00', d[name])


class MixedFailureTests(BehaviorCase):
    def test_missing_rationale_and_unknown_task_both_reported(self):
        r = report(task_attribution=['T9'])
        del r['findings'][0]['rationale']
        self.assertEqual(self.codes(r), {'missing_field', 'unknown_task'})

    def test_unknown_refs_alongside_bad_elements(self):
        self.assertEqual(self.codes(report(task_attribution=[5, 'T9'], obligation_refs=['bad'], evidence_refs=['bad'], severity='bad')),
                         {'invalid_type', 'unknown_task', 'unknown_obligation', 'unknown_evidence', 'invalid_value'})

    def test_claim_rules_survive_unrelated_errors(self):
        r = report('optional_improvement', introduces_regression=True)
        del r['findings'][0]['scenario']
        self.assertEqual(self.codes(r), {'missing_field', 'misclassified_optional'})
        self.assertEqual(self.codes(report('necessary_gap', introduces_regression='yes')), {'invalid_type'})
        self.assertEqual(self.codes(report(violates_existing_obligation='yes')), {'invalid_type'})

    def test_cross_finding_and_report_level_mix(self):
        r = report(file_refs=['../x'])
        r['reviewed_commit'] = 'b' * 40
        r['findings'] += [None, copy.deepcopy(r['findings'][0])]
        ds = self.errors(r)
        self.assertEqual(ds, self.errors(copy.deepcopy(r)))
        self.assertEqual({d['code'] for d in ds}, {'stale_commit', 'unsafe_path', 'duplicate_id', 'invalid_type'})
        keys = [(int(d['path'].split('/')[2]) if d['path'].startswith('/findings/') else -1,
                 d['path'], d['code'], d['message']) for d in ds]
        self.assertEqual(keys, sorted(set(keys)))

    def test_stage_a_short_circuits_b_and_c(self):
        nested = []
        for _ in range(9):
            nested = [nested]
        for value in (b'bad', [0] * 201, nested):
            with patch.object(Draft202012Validator, 'iter_errors', side_effect=AssertionError), \
                 patch.object(gf, '_semantic_checks', side_effect=AssertionError):
                self.assertTrue(self.codes(report(scenario=value)) <= {'malformed_input', 'limit_exceeded'})

    def test_internal_exception_converted(self):
        with patch.object(gf, '_semantic_checks', side_effect=ZeroDivisionError):
            ds = self.errors(report())
        self.assertEqual(ds, ({'code': 'malformed_input', 'path': '', 'message': 'unexpected validation failure'},))


class PathTests(BehaviorCase):
    def test_unsafe_paths(self):
        paths = ['/abs', 'C:\\x', 'C:x', '\\\\h\\s', '../a', 'a/../b', 'a\\b', 'a//b', 'a/./b', 'a/', '',
                 'a\x00b', 'a\x1fb', 'a\x7fb', '~', '~user', '~/x', ' a', 'a ', 'a:b', 'e\u0301',
                 'a\u200bb', 'a\u00a0b']
        paths += ['a' + chr(c) + 'b' for c in (0x2215, 0x2044, 0x29f8, 0x2216, 0x29f5, 0xff0f, 0xff3c, 0xfe68, 0x2024, 0xff0e, 0xfe52)]
        for p in paths:
            with self.subTest(path=p):
                self.assertIn('unsafe_path', self.codes(report(file_refs=[p])))

    def test_safe_paths(self):
        for p in ('src/a-b.py', 'folder with spaces/a.txt', 'é/file', '$VAR/%VAR%/*.py'):
            gf.validate_report(report(file_refs=[p]), context())

    def test_safe_nonexistent_path_no_fs_access(self):
        with blocked_io():
            gf.validate_report(report(file_refs=['deleted/nonexistent.py']), context())


def blocked_io():
    stack = ExitStack()
    targets = ['builtins.open', 'io.open', 'os.stat', 'os.lstat', 'os.scandir',
               'subprocess.run', 'subprocess.Popen', 'subprocess.check_output', 'socket.socket']
    targets += ['pathlib.Path.' + method for method in
                ('open', 'read_text', 'read_bytes', 'stat', 'lstat', 'exists', 'is_file', 'resolve', 'iterdir', 'glob', 'rglob', 'write_text', 'write_bytes')]
    for target in targets:
        stack.enter_context(patch(target, side_effect=AssertionError('forbidden I/O')))
    return stack


class AssessmentTests(BehaviorCase):
    def test_empty_report(self):
        r = report()
        r['findings'] = []
        a = gf.assess_report(r, context())
        for name in ('findings', 'correction_roots', 'impact_assessment_required', 'replanning_required', 'diagnosis_required', 'optional_observations'):
            self.assertEqual(getattr(a, name), ())
        self.assert_no_approval(a)

    def assert_no_approval(self, a):
        self.assertIs(a.execution_authorized, False)
        for name in ('passed', 'approved', 'verdict'):
            self.assertFalse(hasattr(a, name))
            self.assertNotIn(name, a.to_dict())

    def test_docstring_states_no_approval(self):
        self.assertIn('An empty report does not grant gate approval.', gf.__doc__)

    def test_single_attribution_nominates_root(self):
        a = gf.assess_report(report(), context())
        self.assertEqual(a.correction_roots, (gf.CorrectionRoot('T1', ('F1',)),))
        self.assert_no_approval(a)
        with self.assertRaises(TypeError):
            gf.Assessment(**dict(a.to_dict(), execution_authorized=True))

    def test_no_inferred_attribution(self):
        a = gf.assess_report(report(task_attribution=[], file_refs=['shared.py'], obligation_refs=['AC-1']), context())
        self.assertEqual(a.correction_roots, ())
        self.assertEqual(a.impact_assessment_required, ('F1',))

    def test_attribution_not_derived_from_id(self):
        a = gf.assess_report(report(id='T1', task_attribution=[]), context())
        self.assertEqual(a.correction_roots, ())

    def test_cross_task_and_shared_file(self):
        r = report(task_attribution=['T1', 'T2'])
        r['findings'] += [report(id='F2', task_attribution=['T2'])['findings'][0], report(id='F3')['findings'][0]]
        a = gf.assess_report(r, context())
        self.assertEqual(a.impact_assessment_required, ('F1',))
        self.assertEqual(a.findings[0].task_attribution, ('T1', 'T2'))
        self.assertEqual(a.correction_roots, (gf.CorrectionRoot('T1', ('F3',)), gf.CorrectionRoot('T2', ('F2',))))

    def test_other_buckets_with_and_without_attribution(self):
        for attribution in ([], ['T1']):
            for classification, bucket in [('necessary_gap', 'replanning_required'), ('uncertain_needs_diagnosis', 'diagnosis_required'), ('optional_improvement', 'optional_observations')]:
                a = gf.assess_report(report(classification, task_attribution=attribution), context())
                self.assertEqual(getattr(a, bucket), ('F1',))
                self.assertEqual(a.correction_roots, ())

    def test_preservation_and_partition(self):
        r = report()
        for i, classification in enumerate(('mandatory_violation_or_regression', 'necessary_gap', 'uncertain_needs_diagnosis', 'optional_improvement')):
            r['findings'].append(report(classification, id='extra' + str(i), task_attribution=[])['findings'][0])
        a = gf.assess_report(r, context())
        self.assertEqual(a.to_dict()['findings'], r['findings'])
        ids = [i for root in a.correction_roots for i in root.finding_ids]
        for bucket in (a.impact_assessment_required, a.replanning_required, a.diagnosis_required, a.optional_observations):
            ids.extend(bucket)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {f['id'] for f in r['findings']})

    def test_gap_only_has_empty_roots_and_replanning(self):
        a = gf.assess_report(report('necessary_gap'), context())
        self.assertEqual(a.correction_roots, ())
        self.assertEqual(a.replanning_required, ('F1',))
        self.assert_no_approval(a)

    def test_low_severity_mandatory_still_routed(self):
        for severity in ('low', 'info'):
            self.assertTrue(gf.assess_report(report(severity=severity), context()).correction_roots)

    def test_determinism_and_immutability(self):
        r, ctx = report(), context()
        before, ctx_before = copy.deepcopy(r), copy.deepcopy(ctx)
        a = gf.assess_report(r, ctx)
        self.assertEqual(a, gf.assess_report(copy.deepcopy(r), context()))
        self.assertEqual(r, before)
        self.assertEqual(ctx, ctx_before)
        plain = a.to_dict()
        plain['findings'][0]['task_attribution'].append('T2')
        r['findings'][0]['scenario'] = 'changed'
        self.assertEqual(a.to_dict()['findings'], before['findings'])
        with self.assertRaises(FrozenInstanceError):
            a.execution_authorized = True
        self.assertEqual(a, gf.assess_report(before, gf.TrustedContext('a' * 40, ['T2', 'T1'], ['SEC-1', 'AC-1'], ['ev/1'])))


class PurityTests(BehaviorCase):
    def test_no_forbidden_imports_and_no_io(self):
        tree = ast.parse(Path(gf.__file__).read_text())
        allowed = {'dataclasses', 'collections', 'json', 're', 'unicodedata', 'pathlib', 'jsonschema'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(n.name.split('.')[0] in allowed for n in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertIn(node.module.split('.')[0], allowed)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, ('print',))
        with blocked_io():
            gf.validate_report(report(), context())
            gf.assess_report(report(), context())


class StructureBoundaryTests(BehaviorCase):
    def test_coarse_limits_and_cycles_before_schema(self):
        deep = []
        for _ in range(7):
            deep = [deep]
        # More than 50,000 nodes, with every individual container in bounds.
        broad = [[[None] * 200 for _ in range(200)] for _ in range(2)]
        cases = [dict.fromkeys([str(i) for i in range(33)]),
                 {'k' * 129: None}, deep, broad]
        for value in cases:
            with self.subTest(kind=type(value).__name__), \
                 patch.object(Draft202012Validator, 'iter_errors', side_effect=AssertionError):
                self.assertEqual(self.codes(report(scenario=value)), {'limit_exceeded'})

    def test_reference_duplicates_are_not_silently_removed(self):
        for field, ref in [('task_attribution', 'T1'), ('obligation_refs', 'AC-1'),
                           ('evidence_refs', 'ev/1'), ('file_refs', 'src/a.py')]:
            ds = self.errors(report(**{field: [ref, ref]}))
            self.assertEqual(len(ds), 1)
            self.assertEqual(ds[0]['code'], 'duplicate_reference')
            self.assertEqual(ds[0]['path'], '/findings/0/' + field + '/1')

    def test_invalid_ids_still_participate_in_duplicate_detection(self):
        r = report(id='bad\n')
        r['findings'].append(copy.deepcopy(r['findings'][0]))
        ds = self.errors(r)
        self.assertEqual(sum(d['code'] == 'invalid_value' for d in ds), 2)
        self.assertEqual(sum(d['code'] == 'duplicate_id' for d in ds), 1)
