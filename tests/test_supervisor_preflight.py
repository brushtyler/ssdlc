"""Read-only preflight and terminal evidence contract. No inference or host writes."""
import argparse
import contextlib
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import orchestrator
import ssdlc_supervisor as s
from ssdlc_tasks import TaskJournal
from helpers.supervisor_fakes import (FakeClock, FakeProc, FakeDocker, FakeProbe,
                                     FakeRefresh, CrashAt, write_controller_events)


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'project'
        self.repo.mkdir()
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.clock = FakeClock()
        self.host = dict(HOME=str(self.home), PATH='/usr/bin:/bin', SSDLC_EXECUTION_MODE='native',
                         SSDLC_INHERIT_HOST_CODEX_AUTH='false', SSDLC_INHERIT_HOST_CLAUDE_AUTH='false')
        self.env = s.build_project_env(self.repo, self.host)
        self.args = s.validate_args(dict(repo=str(self.repo), ticket='T', goal='goal', tier='L'))
        self.canonical = self.repo / '.ssdlc/T'
        self.canonical.mkdir(parents=True)
        self.runtime = self.repo / '.ssdlc/.runtime'
        self.runtime.mkdir()

    def store(self):
        store = s.RunStore(self.repo, 'T', 'c' * 32, self.clock)
        store.prepare()
        with patch.dict(os.environ, self.host, clear=True):
            record = s.new_record(self.args, store)
        store.create(record)
        return store

    def live(self, controller=True):
        store = self.store()
        proc = FakeProc()
        def setup(d):
            for role, pid in [('worker', 1001), ('controller', 2002)]:
                if role == 'controller' and not controller:
                    continue
                intent = ('a' if role == 'worker' else 'b') * 32
                ident = proc.add(pid, nonce=d['nonce'], role=role, gen=1, intent=intent,
                                 run_id=d['run_id'], ticks=pid)
                d['processes'][role] = asdict(ident)
                d['launches'].append(dict(intent_id=intent, role=role, generation=1,
                                         state='published', at=1000., abandoned_at=None))
            d.update(status='running' if controller else 'starting', status_reason='launched' if controller else 'bootstrap')
        store.update(setup)
        return store, proc

    def event_file(self, store):
        return store.root / store.path('controller-events.jsonl')

    def state(self, **fields):
        (self.canonical / 'state.json').write_text(json.dumps(fields))

    def preflight(self, engines={'codex'}, env=None, **kwargs):
        return s.preflight(self.repo, 'T', env or self.env,
            route_resolver=lambda *a: engines, which=lambda tool, path: '/fake/' + tool, **kwargs)

    @staticmethod
    def snapshot(root):
        if not root.exists():
            return None
        return {str(p.relative_to(root)): ('dir', sorted(x.name for x in p.iterdir())) if p.is_dir()
                else (s.digest(p.read_bytes()), p.stat().st_mtime_ns) for p in [root, *root.rglob('*')]}

    def public_cli(self, store, proc, json_output=True):
        args = argparse.Namespace(subcommand='status', repo=str(self.repo), ticket='T', run_id=store.run_id,
                                  json=json_output, recovery_matrix=False)
        actual = s.status
        with patch.object(s, 'status', side_effect=lambda *a: actual(*a, proc=proc, env=self.env, clock=self.clock)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = s.cli(args, argparse.ArgumentParser())
        return code, json.loads(output.getvalue()) if json_output else output.getvalue()


class TestPreflightScope(Case):
    def explicit(self, engine='codex'):
        env = dict(self.env)
        for key in ('RECON', 'PO', 'ARCH', 'SEC', 'DEV', 'QA', 'APPSEC', 'OPS', 'DOCS'):
            env['SSDLC_' + key + '_ENGINE'] = engine
        env['SSDLC_REVIEW_MODELS'] = engine + ':test'
        return env

    def test_single_provider_no_unused_checks(self):
        env = self.explicit()
        seen = []
        def which(tool, path):
            seen.append(tool)
            return '/fake/' + tool if tool in {'codex', 'git'} else None
        with patch.object(s, 'credential_present', return_value=True):
            result = s.preflight(self.repo, 'T', env, tier='L', which=which)
        self.assertTrue(result['ok'], result)
        self.assertEqual(set(seen), {'codex', 'git'})
        self.assertTrue({'claude', 'openrouter', 'pytest', 'npm'}.isdisjoint(c['id'] for c in result['checks']))

    def test_each_required_dependency_distinct(self):
        (self.repo / '.ssdlc/quality-contract.json').write_text(json.dumps(dict(checks=[
            dict(argv=['python3', '-m', 'pytest']), dict(argv=['npm', 'test'])])))
        env = dict(self.env, SSDLC_EXECUTION_MODE='docker')
        for missing in ('git', 'codex', 'docker', 'pytest', 'npm'):
            with self.subTest(missing=missing):
                result = s.preflight(self.repo, 'T', env, route_resolver=lambda *a: {'codex'},
                    which=lambda tool, path: None if tool == missing else '/fake/' + tool,
                    runner=lambda *a, **kw: subprocess.CompletedProcess(a, 0))
                codes = [c['code'] for c in result['checks'] if c['status'] == 'failed' and not c['id'].startswith('auth_')]
                self.assertEqual(codes, [missing + '_missing'])

    def test_review_panels(self):
        for models, default, expected in [
            ('claude:sonnet,codex:test', 'openrouter', {'claude', 'codex'}),
            ('unqualified', 'openrouter', {'codex', 'openrouter'}),
            ('unqualified', 'codex', {'codex'})]:
            env = dict(self.explicit(), SSDLC_REVIEW_MODELS=models, SSDLC_REVIEW_ENGINE=default)
            self.assertEqual(s.resolve_routes(self.repo, 'run', 'L', env, 'T'), expected)
        self.assertEqual(orchestrator.resolve_review_entry('http://model', 'codex'), ('codex', 'http://model'))

    def test_effective_claude_and_default_review_panel(self):
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        for name in ('claude', 'codex'):
            path = bin_dir / name
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o700)
        cred = self.home / '.claude/.credentials.json'
        cred.parent.mkdir()
        cred.write_text('DO-NOT-COPY')
        env = dict(self.explicit(), PATH=str(bin_dir), SSDLC_INHERIT_HOST_CODEX_AUTH='true')
        env.pop('SSDLC_REVIEW_MODELS')
        env['SSDLC_INHERIT_HOST_CLAUDE_AUTH'] = 'false'
        self.assertEqual(s.resolve_routes(self.repo, 'run', 'L', env, 'T'), {'codex'})
        env['SSDLC_INHERIT_HOST_CLAUDE_AUTH'] = 'true'
        before = self.snapshot(self.home)
        self.assertEqual(s.resolve_routes(self.repo, 'run', 'L', env, 'T'), {'claude', 'codex'})
        self.assertEqual(self.snapshot(self.home), before)
        self.assertFalse((self.runtime / 'claude_home').exists())

    def test_codex_inheritance_predicate_without_file_and_empty_home(self):
        bin_dir = self.repo / 'bin'
        bin_dir.mkdir()
        path = bin_dir / 'codex'
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o700)
        env = dict(self.explicit(), PATH=str(bin_dir), SSDLC_INHERIT_HOST_CODEX_AUTH='true')
        env.pop('SSDLC_REVIEW_MODELS')
        before = self.snapshot(self.home)
        self.assertEqual(s.resolve_routes(self.repo, 'run', 'L', env, 'T'), {'codex'})
        self.assertEqual(self.snapshot(self.home), before)

    def test_tier_union_and_plan_profiles(self):
        self.state(task_breakdown=True)
        design = self.canonical / '02-design'
        design.mkdir()
        (design / 'plan.json').write_text(json.dumps(dict(tasks=[dict(profile='SECURITY'), dict(profile='FRONTEND_UI')])))
        calls = []
        def dispatch(role, tier, profile, attempt):
            calls.append((role, tier, profile))
            return 'codex', '', '', ''
        with patch.object(orchestrator.SquadDispatcher, 'dispatch_role', side_effect=dispatch), \
             patch.object(orchestrator.SquadDispatcher, 'resolve_review_models', return_value=(['codex:test'], 'low')):
            s.route_child(self.repo, 'run', 'auto', 'T')
        self.assertEqual({p for r, t, p in calls if r == '03-dev'}, {'SECURITY', 'FRONTEND_UI'})
        self.assertEqual({t for r, t, p in calls if r == '03-dev'}, {'S', 'M', 'L'})
        self.assertEqual({t for r, t, p in calls if r == '00-recon'}, {'M'})
        self.assertNotIn('04-review', {r for r, t, p in calls})


class TestPreflightAuthAndIsolation(Case):
    def test_environment_is_pure_and_reserved_names_stripped(self):
        global_env = self.home / '.ssdlc/global.env'
        global_env.parent.mkdir()
        global_env.write_text('A=global\nB=global\nSSDLC_SUPERVISED_NONCE=bad\n')
        (self.repo / '.ssdlc/project.env').write_text('export B="project"\nSECRET=one\nSSDLC_SUPERVISED_RUN_ID=bad\n')
        before = dict(os.environ)
        a = s.build_project_env(self.repo, dict(self.host, A='host', SSDLC_SUPERVISED_ROLE='bad'))
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        b = s.build_project_env(other, self.host)
        self.assertEqual(a['A'], 'host')
        self.assertEqual(a['B'], 'project')
        self.assertNotIn('SECRET', b)
        self.assertFalse(any(k.startswith(s.MARK) for k in a))
        self.assertEqual(dict(os.environ), before)
        expected = s.digest(json.dumps(sorted((k, s.digest(v)) for k, v in a.items() if k not in {'PWD', 'SHLVL', '_'})))
        self.assertEqual(s.config_digest(a), expected)

    def test_auth_presence_missing_unknown_and_native_truth(self):
        self.assertEqual(next(c for c in self.preflight()['checks'] if c['id'] == 'auth_codex')['code'], 'auth_missing')
        auth = Path(self.env['CODEX_HOME']) / 'auth.json'
        auth.parent.mkdir()
        auth.write_text('token')
        self.assertEqual(next(c for c in self.preflight()['checks'] if c['id'] == 'auth_codex')['status'], 'unverified')
        adapter = FakeProbe.adapter(dict(status='ok', code='auth_verified', token='secret'))
        result = self.preflight(probes={'codex': adapter})
        self.assertTrue(result['ok'])
        self.assertEqual(adapter.calls[0][2], 10)
        self.assertNotIn('token', json.dumps(result))
        self.assertEqual(self.preflight({'agy'})['checks'][2]['status'], 'unsupported')

    def test_auth_wait_before_any_launch(self):
        store = self.store()
        result = dict(ok=False, code='auth_expired', checks=[dict(id='auth_codex', status='failed', code='auth_expired')])
        with patch.object(s, 'spawn', side_effect=AssertionError('auth must precede launch')):
            self.assertFalse(s.preflight_loop(store, self.env, 1, lambda *a: result))
        self.assertEqual(store.read()['status'], 'operator_wait')
        self.assertEqual(store.read()['status_reason'], 'auth_expired')
        self.assertEqual(s.exit_code(s.status_fields(store.read(), s.public(store.read()), 1000)), 10)
        self.assertIn('out of band', s.status_fields(store.read(), s.public(store.read()), 1000)['next_action'])

    def test_refresh_only_expired_and_project_confined(self):
        refresh = FakeRefresh.adapter(dict(status='ok', code='auth_verified', token='discard'))
        expired = FakeProbe.adapter(dict(status='failed', code='auth_expired'))
        before = (self.snapshot(self.home), self.snapshot(self.runtime))
        self.assertTrue(self.preflight(probes={'codex': expired}, refresh=refresh)['ok'])
        self.assertEqual(len(refresh.calls), 1)
        self.assertEqual(refresh.calls[0][1], self.env)
        self.assertTrue(all(self.runtime in Path(x).parents for x in refresh.calls[0][2].values()))
        self.preflight(probes={'codex': FakeProbe.adapter(dict(status='failed', code='auth_missing'))}, refresh=refresh)
        self.assertEqual(len(refresh.calls), 1)
        self.assertEqual(before, (self.snapshot(self.home), self.snapshot(self.runtime)))

    def test_resource_codes_and_safe_probe_error(self):
        env = dict(self.env, SSDLC_EXECUTION_MODE='docker')
        result = self.preflight(env=env, runner=lambda *a, **k: subprocess.CompletedProcess(a, 1))
        self.assertIn('docker_unavailable', [c['code'] for c in result['checks']])
        self.repo.chmod(0o500)
        try:
            self.assertIn('path_unwritable', [c['code'] for c in self.preflight()['checks']])
        finally:
            self.repo.chmod(0o700)
        result = self.preflight(disk_usage=lambda p: type('Disk', (), dict(free=0))())
        self.assertIn('disk_low', [c['code'] for c in result['checks']])
        result = self.preflight(probes={'codex': FakeProbe.adapter(error=RuntimeError('SENTINEL'))})
        self.assertNotIn('SENTINEL', json.dumps(result))
        self.assertEqual(s.normalized_probe(dict(status='unknown', code='probe_unavailable', error='SENTINEL')),
                         dict(status='unknown', code='probe_unavailable', summary=s.SUMMARIES['probe_unavailable']))


class TestObservationAndCompletion(Case):
    def test_quiet_is_running_deadline_strict(self):
        store, proc = self.live()
        store.update(lambda d: d['deadlines'].update(hard_deadline=2000., quiet_threshold_sec=10))
        s.monitor_tick(store, self.env, proc)
        self.clock.now = 2000
        out = s.monitor_tick(store, self.env, proc)
        self.assertEqual(out['status'], 'running')
        self.assertIn('suspect_silence', out['diagnostics'])
        self.assertFalse(proc.signals)
        self.assertEqual(store.read()['recovery']['used'], 0)
        self.clock.now += .001
        out = s.monitor_tick(store, self.env, proc)
        self.assertEqual((out['status'], out['status_reason']), ('failed', 'hard_deadline'))
        self.assertEqual(out['classification']['class'], 'limits')

    def test_progress_sources_and_container_scoping(self):
        store, proc = self.live()
        docker = FakeDocker()
        d = store.read()
        labels = {'ssdlc.run_id': d['run_id'], 'ssdlc.project': d['project']['key']}
        docker.containers = [dict(id='d' * 64, labels=labels, name='unsafe'),
                             dict(id='e' * 64, labels=dict(labels, **{'ssdlc.project': 'other'}))]
        self.state(current_phase='03-dev', current_step='raw-output')
        env = dict(self.env, SSDLC_EXECUTION_MODE='docker')
        proc.add(3003, sid=2002, ticks=3003, ppid=2002)
        write_controller_events(self.event_file(store), d, [dict(type='dispatch_started', dispatch_id='e' * 32,
                                                               engine='codex', task_id='T1', attempt=1)])
        out = s.monitor_tick(store, env, proc, docker)
        self.assertEqual(out['phase'], '03-dev')
        self.assertEqual(out['activity'], dict(engine='codex', task_id='T1', attempt=1))
        self.assertEqual(len(store.read()['processes']['descendants']), 3)
        self.assertEqual([c['id'] for c in store.read()['containers']], ['d' * 64])
        self.assertNotIn('unsafe', json.dumps(store.read()))
        self.clock.now += 1
        self.state(current_phase='05-qa')
        self.assertEqual(s.monitor_tick(store, env, proc, docker)['progress']['progress_at'], self.clock.now)

    def complete_fixture(self, kind='plan'):
        store, proc = self.live()
        del proc.entries[2002]
        def setup(d):
            d['base_commit_at_launch'] = 'base'
            d['authorized'].update(entrypoint=kind, argv_id='orchestrator.' + kind)
            d['exit_evidence'] = dict(source='parent_returncode', code=0, intent_id='b' * 32)
        store.update(setup)
        paths = ['02-design/plan.json'] if kind == 'plan' else ['02-design/adr.md', '02-design/threat-model.md']
        artifacts = {}
        for rel in paths:
            path = self.canonical / rel
            path.parent.mkdir(exist_ok=True)
            path.write_text('{}')
            artifacts[rel] = dict(sha256=s.digest(path.read_bytes()))
        self.state(base_commit='base', status='PLAN_READY' if kind == 'plan' else 'DESIGN_READY', artifacts=artifacts)
        return store, proc

    def test_completion_per_entrypoint_exit_binding_and_wrong_commit(self):
        store, proc = self.complete_fixture()
        with patch.object(s, 'git_read', return_value='base'):
            self.assertEqual(s.status(self.repo, 'T', proc=proc, env=self.env)['status'], 'completed')
        with patch.object(s, 'git_read', return_value='other'):
            self.assertEqual(s.status(self.repo, 'T', proc=proc, env=self.env)['status_reason'], 'completion_mismatch')
        store.update(lambda d: d.update(exit_evidence=dict(source='none', code=None, intent_id=None)))
        with patch.object(s, 'git_read', return_value='base'):
            self.assertEqual(s.status(self.repo, 'T', proc=proc, env=self.env)['status_reason'], 'exit_evidence_unavailable')
            write_controller_events(self.event_file(store), store.read(), [dict(type='run_ended', exit_code=0, exc_type=None)])
            self.assertEqual(s.monitor_tick(store, self.env, proc)['status'], 'completed')
        self.assertEqual(store.read()['exit_evidence']['source'], 'run_ended')

    def test_design_and_run_predicates(self):
        store, proc = self.complete_fixture('design')
        with patch.object(s, 'git_read', return_value='base'):
            self.assertEqual(s.status(self.repo, 'T', proc=proc, env=self.env)['status'], 'completed')
        store.update(lambda d: d['authorized'].update(entrypoint='run', argv_id='orchestrator.run'))
        self.state(base_commit='base', status='AWAITING_SUPERVISOR_SIGNOFF', task_verified_head='verified')
        (self.canonical / 'worktree').mkdir()
        def git(repo, *args):
            return '' if args[0] == 'status' else 'base' if Path(repo) == self.repo else 'verified'
        with patch.object(s, 'git_read', side_effect=git):
            self.assertEqual(s.status(self.repo, 'T', proc=proc, env=self.env)['status'], 'completed')
        with patch.object(s, 'git_read', return_value='bad'):
            self.assertNotEqual(s.status(self.repo, 'T', proc=proc, env=self.env)['status'], 'completed')

    def test_completion_crash_re_evaluates_canonical_evidence(self):
        store, proc = self.complete_fixture()
        store.fault = CrashAt('complete.observed_before_state')
        with patch.object(s, 'git_read', return_value='base'):
            with self.assertRaises(s.InjectedCrash):
                s.monitor_tick(store, self.env, proc)
            self.assertNotEqual(store.read()['status'], 'completed')
            self.state(status='FAILED', base_commit='base')
            self.assertNotEqual(s.monitor_tick(store, self.env, proc)['status'], 'completed')


class ClassificationFixtures(Case):
    def fixture(self, case):
        store, proc = self.live()
        del proc.entries[2002]
        tasks = {'T1': dict(status='FAILED', attempts=5, failed_attempts=[dict(attempt=5, category='CHECK_FAILURE',
                                                                           developer_output='SENTINEL', failure='SENTINEL')])}
        baseline = {}
        events = [dict(type='dispatch_started', dispatch_id='d' * 32, engine='codex', task_id='T1', attempt=5),
                  dict(type='dispatch_finished', dispatch_id='d' * 32, outcome='returned')]
        final = dict(type='failure', category='TASK_EXECUTION', exc_type='TaskError')
        status = 'FAILED'
        if case in (1, 2, 3, 4, 7, 8):
            tasks['T1'].update(attempts=6)
            events += [dict(type='dispatch_started', dispatch_id='e' * 32, engine='codex', task_id='T1', attempt=6)]
            if case != 2:
                events += [dict(type='dispatch_finished', dispatch_id='e' * 32, outcome='returned' if case == 7 else 'raised')]
            if case == 4:
                tasks['T1']['status'] = 'COMPLETED'
                events[-2]['task_id'] = 'T2'
            if case == 8:
                baseline['T1'] = 1
            if case != 7:
                final = None
        if case == 6:
            tasks['T1']['failed_attempts'][-1]['category'] = 'REPAIR_STALLED'
            final.update(category='REPAIR_STALLED', exc_type='DevelopmentBlocked')
        if case == 9:
            status = 'HALTED_BUDGET_EXCEEDED'
        if case == 10:
            events = []
            baseline['T1'] = 1
        if case == 11:
            tasks['T1']['failed_attempts'][-1]['category'] = 'DEVELOPER_INVOCATION'
            final.update(category='DEVELOPER_INVOCATION', exc_type='DevelopmentBlocked')
        if case in (12, 13):
            tasks = {}
            events = [dict(type='dispatch_started', dispatch_id='d' * 32, engine='codex'),
                      dict(type='dispatch_finished', dispatch_id='d' * 32, outcome='raised', exc_type='RuntimeError')]
            final = None
        if final:
            events.append(final)
        events.append(dict(type='run_ended', exit_code=1, exc_type='TaskError'))
        directory = self.canonical / '03-dev/tasks'
        directory.mkdir(parents=True)
        TaskJournal(directory).append('ATTEMPT_FAILED', dict(tasks=tasks))
        self.state(status=status, failure=dict(category='TASK_EXECUTION', message='SENTINEL'))
        def setup(d):
            d['journal_baseline'].update(failed_attempts=baseline, taken_at_launch=True)
        store.update(setup)
        write_controller_events(self.event_file(store), store.read(), events)
        self.env['SSDLC_MAX_DEV_RETRIES'] = '5'
        return store, proc


class TestClassification(ClassificationFixtures):
    def test_C1_through_C13_public_status_and_disjointness(self):
        for case in range(1, 14):
            with self.subTest(case=case):
                # Each fixture gets a fresh metadata root and a real hash-chain writer.
                if (self.repo / '.ssdlc/.supervisor').exists():
                    import shutil
                    shutil.rmtree(self.repo / '.ssdlc/.supervisor')
                    shutil.rmtree(self.canonical / '03-dev')
                store, proc = self.fixture(case)
                before = (self.snapshot(self.canonical), self.snapshot(self.runtime), self.snapshot(store.root))
                code, out = self.public_cli(store, proc)
                classification = out['classification']
                cls, row = ('code_defect', '6') if case in (5, 6) else ('limits', '3') if case == 9 else (
                    ('limits', '3c') if case == 10 else ('uncertain_outcome', '5'))
                self.assertEqual((classification['class'], classification['row']), (cls, row))
                self.assertEqual(classification['terminal_bound'], case in (5, 6))
                self.assertTrue(out['next_action'])
                self.assertEqual(code, 10)
                self.assertIsNone(out['recovery']['next_retry_at'])
                evidence = s.evidence_for(store, store.read(), self.env)
                p = s.classification_predicates(evidence)
                self.assertLessEqual(sum(p[k] for k in ('row3', 'row3c', 'row6')), 1)
                self.assertEqual(s.classify(evidence), s.classify(evidence))
                self.assertEqual(before, (self.snapshot(self.canonical), self.snapshot(self.runtime), self.snapshot(store.root)))
                self.assertFalse(proc.signals)

    def test_guards_unknown_failure_open_join_and_legacy(self):
        events = [dict(type='dispatch_started', dispatch_id='a' * 32, seq=1, engine='codex'),
                  dict(type='dispatch_finished', dispatch_id='b' * 32, seq=2, outcome='returned')]
        self.assertEqual(s.terminal_outcome(events).last_dispatch['outcome'], 'open')
        self.assertEqual(s.classify(dict(events=events))['class'], 'uncertain_outcome')
        self.assertEqual(s.classify(dict(events=[], state=dict(status='FAILED')))['row'], '8')
        self.assertEqual(s.classify(dict(exit_code=1))['diagnostics'], [dict(code='U4')])

    def test_priority_rows_and_unsupported_transition(self):
        fixtures = [
            (dict(preflight=dict(checks=[dict(status='failed', code='auth_expired')])), 'authentication', '1'),
            (dict(journal_corrupt=True), 'integrity', '2'),
            (dict(state=dict(status='ESCALATED_TO_SUPERVISOR')), 'operator_decision', '4'),
            (dict(events=[dict(type='failure', seq=1, category='PLAN_ERROR', exc_type='PlanError')],
                  entrypoint='plan', compatibility_approved=True), 'unsupported_transition', '7'),
        ]
        for evidence, cls, row in fixtures:
            with self.subTest(row=row):
                out = s.classify(evidence)
                self.assertEqual((out['class'], out['row']), (cls, row))

    def test_classification_crash_recomputes_C1_C5_C10(self):
        for case in (1, 5, 10):
            if (self.repo / '.ssdlc/.supervisor').exists():
                import shutil
                shutil.rmtree(self.repo / '.ssdlc/.supervisor')
                shutil.rmtree(self.canonical / '03-dev')
            store, proc = self.fixture(case)
            expected = self.public_cli(store, proc)[1]['classification']
            store.fault = CrashAt('classify.before_persist')
            with self.assertRaises(s.InjectedCrash):
                s.monitor_tick(store, self.env, proc)
            fresh = s.RunStore(self.repo, 'T', store.run_id, self.clock)
            self.assertEqual(s.monitor_tick(fresh, self.env, proc)['classification'], expected)


class TestRecoveryAndFaults(Case):
    def transient(self):
        return dict(ok=False, code='docker_unavailable', checks=[dict(id='docker', status='failed', code='docker_unavailable')])

    def test_recovery_persists_before_sleep_and_exhaustion(self):
        store = self.store()
        store.fault = CrashAt('recovery.after_counter_before_sleep')
        with self.assertRaises(s.InjectedCrash):
            s.preflight_loop(store, self.env, 1, lambda *a: self.transient())
        d = store.read()
        self.assertEqual(d['recovery']['used'], 1)
        self.assertEqual(d['recovery']['next_retry_at'], 1001)
        self.assertFalse(self.clock.sleeps)
        fresh = s.RunStore(self.repo, 'T', store.run_id, self.clock)
        self.assertFalse(s.preflight_loop(fresh, self.env, 1, lambda *a: self.transient()))
        d = fresh.read()
        self.assertEqual(d['recovery']['used'], 3)
        self.assertEqual(d['status_reason'], 'recovery_exhausted')
        self.assertEqual(d['status'], 'operator_wait')
        self.assertEqual(self.clock.now, 1007)

    def test_uncertain_never_retries_or_spawns(self):
        store = self.store()
        before = store.read()
        with patch.object(s, 'spawn', side_effect=AssertionError('no retry')):
            self.assertFalse(s.schedule_recovery(store, s.classify({}), 1))
        self.assertEqual(store.read(), before)

    def test_matrix_cli_is_actual_policy(self):
        for json_output in (False, True):
            args = argparse.Namespace(subcommand='status', json=json_output, recovery_matrix=True)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(s.cli(args, argparse.ArgumentParser()), 0)
            if json_output:
                self.assertEqual(json.loads(output.getvalue()), s.RECOVERY_MATRIX)
            else:
                self.assertIn('uncertain_outcome: operator-only', output.getvalue())
        self.assertEqual([k for k, v in s.RECOVERY_MATRIX.items() if v['mode'] == 'auto'], ['transient_environment_provider'])

    def test_event_replay_F10a_F10b(self):
        for point in ('event.after_state_before_append', 'event.after_append_before_clear'):
            if (self.repo / '.ssdlc/.supervisor').exists():
                import shutil
                shutil.rmtree(self.repo / '.ssdlc/.supervisor')
            store = self.store()
            store.fault = CrashAt(point)
            with self.assertRaises(s.InjectedCrash):
                # Stable heartbeat snapshots are not audit events; exercise
                # exactly-once replay on an actual lifecycle transition.
                store.update(lambda d: d.update(status='blocked', status_reason='reconcile'), event='heartbeat')
            fresh = s.RunStore(self.repo, 'T', store.run_id, self.clock)
            fresh.update(lambda d: None)
            self.assertEqual(len(fresh.events()), 1)
            self.assertEqual(fresh.read()['pending_events'], [])

    def test_restart_inputs_before_generation_or_spawn(self):
        store = self.store()
        proc = FakeProc()
        original = store.read()
        for goal, code in ((None, 'inputs_required'), ('changed', 'fingerprint_mismatch')):
            with patch.object(s, 'spawn', side_effect=AssertionError('no spawn')):
                out = s.restart_worker(store, goal, env=self.env, proc=proc)
            self.assertEqual(out['status_reason'], code)
            self.assertEqual(store.read(), original)
        store.fault = CrashAt('adopt.after_generation_bump_before_spawn')
        with self.assertRaises(s.InjectedCrash):
            s.restart_worker(store, 'goal', env=self.env, proc=proc)
        self.assertEqual(store.read()['worker_generation'], 2)
        with self.assertRaises(s.FencedOut):
            store.update(lambda d: None, (1, 0))
        with patch.object(s, 'spawn', return_value=(None, None, 'x')) as spawn:
            s.restart_worker(store, 'goal', env=self.env, proc=proc)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(store.read()['worker_generation'], 3)

    def test_fresh_process_restart_before_and_after_backoff(self):
        store = self.store()
        cls = s.classify(dict(preflight=self.transient()))
        s.schedule_recovery(store, cls, 1, base=10)
        for now, expected in ((1005, 1010), (1020, 1020)):
            clock = FakeClock(now)
            fresh = s.RunStore(self.repo, 'T', store.run_id, clock)
            seen = []
            def success(*args):
                seen.append(clock.time())
                return dict(ok=True, code='preflight_ok', checks=[])
            self.assertTrue(s.preflight_loop(fresh, self.env, 1, success))
            self.assertEqual(seen, [expected])
            self.assertEqual(fresh.read()['recovery']['used'], 1)
        # Independent interpreter proves persisted inputs/counters are authoritative.
        script = "import json,ssdlc_supervisor as s; d=s.status(" + repr(str(self.repo)) + ", 'T'); print(json.dumps(d))"
        p = subprocess.run([sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertEqual(json.loads(p.stdout)['recovery']['used'], 1)


class TestSecretsAndBoundaryT2(ClassificationFixtures):
    def test_sentinel_all_channels_and_absent_journal_boundary(self):
        secret = 'SENTINEL'
        store, proc = self.live()
        before = (self.snapshot(self.canonical), self.snapshot(self.runtime))
        env = dict(self.env, OPENROUTER_API_KEY=secret, SSDLC_OTHER_SECRET=secret)
        result = self.preflight({'openrouter'}, env=env, probes={'openrouter': FakeProbe.adapter(error=RuntimeError(secret))})
        self.assertNotIn(secret, json.dumps(result))
        expired = FakeProbe.adapter(dict(status='failed', code='auth_expired', token=secret))
        refreshed = self.preflight({'openrouter'}, env=env, probes={'openrouter': expired},
                                  refresh=FakeRefresh.adapter(error=RuntimeError(secret)))
        self.assertNotIn(secret, json.dumps(refreshed))
        # A fake controller attempts secret-bearing fields through the reader.
        # The injected stream is in memory; no fake writes a secret into supervisor storage.
        launch = s.intent_for(store.read(), 'controller')
        attempted = [dict(type='failure', category='TASK_EXECUTION', exc_type='TaskError', message=secret),
                     dict(type='dispatch_started', dispatch_id=secret, engine='codex'),
                     dict(type='dispatch_started', dispatch_id='d' * 32, engine='codex', task_id=secret + '!')]
        records = [dict(seq=i, ts=1000., run_id=store.run_id, role='controller', gen=1,
                        intent=launch['intent_id'], **e) for i, e in enumerate(attempted, 1)]
        original_open = s.open_safe
        fd = os.memfd_create('fake-controller-events')
        self.addCleanup(os.close, fd)
        os.write(fd, ''.join(json.dumps(r) + '\n' for r in records).encode())
        def fake_open(root, rel, *args, **kwargs):
            if rel.endswith('controller-events.jsonl'):
                os.lseek(fd, 0, os.SEEK_SET)
                return os.dup(fd)
            return original_open(root, rel, *args, **kwargs)
        with patch.object(s, 'open_safe', side_effect=fake_open):
            out = s.monitor_tick(store, env, proc)
        self.assertIn('controller_events_malformed', out['diagnostics'])
        self.assertFalse((self.canonical / '03-dev/tasks').exists())
        self.assertEqual(before, (self.snapshot(self.canonical), self.snapshot(self.runtime)))
        del proc.entries[2002]
        for mode in (True, False):
            _, output = self.public_cli(store, proc, mode)
            self.assertNotIn(secret, json.dumps(output))
        for path in store.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))
        self.assertFalse((self.canonical / '03-dev/tasks').exists())
        self.assertEqual(before, (self.snapshot(self.canonical), self.snapshot(self.runtime)))

    def test_C1_C13_canonical_snapshot_and_secret_journal_text(self):
        for case in range(1, 14):
            if (self.repo / '.ssdlc/.supervisor').exists():
                import shutil
                shutil.rmtree(self.repo / '.ssdlc/.supervisor')
                shutil.rmtree(self.canonical / '03-dev')
            store, proc = self.fixture(case)
            before = (self.snapshot(self.canonical), self.snapshot(self.runtime))
            s.monitor_tick(store, self.env, proc)
            for mode in (True, False):
                _, output = self.public_cli(store, proc, mode)
                self.assertNotIn('SENTINEL', json.dumps(output))
            self.assertEqual(before, (self.snapshot(self.canonical), self.snapshot(self.runtime)))
            for path in store.root.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'SENTINEL', path.read_bytes())

    def test_fake_controller_stdio_goes_directly_to_devnull(self):
        store = self.store()
        captured = {}
        class Child:
            pid = 1001
            def poll(self):
                return None
        proc = FakeProc()
        proc.add(1001)
        def popen(*args, **kwargs):
            captured.update(kwargs)
            return Child()
        # Stop at publication, before GO, to exercise the actual spawn stdio arguments.
        store.fault = CrashAt('launch.after_identity_before_go')
        with patch.object(s.subprocess, 'Popen', side_effect=popen):
            with store.locked(), self.assertRaises(s.InjectedCrash):
                s.spawn(store, 'worker', 1, proc=proc)
        self.assertEqual([captured[k] for k in ('stdin', 'stdout', 'stderr')], [subprocess.DEVNULL] * 3)
        self.assertNotIn('SENTINEL', json.dumps(store.read()))


if __name__ == '__main__':
    unittest.main()
