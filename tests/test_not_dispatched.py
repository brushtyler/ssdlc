"""Replay is allowed only from trusted adapter proof before a launch boundary."""
import subprocess
import unittest
from unittest.mock import patch

from test_recovery_limits import LimitsTests
from test_task_bootstrap import RepositoryTests
from ssdlc_recovery import NotDispatched
from ssdlc_tasks import TaskScheduler, TaskJournal, DevelopmentBlocked, TaskError


class DispatchProofTests(LimitsTests):
    def test_real_http_missing_auth_and_unsupervised_refusal_have_no_unknown_outcome(self):
        from orchestrator import EngineRunner, _dispatch_local
        previous = getattr(_dispatch_local, 'limits', None); _dispatch_local.limits = self.store
        try:
            for key, category in [('', 'AUTH_MISSING'), ('offline-sentinel', 'LIMIT_REACHED')]:
                with patch.dict('os.environ', {'OPENROUTER_API_KEY': key, 'SSDLC_SUPERVISED_RUN_ID': ''}), \
                     patch('orchestrator.urllib.request.urlopen', side_effect=AssertionError('No request')):
                    with self.assertRaises(NotDispatched) as stopped:
                        EngineRunner.call_openrouter('offline', 'system', 'user', 'project', 'ticket', '03-dev')
                    self.assertEqual(stopped.exception.category, category)
            self.assertEqual([r['status'] for r in self.store._load()[1]['invocations'].values()], ['not_dispatched'] * 2)
        finally: _dispatch_local.limits = previous

    def test_same_error_category_after_launch_does_not_prove_non_dispatch(self):
        from orchestrator import _supervised_dispatch, _dispatch_local, _dispatch_context
        @_supervised_dispatch
        def run_codex(prompt, cwd): raise DevelopmentBlocked('AUTH_MISSING', 'After unknown launch')
        previous = getattr(_dispatch_local, 'limits', None); _dispatch_local.limits = self.store
        try:
            with _dispatch_context(phase='03-dev'), self.assertRaises(DevelopmentBlocked):
                run_codex('prompt', self.root)
        finally: _dispatch_local.limits = previous
        self.assertEqual(next(iter(self.store._load()[1]['invocations'].values()))['status'], 'uncertain')

    def test_deadline_before_subprocess_is_trusted_non_dispatch(self):
        from orchestrator import EngineRunner, _supervised_dispatch, _dispatch_local, _dispatch_context
        @_supervised_dispatch
        def run_codex(prompt, cwd):
            self.now = 1101
            return EngineRunner._run_role_command(['true'], cwd=cwd, env=None, timeout=30)
        previous = getattr(_dispatch_local, 'limits', None); _dispatch_local.limits = self.store
        try:
            with _dispatch_context(phase='03-dev'), patch('orchestrator.subprocess.run', side_effect=AssertionError('No launch')):
                with self.assertRaises(NotDispatched): run_codex('prompt', self.root)
        finally: _dispatch_local.limits = previous
        self.assertEqual(next(iter(self.store._load()[1]['invocations'].values()))['status'], 'not_dispatched')


class TaskDispatchProofTests(RepositoryTests):
    def test_prelaunch_refusal_reuses_task_attempt_and_nonce(self):
        worktree = self.worktree()
        def refused(*args): raise NotDispatched('AUTH_MISSING', 'Trusted prelaunch refusal')
        scheduler = TaskScheduler(worktree, self.artifacts / '03-dev/tasks', self.plan, self.quality,
            {}, refused, lambda *a: ('Ran 1 test\nOK', '', 0), isolate_candidates=True, recover_timeouts=True)
        with self.assertRaises(NotDispatched): scheduler.run(self.plan['tasks'])
        saved = TaskJournal(self.artifacts / '03-dev/tasks').peek()['tasks']['T1']
        self.assertEqual(saved['status'], 'DISPATCH_RESERVED')
        self.assertEqual(saved['attempts'], 1)
        def developer(task, attempt, prompt, workspace):
            (workspace / 'app.py').write_text('value = 1\n')
            return {'output': 'done', 'exit_code': 0, 'quiescent': True}
        scheduler.developer = developer
        done = scheduler.run(self.plan['tasks'])['tasks']['T1']
        self.assertEqual(done['status'], 'COMPLETED')
        self.assertEqual(done['attempts'], 1)
        self.assertEqual(done['invocation_id'], saved['invocation_id'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (DispatchProofTests, TaskDispatchProofTests)
                              for name in cls.__dict__ if name.startswith('test_'))
