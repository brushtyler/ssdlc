# Native gate convergence

Native gate convergence collects deterministic candidate checks before the review
that needs them. Newly captured required checks set `candidate_proof: true`,
including auto-discovered and explicit bootstrap contracts. Historical sealed
contracts are loaded unchanged and keep their original proof semantics. Every new
run uses task execution; the obsolete task-breakdown flag is ignored.

Unified execution, explicit greenfield bootstrap and corrective bounce (Step 3b.3)
are implemented. See the [task-mode guide](task-mode.md#unified-execution-and-greenfield-bootstrap) for executed
CLI/profile examples, exit codes and per-process metric names.

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

Candidate-proof Ops/Docs use the verified candidate commit and clean worktree
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

Records are additive, but a code revert does not make every ticket executable.
Historical ordinary task records retain their sealed semantics. Bootstrap contracts
with an absent baseline, and new contracts requiring positive test-count proof, may fail older schemas/validators and must not be resumed
under reverted code. Invalidated/open/failed corrective tickets require restoring
the matching pre-invalidation journal/snapshot, implementation diff and registry
checkpoint before old code can execute them. Without that restoration they remain
readable only. Uncertain dispatch has no automated operator-resolution command.

1. Ignored-file contents are not checked for mutation; HEAD, tree and non-ignored
   status are checked.
2. Environment identity and privilege are determined at collection and are not
   re-probed during evaluation. Local identity covers the allowlisted environment,
   interpreter and resolved tool binary, not every transitive dependency. Reuse
   is declined when an identity element cannot be established.
3. A failed verified record is reused for the same binding. The remedy is changing
   source.
4. Historical contracts are never rewritten to activate candidate proof.
5. Trusted substantive Review/QA/AppSec rejection starts an engine-owned corrective
   transaction. Preview remains `execution_authorized=false`. Roots are repaired,
   dependents revalidated and all global gates rerun on the final verified commit.
   Stale evidence, source mutation, exhausted attempts, budgets and cancellation
   stop at verified boundaries. Recovery adds required acknowledgments/work without
   duplicate invalidation or repair; only COMPLETED/APPROVED replay is zero-write.
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

The unified acceptance suite is `test_unified_integration.py`: deterministic fake
Claude/Codex processes and sandbox commands exercise real CLI/pipeline entry points,
bootstrap checkpoints, corrective gates, journal reconciliation and limit boundaries.
No network or provider credentials are required. These offline checks do not claim
real Docker/provider acceptance. Advanced-target merge, automatic scope migration,
GitHub #4, accounting #14, quota polling/new billing units, parallel scheduling,
advisory acceptance and subgoals remain later milestones.

## Stage observability

The CLI emits UTC timestamps and bounded stage events to stderr, preserving JSON
stdout. Mode decisions, obsolete-flag warnings and bootstrap eligibility identify
the run. Corrective creation, invalidation, repair, revalidation, reconciliation,
replay, interruption, gate reruns and completion identify the run, transaction,
bound preview prefix and task IDs. These events contain reason codes and IDs,
not finding prose or test-log bodies. Ordinary diagnostic evidence is retained
separately under the existing access and sanitization rules.

`corrective_gates_rerun` counts the fresh gate traversal after a repair. The other
corrective, bootstrap and legacy counters are listed in the task-mode guide; all
are per-process observations, with no new billing units. CLI logging setup does
not configure the root logger or unrelated libraries. Library callers can attach
their own logging handlers.

## Positive test-count evidence

Newly captured quality contracts require a positive reported test count for typed
`test` checks, both at baseline and for candidate/scheduler verification. A silent
exit-zero command such as `true` is not test evidence and cannot seal a passing
baseline. Silent lint/type checks remain valid when their exit code passes.

Recognized summaries include Python unittest/pytest, Jest/Vitest, Node TAP and
Mocha. An unrecognized test summary blocks until the runner emits a supported
count; it is not guessed to be a passing nonempty suite. The final recognized
summary takes precedence over earlier or nested examples.
Skipped tests do not provide positive proof. An all-skipped unittest, Jest/Vitest
or TAP suite blocks even when its process exits zero; mixed suites must report
at least one executed test. A total without an executed/passed breakdown is not
accepted as a Jest/Vitest passing count.

The `positive_test_count_required` capability is sealed in new contracts. Loading
a historical contract preserves its bytes and earlier semantics. It is never
added by resume. Its digest is part of candidate evidence, so an older weaker
record cannot certify a new contract. After a code revert, records remain
readable, but a contract with an unsupported capability must not execute under
the older validator. No historical contract or journal is migrated.
