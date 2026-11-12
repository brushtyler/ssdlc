# Detached supervision

Plain `run`, `plan` and `design` are foreground commands. Detached supervision, introduced by the #16 milestone, runs the same entrypoints in the background, under a supervisor that records state, heartbeats and audit events. It is **single-host and Linux-only**, built on `/proc`, flock and pidfds.

- It provides no distributed ownership.
- It provides no authentication against a malicious process running under the same user.
- Fingerprints, including the controller snapshot and the frozen configuration, are unkeyed drift checks, not signatures.
- There is no GitHub adapter. Remote triggers (#1) and the GitHub lifecycle (#4) are separate issues, and provider accounting is tracked in #14.

## Commands

```bash
python3 orchestrator.py start --ticket T-1 --goal 'Implement the approved change' --repo . --json
python3 orchestrator.py status --ticket T-1 --repo . --json
python3 orchestrator.py status --recovery-matrix --json
python3 orchestrator.py cancel --ticket T-1 --repo . --grace 5 --json
python3 orchestrator.py resume --ticket T-1 --entrypoint run --from-phase 03-dev --goal 'Implement the approved change' --repo . --json
python3 orchestrator.py resume --ticket T-1 --entrypoint plan --review-repairs 2 --repo . --json
python3 orchestrator.py resume --ticket T-1 --entrypoint design --rounds 4 --repo . --json
```

- **`run --detach`** is an alias for `start`.
- **`status`** is read-only. It reports the state, heartbeat age, phase, deadlines, recovery allowance, preflight checks, classification and the next action.
- **`monitor --restart-worker`** (or `--adopt`) explicitly restores a worker after a verified loss. It is **not** read-only.
- **`start` on a terminal run** records a refusal and exits with code 14. Use an explicit, supported `resume` instead.

## Resume and fingerprints

`resume` starts a new run linked to the old one through `resumes`. Supported transitions:
- `run` from `00-recon` or from `03-dev`;
- `plan` before execution;
- `design` before planning or execution.

Upstream approvals, sealed artifact hashes and baseline compatibility still apply. An unsupported resume adds one audit event and creates neither a run nor canonical state.

**Fingerprints**
- A change in goal, tier, test command, environment or controller fingerprint blocks resume.
- `--reauthorize-fingerprints` records an explicit, audited authorization. It never overrides transition compatibility or approvals.
- Switching from a goal-bearing `run` to `plan` or `design` changes the goal fingerprint to null, so it requires `--reauthorize-fingerprints`.

**Configuration digest**
- The digest covers the whole effective environment, except the supervisor markers, `PWD`, `SHLVL` and `_`. A different `TERM`, `OLDPWD` or `SSH_AUTH_SOCK` can therefore block adoption, so restart from the same effective environment.
- Adoption fails closed if any process running under the same user has unreadable `/proc` metadata. A shared host may need a dedicated service user or container.

**Restarting before the controller exists**
- It requires operator resupply of `--goal` or `--goal-file`, plus the original `--test-cmd` if one was used.
- Raw inputs exist only in the launch pipe and in process memory, and cannot be recovered from a fingerprint.

**Inputs and authority**
- The goal and the ticket prose are inert argv data.
- `--test-cmd` is an operator-authorized command, run with bash. Only its hash is stored. Do not derive it from issue prose.
- `--issue` and `--sync-remote` are never generated.

## Recovery matrix

| Classification | Available action |
| --- | --- |
| transient_environment_provider | Bounded read-only preflight retry, consuming the persisted recovery allowance. |
| authentication | Refresh out of band, then explicitly resume or restart. Presence alone is reported as `auth_unverified`; there is no shipped native probe or refresh adapter. |
| integrity | Inspect and reconcile changed or corrupt evidence before an explicit resume. |
| limits | Explicitly authorize a changed limit; limits are never raised automatically. |
| code_defect | Task mode only, with a returned, terminal-bound failed check or stalled repair. Inspect the diagnostics; if attempts are exhausted, explicitly authorize a supported resume. |
| uncertain_outcome | Inspect and reconcile the engine result. An open or raised dispatch is never retried automatically. |
| operator_decision | Resolve scope or environment decisions explicitly. |
| unsupported_transition | Choose a compatible supported entrypoint. |

**Classification**
- Classification is bound to the terminal event: an earlier failed attempt cannot explain a later open invocation. Diagnostics U1–U7 report missing, ambiguous or unbound evidence.
- A budget, deadline or resource cap is a **limit**. Failed task checks exhaust **code repair**. Legacy development is never classified as `code_defect`.

**Deadlines and allowances**
- Quiet output alone does not mean the controller has stalled.
- No whole-run deadline is inferred. To set one, use `SSDLC_SUPERVISOR_HARD_TIMEOUT` (seconds).
- The recovery allowance is an upper bound, and the caller's handoff deadline still wins. Raise `--handoff-timeout-sec` explicitly when you need more time.

**Completion of an adopted controller**
- It requires the intent-bound `run_ended(exit_code=0)` event plus the entrypoint's canonical completion state. Disappearance alone never proves completion.
- Missing exit evidence leaves the run in `exit_evidence_unavailable` and needs operator reconciliation.
- An adopted worker keeps observing read-only while reconciliation is pending, and never relaunches the controller or a model. If the controller is verified gone and evidence stays missing for three observation ticks, the observer exits and keeps the uncertain result.

## Ownership and cancellation

**Ownership**
- Supervisor metadata, locks, audit, observation events and `controller-events.jsonl` live only under `.ssdlc/.supervisor/`.
- The orchestrator alone owns `.ssdlc/<ticket>/state.json`, the journals, attempts, ledger and approvals.
- Controller hooks carry only whitelisted identifiers, never prompts, goals, commands, credentials or engine output.
- Detached controller stdout and stderr go to `/dev/null`.
- Frozen child environments default to `PYTHONCOERCECLOCALE=0`, so Python cannot silently rewrite a requested C locale after the environment has been hashed.

**Cancellation**
- `cancel` revokes launch authority, verifies each process's birth and session, and signals only admitted pidfds.
- Containers are stopped by full ID, and only after both exact `ssdlc.run_id` and `ssdlc.project` labels have been verified. Other projects, ticket-only matches and recycled PIDs are excluded.
- Repeating `cancel` reconciles containers left behind by a crash. A hard deadline triggers the same scoped cleanup.
- Cleaning up a run that has already ended preserves its outcome. `cancellation_state=done` records that cleanup finished, while the exit code still reflects the run outcome.
