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
scheduler. A missing role/phase cap refuses dispatch. The outer dev/review/QA cycle also
uses the frozen development-attempt ceiling, rather than current environment
defaults.

Authorization publishes a persistent `execution-mode.json` outside `limits/`
and binds its policy digest into `state.json.bounded_execution` as soon as state
exists. An INTENT marker precedes initialization; ACTIVE follows the durable
limit journal. Interrupted initialization retries the same binding without
resetting an existing journal. Every entrypoint fails closed if policy/journal
or either mode binding disappears or changes, including non-autonomous launches.
Native state writes retain the binding even from a stale state projection. This
adds a marker for newly authorized runs; it is not permission to copy a new
controller into an existing run. Historical admission uses #27's transaction,
which publishes the same marker after its final accounting binding is sealed.

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
cumulative-limit reconciliation/authorization is supported through
[issue #27](https://github.com/brushtyler/ssdlc/issues/27), which completes I2.
PR #26 is its independently reviewable first increment for new runs. In particular this
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
| Bounded I2 inference HTTP error/lost response | One POST only. Current route has no tested non-dispatch/idempotency guarantee, so no automatic resend, even for 429/503/504 |
| Bounded transport retries | Disabled in production until a route has tested non-dispatch or replay-safety proof. `LimitsStore.transport` is a reservation primitive, not a connected retry loop; its supplied jitter and `not_before` are not claimed as production scheduling |
| Legacy HTTP route without a frozen I2 profile | Restored three-attempt retry for 429/503/504, URL transport errors and timeout, with generated jitter and bounded backoff; auth failures do not retry. This is legacy compatibility, not I2 safe replay or an autonomous route |
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
Resolved obligations are tracked across all evaluated rounds, including those
already marked resolved in the first review; reopening and resolving one again
cannot count as new progress. Empty/interrupted ADR rounds do not erase that
history or the stagnant streak.

Progress is either a supported independent-review resolution of an obligation,
or a new host-verified evidence hash cited against approved obligations.
Registered static source-inspection facts explicitly tagged
`purpose: progress_evidence` may supply that evidence. Untagged legacy reports,
unknown purpose values and diagnosis reports cannot count as progress. Inspection
uses AST parsing, never imports repository code, and is explicitly not a
correctness oracle. Unregistered/duplicate evidence cannot reset the counter.
The diagnosis is model text and cannot approve the design or become host proof.
Its attached host inspection is tagged `purpose: diagnosis_context`, excluded
from the progress catalogue and rejected by the host evidence predicate. A cited
report from that diagnosis cannot manufacture progress on the next round.

After two consecutive revisions without progress, reserve one explicitly allowed
diagnosis, within the same role/phase/total bounds. No allowance or no remaining
repair round means a registered stop. A lost diagnosis response stays uncertain;
resume cannot call it or the architect again. A captured diagnosis survives
restart and informs the next round. If that round still shows no progress,
`DESIGN_STALLED` persists. An empty/interrupted ADR after diagnosis, an
unavailable valid review, or exhausted authorized rounds also produces a
registered stop without allocating additional rounds. The five-round initial limit does not become six
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

The host-owned `i2-recovery-v2` manifest pins twelve scenarios and exact outcomes,
invocations, attempts, diagnoses and checkpoint status. Candidate worktrees
cannot change this grader/manifest through their writable mounts. CI runs the
entire unit suite, including `test_recovery_benchmark`, which executes and grades
the complete replay corpus. The same gate is available standalone through
`python3 -I ssdlc_replay_gate.py --output FILE`.
Any wrong outcome/counter or exceeded case-time tolerance fails the gate. Changes
to baseline expectations are explicit reviewed fixture changes.

Reports measure checkpoints, expected stops, elapsed time and logical
invocations. Ready-PR, live-model-call, callback and operator-wait fields have
been removed because this harness does not measure them. Expected stops still count required operator
interventions; no callback execution or waiting is fabricated. The current
corpus expects three successful engine outcomes and nine operator-requiring
policy stops, with zero avoidable interventions. It does not claim ready-PR
completion, the ten independently verified C-gate defects, or production latency
and cost guarantees. See `docs/evidence/i2-offline-validation.json` for sanitized
provenance. Raw transcripts, authorizations and run journals stay private.

## Historical I1 admission

[Issue #27 admission](historical-reconciliation.md) provides a separate exact-hash
transition from a stopped I1 generation into cumulative native I2 enforcement.
It never installs a fresh profile over historical consumption. Other new-run I2
hardening remains in issue #29.

## Protected baseline and issue #29 validation

`schemas/recovery-grader-baseline.json` (`i2-integrity-v3`) pins the grader,
expectations, replay fixtures, imported test helpers and existing CI gate by
SHA256. The host verifies those bytes before executing the corpus and before
self-development planning/scheduling. Self-development is identified from the
immutable base commit, not role-created files. Candidate manifests cannot target
those files, the baseline control file, or any path under `tests/replay/`;
capture also blocks them if validation is bypassed. Direct, explicitly reviewed
baseline changes must update the pins alongside the code. Hashes are host-owned
integrity references, not authentication against a host user who replaces both.

The v2 baseline adds oscillating findings, diagnosis-as-evidence and empty ADR
after diagnosis. `escaped_seeded_defects` is derived from the seeded repair's
observed source/checkpoint, not a constant success claim. Other metrics remain
out of scope until measured. The standalone replay is still
`python3 -I ssdlc_replay_gate.py --output FILE`; existing CI unit
discovery executes it and rejects pin drift or wrong outcomes/counters.

`tests/test_i2_hardening.py` additionally covers deleted/tampered mode bindings,
each bounded entrypoint, capture integrity refusals and active writers, isolated
protected violations, real role-adapter timeout fencing, frozen outer limits,
legacy HTTP retry and completion disclosures. Existing `test_not_dispatched`
regressions from #27 remain authority for trusted prelaunch refusal. Missing
role executables, unavailable native developer isolation, missing AGY credentials
and refused supervisor dispatch intent also carry trusted non-dispatch proof; arbitrary
post-launch exception text/category does not authorize replay. Timeout/import
completion uncertainty appears in the evidence pack and the explicit sign-off
output; passing task checks still grants no global readiness by itself.
Deterministic evidence compilation remains an authorized service operation after
historical admission, so a limit/stop report does not require another model call.


Issue #32 isolates the A5 CI job from general unittest discovery. The pinned
`ssdlc_replay_gate.py` requires Python isolated mode and stages only the pinned
test modules/fixtures in a disposable import directory. The original `tests/`
is never added to its import path. New discovery modules cannot monkeypatch this
process; `PYTHONPATH` and user site are ignored. The unit gate also invokes the
isolated entrypoint, but CI independently runs it in a separate job. Production
modules remain the candidate under test; this is import isolation from discovery,
not a sandbox against arbitrary malicious production code or host dependencies.
The v3 baseline preserves all twelve corpus cases and expected outcomes. Target
aliases are normalized before grader protection checks; parent traversal remains
forbidden. Controller snapshots ignore global/system Git configuration and
injected Git config, and override global excludes/fsmonitor, retaining only
repository-owned ignore rules.
