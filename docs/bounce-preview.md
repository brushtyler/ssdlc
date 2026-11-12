# Targeted bounce preview

A bounce preview checks the recorded evidence of a gate and archives a deterministic preview of the targeted repair that would follow. It grants **no** approval or execution authority: every result has `execution_authorized=false`. The repair executor itself is the I5a corrective loop described in `PLAN_AUTONOMY_WORKSTREAMS.md`.

```bash
python3 orchestrator.py bounce-preview --repo . --ticket TKT-001 --phase 04-review --attempt 1
```

The host API is `SSDLCOrchestrator.preview_bounce(phase, attempt)`.

## What the preview checks

- **Input gate.** It selects an exact recorded task-mode gate: `04-review`, `05-qa` or `05b-appsec`. Legacy tickets without host provenance are rejected.
- **Controller.** It requires the existing pinned controller contract and never captures a new one.
- **Side effects.** It invokes no model, developer, scanner or test runner. Raw evidence, checkpoint, scheduler state, attempts, ledger and cost totals are preserved.

## Output

Canonical JSON and a readable summary are written under `bounce-preview/<phase>/attempt-<n>/<revision>/`. They contain ticket-relative source references, SHA-256 hashes and a revision derived from the content.

- **Publication.** It is locked, create-or-verify, and registered through artifact references.
- **Reruns.** An identical verified rerun, including after a restart, writes nothing.
- **Partial or conflicting files.** Matching partial files are verified and completed. Conflicting files, or files that are registered but missing, block the preview without overwriting old evidence.
- **Changed inputs.** They produce a new revision.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Candidates with no blockers, ready for a later executor (grants no authority) |
| 10 | Non-executable or diagnostic preview, including an empty plan |
| 20 | Blocked or rejected |
| 2 | Invalid CLI usage |

Local `preview_counters` track created, reused, recovered and blocked runs without touching provider accounting. Logs contain selectors, revision, status, reason codes and counts, never finding text or provider payloads. Hashes are unkeyed integrity checks (see [Architecture](architecture.md#integrity-model-and-its-limits)).
