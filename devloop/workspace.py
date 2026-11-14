"""Git worktrees for agent work. The main checkout's working tree is never touched."""

import base64
import os
import shutil
import subprocess
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


def git_config_env(pairs):
    """``GIT_CONFIG_*`` variables that apply ``pairs`` on top of every config file, in order
    (an empty value resets multi-valued keys such as ``credential.helper``)."""
    env = {'GIT_CONFIG_COUNT': str(len(pairs))}
    for i, (key, value) in enumerate(pairs):
        env[f'GIT_CONFIG_KEY_{i}'] = key
        env[f'GIT_CONFIG_VALUE_{i}'] = value
    return env


class Workspace:
    def __init__(self, repo_root, state_dir, base_branch, run=subprocess.run, test_env=None):
        self.repo_root = Path(repo_root)
        self.root = self.repo_root / state_dir / 'worktrees'
        self.base_branch = base_branch
        self._run = run
        self._test_env = test_env

    def _git(self, *args, cwd=None, check=True, stdin=None, env=None, unset=()):
        kwargs = {}
        if env or unset:
            inherited = {k: v for k, v in os.environ.items() if k not in unset}
            kwargs['env'] = dict(inherited, **(env or {}))
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
        # -z: without it git quotes unusual names ("\303\251", tabs, newlines), which would hide
        # them from a prefix check.
        out = self._git('diff', '--name-only', '-z', '--no-renames', span, cwd=path)
        return [name for name in out.split('\0') if name]

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

        With ``auth`` (``{url, token}``) the push goes over HTTPS with that token only, and the
        token travels in the environment of this one command, never in a config file or on the
        command line. No other credential can take part: URL rewrites are refused, only the
        HTTPS transport is allowed, and credential helpers, askpass programs and preconfigured
        authentication headers are disabled."""
        env, unset, remote = None, (), 'origin'
        if auth:
            remote = auth['url']
            self._refuse_rewrites(path, remote)
            basic = base64.b64encode(f'x-access-token:{auth["token"]}'.encode()).decode()
            # Git applies the http.<url>.* entries of the most specific URL that matches and
            # drops the others, whatever their order. Keyed on the exact push URL, the reset and
            # the header win over every config file (a tie goes to the later entry, and these
            # come last); a less specific key would lose to, say, a repository-level
            # http.<this url>.extraHeader. Credential helpers are reset by the plain key: git
            # applies every matching credential.* entry in order.
            env = git_config_env([
                ('credential.helper', ''),
                (f'http.{remote}.extraHeader', ''),
                (f'http.{remote}.extraHeader', f'AUTHORIZATION: basic {basic}')])
            env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': 'false', 'SSH_ASKPASS': 'false',
                        'GIT_ALLOW_PROTOCOL': 'https', 'GIT_SSH_COMMAND': 'false'})
            # `git -c` settings inherited from a parent git are applied after GIT_CONFIG_*.
            unset = ('GIT_CONFIG_PARAMETERS',)
        self._git('push', *(['--force'] if replace else []), remote,
                  f'HEAD:refs/heads/{branch}', cwd=path, env=env, unset=unset)

    def _refuse_rewrites(self, path, url):
        """A ``url.<base>.insteadOf`` or ``pushInsteadOf`` rule matching ``url`` could send the
        push elsewhere, for example over SSH with a personal key."""
        out = self._git('config', '--null', '--get-regexp', r'^url\..*\.(push)?insteadof$',
                        cwd=path, check=False)
        for entry in out.split('\0'):
            key, _, prefix = entry.partition('\n')
            if prefix and url.startswith(prefix):
                raise WorkspaceError(f'git config {key} rewrites {url}: refusing to push with '
                                     'credentials other than the app token')

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
