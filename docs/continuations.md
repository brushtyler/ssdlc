# Native stopped-run continuation (I1)

This implements the first compatibility rule in `PLAN_AUTONOMY_WORKSTREAMS.md`:
`pre-checkpoint-import-v1`. It is direct bootstrap work with offline semantic
engines and a credential-free Docker smoke, not autonomous delivery. Validation
and scope are recorded in `docs/evidence/i1-offline-validation.json`.

I1 accepts a stopped task run with a valid version 1.0 journal, no completed
checkpoint, one previously charged root-task attempt and all other tasks pending
with zero attempts. It imports a reviewed, resolved source snapshot as unverified
work. Completed-checkpoint migration, execution lineage changes, changed
requirements/quality policy and further migration of I0 capture journals need
separate compatibility rules and are refused. The DAG amendment validator can
check merge/split ordering, but this execution compatibility rule deliberately
preserves task IDs and all charged attempts.

For an already admitted I2 run stopped on `SCOPE_REQUIRED` after a completed
checkpoint, use the separate [scope-only amendment](scope-amendments.md) rule.
This does not broaden I1's pre-checkpoint import compatibility.

The operator must stop and fence every old controller, supervisor and external
writer before activation. Cooperative ticket/scheduler locks and repeated source
hash checks detect cooperating writers and changed inputs; they do not prove
that arbitrary same-user host processes have stopped. `source_fenced: true` is an
explicit operator attestation, not a fact inferred from a PID lookup or model
text. Hashes bind host-owned artifacts; they do not authenticate them against a
host user who can replace both payload and authorization. No lock is unlinked.

## Prepare, review, activate, verify

Use a pinned, stable controller from the latest merged main. Updating that
controller after preparation requires a new proposal. The target repository
must be clean at the proposed base. Resolve legacy source against that base
before import: never overwrite newer controller files with whole old files and
call that a rebase.

Commands work through the generated `./ssdlc` launcher, or directly through
`python3 /path/to/controller/orchestrator.py`; each accepts `--repo` and `--ticket`.
The following examples use `python3 orchestrator.py` from the controller checkout.
Keep all input/proposal/runtime files private; they can contain prompts and logs.

1. `continuation-inspect --repo TARGET --ticket TICKET --source ORIGINAL` checks
   stopped state, registered inputs, raw journal chain and available original
   developer log/manifest without repairing the source journal. It prints only
   summary identities.
2. Supply a plan with revision exactly previous + 1, the same ticket/task IDs,
   preserved obligations and transitive dependency order, a quality contract with
   the new base and unchanged policy, and a resolved source manifest conforming
   to `schemas/continuation-snapshot.schema.json`. Each changed path declares its
   role (implementation, acceptance test, baseline test change/retirement, shared
   test configuration), rationale, content hash and executable bit, or deletion.
3. `continuation-preview --repo TARGET --ticket TICKET --source ORIGINAL
   --task T01 --plan plan.json --quality quality.json --snapshot RESOLVED_SOURCE
   --manifest snapshot.json` prints the review subject and its hash without
   running repository code. Its fixture report includes Python imports, patch
   strings and removed/signature-changed function names. Parse errors are
   explicit. This is a dependency hint, not a coverage proof or test oracle.
4. Obtain an independent review of that subject. Store an envelope with
   `subject_sha256`, a nonempty `reviewer` attribution and `report` containing the
   existing structured task-plan review. An `APPROVED` string alone is invalid.
   Any host-calculated scope expansion must also be declared in the envelope's
   `scope_diff`, and the exact diff digest must be explicitly authorized at
   activation as `scope_diff_sha256`. Protected paths remain forbidden.
   Prior planning assessments remain review history. I1 consumes this supplied
   review without allocating a model call or a new review allowance.
5. `continuation-prepare` takes the same arguments plus `--review review.json`.
   It validates the review binding, runs only baseline quality checks in fresh
   Docker checkouts, validates the amended plan, copies historical evidence into
   `history/`, captures the resolved source and appends `CONTINUATION_IMPORTED`
   after the exact original journal prefix. Future acceptance files are checked
   structurally, and are not executed as baseline tests. Nonzero/empty baseline,
   missing launchers/cwd or source/metadata mutation refuse preparation. The
   output names a private preparation directory and proposal hash. No generation
   is active yet. Preparation fences ordinary run/plan/design on that target.
6. Review `proposal.json`, `recipe.json`, `projection.json`, `runtime-seal.json`
   and the prepared runtime. Authorize that exact proposal in a separate file:

   ```json
   {
     "schema_version": 1,
     "transition_id": "<32 hex characters from proposal>",
     "proposal_sha256": "<canonical JSON digest of proposal>",
     "old_controller_sha256": "<historical controller artifact SHA256>",
     "new_controller_sha256": "<new runtime controller contract digest>",
     "authorized": true,
     "source_fenced": true
   }
   ```

   The old digest identifies the original registered snapshot bytes; the new
   digest identifies the native execution-contract envelope (including the new
   snapshot). The proposal also binds schema compatibility, candidate snapshot,
   inputs, receipt, runtime seal and cumulative accounting. It is not permission
   to run other tasks or increase limits.
7. `continuation-activate --repo TARGET --ticket TICKET --preparation PREPARATION
   --authorization authorization.json` rechecks the live controller/environment,
   old source, allowances and prepared seal under stable locks. It writes an
   immutable intent/generation, fsyncs payload and directories, then selects it
   through atomic `continuations/active.json`. A retry must use the same proposal
   and authorization. It cannot replace an already selected generation.
8. `continuation-verify --repo TARGET --ticket TICKET` runs only the imported
   candidate checks, using a fresh Docker checkout for each check. It may produce
   the task's first checkpoint; all pending tasks remain pending. Failed checks
   preserve the capture and stop. Interrupted verification uses I0's persistent
   maximum of three collections for the charged attempt, never redispatching a
   developer. Checkpoint intent/completion and repeat verification use the native
   journal/evidence checks. The result explicitly reports zero model dispatches
   and `ready_for_signoff: false`.

CLI verification requires Docker and refuses local fallback. The image ID,
limits and verification environment are pinned and checked before/after checks;
controller and artifact guards remain enabled. The standard Docker verification
adapter masks existing role credentials. Full egress/route hardening remains I3;
only the recorded smoke explicitly adds `--network none`.

## Preserved history and crash behavior

The original run is never rewritten or relabelled as current proof. Its registered
inputs, original state, journal prefix, available developer logs and complete
planning/design sessions are preserved and hash-bound. Ledger entries, known
limits and task attempts carry forward exactly. Missing legacy limit fields stay
unknown; they are not replaced with defaults or zero. I1 disables every model
entrypoint on the new generation; I2 must reconcile/authorize limits before
further development. This prevents unused historical session copies from
implicitly creating new model allowances.

The imported producer remains `exit_code: null`, `termination: imported`, with
`completion_confirmation: unconfirmed_import`; original model text remains
historical data. An import receipt never fabricates a successful invocation or
an earlier checkpoint. Old preflights and global gates remain historical. The
new baseline and task proof are freshly collected under the new bindings. A
single verified imported task does not grant ticket/global readiness.

| Interruption | Result / recovery |
| --- | --- |
| Before preparation is complete | Original run recoverable; incomplete generation unselected; use native `continuation-discard` to archive/remove tracked resources before preparing again |
| After complete preparation | Private sealed inputs can be reviewed and activated by path; ordinary dispatch remains fenced |
| During intent/payload staging | No selected partial generation; repeat the exact activation, validating existing staged bytes |
| After atomic selection, before command returns | Exact activation retry returns the same projection, including after verification progresses |
| During adoption/verification/commit | Native capture and journal recovery; same charged attempt, bounded new check evidence, no developer redispatch |
| Tampered source/seal/receipt/proof or changed controller/environment | Explicit refusal; no automatic cleanup, reset or provider fallback |

The old supervisor record remains the history of its pinned old controller. I1
verification is a dedicated foreground command; it does not resume the old
supervisor process or silently redirect its evidence. Use the command output and
new `runtime-generations/<id>/state.json` / `03-dev/tasks/task-journal.jsonl` for
this verification. Cumulative I2 admission is described in [historical reconciliation](historical-reconciliation.md); it starts a new pinned execution generation, never redirects the old supervisor.

Issue #22's real run has only been inventoried read-only while implementing I1.
Its preserved T01 source must still be reconciled with the latest merged base,
independently reviewed and explicitly authorized using this path. No historical
self-reported tests are accepted as fresh results. The issue #20 regression
principle is covered by retaining historical gates separately from new baseline
and task proof; I1 does not reopen or duplicate its gate-evaluator implementation.
