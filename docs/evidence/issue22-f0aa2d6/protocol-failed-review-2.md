> [!NOTE]
> **Agent:** Claude Code (CLI vnon noto)
> **Model:** sonnet
> **Reasoning Level:** Medium

This original human assessment is preserved as evidence. It is NOT an approved gate receipt: the required machine-readable frame was missing.

The protocol correction is a re-emit of the original review's form. It keeps the one finding and the approval verdict.

This is a read-only review of tracked source and the host receipts. I did not execute any code or tests.

**Receipts**
- The host full suite ran 1486 tests with exit code 0 at head `f0aa2d6`, with 0 provider calls.
- The software-composition scan found no vulnerable dependencies, and no dependency was added.
- Gitleaks reported 4 findings, with no new findings since the prior scan and no allowlist change. All four look synthetic:
  - a fixed protocol identifier `scope-amendment-v1`
  - the same identifier in a test fixture
  - a fake AWS ID and a fake PEM body used to test redaction

**Requirements coverage (58 of 58)**

All 58 requirements map to production code in the diff, not only to test fixtures.
- **Unified execution (AC-1 to AC-5):**
  - `_task_mode` and `_run_phase_dev_legacy` are deleted.
  - The obsolete flag is read only inside `_warn_obsolete_task_flag`.
  - Tier S requires exactly one plan node.
  - New contracts set `candidate_proof`.
  - QA shell commands are labelled `shell_command`.
  - The setup script no longer writes the flag.
  - `jsonschema` is documented as required.
- **Legacy compatibility (AC-6 to AC-12):**
  - A read-only `execution_compatibility` classifier runs before any constructor in the foreground, the supervisor and the supervised worker.
  - Legacy runs exit 23.
  - History import uses a descriptor-based, no-follow reader.
  - `build_task_context` preserves the sealed context values.
- **Bootstrap (AC-13 to AC-21, SEC-5, SEC-6):**
  - The profile is schema-validated against a code-registered policy.
  - Eligibility is re-checked at contract capture.
  - The first checkpoint requires a positive test count.
  - A missing command is recorded as exit 127.
  - Resuming before the proof raises `interrupted`.
  - Neither the plan check nor the protected-path check has a bootstrap branch.
- **Corrective executor (AC-22 to AC-37, SEC-1, 2, 7 to 9, 12):**
  - A rejected gate enters the corrective round, and the preview keeps `execution_authorized=False`.
  - The transaction schema has no authorization field.
  - Evidence and journal lineage are re-verified before each step.
  - The record and backups are written before `invalidate`.
  - Recovery runs under the ticket lock and does not repeat invalidation. An uncertain dispatch stops without redispatch.
  - Frozen limits can only be lowered.
  - The repair prompt is scrubbed and capped at 12,000 characters.
  - AppSec now runs for every tier.
- **Integration tests and docs (AC-38, AC-39):** The journeys call the real entry points with fake providers. The docs state that the work is implemented and list the out-of-scope items.
- **Observability and rollback (NFR-1 to NFR-7):** Stage logs and counters are bounded and redacted. Rollback limits are documented.

**Finding (low severity, plausible, not confirmed by execution)**
- `orchestrator.py:3589`: `TaskJournal(self.dir_dev / 'tasks').peek()` creates `03-dev/tasks` before any journal event exists.
- An early failure before the first journal event would leave a run that compatibility classifies as legacy, so resume exits 23. This costs availability only, because nothing is mutated and no authority is granted. The operator can start a new run.

**Non-blocking test weaknesses**
- `test_protected_task_rejected_with_or_without_bootstrap` (AC-20) only calls `confined()` on a path. The control itself holds, since `_check_plan` and `_protected` have no bootstrap branch. A scheduler-level test would be stronger.
- The NFR-2 trace cites a single creation test. The legacy and bootstrap counters are covered only by other tests.

**Accepted limits**
- Hashes are unkeyed integrity checks, and `ticket_lock` is advisory. The ADR and docs state both.
- The corrective driver keeps several broad `except` blocks that map errors to `evidence_mutated` or `repair_failed`. They fail closed.

No privilege broadening appears in the integration. Gates are only added, the obsolete flag is inert, and corrective authority comes only from the engine-written transaction.

REVIEW_STATUS: APPROVED
