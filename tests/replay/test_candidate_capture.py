"""I0 offline replay: real Git and processes, semantic engines, no provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_task_bootstrap import RepositoryTests
from ssdlc_candidates import CandidateStore
from ssdlc_tasks import DevelopmentBlocked, TaskError, git


class CandidateCaptureTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/issue22-cache.json').read_text())
        (self.repo / '.gitignore').write_text(self.fixture['baseline']['.gitignore'])
        git(self.repo, 'add', '.gitignore')
        git(self.repo, 'commit', '-m', 'I0 replay baseline')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.plan['base_commit'] = self.quality['base_commit'] = self.base
        self.wt = self.worktree()
        self.assertEqual((self.wt / 'app.py').read_text(), self.fixture['baseline']['app.py'])
        self.calls = []
        self.checks = []

    def develop(self, task, attempt, prompt, workspace):
        self.calls.append((attempt, workspace))
        (workspace / 'app.py').write_text(f'value = {attempt}\n')
        return {'output': self.fixture['developer_output'], 'exit_code': 0,
                'engine': 'semantic', 'model': 'offline', 'quiescent': True}

    def verify(self, argv, cwd, timeout, workspace):
        self.checks.append(workspace)
        return 'Ran 1 test\nOK', '', 0

    def make(self, developer=None, verifier=None, **kwargs):
        return self.scheduler(developer or self.develop, verifier or self.verify,
                              isolate_candidates=True, **kwargs)

    def receipt(self):
        return self.artifacts / '03-dev/tasks/T1/attempt-1/capture/receipt.json'

    def fail_developer(self, *args):
        self.fail('A captured/uncertain invocation must not be dispatched again')

    def test_issue22_cleared_environment_cache_replay(self):
        def develop(task, attempt, prompt, workspace):
            result = self.develop(task, attempt, prompt, workspace)
            proc = subprocess.run([sys.executable, '-c', 'import app'], cwd=workspace,
                                  env={'PATH': os.environ.get('PATH', '')}, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(list(workspace.rglob('*.pyc')))
            return result
        def verify(argv, cwd, timeout, workspace):
            proc = subprocess.run([sys.executable, '-c', 'import app; assert app.value in (0, 1)'],
                                  cwd=cwd, env={'PATH': os.environ.get('PATH', '')}, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(list(workspace.rglob('*.pyc')))
            return self.verify(argv, cwd, timeout, workspace)
        result = self.make(develop, verify).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual(len(self.calls), self.fixture['expected']['developer_dispatches'])
        self.assertEqual(len(list(self.wt.rglob('*.pyc'))), self.fixture['expected']['authoritative_bytecode_files'])
        receipt = json.loads(self.receipt().read_text())
        self.assertTrue(receipt['generated_artifacts'])
        self.assertEqual(set(receipt['entries']), {'app.py'})
        self.assertEqual((self.repo / 'app.py').read_text(), 'value = 0\n')
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        self.assertEqual(len(self.checks), len(set(self.checks)))

    def test_secondary_guard_failure_resumes_without_new_development_attempt(self):
        def guard():
            if self.receipt().exists():
                raise TaskError('secondary guard failed')
        with self.assertRaisesRegex(TaskError, 'secondary guard'):
            self.make(guard=guard, max_attempts=1).run(self.plan['tasks'])
        self.assertEqual((self.wt / 'app.py').read_text(), 'value = 0\n')
        state = self.make().journal.peek()
        self.assertEqual(state['tasks']['T1']['status'], 'CANDIDATE_CAPTURED')
        self.assertEqual(state['tasks']['T1']['producer']['model'], 'offline')
        result = self.make(self.fail_developer, max_attempts=1).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['attempts'], 1)
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')

    def test_crash_after_capture_before_journal_reconciles_receipt(self):
        scheduler = self.make()
        save = scheduler._save
        def crash(event):
            if event == 'CANDIDATE_CAPTURED':
                raise OSError('power loss')
            save(event)
        scheduler._save = crash
        with self.assertRaises(OSError):
            scheduler.run(self.plan['tasks'])
        self.assertEqual(scheduler.journal.peek()['tasks']['T1']['status'], 'RUNNING')
        self.assertTrue(self.receipt().exists())
        self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)

    def test_partial_capture_never_authorizes_a_second_dispatch(self):
        import ssdlc_candidates as candidates
        write = candidates._write
        def partial(path, data, *args):
            write(path, data, *args)
            raise OSError('interrupted capture after blob fsync')
        with patch('ssdlc_candidates._write', side_effect=partial):
            with self.assertRaises(OSError):
                self.make().run(self.plan['tasks'])
        self.assertTrue(list(self.receipt().parent.parent.glob('.capture-*')))
        self.assertFalse(self.receipt().exists())
        for _ in range(2):
            with self.assertRaisesRegex(DevelopmentBlocked, 'uncertain'):
                self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)

    def test_unconfirmed_writers_are_uncertain(self):
        def develop(*args):
            result = self.develop(*args)
            result['quiescent'] = False
            return result
        with self.assertRaisesRegex(DevelopmentBlocked, 'quiescent'):
            self.make(develop).run(self.plan['tasks'])
        with self.assertRaisesRegex(DevelopmentBlocked, 'uncertain'):
            self.make(self.fail_developer).run(self.plan['tasks'])

    def test_payload_tampering_never_reinvokes_developer(self):
        with self.assertRaises(TaskError):
            self.make(guard=lambda: (_ for _ in ()).throw(TaskError('stop')) if self.receipt().exists() else None).run(self.plan['tasks'])
        blob = next((self.receipt().parent / 'blobs').iterdir())
        blob.write_text('tampered')
        with self.assertRaisesRegex(DevelopmentBlocked, 'payload changed'):
            self.make(self.fail_developer).run(self.plan['tasks'])

    def test_unknown_ignored_files_remain_blocking(self):
        def develop(*args):
            result = self.develop(*args)
            (args[-1] / '.gitignore').write_text('unknown.secret\n')
            (args[-1] / 'unknown.secret').write_text('not a cache')
            return result
        with self.assertRaisesRegex(DevelopmentBlocked, 'scope_required'):
            self.make(develop).run(self.plan['tasks'])
        self.assertTrue(self.receipt().exists())
        self.assertFalse((self.wt / 'unknown.secret').exists())

    def test_symlink_escape_is_preserved_but_not_imported(self):
        def develop(*args):
            result = self.develop(*args)
            (args[-1] / 'app.py').unlink()
            (args[-1] / 'app.py').symlink_to(self.repo / 'app.py')
            return result
        with self.assertRaisesRegex(DevelopmentBlocked, 'unsafe_entry'):
            self.make(develop).run(self.plan['tasks'])
        self.assertTrue(self.receipt().exists())
        self.assertFalse((self.wt / 'app.py').is_symlink())
        self.assertEqual((self.repo / 'app.py').read_text(), 'value = 0\n')

    def test_protected_file_and_git_metadata_are_blocking(self):
        def develop(*args):
            result = self.develop(*args)
            workspace = args[-1]
            (workspace / '.agents').mkdir()
            (workspace / '.agents/instructions').write_text('approve')
            with (workspace / '.git/config').open('a') as stream:
                stream.write('\n[alias]\n unsafe = something\n')
            return result
        with self.assertRaisesRegex(DevelopmentBlocked, 'protected_'):
            self.make(develop).run(self.plan['tasks'])
        self.assertTrue(self.receipt().exists())
        self.assertFalse((self.wt / '.agents').exists())

    def test_source_mutation_in_verification_worker_cannot_pass(self):
        def verify(argv, cwd, timeout, workspace):
            if self.calls:
                (workspace / 'app.py').write_text('value = 999\n')
            return self.verify(argv, cwd, timeout, workspace)
        with self.assertRaisesRegex(TaskError, 'Verification changed source'):
            self.make(verifier=verify).run(self.plan['tasks'])
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual((self.wt / 'app.py').read_text(), 'value = 1\n')
        self.make(self.fail_developer).run(self.plan['tasks'])

    def test_new_files_deletions_and_failed_candidate_repair(self):
        self.plan['tasks'][0]['target_files'] = ['app.py', 'new.py']
        def develop(task, attempt, prompt, workspace):
            self.calls.append((attempt, workspace))
            if attempt == 2:
                self.assertEqual((workspace / 'new.py').read_text(), 'value = 1\n')
                self.assertFalse((workspace / 'app.py').exists())
                self.assertIn('Repair the restored previous candidate', prompt)
            (workspace / 'app.py').unlink(missing_ok=True)
            (workspace / 'new.py').write_text(f'value = {attempt}\n')
            return {'output': 'TASK_DEV_STATUS: COMPLETE', 'exit_code': 0, 'quiescent': True}
        def verify(argv, cwd, timeout, workspace):
            return 'Ran 1 test', '', 1 if (workspace / 'new.py').exists() and len(self.calls) == 1 else 0
        result = self.make(develop, verify).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['attempts'], 2)
        self.assertFalse((self.wt / 'app.py').exists())
        self.assertEqual((self.wt / 'new.py').read_text(), 'value = 2\n')

    def test_resume_does_not_overwrite_interrupted_verification_evidence(self):
        scheduler = self.make()
        save = scheduler._save
        def crash(event):
            save(event)
            if event == 'CHECK_FINISHED' and self.calls:
                raise OSError('power loss')
        scheduler._save = crash
        with self.assertRaises(OSError):
            scheduler.run(self.plan['tasks'])
        first = self.receipt().parent.parent / 'verification-1/check-0.log'
        data = first.read_bytes()
        result = self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(first.read_bytes(), data)
        self.assertEqual(len(result['tasks']['T1']['capture_verifications']), 2)

    def test_historical_mode_cannot_silently_adopt_new_controller_semantics(self):
        legacy = self.scheduler(lambda *args: (_ for _ in ()).throw(OSError('before dispatch')))
        with self.assertRaises(OSError):
            legacy.run(self.plan['tasks'])
        with self.assertRaisesRegex(TaskError, 'migration'):
            self.make(self.fail_developer).run(self.plan['tasks'])

    def test_uncertain_capture_uses_existing_supervisor_class(self):
        import ssdlc_supervisor as supervisor
        evidence = {'state': {'status': 'ESCALATED_TO_SUPERVISOR',
                              'failure': {'category': 'DEVELOPER_INVOCATION'}},
                    'journal': {'candidate_isolation': 1, 'tasks': {'T1': {'status': 'RUNNING', 'attempts': 1}}}}
        self.assertEqual(supervisor.classify(evidence)['class'], 'uncertain_outcome')
        evidence['integrity'] = True
        self.assertEqual(supervisor.classify(evidence)['class'], 'integrity')
        evidence.pop('integrity')
        evidence['hard_deadline'] = True
        self.assertEqual(supervisor.classify(evidence)['class'], 'limits')

    def test_partial_source_adoption_is_idempotent(self):
        self.plan['tasks'][0]['target_files'].append('new.py')
        def develop(*args):
            result = self.develop(*args)
            (args[-1] / 'new.py').write_text('new = True\n')
            return result
        replace = os.replace
        def crash(source, target):
            replace(source, target)
            if Path(target) == self.wt / 'app.py':
                raise OSError('after first source adoption')
        with patch('ssdlc_candidates.os.replace', side_effect=crash):
            with self.assertRaises(OSError):
                self.make(develop).run(self.plan['tasks'])
        self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertTrue((self.wt / 'new.py').exists())
        self.assertEqual(len(self.calls), 1)

    def test_commit_intent_crashes_never_repeat_development_or_commit(self):
        scheduler = self.make()
        save = scheduler._save
        def crash(event):
            if event == 'TASK_COMPLETED':
                raise OSError('after commit')
            save(event)
        scheduler._save = crash
        with self.assertRaises(OSError):
            scheduler.run(self.plan['tasks'])
        self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(git(self.wt, 'rev-list', '--count', self.base + '..HEAD'), '1')
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_verification_allowance_is_persistent(self):
        def verify(*args):
            if self.calls:
                raise OSError('worker lost')
            return self.verify(*args)
        for _ in range(3):
            with self.assertRaises(OSError):
                self.make(verifier=verify).run(self.plan['tasks'])
        with self.assertRaisesRegex(DevelopmentBlocked, 'allowance exhausted'):
            self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)

    def test_runtime_identity_change_requires_migration(self):
        self.make(runtime_identity={'controller': 'version1'}).run(self.plan['tasks'])
        with self.assertRaisesRegex(TaskError, 'runtime identity changed'):
            self.make(self.fail_developer, runtime_identity={'controller': 'version2'}).run(self.plan['tasks'])

    def test_later_dag_tasks_use_the_current_checkpoint(self):
        self.plan['tasks'].append(self.task('T2', [], [], ['second.py'], ['T1']))
        def develop(task, attempt, prompt, workspace):
            if task['task_id'] == 'T2':
                self.assertEqual((workspace / 'app.py').read_text(), 'value = 1\n')
                (workspace / 'second.py').write_text('second = True\n')
                self.calls.append((attempt, workspace))
                return {'output': 'TASK_DEV_STATUS: COMPLETE', 'exit_code': 0, 'quiescent': True}
            return self.develop(task, attempt, prompt, workspace)
        result = self.make(develop).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T2']['status'], 'COMPLETED')
        self.assertEqual(git(self.wt, 'rev-list', '--count', self.base + '..HEAD'), '2')

    def test_docker_fallback_never_executes_on_host(self):
        from orchestrator import EngineRunner
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), \
                patch.object(EngineRunner, '_run_role_command') as execute:
            with self.assertRaises(DevelopmentBlocked):
                EngineRunner.run_sandboxed_cmd(['python3', '-V'], self.wt, verification_worktree=self.wt)
            with self.assertRaises(DevelopmentBlocked):
                EngineRunner.run_codex('inert', self.wt, require_container=True)
            with self.assertRaises(DevelopmentBlocked):
                EngineRunner.run_claude('inert', self.wt, require_container=True)
            execute.assert_not_called()

    def test_before_commit_crash_recovers_exact_verified_candidate(self):
        scheduler = self.make()
        with patch.object(scheduler, '_complete_intent', side_effect=OSError('before commit')):
            with self.assertRaises(OSError):
                scheduler.run(self.plan['tasks'])
        result = self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual(len(self.calls), 1)

    def test_completed_checkpoint_keeps_capture_evidence_bound(self):
        self.make().run(self.plan['tasks'])
        (self.receipt().parent / 'developer.log').write_text('changed')
        with self.assertRaisesRegex(TaskError, 'evidence'):
            self.make(self.fail_developer).run(self.plan['tasks'])

    def test_verification_mount_destinations_are_unique(self):
        from orchestrator import EngineRunner
        workspace = self.artifacts / 'disposable'
        nested = workspace / 'tests'
        nested.mkdir(parents=True)
        for cwd, writable in [(workspace, workspace), (nested, workspace), (self.repo, self.repo)]:
            with self.subTest(cwd=cwd):
                command = EngineRunner._wrap_in_role_container(
                    ['python3', '-V'], cwd, writable_worktree=writable, pass_ai_keys=False)
                destinations = [command[i + 1].split(':')[1] for i, value in enumerate(command) if value == '-v']
                self.assertEqual(len(destinations), len(set(destinations)))
                self.assertIn(f'{writable.resolve()}:{writable.resolve()}:rw', command)

    def test_pytest_cache_policy_does_not_allow_arbitrary_cache_directory_source(self):
        def develop(*args):
            result = self.develop(*args)
            directory = args[-1] / '.pytest_cache/v/cache'
            directory.mkdir(parents=True)
            (directory / 'nodeids').write_text('[]')
            (directory / 'evil.py').write_text('not a disposable cache')
            return result
        with self.assertRaisesRegex(DevelopmentBlocked, 'scope_required'):
            self.make(develop).run(self.plan['tasks'])
        receipt = json.loads(self.receipt().read_text())
        self.assertIn('.pytest_cache/v/cache/nodeids', receipt['generated_artifacts'])
        self.assertIn('.pytest_cache/v/cache/evil.py', receipt['entries'])
        self.assertFalse((self.wt / '.pytest_cache').exists())

    def test_checkout_does_not_execute_host_configured_filters(self):
        from ssdlc_candidates import checkout
        (self.wt / '.gitattributes').write_text('app.py filter=tripwire\n')
        git(self.wt, 'add', '.gitattributes')
        git(self.wt, 'commit', '-m', 'baseline with filter reference')
        marker = self.repo.parent / 'filter-was-executed'
        configuration = self.repo.parent / 'host.gitconfig'
        configuration.write_text(f'[filter "tripwire"]\n smudge = touch {marker}\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(configuration)}):
            checkout(self.wt, self.artifacts / 'filter-checkout', git(self.wt, 'rev-parse', 'HEAD'))
        self.assertFalse(marker.exists())

    def test_crash_before_atomic_adoption_leaves_no_unknown_source(self):
        replace = os.replace
        def crash(source, target):
            if Path(target) == self.wt / 'app.py':
                raise OSError('before source rename')
            return replace(source, target)
        with patch('ssdlc_candidates.os.replace', side_effect=crash):
            with self.assertRaises(OSError):
                self.make().run(self.plan['tasks'])
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        self.assertTrue(list(self.receipt().parent.parent.glob('.adopt-*')))
        self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)

    def test_verification_reservation_precedes_directory_creation(self):
        scheduler = self.make()
        save = scheduler._save
        def crash(event):
            if event == 'CAPTURE_VERIFICATION_STARTED':
                raise OSError('before reservation commit')
            save(event)
        scheduler._save = crash
        with self.assertRaises(OSError):
            scheduler.run(self.plan['tasks'])
        self.assertFalse((self.receipt().parent.parent / 'verification-1').exists())
        self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)

    def test_failed_rollback_is_reconciled_without_duplicate_attempt(self):
        scheduler = self.make(max_attempts=1)
        def verify(argv, cwd, timeout, workspace):
            return 'Ran 1 test', '', int((workspace / 'app.py').read_text() == 'value = 1\n')
        scheduler.verifier = verify
        with patch.object(scheduler, '_rollback_attempt', side_effect=OSError('before rollback')):
            with self.assertRaises(OSError):
                scheduler.run(self.plan['tasks'])
        self.assertNotEqual(git(self.wt, 'status', '--porcelain'), '')
        with self.assertRaisesRegex(TaskError, 'exhausted'):
            self.make(self.fail_developer, max_attempts=1).run(self.plan['tasks'])
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        self.assertEqual(len(self.calls), 1)

    def test_mode_downgrade_refused_by_invalidation_and_revalidation(self):
        scheduler = self.make()
        scheduler.run(self.plan['tasks'])
        before = scheduler.journal.path.read_bytes()
        historical = self.scheduler(self.fail_developer)
        with self.assertRaisesRegex(TaskError, 'migration'):
            historical.invalidate(['T1'], 'must not downgrade')
        with self.assertRaisesRegex(TaskError, 'migration'):
            historical.revalidate()
        self.assertEqual(scheduler.journal.path.read_bytes(), before)

    def test_checkpoint_does_not_execute_repository_commit_hooks(self):
        directory = Path(git(self.wt, 'rev-parse', '--git-common-dir'))
        if not directory.is_absolute():
            directory = (self.wt / directory).resolve()
        hook = directory / 'hooks/pre-commit'
        marker = self.repo.parent / 'hook-executed'
        hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
        hook.chmod(0o755)
        self.make().run(self.plan['tasks'])
        self.assertFalse(marker.exists())

    def test_before_dispatch_crash_reuses_reserved_identity_and_attempt(self):
        scheduler = self.make(max_attempts=1)
        save = scheduler._save
        def crash(event):
            save(event)
            if event == 'DISPATCH_RESERVED':
                raise OSError('before workspace/provider dispatch')
        scheduler._save = crash
        with self.assertRaises(OSError):
            scheduler.run(self.plan['tasks'])
        record = scheduler.journal.peek()['tasks']['T1']
        self.assertEqual(record['status'], 'DISPATCH_RESERVED')
        self.assertFalse(self.calls)
        result = self.make(max_attempts=1).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['invocation_id'], record['invocation_id'])
        self.assertEqual(result['tasks']['T1']['attempts'], 1)
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_preparation_is_preserved_before_reserved_dispatch(self):
        import ssdlc_candidates as candidates
        checkout = candidates.checkout
        def crash(*args):
            checkout(*args)
            raise OSError('after host preparation')
        scheduler = self.make(max_attempts=1)
        original_capture = scheduler._capture_developer
        def capture(*args):
            # Preflight stays real; only developer preparation is interrupted.
            with patch('ssdlc_candidates.checkout', side_effect=crash):
                return original_capture(*args)
        scheduler._capture_developer = capture
        with self.assertRaises(OSError):
            scheduler.run(self.plan['tasks'])
        self.assertFalse(self.calls)
        self.make(max_attempts=1).run(self.plan['tasks'])
        self.assertTrue(list(self.receipt().parent.parent.glob('interrupted-preparation-*')))
        self.assertEqual(len(self.calls), 1)
