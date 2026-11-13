# devloop: agent dispatcher for this repository

devloop moves issues through implementation, independent review and fixes, so that a human only
decides what starts and what gets merged. It is development tooling for this repository, not part
of the SSDLC orchestrator, and it never touches SSDLC runs or the main checkout's working tree.

## The loop

State lives on GitHub: `agent:*` labels and hidden markers in comments. A tick reads that state,
plans the next steps and runs them. A crashed tick is resumed by the next one.

| Step | Trigger | Result |
|---|---|---|
| Start | A trusted user adds `agent:ready` to an issue | `agent:working`; the implementer works in a fresh worktree |
| Implement | Changes made and local tests pass | Dispatcher commits, pushes `agent/issue-<n>`, opens a PR labelled `agent:review` |
| CI | Checks pending | Wait; failing checks become a review round sent back as `agent:changes` |
| Review | CI passed or absent | A **different** agent posts a structured verdict (see below) |
| Fix | `agent:changes` | The implementer fixes or disputes each open finding, then back to `agent:review` |
| Approve | Verdict `approve` | `agent:ready-to-merge`, only if the PR head is still the reviewed commit and CI passed (both re-read just before promotion) |
| Merge | `agent:ready-to-merge` | **A human merges.** devloop never merges |

Readiness belongs to the approved commit. Every tick re-checks ready PRs:

- if the head is no longer the approved commit, the approval is revoked and the PR goes back to
  `agent:review` for a new review;
- if CI is running again on the approved commit, readiness is suspended (`agent:review`) and the
  recorded approval is applied again, without a new review, once CI passes;
- if CI fails on the approved commit, the failure becomes a review round (`agent:changes`).

Any step can stop at `agent:needs-human` with a comment explaining why: the agent declared itself
blocked, tests still fail after the retry, the reviewer asked for a decision, the round limit
was reached, an agent, `gh` or `git` call failed or timed out, the issue text was changed by an
untrusted user after `agent:ready`, or the PR body lost its marker. While `agent:needs-human` is
set devloop ignores the PR or issue, so to resume: fix the cause, **remove `agent:needs-human`**
and set the label that should run next (`agent:ready` on the issue, `agent:review` or
`agent:changes` on the PR). Every escalation comment repeats this.

The round limit (`max_review_rounds`) is checked before every new round, whether a review or a
CI failure, so a PR whose head keeps moving cannot be reviewed indefinitely. Replaying a verdict
that is already recorded is not a new round.

A devloop PR is recognised by its branch, `agent/issue-<n>`, opened by a trusted user. Its body
ends with a hidden marker that records the issue, implementer and reviewer: text may be edited
above it, but the marker must stay the last line. A PR whose marker is missing stays in the flow
(it still counts against `max_open_prs` and its issue does not restart) and goes to
`agent:needs-human`. A PR left without a state label, for example after a crash right after it
was opened, is put back into `agent:review`, and its issue loses `agent:working`.

Open devloop PRs are capped by `max_open_prs`, including PRs waiting for merge or for a human,
so unattended work cannot outrun review capacity. New issues start in order: `priority_labels`
first, then by number. Issues with a `blocked_labels` label are skipped.

## Structured review

The reviewer returns JSON validated against [`devloop/schemas/review.json`](../devloop/schemas/review.json):
`verdict` (`approve`, `request_changes`, `needs_human`), blocking and non-blocking findings with
file, line and a suggested fix, and the status of every earlier blocking finding (`resolved`,
`unresolved`, `withdrawn`). devloop then enforces consistency:

- `approve` with a blocking or unresolved finding becomes `request_changes`;
- `request_changes` with nothing to fix becomes `needs_human`;
- an earlier finding the reviewer does not assess stays open.

Findings get stable ids (`R<round>-<n>`). The fix step answers each one (`fixed`, `disputed`,
`not_applicable`), and the next round sees those answers. The comment shows the review in
readable form and carries the full record in a hidden base64 marker for the next tick.

Markers are control records, while the rest of each comment is text written by a model, and both
are published by the same account. So the dispatcher neutralizes HTML comments in all published
text, and reads a marker only from the last line of a comment, of the expected kind and with a
valid structure. Text from an implementer or a reviewer cannot record a review, a fix or a PR
link.

## Attribution

Every text devloop publishes starts with the header that [AGENTS.md](../AGENTS.md) requires. The
values come from the configured agent: CLI version from its `--version`, model and reasoning level
as passed on the command line. Text the dispatcher writes on its own, such as escalations and CI
rounds, names `devloop dispatcher` as the agent and no model.

## Usage

```bash
python3 -m devloop setup            # create the agent:* labels
python3 -m devloop status           # devloop PRs, ready issues, planned actions
python3 -m devloop tick --dry-run   # print the plan, change nothing
python3 -m devloop tick             # run one round
python3 -m devloop run --interval 600
python3 -m devloop cleanup          # remove worktrees left by failed steps
```

Run it from any checkout of this repository. Worktrees, agent prompts and logs, and the lock that
prevents two dispatchers from running at once live under `state_dir` (default `.ssdlc/devloop`)
in the main checkout.

### Run from a pinned installation

devloop works on this repository, including on its own code, so run a pinned copy rather than
the checkout it is changing. This is the same rule as the pinned controller SSDLC uses to develop
itself:

```bash
cd /path/to/ssdlc                                        # the repository devloop works on
git worktree add --detach .ssdlc/devloop/pinned <commit> # once per devloop version
PYTHONPATH=.ssdlc/devloop/pinned python3 -P -m devloop tick
```

`-P` is required: without it `python3 -m` puts the current directory first on the module path
and imports the checkout's `devloop/` instead of the pinned one. The configuration, schemas and prompts come from the pinned copy; GitHub state and worktrees come
from the current repository. Moving the pin to a newer commit is an explicit operator action.
`devloop cleanup` removes only the agent worktrees under `state_dir/worktrees`, never the pin.

devloop imports only the Python standard library and itself. A test enforces this, so the tool
can later move to its own repository without changes.

## Configuration

[`devloop/config.toml`](../devloop/config.toml) sets the repository, the test command, limits,
the `implementer` and `reviewer` roles (they must be different agents) and, for each agent, the
model, reasoning effort and extra CLI arguments for writing and reviewing turns. Pass another file
with `--config`.

Each PR records its implementer and reviewer when it is opened, so changing the roles does not
affect open PRs. If the recorded reviewer is no longer configured, the configured one takes over,
but never the agent that implemented the PR: without an independent reviewer, or if the PR's
implementer is no longer configured, the PR goes to `agent:needs-human`.

Agents run headless: `claude -p`, `codex exec`, `agy -p`, with the schema in
[`devloop/schemas/`](../devloop/schemas/). Reviewers run read-only (codex `read-only` sandbox,
agy `plan` mode, claude without edit tools). Implementers edit only their worktree; the
dispatcher does every commit, push and GitHub write.

## Limits

- **No automatic merge.** A human merges every PR.
- **Same GitHub identity.** All agents publish through the operator's `gh` login, so reviews
  are comments, not GitHub approvals, and branch protection cannot tell agents apart.
- **Trust comes from labels.** Only issues whose `agent:ready` label was added by a user in
  `trusted_users` start, only comments by those users reach the prompts, and only their markers
  are read. Labelling approves the issue's title and body as they are: if someone outside
  `trusted_users` changes them later, implementation, review and fixes stop at
  `agent:needs-human` until a trusted user adds `agent:ready` again. Issue text is still model
  input: do not label untrusted content.
- **Failures escalate immediately.** A timeout or a transient error of an agent, `gh` or `git`
  goes to `agent:needs-human` rather than being retried.
- **Single host, sequential.** One tick runs actions one at a time.
- **`gh` compatibility.** Checks, labels, comments and PR creation go through the REST API
  (`gh api`), so devloop does not depend on newer porcelain flags such as `gh pr checks --json`.
  For commit statuses the aggregate state counts, including contexts beyond the first page.
