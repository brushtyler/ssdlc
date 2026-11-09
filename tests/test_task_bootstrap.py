import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import EngineRunner, SSDLCOrchestrator
from ssdlc_tasks import (
    PlanError, TaskError, TaskJournal, TaskPlanValidator, TaskScheduler,
    atomic_json, confined, file_hash, git, ticket_lock, validate_document,
)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        git(self.repo, 'config', 'user.name', 'SSDLC Test')
        git(self.repo, 'config', 'user.email', 'ssdlc@example.invalid')
        (self.repo / '.gitignore').write_text('.ssdlc/\n')
        (self.repo / 'app.py').write_text('value = 0\n')
        git(self.repo, 'add', '.gitignore', 'app.py')
        git(self.repo, 'commit', '-m', 'baseline')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.artifacts = self.repo / '.ssdlc' / 'TKT-1'
        (self.artifacts / '02-design').mkdir(parents=True)
        self.log = self.artifacts / 'baseline.log'
        self.log.write_text('Ran 1 test\nOK')
        self.quality = {
            'schema_version': '1.0', 'contract_revision': 1, 'base_commit': self.base,
            'sources': [], 'standards': [], 'limitations': [], 'exceptions': [],
            'checks': [{'check_id': 'CHECK-GLOBAL', 'argv': ['python3', '-m', 'unittest'], 'cwd': '.', 'timeout_sec': 5, 'kind': 'test', 'required': True}],
            'baseline': [{'check_id': 'CHECK-GLOBAL', 'exit_code': 0, 'log_ref': 'baseline.log', 'sha256': file_hash(self.log)}],
        }
        self.plan = {
            'schema_version': '1.0', 'plan_revision': 1, 'ticket_id': 'TKT-1',
            'execution_strategy': 'sequential_dag', 'base_commit': self.base,
            'quality_contract_ref': '00-recon/quality-contract.json',
            'tasks': [self.task('T1', ['AC-1'], ['SEC-1'], ['app.py'])],
        }

    def task(self, task_id, ac, sec, targets, deps=None):
        return {'task_id': task_id, 'title': 'Implement ' + task_id, 'description': 'Scoped behavior',
                'depends_on': deps or [], 'target_files': targets, 'ac_refs': ac, 'sec_refs': sec,
                'standard_refs': [], 'profile': 'ALGORITHMIC', 'complexity': 'standard',
                'kind': 'task', 'contract_refs': [],
                'verification': {'mode': 'automated', 'quality_check_refs': ['CHECK-GLOBAL'],
                                 'checks': [{'check_id': 'CHECK-' + task_id, 'argv': ['python3', '-m', 'unittest'],
                                             'cwd': '.', 'timeout_sec': 5, 'ac_refs': ac, 'sec_refs': sec}]}}

    def validator(self, requirements='AC-1 SEC-1'):
        return TaskPlanValidator(self.repo, requirements, self.quality, self.artifacts)

    def worktree(self):
        wt = self.artifacts / 'worktree'
        git(self.repo, 'worktree', 'add', '-b', 'ssdlc/test', str(wt), self.base)
        return wt

    def scheduler(self, developer, verifier=None, wt=None, **kwargs):
        return TaskScheduler(wt or self.wt, self.artifacts / '03-dev' / 'tasks', self.plan,
                             self.quality, {'requirements': 'AC-1 SEC-1'}, developer,
                             verifier or (lambda *args: ('Ran 1 test\nOK', '', 0)), **kwargs)


class ValidatorTests(RepositoryTests):
    def test_schema_and_deterministic_topological_order(self):
        second = self.task('T2', ['AC-2'], [], ['second.py'], ['T1'])
        self.plan['tasks'].insert(0, second)
        ordered = self.validator('AC-1 AC-2 SEC-1').validate(self.plan)
        self.assertEqual([t['task_id'] for t in ordered], ['T1', 'T2'])

    def test_invalid_plans_fail_before_execution(self):
        cases = [
            lambda p: p['tasks'].append(copy.deepcopy(p['tasks'][0])),
            lambda p: p['tasks'][0].update(depends_on=['missing']),
            lambda p: p['tasks'][0].update(depends_on=['T1']),
            lambda p: p['tasks'][0].update(ac_refs=['AC-999']),
            lambda p: p['tasks'][0]['verification'].update(checks=[]),
            lambda p: p['tasks'][0]['verification'].update(quality_check_refs=[]),
            lambda p: p['tasks'][0].update(standard_refs=['STD-UNKNOWN']),
            lambda p: p.update(plan_revision=True),
            lambda p: p.update(schema_version='9.0'),
            lambda p: p['tasks'][0].update(target_files=['../outside.py']),
            lambda p: p['tasks'][0].update(target_files=['.agents/rules.md']),
            lambda p: p['tasks'][0].update(target_files=[':!app.py']),
        ]
        for modify in cases:
            plan = copy.deepcopy(self.plan)
            modify(plan)
            with self.subTest(plan=plan), self.assertRaises(PlanError):
                self.validator().validate(plan)

    def test_cycle_and_uncovered_requirements(self):
        self.plan['tasks'].append(self.task('T2', [], [], ['second.py'], ['T1']))
        self.plan['tasks'][0]['depends_on'] = ['T2']
        with self.assertRaisesRegex(PlanError, 'Cycle'):
            self.validator().validate(self.plan)
        self.plan['tasks'][0]['depends_on'] = []
        with self.assertRaisesRegex(PlanError, 'Uncovered'):
            self.validator('AC-1 AC-2 SEC-1').validate(self.plan)

    def test_symlink_escape_and_protected_paths(self):
        (self.repo / 'escape').symlink_to(self.repo.parent, target_is_directory=True)
        for path in ('escape/file.py', '.git/config', '.ssdlc/state.json', '/tmp/out', 'bad\npath'):
            with self.subTest(path=path), self.assertRaises(PlanError):
                confined(self.repo, path)

    def test_sources_baselines_and_standard_coverage(self):
        source = self.repo / 'AGENTS.md'
        source.write_text('Reuse the existing API client')
        self.quality['sources'] = [{'source_id': 'S1', 'path': 'AGENTS.md', 'sha256': file_hash(source)}]
        self.quality['standards'] = [{'standard_id': 'STD-1', 'description': 'Reuse', 'source_refs': ['S1'], 'scope': ['*.py']}]
        with self.assertRaisesRegex(PlanError, 'Missing applicable'):
            self.validator().validate(self.plan)
        self.plan['tasks'][0]['standard_refs'] = ['STD-1']
        self.validator().validate(self.plan)
        source.unlink()
        with self.assertRaisesRegex(PlanError, 'disappeared'):
            self.validator().validate(self.plan)
        self.quality['sources'] = []
        self.quality['standards'] = []
        self.plan['tasks'][0]['standard_refs'] = []
        self.log.unlink()
        with self.assertRaisesRegex(PlanError, 'baseline'):
            self.validator().validate(self.plan)

    def test_adr_reference_requires_real_heading(self):
        adr = self.artifacts / '02-design' / 'adr.md'
        adr.write_text('# Token Schema\nUse existing types')
        self.plan['tasks'][0]['contract_refs'] = ['adr.md#token-schema']
        self.validator().validate(self.plan)
        self.plan['tasks'][0]['contract_refs'] = ['adr.md#invented']
        with self.assertRaisesRegex(PlanError, 'anchor'):
            self.validator().validate(self.plan)

    def test_all_published_schemas_are_executable(self):
        from jsonschema import Draft202012Validator
        for path in (Path(__file__).resolve().parents[1] / 'schemas').glob('*.json'):
            Draft202012Validator.check_schema(json.loads(path.read_text()))


class JournalTests(unittest.TestCase):
    def test_journal_recovers_stale_snapshot_and_partial_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            journal = TaskJournal(directory)
            journal.append('START', {'value': 1})
            with patch('ssdlc_tasks.atomic_json', side_effect=OSError('power loss')):
                with self.assertRaises(OSError):
                    journal.append('CHECKPOINT', {'value': 2})
            with journal.path.open('ab') as stream:
                stream.write(b'{incomplete')
            recovered = TaskJournal(directory).load()
            self.assertEqual(recovered, {'value': 2})
            self.assertEqual(json.loads(journal.snapshot.read_text()), recovered)
            self.assertTrue(list(directory.glob('interrupted-tail-*.bin')))

    def test_corrupt_complete_event_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = TaskJournal(Path(tmp))
            journal.append('START', {'value': 1})
            journal.path.write_text(journal.path.read_text().replace('"value": 1', '"value": 2'))
            with self.assertRaisesRegex(TaskError, 'Corrupt'):
                TaskJournal(Path(tmp)).load()

    def test_lock_rejects_concurrent_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ticket.lock'
            with ticket_lock(path):
                with self.assertRaisesRegex(TaskError, 'Another process'):
                    with ticket_lock(path):
                        pass
            with ticket_lock(path):
                pass


class SchedulerTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.wt = self.worktree()
        self.calls = []

    def developer(self, task, attempt, prompt):
        self.calls.append((task['task_id'], attempt))
        for path in task['target_files']:
            (self.wt / path).write_text(f'value = {attempt}\n')
        return {'output': 'Implemented', 'exit_code': 0, 'model': 'fake', 'engine': 'fake'}

    def test_checkpoints_resume_and_cumulative_diff(self):
        self.plan['tasks'].append(self.task('T2', [], [], ['second.py'], ['T1']))
        scheduler = self.scheduler(self.developer)
        result = scheduler.run(self.plan['tasks'])
        self.assertEqual([call[0] for call in self.calls], ['T1', 'T2'])
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        self.assertEqual(git(self.wt, 'rev-list', '--count', self.base + '..HEAD'), '2')
        self.assertIn('second.py', git(self.wt, 'diff', self.base, 'HEAD'))
        self.scheduler(lambda *args: self.fail('completed work rerun')).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual((self.repo / 'app.py').read_text(), 'value = 0\n')

    def test_failed_attempt_is_archived_then_restored_for_retry(self):
        def verify(*args):
            return ('Ran 1 test', 'failure' if len(self.calls) == 1 else '', 1 if len(self.calls) == 1 else 0)
        def develop(task, attempt, prompt):
            self.assertEqual((self.wt / 'app.py').read_text(), 'value = 0\n')
            return self.developer(task, attempt, prompt)
        result = self.scheduler(develop, verify).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['attempts'], 2)
        self.assertTrue((self.artifacts / '03-dev/tasks/T1/attempt-1/implementation.diff').exists())
        self.assertEqual(git(self.wt, 'rev-list', '--count', self.base + '..HEAD'), '1')

    def test_attempt_limit_is_persistent(self):
        failed = lambda *args: ('Ran 1 test', 'FAIL', 1)
        for _ in range(2):
            with self.assertRaisesRegex(TaskError, 'exhausted'):
                self.scheduler(self.developer, failed, max_attempts=1).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_crash_after_commit_before_state_update_recovers_without_duplicate(self):
        scheduler = self.scheduler(self.developer)
        original = scheduler._save
        def crash(event):
            if event == 'TASK_COMPLETED':
                raise OSError('power loss')
            original(event)
        scheduler._save = crash
        with self.assertRaises(OSError):
            scheduler.run(self.plan['tasks'])
        result = self.scheduler(lambda *args: self.fail('duplicate generation')).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual(git(self.wt, 'rev-list', '--count', self.base + '..HEAD'), '1')

    def test_crash_before_commit_recovers_exact_verified_tree(self):
        scheduler = self.scheduler(self.developer)
        scheduler._complete_intent = lambda *args: (_ for _ in ()).throw(OSError('power loss'))
        with self.assertRaises(OSError):
            scheduler.run(self.plan['tasks'])
        self.scheduler(lambda *args: self.fail('duplicate generation')).run(self.plan['tasks'])
        self.assertEqual(git(self.wt, 'rev-list', '--count', self.base + '..HEAD'), '1')

    def test_out_of_scope_changes_are_preserved_and_block_retry(self):
        def develop(*args):
            result = self.developer(*args)
            (self.wt / 'unexpected.py').write_text('external = True')
            return result
        with self.assertRaisesRegex(TaskError, 'manifest'):
            self.scheduler(develop).run(self.plan['tasks'])
        with self.assertRaisesRegex(TaskError, 'dirty'):
            self.scheduler(self.developer).run(self.plan['tasks'])
        self.assertTrue((self.wt / 'unexpected.py').exists())

    def test_verification_cannot_change_already_modified_source(self):
        def verify(*args):
            (self.wt / 'app.py').write_text('value = 999\n')
            return 'Ran 1 test\nOK', '', 0
        with self.assertRaisesRegex(TaskError, 'Verification changed'):
            self.scheduler(self.developer, verify).run(self.plan['tasks'])
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), self.base)

    def test_hooks_cannot_commit_unverified_content(self):
        git_dir = Path(git(self.repo, 'rev-parse', '--absolute-git-dir'))
        hook = git_dir / 'hooks' / 'pre-commit'
        hook.write_text('#!/bin/sh\nprintf "value = 99\\n" > app.py\ngit add app.py\n')
        hook.chmod(0o755)
        with self.assertRaisesRegex(TaskError, 'differs'):
            self.scheduler(self.developer).run(self.plan['tasks'])

    def test_changed_inputs_and_missing_evidence_are_not_reused(self):
        self.scheduler(self.developer).run(self.plan['tasks'])
        plan = copy.deepcopy(self.plan)
        self.plan['tasks'][0]['description'] = 'Changed objective'
        with self.assertRaisesRegex(TaskError, 'Inputs changed'):
            self.scheduler(self.developer).run(self.plan['tasks'])
        self.plan = plan
        log = self.artifacts / '03-dev/tasks/T1/attempt-1/check-0.log'
        log.unlink()
        with self.assertRaisesRegex(TaskError, 'evidence'):
            self.scheduler(self.developer).run(self.plan['tasks'])

    def test_empty_suite_subgoal_and_review_feedback_cannot_pass(self):
        with self.assertRaisesRegex(TaskError, 'exhausted'):
            self.scheduler(self.developer, lambda *args: ('Ran 0 tests', '', 0), max_attempts=1).run(self.plan['tasks'])
        self.plan['tasks'][0]['kind'] = 'subgoal'
        with self.assertRaisesRegex(TaskError, 'automated tasks only'):
            self.scheduler(self.developer).run(self.plan['tasks'])

    def test_new_ignored_files_are_not_silently_omitted(self):
        git_dir = Path(git(self.repo, 'rev-parse', '--absolute-git-dir'))
        with (git_dir / 'info/exclude').open('a') as stream:
            stream.write('*.log\n')
        def develop(*args):
            result = self.developer(*args)
            (self.wt / 'unexpected.log').write_text('unverified output')
            return result
        with self.assertRaisesRegex(TaskError, 'ignored files'):
            self.scheduler(develop).run(self.plan['tasks'])
        self.assertTrue((self.wt / 'unexpected.log').exists())

    def test_failed_attempt_unlinks_new_symlink_without_deleting_its_target(self):
        self.plan['tasks'][0]['target_files'] = ['new.py']
        def develop(*args):
            (self.wt / 'new.py').symlink_to('app.py')
            return {'output': 'Link', 'exit_code': 0}
        with self.assertRaisesRegex(TaskError, 'exhausted'):
            self.scheduler(develop, lambda *args: ('Ran 1 test', 'FAIL', 1), max_attempts=1).run(self.plan['tasks'])
        self.assertTrue((self.wt / 'app.py').exists())
        self.assertFalse((self.wt / 'new.py').exists())
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_protected_ignored_file_is_detected(self):
        def develop(*args):
            result = self.developer(*args)
            (self.wt / '.ssdlc').mkdir()
            (self.wt / '.ssdlc/state.json').write_text('{}')
            return result
        with self.assertRaisesRegex(TaskError, 'protected'):
            self.scheduler(develop).run(self.plan['tasks'])


class SandboxTests(unittest.TestCase):
    def test_local_cache_is_unique_and_ai_tokens_are_removed(self):
        seen = []
        def invoke(cmd, **kwargs):
            env = kwargs['env']
            self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('CODEX_HOME', env)
            self.assertIn('--strict-markers', env['PYTEST_ADDOPTS'])
            self.assertTrue(Path(env['TMPDIR']).is_dir())
            seen.append(env['TMPDIR'])
            return subprocess.CompletedProcess(cmd, 0, 'OK', '')
        with patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': 'test', 'OPENAI_API_KEY': 'test', 'PYTEST_ADDOPTS': '--strict-markers'}), patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), patch('orchestrator.subprocess.run', side_effect=invoke):
            for _ in range(2):
                EngineRunner.run_sandboxed_cmd(['python3', '-m', 'unittest'], Path.cwd())
        self.assertNotEqual(*seen)
        self.assertTrue(all(not Path(path).exists() for path in seen))

    def test_test_container_masks_secrets_and_keeps_worktree_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            wt = repo / '.ssdlc/TKT-1/worktree'
            wt.mkdir(parents=True)
            cmd = EngineRunner._wrap_in_role_container(['pytest'], wt, pass_ai_keys=False)
            mounts = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == '-v']
            self.assertIn(f'{wt}:{wt}:ro', mounts)
            self.assertFalse(any('.credentials.json' in item or 'auth.json' in item for item in mounts))
            self.assertFalse(any('.runtime' in item and item.endswith(':rw') for item in mounts))
            self.assertIn(f'{repo / ".ssdlc"}:rw,size=16m', cmd)
            self.assertIn('/opt/ssdlc/.ssdlc:rw,size=16m', cmd)


class OrchestratorBootstrapTests(RepositoryTests):
    def make_orchestrator(self):
        runtime = self.repo / '.ssdlc/.runtime'
        meta = {'project_id': 'test-project', 'codex_home': str(runtime / 'codex'),
                'claude_home': str(runtime / 'claude'), 'cache_dir': str(runtime / 'cache')}
        with patch('orchestrator.ProjectRuntimeIsolation.activate', return_value=meta), patch('orchestrator.get_cli_version', return_value='TEST'):
            return SSDLCOrchestrator(self.repo, 'TKT-1', sync_remote=False)

    def seed_inputs(self, orch):
        requirements = orch.dir_po / 'requirements.md'
        requirements.write_text('AC-1: Behavior\nSEC-1: Security invariant\n')
        quality_path = orch.dir_recon / 'quality-contract.json'
        plan_path = orch.dir_design / 'plan.json'
        atomic_json(quality_path, self.quality)
        atomic_json(plan_path, self.plan)
        for name, path in [('01-po/requirements.md', requirements), ('00-recon/quality-contract.json', quality_path), ('02-design/plan.json', plan_path)]:
            orch.record_artifact(name, path)

    def test_task_path_uses_existing_routing_and_resumes_without_ai(self):
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            self.seed_inputs(orch)
            def generate(prompt, **kwargs):
                (kwargs['cwd'] / 'app.py').write_text('value = 1\n')
                return 'Done', 0
            with patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('codex', 'fake', 'low', 'test')), patch('orchestrator.EngineRunner.run_codex', side_effect=generate) as engine, patch('orchestrator.EngineRunner.run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)):
                diff, files = orch.run_phase_dev()
                self.assertIn('app.py', files)
                self.assertIn('+value = 1', diff.read_text())
                orch.run_phase_dev()
                self.assertEqual(engine.call_count, 1)
                self.assertEqual(orch.state['tasks_status']['T1']['status'], 'COMPLETED')

    def test_legacy_path_stays_default_and_enabled_ticket_cannot_downgrade(self):
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'false'}):
            orch = self.make_orchestrator()
            with patch.object(orch, '_run_phase_dev_legacy', return_value=('legacy', [])) as legacy:
                self.assertEqual(orch.run_phase_dev(), ('legacy', []))
                legacy.assert_called_once()
            orch.state['task_breakdown'] = True
            with patch.object(orch, '_run_task_development', return_value=('tasks', [])) as tasks:
                self.assertEqual(orch.run_phase_dev(), ('tasks', []))
                tasks.assert_called_once()

    def test_missing_or_tampered_plan_never_falls_back_to_monolithic(self):
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            self.seed_inputs(orch)
            plan = orch.dir_design / 'plan.json'
            plan.write_text('{}')
            with patch.object(orch, '_run_phase_dev_legacy') as legacy:
                with self.assertRaisesRegex(PlanError, 'unregistered or changed'):
                    orch.run_phase_dev()
                legacy.assert_not_called()

    def test_missing_registered_plan_is_not_regenerated(self):
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            self.seed_inputs(orch)
            (orch.dir_design / 'plan.json').unlink()
            with patch('orchestrator.EngineRunner.invoke_role') as engine:
                with self.assertRaisesRegex(PlanError, 'disappeared'):
                    orch.run_phase_dev()
                engine.assert_not_called()

    def test_registered_missing_upstream_artifact_blocks_code(self):
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            self.seed_inputs(orch)
            adr = orch.dir_design / 'adr.md'
            adr.write_text('# Approved')
            orch.record_artifact('02-design/adr.md', adr)
            adr.unlink()
            with patch('orchestrator.EngineRunner.run_codex') as engine:
                with self.assertRaisesRegex(TaskError, 'disappeared'):
                    orch.run_phase_dev()
                engine.assert_not_called()

    def test_worktree_is_pinned_to_recorded_base(self):
        orch = self.make_orchestrator()
        (self.repo / 'later.py').write_text('later = True')
        git(self.repo, 'add', 'later.py')
        git(self.repo, 'commit', '-m', 'target advanced')
        wt = orch._ensure_worktree()
        self.assertEqual(git(wt, 'rev-parse', 'HEAD'), self.base)
        self.assertFalse((wt / 'later.py').exists())

    def test_approve_keeps_evidence_and_does_not_commit_residual_changes(self):
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            wt = orch._ensure_worktree()
            (wt / 'app.py').write_text('value = 1\n')
            git(wt, 'add', 'app.py')
            git(wt, 'commit', '-m', 'verified checkpoint')
            head = git(wt, 'rev-parse', 'HEAD')
            orch.state.update(task_verified_head=head, status='AWAITING_SUPERVISOR_SIGNOFF')
            orch._save_state()
            evidence = orch.dir_dev / 'implementation.diff'
            evidence.write_text('Evidence must survive')
            (wt / 'unexpected.py').write_text('must not be committed')
            with self.assertRaisesRegex(TaskError, 'verified checkpoint'):
                orch.approve_and_merge()
            (wt / 'unexpected.py').unlink()
            orch.approve_and_merge()
            self.assertEqual(evidence.read_text(), 'Evidence must survive')
            self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), head)
            self.assertTrue(orch.state_file.exists())

    def test_baseline_and_plan_generation_include_the_complete_schema_and_context(self):
        tests_dir = self.repo / 'tests'
        tests_dir.mkdir()
        (tests_dir / 'test_app.py').write_text('import unittest\n')
        git(self.repo, 'add', 'tests/test_app.py')
        git(self.repo, 'commit', '-m', 'project test convention')
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            self.plan['base_commit'] = orch.state['base_commit']
            requirements = orch.dir_po / 'requirements.md'
            requirements.write_text('AC-1 SEC-1')
            orch.record_artifact('01-po/requirements.md', requirements)
            planned = copy.deepcopy(self.plan)
            planned['tasks'][0]['verification']['quality_check_refs'] = ['CHECK-TESTS']
            def invoke(engine, model, system, user, *args, **kwargs):
                payload = json.loads(user)
                if args[-1] == '02-design-task-plan':
                    self.assertIn('quality_contract', payload)
                    self.assertIn('additionalProperties', system)
                    out = json.dumps(planned)
                else:
                    self.assertEqual(payload['plan'], planned)
                    out = 'No missing scenarios found.\nTASK_PLAN_STATUS: APPROVED'
                return out, 0, 0, 0, 'test-trace'
            def dispatch(role, *args, **kwargs):
                return ('openrouter', 'reviewer' if role.endswith('sec') else 'planner', 'low', 'test')
            with patch('orchestrator.EngineRunner.run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)), patch('orchestrator.SquadDispatcher.dispatch_role', side_effect=dispatch), patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
                actual = orch._ensure_task_plan()
            self.assertEqual(actual, planned)
            self.assertTrue((orch.dir_recon / 'quality-contract.json').exists())
            self.assertTrue(orch.state['task_breakdown'])

    def test_late_phase_resume_cannot_mark_unreviewed_work_as_ready(self):
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            with self.assertRaisesRegex(TaskError, 'skipping later gates'):
                orch._run_pipeline('goal', from_phase='08-supervisor')

    def test_existing_task_bounce_is_explicitly_blocked(self):
        self.wt = self.worktree()
        # Feedback must never be silently ignored just because tasks are completed.
        with self.assertRaisesRegex(TaskError, 'explicit invalidation'):
            self.scheduler(lambda *args: self.fail('should not generate')).run(self.plan['tasks'], feedback='Fix behavior')


    def test_pipeline_runs_global_gates_and_bounce_cannot_be_ignored(self):
        for review_passes in (True, False):
            with self.subTest(review_passes=review_passes), patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true', 'SSDLC_EXECUTION_MODE': 'local'}):
                # Each scenario needs an independent branch and journal.
                with tempfile.TemporaryDirectory() as case:
                    repo = Path(case)
                    git(repo, 'init')
                    git(repo, 'config', 'user.name', 'SSDLC Test')
                    git(repo, 'config', 'user.email', 'ssdlc@example.invalid')
                    (repo / '.gitignore').write_text('.ssdlc/\n')
                    (repo / 'app.py').write_text('value = 0\n')
                    git(repo, 'add', '.gitignore', 'app.py')
                    git(repo, 'commit', '-m', 'base')
                    old_repo = self.repo
                    old_base = self.base
                    old_artifacts = self.artifacts
                    old_quality = self.quality
                    old_plan = self.plan
                    self.repo = repo
                    self.base = git(repo, 'rev-parse', 'HEAD')
                    self.artifacts = repo / '.ssdlc/TKT-1'
                    self.artifacts.mkdir(parents=True)
                    (self.artifacts / 'baseline.log').write_text('Ran 1 test\nOK')
                    self.quality = copy.deepcopy(old_quality)
                    self.quality['base_commit'] = self.base
                    self.plan = copy.deepcopy(old_plan)
                    self.plan['base_commit'] = self.base
                    try:
                        orch = self.make_orchestrator()
                        self.seed_inputs(orch)
                        orch.state.update(risk_tier='S', goal='Scoped goal')
                        orch._save_state()
                        def generate(prompt, **kwargs):
                            (kwargs['cwd'] / 'app.py').write_text('value = 1\n')
                            return 'Done', 0
                        with patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('codex', 'fake', 'low', 'test')), patch('orchestrator.EngineRunner.run_codex', side_effect=generate), patch('orchestrator.EngineRunner.run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)), patch.object(orch, 'run_phase_review', return_value=(review_passes, 'Review result')), patch.object(orch, 'run_phase_qa', return_value=(True, 'QA passed')) as qa:
                            if review_passes:
                                evidence = orch.run_pipeline('Scoped goal', from_phase='03-dev')
                                self.assertTrue(evidence.exists())
                                self.assertEqual(orch.state['status'], 'AWAITING_SUPERVISOR_SIGNOFF')
                                qa.assert_called_once()
                            else:
                                with self.assertRaisesRegex(TaskError, 'explicit invalidation'):
                                    orch.run_pipeline('Scoped goal', from_phase='03-dev')
                                self.assertEqual(orch.state['status'], 'ESCALATED_TO_SUPERVISOR')
                                qa.assert_not_called()
                                self.assertTrue((orch.ssdlc_root / 'EVIDENCE_PACK.md').exists())
                    finally:
                        self.repo, self.base, self.artifacts, self.quality, self.plan = old_repo, old_base, old_artifacts, old_quality, old_plan


class SetupTests(unittest.TestCase):
    def test_init_copies_only_generic_standards_and_preserves_existing_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'project'
            repo.mkdir()
            home = Path(tmp) / 'home'
            home.mkdir()
            git(repo, 'init')
            existing = repo / '.ssdlc/standards/LOCAL.md'
            existing.parent.mkdir(parents=True)
            existing.write_text('Existing project rule')
            script = Path(__file__).resolve().parents[1] / 'setup-ssdlc.sh'
            proc = subprocess.run(['bash', str(script), '--init-project', str(repo), '--mode', 'local', '--sync-remote', 'false'],
                                  env={'PATH': os.defpath, 'HOME': str(home)}, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            standards = {p.name for p in existing.parent.glob('*.md')}
            self.assertEqual(standards, {'LOCAL.md', '00-PROJECT_CONVENTIONS.md'})
            self.assertEqual(existing.read_text(), 'Existing project rule')
            self.assertIn('SSDLC_ENABLE_TASK_BREAKDOWN="false"', (repo / '.ssdlc/project.env').read_text())
            self.assertIn('.tools/python/bin/python3', (repo / 'ssdlc').read_text())
            syntax = subprocess.run(['bash', '-n', str(repo / 'ssdlc')], capture_output=True, text=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)


class AdvisorySchemaTests(unittest.TestCase):
    def test_plan_examples_match_published_schemas_and_require_real_evidence(self):
        from ssdlc_tasks import validate_advisory
        root = Path(__file__).resolve().parents[1]
        doc = (root / 'PLAN_TASK_ROUTING_AND_SUBGOALS.md').read_text()
        import re
        plan, advisory = [json.loads(block) for block in re.findall(r'^```json\n(.*?)^```$', doc, re.M | re.S)]
        validate_document('plan', plan)
        validate_document('advisory-proposals', advisory)
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            with self.assertRaisesRegex(PlanError, 'Missing advisory evidence'):
                validate_advisory(advisory, artifacts)
            proposal = advisory['proposals'][0]
            for ref in proposal['source_artifact_refs'] + proposal['evidence_refs']:
                target = artifacts / ref.split('#')[0]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('Evidence')
            hashes = validate_advisory(advisory, artifacts)
            self.assertTrue(hashes)
            self.assertTrue(all(len(value) == 64 for value in hashes.values()))
            proposal['id'] = 'AMBIGUOUS'
            with self.assertRaisesRegex(PlanError, 'unqualified'):
                validate_advisory(advisory, artifacts)


if __name__ == '__main__':
    unittest.main()
