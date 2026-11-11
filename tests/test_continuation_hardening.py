"""Issue #27 regressions from the main/c78f65f review. No live providers."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from test_continuations import ContinuationTests
from ssdlc_continuations import inspect_source, validate_runtime_projection
from ssdlc_preparations import discard, unfinished
from ssdlc_tasks import TaskError, TaskJournal, atomic_json, digest, git
from ssdlc_transitions import TransitionError, runtime_root


class ContinuationHardeningTests(ContinuationTests):
    # Inherit the prior positive controls too: the new refusal must preserve them.
    def test_failed_in_place_baseline_does_not_fence_original_and_discard_is_native(self):
        self.target = self.artifacts
        before = inspect_source(self.artifacts)
        with self.assertRaisesRegex(TaskError, 'baseline failed'):
            self.prepare(verifier=lambda *args: ('Ran 1 test\nFAILED', '', 1))
        self.assertEqual(runtime_root(self.artifacts), self.artifacts)
        ids = unfinished(self.target); self.assertEqual(len(ids), 1)
        with self.assertRaisesRegex(TaskError, 'native discard'):
            self.prepare()
        result = discard(self.target, ids[0], writers_quiescent=lambda record: True)
        self.assertEqual(result['status'], 'ABORTED')
        self.assertEqual(discard(self.target, ids[0], writers_quiescent=lambda r: False), result)
        self.assertFalse((self.target / 'runtime-generations' / ids[0]).exists())
        self.assertEqual(inspect_source(self.artifacts), before)
        prepared = self.prepare(); self.assertTrue(prepared['proposal'])

    def test_preparation_faults_are_tracked_and_original_remains_recoverable(self):
        self.target = self.artifacts
        for boundary in ('worktree_created', 'prepared'):
            with self.subTest(boundary=boundary):
                with self.assertRaises(OSError):
                    self.prepare(fault=lambda p: (_ for _ in ()).throw(OSError('crash')) if p == boundary else None)
                self.assertEqual(runtime_root(self.target), self.target)
                ids = unfinished(self.target); self.assertEqual(len(ids), 1)
                discard(self.target, ids[0], writers_quiescent=lambda r: True)
        self.assertEqual(unfinished(self.target), [])

    def test_discard_refuses_active_generation_and_live_writers(self):
        prepared = self.prepare(); identity = prepared['proposal']['transition_id']
        with self.assertRaisesRegex(TaskError, 'quiescent'):
            discard(self.target, identity, writers_quiescent=lambda r: False)
        self.activate(prepared)
        with self.assertRaisesRegex(TaskError, 'active generation'):
            discard(self.target, identity, writers_quiescent=lambda r: True)

    def test_scope_addition_requires_declaration_and_exact_scope_authorization(self):
        self.new_plan['tasks'][0]['target_files'].append('extra.py')
        from ssdlc_continuations import amendment_subject, impact_report
        subject = amendment_subject(inspect_source(self.artifacts), self.new_plan, self.quality, self.context,
                                    self.manifest, impact_report(self.repo, self.snapshot, self.manifest))
        with self.assertRaisesRegex(TaskError, 'scope_diff declaration'):
            self.prepare(review={'subject_sha256': digest(subject), 'reviewer': 'reviewer',
                                 'report': {'verdict': 'APPROVED', 'findings': []}})

    def test_declared_scope_is_registered_but_missing_explicit_authorization_refuses(self):
        self.new_plan['tasks'][0]['target_files'].append('extra.py')
        prepared = self.prepare()
        self.assertEqual(prepared['proposal']['scope_diff']['T1']['added'], ['extra.py'])
        from ssdlc_continuations import activate_generation
        auth = self.authorize(prepared); auth.pop('scope_diff_sha256')
        with self.assertRaisesRegex(TaskError, 'explicit authorization'):
            activate_generation(self.target, prepared['preparation'], auth)
        self.activate(prepared)

    def test_actual_runtime_counter_reset_is_rejected_even_with_facts_unchanged(self):
        prepared = self.prepare()
        root = self.target / 'runtime-generations' / prepared['proposal']['transition_id']
        journal = TaskJournal(root / '03-dev/tasks'); state = journal.load()
        state['tasks']['T1']['attempts'] = 0
        journal.append('CORRUPT_TEST_PROJECTION', state)
        with self.assertRaisesRegex(TaskError, 'charged task attempts'):
            validate_runtime_projection(inspect_source(self.artifacts), root)

    def test_sigkill_preparation_leaves_tracked_resources_and_native_discard_recovers(self):
        import multiprocessing
        import os
        import signal
        self.target = self.artifacts
        for boundary in ('worktree_created', 'prepared'):
            with self.subTest(boundary=boundary):
                def child():
                    self.prepare(fault=lambda point: os.kill(os.getpid(), signal.SIGKILL) if point == boundary else None)
                process = multiprocessing.get_context('fork').Process(target=child)
                process.start(); process.join(20)
                if process.is_alive():
                    process.kill(); process.join(); self.fail('Preparation did not reach crash boundary')
                self.assertEqual(process.exitcode, -signal.SIGKILL)
                ids = unfinished(self.target)
                self.assertEqual(len(ids), 1)
                with self.assertRaisesRegex(TaskError, 'native discard'): self.prepare()
                discard(self.target, ids[0], writers_quiescent=lambda r: True)
                self.assertEqual(runtime_root(self.target), self.artifacts)


def load_tests(loader, tests, pattern):
    # Reuse the fixture, not duplicate the original module's positive controls.
    return unittest.TestSuite(ContinuationHardeningTests(name) for name in sorted(ContinuationHardeningTests.__dict__)
                              if name.startswith('test_'))
