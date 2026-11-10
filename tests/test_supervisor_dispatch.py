"""Real engine emitters, routing parity, crash windows and terminal evidence."""
import contextlib
import ast
import inspect
import textwrap
import io
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import patch

import orchestrator as o
import ssdlc_supervisor as s
import test_task_bootstrap as bootstrap
from test_supervisor_preflight import Case, ClassificationFixtures
from helpers.supervisor_fakes import hook_environment
from ssdlc_tasks import DevelopmentBlocked, TaskJournal


class TestDispatchHooks(Case):
    def setUp(self):
        super().setUp()
        self.store_, self.proc = self.live()
        self.hooks = patch.dict(os.environ, hook_environment(self.store_))
        self.hooks.start(); self.addCleanup(self.hooks.stop)
        self.engine_patch = patch.object(o.EngineRunner, '_should_spawn_role_container', return_value=False)
        self.engine_patch.start(); self.addCleanup(self.engine_patch.stop)

    def events(self):
        events, malformed = s.controller_events(self.store_, self.store_.read())
        self.assertEqual(malformed, 0)
        return events

    def fake_command(self, argv, **kwargs):
        self.assertEqual(getattr(o._dispatch_local, 'depth', 0), 1)
        self.assertEqual(self.events()[-1]['type'], 'dispatch_started')
        output = json.dumps(dict(result='OK', total_cost_usd=.1)) if 'claude' in argv[0] else 'OK'
        return subprocess.CompletedProcess(argv, 0, output, '')

    def test_unknown_exception_names_preserve_terminal_events_without_raw_names(self):
        for name in ['KeyError', 'JSONDecodeError', 'URLError', 'FileNotFoundError', 'PRIVATE_SENTINEL_CUSTOM_EXCEPTION']:
            o._supervision_event('failure', category='TASK_EXECUTION', exc_type=name)
            o._supervision_event('run_ended', exit_code=1, exc_type=name)
        events = self.events()
        self.assertEqual(len(events), 10)
        self.assertTrue(all(e['exc_type'] == 'Exception' for e in events))
        text = self.event_file(self.store_).read_text()
        self.assertNotIn('PRIVATE_SENTINEL_CUSTOM_EXCEPTION', text)
        self.assertTrue(s.exit_evidence(self.store_.read(), events)['code'] == 1)

    def test_unknown_dispatch_exception_keeps_raised_terminal_binding(self):
        with patch.object(o.EngineRunner, '_run_role_command', side_effect=KeyError('PRIVATE_SENTINEL_MESSAGE')):
            with self.assertRaises(KeyError):
                o.EngineRunner.invoke_role('codex', 'fake', 'sys', 'prompt', self.repo, 'p', 'T', '03-dev')
        events = self.events()
        self.assertEqual([e['type'] for e in events], ['dispatch_started', 'dispatch_finished'])
        self.assertEqual(events[-1]['exc_type'], 'Exception')
        self.assertEqual(events[-1]['outcome'], 'raised')
        self.assertNotIn('PRIVATE_SENTINEL_MESSAGE', self.event_file(self.store_).read_text())

    def test_inventory_and_spawn_inside_dispatch(self):
        expected = {'invoke_role', 'call_openrouter', 'run_agy', 'run_codex', 'run_claude'}
        wrapped = {name for name in vars(o.EngineRunner) if getattr(getattr(o.EngineRunner, name), '_supervised_dispatch', False) is True}
        self.assertEqual(wrapped, expected)
        for name in vars(o.EngineRunner):
            if name.startswith('_') or name == 'run_sandboxed_cmd':
                continue
            method = getattr(o.EngineRunner, name)
            if not callable(method):
                continue
            body = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(method))))
            spawns = [node for node in ast.walk(body) if isinstance(node, ast.Call) and
                      isinstance(node.func, ast.Attribute) and node.func.attr in
                      {'_run_role_command', 'urlopen', 'run', 'Popen', 'call', 'check_call', 'check_output'}]
            if spawns:
                self.assertIs(getattr(method, '_supervised_dispatch', False), True, name)
        self.assertFalse(getattr(o.EngineRunner.run_sandboxed_cmd, '_supervised_dispatch', False))
        with patch.object(o.EngineRunner, '_run_role_command', side_effect=self.fake_command):
            for engine in ('agy', 'codex', 'claude'):
                o.EngineRunner.invoke_role(engine, 'fake', 'system', 'prompt', self.repo, 'p', 'T', '04-review-a1')
        events = self.events()
        self.assertEqual(len(events), 6)
        self.assertEqual({e['engine'] for e in events if e['type'] == 'dispatch_started'}, {'agy', 'codex', 'claude'})
        self.assertTrue(all(e['phase'] == '04-review' for e in events if e['type'] == 'dispatch_started'))

    def test_openrouter_http_and_fallback_reentrancy(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(dict(choices=[dict(message=dict(content='OK'))])).encode()
        def http(req, **kwargs):
            self.assertEqual(getattr(o._dispatch_local, 'depth', 0), 1)
            self.assertIn('/chat/completions', req.full_url)
            return Response()
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'fake'}), patch.object(o.urllib.request, 'urlopen', side_effect=http):
            result = o.EngineRunner.call_openrouter('model', 'sys', 'user', 'p', 'T', '01-po')
            self.assertEqual(result[0], 'OK')
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': ''}), patch.object(o.shutil, 'which', return_value='/fake/claude'), patch.object(o.EngineRunner, '_run_role_command', side_effect=self.fake_command):
            o.EngineRunner.call_openrouter('claude-fake', 'sys', 'user', 'p', 'T', '01-po')
        events = self.events()
        self.assertEqual(len(events), 4)
        self.assertEqual([e['engine'] for e in events if e['type'] == 'dispatch_started'], ['openrouter', 'openrouter'])

    def test_contract_preservation_return_rc_exception_and_context(self):
        for engine in ('codex', 'claude'):
            for rc in (0, 7):
                with self.subTest(engine=engine, rc=rc), patch.object(o.EngineRunner, '_run_role_command', return_value=subprocess.CompletedProcess([], rc, 'raw', '')) as command:
                    with o._dispatch_context(task_id='T1', attempt=3, phase='03-dev'):
                        kwargs = dict(cwd=self.repo, writable_worktree=self.repo)
                        if engine == 'claude': kwargs.update(max_budget_usd=2.3, read_only=False)
                        else: kwargs['sandbox'] = 'workspace-write'
                        result = getattr(o.EngineRunner, 'run_' + engine)('prompt', **kwargs)
                    self.assertEqual(result[1], rc)
                    self.assertEqual(Path(command.call_args.kwargs['cwd']), self.repo)
                    events = self.events()
                    self.assertEqual(events[-2]['task_id'], 'T1')
                    self.assertEqual(events[-2]['attempt'], 3)
                    self.assertEqual(events[-1]['outcome'], 'returned')
                    if engine == 'claude':
                        argv = command.call_args.args[0]
                        self.assertEqual(argv[argv.index('--max-budget-usd') + 1], '2.30')
        error = subprocess.TimeoutExpired(['fake'], 1)
        with patch.object(o.EngineRunner, '_run_role_command', side_effect=error):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                o.EngineRunner.run_codex('prompt', self.repo)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events()[-1]['exc_type'], 'TimeoutExpired')
        with patch.object(o.EngineRunner, '_run_role_command', return_value=subprocess.CompletedProcess([], 7, 'raw', '')):
            with self.assertRaises(RuntimeError):
                o.EngineRunner.invoke_role('claude', 'model', 'sys', 'user', self.repo, 'p', 'T', '01-po')
        self.assertEqual(self.events()[-1]['outcome'], 'raised')

    def test_unsupervised_no_events_and_same_call(self):
        path = self.event_file(self.store_)
        with patch.dict(os.environ, {'SSDLC_SUPERVISED_RUN_ID': ''}), patch.object(o.EngineRunner, '_run_role_command', return_value=subprocess.CompletedProcess([], 7, 'raw', '')):
            self.assertEqual(o.EngineRunner.run_codex('prompt', self.repo), ('raw', 7))
        self.assertFalse(path.exists())

    def test_identifier_whitelist_path_symlinks_and_sequence_under_flock(self):
        for _ in range(2): o._supervision_event('progress', step='running')
        self.assertEqual([e['seq'] for e in self.events()], [1, 2])
        before = self.event_file(self.store_).read_bytes()
        for fields in [dict(prompt='SECRET'), dict(task_id='../SECRET', attempt=1), dict(attempt=0), dict(category='SECRET')]:
            o._supervision_event('progress', **fields)
        self.assertEqual(self.event_file(self.store_).read_bytes(), before)
        outside = self.repo / 'outside'
        outside.write_text('unchanged')
        path = self.event_file(self.store_)
        path.unlink(); path.symlink_to(outside)
        o._supervision_event('progress')
        self.assertEqual(outside.read_text(), 'unchanged')
        with patch.dict(os.environ, {'SSDLC_SUPERVISED_EVENTS': str(outside)}):
            o._supervision_event('progress')
        self.assertEqual(outside.read_text(), 'unchanged')

    def test_failed_start_write_prevents_engine_and_reserved_fields_are_rejected(self):
        with patch.object(s, 'open_safe', side_effect=OSError('private path')), patch.object(o.EngineRunner, '_run_role_command') as command:
            with self.assertRaises(s.SupervisorIntegrityError):
                o.EngineRunner.run_codex('secret prompt', self.repo)
            command.assert_not_called()
        path = self.event_file(self.store_)
        for fields in [dict(run_id='SENTINEL'), dict(seq=99), dict(ts='SENTINEL'), dict(type='SENTINEL')]:
            self.assertFalse(o._supervision_event('failure', category='TASK_EXECUTION', exc_type='TaskError', **fields))
        with patch.dict(os.environ, {'SSDLC_SUPERVISED_RUN_ID': 'SENTINEL'}):
            self.assertFalse(o._supervision_event('progress'))
        self.assertFalse(path.exists())

    def test_concurrent_writers_assign_per_run_sequence_under_flock(self):
        children = []
        for _ in range(4):
            pid = os.fork()
            if pid == 0:
                try:
                    ok = all(o._supervision_event('progress', step='running') for _ in range(8))
                    os._exit(0 if ok else 1)
                finally:
                    os._exit(2)
            children.append(pid)
        for pid in children:
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertEqual([e['seq'] for e in self.events()], list(range(1, 33)))

    def test_main_exit_none_and_bool_preserve_python_exit_semantics(self):
        for value, expected in [(None, 0), (0, 0), (False, 0), (True, 1), (2, 2), ('PRIVATE_EXIT_MESSAGE', 1)]:
            with self.subTest(value=value):
                self.event_file(self.store_).unlink(missing_ok=True)
                error = SystemExit(value)
                with patch.object(o, '_supervised_preflight', return_value=True), patch.object(o, '_foreground_main', side_effect=error):
                    with self.assertRaises(SystemExit) as raised:
                        o.main(['run'])
                self.assertIs(raised.exception, error)
                events = self.events()
                self.assertEqual([e['type'] for e in events], ['run_ended'] if expected == 0 else ['failure', 'run_ended'])
                self.assertEqual(events[-1]['exit_code'], expected)
                self.assertIs(type(events[-1]['exit_code']), int)
                self.assertNotIn('PRIVATE_EXIT_MESSAGE', self.event_file(self.store_).read_text())

    def test_main_exit_return_values_keep_original_return_and_typed_evidence(self):
        for value, expected in [(None, 0), (0, 0), (False, 0), (True, 1), (2, 2), ('PRIVATE_RETURN_MESSAGE', 1)]:
            with self.subTest(value=value):
                self.event_file(self.store_).unlink(missing_ok=True)
                with patch.object(o, '_supervised_preflight', return_value=True), patch.object(o, '_foreground_main', return_value=value):
                    self.assertIs(o.main(['run']), value)
                events = self.events()
                self.assertEqual([e['type'] for e in events], ['run_ended'])
                self.assertEqual(events[-1]['exit_code'], expected)
                self.assertIs(type(events[-1]['exit_code']), int)
                self.assertNotIn('PRIVATE_RETURN_MESSAGE', self.event_file(self.store_).read_text())

    def test_integrity_preflight_before_constructor_and_ended(self):
        with patch.object(o.SSDLCOrchestrator, '__init__', side_effect=AssertionError):
            self.assertEqual(o.main(['run', '--ticket', 'T', '--goal', 'g']), 21)
        self.assertEqual([(e['type'], e.get('category'), e.get('exit_code')) for e in self.events()], [('failure', 'INTEGRITY', None), ('run_ended', None, 21)])
        with patch.object(o, '_supervised_preflight', return_value=True), patch.object(o, '_foreground_main', side_effect=RuntimeError('private')):
            with self.assertRaisesRegex(RuntimeError, 'private'):
                o.main(['design'])
        self.assertEqual(self.events()[-1]['exit_code'], 1)
        self.assertNotIn('private', self.event_file(self.store_).read_text())

    def test_F26_F27_kill_restart_open_dispatch_never_retry(self):
        # SIGKILL prevents exception/finally handlers from manufacturing a finish.
        for point in ('dispatch.after_started_before_engine', 'dispatch.after_engine_before_finished'):
            for kind in ('task-development', 'legacy-development', 'invoke_role'):
                with self.subTest(point=point, kind=kind):
                    path = self.event_file(self.store_)
                    path.unlink(missing_ok=True)
                    pid = os.fork()
                    if pid == 0:
                        try:
                            def fault(name):
                                if name == point: os.kill(os.getpid(), signal.SIGKILL)
                            with patch.object(o, '_supervision_fault', side_effect=fault), patch.object(o.EngineRunner, '_run_role_command', side_effect=self.fake_command):
                                with o._dispatch_context(task_id='T1' if kind == 'task-development' else None, attempt=1 if kind == 'task-development' else None):
                                    if kind == 'invoke_role': o.EngineRunner.invoke_role('codex', 'm', 's', 'u', self.repo, 'p', 'T', '03-dev')
                                    else: o.EngineRunner.run_codex('prompt', self.repo)
                        finally:
                            os._exit(99)
                    _, code = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(code), -signal.SIGKILL)
                    events = self.events()
                    self.assertEqual([e['type'] for e in events], ['dispatch_started'])
                    self.assertEqual(s.terminal_outcome(events).last_dispatch['outcome'], 'open')
                    result = s.classify(dict(events=events))
                    self.assertEqual(result['class'], 'uncertain_outcome')


class DevelopmentCase(bootstrap.RepositoryTests):
    make_orchestrator = bootstrap.OrchestratorBootstrapTests.make_orchestrator
    seed_inputs = bootstrap.OrchestratorBootstrapTests.seed_inputs

    def attach_hooks(self, orch):
        # Metadata belongs to a separate supervisor ticket; canonical task state is untouched.
        store = s.RunStore(self.repo, 'HOOK', 'c'*32)
        store.prepare()
        a = s.validate_args(dict(repo=str(self.repo), ticket='HOOK', goal='goal'))
        store.create(s.new_record(a, store))
        store.update(lambda d: d['launches'].append(dict(role='controller', generation=1, intent_id='b'*32, state='published', at=1000.)))
        return store

    def execute(self, engine, task, rc=0, raised=False):
        orch = self.make_orchestrator()
        orch.state.update(risk_tier='M', dev_profile='ALGORITHMIC')
        if task:
            self.seed_inputs(orch)
            orch.state.update(status='PLAN_READY', task_breakdown=True)
            orch._save_state()
        store = self.attach_hooks(orch)
        def command(argv, **kw):
            self.assertEqual(getattr(o._dispatch_local, 'depth', 0), 1)
            if raised: raise subprocess.TimeoutExpired(['fake'], 1)
            if rc == 0: (Path(kw['cwd']) / 'app.py').write_text('value = 1\n')
            output = json.dumps(dict(result='REVIEW_STATUS: APPROVED')) if engine == 'claude' else 'REVIEW_STATUS: APPROVED'
            return subprocess.CompletedProcess(argv, rc, output, '')
        with patch.dict(os.environ, hook_environment(store)), patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': str(task).lower(), 'SSDLC_DEV_ENGINE': engine, 'SSDLC_MAX_DEV_RETRIES': '1'}), patch.object(o.EngineRunner, '_should_spawn_role_container', return_value=False), patch.object(o.EngineRunner, '_run_role_command', side_effect=command), patch.object(o.EngineRunner, 'run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)), contextlib.redirect_stdout(io.StringIO()):
            try:
                orch.run_phase_dev()
            except (DevelopmentBlocked, subprocess.TimeoutExpired):
                if rc == 0 and not raised: raise
        events, malformed = s.controller_events(store, store.read())
        self.assertEqual(malformed, 0)
        dispatches = [e for e in events if e['type'] == 'dispatch_started']
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]['engine'], engine)
        if task:
            self.assertEqual((dispatches[0]['task_id'], dispatches[0]['attempt']), ('T1', 1))
        finished = [e for e in events if e['type'] == 'dispatch_finished']
        self.assertEqual(finished[0]['outcome'], 'raised' if raised else 'returned')
        return orch, store

    def crash_real_develop(self, task, point):
        pid = os.fork()
        if pid == 0:
            try:
                def crash(name):
                    if name == point: os.kill(os.getpid(), signal.SIGKILL)
                with patch.object(o, '_supervision_fault', side_effect=crash):
                    self.execute('codex', task)
            finally:
                os._exit(99)
        _, code = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(code), -signal.SIGKILL)
        store = s.RunStore(self.repo, 'HOOK', 'c'*32)
        events, malformed = s.controller_events(store, store.read())
        self.assertEqual(malformed, 0)
        self.assertEqual([e['type'] for e in events], ['phase_started', 'dispatch_started'])
        self.assertEqual(s.classify(dict(events=events))['class'], 'uncertain_outcome')
        self.assertIsNone(store.read()['recovery']['next_retry_at'])



class TestRealDevelopmentAndParity(DevelopmentCase):
    def test_real_TaskScheduler_develop_success(self):
        self.execute('codex', True)

    def test_real_TaskScheduler_develop_returned_nonzero(self):
        self.execute('codex', True, rc=7)

    def test_real_TaskScheduler_develop_timeout(self):
        self.execute('codex', True, raised=True)

    def test_real_legacy_success(self):
        self.execute('claude', False)

    def test_real_legacy_nonzero(self):
        self.execute('claude', False, rc=7)

    def test_real_legacy_timeout(self):
        self.execute('claude', False, raised=True)


    def test_F26_real_task_develop_kill_restart(self):
        self.crash_real_develop(True, 'dispatch.after_started_before_engine')

    def test_F26_real_legacy_develop_kill_restart(self):
        self.crash_real_develop(False, 'dispatch.after_started_before_engine')

    def test_F27_real_task_develop_kill_restart(self):
        self.crash_real_develop(True, 'dispatch.after_engine_before_finished')

    def test_F27_real_legacy_develop_kill_restart(self):
        self.crash_real_develop(False, 'dispatch.after_engine_before_finished')


class TestRouteDispatchParity(DevelopmentCase):
    # Only parity methods are discovered in this class.
    def test_task_development_and_task_review_route_parity(self):
        self.parity(task=True, engine='codex')

    def test_task_claude_development_and_task_review_route_parity(self):
        self.parity(task=True, engine='claude')

    def test_legacy_development_and_legacy_review_route_parity(self):
        self.parity(task=False, engine='claude')

    def parity(self, task, engine):
        env = {'SSDLC_DEV_ENGINE': engine, 'SSDLC_REVIEW_MODELS': 'codex:fake,claude:fake',
               'SSDLC_REVIEW_ENGINE': 'openrouter', 'SSDLC_ENABLE_TASK_BREAKDOWN': str(task).lower(),
               'SSDLC_DEV_USE_OPENROUTER': 'false'}
        # Freeze all entrypoint roles so the route inventory has exactly these two engines.
        for roles in s.ROLE_KEYS_BY_ENTRYPOINT.values():
            for role in roles: env['SSDLC_' + role.split('-')[-1].upper() + '_ENGINE'] = engine
        recorder = o.SquadDispatcher.dispatch_role
        roles = []
        def record(role, *args, **kwargs):
            roles.append(role)
            return recorder(role, *args, **kwargs)
        with patch.dict(os.environ, env), patch.object(o.SquadDispatcher, 'dispatch_role', side_effect=record), patch.object(o.urllib.request, 'urlopen', side_effect=AssertionError('unused provider invoked')) as http:
            orch, store = self.execute(engine, task)
            # Task review verifies the actual candidate head in the normal review path.
            if task:
                wt = orch.worktree_dir
                orch.state['task_verified_head'] = o.git(wt, 'rev-parse', 'HEAD')
            def command(argv, **kwargs):
                self.assertEqual(getattr(o._dispatch_local, 'depth', 0), 1)
                value = 'REVIEW_STATUS: APPROVED'
                if task:
                    prompt = kwargs.get('input', '')
                    nonce = re.search(r'BEGIN SSDLC GATE REPORT ([a-f0-9]{16})', prompt)[1]
                    report = dict(schema_version='1.0', reviewed_commit=orch.state['task_verified_head'], source_gate='code_review', findings=[])
                    value = f'-----BEGIN SSDLC GATE REPORT {nonce}-----\n' + json.dumps(report) + f'\n-----END SSDLC GATE REPORT {nonce}-----\n' + value
                return subprocess.CompletedProcess(argv, 0, json.dumps(dict(result=value)) if 'claude' in argv[0] else value, '')
            with patch.dict(os.environ, hook_environment(store)), patch.object(o.EngineRunner, '_should_spawn_role_container', return_value=False), patch.object(o.EngineRunner, '_run_role_command', side_effect=command), contextlib.redirect_stdout(io.StringIO()):
                orch.run_phase_review()
            observed = {e['engine'] for e in s.controller_events(store, store.read())[0] if e['type'] == 'dispatch_started'}
            frozen = s.build_project_env(self.repo, os.environ)
            routes = s.resolve_routes(self.repo, 'run', 'M', frozen, orch.ticket_id)
            self.assertLessEqual(observed, routes)
            self.assertEqual(observed, routes)
            self.assertEqual(routes, {'codex', 'claude'})
            self.assertNotIn('agy', observed | routes)
            self.assertNotIn('openrouter', observed | routes)
            self.assertIn('03-dev', roles)
            child = s.route_child(self.repo, 'run', 'M', orch.ticket_id)
            self.assertLessEqual(set(roles), set(child['routes']))
            http.assert_not_called()


class TestClassificationRealEmitters(ClassificationFixtures):
    def test_C1_C2_C5_C6_C11_C12_C13_status_json_real_emitters(self):
        for case in (1, 2, 5, 6, 11, 12, 13):
            with self.subTest(case=case):
                if (self.repo / '.ssdlc/.supervisor').exists():
                    import shutil
                    shutil.rmtree(self.repo / '.ssdlc/.supervisor')
                    shutil.rmtree(self.canonical / '03-dev')
                store, proc = self.fixture(case)
                path = self.event_file(store)
                expected = [json.loads(x) for x in path.read_text().splitlines()]
                path.unlink()
                # Replay through the production writer (closed schema, seq, intent and fsync).
                with patch.dict(os.environ, hook_environment(store)):
                    index = 0
                    while index < len(expected):
                        event = expected[index]
                        if event['type'] == 'dispatch_started':
                            closed = index + 1 < len(expected) and expected[index + 1]['type'] == 'dispatch_finished'
                            raised = closed and expected[index + 1]['outcome'] == 'raised'
                            def fault(point):
                                if not closed and point == 'dispatch.after_started_before_engine':
                                    raise s.InjectedCrash(point)
                            error = RuntimeError('private engine diagnostic')
                            command = patch.object(o.EngineRunner, '_run_role_command', side_effect=error) if raised else patch.object(o.EngineRunner, '_run_role_command', return_value=subprocess.CompletedProcess([], 0, 'OK', ''))
                            with o._dispatch_context(task_id=event.get('task_id'), attempt=event.get('attempt')), patch.object(o, '_supervision_fault', side_effect=fault), command, patch.object(o.EngineRunner, '_should_spawn_role_container', return_value=False):
                                try: o.EngineRunner.run_codex('private prompt', self.repo)
                                except (RuntimeError, s.InjectedCrash): pass
                            index += 2 if closed else 1
                        else:
                            fields = {k: v for k, v in event.items() if k not in {'type', 'seq', 'ts', 'run_id', 'role', 'gen', 'intent'}}
                            o._supervision_event(event['type'], **fields)
                            index += 1
                self.assertEqual(s.controller_events(store, store.read())[1], 0)
                code, out = self.public_cli(store, proc)
                self.assertEqual(code, 10)
                self.assertEqual(out['classification']['class'], 'code_defect' if case in (5, 6) else 'uncertain_outcome')


class TestFrozenActivationAndPreflight(Case):
    def test_supervised_activation_skips_both_env_loads(self):
        global_env = self.home / '.ssdlc/global.env'
        global_env.parent.mkdir()
        global_env.write_text('PRIVATE=changed\n')
        (self.repo / '.ssdlc/project.env').write_text('PRIVATE=changed\n')
        with patch.dict(os.environ, dict(self.host, SSDLC_SUPERVISED_RUN_ID='a'*32, PRIVATE='frozen'), clear=True), patch.object(o, 'load_env_file', side_effect=AssertionError('frozen environment reloaded')):
            o.ProjectRuntimeIsolation(self.repo).activate()
            self.assertEqual(os.environ['PRIVATE'], 'frozen')

    def test_controller_preflight_keeps_launch_generation_after_worker_adoption(self):
        store, proc = self.live()
        markers = hook_environment(store)
        store.update(lambda d: d.update(worker_generation=2))
        with patch.dict(os.environ, dict(self.env, **markers), clear=True):
            self.assertTrue(o._supervised_preflight())
            with patch.dict(os.environ, {'SSDLC_SUPERVISED_GEN': '2'}):
                self.assertFalse(o._supervised_preflight())
            store.update(lambda d: d.update(launch_revoked=True))
            self.assertFalse(o._supervised_preflight())

    def test_preflight_frozen_snapshot_config_and_currency_no_canonical_write(self):
        store, proc = self.live()
        markers = hook_environment(store)
        before = self.snapshot(self.canonical), self.snapshot(self.runtime)
        with patch.dict(os.environ, dict(self.env, **markers), clear=True):
            self.assertTrue(o._supervised_preflight())
            for fields in [dict(SSDLC_SUPERVISED_CONFIG_SHA='f'*64), dict(SSDLC_SUPERVISED_CONTROLLER_SHA='f'*64), dict(SSDLC_SUPERVISED_GEN='2'), dict(SSDLC_SUPERVISED_INTENT='f'*32)]:
                with patch.dict(os.environ, fields):
                    self.assertFalse(o._supervised_preflight())
            store.update(lambda d: d.update(launch_revoked=True))
            self.assertFalse(o._supervised_preflight())
        self.assertEqual(before, (self.snapshot(self.canonical), self.snapshot(self.runtime)))
