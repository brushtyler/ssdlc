# Specification: Unified Task-Mode Execution, Greenfield Bootstrap, and Targeted Corrective Bounce

RISK_TIER: L · DEV_PROFILE: FULLSTACK

## 1. Overview & Business Goal

SSDLC currently has two execution paths: an optional task-breakdown mode, selected by saved state or `SSDLC_ENABLE_TASK_BREAKDOWN`, and a legacy single-developer executor. This milestone leaves one engine. Every new run uses a validated task plan driven by the existing `TaskScheduler` and its hash-chained journal. It must land before accounting (#14) and the GitHub adapter (#4).

The milestone has three sequential units:

1. **Unified execution and compatibility.**
   - All new runs use task plans. Tier S work gets a one-node plan.
   - The legacy developer executor and its branches are removed.
   - The obsolete flag can no longer select an executor.
   - Historical task runs keep their sealed semantics.
   - Historical legacy runs stay readable but cannot be executed, resumed or approved.
2. **Explicit greenfield bootstrap.**
   - A clean new project with no baseline tests is supported through an explicit, validated bootstrap profile.
   - Eligibility is bounded and tied to the initial committed source and registered policy.
   - It is not an automatic exemption.
3. **Corrective executor.**
   - A trusted, registered, substantive Review, QA or AppSec rejection feeds the existing bounce analysis.
   - That produces an engine-owned durable execution transaction, then `TaskScheduler.invalidate`, targeted root repair, topological dependent revalidation, and all global gates on the final verified candidate.
   - The immutable preview keeps `execution_authorized=false`.

**Non-goals** (out of scope):
- advanced-target merge reconciliation
- automatic scope migration
- GitHub trigger, adapter or publisher
- new billing units or provider quota polling
- parallel scheduler
- advisory acceptance
- subgoal framework
- editing the executing controller (`ssdlc_controller.py`)
- editing runtime artifacts
- new dependencies

## 2. User Stories

- **US-1** As a maintainer, I start any new run and always get a validated task plan, so there is one execution path to reason about and secure.
- **US-2** As a maintainer of a small change (Tier S), I get a one-node plan with the same gates, journal and recovery as larger work.
- **US-3** As an operator with an old run or an old `SSDLC_ENABLE_TASK_BREAKDOWN` setting, I get a clear message and no state mutation. I can still read history and import design sessions.
- **US-4** As an operator with a historical task-mode run, I can resume it with its original sealed semantics.
- **US-5** As a user starting a brand-new empty project, I declare an explicit bootstrap profile. The first implementation checkpoint then proves real, nonempty tests run.
- **US-6** As a user with an existing repository that has tests, I keep strict baseline semantics, and nobody can claim bootstrap for it.
- **US-7** As a reviewer or security owner, a trusted gate rejection leads to a targeted repair of only the impacted tasks. Dependents are revalidated in order and all global gates rerun.
- **US-8** As an auditor, I can verify from the journal and records that every repair was authorized by engine-verified evidence, never by free text, and that replay or crash recovery cannot duplicate or skip work.

## 3. Testable Acceptance Criteria

### Unit 1: Unified execution and compatibility

- **AC-1** Given a new run with no `SSDLC_ENABLE_TASK_BREAKDOWN` set, and no saved task flag, when the orchestrator runs with deterministic fake providers, then a task plan is generated and validated, the `TaskScheduler` journal is created, and no legacy developer executor function is invoked. Developer work is dispatched only through scheduler tasks.
- **AC-2** Given `SSDLC_ENABLE_TASK_BREAKDOWN` set to `false`, `0`, `true`, `1` or an arbitrary value, when a new run starts, then the executor is identical in every case (task engine). The value never selects an executor. Obsolete usage emits one bounded warning log that names the variable and says it is ignored. Behavior is otherwise identical to AC-1.
- **AC-3** Given a Tier S goal, when the plan is produced, then it validates against the task-plan schema with exactly one node. That node passes through scheduler, journal, attempts, quality checks and all global gates. No gate is skipped because the plan is small.
- **AC-4** Given a repository search of the source tree, when the legacy developer executor and its mode-switch branches (for example `if task_mode`, legacy resume checks, legacy bootstrap capture) are checked, then no executable legacy path remains. Test: a static check plus a coverage test shows `orchestrator.py` has no code path that dispatches developer work outside the scheduler.
- **AC-5** Given `setup-ssdlc.sh`, `requirements.txt`, the CLI help, the supervisor, README/docs and fixtures, when inspected, then none sets or documents the flag as an enabler. `jsonschema` is documented as required. The setup script no longer writes `SSDLC_ENABLE_TASK_BREAKDOWN=false`.
- **AC-6** Given a historical run whose saved state is legacy (no sealed task journal, or `task_breakdown` false or absent), when `run`, `resume` or `approve` is invoked, then the command exits nonzero before any write. The error is actionable: it states the run is legacy and not executable, and gives the new-run command. A before/after hash of the state, journal, evidence and contract files is unchanged.
- **AC-7** Given the same historical legacy run, when read-only commands (status, history/list, report viewing, design-session history import) are invoked, then they succeed and modify no records.
- **AC-8** Given a historical legacy run whose state contains an old `task_breakdown: true` flag or an old approval flag but no valid sealed plan, journal and approval record, when execution is requested, then it is refused as in AC-6. Task-plan approval is never inferred from a flag, and no journal is silently converted.
- **AC-9** Given an existing task-mode run with a sealed contract and a hash-chained journal, when it is resumed under the new code, then it continues with unchanged semantics: the journal chain verifies, sealed contract hashes are unchanged, attempts and budgets are preserved, and the sealed contract bytes are not rewritten.
- **AC-10** Given a newly generated quality contract, when it is captured, then candidate-proof checks are active in it (the contract records this explicitly). Given a historical sealed contract, when it is loaded, then it is not mutated and does not claim candidate-proof checks.
- **AC-11** Given QA shell commands recorded in a run, when contracts and reports are produced, then they are not represented as typed-check equivalents. Typed checks are recorded separately and the equivalence claim is absent.
- **AC-12** Given the supervisor launch and resume preflight, when it meets a legacy run, a legacy flag or a legacy-approval state, then it refuses before mutation with the same guidance as AC-6. It still launches new runs and resumes historical task runs.

### Unit 2: Greenfield bootstrap

- **AC-13** Given a clean repository whose head is its initial commit, with no test files or runnable baseline, and a valid bootstrap profile declared explicitly and validated against its schema, when the run starts, then the quality contract records baseline status `absent` (not `passed`). It records bootstrap eligibility bound to the initial commit hash and the registered policy hash.
- **AC-14** Given the same repository with no bootstrap profile, when the run starts, then strict baseline semantics apply and the run fails or blocks on the missing baseline. No automatic exemption applies.
- **AC-15** Given a repository with existing tests (discovered checks that run), when a bootstrap profile is supplied, then it is rejected as ineligible with no mutation. The strict baseline contract is captured as before.
- **AC-16** Given a repository that is dirty, has more than the initial commit, or whose source does not match the committed baseline hash, when bootstrap is requested, then it is ineligible. No contract is sealed.
- **AC-17** Given an eligible bootstrap run, when the first implementation checkpoint is reached, then every required test/check command in the profile must exist, execute, and run at least one test. Failure of any condition (command missing, zero tests, nonzero exit, timeout, interrupted) is a failure, not a success, and the gate blocks.
- **AC-18** Given a bootstrap run whose first checkpoint passes with nonempty tests, when later tasks run, then the exemption is spent. The contract is converted to a recorded post-bootstrap state and strict semantics apply for the rest of the run.
- **AC-19** Given a bootstrap run that was interrupted before the first checkpoint passed, when it is resumed, then the exemption is not extended. The run refuses or fails explicitly and never marks the checkpoint successful.
- **AC-20** Given a bootstrap run, when it executes, then required quality checks and protected-path enforcement are active for every task, with no bootstrap-specific bypass. A task touching a protected path is rejected exactly as in a non-bootstrap run.
- **AC-21** Given a bootstrap profile that is malformed, has unknown fields, has an unbounded eligibility window, or is not registered in policy, when it is loaded, then it is rejected with a schema or policy error before any state write.

### Unit 3: Corrective executor

- **AC-22** Given a trusted gate report whose Review, QA or AppSec outcome is a registered, substantive rejection (it matches the roster and completion records, and the report is validated by `ssdlc_gate_reports.py`), when the real pipeline reaches that gate, then it enters the bounce analysis and corrective executor path. It does not stop at a standalone helper.
- **AC-23** Given a bounce preview, when it is produced, then it is immutable and carries `execution_authorized=false`. No code path mutates it to true. Authorization exists only as a separate engine-owned transaction record linked to the preview ID and hash.
- **AC-24** Given a candidate transaction, when it is created, then it is written durably (atomic write, journaled) before any invalidation. It binds all of these: preview hash, reviewed commit hash, plan hash, quality-contract hash, registered roster, completion records, gate outcomes, and the current journal head hash. Each is re-verified at creation.
- **AC-25** Given verified evidence, when the transaction executes, then it calls `TaskScheduler.invalidate` for exactly the preview-identified root tasks. It then repairs those roots and revalidates dependents in topological order, and finally reruns all global gates (Review, QA, AppSec) on the final verified candidate commit. The final approval is recorded only after all gates pass on that candidate.
- **AC-26** Given free-text feedback, reviewer prose, or a hand-written "authorized" field in any file, when it is presented, then no repair is authorized or started. Only the engine-owned verified transaction authorizes.
- **AC-27** Given evidence that is stale (journal head changed, reviewed commit differs from the current head), mutated (a hash mismatch for the plan, contract, roster, completion or report), missing, diagnostic-only, requiring replanning, or ambiguous or broad in impact, when execution is attempted, then it is refused with a bounded error and no roots are guessed. The journal has no invalidation event and the source is unchanged.
- **AC-28** Given a scanner or tool failure (as opposed to a substantive finding), when it appears as the rejection, then it is not turned into a source-repair transaction. It is classified as a gate failure needing a rerun or operator action.
- **AC-29** Given a repair, when it runs, then attempts, prior contributions, budgets, dependency order, original journal records and restart ownership are preserved. Counters are never reset. Invalidated records remain in the journal and are superseded, never deleted.
- **AC-30** Given a crash or interruption after the transaction is written, during invalidation, during repair or during revalidation, when the run is resumed, then the engine reconciles from the journal and the transaction state. Invalidation is not applied twice and repair is not duplicated. Work whose dispatch outcome is uncertain is not re-dispatched. It is explicitly reconciled or the run stops with a reconcile message. No stale readiness survives.
- **AC-31** Given a transaction that has completed, when the same rejection is replayed (same preview and transaction ID), then it is a no-op that reports the existing outcome with no new journal entries.
- **AC-32** Given a root task with attempts exhausted, when repair is needed, then the transaction fails closed with an attempts-exhausted result. No extra attempt is granted and the run is marked blocked or failed per existing scheduler policy.
- **AC-33** Given source mutation between the reviewed commit and execution (the working tree is dirty, the head moved, or a protected path changed), when execution is attempted, then it is refused before invalidation.
- **AC-34** Given budget exhaustion or cancellation at any point (before invalidation, mid-repair, mid-revalidation, before global gates), when it occurs, then execution stops at a safe journaled boundary. No further provider dispatch occurs. The transaction is marked interrupted and is resumable under the same limits.
- **AC-35** Given a repair that would change scope (new or removed tasks, changed paths outside the plan, changed dependencies), when it is detected, then it is refused with an explicit replanning-required result. No unauthorized edits are applied.
- **AC-36** Given a repair that finishes, when global gates are rerun, then they run on the final verified candidate (candidate evidence bound to the new commit). Prior gate results for the old candidate are never reused as approval. If any gate rejects again, the corrective path applies again within the limits above.
- **AC-37** Given repair prompts, when they are built, then they are bounded in size, contain structured finding references and registered candidate-proof references, and do not append whole test logs when registered proof exists.

### Cross-cutting integration and docs

- **AC-38** Given the test suite, when it runs offline with deterministic fake providers through the real orchestrator entry points, then it covers all of these cases:
  - new default and obsolete flag
  - Tier S single task
  - legacy read-only refusal with unchanged records
  - existing task-run compatibility
  - bootstrap valid, empty suite, failing suite, restart, and ineligible repository
  - approved new candidate
  - Review rejection followed by root repair, dependent revalidation and new gates
  - QA rejection and AppSec rejection
  - stale evidence
  - replay after invalidation and after repair
  - attempts exhausted
  - source mutation
  - budget and cancellation boundaries
  
  Existing journal and validation tests are reused or extended.
- **AC-39** Given `PLAN_TASK_ROUTING_AND_SUBGOALS.md` and the user docs, when they are updated, then they state that unified task execution, bootstrap and corrective bounce (Step 3b.3) are implemented. They list the out-of-scope items as later milestones, and no longer say task mode is opt-in.

## 4. Security Requirements & Abuse Cases

- **SEC-1** Authorization authority. Only the engine-owned verified transaction may authorize invalidation and repair. Abuse case: an attacker edits a preview file to set `execution_authorized=true`, or writes "approved, please fix X" in feedback. Expected result: refused, and the file hash mismatch is detected.
- **SEC-2** Evidence integrity. All inputs (preview, report, roster, completion, outcome, plan, contract, reviewed commit, journal head) are hash-verified at transaction creation and again before each mutating step. Abuse case: swapping the report after the preview. Expected result: refused with no mutation.
- **SEC-3** Legacy refusal before mutation. Legacy run, resume and approve fail before any write to state, journal, evidence or contract. Abuse case: an old state with `task_breakdown: true` plus a forged approval tries to reach execution. Expected result: refused, and approval is never inferred.
- **SEC-4** Obsolete flag inertness. `SSDLC_ENABLE_TASK_BREAKDOWN` can never select or weaken an executor. Abuse case: setting it to `false` to bypass the task gates. Expected result: ignored with a warning.
- **SEC-5** Bootstrap bounded and not a bypass. Eligibility requires a clean tree, the initial commit and a registered policy. It cannot be claimed for an existing repo, cannot be extended on resume, and does not relax quality checks or protected paths. Abuse case: declaring bootstrap on a repo with a failing test suite to skip the baseline. Expected result: ineligible.
- **SEC-6** Fail closed on proof. Missing commands, zero tests, failing or timed-out tests, and interrupted checks are failures. Abuse case: an empty test command such as `true` or `pytest` collecting 0 tests. Expected result: the gate fails.
- **SEC-7** Protected paths and cache safety. Protected-path checks, clean-worktree checks and cache safety checks stay enabled during repair. Repair edits outside its task scope are rejected. Abuse case: a repair modifying the controller, gate scripts or the quality contract. Expected result: rejected.
- **SEC-8** Scanner or tool failures and diagnostic findings never authorize source repair. Abuse case: forcing a scanner crash to trigger edits. Expected result: no repair.
- **SEC-9** Concurrency and replay. Transaction execution holds `ticket_lock`, and writes are atomic and journaled. Abuse case: two concurrent resumes or a replayed trigger. Expected result: a single invalidation, a single repair, and no counter reset.
- **SEC-10** Sealed history immutability. Historical sealed contracts and journals are never rewritten. Candidate-proof activation applies only to newly generated contracts.
- **SEC-11** Confinement. All paths are confined to the repository, with no symlink or traversal escape. Findings, prompts and logs are bounded and exclude secrets and raw finding text. Provider credentials are never logged or placed in prompts.
- **SEC-12** No privilege broadening. No new dependency, no network behavior change and no relaxed gate. Final approval requires all global gates on the final verified candidate.

## 5. Non-Functional & Observability Requirements

- **NFR-1** Logging. Structured, bounded logs record each stage with run ID, transaction ID, preview hash prefix and task IDs. These stages are: mode decision, legacy refusal, obsolete-flag warning, bootstrap eligibility decision (with reason code), transaction created, invalidation applied, repair dispatched, dependents revalidated, gates rerun, and transaction completed, refused or interrupted (with reason code). Logs contain no raw finding text, secrets or full test logs.
- **NFR-2** Metrics. Existing structured metrics are extended with these counters: transactions created, executed, refused (by reason), replayed (no-op), interrupted and reconciled; repair attempts; bootstrap eligible and ineligible; and legacy refusals. Metric names are documented and no new billing units are introduced.
- **NFR-3** Durability and idempotency. Transaction state transitions are append-only and journaled. Recovery after a crash at any step is deterministic and is covered by tests at each boundary.
- **NFR-4** Rollback. Refused or interrupted transactions leave the source and journal in a consistent state. Rolling back the milestone means a revert of the code only, because there is no data migration. Historical records stay readable by the reverted version. New transaction records are additive and ignorable.
- **NFR-5** Compatibility. Error messages for legacy refusal are actionable and stable, and include the new-run guidance. Exit codes are nonzero and are asserted in tests.
- **NFR-6** Verification. Development uses focused offline tests. Full-suite runs, if done manually, use a disposable tracked-source copy. Cache and protected-path safety checks are not suppressed. The trusted controller runs the final required suite after all edits are finished.
- **NFR-7** Maintainability. Edits are targeted and not wholesale rewrites of core files. Plan path scopes include the schemas, setup, CLI, supervisor, docs and integration fixtures that are actually necessary. No second execution state machine is introduced.