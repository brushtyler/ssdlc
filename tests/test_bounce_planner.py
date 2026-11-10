"""Pure data-shape examples; these do not exercise evidence verification or bridges."""
import copy
import tempfile
import unittest
from pathlib import Path

from ssdlc_bounce import (BounceError, canonical_json, plan_bounce, select_response,
                          validate_controls)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.commit = 'a' * 40
        self.quality = dict(schema_version='1.0', contract_revision=1,
                            base_commit=self.commit, sources=[], standards=[],
                            checks=[dict(check_id='CHECK-GLOBAL', argv=['true'], cwd='.',
                                         timeout_sec=5, kind='test', required=True)],
                            baseline=[dict(check_id='CHECK-GLOBAL', exit_code=0,
                                           log_ref='baseline.log', sha256='b' * 64)],
                            limitations=[], exceptions=[])
        self.plan = dict(schema_version='1.0', plan_revision=1, ticket_id='TEST',
                         execution_strategy='sequential_dag', base_commit=self.commit,
                         quality_contract_ref='00-recon/quality-contract.json', tasks=[])
        for name, deps in [('A', []), ('B', ['A']), ('C', ['A']),
                           ('D', ['B', 'C']), ('E', [])]:
            self.plan['tasks'].append(dict(task_id=name, title=name, description=name,
                depends_on=deps, target_files=[name + '.py'], ac_refs=['AC-1'], sec_refs=[],
                standard_refs=[], profile='ALGORITHMIC', complexity='standard', kind='task',
                contract_refs=[], verification=dict(mode='automated', quality_check_refs=['CHECK-GLOBAL'],
                    checks=[dict(check_id='CHECK-' + name, argv=['true'], cwd='.',
                                 timeout_sec=5, ac_refs=['AC-1'], sec_refs=[])])))

    def finding(self, kind='mandatory_violation_or_regression', tasks=None, ident='F1'):
        return dict(id=ident, severity='high', classification=kind, evidence_kind='observed',
            scenario='Failure', rationale='AC-1', task_attribution=['A'] if tasks is None else tasks,
            file_refs=['A.py'], obligation_refs=['AC-1'], evidence_refs=['ev'],
            violates_existing_obligation=kind == 'mandatory_violation_or_regression',
            introduces_regression=False, necessary_for_requested_correctness=kind == 'necessary_gap')

    def evaluation(self, findings=None, ordinal=1, **changes):
        e = dict(gate='qa', identity='evaluation-' + str(ordinal), reviewer_ordinal=ordinal,
                 outcome_class='rejected_substantive', verdict='FAILED', controls={'det_ok': True},
                 evidence_refs=['ev'], report=dict(schema_version='1.0', reviewed_commit=self.commit,
                                                  source_gate='qa', findings=findings or []))
        e.update(changes)
        return e

    def run_plan(self, evaluations):
        return plan_bounce(repo=self.repo, plan=self.plan, requirements_text='AC-1',
            quality=self.quality, reviewed_commit=self.commit, evaluations=evaluations,
            phase='qa', attempt=1)

    def codes(self, result):
        return {b['code'] for b in result['blockers']}

    def test_diamond_collision_determinism_and_unchanged_state(self):
        evaluations = [self.evaluation([self.finding()]), self.evaluation([self.finding()], 2)]
        state = dict(tasks={'A': {'attempt': 2, 'status': 'verified'}}, scheduler={'pending': ['B']})
        (self.repo / 'state.json').write_text(canonical_json(state))
        before = copy.deepcopy((self.plan, self.quality, evaluations, state))
        files = {p.name: p.read_bytes() for p in self.repo.iterdir()}
        result = self.run_plan(evaluations)
        self.assertEqual(result['repair_roots'], ['A'])
        self.assertEqual(result['revalidation_tasks'], ['B', 'C', 'D'])
        self.assertEqual(len(result['findings']), 2)
        self.assertNotEqual(result['findings'][0]['identity'], result['findings'][1]['identity'])
        self.assertEqual(result['status'], 'candidates')
        self.assertEqual(canonical_json(result), canonical_json(self.run_plan(evaluations)))
        self.assertEqual(before, (self.plan, self.quality, evaluations, state))
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.repo.iterdir()})
        self.assertFalse(result['execution_authorized'])
        self.assertFalse(result['approved'])

    def test_multiple_roots(self):
        result = self.run_plan([self.evaluation([self.finding(tasks=['B']),
                                                self.finding(tasks=['C'], ident='F2')])])
        self.assertEqual(result['repair_roots'], ['B', 'C'])
        self.assertEqual(result['revalidation_tasks'], ['D'])

    def test_classifications_and_no_path_attribution(self):
        for kind, tasks, bucket in [
            ('mandatory_violation_or_regression', [], 'impact_analysis'),
            ('mandatory_violation_or_regression', ['A', 'B'], 'impact_analysis'),
            ('necessary_gap', ['A'], 'replanning'),
            ('uncertain_needs_diagnosis', ['A'], 'diagnosis'),
            ('optional_improvement', ['A'], 'optional_observations')]:
            with self.subTest(kind=kind, tasks=tasks):
                optional = bucket == 'optional_observations'
                result = self.run_plan([self.evaluation([self.finding(kind, tasks)],
                    verdict='PASSED' if optional else 'FAILED',
                    outcome_class='approved' if optional else 'rejected_substantive')])
                self.assertTrue(result[bucket])
                self.assertEqual(result['candidates'], [])
                self.assertEqual(result['repair_roots'], [])
                self.assertEqual(result['status'], 'empty' if optional else 'blocked')
                self.assertFalse(result['execution_authorized'])

    def test_invalid_unknown_and_cyclic_graph(self):
        result = self.run_plan([self.evaluation([self.finding(tasks=['missing'])])])
        self.assertIn('unknown_root', self.codes(result))
        self.assertEqual(result['revalidation_tasks'], [])
        for deps in (['D'], ['missing']):
            self.plan['tasks'][0]['depends_on'] = deps
            result = self.run_plan([self.evaluation([self.finding()])])
            self.assertIn('invalid_graph', self.codes(result))
            self.assertEqual(result['repair_roots'], [])
            self.assertEqual(result['revalidation_tasks'], [])

    def test_forged_assessment_has_no_authority(self):
        result = self.run_plan([self.evaluation([], verdict='PASSED', outcome_class='approved',
            assessment={'correction_roots': [{'task_id': 'E', 'finding_ids': ['F1']}]})])
        self.assertEqual(result['candidates'], [])
        self.assertFalse(result['execution_authorized'])

    def test_empty_controls_and_negative_verdict(self):
        for verdict in ('PASSED', 'FAILED'):
            result = self.run_plan([self.evaluation([], verdict=verdict, controls={'det_ok': False})])
            self.assertIn('control_failed', self.codes(result))
            self.assertEqual('negative_verdict_without_roots' in self.codes(result), verdict == 'FAILED')
            self.assertEqual(result['status'], 'blocked')
        result = self.run_plan([self.evaluation([], verdict='PASSED', outcome_class='approved')])
        self.assertEqual(result['blockers'], [])
        self.assertFalse(result['execution_authorized'])

    def test_review_and_appsec_verdicts(self):
        for gate, source, positive, negative, controls, extra in (
            ('review', 'code_review', 'APPROVED', 'CHANGES_REQUESTED', {}, {}),
            ('appsec', 'appsec', 'SECURE', 'VULNERABLE', {'scanners': True},
             {'scanner_status': {'scanners': []}})):
            for verdict in (positive, negative):
                e = self.evaluation([], gate=gate, verdict=verdict, controls=controls,
                    outcome_class='approved' if verdict == positive else 'rejected_substantive',
                    **extra)
                e['report']['source_gate'] = source
                result = self.run_plan([e])
                self.assertEqual(result['status'], 'empty' if verdict == positive else 'blocked')
                self.assertNotIn('contradictory_evidence', self.codes(result))
        self.assertFalse(self.run_plan([])['execution_authorized'])

    def test_terminal_outcomes_are_diagnostics(self):
        for outcome in ('protocol_error', 'integrity_blocked', 'provider_error', 'interrupted', 'unknown'):
            result = self.run_plan([self.evaluation([self.finding()], outcome_class=outcome)])
            self.assertTrue(result['diagnosis'])
            self.assertTrue(result['blockers'])
            self.assertEqual(result['candidates'], [])


class HelperTests(unittest.TestCase):
    def test_response_ordinals_and_independent_invocations(self):
        first = {'response-1.md': '', 'diagnostics-1.json': []}
        second = dict(first, **{'response-2.md': '', 'prompt-2.json': {},
            'correction-record.json': {}, 'diagnostics-1.json': ['bad'], 'diagnostics-2.json': []})
        for records, n in ((first, 1), (second, 2)):
            for count in range(3):
                data = dict(records, **{f'invocation-{i + 1}.json': {} for i in range(count)})
                self.assertEqual(select_response(data), (n, f'response-{n}.md'))
        for records in ({}, {'response-2.md': ''}, {'response-3.md': ''},
                        dict(first, **{'response-1.md.bak': ''}),
                        dict(first, **{'prompt-2.json': {}}),
                        dict(second, **{'diagnostics-1.json': []})):
            with self.subTest(records=records), self.assertRaises(BounceError) as error:
                select_response(records)
            self.assertEqual(error.exception.code, 'contradictory_evidence')
        with self.assertRaises(BounceError) as error:
            select_response(dict(first, **{'diagnostics-1.json': {}}))
        self.assertEqual(error.exception.code, 'malformed_record')

    def test_exact_controls_f25_f26(self):
        self.assertEqual(validate_controls('review', {}), {})
        for gate, controls in [('qa', {}), ('qa', {'det_ok': 'passed'}),
                ('qa', {'det_ok': 1}), ('qa', {'det_ok': True, 'x': True}),
                ('review', {'x': True}), ('appsec', {'scanners': 1}), ('qa', [])]:
            with self.subTest(controls=controls), self.assertRaises(BounceError) as error:
                validate_controls(gate, controls)
            self.assertEqual(error.exception.code, 'malformed_record')
        self.assertEqual(validate_controls('qa', {'det_ok': False}), {'det_ok': False})
        status = {'scanners': [{'id': 'S', 'status': 'failed', 'required': True}]}
        with self.assertRaises(BounceError) as error:
            validate_controls('appsec', {'scanners': True}, status)
        self.assertEqual(error.exception.code, 'contradictory_evidence')
        self.assertEqual(validate_controls('appsec', {'scanners': False}, status), {'scanners': False})
        status['scanners'][0]['status'] = 'not_applicable'
        self.assertEqual(validate_controls('appsec', {'scanners': True}, status), {'scanners': True})


if __name__ == '__main__':
    unittest.main()
