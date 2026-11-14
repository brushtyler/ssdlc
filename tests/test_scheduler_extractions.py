"""Recorded ordinary scheduler traces must survive preparatory extractions."""
import copy
import json

from replay.repository_fixtures import RepositoryTests
from ssdlc_tasks import TaskError, git, ticket_lock


class SchedulerExtractionTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.wt = self.worktree()
        self.dispatches = []

    def developer(self, task, attempt, prompt):
        self.dispatches.append((task['task_id'], attempt))
        (self.wt / 'app.py').write_text('value = 1\n')
        return {'output': 'Implemented', 'exit_code': 0}

    def events(self, scheduler):
        return [json.loads(line) for line in scheduler.journal.path.read_text().splitlines()]

    def complete(self):
        scheduler = self.scheduler(self.developer)
        state = scheduler.run(self.plan['tasks'])
        events = self.events(scheduler)
        self.assertEqual([e['event'] for e in events], [
            'INITIALIZED', 'PREFLIGHT_STARTED', 'CHECK_FINISHED', 'PREFLIGHT_PASSED',
            'TASK_STARTED', 'VERIFYING', 'CHECK_FINISHED', 'CHECK_FINISHED',
            'COMMIT_INTENT', 'TASK_COMPLETED'])
        self.assertEqual([e['state']['tasks']['T1']['status'] for e in events], [
            'PENDING', 'PENDING', 'PENDING', 'PENDING', 'RUNNING', 'VERIFYING',
            'VERIFYING', 'VERIFYING', 'COMMIT_INTENT', 'COMPLETED'])
        self.assertEqual(state, events[-1]['state'])
        self.assertEqual(state['tasks']['T1']['attempts'], 1)
        self.assertEqual(state['expected_head'], git(self.wt, 'rev-parse', 'HEAD'))
        self.assertEqual(self.dispatches, [('T1', 1)])
        return scheduler

    def test_ordinary_run_trace_and_noop_resume(self):
        scheduler = self.complete()
        before = scheduler.journal.path.read_bytes()
        state = copy.deepcopy(scheduler.state)
        self.assertEqual(scheduler.run(self.plan['tasks']), state)
        self.assertEqual(scheduler.journal.path.read_bytes(), before)
        self.assertEqual(scheduler.verify_inventory(), [])

    def test_invalidation_and_revalidation_recorded_trace(self):
        scheduler = self.complete()
        original = copy.deepcopy(scheduler.state['tasks']['T1'])
        self.assertEqual(scheduler.invalidate(['T1'], 'recorded reason'), ['T1'])
        invalid = copy.deepcopy(scheduler.state)
        self.assertEqual(invalid['tasks']['T1'], dict(original, status='INVALIDATED'))
        self.assertEqual(invalid['invalidations'], [
            dict(roots=['T1'], closure=['T1'], reason='recorded reason')])
        result = scheduler.revalidate()
        self.assertEqual((result.passed, result.failed, result.blocked, result.unchanged),
                         (['T1'], [], [], []))
        events = self.events(scheduler)[10:]
        self.assertEqual([e['event'] for e in events], [
            'TASKS_INVALIDATED', 'REVALIDATION_STARTED', 'REVALIDATION_TASK_STARTED',
            'REVALIDATION_CHECK_STARTED', 'REVALIDATION_CHECK_FINISHED',
            'REVALIDATION_CHECK_STARTED', 'REVALIDATION_CHECK_FINISHED',
            'REVALIDATION_TASK_PASSED', 'REVALIDATION_FINISHED'])
        record = scheduler.state['tasks']['T1']
        retained = {k: v for k, v in record.items() if k not in ('revalidations', 'restored_by')}
        self.assertEqual(retained, original)
        self.assertEqual(record['restored_by'], 1)
        self.assertEqual(record['revalidations'][0]['status'], 'PASSED')
        self.assertEqual(scheduler.state['expected_head'], invalid['expected_head'])
        self.assertEqual(scheduler.state['revalidation_runs'][0]['status'], 'COMPLETE')
        self.assertEqual(self.dispatches, [('T1', 1)])
        self.assertEqual(scheduler.verify_inventory(), [])
        before = scheduler.journal.path.read_bytes()
        with ticket_lock(scheduler.directory / 'scheduler.lock'):
            scheduler._finalize_txn_run(scheduler.state['revalidation_runs'][0])
        self.assertEqual(scheduler.journal.path.read_bytes(), before)

    def test_run_refuses_invalidated_tasks_and_feedback_without_events(self):
        scheduler = self.complete()
        before = scheduler.journal.path.read_bytes()
        with self.assertRaisesRegex(TaskError, 'feedback requires explicit invalidation'):
            scheduler.run(self.plan['tasks'], feedback='please repair')
        self.assertEqual(scheduler.journal.path.read_bytes(), before)
        scheduler.invalidate(['T1'], 'test')
        before = scheduler.journal.path.read_bytes()
        with self.assertRaisesRegex(TaskError, 'Invalidated work cannot be run'):
            scheduler.run(self.plan['tasks'])
        self.assertEqual(scheduler.journal.path.read_bytes(), before)
        self.assertEqual(self.dispatches, [('T1', 1)])

    def test_active_inventory_skeleton_keeps_missing_checks_blocking(self):
        scheduler = self.complete()
        scheduler.state['tasks']['T1']['checks'] = []
        expected = scheduler._inventory_problems(scheduler.state)
        self.assertTrue(any(p['reason'] == 'missing_checks' for p in expected))
        for status in ('COMPLETED', 'RUNNING', 'VERIFYING', 'FAILED'):
            scheduler.state['tasks']['T1']['status'] = status
            self.assertEqual(scheduler._inventory_problems_active('T1', 'T1', 'T1'),
                             scheduler._inventory_problems(scheduler.state))
