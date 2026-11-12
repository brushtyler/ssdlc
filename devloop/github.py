"""Thin wrapper over the `gh` CLI. All GitHub state devloop relies on goes through here.

Checks, labels, comments and pull request creation use the REST API through `gh api`, which
behaves the same across `gh` versions. Porcelain commands are used only for reads whose `--json`
output is long-standing (`issue list/view`, `pr list/view`).
"""

import json
import re
import subprocess
from urllib.parse import quote

_RUN_ID = re.compile(r'/actions/runs/(\d+)')
_FAILED_CONCLUSIONS = {'failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure',
                       'stale'}
_FAILED_STATUSES = {'failure', 'error'}


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

    def _json_lines(self, *args):
        return [json.loads(line) for line in self._gh(*args).stdout.splitlines() if line.strip()]

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

    def pr_head(self, number):
        """The PR's current head commit, read fresh."""
        return self._json('api', f'repos/{self.repo}/pulls/{number}')['head']['sha']

    def checks(self, sha):
        """``(state, failed_names, failed_run_ids)`` for commit ``sha``.

        ``state`` is pass, fail, pending or none. Both check runs (Actions) and commit statuses
        count.
        """
        runs = self._json_lines('api', '--paginate', f'repos/{self.repo}/commits/{sha}/check-runs',
                                '--jq', '.check_runs[] | {name, status, conclusion, details_url}')
        statuses = self._json('api', f'repos/{self.repo}/commits/{sha}/status')
        return summarize_checks(normalize_checks(runs, statuses.get('statuses') or []))

    def failed_log(self, run_id, max_lines=80):
        done = self._gh('run', 'view', str(run_id), '-R', self.repo, '--log-failed', check=False)
        return '\n'.join(done.stdout.splitlines()[-max_lines:])

    # Writes --------------------------------------------------------------
    def edit_labels(self, number, add=(), remove=()):
        if add:
            args = ['api', '-X', 'POST', f'repos/{self.repo}/issues/{number}/labels']
            for label in add:
                args += ['-f', f'labels[]={label}']
            self._gh(*args)
        for label in remove:
            done = self._gh('api', '-X', 'DELETE',
                            f'repos/{self.repo}/issues/{number}/labels/{quote(label, safe="")}',
                            check=False)
            # Removing a label that is not there is a no-op, not an error.
            if done.returncode != 0 and 'Label does not exist' not in done.stdout + done.stderr:
                raise GitHubError(f'remove label {label} from #{number}: {done.stderr.strip()[:500]}')

    def comment(self, number, body):
        self._gh('api', '-X', 'POST', f'repos/{self.repo}/issues/{number}/comments',
                 '-F', 'body=@-', stdin=body)

    def create_pr(self, *, base, head, title, body, labels=()):
        created = json.loads(self._gh(
            'api', '-X', 'POST', f'repos/{self.repo}/pulls', '-f', f'title={title}',
            '-f', f'head={head}', '-f', f'base={base}', '-F', 'body=@-', stdin=body).stdout)
        self.edit_labels(created['number'], add=labels)
        return created['html_url']

    def ensure_label(self, name, color, description):
        self._gh('label', 'create', name, '-R', self.repo, '--color', color,
                 '--description', description, '--force')


def normalize_checks(check_runs, statuses):
    """Check runs and commit statuses as ``{name, bucket, link}`` (bucket: pass, fail, pending)."""
    checks = []
    for run in check_runs:
        if run.get('status') != 'completed':
            bucket = 'pending'
        elif run.get('conclusion') in _FAILED_CONCLUSIONS:
            bucket = 'fail'
        else:
            bucket = 'pass'
        checks.append({'name': run.get('name'), 'bucket': bucket, 'link': run.get('details_url')})
    for status in statuses:
        state = status.get('state')
        bucket = 'fail' if state in _FAILED_STATUSES else 'pending' if state == 'pending' else 'pass'
        checks.append({'name': status.get('context'), 'bucket': bucket,
                       'link': status.get('target_url')})
    return checks


def summarize_checks(checks):
    if not checks:
        return 'none', [], []
    failed = [c for c in checks if c.get('bucket') == 'fail']
    if failed:
        run_ids = sorted({m.group(1) for c in failed if (m := _RUN_ID.search(c.get('link') or ''))})
        return 'fail', [c['name'] for c in failed], run_ids
    if any(c.get('bucket') == 'pending' for c in checks):
        return 'pending', [], []
    return 'pass', [], []
