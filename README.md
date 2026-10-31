# Multi-Project Isolated SSDLC Orchestrator (`/opt/ssdlc`)

A zero-global-touch, **Risk-Tiered Secure Software Development Lifecycle (SSDLC)** toolkit that orchestrates **Antigravity (`agy`)**, **OpenAI Codex (`codex`)**, **Claude Code (`claude`)**, and **OpenRouter** across **$X$ parallel projects** using a shared Docker base image (`ssdlc-runner:latest`) and strict per-project credential/state isolation.

---

## 1. Key Guarantees for Parallel Multi-Project Execution

1. **Zero Host Config Pollution**:
   * Never modifies `/root/.codex`, `/root/.gemini`, `/root/.claude`, or `/usr/local/bin`. Existing projects on the VM continue running undisturbed.
2. **Per-Project CLI State Isolation (No SQLite Locks)**:
   * Every initialized project gets its own `<project>/.ssdlc/.runtime/codex_home` (`CODEX_HOME`) and `claude_home` (`CLAUDE_CONFIG_DIR`).
3. **Per-Project Unique Tokens & Cost Traceability**:
   * Each project loads its own `<project>/.ssdlc/project.env` with dedicated `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY`.
   * Every OpenRouter request injects `HTTP-Referer: https://ssdlc.local/<project-id>`, `X-Title: SSDLC/<project-id>/<ticket-id>`, and `user: "<project-id>:<ticket-id>:<phase>"`.
   * SHA-256 key fingerprints and exact CLI versions are recorded in `<project>/.ssdlc/<ticket>/state.json` and `EVIDENCE_PACK.md`.
4. **One-Command Software Updates (`codex`, `claude`, `agy`, `uv`)**:
   * All project containers mount `/opt/ssdlc/.tools` read-only (`:ro`) at the front of `PATH`.
   * Running `/opt/ssdlc/setup-ssdlc.sh --update-tools` (or `./ssdlc update-tools` from any project) updates `codex`, `claude`, and `uv` inside `/opt/ssdlc/.tools/` so **every project container immediately uses the new versions** without rebuilding per-project containers.
   * Running `/opt/ssdlc/setup-ssdlc.sh --rebuild-image` refreshes the cached CLI layer of the shared `ssdlc-runner:latest` Docker image in seconds.

---

## 2. Quick Start & VM Replication

### Step 1: Bootstrap `/opt/ssdlc` on a VM
Copy `/opt/ssdlc` to any VM and run:

```bash
chmod +x /opt/ssdlc/setup-ssdlc.sh
/opt/ssdlc/setup-ssdlc.sh --bootstrap
```

### Step 2: Initialize Isolated Projects ($1 \dots X$)
Initialize any number of repositories without affecting global configs:

```bash
/opt/ssdlc/setup-ssdlc.sh --init-project /opt/project-alpha --id proj-alpha --mode docker
/opt/ssdlc/setup-ssdlc.sh --init-project /opt/project-beta  --id proj-beta  --mode docker
```

What `--init-project` creates **exclusively inside `<project>/`**:
* `.ssdlc/project.env`: Unique `SSDLC_PROJECT_ID`, API keys, and role-to-model routing.
* `.ssdlc/docker-compose.yml`: Isolated container config sharing `ssdlc-runner:latest` + shared `uv`/`npm` cache volumes.
* `.ssdlc/.runtime/`: Isolated `CODEX_HOME` and `CLAUDE_CONFIG_DIR`.
* `.agents/skills/ssdlc/SKILL.md`: Workspace-local Antigravity skill (discovered automatically when `agy` opens `<project>`).
* `./ssdlc`: Project-local CLI wrapper.

### Step 3: Run Parallel Tickets & Supervise Evidence
Inside `/opt/project-alpha`:

```bash
# 1. Add your project-specific API key to .ssdlc/project.env
nano .ssdlc/project.env

# 2. Verify isolation, CLI versions, and key fingerprints
./ssdlc doctor

# 3. Run the autonomous SSDLC loop (auto-detects Risk Tier S, M, or L)
./ssdlc run --ticket TKT-001 --goal "Add OAuth2 PKCE login flow" --tier auto

# 4. Inspect the SHA-256 Evidence Pack
cat .ssdlc/TKT-001/EVIDENCE_PACK.md

# 5. Approve & merge (automatically prunes temporary worktree to save disk space)
./ssdlc approve TKT-001
```

---

## 3. Updating `codex`, `claude`, `agy`, and `uv`

* **Live Fast Update (updates `/opt/ssdlc/.tools` mounted into all containers)**:
  ```bash
  /opt/ssdlc/setup-ssdlc.sh --update-tools
  # or from inside any initialized project:
  ./ssdlc update-tools
  ```
* **Rebuild Shared Docker Image CLI Layer**:
  ```bash
  /opt/ssdlc/setup-ssdlc.sh --rebuild-image
  ```
* **Check Installed Versions**:
  ```bash
  /opt/ssdlc/setup-ssdlc.sh --versions
  ```
