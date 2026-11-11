"""Issue #29 boundaries, real Git/journals, no provider calls."""
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from replay.test_candidate_capture import CandidateCaptureTests
from test_recovery_limits import LimitsTests, policy
from ssdlc_candidates import CandidateError, CandidateStore
from ssdlc_recovery import LimitsStore, QuiescentTimeout
from ssdlc_tasks import TaskError, TaskJournal, atomic_json, file_hash, git


class CaptureIntegrityTests(CandidateCaptureTests):
    def assert_preserved(self, mutate, issue):
        def developer(*args):
            result = self.develop(*args)
            mutate(args[-1])
            return result
        with self.assertRaisesRegex(TaskError, 'INTEGRITY'):
            self.make(developer).run(self.plan['tasks'])
        receipt = json.loads(self.receipt().read_text())
        self.assertIn(issue, receipt['issues'])
        self.assertEqual((self.receipt().parent / 'developer.log').read_text(), self.fixture['developer_output'])
        self.assertTrue((self.receipt().parent / 'blobs' / receipt['entries']['app.py']['after']['sha256']).exists())
        with self.assertRaisesRegex(TaskError, 'INTEGRITY'):
            self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.wt / 'app.py').read_text(), 'value = 0\n')

    def test_missing_git_has_receipt_and_preserved_source(self):
        self.assert_preserved(lambda ws: shutil.rmtree(ws / '.git'), 'git_metadata_unavailable')

    def test_replaced_git_is_not_followed_or_executed(self):
        def replace(ws):
            shutil.rmtree(ws / '.git'); (ws / '.git').symlink_to(self.repo / '.git')
        self.assert_preserved(replace, 'git_metadata_unavailable')

    def test_corrupted_index_preserves_payload_and_output(self):
        self.assert_preserved(lambda ws: (ws / '.git/index').write_bytes(b'corrupt index'), 'git_metadata_unavailable')

    def test_nested_git_is_preserved_by_digest_and_refused(self):
        def nested(ws):
            (ws / 'fixture').mkdir(); git(ws / 'fixture', 'init')
        self.assert_preserved(nested, 'unsafe_entry')

    def test_backslash_filename_has_a_rejection_receipt(self):
        self.assert_preserved(lambda ws: (ws / 'bad\\name.py').write_text('not imported'), 'unsafe_entry')

    def test_control_character_filename_has_a_rejection_receipt(self):
        self.assert_preserved(lambda ws: (ws / 'bad\nname.py').write_text('not imported'), 'unsafe_entry')

    def test_protected_path_alone_blocks(self):
        def protected(ws):
            (ws / '.agents').mkdir(); (ws / '.agents/rules.md').write_text('changed')
        self.assert_preserved(protected, 'protected_path')

    def test_git_metadata_alone_blocks_without_source_protected_change(self):
        self.assert_preserved(lambda ws: (ws / '.git/config').write_text('[core]\nfilemode = false\n'), 'protected_metadata')

    def test_active_writer_during_capture_never_produces_adoptable_source(self):
        from ssdlc_candidates import _capture_read
        changed, finish = threading.Event(), threading.Event()
        threads = []
        def read(ws, name):
            if name == 'app.py' and not threads:
                def writer():
                    (ws / name).write_text('value = 99\n'); changed.set(); finish.wait(5)
                thread = threading.Thread(target=writer); threads.append(thread); thread.start()
                self.assertTrue(changed.wait(3)); self.assertTrue(thread.is_alive())
            return _capture_read(ws, name)
        try:
            with patch('ssdlc_candidates._capture_read', side_effect=read):
                self.assert_preserved(lambda ws: None, 'source_changed')
        finally:
            finish.set()
            for thread in threads: thread.join(3)

    def test_failure_to_publish_receipt_keeps_journal_bound_integrity_refusal(self):
        with patch.object(CandidateStore, 'capture', side_effect=CandidateError('Cannot form receipt')):
            with self.assertRaisesRegex(TaskError, 'output and rejection preserved'):
                self.make().run(self.plan['tasks'])
        row = self.make().journal.peek()['tasks']['T1']
        self.assertIn('capture_rejection', row)
        refusal = json.loads((self.artifacts / '03-dev/tasks' / row['capture_rejection']['ref']).read_text())
        self.assertEqual(refusal['category'], 'INTEGRITY')
        self.assertEqual((self.artifacts / '03-dev/tasks' / refusal['output_ref']).read_text(), self.fixture['developer_output'])
        with self.assertRaisesRegex(TaskError, 'Prior capture was rejected'):
            self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)

    def test_role_timeout_adapter_fences_before_capture_without_redispatch(self):
        from orchestrator import EngineRunner
        def subprocess_run(argv, **kwargs):
            if argv[:2] == ['docker', 'run']:
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'], output='partial', stderr='timeout')
            self.assertIn(argv[1], ('rm', 'ps'))
            return subprocess.CompletedProcess(argv, 0, '', '')
        # Patch the adapter process calls only inside the provider callback so
        # the scheduler continues to use real Git subprocesses.
        def actual(task, attempt, prompt, ws):
            self.develop(task, attempt, prompt, ws)
            with patch('orchestrator.subprocess.run', side_effect=subprocess_run), patch.object(EngineRunner, '_find_repo_root', return_value=self.repo):
                EngineRunner._run_role_command(['docker', 'run', '--name', 'owned-test-role', 'image'], cwd=ws, env=None, timeout=30)
        state = self.scheduler(actual, self.verify, isolate_candidates=True, recover_timeouts=True).run(self.plan['tasks'])
        self.assertEqual(state['tasks']['T1']['completion_confirmation'], 'unconfirmed_completion')
        self.assertTrue(state['tasks']['T1']['producer']['fence']['absence_confirmed'])
        self.make(self.fail_developer, recover_timeouts=True).run(self.plan['tasks'])
        self.assertEqual(len(self.calls), 1)

    def test_resumed_developer_log_is_not_truncated(self):
        state = self.make().run(self.plan['tasks'])
        path = self.artifacts / '03-dev/tasks/T1/attempt-1/developer.log'
        original = path.read_bytes()
        self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(state['tasks']['T1']['status'], 'COMPLETED')

    def test_resume_after_log_publication_reuses_bytes_without_an_overwrite(self):
        import os
        path = self.artifacts / '03-dev/tasks/T1/attempt-1/developer.log'
        def guard():
            if path.exists(): raise TaskError('Interrupt after log publication')
        with self.assertRaisesRegex(TaskError, 'Interrupt after log'):
            self.make(guard=guard).run(self.plan['tasks'])
        original = path.read_bytes(); replace = os.replace
        def checked_replace(source, destination):
            if Path(destination) == path: self.fail('Log must be reused')
            return replace(source, destination)
        with patch('ssdlc_tasks.os.replace', side_effect=checked_replace):
            self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertEqual(path.read_bytes(), original)

    def test_complete_rejection_after_crash_before_journal_append_is_reconciled(self):
        scheduler = self.make(); save = scheduler._save
        def crash(event):
            if event == 'CAPTURE_INTEGRITY_REJECTED': raise OSError('crash before journal append')
            save(event)
        scheduler._save = crash
        with patch.object(CandidateStore, 'capture', side_effect=CandidateError('Cannot form receipt')):
            with self.assertRaises(OSError): scheduler.run(self.plan['tasks'])
        self.assertNotIn('capture_rejection', scheduler.journal.peek()['tasks']['T1'])
        with self.assertRaisesRegex(TaskError, 'Prior capture was rejected'):
            self.make(self.fail_developer).run(self.plan['tasks'])
        self.assertIn('capture_rejection', scheduler.journal.peek()['tasks']['T1'])
        self.assertEqual(len(self.calls), 1)


class PersistentModeTests(LimitsTests):
    def attach_state(self):
        atomic_json(self.root / 'state.json', {'ticket_id': 'T1', 'ledger': []})
        self.store.authorize(policy(), self.binding)
        return json.loads((self.root / 'state.json').read_text())

    def test_every_phase_refuses_deleted_limits_even_without_autonomous_env(self):
        from orchestrator import _bounded_entrypoint
        state = self.attach_state()
        self.assertEqual(state['bounded_execution']['kind'], 'bounded-i2')
        shutil.rmtree(self.root / 'limits')
        owner = SimpleNamespace(ssdlc_root=self.root, ticket_id='T1')
        for name in ('run_phase_recon', 'run_phase_po', 'run_phase_pair_design', 'resume_task_planning',
                     'run_phase_dev', 'run_phase_review', 'run_phase_qa'):
            with self.subTest(phase=name):
                def body(owner): self.fail('Must refuse before entrypoint')
                body.__name__ = name
                with patch.dict('os.environ', {'SSDLC_AUTONOMOUS': 'false'}), self.assertRaises(TaskError):
                    _bounded_entrypoint(body)(owner)
        with self.assertRaisesRegex(TaskError, 'cannot reset consumption'):
            self.store.authorize(policy(), self.binding)

    def test_mode_marker_or_state_binding_tampering_refuses(self):
        self.attach_state()
        original_state = (self.root / 'state.json').read_bytes()
        atomic_json(self.root / 'state.json', [])
        with self.assertRaisesRegex(TaskError, 'mode is missing or malformed'): self.store._load()
        (self.root / 'state.json').write_bytes(original_state)
        original = (self.root / 'execution-mode.json').read_bytes()
        (self.root / 'execution-mode.json').unlink()
        with self.assertRaisesRegex(TaskError, 'mode changed'): self.store._load()
        from orchestrator import SSDLCOrchestrator
        owner = SimpleNamespace(ssdlc_root=self.root, state_file=self.root / 'state.json', state={'ticket_id': 'T1'})
        with self.assertRaisesRegex(TaskError, 'refusing state overwrite'): SSDLCOrchestrator._save_state(owner)
        self.assertIn('bounded_execution', json.loads(owner.state_file.read_text()))
        (self.root / 'execution-mode.json').write_bytes(original)
        state = json.loads((self.root / 'state.json').read_text()); state.pop('bounded_execution')
        atomic_json(self.root / 'state.json', state)
        with self.assertRaisesRegex(TaskError, 'mode changed'): self.store._load()

    def test_native_state_save_preserves_mode_even_from_a_stale_projection(self):
        from orchestrator import SSDLCOrchestrator
        self.attach_state()
        owner = SimpleNamespace(ssdlc_root=self.root, state_file=self.root / 'state.json', state={'ticket_id': 'T1'})
        SSDLCOrchestrator._save_state(owner)
        self.assertEqual(json.loads(owner.state_file.read_text())['bounded_execution']['kind'], 'bounded-i2')

    def test_crash_during_authorization_does_not_reset_counters_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LimitsStore(Path(tmp), self.clock)
            with patch.object(store, '_publish_mode', side_effect=OSError('crash')):
                with self.assertRaises(OSError): store.authorize(policy(), self.binding)
            self.assertEqual(json.loads((Path(tmp) / 'execution-mode.json').read_text())['status'], 'INTENT')
            store.authorize(policy(), self.binding)
            self.assertEqual(store._load()[1]['started_at'], self.now)
            identity = store.reserve('03-dev', '03-dev', 'codex'); store.finish(identity, 'returned')
            store.authorize(policy(), self.binding)
            self.assertEqual(len(store._load()[1]['invocations']), 1)

    def test_legacy_transport_uses_three_attempts_and_auth_does_not_retry(self):
        import urllib.error
        from orchestrator import EngineRunner, _dispatch_local
        previous = getattr(_dispatch_local, 'limits', None); _dispatch_local.limits = None
        try:
            for error, count in [(urllib.error.URLError('lost'), 3),
                                 (urllib.error.HTTPError('https://example.invalid', 401, 'auth', {}, None), 1),
                                 (urllib.error.HTTPError('https://example.invalid', 503, 'busy', {}, None), 3)]:
                with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'offline-sentinel', 'SSDLC_SUPERVISED_RUN_ID': ''}), \
                     patch('orchestrator.urllib.request.urlopen', side_effect=error) as request, patch('orchestrator.time.sleep') as delay:
                    with self.assertRaises(type(error)): EngineRunner.call_openrouter('model', 's', 'u', 'p', 't', '03-dev')
                self.assertEqual(request.call_count, count); self.assertEqual(delay.call_count, count - 1)
        finally: _dispatch_local.limits = previous

    def test_missing_role_executable_is_proven_not_dispatched(self):
        from orchestrator import EngineRunner, _dispatch_local, _dispatch_context, _supervised_dispatch
        from ssdlc_recovery import NotDispatched
        @_supervised_dispatch
        def run_codex(prompt, cwd):
            return EngineRunner._run_role_command(['not-installed'], cwd=cwd, env=None, timeout=30)
        previous = getattr(_dispatch_local, 'limits', None); _dispatch_local.limits = self.store
        try:
            with _dispatch_context(phase='03-dev'), patch.object(EngineRunner, '_find_repo_root', return_value=self.root), \
                 patch('orchestrator.subprocess.run', side_effect=FileNotFoundError('not-installed')):
                with self.assertRaises(NotDispatched): run_codex('offline', self.root)
        finally: _dispatch_local.limits = previous
        self.assertEqual(next(iter(self.store._load()[1]['invocations'].values()))['status'], 'not_dispatched')

    def test_supervisor_intent_refusal_precedes_engine_and_is_not_uncertain(self):
        from orchestrator import _dispatch_local, _dispatch_context, _supervised_dispatch
        from ssdlc_recovery import NotDispatched
        @_supervised_dispatch
        def run_codex(prompt, cwd): self.fail('No engine may start')
        previous = getattr(_dispatch_local, 'limits', None); _dispatch_local.limits = self.store
        try:
            with _dispatch_context(phase='03-dev'), patch.dict('os.environ', {'SSDLC_SUPERVISED_RUN_ID': 'offline'}), \
                 patch('orchestrator._supervision_event', return_value=False):
                with self.assertRaisesRegex(NotDispatched, 'engine not launched'): run_codex('offline', self.root)
        finally: _dispatch_local.limits = previous
        self.assertEqual(next(iter(self.store._load()[1]['invocations'].values()))['status'], 'not_dispatched')


class GraderTargetTests(CandidateCaptureTests):
    def setUp(self):
        super().setUp()
        # Reuse the existing worktree but establish a self-development baseline.
        for name in ('orchestrator.py', 'ssdlc_tasks.py', 'tests/replay/benchmark.py'):
            path = self.repo / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('# host grader fixture\n')
        git(self.repo, 'add', 'orchestrator.py', 'ssdlc_tasks.py', 'tests')
        git(self.repo, 'commit', '-m', 'Host grader identity')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.plan['base_commit'] = self.quality['base_commit'] = self.base
        git(self.wt, 'reset', '--hard', self.base)

    def test_manifest_cannot_authorize_a_grader_or_baseline_edit(self):
        from ssdlc_grader import REQUIRED
        for name in REQUIRED:
            with self.subTest(path=name):
                self.plan['tasks'][0]['target_files'] = [name]
                with self.assertRaisesRegex(TaskError, 'explicit reviewed revision'):
                    self.validator().validate(self.plan)
        self.plan['tasks'][0]['target_files'] = ['tests/replay/new-grader.py']
        with self.assertRaisesRegex(TaskError, 'explicit reviewed revision'): self.validator().validate(self.plan)
        self.plan['tasks'][0]['target_files'] = ['app.py']
        self.assertEqual(self.validator().validate(self.plan)[0]['task_id'], 'T1')

    def test_capture_refuses_grader_edit_even_if_manifest_validation_is_bypassed(self):
        self.plan['tasks'][0]['target_files'].append('tests/replay/benchmark.py')
        def developer(*args):
            result = self.develop(*args)
            (args[-1] / 'tests/replay/benchmark.py').write_text('# weakened oracle\n')
            return result
        with self.assertRaisesRegex(TaskError, 'protected_path'):
            self.make(developer).run(self.plan['tasks'])
        receipt = json.loads(self.receipt().read_text())
        self.assertIn('protected_path', receipt['issues'])


class GraderProtectionTests(unittest.TestCase):
    def test_each_grader_dependency_tamper_is_detected(self):
        from ssdlc_grader import verify_baseline, REQUIRED
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in REQUIRED:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, path)
            self.assertEqual(verify_baseline(root), 'i2-hardening-v2')
            for name in REQUIRED - {'schemas/recovery-grader-baseline.json'}:
                with self.subTest(path=name):
                    path = root / name; original = path.read_bytes()
                    path.write_bytes(original + b'\n# changed baseline\n')
                    with self.assertRaisesRegex(RuntimeError, 'without reviewed baseline'): verify_baseline(root)
                    path.write_bytes(original)

    def test_manifest_cannot_remove_grader_members(self):
        from ssdlc_grader import verify_baseline, MANIFEST
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / MANIFEST; path.parent.mkdir(parents=True)
            document = json.loads((source / MANIFEST).read_text()); document['files'].pop('tests/replay/benchmark.py')
            atomic_json(path, document)
            with self.assertRaisesRegex(RuntimeError, 'membership changed'): verify_baseline(root)


class DeliveryDisclosureTests(unittest.TestCase):
    def test_evidence_pack_and_signoff_show_unconfirmed_producer_completion(self):
        from orchestrator import SSDLCOrchestrator
        from test_task_bootstrap import OrchestratorBootstrapTests
        # Reuse the repository/controller fixture, not its test methods.
        case = OrchestratorBootstrapTests('test_approve_keeps_evidence_and_does_not_commit_residual_changes')
        case.setUp()
        try:
            with patch.dict('os.environ', {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
                owner = case.make_orchestrator()
                wt = owner._ensure_worktree()
                (wt / 'app.py').write_text('value = 1\n'); git(wt, 'add', 'app.py'); git(wt, 'commit', '-m', 'checked source')
                head = git(wt, 'rev-parse', 'HEAD')
                owner.state.update(task_verified_head=head, goal='offline',
                    tasks_status={'T1': {'attempts': 1, 'status': 'COMPLETED', 'completion_confirmation': 'unconfirmed_completion'}})
                TaskJournal(owner.dir_dev / 'tasks').append('OFFLINE_COMPLETION', {'tasks': owner.state['tasks_status']})
                owner.state['tasks_status'] = {}  # stale state must not hide the journal disclosure
                owner._save_state()
                path = owner.compile_evidence_pack(True)
                self.assertIn('unconfirmed_completion', path.read_text())
                self.assertIn('independent checks, not producer confirmation', path.read_text())
                with patch('sys.stdout', new_callable=io.StringIO) as output:
                    owner.approve_and_merge()
                self.assertIn('unconfirmed_completion', output.getvalue())
        finally: case.doCleanups()

    def test_outer_cycle_uses_frozen_policy_not_environment(self):
        from orchestrator import SSDLCOrchestrator, _dispatch_local
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner = SSDLCOrchestrator.__new__(SSDLCOrchestrator)
            owner.ssdlc_root = root; owner.dir_dev = root / '03-dev'; owner.state = {'risk_tier': 'S'}
            envelope = {'policy': policy(development_attempts=2)}
            store = SimpleNamespace(_load=lambda **kw: (envelope, {}))
            previous = getattr(_dispatch_local, 'limits', None); _dispatch_local.limits = store
            try:
                with patch.dict('os.environ', {'SSDLC_MAX_DEV_RETRIES': '9', 'SSDLC_SUPERVISED_RUN_ID': ''}), \
                     patch.object(owner, '_task_mode', return_value=False), \
                     patch.object(owner, 'run_phase_dev') as developer, \
                     patch.object(owner, 'run_phase_review', return_value=(False, 'rejected')), \
                     patch.object(owner, '_stop_on_terminal_gate'), patch.object(owner, 'check_budget_circuit_breaker'), \
                     patch.object(owner, 'compile_evidence_pack', return_value=root / 'evidence.md'):
                    owner._run_pipeline('offline', from_phase='03-dev')
                self.assertEqual(developer.call_count, 2)
            finally: _dispatch_local.limits = previous


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (CaptureIntegrityTests, PersistentModeTests, GraderTargetTests, GraderProtectionTests, DeliveryDisclosureTests)
                              for name in cls.__dict__ if name.startswith('test_'))
