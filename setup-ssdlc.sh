#!/usr/bin/env bash
# ==============================================================================
# setup-ssdlc.sh
# Zero-Global-Touch SSDLC Toolkit, Shared Image Builder, CLI Updater & Project Initializer
#
# GUARANTEES:
#   - NEVER modifies ~/.codex, ~/.gemini, ~/.claude, or host global directories.
#   - All shared tools live inside .tools/ and the 'ssdlc-runner:latest' Docker image.
#   - Each project gets its own isolated .ssdlc/project.env, .ssdlc/.runtime/ (CODEX_HOME &
#     CLAUDE_CONFIG_DIR), .ssdlc/docker-compose.yml, .agents/skills/ssdlc/, and ./ssdlc CLI.
# ==============================================================================

set -euo pipefail

SSDLC_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="${SSDLC_HOME}/.tools"
TOOLS_BIN="${TOOLS_DIR}/bin"
TOOLS_NPM="${TOOLS_DIR}/npm-global"
IMAGE_NAME="ssdlc-runner:latest"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

usage() {
  cat << EOF
Usage: $0 [COMMAND] [OPTIONS]

Commands:
  --bootstrap                     Initialize .tools and build '${IMAGE_NAME}' (Zero changes to host configs)
  --update-tools                  Update codex, claude, agy, and uv inside .tools (instant live update for all projects)
  --rebuild-image                 Rebuild the thin CLI layer of '${IMAGE_NAME}' with latest codex & claude
  --init-project <DIR> [OPTIONS]  Initialize isolated SSDLC config & Docker runner inside project <DIR>
      --id <PROJECT_ID>           Unique project identifier for OpenRouter & state telemetry (default: folder name)
      --mode <docker|local>       Default execution mode for the project (default: docker)
      --sync-remote <auto|true|false>  Remote sync policy (default: auto, based on origin)
  --versions                      Display current versions of codex, claude, agy, uv, and docker
  --help, -h                      Show this help message

Examples:
  # 1. Bootstrap SSDLC toolkit & shared Docker image:
  ./setup-ssdlc.sh --bootstrap

  # 2. Initialize Project A and Project B (each isolated with its own keys & state):
  ./setup-ssdlc.sh --init-project /path/to/project-alpha --id proj-alpha --mode docker
  ./setup-ssdlc.sh --init-project /path/to/project-beta  --id proj-beta  --mode docker

  # 3. Update codex, claude, and uv across all projects in one command:
  ./setup-ssdlc.sh --update-tools
EOF
}

# ------------------------------------------------------------------------------
# 1. Isolated Shared Tools Management (.tools)
#    Mounted read-only into all project containers and used by local-isolated runs.
# ------------------------------------------------------------------------------
sync_or_update_tools() {
  log_info "Updating isolated CLI tools inside ${TOOLS_DIR} (host configs untouched)..."
  mkdir -p "${TOOLS_BIN}" "${TOOLS_NPM}/bin"

  # Python dependencies stay inside the shared toolkit, never in host site-packages.
  if [ ! -x "${TOOLS_DIR}/python/bin/python3" ]; then
    python3 -m venv "${TOOLS_DIR}/python"
  fi
  "${TOOLS_DIR}/python/bin/python3" -m pip install -r "${SSDLC_HOME}/requirements.txt"

  # 1. Sync or link host 'agy' and 'codex' as fallback if present on host
  if [ -x "${HOME}/.local/bin/agy" ]; then
    ln -sf "${HOME}/.local/bin/agy" "${TOOLS_BIN}/agy"
    log_ok "Linked agy -> $(${TOOLS_BIN}/agy --version 2>/dev/null | head -n1 || echo 'ready')"
  elif command -v agy >/dev/null 2>&1; then
    ln -sf "$(command -v agy)" "${TOOLS_BIN}/agy"
  fi

  if [ -x "/usr/local/bin/codex" ] && [ ! -x "${TOOLS_NPM}/bin/codex" ]; then
    ln -sf "/usr/local/bin/codex" "${TOOLS_BIN}/codex"
  fi

  # 2. Install/Update 'uv' strictly inside .tools/bin
  log_info "Installing/updating 'uv' in ${TOOLS_BIN}..."
  if env UV_INSTALL_DIR="${TOOLS_BIN}" INSTALLER_NO_MODIFY_PATH=1 \
       curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; then
    log_ok "uv ready at ${TOOLS_BIN}/uv ($("${TOOLS_BIN}/uv" --version 2>/dev/null || echo 'ok'))"
  else
    log_warn "Could not download uv from astral.sh (offline or restricted network)."
  fi

  # 3. Update @openai/codex and @anthropic-ai/claude-code via npm inside .tools/npm-global
  if command -v npm >/dev/null 2>&1; then
    log_info "Updating @openai/codex and @anthropic-ai/claude-code in ${TOOLS_NPM}..."
    NPM_CONFIG_PREFIX="${TOOLS_NPM}" npm install -g @openai/codex@latest @anthropic-ai/claude-code@latest || \
      log_warn "npm install failed or partially completed."
    for bin_name in codex claude; do
      # Check the executable using its absolute host path.
      if [ -x "${TOOLS_NPM}/bin/${bin_name}" ]; then
        # The target is relative to the link's directory (.tools/bin), not the cwd.
        # This keeps the same link valid on the host and under /opt/ssdlc in Docker.
        ln -sf "../npm-global/bin/${bin_name}" "${TOOLS_BIN}/${bin_name}"
      fi
    done
  elif command -v docker >/dev/null 2>&1; then
    log_info "Host npm not installed; using Docker container to update codex & claude in ${TOOLS_NPM}..."
    # Relative links written into the bind mount also work at its host path.
    docker run --rm \
      -v "${TOOLS_DIR}:/opt/ssdlc/.tools" \
      -e NPM_CONFIG_PREFIX="/opt/ssdlc/.tools/npm-global" \
      node:22-slim \
      bash -c "npm install -g @openai/codex@latest @anthropic-ai/claude-code@latest && \
               ln -sf ../npm-global/bin/codex /opt/ssdlc/.tools/bin/codex && \
               ln -sf ../npm-global/bin/claude /opt/ssdlc/.tools/bin/claude" || \
      log_warn "Docker-based npm tool update skipped or failed."
  fi

  # 4. If agy supports self-update and is installed, trigger check only when requested
  log_ok "Shared tools directory ${TOOLS_BIN} updated."
}

# ------------------------------------------------------------------------------
# 2. Build / Rebuild Shared Docker Image (ssdlc-runner:latest)
# ------------------------------------------------------------------------------
build_shared_image() {
  local bust_cache="${1:-false}"
  if ! command -v docker >/dev/null 2>&1; then
    log_err "Docker is not installed on this machine."
    return 1
  fi

  chmod +x "${SSDLC_HOME}/orchestrator.py"

  local build_args=()
  if [ "$bust_cache" = "true" ]; then
    build_args+=(--build-arg "TOOLS_CACHEBUST=$(date +%s)")
    log_info "Rebuilding CLI layer of ${IMAGE_NAME} with fresh cache bust..."
  else
    log_info "Building shared base image ${IMAGE_NAME}..."
  fi

  docker build "${build_args[@]}" -t "${IMAGE_NAME}" "${SSDLC_HOME}"
  log_ok "Docker image '${IMAGE_NAME}' is ready (shared across all projects via OverlayFS)."
}

# ------------------------------------------------------------------------------
# 3. Show Versions (Host + .tools + Container)
# ------------------------------------------------------------------------------
show_versions() {
  echo "=== SSDLC Toolkit Versions (${SSDLC_HOME}) ==="
  export PATH="${TOOLS_BIN}:${TOOLS_NPM}/bin:${HOME}/.local/bin:/usr/local/bin:${PATH}"
  for cmd in agy codex claude uv git docker; do
    if command -v "$cmd" >/dev/null 2>&1; then
      echo "  - ${cmd}: $("$cmd" --version 2>/dev/null | head -n1) ($(command -v "$cmd"))"
    else
      echo "  - ${cmd}: NOT FOUND"
    fi
  done
  if command -v docker >/dev/null 2>&1 && docker image inspect "${IMAGE_NAME}" >/dev/null 2>&1; then
    echo "  - Docker Image ${IMAGE_NAME}: BUILT ($(docker image inspect "${IMAGE_NAME}" --format '{{.Created}}'))"
  else
    echo "  - Docker Image ${IMAGE_NAME}: NOT BUILT YET (run: $0 --bootstrap)"
  fi
}

# ------------------------------------------------------------------------------
# 4. Initialize an Isolated Project (<PROJECT_DIR>)
#    Zero changes outside <PROJECT_DIR>!
# ------------------------------------------------------------------------------
init_project() {
  local proj_dir="$1"
  local proj_id="$2"
  local exec_mode="$3"
  local sync_mode="$4"
  local sync_mode_explicit="$5"

  mkdir -p "${proj_dir}"
  proj_dir="$(cd "${proj_dir}" && pwd)"
  if [ -z "$proj_id" ]; then
    proj_id="$(basename "${proj_dir}")"
  fi

  log_info "Initializing isolated SSDLC environment for project '${proj_id}' at ${proj_dir}..."

  local proj_ssdlc="${proj_dir}/.ssdlc"
  local proj_runtime="${proj_ssdlc}/.runtime"
  local proj_skill_dir="${proj_dir}/.agents/skills/ssdlc"

  mkdir -p "${proj_runtime}/codex_home" "${proj_runtime}/claude_home" "${proj_runtime}/cache" "${proj_skill_dir}"

  # 1. Create per-project .ssdlc/project.env (never overwrites existing keys)
  local env_file="${proj_ssdlc}/project.env"
  if [ ! -f "${env_file}" ]; then
    cat << EOF > "${env_file}"
# ==============================================================================
# ${env_file}
# Per-Project Isolated SSDLC Configuration & Unique API Keys
# Project: ${proj_id}
# ==============================================================================

export SSDLC_PROJECT_ID="${proj_id}"
export SSDLC_EXECUTION_MODE="${exec_mode}"   # "docker" (recommended) or "local"
export SSDLC_SYNC_REMOTE="${sync_mode}"  # auto detects GitHub/GitLab origin

# --- Unique Project API Keys (for per-project billing & auditability) ---
# export OPENROUTER_API_KEY="sk-or-v1-${proj_id}-..."
# export OPENAI_API_KEY="sk-proj-${proj_id}-..."
# export ANTHROPIC_API_KEY="sk-ant-${proj_id}-..."

# Optional: Set to "true" if you want this project to inherit
# host flat-rate AI subscriptions (ChatGPT Plus/Pro and Claude Pro/Max)
export SSDLC_INHERIT_HOST_CODEX_AUTH="true"
export SSDLC_INHERIT_HOST_CLAUDE_AUTH="true"

# --- Role Engine & Model Routing ---
# --- Dynamic Squad Dispatcher Routing ---
# Set to "auto" to let SquadDispatcher dynamically select the optimal engine
# (Claude Max for UI/architecture, Codex for algorithms/STRIDE, agy for fast docs/recon, OpenRouter for DeepSeek-R1)
export SSDLC_RECON_ENGINE="auto"
export SSDLC_RECON_MODEL=""

export SSDLC_PO_ENGINE="auto"
export SSDLC_PO_MODEL=""

export SSDLC_DESIGN_ARCH_ENGINE="auto"
export SSDLC_DESIGN_ARCH_MODEL=""
export SSDLC_DESIGN_SEC_ENGINE="auto"
export SSDLC_DESIGN_SEC_MODEL=""

export SSDLC_DEV_ENGINE="auto"
export SSDLC_DEV_MODEL=""
export SSDLC_DEV_ESCALATION_MODEL=""
export SSDLC_DEV_USE_OPENROUTER="false"

export SSDLC_REVIEW_ENGINE="auto"
export SSDLC_REVIEW_MODELS=""

export SSDLC_QA_ENGINE="auto"
export SSDLC_QA_MODEL=""

export SSDLC_APPSEC_ENGINE="auto"
export SSDLC_APPSEC_MODEL=""

export SSDLC_OPS_ENGINE="auto"
export SSDLC_OPS_MODEL=""

export SSDLC_DOCS_ENGINE="auto"
export SSDLC_DOCS_MODEL=""

export SSDLC_DEFAULT_TIER="auto"
export SSDLC_CONTAINER_PIDS_LIMIT="1024"
export SSDLC_MAX_DESIGN_ROUNDS=3
export SSDLC_MAX_DEV_RETRIES=3

# --- 3. Ephemeral Container Resource & Privilege Limits ---
export SSDLC_CONTAINER_MEMORY="4g"
export SSDLC_CONTAINER_CPUS="2"
export SSDLC_CONTAINER_PIDS_LIMIT="512"
EOF
    chmod 600 "${env_file}"
    log_ok "Created project config: ${env_file}"
  else
    log_ok "Preserved existing project config: ${env_file}"
    if [ "${sync_mode_explicit}" = "true" ]; then
      if grep -q '^export SSDLC_SYNC_REMOTE=' "${env_file}"; then
        sed -i "s/^export SSDLC_SYNC_REMOTE=.*/export SSDLC_SYNC_REMOTE=\"${sync_mode}\"/" "${env_file}"
      else
        printf '\nexport SSDLC_SYNC_REMOTE="%s"\n' "${sync_mode}" >> "${env_file}"
      fi
    elif ! grep -q '^export SSDLC_SYNC_REMOTE=' "${env_file}"; then
      printf '\nexport SSDLC_SYNC_REMOTE="auto"\n' >> "${env_file}"
    fi
  fi

  # 2. Create isolated CODEX_HOME/config.toml inside <project>/.ssdlc/.runtime/codex_home
  local proj_codex_toml="${proj_runtime}/codex_home/config.toml"
  if [ ! -f "${proj_codex_toml}" ]; then
    cat << EOF > "${proj_codex_toml}"
model = "gpt-6.1-sol"
model_reasoning_effort = "medium"
approvals_reviewer = "auto_review"
sandbox_mode = "danger-full-access"

[sandbox_workspace_write]
writable_roots = [
  "${proj_runtime}/codex_home",
  "${proj_runtime}/cache",
  "/workspace/.ssdlc/.runtime/codex_home",
  "/workspace/.ssdlc/.runtime/cache",
  "/root/.cache",
  "/root/.npm"
]

[projects."${proj_dir}"]
trust_level = "trusted"

[projects."/workspace"]
trust_level = "trusted"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
EOF
    chmod 600 "${proj_codex_toml}"
    log_ok "Created isolated CODEX_HOME config: ${proj_codex_toml}"
  fi

  # 2b. Initialize Living Standards Registry (<project>/.ssdlc/standards/)
  local proj_standards="${proj_ssdlc}/standards"
  mkdir -p "${proj_standards}"
  local standards_profile="${SSDLC_STANDARDS_PROFILE:-general}"
  case "${standards_profile}" in
    general|yuna) ;;
    *) log_err "Unsupported SSDLC_STANDARDS_PROFILE: ${standards_profile}"; return 1 ;;
  esac
  if [ -d "${SSDLC_HOME}/standards" ]; then
    for std_template in "${SSDLC_HOME}/standards/"*.md; do
      [ -f "${std_template}" ] || continue
      local base_name
      base_name="$(basename "${std_template}")"
      if [ "${standards_profile}" = "general" ] && [ "${base_name}" != "00-PROJECT_CONVENTIONS.md" ]; then
        continue
      fi
      if [ ! -f "${proj_standards}/${base_name}" ]; then
        cp "${std_template}" "${proj_standards}/${base_name}"
        log_ok "Created initial standard: ${proj_standards}/${base_name}"
      fi
    done
  fi

  # 3. Create per-project Docker Compose file (<project>/.ssdlc/docker-compose.yml)
  local compose_file="${proj_ssdlc}/docker-compose.yml"
  cat << EOF > "${compose_file}"
services:
  ssdlc:
    image: ${IMAGE_NAME}
    container_name: ssdlc-${proj_id}
    working_dir: /workspace
    env_file:
      - ./project.env
    environment:
      - SSDLC_PROJECT_ID=${proj_id}
      - CODEX_HOME=/workspace/.ssdlc/.runtime/codex_home
      - CLAUDE_CONFIG_DIR=/workspace/.ssdlc/.runtime/claude_home
      - XDG_CACHE_HOME=/workspace/.ssdlc/.runtime/cache
      - HOME=/workspace/.ssdlc/.runtime
      - IS_SANDBOX=1
      - NODE_OPTIONS=--max-old-space-size=1536
      - CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-}
    volumes:
      - ${proj_dir}:/workspace
      - ${SSDLC_HOME}:/opt/ssdlc:ro
      - /usr/local/bin/codex:/opt/host-bin/codex:ro
      - /usr/local/bin/claude:/opt/host-bin/claude:ro
      - ${HOME}/.local/bin/agy:/opt/host-bin/agy:ro
      - ${HOME}/.codex:${HOME}/.codex:ro
      - ${HOME}/.claude:${HOME}/.claude:ro
      - ${HOME}/.claude.json:${HOME}/.claude.json:ro
      - ${HOME}/.gitconfig:${HOME}/.gitconfig:ro
      - ssdlc-shared-uv-cache:/root/.cache/uv
      - ssdlc-shared-npm-cache:/root/.npm

volumes:
  ssdlc-shared-uv-cache:
    name: ssdlc-shared-uv-cache
  ssdlc-shared-npm-cache:
    name: ssdlc-shared-npm-cache
EOF
  log_ok "Created per-project Docker Compose file: ${compose_file}"

  # 4. Create Workspace-Scoped Antigravity Skill (<project>/.agents/skills/ssdlc/SKILL.md)
  cat << 'EOF' > "${proj_skill_dir}/SKILL.md"
---
name: ssdlc
description: >-
  Project-isolated Risk-Tiered SSDLC Orchestrator (00-Recon, 01-PO, 02-Pair Design
  & STRIDE Threat Model, 03-Dev in isolated Git worktree, 04-Multi-Model Reviewer,
  05-QA, 05b-AppSec Red-Teamer, 06-DevSecOps, 07-Docs, 08-Supervisor Evidence Pack).
  Uses per-project keys and state in .ssdlc/ without touching global host configs.
---

# Project-Isolated SSDLC Coordinator Skill

This project is configured with an isolated SSDLC pipeline (`./ssdlc`). All CLI state (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`), API keys (`.ssdlc/project.env`), and SHA-256 evidence bundles (`.ssdlc/<ticket-id>/`) are strictly scoped to this repository.

## Commands

- **Verify project isolation, CLI versions, and key fingerprints**:
  ```bash
  ./ssdlc doctor
  ```
- **Run the autonomous SSDLC pipeline for a ticket**:
  ```bash
  ./ssdlc run --ticket TKT-001 --goal "<USER_GOAL>" --tier auto
  ```
- **Approve & merge the verified worktree after supervisor inspection**:
  ```bash
  ./ssdlc approve TKT-001
  ```
- **Reject & bounce back to Dev/Review/QA with supervisor feedback**:
  ```bash
  ./ssdlc reject TKT-001 --reason "<FEEDBACK>"
  ```
- **Update CLI tools (codex, claude, agy, uv) across the shared runner**:
  ```bash
  ./ssdlc update-tools
  ```
EOF
  log_ok "Created workspace-local Antigravity skill: ${proj_skill_dir}/SKILL.md"

  # 5. Create Project Wrapper Script (<project>/ssdlc)
  local wrapper="${proj_dir}/ssdlc"
  cat << EOF > "${wrapper}"
#!/usr/bin/env bash
set -euo pipefail

PROJ_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="\${PROJ_DIR}/.ssdlc/project.env"
SSDLC_HOME="${SSDLC_HOME}"

if [ -f "\${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "\${ENV_FILE}"
  set +a
fi

if [ "\${1:-}" = "update-tools" ]; then
  shift
  exec "\${SSDLC_HOME}/setup-ssdlc.sh" --update-tools "\$@"
fi

if [ "\${1:-}" = "rebuild-image" ]; then
  shift
  exec "\${SSDLC_HOME}/setup-ssdlc.sh" --rebuild-image "\$@"
fi

export PATH="\${SSDLC_HOME}/.tools/bin:\${SSDLC_HOME}/.tools/npm-global/bin:\${HOME}/.local/bin:/usr/local/bin:\${PATH}"
export CODEX_HOME="\${PROJ_DIR}/.ssdlc/.runtime/codex_home"
export CLAUDE_CONFIG_DIR="\${PROJ_DIR}/.ssdlc/.runtime/claude_home"
export XDG_CACHE_HOME="\${PROJ_DIR}/.ssdlc/.runtime/cache"

# orchestrator.py runs on the host as the trusted state machine and spawns
# role-scoped Docker containers with kernel-enforced :ro mounts on .ssdlc/ specs/evidence
# and :rw ONLY on the isolated ticket worktree during Phase 3 Dev.
SSDLC_PYTHON="\${SSDLC_HOME}/.tools/python/bin/python3"
if [ ! -x "\${SSDLC_PYTHON}" ]; then
  SSDLC_PYTHON="python3"
fi
exec "\${SSDLC_PYTHON}" "\${SSDLC_HOME}/orchestrator.py" "\$@" --repo "\${PROJ_DIR}"
EOF
  chmod +x "${wrapper}"
  log_ok "Created project CLI entrypoint: ${wrapper}"

  # Ensure .ssdlc/.runtime is gitignored inside the project
  if [ -d "${proj_dir}/.git" ] || [ -f "${proj_dir}/.gitignore" ]; then
    if ! grep -q "\.ssdlc/\.runtime" "${proj_dir}/.gitignore" 2>/dev/null; then
      cat << 'EOF' >> "${proj_dir}/.gitignore"

# SSDLC per-project runtime secrets & active worktrees (keep evidence packs if desired)
.ssdlc/project.env
.ssdlc/.runtime/
.ssdlc/*/worktree/
EOF
      log_ok "Updated ${proj_dir}/.gitignore to exclude .ssdlc/project.env and .ssdlc/.runtime/"
    fi
  fi

  echo ""
  log_ok "Project '${proj_id}' initialized with 100% isolated config!"
  echo "  - Edit project API keys : ${env_file}"
  echo "  - Check project health  : cd ${proj_dir} && ./ssdlc doctor"
  echo "  - Run SSDLC pipeline    : cd ${proj_dir} && ./ssdlc run -t TKT-001 -g \"Your feature goal\""
}

# ------------------------------------------------------------------------------
# CLI Argument Dispatch
# ------------------------------------------------------------------------------
if [ $# -eq 0 ]; then
  usage
  exit 0
fi

ACTION=""
PROJ_DIR=""
PROJ_ID=""
EXEC_MODE="docker"
SYNC_MODE="auto"
SYNC_MODE_EXPLICIT="false"

while [ $# -gt 0 ]; do
  case "$1" in
    --bootstrap)
      ACTION="bootstrap"
      shift
      ;;
    --update-tools)
      ACTION="update-tools"
      shift
      ;;
    --rebuild-image)
      ACTION="rebuild-image"
      shift
      ;;
    --versions)
      ACTION="versions"
      shift
      ;;
    --init-project)
      ACTION="init-project"
      PROJ_DIR="${2:-}"
      if [ -z "$PROJ_DIR" ]; then
        log_err "--init-project requires a target directory path."
        exit 1
      fi
      shift 2
      ;;
    --id)
      PROJ_ID="${2:-}"
      shift 2
      ;;
    --mode)
      EXEC_MODE="${2:-docker}"
      shift 2
      ;;
    --sync-remote)
      SYNC_MODE="${2:-}"
      if [[ ! "${SYNC_MODE}" =~ ^(auto|true|false)$ ]]; then
        log_err "--sync-remote must be auto, true, or false."
        exit 1
      fi
      SYNC_MODE_EXPLICIT="true"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      log_err "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

case "$ACTION" in
  bootstrap)
    chmod +x "${SSDLC_HOME}/orchestrator.py"
    sync_or_update_tools
    build_shared_image "false"
    show_versions
    ;;
  update-tools)
    sync_or_update_tools
    show_versions
    ;;
  rebuild-image)
    sync_or_update_tools
    build_shared_image "true"
    show_versions
    ;;
  versions)
    show_versions
    ;;
  init-project)
    init_project "$PROJ_DIR" "$PROJ_ID" "$EXEC_MODE" "$SYNC_MODE" "$SYNC_MODE_EXPLICIT"
    ;;
  *)
    usage
    ;;
esac
