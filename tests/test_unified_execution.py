"""Unified execution through real entry points with offline provider boundaries."""
import ast
import copy
import contextlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
import itertools
from unittest.mock import patch

import orchestrator as o
from ssdlc_tasks import PlanError, TaskError, TaskJournal, atomic_json, git
import test_task_bootstrap as fixtures
from test_full_checkpoint_review import framed


class UnifiedExecutionTests(fixtures.RepositoryTests):
    make_orchestrator = fixtures.OrchestratorBootstrapTests.make_orchestrator
    seed_inputs = fixtures.OrchestratorBootstrapTests.seed_inputs

    def setUp(self):
        super().setUp()
        # Simulate the role boundary and an immutable image, retaining real
        # candidate capture, isolated workspaces and scheduler checks.
        runtime = patch.object(o.EngineRunner, '_should_spawn_role_container', return_value=True)
        runtime.start()
        self.addCleanup(runtime.stop)
        environment = patch.object(o.SSDLCOrchestrator, '_verification_environment', return_value={
            'mode': 'role_container', 'platform': 'fixture',
            'image_id': 'sha256:' + '1' * 64, 'reuse_eligible': True})
        environment.start()
        self.addCleanup(environment.stop)

    def new_run(self, flag=None, repair=False):
        tests = self.repo / 'tests'
        tests.mkdir()
        (tests / 'test_app.py').write_text('import unittest\nclass TestApp(unittest.TestCase):\n    def test_value(self): self.assertTrue(True)\n')
        git(self.repo, 'add', 'tests/test_app.py')
        git(self.repo, 'commit', '-m', 'test convention')
        orch = self.make_orchestrator()
        orch.state['specialists_triggered'] = ['APPSEC_SUPPLY_CHAIN_REDTEAM']
        orch._save_state()
        planned = copy.deepcopy(self.plan)
        planned['base_commit'] = orch.state['base_commit']
        planned['tasks'][0]['verification']['quality_check_refs'] = ['CHECK-TESTS']
        calls = []
        developers = []
        def dispatch(role, *args, **kwargs):
            return 'codex', ('reviewer' if role.endswith('sec') else 'planner'), 'low', 'fixture'
        def invoke(engine, model, system, user, *args, **kwargs):
            phase = args[-1]
            calls.append(phase)
            if phase == '00-recon':
                out = 'Small scoped change\nRISK_TIER: S\nDEV_PROFILE: ALGORITHMIC'
            elif phase == '01-po':
                out = 'AC-1: Correct behavior\nSEC-1: Secure behavior\n'
            elif phase == '02-design-task-plan':
                self.assertIn('Tier S requires exactly one plan node', system)
                candidate = copy.deepcopy(planned)
                if repair and calls.count(phase) == 1:
                    candidate['tasks'].append(self.task('T2', [], [], ['other.py']))
                    candidate['tasks'][1]['verification']['quality_check_refs'] = ['CHECK-TESTS']
                out = json.dumps(candidate)
            elif phase == '02-design-task-plan-review':
                out = json.dumps(dict(verdict='APPROVED', findings=[]))
            else:
                gate = {'04-review': 'review', '05-qa': 'qa', '05b-appsec': 'appsec'}[orch.state['current_phase']]
                out = framed(gate, orch)
            return out, 0, 0, 0, 'fixture'
        def develop(prompt, **kwargs):
            stack = inspect.stack()
            self.assertTrue(any(f.function in ('_run', '_execute_task') and isinstance(f.frame.f_locals.get('self'), o.TaskScheduler) for f in stack))
            developers.append(prompt)
            (kwargs['cwd'] / 'app.py').write_text('value = 1\n')
            return 'TASK_DEV_STATUS: COMPLETE', 0
        def check(argv, *args, **kwargs):
            return ('1000' if argv == ['id', '-u'] else 'Ran 1 test\nOK'), '', 0
        env = dict(os.environ, SSDLC_EXECUTION_MODE='local', GIT_AUTHOR_DATE='2026-01-01T00:00:00Z', GIT_COMMITTER_DATE='2026-01-01T00:00:00Z')
        env.pop('SSDLC_ENABLE_TASK_BREAKDOWN', None)
        if flag is not None:
            env['SSDLC_ENABLE_TASK_BREAKDOWN'] = flag
        invocation_ids = itertools.count(1)
        with patch('ssdlc_tasks.uuid.uuid4', side_effect=lambda: uuid.UUID(int=next(invocation_ids))), \
             patch.dict(os.environ, env, clear=True), patch.object(o, '_OBSOLETE_FLAG_WARNED', False), \
             patch.object(o, 'SSDLCOrchestrator', return_value=orch), \
             patch.object(o.SquadDispatcher, 'dispatch_role', side_effect=dispatch), \
             patch.object(o.SquadDispatcher, 'resolve_review_models', return_value=(['codex:reviewer'], 'low')), \
             patch.object(o.EngineRunner, 'invoke_role', side_effect=invoke), \
             patch.object(o.EngineRunner, 'run_codex', side_effect=develop), \
             patch.object(o.EngineRunner, 'run_sandboxed_cmd', side_effect=check), \
             patch.object(orch, '_run_appsec_scans', return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()):
            logs = io.StringIO()
            import logging
            handler = logging.StreamHandler(logs)
            logger = logging.getLogger(o.__name__)
            logger.addHandler(handler)
            try:
                self.assertEqual(o._foreground_main(['run', '--repo', str(self.repo), '--ticket', 'TKT-1', '--goal', 'Scoped change', '--tier', 'S', '--no-sync-remote', '--test-cmd', 'printf shell-smoke']), 0)
            finally:
                logger.removeHandler(handler)
        self.assertEqual(len(developers), 1)
        self.assertTrue((orch.dir_design / 'plan.json').is_file())
        self.assertEqual(len(json.loads((orch.dir_design / 'plan.json').read_text())['tasks']), 1)
        journal = (orch.dir_dev / 'tasks/task-journal.jsonl').read_bytes()
        state, sequence, previous = TaskJournal._replay(journal)
        self.assertEqual(state['tasks']['T1']['status'], 'COMPLETED')
        self.assertGreater(sequence, 1)
        self.assertTrue(any(g['gate'].startswith('CODE_REVIEW') and g['passed'] for g in orch.state['gate_history']))
        self.assertTrue(any(g['gate'].startswith('QA_') and g['passed'] for g in orch.state['gate_history']))
        self.assertTrue(any(g['gate'].startswith('APPSEC_') and g['passed'] for g in orch.state['gate_history']))
        quality = json.loads((orch.dir_recon / 'quality-contract.json').read_text())
        self.assertTrue(all(c['candidate_proof'] for c in quality['checks'] if c['required']))
        self.assertNotIn('printf shell-smoke', json.dumps(quality['checks']))
        qa = (orch.dir_qa / 'deterministic-checks-a1.md').read_text()
        self.assertIn('shell_command', qa)
        self.assertNotIn('equivalent', qa)
        self.assertIn('shell_command', (orch.ssdlc_root / 'EVIDENCE_PACK.md').read_text())
        return journal, logs.getvalue(), calls

    def test_default_new_run_uses_scheduler_and_all_gates(self):
        _, warning, _ = self.new_run()
        self.assertEqual(warning, '')

    def test_tier_s_two_nodes_are_repaired_before_dispatch(self):
        _, _, calls = self.new_run(repair=True)
        self.assertEqual(calls.count('02-design-task-plan'), 2)
        self.assertEqual(calls.count('02-design-task-plan-review'), 1)

    def test_obsolete_values_have_identical_journals_and_one_bounded_warning(self):
        journals = []
        # Recreate the same disposable path so protected Git pointers are identical.
        with patch.dict(os.environ, {'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z'}):
            for flag in ('false', '0', 'true', '1', 'garbage'):
                with self.subTest(flag=flag):
                    import shutil
                    shutil.rmtree(self.repo)
                    self.repo.mkdir()
                    git(self.repo, 'init')
                    git(self.repo, 'config', 'user.name', 'SSDLC Test')
                    git(self.repo, 'config', 'user.email', 'ssdlc@example.invalid')
                    (self.repo / '.gitignore').write_text('.ssdlc/\n')
                    (self.repo / 'app.py').write_text('value = 0\n')
                    git(self.repo, 'add', '.gitignore', 'app.py')
                    git(self.repo, 'commit', '-m', 'baseline')
                    self.base = git(self.repo, 'rev-parse', 'HEAD')
                    self.plan['base_commit'] = self.base
                    self.quality['base_commit'] = self.base
                    (self.artifacts / '02-design').mkdir(parents=True)
                    self.log.write_text('Ran 1 test\nOK')
                    journal, warning, _ = self.new_run(flag)
                    journals.append(journal)
                    self.assertEqual(warning.strip(), 'obsolete-flag name=SSDLC_ENABLE_TASK_BREAKDOWN action=ignored')
        self.assertTrue(all(j == journals[0] for j in journals))

    def test_registered_historical_contract_bytes_are_unchanged(self):
        orch = self.make_orchestrator()
        self.seed_inputs(orch)
        path = orch.dir_recon / 'quality-contract.json'
        original = path.read_bytes()
        with patch.object(o.EngineRunner, 'run_sandboxed_cmd') as checks:
            actual = orch._ensure_quality_contract()
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn('candidate_proof', actual['checks'][0])
        checks.assert_not_called()

    def test_historical_scheduler_resume_preserves_contract_and_journal_prefix(self):
        orch = self.make_orchestrator()
        self.seed_inputs(orch)
        quality = orch.dir_recon / 'quality-contract.json'
        original = quality.read_bytes()
        def develop(prompt, **kwargs):
            (kwargs['cwd'] / 'app.py').write_text('value = 1\n')
            return 'TASK_DEV_STATUS: COMPLETE', 0
        with patch.object(o.SquadDispatcher, 'dispatch_role', return_value=('codex', 'fixture', 'low', 'fixture')), \
             patch.object(o.EngineRunner, 'run_codex', side_effect=develop) as provider, \
             patch.object(o.EngineRunner, 'run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)):
            orch._run_task_development()
            path = orch.dir_dev / 'tasks/task-journal.jsonl'
            prefix = path.read_bytes()
            ledger = copy.deepcopy(orch.state['ledger'])
            orch._run_task_development()
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(original, quality.read_bytes())
        self.assertTrue(path.read_bytes().startswith(prefix))
        self.assertEqual(ledger, orch.state['ledger'])
        self.assertFalse((orch.dir_design / 'adr.md').exists())
        state, _, _ = TaskJournal._replay(path.read_bytes())
        self.assertEqual(state['tasks']['T1']['attempts'], 1)

    def test_reject_records_no_execution_authority(self):
        orch = self.make_orchestrator()
        with patch.object(o, 'SSDLCOrchestrator', return_value=orch), \
             patch.object(orch, '_run_task_development') as developer, \
             patch.object(o.EngineRunner, 'invoke_role') as provider, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(o._foreground_main(['reject', 'TKT-1', '--repo', str(self.repo), '--reason', 'please repair']), 0)
        developer.assert_not_called()
        provider.assert_not_called()
        self.assertFalse(orch.state['gate_history'][-1]['passed'])
        self.assertEqual(orch.state['gate_history'][-1]['gate'], 'SUPERVISOR_SIGNOFF')
        self.assertIn('Free text authorizes no repair', output.getvalue())

    def test_too_many_candidate_checks_fail_before_baseline_or_sealing(self):
        policy = self.repo / '.ssdlc/quality-contract.json'
        quality = copy.deepcopy(self.quality)
        quality['checks'] = [dict(quality['checks'][0], check_id=f'CHECK-{i}') for i in range(33)]
        atomic_json(policy, quality)
        orch = self.make_orchestrator()
        with patch.object(o.EngineRunner, 'run_sandboxed_cmd') as checks:
            with self.assertRaisesRegex(PlanError, '32 candidate checks'):
                orch._ensure_quality_contract()
        checks.assert_not_called()
        self.assertFalse((orch.dir_recon / 'quality-contract.json').exists())

    def test_shared_context_preserves_absent_and_empty_artifacts(self):
        root = self.artifacts
        context = o.build_task_context(root)
        self.assertEqual(set(context.values()), {o.OPTIONAL_FALLBACK})
        (root / '02-design/adr.md').write_text('')
        self.assertEqual(o.build_task_context(root)['02-design/adr.md'], '')
        self.assertEqual(inspect.signature(o.SSDLCOrchestrator._read_optional).parameters['fallback'].default, o.OPTIONAL_FALLBACK)

    def test_native_development_entrypoint_enforces_persisted_limits(self):
        orch = self.make_orchestrator()
        limits = orch.ssdlc_root / 'limits'
        limits.mkdir()
        # Partial or corrupt authorization must stop the actual native wrapper
        # before it can enter the task engine or dispatch a developer.
        atomic_json(limits / 'policy.json', {})
        with patch.object(orch, '_run_task_development') as engine:
            with self.assertRaises(TaskError):
                orch.run_phase_dev()
        engine.assert_not_called()

    def test_no_mode_switch_or_legacy_executor_remains(self):
        source = Path(o.__file__).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertNotIn(node.name, ('_task_mode', '_run_phase_dev_legacy'))
            if isinstance(node, ast.If):
                self.assertFalse(any(isinstance(n, ast.Name) and n.id == 'task_mode' for n in ast.walk(node.test)))
        wrapper = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'run_phase_dev')
        self.assertEqual([a.arg for a in wrapper.args.args], ['self', 'recover_protocol_task'])
        self.assertEqual([n.attr for n in ast.walk(wrapper) if isinstance(n, ast.Attribute)], ['_run_task_development'])
        self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_supervised_phase' for n in wrapper.decorator_list))
        pipeline = inspect.getsource(o.SSDLCOrchestrator._run_pipeline)
        self.assertIn('self.run_phase_dev()', pipeline)
        warning = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_warn_obsolete_task_flag')
        for number, line in enumerate(source.splitlines(), 1):
            if 'SSDLC_ENABLE_TASK_BREAKDOWN' in line:
                self.assertTrue(warning.lineno <= number <= warning.end_lineno)


class SetupSmokeTests(unittest.TestCase):
    def test_supported_setup_creates_usable_local_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, home = root / 'project', root / 'home'
            project.mkdir()
            home.mkdir()
            script = Path(o.__file__).with_name('setup-ssdlc.sh')
            result = subprocess.run(['bash', str(script), '--init-project', str(project), '--id', 'smoke-project', '--mode', 'local', '--sync-remote', 'false'], env={'PATH': os.defpath, 'HOME': str(home)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = subprocess.run(['bash', '-c', 'source "$1"; printf "%s\\n" "$SSDLC_PROJECT_ID" "$SSDLC_EXECUTION_MODE" "$SSDLC_SYNC_REMOTE" "${SSDLC_ENABLE_TASK_BREAKDOWN-unset}"', 'smoke', str(project / '.ssdlc/project.env')], env={'PATH': os.defpath, 'HOME': str(home)}, capture_output=True, text=True)
            self.assertEqual(config.returncode, 0, config.stderr)
            self.assertEqual(config.stdout.splitlines(), ['smoke-project', 'local', 'false', 'unset'])
            self.assertTrue((project / 'ssdlc').is_file())
            self.assertFalse(list(home.iterdir()))
