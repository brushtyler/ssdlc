# Task mode

Task mode breaks development into a reviewed DAG of tasks. Each task has a strict file manifest, owned requirements and automated checks. It is the default for every new run, including Tier S:

```bash
# Task mode is the default for every new run, including Tier S.
```

Before you enable it, review any project-specific standards already installed. Global review, QA and AppSec still run after development. Recovery after a stop is covered by the [recovery documents](../README.md#recovery-and-continuation).

## Requirements catalogue

The PO, design and planning phases share one strict catalogue of `AC-*` and `SEC-*` IDs.

**Defining IDs**
- An ID can be defined in a Markdown heading, a list entry, the first column of a table, or an `AC-1: ...` line. Compact ID-only lists also work.
- IDs are case-sensitive and use letters, digits and hyphens. Explicit children use forms such as `AC-1-1` or `AC-1a`.
- Malformed or duplicate definitions block planning.
- Prose mentions and fenced examples do not create requirements. Undefined prose mentions are listed separately for review.

**Ownership and coverage**
- Every defined ID, including parent headings and sub-cases, needs explicit task ownership and local check coverage. A hierarchy never implies coverage.
- A check can claim only requirements owned by its task.

**Diagnostics and contract references**
- Mapping diagnostics report every ownership, coverage and contract defect at once, with task and check IDs.
- Contract references point to actual design filenames and ATX heading anchors relative to `02-design`, for example `adr.md#interfaces`.
- Headings inside fenced code do not count, and repeated anchors get numeric suffixes.
- Duplicate JSON keys, non-finite constants and multiple candidate objects are rejected.

Structural coverage still needs an independent semantic review of whether the checks really exercise the claimed behaviour.

### PO format validation and recovery

PO output must pass the catalogue before `PO_SPEC_GATE` can pass. The PO adapter can rewrite unambiguous dotted IDs (`AC-1.1` → `AC-1-1`) and their references without calling a model again. It archives the original text, the canonical text, the ID mapping and their hashes under `01-po/validation/`. Collisions, duplicates, missing AC/SEC families and other malformed IDs still block.

For a ticket that stopped before design, planning or development:

```bash
./ssdlc po --ticket TKT-001 --repo /path/to/project
```

This command validates the registered specification without a provider call, without repeating recon or tests, and without changing counters. Success records `PO_READY`; continue with `design` and `plan`. It does not regenerate specifications or migrate tickets after downstream work.

## Design sessions

Pair design persists its session, immutable drafts and reviews, and a sealed copy of its inputs under `02-design/design-session/`.

- **Context.** The architect and the reviewer receive the cumulative reviews and the latest ADR. They also get the tracked source files named by recon, with hashes and numbered excerpts: up to four files, 80,000 characters in total and 50,000 per file. Truncation is explicit.
- **Review format.** The reviewer returns `schemas/design-review.schema.json`, with STRIDE analysis, stable finding IDs and explicit blocker dispositions. Optional advice cannot block. New blockers must reference approved requirements or quality checks.
- **Round limit.** New sessions allow five rounds by default; `SSDLC_MAX_DESIGN_ROUNDS` sets the initial limit. The limit and the consumed reservations survive restarts. An interrupted architect call consumes its round, and a captured ADR is reused.
- **Progress and stops.** Progress is measured semantically: rewording an ADR or renaming findings is not progress. Repeated rounds without progress trigger one bounded diagnosis and then stop as `DESIGN_STALLED` (see [Bounded recovery](bounded-recovery.md)).
- **Approval.** Approval needs a valid independent review, never just an exhausted limit. Canonical `adr.md` and `threat-model.md` are published only after approval.

Drafts and critiques are saved immediately under `02-design/rounds/<session-id>/`. When approval is withheld, the console shows an excerpt of the critique and the path to the full report.

To resume design only, with an explicit total limit of up to eight rounds:

```bash
./ssdlc design --ticket TKT-001 --rounds 5
./ssdlc design --ticket TKT-001 --rounds 5 --import-legacy rounds/ROUND_DIRECTORY   # one-time legacy import
```

`design` verifies upstream approvals, input hashes and a clean, unchanged target. It refuses an existing plan or worktree and never starts development. A lower limit is rejected, extensions are recorded, and repeating the command does not reset rounds. Legacy rounds are copied and counted; their critiques remain context, not approval.

## Task planning

The planner returns a structured plan (`schemas/plan.schema.json`), which an independent reviewer evaluates (`schemas/task-plan-review.schema.json`). Both receive the cumulative diagnostic and review history.

**Findings**
- Findings keep stable IDs with explicit OPEN or RESOLVED dispositions.
- Every OPEN BLOCKING finding needs a known AC/SEC, standard or quality-check reference, concrete evidence and a requested change.
- New findings after the first review must point to an earlier omission or a defect introduced by a revision.

**Allowances**
- Structural repairs: one original candidate plus two repairs.
- Semantic revisions: two by default. `plan --review-repairs N` extends them explicitly (range 0–4); the extension is recorded.
- All reservations persist. Exhaustion stops the session; it never approves.

**Stops**
- Three consecutive reviews that keep the same blocking IDs stop as `STAGNATION`.
- A missing environment prerequisite stops as `ENVIRONMENT_PREREQUISITE`.
- Invalid reviewer output gets one correction on the same candidate, then stops as `REVIEW_PROTOCOL`.

Evidence lives in `02-design/task-planning/`.

```bash
./ssdlc plan --ticket TKT-001                     # retry planning without repeating PO/design
./ssdlc plan --ticket TKT-001 --review-repairs 2  # explicit semantic extension
./ssdlc plan --ticket TKT-001 --recover-review    # revalidate a captured rejected review after a validator fix
```

`plan` checks the registered upstream hashes and design approval, and requires that execution has not started. Success records `PLAN_READY`; continue with `run --from-phase 03-dev`.

## Task execution

The scheduler runs one task at a time. It writes manifests, logs, diffs, `task-journal.jsonl` (authoritative) and `tasks-state.json` under `03-dev/tasks/`.

1. **Baseline preflight.** Before the first developer call, including on resume, the required quality checks run at the clean checkpoint in the actual task worktree. Failing or empty checks stop as `BASELINE_MISMATCH` without consuming attempts; source mutations stop as `BASELINE_MUTATION`.
2. **Dispatch and capture.** The developer works in a disposable workspace. The host captures the candidate and checks it against the manifest and protected paths ([Candidate capture](candidate-capture.md)).
3. **Verification.** Task checks and required quality checks run in a fresh evidence directory. Only passing verification creates a Git checkpoint. `TASK_DEV_STATUS: COMPLETE` requests verification; it never replaces it.
4. **Repair.** After an ordinary check failure, the full patch, tree, developer output and logs are archived, and a clean checkpoint is restored. The next attempt verifies the archived evidence and restores the candidate for repair in place. The prompt includes cumulative summaries and excerpts of the failed logs: 6,000 characters per log and 24,000 in total.
5. **Stops.** Each stop condition has its own label:
   - `TASK_DEV_STATUS: SCOPE_REQUIRED`: the developer needs files outside the manifest. Partial in-scope work is archived and the run rolls back to the checkpoint. Recovery uses a reviewed [scope amendment](scope-amendments.md) or the [feedback workflow](developer-feedback.md).
   - `TASK_DEV_STATUS: ENVIRONMENT_BLOCKED`: the developer cannot proceed for environment reasons; partial work is handled the same way.
   - `DEVELOPER_INVOCATION`: the developer process exited with a nonzero code.
   - `REPAIR_STALLED`: the repair produced an identical failing tree with identical check outcomes.

Attempt counters are persistent; a restart never resets them. A crash around a verified checkpoint is reconciled without generating or committing the task twice. Dirty worktrees, unexpected HEAD changes, modified inputs or missing evidence stop with a diagnostic and preserve the work.

### Protocol recovery

After a parser fix, an unverified `DEVELOPER_PROTOCOL` candidate can be recovered explicitly with `TaskScheduler.run(..., recover_protocol_task=task_id)`. Recovery checks the original response against the journal and verifies the archived patch, base and scope. It then runs every check without another developer call and without resetting counters. Scope and environment blockers cannot use this path.

### Invalidation and revalidation

```python
scheduler.invalidate(["T1"], reason="Recheck affected tasks and their dependants")
result = scheduler.revalidate()
if not result.complete:
    print({"failed": result.failed, "blocked": result.blocked})
```

`revalidate()` works on a journal that contains only COMPLETED and INVALIDATED tasks. It verifies invalidated tasks in dependency order and does not call a developer. Passing tasks get fresh evidence. Failing tasks stay INVALIDATED and their dependants are blocked. Normal `run()` resumes only after all invalidated work has passed revalidation.

### Evidence integrity

Each attempt uses a fresh directory, with exclusive log creation and durable check start and completion events. Check logs, failed-attempt diffs, preflight logs and revalidation logs are hash-checked. Root identity and directory ancestry are checked before writes, which detects evidence-root redirection. A mutation of source or HEAD never produces a false COMPLETED transition. Discard a scheduler instance after an exception. See also [Architecture › Integrity model](architecture.md#integrity-model-and-its-limits).

## Global gates in task mode

### Review and QA input

- **Review** receives the complete cumulative diff and runs read-only in the verified task worktree. Diffs larger than `SSDLC_REVIEW_MAX_DIFF_CHARS` stop before any model call; they are never silently truncated. Prompts and raw reviews are archived under `04-review/runs/`. If the checkpoint changes during review, approval is withheld.
- **QA** uses the same diff budget and worktree, and archives its runs under `05-qa/runs/`.

### Gate reports

Review, QA and AppSec must produce a human-readable report plus exactly one JSON object between these markers, each on its own line:

```text
-----BEGIN SSDLC GATE REPORT <id16>-----
-----END SSDLC GATE REPORT <id16>-----
```

The report then ends with exactly one final verdict:
- `REVIEW_STATUS: APPROVED|CHANGES_REQUESTED`
- `QA_STATUS: PASSED|FAILED`
- `APPSEC_STATUS: SECURE|VULNERABLE`

The nonce `<id16>` is the first 16 characters of the host's evaluation identity. That identity binds:
- the gate, the reviewer engine and model and its ordinal, the attempt and the verified commit;
- the hashes of the diff, plan, obligations, evidence, prompts and report schema;
- the mandatory controls, the trusted context and the reasoning effort.

**Approval and correction**
- Approval also requires valid findings, unchanged inputs and passing mandatory controls.
- Failed or unknown required scans block AppSec. A missing tool, or an npm dependency scope without a lockfile, counts as unknown.
- One protocol correction is allowed, and it preserves substantive findings.
- A substantive rejection is final for that evaluation.

**Reservations and reuse**
- Reservations are persisted before each call. An interrupted call with an unknown outcome stops on recovery and is never retried blindly.
- Changed inputs never reuse a cached approval and never reset the allowance.

Artifacts live under `<phase>/evaluations/<slot16>/`:
- inputs, prompts and captured evidence;
- reservations, raw responses and diagnostics;
- the normalized `report.json` and `assessment.json`, plus telemetry;
- the final hashed `outcome.json`;
- for AppSec, also `scanner-status.json` and the scanner logs.

With an explicit quality contract that opts in with `candidate_proof`, deterministic candidate checks are collected before the reviews that need them ([Native gate convergence](native-gate-convergence.md)).

### Approval

The bootstrap stops on review feedback instead of skipping rejected tasks. Approval requires passing global gates, a clean checkpoint and an unchanged, clean target. It then fast-forwards to the verified SHA and preserves the evidence.

## Current limits

- Strict file manifests and automated task checks are required. Scope changes only through explicit, reviewed amendments ([Scope amendments](scope-amendments.md), [Developer feedback](developer-feedback.md)); there is no automatic replanning.
- Subgoals and automatic plan migrations are rejected. Per-task routing and subgoals are planned in `PLAN_TASK_ROUTING_AND_SUBGOALS.md`.
- Targeted review bounce is preview-only ([Bounce preview](bounce-preview.md)).
- Advisory schemas and provenance validation exist; proposal collection, export and `--from-proposal` are not implemented yet.
- Global review, QA and AppSec still run through the existing pipeline. The complete independent review matrix is a later increment.

## Validation in Docker

```bash
python3 tests/docker_smoke.py --image ssdlc-runner:bootstrap-test --suite   # read-only source, hidden credentials, writable scratch
python3 tests/docker_pid_smoke.py --image ssdlc-runner:bootstrap-test       # PID-limit failure path (temporary limit of 32)
```

Both tests use synthetic credentials only. The selected image must contain the `requirements.txt` dependencies.

## Unified execution and greenfield bootstrap

Every new run uses the task engine. Tier S requires exactly one plan node and uses the same scheduler, journal and quality gates. `jsonschema` is required (install `requirements.txt`).

The obsolete task-breakdown environment setting is ignored with a warning. Newly captured required checks carry candidate proof; historical sealed contracts remain unchanged. QA shell commands are reported separately and do not establish typed-check equivalence. Supervisor rejection records feedback only and authorizes no repair.

For a normal project with a passing nonempty baseline, the supported CLI is:

```bash
python3 orchestrator.py run --repo . --ticket TKT-1 --goal "Scoped change" --tier S --no-sync-remote --test-cmd 'printf shell-smoke'
```

For a clean greenfield repository at its initial commit, explicitly create
`.ssdlc/bootstrap-profile.json` (keep `.ssdlc/` ignored):

```json
{"schema_version":"1.0","profile_id":"python-unittest","window_tasks":1}
```

The same run command then captures baseline status `absent`, bound to the initial
commit/tree and registered policy. The first implementation must add real tests:
`python3 -m unittest discover -s tests` must execute at least one passing test.
Empty, failing, missing, timed-out or interrupted checks block. The one-task
exemption becomes journaled `PROVEN`/post-bootstrap without rewriting the sealed
contract. Restart before proof fails explicitly; a profile cannot renew eligibility.
Existing tests, dirty trees or later commits are ineligible (exit 24). Without an
explicit profile, strict baseline rules apply. Protected paths and task checks
remain enforced throughout.

The offline suite executes these CLI/configuration behaviors through real entry
points with fake providers and sandbox commands:

```bash
python3 -m unittest discover -s tests -p test_unified_integration.py
```

Run exits: **0** means the requested pipeline command completed (signoff remains
separate); **23** refuses a legacy/non-executable startup, with a new-run command;
**24** refuses bootstrap eligibility/profile validation; **2** is invalid CLI usage.
Pipeline execution errors fail nonzero; they do not imply successful signoff.
Preview's separate **0/10/20** outcomes are described above.

Metrics are per-process counters, not billing units: `corrective_transactions_created`,
`corrective_transactions_executed`, `corrective_transactions_refused` (by reason),
`corrective_transactions_replayed`, `corrective_transactions_interrupted`,
`corrective_transactions_reconciled`, `corrective_repair_attempts`,
`bootstrap_eligible`, `bootstrap_ineligible` and `legacy_refusals`.
Structured logs include stage and bounded reason codes without raw finding prose.
