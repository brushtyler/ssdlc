"""Verifier timeouts retain partial diagnostics without accepting a candidate."""
import hashlib
import subprocess

import replay.repository_fixtures as fixtures
from ssdlc_tasks import DevelopmentBlocked


class VerificationTimeoutTests(fixtures.RepositoryTests):
    def setUp(self):
        super().setUp()
        self.wt = self.worktree()
        self.dispatches = []

    def check_timeout(self, stdout, stderr, expected):
        def develop(task, attempt, prompt, workspace):
            self.dispatches.append(attempt)
            (workspace / 'app.py').write_text('value = 1\n')
            return {'output': 'TASK_DEV_STATUS: COMPLETE', 'exit_code': 0, 'quiescent': True}

        def verify(argv, cwd, timeout, workspace):
            if (workspace / 'app.py').read_text() == 'value = 0\n':
                return 'Ran 1 test\nOK', '', 0
            raise subprocess.TimeoutExpired(['test-command'], timeout, output=stdout, stderr=stderr)

        scheduler = self.scheduler(develop, verify, isolate_candidates=True,
                                   recover_timeouts=True, max_attempts=2)
        with self.assertRaises(DevelopmentBlocked) as caught:
            scheduler.run(self.plan['tasks'])
        self.assertEqual(caught.exception.category, 'ENVIRONMENT_DIAGNOSIS')
        record = scheduler.journal.peek()['tasks']['T1']
        self.assertEqual(record['status'], 'FAILED')
        self.assertEqual(record['attempts'], 1)
        self.assertEqual(self.dispatches, [1])
        self.assertTrue((self.artifacts / '03-dev/tasks/T1/attempt-1/capture/receipt.json').is_file())
        for check in record['checks']:
            self.assertEqual(check['exit_code'], 124)
            log = self.artifacts / '03-dev/tasks' / check['log_ref']
            data = log.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), check['sha256'])
            text = data.decode()
            self.assertIn('Verification timeout:', text)
            for fragment in expected:
                self.assertIn(fragment, text)

    def test_bytes_and_invalid_utf8_are_retained(self):
        self.check_timeout(b'last running test\n\xff', b'partial traceback\n',
                           ['last running test', '\ufffd', 'partial traceback'])

    def test_text_output_is_retained(self):
        self.check_timeout('test progress\n', 'diagnostic stack\n',
                           ['test progress', 'diagnostic stack'])

    def test_timeout_without_output_is_still_failure(self):
        self.check_timeout(None, None, [])
