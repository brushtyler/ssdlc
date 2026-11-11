# Explicit scope amendments after a checkpoint

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

This rule does not merge/split tasks, alter requirements, loosen checks, change
verification environments, grant attempts or raise budgets. Arbitrary controller
migration, invalidated-task recovery and automatic replanning still require their
own compatibility rules. Final review/QA/AppSec and delivery requirements remain
those of the original run. #36 separately tracks native phase-boundary stops and
retaining failed-baseline diagnostics during I1/I2 preparation.
