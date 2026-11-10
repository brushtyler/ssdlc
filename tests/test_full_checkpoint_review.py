import json
from pathlib import Path
from unittest.mock import patch

from orchestrator import SSDLCOrchestrator
from ssdlc_tasks import TaskError, atomic_json, git
from test_task_bootstrap import RepositoryTests


def framed(gate, orch, findings=(), verdict=None, **changes):
    phase = {'review': orch.dir_review, 'qa': orch.dir_qa, 'appsec': orch.dir_appsec}[gate]
    inputs = list((phase / 'evaluations').glob('*/input.json'))
    nonce = json.loads(max(inputs, key=lambda p: p.stat().st_mtime_ns).read_text())['identity'][:16]
    obj = dict(schema_version='1.0', source_gate={'review': 'code_review', 'qa': 'qa', 'appsec': 'appsec'}[gate],
               reviewed_commit=orch.state['task_verified_head'], findings=list(findings))
    obj.update(changes)
    marker = {'review': 'REVIEW_STATUS', 'qa': 'QA_STATUS', 'appsec': 'APPSEC_STATUS'}[gate]
    verdict = verdict or {'review': 'APPROVED', 'qa': 'PASSED', 'appsec': 'SECURE'}[gate]
    return (f'-----BEGIN SSDLC GATE REPORT {nonce}-----\n' + json.dumps(obj) +
            f'\n-----END SSDLC GATE REPORT {nonce}-----\nHuman report\n{marker}: {verdict}')


class FullCheckpointReviewTests(RepositoryTests):
    def seed(self):
        runtime = self.repo/'.ssdlc/.runtime'
        with patch('orchestrator.ProjectRuntimeIsolation.activate', return_value={
            'project_id': 'test', 'codex_home': str(runtime/'codex'), 'claude_home': str(runtime/'claude')
        }), patch('orchestrator.get_cli_version', return_value='TEST'):
            orch = SSDLCOrchestrator(self.repo, 'TKT-1', sync_remote=False)
        orch.state.update(task_breakdown=True, risk_tier='M')
        wt = orch._ensure_worktree()
        (wt/'app.py').write_text('value = 1\n# '+ 'x'*65000 +'\n# FINAL_DIFF_SENTINEL\n')
        git(wt, 'add', 'app.py')
        git(wt, 'commit', '-m', 'verified test checkpoint')
        orch.state['task_verified_head'] = git(wt, 'rev-parse', 'HEAD')
        diff = git(wt, 'diff', '--binary', self.base, 'HEAD')+'\n'
        path = orch.dir_dev/'implementation.diff'
        path.write_text(diff)
        orch.record_artifact('03-dev/implementation.diff', path)
        atomic_json(orch.dir_recon/'quality-contract.json', self.quality)
        orch.record_artifact('00-recon/quality-contract.json', orch.dir_recon/'quality-contract.json')
        (orch.dir_po/'requirements.md').write_text('AC-1: Correct behavior\nSEC-1: Secure behavior\n')
        orch.record_artifact('01-po/requirements.md', orch.dir_po/'requirements.md')
        atomic_json(orch.dir_design/'plan.json', self.plan)
        orch.record_artifact('02-design/plan.json', orch.dir_design/'plan.json')
        orch._save_state()
        return orch, diff

    def test_complete_large_diff_is_reviewed_in_verified_worktree_and_archived(self):
        orch, diff = self.seed()
        self.assertGreater(len(diff), 60000)
        def invoke(engine, model, system, user, cwd, *args, **kwargs):
            self.assertEqual(cwd, orch.worktree_dir)
            self.assertIn(diff, user)
            self.assertIn('FINAL_DIFF_SENTINEL', user)
            self.assertEqual(kwargs['sandbox'], 'read-only')
            return framed('review', orch), 0, 0, 0, 'test'
        with patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:reviewer'], 'low')), patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
            passed, _ = orch.run_phase_review()
        self.assertTrue(passed)
        run = next((orch.dir_review/'runs').iterdir())
        inputs = json.loads((run/'inputs.json').read_text())
        self.assertIn(diff, inputs['user_prompt'])
        self.assertEqual(inputs['verified_head'], orch.state['task_verified_head'])
        self.assertTrue((run/'reviewer-1.md').is_file())

    def test_explicit_review_budget_rejects_without_silent_truncation(self):
        orch, _ = self.seed()
        with patch.dict('os.environ', {'SSDLC_REVIEW_MAX_DIFF_CHARS': '60000'}), patch('orchestrator.EngineRunner.invoke_role') as engine:
            with self.assertRaisesRegex(TaskError, 'no truncation performed'):
                orch.run_phase_review()
            engine.assert_not_called()

    def test_review_refuses_stale_diff_or_dirty_checkpoint(self):
        orch, _ = self.seed()
        path = orch.dir_dev/'implementation.diff'
        original = path.read_text()
        path.write_text('stale or truncated diff')
        with patch('orchestrator.EngineRunner.invoke_role') as engine:
            with self.assertRaisesRegex(TaskError, 'diff differs'):
                orch.run_phase_review()
            path.write_text(original)
            (orch.worktree_dir/'app.py').write_text('unverified changes')
            with self.assertRaisesRegex(TaskError, 'clean verified'):
                orch.run_phase_review()
            engine.assert_not_called()

    def test_input_mutation_during_review_never_grants_approval(self):
        orch, _ = self.seed()
        def mutate(*args, **kwargs):
            (orch.worktree_dir/'app.py').write_text('changed during review')
            return 'REVIEW_STATUS: APPROVED', 0, 0, 0, 'test'
        with patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:reviewer'], 'low')), patch('orchestrator.EngineRunner.invoke_role', side_effect=mutate):
            with self.assertRaisesRegex(TaskError, 'checkpoint changed during review'):
                orch.run_phase_review()
        self.assertFalse(orch.state['gate_history'][-1]['passed'])
        self.assertEqual(orch.state['ledger'][-1]['status'], 'INPUT_CHANGED')
        self.assertEqual(len(list((orch.dir_review/'runs').glob('*/reviewer-1.md'))), 1)

    def test_qa_receives_complete_diff_and_verified_worktree(self):
        orch, diff = self.seed()
        def invoke(engine, model, system, user, cwd, *args, **kwargs):
            self.assertEqual(cwd, orch.worktree_dir)
            self.assertIn(diff, user)
            self.assertIn('FINAL_DIFF_SENTINEL', user)
            return framed('qa', orch), 0, 0, 0, 'test'
        with patch.object(orch, '_run_deterministic_scanners', return_value=(True, 'Test suite passed')), patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
            passed, _ = orch.run_phase_qa()
        self.assertTrue(passed)
        inputs = json.loads(next((orch.dir_qa/'runs').glob('*/inputs.json')).read_text())
        self.assertIn(diff, inputs['user_prompt'])

    def test_qa_approval_requires_one_exact_final_marker_and_deterministic_success(self):
        orch, _ = self.seed()
        cases = ['Example: QA_STATUS: PASSED\nQA_STATUS: FAILED', 'QA_STATUS: PASSED\nExtra text',
                 '```\nQA_STATUS: PASSED\n```', 'QA_STATUS: PASSED (all good)', 'No verdict']
        with patch.object(orch, '_run_deterministic_scanners', return_value=(True, 'Tests passed')):
            for i, response in enumerate(cases, 1):
                with self.subTest(response=response), patch('orchestrator.EngineRunner.invoke_role', side_effect=lambda *a, **k: (framed('qa', orch).rsplit('QA_STATUS:', 1)[0] + response, 0, 0, 0, 'test')):
                    with self.assertRaises(TaskError):
                        orch.run_phase_qa(attempt=i)
        with patch.object(orch, '_run_deterministic_scanners', return_value=(False, 'Tests failed')), patch('orchestrator.EngineRunner.invoke_role', side_effect=lambda *a, **k: (framed('qa', orch), 0, 0, 0, 'test')):
            self.assertFalse(orch.run_phase_qa(attempt=6)[0])

    def test_qa_cannot_approve_a_checkpoint_changed_during_analysis(self):
        orch, _ = self.seed()
        def mutate(*args, **kwargs):
            (orch.worktree_dir/'app.py').write_text('unverified external change')
            return 'QA_STATUS: PASSED', 0, 0, 0, 'test'
        with patch.object(orch, '_run_deterministic_scanners', return_value=(True, 'Tests passed')), patch('orchestrator.EngineRunner.invoke_role', side_effect=mutate):
            self.assertFalse(orch.run_phase_qa()[0])
        self.assertEqual(orch.state['ledger'][-1]['status'], 'INPUT_CHANGED')
        self.assertFalse(orch.state['gate_history'][-1]['passed'])

    def test_qa_honors_explicit_full_diff_budget(self):
        orch, _ = self.seed()
        with patch.dict('os.environ', {'SSDLC_REVIEW_MAX_DIFF_CHARS': '60000'}), patch.object(orch, '_run_deterministic_scanners', return_value=(True, 'Tests passed')), patch('orchestrator.EngineRunner.invoke_role') as invoke:
            with self.assertRaisesRegex(TaskError, 'no truncation performed'):
                orch.run_phase_qa()
            invoke.assert_not_called()
