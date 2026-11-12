"""Thin wrapper over the `gh` CLI. All GitHub state devloop relies on goes through here."""

import json
import re
import subprocess

_RUN_ID = re.compile(r'/actions/runs/(\d+)')


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo, run=subprocess.run):
        self.repo = repo
        self._run = run

    def _gh(self, *args, stdin=None, check=True):
        done = self._run(['gh', *args], input=stdin, capture_output=True, text=True, timeout=300)
        if check and done.returncode != 0:
            raise GitHubError(f'gh {" ".join(args[:3])}: {done.stderr.strip()[:500]}')
        return done

    def _json(self, *args):
        return json.loads(self._gh(*args).stdout or 'null')

    # Reads ---------------------------------------------------------------
    def issues_with_label(self, label):
        return self._json('issue', 'list', '-R', self.repo, '--state', 'open', '--label', label,
                          '--limit', '200', '--json', 'number,title,labels')

    def issue(self, number):
        return self._json('issue', 'view', str(number), '-R', self.repo,
                          '--json', 'number,title,body,labels,comments,state')

    def last_labeler(self, number, label):
        """Login of whoever most recently added ``label`` to issue ``number``."""
        done = self._gh('api', '--paginate', f'repos/{self.repo}/issues/{number}/events',
                        '--jq', f'.[] | select(.event == "labeled" and .label.name == {json.dumps(label)})'
                                ' | .actor.login')
        logins = done.stdout.split()
        return logins[-1] if logins else None

    def open_prs(self):
        return self._json('pr', 'list', '-R', self.repo, '--state', 'open', '--limit', '200',
                          '--json', 'number,title,body,labels,headRefName,headRefOid,author,'
                                    'isCrossRepository')

    def pr_comments(self, number):
        return self._json('pr', 'view', str(number), '-R', self.repo, '--json', 'comments')['comments']

    def checks(self, number):
        """``(state, failed_names, failed_run_ids)``; state is pass, fail, pending or none."""
        done = self._gh('pr', 'checks', str(number), '-R', self.repo, '--json', 'name,bucket,link',
                        check=False)
        try:
            checks = json.loads(done.stdout)
        except json.JSONDecodeError:
            if 'no checks' in done.stderr.lower():
                return 'none', [], []
            raise GitHubError(f'gh pr checks {number}: {done.stderr.strip()[:500]}')
        return summarize_checks(checks)

    def failed_log(self, run_id, max_lines=80):
        done = self._gh('run', 'view', str(run_id), '-R', self.repo, '--log-failed', check=False)
        return '\n'.join(done.stdout.splitlines()[-max_lines:])

    # Writes --------------------------------------------------------------
    def edit_labels(self, number, add=(), remove=()):
        args = ['issue', 'edit', str(number), '-R', self.repo]
        for label in add:
            args += ['--add-label', label]
        for label in remove:
            args += ['--remove-label', label]
        if add or remove:
            self._gh(*args)

    def comment(self, number, body):
        self._gh('issue', 'comment', str(number), '-R', self.repo, '--body-file', '-', stdin=body)

    def create_pr(self, *, base, head, title, body, labels=()):
        args = ['pr', 'create', '-R', self.repo, '--base', base, '--head', head,
                '--title', title, '--body-file', '-']
        for label in labels:
            args += ['--label', label]
        return self._gh(*args, stdin=body).stdout.strip()

    def ensure_label(self, name, color, description):
        self._gh('label', 'create', name, '-R', self.repo, '--color', color,
                 '--description', description, '--force')


def summarize_checks(checks):
    buckets = [c.get('bucket') for c in checks]
    if not checks:
        return 'none', [], []
    failed = [c for c in checks if c.get('bucket') in ('fail', 'cancel')]
    if failed:
        run_ids = sorted({m.group(1) for c in failed if (m := _RUN_ID.search(c.get('link') or ''))})
        return 'fail', [c['name'] for c in failed], run_ids
    if any(b == 'pending' for b in buckets):
        return 'pending', [], []
    return 'pass', [], []
