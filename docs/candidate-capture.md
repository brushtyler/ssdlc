# Candidate capture and disposable task workspaces

Scope: [issue #23](https://github.com/brushtyler/ssdlc/issues/23), increment I0 of
`PLAN_AUTONOMY_WORKSTREAMS.md`. This is a direct bootstrap implementation, not an
autonomous SSDLC delivery demonstration. Offline validation is recorded in
`docs/evidence/i0-offline-validation.json`; final full-suite CI binds to the PR head.

New task runs use version 1 capture. An admitted sealed task journal retains its
recorded execution mode: an absent isolation marker denotes the pre-I0 format
and is not silently converted. A captured journal still requires its recorded
runtime identity. Controller or bounded-recovery changes require the explicit
I1/I2 admission path; copying a controller into a suspended run is not migration.
Historical non-task runs remain readable but cannot execute; see the
[task-mode compatibility guide](task-mode.md#unified-execution-and-greenfield-bootstrap).

## Trust and state contract

The host/controller owns the journal and private receipt store. Role code can
write its disposable developer checkout, including its independent Git objects;
it cannot write the authoritative candidate or receipt parent through that
checkout's Docker mount. Baseline repository content and role output are
untrusted. A same-user host writer able to replace both receipts and journal
references is outside this integrity boundary. Hashes provide binding, not
authenticity against that writer. Existing protected-path guards remain enabled.

The task-mode adapter requires Docker at preflight, development and verification,
and refuses local fallback even if a later runtime probe changes. Successful
return of the ephemeral Docker role establishes writer termination; a thrown
timeout, resource failure or lost return does not grant that fact. Such a task
stays uncertain without another developer dispatch. Timeout-candidate adoption
is deliberately reserved for I2. Semantic offline adapters explicitly assert
their own quiescence for the test harness; model prose never supplies this field.

The normal sequence is:

1. Journal `DISPATCH_RESERVED` with a persistent attempt and unique invocation ID.
2. Create an independent checkout at the expected head; overlay only the
   existing approved repair candidate, if any. Journal `TASK_STARTED` immediately
   before invoking the developer there.
3. After quiescence, capture a private immutable payload and receipt conforming
   to `schemas/candidate-capture.schema.json`. Include additions, deletions,
   modes, original/starting/candidate identities, producer and output digest.
   Bind explicit plan/quality/controller digests, environment identity, invocation
   ID, source payload digest, termination status and protected snapshot.
4. Fsync blobs, receipt and directories; publish the complete directory by rename.
   Journal `CANDIDATE_CAPTURED` and its receipt digest before secondary guards.
   Capture grants neither scope approval nor verification.
5. Recheck guards and adopt only a safe, approved payload. Per-file atomic writes
   permit idempotent reconciliation of an interrupted adoption. Unexpected
   authoritative edits are refused. Adoption scratch stays in the private store,
   on the same filesystem, so interrupted writes leave no unknown source files.
6. Run checks in fresh independent copies. Reuse the existing verification,
   selective staging, `COMMIT_INTENT` and checkpoint-completion protocol.

| Interrupted point | Resume behavior |
| --- | --- |
| `DISPATCH_RESERVED` before provider execution | Preserve interrupted preparation; reuse invocation identity and reserved attempt |
| `RUNNING` without a complete receipt | Stop as uncertain; do not dispatch another developer |
| Complete receipt, journal still `RUNNING` | Validate the original binding and payload, journal capture, then verify |
| Captured/adoption/verification | Revalidate receipt; reconcile known source states; run fresh checks without development |
| `COMMIT_INTENT` | Existing exact-tree and evidence checks finish the checkpoint without a second commit |
| Recorded failure before rollback finishes | Validate the capture and known source states, finish rollback, then honor the remaining attempt allowance |
| Completed | Existing proof validation; no developer call |

Verification evidence uses a new `verification-N` directory on every collection.
The journal reserves it before execution. The fixed v1 allowance is three
collections per developer attempt, including interrupted collections; resume
does not reset it. Verification does not spend a development attempt. Failed
code checks retain their normal repair context and consume the next development
attempt only when a new developer is dispatched. Historical receipt/log paths
stay on disk. A malformed or altered receipt, output or blob stops recovery.
I0 checkpoints disable repository commit hooks; required checks belong in the
isolated quality contract. Independent checkout creation ignores host Git filters.

## Artifact and execution boundaries

Only newly generated regular CPython `__pycache__/*.cpython-N[.opt-N].pyc` files
and the fixed pytest cache files (README, cache tag, ignore file and
`v/cache/{nodeids,lastfailed,stepwise}`) are disposable in this Python adapter.
Tracked or explicitly targeted files are never excluded through that rule.
`.gitignore` is not an authorization source:
unknown ignored logs/files, protected changes, unsafe links and out-of-scope
changes remain blocking and their developer checkout is retained. Other cache
formats must use declared scratch outside source until another tested adapter
policy is added. No cache is transferred into the authoritative candidate or
reused by later checks.

Each check receives a fresh source copy, writable in its own Docker mount, while
the host repository/toolkit stay read-only and existing credential masks remain
active. Writes to pre-existing source or checkout metadata reject the result;
new scratch files stay disposable and cannot contaminate subsequent checks.
Python cache environment variables remain defense in depth. Cleared-environment
subprocesses can create bytecode in the copy without changing authoritative
source. Checks never execute directly on the host through this task-mode path.

Full network restriction, dependency provisioning, independent acceptance tests,
instruction/tool hardening, publication scanning and final autonomous-profile
enforcement remain later A/B/C increments. This receipt store is private runtime
state, not a public evidence pack. The issue #22 preserved T01 has not been
imported, verified or resumed by implementing I0.

## Offline acceptance

`python3 -m unittest discover -s tests -p test_candidate_capture.py -v` discovers
the protected fixture and semantic adapters under `tests/replay/`. It uses real
Git operations and Python subprocesses with semantic role adapters. It exercises
the issue #22 cache mechanism, guard/capture/verification/commit interruptions,
tampering, unsafe changes, additions/deletions, persistent attempts and refusal
to silently change historical execution mode. No provider, subscription login
or real development run is needed for these cases.

## I2 integrity rejection hardening (#29)

After a confirmed return/termination, metadata failures no longer discard the
source/output evidence. Missing/replaced `.git` or a corrupt index are recorded
as `git_metadata_unavailable`/`protected_metadata` without executing Git against
changed configuration. Nested `.git`, backslash/control-character names, links
and non-regular entries are recorded as unsafe. Regular bytes use confined,
no-follow file descriptors and content-addressed blobs; an unsafe name is never
used as an evidence-store destination or adopted into authoritative source.

A mutation observed while copying or on the final stability check adds
`source_changed`/`source_unavailable`. Copied bytes retain their actual digest;
a rejection receipt is not a claim of a stable or executable candidate. These
receipts preserve available payload and producer output and produce INTEGRITY,
including on resume, without another developer call. An adapter's unconfirmed
writer outcome still remains uncertain; model text cannot supply quiescence.
If the main receipt cannot be formed but the evidence filesystem remains writable,
a hash-bound `capture-rejection.json` and `rejected-output.log` are journaled as
a non-dispatchable integrity refusal. A storage failure preventing even this
record grants no replay permission; the retained workspace requires recovery.
A complete refusal left by a crash before its journal append is recovered by
matching the original invocation binding and output digest; it is not downgraded
to an uncertain producer outcome or redispatched.

The immutable `capture/developer.log` remains original producer evidence. Its
readable attempt-level copy is flushed and atomically replaced on first write;
resume reuses identical bytes instead of truncating the file. Timeout completion
remains `unconfirmed_completion` even after independent tests pass and appears
in both evidence-pack and sign-off disclosures.

In self-development, the host grader baseline and `tests/replay/` are protected
in planning, snapshots and capture. Revising the grader or its fakes requires an
explicit baseline review, rather than a candidate changing its own expectations.
Full instruction/network containment and independent oracles retain their I3/I4
scope; this change does not claim protection against a same-user host attacker.
