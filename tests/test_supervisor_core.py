"""Core lifecycle/security contract. All writes use temporary repositories."""
import contextlib
from dataclasses import asdict, replace
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import ssdlc_supervisor as sup
from helpers.supervisor_fakes import FakeClock, FakeProc, FakeDocker, CrashAt

ROOT = Path(__file__).resolve().parents[1]


class CoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.home = self.repo / 'home'
        self.home.mkdir()
        # Lifecycle tests launch a fake controller, but still exercise the real
        # read-only preflight in its detached worker. Supply isolated prerequisites
        # instead of depending on host subscriptions/tools or bypassing preflight.
        auth = self.home / '.codex'
        auth.mkdir()
        (auth / 'auth.json').write_text('{}')
        tools = self.home / 'bin'
        tools.mkdir()
        fake_codex = tools / 'codex'
        fake_codex.write_text('#!/bin/sh\nexit 99\n')
        fake_codex.chmod(0o700)  # Any accidental inference invocation must fail.
        env = {'HOME': str(self.home), 'PATH': str(tools) + os.pathsep + os.environ.get('PATH', os.defpath),
               'SSDLC_EXECUTION_MODE': 'native', 'SSDLC_REVIEW_MODELS': 'codex:fixture',
               'SSDLC_INHERIT_HOST_CODEX_AUTH': 'true'}
        for roles in sup.ROLE_KEYS_BY_ENTRYPOINT.values():
            for role in roles:
                env['SSDLC_' + role.split('-')[-1].upper() + '_ENGINE'] = 'codex'
        self.env_patch = patch.dict(os.environ, env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, stdout=subprocess.DEVNULL)
        docker_patch = patch.object(sup, 'DockerCancellation', return_value=FakeDocker())
        docker_patch.start()
        self.addCleanup(docker_patch.stop)
        self.clock = FakeClock()
        self.args = dict(repo=str(self.repo), ticket='T-1', goal='inert goal', tier='L')

    def store(self, fault=None):
        a = sup.validate_args(self.args)
        store = sup.RunStore(self.repo, 'T-1', 'c' * 32, self.clock, fault)
        store.prepare()
        store.create(sup.new_record(a, store))
        return store

    def fake_run(self, controller=True, fault=None):
        store = self.store(fault)
        proc = FakeProc()
        def setup(d):
            for role, pid, ticks in [('worker', 1001, 100), ('controller', 2002, 200)]:
                if role == 'controller' and not controller:
                    continue
                intent = ('a' if role == 'worker' else 'b') * 32
                ident = proc.add(pid, nonce=d['nonce'], role=role, intent=intent,
                                 run_id=d['run_id'], ticks=ticks)
                d['processes'][role] = asdict(ident)
                d['launches'].append(dict(intent_id=intent, role=role, generation=1, state='published', at=1000., abandoned_at=None))
            d.update(status='running', status_reason='launched')
            d['handoff'] = dict(outcome='launched', at=1000.)
        store.update(setup)
        return store, proc

    def controller(self, body='import time\ntime.sleep(30)\n'):
        p = self.repo / 'controller.py'
        p.write_text(body)
        self.args['controller'] = str(p)
        return p

    def cleanup_run(self):
        try:
            sup.cancel(self.repo, 'T-1', grace=0)
        except (FileNotFoundError, sup.SupervisorIntegrityError):
            pass

    def wait_for(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        self.fail('helper deadline exceeded')


class TestIdentityAndMetadata(CoreCase):
    def test_atomic_schema_and_corruption_never_overwritten(self):
        store = self.store()
        path = sup._safe_path(store.root, store.path('run.json'))
        valid = path.read_bytes()
        for value, reason in [(b'{', 'metadata_corrupt'), (b'{}', 'metadata_version'),
                              (valid.replace(b'"schema_version":1', b'"schema_version":0'), 'metadata_version'),
                              (valid.replace(b'"schema_version":1', b'"schema_version":2'), 'metadata_version'),
                              (valid.replace(b'"schema_version":1', b'"schema_version":true'), 'metadata_version'),
                              (valid.replace(b'"revision":0', b'"revision":"0"'), 'metadata_corrupt')]:
            path.write_bytes(value)
            with self.subTest(reason=reason, value=value[:30]):
                with self.assertRaises(sup.SupervisorIntegrityError) as e:
                    store.update(lambda d: d.update(status='running'))
                self.assertEqual(e.exception.code, reason)
                self.assertEqual(path.read_bytes(), value)
        path.write_bytes(valid)
        self.assertEqual(store.read()['schema_version'], 1)

    def test_current_corruption_cannot_create_replacement(self):
        store = self.store()
        p = store.root / 'T-1/current.json'
        p.write_text('{')
        with self.assertRaises(sup.SupervisorIntegrityError):
            sup.start(self.args)
        self.assertEqual(len(list((store.root / 'T-1/runs').iterdir())), 1)

    def test_identity_all_fields_and_markers(self):
        proc = FakeProc()
        ident = proc.add(10)
        self.assertEqual(sup.verify(ident, 'a'*32, 'worker', 1, proc, 'b'*32, 'c'*32), sup.OWNED)
        for field, value in [('boot_id', 'other'), ('start_ticks', 99), ('pgid', 55), ('sid', 55), ('uid', 999)]:
            self.assertEqual(sup.verify(replace(ident, **{field: value}), 'a'*32, 'worker', 1, proc), sup.NOT_OWNED)
        self.assertEqual(sup.verify(ident, 'wrong', 'worker', 1, proc), sup.NOT_OWNED)
        self.assertEqual(sup.verify(ident, 'a'*32, 'controller', 1, proc), sup.NOT_OWNED)
        self.assertEqual(sup.verify(ident, 'a'*32, 'worker', 2, proc), sup.NOT_OWNED)
        proc.entries[10][3] = PermissionError()
        self.assertEqual(sup.verify(ident, 'a'*32, 'worker', 1, proc), sup.UNKNOWN)
        proc.entries[10][1] = 'Z'
        self.assertEqual(sup.verify(ident, 'a'*32, 'worker', 1, proc), sup.GONE)
        del proc.entries[10]
        self.assertEqual(sup.verify(ident, 'a'*32, 'worker', 1, proc), sup.GONE)

    def test_proc_stat_uses_last_parenthesis(self):
        real_read = Path.read_text
        pid = os.getpid()
        raw = (Path('/proc') / str(pid) / 'stat').read_text()
        original = sup.Proc().stat(pid)[0]
        rewritten = raw[:raw.index('(')] + '(strange ) process ( name)' + raw[raw.rfind(')') + 1:]
        def read(p, *a, **kw):
            return rewritten if str(p) == f'/proc/{pid}/stat' else real_read(p, *a, **kw)
        with patch.object(Path, 'read_text', read):
            self.assertEqual(sup.Proc().stat(pid)[0], original)

    def test_live_worker_dead_controller_and_reverse_are_read_only_reconcile(self):
        store, proc = self.fake_run()
        before = (store.root / store.path('run.json')).read_bytes()
        for gone, alive in [(1001, 2002), (2002, 1001)]:
            with self.subTest(gone=gone):
                saved = proc.entries.pop(gone)
                result = sup.status(self.repo, 'T-1', proc=proc)
                self.assertEqual(result['status'], 'reconcile')
                self.assertIn(alive, proc.entries)
                proc.entries[gone] = saved
        self.assertEqual((store.root / store.path('run.json')).read_bytes(), before)
        self.assertFalse(proc.signals)

    def test_modes_wrong_uid_symlinks_and_escapes(self):
        store = self.store()
        for p in store.root.rglob('*'):
            self.assertEqual(p.stat().st_mode & 0o777, 0o700 if p.is_dir() else 0o600)
        with self.assertRaises(sup.SupervisorIntegrityError):
            sup._safe_path(store.root, '../escape')
        p = store.root / 'T-1/link'
        p.symlink_to(self.repo)
        with self.assertRaises(sup.SupervisorIntegrityError):
            sup._safe_path(store.root, 'T-1/link/child')
        p.unlink()
        p = store.root / store.path('run.json')
        p.chmod(0o644)
        with self.assertRaises(sup.SupervisorIntegrityError):
            store.read()
        p.chmod(0o600)
        real_lstat = Path.lstat
        def wrong_uid(path):
            st = real_lstat(path)
            if path == p:
                fields = list(st)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return st
        with patch.object(Path, 'lstat', wrong_uid):
            with self.assertRaises(sup.SupervisorIntegrityError):
                store.read()

    def test_non_linux_and_exit_codes(self):
        with patch.object(sys, 'platform', 'darwin'):
            result = sup.start(self.args)
            self.assertEqual(result['status_reason'], 'unsupported_platform')
            self.assertEqual(sup.exit_code(result), 14)
            self.assertFalse((self.repo / '.ssdlc').exists())
        for state, code in [('running', 0), ('operator_wait', 10), ('blocked', 11), ('failed', 12), ('locked', 13), ('unsupported', 14)]:
            self.assertEqual(sup.exit_code(sup.public({}, state, state)), code)


class TestVerticalSliceAndArgv(CoreCase):
    def test_exact_fixed_argv_and_operator_authority(self):
        goal = '; touch ' + str(self.repo / 'injected')
        a = dict(self.args, goal=goal, test_cmd='printf explicit-operator-command')
        argv = sup.build_argv('orchestrator.run', a)
        self.assertIsInstance(argv, list)
        self.assertEqual(argv, [sys.executable, str(ROOT / 'orchestrator.py'), 'run', '--ticket', 'T-1',
            '--goal', goal, '--tier', 'L', '--repo', str(self.repo), '--from-phase', '00-recon',
            '--test-cmd', a['test_cmd'], '--no-sync-remote'])
        self.assertNotIn('--sync-remote', argv)
        self.assertNotIn('--issue', argv)
        self.assertEqual(sup.build_argv('orchestrator.plan', a),
                         [sys.executable, str(ROOT / 'orchestrator.py'), 'plan', '--ticket', 'T-1', '--repo', str(self.repo)])
        with self.assertRaises(ValueError):
            sup.build_argv('orchestrator.unknown', a)
        self.assertFalse((self.repo / 'injected').exists())

    def test_invalid_inputs_rejected_before_lock_or_spawn(self):
        for change in [dict(ticket='../x'), dict(ticket='-x'), dict(tier='XL'), dict(goal='-x'),
                       dict(repo='-x'), dict(recovery_allowance='-1'), dict(recovery_allowance='1.0'),
                       dict(recovery_allowance=True), dict(recovery_allowance='101'),
                       dict(from_phase='04-review'), dict(issue=1), dict(sync_remote=True), dict(goal='x'*65537)]:
            with self.subTest(change=str(change)[:80]), patch.object(sup, 'lock') as lock, patch.object(sup.subprocess, 'Popen') as spawn:
                with self.assertRaises(ValueError):
                    sup.start(dict(self.args, **change))
                lock.assert_not_called()
                spawn.assert_not_called()
                self.assertFalse((self.repo / '.ssdlc').exists())

    def test_cli_start_and_detach_delegate_exclusively_to_builder(self):
        import orchestrator
        for command, extra in [('start', []), ('run', ['--detach'])]:
            received = self.repo / (command + '.json')
            controller = self.controller('import json, sys, time\nfrom pathlib import Path\n'
                f'Path({str(received)!r}).write_text(json.dumps(sys.argv[1:]))\ntime.sleep(30)\n')
            original = sup.validate_args
            def with_controller(a):
                result = original(a)
                result['controller'] = str(controller)
                return result
            goal = '; touch ' + str(self.repo / 'INJECTION') + ' title ; rm -rf'
            with patch.object(sup, 'validate_args', with_controller), contextlib.redirect_stdout(io.StringIO()) as out:
                self.addCleanup(self.cleanup_run)
                code = orchestrator.main([command, '--ticket', 'T-1', '--goal', goal, '--repo', str(self.repo), '--tier', 'S', '--json'] + extra)
            self.assertEqual(code, 0, out.getvalue())
            result = json.loads(out.getvalue())
            self.assertEqual(result['handoff']['outcome'], 'launched')
            self.wait_for(received.exists)
            expected = sup.build_argv('orchestrator.run', dict(self.args, goal=goal, tier='S', controller=str(controller)))[2:]
            self.assertEqual(json.loads(received.read_text()), expected)
            self.assertFalse((self.repo / 'INJECTION').exists())
            store = sup.RunStore(self.repo, 'T-1')
            d = store.current()
            self.assertEqual(d['authorized']['test_cmd_authority'], 'operator_flag')
            self.assertNotIn(goal, json.dumps(d))
            self.cleanup_run()
            # Use a new temporary repo for the second alias, preserving metadata.
            if command == 'start':
                self.repo = self.repo / 'second'
                self.repo.mkdir()
                self.args['repo'] = str(self.repo)

    def test_cli_argument_error_safe_and_no_artifacts(self):
        import orchestrator
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            orchestrator.main(['start', '--ticket', '../bad', '--goal', 'goal', '--repo', str(self.repo)])
        self.assertEqual(e.exception.code, 2)
        self.assertFalse((self.repo / '.ssdlc').exists())


class TestCoreFaultInjection(CoreCase):
    def test_F01_F02_no_run_and_retry(self):
        self.controller()
        for point in ['lock.before_acquire', 'start.after_lock_before_intent']:
            with self.subTest(point=point):
                with self.assertRaises(sup.InjectedCrash):
                    sup.start(self.args, fault=CrashAt(point))
                self.assertFalse(list(self.repo.glob('.ssdlc/.supervisor/*/runs/*/run.json')))
        self.addCleanup(self.cleanup_run)
        self.assertEqual(sup.start(self.args)['handoff']['outcome'], 'launched')

    def test_F03_F03b_F07_abandonment_fence_consumption(self):
        for role, point in [('worker', 'launch.after_intent_before_pipe'), ('worker', 'launch.after_pipe_before_spawn'),
                            ('controller', 'controller.after_intent_before_spawn')]:
            with self.subTest(point=point):
                store = self.store()
                store.fault = CrashAt(point)
                with store.locked(), self.assertRaises(sup.InjectedCrash):
                    sup.spawn(store, role, 1, True)
                self.assertEqual(store.read()['launches'][0]['state'], 'pending')
                with sup.lock(store.root, 'T-1/ownership.lock'):
                    d = sup.reconcile(store, FakeProc())
                self.assertEqual(d['launches'][0]['state'], 'abandoned')
                self.assertEqual(d['recovery']['used'], 1)
                sup.reconcile(store, FakeProc())
                self.assertEqual(store.read()['recovery']['used'], 1)
                # Subsequent subcases get their own isolated store.
                self.repo = self.repo / role / point.replace('.', '_')
                self.repo.mkdir(parents=True)
                self.args['repo'] = str(self.repo)

    def test_reconcile_allowance_zero_blocks(self):
        self.args['recovery_allowance'] = 0
        store = self.store()
        store.update(lambda d: d['launches'].append(dict(intent_id='a'*32, role='worker', generation=1,
                                                        state='pending', at=1000.)))
        d = sup.reconcile(store, FakeProc())
        self.assertEqual(d['status_reason'], 'reconcile')
        self.assertEqual(d['recovery']['used'], 0)
        self.assertEqual(d['launches'][0]['state'], 'abandoned')

    def test_F04_F08_adopt_exactly_one_marker_match_and_ambiguous_scan_blocks(self):
        store = self.store()
        def pending(d):
            d['launches'].append(dict(intent_id='a'*32, role='controller', generation=1, state='spawned', at=1000.))
        store.update(pending)
        d = store.read()
        proc = FakeProc()
        ident = proc.add(21, nonce=d['nonce'], role='controller', intent='a'*32, run_id=d['run_id'])
        sup.reconcile(store, proc)
        self.assertEqual(store.read()['processes']['controller'], asdict(ident))
        # Identity published before GO; an unreleased gone process is abandoned.
        del proc.entries[21]
        sup.reconcile(store, proc)
        self.assertEqual(store.read()['launches'][0]['state'], 'abandoned')

    def test_unreadable_or_multiple_candidates_block(self):
        store = self.store()
        store.update(lambda d: d['launches'].append(dict(intent_id='a'*32, role='worker', generation=1, state='pending', at=1000.)))
        d = store.read()
        proc = FakeProc()
        proc.add(21, nonce=d['nonce'], intent='a'*32, run_id=d['run_id'])
        proc.add(22, nonce=d['nonce'], intent='a'*32, run_id=d['run_id'])
        self.assertEqual(sup.reconcile(store, proc)['status_reason'], 'reconcile')
        del proc.entries[22]
        proc.entries[21][3] = PermissionError()
        self.assertEqual(sup.reconcile(store, proc)['status_reason'], 'reconcile')
        self.assertEqual(store.read()['recovery']['used'], 0)

    def test_real_F04_F05_shim_exits_on_EOF_without_controller(self):
        self.controller()
        for point in ['launch.after_spawn_before_identity', 'launch.after_identity_before_go']:
            with self.subTest(point=point):
                with self.assertRaises(sup.InjectedCrash):
                    sup.start(self.args, fault=CrashAt(point))
                store = sup.RunStore(self.repo, 'T-1')
                d = store.current()
                def no_live_marker():
                    for pid in sup.Proc().pids():
                        try:
                            ident, state, _ = sup.Proc().stat(pid)
                            env = sup.Proc().environ(pid)
                            if state != 'Z' and env.get(sup.MARK + 'NONCE') == d['nonce']:
                                return False
                        except OSError:
                            continue
                    return True
                self.wait_for(no_live_marker)
                self.assertIsNone(store.read()['processes']['controller'])
                self.assertFalse(any(e['type'] == 'shim_released' for e in store.events()))
                self.repo = self.repo / point.replace('.', '_')
                self.repo.mkdir()
                self.args['repo'] = str(self.repo)
                self.controller()

    def test_partial_worker_payload_blocks_before_controller_spawn(self):
        store = self.store()
        r, w = os.pipe()
        os.write(w, b'\0\0\0\x05abc')
        os.close(w)
        args = type('Args', (), dict(payload_fd=r, generation=1))()
        try:
            with self.assertRaises(EOFError), patch.object(sup, 'spawn') as spawn:
                sup.worker(store, args)
            spawn.assert_not_called()
            self.assertIsNone(store.read()['processes']['controller'])
        finally:
            with contextlib.suppress(OSError):
                os.close(r)

    def test_F20_controller_publication_wins_timeout_under_lock(self):
        store = self.store()
        proc = FakeProc()
        class Child:
            pid = 9001
        d = store.read()
        proc.add(9001, nonce=d['nonce'], role='controller', run_id=d['run_id'])
        with patch.object(sup.subprocess, 'Popen', return_value=Child()):
            with store.locked():
                child, pipe, intent = sup.spawn(store, 'controller', 1, True, proc)
        os.close(pipe)
        d = store.read()
        self.assertEqual(d['handoff']['outcome'], 'launched')
        self.assertEqual(d['launches'][-1]['state'], 'published')
        self.assertEqual(store.handoff('blocked', 'handoff_timeout')['handoff']['outcome'], 'launched')
        self.assertFalse(store.read()['launch_revoked'])

    def test_F05_F05b_released_gone_is_uncertain(self):
        store, proc = self.fake_run(controller=False)
        d = store.read()
        intent = d['launches'][0]['intent_id']
        store.append(store.path('events.jsonl'), dict(event_id=store.run_id + ':0:released', type='shim_released', intent=intent))
        del proc.entries[1001]
        self.assertEqual(sup.reconcile(store, proc)['status_reason'], 'uncertain_outcome')
        self.assertEqual(store.read()['recovery']['used'], 0)

    def test_F05c_abandoned_intent_rejects_delayed_shim(self):
        store = self.store()
        store.update(lambda d: d['launches'].append(dict(intent_id='a'*32, role='worker', generation=1, state='pending', at=1000.)))
        sup.reconcile(store, FakeProc())
        r, w = os.pipe()
        os.write(w, b'GO')
        os.close(w)
        args = type('Args', (), dict(repo=str(self.repo), ticket='T-1', run_id=store.run_id,
                                    role='worker', generation=1, intent='a'*32, go_fd=r))()
        try:
            with patch.object(sup, 'worker') as worker:
                self.assertEqual(sup.boot(args), 0)
                worker.assert_not_called()
        finally:
            with contextlib.suppress(OSError):
                os.close(r)

    def test_F05d_partial_payload_and_size_bound(self):
        r, w = os.pipe()
        os.write(w, b'\0\0\0\x05abc')
        os.close(w)
        try:
            with self.assertRaises(EOFError):
                sup.transfer(r, timeout=1)
        finally:
            os.close(r)
        r, w = os.pipe()
        try:
            with self.assertRaises(ValueError):
                sup.transfer(w, b'x' * 1048577)
        finally:
            os.close(r)
            os.close(w)

    def test_F05e_stalled_payload_times_out_and_late_worker_fenced(self):
        store = self.store()
        r, w = os.pipe()
        try:
            with self.assertRaises(TimeoutError):
                sup.transfer(w, b'x' * 100000, timeout=.02)
        finally:
            os.close(r)
            os.close(w)
        d = store.handoff('blocked', 'inputs_incomplete')
        self.assertTrue(d['launch_revoked'])
        with self.assertRaises(sup.FencedOut):
            store.handoff('launched', 'launched', 1)
        self.assertIsNone(store.read()['processes']['controller'])

    def test_F06a_F06b_atomic_boundaries(self):
        store = self.store()
        before = store.read()
        for point, expected in [('atomic.after_tmp_before_replace', 'starting'),
                                ('atomic.after_replace_before_dirsync', 'running')]:
            store.fault = CrashAt(point)
            with self.assertRaises(sup.InjectedCrash):
                store.update(lambda d: d.update(status='running'))
            self.assertEqual(store.read()['status'], expected)
        self.assertEqual(before['run_id'], store.read()['run_id'])

    def test_event_replay_idempotent_both_sides_of_append(self):
        store = self.store()
        for point in ['event.after_state_before_append', 'event.after_append_before_clear']:
            store.fault = CrashAt(point)
            with self.assertRaises(sup.InjectedCrash):
                store.update(lambda d: d.update(status='running'), event='started')
            store.update(lambda d: None)
            events = store.events()
            self.assertEqual(len(events), len({e['event_id'] for e in events}))
        self.assertEqual(len(store.events()), 2)

    def test_F11_F11b_F11c_cancel_intent_revokes_worker_only(self):
        store, proc = self.fake_run(controller=False)
        with self.assertRaises(sup.InjectedCrash):
            sup.cancel(self.repo, 'T-1', clock=self.clock, proc=proc, fault=CrashAt('cancel.after_intent_before_signal'))
        self.assertTrue(store.read()['launch_revoked'])
        self.assertFalse(proc.signals)
        with store.locked(), self.assertRaises(sup.FencedOut):
            sup.spawn(store, 'controller', 1, True, proc)
        result = sup.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual({pid for pid, _ in proc.signals}, {1001})

    def test_F12_F13_cancel_restart_idempotent_and_grace_from_term(self):
        store, proc = self.fake_run()
        for point in ['cancel.after_term_before_kill', 'cancel.after_kill_before_record']:
            with self.assertRaises(sup.InjectedCrash):
                sup.cancel(self.repo, 'T-1', grace=5, clock=self.clock, proc=proc, fault=CrashAt(point))
        self.assertEqual(self.clock.now, 1005.)
        result = sup.cancel(self.repo, 'T-1', grace=5, clock=self.clock, proc=proc)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(store.read()['in_flight'], 'uncertain')
        before = proc.signals[:]
        sup.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc)
        self.assertEqual(proc.signals, before)
        self.assertEqual([pid for pid, sig in before if sig == signal.SIGTERM], [2002, 1001])

    def test_F18_F19_F20_handoff_CAS_both_orders_and_publication_fence(self):
        store = self.store()
        d = store.handoff('blocked', 'handoff_timeout')
        self.assertTrue(d['launch_revoked'])
        with self.assertRaises(sup.FencedOut):
            store.handoff('launched', 'launched', 1)
        with store.locked(), self.assertRaises(sup.FencedOut):
            sup.spawn(store, 'controller', 1, True, FakeProc())
        self.repo = self.repo / 'second'
        self.repo.mkdir()
        self.args['repo'] = str(self.repo)
        store = self.store()
        launched = store.handoff('launched', 'launched', 1)
        self.assertEqual(store.handoff('blocked', 'handoff_timeout'), launched)
        self.assertFalse(store.read()['launch_revoked'])
        store.update(lambda d: d.update(worker_generation=2))
        with self.assertRaises(sup.FencedOut):
            store.update(lambda d: None, fence=(1, 0))

    def test_cancel_unowned_members_pid_reuse_and_pidfd_unsupported(self):
        store, proc = self.fake_run()
        proc.add(3003, sid=2002, ticks=250, nonce='wrong')
        proc.entries[3003][3] = {}
        result = sup.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc)
        self.assertEqual(result['status_reason'], 'unowned_session_member')
        self.assertFalse(proc.signals)
        del proc.entries[3003]
        with patch.object(os, 'pidfd_open', create=True) as fd:
            # An explicit missing kernel API fails closed.
            del os.pidfd_open
            try:
                result = sup.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc)
            finally:
                os.pidfd_open = fd
        self.assertEqual(result['status_reason'], 'pidfd_unsupported')
        self.assertFalse(proc.signals)

    def test_session_birth_bound_and_descendant_admission_after_leader_exit(self):
        store, proc = self.fake_run()
        d = store.read()
        proc.add(2003, sid=2002, ticks=220, nonce=d['nonce'], role='controller', intent='b'*32,
                 run_id=d['run_id'], ppid=2002)
        proc.add(1002, sid=1001, ticks=110, nonce=d['nonce'], role='worker', intent='a'*32,
                 run_id=d['run_id'], ppid=1001)
        del proc.entries[2002]
        self.assertEqual(sup.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc)['status'], 'cancelled')
        self.assertEqual({pid for pid, _ in proc.signals}, {1001, 1002, 2003})
        self.assertEqual([pid for pid, sig in proc.signals if sig == signal.SIGTERM][-1], 1001)


class TestNoSecretsAndCanonicalBoundary(CoreCase):
    def snapshot(self, root):
        if not root.exists():
            return None
        return {str(p.relative_to(root)): (p.stat().st_mtime_ns,
            tuple(sorted(x.name for x in p.iterdir())) if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest())
            for p in [root] + list(root.rglob('*'))}

    def test_sentinel_all_channels_and_canonical_runtime_unchanged(self):
        secret = 'SENTINEL_SECRET_27c91'
        ticket = self.repo / '.ssdlc/T-1'
        runtime = self.repo / '.ssdlc/.runtime'
        for root in [ticket, runtime]:
            (root / 'nested').mkdir(parents=True)
            (root / 'nested/evidence').write_text('canonical fixture')
        before = [self.snapshot(p) for p in [ticket, runtime]]
        self.controller('import sys, time\nprint(' + repr(secret) + ', flush=True)\n'
                        'print(' + repr(secret) + ', file=sys.stderr, flush=True)\ntime.sleep(30)\n')
        self.args.update(goal=secret, test_cmd='echo ' + secret)
        self.addCleanup(self.cleanup_run)
        with patch.dict(os.environ, {'PROVIDER_TOKEN': secret}), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            result = sup.start(self.args)
            self.assertEqual(result['handoff']['outcome'], 'launched')
            store = sup.RunStore(self.repo, 'T-1')
            d = store.current()
            self.assertEqual(d['authorized']['test_cmd_sha256'], sup.digest('echo ' + secret))
            self.assertEqual(sup.status(self.repo, 'T-1')['status'], 'running')
            sup.reconcile(store)
            with self.assertRaises(ValueError):
                store.append(store.path('events.jsonl'), dict(type='failure', provider_text=secret))
            result = sup.cancel(self.repo, 'T-1', grace=0)
            self.assertEqual(result['status'], 'cancelled')
        self.assertNotIn(secret, out.getvalue() + err.getvalue() + json.dumps(result))
        for p in store.root.rglob('*'):
            self.assertEqual(p.stat().st_mode & 0o777, 0o700 if p.is_dir() else 0o600)
            if p.is_file():
                self.assertNotIn(secret.encode(), p.read_bytes(), str(p))
        self.assertEqual(before, [self.snapshot(p) for p in [ticket, runtime]])
        # No output files exist; the process stdio target is the kernel null device.
        self.assertFalse(list(store.root.rglob('*.out')))

    def test_status_pending_cancel_is_strictly_read_only(self):
        store, proc = self.fake_run()
        store.update(lambda d: d.update(cancel=dict(epoch=1, requested_at=1000., state='requested')))
        before = self.snapshot(store.root)
        with patch.object(sup, 'cancel', side_effect=AssertionError('status executed cancel')):
            result = sup.status(self.repo, 'T-1', proc=proc)
        self.assertEqual(result['status'], 'cancelling')
        self.assertEqual(self.snapshot(store.root), before)
        self.assertFalse(proc.signals)


    def test_pending_cancel_keeps_priority_after_controller_exit(self):
        store, proc = self.fake_run()
        store.update(lambda d: d.update(cancel=dict(epoch=1, requested_at=1000., state='requested')))
        del proc.entries[2002]
        before = self.snapshot(store.root)
        with patch.object(sup, 'classify', side_effect=AssertionError('cancel reclassified as a crash')):
            result = sup.status(self.repo, 'T-1', proc=proc)
        self.assertEqual(result['status'], 'cancelling')
        self.assertEqual(result['next_action'], 'repeat_cancel')
        self.assertEqual(self.snapshot(store.root), before)
        self.assertFalse(proc.signals)


class TestRealProcesses(CoreCase):
    def test_detach_survives_caller_exit_and_exactly_one_concurrent_launch(self):
        received = self.repo / 'launches'
        self.controller('import os, time\nfd=os.open(' + repr(str(received)) + ', os.O_CREAT|os.O_WRONLY|os.O_APPEND, 0o600)\nos.write(fd,b"launch\\n")\nos.close(fd)\ntime.sleep(30)\n')
        self.addCleanup(self.cleanup_run)
        code = 'import json,ssdlc_supervisor as s; print(json.dumps(s.start(json.loads(' + repr(json.dumps(self.args)) + '))))'
        callers = [subprocess.Popen([sys.executable, '-c', code], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(5)]
        results = []
        for caller in callers:
            out, err = caller.communicate(timeout=15)
            self.assertEqual(caller.returncode, 0, err.decode())
            results.append(json.loads(out))
        self.assertEqual(sum(x['handoff']['outcome'] == 'launched' and x['status'] == 'running' for x in results), 1)
        self.assertTrue(all(x['status'] in {'running', 'locked', 'already_running'} for x in results), results)
        self.wait_for(received.exists)
        self.assertEqual(received.read_text(), 'launch\n')
        self.assertEqual(sup.status(self.repo, 'T-1')['status'], 'running')
        cancelled = sup.cancel(self.repo, 'T-1', grace=0)
        self.assertEqual(cancelled['status'], 'cancelled', cancelled)

    def test_scoped_cancel_unrelated_session_untouched(self):
        self.controller()
        sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        def reap_sibling():
            if sibling.poll() is None:
                sibling.kill()
            sibling.wait(timeout=5)
        self.addCleanup(reap_sibling)
        self.addCleanup(self.cleanup_run)
        self.assertEqual(sup.start(self.args)['status'], 'running')
        self.assertEqual(sup.cancel(self.repo, 'T-1', grace=0)['status'], 'cancelled')
        self.assertIsNone(sibling.poll())
        sibling.terminate()
        sibling.wait(timeout=5)

    def test_real_pidfd_leader_exit_between_verify_and_signal(self):
        helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        self.addCleanup(lambda: helper.kill() if helper.poll() is None else None)
        ident = sup.Proc().stat(helper.pid)[0]
        fd = os.pidfd_open(helper.pid)
        helper.terminate()
        helper.wait(timeout=5)
        try:
            self.assertEqual(sup.verify(ident, 'not-ours', 'worker', 1), sup.GONE)
            with self.assertRaises(ProcessLookupError):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        finally:
            os.close(fd)

    def test_real_cancel_restart_after_term(self):
        self.controller('import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(30)\n')
        self.addCleanup(self.cleanup_run)
        sup.start(self.args)
        with self.assertRaises(sup.InjectedCrash):
            sup.cancel(self.repo, 'T-1', grace=0, fault=CrashAt('cancel.after_term_before_kill'))
        self.assertEqual(sup.status(self.repo, 'T-1')['status'], 'cancelling')
        self.assertEqual(sup.cancel(self.repo, 'T-1', grace=0)['status'], 'cancelled')


class TestCancellationExitRace(CoreCase):
    def check_admission(self, transition):
        store, proc = self.fake_run()
        real_environ = proc.environ
        def exiting(pid):
            if pid == 2002:
                if transition == 'gone':
                    del proc.entries[pid]
                    raise ProcessLookupError()
                if transition == 'zombie':
                    proc.entries[pid][1] = 'Z'
                elif transition == 'reused':
                    proc.entries[pid][0] = replace(proc.entries[pid][0], start_ticks=999)
                elif transition == 'changed-session':
                    proc.entries[pid][0] = replace(proc.entries[pid][0], sid=9999, pgid=9999)
                proc.entries[pid][3] = {}
            return real_environ(pid)
        with patch.object(proc, 'environ', side_effect=exiting):
            admitted, error = sup._admit_session(store.read(), 'controller', proc)
        self.assertEqual(admitted, [])
        self.assertEqual(error, 'unowned_session_member' if transition in {'live-unowned', 'changed-session'} else None)
        self.assertEqual(proc.closed.count((2002, 200)), 1)
        self.assertFalse(proc.signals)

    def test_zombie_during_environment_read_is_not_a_live_unowned_member(self):
        self.check_admission('zombie')

    def test_disappeared_process_is_ignored(self):
        self.check_admission('gone')

    def test_reused_pid_closes_only_the_old_pinned_descriptor(self):
        self.check_admission('reused')

    def test_live_unowned_session_member_still_blocks(self):
        self.check_admission('live-unowned')

    def test_live_identity_change_still_blocks(self):
        self.check_admission('changed-session')


class TestTerminalCleanupAndDeadline(CoreCase):
    def check_terminal(self, status, reason):
        store, proc = self.fake_run()
        store.update(lambda d: d.update(status=status, status_reason=reason))
        before = store.read()
        out = sup.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc)
        self.assertEqual((out['status'], out['status_reason']), (status, reason))
        self.assertEqual(out['cancellation_state'], 'done')
        after = store.read()
        self.assertEqual(after['cancel']['state'], 'done')
        self.assertEqual(after['classification'], before['classification'])
        self.assertEqual(after['completion_kind'], before['completion_kind'])
        if status == 'completed':
            self.assertEqual(after.get('in_flight'), before.get('in_flight'))
        signals = list(proc.signals)
        self.assertEqual(sup.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc)['status'], status)
        self.assertEqual(proc.signals, signals)

    def test_completed_outcome_survives_cleanup(self):
        self.check_terminal('completed', 'completed')

    def test_failed_outcome_survives_cleanup(self):
        self.check_terminal('failed', 'hard_deadline')

    def test_blocked_outcome_survives_cleanup(self):
        self.check_terminal('blocked', 'reconcile')

    def test_operator_wait_outcome_survives_cleanup(self):
        self.check_terminal('operator_wait', 'auth_missing')

    def test_deadline_stops_owned_resources_and_preserves_limits_outcome(self):
        store, proc = self.fake_run()
        d = store.read()
        store.update(lambda v: v['deadlines'].update(hard_deadline=999.))
        owned = dict(id='d'*64, state='running', labels={'ssdlc.run_id': d['run_id'], 'ssdlc.project': d['project']['key']})
        foreign = dict(id='e'*64, state='running', labels={'ssdlc.run_id': 'f'*32, 'ssdlc.project': d['project']['key']})
        docker = FakeDocker()
        docker.containers = [owned, foreign]
        out = sup.monitor_tick(store, {'SSDLC_EXECUTION_MODE': 'native'}, proc=proc, docker=docker)
        self.assertEqual((out['status'], out['status_reason']), ('failed', 'hard_deadline'))
        self.assertEqual(out['classification']['class'], 'limits')
        self.assertEqual(store.read()['cancel']['state'], 'done')
        self.assertEqual(set(pid for pid, sig in proc.signals), {1001, 2002})
        self.assertFalse(proc.entries)
        self.assertEqual(docker.stopped, ['d'*64])
        self.assertEqual(foreign['state'], 'running')
        self.assertEqual(store.read()['recovery']['used'], 0)

    def test_pending_cancel_is_completed_by_monitor_but_status_is_read_only(self):
        store, proc = self.fake_run()
        with self.assertRaises(sup.InjectedCrash):
            sup.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc,
                       fault=CrashAt('cancel.after_intent_before_signal'))
        self.assertEqual(sup.status(self.repo, 'T-1', proc=proc)['status'], 'cancelling')
        self.assertFalse(proc.signals)
        out = sup.monitor_tick(store, {'SSDLC_EXECUTION_MODE': 'native'}, proc=proc)
        self.assertEqual(out['status'], 'cancelled')
        self.assertEqual(store.read()['cancel']['state'], 'done')

    def test_native_cleanup_does_not_require_docker(self):
        store, proc = self.fake_run()
        self.assertFalse(store.read()['authorized']['containers_possible'])
        with patch.object(sup, 'DockerCancellation', side_effect=AssertionError('unused Docker queried')):
            self.assertEqual(sup.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc)['status'], 'cancelled')

    def test_malformed_preserved_terminal_is_rejected_before_write(self):
        store, proc = self.fake_run()
        with self.assertRaises(sup.SupervisorIntegrityError):
            store.update(lambda d: d.update(cancel=dict(epoch=1, requested_at=1000., state='requested',
                preserved_terminal=dict(status='running', reason='launched'))))
        self.assertIsNone(store.read()['cancel'])

class TestLifecycleObservationBoundaries(CoreCase):
    def test_owned_worker_preflight_and_backoff_are_not_reconciliation(self):
        store, proc = self.fake_run(controller=False)
        store.update(lambda d: d.update(status='starting', status_reason='retry_scheduled'))
        before = store.read()
        self.assertEqual(sup.status(self.repo, 'T-1', proc=proc)['status'], 'starting')
        self.assertEqual(store.read(), before)
        out = sup.monitor_tick(store, {'SSDLC_EXECUTION_MODE': 'native'}, proc=proc)
        self.assertEqual(out['status'], 'starting')
        self.assertEqual(store.read()['status'], 'starting')
        self.assertEqual(store.read()['recovery']['used'], 0)
        self.assertFalse(proc.signals)
        self.assertFalse(any(x['role'] == 'controller' for x in store.read()['launches']))

    def test_missing_published_controller_still_requires_reconciliation(self):
        store, proc = self.fake_run()
        del proc.entries[2002]
        self.assertEqual(sup.status(self.repo, 'T-1', proc=proc)['status'], 'reconcile')
        self.assertFalse(proc.signals)

    def test_pending_controller_without_identity_still_requires_reconciliation(self):
        store, proc = self.fake_run(controller=False)
        store.update(lambda d: d['launches'].append(dict(intent_id='b'*32, role='controller', generation=1,
            state='pending', at=1000., abandoned_at=None)))
        self.assertEqual(sup.status(self.repo, 'T-1', proc=proc)['status'], 'reconcile')
        self.assertFalse(proc.signals)

    def test_postmortem_monitor_preserves_prior_outcome_after_deadline(self):
        store, proc = self.fake_run()
        classification = dict(**{'class': 'code_defect'}, reason_code='terminal_check_failure', row='6',
                              terminal_bound=True, diagnostics=[])
        for status in ['failed', 'operator_wait', 'blocked']:
            with self.subTest(status=status):
                store.update(lambda d: d.update(status=status, status_reason='terminal_check_failure', classification=classification))
                store.update(lambda d: d['deadlines'].update(hard_deadline=999.))
                before = store.read()
                with patch.object(sup, 'classify', side_effect=AssertionError('terminal outcome reclassified')):
                    out = sup.monitor_tick(store, {'SSDLC_EXECUTION_MODE': 'native'}, proc=proc)
                self.assertEqual(out['status'], status)
                self.assertEqual(out['classification'], classification)
                self.assertEqual(store.read(), before)
                self.assertFalse(proc.signals)
                # Also check the normal post-mortem case with a gone controller.
                controller = proc.entries.pop(2002)
                try:
                    with patch.object(sup, 'classify', side_effect=AssertionError('terminal status reclassified')):
                        view = sup.status(self.repo, 'T-1', proc=proc)
                    self.assertEqual(view['status'], status)
                    self.assertEqual(view['classification'], classification)
                    self.assertEqual(store.read(), before)
                finally:
                    proc.entries[2002] = controller

    def test_persisted_deadline_resumes_only_its_pending_cleanup(self):
        store, proc = self.fake_run()
        classification = sup.classify(dict(hard_deadline=True))
        store.update(lambda d: d.update(status='failed', status_reason='hard_deadline', classification=classification))
        out = sup.monitor_tick(store, {'SSDLC_EXECUTION_MODE': 'native'}, proc=proc)
        self.assertEqual((out['status'], out['status_reason']), ('failed', 'hard_deadline'))
        self.assertEqual(out['classification'], classification)
        self.assertEqual(out['cancellation_state'], 'done')
        self.assertFalse(proc.entries)

class TestReviewDiagnosisRegressions(CoreCase):
    def test_whole_run_deadline_requires_explicit_authorization(self):
        env = {'SSDLC_DEV_TIMEOUT': '1', 'SSDLC_MAX_DEV_RETRIES': '1', 'SSDLC_MAX_DESIGN_ROUNDS': '1'}
        self.assertIsNone(sup.configured_hard_deadline(env, 1000))
        env['SSDLC_SUPERVISOR_HARD_TIMEOUT'] = '120'
        self.assertEqual(sup.configured_hard_deadline(env, 1000), 1120.)
        for value in ['0', '-1', 'invalid']:
            env['SSDLC_SUPERVISOR_HARD_TIMEOUT'] = value
            with self.assertRaises(ValueError):
                sup.configured_hard_deadline(env, 1000)

    def test_ambient_environment_is_pinned_as_required_by_adr(self):
        env = {'PATH': '/fixture', 'TERM': 'xterm', 'OLDPWD': '/first'}
        self.assertEqual(sup.config_digest(env), sup.config_digest(dict(env, PWD='/noise', SHLVL='4', _='noise')))
        for key in ['TERM', 'OLDPWD', 'SSH_AUTH_SOCK']:
            self.assertNotEqual(sup.config_digest(env), sup.config_digest(dict(env, **{key: 'changed'})))

    def test_repeated_start_reports_live_owned_run_before_new_run_compatibility(self):
        store, proc = self.fake_run()
        before = store.read()
        with patch.object(sup, 'Proc', return_value=proc), patch.object(sup, 'compatibility', side_effect=AssertionError('new run compatibility called')), patch.object(sup, 'spawn', side_effect=AssertionError('duplicate launch')):
            out = sup.start(self.args)
        self.assertEqual((out['status'], out['status_reason']), ('already_running', 'already_running'))
        self.assertEqual(store.read(), before)
        self.assertFalse((store.root / store.ticket / 'audit.jsonl').exists())

    def check_monitor_cancel_race(self, expired):
        store, proc = self.fake_run()
        if expired:
            store.update(lambda d: d['deadlines'].update(hard_deadline=999.))
        saved = []
        def inject(point):
            if point == 'classify.before_persist':
                sup.cancel(self.repo, 'T-1', run_id=store.run_id, grace=0, clock=self.clock, proc=proc)
                saved.append(store.read())
        store.fault.hit = inject
        out = sup.monitor_tick(store, {'SSDLC_EXECUTION_MODE': 'native'}, proc=proc)
        self.assertEqual(out['status'], 'cancelled')
        self.assertEqual(out['cancellation_state'], 'done')
        self.assertEqual(store.read(), saved[0])
        self.assertFalse(proc.entries)

    def test_cli_monitor_cannot_overwrite_concurrent_cancel(self):
        self.check_monitor_cancel_race(False)

    def test_stale_deadline_observation_cannot_overwrite_concurrent_cancel(self):
        self.check_monitor_cancel_race(True)

class TestCompletionModeAndForegroundRegressions(CoreCase):
    def completed_legacy(self):
        store, proc = self.fake_run()
        del proc.entries[2002]
        store.update(lambda d: d.update(base_commit_at_launch='base', exit_evidence=dict(source='parent_returncode', code=0, intent_id='b'*32)))
        store.update(lambda d: d['authorized'].update(task_mode=False))
        path = self.repo / '.ssdlc/T-1/state.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(base_commit='base', status='AWAITING_SUPERVISOR_SIGNOFF')))
        return store, proc

    def test_legacy_completion_has_no_task_checkpoint_requirement(self):
        store, proc = self.completed_legacy()
        canonical = self.repo / '.ssdlc/T-1/state.json'
        before = canonical.read_bytes()
        with patch.object(sup, 'git_read', return_value='base'):
            out = sup.status(self.repo, 'T-1', proc=proc)
        self.assertEqual((out['status'], out['completion_kind']), ('completed', 'run'))
        self.assertEqual(canonical.read_bytes(), before)

    def test_task_completion_still_requires_verified_clean_worktree(self):
        store, proc = self.completed_legacy()
        store.update(lambda d: d['authorized'].update(task_mode=True))
        with patch.object(sup, 'git_read', return_value='base'):
            out = sup.status(self.repo, 'T-1', proc=proc)
        self.assertEqual(out['status_reason'], 'completion_mismatch')

    def test_old_record_without_mode_does_not_gain_legacy_completion_authority(self):
        store, proc = self.completed_legacy()
        store.update(lambda d: d['authorized'].pop('task_mode'))
        with patch.object(sup, 'git_read', return_value='base'):
            out = sup.status(self.repo, 'T-1', proc=proc)
        self.assertEqual(out['status_reason'], 'completion_mismatch')

    def test_foreground_resume_retains_legacy_baseline_refusal_contract(self):
        import types
        import orchestrator as o
        root = self.repo / '.ssdlc/T-1'
        fake = types.SimpleNamespace(ssdlc_root=root, state={}, _task_mode=lambda: True,
            PHASE_ORDER=o.SSDLCOrchestrator.PHASE_ORDER, _ensure_task_plan=lambda: None,
            run_phase_dev=lambda **kwargs: (_ for _ in ()).throw(RuntimeError('reached_dev')))
        with self.assertRaisesRegex(RuntimeError, 'reached_dev'), contextlib.redirect_stdout(io.StringIO()):
            o.SSDLCOrchestrator._run_pipeline(fake, 'goal', from_phase='03-dev')
        # Supervised compatibility remains read-only and still refuses missing state.
        self.assertFalse(o.resume_compatibility(self.repo, 'T-1', 'run', '03-dev', {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'})['ok'])

if __name__ == '__main__':
    unittest.main()
