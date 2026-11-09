import json
from pathlib import Path
from unittest.mock import patch

from orchestrator import SSDLCOrchestrator
from ssdlc_tasks import TaskError, atomic_json, git
from test_task_bootstrap import RepositoryTests


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
        return orch, diff

    def test_complete_large_diff_is_reviewed_in_verified_worktree_and_archived(self):
        orch, diff = self.seed()
        self.assertGreater(len(diff), 60000)
        def invoke(engine, model, system, user, cwd, *args, **kwargs):
            self.assertEqual(cwd, orch.worktree_dir)
            self.assertIn(diff, user)
            self.assertIn('FINAL_DIFF_SENTINEL', user)
            self.assertEqual(kwargs['sandbox'], 'read-only')
            return 'Review complete.\nREVIEW_STATUS: APPROVED', 0, 0, 0, 'test'
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
