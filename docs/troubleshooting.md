# Troubleshooting

## Memory and OOM

Large generation passes combined with test runners can use a lot of RAM.

- Add swap. 4 GB is recommended:
  ```bash
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  ```
- Cap Node.js memory in `.ssdlc/project.env`: `export NODE_OPTIONS="--max-old-space-size=1536"`.
- Container limits are set by `SSDLC_CONTAINER_MEMORY`, `SSDLC_CONTAINER_CPUS` and `SSDLC_CONTAINER_PIDS_LIMIT`.

A measured PID or OOM event stops the run as `ENVIRONMENT_RESOURCE` and keeps the source ([Configuration](configuration.md#execution-mode-and-containers)). Raise the limit and resume. The attempt is not repeated automatically.

## Resuming an interrupted run

Resume from a phase without repeating approved upstream work:

```bash
./ssdlc run --ticket TKT-001 --goal "The original scoped goal" --from-phase 03-dev
```

Use the same goal and test command as the original run. In task mode, `po`, `design` and `plan` resume their own phases ([Task mode](task-mode.md)). Supervised runs use `resume` ([Supervision](supervision.md)).

A run that stopped and cannot simply be resumed needs an explicit transition: changed controller, captured candidate, `SCOPE_REQUIRED` after a checkpoint, or exhausted frozen limits. See the [recovery documents](../README.md#recovery-and-continuation). Never edit journals, attempts or limits by hand: the controller detects it and refuses to continue.

## Common stops

| Stop | Meaning | What to do |
| --- | --- | --- |
| `BASELINE_MISMATCH` | Required checks fail at the clean checkpoint, before any developer call | Fix the environment or the baseline; no attempt is consumed |
| `BASELINE_MUTATION` | A check modified the source | Inspect the preserved changes |
| `SCOPE_REQUIRED` | The task needs files outside its manifest | Run a reviewed [scope amendment](scope-amendments.md) or the [feedback workflow](developer-feedback.md) |
| `ENVIRONMENT_BLOCKED` / `ENVIRONMENT_PREREQUISITE` | Missing tooling or environment | Fix the environment, then resume |
| `REPAIR_STALLED` | The repair produced an identical failing tree with identical outcomes | Inspect the failure; a new authorized attempt or plan change is needed |
| `DESIGN_STALLED` / `STAGNATION` | Design or planning made no semantic progress | Inspect the open findings; consider an amendment or a split |
| `REVIEW_PROTOCOL` | The reviewer's output stayed invalid after one correction | Inspect the raw response |
| `LIMIT_REACHED` | A frozen limit or deadline is exhausted | Only an explicit, reviewed extension continues the run |
| `OUTCOME_UNCERTAIN` | An invocation has no confirmed outcome | Reconcile; no automatic redispatch |
| `BUDGET_CAP_GATE` | Reported spend reached `SSDLC_BUDGET_CAP_USD` | Raise the cap explicitly, or stop |

## GitHub synchronization

- `./ssdlc doctor` reports whether `GITHUB_TOKEN` or `GH_TOKEN` is visible. SSDLC does not read the `gh` login on its own; run `export GITHUB_TOKEN="$(gh auth token)"`.
- A 403 or 404 response usually means the token cannot create issues or PRs, or cannot push to the repository.

## SSH deploy keys

To push with a repository deploy key, configure Git to use it:

```bash
git config core.sshCommand "ssh -i ~/.ssh/id_ed25519_deploy -o IdentitiesOnly=yes"
```

The deploy key must have write access. When a token is set, the push uses HTTPS and the token instead.
