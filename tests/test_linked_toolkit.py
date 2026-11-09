from unittest.mock import patch

from orchestrator import EngineRunner
from ssdlc_tasks import git
from test_task_bootstrap import RepositoryTests


class LinkedToolkitTests(RepositoryTests):
    def test_linked_controller_git_metadata_is_mounted_read_only(self):
        toolkit = self.worktree()
        common = git(toolkit, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        with patch('orchestrator.__file__', str(toolkit/'orchestrator.py')):
            cmd = EngineRunner._wrap_in_role_container(['python3', '-V'], self.repo, pass_ai_keys=False)
        mounts = [cmd[i+1] for i, value in enumerate(cmd[:-1]) if value == '-v']
        self.assertIn(f'{common}:{common}:ro', mounts)
        self.assertNotIn(f'{common}:{common}:rw', mounts)
        self.assertEqual(mounts.count(f'{common}:{common}:ro'), 1)
