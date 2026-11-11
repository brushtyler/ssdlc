"""I2 timeout replay uses real Git/journal and host-owned semantic role adapters."""
import subprocess
from unittest.mock import patch
from test_task_bootstrap import RepositoryTests
from ssdlc_tasks import TaskScheduler, TaskError, git
from ssdlc_recovery import QuiescentTimeout, RecoveryStop


class TimeoutRecoveryTests(RepositoryTests):
    def setUp(self):
        super().setUp(); self.wt = self.worktree(); self.calls = []

    def develop(self, task, attempt, prompt, workspace):
        self.calls.append(attempt)
        (workspace / 'app.py').write_text('value = 1\n')
        raise QuiescentTimeout(30, b'partial output', b'no completion marker',
                               {'container': 'owned-offline-role', 'removed': True, 'absence_confirmed': True})

    def make(self, developer=None, verifier=None, **kwargs):
        return self.scheduler(developer or self.develop, verifier or (lambda *a: ('Ran 1 test\nOK', '', 0)),
                              isolate_candidates=True, recover_timeouts=True, **kwargs)

    def test_quiescent_timeout_produces_checkpoint_without_another_dispatch(self):
        state = self.make().run(self.plan['tasks'])
        record = state['tasks']['T1']
        self.assertEqual(record['status'], 'COMPLETED'); self.assertEqual(record['attempts'], 1)
        self.assertEqual(record['completion_confirmation'], 'unconfirmed_completion')
        self.assertEqual(record['producer']['termination'], 'timeout')
        self.assertIsNone(record['producer']['exit_code'])
        self.make(developer=lambda *a: self.fail('No redispatch')).run(self.plan['tasks'])
        self.assertEqual(self.calls, [1])

    def test_unconfirmed_timeout_preserves_uncertainty_and_never_redispatches(self):
        def unknown(*a): self.calls.append(1); raise subprocess.TimeoutExpired(['redacted'], 30)
        with self.assertRaisesRegex(TaskError, 'not confirmed fenced'):
            self.make(developer=unknown).run(self.plan['tasks'])
        with self.assertRaises(TaskError): self.make(developer=lambda *a: self.fail('No redispatch')).run(self.plan['tasks'])
        self.assertEqual(self.calls, [1])

    def test_partial_candidate_failure_repairs_in_place_with_next_charged_attempt(self):
        def verifier(argv, cwd, timeout, workspace):
            return ('Ran 1 test\nOK', '', 0) if (workspace / 'app.py').read_text() in ('value = 0\n', 'value = 2\n') else ('Ran 1 test\nFAILED', '', 1)
        def develop(task, attempt, prompt, workspace):
            if attempt == 1: return self.develop(task, attempt, prompt, workspace)
            self.calls.append(attempt)
            self.assertEqual((workspace / 'app.py').read_text(), 'value = 1\n')
            (workspace / 'app.py').write_text('value = 2\n')
            return {'output': 'TASK_DEV_STATUS: COMPLETE', 'exit_code': 0, 'quiescent': True}
        state = self.make(developer=develop, verifier=verifier, max_attempts=2).run(self.plan['tasks'])
        self.assertEqual(state['tasks']['T1']['status'], 'COMPLETED'); self.assertEqual(self.calls, [1, 2])

    def test_timeout_scope_and_protected_paths_still_block(self):
        def escape(task, attempt, prompt, workspace):
            (workspace / 'unauthorized.py').write_text('unsafe')
            return self.develop(task, attempt, prompt, workspace)
        with self.assertRaises(TaskError): self.make(developer=escape).run(self.plan['tasks'])
        self.assertFalse((self.wt / 'unauthorized.py').exists())

    def test_git_lock_without_ownership_is_preserved_and_registered(self):
        lock = self.wt / '.git'
        from pathlib import Path
        lock = Path(git(self.wt, 'rev-parse', '--git-path', 'index.lock'))
        lock.write_text('not an owner receipt')
        with self.assertRaisesRegex(RecoveryStop, 'ownership is unproven'):
            self.make().run(self.plan['tasks'])
        self.assertEqual(lock.read_text(), 'not an owner receipt'); self.assertEqual(self.calls, [])

    def test_persistent_attempt_limit_cannot_increase_on_resume(self):
        self.make(max_attempts=1).run(self.plan['tasks'])
        with self.assertRaisesRegex(TaskError, 'cannot change'):
            self.make(max_attempts=2).run(self.plan['tasks'])

    def test_harness_failure_preserves_candidate_without_developer_repair(self):
        def verifier(argv, cwd, timeout, workspace):
            if (workspace / 'app.py').read_text() == 'value = 0\n': return 'Ran 1 test\nOK', '', 0
            return '', 'launcher unavailable', 127
        with self.assertRaisesRegex(TaskError, 'ENVIRONMENT_DIAGNOSIS'):
            self.make(verifier=verifier, max_attempts=2).run(self.plan['tasks'])
        self.assertEqual(self.calls, [1])
        self.assertTrue((self.artifacts / '03-dev/tasks/T1/attempt-1/capture/receipt.json').exists())
