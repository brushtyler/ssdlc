import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import EngineRunner
from ssdlc_tasks import TaskError


class AgyAuthTests(unittest.TestCase):
    def test_agy_receives_only_its_project_scoped_writable_oauth_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root/'home'
            repo = root/'repo'
            (repo/'.ssdlc').mkdir(parents=True)
            source = home/'.gemini/antigravity-cli/antigravity-oauth-token'
            source.parent.mkdir(parents=True)
            source.write_text('SYNTHETIC_TOKEN')
            with patch('pathlib.Path.home', return_value=home):
                cmd = EngineRunner._wrap_in_role_container(['agy', 'models'], repo, pass_ai_keys=False, auth_profile='agy')
                verifier = EngineRunner._wrap_in_role_container(['true'], repo, pass_ai_keys=False)
            target = repo/'.ssdlc/.runtime/agy_auth/antigravity-cli/antigravity-oauth-token'
            self.assertEqual(target.read_text(), source.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertIn(str(target.parents[1])+':/root/.gemini:rw', cmd)
            self.assertFalse(any(str(home) in arg for arg in cmd))
            self.assertNotIn(str(target.parents[1])+':/root/.gemini:rw', verifier)
            target.write_text('REFRESHED_COPY')
            self.assertEqual(source.read_text(), 'SYNTHETIC_TOKEN')

    def test_missing_auth_fails_before_interactive_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'.ssdlc').mkdir()
            with patch('pathlib.Path.home', return_value=root/'empty-home'):
                with self.assertRaisesRegex(TaskError, 'authentication is unavailable'):
                    EngineRunner._wrap_in_role_container(['agy', 'models'], root, pass_ai_keys=False, auth_profile='agy')

    def test_agy_adapter_requests_specific_auth_profile(self):
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=True), patch.object(EngineRunner, '_wrap_in_role_container', side_effect=TaskError('stop')) as wrapper:
            with self.assertRaises(TaskError):
                EngineRunner.run_agy('task', Path.cwd())
            self.assertEqual(wrapper.call_args.kwargs['auth_profile'], 'agy')
            self.assertFalse(wrapper.call_args.kwargs['pass_ai_keys'])
