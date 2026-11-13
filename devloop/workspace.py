"""Git worktrees for agent work. The main checkout's working tree is never touched."""

import base64
import os
import shutil
import subprocess
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(self, repo_root, state_dir, base_branch, run=subprocess.run, test_env=None):
        self.repo_root = Path(repo_root)
        self.root = self.repo_root / state_dir / 'worktrees'
        self.base_branch = base_branch
        self._run = run
        self._test_env = test_env

    def _git(self, *args, cwd=None, check=True, stdin=None, env=None):
        kwargs = {'env': dict(os.environ, **env)} if env else {}
        try:
            done = self._run(['git', *args], cwd=cwd or self.repo_root, input=stdin,
                             capture_output=True, text=True, timeout=600, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(f'git {" ".join(args[:2])}: timed out after {exc.timeout}s') from exc
        except OSError as exc:
            raise WorkspaceError(f'git could not start: {exc}') from exc
        if check and done.returncode != 0:
            raise WorkspaceError(f'git {" ".join(args[:2])}: {done.stderr.strip()[:500]}')
        return done.stdout

    def path(self, name):
        return self.root / name

    def _fresh(self, path):
        if path.exists():
            self._git('worktree', 'remove', '--force', str(path), check=False)
            if path.exists():
                shutil.rmtree(path)
        self._git('worktree', 'prune')
        path.parent.mkdir(parents=True, exist_ok=True)

    def new_branch(self, name, branch):
        """A worktree on ``branch`` reset to the latest remote base branch."""
        self._git('fetch', 'origin', self.base_branch)
        path = self.path(name)
        self._fresh(path)
        self._git('worktree', 'add', '--force', '-B', branch, str(path),
                  f'origin/{self.base_branch}')
        return path

    def existing_branch(self, name, branch):
        """A worktree on ``branch`` at its pushed head."""
        self._git('fetch', 'origin', self.base_branch,
                  f'+refs/heads/{branch}:refs/remotes/origin/{branch}')
        path = self.path(name)
        self._fresh(path)
        self._git('worktree', 'add', '--force', '-B', branch, str(path), f'origin/{branch}')
        return path

    def remove(self, name):
        path = self.path(name)
        if path.exists():
            self._git('worktree', 'remove', '--force', str(path), check=False)
        self._git('worktree', 'prune', check=False)

    def names(self):
        return sorted(p.name for p in self.root.iterdir()) if self.root.exists() else []

    def head(self, path):
        return self._git('rev-parse', 'HEAD', cwd=path).strip()

    def has_changes(self, path):
        return bool(self._git('status', '--porcelain', cwd=path).strip())

    def diff_stat(self, path):
        return self._git('diff', '--stat', f'origin/{self.base_branch}...HEAD', cwd=path)

    def changed_files(self, path, since=None):
        """Paths added, changed or deleted since commit ``since``, or on the branch with respect
        to the base branch."""
        span = f'{since}..HEAD' if since else f'origin/{self.base_branch}...HEAD'
        out = self._git('diff', '--name-only', '--no-renames', span, cwd=path)
        return [line for line in out.splitlines() if line]

    def commit_all(self, path, message, author=None):
        """Commit everything; ``author`` (``{name, email}``) is both author and committer."""
        env = None
        if author:
            env = {'GIT_AUTHOR_NAME': author['name'], 'GIT_AUTHOR_EMAIL': author['email'],
                   'GIT_COMMITTER_NAME': author['name'], 'GIT_COMMITTER_EMAIL': author['email']}
        self._git('add', '-A', cwd=path)
        self._git('commit', '-F', '-', cwd=path, stdin=message, env=env)
        return self.head(path)

    def push(self, path, branch, *, replace=False, auth=None):
        """Push HEAD; ``replace`` overwrites a stale devloop branch left by an earlier attempt.

        With ``auth`` (``{url, token}``) the push goes over HTTPS with that token only: stored
        credential helpers are disabled and the token travels in the environment of this one
        command, never in a config file or on the command line."""
        env, remote = None, 'origin'
        if auth:
            remote = auth['url']
            # A url.<base>.insteadOf rule could send the push over SSH with a personal key.
            effective = self._git('ls-remote', '--get-url', remote, cwd=path).strip()
            if effective != remote:
                raise WorkspaceError(f'git rewrites {remote} to {effective}: refusing to push '
                                     'with credentials other than the app token')
            basic = base64.b64encode(f'x-access-token:{auth["token"]}'.encode()).decode()
            env = {'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': '2',
                   'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': '',
                   'GIT_CONFIG_KEY_1': 'http.https://github.com/.extraheader',
                   'GIT_CONFIG_VALUE_1': f'AUTHORIZATION: basic {basic}'}
        self._git('push', *(['--force'] if replace else []), remote,
                  f'HEAD:refs/heads/{branch}', cwd=path, env=env)

    def run_tests(self, path, command, timeout):
        """``(passed, output_tail)`` for the project test command in ``path``."""
        kwargs = {'env': self._test_env} if self._test_env is not None else {}
        try:
            done = self._run(['bash', '-c', command], cwd=path, capture_output=True, text=True,
                             timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired:
            return False, f'Test command timed out after {timeout}s'
        tail = '\n'.join((done.stdout + '\n' + done.stderr).strip().splitlines()[-60:])
        return done.returncode == 0, tail
