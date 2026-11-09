import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import EngineRunner
from ssdlc_tasks import ResourceLimitError, TaskError


class ContainerResourceTests(unittest.TestCase):
    def test_pristine_toolkit_does_not_require_nested_mountpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root/'repo'
            (repo/'.ssdlc').mkdir(parents=True)
            toolkit = root/'toolkit'
            toolkit.mkdir()
            with patch('orchestrator.__file__', str(toolkit/'orchestrator.py')):
                cmd = EngineRunner._wrap_in_role_container(['true'], repo, pass_ai_keys=False)
            self.assertNotIn('/opt/ssdlc/.ssdlc:rw,size=16m', cmd)
            self.assertFalse((toolkit/'.ssdlc').exists())

    def test_cli_container_calls_accept_string_cwd_and_record_resources(self):
        for engine in ('codex', 'claude'):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root/'.ssdlc').mkdir()
                toolkit = root/'toolkit'
                toolkit.mkdir()
                def invoke(cmd, **kwargs):
                    name = cmd[cmd.index('--name')+1]
                    marker = '__SSDLC_RESOURCE_' + name + '__ '
                    stdout = 'QA_STATUS: PASSED' if engine == 'codex' else json.dumps({'result': 'QA_STATUS: PASSED'})
                    return subprocess.CompletedProcess(cmd, 0, stdout, marker+'pids.events.max=0\n')
                with patch('orchestrator.__file__', str(toolkit/'orchestrator.py')), patch.object(EngineRunner, '_should_spawn_role_container', return_value=True), patch('orchestrator.subprocess.run', side_effect=invoke):
                    result = getattr(EngineRunner, 'run_'+engine)('Review', root)
                self.assertEqual(result[0], 'QA_STATUS: PASSED')
                evidence = list((root/'.ssdlc/.runtime/resources').glob('*.json'))
                self.assertEqual(len(evidence), 1)
                self.assertTrue(json.loads(evidence[0].read_text())['telemetry_available'])

    def test_container_has_init_and_finite_default_pid_limit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'SSDLC_CONTAINER_PIDS_LIMIT': '1024'}):
            repo = Path(tmp)
            (repo/'.ssdlc').mkdir()
            cmd = EngineRunner._wrap_in_role_container(['true'], repo, pass_ai_keys=False)
            self.assertIn('--init', cmd)
            self.assertEqual(cmd[cmd.index('--pids-limit')+1], '1024')
            for value in ('-1', '0', 'unlimited'):
                with patch.dict(os.environ, {'SSDLC_CONTAINER_PIDS_LIMIT': value}), self.assertRaises(TaskError):
                    EngineRunner._wrap_in_role_container(['true'], repo)

    def test_counters_saved_and_removed_from_cli_stderr_before_rm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'.ssdlc').mkdir()
            cmd = EngineRunner._wrap_in_role_container(['true'], root, pass_ai_keys=False)
            name = cmd[cmd.index('--name')+1]
            marker = '__SSDLC_RESOURCE_' + name + '__ '
            stderr = 'original diagnostic\n' + marker + 'pids.events.max=1\n' + marker + 'memory.events.oom_kill=0\n'
            with patch('orchestrator.subprocess.run', return_value=subprocess.CompletedProcess(cmd, 0, 'OK', stderr)):
                result = EngineRunner._run_role_command(cmd, cwd=root, env=None, timeout=10)
            self.assertTrue(result.resource_failure)
            self.assertEqual(result.stderr, 'original diagnostic\n')
            evidence = json.loads(Path(result.resource_evidence).read_text())
            self.assertTrue(evidence['resource_failure'])
            self.assertEqual(evidence['counters']['pids.events.max'], '1')
            self.assertEqual(evidence['exit_code'], 0)

    def test_missing_telemetry_is_not_reported_as_measured_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'.ssdlc').mkdir()
            cmd = EngineRunner._wrap_in_role_container(['true'], root, pass_ai_keys=False)
            with patch('orchestrator.subprocess.run', return_value=subprocess.CompletedProcess(cmd, 137, '', 'killed')):
                result = EngineRunner._run_role_command(cmd, cwd=root, env=None, timeout=10)
            evidence = json.loads(Path(result.resource_evidence).read_text())
            self.assertFalse(evidence['telemetry_available'])
            self.assertEqual(evidence['counters'], {})
            self.assertEqual(result.returncode, 137)

    def test_verification_retries_once_in_new_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'.ssdlc').mkdir()
            calls = []
            def invoke(cmd, **kwargs):
                calls.append((cmd, kwargs))
                result = subprocess.CompletedProcess(cmd, 0, 'OK', '')
                result.resource_failure = len(calls) == 1
                result.resource_evidence = 'first-resource.json'
                return result
            with patch.object(EngineRunner, '_should_spawn_role_container', return_value=True), patch.object(EngineRunner, '_run_role_command', side_effect=invoke):
                out, err, rc = EngineRunner.run_sandboxed_cmd(['true'], root)
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 2)
            names = [cmd[cmd.index('--name')+1] for cmd, _ in calls]
            self.assertNotEqual(*names)
            self.assertIn('first-resource.json', err)
            self.assertLessEqual(calls[1][1]['timeout'], calls[0][1]['timeout'])

    def test_second_resource_failure_never_passes_despite_zero_exit(self):
        result = subprocess.CompletedProcess([], 0, 'OK', '')
        result.resource_failure = True
        result.resource_evidence = 'resource.json'
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), patch.object(EngineRunner, '_run_role_command', return_value=result) as invoke:
            out, err, rc = EngineRunner.run_sandboxed_cmd(['true'], Path.cwd())
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(rc, 125)
        self.assertIn('ENVIRONMENT_RESOURCE', err)

    def test_ai_resource_failure_does_not_trigger_model_correction(self):
        result = subprocess.CompletedProcess([], 0, 'claimed success', '')
        result.resource_failure = True
        result.resource_evidence = 'resource.json'
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), patch.object(EngineRunner, '_run_role_command', return_value=result) as invoke:
            with self.assertRaises(ResourceLimitError):
                EngineRunner.run_codex('task', Path.cwd())
        invoke.assert_called_once()

    def test_timeout_removes_only_the_owned_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'.ssdlc').mkdir()
            cmd = EngineRunner._wrap_in_role_container(['true'], root, pass_ai_keys=False)
            name = cmd[cmd.index('--name')+1]
            with patch('orchestrator.subprocess.run', side_effect=[subprocess.TimeoutExpired(cmd, 10), subprocess.CompletedProcess([], 0, '', '')]) as invoke:
                with self.assertRaises(subprocess.TimeoutExpired):
                    EngineRunner._run_role_command(cmd, cwd=root, env=None, timeout=10)
            self.assertEqual(invoke.call_args_list[-1].args[0], ['docker', 'rm', '-f', name])
            evidence = root/'.ssdlc/.runtime/resources'/(name+'.json')
            self.assertTrue(json.loads(evidence.read_text())['timed_out'])

    def test_timeout_never_exposes_role_argv_in_exception_or_traceback(self):
        import traceback
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = ['docker', 'run', '--name', 'owned-test', '-e', 'API_KEY=private-test-value', 'image']
            original = subprocess.TimeoutExpired(cmd, 10, output=b'partial output', stderr=b'partial diagnostic')
            with patch('orchestrator.subprocess.run', side_effect=[original, subprocess.CompletedProcess([], 0, '', '')]):
                try:
                    EngineRunner._run_role_command(cmd, cwd=root, env=None, timeout=10)
                except subprocess.TimeoutExpired as exc:
                    self.assertEqual(exc.output, b'partial output')
                    self.assertEqual(exc.stderr, b'partial diagnostic')
                    self.assertEqual(exc.cmd, ['ssdlc-role', 'owned-test'])
                    for rendered in (str(exc), repr(exc), traceback.format_exc()):
                        self.assertNotIn('private-test-value', rendered)
                        self.assertNotIn('API_KEY=', rendered)
                else:
                    self.fail('Timeout was swallowed')
