"""Corrective scheduler traversal and retained-evidence regression tests."""
import contextlib
import copy
import unittest
from unittest.mock import patch

from replay.repository_fixtures import RepositoryTests
from ssdlc_tasks import TaskError, TaskJournal, file_hash, git, ticket_lock


class RepairTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.wt = self.worktree()
        self.calls = []
        self.plan['tasks'] = [self.task('A', ['AC-1'], ['SEC-1'], ['a.py']),
                              self.task('B', [], [], ['b.py'], ['A']),
                              self.task('C', [], [], ['c.py'], ['B'])]
        self.ordered = self.plan['tasks']
        self.engine = self.scheduler(self.developer, max_attempts=6)
        self.engine.run(self.ordered)
        self.calls.clear()
        self.steps = []

    def developer(self, task, attempt, prompt):
        self.calls.append(task['task_id'])
        for name in task['target_files']:
            (self.wt / name).write_text(f'value = {attempt}\n')
        return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0)

    def repair(self, roots=('A', 'C'), txn='one'):
        self.engine.invalidate(roots, 'corrective:' + txn)
        return self.engine.repair(self.ordered, roots, {}, txn_id=txn,
                                  on_step=lambda name, info: self.steps.append((name, info)))

    def test_exact_roots_interleave_verification_and_keep_history(self):
        original = copy.deepcopy(self.engine.state)
        state = self.repair()
        self.assertEqual(self.calls, ['A', 'C'])
        self.assertEqual([n for n, _ in self.steps], ['REPAIR_COMMITTED', 'DEPENDENT_VERIFIED',
                         'REPAIR_COMMITTED', 'REVALIDATION_RUN_FINISHED', 'FINAL_PREFLIGHT_PASSED'])
        for tid in ('A', 'C'):
            self.assertEqual(state['tasks'][tid]['attempts'], 2)
            self.assertEqual(state['tasks'][tid]['superseded'][0]['checks'], original['tasks'][tid]['checks'])
        self.assertEqual(state['tasks']['B']['attempts'], 1)
        self.assertEqual(state['revalidation_runs'][0]['status'], 'COMPLETE')
        self.assertFalse(self.engine.verify_inventory())
        before = self.engine.journal.path.read_bytes()
        self.engine.repair(self.ordered, ['A', 'C'], {}, txn_id='one', on_step=lambda *a: None)
        self.assertEqual(before, self.engine.journal.path.read_bytes())

    def test_two_generations_preserve_support_and_reject_unrelated_head(self):
        self.repair(['A'], 'one')
        self.repair(['C'], 'two')
        self.assertFalse(self.engine.verify_inventory())
        b = self.engine.state['tasks']['B']
        b['revalidations'][-1]['head'] = self.base
        self.assertIn('invalid_supporting_attempt', [p['reason'] for p in self.engine._evidence_problems(b, 'B')])
        self.assertEqual(len(self.engine.state['generations']), 2)

    def test_old_failed_candidate_is_not_restored(self):
        # Manufacture a genuine failed-then-successful generation with retained logs.
        count = 0
        def verify(*args):
            nonlocal count
            count += 1
            return ('Ran 1 test\n', '', 1 if count == 1 else 0)
        self.engine.invalidate(['C'], 'corrective:first')
        self.engine.verifier = verify
        self.engine.repair(self.ordered, ['C'], {}, txn_id='first', on_step=lambda *a: None)
        old = copy.deepcopy(self.engine.state['tasks']['C']['failed_attempts'])
        self.engine.verifier = lambda *a: ('Ran 1 test\nOK', '', 0)
        with patch.object(self.engine, '_restore_candidate', side_effect=AssertionError('old candidate')):
            state = self.repair(['C'], 'second')
        self.assertEqual(state['tasks']['C']['failed_attempts'], old)
        self.assertEqual(state['tasks']['C']['failed_floor'], len(old))

    def test_attempts_exhausted_before_invalidation(self):
        self.engine.max_attempts = 1
        before = self.engine.journal.path.read_bytes()
        with self.assertRaisesRegex(TaskError, 'attempts_exhausted'):
            self.engine.invalidate(['A'], 'corrective:one')
        self.assertEqual(before, self.engine.journal.path.read_bytes())
        self.assertFalse(self.calls)

    def test_scope_and_protected_edits_rolled_back(self):
        for name, marker, code in [('outside.py', 'COMPLETE', 'scope_changed'),
                                    ('a.py', 'SCOPE_REQUIRED', 'scope_changed'),
                                    ('ssdlc_controller.py', 'COMPLETE', 'protected_path_modified'),
                                    ('ssdlc_gate_reports.py', 'COMPLETE', 'protected_path_modified'),
                                    ('quality-contract.json', 'COMPLETE', 'protected_path_modified')]:
            with self.subTest(name=name):
                # Each refusal is terminal; restore the fixture's authorized checkpoint
                # in memory for the next independent case, keeping every event on disk.
                initial = copy.deepcopy(self.engine.state)
                def bad(task, attempt, prompt):
                    (self.wt / name).write_text('unauthorized\n')
                    return dict(output='TASK_DEV_STATUS: ' + marker, exit_code=0)
                self.engine.developer = bad
                txn = name + marker
                self.engine.invalidate(['A'], 'corrective:' + txn)
                with self.assertRaisesRegex(TaskError, code):
                    self.engine.repair(self.ordered, ['A'], {}, txn_id=txn, on_step=lambda *a: None)
                self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
                self.engine.state = initial
                self.engine._save('TEST_FIXTURE_RESTORED')

    def test_blocker_empty_checks_pcf_is_narrow_and_terminal(self):
        def blocked(task, attempt, prompt):
            self.developer(task, attempt, prompt)
            return dict(output='TASK_DEV_STATUS: ENVIRONMENT_BLOCKED', exit_code=0)
        self.engine.developer = blocked
        with self.assertRaisesRegex(TaskError, 'repair_failed'):
            self.repair(['A'])
        self.assertEqual(self.calls, ['A'])
        record = self.engine.state['tasks']['A']
        self.assertEqual(record['checks'], [])
        self.assertIn('missing_checks', [p['reason'] for p in self.engine._evidence_problems(record, 'A')])
        self.assertFalse(self.engine._inventory_problems_active(None, None, 'A'))
        self.assertTrue(self.engine._inventory_problems_active(None, None, 'B'))
        original = copy.deepcopy(record)
        for change in ('completed', 'check_failure', 'hash', 'attempt', 'floor'):
            record.clear(); record.update(copy.deepcopy(original))
            if change == 'completed': record['status'] = 'COMPLETED'
            if change == 'check_failure': record['failed_attempts'][-1]['category'] = 'CHECK_FAILURE'
            if change == 'hash': record['failed_attempts'][-1]['diff_sha256'] = '0' * 64
            if change == 'attempt': record['failed_attempts'][-1]['attempt'] += 1
            if change == 'floor': record['failed_floor'] = len(record['failed_attempts'])
            self.assertTrue(self.engine._inventory_problems_active(None, None, 'A'), change)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_superseded_log_stays_in_inventory(self):
        self.repair()
        record = self.engine.state['tasks']['A']
        check = record['superseded'][0]['checks'][0]
        (self.engine.directory / check['log_ref']).write_text('changed')
        self.assertTrue(self.engine.verify_inventory())

    def test_active_missing_checks_requires_open_corrective_generation(self):
        save = self.engine._save
        observed = []

        def inspect(event):
            save(event)
            if event not in ('TASK_STARTED', 'VERIFYING'):
                return
            observed.append(event)
            record = self.engine.state['tasks']['A']
            self.assertEqual(record['checks'], [])
            self.assertFalse(self.engine._inventory_problems_active('A', None))
            run = self.engine.state['revalidation_runs'][-1]
            txn = run['txn_id']
            try:
                run['txn_id'] = 'unrelated'
                problems = self.engine._inventory_problems_active('A', None)
                self.assertIn('missing_checks', [p['reason'] for p in problems])
            finally:
                run['txn_id'] = txn

        with patch.object(self.engine, '_save', side_effect=inspect):
            self.repair(['A'])
        self.assertEqual(observed, ['TASK_STARTED', 'VERIFYING'])

    def test_no_lock_reentry(self):
        self.engine.invalidate(['A', 'C'], 'corrective:one')
        held = False
        @contextlib.contextmanager
        def lock(path):
            nonlocal held
            self.assertFalse(held)
            held = True
            try:
                with ticket_lock(path): yield
            finally:
                held = False
        def step(*args):
            self.assertTrue(held)
            self.assertFalse(self.engine._inventory_problems_active(None, None))
        with patch('ssdlc_tasks.ticket_lock', lock):
            self.engine.repair(self.ordered, ['A', 'C'], {}, txn_id='one', on_step=step)

    def test_read_only_journal_positions_trim_tail_and_verify_anchor(self):
        path = self.engine.journal.path
        head = TaskJournal.head(path)
        original = path.read_bytes()
        path.write_bytes(original + b'{torn')
        self.assertEqual(TaskJournal.head(path), head)
        state, events = TaskJournal.replay_range(path, head['sequence'], head['sha256'])
        self.assertEqual(events, [])
        self.assertEqual(state, self.engine.state)
        self.assertEqual(path.read_bytes(), original + b'{torn')
        with self.assertRaises(TaskError):
            TaskJournal.replay_range(path, head['sequence'], '0' * 64)
        missing = path.parent / 'absent' / 'journal'
        self.assertIsNone(TaskJournal.head(missing))
        self.assertFalse(missing.parent.exists())

    def test_final_checkpoint_failure_is_terminal(self):
        def step(name, info):
            if name == 'REVALIDATION_RUN_FINISHED':
                self.engine.verifier = lambda *a: ('Ran 1 test\nFAILED', '', 1)
        self.engine.invalidate(['C'], 'corrective:one')
        with self.assertRaisesRegex(TaskError, 'final_checkpoint_failed'):
            self.engine.repair(self.ordered, ['C'], {}, txn_id='one', on_step=step)
        count = len(self.engine.state['preflights'])
        with self.assertRaisesRegex(TaskError, 'final_checkpoint_failed'):
            self.engine.repair(self.ordered, ['C'], {}, txn_id='one', on_step=step)
        self.assertEqual(len(self.engine.state['preflights']), count)


if __name__ == '__main__':
    unittest.main()
