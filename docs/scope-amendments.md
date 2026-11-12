# Explicit scope amendments after a checkpoint

Added targets that were changed by a completed task now carry the native
[owner-revalidation contract](developer-feedback.md#completed-owner-scope-recovery-38).
Preview lists the owners and check obligations; preparation requires explicit
owner coverage review. The scheduler verifies those owners on the amended
candidate before accepting the introducing task. Existing feedback requests
can be bound through `--feedback-request`, with exact operator authorization
and no repeated model calls. A changed overall feedback-evidence hash requires
the separately declared `--reconcile-feedback-evidence` path; semantic inputs
and recorded request evidence must remain unchanged.

Offline validation and its limits are recorded in
[the sanitized validation summary](evidence/post-checkpoint-scope-validation.json).

This implements the post-checkpoint scope-recovery case needed by #22 under A6a
of `PLAN_AUTONOMY_WORKSTREAMS.md`. I1's `pre-checkpoint-import-v1` remains
unchanged. An admitted I2 generation can prepare a separate, reviewed generation
when exactly one uncompleted task stopped with `SCOPE_REQUIRED`.

The operator must stop and fence the source controller, supervisor and other
writers. Cooperative locks, Docker mount inspection and repeated hashes detect
cooperating writers; `source_fenced: true` remains an operator attestation.
Hashes bind host-owned references; they are not authentication against a host
user able to replace both payload and authorization.

## Compatibility and accounting

Only literal target-path additions to that one task are accepted. The plan
revision advances exactly once. Requirements, task identities, dependency order,
checks, quality policy, base and verification environment stay unchanged.
Protected paths and symlink targets are refused. Other tasks must be completed
or pending with zero attempts. Unresolved dispatch outcomes and other recovery
gates block preparation. Attempts, budgets, invocation caps and the absolute
deadline cannot be extended through this transition.

Preparation executes no candidate code and makes no provider calls. It retains
the original journal prefix, failures, captured patch, proof, limits and
accounting journal in a separate generation. Completed receipts remain checked
against their original input bindings; they are historical proof, not fresh
results attributed to the revised plan. The native scheduler performs its
required clean-checkpoint preflight before the next developer dispatch. A failed
preflight stops execution. The original `SCOPE_REQUIRED` record is retained; a
separate authorized reconciliation record permits restoring that patch and
consuming the next existing attempt.

## Native workflow

Use a pinned checkout of the latest merged controller. Keep all input, proposal
and runtime files private because they include full historical evidence.

1. `python3 orchestrator.py continuation-scope-inspect --repo TARGET --ticket TICKET`
   checks the selected stopped generation and reports attempts, cumulative
   invocations and deadline without repairing journals.
2. Create the revised plan, then run
   `continuation-scope-preview --repo TARGET --ticket TICKET --plan PLAN --reason REASON`.
   The host calculates the exact subject and scope diff. The bounded reason is
   reviewed operator context for repair; it does not authorize execution.
3. Obtain an independent review of that subject and store this envelope:

   ```json
   {
     "subject_sha256": "<preview subject digest>",
     "reviewer": "<independent reviewer attribution>",
     "report": {"verdict": "APPROVED", "findings": []},
     "scope_diff": {"T02": {"added": ["tests/test_unified_execution.py"], "removed": []}}
   }
   ```

4. `continuation-scope-prepare --repo TARGET --ticket TICKET --plan PLAN --reason REASON --review REVIEW`
   stages the new generation and prints its preparation path and proposal hash.
   Preparation fences ordinary dispatch. Nothing is selected or dispatched.
5. Review the sealed preparation and authorize the exact proposal separately:

   ```json
   {
     "schema_version": 1,
     "transition_id": "<proposal ID>",
     "proposal_sha256": "<proposal digest>",
     "old_controller_sha256": "<old execution-contract digest>",
     "new_controller_sha256": "<new execution-contract digest>",
     "scope_diff_sha256": "<exact host scope-diff digest>",
     "authorized": true,
     "source_fenced": true
   }
   ```

6. `continuation-scope-activate --repo TARGET --ticket TICKET --preparation PREPARATION --authorization AUTHORIZATION`
   rechecks the source, staged payload, effective limits and exact parent selector
   under stable locks. It atomically selects the new generation, without model
   dispatch. Retry the exact command after an interruption; it does not select an
   older parent or duplicate a selected transition.
7. Resume through native `run --from-phase 03-dev`, with the original operator
   boundary before paid global gates. Activation grants no readiness or sign-off
   and does not authorize a broader pipeline.

An unselected or rejected preparation is discarded through
`continuation-scope-discard --repo TARGET --ticket TICKET --preparation PREPARATION`.
The existing owned-resource discard archives evidence, confirms quiescence and
removes only the recorded staged worktree. It never removes the selected parent.

## Remaining boundaries

The ordinary scope-only rule does not merge/split tasks, alter requirements, loosen checks, change
verification environments, grant attempts or raise budgets. Arbitrary controller
migration, invalidated-task recovery and automatic replanning still require their
own compatibility rules. Final review/QA/AppSec and delivery requirements remain
those of the original run. #36 separately tracks native phase-boundary stops and
retaining failed-baseline diagnostics during I1/I2 preparation.

## Operator repair after a verification timeout

`operator-verification-repair-v1` is a separate, exact operator capability for a
single failed task with a durable, quiescent candidate and an observed verifier
timeout (`ENVIRONMENT_DIAGNOSIS`, check exit 124). Other failed checks must have
exit 0 or 124. It is not an automatic retry and does not reinterpret a timeout
as a successful test run.

The proposal may add at most four literal Python test paths to that task, grant
exactly one next attempt when the current attempt allowance is exhausted, and
increase timeouts of quality checks that actually timed out. The check command,
working directory, requirement coverage, required status and all other quality
fields stay identical. Each new timeout is bounded by the existing invocation
timeout. Existing owner obligations are inherited and require exact coverage
review and fresh candidate checks, including separately reviewed fixture edits.
Production source already in the task manifest can be repaired by its next
developer; this capability cannot add production targets.

`development_attempts` is the run's default ceiling, so its one-step increase
also applies to other uncompleted tasks. Their counters are not reset. For
example, changing 4 to 5 gives an exhausted T12 only attempt 5, while a pending
T13 receives the new default ceiling of 5. Review this effect explicitly.

Budget caps, historical unknown-usage reserves, invocation and role caps,
deadline, invocation timeout, transport retries and automatic feedback grants
cannot change. Consumed attempts, failed-attempt records, captures and usage
remain historical facts. A new development call consumes the next attempt and
one invocation; preview, preparation and activation dispatch no model.

Prepare `PLAN`, `QUALITY` and `PROFILE` from the stopped generation. `PLAN` is
the ordinary scope-only addition; `QUALITY` changes only observed timeouts;
`PROFILE` differs only in `development_attempts`, increased by exactly one.
Preview and prepare use the existing CLI with a separate capability:

```bash
./ssdlc continuation-scope-preview --repo TARGET --ticket TICKET \
  --plan PLAN --reason REASON \
  --verification-repair-quality QUALITY --verification-repair-profile PROFILE

./ssdlc continuation-scope-prepare --repo TARGET --ticket TICKET \
  --plan PLAN --reason REASON --review REVIEW \
  --verification-repair-quality QUALITY --verification-repair-profile PROFILE
```

The independent review binds the preview's exact subject, scope diff and owner
coverage contract. The ordinary fenced authorization additionally requires the
proposal's `profile_sha256`, `quality_contract_sha256`,
`verification_repair_sha256`, and `authorize_extensions: true`. Ordinary scope
or owner-feedback consent cannot authorize this operation. Activation and
discard use the existing commands; resume still uses native
`run --from-phase 03-dev` and the run's previously agreed phase boundary.

If feedback recording already exists, the source must be an admitted generation.
Its authority, requests and funded responses remain immutable. A separate manual
receipt counts the added scope against cumulative amendment/file consumption,
even when the operator explicitly exceeds the automatic grant. Automatic grants
are not enlarged. The selected generation remains fenced until that receipt is
reconciled; replay creates neither a second receipt nor another extension.

This repairs execution capacity and reviewed scope. It neither establishes code
correctness nor replaces final review, QA, AppSec or integration evidence.

### Separately authorized deadline extension

`operator-verification-repair-deadline-v1` adds a distinct deadline capability to
the same reviewed timeout/fixture repair. It can prepare and activate after the
old execution deadline has expired, provided the new proposed deadline is still
in the future and every other capacity and integrity check passes. It cannot
recover an uncertain invocation, replenish money or reset task history.

Pass `--verification-repair-deadline UNIX_SECONDS` to preview and prepare along
with the quality and profile arguments above. The new profile raises
`wall_seconds` by exactly `new_deadline - old_deadline`, in integral seconds,
in addition to the already reviewed one-step default attempt increase. All
other profile fields stay identical. A new deadline cannot borrow v1 consent.
Authorization additionally requires the proposal's
`deadline_extension_sha256` and `authorize_deadline_extension: true`.

The original `started_at` and historical accounting anchor remain immutable.
A bound extension chain and a new limits-journal event record the old/new
deadline and wall allowance. Staging validates the proposed counters without
making them executable; only exact activation selects the new generation.
An already expired proposed deadline is refused. Replay does not extend it
again, and a changed extension chain or live deadline fails integrity checks.

This changes the native execution deadline. It does not renew a separate,
expired feedback-recording policy: its authority, expiry, requests and funded
role history remain unchanged. Such a recording policy still requires its own
admission before further automatic consultations; deterministic checks and
authorized native development use the new execution deadline.
