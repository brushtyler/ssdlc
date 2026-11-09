import json
import tempfile
import unittest
from pathlib import Path

from ssdlc_planning import PlanRepair
from ssdlc_tasks import PlanError, TaskPlanValidator, parse_plan_output


class PlanRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []
        self.reviews = []
        self.answers = ['{"ok": false}', '{"ok": true}']

    def generate(self, attempt, inputs, previous):
        self.calls.append(previous)
        return self.answers[min(attempt - 1, len(self.answers) - 1)]

    def validate(self, plan):
        if not plan.get('ok'):
            raise PlanError('Missing check', [{'code': 'MISSING_VERIFICATION_REF', 'task_id': 'T1', 'missing_refs': ['AC-4']}])

    def review(self, plan):
        self.reviews.append(plan)
        return 'Behavior checked.\nTASK_PLAN_STATUS: APPROVED'

    def repair(self, **overrides):
        values = dict(directory=self.root, inputs={'sealed': 'inputs'}, validator=self.validate,
                      generate=self.generate, review=self.review, budget=lambda: None)
        values.update(overrides)
        return PlanRepair(**values)

    def test_repair_receives_candidate_and_precise_diagnostics(self):
        result = self.repair().run()
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.reviews), 1)
        feedback = json.loads(self.calls[-1]['diagnostics'])
        self.assertEqual(feedback[0]['missing_refs'], ['AC-4'])
        self.assertEqual(json.loads(self.calls[-1]['candidate']), {'ok': False})
        self.assertEqual(len(list((self.root / 'task-planning').glob('*candidate.txt'))), 2)

    def test_previous_candidate_is_validated_and_repaired_without_regeneration(self):
        self.repair().run(initial_candidate='{"ok": false}')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads(self.calls[0]['candidate']), {'ok': False})

    def test_structural_repairs_do_not_exhaust_semantic_revision_allowance(self):
        self.answers = ['{"ok": false}', '{"ok": false}', '{"ok": true}', '{"ok": true}']
        verdicts = iter(['TASK_PLAN_STATUS: REVISION_REQUIRED', 'TASK_PLAN_STATUS: APPROVED'])
        self.repair(review=lambda _: next(verdicts)).run()
        self.assertEqual(len(self.calls), 4)
        self.assertIn('REVISION_REQUIRED', self.calls[-1]['review'])

    def test_semantic_limit_persists_and_requires_explicit_extension(self):
        self.answers = ['{"ok": true}']
        reject = lambda _: 'TASK_PLAN_STATUS: REVISION_REQUIRED'
        for _ in range(2):
            with self.assertRaisesRegex(PlanError, 'Semantic.*exhausted'):
                self.repair(review=reject).run()
        self.assertEqual(len(self.calls), 3)
        self.repair(review_repairs=3).run()
        self.assertEqual(len(self.calls), 4)
        state = json.loads((self.root/'task-planning/session.json').read_text())
        self.assertEqual(state['policy_updates'][0]['previous_review_repairs'], 2)
        self.assertEqual(state['limits']['review_repairs'], 3)

    def test_legacy_limit_is_not_silently_increased(self):
        self.answers = ['{"ok": true}']
        self.repair().run()
        path = self.root/'task-planning/session.json'
        state = json.loads(path.read_text())
        del state['limits']
        path.write_text(json.dumps(state))
        self.repair().run()
        self.assertEqual(json.loads(path.read_text())['limits']['review_repairs'], 0)
        with self.assertRaisesRegex(PlanError, 'between 0 and 4'):
            self.repair(review_repairs=99)

    def test_valid_first_candidate_needs_no_repair(self):
        self.answers = ['{"ok": true}']
        self.assertTrue(self.repair().run()['ok'])
        self.assertEqual(len(self.calls), 1)

    def test_restarts_do_not_reset_exhausted_limit(self):
        self.answers = ['{"ok": false}']
        for _ in range(2):
            with self.assertRaisesRegex(PlanError, 'exhausted'):
                self.repair().run()
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.reviews)

    def test_semantic_rejection_is_not_automatic_approval(self):
        self.answers = ['{"ok": true}']
        verdicts = iter(['Missing behavioral coverage\nTASK_PLAN_STATUS: REVISION_REQUIRED', 'TASK_PLAN_STATUS: APPROVED'])
        self.repair(review=lambda _: next(verdicts)).run()
        self.assertEqual(len(self.calls), 2)
        self.assertIn('Missing behavioral coverage', self.calls[-1]['review'])

    def test_approved_session_reused_without_calls(self):
        self.repair().run()
        result = self.repair(generate=lambda *a: self.fail('regeneration'), review=lambda *a: self.fail('review repeated')).run()
        self.assertTrue(result['ok'])

    def test_interrupted_generation_consumes_reservation(self):
        def crash(*args):
            raise OSError('interrupted invocation')
        with self.assertRaises(OSError):
            self.repair(generate=crash).run()
        self.repair().run()
        self.assertEqual(len(self.calls), 1)
        state = json.loads((self.root / 'task-planning/session.json').read_text())
        self.assertEqual(len(state['attempts']), 2)

    def test_changed_inputs_or_evidence_refused(self):
        self.repair().run()
        with self.assertRaisesRegex(PlanError, 'inputs changed'):
            self.repair(inputs={'sealed': 'different'}).run()
        (self.root / 'task-planning/attempt-2-review.txt').write_text('forged')
        with self.assertRaisesRegex(PlanError, 'evidence changed'):
            self.repair().run()

    def test_budget_failure_prevents_model_call(self):
        def stop():
            raise PlanError('budget exhausted')
        with self.assertRaisesRegex(PlanError, 'budget'):
            self.repair(budget=stop).run()
        self.assertFalse(self.calls)

    def test_fenced_preamble_and_ambiguous_candidates(self):
        self.assertEqual(parse_plan_output('Here is the plan:\n```json\n{"ok": true}\n```'), {'ok': True})
        with self.assertRaises(PlanError):
            parse_plan_output('```json\n{}\n```\n```json\n{}\n```')
        with self.assertRaises(PlanError):
            parse_plan_output('[]')

    def test_multiple_mapping_errors_collected(self):
        import re
        source = Path(__file__).resolve().parents[1] / 'PLAN_TASK_ROUTING_AND_SUBGOALS.md'
        plan = json.loads(re.findall(r'^```json\n(.*?)^```$', source.read_text(), re.M | re.S)[0])
        # Reuse a minimal valid contract and make two mapping failures.
        quality = {'schema_version': '1.0', 'contract_revision': 1, 'base_commit': plan['base_commit'],
                   'sources': [], 'standards': [], 'checks': [{'check_id': 'Q', 'argv': ['true'], 'cwd': '.', 'timeout_sec': 1, 'kind': 'test', 'required': True}],
                   'baseline': [], 'limitations': [], 'exceptions': []}
        task = plan['tasks'][0]
        task['ac_refs'] = ['AC-1', 'AC-2']
        task['verification']['checks'][0]['ac_refs'] = []
        duplicate = json.loads(json.dumps(task))
        duplicate['task_id'] = 'T2'
        plan['tasks'].append(duplicate)
        with self.assertRaises(PlanError) as caught:
            TaskPlanValidator(self.root, 'AC-1 AC-2', quality).validate(plan)
        self.assertEqual([d['task_id'] for d in caught.exception.diagnostics], [task['task_id'], 'T2'])


from unittest.mock import patch
from test_task_bootstrap import RepositoryTests
from orchestrator import SSDLCOrchestrator
from ssdlc_tasks import atomic_json


class PlanningResumeTests(RepositoryTests):
    def seed(self):
        runtime = self.repo / '.ssdlc/.runtime'
        with patch('orchestrator.ProjectRuntimeIsolation.activate', return_value={
            'project_id': 'test', 'codex_home': str(runtime/'codex'), 'claude_home': str(runtime/'claude'),
        }), patch('orchestrator.get_cli_version', return_value='TEST'):
            orch = SSDLCOrchestrator(self.repo, 'TKT-1', sync_remote=False)
        orch.state['task_breakdown'] = True
        for name, content in [('01-po/requirements.md', 'AC-1 SEC-1'), ('02-design/adr.md', '# Approved design'),
                              ('02-design/threat-model.md', 'DESIGN_STATUS: APPROVED')]:
            path = orch.ssdlc_root / name
            path.write_text(content)
            orch.record_artifact(name, path)
        path = orch.dir_recon / 'quality-contract.json'
        atomic_json(path, self.quality)
        orch.record_artifact('00-recon/quality-contract.json', path)
        orch.record_gate('PAIR_DESIGN_GATE', True, 'Approved')
        return orch

    def test_resume_only_plans_without_repeating_upstream_phases_or_running_code(self):
        orch = self.seed()
        with patch.object(orch, '_ensure_task_plan') as plan, patch.object(orch, 'run_phase_po') as po, patch.object(orch, 'run_phase_pair_design') as design, patch.object(orch, 'run_phase_dev') as dev:
            orch.resume_task_planning()
            plan.assert_called_once()
            po.assert_not_called()
            design.assert_not_called()
            dev.assert_not_called()
        self.assertEqual(orch.state['status'], 'PLAN_READY')
        self.assertFalse(orch.worktree_dir.exists())

    def test_changed_approved_input_blocks_before_any_planner_call(self):
        orch = self.seed()
        (orch.dir_design / 'adr.md').write_text('Changed')
        with patch.object(orch, '_ensure_task_plan') as plan:
            with self.assertRaisesRegex(PlanError, 'changed'):
                orch.resume_task_planning()
            plan.assert_not_called()

    def test_rejected_design_cannot_be_reused(self):
        orch = self.seed()
        orch.record_gate('PAIR_DESIGN_GATE', False, 'Needs revision')
        with self.assertRaisesRegex(PlanError, 'not been approved'):
            orch.resume_task_planning()

    def test_started_execution_requires_explicit_migration(self):
        orch = self.seed()
        orch.worktree_dir.mkdir()
        with self.assertRaisesRegex(PlanError, 'Execution already started'):
            orch.resume_task_planning()


if __name__ == '__main__':
    unittest.main()
