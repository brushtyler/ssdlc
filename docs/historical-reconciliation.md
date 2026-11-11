# Cumulative I1 → I2 admission (issue #27)

This compatibility rule, `i1-cumulative-limits-v1`, admits a stopped, explicitly
fenced I1 generation to native development with cumulative I2 limits. Preparation
and activation never call a provider. They do not resume the historical #22 run,
authorize a new budget implicitly, approve a PR, or grant global readiness.

## Operator workflow

Use a stable controller from the latest merged main, outside the candidate tree.
Stop every original controller, supervisor and external writer. Docker mount
observations and stable ticket/scheduler locks supplement the explicit
`source_fenced: true` attestation; they do not prove arbitrary host processes are
quiescent. Never delete locks or modify a historical journal to gain admission.
Keep requests, cap sources, proposals, authorizations and runtimes private.

1. Run `python3 orchestrator.py continuation-limits-inspect --repo TARGET
   --ticket TICKET`. It reads the selected I1 journal without repairing it and
   reports ledger row hashes, engines, reported costs, session hashes/counts and
   consumed task attempts. It excludes session transcripts.
2. Write a request conforming to
   `schemas/reconciliation-request.schema.json`. `profile` must also satisfy
   `schemas/recovery-limits.schema.json`. The profile describes **cumulative
   totals**, including all prior execution and waiting time, not fresh allowance.
3. Run `continuation-limits-prepare --repo TARGET --ticket TICKET
   --request request.json`. It stages a separate `execution-runtimes/<id>`,
   preserves the full source journal prefix/history and ledger, binds the current
   controller and Docker image, collects a fresh baseline and fresh pertinent
   candidate/checkpoint checks. It never dispatches a developer. Only a published
   pending intent fences normal execution; a rejected preparation leaves the
   original recoverable.
4. Review the private `execution/prepared-<id>/proposal.json`, `recipe.json`,
   `projection.json`, `runtime-seal.json` and actual staged journals. Authorize
   their exact hashes using `schemas/reconciliation-authorization.schema.json`:

   ```json
   {
     "schema_version": 1,
     "transition_id": "<proposal transition_id>",
     "proposal_sha256": "<canonical JSON SHA256>",
     "profile_sha256": "<proposal profile_sha256>",
     "decisions_sha256": "<canonical JSON SHA256 of proposal.decisions>",
     "old_controller_sha256": "<proposal old_controller_sha256>",
     "new_controller_sha256": "<proposal new_controller_sha256>",
     "authorized": true,
     "source_fenced": true,
     "accept_unknown_history": true,
     "authorize_extensions": false
   }
   ```

   Set `authorize_extensions: true` only when approving every listed extension.
   Unknown caps, ambiguous attribution, opaque sessions or missing timestamps
   require `accept_unknown_history: true`. When `scope_diff` adds any targets,
   include `scope_diff_sha256` binding that entire host-calculated diff. Both
   controller identities use `execution-contract-json-sha256`; the historical
   artifact-byte hash is separately named. JSON digests are SHA256 of Python's
   `json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()`, as implemented by `ssdlc_tasks.digest`.
5. Run `continuation-limits-activate --repo TARGET --ticket TICKET
   --preparation PREPARATION --authorization authorization.json`. It rechecks
   exact source/registered inputs, external cap-source bytes, controller/image,
   staged seal, actual task and accounting projections, remaining capacity and
   deadline. Immutable intent/payload publication precedes the atomic
   `execution/active.json` selector. Retry the exact command after interruption;
   it returns the same generation without resetting later execution progress.
6. Start a **new** native foreground or supervised run with `run --from-phase
   03-dev`, the same ticket and target, using that pinned controller. Inspect
   compatibility before launching a supervisor. Original supervisor records stay
   pinned to their original generation and cannot silently read the new one.
   New status, journal, completion and deadline readers resolve the selected
   runtime. Earlier recon/PO/design/planning entrypoints are refused before they
   can allocate new sessions. Current review, QA and other global gates still run.

To abandon an unselected preparation, use `continuation-limits-discard --repo
TARGET --ticket TICKET --preparation PREPARATION`. For an I1 preparation use
`continuation-discard` with its recorded path. Discard archives staged evidence
before removing only the recorded owned worktree, branch and runtime. It is
idempotent, refuses selected generations, changed/unowned Git resources,
unowned index locks and unproven writer quiescence. Rejected/orphaned tracked
preparations require discard before a second preparation; untracked legacy
orphans require explicit ownership reconciliation and are never blindly removed.

## Accounting request

`ledger_mapping` contains exactly one `{index, sha256, role, phase, origins}` for
each source row. Each origin may appear once across all rows/supplements; a row
can cover at most one origin. All structured task attempts, design rounds and
reserved reviews/diagnoses, planning invocations and review reservations need
coverage. Extra ledger rows still count. Missing ledger telemetry is represented
by `supplemental_invocations` with explicit role/phase/engine/reason; it never
refunds a charged reservation. A structured unknown session requires an explicit
conservative `opaque_floors` allocation; `count` cannot undercut known `used`.
`ssdlc_historical.reservation_catalogue(facts, opaque_floors)` exposes the stable
origin identifiers for an offline request builder.

Positive Claude amounts are CLI estimates; positive OpenRouter amounts are
reported metered usage. Other positive amounts need explicit `usage_kind` and
`reason`. Absent/default-zero legacy telemetry remains `null`, not a claim of
zero billing. This admits execution with explicitly accepted unknown history;
it does not estimate subscription billing or implement issue #14's quota work.
All invocations count toward total, role and phase caps exactly once. A legacy
aggregate `budget` also remains enforced as `combined_reported_usd_cap`; raising
it requires its own listed extension, even if individual cost-kind caps fit.

`limit_sources` optionally identifies exact hashed JSON/env files and an explicit
mapping of canonical numeric cap names to JSON pointers/env keys. Env files are
parsed as data, never sourced as shell. Conflicting evidence is refused. Missing
caps are listed as unknown rather than imported from present environment defaults.
The native frozen profile and known role/phase/scalar cap increases are bound to
operator decisions. Historical transport consumption is unknown in this rule;
`transport_retries` must be zero. Existing I2-profile extension is a separate
transition and is refused here.

`time.started_at` must equal original `state.created_at` with basis
`source_created_at`. If the timestamp is absent/unparseable, use an explicit
`operator_attestation` with reason; the uncertainty is recorded for authorization.
The absolute deadline is `started_at + profile.wall_seconds`, including suspension
and operator waits. Exhausted caps/deadlines prevent activation/dispatch; increasing
them requires a new reviewed preparation and exact extension authorization.

## Recovery invariants and limits

The rule accepts I1's single captured root task and untouched pending tasks.
Already completed T01 is freshly checked without developer or new commit. A
captured candidate is verified using its original immutable binding, under new
controller/input checks. Old capture identity is never relabelled as a new
producer outcome. Fresh check collections consume the persistent allowance
(maximum three per charged attempt); admission cannot reset that count.

Failures previously retained only in `origin_record` are restored to active
scheduler history with hash-checked archived evidence. Ambiguous/partially lost
history is refused. Historical non-code reconciliation gates remain blocking;
this rule does not grant a blanket override. A failed partial candidate uses the
next charged development attempt and prior diff/log context. An unchanged repair
with unchanged failing outcomes still yields `REPAIR_STALLED`. Reverification of
the same charged attempt is not falsely counted as a second development repair.
Actual runtime floors for attempts, failures and verification reservations are
bound in the policy, not merely copied into an unused projection.

Every new provider invocation goes through native I2 reservations. Only the
trusted adapter's `NotDispatched` proof, raised before launch, can reconcile a
prelaunch auth/deadline/HTTP refusal as `not_dispatched`; the task retains its
attempt and nonce. Reservations remain counted. Same category/text after an
unknown launch is `uncertain` and blocks further dispatch. No arbitrary provider
message establishes non-dispatch, timeout quiescence or permission to retry.

A fresh failure of an already committed checkpoint refuses admission; rewriting
that checkpoint automatically belongs to I5a's corrective loop. This rule does
not support task identity changes, planning restart or a second execution-profile
migration. Hashes assume host-owned references and authorization are protected;
full egress/host containment remains I3. Hardening of other new-run I2 paths is
tracked separately in #29.

## Verification and next rollout

`tests/test_reconciliation.py`, `test_historical_accounting.py`,
`test_not_dispatched.py` and `test_continuation_hardening.py` run in the existing
CI unit discovery gate. They exercise real Git/journals, native constructor and
provider wrapper accounting with semantic/fake engines; there are no paid calls.
The sanitized #22-shaped accounting replay includes 25 ledger entries, six
design rounds, four planning attempts, one T01 attempt, preserved limits and
unknown usage. It is not a replay of private model transcripts or a live resume.

After merge, reconcile the actual #22 candidate against latest main, obtain the
independent amendment review, prepare the concrete cumulative proposal and obtain
its authorization before resuming through SSDLC. Record before/after logical and
live calls, attempts, duplicated work, model/check/elapsed/operator-wait time and
required/avoidable interventions. Preserve the historical baseline and report
fresh global-gate outcomes; completing this offline PR is not that evaluation.
