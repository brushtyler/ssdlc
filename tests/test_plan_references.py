import copy
import json
import unittest
from pathlib import Path

from ssdlc_catalogues import CatalogueError, heading_anchors, requirement_catalogue
from ssdlc_tasks import PlanError, parse_plan_output
from test_task_bootstrap import RepositoryTests


class CatalogueTests(unittest.TestCase):
    def test_definitions_not_mentions_or_fenced_examples(self):
        text = '''Requirements mention AC-99 and `AC-98` as examples.
### AC-1: Parent
- **AC-1a.** Child refers to AC-1.
1. **SEC-1 (Safety).** Refuse mutation.
| AC-2 | Table definition |
```markdown
### AC-100: Example
```
~~~
- SEC-100: Example
~~~
'''
        cat = requirement_catalogue(text)
        self.assertEqual(cat['required_ids'], ['AC-1', 'AC-1a', 'AC-2', 'SEC-1'])
        self.assertEqual(cat['undefined_mentions'], ['AC-99'])
        self.assertEqual(cat['definitions'][1]['parent_id'], 'AC-1')

    def test_duplicates_are_spec_errors_with_source_lines(self):
        with self.assertRaises(CatalogueError) as caught:
            requirement_catalogue('### AC-1: One\n- **AC-1.** Two')
        self.assertEqual(caught.exception.diagnostics[0]['source_lines'], [1, 2])

    def test_hierarchy_does_not_confuse_numeric_prefixes(self):
        cat = requirement_catalogue('AC-1 AC-10 AC-1a AC-10a SEC-1')
        parents = {item['id']: item['parent_id'] for item in cat['definitions']}
        self.assertIsNone(parents['AC-10'])
        self.assertEqual(parents['AC-10a'], 'AC-10')

    def test_malformed_definitions_are_not_silently_truncated_or_normalized(self):
        for text in ('### AC-1.1: Unsupported dotted ID', '- **ac-1:** Wrong case', 'SEC-1_bad: Bad ID'):
            with self.subTest(text=text), self.assertRaises(CatalogueError) as caught:
                requirement_catalogue(text)
            self.assertEqual(caught.exception.diagnostics[0]['code'], 'INVALID_REQUIREMENT_DEFINITION')
        self.assertEqual(requirement_catalogue('AC-99 appears only in this narrative.')['required_ids'], [])

    def test_contract_examples_and_duplicate_headings(self):
        self.assertEqual(heading_anchors('# API\n```\n# Fake\n```\n# API\n# API-1'),
                         ['api', 'api-1', 'api-1-1'])

    def test_duplicate_keys_and_non_finite_json_are_rejected(self):
        for raw in ('{"tasks": [], "tasks": [1]}', '{"task":{"id":1,"id":2}}',
                    '{"timeout":NaN}', '{"timeout":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(PlanError) as caught:
                parse_plan_output(raw)
            self.assertEqual(caught.exception.diagnostics[0]['code'], 'INVALID_JSON')

    def test_raw_and_fenced_alternatives_are_ambiguous(self):
        for raw in ('{}\n```json\n{}\n```', '```json\n{}\n```\n{"other":1}'):
            with self.subTest(raw=raw), self.assertRaises(PlanError) as caught:
                parse_plan_output(raw)
            self.assertEqual(caught.exception.diagnostics[0]['code'], 'AMBIGUOUS_OUTPUT')


class ReferenceTests(RepositoryTests):
    def diagnostics(self, requirements='AC-1 SEC-1'):
        with self.assertRaises(PlanError) as caught:
            self.validator(requirements).validate(self.plan)
        return caught.exception.diagnostics

    def test_check_refs_cannot_cross_namespace_or_task_boundary(self):
        check = self.plan['tasks'][0]['verification']['checks'][0]
        check['ac_refs'] = ['AC-1', 'SEC-1', 'AC-404']
        check['sec_refs'] = []
        issues = self.diagnostics()
        self.assertEqual({d['code'] for d in issues},
                         {'WRONG_REQUIREMENT_KIND', 'UNKNOWN_REQUIREMENT_REF',
                          'CHECK_REFS_OUTSIDE_TASK', 'MISSING_VERIFICATION_REF'})
        missing = next(d for d in issues if d['code'] == 'MISSING_VERIFICATION_REF')
        self.assertEqual(missing['missing_refs'], ['SEC-1'])
        outside = next(d for d in issues if d['code'] == 'CHECK_REFS_OUTSIDE_TASK')
        self.assertEqual(outside['check_id'], 'CHECK-T1')
        self.assertEqual(outside['offending_refs'], ['AC-404', 'SEC-1'])

    def test_wrong_task_namespace_does_not_count_as_global_coverage(self):
        self.plan['tasks'][0]['ac_refs'] = ['AC-1', 'SEC-1']
        self.plan['tasks'][0]['sec_refs'] = []
        issues = self.diagnostics()
        self.assertEqual(next(d for d in issues if d['code'] == 'UNCOVERED_REQUIREMENTS')['missing_refs'], ['SEC-1'])

    def test_parents_and_children_require_explicit_task_and_check_coverage(self):
        requirements = '### AC-1: Parent\n- **AC-1a.** Child\nSEC-1: Safety'
        issues = self.diagnostics(requirements)
        self.assertEqual(issues[-1]['missing_refs'], ['AC-1a'])
        task = self.plan['tasks'][0]
        task['ac_refs'] = ['AC-1a']
        task['verification']['checks'][0]['ac_refs'] = ['AC-1a']
        self.assertEqual(self.diagnostics(requirements)[-1]['missing_refs'], ['AC-1'])
        task['ac_refs'] = ['AC-1', 'AC-1a']
        task['verification']['checks'][0]['ac_refs'] = ['AC-1', 'AC-1a']
        self.validator(requirements).validate(self.plan)

    def test_contract_error_includes_real_available_refs_and_task(self):
        (self.artifacts/'02-design/adr.md').write_text('# Interfaces\n```\n# Example\n```')
        self.plan['tasks'][0]['contract_refs'] = ['ADR-title.md#interfaces', 'adr.md#example', '../adr.md']
        issues = self.diagnostics()
        self.assertEqual(len(issues), 3)
        for issue in issues:
            self.assertEqual(issue['code'], 'INVALID_CONTRACT_REF')
            self.assertEqual(issue['task_id'], 'T1')
            self.assertEqual(issue['available_refs'], ['adr.md', 'adr.md#interfaces'])

    def test_preparatory_checks_do_not_cover_unowned_requirements(self):
        task = self.plan['tasks'][0]
        task['ac_refs'] = []
        task['sec_refs'] = []
        issues = self.diagnostics()
        self.assertEqual(issues[-1]['missing_refs'], ['AC-1', 'SEC-1'])
        self.assertEqual(sum(i['code'] == 'CHECK_REFS_OUTSIDE_TASK' for i in issues), 2)

    def test_future_schema_modes_fail_before_semantic_review(self):
        self.plan['tasks'][0]['kind'] = 'subgoal'
        self.assertEqual(self.diagnostics()[0]['code'], 'UNSUPPORTED_EXECUTION_MODE')

    def test_three_real_rejected_candidates_report_all_reference_defects(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/rejected_step3a2_references.json').read_text())
        for name, content in fixture['contracts'].items():
            (self.artifacts/'02-design'/name).write_text(content)
        for attempt in fixture['attempts']:
            with self.subTest(attempt=attempt['attempt']):
                self.plan['tasks'] = []
                for source in attempt['tasks']:
                    task = self.task(source['task_id'], source['ac_refs'], source['sec_refs'], ['app.py'])
                    task['contract_refs'] = source['contract_refs']
                    template = task['verification']['checks'][0]
                    task['verification']['checks'] = [{**copy.deepcopy(template), **check} for check in source['checks']]
                    self.plan['tasks'].append(task)
                before = copy.deepcopy(self.plan)
                issues = self.diagnostics(fixture['requirements'])
                self.assertEqual(self.plan, before, 'Validation must not repair reference labels')
                if attempt['attempt'] in (1, 2):
                    self.assertEqual(next(d for d in issues if d['code'] == 'UNCOVERED_REQUIREMENTS')['missing_refs'],
                                     ['AC-1', 'AC-2', 'AC-3', 'AC-4'])
                if attempt['attempt'] == 1:
                    self.assertTrue(any(d['code'] == 'INVALID_CONTRACT_REF' for d in issues))
                elif attempt['attempt'] == 2:
                    self.assertEqual([d['code'] for d in issues], ['UNCOVERED_REQUIREMENTS'])
                else:
                    outside = [d for d in issues if d['code'] == 'CHECK_REFS_OUTSIDE_TASK']
                    self.assertEqual(len(outside), 2)
                    self.assertEqual({d['check_id'] for d in outside}, {'T1-noop', 'T1-snapshot'})
                    self.assertEqual(outside[0]['task_id'], 'T1-preflight-helpers')
                    self.assertEqual(outside[0]['offending_refs'], ['AC-4'])


if __name__ == '__main__':
    unittest.main()
