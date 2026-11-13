> [!NOTE]
> **Agent:** Claude Code (CLI vnon noto)
> **Model:** sonnet
> **Reasoning Level:** Medium

This original human assessment is preserved as evidence. It is NOT an approved gate receipt: the required machine-readable frame was missing.

The gitleaks findings are confirmed as false positives: a protocol identifier and synthetic redaction fixtures.

I found no blocking defect, so I am approving. I did not run any code. I relied on the host receipts and read the diff and source.

**Verification receipts**
- The full suite ran 1486 tests with exit code 0 at the exact head `f0aa2d6`, with 0 provider calls.
- The software-composition scan found no vulnerable dependencies, and no dependency was added.
- Gitleaks reported 4 findings, none new since the prior scan and no allowlist change:
  - `ssdlc_scope_amendments.py:407` is the fixed string `'scope-amendment-v1'`, which is not a secret.
  - `tests/test_transitions.py:95` contains the same string.
  - `tests/test_repair_prompt.py:92-93` are an obviously fake AWS ID and PEM body used to test redaction.

**Requirement coverage**

All 58 requirements map to production code in the diff, not only to fixtures.
- **AC-1 to AC-5:** `_task_mode` and `_run_phase_dev_legacy` are deleted, and the obsolete flag is read only inside `_warn_obsolete_task_flag`. Tier S is enforced as one plan node. New contracts set `candidate_proof`, and QA shell commands are labelled `shell_command`. The setup script no longer writes the flag, and `requirements.txt` marks `jsonschema` as required.
- **AC-6 to AC-12:**
  - A read-only `execution_compatibility` classifier runs before any constructor in the foreground, in the supervisor and in the supervised worker. Legacy runs exit 23, and the legacy-refusal tests assert exit 23 and unchanged records.
  - History import uses a descriptor-based, no-follow reader, so the old ticket's files are not touched.
  - `build_task_context` preserves the sealed context values.
- **AC-13 to AC-21, SEC-5 and SEC-6:**
  - The bootstrap profile is schema-validated against a code-registered policy.
  - Eligibility requires the initial commit, a clean tree and no existing tests, and it is re-checked at contract capture.
  - The first checkpoint requires a positive test count, and a missing command is recorded as exit 127.
  - Resuming before the proof raises `interrupted`.
  - Neither `_check_plan` nor `_protected` has a bootstrap branch.
- **AC-22 to AC-37 and SEC-1, 2, 7 to 9, 12:**
  - Rejected gates enter `_corrective_round`. The preview is built with `execution_authorized=False`.
  - The transaction schema has no authorization field.
  - Evidence is re-verified before each step through `verify_evidence` and `verify_lineage`.
  - The record and backups are written before `invalidate`.
  - Recovery runs under the ticket lock and does not repeat invalidation. An uncertain dispatch stops without redispatch.
  - Frozen limits can only be lowered, and the repair prompt is scrubbed and capped at 12,000 characters.
  - AppSec now runs for every tier.
- **AC-38 and AC-39:** The journeys call the real entry points, `_foreground_main` and `run_pipeline`, with fake providers. The docs state that the work is implemented and list the out-of-scope items.
- **NFR-1 to NFR-7:** Stage logs and counters are bounded and redacted. Rollback limits are documented in `native-gate-convergence.md`.

**Test-coverage weaknesses (non-blocking)**
- The AC-20 test `test_protected_task_rejected_with_or_without_bootstrap` only calls `confined()` on a path. The control itself is sound, because no bootstrap-specific branch exists in `_check_plan` or `_protected`. A scheduler-level test would be stronger.
- The NFR-2 trace points to one creation test for the counters. The legacy and bootstrap counters are covered only by other tests.

**Minor operability gap (PLAUSIBLE, not verified by execution)**
- `_make_task_scheduler` builds `TaskJournal(self.dir_dev / 'tasks')` at `orchestrator.py:3589`. The constructor creates `03-dev/tasks`.
- `execution_compatibility` then classifies the run as legacy, because the directory exists but no journal has been written. The same happens if a worktree exists before the first journal event.
- A crash or error in that window, before `INITIALIZED` is written, would leave a new-code run refused with the legacy message. This costs availability only. It grants no authority, nothing is mutated, and the operator can start a new run.

**Accepted limits**
- Hashes are unkeyed integrity checks, and `ticket_lock` is only advisory. Both are stated in the ADR and the docs.
- The corrective driver retains several broad `except` blocks that map errors to `evidence_mutated` or `repair_failed`. They fail closed.

No privilege broadening appears in the diff. Gates are only added (AppSec on every tier), the obsolete flag is inert, and corrective authority comes only from the engine-written transaction.

REVIEW_STATUS: APPROVED
