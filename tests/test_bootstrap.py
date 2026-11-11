import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestrator
import ssdlc_bootstrap as bootstrap
import ssdlc_supervisor as supervisor
from ssdlc_tasks import PlanError, TaskPlanValidator, digest, git


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.run_git('config', 'user.email', 'test@example.invalid')
        self.run_git('config', 'user.name', 'Test')
        (self.repo / '.gitignore').write_text('.ssdlc/\n')
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'initial')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo / '.ssdlc').mkdir()
        self.profile = dict(schema_version='1.0', profile_id='python-unittest', window_tasks=1)
        self.write_profile(self.profile)

    def run_git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)

    def write_profile(self, value):
        (self.repo / '.ssdlc/bootstrap-profile.json').write_text(json.dumps(value))

    def snapshot(self):
        return {str(p.relative_to(self.repo)): p.read_bytes() if p.is_file() else None
                for p in (self.repo / '.ssdlc').rglob('*')}

    def contract_orchestrator(self):
        orch = orchestrator.SSDLCOrchestrator.__new__(orchestrator.SSDLCOrchestrator)
        orch.repo_root = self.repo
        orch.ssdlc_root = self.repo / '.ssdlc/T'
        orch.dir_recon = orch.ssdlc_root / '00-recon'
        orch.dir_recon.mkdir(parents=True)
        orch.state = dict(base_commit=self.base)
        orch.record_artifact = lambda *args: None
        return orch

    def test_binding_and_contract(self):
        binding = bootstrap.decide(self.repo, self.base, bootstrap.load_profile(self.repo))
        self.assertEqual(binding['profile_sha256'], digest(self.profile))
        self.assertEqual(binding['policy_sha256'], bootstrap.policy_sha256('python-unittest'))
        self.assertEqual(binding['initial_tree'], git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        orch = self.contract_orchestrator()
        with patch.object(orchestrator.EngineRunner, 'run_sandboxed_cmd', side_effect=AssertionError('baseline ran')):
            contract = orch._ensure_quality_contract()
        self.assertEqual(contract['baseline'], [])
        self.assertEqual(contract['bootstrap'], binding)
        self.assertEqual(binding['initial_commit'], self.base)
        self.assertEqual(binding['baseline_status'], 'absent')
        self.assertEqual(contract['checks'], bootstrap.profile_checks('python-unittest'))
        validator = TaskPlanValidator(self.repo, 'AC-1', contract)
        validator._validate_quality()
        for field, value in [('initial_commit', '0' * 40), ('baseline_status', 'passed')]:
            with self.subTest(field=field):
                contract['bootstrap'] = {**binding, field: value}
                with self.assertRaisesRegex(PlanError, 'Baseline must cover'):
                    validator._validate_quality()
        contract['bootstrap'] = binding
        (self.repo / '.ssdlc/bootstrap-profile.json').write_text('broken')
        orch.state['artifacts'] = {'00-recon/quality-contract.json': {'sha256': orchestrator.sha256_file(orch.dir_recon / 'quality-contract.json')}}
        self.assertEqual(orch._ensure_quality_contract(), contract)

    def test_no_profile_strict(self):
        (self.repo / '.ssdlc/bootstrap-profile.json').unlink()
        self.assertIsNone(bootstrap.load_profile(self.repo))
        self.assertEqual(orchestrator._discover_checks(self.repo), [])
        orch = self.contract_orchestrator()
        with self.assertRaisesRegex(PlanError, 'No project checks discovered'):
            orch._ensure_quality_contract()
        self.assertFalse((orch.dir_recon / 'quality-contract.json').exists())

    def assert_refused(self, reason):
        before = self.snapshot()
        result = orchestrator.startup_preflight(self.repo, 'T', 'run')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], reason)
        for suffix in ([], ['--detach']):
            self.assertEqual(orchestrator.main(['run', '--repo', str(self.repo), '--ticket', 'T', '--goal', 'test'] + suffix), 24)
        args = dict(repo=str(self.repo), ticket='T', goal='test')
        with patch.object(supervisor.RunStore, 'prepare', side_effect=AssertionError('supervisor wrote')):
            for entrypoint in (supervisor.start, supervisor.resume):
                result = entrypoint(args)
                self.assertEqual(result['status_reason'], 'bootstrap_ineligible')
                self.assertEqual(supervisor.exit_code(result), 24)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.repo / '.ssdlc/T/00-recon/quality-contract.json').exists())

    def test_dirty(self):
        (self.repo / 'new.py').write_text('x=1')
        self.assert_refused('dirty_tree')

    def test_existing_tests(self):
        (self.repo / 'app.spec.js').write_text('test')
        self.run_git('add', '.')
        self.run_git('commit', '--amend', '--no-edit', '-q')
        self.assert_refused('existing_tests')

    def test_two_commits(self):
        (self.repo / 'app.py').write_text('x=1')
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'second')
        self.assert_refused('not_initial_commit')

    def test_tree_mismatch(self):
        real_git = bootstrap.git

        def changed_tree(repo, *args):
            if args == ('rev-parse', self.base + '^{tree}'):
                return '0' * 40
            return real_git(repo, *args)

        with patch.object(bootstrap, 'git', side_effect=changed_tree):
            self.assert_refused('tree_mismatch')

    def test_discovered_checks(self):
        (self.repo / 'package.json').write_text(json.dumps({'scripts': {'test': 'true'}}))
        self.run_git('add', '.')
        self.run_git('commit', '--amend', '--no-edit', '-q')
        self.assert_refused('existing_tests')

    def test_execution_boundary_rechecks_eligibility(self):
        self.assertTrue(orchestrator.startup_preflight(self.repo, 'T', 'run')['ok'])
        orch = self.contract_orchestrator()
        (self.repo / 'new.py').write_text('x=1')
        with self.assertRaises(bootstrap.BootstrapError) as raised:
            orch._ensure_quality_contract()
        self.assertEqual(raised.exception.code, 'dirty_tree')
        self.assertFalse((orch.dir_recon / 'quality-contract.json').exists())

    def test_pre_execution_resume_refuses_before_writes(self):
        root = self.repo / '.ssdlc/T'
        root.mkdir()
        (root / 'state.json').write_text(json.dumps(dict(task_engine_v1=True, base_commit=self.base)))
        (self.repo / 'new.py').write_text('x=1')
        self.assert_refused('dirty_tree')

    def test_registered_pytest_profile(self):
        self.profile['profile_id'] = 'python-pytest'
        self.write_profile(self.profile)
        binding = bootstrap.decide(self.repo, self.base, bootstrap.load_profile(self.repo))
        self.assertEqual(binding['policy_sha256'], bootstrap.policy_sha256('python-pytest'))
        self.assertEqual(bootstrap.profile_checks('python-pytest')[0]['argv'],
                         ['python3', '-m', 'pytest', 'tests'])

    def test_policy_conflict(self):
        (self.repo / '.ssdlc/quality-contract.json').write_text('{}')
        self.assert_refused('policy_conflict')

    def test_invalid_profiles(self):
        for value in ({**self.profile, 'unknown': True}, {**self.profile, 'window_tasks': 0},
                      {**self.profile, 'profile_id': 'custom'}, []):
            with self.subTest(value=value):
                self.write_profile(value)
                self.assert_refused('not_registered' if isinstance(value, dict) and value.get('profile_id') == 'custom' else 'profile_invalid')
        (self.repo / '.ssdlc/bootstrap-profile.json').write_text('{')
        self.assert_refused('profile_invalid')

    def test_count(self):
        self.assertEqual(bootstrap.positive_test_count('', 'Ran 2 tests in 0.01s'), 2)
        self.assertEqual(bootstrap.positive_test_count('=== 3 passed in 0.1s ===', ''), 3)
        self.assertEqual(bootstrap.positive_test_count('no tests ran', ''), 0)
        self.assertIsNone(bootstrap.positive_test_count('', ''))
