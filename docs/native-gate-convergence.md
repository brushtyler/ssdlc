# Native gate convergence

Native gate convergence collects deterministic candidate checks before the review
that needs them. It is opt-in: an explicit quality contract must set
`candidate_proof: true` on at least one required check. The auto-discovered
contract does not opt in. Without that switch, the legacy pipeline and artifact
formats remain unchanged, including the legacy Ops/Docs context limitations.

## Candidate evidence and decisions

Baseline results are historical baseline data, never proof of the current
candidate. Candidate records bind source commit and tree, controller revision,
quality-contract digest, exact command (including cwd and timeout), environment
identity, privilege mode, result, log digest and provenance. Records and logs are
registered, confined and digest-verified; missing, unsafe, mutated or stale
evidence is rejected fail-closed. Required proof is supplied or input construction
fails, rather than silently dropping required references.

Evidence reuse is limited to proven equivalence across source commit and tree,
controller, contract, command, environment identity and privilege mode. Reuse is
declined when environment identity cannot be established. The check can still
run and supply proof for that collection, but another gate must collect again.
Local checks always decline cross-gate reuse: interpreter and environment hashes
do not establish equivalence of mutable dependencies outside the source tree.
Continuing an existing phase attempt preserves its immutable catalogue snapshot;
that continuation is distinct from reuse in a new collection. Reuse, execution,
rejections and invalidation are recorded in structured decision artifacts.

Checks run before review dispatch, and later QA/AppSec evidence is not demanded
by that review. Source or controller mutation invalidates proof. Passing checks
never grant readiness: all configured review, QA and AppSec approvals remain
required, with the existing structured assessment and blocker rules intact.

## Diagnosis and history

Diagnosis supplies untrusted analysis only and never changes a verdict, clears a
blocker, resolves a finding, or accepts an advisory. Text claiming approval has
no authority over structured assessments.

The diagnosis allowance is bounded (default two per ticket), persisted, and
never reset. Immutable request, evaluation association, reservation, consumption,
result and uncertainty records are separate from development-attempt accounting.
Reported diagnosis costs, tokens and duration enter the existing ledger exactly
once, including recovery of a persisted result whose ledger write was interrupted.
The producer remains the one recorded at dispatch. A budget refusal before dispatch
does not become an uncertain invocation or consume an inference. This preserves
the existing accounting units; subscription/API billing classification is separate.
Consumption is persisted before dispatch. Duplicate requests and restarts do
not grant another inference; a lost response or uncertain dispatch is recorded
as uncertain, without a fabricated result or implicit retry. Exhaustion and
uncertainty require manual intervention. Operators must inspect the referenced
records and reject or change source as appropriate; exhaustion requires rejection
or a new ticket. There is no provider switch or allowance increase.

History is an immutable per-phase-attempt snapshot, persisted before the phase
roster and reused verbatim on resume. Completed earlier groups are ordered by
attempt, then review, QA and AppSec. Same-group and future findings are excluded.
Relevant findings and registered diagnoses carry provenance; relevance uses task
or file intersection rather than semantic matching. Newly available findings or
diagnoses reach only later phase attempts. Bounded history and excerpts disclose
omissions; history never automatically resolves findings.

## Ops/Docs context and integrity boundaries

Opted-in task-mode Ops/Docs use the verified candidate commit and clean worktree
as their cwd. The implementation diff is full-or-rejected: an oversized diff
fails rather than being silently truncated. Other inputs may be truncated, with
machine-readable disclosure of included and total sizes, truncation flags and
`full_audit_coverage_permitted`. Truncated input forbids complete-audit claims.
The host supplies that instruction and disclosure but does not parse model prose
to enforce coverage claims. Candidate integrity is rechecked after invocation.

New artifacts use immutable, create-exclusive publication and registered hashes.
Hashes detect change, not authenticity. An external writer able to replace both
artifacts and their registry is outside this integrity model. Gate-readable refs
are modelled as host-readable: roles receive captured evidence and inline
excerpts rather than opening the state tree themselves. Protected runtime
artifacts, seals, policy and the executing controller are not modified.

Structured artifacts expose check execution/reuse/rejections, diagnosis lifecycle
and allowance consumption, history snapshots and context truncation without log
parsing. Check logs are tail-bounded to 1 MiB, but raw command output is not
scrubbed for secrets. A runner resource retry may succeed; its returned result
is recorded, while an exhausted resource retry returning 125 is a failed check.

## Rollback and known limits

Rollback is additive: reverting code leaves new artifact directories inert and
preserves prior state and evidence without deletion or rewriting. Opted-in
contracts fail old schemas loudly because `candidate_proof` is not accepted by
their closed check schema. Legacy contracts continue on their existing path.

1. Ignored-file contents are not checked for mutation; HEAD, tree and non-ignored
   status are checked.
2. Environment identity and privilege are determined at collection and are not
   re-probed during evaluation. Local identity covers the allowlisted environment,
   interpreter and resolved tool binary, not every transitive dependency. Reuse
   is declined when an identity element cannot be established.
3. A failed verified record is reused for the same binding. The remedy is changing
   source.
4. The default auto-discovered contract does not opt in.
5. Task-mode review rejection ends the run. Later evaluations come from later
   gates or operator-driven resumes; no bounce execution or scheduler is added.
6. An operator-resolution command for uncertain diagnosis is out of scope.
   Resolution requires changed source or rejection.
7. The diagnosis lock coordinates cooperating processes only. Holding it across
   dispatch serializes diagnoses; the default allowance is two.
8. Check logs are tail-bounded but not scrubbed for secrets.

## Offline verification

The focused `unittest` files are `test_candidate_evidence`, `test_gate_sequence`,
`test_gate_diagnosis`, `test_gate_diagnosis_history` and `test_opsdocs_context`.
They use local fixtures and mocked execution/provider boundaries; no network
service is required. They cover stale bindings, privilege differences, mutation,
unsafe refs, strict decisions, sequencing, diagnosis recovery and exhaustion,
history, Ops/Docs disclosures and protected-artifact integrity. AC-25 verification
runs all five files and the existing full suite unmodified; AC-14 is checked by
legacy compatibility coverage. Run the global suite on a temporary copy of
tracked sources when subprocesses could generate ignored files in the checkout.
