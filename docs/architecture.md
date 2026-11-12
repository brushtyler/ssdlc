# Pipeline and architecture

## Phases

| Phase | Role | Focus and artifacts |
| :--- | :--- | :--- |
| **`00-recon`** | Codebase archaeologist | Maps blast radius, architectural impact and dependencies. Assigns the risk tier (`S`, `M`, `L`) and development profile (`FULLSTACK`, `ALGORITHMIC`, `REFACTOR`, `FRONTEND_UI`, …). |
| **`01-po`** | Product owner | Testable user stories, Given/When/Then acceptance criteria (`AC-*`) and security abuse cases (`SEC-*`). |
| **`02-design`** | Architect and security reviewer | Pair design session producing `adr.md` and `threat-model.md` (STRIDE). In task mode it also produces the reviewed task plan. |
| **`03-dev`** | Developer | Code and tests in an isolated worktree branch (`ssdlc/<ticket-id>`). In task mode, one task at a time from the plan DAG. |
| **`04-review`** | Multi-model review panel | Independent review by distinct models. Blocks on security flaws, regressions or spec violations. |
| **`05-qa`** | QA engineer | Deterministic test suites (`--test-cmd`) and acceptance-criteria validation. |
| **`05b-appsec`** | AppSec red-teamer | Dependency vetting, secret scanning, OWASP top-10 and supply-chain checks. |
| **`06-ops`** | DevSecOps / SRE | Deployment checklist, monitoring, metrics and rollback procedures. |
| **`07-docs`** | Technical writer | User documentation, migration guides and release notes. |
| **`08-supervisor`** | Evidence coordinator | Compiles `EVIDENCE_PACK.md`, synchronizes the remote, opens a draft PR and waits for human sign-off. |

## Risk tiers

With `--tier auto`, `00-recon` assigns the tier:

- **Tier S** (low risk, small fixes): `00-recon` → `01-po` → `03-dev` → `04-review` (single reviewer) → `05-qa` → `08-supervisor`. There is no pair design and no DevSecOps work.
- **Tier M** (standard feature): adds pair design, multi-model review and documentation.
- **Tier L** (high risk, architectural or fullstack): the full pipeline, including STRIDE threat modelling, consensus review, AppSec red-teaming and DevSecOps runbooks.

The tier also selects the default reasoning effort, the developer model and the budget caps (see [Configuration](configuration.md)).

## Isolation

```text
Host
├── /usr/local/bin, ~/.codex, ~/.claude      untouched
├── <ssdlc-toolkit>/
│   ├── orchestrator.py, ssdlc_*.py          controller
│   ├── setup-ssdlc.sh                       bootstrap and project initializer
│   └── .tools/                              shared CLIs (mounted read-only)
└── <project>/
    ├── ssdlc                                project-local CLI wrapper
    └── .ssdlc/
        ├── project.env                      project keys and routing
        ├── docker-compose.yml               container definition
        ├── .runtime/                        isolated CODEX_HOME, CLAUDE_CONFIG_DIR, agy_auth, resources
        ├── .supervisor/                     supervisor metadata, locks, audit
        └── <ticket>/                        see "Run layout"
```

- Several projects can run on the same host without credential collisions or SQLite lock conflicts.
- In Docker mode:
  - verification containers mount the source read-only and receive no runtime credentials;
  - role containers carry the `ssdlc.run_id` and `ssdlc.project` labels, used for scoped cancellation.
- When the toolkit or the target is a linked Git checkout, its shared Git metadata is mounted read-only. Git stays visible, but repository configuration and other worktrees cannot be written.

## Run layout

```text
.ssdlc/<ticket>/
├── state.json                     canonical run state (owned by the orchestrator)
├── 00-recon/ … 07-docs/           phase artifacts, evaluations, evidence
│   └── 00-recon/controller-contract.json
├── 03-dev/tasks/                  task mode: manifests, logs, diffs, task-journal.jsonl, tasks-state.json
├── worktree/                      isolated Git worktree
├── EVIDENCE_PACK.md
├── execution/active.json          selected execution generation (recovered runs)
├── execution-runtimes/<id>/       one directory per execution generation
└── feedback/                      feedback ledger (when the feedback workflow is enabled)
```

The journals are authoritative: `task-journal.jsonl` for tasks and `limits/task-journal.jsonl` for frozen limits. JSON state files are projections of them.

Recovery transitions never edit an existing generation. They create a new generation that preserves the journal prefix, attempts, invocations and limits, and select it atomically after an explicit authorization. Readers for status, journal, completion and deadline resolve the selected runtime.

## Controller contract

Before new planning or the first task, the host captures and registers `00-recon/controller-contract.json`. It binds the path of the executing controller, its Git HEAD and status, and the hashes of its tracked and non-ignored files.

- The contract is checked before and after every developer invocation and verification check.
- A committed controller change is detected even when the checkout is clean.
- A missing or altered contract blocks execution.
- The fingerprint ignores global and system Git configuration, so it does not depend on `HOME`.

Keep the executing installation pinned to a reviewed version. When SSDLC develops itself, run that pinned controller against a separate target checkout. A run continues on its pinned controller; changing controller requires an explicit migration (see the [recovery documents](../README.md#recovery-and-continuation)).

## Integrity model and its limits

Every intermediate artifact is registered with a SHA-256 hash and checked on recovery. Changed upstream inputs block later phases instead of being silently reused. These checks have limits:

- **Unkeyed hashes are not authentication.** Anyone who can rewrite both the content and its hash defeats them.
- **Changes are detected only at checkpoints.** There is no continuous containment against external writers; containment is workstream B of the autonomy plan.
- **Coverage is partial.** Ignored Git files, unreferenced partial logs and artifacts outside the evidence inventory are not covered.
- **Strict parsing of model output** does not establish model honesty or eliminate prompt injection.
- **Locks** coordinate SSDLC processes. They do not stop arbitrary writers, and `source_fenced` in recovery authorizations remains an operator attestation (see [#45](https://github.com/brushtyler/ssdlc/issues/45)).
