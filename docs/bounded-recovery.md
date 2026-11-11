# Bounded recovery and design (I2)

I2 implements A4.4–A4.7, A6b, the new A3 stop inventory and an initial A5 CI
replay gate from `PLAN_AUTONOMY_WORKSTREAMS.md`. This is direct bootstrap work
with semantic engines, real Git and credential-free Docker verification. It is
not an unattended delivery demonstration or the final autonomous profile.

## Activation and limits

Fresh tickets may pass `--limits-profile FILE` to `run`, `start`, `plan` or
`design`. Detached children read the same frozen policy; they never reconstruct
it from a new set of environment defaults. A profile conforms to
`schemas/recovery-limits.schema.json` and explicitly names total invocations,
wall seconds, per-role/per-phase counts, development attempts, design rounds,
per-call timeout, transport retries, design diagnoses, API-reported dollars and
CLI-estimated dollars. No production profile is inferred from a risk tier.
`tests/replay/fixtures/i2-recovery.json` and the tests' `offline-i2-explicit`
profile are a versioned semantic benchmark baseline, not live sizing advice.
The design-repair scenario uses nine logical invocations; its protected policy
allows only the explicitly configured headroom. Live sampling and fuller usage
reporting remain separate work, including issue #14.

Example invocation, after reviewing the actual profile for the scoped ticket:

```sh
./ssdlc run --detach --ticket TICKET --goal 'SCOPED GOAL' --limits-profile /private/profile.json
```

A profile is bound to ticket, original base and controller snapshot in
`limits/policy.json`. The existing hash-chained journal in `limits/` is authority
for reservations and consumption; its snapshot is only a projection. Stable
locks serialize updates. Every outer role invocation reserves before dispatch,
including direct phase entrypoints and nested CLI/API adapters (counted once).
A lost response remains charged and blocks another dispatch. Returned or
confirmed-terminated calls reconcile once. No provider switch, restart or failed
repair refunds a reservation. Task attempt ceilings are persisted for the I2
scheduler. A missing role/phase cap refuses dispatch.

`metered_usd` sums positive API usage observations;
`cli_estimate_usd` sums positive CLI estimates. They are independent thresholds,
not interchangeable charges. Missing/default-zero telemetry remains `null`;
Codex/agy calls with no reported cost do not become free. A CLI estimate does
not establish the subscription, billing route, charged amount or quota delta.
These thresholds stop later calls when observed totals reach them. They do not
guarantee a financial ceiling on a call whose price is unknown in advance;
invocation/time bounds provide the independent hard limits. No API route falls
back silently to a different provider under this policy.

The absolute UTC deadline includes preflight, execution and operator waits.
Per-call subprocess timeouts are clamped to remaining ticket time and the frozen
call timeout. The detached supervisor uses the earlier of its own configured
deadline and the original ticket deadline, including on resume. Cleanup has the
existing bounded Docker grace. Bounded HTTP inference requires the detached
supervisor to enforce the overall deadline; socket inactivity timeouts alone
cannot establish a whole-request deadline.

`SSDLC_AUTONOMOUS=true` refuses launch without a profile and checks all known
required role/phase caps before the first model invocation. It does not activate
B5, GitHub issue intake or final unattended delivery. Ordinary legacy execution
without a profile retains its existing limits, apart from the safe HTTP and Git
lock refusals below. Deleting a policy while its journal exists refuses as
integrity loss. Replacing or increasing an existing profile is unsupported and
refused; it never resets consumed history. An authorized extension needs a
separate explicit transition, rather than editing policy/journal files.

Existing tickets cannot acquire a fresh profile over historical accounting.
I1 imported generations remain verification-only until a separately reviewed
cumulative-limit reconciliation/authorization is supported. In particular this
increment neither resumes issue #22 nor treats its existing ledger/unknown
legacy caps as zero. This conservative compatibility refusal is deliberate;
the original source, counters and limits remain preserved. Do not copy this
controller over a frozen run or manually rewrite its state.

## Timeout, transport and lock contracts

| Condition | Native result |
| --- | --- |
| Docker role times out; removal succeeds and an exact-name daemon query confirms absence | Trusted adapter emits a fenced timeout; capture before secondary guards; verify the confined source without another developer call |
| Removal or absence check fails, local timeout, or dispatch outcome unknown | `OUTCOME_UNCERTAIN`; preserve workspace/reservation, no second invocation |
| Timed-out candidate passes required checks | Checkpoint with `termination: timeout`, `exit_code: null`, `completion_confirmation: unconfirmed_completion`; no ticket/PR readiness |
| Timed-out candidate fails code checks | Preserve partial candidate and normal repair context; the next developer consumes the next attempt and repairs in place |
| Launcher/timeout/resource exit (124–127), empty suite, or failing clean preflight under I2 | `ENVIRONMENT_DIAGNOSIS`; preserve evidence, no model code-repair loop. Broader collection/witness classification remains I4 |
| Protected, unsafe or out-of-scope source | Existing capture and scope/integrity guards still refuse adoption |
| Inference HTTP error/lost response | One POST only. Current route has no tested non-dispatch/idempotency guarantee, so no automatic resend, even for 429/503/504 |
| Future trusted adapter proves non-dispatch on a supported operation | Persistent retry reservation, exponential backoff plus supplied bounded jitter, named retry allowance and original deadline; uncertainty grants no replay authority |
| Git index lock exists without a host ownership/creation receipt | `GIT_LOCK_UNOWNED`; retain the file. Neither age nor a PID lookup authorizes deletion |
| Stable ticket/scheduler/limits `flock` files exist | Reuse the existing kernel locks; never unlink them |

Resource telemetry anchors its host destination before Docker launch, so a
container-created mountpoint cannot redirect the evidence into candidate source.
Only the trusted adapter constructs fenced timeout metadata. Repository output
or a model completion marker cannot establish quiescence. The containment claim
assumes the Docker daemon and host remain trusted; privileged escape/external
writers remain outside this boundary and B work is still required.

## Design convergence

Bounded design sessions keep full immutable ADRs/reviews and host-generated ADR
deltas (at most 12,000 characters per delta in history), short review excerpts,
compact prior assessments and references to the complete retained artifacts.
They preserve stable approved obligation IDs and all round/review reservations.
New prose, hashes, finding IDs and unsupported assertions do not count as progress.

Progress is either a supported independent-review resolution of an obligation,
or a new host-verified evidence hash cited against approved obligations.
Registered static source-inspection facts may supply that evidence. Inspection
uses AST parsing, never imports repository code, and is explicitly not a
correctness oracle. Unregistered/duplicate evidence cannot reset the counter.
The diagnosis is model text and cannot approve the design or become host proof.

After two consecutive revisions without progress, reserve one explicitly allowed
diagnosis, within the same role/phase/total bounds. No allowance or no remaining
repair round means a registered stop. A lost diagnosis response stays uncertain;
resume cannot call it or the architect again. A captured diagnosis survives
restart and informs the next round. If that round still shows no progress,
`DESIGN_STALLED` persists. The five-round initial limit does not become six
implicitly. Changing the policy of an existing design session requires an
explicit migration; legacy sessions retain their old contract.

## Stop inventory and CI authority

`ssdlc_recovery.STOPS` maps every new recovery reason into the existing eight
supervisor classes without changing authentication/integrity precedence:

| Reason | Existing class | Allowance / exhausted behavior |
| --- | --- | --- |
| `LIMIT_REACHED` | limits | Frozen invocation/time/cost/task limits; explicit stop |
| `OUTCOME_UNCERTAIN` | uncertain_outcome | No automatic redispatch or refund |
| `GIT_LOCK_UNOWNED` | operator_decision | No automatic cleanup without ownership proof |
| `DESIGN_STALLED` | operator_decision | One named diagnosis at most; no hidden round |
| `ENVIRONMENT_DIAGNOSIS` | transient_environment_provider | Evidence retained; supervisor does not automatically restart a controller that already dispatched |

This is the incremental A3 inventory, not a claim that all older legacy stops
have been normalized. Existing failure precedence, ownership checks, capture
integrity and privacy cases remain in the complete suite.

The host-owned `i2-recovery-v1` manifest pins nine scenarios and exact outcomes,
invocations, attempts, diagnoses and checkpoint status. Candidate worktrees
cannot change this grader/manifest through their writable mounts. CI runs the
entire unit suite, including `test_recovery_benchmark`, which executes and grades
the complete replay corpus. The same gate is available standalone through
`PYTHONPATH=tests python3 -m replay.benchmark --output FILE`.
Any wrong outcome/counter or exceeded case-time tolerance fails the gate. Changes
to baseline expectations are explicit reviewed fixture changes.

Reports separate checkpoints, expected stops, ready PRs, elapsed time, logical
invocations and live model calls. Expected stops still count required operator
interventions; no callback execution or waiting is fabricated. The current
corpus expects three successful engine outcomes and six operator-requiring
policy stops, with zero avoidable interventions. It does not claim ready-PR
completion, the ten independently verified C-gate defects, or production latency
and cost guarantees. See `docs/evidence/i2-offline-validation.json` for sanitized
provenance. Raw transcripts, authorizations and run journals stay private.
