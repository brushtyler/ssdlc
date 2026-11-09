import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import EngineRunner, ProjectRuntimeIsolation, SquadDispatcher


class ClaudeAuthTests(unittest.TestCase):
    def test_activation_copies_credentials_into_isolated_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"
            source = home / ".claude" / ".credentials.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({"claudeAiOauth": {"accessToken": "test-token"}}))
            repo.mkdir()
            with patch("pathlib.Path.home", return_value=home), patch.dict(
                os.environ, {"SSDLC_INHERIT_HOST_CLAUDE_AUTH": "true"}
            ):
                isolation = ProjectRuntimeIsolation(repo)
                isolation.activate()
                target = isolation.claude_home / ".credentials.json"
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_account_metadata_alone_does_not_enable_claude(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": tmp, "CLAUDE_CODE_OAUTH_TOKEN": ""}
        ), patch("shutil.which", return_value="/path/to/claude"):
            (Path(tmp) / ".claude.json").write_text("{}")
            self.assertFalse(SquadDispatcher.has_claude_max())
            (Path(tmp) / ".credentials.json").write_text("{}")
            self.assertTrue(SquadDispatcher.has_claude_max())

    def test_role_container_receives_oauth_token_only_for_ai_calls(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}
        ):
            repo = Path(tmp)
            (repo / ".ssdlc").mkdir()
            for enabled in (True, False):
                cmd = EngineRunner._wrap_in_role_container(
                    ["claude", "--version"], repo, pass_ai_keys=enabled
                )
                self.assertEqual("CLAUDE_CODE_OAUTH_TOKEN=test-token" in cmd, enabled)

    def test_non_root_permission_error_on_restricted_paths_is_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"
            home.mkdir()
            repo.mkdir()
            with patch("pathlib.Path.home", return_value=home), \
                 patch("os.geteuid", return_value=1000, create=True):
                # When non-root, restricted paths must not raise PermissionError
                isolation = ProjectRuntimeIsolation(repo)
                isolation.activate()
                # Verify dispatcher doesn't raise PermissionError
                SquadDispatcher.has_codex_auth()
                SquadDispatcher.has_agy()


if __name__ == "__main__":
    unittest.main()
