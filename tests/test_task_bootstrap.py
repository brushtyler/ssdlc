import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import EngineRunner, SSDLCOrchestrator
from replay.repository_fixtures import RepositoryTests
from ssdlc_tasks import (
    PlanError, TaskError, DevelopmentBlocked, TaskJournal, TaskPlanValidator, TaskScheduler,
    atomic_json, confined, digest, file_hash, git, ticket_lock, validate_document,
)




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

    def test_command_argv_preserves_repeated_options_and_arguments(self):
        argv = ['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-k', 'first', '-k', 'second']
        self.plan['tasks'][0]['verification']['checks'][0]['argv'] = argv
        self.quality['checks'][0]['argv'] = argv
        self.validator().validate(self.plan)
        self.assertEqual(self.plan['tasks'][0]['verification']['checks'][0]['argv'], argv)


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

    def completed_chain(self):
        self.plan['tasks'] = [
            self.task('A', ['AC-1'], ['SEC-1'], ['a.py']),
            self.task('B', [], [], ['b.py'], ['A']),
            self.task('C', [], [], ['c.py'], ['B']),
            self.task('D', [], [], ['d.py']),
        ]
        scheduler = self.scheduler(self.developer)
        scheduler.run(self.plan['tasks'])
        # Keep fixture setup separate from fresh schedulers used to instrument locks.
        self.completed_scheduler = scheduler
        return scheduler

    def file_bytes(self):
        journal = self.completed_scheduler.journal
        return journal.path.read_bytes(), journal.snapshot.read_bytes()

    def tail_artifacts(self):
        return sorted(self.completed_scheduler.directory.glob('interrupted-tail-*.bin'))

    def with_stale_snapshot(self):
        journal = self.completed_scheduler.journal
        first = json.loads(journal.path.read_bytes().splitlines()[0])
        atomic_json(journal.snapshot, first['state'])

    def with_partial_tail(self):
        with self.completed_scheduler.journal.path.open('ab') as stream:
            stream.write(b'{incomplete')

    def fail_dev(self, *args):
        self.fail('Developer must not be invoked')

    def fail_ver(self, *args):
        self.fail('Verifier must not be invoked')

    def journaled_state(self, mutator):
        scheduler = self.completed_scheduler
        state = copy.deepcopy(scheduler.state)
        mutator(state)
        scheduler.journal.append('TEST_STATE', state)
        scheduler.state = state
        return state

    def test_invalidate_a_closure(self):
        scheduler = self.completed_chain()
        before = copy.deepcopy(scheduler.state)
        count = len(self.file_bytes()[0].splitlines())
        self.assertEqual(scheduler.invalidate(['A'], ' reason '), ['A', 'B', 'C'])
        lines = self.file_bytes()[0].splitlines()
        self.assertEqual(len(lines), count + 1)
        event = json.loads(lines[-1])
        self.assertEqual(event['event'], 'TASKS_INVALIDATED')
        self.assertEqual(event['state']['invalidations'][-1],
                         {'roots': ['A'], 'closure': ['A', 'B', 'C'], 'reason': 'reason'})
        for tid in ['A', 'B', 'C']:
            self.assertEqual(scheduler.state['tasks'][tid]['status'], 'INVALIDATED')
        self.assertEqual(scheduler.state['tasks']['D'], before['tasks']['D'])

    def test_invalidate_b_closure(self):
        scheduler = self.completed_chain()
        before = copy.deepcopy(scheduler.state)
        self.assertEqual(scheduler.invalidate(['B'], 'reason'), ['B', 'C'])
        for tid in ['A', 'D']:
            self.assertEqual(scheduler.state['tasks'][tid], before['tasks'][tid])

    def test_invalidate_deterministic(self):
        scheduler = self.completed_chain()
        baseline = self.file_bytes()
        expected = {'roots': ['A', 'C'], 'closure': ['A', 'B', 'C'], 'reason': 'reason'}
        for ids in (['C', 'A', 'A'], ['A', 'C'], ('C', 'A'), {'A', 'C'}, frozenset(['C', 'A'])):
            with self.subTest(ids=ids):
                scheduler.journal.path.write_bytes(baseline[0])
                scheduler.journal.snapshot.write_bytes(baseline[1])
                fresh = self.scheduler(self.fail_dev, self.fail_ver)
                self.assertEqual(fresh.invalidate(ids, 'reason'), expected['closure'])
                self.assertEqual(fresh.state['invalidations'][-1], expected)

    def test_invalidate_accepts_custom_collection(self):
        from collections import deque
        scheduler = self.completed_chain()
        self.assertEqual(scheduler.invalidate(deque(['B']), 'reason'), ['B', 'C'])

    def test_invalidate_accepts_long_reason(self):
        scheduler = self.completed_chain()
        scheduler.invalidate(['A'], '  ' + 'x' * 5000 + '\n')
        self.assertEqual(scheduler.state['invalidations'][-1]['reason'], 'x' * 5000)

    def test_invalidate_preserves_audit_fields(self):
        original = self.completed_chain()
        before = copy.deepcopy(original.state)
        head = git(self.wt, 'rev-parse', 'HEAD')
        scheduler = self.scheduler(self.fail_dev, self.fail_ver)
        scheduler.invalidate(['A'], 'reason')
        for tid, record in before['tasks'].items():
            expected = copy.deepcopy(record)
            if tid in ['A', 'B', 'C']:
                expected['status'] = 'INVALIDATED'
            self.assertEqual(scheduler.state['tasks'][tid], expected)
        self.assertEqual(scheduler.state['expected_head'], before['expected_head'])
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), head)

    def test_invalidate_rejects_bad_requests_without_byte_change(self):
        scheduler = self.completed_chain()
        baseline = self.file_bytes()
        for damage in (self.with_stale_snapshot, self.with_partial_tail):
            scheduler.journal.path.write_bytes(baseline[0])
            scheduler.journal.snapshot.write_bytes(baseline[1])
            damage()
            cases = [(ids, 'reason') for ids in
                     ('A', b'A', None, [], (), {}, {'A': 1}, (x for x in ['A']), [''], [1],
                      ['Z'], ['../x'], ['A', 'Z'], bytearray(b'A'))]
            cases += [(['A'], reason) for reason in (None, '', '  ', 5)]
            for ids, reason in cases:
                with self.subTest(damage=damage.__name__, ids=ids, reason=reason):
                    before = self.file_bytes()
                    fresh = self.scheduler(self.fail_dev, self.fail_ver)
                    with patch.object(fresh.journal, 'peek', side_effect=AssertionError('peek')), patch.object(
                            fresh.journal, 'load', side_effect=AssertionError('load')):
                        with self.assertRaises(TaskError):
                            fresh.invalidate(ids, reason)
                    self.assertEqual(self.file_bytes(), before)
                    self.assertEqual(self.tail_artifacts(), [])

    def test_invalidate_lock_boundary(self):
        self.completed_chain()
        scheduler = self.scheduler(self.fail_dev, self.fail_ver)
        calls = []
        lock = scheduler.directory / 'scheduler.lock'
        def wrap(name, original):
            def checked(*args):
                calls.append(name)
                with self.assertRaisesRegex(TaskError, '^Another process is running this ticket$'):
                    with ticket_lock(lock):
                        pass
                return original(*args)
            return checked
        with patch.object(scheduler.journal, 'peek', wrap('peek', scheduler.journal.peek)), patch.object(
                scheduler.journal, 'load', wrap('load', scheduler.journal.load)), patch.object(
                scheduler, '_save', wrap('save', scheduler._save)):
            before = self.file_bytes()
            with ticket_lock(lock):
                with self.assertRaisesRegex(TaskError, 'Another process'):
                    scheduler.invalidate(['A'], 'reason')
            self.assertEqual(calls, [])
            self.assertEqual(self.file_bytes(), before)
            scheduler.invalidate(['A'], 'reason')
            self.assertEqual(calls, ['peek', 'load', 'save'])
        with ticket_lock(lock):
            pass

    def test_invalidate_lock_contention(self):
        scheduler = self.completed_chain()
        before = self.file_bytes()
        with ticket_lock(scheduler.directory / 'scheduler.lock'):
            with self.assertRaisesRegex(TaskError, 'Another process'):
                scheduler.invalidate(['A'], 'reason')
        self.assertEqual(self.file_bytes(), before)

    def test_invalidate_snapshot_crash_recovers_full_closure(self):
        scheduler = self.completed_chain()
        count = len(self.file_bytes()[0].splitlines())
        def crash(path, state):
            if any(rec['status'] == 'INVALIDATED' for rec in state['tasks'].values()):
                raise OSError('snapshot interrupted')
            atomic_json(path, state)
        with patch('ssdlc_tasks.atomic_json', side_effect=crash):
            with self.assertRaises(OSError):
                scheduler.invalidate(['A'], 'reason')
        self.assertEqual(len(self.file_bytes()[0].splitlines()), count + 1)
        fresh = self.scheduler(self.fail_dev, self.fail_ver)
        state = fresh.journal.load()
        self.assertEqual([state['tasks'][tid]['status'] for tid in 'ABCD'],
                         ['INVALIDATED'] * 3 + ['COMPLETED'])
        self.assertEqual(json.loads(fresh.journal.snapshot.read_text()), state)

    def test_invalidate_repairs_stale_snapshot_for_valid_request(self):
        scheduler = self.completed_chain()
        count = len(self.file_bytes()[0].splitlines())
        self.with_stale_snapshot()
        scheduler.invalidate(['A'], 'reason')
        lines = self.file_bytes()[0].splitlines()
        self.assertEqual([json.loads(line)['event'] for line in lines[count:]], ['TASKS_INVALIDATED'])
        self.assertEqual(json.loads(self.file_bytes()[1]), scheduler.state)

    def test_run_refuses_invalidated_state(self):
        scheduler = self.completed_chain()
        scheduler.invalidate(['A'], 'reason')
        state = copy.deepcopy(scheduler.state)
        before = self.file_bytes()
        calls = list(self.calls)
        fresh = self.scheduler(self.fail_dev, self.fail_ver)
        with self.assertRaisesRegex(TaskError, 'revalidation API') as raised:
            fresh.run(self.plan['tasks'])
        self.assertTrue(str(raised.exception).endswith('A, B, C'))
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(scheduler.state, state)
        self.assertEqual(fresh.journal.peek(), state)
        self.assertEqual([state['tasks'][tid]['status'] for tid in 'ABCD'],
                         ['INVALIDATED'] * 3 + ['COMPLETED'])
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.tail_artifacts(), [])

    def test_run_refuses_after_snapshot_crash_without_repair(self):
        scheduler = self.completed_chain()
        completed = copy.deepcopy(scheduler.state)
        count = len(self.file_bytes()[0].splitlines())
        def crash(path, state):
            if any(rec['status'] == 'INVALIDATED' for rec in state['tasks'].values()):
                raise OSError('snapshot interrupted')
            atomic_json(path, state)
        with patch('ssdlc_tasks.atomic_json', side_effect=crash):
            with self.assertRaises(OSError):
                scheduler.invalidate(['A'], 'reason')
        before = self.file_bytes()
        self.assertEqual(len(before[0].splitlines()), count + 1)
        self.assertEqual(json.loads(before[1]), completed)
        fresh = self.scheduler(self.fail_dev, self.fail_ver)
        state = fresh.journal.peek()
        self.assertEqual([state['tasks'][tid]['status'] for tid in 'ABCD'],
                         ['INVALIDATED'] * 3 + ['COMPLETED'])
        with self.assertRaisesRegex(TaskError, 'revalidation API'):
            fresh.run(self.plan['tasks'])
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(fresh.journal.peek(), state)
        self.assertEqual(self.tail_artifacts(), [])

    def test_run_refuses_invalidated_journal_with_partial_tail(self):
        scheduler = self.completed_chain()
        scheduler.invalidate(['A'], 'reason')
        state = copy.deepcopy(scheduler.state)
        self.with_partial_tail()
        before = self.file_bytes()
        fresh = self.scheduler(self.fail_dev, self.fail_ver)
        with self.assertRaisesRegex(TaskError, 'revalidation API'):
            fresh.run(self.plan['tasks'])
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(fresh.journal.peek(), state)
        self.assertEqual(self.tail_artifacts(), [])

    def test_run_refuses_holds_lock_contention(self):
        scheduler = self.completed_chain()
        scheduler.invalidate(['A'], 'reason')
        self.with_partial_tail()
        before = self.file_bytes()
        fresh = self.scheduler(self.fail_dev, self.fail_ver)
        with patch.object(fresh.journal, 'peek', side_effect=AssertionError('Peek must hold lock')):
            with ticket_lock(scheduler.directory / 'scheduler.lock'):
                with self.assertRaisesRegex(TaskError, 'Another process'):
                    fresh.run(self.plan['tasks'])
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.tail_artifacts(), [])

    def test_invalidate_rejects_non_completed(self):
        scheduler = self.completed_chain()
        baseline = self.file_bytes()
        cases = [(status, lambda s, status=status: s['tasks']['B'].update(status=status))
                 for status in ('PENDING', 'FAILED', 'RUNNING', 'VERIFYING', 'COMMIT_INTENT')]
        cases += [('no scheduler state', lambda s: s['tasks'].pop('B')),
                  ('not in the plan', lambda s: s['tasks'].update(X={'status': 'COMPLETED'}))]
        for message, mutate in cases:
            for damage in (lambda: None, self.with_stale_snapshot, self.with_partial_tail):
                with self.subTest(message=message, damage=damage.__name__):
                    scheduler.journal.path.write_bytes(baseline[0])
                    scheduler.journal.snapshot.write_bytes(baseline[1])
                    scheduler.state = scheduler.journal.load()
                    self.journaled_state(mutate)
                    damage()
                    before = self.file_bytes()
                    with self.assertRaisesRegex(TaskError, message) as raised:
                        self.scheduler(self.fail_dev, self.fail_ver).invalidate(['A'], 'reason')
                    self.assertIn("'X'" if message == 'not in the plan' else "'B'", str(raised.exception))
                    self.assertEqual(self.file_bytes(), before)
                    self.assertEqual(self.tail_artifacts(), [])

    def test_second_invalidate_rejected_invalidate(self):
        scheduler = self.completed_chain()
        scheduler.invalidate(['A'], 'reason')
        for stale in (False, True):
            if stale:
                self.with_stale_snapshot()
            before = self.file_bytes()
            with self.assertRaisesRegex(TaskError, 'INVALIDATED'):
                self.scheduler(self.fail_dev, self.fail_ver).invalidate(['A'], 'again')
            self.assertEqual(self.file_bytes(), before)
            self.assertEqual(self.tail_artifacts(), [])

    def assert_invalidate_integrity_failure(self, scheduler, message):
        before = self.file_bytes()
        with self.assertRaisesRegex(TaskError, message):
            scheduler.invalidate(['A'], 'reason')
        self.assertEqual(self.file_bytes(), before)
        self.assertNotIn(b'TASKS_INVALIDATED', self.file_bytes()[0])

    def test_invalidate_fails_closed_dirty_tree(self):
        self.completed_chain().journal.load()
        (self.wt / 'untracked').write_text('external')
        self.assert_invalidate_integrity_failure(self.scheduler(self.fail_dev, self.fail_ver), 'worktree is dirty')

    def test_invalidate_fails_closed_head_changed(self):
        self.completed_chain().journal.load()
        git(self.wt, 'commit', '--allow-empty', '-m', 'external')
        self.assert_invalidate_integrity_failure(self.scheduler(self.fail_dev, self.fail_ver), 'Unexpected worktree HEAD')

    def test_invalidate_fails_closed_protected_path(self):
        self.completed_chain().journal.load()
        (self.wt / '.git').write_text((self.wt / '.git').read_text() + '\n')
        self.assert_invalidate_integrity_failure(self.scheduler(self.fail_dev, self.fail_ver), 'Protected worktree paths changed')

    def test_invalidate_fails_closed_ignored_file(self):
        self.completed_chain().journal.load()
        # Keep the ignored-path check independent of protected metadata.
        exclude = Path(self.tmp.name) / 'excludes'
        exclude.write_text('new-ignored\n')
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.excludesFile',
                                     'GIT_CONFIG_VALUE_0': str(exclude)}):
            (self.wt / 'new-ignored').write_text('external')
            self.assert_invalidate_integrity_failure(self.scheduler(self.fail_dev, self.fail_ver), 'Ignored worktree files changed')

    def test_invalidate_fails_closed_plan_fingerprint(self):
        self.completed_chain().journal.load()
        self.plan['tasks'][0]['description'] = 'changed'
        self.assert_invalidate_integrity_failure(self.scheduler(self.fail_dev, self.fail_ver), 'Inputs changed')

    def test_invalidate_fails_closed_quality_fingerprint(self):
        self.completed_chain().journal.load()
        self.quality['contract_revision'] += 1
        self.assert_invalidate_integrity_failure(self.scheduler(self.fail_dev, self.fail_ver), 'Inputs changed')

    def test_invalidate_fails_closed_context_fingerprint(self):
        self.completed_chain().journal.load()
        scheduler = self.scheduler(self.fail_dev, self.fail_ver)
        scheduler.context = {'requirements': 'changed'}
        self.assert_invalidate_integrity_failure(scheduler, 'Inputs changed')

    def test_invalidate_fails_closed_evidence_on_unrelated_task(self):
        self.completed_chain().journal.load()
        (self.completed_scheduler.directory / 'D/attempt-1/check-0.log').write_text('tampered')
        self.assert_invalidate_integrity_failure(self.scheduler(self.fail_dev, self.fail_ver), 'Completed task evidence is missing or altered')

    def test_invalidate_fails_closed_evidence_in_closure(self):
        self.completed_chain().journal.load()
        (self.completed_scheduler.directory / 'A/attempt-1/check-0.log').write_text('tampered')
        self.assert_invalidate_integrity_failure(self.scheduler(self.fail_dev, self.fail_ver), 'Completed task evidence is missing or altered')

    def test_invalidate_reason_is_data_only(self):
        scheduler = self.completed_chain()
        reason = "  double\" single' backslash\\\nλ $(touch PWNED) `touch PWNED2` ../../etc/passwd  "
        before_files = {p.relative_to(Path(self.tmp.name)) for p in Path(self.tmp.name).rglob('*')}
        status = git(self.wt, 'status', '--porcelain')
        count = len(self.file_bytes()[0].splitlines())
        baseline = self.file_bytes()
        scheduler.invalidate(['A'], reason)
        lines = self.file_bytes()[0].splitlines()
        self.assertEqual(len(lines), count + 1)
        self.assertEqual(json.loads(lines[-1])['state']['invalidations'][-1]['reason'], reason.strip())
        self.assertEqual({p.relative_to(Path(self.tmp.name)) for p in Path(self.tmp.name).rglob('*')}, before_files)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), status)
        for case in ('unknown', 'state', 'lock'):
            scheduler.journal.path.write_bytes(baseline[0])
            scheduler.journal.snapshot.write_bytes(baseline[1])
            scheduler.state = scheduler.journal.load()
            if case == 'state':
                self.journaled_state(lambda s: s['tasks']['B'].update(status='FAILED'))
            import contextlib
            with ticket_lock(scheduler.directory / 'scheduler.lock') if case == 'lock' else contextlib.nullcontext():
                with self.assertRaises(TaskError) as raised:
                    self.scheduler(self.fail_dev, self.fail_ver).invalidate(['Z'] if case == 'unknown' else ['A'], reason)
            for fragment in ('double"', "single'", 'backslash\\', 'λ', '$(touch PWNED)', '`touch PWNED2`', '../../etc/passwd'):
                self.assertNotIn(fragment, str(raised.exception))

    def test_invalidate_rejects_malformed_plan_before_io(self):
        scheduler = self.scheduler(self.fail_dev, self.fail_ver)
        for plan in ({}, {'tasks': None}, {'tasks': [{}]}, {'tasks': [None]},
                     {'tasks': [{'task_id': []}]}):
            with self.subTest(plan=plan):
                scheduler.plan = plan
                with patch.object(scheduler.journal, 'peek', side_effect=AssertionError('peek')):
                    with self.assertRaisesRegex(TaskError, '^Malformed plan$'):
                        scheduler.invalidate(['A'], 'reason')
        for deps, message in ((['Z'], 'Plan has unknown dependency'), (None, 'Malformed plan')):
            scheduler.plan = {'tasks': [{'task_id': 'A', 'depends_on': deps}]}
            with self.assertRaisesRegex(TaskError, '^' + message + '$'):
                scheduler.invalidate(['A'], 'reason')

    def test_invalidate_requires_scheduler_state(self):
        scheduler = self.scheduler(self.fail_dev, self.fail_ver)
        with self.assertRaisesRegex(TaskError, '^No scheduler state to invalidate$'):
            scheduler.invalidate(['T1'], 'reason')
        self.assertFalse(scheduler.journal.path.exists())
        self.assertFalse(scheduler.journal.snapshot.exists())

    def test_invalidate_docstring_states_limitations(self):
        doc = TaskScheduler.invalidate.__doc__
        for text in ('integrity-only', 'not authentication', 'ignored-file contents are not detected',
                     'upstream', 'DAG', 'Discard', 'TASKS_INVALIDATED', 'pre-change', 'backup', 'journal and snapshot'):
            self.assertIn(text, doc)

    def test_journal_peek_is_read_only(self):
        scheduler = self.completed_chain()
        expected = copy.deepcopy(scheduler.state)
        self.with_stale_snapshot()
        self.with_partial_tail()
        before = self.file_bytes()
        # Use a fresh instance with a distinct append position, independently of run().
        journal = self.scheduler(self.fail_dev, self.fail_ver).journal
        journal.sequence, journal.previous = 17, 'unchanged'
        with patch('ssdlc_tasks.atomic_json', side_effect=AssertionError('snapshot write')):
            self.assertEqual(journal.peek(), expected)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.tail_artifacts(), [])
        self.assertEqual((journal.sequence, journal.previous), (17, 'unchanged'))

        lines = before[0].splitlines(keepends=True)
        event = json.loads(lines[0])
        event['sha256'] = 'broken'
        lines[0] = (json.dumps(event) + '\n').encode()
        journal.path.write_bytes(b''.join(lines))
        broken = self.file_bytes()
        with self.assertRaisesRegex(TaskError, '^Corrupt task journal; refusing recovery$'):
            journal.peek()
        self.assertEqual(self.file_bytes(), broken)
        self.assertEqual(self.tail_artifacts(), [])
        self.assertEqual((journal.sequence, journal.previous), (17, 'unchanged'))

        journal.path.unlink()
        snapshot = journal.snapshot.read_bytes()
        self.assertIsNone(journal.peek())
        self.assertFalse(journal.path.exists())
        self.assertEqual(journal.snapshot.read_bytes(), snapshot)
        self.assertEqual(self.tail_artifacts(), [])
        self.assertEqual((journal.sequence, journal.previous), (17, 'unchanged'))

    def test_journal_recovery_refactor_order(self):
        scheduler = self.completed_chain()
        journal = self.scheduler(self.fail_dev, self.fail_ver).journal
        prefix = scheduler.journal.path.read_bytes()
        corrupt = b'{"sha256": "broken"}\n'
        tail = b'{incomplete'
        journal.path.write_bytes(prefix + corrupt + tail)
        self.with_stale_snapshot()
        before = self.file_bytes()
        with self.assertRaisesRegex(TaskError, '^Corrupt task journal; refusing recovery$'):
            journal.peek()
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.tail_artifacts(), [])

        with patch('ssdlc_tasks.atomic_json', side_effect=AssertionError('snapshot write')):
            with self.assertRaisesRegex(TaskError, '^Corrupt task journal; refusing recovery$'):
                journal.load()
        self.assertEqual(journal.path.read_bytes(), prefix + corrupt)
        self.assertEqual(journal.snapshot.read_bytes(), before[1])
        artifacts = self.tail_artifacts()
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].read_bytes(), tail)
        self.assertEqual(artifacts[0].name, 'interrupted-tail-' + file_hash(artifacts[0]) + '.bin')

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

    def test_failed_attempt_is_archived_then_repaired_in_place(self):
        def verify(*args):
            return ('Ran 1 test', 'failure' if len(self.calls) == 1 else '', 1 if len(self.calls) == 1 else 0)
        def develop(task, attempt, prompt):
            self.assertEqual((self.wt / 'app.py').read_text(), f'value = {attempt - 1}\n')
            if attempt == 2:
                self.assertIn('failure', prompt)
                self.assertIn('Repair the restored previous candidate', prompt)
            return self.developer(task, attempt, prompt)
        result = self.scheduler(develop, verify).run(self.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['attempts'], 2)
        self.assertTrue((self.artifacts / '03-dev/tasks/T1/attempt-1/implementation.diff').exists())
        self.assertEqual(git(self.wt, 'rev-list', '--count', self.base + '..HEAD'), '1')

    def test_attempt_limit_is_persistent(self):
        failed = lambda *args: ('Ran 1 test', 'FAIL' if self.calls else '', 1 if self.calls else 0)
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
            if self.calls:
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
        with self.assertRaisesRegex(TaskError, 'BASELINE_MISMATCH'):
            self.scheduler(self.developer, lambda *args: ('Ran 0 tests', '', 0), max_attempts=1).run(self.plan['tasks'])
        self.assertEqual(self.calls, [])
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
            self.scheduler(develop, lambda *args: ('Ran 1 test', 'FAIL', 1 if (self.wt / 'new.py').is_symlink() else 0), max_attempts=1).run(self.plan['tasks'])
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

    def test_developer_python_caches_are_redirected_per_invocation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'PYTEST_ADDOPTS': '--strict-markers'}):
            root = Path(tmp)
            (root / '.ssdlc').mkdir()
            commands = [EngineRunner._wrap_in_role_container(['codex', 'exec', '-'], root, writable_worktree=root) for _ in range(2)]
            environments = [{cmd[i+1].split('=', 1)[0]: cmd[i+1].split('=', 1)[1]
                             for i, arg in enumerate(cmd[:-1]) if arg == '-e'} for cmd in commands]
            for env in environments:
                self.assertTrue(Path(env['PYTHONPYCACHEPREFIX']).is_relative_to(root / '.ssdlc/.runtime/cache/roles'))
                self.assertIn('--strict-markers', env['PYTEST_ADDOPTS'])
                self.assertIn('cache_dir=', env['PYTEST_ADDOPTS'])
            self.assertNotEqual(environments[0]['PYTHONPYCACHEPREFIX'], environments[1]['PYTHONPYCACHEPREFIX'])

    def test_test_container_masks_secrets_and_keeps_worktree_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            wt = repo / '.ssdlc/TKT-1/worktree'
            wt.mkdir(parents=True)
            toolkit = Path(tmp) / 'toolkit'
            toolkit.mkdir()
            (toolkit / '.ssdlc').mkdir()
            with patch('orchestrator.__file__', str(toolkit / 'orchestrator.py')):
                cmd = EngineRunner._wrap_in_role_container(['pytest'], wt, pass_ai_keys=False)
            mounts = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == '-v']
            self.assertIn(f'{wt}:{wt}:ro', mounts)
            self.assertFalse(any('.credentials.json' in item or 'auth.json' in item for item in mounts))
            self.assertFalse(any('.runtime' in item and item.endswith(':rw') for item in mounts))
            self.assertIn(f'{repo / ".ssdlc"}:rw,size=16m', cmd)
            self.assertIn('/opt/ssdlc/.ssdlc:rw,size=16m', cmd)
            (toolkit / '.ssdlc').rmdir()
            with patch('orchestrator.__file__', str(toolkit / 'orchestrator.py')):
                pristine = EngineRunner._wrap_in_role_container(['pytest'], wt, pass_ai_keys=False)
            self.assertNotIn('/opt/ssdlc/.ssdlc:rw,size=16m', pristine)
            self.assertIn(f'{repo / ".ssdlc"}:rw,size=16m', pristine)


class OrchestratorBootstrapTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        environment = patch.object(SSDLCOrchestrator, '_verification_environment', return_value={
            'mode': 'role_container', 'platform': 'fixture',
            'image_id': 'sha256:' + '1' * 64, 'reuse_eligible': True})
        environment.start()
        self.addCleanup(environment.stop)

    def test_failure_pack_preserves_actual_failed_phase_and_category(self):
        orch = self.make_orchestrator()
        def fail(*args):
            orch.state.update(current_phase='03-dev', current_step='task-development', status='IN_PROGRESS')
            raise DevelopmentBlocked('BASELINE_MISMATCH', 'Clean worktree checks failed')
        def pack(**kwargs):
            orch.state.update(current_phase='08-supervisor')
        with patch.dict(os.environ, {'SSDLC_EXECUTION_MODE': 'local'}), patch.object(orch, '_ensure_quality_contract'), patch.object(orch, '_run_pipeline', side_effect=fail), patch.object(orch, 'compile_evidence_pack', side_effect=pack):
            with self.assertRaisesRegex(DevelopmentBlocked, 'BASELINE_MISMATCH'):
                orch.run_pipeline('Scoped goal')
        self.assertEqual(orch.state['current_phase'], '03-dev')
        self.assertEqual(orch.state['current_step'], 'task-development')
        self.assertEqual(orch.state['status'], 'FAILED')
        self.assertEqual(orch.state['failure']['phase'], '03-dev')
        self.assertEqual(orch.state['failure']['category'], 'BASELINE_MISMATCH')

    def test_controller_baseline_precedes_development_and_detects_committed_change(self):
        orch = self.make_orchestrator()
        self.seed_inputs(orch)
        with patch('orchestrator.__file__', str(self.repo / 'orchestrator.py')):
            baseline = orch._ensure_controller_contract()
            self.assertEqual(baseline['snapshot']['head'], self.base)
            self.assertIn('00-recon/controller-contract.json', orch.state['artifacts'])
            (self.repo / 'app.py').write_text('value = 9\n')
            git(self.repo, 'add', 'app.py')
            git(self.repo, 'commit', '-m', 'unexpected controller change')
            with patch('orchestrator.EngineRunner.run_codex') as developer:
                with self.assertRaisesRegex(TaskError, 'controller changed'):
                    orch._run_task_development()
                developer.assert_not_called()
            self.assertFalse(orch.worktree_dir.exists())

    def test_registered_controller_baseline_cannot_be_replaced_or_recaptured(self):
        orch = self.make_orchestrator()
        with patch('orchestrator.__file__', str(self.repo / 'orchestrator.py')):
            orch._ensure_controller_contract()
            path = orch.dir_recon / 'controller-contract.json'
            path.write_text('{}')
            with self.assertRaisesRegex(TaskError, 'changed controller contract'):
                orch._ensure_controller_contract()
            path.unlink()
            with self.assertRaisesRegex(TaskError, 'Missing controller contract'):
                orch._ensure_controller_contract()

    def make_orchestrator(self):
        runtime = self.repo / '.ssdlc/.runtime'
        meta = {'project_id': 'test-project', 'codex_home': str(runtime / 'codex'),
                'claude_home': str(runtime / 'claude'), 'cache_dir': str(runtime / 'cache')}
        with patch('orchestrator.ProjectRuntimeIsolation.activate', return_value=meta), patch('orchestrator.get_cli_version', return_value='TEST'):
            return SSDLCOrchestrator(self.repo, 'TKT-1', sync_remote=False)

    def prepare_checkpoint(self, orch):
        wt = orch._ensure_worktree()
        (wt / 'app.py').write_text('value = 1\n')
        git(wt, 'add', 'app.py')
        git(wt, 'commit', '-m', 'verified fixture')
        orch.state['task_verified_head'] = git(wt, 'rev-parse', 'HEAD')
        diff = orch.dir_dev / 'implementation.diff'
        diff.write_text(git(wt, 'diff', '--binary', self.base, 'HEAD') + '\n')
        orch.record_artifact('03-dev/implementation.diff', diff)

    def seed_inputs(self, orch):
        requirements = orch.dir_po / 'requirements.md'
        requirements.write_text('AC-1: Behavior\nSEC-1: Security invariant\n')
        quality_path = orch.dir_recon / 'quality-contract.json'
        plan_path = orch.dir_design / 'plan.json'
        atomic_json(quality_path, self.quality)
        atomic_json(plan_path, self.plan)
        for name, path in [('01-po/requirements.md', requirements), ('00-recon/quality-contract.json', quality_path), ('02-design/plan.json', plan_path)]:
            orch.record_artifact(name, path)

    @patch('orchestrator.EngineRunner._should_spawn_role_container', return_value=True)
    def test_task_path_uses_existing_routing_and_resumes_without_ai(self, _runtime):
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            self.seed_inputs(orch)
            orch.state['status'] = 'PLAN_READY'
            orch._save_state()
            def generate(prompt, **kwargs):
                self.assertEqual(orch.state['status'], 'IN_PROGRESS')
                (kwargs['cwd'] / 'app.py').write_text('value = 1\n')
                return 'Done', 0
            with patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('codex', 'fake', 'low', 'test')), patch('orchestrator.EngineRunner.run_codex', side_effect=generate) as engine, patch('orchestrator.EngineRunner.run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)):
                diff, files = orch._run_task_development()
                self.assertIn('app.py', files)
                self.assertIn('+value = 1', diff.read_text())
                orch._run_task_development()
                self.assertEqual(engine.call_count, 1)
                self.assertEqual(orch.state['tasks_status']['T1']['status'], 'COMPLETED')

    @patch('orchestrator.EngineRunner._should_spawn_role_container', return_value=True)
    def test_changed_environment_preserves_capture_before_refusing_verification(self, _runtime):
        orch = self.make_orchestrator()
        self.seed_inputs(orch)
        environment = {'mode': 'role_container', 'platform': 'fixture', 'image_id': 'sha256:' + '1' * 64}
        def generate(prompt, **kwargs):
            (kwargs['cwd'] / 'app.py').write_text('value = 1\n')
            environment['image_id'] = 'sha256:' + '2' * 64
            return 'Done', 0
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}), \
                patch.object(orch, '_verification_environment', side_effect=lambda *a: dict(environment)), \
                patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('codex', 'fake', 'low', 'test')), \
                patch('orchestrator.EngineRunner.run_codex', side_effect=generate) as engine, \
                patch('orchestrator.EngineRunner.run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)):
            with self.assertRaisesRegex(DevelopmentBlocked, 'environment changed'):
                orch.run_phase_dev()
            self.assertTrue((orch.dir_dev / 'tasks/T1/attempt-1/capture/receipt.json').is_file())
            self.assertEqual((orch.worktree_dir / 'app.py').read_text(), 'value = 0\n')
            environment['image_id'] = 'sha256:' + '1' * 64
            orch.run_phase_dev()
            self.assertEqual(engine.call_count, 1)

    def test_default_task_path_refuses_missing_quality_before_dispatch(self):
        orch = self.make_orchestrator()
        with patch('orchestrator.EngineRunner.run_codex') as developer:
            with self.assertRaisesRegex(PlanError, 'No project checks'):
                orch._run_task_development()
            developer.assert_not_called()

    def test_missing_or_tampered_plan_never_dispatches(self):
        orch = self.make_orchestrator()
        self.seed_inputs(orch)
        (orch.dir_design / 'plan.json').write_text('{}')
        with patch('orchestrator.EngineRunner.run_codex') as developer:
            with self.assertRaisesRegex(PlanError, 'unregistered or changed'):
                orch._run_task_development()
            developer.assert_not_called()

    def test_missing_registered_plan_is_not_regenerated(self):
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            self.seed_inputs(orch)
            (orch.dir_design / 'plan.json').unlink()
            with patch('orchestrator.EngineRunner.invoke_role') as engine:
                with self.assertRaisesRegex(PlanError, 'disappeared'):
                    orch._run_task_development()
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
                    orch._run_task_development()
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
                    validator = TaskPlanValidator(orch.repo_root, requirements.read_text(),
                                                  payload['quality_contract'], orch.ssdlc_root)
                    self.assertEqual(payload['requirement_catalogue'], validator.requirement_catalogue)
                    self.assertEqual(payload['available_contract_refs'], validator.available_contract_refs())
                    self.assertEqual(payload['requirement_catalogue']['required_ids'], ['AC-1', 'SEC-1'])
                    self.assertIn('additionalProperties', system)
                    out = json.dumps(planned)
                else:
                    self.assertEqual(payload['plan'], planned)
                    self.assertIn('execution_contract', payload['context'])
                    self.assertEqual(payload['review_history'], [])
                    impact = payload['fixture_impact']
                    self.assertEqual(impact['changed_paths'], ['app.py'])
                    self.assertFalse(impact['complete_dependency_proof'])
                    ref = '02-design/task-planning/fixture-impact-' + digest(planned) + '.json'
                    self.assertEqual(orch.state['artifacts'][ref]['sha256'], file_hash(orch.ssdlc_root / ref))
                    out = json.dumps({'verdict': 'APPROVED', 'findings': []})
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


    @patch('orchestrator.EngineRunner._should_spawn_role_container', return_value=True)
    def test_pipeline_runs_global_gates_and_bounce_cannot_be_ignored(self, _runtime):
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
                        with patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('codex', 'fake', 'low', 'test')), patch('orchestrator.EngineRunner.run_codex', side_effect=generate), patch('orchestrator.EngineRunner.run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)), patch.object(orch, 'run_phase_review', return_value=(review_passes, 'Review result')), patch.object(orch, 'run_phase_qa', return_value=(True, 'QA passed')) as qa, patch.object(orch, 'run_phase_appsec', return_value=(True, 'AppSec passed')) as appsec:
                            if review_passes:
                                evidence = orch.run_pipeline('Scoped goal', from_phase='03-dev')
                                self.assertTrue(evidence.exists())
                                self.assertEqual(orch.state['status'], 'AWAITING_SUPERVISOR_SIGNOFF')
                                qa.assert_called_once()
                                appsec.assert_called_once_with(attempt=1)
                            else:
                                with self.assertRaisesRegex(TaskError, 'explicit invalidation'):
                                    orch.run_pipeline('Scoped goal', from_phase='03-dev')
                                self.assertEqual(orch.state['status'], 'ESCALATED_TO_SUPERVISOR')
                                qa.assert_not_called()
                                appsec.assert_not_called()
                                self.assertTrue((orch.ssdlc_root / 'EVIDENCE_PACK.md').exists())
                    finally:
                        self.repo, self.base, self.artifacts, self.quality, self.plan = old_repo, old_base, old_artifacts, old_quality, old_plan

    def test_phase_review_requests_concrete_edge_case_analysis(self):
        orch = self.make_orchestrator()
        self.seed_inputs(orch)
        self.prepare_checkpoint(orch)
        captured_sys_prompts = []

        def fake_invoke_role(engine, model, sys_prompt, user_prompt, *args, **kwargs):
            captured_sys_prompts.append(sys_prompt)
            from test_full_checkpoint_review import framed
            return framed('review', orch), 0, 10, 10, 'trace'


        with patch("orchestrator.SquadDispatcher.resolve_review_models", return_value=(["mock-engine:mock-model"], "low")), \
             patch("orchestrator.EngineRunner.invoke_role", side_effect=fake_invoke_role):
            passed, report = orch.run_phase_review(attempt=1)
            self.assertTrue(passed)
            self.assertTrue(len(captured_sys_prompts) > 0)
            self.assertIn("Edge cases & boundary conditions", captured_sys_prompts[0])
            self.assertIn("Edge Cases & Boundary Conditions Analysis", captured_sys_prompts[0])
            for requirement in ("concrete scenario", "expected behavior", "implementation handling",
                                "test gap", "crashes", "retries", "recovery", "concurrency"):
                self.assertIn(requirement, captured_sys_prompts[0])

    def test_review_gate_requires_one_unambiguous_final_verdict(self):
        orch = self.make_orchestrator()
        self.seed_inputs(orch)
        self.prepare_checkpoint(orch)
        cases = [
            ('Analysis\nREVIEW_STATUS: APPROVED\n\n', True, 'APPROVED'),
            ('Analysis\nREVIEW_STATUS: CHANGES_REQUESTED', False, 'CHANGES_REQUESTED'),
            ('Example: REVIEW_STATUS: APPROVED\nREVIEW_STATUS: CHANGES_REQUESTED', False, 'INVALID_VERDICT'),
            ('REVIEW_STATUS: CHANGES_REQUESTED\nREVIEW_STATUS: APPROVED', False, 'INVALID_VERDICT'),
            ('REVIEW_STATUS: APPROVED\nREVIEW_STATUS: APPROVED', False, 'INVALID_VERDICT'),
            ('REVIEW_STATUS: APPROVED\nMore analysis', False, 'INVALID_VERDICT'),
            ('```\nREVIEW_STATUS: APPROVED\n```', False, 'INVALID_VERDICT'),
            ('REVIEW_STATUS: APPROVED (no defects)', False, 'INVALID_VERDICT'),
            ('REVIEW_STATUS: UNKNOWN', False, 'INVALID_VERDICT'),
            ('No verdict provided', False, 'INVALID_VERDICT'),
            ('', False, 'INVALID_VERDICT'),
        ]
        for attempt, (response, expected, ledger_status) in enumerate(cases, 1):
            with self.subTest(response=response), patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:reviewer'], 'low')), patch('orchestrator.EngineRunner.invoke_role', return_value=(response, 0, 0, 0, 'trace')):
                from orchestrator import parse_review_verdict
                verdict = parse_review_verdict(response)
                self.assertEqual(verdict == 'APPROVED', expected)
                self.assertEqual(verdict or 'INVALID_VERDICT', ledger_status)
                from test_full_checkpoint_review import framed
                from ssdlc_gate_reports import GateTerminalError
                def invoke(*args, **kwargs):
                    prefix = framed('review', orch).rsplit('REVIEW_STATUS:', 1)[0]
                    return prefix + response, 0, 0, 0, 'trace'
                with patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
                    try:
                        passed, report = orch.run_phase_review(attempt)
                    except GateTerminalError:
                        passed = False
                self.assertEqual(passed, expected)
                self.assertEqual(orch.state['gate_history'][-1]['passed'], expected)
                self.assertTrue(any(response in p.read_text() for p in orch.dir_review.glob('evaluations/*/response-1.md')))


    def test_one_invalid_reviewer_blocks_panel_approval(self):
        orch = self.make_orchestrator()
        self.seed_inputs(orch)
        self.prepare_checkpoint(orch)
        from test_full_checkpoint_review import framed
        calls = []
        def responses(*args, **kwargs):
            calls.append(1)
            return (framed('review', orch) if len(calls) == 1 else 'REVIEW_STATUS: APPROVED\nREVIEW_STATUS: CHANGES_REQUESTED'), 0, 0, 0, 'trace'
        with patch('orchestrator.SquadDispatcher.resolve_review_models', return_value=(['mock:one', 'mock:two'], 'low')), patch('orchestrator.EngineRunner.invoke_role', side_effect=responses):
            from ssdlc_gate_reports import GateTerminalError
            with self.assertRaises(GateTerminalError):
                orch.run_phase_review()


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
            self.assertNotIn('SSDLC_ENABLE_TASK_BREAKDOWN', (repo / '.ssdlc/project.env').read_text())
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


class PromptTransportTests(unittest.TestCase):
    def test_large_prompts_use_stdin_and_codex_traces_do_not_enter_phase_result(self):
        prompt = 'x' * 200000
        for engine in ('codex', 'claude'):
            with self.subTest(engine=engine), patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), patch('orchestrator.subprocess.run') as run:
                output = 'phase result' if engine == 'codex' else json.dumps({'result': 'phase result'})
                run.return_value = subprocess.CompletedProcess([], 0, output, 'tool transcript')
                result = getattr(EngineRunner, 'run_' + engine)(prompt, Path.cwd())
                self.assertEqual(result[0], 'phase result')
                self.assertEqual(run.call_args.kwargs['input'], prompt)
                self.assertNotIn(prompt, run.call_args.args[0])

    def test_docker_preserves_piped_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.ssdlc').mkdir()
            cmd = EngineRunner._wrap_in_role_container(['codex', 'exec', '-'], root, stdin=True)
            self.assertEqual(cmd[:3], ['docker', 'run', '-i'])


class RevalidateTests(SchedulerTests):
    def assert_evidence_root_redirection_refused(self, redirect_parent=False):
        scheduler = self.invalidated_chain()
        root = scheduler.directory
        originals = {root/c['log_ref']: (root/c['log_ref']).read_bytes()
                     for task in scheduler.state['tasks'].values() for c in task['checks']}
        def redirect():
            source = root.parent if redirect_parent else root
            destination = source.with_name(source.name + '-redirected')
            source.rename(destination)
            source.symlink_to(destination, target_is_directory=True)
        scheduler.verifier = self.scripted_verifier([redirect])
        with self.assertRaisesRegex(TaskError, 'Evidence root changed during verification'):
            scheduler.revalidate()
        self.assertEqual(len(self.verifier_calls), 1)
        self.assertEqual(scheduler.state['tasks']['A']['status'], 'INVALIDATED')
        self.assertEqual(scheduler.state['tasks']['B']['status'], 'INVALIDATED')
        self.assertEqual(scheduler.state['tasks']['D']['status'], 'COMPLETED')
        run = scheduler.state['revalidation_runs'][-1]
        self.assertEqual(run['status'], 'ABORTED')
        self.assertEqual(run['cursor']['task_id'], 'A')
        self.assertEqual(run['cursor']['check_id'], 'CHECK-A')
        attempt = scheduler.state['tasks']['A']['revalidations'][-1]
        self.assertEqual(attempt['checks'], [])
        self.assertFalse((root/attempt['dir']/'check-0.log').exists())
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    def test_evidence_root_redirection_stops_before_writing_check_log(self):
        self.assert_evidence_root_redirection_refused()

    def test_evidence_root_ancestor_redirection_stops_before_writing_check_log(self):
        self.assert_evidence_root_redirection_refused(redirect_parent=True)

    def scripted_verifier(self, actions=()):
        self.verifier_calls = []
        actions = iter(actions)

        def verify(argv, cwd, timeout):
            self.verifier_calls.append((argv, cwd, timeout))
            action = next(actions, None)
            if action is not None:
                result = action()
                if result is not None:
                    return result
            return 'Ran 1 test\nOK', '', 0
        return verify

    def invalidated_chain(self):
        scheduler = self.completed_chain()
        scheduler.invalidate(['A'], 'precaution')
        scheduler.developer = self.fail_dev
        scheduler.verifier = self.scripted_verifier()
        return scheduler

    def test_all_pass_ordered(self):
        scheduler = self.invalidated_chain()
        head = git(self.wt, 'rev-parse', 'HEAD')
        count = git(self.wt, 'rev-list', '--count', 'HEAD')
        sources = {p: (self.wt / p).read_bytes() for p in ('a.py', 'b.py', 'c.py', 'd.py')}
        result = scheduler.revalidate()
        self.assertEqual(result.passed, ['A', 'B', 'C'])
        self.assertEqual(result.unchanged, ['D'])
        self.assertTrue(result.complete)
        self.assertEqual(len(self.verifier_calls), 6)
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.wt, 'rev-list', '--count', 'HEAD'), count)
        self.assertEqual(sources, {p: (self.wt / p).read_bytes() for p in sources})
        for tid in result.passed:
            attempt = scheduler.state['tasks'][tid]['revalidations'][0]
            self.assertEqual(attempt['head'], head)
            self.assertEqual(attempt['tree'], git(self.wt, 'rev-parse', 'HEAD^{tree}'))
            self.assertTrue((scheduler.directory / attempt['dir']).is_dir())

    def test_start_marker_ordering(self):
        scheduler = self.invalidated_chain()
        observed = []
        def verify(*args):
            events = [json.loads(line) for line in scheduler.journal.path.read_text().splitlines()]
            self.assertEqual(events[-1]['event'], 'REVALIDATION_CHECK_STARTED')
            run = events[-1]['state']['revalidation_runs'][-1]
            cursor = run['cursor']
            observed.append((cursor['task_id'], cursor['check_id']))
            return 'Ran 1 test\nOK', '', 0
        scheduler.verifier = verify
        scheduler.revalidate()
        expected = [(tid, cid) for tid in ('A', 'B', 'C')
                    for cid in scheduler._expected_check_ids(tid)]
        self.assertEqual(observed, expected)
        events = [json.loads(line) for line in scheduler.journal.path.read_text().splitlines()]
        self.assertEqual(sum(e['event'] == 'REVALIDATION_CHECK_STARTED' for e in events), len(expected))
        for tid in ('A', 'B', 'C'):
            attempt = scheduler.state['tasks'][tid]['revalidations'][0]
            self.assertEqual([c['check_id'] for c in attempt['started_checks']],
                             scheduler._expected_check_ids(tid))
        self.scheduler(self.fail_dev, self.fail_ver).run(self.plan['tasks'])

    def test_b_fails_c_blocked(self):
        scheduler = self.invalidated_chain()
        before = copy.deepcopy(scheduler.state['tasks']['D'])
        scheduler.verifier = self.scripted_verifier([None, None, lambda: ('failed', '', 1)])
        result = scheduler.revalidate()
        self.assertEqual(result.passed, ['A'])
        self.assertEqual([f['task_id'] for f in result.failed], ['B'])
        self.assertEqual(result.blocked, [dict(task_id='C', blocked_by=['B'])])
        self.assertFalse(result.complete)
        check = result.failed[0]['checks'][0]
        self.assertEqual(check['outcome'], 'exit:1')
        self.assertEqual(file_hash(scheduler.directory / check['log_ref']), check['sha256'])
        self.assertEqual(len(self.verifier_calls), 4)
        self.assertEqual(scheduler.state['tasks']['D'], before)

    def test_later_success_recovers(self):
        scheduler = self.invalidated_chain()
        scheduler.verifier = self.scripted_verifier([None, None, lambda: ('failed', '', 1)])
        scheduler.revalidate()
        old = scheduler.state['tasks']['B']['revalidations'][0]['checks'][0]
        scheduler.verifier = self.scripted_verifier()
        result = scheduler.revalidate()
        self.assertEqual(result.passed, ['B', 'C'])
        self.assertEqual(len(self.verifier_calls), 4)
        self.assertEqual(scheduler.state['tasks']['B']['restored_by'], 2)
        self.assertEqual(file_hash(scheduler.directory / old['log_ref']), old['sha256'])

    def test_provenance_and_logs_preserved(self):
        scheduler = self.invalidated_chain()
        before = copy.deepcopy(scheduler.state)
        prefix = scheduler.journal.path.read_bytes()
        logs = {p: p.read_bytes() for p in scheduler.directory.rglob('*') if p.is_file()
                and p.suffix in ('.log', '.diff')}
        scheduler.revalidate()
        self.assertTrue(scheduler.journal.path.read_bytes().startswith(prefix))
        for tid, record in before['tasks'].items():
            for key, value in record.items():
                if key != 'status':
                    self.assertEqual(scheduler.state['tasks'][tid][key], value)
        self.assertTrue(all(p.read_bytes() == data for p, data in logs.items()))

    def assert_revalidation_refused(self, scheduler):
        scheduler.verifier = self.fail_ver
        before = scheduler.journal.path.read_bytes() if scheduler.journal.path.exists() else None
        with self.assertRaises(TaskError):
            scheduler.revalidate()
        after = scheduler.journal.path.read_bytes() if scheduler.journal.path.exists() else None
        self.assertEqual(before, after)

    def test_refusals_before_any_check(self):
        scheduler = self.scheduler(self.fail_dev, self.fail_ver)
        self.assert_revalidation_refused(scheduler)
        scheduler = self.invalidated_chain()
        baseline = copy.deepcopy(scheduler.state)
        for status in ('PENDING', 'RUNNING', 'FAILED'):
            self.journaled_state(lambda state: state['tasks']['A'].update(status=status))
            self.assert_revalidation_refused(scheduler)
            scheduler.state = copy.deepcopy(baseline)
            scheduler._save('TEST_RESTORE')
        for field in ('plan', 'quality', 'context'):
            value = getattr(scheduler, field)
            setattr(scheduler, field, dict(value, changed=True))
            self.assert_revalidation_refused(scheduler)
            setattr(scheduler, field, value)
        source = self.wt / 'a.py'
        data = source.read_bytes()
        source.write_text('dirty')
        self.assert_revalidation_refused(scheduler)
        source.write_bytes(data)
        refs = [baseline['tasks'][tid]['checks'][0]['log_ref'] for tid in ('A', 'D')]
        refs.append(baseline['preflights'][0]['checks'][0]['log_ref'])
        for ref in refs:
            log = scheduler.directory / ref
            data = log.read_bytes()
            for tamper in ('alter', 'delete'):
                if tamper == 'alter':
                    log.write_text('tampered')
                else:
                    log.unlink()
                self.assert_revalidation_refused(scheduler)
                log.write_bytes(data)
        self.assertEqual(scheduler.journal.peek()['tasks']['A']['status'], 'INVALIDATED')
        protected = self.wt / '.agents'
        protected.mkdir()
        (protected / 'changed').write_text('protected')
        self.assert_revalidation_refused(scheduler)
        (protected / 'changed').unlink()
        protected.rmdir()
        ignored = self.wt / '.ssdlc' / 'new-ignored'
        ignored.parent.mkdir(exist_ok=True)
        ignored.write_text('ignored')
        self.assert_revalidation_refused(scheduler)
        ignored.unlink()
        head = git(self.wt, 'rev-parse', 'HEAD')
        git(self.wt, 'commit', '--allow-empty', '-m', 'external head')
        self.assert_revalidation_refused(scheduler)
        # Return this fake repository to its checkpoint without rewriting source.
        git(self.wt, 'update-ref', 'HEAD', head)
        scheduler.verifier = self.scripted_verifier()
        scheduler.revalidate()
        scheduler.invalidate(['A'], 'again')
        log = scheduler.directory / scheduler.state['tasks']['A']['revalidations'][0]['checks'][0]['log_ref']
        data = log.read_bytes()
        log.unlink()
        self.assert_revalidation_refused(scheduler)
        log.write_bytes(data)

    def test_symlink_loop_preflight_refusal(self):
        scheduler = self.invalidated_chain()
        ref = scheduler.state['tasks']['D']['checks'][0]['log_ref']
        log = scheduler.directory / ref
        log.unlink()
        log.symlink_to(log.name)
        self.assertTrue(log.is_symlink())
        self.assertEqual(os.readlink(log), log.name)
        before = scheduler.journal.path.read_bytes()
        with self.assertRaisesRegex(TaskError, "Evidence invalid for task 'D' check 'CHECK-D'"):
            scheduler.revalidate()
        self.assertEqual(self.verifier_calls, [])
        self.assertEqual(before, scheduler.journal.path.read_bytes())

    def test_inventory_scope_documented(self):
        scheduler = self.invalidated_chain()
        for name in ('developer.log', 'manifest.json', 'implementation.diff'):
            path = scheduler.directory / 'A' / 'attempt-1' / name
            self.assertTrue(path.exists())
            path.write_text('outside recorded hash inventory')
        self.assertTrue(scheduler.revalidate().complete)

    def test_lock_contention(self):
        scheduler = self.invalidated_chain()
        with ticket_lock(scheduler.directory / 'scheduler.lock'):
            self.assert_revalidation_refused(scheduler)
        def competing():
            other = self.scheduler(self.fail_dev, self.fail_ver)
            for action in (lambda: other.invalidate(['A'], 'busy'),
                           lambda: other.run(self.plan['tasks'])):
                with self.assertRaisesRegex(TaskError, 'Another process'):
                    action()
        scheduler.verifier = self.scripted_verifier([competing])
        scheduler.revalidate()

    def test_idempotent_after_complete(self):
        scheduler = self.invalidated_chain()
        scheduler.revalidate()
        before = scheduler.journal.path.read_bytes()
        scheduler.verifier = self.fail_ver
        result = scheduler.revalidate()
        self.assertEqual((result.passed, result.failed, result.blocked), ([], [], []))
        self.assertTrue(result.complete)
        self.assertEqual(before, scheduler.journal.path.read_bytes())
        log = scheduler.directory / scheduler.state['tasks']['B']['revalidations'][0]['checks'][0]['log_ref']
        log.write_text('tampered')
        self.assert_revalidation_refused(scheduler)

    def test_dependency_graph_errors(self):
        scheduler = self.invalidated_chain()
        original = copy.deepcopy(self.plan)
        for deps in (['unknown'], ['C']):
            self.plan['tasks'][0]['depends_on'] = deps
            self.journaled_state(lambda state: state.update(inputs_sha256=digest(
                dict(plan=self.plan, quality=self.quality, context=scheduler.context))))
            self.assert_revalidation_refused(scheduler)
        self.plan.clear()
        self.plan.update(original)
        self.journaled_state(lambda state: state.update(inputs_sha256=digest(
            dict(plan=self.plan, quality=self.quality, context=scheduler.context))))
        self.journaled_state(lambda state: state['tasks']['C'].update(status='COMPLETED'))
        self.assert_revalidation_refused(scheduler)

    def test_run_enforces_revalidation_evidence(self):
        scheduler = self.invalidated_chain()
        scheduler.revalidate()
        baseline = copy.deepcopy(scheduler.state)
        mutations = [
            lambda a: a.update(status='FAILED'),
            lambda a: a['checks'].pop(),
            lambda a: a['checks'].reverse(),
            lambda a: a['checks'][0].update(check_id='substitute'),
            lambda a: a['started_checks'].pop(),
            lambda a: a.update(head='wrong'),
        ]
        for mutate in mutations:
            scheduler.state = copy.deepcopy(baseline)
            mutate(scheduler.state['tasks']['B']['revalidations'][0])
            scheduler._save('TEST_TAMPER')
            with self.assertRaisesRegex(TaskError, 'Completed task evidence'):
                self.scheduler(self.fail_dev, self.fail_ver).run(self.plan['tasks'])
            self.assert_revalidation_refused(scheduler)
        scheduler.state = baseline
        scheduler._save('TEST_RESTORE')
        for mutation in ('duplicate', 'missing_restored_by'):
            scheduler.state = copy.deepcopy(baseline)
            record = scheduler.state['tasks']['B']
            if mutation == 'duplicate':
                record['revalidations'].append(copy.deepcopy(record['revalidations'][0]))
            else:
                record['restored_by'] = 999
            scheduler._save('TEST_TAMPER')
            with self.assertRaisesRegex(TaskError, 'Completed task evidence'):
                self.scheduler(self.fail_dev, self.fail_ver).run(self.plan['tasks'])
            self.assert_revalidation_refused(scheduler)
        scheduler.state = baseline
        scheduler._save('TEST_RESTORE')
        self.scheduler(self.fail_dev, self.fail_ver).run(self.plan['tasks'])

    def test_run_interplay(self):
        scheduler = self.invalidated_chain()
        with self.assertRaisesRegex(TaskError, 'revalidation API'):
            self.scheduler(self.fail_dev, self.fail_ver).run(self.plan['tasks'])
        scheduler.revalidate()
        self.scheduler(self.fail_dev, self.fail_ver).run(self.plan['tasks'])

    def test_docstring_states_limitations(self):
        doc = TaskScheduler.revalidate.__doc__
        for text in ('unkeyed', 'not authentication', 'Ignored-file contents',
                     'external writers', 'developer.log', 'manifest.json',
                     'implementation.diff', 'unreferenced partial logs', 'baseline logs'):
            self.assertIn(text, doc)

    def test_check_plan_equivalence(self):
        scheduler = self.completed_chain()
        task = self.plan['tasks'][0]
        task['verification']['checks'][0].update(argv=['task-check'], timeout_sec=7)
        directory = scheduler.directory / 'A' / 'probe'
        directory.mkdir()
        scheduler.verifier = self.scripted_verifier()
        record = {}
        starts, finishes = [], []
        results = scheduler._verify(task, record, directory,
                                    before_check=lambda i, cid: starts.append((i, cid)),
                                    after_check=lambda i, result: finishes.append((i, result)))
        plan = scheduler._check_plan(task)
        self.assertEqual(self.verifier_calls,
                         [(c['argv'], confined(self.wt, c['cwd']), c['timeout_sec']) for c in plan])
        self.assertEqual([c['check_id'] for c in results], scheduler._expected_check_ids('A'))
        self.assertEqual(starts, list(enumerate(scheduler._expected_check_ids('A'))))
        self.assertEqual(finishes, list(enumerate(results)))

    def test_exclusive_log_writes(self):
        from ssdlc_tasks import _EvidenceCollision
        scheduler = self.completed_chain()
        task = self.plan['tasks'][0]
        prior = scheduler.directory / 'prior.log'
        prior.write_text('preserve')
        for shape in ('file', 'symlink', 'fresh'):
            with self.subTest(shape=shape):
                directory = scheduler.directory / 'A' / shape
                scheduler._make_attempt_dir(directory)
                log = directory / 'check-0.log'
                def collide():
                    if shape == 'file':
                        log.write_text('existing')
                    elif shape == 'symlink':
                        log.symlink_to(prior)
                scheduler.verifier = self.scripted_verifier([collide])
                if shape == 'fresh':
                    results = scheduler._verify(task, {}, directory, exclusive=True)
                    self.assertEqual(file_hash(log), results[0]['sha256'])
                else:
                    with self.assertRaises(_EvidenceCollision) as caught:
                        scheduler._verify(task, {}, directory, exclusive=True)
                    self.assertEqual(caught.exception.kind, 'collision')
                    self.assertEqual(caught.exception.check_id, 'CHECK-A')
                    self.assertEqual(log.read_text(), 'existing' if shape == 'file' else 'preserve')
                self.assertEqual(prior.read_text(), 'preserve')

    def test_run_refuses_failed_attempt_evidence_tamper(self):
        # First task verification fails after the global checkpoint passes.
        scheduler = self.scheduler(self.developer, self.scripted_verifier([
            None, lambda: ('Ran 1 test\nFAILED', '', 1)]))
        scheduler.run(self.plan['tasks'])
        record = scheduler.state['tasks']['T1']
        self.assertEqual(len(record['failed_attempts']), 1)
        log = scheduler.directory / record['failed_attempts'][0]['checks'][0]['log_ref']
        log.write_text('altered')
        fresh = self.scheduler(self.fail_dev, self.fail_ver)
        with self.assertRaisesRegex(TaskError, '^Completed task evidence is missing or altered$'):
            fresh.run(self.plan['tasks'])

    def test_symlink_loop_run_refusal(self):
        scheduler = self.completed_chain()
        ref = scheduler.state['tasks']['A']['checks'][0]['log_ref']
        log = scheduler.directory / ref
        log.unlink()
        log.symlink_to(log.name)
        self.assertTrue(log.is_symlink())
        self.assertEqual(os.readlink(log), log.name)
        problems = scheduler._evidence_problems(scheduler.state['tasks']['A'], 'A')
        self.assertEqual(problems[0]['task_id'], 'A')
        self.assertEqual(problems[0]['path'], ref)
        with self.assertRaisesRegex(TaskError, '^Completed task evidence is missing or altered$'):
            self.scheduler(self.fail_dev, self.fail_ver).run(self.plan['tasks'])

    def recovered(self, scheduler):
        fresh = self.scheduler(self.fail_dev, self.scripted_verifier())
        head = git(self.wt, 'rev-parse', 'HEAD')
        self.assertEqual(head, scheduler.journal.peek()['expected_head'])
        preserved = {p: p.read_bytes() for p in scheduler.directory.rglob('*')
                     if p.is_file() and p.suffix in ('.log', '.diff')}
        durable = fresh.journal.peek()
        forged = copy.deepcopy(durable)
        for record in forged['tasks'].values():
            record['status'] = 'COMPLETED'
        forged['untrusted_snapshot'] = True
        atomic_json(fresh.journal.snapshot, forged)
        self.assertEqual(fresh.journal.load(), durable)
        fresh.state = copy.deepcopy(durable)
        self.assertEqual(fresh._inventory_problems(durable), [])
        completed = [t for t, r in durable['tasks'].items() if r['status'] == 'COMPLETED']
        for record in durable['tasks'].values():
            attempts = record.get('revalidations', [])
            for key in ('attempt', 'dir'):
                self.assertEqual(len(attempts), len({a[key] for a in attempts}))
        fresh.revalidate()
        self.assertEqual(fresh._inventory_problems(fresh.state), [])
        self.assertEqual(len(self.verifier_calls), 2 * (4 - len(completed)))
        self.assertTrue(all(r['status'] == 'COMPLETED' for r in fresh.state['tasks'].values()))
        fresh.run(self.plan['tasks'])
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), head)
        for path, contents in preserved.items():
            self.assertEqual(path.read_bytes(), contents)
        return fresh

    def test_check_start_markers(self):
        scheduler = self.invalidated_chain()
        def crash():
            raise OSError('verifier interrupted')
        scheduler.verifier = self.scripted_verifier([None, crash])
        with self.assertRaisesRegex(OSError, 'verifier interrupted'):
            scheduler.revalidate()
        attempt = scheduler.journal.peek()['tasks']['A']['revalidations'][0]
        self.assertEqual(len(attempt['started_checks']), 2)
        self.assertEqual(len(attempt['checks']), 1)
        partial = scheduler.directory / attempt['dir']
        saved = (partial / 'check-0.log').read_bytes()
        fresh = self.recovered(scheduler)
        self.assertEqual(fresh.state['tasks']['A']['revalidations'][0]['status'], 'INTERRUPTED')
        self.assertNotEqual(fresh.state['tasks']['A']['revalidations'][-1]['dir'], attempt['dir'])
        self.assertEqual((partial / 'check-0.log').read_bytes(), saved)

    def test_attempt_ids_unique_after_collision_and_crash(self):
        scheduler = self.invalidated_chain()
        occupied = scheduler.directory / 'A/revalidate-1'
        occupied.mkdir()
        (occupied / 'keep').write_text('preserved')
        with patch.object(scheduler, '_make_attempt_dir', side_effect=OSError('F1')):
            with self.assertRaises(OSError):
                scheduler.revalidate()
        fresh = self.recovered(scheduler)
        attempts = fresh.state['tasks']['A']['revalidations']
        self.assertEqual([a['attempt'] for a in attempts], [2, 3])
        self.assertFalse((scheduler.directory / 'A/revalidate-2').exists())
        self.assertEqual((occupied / 'keep').read_text(), 'preserved')

    def mutation_case(self, mutate, kind='evidence_changed', actions_before=0):
        scheduler = self.invalidated_chain()
        def action():
            mutate(scheduler)
        scheduler.verifier = self.scripted_verifier([None] * actions_before + [action])
        with self.assertRaises(TaskError) as caught:
            scheduler.revalidate()
        state = scheduler.journal.peek()
        aborted = [a for r in state['tasks'].values() for a in r.get('revalidations', [])
                   if a['status'] == 'ABORTED']
        self.assertEqual(len(aborted), 1)
        self.assertEqual(aborted[0]['abort']['kind'], kind)
        self.assertIn('check', str(caught.exception))
        self.assertEqual(len(self.verifier_calls), actions_before + 1)
        return scheduler

    def test_evidence_altered_mid_operation(self):
        self.mutation_case(lambda s: (s.directory / 'A/revalidate-1/check-0.log').write_text('tamper'),
                           actions_before=1)

    def test_inventory_preflight_evidence(self):
        scheduler = self.invalidated_chain()
        ref = scheduler.state['preflights'][0]['checks'][0]['log_ref']
        path = scheduler.directory / ref
        saved = path.read_bytes()
        before = scheduler.journal.path.read_bytes()
        for delete in (False, True):
            if delete:
                path.unlink()
            else:
                path.write_text('tampered preflight')
            with self.assertRaisesRegex(TaskError, 'Evidence invalid'):
                self.scheduler(self.fail_dev, self.fail_ver).revalidate()
            self.assertEqual(scheduler.journal.path.read_bytes(), before)
            path.write_bytes(saved)
        def alter():
            path.write_text('tampered during verification')
        scheduler.verifier = self.scripted_verifier([alter])
        with self.assertRaises(TaskError):
            scheduler.revalidate()
        abort = scheduler.journal.peek()['tasks']['A']['revalidations'][0]['abort']
        self.assertEqual(abort['kind'], 'evidence_changed')
        self.assertEqual(abort['path'], ref)

    def test_inventory_failed_attempt_evidence(self):
        self.plan['tasks'] = [self.task('A', [], [], ['a.py'])]
        scheduler = self.scheduler(self.developer, self.scripted_verifier([
            None, lambda: ('failed', '', 1)]))
        scheduler.run(self.plan['tasks'])
        scheduler.invalidate(['A'], 'precaution')
        scheduler.developer = self.fail_dev
        archived = scheduler.state['tasks']['A']['failed_attempts'][0]
        for ref in (archived['checks'][0]['log_ref'], archived['diff_ref']):
            path = scheduler.directory / ref
            saved = path.read_bytes()
            before = scheduler.journal.path.read_bytes()
            path.write_text('tamper')
            with self.assertRaisesRegex(TaskError, "Evidence invalid for task 'A'"):
                self.scheduler(self.fail_dev, self.fail_ver).revalidate()
            self.assertEqual(scheduler.journal.path.read_bytes(), before)
            path.write_bytes(saved)
        ref = archived['checks'][0]['log_ref']
        def alter():
            (scheduler.directory / ref).write_text('tamper')
        scheduler.verifier = self.scripted_verifier([alter])
        with self.assertRaises(TaskError):
            scheduler.revalidate()
        self.assertEqual(scheduler.journal.peek()['tasks']['A']['revalidations'][0]['abort']['kind'],
                         'evidence_changed')

    def test_verifier_mutation_stops(self):
        def commit(case, scheduler):
            git(case.wt, 'commit', '--allow-empty', '-m', 'verifier mutation')
        def protected(case, scheduler):
            (case.wt / '.ssdlc').mkdir(exist_ok=True)
            (case.wt / '.ssdlc/changed').write_text('mutation')
        for name, mutate in (
                ('source', lambda c, s: (c.wt / 'a.py').write_text('changed')),
                ('head', commit), ('protected', protected),
                ('ignored', protected)):
            with self.subTest(name=name):
                case = RevalidateTests('test_all_pass_ordered')
                case.setUp()
                try:
                    case.mutation_case(lambda scheduler: mutate(case, scheduler), 'mutation')
                finally:
                    case.doCleanups()

    def test_escaping_symlink_in_unrelated_evidence(self):
        outside = self.repo / 'outside'
        outside.write_text('preserved')
        def mutate(s):
            log = s.directory / s.state['tasks']['D']['checks'][0]['log_ref']
            log.unlink()
            log.symlink_to(outside)
        self.mutation_case(mutate)
        self.assertEqual(outside.read_text(), 'preserved')

    def test_symlink_loop_during_call(self):
        for shape in ('leaf', 'directory'):
            with self.subTest(shape=shape):
                case = RevalidateTests('test_all_pass_ordered')
                case.setUp()
                try:
                    def mutate(s):
                        log = s.directory / s.state['tasks']['D']['checks'][0]['log_ref']
                        if shape == 'leaf':
                            log.unlink()
                            log.symlink_to(log.name)
                        else:
                            directory = log.parent
                            directory.rename(directory.with_name('preserved-attempt'))
                            directory.symlink_to(directory.name)
                    scheduler = case.mutation_case(mutate, actions_before=2)
                    attempt = scheduler.journal.peek()['tasks']['B']['revalidations'][0]
                    case.assertEqual(attempt['status'], 'ABORTED')
                    case.assertIn('B', attempt['abort']['message'])
                    case.assertIn('D', attempt['abort']['message'])
                finally:
                    case.doCleanups()

    def test_symlinked_ancestry_aborts(self):
        def mutate(s):
            directory = s.directory / 'A/revalidate-1'
            directory.rmdir()
            directory.symlink_to(directory.name)
        self.mutation_case(mutate, 'path')

    def test_journal_io_error_not_classified_as_abort(self):
        for event in ('REVALIDATION_CHECK_FINISHED', 'REVALIDATION_ABORTED'):
            for error in (OSError, RuntimeError):
                with self.subTest(event=event, error=error):
                    case = RevalidateTests('test_all_pass_ordered')
                    case.setUp()
                    try:
                        scheduler = case.invalidated_chain()
                        if event == 'REVALIDATION_ABORTED':
                            def collide():
                                (scheduler.directory / 'A/revalidate-1/check-0.log').write_text('collision')
                            scheduler.verifier = case.scripted_verifier([collide])
                        append = scheduler.journal.append
                        def broken(kind, state):
                            if kind == event:
                                raise error('journal unavailable')
                            return append(kind, state)
                        with patch.object(scheduler.journal, 'append', side_effect=broken):
                            with case.assertRaisesRegex(error, 'journal unavailable') as caught:
                                scheduler.revalidate()
                        case.assertIs(type(caught.exception), error)
                        state = scheduler.journal.peek()
                        case.assertEqual(state['tasks']['A']['revalidations'][0]['status'], 'RUNNING')
                        case.recovered(scheduler)
                    finally:
                        case.doCleanups()

    def test_crash_after_last_pass_finalizes_audit(self):
        scheduler = self.invalidated_chain()
        append = scheduler.journal.append
        def broken(event, state):
            if event == 'REVALIDATION_FINISHED':
                raise OSError('before finish')
            return append(event, state)
        with patch.object(scheduler.journal, 'append', side_effect=broken):
            with self.assertRaises(OSError):
                scheduler.revalidate()
        fresh = self.scheduler(self.fail_dev, self.fail_ver)
        self.assertEqual(fresh.revalidate().finalized_run, 1)
        before = fresh.journal.path.read_bytes()
        self.assertIsNone(fresh.revalidate().finalized_run)
        self.assertEqual(fresh.journal.path.read_bytes(), before)

    def test_torn_tail_recovery(self):
        scheduler = self.invalidated_chain()
        with patch.object(scheduler, '_make_attempt_dir', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                scheduler.revalidate()
        with scheduler.journal.path.open('ab') as stream:
            stream.write(b'{torn')
        fresh = self.recovered(scheduler)
        self.assertTrue(list(fresh.directory.glob('interrupted-tail-*.bin')))

    def test_old_loaders_tolerate_events(self):
        scheduler = self.invalidated_chain()
        scheduler.revalidate()
        journal = TaskJournal(scheduler.directory)
        self.assertEqual(journal.peek(), journal.load())

    def test_crash_boundaries_every_append(self):
        # Each case owns a repository: faults cannot influence another boundary.
        def fixture():
            case = RevalidateTests('test_all_pass_ordered')
            case.setUp()
            return case
        case = fixture()
        try:
            scheduler = case.invalidated_chain()
            events = []
            append = scheduler.journal.append
            def count(event, state):
                events.append(event)
                return append(event, state)
            with patch.object(scheduler.journal, 'append', side_effect=count):
                scheduler.revalidate()
        finally:
            case.doCleanups()
        for mode in ('pre', 'snapshot'):
            for boundary in range(1, len(events) + 1):
                with self.subTest(mode=mode, boundary=boundary, event=events[boundary - 1]):
                    case = fixture()
                    try:
                        scheduler = case.invalidated_chain()
                        append = scheduler.journal.append
                        calls = 0
                        def fault(event, state):
                            nonlocal calls
                            calls += 1
                            if calls == boundary:
                                if mode == 'pre':
                                    raise OSError('injected append')
                                with patch('ssdlc_tasks.atomic_json', side_effect=OSError('snapshot')):
                                    return append(event, state)
                            return append(event, state)
                        with patch.object(scheduler.journal, 'append', side_effect=fault):
                            with case.assertRaises(OSError):
                                scheduler.revalidate()
                        case.recovered(scheduler)
                    finally:
                        case.doCleanups()


    def test_crash_boundaries_every_append_exceptional_events(self):
        for event in ('REVALIDATION_INTERRUPTED', 'REVALIDATION_TASK_FAILED',
                      'REVALIDATION_TASK_BLOCKED', 'REVALIDATION_ABORTED'):
            for mode in ('pre', 'snapshot'):
                with self.subTest(event=event, mode=mode):
                    case = RevalidateTests('test_all_pass_ordered')
                    case.setUp()
                    try:
                        scheduler = case.invalidated_chain()
                        if event == 'REVALIDATION_INTERRUPTED':
                            with patch.object(scheduler, '_make_attempt_dir', side_effect=OSError('F1')):
                                with case.assertRaises(OSError):
                                    scheduler.revalidate()
                            scheduler = case.scheduler(case.fail_dev)
                        elif event in ('REVALIDATION_TASK_FAILED', 'REVALIDATION_TASK_BLOCKED'):
                            scheduler.verifier = case.scripted_verifier([
                                lambda: ('failed', '', 1)])
                        else:
                            scheduler.verifier = case.scripted_verifier([
                                lambda: (scheduler.directory / 'A/revalidate-1/check-0.log').write_text('collision') and None])
                        append = scheduler.journal.append
                        def fault(kind, state):
                            if kind == event:
                                if mode == 'pre':
                                    raise OSError('injected')
                                with patch('ssdlc_tasks.atomic_json', side_effect=OSError('snapshot')):
                                    return append(kind, state)
                            return append(kind, state)
                        with patch.object(scheduler.journal, 'append', side_effect=fault):
                            with case.assertRaises(OSError):
                                scheduler.revalidate()
                        case.recovered(scheduler)
                    finally:
                        case.doCleanups()

    def test_crash_boundaries_every_append_filesystem_classes(self):
        for boundary in ('F1', 'F2', 'F3', 'F4', 'F7', 'F8'):
            for check_index in (0, 1):
                with self.subTest(boundary=boundary, check_index=check_index):
                    case = RevalidateTests('test_all_pass_ordered')
                    case.setUp()
                    try:
                        scheduler = case.invalidated_chain()
                        append = scheduler.journal.append
                        make = scheduler._make_attempt_dir
                        def fault(event, state):
                            cursor = state['revalidation_runs'][-1]['cursor']
                            if cursor['task_id'] == 'B':
                                target = scheduler._expected_check_ids('B')[check_index]
                                if (cursor['check_id'] == target and
                                    ((boundary == 'F2' and event == 'REVALIDATION_CHECK_STARTED') or
                                     (boundary == 'F4' and event == 'REVALIDATION_CHECK_FINISHED'))):
                                    raise OSError(boundary)
                            return append(event, state)
                        def mkdir(path):
                            if boundary == 'F1' and path.parent.name == 'B':
                                raise OSError('F1')
                            return make(path)
                        calls = 0
                        def verify(*args):
                            nonlocal calls
                            calls += 1
                            if calls == 3 + check_index:
                                if boundary == 'F3':
                                    raise OSError('F3')
                                if boundary == 'F7':
                                    scheduler.guard = lambda: (_ for _ in ()).throw(OSError('F7'))
                                if boundary == 'F8':
                                    # An unreferenced collision is an ancestry/write security stop;
                                    # retained evidence alteration exercises the inventory gate.
                                    log.write_text('altered')
                            return 'Ran 1 test\nOK', '', 0
                        log = scheduler.directory / scheduler.state['tasks']['D']['checks'][0]['log_ref']
                        saved = log.read_bytes()
                        scheduler.verifier = verify
                        with patch.object(scheduler.journal, 'append', side_effect=fault), \
                             patch.object(scheduler, '_make_attempt_dir', side_effect=mkdir):
                            with case.assertRaises(TaskError if boundary == 'F8' else OSError):
                                scheduler.revalidate()
                        if boundary == 'F8':
                            case.assertEqual(scheduler.journal.peek()['tasks']['B']['revalidations'][0]['abort']['kind'],
                                             'evidence_changed')
                            log.write_bytes(saved)  # operator reconciliation after the recorded stop
                        case.recovered(scheduler)
                    finally:
                        case.doCleanups()


if __name__ == '__main__':
    unittest.main()
