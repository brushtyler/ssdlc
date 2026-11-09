import json
from unittest.mock import patch

from ssdlc_tasks import DevelopmentBlocked, TaskError, diagnostic_excerpt, file_hash, git
from ssdlc_tasks import test_suite_is_empty as suite_is_empty
from test_task_bootstrap import RepositoryTests


class DevelopmentRepairTests(RepositoryTests):
    def test_nested_empty_suite_diagnostic_does_not_override_real_unittest_summary(self):
        stdout = 'Fixture diagnostic:\nRan 0 tests\n\nOK\n'
        stderr = 'Ran 157 tests in 23.381s\n\nOK\n'
        self.assertFalse(suite_is_empty(stdout, stderr))
        result = self.scheduler(self.developer, lambda *args: (stdout, stderr, 0)).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')

    def test_genuinely_empty_runner_summaries_still_block(self):
        for stdout, stderr in [('', 'Ran 0 tests in 0.001s\nOK'), ('collected 0 items', ''),
                               ('================ no tests ran in 0.01s ================', ''),
                               ('Ran 3 tests\nOK', 'Ran 0 tests\nOK')]:
            with self.subTest(stdout=stdout, stderr=stderr):
                self.assertTrue(suite_is_empty(stdout, stderr))

    def test_positive_final_pytest_summary_overrides_nested_empty_fixture(self):
        for text in ('collected 0 items\n================ 5 passed in 0.2s ================',
                     'no tests ran in 0.1s\n5 passed in 0.2s', 'collected 5 items'):
            with self.subTest(text=text):
                self.assertFalse(suite_is_empty(text))

    def test_incidental_quoted_empty_suite_text_is_not_a_runner_summary(self):
        self.assertFalse(suite_is_empty('Assertion message: no tests ran is an example'))

    def setUp(self):
        super().setUp()
        self.wt = self.worktree()
        self.calls = []
        self.verifications = []
        self.quality['checks'][0]['argv'] = ['quality']
        self.plan['tasks'][0]['verification']['checks'][0]['argv'] = ['task-check']

    def developer(self, task, attempt, prompt):
        self.calls.append((attempt, prompt))
        (self.wt/'app.py').write_text(f'value = {attempt}\n')
        return {'output': 'Implemented', 'exit_code': 0}

    def verifier(self, argv, cwd, timeout):
        self.assertEqual(cwd, self.wt)
        self.verifications.append(argv)
        if argv == ['task-check'] and len(self.calls) == 1:
            return 'Ran 1 test', 'Traceback: AssertionError: expected recovery after journal append', 1
        return 'Ran 1 test\nOK', '', 0

    def failed_candidate(self):
        with self.assertRaisesRegex(TaskError, 'exhausted'):
            self.scheduler(self.developer, self.verifier, max_attempts=1).run(self.plan['tasks'])
        scheduler = self.scheduler(self.developer, self.verifier, max_attempts=2)
        return scheduler, scheduler.journal.load()['tasks']['T1']['failed_attempts'][-1]

    def test_actual_worktree_baseline_failure_consumes_no_developer_attempts(self):
        def broken_baseline(argv, cwd, timeout):
            self.assertEqual(cwd, self.wt)
            return 'Ran 109 tests', 'AssertionError: /opt/ssdlc/.ssdlc mountpoint does not exist', 1
        scheduler = self.scheduler(self.developer, broken_baseline)
        with self.assertRaisesRegex(DevelopmentBlocked, 'BASELINE_MISMATCH'):
            scheduler.run(self.plan['tasks'])
        self.assertEqual(self.calls, [])
        state = scheduler.journal.load()
        self.assertEqual(state['tasks']['T1']['attempts'], 0)
        self.assertEqual(state['tasks']['T1']['status'], 'PENDING')
        self.assertEqual(state['preflights'][0]['status'], 'FAILED')
        evidence = scheduler.directory/state['preflights'][0]['checks'][0]['log_ref']
        self.assertIn('mountpoint does not exist', evidence.read_text())
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_baseline_mutation_is_preserved_and_never_retried_with_ai(self):
        def mutating_check(*args):
            (self.wt/'app.py').write_text('unexpected mutation\n')
            return 'Ran 1 test', '', 0
        scheduler = self.scheduler(self.developer, mutating_check)
        with self.assertRaisesRegex(DevelopmentBlocked, 'BASELINE_MUTATION'):
            scheduler.run(self.plan['tasks'])
        self.assertEqual(self.calls, [])
        self.assertEqual((self.wt/'app.py').read_text(), 'unexpected mutation\n')

    def test_resume_repairs_archived_deleted_and_new_binary_files_in_place(self):
        task = self.plan['tasks'][0]
        task['target_files'] = ['app.py', 'new.bin']
        def developer(task, attempt, prompt):
            self.calls.append((attempt, prompt))
            if attempt == 1:
                (self.wt/'app.py').unlink()
                (self.wt/'new.bin').write_bytes(b'\0original candidate\xff')
            else:
                self.assertFalse((self.wt/'app.py').exists())
                self.assertEqual((self.wt/'new.bin').read_bytes(), b'\0original candidate\xff')
                self.assertIn('AssertionError: expected recovery after journal append', prompt)
                self.assertIn('Repair the restored previous candidate', prompt)
                context = json.loads(prompt.split('\nREPAIR CONTEXT:\n', 1)[1])
                self.assertEqual(context['attempt'], 1)
                self.assertEqual(len(context['history']), 1)
                (self.wt/'app.py').write_text('value = 2\n')
            return {'output': 'Implemented', 'exit_code': 0}
        with self.assertRaisesRegex(TaskError, 'exhausted'):
            self.scheduler(developer, self.verifier, max_attempts=1).run(self.plan['tasks'])
        root = self.artifacts/'03-dev/tasks'
        diff_hash = file_hash(root/'T1/attempt-1/implementation.diff')
        self.assertTrue((self.wt/'app.py').exists())
        self.assertFalse((self.wt/'new.bin').exists())
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        result = self.scheduler(developer, self.verifier, max_attempts=2).run(self.plan['tasks'])
        self.assertEqual([a for a, _ in self.calls], [1, 2])
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual(file_hash(root/'T1/attempt-1/implementation.diff'), diff_hash)
        self.assertEqual(git(self.wt, 'rev-list', '--count', self.base+'..HEAD'), '1')

    def test_changed_patch_blocks_restoration_before_another_developer_call(self):
        scheduler, candidate = self.failed_candidate()
        (scheduler.directory/candidate['diff_ref']).write_text('forged patch')
        with self.assertRaisesRegex(TaskError, 'Repair patch changed'):
            scheduler.run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_changed_failed_check_log_blocks_repair(self):
        scheduler, candidate = self.failed_candidate()
        failed = next(c for c in candidate['checks'] if c['exit_code'])
        (scheduler.directory/failed['log_ref']).write_text('forged successful result')
        with self.assertRaisesRegex(TaskError, 'Repair verification evidence changed'):
            scheduler.run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_scope_blocker_stops_after_one_attempt_and_retains_evidence(self):
        def developer(*args):
            result = self.developer(*args)
            result['output'] = 'Need to change orchestrator.py outside target_files.\nTASK_DEV_STATUS: SCOPE_REQUIRED'
            return result
        scheduler = self.scheduler(developer, self.verifier)
        for _ in range(2):
            with self.assertRaisesRegex(DevelopmentBlocked, 'SCOPE_REQUIRED'):
                scheduler.run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.verifications, [['quality']])
        record = scheduler.journal.load()['tasks']['T1']
        self.assertEqual(record['attempts'], 1)
        self.assertEqual(record['status'], 'FAILED')
        self.assertTrue((scheduler.directory/record['failed_attempts'][-1]['diff_ref']).is_file())
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_environment_blocker_without_diff_cannot_be_approved(self):
        def developer(*args):
            self.calls.append(args)
            return {'output': 'Required tool is missing.\nTASK_DEV_STATUS: ENVIRONMENT_BLOCKED', 'exit_code': 0}
        scheduler = self.scheduler(developer, self.verifier)
        with self.assertRaisesRegex(DevelopmentBlocked, 'ENVIRONMENT_BLOCKED'):
            scheduler.run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_identical_failed_candidate_stops_without_consuming_remaining_slots(self):
        def unchanged(task, attempt, prompt):
            self.calls.append((attempt, prompt))
            (self.wt/'app.py').write_text('same failing candidate\n')
            return {'output': 'Implemented', 'exit_code': 0}
        def verify(argv, *args):
            return ('Ran 1 test', 'AssertionError: still failing', 1) if argv == ['task-check'] else ('Ran 1 test', '', 0)
        scheduler = self.scheduler(unchanged, verify, max_attempts=4)
        for _ in range(2):
            with self.assertRaisesRegex(DevelopmentBlocked, 'REPAIR_STALLED'):
                scheduler.run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_crash_during_repair_does_not_adopt_an_unverified_dirty_candidate(self):
        def developer(task, attempt, prompt):
            if attempt == 2:
                self.calls.append((attempt, prompt))
                self.assertEqual((self.wt/'app.py').read_text(), 'value = 1\n')
                raise OSError('interrupted developer')
            return self.developer(task, attempt, prompt)
        with self.assertRaisesRegex(OSError, 'interrupted'):
            self.scheduler(developer, self.verifier).run(self.plan['tasks'])
        with self.assertRaisesRegex(TaskError, 'dirty'):
            self.scheduler(developer, self.verifier).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual((self.wt/'app.py').read_text(), 'value = 1\n')

    def test_process_failure_is_not_treated_as_a_test_driven_code_repair(self):
        def developer(*args):
            result = self.developer(*args)
            result['exit_code'] = 137
            return result
        with self.assertRaisesRegex(DevelopmentBlocked, 'DEVELOPER_INVOCATION'):
            self.scheduler(developer, self.verifier).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.verifications, [['quality']])

    def test_long_failure_output_keeps_the_traceback_with_a_bounded_excerpt(self):
        text = 'test_start\n' + 'successful test ... ok\n' * 1000 + 'AssertionError: actual final failure'
        excerpt = diagnostic_excerpt(text, 6000)
        self.assertLessEqual(len(excerpt), 6000)
        self.assertTrue(excerpt.startswith('test_start'))
        self.assertTrue(excerpt.endswith('AssertionError: actual final failure'))
        self.assertIn('excerpt truncated', excerpt)
        self.assertEqual(diagnostic_excerpt(text, 0), '')

    def test_clean_rollback_crash_can_resume_the_archived_candidate(self):
        scheduler = self.scheduler(self.developer, self.verifier)
        original = scheduler._save
        def crash(event):
            if event == 'ATTEMPT_RESTORED':
                raise OSError('interrupted after rollback')
            original(event)
        scheduler._save = crash
        with self.assertRaisesRegex(OSError, 'after rollback'):
            scheduler.run(self.plan['tasks'])
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        def repair(task, attempt, prompt):
            self.assertEqual((self.wt/'app.py').read_text(), 'value = 1\n')
            return self.developer(task, attempt, prompt)
        result = self.scheduler(repair, self.verifier).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual(len(self.calls), 2)
