"""T3 authorization, ownership and canonical-state boundaries; no cloud calls."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import struct
import sys
import time
import unittest
from unittest.mock import patch

import orchestrator as o
import ssdlc_supervisor as s
from test_supervisor_core import CoreCase
from helpers.supervisor_fakes import FakeClock, FakeDocker, CrashAt, hook_environment


class TestResumeAndArgv(CoreCase):
    def baseline(self):
        root = self.repo / '.ssdlc/T-1'
        root.mkdir(parents=True, exist_ok=True)
        (root / 'state.json').write_text('{}')
        return root

    def terminal(self):
        store = self.store()
        store.update(lambda d: d.update(status='failed', status_reason='reconcile'))
        return store

    def test_allow_list_and_literal_goal_test_command(self):
        goal = 'ticket title ; rm -rf never-executed'
        for entry, flag, good, bad in [('plan', 'review_repairs', (0, 4), (-1, 5)), ('design', 'rounds', (1, 8), (0, 9))]:
            for number in good:
                argv = s.build_argv('orchestrator.' + entry, dict(self.args, **{flag: number}))
                self.assertIsInstance(argv, list)
                self.assertIn(str(number), argv)
                self.assertNotIn('--goal', argv)
                self.assertNotIn('--sync-remote', argv)
                self.assertNotIn('--issue', argv)
            for number in bad:
                with self.assertRaises(ValueError):
                    s.build_argv('orchestrator.' + entry, dict(self.args, **{flag: number}))
        argv = s.build_argv('orchestrator.run', dict(self.args, goal=goal, test_cmd='printf authorized'))
        self.assertEqual(argv[argv.index('--goal') + 1], goal)
        self.assertEqual(argv[argv.index('--test-cmd') + 1], 'printf authorized')
        self.assertFalse((self.repo / 'never-executed').exists())

    def test_read_only_compatibility_no_constructor_runtime_or_save(self):
        root = self.baseline()
        with patch.object(o.SSDLCOrchestrator, '__init__', side_effect=AssertionError), patch.object(o.ProjectRuntimeIsolation, 'activate', side_effect=AssertionError), patch.object(o.SSDLCOrchestrator, '_save_state', side_effect=AssertionError):
            before = tree(root)
            self.assertTrue(o.resume_compatibility(self.repo, 'T-1', 'run', '03-dev')['ok'])
            for entry in ('plan', 'design', 'unknown'):
                self.assertFalse(o.resume_compatibility(self.repo, 'T-1', entry)['ok'])
            self.assertEqual(tree(root), before)
        (root / '03-dev/tasks').mkdir(parents=True)
        (root / '03-dev/tasks/task-journal.jsonl').write_text('')
        self.assertFalse(o.resume_compatibility(self.repo, 'T-1', 'run', '00-recon', {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'})['ok'])

    def test_refusal_one_audit_no_metadata_or_spawn(self):
        root = self.baseline()
        store = self.terminal()
        before = tree(root), tree(self.repo / '.ssdlc/.runtime'), tree(store.root)
        with patch.object(s, 'spawn', side_effect=AssertionError):
            result = s.resume(dict(self.args, entrypoint='plan'), self.clock)
        self.assertEqual(s.exit_code(result), 14)
        self.assertEqual((tree(root), tree(self.repo / '.ssdlc/.runtime')), before[:2])
        after = tree(store.root)
        before[2]['T-1'] = ('dir', sorted([*before[2]['T-1'][1], 'audit.jsonl']))
        audit = 'T-1/audit.jsonl'
        self.assertEqual({k: v for k, v in after.items() if k != audit}, {k: v for k, v in before[2].items() if k != audit})
        self.assertEqual(len((store.root / audit).read_text().splitlines()), 1)
        self.assertTrue(result['next_action'])

    def test_fingerprint_pinning_and_audited_reauthorization(self):
        self.baseline()
        store = self.terminal()
        original = store.read()
        for change in [dict(goal='changed'), dict(tier='S'), dict(test_cmd='true')]:
            with self.subTest(change=list(change)), patch.object(s, 'spawn', side_effect=AssertionError):
                out = s.resume(dict(self.args, entrypoint='run', **change), self.clock)
                self.assertEqual(out['status_reason'], 'fingerprint_mismatch')
                self.assertEqual(store.read(), original)
        with patch.object(s, 'start', return_value={'status': 'running'}) as launch:
            s.resume(dict(self.args, entrypoint='run', goal='changed', reauthorize_fingerprints=True), self.clock)
        a = launch.call_args.args[0]
        self.assertEqual(a['_resumes'], store.run_id)
        self.assertEqual(a['_reauthorization'][0]['fields'], ['goal_sha256'])
        self.assertEqual(json.loads((store.root / 'T-1/audit.jsonl').read_text())['type'], 'reauthorization')
        with patch.dict(os.environ, {'T3_CONFIG_CHANGE': 'yes'}), patch.object(s, 'spawn', side_effect=AssertionError):
            self.assertEqual(s.resume(dict(self.args, entrypoint='run'), self.clock)['status_reason'], 'fingerprint_mismatch')
        with patch.object(s, 'controller_snapshot_digest', return_value='f' * 64), patch.object(s, 'spawn', side_effect=AssertionError):
            self.assertEqual(s.resume(dict(self.args, entrypoint='run'), self.clock)['status_reason'], 'fingerprint_mismatch')

    def test_accepted_resume_new_run_preserves_canonical_state(self):
        self.baseline()
        self.controller('import time\ntime.sleep(30)\n')
        store = self.terminal()
        before = tree(self.repo / '.ssdlc/T-1'), tree(self.repo / '.ssdlc/.runtime')
        result = s.resume(dict(self.args, entrypoint='run', from_phase='03-dev'))
        self.addCleanup(lambda: s.cancel(self.repo, 'T-1', grace=0))
        self.assertEqual(result['handoff']['outcome'], 'launched')
        current = s.RunStore(self.repo, 'T-1').current()
        self.assertNotEqual(current['run_id'], store.run_id)
        self.assertEqual(current['resumes'], store.run_id)
        self.assertEqual(before, (tree(self.repo / '.ssdlc/T-1'), tree(self.repo / '.ssdlc/.runtime')))

    def test_worker_rechecks_compatibility_before_spawn(self):
        store = self.store()
        d = store.read()
        payload = json.dumps(dict(args=s.validate_args(self.args), env=s.build_project_env(self.repo, os.environ))).encode()
        read, write = os.pipe()
        os.write(write, struct.pack('!I', len(payload)) + payload); os.close(write)
        with patch.object(s, 'preflight_loop', return_value=True), patch.object(s, 'compatibility', return_value=dict(ok=False)), patch.object(s, 'spawn', side_effect=AssertionError):
            self.assertEqual(s.worker(store, argparse.Namespace(payload_fd=read, generation=1)), 14)
        self.assertIsNone(store.read()['processes']['controller'])

    def compatible_resume(self, entrypoint):
        (self.repo / '.gitignore').write_text('.ssdlc/\nhome/\ncontroller.py\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.gitignore'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'baseline'], check=True, stdout=subprocess.DEVNULL)
        root = self.baseline()
        state = dict(task_breakdown=True, risk_tier='S', base_commit=s.git_read(self.repo, 'rev-parse', 'HEAD'),
                     gate_history=[dict(gate=g, passed=True) for g in ('PAIR_DESIGN_GATE', 'RECON_BLAST_RADIUS_GATE', 'PO_SPEC_GATE')], artifacts={})
        for rel, content in [('00-recon/quality-contract.json', '{"checks": []}'), ('01-po/requirements.md', 'AC-1: behavior'), ('00-recon/context-and-blast-radius.md', 'approved baseline')]:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            state['artifacts'][rel] = dict(sha256=s.digest(content))
        (root / 'state.json').write_text(json.dumps(state))
        self.args['entrypoint'] = entrypoint
        self.controller()
        old = self.terminal()
        before = tree(root), tree(self.repo / '.ssdlc/.runtime')
        self.assertTrue(o.resume_compatibility(self.repo, 'T-1', entrypoint)['ok'])
        result = s.resume(dict(self.args, review_repairs=2 if entrypoint == 'plan' else None, rounds=4 if entrypoint == 'design' else None))
        self.addCleanup(self.cleanup_run)
        self.assertEqual(result['handoff']['outcome'], 'launched')
        current = s.RunStore(self.repo, 'T-1').current()
        self.assertEqual(current['resumes'], old.run_id)
        self.assertIsNone(current['authorized']['goal_sha256'])
        self.assertIsNone(current['authorized']['goal_len'])
        self.assertEqual(before, (tree(root), tree(self.repo / '.ssdlc/.runtime')))

    def test_compatible_plan_resume_new_run_without_canonical_writes(self):
        self.compatible_resume('plan')

    def test_compatible_design_resume_new_run_without_canonical_writes(self):
        self.compatible_resume('design')


def tree(root):
    if not root.exists():
        return None
    # Include directory listings without timestamp changes caused by audit creation.
    return {str(p.relative_to(root)): ('dir', sorted(x.name for x in p.iterdir())) if p.is_dir()
            else (s.digest(p.read_bytes()), p.stat().st_mtime_ns) for p in [root, *root.rglob('*')]}


class TestScopedCancel(CoreCase):
    def test_both_labels_exact_and_full_id_only(self):
        store, proc = self.fake_run()
        d = store.read()
        docker = FakeDocker()
        matching = dict(id='d' * 64, state='running', labels={'ssdlc.run_id': d['run_id'], 'ssdlc.project': d['project']['key']})
        other_project = dict(id='e' * 64, state='running', labels={'ssdlc.run_id': d['run_id'], 'ssdlc.project': 'other', 'ssdlc.ticket': 'T-1'})
        single_label = dict(id='f' * 64, state='running', labels={'ssdlc.run_id': d['run_id']})
        short = dict(matching, id='a' * 12)
        docker.containers = [matching, other_project, single_label, short]
        # Deliberately return unfiltered candidates: executor independently verifies labels.
        with patch.object(docker, 'list', return_value=docker.containers):
            self.assertEqual(s.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc, docker=docker)['status'], 'cancelled')
        self.assertEqual(docker.stopped, ['d' * 64])
        self.assertEqual(other_project['state'], 'running')

    def test_F14_crash_restart_relists_no_duplicate_stop(self):
        store, proc = self.fake_run()
        d = store.read()
        docker = FakeDocker()
        docker.containers = [dict(id='d'*64, state='running', labels={'ssdlc.run_id': d['run_id'], 'ssdlc.project': d['project']['key']})]
        with self.assertRaises(s.InjectedCrash):
            s.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc, docker=docker, fault=CrashAt('cancel.after_container_stop_before_record'))
        self.assertEqual(docker.stopped, ['d'*64])
        self.assertEqual(s.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc, docker=docker)['status'], 'cancelled')
        self.assertEqual(docker.stopped, ['d'*64])

    def test_unrelated_and_recycled_processes_are_not_signalled(self):
        store, proc = self.fake_run()
        proc.add(3333, sid=3333, pgid=2002, nonce='z'*32)
        leader = store.read()['processes']['controller']
        proc.add(leader['pid'], sid=leader['sid'], ticks=leader['start_ticks']+1, nonce='z'*32)
        result = s.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc, docker=FakeDocker())
        self.assertNotIn(3333, [pid for pid, _ in proc.signals])
        self.assertNotIn(2002, [pid for pid, _ in proc.signals])
        self.assertIn(result['status'], {'blocked', 'cancelled'})

    def test_real_docker_argv_filters_inspects_and_stops_full_id(self):
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            out = ('d'*64 + '\n').encode() if argv[1] == 'ps' else json.dumps([dict(Id='d'*64, Name='ignored', Config=dict(Labels={'ssdlc.run_id': 'r', 'ssdlc.project': 'p'}), State=dict(Status='running'))]).encode()
            return subprocess.CompletedProcess(argv, 0, out)
        with patch.object(s.subprocess, 'run', side_effect=command):
            adapter = s.DockerObservation()
            for c in adapter.list('r', 'p'):
                adapter.stop(c, 3)
        self.assertEqual(calls[0], ['docker', 'ps', '-aq', '--filter', 'label=ssdlc.run_id=r', '--filter', 'label=ssdlc.project=p'])
        self.assertEqual(calls[-1], ['docker', 'stop', '-t', '3', 'd'*64])


class TestSecretsAndBoundaryT3(CoreCase):
    baseline = TestResumeAndArgv.baseline
    terminal = TestResumeAndArgv.terminal
    # Reuse fixture helpers without rediscovering the parent test methods below.
    def test_sentinels_refused_accepted_cancel_and_controller_events(self):
        sentinel = 'SENTINEL_SECRET_T3_78a4'
        self.args.update(goal=sentinel + ' ; rm -rf inert', test_cmd='printf ' + sentinel)
        self.baseline()
        self.controller('import time\ntime.sleep(30)\n')
        with patch.dict(os.environ, {'T3_PRIVATE': sentinel}):
            store = self.terminal()
            canonical = tree(self.repo / '.ssdlc/T-1'), tree(self.repo / '.ssdlc/.runtime')
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                denied = s.resume(dict(self.args, entrypoint='plan', payload=sentinel), self.clock)
                self.assertEqual(s.exit_code(denied), 14)
                accepted = s.resume(dict(self.args, entrypoint='run', from_phase='03-dev', payload=sentinel))
                self.assertEqual(accepted['handoff']['outcome'], 'launched')
                current = s.RunStore(self.repo, 'T-1'); current.current()
                with patch.dict(os.environ, hook_environment(current)):
                    o._supervision_event('failure', category=sentinel, exc_type='RuntimeError')
                    o._supervision_event('dispatch_started', dispatch_id='d'*32, engine='codex', task_id=sentinel + '/bad', attempt=1)
                    o._supervision_event('failure', category='TASK_EXECUTION', exc_type='RuntimeError', output=sentinel)
                    o._supervision_event('failure', category='TASK_EXECUTION', exc_type='RuntimeError')
                    o._supervision_event('run_ended', exit_code=0)
                docker = FakeDocker()
                d = current.read()
                docker.containers = [dict(id='d'*64, name=sentinel, state='running', labels={'ssdlc.run_id': d['run_id'], 'ssdlc.project': d['project']['key'], 'private': sentinel})]
                s.cancel(self.repo, 'T-1', grace=0, docker=docker)
            self.assertEqual(canonical, (tree(self.repo / '.ssdlc/T-1'), tree(self.repo / '.ssdlc/.runtime')))
            for path in store.root.rglob('*'):
                if path.is_file():
                    self.assertNotIn(sentinel.encode(), path.read_bytes(), str(path))
            self.assertNotIn(sentinel, output.getvalue() + errors.getvalue())
            events = s.controller_events(current, current.read())[0]
            self.assertEqual([e['type'] for e in events], ['failure', 'run_ended'])


class TestDocs(unittest.TestCase):
    def test_required_sections_limits_and_no_github_adapter(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / 'README.md').read_text()
        for text in ['foreground', 'start --ticket', 'status --ticket', 'cancel --ticket', 'resume --ticket',
                     'Recovery matrix', 'single-host', 'Linux', 'auth_unverified', 'no shipped native probe',
                     '--test-cmd', 'run_ended', 'unkeyed', 'code_defect', 'operator resupply', '#16', '#1', '#4', '#14']:
            self.assertIn(text, readme)
        milestone = (root / 'PLAN_TASK_ROUTING_AND_SUBGOALS.md').read_text()
        self.assertIn('T3', milestone)
        self.assertIn('No GitHub adapter', milestone)
        self.assertFalse(any('github' in name.lower() for name in dir(s)))


class TestCrashRecoveryT3(CoreCase):
    def test_F21_worker_killed_large_output_adoption_requires_intent_bound_run_ended(self):
        # Target-repository setup is confined to the temporary test fixture.
        (self.repo / '.gitignore').write_text('.ssdlc/\nhome/\ncontroller.py\nrelease\nready\n')
        (self.repo / 'app.py').write_text('value = 0\n')
        for command in [('add', '.gitignore', 'app.py'), ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')]:
            subprocess.run(['git', '-C', str(self.repo), *command], check=True)
        base = s.git_read(self.repo, 'rev-parse', 'HEAD')
        canonical = self.repo / '.ssdlc/T-1'
        canonical.mkdir(parents=True)
        wt = canonical / 'worktree'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(wt), base], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (canonical / 'state.json').write_text(json.dumps(dict(status='AWAITING_SUPERVISOR_SIGNOFF', base_commit=base, task_verified_head=base)))
        toolkit = str(Path(o.__file__).resolve().parent)
        self.controller("import os,sys,time\nfrom pathlib import Path\n" +
            f"sys.path.insert(0, {toolkit!r})\nfrom orchestrator import _supervision_event\n" +
            f"release=Path({str(self.repo / 'release')!r})\n" +
            f"Path({str(self.repo / 'ready')!r}).write_text('ready')\n" +
            "while not release.exists():\n os.write(1,b'x'*65536)\n os.write(2,b'y'*65536)\n time.sleep(.001)\n_supervision_event('run_ended',exit_code=0)\n")
        result = s.start(self.args)
        self.addCleanup(self.cleanup_run)
        self.assertEqual(result['handoff']['outcome'], 'launched')
        store = s.RunStore(self.repo, 'T-1'); d = store.current()
        deadline = time.monotonic() + 3
        while not (self.repo / 'ready').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.repo / 'ready').exists())
        leader = d['processes']['worker']
        launch = s.intent_for(d, 'worker')
        self.assertEqual(s.verify(leader, d['nonce'], 'worker', launch['generation'], intent_id=launch['intent_id'], run_id=d['run_id']), s.OWNED)
        os.kill(leader['pid'], signal.SIGKILL)
        deadline = time.monotonic() + 3
        while s.identity_state(store.read(), 'worker') == s.OWNED and time.monotonic() < deadline:
            time.sleep(.01)
        # This real-helper test controls only its two child processes. A shared
        # host can contain unrelated same-uid non-dumpable processes; the production
        # all-process scan intentionally refuses adoption in that environment.
        proc = s.Proc()
        proc.pids = lambda: [leader['pid'], d['processes']['controller']['pid']]
        adopted = s.restart_worker(store, proc=proc)
        self.assertEqual(adopted['status'], 'running', (adopted, s.identity_state(store.read(), 'controller'), s.identity_state(store.read(), 'worker')))
        self.assertEqual(store.read()['worker_generation'], 2)
        (self.repo / 'release').write_text('go')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            outcome = s.status(self.repo, 'T-1')
            if outcome['status'] == 'completed' and store.read()['exit_evidence']['source'] == 'run_ended': break
            time.sleep(.02)
        self.assertEqual(outcome['status'], 'completed')
        self.assertEqual(store.read()['exit_evidence']['source'], 'run_ended')
        event_file = store.root / store.path('controller-events.jsonl')
        record = json.loads(event_file.read_text())
        self.assertEqual(record['gen'], 1)
        self.assertEqual(record['intent'], s.intent_for(store.read(), 'controller')['intent_id'])
        # Even an otherwise valid success from another intent cannot prove completion.
        event_file.write_text(json.dumps(dict(record, intent='f'*32)) + '\n')
        def clear(v): v['exit_evidence'] = dict(source='none', code=None, intent_id=None)
        store.update(clear)
        self.assertEqual(s.status(self.repo, 'T-1')['status_reason'], 'exit_evidence_unavailable')

    def test_F14_real_kill_restart_relists_containers(self):
        store, proc = self.fake_run()
        stopped = self.repo / 'stopped'
        d = store.read()
        class Docker:
            def list(self, run_id, project):
                return [] if stopped.exists() else [dict(id='d'*64, state='running', labels={'ssdlc.run_id': d['run_id'], 'ssdlc.project': d['project']['key']})]
            def stop(self, container, grace):
                with stopped.open('a') as out:
                    out.write(container['id'] + '\n'); out.flush(); os.fsync(out.fileno())
        class Fault:
            def hit(self, point):
                if point == 'cancel.after_container_stop_before_record': os.kill(os.getpid(), signal.SIGKILL)
        pid = os.fork()
        if pid == 0:
            try: s.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc, fault=Fault(), docker=Docker())
            finally: os._exit(99)
        _, code = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(code), -signal.SIGKILL)
        result = s.cancel(self.repo, 'T-1', grace=0, clock=self.clock, proc=proc, docker=Docker())
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(stopped.read_text().splitlines(), ['d'*64])


class TestCompletionObservationSnapshot(CoreCase):
    def test_late_exit_event_cannot_publish_completion_without_its_evidence(self):
        store, proc = self.fake_run()
        del proc.entries[2002]
        ended = dict(type='run_ended', seq=1, exit_code=0)
        # An event arrives between the monitor snapshot and status evaluation.
        # The first tick must not reread it and combine two different snapshots.
        with patch.object(s, 'controller_events', side_effect=[([], 0), ([ended], 0)]) as events, \
                patch.object(s, 'completion', return_value=True):
            first = s.monitor_tick(store, {'SSDLC_EXECUTION_MODE': 'native'}, proc=proc)
            self.assertEqual(events.call_count, 1)
            self.assertEqual(first['status'], 'reconcile')
            self.assertEqual(store.read()['exit_evidence']['source'], 'none')
            second = s.monitor_tick(store, {'SSDLC_EXECUTION_MODE': 'native'}, proc=proc)
        self.assertEqual(second['status'], 'completed')
        evidence = store.read()['exit_evidence']
        self.assertEqual(evidence['source'], 'run_ended')
        self.assertEqual(evidence['code'], 0)
        self.assertEqual(evidence['intent_id'], s.intent_for(store.read(), 'controller')['intent_id'])
        self.assertIsNone(second['classification'])
        self.assertEqual(second['next_action'], 'status')
