"""The dispatcher: reads GitHub state, plans the next steps, runs them.

State lives on GitHub only (labels plus hidden markers in comments written by
trusted users), so a tick can crash at any point and the next one resumes.

Issue  agent:ready ─► agent:working ─► PR opened
PR     agent:review ─► (CI fail | request_changes) ─► agent:changes ─► agent:review ─► …
                   └─► approve ─► agent:ready-to-merge   (a human merges)
       any step can escalate to agent:needs-human.

Readiness is bound to the approved commit: a PR is promoted only if its head is still the
reviewed commit and CI passed. A ready PR whose head moves, or whose CI is running again, goes
back to review (to agent:changes when CI fails on it).
"""

import string
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agents import AgentError
from .github import GitHubError
from .markdown import (dispatcher_header, publishable, read_marker, render_escalation, render_fix,
                       render_review)
from .review import ci_failure_review, normalize_review, open_findings
from .workspace import WorkspaceError

PROMPT_DIR = Path(__file__).resolve().parent / 'prompts'
EXPENSIVE = ('review', 'fix', 'implement')
LABEL_STYLE = {
    'ready': ('0e8a16', 'devloop: pronta per essere implementata da un agent'),
    'working': ('fbca04', 'devloop: implementazione in corso'),
    'review': ('1d76db', 'devloop: in attesa di review automatica'),
    'changes': ('d93f0b', 'devloop: modifiche richieste dalla review'),
    'ready_to_merge': ('0e8a16', 'devloop: approvata, in attesa del merge umano'),
    'needs_human': ('b60205', 'devloop: serve una decisione umana'),
}


@dataclass
class PullRequest:
    number: int
    issue: int
    head_ref: str
    head_sha: str
    title: str
    body: str
    labels: set
    meta: dict
    reviews: list = field(default_factory=list)
    fixes: list = field(default_factory=list)
    checks: str = 'none'
    failed_checks: list = field(default_factory=list)
    failed_runs: list = field(default_factory=list)


@dataclass
class Snapshot:
    prs: list
    ready_issues: list   # dicts: number, title, labels (set), labeler
    working_issues: list  # issue numbers


@dataclass(frozen=True)
class Action:
    kind: str
    number: int
    reason: str = ''


def label_names(labels):
    return {label['name'] if isinstance(label, dict) else label for label in labels}


def plan(snapshot, cfg):
    """Pure planning: the actions one tick should take, in order."""
    lb = cfg.labels
    actions = []
    pr_issues = {pr.issue for pr in snapshot.prs}
    for number in snapshot.working_issues:
        if number not in pr_issues:
            actions.append(Action('stale', number, 'agent:working without an open devloop PR'))
    for pr in snapshot.prs:
        if lb.needs_human in pr.labels:
            continue
        latest = pr.reviews[-1] if pr.reviews else None
        if lb.ready_to_merge in pr.labels:
            # Readiness belongs to the approved commit: re-check it on every tick.
            approved = latest is not None and latest['verdict'] == 'approve'
            if not approved or latest['head_sha'] != pr.head_sha:
                actions.append(Action('revoke', pr.number, 'head is not the approved commit'))
            elif pr.checks == 'fail':
                actions.append(Action('ci_failed', pr.number, 'CI failed after approval'))
            elif pr.checks == 'pending':
                # CI re-running on the approved commit: not ready until it passes again. The
                # recorded approval is replayed from agent:review, without a new review.
                actions.append(Action('revoke', pr.number, 'CI pending on the approved commit'))
            continue
        if lb.changes in pr.labels:
            actions.append(Action('fix', pr.number))
        elif lb.review in pr.labels:
            answered = latest is not None and any(f['round'] == latest['round'] for f in pr.fixes)
            replay = latest is not None and latest['head_sha'] == pr.head_sha and not answered
            if replay and latest['verdict'] != 'approve':
                actions.append(Action('apply_review', pr.number, 'verdict already recorded'))
            elif pr.checks == 'pending':
                continue
            elif pr.checks == 'fail':
                actions.append(Action('ci_failed', pr.number))
            elif replay:
                actions.append(Action('apply_review', pr.number, 'approval already recorded'))
            else:
                actions.append(Action('review', pr.number))
    free = cfg.max_open_prs - len(snapshot.prs)
    if free > 0:
        blocked = set(cfg.blocked_labels) | {lb.working, lb.needs_human}
        candidates = [i for i in snapshot.ready_issues
                      if not (i['labels'] & blocked) and i['number'] not in pr_issues
                      and i['labeler'] in cfg.trusted_users]
        candidates.sort(key=lambda i: (not (i['labels'] & set(cfg.priority_labels)), i['number']))
        actions += [Action('implement', i['number']) for i in candidates[:free]]
    capped, spent = [], 0
    for action in actions:
        if action.kind in EXPENSIVE:
            if spent >= cfg.max_actions_per_tick:
                continue
            spent += 1
        capped.append(action)
    return capped


def _template(name, **values):
    text = (PROMPT_DIR / f'{name}.md').read_text(encoding='utf-8')
    return string.Template(text).safe_substitute({k: str(v) for k, v in values.items()})


def _format_findings(findings):
    lines = []
    for f in findings:
        where = f' ({f["file"]}{":" + str(f["line"]) if f["line"] > 0 else ""})' if f['file'] else ''
        lines.append(f'- **{f["id"]}**{where}: {f["title"]}\n  {f["detail"]}'
                     + (f'\n  Suggested fix: {f["suggested_fix"]}' if f['suggested_fix'] else ''))
    return '\n'.join(lines) or '(none)'


def format_previous_rounds(reviews, fixes):
    if not reviews:
        return 'This is the first round.'
    parts = []
    for review in reviews:
        parts.append(f'### Round {review["round"]}: {review["verdict"]} on {review["head_sha"][:12]}'
                     f' ({review["source"]})\n\n{review["summary"]}\n\nBlocking findings:\n'
                     + _format_findings(review['blocking_findings']))
        for fix in fixes:
            if fix['round'] == review['round']:
                parts.append('Implementer responses:\n' + '\n'.join(
                    f'- {r["finding_id"]}: {r["action"]}: {r["note"]}' for r in fix['responses']))
    return '\n\n'.join(parts)


class Dispatcher:
    def __init__(self, cfg, gh, ws, runner, log=print):
        self.cfg = cfg
        self.gh = gh
        self.ws = ws
        self.runner = runner
        self.log = log

    # Snapshot ------------------------------------------------------------
    def _trusted_markers(self, comments, kind):
        """Records of ``kind`` from trusted users' comments: at most one per comment."""
        found = []
        for comment in comments:
            if (comment.get('author') or {}).get('login') in self.cfg.trusted_users:
                record = read_marker(comment.get('body', ''), kind)
                if record is not None:
                    found.append(record)
        return found

    def _devloop_pr(self, raw):
        if raw.get('isCrossRepository') or (raw.get('author') or {}).get('login') not in self.cfg.trusted_users:
            return None
        if not raw['headRefName'].startswith(self.cfg.branch_prefix):
            return None
        meta = read_marker(raw.get('body', ''), 'pr')
        if meta is None:
            return None
        return PullRequest(number=raw['number'], issue=meta['issue'],
                           head_ref=raw['headRefName'], head_sha=raw['headRefOid'],
                           title=raw['title'], body=raw.get('body', ''),
                           labels=label_names(raw['labels']), meta=meta)

    def snapshot(self):
        lb = self.cfg.labels
        prs = []
        for raw in self.gh.open_prs():
            pr = self._devloop_pr(raw)
            if pr is None:
                continue
            if pr.labels & {lb.review, lb.changes, lb.ready_to_merge}:
                comments = self.gh.pr_comments(pr.number)
                pr.reviews = sorted(self._trusted_markers(comments, 'review'), key=lambda r: r['round'])
                pr.fixes = self._trusted_markers(comments, 'fix')
            if pr.labels & {lb.review, lb.ready_to_merge}:
                pr.checks, pr.failed_checks, pr.failed_runs = self.gh.checks(pr.head_sha)
            prs.append(pr)
        ready = []
        for raw in self.gh.issues_with_label(lb.ready):
            ready.append({'number': raw['number'], 'title': raw['title'],
                          'labels': label_names(raw['labels']),
                          'labeler': self.gh.last_labeler(raw['number'], lb.ready)})
        working = [raw['number'] for raw in self.gh.issues_with_label(lb.working)]
        return Snapshot(prs=prs, ready_issues=ready, working_issues=working)

    # Tick ----------------------------------------------------------------
    def tick(self, dry_run=False):
        snap = self.snapshot()
        actions = plan(snap, self.cfg)
        prs = {pr.number: pr for pr in snap.prs}
        for action in actions:
            self.log(f'{"[dry-run] " if dry_run else ""}{action.kind} #{action.number}'
                     + (f': {action.reason}' if action.reason else ''))
            if dry_run:
                continue
            handler = getattr(self, f'_do_{action.kind}')
            try:
                handler(prs.get(action.number) or action.number)
            except (AgentError, GitHubError, WorkspaceError) as exc:
                self.log(f'  failed: {exc}')
                self._escalate_failure(action, prs.get(action.number), exc)
        return actions

    # Helpers -------------------------------------------------------------
    def _escalate(self, number, reason, detail='', remove=()):
        self.gh.comment(number, render_escalation(reason, detail))
        self.gh.edit_labels(number, add=[self.cfg.labels.needs_human], remove=remove)

    def _escalate_failure(self, action, pr, exc):
        lb = self.cfg.labels
        log = getattr(exc, 'log', None)
        where = f' Log locale: `{Path(log).name}` in `{self.cfg.state_dir}/logs`.' if log else ''
        reason = f'Il passo `{action.kind}` è fallito: {exc}.{where}'
        try:
            if pr is not None:
                self._escalate(pr.number, reason,
                               remove=[lb.review, lb.changes, lb.ready_to_merge])
            else:
                self._escalate(action.number, reason, remove=[lb.working, lb.ready])
        except GitHubError as gh_exc:
            self.log(f'  could not escalate: {gh_exc}')

    def _issue_context(self, number):
        issue = self.gh.issue(number)
        comments = [c for c in issue.get('comments') or []
                    if (c.get('author') or {}).get('login') in self.cfg.trusted_users]
        rendered = '\n\n'.join(f'### Comment by {c["author"]["login"]}\n\n{c["body"]}' for c in comments)
        return issue, ('## Issue comments\n\n' + rendered) if rendered else ''

    def _write_turn(self, agent, prompt, schema, path, label):
        """Run a writing agent, then the tests; retry with the failures. Returns the output and
        ``None`` on success, or the output and a reason to escalate."""
        for attempt in range(1 + self.cfg.implement_test_retries):
            result = self.runner.run(agent, 'write', prompt, schema, path, f'{label}-{attempt + 1}')
            if result['status'] == 'blocked':
                return result, None
            if not self.ws.has_changes(path) and schema == 'implement':
                return result, ('Implementazione senza modifiche ai file.', '')
            passed, tail = self.ws.run_tests(path, self.cfg.test_cmd, self.cfg.test_timeout_seconds)
            if passed:
                return result, None
            prompt += ('\n\n## Test failures after your previous attempt\n\nThe test command failed. '
                       f'Fix the cause, then finish again.\n\n```text\n{tail}\n```\n')
        return result, ('I test locali falliscono anche dopo i tentativi di correzione.', tail)

    def _commit_message(self, agent, message, issue):
        message = message.strip() + f'\n\nRefs #{issue}\n'
        if agent.commit_trailer:
            message += f'\n{agent.commit_trailer}\n'
        return message

    def _blocked(self, number, agent, reason, remove):
        self.gh.comment(number, publishable(self.runner.header(agent) + '\n## Lavoro bloccato\n\n'
                                            + reason.strip()))
        self.gh.edit_labels(number, add=[self.cfg.labels.needs_human], remove=remove)

    def _approval_current(self, pr, review):
        """Re-read head and CI just before promoting: approval holds only for the reviewed commit
        with CI passed or absent. Otherwise the PR stays in review for the next tick."""
        head = self.gh.pr_head(pr.number)
        if head != review['head_sha']:
            self.log(f'  not promoted: head moved to {head[:12]} after the review')
            return False
        state = self.gh.checks(head)[0]
        if state not in ('pass', 'none'):
            self.log(f'  not promoted: CI is {state}')
            return False
        return True

    def _apply_verdict(self, pr, review, announce):
        lb = self.cfg.labels
        transient = [lb.review, lb.changes, lb.ready_to_merge]
        if review['verdict'] == 'approve':
            if self._approval_current(pr, review):
                self.gh.edit_labels(pr.number, add=[lb.ready_to_merge],
                                    remove=[lb.review, lb.changes])
        elif review['verdict'] == 'needs_human':
            self.gh.edit_labels(pr.number, add=[lb.needs_human], remove=transient)
        elif review['round'] >= self.cfg.max_review_rounds:
            if announce:
                self.gh.comment(pr.number, render_escalation(
                    f'Raggiunto il limite di {self.cfg.max_review_rounds} round di review con '
                    'finding bloccanti ancora aperti.'))
            self.gh.edit_labels(pr.number, add=[lb.needs_human], remove=transient)
        else:
            self.gh.edit_labels(pr.number, add=[lb.changes], remove=[lb.review, lb.ready_to_merge])

    # Actions -------------------------------------------------------------
    def _do_revoke(self, pr):
        lb = self.cfg.labels
        latest = pr.reviews[-1] if pr.reviews else None
        approved = latest['head_sha'] if latest and latest['verdict'] == 'approve' else None
        if approved == pr.head_sha:
            title = 'Readiness sospesa'
            reason = (f'La CI del commit approvato `{approved[:12]}` è di nuovo in corso. La PR '
                      'torna in review: l\'approvazione resta registrata e verrà riapplicata '
                      'senza una nuova review se la CI passa')
        elif approved:
            title = 'Approvazione revocata'
            reason = (f'Il commit `{pr.head_sha[:12]}` non è quello approvato (`{approved[:12]}`). '
                      'La PR torna in review')
        else:
            title = 'Approvazione revocata'
            reason = 'Non risulta una review devloop approvata. La PR torna in review'
        self.gh.comment(pr.number,
                        publishable(dispatcher_header() + f'\n## {title}\n\n' + reason + '.'))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.ready_to_merge])

    def _do_stale(self, number):
        lb = self.cfg.labels
        self._escalate(number, 'Implementazione interrotta: la issue era in lavorazione ma non '
                               'esiste una PR devloop aperta. Rimettere `agent:ready` per ripartire.',
                       remove=[lb.working])

    def _do_implement(self, number):
        cfg, lb = self.cfg, self.cfg.labels
        agent = cfg.agent(cfg.implementer)
        issue, comments = self._issue_context(number)
        self.gh.edit_labels(number, add=[lb.working], remove=[lb.ready])
        name, branch = f'issue-{number}', f'{cfg.branch_prefix}{number}'
        path = self.ws.new_branch(name, branch)
        prompt = _template('implement', repo=cfg.repo, base_branch=cfg.base_branch,
                           issue_number=number, issue_title=issue['title'],
                           issue_body=issue.get('body') or '', issue_comments=comments,
                           test_cmd=cfg.test_cmd, language=cfg.language)
        result, failure = self._write_turn(agent, prompt, 'implement', path, f'issue-{number}-implement')
        if result['status'] == 'blocked':
            return self._blocked(number, agent, result['blocked_reason'], remove=[lb.working])
        if failure:
            return self._escalate(number, failure[0], failure[1], remove=[lb.working])
        self.ws.commit_all(path, self._commit_message(agent, result['commit_message'], number))
        self.ws.push(path, branch, replace=True)
        meta = {'issue': number, 'implementer': agent.key, 'reviewer': cfg.reviewer}
        body = publishable(self.runner.header(agent) + '\n' + result['pr_body'].strip()
                           + f'\n\nCloses #{number}', 'pr', meta)
        url = self.gh.create_pr(base=cfg.base_branch, head=branch, title=result['pr_title'],
                                body=body, labels=[lb.review])
        self.gh.edit_labels(number, remove=[lb.working])
        self.ws.remove(name)
        self.log(f'  opened {url}')

    def _do_ci_failed(self, pr):
        log_tail = '\n'.join(self.gh.failed_log(run) for run in pr.failed_runs[:2])
        review = ci_failure_review(failed_checks=pr.failed_checks, log_tail=log_tail,
                                   previous_open=open_findings(pr.reviews),
                                   round_no=len(pr.reviews) + 1,
                                   max_rounds=self.cfg.max_review_rounds, head_sha=pr.head_sha)
        self.gh.comment(pr.number, render_review(review, dispatcher_header()))
        self._apply_verdict(pr, review, announce=True)

    def _do_apply_review(self, pr):
        self._apply_verdict(pr, pr.reviews[-1], announce=False)

    def _reviewer_key(self, pr):
        """The PR's recorded reviewer, else the configured one; never the PR's implementer, even
        after a configuration change. None when no such agent is configured."""
        implementer = pr.meta['implementer']
        for key in (pr.meta['reviewer'], self.cfg.reviewer):
            if key in self.cfg.agents and key != implementer:
                return key
        return None

    def _do_review(self, pr):
        cfg = self.cfg
        reviewer_key = self._reviewer_key(pr)
        if reviewer_key is None:
            return self._escalate(
                pr.number, f'Nessun reviewer configurato diverso dall\'implementer della PR '
                           f'(`{pr.meta["implementer"]}`): la review non può essere indipendente.',
                remove=[cfg.labels.review])
        agent = cfg.agent(reviewer_key)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        if self.ws.head(path) != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return
        issue, _ = self._issue_context(pr.issue)
        round_no = len(pr.reviews) + 1
        previous_open = open_findings(pr.reviews)
        prompt = _template('review', repo=cfg.repo, base_branch=cfg.base_branch,
                           pr_number=pr.number, round=round_no, max_rounds=cfg.max_review_rounds,
                           implementer=pr.meta.get('implementer', cfg.implementer),
                           ci=pr.checks, test_cmd=cfg.test_cmd,
                           issue_number=pr.issue, issue_title=issue['title'],
                           issue_body=issue.get('body') or '', pr_body=pr.body,
                           diff_stat=self.ws.diff_stat(path),
                           previous=format_previous_rounds(pr.reviews, pr.fixes),
                           language=cfg.language)
        raw = self.runner.run(agent, 'review', prompt, 'review', path, f'pr-{pr.number}-review')
        review = normalize_review(raw, previous_open=previous_open, round_no=round_no,
                                  max_rounds=cfg.max_review_rounds, head_sha=pr.head_sha,
                                  ci=pr.checks, reviewer=agent.key)
        self.gh.comment(pr.number, render_review(review, self.runner.header(agent)))
        self._apply_verdict(pr, review, announce=True)
        self.ws.remove(name)
        self.log(f'  verdict: {review["verdict"]}')

    def _do_fix(self, pr):
        cfg, lb = self.cfg, self.cfg.labels
        if not pr.reviews:
            return self._escalate(pr.number, 'Modifiche richieste ma nessuna review devloop da '
                                             'correggere.', remove=[lb.changes])
        latest = pr.reviews[-1]
        findings = open_findings(pr.reviews)
        if not findings:
            return self._escalate(pr.number, 'Modifiche richieste ma nessun finding aperto.',
                                  remove=[lb.changes])
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        if self.ws.head(path) != latest['head_sha']:
            self.log('  branch changed after the review; sending it back to review')
            self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes])
            return
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, f'L\'implementer della PR (`{pr.meta["implementer"]}`) non è più '
                           'configurato: serve una decisione su chi corregge.',
                remove=[lb.changes])
        agent = cfg.agent(pr.meta['implementer'])
        issue, _ = self._issue_context(pr.issue)
        prompt = _template('fix', repo=cfg.repo, pr_number=pr.number, issue_number=pr.issue,
                           issue_title=issue['title'], issue_body=issue.get('body') or '',
                           findings=_format_findings(findings), test_cmd=cfg.test_cmd,
                           language=cfg.language)
        result, failure = self._write_turn(agent, prompt, 'fix', path,
                                           f'pr-{pr.number}-fix-r{latest["round"]}')
        if result['status'] == 'blocked':
            return self._blocked(pr.number, agent, result['blocked_reason'], remove=[lb.changes])
        if failure:
            return self._escalate(pr.number, failure[0], failure[1], remove=[lb.changes])
        if self.ws.has_changes(path):
            self.ws.commit_all(path, self._commit_message(agent, result['commit_message'], pr.issue))
            self.ws.push(path, pr.head_ref)
        fix = dict(result, head_sha=self.ws.head(path))
        self.gh.comment(pr.number, render_fix(fix, self.runner.header(agent), latest['round']))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes])
        self.ws.remove(name)


def ensure_labels(gh, labels):
    for attr, (color, description) in LABEL_STYLE.items():
        gh.ensure_label(getattr(labels, attr), color, description)


def run_forever(dispatcher, interval, sleep=time.sleep):
    while True:
        try:
            dispatcher.tick()
        except (GitHubError, WorkspaceError) as exc:
            dispatcher.log(f'tick failed: {exc}')
        sleep(interval)
