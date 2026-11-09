#!/usr/bin/env python3
"""
SSDLC Multi-Agent Orchestrator (orchestrator.py)
Zero-Global-Touch, Multi-Project Isolated SSDLC State Machine.

Key Isolation & Traceability Guarantees:
  - Never touches global ~/.codex, ~/.claude, or ~/.gemini configs.
  - Scopes CODEX_HOME and CLAUDE_CONFIG_DIR to <project>/.ssdlc/.runtime/
    so X projects can run in parallel without SQLite locks or config collisions.
  - Loads project-specific API keys from <project>/.ssdlc/project.env.
  - Tags every OpenRouter API call with project_id, ticket_id, phase, and
    records key fingerprints + CLI tool versions in state.json & EVIDENCE_PACK.md.
"""

from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ssdlc_planning import PlanRepair

from ssdlc_tasks import (
    PlanError, TaskError, ResourceLimitError, TaskPlanValidator, TaskScheduler, atomic_json,
    confined, file_hash, git, ticket_lock, validate_document, log_retry_reason,
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_review_verdict(report: str) -> Optional[str]:
    """Require one verdict marker and an exact final verdict line."""
    markers = re.findall(r"\bREVIEW_STATUS\s*:", report)
    final_line = report.rstrip().splitlines()[-1].strip() if report.strip() else ""
    if len(markers) != 1:
        return None
    for verdict in ("APPROVED", "CHANGES_REQUESTED"):
        if final_line == f"REVIEW_STATUS: {verdict}":
            return verdict
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint_secret(secret: str) -> str:
    if not secret:
        return "UNSET"
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]
    prefix = secret[:7] if len(secret) > 10 else "***"
    return f"{prefix}...[sha256:{digest}]"


def get_cli_version(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        return "MISSING"
    try:
        proc = subprocess.run(
            [path, "-V" if binary == "tmux" else "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (proc.stdout or proc.stderr or "").strip().splitlines()
        return f"{out[0]} ({path})" if out else f"FOUND ({path})"
    except Exception:
        return f"FOUND ({path})"


def load_env_file(env_path: Path, override: bool = False) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if override or k not in os.environ:
                os.environ[k] = v


class ProjectRuntimeIsolation:
    """Sets up and enforces per-project isolated CODEX_HOME and CLAUDE_CONFIG_DIR."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.ssdlc_dir = self.repo_root / ".ssdlc"
        self.runtime_dir = self.ssdlc_dir / ".runtime"
        self.codex_home = self.runtime_dir / "codex_home"
        self.claude_home = self.runtime_dir / "claude_home"
        self.cache_dir = self.runtime_dir / "cache"
        self.project_env = self.ssdlc_dir / "project.env"

    def ensure_trusted_path(self, path: Path) -> None:
        codex_toml = self.codex_home / "config.toml"
        if not codex_toml.exists():
            return
        resolved = str(path.resolve())
        header = f'[projects."{resolved}"]'
        content = codex_toml.read_text(encoding="utf-8")
        if header not in content:
            content = content.rstrip() + f'\n\n{header}\ntrust_level = "trusted"\n'
            codex_toml.write_text(content, encoding="utf-8")

    def activate(self) -> Dict[str, str]:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.claude_home.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 1. Load host-wide defaults from ~/.ssdlc/global.env (if present)
        global_env = Path.home() / ".ssdlc" / "global.env"
        if global_env.exists():
            load_env_file(global_env, override=False)

        # 2. Load project-specific .ssdlc/project.env (takes precedence)
        load_env_file(self.project_env, override=True)

        # Ensure isolated CODEX_HOME/config.toml has OpenRouter provider, auto_review,
        # sandbox_workspace_write writable_roots, and trusted project paths without touching ~/.codex
        codex_toml = self.codex_home / "config.toml"
        if not codex_toml.exists():
            default_model = os.environ.get("SSDLC_DEV_MODEL") or "gpt-6.1-sol"
            writable_roots = sorted(
                {
                    str(self.codex_home),
                    str(self.cache_dir),
                    "/workspace/.ssdlc/.runtime/codex_home",
                    "/workspace/.ssdlc/.runtime/cache",
                    "/root/.cache",
                    "/root/.npm",
                }
            )
            roots_toml = ", ".join(f'"{r}"' for r in writable_roots)
            trusted_blocks = [f'[projects."{self.repo_root}"]\ntrust_level = "trusted"']
            if str(self.repo_root) != "/workspace":
                trusted_blocks.append('[projects."/workspace"]\ntrust_level = "trusted"')
            trusted_toml = "\n\n".join(trusted_blocks)

            codex_toml.write_text(
                f'model = "{default_model}"\n'
                'model_reasoning_effort = "medium"\n'
                'approvals_reviewer = "auto_review"\n'
                'sandbox_mode = "danger-full-access"\n\n'
                '[sandbox_workspace_write]\n'
                f'writable_roots = [{roots_toml}]\n\n'
                f'{trusted_toml}\n\n'
                '[model_providers.openrouter]\n'
                'name = "OpenRouter"\n'
                'base_url = "https://openrouter.ai/api/v1"\n'
                'env_key = "OPENROUTER_API_KEY"\n',
                encoding="utf-8",
            )
            os.chmod(codex_toml, 0o600)
        else:
            self.ensure_trusted_path(self.repo_root)

        # Auto-inherit read-only host auth.json by default if present (unless set to false)
        if os.environ.get("SSDLC_INHERIT_HOST_CODEX_AUTH", "true").lower() == "true":
            candidates = [Path.home() / ".codex" / "auth.json", Path("/root/.codex/auth.json")]
            host_auth = next((p for p in candidates if p.exists() and p != (self.codex_home / "auth.json")), None)
            proj_auth = self.codex_home / "auth.json"
            if host_auth and (not proj_auth.exists() or host_auth.stat().st_mtime > proj_auth.stat().st_mtime):
                shutil.copy2(host_auth, proj_auth)
                os.chmod(proj_auth, 0o600)

        # Auto-inherit host Claude auth by default if present (unless set to false)
        if os.environ.get("SSDLC_INHERIT_HOST_CLAUDE_AUTH", "true").lower() == "true":
            candidates_claude = [Path.home() / ".claude.json", Path("/root/.claude.json")]
            host_claude_json = next(
                (p for p in candidates_claude if p.exists() and p != (self.claude_home / ".claude.json")), None
            )
            if host_claude_json:
                for dest in (self.claude_home / ".claude.json", self.runtime_dir / ".claude.json"):
                    if not dest.exists() or host_claude_json.stat().st_mtime > dest.stat().st_mtime:
                        shutil.copy2(host_claude_json, dest)
                        os.chmod(dest, 0o600)
            host_claude_cred = Path.home() / ".claude" / ".credentials.json"
            proj_claude_cred = self.claude_home / ".credentials.json"
            if host_claude_cred.is_file() and (
                not proj_claude_cred.exists()
                or host_claude_cred.stat().st_mtime > proj_claude_cred.stat().st_mtime
            ):
                shutil.copy2(host_claude_cred, proj_claude_cred)
                os.chmod(proj_claude_cred, 0o600)

        # Auto-inherit git user identity from host ~/.gitconfig
        for hg in (Path("/root/.gitconfig"), Path.home() / ".gitconfig"):
            if hg.exists() and hg != (self.runtime_dir / ".gitconfig"):
                runtime_gc = self.runtime_dir / ".gitconfig"
                if not runtime_gc.exists() or hg.stat().st_mtime > runtime_gc.stat().st_mtime:
                    shutil.copy2(hg, runtime_gc)
                    os.chmod(runtime_gc, 0o644)
                break

        os.environ["CODEX_HOME"] = str(self.codex_home)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.claude_home)
        os.environ["XDG_CACHE_HOME"] = str(self.cache_dir)

        project_id = os.environ.get("SSDLC_PROJECT_ID", self.repo_root.name)
        os.environ["SSDLC_PROJECT_ID"] = project_id
        return {
            "project_id": project_id,
            "codex_home": str(self.codex_home),
            "claude_home": str(self.claude_home),
            "cache_dir": str(self.cache_dir),
            "project_env": str(self.project_env),
        }


class CostAndTelemetryEntry:
    def __init__(
        self,
        phase: str,
        role: str,
        engine: str,
        model: str,
        status: str,
        duration_sec: float,
        cost_usd: float = 0.0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        trace_user: str = "",
        reasoning_effort: str = "medium",
    ):
        self.phase = phase
        self.role = role
        self.engine = engine
        self.model = model
        self.status = status
        self.duration_sec = round(duration_sec, 2)
        self.cost_usd = round(cost_usd, 6)
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.trace_user = trace_user
        self.reasoning_effort = reasoning_effort

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": utc_now(),
            "phase": self.phase,
            "role": self.role,
            "engine": self.engine,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "status": self.status,
            "duration_sec": self.duration_sec,
            "cost_usd": self.cost_usd,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "trace_user": self.trace_user,
        }


class SquadDispatcher:
    """
    Intelligent Meta-Orchestrator & Cost/Expertise Triage.
    Dynamically routes each SSDLC phase to the optimal engine and model based on:
      1. Phase & Role requirement
      2. Task Profile ('FRONTEND_UI' | 'ALGORITHMIC' | 'DATABASE_MIGRATION' | 'SECURITY' | 'DOCS' | 'REFACTOR')
      3. Risk Tier ('S' | 'M' | 'L') and Retry Escalation Attempt
      4. Available Local CLI Subscriptions (Claude Max, Codex Plus/Pro, agy Flash) vs OpenRouter
    """

    EFFORT_LADDER = ["low", "medium", "high", "xhigh"]

    BASE_EFFORT_MATRIX: Dict[str, Dict[str, str]] = {
        "00-recon":       {"S": "low",    "M": "low",    "L": "medium"},
        "01-po":          {"S": "low",    "M": "medium", "L": "medium"},
        "02-design-arch": {"S": "low",    "M": "medium", "L": "medium"},
        "02-design-sec":  {"S": "low",    "M": "medium", "L": "medium"},
        "03-dev":         {"S": "low",    "M": "low",    "L": "medium"},
        "04-review":      {"S": "low",    "M": "medium", "L": "medium"},
        "05-qa":          {"S": "low",    "M": "low",    "L": "medium"},
        "05b-appsec":     {"S": "low",    "M": "medium", "L": "medium"},
        "06-ops":         {"S": "low",    "M": "low",    "L": "medium"},
        "07-docs":        {"S": "low",    "M": "low",    "L": "low"},
    }

    @classmethod
    def resolve_reasoning_effort(
        cls, phase_key: str, risk_tier: str, attempt: int = 1
    ) -> str:
        tier = (risk_tier or "M").upper()
        if tier not in ("S", "M", "L"):
            tier = "M"
        base = cls.BASE_EFFORT_MATRIX.get(phase_key, {}).get(tier, "medium")
        idx = cls.EFFORT_LADDER.index(base) if base in cls.EFFORT_LADDER else 1
        if attempt > 1:
            bump = 2 if attempt >= 3 else (attempt - 1)
            idx = min(len(cls.EFFORT_LADDER) - 1, idx + bump)

        max_effort = os.environ.get("SSDLC_MAX_REASONING_EFFORT", "medium").lower().strip()
        if max_effort in cls.EFFORT_LADDER:
            max_idx = cls.EFFORT_LADDER.index(max_effort)
            idx = min(idx, max_idx)

        return cls.EFFORT_LADDER[idx]

    @classmethod
    def has_claude_max(cls) -> bool:
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        return bool(
            (config_dir / ".credentials.json").is_file()
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        ) and bool(shutil.which("claude"))

    @classmethod
    def has_codex_auth(cls) -> bool:
        return bool(
            (Path("/root/.codex/auth.json").exists())
            or (Path.home() / ".codex" / "auth.json").exists()
            or os.environ.get("SSDLC_INHERIT_HOST_CODEX_AUTH", "true").lower() == "true"
        ) and bool(shutil.which("codex"))

    @classmethod
    def has_agy(cls) -> bool:
        return bool(
            (Path.home() / ".local" / "bin" / "agy").exists()
            or Path("/root/.local/bin/agy").exists()
            or Path("/opt/host-bin/agy").exists()
            or shutil.which("agy")
        )

    @classmethod
    def has_openrouter(cls) -> bool:
        return bool(os.environ.get("OPENROUTER_API_KEY", "").strip())

    @classmethod
    def dispatch_role(
        cls,
        phase_key: str,
        risk_tier: str = "M",
        dev_profile: str = "ALGORITHMIC",
        attempt: int = 1,
    ) -> Tuple[str, str, str, str]:
        """
        Returns (engine, model, effort, reasoning_justification).
        Prioritizes:
          - claude (Claude Max) for UI/UX, complex frontend, architecture, and visual audit.
          - agy (gemini-3.8-flash) for ultra-fast, zero-cost Recon, PO, Docs, and summaries.
          - codex (gpt-6-luna / gpt-6.1-sol) for algorithmic backend code and STRIDE security logic.
          - openrouter (deepseek-r1) for specialized adversarial reasoning.
        """
        tier = (risk_tier or "M").upper()
        profile = (dev_profile or "ALGORITHMIC").upper()
        effort = cls.resolve_reasoning_effort(phase_key, tier, attempt=attempt)

        # Check for explicit per-phase user override
        env_engine_var = f"SSDLC_{phase_key.split('-')[-1].upper()}_ENGINE"
        env_model_var = f"SSDLC_{phase_key.split('-')[-1].upper()}_MODEL"
        explicit_engine = os.environ.get(env_engine_var, "").lower().strip()
        explicit_model = os.environ.get(env_model_var, "").strip()

        if explicit_engine and explicit_engine != "auto":
            return explicit_engine, explicit_model, effort, "Explicit env override"

        has_claude = cls.has_claude_max()
        has_codex = cls.has_codex_auth()
        has_agy = cls.has_agy()
        has_or = cls.has_openrouter()

        # PHASE 0: 00-recon (fast repository archaeology)
        if phase_key == "00-recon":
            if has_agy:
                return "agy", "gemini-3.8-flash-low", "low", "Fast repo archaeology via agy (Gemini 3.8 Flash, zero subscription impact)"
            if has_codex:
                return "codex", "gpt-6-luna", "low", "Fast repo archaeology via Codex Luna"
            return "openrouter", "google/gemini-2.0-flash-001", "low", "OpenRouter Gemini Flash fallback"

        # PHASE 1: 01-po (Product Owner & Acceptance Criteria)
        if phase_key == "01-po":
            if profile == "FRONTEND_UI" and has_claude:
                return "claude", "sonnet", effort, "Claude Max (Sonnet 5.5) for precise UI/UX ACs and Safe Area matrix"
            if has_agy and tier == "S":
                return "agy", "gemini-3.8-flash-medium", effort, "agy Gemini Flash for rapid Tier S specification"
            if has_claude:
                return "claude", "sonnet", effort, "Claude Max (Sonnet 5.5) specification steward"
            return "openrouter", "anthropic/claude-3.7-sonnet", effort, "OpenRouter fallback"

        # PHASE 2: 02-design-arch (Architecture & ADR)
        if phase_key == "02-design-arch":
            if has_claude:
                return "claude", "sonnet", effort, "Claude Max (Sonnet 5.5) Lead Systems & UI/UX Architect"
            if has_codex:
                return "codex", "gpt-6.1-sol", effort, "Codex Sol Systems Architect"
            return "openrouter", "anthropic/claude-3.7-sonnet", effort, "OpenRouter Architect fallback"

        # PHASE 2: 02-design-sec (Security Architect & Threat Modeling)
        if phase_key == "02-design-sec":
            if has_codex:
                return "codex", "gpt-6.1-sol", effort, "Codex Sol for rigorous STRIDE threat analysis"
            if has_or:
                return "openrouter", "deepseek/deepseek-r1", effort, "OpenRouter DeepSeek-R1 logic reasoning"
            if has_claude:
                return "claude", "sonnet", effort, "Claude Max (Sonnet 5.5) Security Architect"
            return "openrouter", "deepseek/deepseek-r1", effort, "Fallback STRIDE model"

        # PHASE 3: 03-dev (Implementation in isolated worktree)
        if phase_key == "03-dev":
            if profile == "FRONTEND_UI" and has_claude:
                return "claude", "sonnet", effort, "Claude Max (Sonnet 5.5) specialized Frontend/Mobile/UI implementation"
            if tier == "S" and has_codex:
                return "codex", "gpt-6-luna", effort, "Codex Luna fast lightweight spot-fix implementation"
            if has_claude and profile in ("FULLSTACK", "REFACTOR"):
                return "claude", "sonnet", effort, "Claude Max (Sonnet 5.5) fullstack refactor implementation"
            if has_codex:
                return "codex", "gpt-6.1-sol", effort, "Codex Sol algorithmic/backend implementation"
            if has_claude:
                return "claude", "sonnet", effort, "Claude Max fallback"
            return "openrouter", "anthropic/claude-3.7-sonnet", effort, "OpenRouter Dev fallback"

        # PHASE 4: 04-review (Multi-model code review)
        if phase_key == "04-review":
            if profile == "FRONTEND_UI" and has_claude:
                return "claude", "sonnet", effort, "Claude Max (Sonnet 5.5) UI/UX Fidelity & Visual Spec Checker"
            if has_claude:
                return "claude", "sonnet", effort, "Claude Max (Sonnet 5.5) Senior Code Reviewer"
            if has_codex:
                return "codex", "gpt-6.1-sol", effort, "Codex Sol Code Reviewer"
            return "openrouter", "anthropic/claude-3.7-sonnet", effort, "OpenRouter Review fallback"

        # PHASE 5: 05-qa (Traceability & Verification)
        if phase_key == "05-qa":
            if has_claude:
                return "claude", "sonnet", effort, "Claude Max (Sonnet 5.5) QA & Acceptance Criteria Verifier"
            if has_agy:
                return "agy", "gemini-3.8-flash-medium", effort, "agy Gemini Flash QA matrix verifier"
            return "openrouter", "anthropic/claude-3.7-sonnet", effort, "OpenRouter QA fallback"

        # PHASE 5b: 05b-appsec (Red-Teaming & SCA)
        if phase_key == "05b-appsec":
            if has_or:
                return "openrouter", "deepseek/deepseek-r1", effort, "OpenRouter DeepSeek-R1 Offensive Red-Teamer"
            if has_codex:
                return "codex", "gpt-6.1-sol", effort, "Codex Sol Offensive Red-Teamer"
            if has_claude:
                return "claude", "sonnet", effort, "Claude Max AppSec Auditor"
            return "openrouter", "deepseek/deepseek-r1", effort, "Fallback Red-Teamer"

        # PHASE 6: 06-ops (Release Runbook & Observability)
        if phase_key == "06-ops":
            if has_codex:
                return "codex", "gpt-6-luna", effort, "Codex Luna fast DevSecOps release & rollback runbook"
            if has_agy:
                return "agy", "gemini-3.8-flash-low", effort, "agy Gemini Flash DevSecOps runbook"
            if has_claude:
                return "claude", "sonnet", effort, "Claude Max DevSecOps runbook"
            return "openrouter", "anthropic/claude-3.7-sonnet", effort, "OpenRouter Ops fallback"

        # PHASE 7: 07-docs (Changelog & API Docs)
        if phase_key == "07-docs":
            if has_agy:
                return "agy", "gemini-3.8-flash-low", effort, "agy Gemini 3.8 Flash (ultra-fast zero-cost changelog steward)"
            if has_codex:
                return "codex", "gpt-6-luna", effort, "Codex Luna documentation steward"
            if has_claude:
                return "claude", "sonnet", effort, "Claude Max documentation steward"
            return "openrouter", "google/gemini-2.0-flash-001", effort, "OpenRouter Docs fallback"

        return "openrouter", "anthropic/claude-3.7-sonnet", effort, "Default fallback"

    # Backwards compatibility methods
    @classmethod
    def resolve_dev_engine_and_model(cls, risk_tier: str, dev_profile: str, attempt: int = 1):
        engine, model, effort, _ = cls.dispatch_role("03-dev", risk_tier, dev_profile, attempt)
        return engine, model, effort

    @classmethod
    def resolve_po_model(cls, risk_tier: str) -> str:
        _, model, _, _ = cls.dispatch_role("01-po", risk_tier)
        return model

    @classmethod
    def resolve_design_models(cls, risk_tier: str) -> Tuple[str, str]:
        _, arch_model, _, _ = cls.dispatch_role("02-design-arch", risk_tier)
        _, sec_model, _, _ = cls.dispatch_role("02-design-sec", risk_tier)
        return arch_model, sec_model

    @classmethod
    def resolve_ops_model(cls, risk_tier: str) -> str:
        _, model, _, _ = cls.dispatch_role("06-ops", risk_tier)
        return model

    @classmethod
    def resolve_docs_model(cls, risk_tier: str) -> str:
        _, model, _, _ = cls.dispatch_role("07-docs", risk_tier)
        return model

    @classmethod
    def resolve_review_models(cls, risk_tier: str, attempt: int = 1) -> Tuple[List[str], str]:
        tier = (risk_tier or "M").upper()
        effort = cls.resolve_reasoning_effort("04-review", tier, attempt=attempt)
        models_csv = os.environ.get("SSDLC_REVIEW_MODELS", "").strip()
        if models_csv:
            return [m.strip() for m in models_csv.split(",") if m.strip()], effort
        # Cross-model review: Claude Sonnet (Max) + Codex Sol (Plus/Pro)
        if cls.has_claude_max() and cls.has_codex_auth():
            return ["claude:sonnet", "codex:gpt-6.1-sol"], effort
        if cls.has_claude_max():
            return ["claude:sonnet"], effort
        if cls.has_codex_auth():
            return ["codex:gpt-6.1-sol"], effort
        return ["deepseek/deepseek-r1", "anthropic/claude-3.7-sonnet"], effort


DynamicRoutingPolicy = SquadDispatcher


class EngineRunner:
    """Unified adapter for OpenRouter API, codex CLI, and claude CLI with kernel-enforced :ro/:rw container mounts."""

    IMAGE_NAME = "ssdlc-runner:latest"

    @staticmethod
    def _should_spawn_role_container() -> bool:
        if Path("/.dockerenv").exists():
            return False
        if os.environ.get("SSDLC_EXECUTION_MODE", "docker").lower() != "docker":
            return False
        if not shutil.which("docker"):
            return False
        chk = subprocess.run(
            ["docker", "image", "inspect", EngineRunner.IMAGE_NAME],
            capture_output=True,
        )
        return chk.returncode == 0

    @staticmethod
    def _find_repo_root(start: Path) -> Path:
        cur = start.resolve()
        for p in [cur] + list(cur.parents):
            if (p / ".ssdlc").is_dir():
                return p
        return cur

    @staticmethod
    def _wrap_in_role_container(
        inner_cmd: List[str],
        cwd: Path,
        writable_worktree: Optional[Path] = None,
        pass_ai_keys: bool = True,
        verification_scratch: Optional[Path] = None,
        verification_env: Optional[Dict[str, str]] = None,
        stdin: bool = False,
        auth_profile: Optional[str] = None,
    ) -> List[str]:
        """
        Wraps a CLI/test invocation in a fresh ephemeral container where:
          - The entire project repository (including .git/hooks, .git/config, .ssdlc/01-po,
            02-design, state.json, .agents) is mounted READ-ONLY (:ro) at the kernel level.
          - .ssdlc/.runtime (CODEX_HOME, CLAUDE_CONFIG_DIR, XDG_CACHE_HOME) is mounted :rw.
          - ONLY when writable_worktree is passed (Phase 3 Dev) is the ticket worktree mounted :rw,
            plus its specific .git/worktrees/<wt_name> metadata dir (keeping .git/hooks & .git/config :ro).
          - Enforces --security-opt no-new-privileges:true, --pids-limit, --memory, and --cpus.
          - Strips AI API keys when pass_ai_keys=False (e.g., when running untrusted unit tests).
        """
        repo_root = EngineRunner._find_repo_root(cwd)
        if auth_profile not in (None, "agy"):
            raise TaskError("Unsupported role authentication profile")
        runtime_dir = repo_root / ".ssdlc" / ".runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        ssdlc_home = Path(__file__).resolve().parent

        pids_limit = os.environ.get("SSDLC_CONTAINER_PIDS_LIMIT", "1024")
        if not pids_limit.isdigit() or int(pids_limit) < 16:
            raise TaskError("SSDLC_CONTAINER_PIDS_LIMIT must be a finite integer >= 16")
        mem_limit = os.environ.get("SSDLC_CONTAINER_MEMORY", "4g")
        cpus_limit = os.environ.get("SSDLC_CONTAINER_CPUS", "2")

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--name",
            "ssdlc-role-" + uuid.uuid4().hex,
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(pids_limit),
            "--memory",
            str(mem_limit),
            "--cpus",
            str(cpus_limit),
            "--entrypoint",
            "/bin/sh",
            "-w",
            str(cwd.resolve()),
            "-v",
            f"{repo_root}:{repo_root}:ro",
            "-v",
            f"{ssdlc_home}:/opt/ssdlc:ro",
        ]
        if stdin:
            docker_cmd.insert(2, "-i")
        if pass_ai_keys:
            docker_cmd.extend([
                "-v", f"{runtime_dir}:{runtime_dir}:rw",
                "-v", "ssdlc-shared-uv-cache:/root/.cache/uv",
                "-v", "ssdlc-shared-npm-cache:/root/.npm",
            ])
            # Developer agents also run tests. Keep their caches outside source,
            # just as for the trusted verification runner, without ignoring files.
            cache_parent = runtime_dir / "cache" / "roles"
            cache_parent.mkdir(parents=True, exist_ok=True)
            role_cache = Path(tempfile.mkdtemp(prefix="invocation-", dir=cache_parent))
            for key, value in {
                "PYTHONPYCACHEPREFIX": str(role_cache / "pycache"),
                "PYTEST_ADDOPTS": (os.environ.get("PYTEST_ADDOPTS", "") + f" -o cache_dir={role_cache / 'pytest'}").strip(),
                "MYPY_CACHE_DIR": str(role_cache / "mypy"),
                "RUFF_CACHE_DIR": str(role_cache / "ruff"),
            }.items():
                docker_cmd.extend(["-e", f"{key}={value}"])
        else:
            # Test code must not read isolated credentials through the repository mount.
            docker_cmd.extend(["--tmpfs", f"{repo_root / '.ssdlc'}:rw,size=16m"])
            # A pristine, read-only toolkit has no runtime directory to mask;
            # Docker cannot create a nested mountpoint inside that bind mount.
            if (ssdlc_home / ".ssdlc").exists():
                docker_cmd.extend(["--tmpfs", "/opt/ssdlc/.ssdlc:rw,size=16m"])
            if cwd.resolve() != repo_root:
                docker_cmd.extend(["-v", f"{cwd.resolve()}:{cwd.resolve()}:ro"])
            if verification_scratch is not None:
                docker_cmd.extend(["-v", f"{verification_scratch}:{verification_scratch}:rw"])
            for key, value in (verification_env or {}).items():
                docker_cmd.extend(["-e", f"{key}={value}"])
        if not str(repo_root).startswith("/tmp/"):
            docker_cmd.extend(["--tmpfs", "/tmp:rw,exec,size=512m"])
        if Path("/usr/local/bin/codex").exists():
            docker_cmd.extend(["-v", "/usr/local/bin/codex:/opt/host-bin/codex:ro"])
        host_agy = Path.home() / ".local" / "bin" / "agy"
        if host_agy.exists():
            docker_cmd.extend(["-v", f"{host_agy}:/opt/host-bin/agy:ro"])

        # Auto-mount host subscriptions into container as read-only (zero host pollution)
        # Claude Max auth
        host_claude_json = Path.home() / ".claude.json"
        if pass_ai_keys and host_claude_json.exists():
            docker_cmd.extend(["-v", f"{host_claude_json}:/root/.claude.json:ro"])
        host_claude_cred = Path.home() / ".claude" / ".credentials.json"
        if pass_ai_keys and host_claude_cred.exists():
            docker_cmd.extend([
                "-v", f"{host_claude_cred}:/root/.claude/.credentials.json:ro",
                "--tmpfs", "/root/.cache/claude-cli-nodejs:rw",
                "--tmpfs", "/root/.claude/backups:rw",
                "--tmpfs", "/root/.claude/cache:rw",
                "--tmpfs", "/root/.claude/sessions:rw",
            ])

        # Codex Plus/Pro auth
        host_codex_auth = Path.home() / ".codex" / "auth.json"
        if pass_ai_keys and host_codex_auth.exists():
            docker_cmd.extend(["-v", f"{host_codex_auth}:/root/.codex/auth.json:ro"])
        host_codex_ver = Path.home() / ".codex" / "version.json"
        if pass_ai_keys and host_codex_ver.exists():
            docker_cmd.extend(["-v", f"{host_codex_ver}:/root/.codex/version.json:ro"])

        # agy Gemini Flash auth & ephemeral tmpfs mounts
        host_gemini = Path.home() / ".gemini"
        if auth_profile == "agy":
            # AGY is an AI role, not an untrusted verifier. Give it only its own
            # copied OAuth store, writable for refresh without touching host auth.
            host_token = host_gemini / "antigravity-cli" / "antigravity-oauth-token"
            agy_home = runtime_dir / "agy_auth"
            token = agy_home / "antigravity-cli" / "antigravity-oauth-token"
            if not host_token.is_file() and not token.is_file():
                raise TaskError("AGY authentication is unavailable; configure host login before selecting agy")
            token.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(agy_home, 0o700)
            os.chmod(token.parent, 0o700)
            if host_token.is_file() and (not token.exists() or host_token.stat().st_mtime > token.stat().st_mtime):
                shutil.copy2(host_token, token)
            os.chmod(token, 0o600)
            docker_cmd.extend(["-v", f"{agy_home}:/root/.gemini:rw"])
        elif pass_ai_keys and host_gemini.exists():
            docker_cmd.extend([
                "-v", f"{host_gemini}:/root/.gemini:ro",
                "--tmpfs", "/root/.gemini/antigravity-cli/brain:rw",
                "--tmpfs", "/root/.gemini/antigravity-cli/conversations:rw",
                "--tmpfs", "/root/.gemini/antigravity-cli/log:rw",
                "--tmpfs", "/root/.gemini/antigravity-cli/cache:rw",
                "--tmpfs", "/root/.gemini/antigravity-cli/annotations:rw",
            ])

        if writable_worktree is not None:
            wt_resolved = writable_worktree.resolve()
            docker_cmd.extend(["-v", f"{wt_resolved}:{wt_resolved}:rw"])
            # Mount ONLY .git/worktrees/<wt_name> :rw so git status/diff inside the worktree works
            # while .git/hooks, .git/config, and .git/refs remain kernel-enforced :ro!
            wt_git_meta = repo_root / ".git" / "worktrees" / wt_resolved.name
            if wt_git_meta.is_dir():
                docker_cmd.extend(["-v", f"{wt_git_meta}:{wt_git_meta}:rw"])

        base_env_keys = ["SSDLC_PROJECT_ID"]
        if pass_ai_keys:
            base_env_keys.extend(
                [
                    "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CACHE_HOME",
                    "OPENROUTER_API_KEY",
                    "OPENAI_API_KEY",
                    "ANTHROPIC_API_KEY",
                    "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_AUTH_TOKEN",
                    "CLAUDE_CODE_OAUTH_TOKEN",
                ]
            )

        for env_key in base_env_keys:
            val = os.environ.get(env_key)
            if val:
                docker_cmd.extend(["-e", f"{env_key}={val}"])

        name = docker_cmd[docker_cmd.index("--name") + 1]
        marker = "__SSDLC_RESOURCE_" + name + "__"
        # Shell builtins still work at the PID limit. Read counters while the
        # cgroup exists, before --rm removes it, without modifying CLI stdout.
        wrapper = ('"$@"; rc=$?; '
                   'for f in pids.current pids.max pids.peak pids.events memory.peak memory.events; do '
                   'if [ -r "/sys/fs/cgroup/$f" ]; then '
                   'while read -r key value; do '
                   'if [ -n "$value" ]; then '
                   f'printf "{marker} %s.%s=%s\\n" "$f" "$key" "$value" >&2; '
                   'else '
                   f'printf "{marker} %s=%s\\n" "$f" "$key" >&2; '
                   'fi; done < "/sys/fs/cgroup/$f"; fi; done; exit "$rc"')
        docker_cmd.extend([EngineRunner.IMAGE_NAME, "-c", wrapper, "ssdlc-role", *inner_cmd])
        return docker_cmd

    @staticmethod
    def _run_role_command(cmd, *, cwd, env, timeout, input=None):
        """Execute once; preserve resource evidence even for zero-exit PID failures."""
        cwd = Path(cwd)
        docker = cmd[:2] == ["docker", "run"] and "--name" in cmd
        name = cmd[cmd.index("--name") + 1] if docker else None
        timed_out = False
        proc = None
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                                  text=True, timeout=timeout, input=input)
        except subprocess.TimeoutExpired:
            timed_out = True
            if name:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
            raise
        finally:
            if name:
                metrics = {}
                marker = "__SSDLC_RESOURCE_" + name + "__ "
                if proc is not None:
                    lines = (proc.stderr or "").splitlines(keepends=True)
                    for line in lines:
                        if line.startswith(marker):
                            key, separator, value = line[len(marker):].strip().partition("=")
                            if separator:
                                metrics[key] = value
                    proc.stderr = "".join(line for line in lines if not line.startswith(marker))
                path = EngineRunner._find_repo_root(cwd) / ".ssdlc/.runtime/resources" / (name + ".json")
                failed = any(int(metrics.get(key, "0")) > 0 for key in
                             ("pids.events.max", "memory.events.oom", "memory.events.oom_kill"))
                atomic_json(path, {"container": name, "cwd": str(cwd), "timed_out": timed_out,
                                   "exit_code": None if proc is None else proc.returncode,
                                   "telemetry_available": bool(metrics), "counters": metrics,
                                   "resource_failure": failed})
                if proc is not None:
                    proc.resource_failure = failed
                    proc.resource_evidence = str(path)
        return proc

    @staticmethod
    def run_sandboxed_cmd(
        cmd: List[str],
        cwd: Path,
        timeout: int = 300,
        _resource_retry: bool = True,
    ) -> Tuple[str, str, int]:
        """
        Executes a deterministic test or scanner command inside the read-only role container
        (with AI API keys stripped and /tmp on tmpfs) when Docker role isolation is enabled.
        """
        # Scratch is per invocation, so tickets and retries cannot share writable caches.
        with tempfile.TemporaryDirectory(prefix="ssdlc-check-") as scratch_name:
            scratch = Path(scratch_name)
            settings = {
                "TMPDIR": str(scratch), "XDG_CACHE_HOME": str(scratch / "cache"),
                "HOME": str(scratch / "home"), "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
                "MYPY_CACHE_DIR": str(scratch / "mypy"), "RUFF_CACHE_DIR": str(scratch / "ruff"),
                "npm_config_cache": str(scratch / "npm"),
                "PYTEST_ADDOPTS": (os.environ.get("PYTEST_ADDOPTS", "") + f" -o cache_dir={scratch / 'pytest'}").strip(),
            }
            (scratch / "home").mkdir()
            if EngineRunner._should_spawn_role_container():
                exec_cmd = EngineRunner._wrap_in_role_container(
                    cmd, cwd=cwd, pass_ai_keys=False,
                    verification_scratch=scratch, verification_env=settings,
                )
                env = None
            else:
                # Local mode has isolated cache/env but no kernel-enforced read-only mount.
                exec_cmd = cmd
                hidden = {"OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                          "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME",
                          "CLAUDE_CONFIG_DIR", "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN"}
                env = {k: v for k, v in os.environ.items() if k not in hidden}
                env.update(settings)
            started = time.monotonic()
            proc = EngineRunner._run_role_command(exec_cmd, cwd=cwd, env=env, timeout=timeout)
            if getattr(proc, "resource_failure", False):
                remaining = int(timeout - (time.monotonic() - started))
                if _resource_retry and remaining > 0:
                    print(f"[resources] Verification hit a container limit; retrying once in a fresh container. Evidence: {proc.resource_evidence}", flush=True)
                    out, err, rc = EngineRunner.run_sandboxed_cmd(cmd, cwd, remaining, _resource_retry=False)
                    return out, f"Environment resource retry; first-attempt evidence: {proc.resource_evidence}\n" + err, rc
                return proc.stdout or "", (proc.stderr or "") + f"\nENVIRONMENT_RESOURCE: {proc.resource_evidence}", 125
        return proc.stdout or "", proc.stderr or "", proc.returncode

    @staticmethod
    def call_openrouter(
        model: str,
        system_prompt: str,
        user_prompt: str,
        project_id: str,
        ticket_id: str,
        phase: str,
        reasoning_effort: str = "medium",
        temperature: float = 0.2,
    ) -> Tuple[str, float, int, int, str]:
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        trace_user = f"{project_id}:{ticket_id}:{phase}"
        if not api_key:
            out, c, ti, to = EngineRunner.call_cli_fallback(
                model, system_prompt, user_prompt, reasoning_effort=reasoning_effort
            )
            return out, c, ti, to, trace_user

        url = "https://openrouter.ai/api/v1/chat/completions"
        or_effort = "high" if reasoning_effort == "xhigh" else reasoning_effort
        payload: Dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "user": trace_user,
            "reasoning": {"effort": or_effort},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        req_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": f"https://ssdlc.local/{project_id}",
            "X-Title": f"SSDLC/{project_id}/{ticket_id}",
        }

        max_attempts = 3
        data: Dict[str, Any] = {}
        for attempt_idx in range(1, max_attempts + 1):
            req = urllib.request.Request(url, data=req_bytes, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                if e.code in (429, 500, 502, 503, 504) and attempt_idx < max_attempts:
                    backoff = 2 ** attempt_idx
                    print(f"    [WARN] OpenRouter HTTP {e.code} on {phase}; retrying in {backoff}s ({attempt_idx}/{max_attempts})...")
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"OpenRouter HTTP {e.code}: {err_body}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt_idx < max_attempts:
                    backoff = 2 ** attempt_idx
                    print(f"    [WARN] OpenRouter network error ({e}) on {phase}; retrying in {backoff}s ({attempt_idx}/{max_attempts})...")
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"OpenRouter network failure after {max_attempts} attempts: {e}") from e

        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        cost_usd = float(usage.get("cost", 0.0) or 0.0)
        return content, cost_usd, tokens_in, tokens_out, trace_user

    @staticmethod
    def call_cli_fallback(
        model_hint: str,
        system_prompt: str,
        user_prompt: str,
        reasoning_effort: str = "medium",
    ) -> Tuple[str, float, int, int]:
        full_prompt = f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nTASK:\n{user_prompt}"
        if "claude" in model_hint.lower() and shutil.which("claude"):
            out, _, c, ti, to = EngineRunner.run_claude(
                full_prompt, cwd=Path.cwd(), read_only=True, model=model_hint, reasoning_effort=reasoning_effort
            )
            return out, c, ti, to
        if shutil.which("codex"):
            out, _ = EngineRunner.run_codex(
                full_prompt,
                cwd=Path.cwd(),
                sandbox="read-only",
                reasoning_effort=reasoning_effort,
            )
            return out, 0.0, 0, 0
        if shutil.which("claude"):
            out, _, c, ti, to = EngineRunner.run_claude(
                full_prompt, cwd=Path.cwd(), read_only=True, model=model_hint, reasoning_effort=reasoning_effort
            )
            return out, c, ti, to
        raise RuntimeError(
            "Neither OPENROUTER_API_KEY nor local 'codex'/'claude' CLI is available."
        )

    @staticmethod
    def run_agy(
        prompt: str,
        cwd: Path,
        model: str = "gemini-3.8-flash-low",
        reasoning_effort: str = "low",
        timeout: int = 1200,
    ) -> Tuple[str, int]:
        """
        Executes agy CLI in headless print mode.
        Default model: gemini-3.8-flash-low (fast, free, generous quota).
        Avoids rate-limited models on agy.
        """
        target_model = model or "gemini-3.8-flash-low"
        effort = reasoning_effort if reasoning_effort in ("low", "medium", "high", "xhigh") else "low"
        cmd = [
            "agy",
            "-p",
            prompt,
            "--model",
            target_model,
            "--effort",
            effort,
            "--dangerously-skip-permissions",
        ]
        if EngineRunner._should_spawn_role_container():
            cmd = EngineRunner._wrap_in_role_container(
                cmd, cwd=cwd, writable_worktree=None, pass_ai_keys=False, auth_profile="agy"
            )
        proc = EngineRunner._run_role_command(
            cmd,
            cwd=str(cwd),
            env=os.environ.copy(),
            timeout=timeout,
        )
        if getattr(proc, "resource_failure", False):
            raise ResourceLimitError(f"ENVIRONMENT_RESOURCE: container limit reached; code correction not requested. Evidence: {proc.resource_evidence}")
        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return combined.strip(), proc.returncode

    @staticmethod
    def run_codex(
        prompt: str,
        cwd: Path,
        sandbox: str = "read-only",
        model: str = "",
        reasoning_effort: str = "medium",
        use_openrouter: bool = False,
        writable_worktree: Optional[Path] = None,
    ) -> Tuple[str, int]:
        use_container = EngineRunner._should_spawn_role_container()
        effective_sandbox = "danger-full-access" if (use_container or Path("/.dockerenv").exists()) else sandbox
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            effective_sandbox,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
        ]
        if use_openrouter and os.environ.get("OPENROUTER_API_KEY"):
            cmd.extend(["-c", 'model_provider="openrouter"'])
        if model:
            cmd.extend(["-m", model])
        cmd.append("-")
        if use_container:
            cmd = EngineRunner._wrap_in_role_container(
                cmd, cwd=cwd, writable_worktree=writable_worktree, stdin=True
            )
        dev_timeout = int(os.environ.get("SSDLC_DEV_TIMEOUT", "2400"))
        proc = EngineRunner._run_role_command(
            cmd,
            input=prompt,
            cwd=str(cwd),
            env=os.environ.copy(),
            timeout=dev_timeout,
        )
        if getattr(proc, "resource_failure", False):
            raise ResourceLimitError(f"ENVIRONMENT_RESOURCE: container limit reached; code correction not requested. Evidence: {proc.resource_evidence}")
        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        # Successful stdout is the phase result; stderr contains tool transcripts.
        return (proc.stdout or "").strip() if proc.returncode == 0 else combined.strip(), proc.returncode

    @staticmethod
    def run_claude(
        prompt: str,
        cwd: Path,
        read_only: bool = True,
        use_openrouter: bool = False,
        writable_worktree: Optional[Path] = None,
        model: str = "",
        reasoning_effort: str = "medium",
        max_budget_usd: float = 0.0,
    ) -> Tuple[str, int, float, int, int]:
        env = os.environ.copy()
        env["IS_SANDBOX"] = "1"
        if use_openrouter and env.get("OPENROUTER_API_KEY"):
            env["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api"
            env["ANTHROPIC_AUTH_TOKEN"] = env["OPENROUTER_API_KEY"]
            env["ANTHROPIC_API_KEY"] = env["OPENROUTER_API_KEY"]

        # Default model is 'sonnet' (resolves to claude-sonnet-5-5 under Claude Max)
        target_model = model or os.environ.get("SSDLC_DEV_MODEL", "sonnet")
        if "/" in target_model:
            target_model = target_model.split("/")[-1]

        cmd = ["claude", "-p", "--model", target_model, "--output-format", "json"]
        if not read_only:
            cmd.append("--dangerously-skip-permissions")

        # Effort cap
        effort_level = "low" if reasoning_effort == "low" else "medium"
        cmd.extend(["--effort", effort_level])

        # Budget cap per execution (only if explicitly set > 0)
        if max_budget_usd > 0:
            cmd.extend(["--max-budget-usd", f"{max_budget_usd:.2f}"])

        if EngineRunner._should_spawn_role_container():
            cmd = EngineRunner._wrap_in_role_container(
                cmd,
                cwd=cwd,
                writable_worktree=(None if read_only else writable_worktree),
                stdin=True,
            )
        dev_timeout = int(os.environ.get("SSDLC_DEV_TIMEOUT", "2400"))
        proc = EngineRunner._run_role_command(
            cmd,
            input=prompt,
            cwd=str(cwd),
            env=env,
            timeout=dev_timeout,
        )
        if getattr(proc, "resource_failure", False):
            raise ResourceLimitError(f"ENVIRONMENT_RESOURCE: container limit reached; code correction not requested. Evidence: {proc.resource_evidence}")
        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        cost_usd = 0.0
        tokens_in = 0
        tokens_out = 0
        result_text = combined.strip()
        try:
            parsed = json.loads(proc.stdout.strip())
            if isinstance(parsed, dict):
                result_text = parsed.get("result", "") or combined.strip()
                cost_usd = float(parsed.get("total_cost_usd", 0.0) or 0.0)
                usage = parsed.get("usage", {})
                tokens_in = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
                tokens_out = int(usage.get("output_tokens", 0))
        except Exception:
            pass
        return result_text, proc.returncode, cost_usd, tokens_in, tokens_out

    @staticmethod
    def invoke_role(
        engine: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        cwd: Path,
        project_id: str,
        ticket_id: str,
        phase: str,
        sandbox: str = "read-only",
        reasoning_effort: str = "medium",
    ) -> Tuple[str, float, int, int, str]:
        engine = engine.lower().strip()
        trace_user = f"{project_id}:{ticket_id}:{phase}"
        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        if engine == "agy":
            out, rc = EngineRunner.run_agy(
                full_prompt, cwd=cwd, model=model, reasoning_effort=reasoning_effort
            )
            if rc != 0:
                raise RuntimeError(f"agy exited with code {rc}:\n{out}")
            return out, 0.0, 0, 0, trace_user

        if engine == "claude":
            out, rc, cost, tin, tout = EngineRunner.run_claude(
                full_prompt, cwd=cwd, read_only=(sandbox == "read-only"), model=model, reasoning_effort=reasoning_effort
            )
            if rc != 0:
                raise RuntimeError(f"claude exited with code {rc}:\n{out}")
            return out, cost, tin, tout, trace_user

        if engine == "codex":
            out, rc = EngineRunner.run_codex(
                full_prompt,
                cwd=cwd,
                sandbox=sandbox,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            if rc != 0:
                raise RuntimeError(f"codex exited with code {rc}:\n{out}")
            return out, 0.0, 0, 0, trace_user

        return EngineRunner.call_openrouter(
            model,
            system_prompt,
            user_prompt,
            project_id,
            ticket_id,
            phase,
            reasoning_effort=reasoning_effort,
        )


class RemoteSyncManager:
    """Manages synchronization between local SSDLC runs and remote Git/Tracker platforms (GitHub, GitLab)."""

    def __init__(self, repo_root: Path, ticket_id: str, enabled: Optional[bool] = None):
        self.repo_root = repo_root.resolve()
        self.ticket_id = ticket_id
        configured = os.environ.get("SSDLC_SYNC_REMOTE", "auto").strip().lower()
        if configured not in ("auto", "true", "false", "1", "0", "yes", "no"):
            configured = "auto"
        self.mode = ("true" if enabled else "false") if enabled is not None else configured
        self.provider = "generic"
        self.host = ""
        self.owner = ""
        self.repo = ""
        self.remote_url = ""
        self.token = ""
        self._detect_remote()
        self.enabled = (
            self.provider in ("github", "gitlab")
            if self.mode == "auto"
            else self.mode in ("true", "1", "yes")
        )

    def _detect_remote(self) -> None:
        try:
            res = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
            self.remote_url = res.stdout.strip()
        except Exception:
            return

        m_github = re.search(
            r"^(?:git@github\.com:|https?://github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$",
            self.remote_url,
        )
        if m_github:
            self.provider = "github"
            self.host = "github.com"
            self.owner = m_github.group(1)
            self.repo = m_github.group(2)
            self.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
            return

        m_gitlab = re.search(
            r"^(?:git@(gitlab\.[\w.-]+):|https?://(gitlab\.[\w.-]+)/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$",
            self.remote_url,
        )
        if m_gitlab:
            self.provider = "gitlab"
            self.host = m_gitlab.group(1) or m_gitlab.group(2)
            self.owner = m_gitlab.group(3)
            self.repo = m_gitlab.group(4)
            self.token = os.environ.get("GITLAB_TOKEN") or os.environ.get("GL_TOKEN", "")
            return

    def sync_issue(
        self, title: str, body: str, existing_issue_id: Optional[int] = None
    ) -> Tuple[Optional[int], Optional[str]]:
        if not self.enabled:
            return None, None
        if self.provider != "github" or not self.token:
            if self.enabled and not self.token:
                print("    [REMOTE-SYNC] Note: GITHUB_TOKEN unset; skipping remote issue creation/sync.")
            return None, None

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "SSDLC-Orchestrator",
        }

        try:
            if existing_issue_id:
                url = f"https://api.github.com/repos/{self.owner}/{self.repo}/issues/{existing_issue_id}/comments"
                payload = json.dumps({"body": f"### [SSDLC PO Specification]\n\n{body}"}).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    html_url = data.get("html_url")
                    print(f"    [REMOTE-SYNC] Synced specification to Issue #{existing_issue_id}: {html_url}")
                    return existing_issue_id, html_url
            else:
                url = f"https://api.github.com/repos/{self.owner}/{self.repo}/issues"
                payload = json.dumps(
                    {
                        "title": f"[SSDLC] {self.ticket_id}: {title[:100]}",
                        "body": body,
                        "labels": ["ssdlc", "in-progress"],
                    }
                ).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    num = data.get("number")
                    html_url = data.get("html_url")
                    print(f"    [REMOTE-SYNC] Created GitHub Issue #{num}: {html_url}")
                    return num, html_url
        except Exception as e:
            print(f"    [REMOTE-SYNC] Issue sync notice: {e}")
            return None, None

    def push_branch(self, branch: str, cwd: Path) -> bool:
        if not self.enabled:
            return False
        print(f"\n==> [Remote-Sync] Pushing branch '{branch}' to origin...")
        try:
            push_target = "origin"
            if self.provider == "github" and self.token and self.owner and self.repo:
                push_target = f"https://x-access-token:{self.token}@{self.host}/{self.owner}/{self.repo}.git"
            elif self.provider == "gitlab" and self.token and self.owner and self.repo:
                push_target = f"https://oauth2:{self.token}@{self.host}/{self.owner}/{self.repo}.git"
            cmd = ["git", "push", "-u", push_target, f"{branch}:{branch}"]
            proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
            if proc.returncode == 0:
                print(f"    [OK] Remote branch synced: {self.remote_url} ({branch})")
                return True
            else:
                print(f"    [WARN] Remote push failed: {proc.stderr.strip()}")
                return False
        except Exception as e:
            print(f"    [WARN] Remote push error: {e}")
            return False

    def create_or_update_pr(
        self,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        issue_number: Optional[int] = None,
        draft: bool = True,
    ) -> Optional[str]:
        if not self.enabled:
            return None

        compare_url = None
        if self.provider == "github" and self.owner and self.repo:
            compare_url = f"https://github.com/{self.owner}/{self.repo}/compare/{base_branch}...{branch}?expand=1"

        if self.provider != "github" or not self.token:
            if compare_url:
                print(f"    [REMOTE-SYNC] Review / Open PR manually at: {compare_url}")
            return compare_url

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "SSDLC-Orchestrator",
        }

        pr_body = body
        if issue_number:
            pr_body = f"Closes #{issue_number}\n\n" + pr_body

        try:
            list_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/pulls?head={self.owner}:{branch}&state=open"
            req_list = urllib.request.Request(list_url, headers=headers)
            with urllib.request.urlopen(req_list, timeout=15) as resp:
                existing_prs = json.loads(resp.read().decode("utf-8"))

            if existing_prs:
                pr = existing_prs[0]
                pr_number = pr["number"]
                update_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/pulls/{pr_number}"
                payload = json.dumps({"body": pr_body}).encode("utf-8")
                req_up = urllib.request.Request(update_url, data=payload, headers=headers, method="PATCH")
                with urllib.request.urlopen(req_up, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    html_url = data.get("html_url")
                    print(f"    [OK] Updated Pull Request #{pr_number}: {html_url}")
                    return html_url
            else:
                create_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/pulls"
                payload = json.dumps(
                    {
                        "title": f"[SSDLC] {self.ticket_id}: {title[:100]}",
                        "head": branch,
                        "base": base_branch,
                        "body": pr_body,
                        "draft": draft,
                    }
                ).encode("utf-8")
                req_cr = urllib.request.Request(create_url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req_cr, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    html_url = data.get("html_url")
                    print(f"    [OK] Created Draft Pull Request #{data.get('number')}: {html_url}")
                    return html_url
        except Exception as e:
            print(f"    [WARN] Pull Request creation via GitHub API failed: {e}")
            if compare_url:
                print(f"    [REMOTE-SYNC] Review / Open PR manually at: {compare_url}")
            return compare_url


class SSDLCOrchestrator:
    def __init__(
        self,
        repo_root: Path,
        ticket_id: str,
        sync_remote: Optional[bool] = None,
        issue_id: Optional[int] = None,
    ):
        self.repo_root = repo_root.resolve()
        self.isolation = ProjectRuntimeIsolation(self.repo_root)
        self.iso_meta = self.isolation.activate()
        self.project_id = self.iso_meta["project_id"]
        self.ticket_id = ticket_id

        self.ssdlc_root = self.repo_root / ".ssdlc" / ticket_id
        self.dir_recon = self.ssdlc_root / "00-recon"
        self.dir_po = self.ssdlc_root / "01-po"
        self.dir_design = self.ssdlc_root / "02-design"
        self.dir_dev = self.ssdlc_root / "03-dev"
        self.dir_review = self.ssdlc_root / "04-review"
        self.dir_qa = self.ssdlc_root / "05-qa"
        self.dir_appsec = self.ssdlc_root / "05b-appsec"
        self.dir_ops = self.ssdlc_root / "06-ops"
        self.dir_docs = self.ssdlc_root / "07-docs"
        self.worktree_dir = self.ssdlc_root / "worktree"
        self.state_file = self.ssdlc_root / "state.json"

        for d in (
            self.dir_recon,
            self.dir_po,
            self.dir_design,
            self.dir_dev,
            self.dir_review,
            self.dir_qa,
            self.dir_appsec,
            self.dir_ops,
            self.dir_docs,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.state = self._load_or_init_state()
        self.remote_sync = RemoteSyncManager(self.repo_root, self.ticket_id, enabled=sync_remote)
        self.issue_id = issue_id or self.state.get("issue_id")

    def _task_mode(self) -> bool:
        return bool(self.state.get("task_breakdown")) or os.environ.get("SSDLC_ENABLE_TASK_BREAKDOWN", "false").lower() == "true"

    def _ensure_quality_contract(self) -> Dict[str, Any]:
        target = self.dir_recon / "quality-contract.json"
        if target.exists():
            meta = self.state.get("artifacts", {}).get("00-recon/quality-contract.json")
            if not meta or sha256_file(target) != meta["sha256"]:
                raise PlanError("Quality snapshot is unregistered or changed; explicit migration required")
            return json.loads(target.read_text())
        if "00-recon/quality-contract.json" in self.state.get("artifacts", {}):
            raise PlanError("Registered quality contract disappeared; refusing regeneration")
        if not self._task_mode():
            raise PlanError("Task breakdown is disabled")
        if (git(self.repo_root, "rev-parse", "HEAD") != self.state["base_commit"]
                or git(self.repo_root, "status", "--porcelain")):
            raise PlanError("Create a clean, committed baseline before capturing the quality contract")
        policy = self.repo_root / ".ssdlc" / "quality-contract.json"
        if policy.exists():
            contract = json.loads(policy.read_text())
            contract["base_commit"] = self.state["base_commit"]
            contract["baseline"] = []
        else:
            sources = []
            standards = []
            candidates = [self.repo_root / name for name in ("AGENTS.md", ".editorconfig", "pyproject.toml", "package.json", "ruff.toml", ".ruff.toml", "mypy.ini", "pytest.ini")]
            candidates += sorted((self.repo_root / ".ssdlc" / "standards").glob("*.md"))
            for index, path in enumerate(p for p in candidates if p.is_file()):
                relative = str(path.relative_to(self.repo_root))
                confined(self.repo_root, relative, protect=False)
                source_id = f"SOURCE-{index + 1}"
                sources.append({"source_id": source_id, "path": relative, "sha256": sha256_file(path)})
                standards.append({"standard_id": f"STD-{index + 1}", "description": path.read_text(),
                                  "source_refs": [source_id], "scope": ["*"]})
            checks = []
            test_files = list((self.repo_root / "tests").rglob("test_*.py"))
            if test_files:
                pytest_style = any(re.search(r"^def test_|^import pytest|^from pytest", p.read_text(), re.M) for p in test_files)
                argv = ["python3", "-m", "pytest", "tests"] if pytest_style else ["python3", "-m", "unittest", "discover", "-s", "tests"]
                checks.append({"check_id": "CHECK-TESTS", "argv": argv, "cwd": ".", "timeout_sec": 300, "kind": "test", "required": True})
            package = self.repo_root / "package.json"
            if package.exists():
                scripts = json.loads(package.read_text()).get("scripts", {})
                for script, kind in (("test", "test"), ("lint", "lint"), ("typecheck", "types")):
                    if script in scripts:
                        checks.append({"check_id": f"CHECK-NPM-{script.upper()}", "argv": ["npm", "run", script], "cwd": ".", "timeout_sec": 300, "kind": kind, "required": True})
            pyproject = self._read_optional(self.repo_root / "pyproject.toml", "")
            for marker, name, argv, kind in (
                ("[tool.ruff", "RUFF", ["python3", "-m", "ruff", "check", "."], "lint"),
                ("[tool.mypy", "MYPY", ["python3", "-m", "mypy", "."], "types"),
            ):
                if marker in pyproject:
                    checks.append({"check_id": f"CHECK-{name}", "argv": argv, "cwd": ".", "timeout_sec": 300, "kind": kind, "required": True})
            if not checks:
                raise PlanError("No project checks discovered. Configure .ssdlc/quality-contract.json before enabling task breakdown")
            contract = {"schema_version": "1.0", "contract_revision": 1, "base_commit": self.state["base_commit"],
                        "sources": sources, "standards": standards, "checks": checks, "baseline": [],
                        "limitations": ["Automatic discovery covers Python tests, configured Ruff/Mypy and npm test/lint/typecheck; provide an explicit contract for other tools."], "exceptions": []}
        validate_document("quality-contract", contract)
        baseline_dir = self.dir_recon / "baseline"
        baseline_dir.mkdir(exist_ok=True)
        for check in contract["checks"]:
            self.check_budget_circuit_breaker()
            cwd = confined(self.repo_root, check["cwd"])
            out, err, rc = EngineRunner.run_sandboxed_cmd(check["argv"], cwd, check["timeout_sec"])
            log = baseline_dir / f"{check['check_id']}.log"
            log.write_text(out + "\n" + err)
            contract["baseline"].append({"check_id": check["check_id"], "exit_code": rc,
                                         "log_ref": str(log.relative_to(self.ssdlc_root)), "sha256": sha256_file(log)})
            if rc or re.search(r"Ran 0 tests\b|\bno tests ran\b|\bcollected 0 items\b", out + err):
                raise PlanError(f"Baseline check {check['check_id']} failed or ran no tests; inspect {log}")
        atomic_json(target, contract)
        self.record_artifact("00-recon/quality-contract.json", target)
        return contract

    def _ensure_task_plan(self, review_repairs=None) -> Dict[str, Any]:
        self.state["task_breakdown"] = True
        self._save_state()
        quality = self._ensure_quality_contract()
        plan_path = self.dir_design / "plan.json"
        requirements = (self.dir_po / "requirements.md").read_text()
        validator = TaskPlanValidator(self.repo_root, requirements, quality, self.ssdlc_root)
        if plan_path.exists():
            meta = self.state.get("artifacts", {}).get("02-design/plan.json")
            if not meta or sha256_file(plan_path) != meta["sha256"]:
                raise PlanError("Task plan is unregistered or changed; explicit migration required")
            plan = json.loads(plan_path.read_text())
            if plan["ticket_id"] != self.ticket_id or plan["base_commit"] != self.state["base_commit"]:
                raise PlanError("Registered plan does not match this ticket and base commit")
            validator.validate(plan)
            return plan
        if "02-design/plan.json" in self.state.get("artifacts", {}):
            raise PlanError("Registered task plan disappeared; refusing regeneration")
        engine, model, effort, _ = SquadDispatcher.dispatch_role(
            "02-design-arch", self.state.get("risk_tier", "M"), self.state.get("dev_profile", "ALGORITHMIC")
        )
        context = {"requirements": requirements,
                   "requirement_catalogue": validator.requirement_catalogue,
                   "available_contract_refs": validator.available_contract_refs(),
                   "adr": self._read_optional(self.dir_design / "adr.md", "Tier S: lightweight plan"),
                   "threat_model": self._read_optional(self.dir_design / "threat-model.md", "See SEC requirements"),
                   "quality_contract": quality, "repo_context": self._gather_repo_context(),
                   "ticket_id": self.ticket_id, "base_commit": self.state["base_commit"]}
        schema = (Path(__file__).parent / "schemas" / "plan.schema.json").read_text()
        prompt = ("Produce only a JSON task plan matching this schema. Use exact literal file paths, cover EVERY AC and SEC, "
                  "and reference all required quality checks and applicable standards. Each requirement must map to executable checks. "
                  "Use kind=task and automated verification only: this bootstrap has no subgoal or review_only engine. "
                  "Preserve the supplied base_commit, ticket_id and quality_contract_ref. Do not reduce scope or security constraints.\n" + schema)
        prompt += ("\nEvery implementation task must include its new behavior tests in target_files and run them locally. "
                   "Do not defer all new tests to a final task. Preparatory tasks may have empty AC/SEC references "
                   "with local regression checks, but must still obey all applicable safety invariants. "
                   "Each claimed requirement must have an actual behavioral check, not merely an added label. "
                   "On repair, fix the reported defects without dropping requirements or weakening checks.")
        prompt += ("\nThe requirement_catalogue is authoritative: explicitly cover every required_id, including both "
                   "parent AC headings and defined sub-cases. Parent/child coverage is never implicit. "
                   "Put AC IDs only in ac_refs and SEC IDs only in sec_refs, for tasks AND checks. "
                   "Each check's references must be a subset of its owning task's corresponding field; "
                   "each task reference must have a local executable check. "
                   "References are exact and case-sensitive; prose mentions are not definitions. "
                   "Use contract_refs from available_contract_refs, relative to 02-design, e.g. adr.md#heading. "
                   "ADR document titles are not filenames. Never invent filenames or anchors.")
        rev_engine, rev_model, rev_effort, _ = SquadDispatcher.dispatch_role(
            "02-design-sec", self.state.get("risk_tier", "M"), self.state.get("dev_profile", "ALGORITHMIC")
        )
        if not model or not rev_model or rev_model.split("/")[-1] == model.split("/")[-1]:
            raise PlanError("Task plan needs an independent reviewer")
        def validate(plan):
            validator.validate(plan)
            if plan["ticket_id"] != self.ticket_id or plan["base_commit"] != self.state["base_commit"]:
                raise PlanError("Planner changed ticket identity or base_commit")
        def generate(attempt, inputs, previous):
            t0 = time.time()
            out, cost, tin, tout, trace = EngineRunner.invoke_role(
                engine, model, prompt, json.dumps({**context, "repair": previous} if previous else context, indent=2),
                self.repo_root, self.project_id, self.ticket_id,
                "02-design-task-plan", reasoning_effort=effort,
            )
            self.record_ledger(CostAndTelemetryEntry(
                f"02-design-task-plan-a{attempt}", "Task Planner", engine, model, "GENERATED",
                time.time() - t0, cost, tin, tout, trace, effort))
            return out
        def review(plan):
            t0 = time.time()
            out, cost, tin, tout, trace = EngineRunner.invoke_role(
                rev_engine, rev_model,
                "Review decomposition, missing scenarios and requirement/check coverage. Reject label-only coverage: "
                "checks must exercise the claimed behavior and implementation tasks must include their tests. "
                "End with exactly TASK_PLAN_STATUS: APPROVED or TASK_PLAN_STATUS: REVISION_REQUIRED.",
                json.dumps({"context": context, "plan": plan}), self.repo_root,
                self.project_id, self.ticket_id, "02-design-task-plan-review", reasoning_effort=rev_effort,
            )
            self.record_ledger(CostAndTelemetryEntry(
                "02-design-task-plan-review", "Plan Reviewer", rev_engine, rev_model, "REVIEWED",
                time.time() - t0, cost, tin, tout, trace, rev_effort))
            return out
        repair = PlanRepair(self.dir_design,
                            {"context": context, "schema": schema, "prompt": prompt,
                             "planner": [engine, model, effort], "reviewer": [rev_engine, rev_model, rev_effort]},
                            validate, generate, review, self.check_budget_circuit_breaker, review_repairs=review_repairs)
        try:
            previous_candidate = self.dir_design / "task-plan-candidate.txt"
            previous_meta = self.state.get('artifacts', {}).get('02-design/task-plan-candidate.txt')
            initial = None
            if not repair.path.exists() and previous_meta:
                if not previous_candidate.is_file() or file_hash(previous_candidate) != previous_meta['sha256']:
                    raise PlanError('Previous candidate evidence changed or disappeared')
                initial = previous_candidate.read_text()
            plan = repair.run(initial_candidate=initial)
        except PlanError as exc:
            self.record_gate("TASK_PLAN_GATE", False, str(exc))
            raise
        for path in sorted(repair.root.glob("*")):
            if path.is_file():
                self.record_artifact(str(path.relative_to(self.ssdlc_root)), path)
        atomic_json(plan_path, plan)
        self.record_artifact("02-design/plan.json", plan_path)
        self.record_gate("TASK_PLAN_GATE", True, "Validated plan and independent semantic review")
        return plan

    def resume_task_planning(self, review_repairs=None) -> Path:
        """Resume only planning against sealed upstream artifacts; never execute code."""
        with ticket_lock(self.ssdlc_root / "ticket.lock"):
            self.state = self._load_or_init_state()
            if not self._task_mode():
                raise PlanError("Planning resume requires task breakdown")
            if self.worktree_dir.exists() or (self.dir_dev / "tasks/task-journal.jsonl").exists():
                raise PlanError("Execution already started; explicit plan migration is required")
            gates = [g for g in self.state.get("gate_history", []) if g['gate'] == 'PAIR_DESIGN_GATE']
            if not gates or not gates[-1]['passed']:
                raise PlanError("Design has not been approved")
            required = ["00-recon/quality-contract.json", "01-po/requirements.md"]
            if self.state.get("risk_tier") != "S":
                required += ["02-design/adr.md", "02-design/threat-model.md"]
            for name in required:
                meta = self.state.get("artifacts", {}).get(name)
                path = self.ssdlc_root / name
                if not meta or not path.is_file() or file_hash(path) != meta['sha256']:
                    raise PlanError(f"Planning input is missing, unapproved or changed: {name}")
            self.state.pop("failure", None)
            self.state.update(current_phase="02-design", current_step="task-plan", status="IN_PROGRESS")
            self._save_state()
            try:
                self._ensure_task_plan(review_repairs=review_repairs)
            except Exception as exc:
                self.state.update(status="FAILED", failure={"phase": "task-plan", "message": str(exc)})
                self._save_state()
                raise
            self.state['status'] = 'PLAN_READY'
            self.state.pop('failure', None)
            self._save_state()
            return self.dir_design / 'plan.json'

    def _run_task_development(self, feedback: str = "") -> Tuple[Path, List[str]]:
        plan = self._ensure_task_plan()
        quality = json.loads((self.dir_recon / "quality-contract.json").read_text())
        wt = self._ensure_worktree()
        context = {name: self._read_optional(self.ssdlc_root / name) for name in (
            "01-po/requirements.md", "02-design/adr.md", "02-design/threat-model.md",
        )}
        validator = TaskPlanValidator(self.repo_root, context["01-po/requirements.md"], quality, self.ssdlc_root)
        ordered = validator.validate(plan)
        protected = {name: dict(meta) for name, meta in self.state.get("artifacts", {}).items()
                     if name.startswith(("00-recon/", "01-po/", "02-design/"))}
        def guard():
            validator.validate(plan)
            for name, meta in protected.items():
                path = self.ssdlc_root / meta["path"]
                if not path.is_file() or sha256_file(path) != meta["sha256"]:
                    raise TaskError(f"Protected artifact changed or disappeared: {name}")
        def develop(task, attempt, prompt):
            engine, model, effort, reason = SquadDispatcher.dispatch_role(
                "03-dev", self.state.get("risk_tier", "M"), task["profile"], attempt=attempt,
            )
            t0 = time.time()
            use_or = os.environ.get("SSDLC_DEV_USE_OPENROUTER", "false").lower() == "true"
            cost, tin, tout = 0.0, 0, 0
            if engine == "claude":
                tier_budget = {"S": 1.0, "M": 2.5, "L": 8.0}.get(self.state.get("risk_tier", "M"), 2.5)
                dev_budget = float(os.environ.get("SSDLC_DEV_BUDGET_USD") or tier_budget)
                out, rc, cost, tin, tout = EngineRunner.run_claude(
                    prompt, cwd=wt, read_only=False, use_openrouter=use_or,
                    writable_worktree=wt, model=model, reasoning_effort=effort,
                    max_budget_usd=dev_budget,
                )
            elif engine == "codex":
                out, rc = EngineRunner.run_codex(
                    prompt, cwd=wt, sandbox="workspace-write", model=model,
                    reasoning_effort=effort, use_openrouter=use_or, writable_worktree=wt,
                )
            else:
                raise TaskError(f"Unsupported bootstrap developer engine: {engine}")
            self.record_ledger(CostAndTelemetryEntry(
                f"03-dev-{task['task_id']}-a{attempt}", "Task Developer", engine, model,
                "GENERATED" if rc == 0 else f"RC_{rc}", time.time() - t0, cost, tin, tout,
                f"{self.project_id}:{self.ticket_id}:{task['task_id']}", effort,
            ))
            return {"output": out, "exit_code": rc, "engine": engine, "model": model, "effort": effort, "reason": reason}
        scheduler = TaskScheduler(
            wt, self.dir_dev / "tasks", plan, quality, context, develop,
            EngineRunner.run_sandboxed_cmd, guard, self.check_budget_circuit_breaker,
            max_attempts=int(os.environ.get("SSDLC_MAX_DEV_RETRIES", "3")),
        )
        self.state["current_phase"] = "03-dev"
        self._save_state()
        result = scheduler.run(ordered, feedback)
        self.state["tasks_status"] = result["tasks"]
        self.state["task_verified_head"] = result["expected_head"]
        diff = self.dir_dev / "implementation.diff"
        diff.write_text(git(wt, "diff", "--binary", self.state["base_commit"], "HEAD") + "\n")
        changed = git(wt, "diff", "--name-only", self.state["base_commit"], "HEAD").splitlines()
        specialists = set(self.state.get("specialists_triggered", []))
        for path in changed:
            lower = path.lower()
            if any(k in lower for k in ("migration", "schema", "prisma", "alembic", ".sql", "models.py")):
                specialists.add("DB_MIGRATION_AND_PRIVACY_SPECIALIST")
            if any(k in lower for k in ("package.json", "lock", "pyproject.toml", "requirements", "auth", "crypto", "jwt", "oauth")):
                specialists.add("APPSEC_SUPPLY_CHAIN_REDTEAM")
            if any(k in lower for k in ("async", "worker", "queue", "cache", "pool", "concurrent")):
                specialists.add("PERF_AND_CONCURRENCY_SRE")
        self.state["specialists_triggered"] = sorted(specialists)
        self._save_state()
        self.record_artifact("03-dev/implementation.diff", diff)
        self.record_artifact("03-dev/tasks-state.json", scheduler.journal.snapshot)
        self.record_artifact("03-dev/task-journal.jsonl", scheduler.journal.path)
        self.record_gate("TASK_EXECUTION_GATE", True, f"{len(ordered)} tasks locally verified; global review/QA still required")
        return diff, changed

    def _load_or_init_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        state = {
            "project_id": self.project_id,
            "ticket_id": self.ticket_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "current_phase": "INIT",
            "risk_tier": "M",
            "specialists_triggered": [],
            "status": "IN_PROGRESS",
            "goal": "",
            "base_commit": self._get_head_sha(self.repo_root),
            "dev_branch": f"ssdlc/{self.ticket_id}",
            "isolation": {
                "codex_home": self.iso_meta["codex_home"],
                "claude_home": self.iso_meta["claude_home"],
                "openrouter_key_fingerprint": fingerprint_secret(os.environ.get("OPENROUTER_API_KEY", "")),
                "openai_key_fingerprint": fingerprint_secret(os.environ.get("OPENAI_API_KEY", "")),
                "anthropic_key_fingerprint": fingerprint_secret(os.environ.get("ANTHROPIC_API_KEY", "")),
            },
            "cli_versions": {
                "agy": get_cli_version("agy"),
                "codex": get_cli_version("codex"),
                "claude": get_cli_version("claude"),
            },
            "ledger": [],
            "artifacts": {},
            "gate_history": [],
        }
        self._save_state(state)
        return state

    def _save_state(self, state: Optional[Dict[str, Any]] = None) -> None:
        if state is not None:
            self.state = state
        self.state["updated_at"] = utc_now()
        atomic_json(self.state_file, self.state)

    def record_artifact(self, rel_name: str, path: Path) -> None:
        if path.exists():
            self.state["artifacts"][rel_name] = {
                "path": str(path.relative_to(self.ssdlc_root)),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "recorded_at": utc_now(),
            }
            self._save_state()

    def record_ledger(self, entry: CostAndTelemetryEntry) -> None:
        self.state["ledger"].append(entry.to_dict())
        self._save_state()

    def record_gate(self, gate_name: str, passed: bool, details: str) -> None:
        self.state["gate_history"].append(
            {
                "timestamp": utc_now(),
                "gate": gate_name,
                "passed": passed,
                "details": details,
            }
        )
        self._save_state()

    @staticmethod
    def _get_head_sha(repo: Path) -> str:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "UNCOMMITTED"

    def _gather_repo_context(self) -> str:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        files = proc.stdout.strip().splitlines()[:250] if proc.returncode == 0 else []
        log_proc = subprocess.run(
            ["git", "log", "-n", "10", "--oneline"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        recent_commits = log_proc.stdout.strip() if log_proc.returncode == 0 else ""
        tree_str = "\n".join(files) if files else "(empty or non-git directory)"

        # Living Standards & Design System memory retrieval
        standards_ctx = []
        standards_dir = self.repo_root / ".ssdlc" / "standards"
        if standards_dir.is_dir():
            for std_file in sorted(standards_dir.glob("*.md")):
                try:
                    content = std_file.read_text(encoding="utf-8")
                    standards_ctx.append(f"\n--- {std_file.name} ---\n{content}\n")
                except Exception:
                    pass
        standards_str = "\n".join(standards_ctx) if standards_ctx else "(No .ssdlc/standards/ found)"

        return (
            f"Project ID: {self.project_id}\n"
            f"Repository Root: {self.repo_root}\n"
            f"Recent Commits:\n{recent_commits}\n\n"
            f"LIVING PROJECT STANDARDS & REUSABLE ASSETS (MANDATORY):\n{standards_str}\n\n"
            f"Tracked Files (up to 250):\n{tree_str}\n"
        )

    # ------------------------------------------------------------------
    # PHASE 0: Codebase Archaeologist / Recon & Risk-Tier Classification
    # ------------------------------------------------------------------
    def run_phase_recon(self, goal: str, requested_tier: str = "auto") -> Tuple[Path, str]:
        print(f"\n==> [{self.project_id} | Phase 0: 00-recon] Codebase Archaeologist mapping blast radius & Risk Tier...")
        self.state["goal"] = goal
        self.state["current_phase"] = "00-recon"
        self._save_state()

        engine, model, effort, reason = SquadDispatcher.dispatch_role("00-recon", "M", attempt=1)
        print(f"    [Squad Dispatcher] 00-recon allocated to {engine}:{model} (reasoning={effort}) — {reason}")
        repo_ctx = self._gather_repo_context()

        sys_prompt = (
            "You are the Codebase Archaeologist & Lead Recon Engineer in an SSDLC team.\n"
            "Analyze the repository structure and the user's goal to produce:\n"
            "1. ## Affected Modules & Blast Radius (which existing files/components are impacted)\n"
            "2. ## Existing Patterns & Reusable Utilities (auth, logging, error handling, test frameworks)\n"
            "3. ## Technical Constraints & Risks (DB migrations, concurrency, external APIs, secrets)\n"
            "4. ## SSDLC Risk Tier & Task Profile Classification:\n"
            "   Classify the change into one of:\n"
            "   - `RISK_TIER: S` (Low-risk bugfix, typo, minor UI/doc/refactor with no new deps or auth/DB changes)\n"
            "   - `RISK_TIER: M` (Standard feature or multi-file change)\n"
            "   - `RISK_TIER: L` (Security-critical: Auth, Crypto, Payments, DB Schema Migration, New External Dependencies, Public API surface)\n"
            "   And classify the implementation profile into one of:\n"
            "   - `DEV_PROFILE: FRONTEND_UI` (UI, layout, React Native, Expo, CSS, web client, components)\n"
            "   - `DEV_PROFILE: ALGORITHMIC` (Backend logic, concurrency, data structures, CLI, APIs)\n"
            "   - `DEV_PROFILE: FULLSTACK` (Fullstack integration, multi-file CRUD)\n"
            "   - `DEV_PROFILE: SECURITY` (Auth, crypto, permissions, sanitization)\n"
            "   - `DEV_PROFILE: REFACTOR` (Large multi-file restructuring)\n"
            "Ensure both `RISK_TIER: <S|M|L>` and `DEV_PROFILE: <PROFILE>` appear at the end."
        )
        user_prompt = f"REPO CONTEXT:\n{repo_ctx}\n\nGOAL:\n{goal}"

        t0 = time.time()
        out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
            engine,
            model,
            sys_prompt,
            user_prompt,
            self.repo_root,
            self.project_id,
            self.ticket_id,
            "00-recon",
            reasoning_effort=effort,
        )
        dur = time.time() - t0

        recon_file = self.dir_recon / "context-and-blast-radius.md"
        recon_file.write_text(out, encoding="utf-8")
        self.record_artifact("00-recon/context-and-blast-radius.md", recon_file)

        detected_tier = "M"
        m = re.search(r"RISK_TIER:\s*([SML])\b", out)
        if m:
            detected_tier = m.group(1)

        dev_profile = "ALGORITHMIC"
        mp = re.search(r"DEV_PROFILE:\s*(FRONTEND_UI|ALGORITHMIC|FULLSTACK|SECURITY|REFACTOR|DOCS)\b", out)
        if mp:
            dev_profile = mp.group(1)
        elif any(k in goal.lower() for k in ("ui", "ux", "layout", "carousel", "screen", "style", "css", "tailwind", "react native", "expo", "component")):
            dev_profile = "FRONTEND_UI"

        final_tier = detected_tier if requested_tier.lower() == "auto" else requested_tier.upper()
        self.state["risk_tier"] = final_tier
        self.state["dev_profile"] = dev_profile
        self._save_state()

        self.record_ledger(
            CostAndTelemetryEntry(
                "00-recon",
                "Codebase Recon",
                engine,
                model,
                f"TIER_{final_tier}/{dev_profile}",
                dur,
                cost,
                tin,
                tout,
                trace_user,
                reasoning_effort=effort,
            )
        )
        self.record_gate(
            "RECON_BLAST_RADIUS_GATE",
            True,
            f"Assigned Risk Tier: {final_tier} (detected={detected_tier}), Dev Profile: {dev_profile}",
        )
        print(f"    [OK] Blast radius mapped -> Tier: {final_tier}, Profile: {dev_profile} ({recon_file.relative_to(self.repo_root)})")
        return recon_file, final_tier

    # ------------------------------------------------------------------
    # PHASE 1: Product Owner (PO)
    # ------------------------------------------------------------------
    def run_phase_po(self, goal: str) -> Path:
        risk_tier = self.state.get("risk_tier", "M")
        dev_profile = self.state.get("dev_profile", "ALGORITHMIC")
        engine, model, effort, reason = SquadDispatcher.dispatch_role("01-po", risk_tier, dev_profile, attempt=1)
        print(f"\n==> [{self.project_id} | Phase 1: 01-po] Product Owner defining Stories & ACs ({engine}:{model}, reasoning={effort})...")
        print(f"    [Squad Dispatcher] 01-po allocated — {reason}")
        self.state["current_phase"] = "01-po"
        self._save_state()

        recon_text = (self.dir_recon / "context-and-blast-radius.md").read_text(encoding="utf-8")

        ui_nfr_prompt = ""
        if dev_profile == "FRONTEND_UI" and not self._task_mode():
            ui_nfr_prompt = (
                "\n\n### MANDATORY FRONTEND / MOBILE NON-FUNCTIONAL REQUIREMENTS:\n"
                "- NFR-DESIGN-SYSTEM: Every UI element MUST strictly use the living design tokens from DESIGN_SYSTEM.md (space.*, shape.*, type.*, useTheme()). Zero hardcoded hex colors or arbitrary spacing numbers permitted.\n"
                "- NFR-REUSE-FIRST: Explicitly mandate reusing existing primitives from COMPONENT_CATALOG.md (<Button>, <Card>, <Loading>, <SecureImage>) instead of inventing bespoke widgets.\n"
                "- NFR-SAFE-AREA: Explicit handling of notch, Dynamic Island, status bar, and home indicator insets (useSafeAreaInsets or container).\n"
                "- NFR-4-STATES: Every interactive component or screen must define 4 states: Loading (skeleton/spinner with max 5s timeout), Empty, Error (with retry trigger), and Success/Active.\n"
                "- NFR-OFFLINE-AUTH: Behavior when guest or network is offline.\n"
                "- NFR-ASSETS: Explicit requirement for vector/SVG icons (e.g. @expo/vector-icons or SVG component); raw Unicode glyphs or emoji as icons are STRICTLY PROHIBITED.\n"
            )

        sys_prompt = (
            "You are the Product Owner (PO) in a strict Secure Software Development Lifecycle (SSDLC).\n"
            "Using the Codebase Recon report and user goal, produce a rigorous Markdown specification:\n"
            "1. ## Overview & Business Goal\n"
            "2. ## User Stories\n"
            "3. ## Testable Acceptance Criteria (numbered AC-1, AC-2, ... with Given/When/Then)\n"
            "4. ## Security Requirements & Abuse Cases (numbered SEC-1, SEC-2, ...)\n"
            "5. ## Non-Functional & Observability Requirements (logging, metrics, rollback expectations)\n"
            f"{ui_nfr_prompt}"
            "Be concrete, testable, and unambiguous."
        )
        user_prompt = f"CODEBASE RECON & BLAST RADIUS:\n{recon_text}\n\nUSER GOAL:\n{goal}"

        t0 = time.time()
        out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
            engine,
            model,
            sys_prompt,
            user_prompt,
            self.repo_root,
            self.project_id,
            self.ticket_id,
            "01-po",
            reasoning_effort=effort,
        )
        dur = time.time() - t0

        req_file = self.dir_po / "requirements.md"
        req_file.write_text(out, encoding="utf-8")
        self.record_artifact("01-po/requirements.md", req_file)
        self.record_ledger(
            CostAndTelemetryEntry(
                "01-po",
                "Product Owner",
                engine,
                model,
                "PASS",
                dur,
                cost,
                tin,
                tout,
                trace_user,
                reasoning_effort=effort,
            )
        )

        has_ac = bool(re.search(r"AC-\d+", out))
        has_sec = bool(re.search(r"SEC-\d+", out))
        if not (has_ac and has_sec):
            self.record_gate("PO_SPEC_GATE", False, "Missing numbered AC-x or SEC-x items")
            raise RuntimeError("PO Gate Failed: requirements.md must define AC-1..N and SEC-1..N.")
        self.record_gate("PO_SPEC_GATE", True, "Verified numbered ACs and Security Abuse Cases.")
        print(f"    [OK] Wrote {req_file.relative_to(self.repo_root)}")

        if self.remote_sync.enabled:
            issue_num, issue_url = self.remote_sync.sync_issue(
                goal, out, existing_issue_id=self.issue_id
            )
            if issue_num:
                self.issue_id = issue_num
                self.state["issue_id"] = issue_num
                self.state["issue_url"] = issue_url
                self._save_state()

        return req_file

    # ------------------------------------------------------------------
    # PHASE 2: Pair Design & STRIDE Threat Modeling
    # ------------------------------------------------------------------
    def run_phase_pair_design(self) -> Tuple[Path, Path]:
        print(f"\n==> [{self.project_id} | Phase 2: 02-design] Pair Design (Architect <-> Security Threat Modeler)...")
        self.state["current_phase"] = "02-design"
        self._save_state()

        recon_text = (self.dir_recon / "context-and-blast-radius.md").read_text(encoding="utf-8")
        req_text = (self.dir_po / "requirements.md").read_text(encoding="utf-8")

        risk_tier = self.state.get("risk_tier", "M")
        dev_profile = self.state.get("dev_profile", "ALGORITHMIC")
        arch_engine, arch_model, _, arch_reason = SquadDispatcher.dispatch_role("02-design-arch", risk_tier, dev_profile)
        sec_engine, sec_model, _, sec_reason = SquadDispatcher.dispatch_role("02-design-sec", risk_tier, dev_profile)
        print(f"    [Squad Dispatcher] Architect: {arch_engine}:{arch_model} ({arch_reason})")
        print(f"    [Squad Dispatcher] Security Reviewer: {sec_engine}:{sec_model} ({sec_reason})")
        max_rounds = int(os.environ.get("SSDLC_MAX_DESIGN_ROUNDS", "3"))

        dialogue_log: List[str] = ["# Pair Design & STRIDE Threat Modeling Transcript\n"]
        round_directory = self.dir_design / "rounds" / uuid.uuid4().hex
        round_directory.mkdir(parents=True, exist_ok=True)
        current_adr = ""
        current_threat_model = ""
        approved = False
        for rnd in range(1, max_rounds + 1):
            arch_effort = SquadDispatcher.resolve_reasoning_effort("02-design-arch", risk_tier, attempt=rnd)
            sec_effort = SquadDispatcher.resolve_reasoning_effort("02-design-sec", risk_tier, attempt=rnd)
            print(f"    -> Round {rnd}/{max_rounds}: Architect ({arch_engine}:{arch_model}, reasoning={arch_effort}) proposing ADR...")
            ui_arch_rules = ""
            if dev_profile == "FRONTEND_UI" and not self._task_mode():
                ui_arch_rules = (
                    "\nUI/UX ARCHITECTURE & DESIGN SYSTEM RULES:\n"
                    "- ZERO DRIFT: Specify exact Design System tokens (import { space, shape, type, useTheme } from '@/theme/tokens').\n"
                    "- REUSE MANDATE: Check COMPONENT_CATALOG.md and reuse existing primitives (<Button>, <Card>, <Loading>, <SecureImage>) rather than designing duplicates.\n"
                    "- Ensure Safe Area Insets are factored into all container boundaries via useSafeAreaInsets.\n"
                    "- Enforce vector/SVG icons (@expo/vector-icons or custom SVG) and explicitly forbid raw Unicode glyphs.\n"
                )
            arch_sys = (
                "You are the Lead Systems Architect in a Pair Design session.\n"
                "Draft or revise a complete Architecture Decision Record (ADR) that satisfies all PO Acceptance "
                "Criteria (AC-*) and Security Requirements (SEC-*), and resolves any critiques from the Security Reviewer.\n"
                "Include: Exact file paths to create/modify, interfaces, DB/state impact, error handling, and test strategy."
                f"{ui_arch_rules}"
            )
            arch_user = (
                f"CODEBASE RECON:\n{recon_text}\n\n"
                f"PO REQUIREMENTS:\n{req_text}\n\n"
                f"PREVIOUS ADR DRAFT:\n{current_adr or '(None - Round 1)'}\n\n"
                f"SECURITY REVIEWER CRITIQUE:\n{current_threat_model or '(None - Round 1)'}"
            )
            t0 = time.time()
            current_adr, cost, tin, tout, trace_user = EngineRunner.invoke_role(
                arch_engine,
                arch_model,
                arch_sys,
                arch_user,
                self.repo_root,
                self.project_id,
                self.ticket_id,
                f"02-design-arch-r{rnd}",
                reasoning_effort=arch_effort,
            )
            round_adr = round_directory / f"adr-round-{rnd}.md"
            round_adr.write_text(current_adr, encoding="utf-8")
            self.record_artifact(str(round_adr.relative_to(self.ssdlc_root)), round_adr)
            dur = time.time() - t0
            self.record_ledger(
                CostAndTelemetryEntry(
                    f"02-design-r{rnd}",
                    "Architect",
                    arch_engine,
                    arch_model,
                    "DRAFT",
                    dur,
                    cost,
                    tin,
                    tout,
                    trace_user,
                    reasoning_effort=arch_effort,
                )
            )
            dialogue_log.append(f"## Round {rnd} — Architect ({arch_model}, reasoning={arch_effort})\n\n{current_adr}\n\n---\n")

            print(f"    -> Round {rnd}/{max_rounds}: Security Critic ({sec_engine}:{sec_model}, reasoning={sec_effort}) running STRIDE audit...")
            sec_sys = (
                "You are a Staff Adversarial Security & Systems Reviewer.\n"
                "Critique the Architect's ADR against the PO Requirements using STRIDE "
                "(Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege).\n"
                "Also check for race conditions, broken invariants, and over-engineering.\n"
                "At the very end of your response, output one of these exact lines:\n"
                "DESIGN_STATUS: APPROVED (if all critical/major design & security flaws are addressed)\n"
                "or\n"
                "DESIGN_STATUS: REVISION_REQUIRED (if blocking flaws remain)"
            )
            sec_user = (
                f"PO REQUIREMENTS:\n{req_text}\n\n"
                f"PROPOSED ADR (Round {rnd}):\n{current_adr}"
            )
            t0 = time.time()
            current_threat_model, cost, tin, tout, trace_user = EngineRunner.invoke_role(
                sec_engine,
                sec_model,
                sec_sys,
                sec_user,
                self.repo_root,
                self.project_id,
                self.ticket_id,
                f"02-design-sec-r{rnd}",
                reasoning_effort=sec_effort,
            )
            round_review = round_directory / f"security-review-round-{rnd}.md"
            round_review.write_text(current_threat_model, encoding="utf-8")
            self.record_artifact(str(round_review.relative_to(self.ssdlc_root)), round_review)
            dur = time.time() - t0
            status_tag = (
                "APPROVED"
                if "DESIGN_STATUS: APPROVED" in current_threat_model
                else "REVISION_REQUIRED"
            )
            self.record_ledger(
                CostAndTelemetryEntry(
                    f"02-design-r{rnd}",
                    "Security Threat Modeler",
                    sec_engine,
                    sec_model,
                    status_tag,
                    dur,
                    cost,
                    tin,
                    tout,
                    trace_user,
                    reasoning_effort=sec_effort,
                )
            )
            dialogue_log.append(
                f"## Round {rnd} — Security & Systems Reviewer ({sec_model}, reasoning={sec_effort})\n\n{current_threat_model}\n\n---\n"
            )

            if status_tag == "APPROVED":
                approved = True
                print(f"    [OK] Pair Design converged and APPROVED in round {rnd}.")
                break
            decision = f"next round {rnd + 1}/{max_rounds}" if rnd < max_rounds else "attempt limit exhausted; no next round"
            print(f"[02-design] Round {rnd}/{max_rounds}: REVISION_REQUIRED (explicit approval not received); {decision}.", flush=True)
            log_retry_reason("02-design", current_threat_model, round_review)

        adr_path = self.dir_design / "adr.md"
        tm_path = self.dir_design / "threat-model.md"
        dlg_path = self.dir_design / "pair-dialogue.md"

        adr_path.write_text(current_adr, encoding="utf-8")
        tm_path.write_text(current_threat_model, encoding="utf-8")
        dlg_path.write_text("\n".join(dialogue_log), encoding="utf-8")

        self.record_artifact("02-design/adr.md", adr_path)
        self.record_artifact("02-design/threat-model.md", tm_path)
        self.record_artifact("02-design/pair-dialogue.md", dlg_path)
        self.record_gate(
            "PAIR_DESIGN_GATE",
            approved,
            f"Completed in {rnd} round(s) (explicit approval={approved}).",
        )
        if not approved:
            raise RuntimeError("Pair design did not receive explicit approval")
        return adr_path, tm_path

    # ------------------------------------------------------------------
    # PHASE 3: Developer (Isolated Git Worktree)
    # ------------------------------------------------------------------
    def _ensure_worktree(self) -> Path:
        branch = self.state["dev_branch"]
        if self.worktree_dir.exists() and (self.worktree_dir / ".git").exists():
            self.isolation.ensure_trusted_path(self.worktree_dir)
            return self.worktree_dir
        if self.worktree_dir.exists():
            shutil.rmtree(self.worktree_dir)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self.repo_root),
            capture_output=True,
        )
        chk = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(self.repo_root),
            capture_output=True,
        )
        if chk.returncode == 0:
            cmd = ["git", "worktree", "add", str(self.worktree_dir), branch]
        else:
            cmd = ["git", "worktree", "add", "-b", branch, str(self.worktree_dir), self.state["base_commit"]]
        proc = subprocess.run(cmd, cwd=str(self.repo_root), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create git worktree: {proc.stderr}")
        self.isolation.ensure_trusted_path(self.worktree_dir)
        return self.worktree_dir

    def _read_optional(self, path: Path, fallback: str = "(Skipped in Tier S)") -> str:
        return path.read_text(encoding="utf-8") if path.exists() else fallback

    def run_phase_dev(self, feedback: str = "", attempt: int = 1) -> Tuple[Path, List[str]]:
        if self._task_mode():
            return self._run_task_development(feedback)
        return self._run_phase_dev_legacy(feedback, attempt)

    def _run_phase_dev_legacy(self, feedback: str = "", attempt: int = 1) -> Tuple[Path, List[str]]:
        risk_tier = self.state.get("risk_tier", "M")
        dev_profile = self.state.get("dev_profile", "ALGORITHMIC")
        dev_engine, dev_model, dev_effort, dev_reason = SquadDispatcher.dispatch_role(
            "03-dev", risk_tier, dev_profile, attempt=attempt
        )
        print(
            f"\n==> [{self.project_id} | Phase 3: 03-dev] Developer ({dev_engine}:{dev_model or 'default'}, "
            f"reasoning={dev_effort}, profile={dev_profile}) in isolated worktree (Attempt {attempt})..."
        )
        print(f"    [Squad Dispatcher] 03-dev allocated — {dev_reason}")
        self.state["current_phase"] = "03-dev"
        self._save_state()

        wt = self._ensure_worktree()
        recon_text = self._read_optional(self.dir_recon / "context-and-blast-radius.md")
        req_text = self._read_optional(self.dir_po / "requirements.md")
        adr_text = self._read_optional(self.dir_design / "adr.md")
        tm_text = self._read_optional(self.dir_design / "threat-model.md")

        use_or = os.environ.get("SSDLC_DEV_USE_OPENROUTER", "false").lower() == "true"

        ui_dev_rules = ""
        if dev_profile == "FRONTEND_UI":
            ui_dev_rules = (
                "\n\nMANDATORY FRONTEND / UI/UX IMPLEMENTATION RULES:\n"
                "- ZERO TOKEN DRIFT: Import space, shape, type, useTheme from `@/theme/tokens`. Never use hardcoded hex colors or arbitrary spacing numbers.\n"
                "- REUSE COMPONENT CATALOG: Reuse primitives from `@/components/Primitives` (<Button>, <Card>, <Loading>) before inventing custom widgets.\n"
                "- Do NOT use raw Unicode characters or emojis (e.g. '♥', '★', arrows) as icons. Always use vector icons (e.g. from `@expo/vector-icons` or SVG).\n"
                "- Always use `useSafeAreaInsets` or safe area containers for top/bottom boundaries to prevent notch and status bar collisions.\n"
                "- Implement all 4 component states: Loading (skeleton/spinner with max 5s timeout), Empty, Error (with retry trigger), and Success.\n"
            )

        prompt = (
            "You are the Developer in an SSDLC pipeline working inside an isolated Git worktree.\n"
            "Implement the code and unit tests required by the PO Requirements and the approved ADR + Threat Model.\n"
            "STRICT RULES:\n"
            "- Do NOT modify any files inside `.ssdlc/` or `.agents/`.\n"
            "- Write clean, production-grade code and unit tests.\n\n"
            f"CODEBASE RECON:\n{recon_text}\n\n"
            f"PO REQUIREMENTS:\n{req_text}\n\n"
            f"APPROVED ARCHITECTURE (ADR):\n{adr_text}\n\n"
            f"THREAT MODEL MITIGATIONS REQUIRED:\n{tm_text}\n"
            f"{ui_dev_rules}"
        )
        if feedback:
            prompt += f"\n\nCRITICAL FEEDBACK FROM PREVIOUS REVIEW/QA/APPSEC GATE (MUST FIX):\n{feedback}\n"

        t0 = time.time()
        in_container = Path("/.dockerenv").exists() or os.environ.get("SSDLC_EXECUTION_MODE", "docker") == "docker"
        dev_sandbox = "danger-full-access" if in_container else "workspace-write"
        tier_dev_budgets = {"S": 1.0, "M": 2.5, "L": 8.0}
        dev_budget_env = os.environ.get("SSDLC_DEV_BUDGET_USD", "").strip()
        dev_budget = float(dev_budget_env) if dev_budget_env else tier_dev_budgets.get(risk_tier, 2.5)
        if dev_engine == "claude":
            out, rc, dev_cost, dev_tin, dev_tout = EngineRunner.run_claude(
                prompt,
                cwd=wt,
                read_only=False,
                use_openrouter=use_or,
                writable_worktree=wt,
                model=dev_model,
                reasoning_effort=dev_effort,
                max_budget_usd=dev_budget,
            )
        else:
            out, rc = EngineRunner.run_codex(
                prompt,
                cwd=wt,
                sandbox=dev_sandbox,
                model=dev_model,
                reasoning_effort=dev_effort,
                use_openrouter=use_or,
                writable_worktree=wt,
            )
            dev_cost, dev_tin, dev_tout = 0.0, 0, 0
        dur = time.time() - t0

        # Cryptographic SHA-256 integrity check on upstream .ssdlc/ specs & threat model
        for protected_rel in ("01-po/requirements.md", "02-design/adr.md", "02-design/threat-model.md"):
            meta = self.state.get("artifacts", {}).get(protected_rel)
            if meta:
                protected_file = self.ssdlc_root / meta["path"]
                if protected_file.exists() and sha256_file(protected_file) != meta["sha256"]:
                    self.record_gate("DEV_TAMPER_CHECK", False, f"SHA-256 mismatch on {protected_rel}")
                    raise RuntimeError(
                        f"Security Violation: Upstream SSDLC artifact {protected_rel} was modified during Phase 3 Dev!"
                    )

        log_path = self.dir_dev / f"dev-attempt-{attempt}.log"
        log_path.write_text(out, encoding="utf-8")
        self.record_artifact(f"03-dev/dev-attempt-{attempt}.log", log_path)

        subprocess.run(["git", "add", "-A"], cwd=str(wt), capture_output=True)
        tamper_chk = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(wt),
            capture_output=True,
            text=True,
        )
        changed_files = [
            f.strip() for f in tamper_chk.stdout.splitlines() if f.strip()
        ]
        for cf in changed_files:
            if cf.startswith(".ssdlc/") or cf.startswith(".agents/"):
                subprocess.run(["git", "reset", "HEAD", cf], cwd=str(wt), capture_output=True)
                subprocess.run(["git", "checkout", "--", cf], cwd=str(wt), capture_output=True)
                self.record_gate("DEV_TAMPER_CHECK", False, f"Dev attempted to touch {cf}")
                raise RuntimeError(f"Security Violation: Dev agent attempted to modify {cf}")

        diff_proc = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=str(wt),
            capture_output=True,
            text=True,
        )
        diff_text = diff_proc.stdout
        diff_path = self.dir_dev / "implementation.diff"
        diff_path.write_text(diff_text, encoding="utf-8")
        self.record_artifact("03-dev/implementation.diff", diff_path)

        specialists = set(self.state.get("specialists_triggered", []))
        for cf in changed_files:
            lower_cf = cf.lower()
            if any(k in lower_cf for k in ("migration", "schema", "prisma", "alembic", ".sql", "models.py")):
                specialists.add("DB_MIGRATION_AND_PRIVACY_SPECIALIST")
            if any(k in lower_cf for k in ("package.json", "package-lock.json", "pyproject.toml", "requirements", "cargo.toml", "go.mod", "auth", "crypto", "jwt", "oauth")):
                specialists.add("APPSEC_SUPPLY_CHAIN_REDTEAM")
            if any(k in lower_cf for k in ("async", "worker", "queue", "cache", "pool", "concurrent")):
                specialists.add("PERF_AND_CONCURRENCY_SRE")
        self.state["specialists_triggered"] = sorted(specialists)
        self._save_state()

        self.record_ledger(
            CostAndTelemetryEntry(
                f"03-dev-a{attempt}",
                "Developer",
                dev_engine,
                dev_model or "default",
                "COMPLETED" if rc == 0 else f"RC_{rc}",
                dur,
                cost_usd=dev_cost,
                tokens_in=dev_tin,
                tokens_out=dev_tout,
                trace_user=f"{self.project_id}:{self.ticket_id}:03-dev-a{attempt}",
                reasoning_effort=dev_effort,
            )
        )
        self.record_gate(
            f"DEV_IMPLEMENTATION_A{attempt}",
            bool(diff_text.strip()),
            f"Changed {len(changed_files)} file(s); engine={dev_engine}, reasoning={dev_effort}; specialists={self.state['specialists_triggered']}",
        )
        print(f"    [OK] Captured diff ({len(changed_files)} files) -> {diff_path.relative_to(self.repo_root)}")
        return diff_path, changed_files

    # ------------------------------------------------------------------
    # PHASE 4: Code Reviewer (+ Conditional DB & Perf/SRE Specialist Lenses)
    # ------------------------------------------------------------------
    def run_phase_review(self, attempt: int = 1) -> Tuple[bool, str]:
        risk_tier = self.state.get("risk_tier", "M")
        dev_profile = self.state.get("dev_profile", "ALGORITHMIC")
        review_models, rev_effort = SquadDispatcher.resolve_review_models(risk_tier, attempt=attempt)
        print(
            f"\n==> [{self.project_id} | Phase 4: 04-review] Multi-Model Code Reviewer "
            f"({len(review_models)} reviewer(s), reasoning={rev_effort})..."
        )
        print(f"    [Squad Dispatcher] Review panel: {review_models}")
        self.state["current_phase"] = "04-review"
        self._save_state()

        req_text = self._read_optional(self.dir_po / "requirements.md")
        adr_text = self._read_optional(self.dir_design / "adr.md")
        tm_text = self._read_optional(self.dir_design / "threat-model.md")
        diff_text = self._read_optional(self.dir_dev / "implementation.diff", "")

        if not diff_text.strip():
            msg = "REVIEW_STATUS: CHANGES_REQUESTED\nNo code changes were produced in implementation.diff."
            return False, msg

        specialists = self.state.get("specialists_triggered", [])
        specialist_instructions = ""
        if "DB_MIGRATION_AND_PRIVACY_SPECIALIST" in specialists:
            specialist_instructions += (
                "\n- **DB & Data Privacy Specialist Lens ACTIVE**: Verify zero-downtime backward-compatible "
                "migrations, index coverage, lock contention, and PII encryption/retention."
            )
        if "PERF_AND_CONCURRENCY_SRE" in specialists:
            specialist_instructions += (
                "\n- **Performance & Concurrency SRE Lens ACTIVE**: Verify absence of N+1 queries, race conditions, "
                "unbounded memory/queues, missing timeouts, and non-idempotent retries."
            )

        ui_review_rules = ""
        if dev_profile == "FRONTEND_UI":
            ui_review_rules = (
                "\n\n### MANDATORY FRONTEND / UI/UX & LIVING DESIGN SYSTEM AUDIT:\n"
                "- Check every UI Acceptance Criterion (AC-*) in requirements.md 1-by-1.\n"
                "- ZERO TOKEN DRIFT: Verify all styling references theme.color.*, space.*, and shape.*. If the diff contains hardcoded hex colors (#...) or arbitrary spacing numbers (e.g. padding: 13, margin: 7), YOU MUST REJECT WITH CHANGES_REQUESTED.\n"
                "- REUSE VERIFICATION: Verify that existing primitives from COMPONENT_CATALOG.md (<Button>, <Card>, <Loading>) are imported instead of duplicating basic UI widgets.\n"
                "- Safe Area Insets: Verify safe area insets (useSafeAreaInsets or platform container) are used; reject hardcoded status bar paddings.\n"
                "- Visual Assets: If the specification requires SVG or vector icons, verify vector imports. If the code uses raw Unicode strings (like emojis, arrows, symbols) as icon substitutes, YOU MUST REJECT WITH CHANGES_REQUESTED.\n"
                "- Component States: Verify Loading, Empty, and Error states are implemented.\n"
            )

        if self._task_mode():
            if len(diff_text) > 60000:
                raise TaskError("Bootstrap review cannot truncate the cumulative diff; split this ticket")
            ui_review_rules = (
                "\nPROJECT QUALITY CONTRACT (apply the actual project rules, including test/config changes):\n"
                + (self.dir_recon / "quality-contract.json").read_text()
                + "\nCheck missing scenarios and task interactions before delivery.\n"
            )
        review_engine = os.environ.get("SSDLC_REVIEW_ENGINE", "openrouter")

        sys_prompt = (
            "You are a Principal Code Reviewer in an SSDLC pipeline.\n"
            "Audit the Git Diff strictly against:\n"
            "1. The agreed Architecture Decision Record (ADR) — check for design drift.\n"
            "2. The Threat Model — verify every required security mitigation is implemented.\n"
            "3. Code quality — race conditions, error handling, resource leaks, maintainability.\n"
            "4. Edge cases & boundary conditions — verify relevant input validation (null, empty, limits, invalid formats), "
            "corner cases, unhandled errors, and unexpected failure modes. Include crashes, retries, interrupted execution "
            "and recovery, and concurrency where applicable.\n"
            f"{specialist_instructions}\n"
            f"{ui_review_rules}\n"
            "Output a structured Markdown review with:\n"
            "- ### ADR Conformance Checklist\n"
            "- ### Threat Model Mitigation Checklist\n"
            "- ### Edge Cases & Boundary Conditions Analysis\n"
            "  For each relevant case, identify the concrete scenario, expected behavior, implementation handling "
            "(with file/symbol references), and the test that demonstrates it or a specific test gap. "
            "Distinguish observed evidence from assumptions; do not claim tests ran without execution evidence. "
            "Explain when a category is not applicable instead of filling the section with a generic OK.\n"
            "- ### Specialist Lens Audit (DB / Perf / SRE / UI if active)\n"
            "- ### Findings (Critical / Major / Minor)\n"
            "At the very end, output EXACTLY one of:\n"
            "REVIEW_STATUS: APPROVED\n"
            "REVIEW_STATUS: CHANGES_REQUESTED\n"
            "Use APPROVED only when there are zero Critical or Major defects; otherwise use CHANGES_REQUESTED. "
            "Emit the verdict marker once only, as the final standalone line, with no annotations or code fence."
        )
        user_prompt = (
            f"PO REQUIREMENTS:\n{req_text}\n\n"
            f"ADR:\n{adr_text}\n\n"
            f"THREAT MODEL:\n{tm_text}\n\n"
            f"GIT DIFF TO REVIEW:\n```diff\n{diff_text[:60000]}\n```"
        )

        combined_reviews: List[str] = [f"# Multi-Model Code Review Report (Attempt {attempt}, reasoning={rev_effort})\n"]
        all_approved = True

        for mdl in review_models:
            if ":" in mdl and not mdl.startswith("http"):
                effective_engine, effective_model = mdl.split(":", 1)
            else:
                effective_engine = review_engine
                effective_model = mdl
            print(f"    -> Running Reviewer: {effective_engine}:{effective_model} (reasoning={rev_effort})...")
            t0 = time.time()
            rev_out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
                effective_engine,
                effective_model,
                sys_prompt,
                user_prompt,
                self.repo_root,
                self.project_id,
                self.ticket_id,
                f"04-review-a{attempt}",
                sandbox="read-only",
                reasoning_effort=rev_effort,
            )
            dur = time.time() - t0
            verdict = parse_review_verdict(rev_out)
            passed = verdict == "APPROVED"
            if not passed:
                all_approved = False
            self.record_ledger(
                CostAndTelemetryEntry(
                    f"04-review-a{attempt}",
                    "Code Reviewer",
                    effective_engine,
                    effective_model,
                    verdict or "INVALID_VERDICT",
                    dur,
                    cost,
                    tin,
                    tout,
                    trace_user,
                    reasoning_effort=rev_effort,
                )
            )
            verdict_note = "Invalid or ambiguous final verdict; approval withheld.\n\n" if verdict is None else ""
            combined_reviews.append(f"## Reviewer: `{effective_engine}:{effective_model}` ({verdict or 'INVALID_VERDICT'}, reasoning={rev_effort})\n\n{verdict_note}{rev_out}\n\n---\n")

        report_text = "\n".join(combined_reviews)
        rev_path = self.dir_review / f"review-report-a{attempt}.md"
        rev_path.write_text(report_text, encoding="utf-8")
        canon_path = self.dir_review / "review-report.md"
        canon_path.write_text(report_text, encoding="utf-8")

        self.record_artifact(f"04-review/review-report-a{attempt}.md", rev_path)
        self.record_artifact("04-review/review-report.md", canon_path)
        self.record_gate(
            f"CODE_REVIEW_A{attempt}",
            all_approved,
            f"Reviewed by {len(review_models)} model(s); approved={all_approved}",
        )
        print(f"    [{'OK' if all_approved else 'BOUNCE'}] Code Review status: {'APPROVED' if all_approved else 'CHANGES_REQUESTED'}")
        return all_approved, report_text

    # ------------------------------------------------------------------
    # PHASE 5: QA & Security Verification
    # ------------------------------------------------------------------
    def _run_deterministic_scanners(self, wt: Path, test_cmd: Optional[str]) -> Tuple[bool, str]:
        logs: List[str] = ["# Deterministic QA & Security Execution Logs\n"]
        overall_ok = True

        diff_text = self._read_optional(self.dir_dev / "implementation.diff", "")
        secret_patterns = [
            (r"sk-[a-zA-Z0-9]{20,}", "OpenAI/Anthropic/OpenRouter Secret Key"),
            (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
            (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "Private Key"),
            (r"ghp_[a-zA-Z0-9]{36}", "GitHub Personal Access Token"),
        ]
        leaked = []
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                for pat, label in secret_patterns:
                    if re.search(pat, line):
                        leaked.append(label)
        if leaked:
            overall_ok = False
            logs.append(f"## [FAIL] Secret Scanner\nDetected potential hardcoded secrets in diff: { sorted(set(leaked)) }\n")
        else:
            logs.append("## [PASS] Secret Scanner\nZero hardcoded secret patterns found in diff.\n")

        if shutil.which("gitleaks") or EngineRunner._should_spawn_role_container():
            gl_out, gl_err, gl_rc = EngineRunner.run_sandboxed_cmd(
                ["gitleaks", "detect", "--source", str(wt), "--no-git", "-v"],
                cwd=wt,
                timeout=120,
            )
            logs.append(f"### gitleaks exit code: {gl_rc}\n")
            if gl_rc != 0:
                overall_ok = False
                logs.append(f"```\n{gl_out}\n{gl_err}\n```\n")

        effective_test_cmd = test_cmd
        if not effective_test_cmd:
            if (wt / "pytest.ini").exists() or (wt / "pyproject.toml").exists() or list(wt.glob("test_*.py")) or list(wt.glob("tests/test_*.py")):
                effective_test_cmd = "python3 -m unittest discover"
                if shutil.which("pytest") or EngineRunner._should_spawn_role_container():
                    effective_test_cmd = "pytest"
            elif (wt / "package.json").exists():
                effective_test_cmd = "npm test"

        if effective_test_cmd:
            print(f"    -> Running deterministic test suite in isolated sandbox: `{effective_test_cmd}`...")
            t_out, t_err, t_rc = EngineRunner.run_sandboxed_cmd(
                ["bash", "-c", effective_test_cmd],
                cwd=wt,
                timeout=300,
            )
            test_passed = t_rc == 0
            if not test_passed:
                overall_ok = False
            logs.append(
                f"## [{'PASS' if test_passed else 'FAIL'}] Test Suite (`{effective_test_cmd}` — exit code {t_rc})\n"
                f"```text\n{t_out + t_err}\n```\n"
            )
        else:
            logs.append("## [INFO] Test Runner\nNo custom `--test-cmd` provided or auto-detected.\n")

        return overall_ok, "\n".join(logs)

    def run_phase_qa(self, test_cmd: Optional[str] = None, attempt: int = 1) -> Tuple[bool, str]:
        risk_tier = self.state.get("risk_tier", "M")
        dev_profile = self.state.get("dev_profile", "ALGORITHMIC")
        qa_engine, qa_model, qa_effort, qa_reason = SquadDispatcher.dispatch_role(
            "05-qa", risk_tier, dev_profile, attempt=attempt
        )
        print(f"\n==> [{self.project_id} | Phase 5: 05-qa] QA & Verifier ({qa_engine}:{qa_model}, reasoning={qa_effort}) running checks + AC Matrix...")
        print(f"    [Squad Dispatcher] 05-qa allocated — {qa_reason}")
        self.state["current_phase"] = "05-qa"
        self._save_state()

        wt = self._ensure_worktree()
        det_ok, det_logs = self._run_deterministic_scanners(wt, test_cmd)
        det_log_path = self.dir_qa / f"deterministic-checks-a{attempt}.md"
        det_log_path.write_text(det_logs, encoding="utf-8")
        self.record_artifact(f"05-qa/deterministic-checks-a{attempt}.md", det_log_path)

        req_text = self._read_optional(self.dir_po / "requirements.md")
        tm_text = self._read_optional(self.dir_design / "threat-model.md")
        diff_text = self._read_optional(self.dir_dev / "implementation.diff", "")

        ui_qa_rules = ""
        if dev_profile == "FRONTEND_UI":
            ui_qa_rules = (
                "\n\nMANDATORY UI/UX ACCEPTANCE VERIFICATION:\n"
                "- Verify all Safe Area insets and dynamic notch padding.\n"
                "- Verify that vector icons are used instead of Unicode emojis.\n"
                "- Verify Loading, Empty, and Error UI fallback states.\n"
            )

        sys_prompt = (
            "You are the Lead QA & Verification Engineer in an SSDLC pipeline.\n"
            "Verify the implementation diff and deterministic test logs against EVERY Acceptance Criterion (AC-*) "
            "and Security Requirement (SEC-*) in `01-po/requirements.md`.\n"
            f"{ui_qa_rules}"
            "Produce:\n"
            "1. ## Requirements Traceability Matrix (Markdown Table: ID | Requirement | Verification Evidence | Status PASS/FAIL)\n"
            "2. ## Edge Cases & Behavioral Verification Analysis\n"
            "3. ## Verdict\n"
            "At the very end, output EXACTLY one of:\n"
            "QA_STATUS: PASSED (only if all ACs, SEC items, and deterministic tests pass)\n"
            "or\n"
            "QA_STATUS: FAILED (if any AC, SEC item, or test fails)"
        )
        user_prompt = (
            f"PO REQUIREMENTS:\n{req_text}\n\n"
            f"THREAT MODEL:\n{tm_text}\n\n"
            f"DETERMINISTIC TEST & SCAN LOGS:\n{det_logs}\n\n"
            f"IMPLEMENTATION DIFF:\n```diff\n{diff_text[:60000]}\n```"
        )

        t0 = time.time()
        qa_out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
            qa_engine,
            qa_model,
            sys_prompt,
            user_prompt,
            self.repo_root,
            self.project_id,
            self.ticket_id,
            f"05-qa-a{attempt}",
            sandbox="read-only",
            reasoning_effort=qa_effort,
        )
        dur = time.time() - t0

        llm_qa_ok = "QA_STATUS: PASSED" in qa_out
        final_qa_ok = det_ok and llm_qa_ok

        qa_path = self.dir_qa / "qa-traceability-matrix.md"
        qa_path.write_text(qa_out, encoding="utf-8")
        self.record_artifact("05-qa/qa-traceability-matrix.md", qa_path)
        self.record_ledger(
            CostAndTelemetryEntry(
                f"05-qa-a{attempt}",
                "QA & Security Verifier",
                qa_engine,
                qa_model,
                "PASSED" if final_qa_ok else "FAILED",
                dur,
                cost,
                tin,
                tout,
                trace_user,
                reasoning_effort=qa_effort,
            )
        )
        self.record_gate(
            f"QA_VERIFICATION_A{attempt}",
            final_qa_ok,
            f"deterministic_ok={det_ok}, ac_matrix_ok={llm_qa_ok}, reasoning={qa_effort}",
        )
        print(f"    [{'OK' if final_qa_ok else 'BOUNCE'}] QA Gate status: {'PASSED' if final_qa_ok else 'FAILED'}")
        return final_qa_ok, f"{det_logs}\n\n{qa_out}"

    # ------------------------------------------------------------------
    # PHASE 5b: AppSec Red-Teamer & Supply-Chain / SCA Auditor
    # ------------------------------------------------------------------
    def run_phase_appsec(self, attempt: int = 1) -> Tuple[bool, str]:
        risk_tier = self.state.get("risk_tier", "M")
        dev_profile = self.state.get("dev_profile", "ALGORITHMIC")
        appsec_engine, appsec_model, appsec_effort, appsec_reason = SquadDispatcher.dispatch_role(
            "05b-appsec", risk_tier, dev_profile, attempt=attempt
        )
        print(f"\n==> [{self.project_id} | Phase 5b: 05b-appsec] AppSec Red-Teamer ({appsec_engine}:{appsec_model}, reasoning={appsec_effort})...")
        print(f"    [Squad Dispatcher] 05b-appsec allocated — {appsec_reason}")
        self.state["current_phase"] = "05b-appsec"
        self._save_state()

        wt = self._ensure_worktree()
        req_text = self._read_optional(self.dir_po / "requirements.md")
        tm_text = self._read_optional(self.dir_design / "threat-model.md")
        diff_text = self._read_optional(self.dir_dev / "implementation.diff", "")

        sca_logs = ["# Supply-Chain & Dependency Audit (SCA)\n"]
        if (wt / "package-lock.json").exists() and (shutil.which("npm") or EngineRunner._should_spawn_role_container()):
            npma_out, npma_err, npma_rc = EngineRunner.run_sandboxed_cmd(
                ["npm", "audit", "--audit-level=high"],
                cwd=wt,
                timeout=120,
            )
            sca_logs.append(f"## npm audit (exit {npma_rc})\n```\n{(npma_out + npma_err)[:8000]}\n```\n")
        if ((wt / "requirements.txt").exists() or (wt / "pyproject.toml").exists()) and (shutil.which("pip-audit") or EngineRunner._should_spawn_role_container()):
            pa_out, pa_err, pa_rc = EngineRunner.run_sandboxed_cmd(
                ["pip-audit"],
                cwd=wt,
                timeout=120,
            )
            sca_logs.append(f"## pip-audit (exit {pa_rc})\n```\n{(pa_out + pa_err)[:8000]}\n```\n")

        sca_text = "\n".join(sca_logs)
        sca_path = self.dir_appsec / "supply-chain-sca.md"
        sca_path.write_text(sca_text, encoding="utf-8")
        self.record_artifact("05b-appsec/supply-chain-sca.md", sca_path)

        sys_prompt = (
            "You are the Principal AppSec Red-Teamer & Supply-Chain Security Auditor.\n"
            "Attempt to break the implementation diff and audit any new dependencies:\n"
            "1. ## Offensive Security & Exploit Analysis (Injection, Auth Bypass, IDOR, SSRF, Path Traversal, Deserialization, DoS)\n"
            "2. ## Supply-Chain & Dependency Review (new packages, typosquatting, CVEs, license risks)\n"
            "3. ## Secrets, Crypto & Data Exposure Check\n"
            "At the very end, output EXACTLY one of:\n"
            "APPSEC_STATUS: SECURE (if no exploitable vulnerabilities or high-severity supply-chain risks exist)\n"
            "or\n"
            "APPSEC_STATUS: VULNERABLE (if any exploitable flaw or unmitigated security risk exists)"
        )
        user_prompt = (
            f"PO SECURITY REQUIREMENTS:\n{req_text}\n\n"
            f"THREAT MODEL:\n{tm_text}\n\n"
            f"SCA SCANNER LOGS:\n{sca_text}\n\n"
            f"IMPLEMENTATION DIFF:\n```diff\n{diff_text[:60000]}\n```"
        )

        t0 = time.time()
        appsec_out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
            appsec_engine,
            appsec_model,
            sys_prompt,
            user_prompt,
            self.repo_root,
            self.project_id,
            self.ticket_id,
            f"05b-appsec-a{attempt}",
            sandbox="read-only",
            reasoning_effort=appsec_effort,
        )
        dur = time.time() - t0

        secure = "APPSEC_STATUS: SECURE" in appsec_out
        redteam_path = self.dir_appsec / "redteam-audit.md"
        redteam_path.write_text(appsec_out, encoding="utf-8")
        self.record_artifact("05b-appsec/redteam-audit.md", redteam_path)

        self.record_ledger(
            CostAndTelemetryEntry(
                f"05b-appsec-a{attempt}",
                "AppSec Red-Teamer",
                appsec_engine,
                appsec_model,
                "SECURE" if secure else "VULNERABLE",
                dur,
                cost,
                tin,
                tout,
                trace_user,
                reasoning_effort=appsec_effort,
            )
        )
        self.record_gate(f"APPSEC_REDTEAM_A{attempt}", secure, f"AppSec verdict: {'SECURE' if secure else 'VULNERABLE'} (reasoning={appsec_effort})")
        print(f"    [{'OK' if secure else 'BOUNCE'}] AppSec Gate status: {'SECURE' if secure else 'VULNERABLE'}")
        return secure, appsec_out

    # ------------------------------------------------------------------
    # PHASE 6: DevSecOps Release, Rollback & Observability Engineer
    # ------------------------------------------------------------------
    def run_phase_ops(self) -> Path:
        risk_tier = self.state.get("risk_tier", "M")
        dev_profile = self.state.get("dev_profile", "ALGORITHMIC")
        ops_engine, ops_model, ops_effort, ops_reason = SquadDispatcher.dispatch_role(
            "06-ops", risk_tier, dev_profile, attempt=1
        )
        print(f"\n==> [{self.project_id} | Phase 6: 06-ops] DevSecOps ({ops_engine}:{ops_model}, reasoning={ops_effort})...")
        print(f"    [Squad Dispatcher] 06-ops allocated — {ops_reason}")
        self.state["current_phase"] = "06-ops"
        self._save_state()

        adr_text = self._read_optional(self.dir_design / "adr.md")
        diff_text = self._read_optional(self.dir_dev / "implementation.diff", "")

        sys_prompt = (
            "You are the Lead DevSecOps & Release Reliability Engineer.\n"
            "Review the implementation diff and architecture to produce an actionable Release & Observability Runbook:\n"
            "1. ## Configuration & Environment Variables Added/Changed\n"
            "2. ## Database / State Migration & Zero-Downtime Rollout Steps\n"
            "3. ## Observability & Log Hygiene Audit (verify no PII/secrets in logs; list key metrics/alerts)\n"
            "4. ## Exact Rollback Procedure (how to safely revert if production metrics degrade)"
        )
        user_prompt = f"ADR:\n{adr_text}\n\nIMPLEMENTATION DIFF:\n```diff\n{diff_text[:50000]}\n```"

        t0 = time.time()
        ops_out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
            ops_engine,
            ops_model,
            sys_prompt,
            user_prompt,
            self.repo_root,
            self.project_id,
            self.ticket_id,
            "06-ops",
            sandbox="read-only",
            reasoning_effort=ops_effort,
        )
        dur = time.time() - t0

        ops_path = self.dir_ops / "release-and-rollback.md"
        ops_path.write_text(ops_out, encoding="utf-8")
        self.record_artifact("06-ops/release-and-rollback.md", ops_path)
        self.record_ledger(
            CostAndTelemetryEntry(
                "06-ops", "DevSecOps Engineer", ops_engine, ops_model, "READY", dur, cost, tin, tout, trace_user, reasoning_effort=ops_effort
            )
        )
        self.record_gate("DEVSECOPS_RELEASE_GATE", True, f"Generated release & rollback runbook (reasoning={ops_effort}).")
        print(f"    [OK] Wrote {ops_path.relative_to(self.repo_root)}")
        return ops_path

    # ------------------------------------------------------------------
    # PHASE 7: Documentation & API Contract Steward
    # ------------------------------------------------------------------
    def run_phase_docs(self) -> Path:
        risk_tier = self.state.get("risk_tier", "M")
        dev_profile = self.state.get("dev_profile", "ALGORITHMIC")
        docs_engine, docs_model, docs_effort, docs_reason = SquadDispatcher.dispatch_role(
            "07-docs", risk_tier, dev_profile, attempt=1
        )
        print(f"\n==> [{self.project_id} | Phase 7: 07-docs] Documentation Steward ({docs_engine}:{docs_model}, reasoning={docs_effort})...")
        print(f"    [Squad Dispatcher] 07-docs allocated — {docs_reason}")
        self.state["current_phase"] = "07-docs"
        self._save_state()

        req_text = self._read_optional(self.dir_po / "requirements.md")
        diff_text = self._read_optional(self.dir_dev / "implementation.diff", "")

        sys_prompt = (
            "You are the Technical Writer & API Contract Steward.\n"
            "Based on the PO Requirements and verified implementation diff, produce:\n"
            "1. ## Changelog Entry (Keep-a-Changelog format: Added / Changed / Fixed / Security)\n"
            "2. ## API / CLI / Configuration Contract Updates\n"
            "3. ## Developer & User Documentation Snippet"
        )
        user_prompt = f"PO REQUIREMENTS:\n{req_text}\n\nIMPLEMENTATION DIFF:\n```diff\n{diff_text[:50000]}\n```"

        t0 = time.time()
        docs_out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
            docs_engine,
            docs_model,
            sys_prompt,
            user_prompt,
            self.repo_root,
            self.project_id,
            self.ticket_id,
            "07-docs",
            sandbox="read-only",
            reasoning_effort=docs_effort,
        )
        dur = time.time() - t0

        docs_path = self.dir_docs / "documentation-and-changelog.md"
        docs_path.write_text(docs_out, encoding="utf-8")
        self.record_artifact("07-docs/documentation-and-changelog.md", docs_path)
        self.record_ledger(
            CostAndTelemetryEntry(
                "07-docs", "Documentation Steward", docs_engine, docs_model, "UPDATED", dur, cost, tin, tout, trace_user, reasoning_effort=docs_effort
            )
        )
        self.record_gate("DOCS_STEWARD_GATE", True, f"Generated changelog and API/contract documentation (reasoning={docs_effort}).")
        print(f"    [OK] Wrote {docs_path.relative_to(self.repo_root)}")
        return docs_path

    # ------------------------------------------------------------------
    # PHASE 8: Coordinator Evidence Pack (Human Supervision Gate)
    # ------------------------------------------------------------------
    def compile_evidence_pack(self, ready_for_signoff: bool) -> Path:
        print(f"\n==> [{self.project_id} | Phase 8: 08-supervisor] Compiling SHA-256 Evidence Pack for Human Supervisor...")
        self.state["current_phase"] = "08-supervisor"
        self.state["status"] = "AWAITING_SUPERVISOR_SIGNOFF" if ready_for_signoff else "ESCALATED_TO_SUPERVISOR"
        self._save_state()

        total_cost = sum(item.get("cost_usd", 0.0) for item in self.state["ledger"])
        total_dur = sum(item.get("duration_sec", 0.0) for item in self.state["ledger"])

        ledger_rows = []
        for item in self.state["ledger"]:
            ledger_rows.append(
                f"| `{item['phase']}` | {item['role']} | `{item['engine']}:{item['model']}` | `{item.get('reasoning_effort', 'medium')}` | `{item.get('trace_user', '')}` | **{item['status']}** | {item['duration_sec']}s | ${item['cost_usd']:.4f} |"
            )

        artifact_rows = []
        for name, meta in sorted(self.state["artifacts"].items()):
            artifact_rows.append(
                f"| [`{name}`](./{meta['path']}) | `{meta['sha256'][:16]}...` | {meta['size_bytes']} B | {meta['recorded_at']} |"
            )

        gate_rows = []
        for g in self.state["gate_history"]:
            badge = "PASS" if g["passed"] else "FAIL"
            gate_rows.append(f"| `{g['gate']}` | **{badge}** | {g['details']} | {g['timestamp']} |")

        specialists_str = ", ".join(self.state.get("specialists_triggered", [])) or "None"
        iso = self.state.get("isolation", {})
        vers = self.state.get("cli_versions", {})

        pack_md = f"""# SSDLC Supervisor Evidence Pack — `{self.project_id}` / `{self.ticket_id}`

- **Project ID**: `{self.project_id}`
- **Ticket ID**: `{self.ticket_id}`
- **Status**: `{self.state['status']}`
- **Risk Tier & Dev Profile**: `Tier {self.state.get('risk_tier', 'M')}` / `{self.state.get('dev_profile', 'ALGORITHMIC')}`
- **Conditional Specialists Triggered**: `{specialists_str}`
- **Goal**: {self.state['goal']}
- **Base Commit**: `{self.state['base_commit']}`
- **Isolated Worktree Branch**: `{self.state['dev_branch']}`
- **Total Duration**: `{total_dur:.1f}s`
- **Tracked OpenRouter Cost**: `${total_cost:.4f} USD`

---

## 1. Project Isolation & Key Traceability

| Property | Value |
| :--- | :--- |
| **Isolated `CODEX_HOME`** | `{iso.get('codex_home')}` |
| **Isolated `CLAUDE_CONFIG_DIR`** | `{iso.get('claude_home')}` |
| **OpenRouter Key Fingerprint** | `{iso.get('openrouter_key_fingerprint')}` |
| **OpenAI Key Fingerprint** | `{iso.get('openai_key_fingerprint')}` |
| **Anthropic Key Fingerprint** | `{iso.get('anthropic_key_fingerprint')}` |
| **`agy` Version** | `{vers.get('agy')}` |
| **`codex` Version** | `{vers.get('codex')}` |
| **`claude` Version** | `{vers.get('claude')}` |

---

## 2. Deterministic Gate Audit Trail

| Gate | Verdict | Details | Timestamp |
| :--- | :--- | :--- | :--- |
{chr(10).join(gate_rows)}

---

## 3. Role Execution, Reasoning Effort, Trace Tag & Cost Ledger

| Phase | Role | Engine / Model | Reasoning | Trace Tag (`user`) | Status | Duration | Cost (USD) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
{chr(10).join(ledger_rows)}

---

## 4. On-Disk Evidence Manifest (SHA-256 Verified)

| Evidence Artifact | SHA-256 Digest (Prefix) | Size | Timestamp |
| :--- | :--- | :--- | :--- |
{chr(10).join(artifact_rows)}

---

## 5. Supervisor Actions

To inspect the code diff in the isolated worktree:
```bash
git -C "{self.worktree_dir}" status
cat "{self.dir_dev / 'implementation.diff'}"
```

To **APPROVE** and merge the verified branch into your project repository (and prune the temporary worktree):
```bash
./ssdlc approve {self.ticket_id}
```

To **REJECT** and send feedback back to the team:
```bash
./ssdlc reject {self.ticket_id} --reason "Describe required adjustment"
```
"""
        pack_path = self.ssdlc_root / "EVIDENCE_PACK.md"
        pack_path.write_text(pack_md, encoding="utf-8")
        self.record_artifact("EVIDENCE_PACK.md", pack_path)
        print(f"    [OK] Evidence Pack ready at: {pack_path.relative_to(self.repo_root)}")

        if self.remote_sync.enabled and (not self._task_mode() or ready_for_signoff):
            wt = self.worktree_dir
            branch = self.state.get("dev_branch", f"ssdlc/{self.ticket_id}")
            base_branch = "main"
            try:
                cur_b = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                )
                if cur_b.returncode == 0 and cur_b.stdout.strip():
                    base_branch = cur_b.stdout.strip()
            except Exception:
                pass

            if self._task_mode() and (git(wt, "status", "--porcelain") or self._get_head_sha(wt) != self.state.get("task_verified_head")):
                raise TaskError("Refusing to publish changes outside verified checkpoints")
            if wt.exists() and not self._task_mode():
                subprocess.run(["git", "add", "-A"], cwd=str(wt), capture_output=True)
                status_chk = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(wt),
                    capture_output=True,
                    text=True,
                )
                if status_chk.stdout.strip():
                    subprocess.run(
                        ["git", "commit", "-m", f"feat({self.ticket_id}): implementation checkpoint"],
                        cwd=str(wt),
                        capture_output=True,
                        text=True,
                    )

            if not self.state.get("issue_id"):
                req_text = self._read_optional(self.dir_po / "requirements.md")
                issue_num, issue_url = self.remote_sync.sync_issue(
                    self.state.get("goal", self.ticket_id), req_text
                )
                if issue_num:
                    self.issue_id = issue_num
                    self.state["issue_id"] = issue_num
                    self.state["issue_url"] = issue_url
                    self._save_state()

            pushed = self.remote_sync.push_branch(branch, cwd=(wt if wt.exists() else self.repo_root))
            if pushed:
                pr_url = self.remote_sync.create_or_update_pr(
                    branch=branch,
                    base_branch=base_branch,
                    title=self.state.get("goal", self.ticket_id),
                    body=pack_md,
                    issue_number=self.state.get("issue_id"),
                    draft=True,
                )
                if pr_url:
                    self.state["pr_url"] = pr_url
                    self._save_state()
                    if "- **Remote Pull Request**:" not in pack_md:
                        pack_md += f"\n- **Remote Pull Request**: [{pr_url}]({pr_url})\n"
                        pack_path.write_text(pack_md, encoding="utf-8")
                        self.record_artifact("EVIDENCE_PACK.md", pack_path)

        return pack_path

    # ------------------------------------------------------------------
    # Full End-to-End Risk-Tiered Pipeline (with --from-phase Resumability)
    # ------------------------------------------------------------------
    PHASE_ORDER = [
        "00-recon",
        "01-po",
        "02-design",
        "03-dev",
        "04-review",
        "05-qa",
        "05b-appsec",
        "06-ops",
        "07-docs",
        "08-supervisor",
    ]

    def check_budget_circuit_breaker(self) -> None:
        env_cap = os.environ.get("SSDLC_BUDGET_CAP_USD", "").strip()
        if env_cap:
            budget_cap = float(env_cap)
        else:
            tier = (self.state.get("risk_tier") or "M").upper()
            tier_caps = {"S": 3.0, "M": 6.0, "L": 20.0}
            budget_cap = tier_caps.get(tier, 6.0)

        if budget_cap <= 0:
            return
        total_spent = sum(item.get("cost_usd", 0.0) for item in self.state.get("ledger", []))
        if total_spent >= budget_cap:
            msg = f"Circuit Breaker: Total spend (${total_spent:.2f}) reached or exceeded budget cap (${budget_cap:.2f})!"
            print(f"\n[CIRCUIT-BREAKER] {msg}")
            self.record_gate("BUDGET_CAP_GATE", False, msg)
            self.state["status"] = "HALTED_BUDGET_EXCEEDED"
            self._save_state()
            raise TaskError(msg)

    def run_pipeline(
        self,
        goal: str,
        test_cmd: Optional[str] = None,
        tier: str = "auto",
        from_phase: str = "00-recon",
    ) -> Path:
        with ticket_lock(self.ssdlc_root / "ticket.lock"):
            # Reload state after acquiring the lock, not from a potentially stale constructor.
            self.state = self._load_or_init_state()
            if self._task_mode():
                self.state["task_breakdown"] = True
                self._save_state()
                if os.environ.get("SSDLC_EXECUTION_MODE", "docker") == "docker" and not EngineRunner._should_spawn_role_container():
                    raise TaskError("Task mode in Docker requires role-container isolation. Run the controller on the host with the runner image available, or explicitly select local mode.")
                self._ensure_quality_contract()
            try:
                return self._run_pipeline(goal, test_cmd, tier, from_phase)
            except TaskError as exc:
                self.record_gate("TASK_EXECUTION_GATE", False, str(exc))
                self.compile_evidence_pack(ready_for_signoff=False)
                raise

    def _run_pipeline(
        self,
        goal: str,
        test_cmd: Optional[str] = None,
        tier: str = "auto",
        from_phase: str = "00-recon",
    ) -> Path:
        if self._task_mode():
            if from_phase not in ("00-recon", "03-dev"):
                raise TaskError("Bootstrap task mode resumes from 03-dev; skipping later gates requires Step 3 attestations")
            if from_phase == "00-recon" and (self.dir_dev / "tasks" / "task-journal.jsonl").exists():
                raise TaskError("Existing task execution must resume with --from-phase 03-dev to preserve its approved inputs")
        start_idx = self.PHASE_ORDER.index(from_phase) if from_phase in self.PHASE_ORDER else 0

        if start_idx <= self.PHASE_ORDER.index("00-recon"):
            _, active_tier = self.run_phase_recon(goal, requested_tier=tier)
            self.check_budget_circuit_breaker()
        else:
            active_tier = self.state.get("risk_tier", tier if tier != "auto" else "M")
            print(f"    [RESUME] Skipping 00-recon (using existing risk_tier={active_tier})")

        if start_idx <= self.PHASE_ORDER.index("01-po"):
            self.run_phase_po(goal)
            self.check_budget_circuit_breaker()
        else:
            print("    [RESUME] Skipping 01-po")

        if start_idx <= self.PHASE_ORDER.index("02-design"):
            if active_tier in ("M", "L"):
                self.run_phase_pair_design()
            else:
                self.record_gate("PAIR_DESIGN_GATE", True, "Skipped in Tier S (low-risk fast track).")
            self.check_budget_circuit_breaker()
        else:
            print("    [RESUME] Skipping 02-design")

        if self._task_mode():
            self._ensure_task_plan()

        max_retries = int(os.environ.get("SSDLC_MAX_DEV_RETRIES", "3"))
        feedback = ""
        passed_all = False

        if start_idx <= self.PHASE_ORDER.index("05b-appsec"):
            for attempt in range(1, max_retries + 1):
                if attempt > 1 or start_idx <= self.PHASE_ORDER.index("03-dev"):
                    self.run_phase_dev(feedback=feedback, attempt=attempt)
                    self.check_budget_circuit_breaker()
                else:
                    print("    [RESUME] Skipping initial 03-dev execution")

                if attempt > 1 or start_idx <= self.PHASE_ORDER.index("04-review"):
                    rev_ok, rev_report = self.run_phase_review(attempt=attempt)
                    self.check_budget_circuit_breaker()
                    if not rev_ok:
                        feedback = f"Code Reviewer rejected the diff (Attempt {attempt}):\n{rev_report}"
                        continue
                else:
                    print("    [RESUME] Skipping initial 04-review execution")

                if attempt > 1 or start_idx <= self.PHASE_ORDER.index("05-qa"):
                    qa_ok, qa_report = self.run_phase_qa(test_cmd=test_cmd, attempt=attempt)
                    self.check_budget_circuit_breaker()
                    if not qa_ok:
                        feedback = f"QA Gate rejected the implementation (Attempt {attempt}):\n{qa_report}"
                        continue
                else:
                    print("    [RESUME] Skipping initial 05-qa execution")

                run_appsec = (
                    active_tier == "L"
                    or "APPSEC_SUPPLY_CHAIN_REDTEAM" in self.state.get("specialists_triggered", [])
                    or from_phase == "05b-appsec"
                )
                if run_appsec:
                    appsec_ok, appsec_report = self.run_phase_appsec(attempt=attempt)
                    self.check_budget_circuit_breaker()
                    if not appsec_ok:
                        feedback = f"AppSec Red-Teamer rejected the implementation (Attempt {attempt}):\n{appsec_report}"
                        continue

                passed_all = True
                break
        else:
            passed_all = True

        if passed_all and active_tier in ("M", "L"):
            # Elastic pipeline: On pure FRONTEND_UI without infra/DB changes, skip 06-ops to save tokens
            is_frontend_ui = self.state.get("dev_profile") == "FRONTEND_UI"
            diff_text = self._read_optional(self.dir_dev / "implementation.diff", "").lower()
            has_infra_changes = any(k in diff_text for k in ("docker", "k8s", "alembic", "prisma", "migration", ".sql", "compose", "terraform"))
            if is_frontend_ui and not has_infra_changes:
                print("    [Squad Dispatcher] Elastic Pipeline: Skipping 06-ops (pure frontend UI task with zero infra/DB impact).")
            elif start_idx <= self.PHASE_ORDER.index("06-ops"):
                self.run_phase_ops()
                self.check_budget_circuit_breaker()

            if start_idx <= self.PHASE_ORDER.index("07-docs"):
                self.run_phase_docs()
                self.check_budget_circuit_breaker()

        if self._task_mode() and passed_all:
            if (self._get_head_sha(self.worktree_dir) != self.state.get("task_verified_head")
                    or git(self.worktree_dir, "status", "--porcelain")):
                raise TaskError("Code changed after task verification; global gates cannot authorize delivery")
        return self.compile_evidence_pack(ready_for_signoff=passed_all)

    def approve_and_merge(self) -> None:
        with ticket_lock(self.ssdlc_root / "ticket.lock"):
            self.state = self._load_or_init_state()
            self._approve_and_merge()

    def _approve_and_merge(self) -> None:
        if self._task_mode():
            if self.state.get("status") != "AWAITING_SUPERVISOR_SIGNOFF":
                raise TaskError("Task ticket has not passed the global gates")
            if (self._get_head_sha(self.worktree_dir) != self.state.get("task_verified_head")
                    or git(self.worktree_dir, "status", "--porcelain")):
                raise TaskError("Task worktree no longer matches the verified checkpoint")
            if (git(self.repo_root, "rev-parse", "HEAD") != self.state["base_commit"]
                    or git(self.repo_root, "status", "--porcelain")):
                raise TaskError("Bootstrap approval requires a clean unchanged target; advanced-target integration belongs to Step 3")
        if self.state.get("status") not in ("AWAITING_SUPERVISOR_SIGNOFF", "ESCALATED_TO_SUPERVISOR"):
            raise RuntimeError(f"Ticket {self.ticket_id} is in status {self.state.get('status')}, cannot approve.")
        wt = self.worktree_dir
        branch = self.state["dev_branch"]
        if wt.exists() and not self._task_mode():
            subprocess.run(["git", "add", "-A"], cwd=str(wt), check=True)
            subprocess.run(
                ["git", "commit", "-m", f"feat({self.ticket_id}): SSDLC verified implementation"],
                cwd=str(wt),
                capture_output=True,
                text=True,
            )
            sha = self._get_head_sha(wt)
            (self.dir_dev / "commit-sha.txt").write_text(sha + "\n", encoding="utf-8")
            self.record_artifact("03-dev/commit-sha.txt", self.dir_dev / "commit-sha.txt")

        if self._task_mode():
            git(self.repo_root, "merge", "--ff-only", self.state["task_verified_head"])
        else:
            subprocess.run(["git", "merge", "--no-ff", branch, "-m", f"Merge SSDLC verified branch {branch}"], cwd=str(self.repo_root), check=True)

        # Compound Knowledge: Auto-register any new components into .ssdlc/standards/COMPONENT_CATALOG.md
        standards_catalog = self.repo_root / ".ssdlc" / "standards" / "COMPONENT_CATALOG.md"
        diff_file = self.dir_dev / "implementation.diff"
        if standards_catalog.exists() and diff_file.exists():
            diff_text = self._read_optional(diff_file, "")
            new_components = re.findall(r"\+\+\+ b/(?:client/[^/]+/)?src/components/([^/\s]+\.tsx)", diff_text)
            if new_components:
                catalog_text = standards_catalog.read_text(encoding="utf-8")
                updates = []
                for comp in sorted(set(new_components)):
                    if comp not in catalog_text:
                        updates.append(f"- **`{comp}`**: Introduced in ticket `{self.ticket_id}`.\n")
                if updates:
                    standards_catalog.write_text(catalog_text.rstrip() + "\n\n## Recently Merged Components\n" + "".join(updates), encoding="utf-8")
                    print(f"    [Standards Registry] Auto-registered {len(updates)} new component(s) into COMPONENT_CATALOG.md")

        # Prune worktree to reclaim disk space while preserving .ssdlc/<ticket> evidence
        if wt.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=str(self.repo_root), capture_output=True)
        self.state["status"] = "APPROVED_AND_MERGED"
        self.record_gate("SUPERVISOR_SIGNOFF", True, "Supervisor approved, merged branch, and pruned temporary worktree.")
        print(f"[OK] Ticket {self.ticket_id} approved, merged, and worktree cleaned up.")

        if self.remote_sync.enabled:
            cur_b = subprocess.run(["git", "branch", "--show-current"], cwd=str(self.repo_root), capture_output=True, text=True)
            if cur_b.returncode == 0 and cur_b.stdout.strip():
                print(f"==> [Remote-Sync] Pushing merged '{cur_b.stdout.strip()}' to origin...")
                subprocess.run(["git", "push", "origin", cur_b.stdout.strip()], cwd=str(self.repo_root))


def cmd_doctor(repo: Path) -> int:
    iso = ProjectRuntimeIsolation(repo).activate()
    print(f"=== SSDLC Project Environment Doctor ({iso['project_id']}) ===")
    print(f"  - Project Root       : {repo}")
    print(f"  - Project Env File   : {iso['project_env']} ({'EXISTS' if Path(iso['project_env']).exists() else 'MISSING'})")
    print(f"  - Isolated CODEX_HOME: {iso['codex_home']}")
    print(f"  - Isolated CLAUDE_DIR: {iso['claude_home']}")
    print("  - CLI Versions       :")
    for binary in ("agy", "codex", "claude", "git", "tmux", "uv", "gitleaks"):
        print(f"      * {binary:<10}: {get_cli_version(binary)}")
    print("  - Key & Subscription Status:")
    print(f"      * OPENROUTER_API_KEY : {fingerprint_secret(os.environ.get('OPENROUTER_API_KEY', ''))}")
    print(f"      * OPENAI_API_KEY     : {fingerprint_secret(os.environ.get('OPENAI_API_KEY', ''))}")
    print(f"      * ANTHROPIC_API_KEY  : {fingerprint_secret(os.environ.get('ANTHROPIC_API_KEY', ''))}")

    # Check Codex Subscription
    codex_auth = Path(iso['codex_home']) / "auth.json"
    if not codex_auth.exists():
        codex_auth = Path.home() / ".codex" / "auth.json"
    codex_status = "UNSET"
    if codex_auth.exists():
        try:
            cd = json.loads(codex_auth.read_text(encoding="utf-8"))
            mode = cd.get("auth_mode", "unknown")
            plan = cd.get("chatgpt_plan_type", "active")
            codex_status = f"ACTIVE ({mode} / {plan})"
        except Exception:
            codex_status = "EXISTS"
    print(f"      * CODEX_SUBSCRIPTION : {codex_status}")

    # Account metadata alone is not proof of an authenticated Claude CLI.
    claude_cred = Path(iso['claude_home']) / ".credentials.json"
    claude_status = "UNSET"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        claude_status = f"OAUTH_TOKEN ({fingerprint_secret(os.environ['CLAUDE_CODE_OAUTH_TOKEN'])})"
    elif claude_cred.is_file():
        try:
            credentials = json.loads(claude_cred.read_text(encoding="utf-8"))
            if (credentials.get("claudeAiOauth") or {}).get("accessToken"):
                claude_status = "CREDENTIALS_PRESENT (validity unverified)"
            else:
                claude_status = "CREDENTIALS_INVALID"
        except (OSError, ValueError):
            claude_status = "CREDENTIALS_INVALID"
    print(f"      * CLAUDE_SUBSCRIPTION: {claude_status}")

    agy_token = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    agy_status = "AUTH_PRESENT (plan unverified)" if agy_token.is_file() else "UNSET"
    print(f"      * AGY_SUBSCRIPTION   : {agy_status}")

    remote_sync = RemoteSyncManager(repo, "doctor")
    token_status = "EXISTS" if remote_sync.token else "UNSET"
    print("  - Remote Sync Status :")
    print(f"      * Remote URL        : {remote_sync.remote_url or 'NONE'}")
    print(f"      * Provider          : {remote_sync.provider} ({remote_sync.owner}/{remote_sync.repo if remote_sync.owner else '-'})")
    print(f"      * API Token         : {token_status}")
    print(f"      * Sync Remote Flag  : {'ENABLED' if remote_sync.enabled else 'DISABLED'} ({remote_sync.mode})")
    if remote_sync.provider == "github" and remote_sync.enabled and not remote_sync.token:
        gh_path = shutil.which("gh")
        if gh_path:
            try:
                gh_auth = subprocess.run(
                    [gh_path, "auth", "token"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if gh_auth.returncode == 0:
                    print('      * Action            : gh is authenticated; run export GITHUB_TOKEN="$(gh auth token)" before ./ssdlc run. Verify the token can access this repository.')
                else:
                    print('      * Action            : Run gh auth login, then export GITHUB_TOKEN="$(gh auth token)" before ./ssdlc run. Verify the token can access this repository.')
            except (OSError, subprocess.TimeoutExpired):
                print("      * Action            : Unable to check gh authentication; set GITHUB_TOKEN before ./ssdlc run and verify repository access.")
        else:
            print("      * Action            : Set GITHUB_TOKEN before ./ssdlc run and verify repository access.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Project-Isolated Multi-CLI SSDLC Orchestrator")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_doc = sub.add_parser("doctor", help="Check project isolation, CLI versions, and API key fingerprints")
    p_doc.add_argument("--repo", "-r", default=".", help="Path to project repository")

    p_run = sub.add_parser("run", help="Run the risk-tiered SSDLC pipeline for a ticket")
    p_run.add_argument("--ticket", "-t", required=True, help="Ticket ID (e.g., TKT-001-auth)")
    p_run.add_argument("--goal", "-g", required=True, help="High-level requirement or feature goal")
    p_run.add_argument("--tier", default=None, choices=["auto", "S", "M", "L"], help="Risk tier: auto, S, M, or L")
    p_run.add_argument("--repo", "-r", default=".", help="Path to Git repository (default: current dir)")
    p_run.add_argument("--test-cmd", default=None, help="Deterministic test command for Phase 5 QA")
    p_run.add_argument(
        "--from-phase",
        default="00-recon",
        choices=SSDLCOrchestrator.PHASE_ORDER,
        help="Resume an interrupted ticket starting from a specific phase (default: 00-recon)",
    )
    run_sync = p_run.add_mutually_exclusive_group()
    run_sync.add_argument("--sync-remote", dest="sync_remote", action="store_true", default=None,
                          help="Enable remote synchronization")
    run_sync.add_argument("--no-sync-remote", dest="sync_remote", action="store_false",
                          help="Disable remote synchronization")
    p_run.add_argument(
        "--issue",
        type=int,
        default=None,
        help="Link run to an existing remote Issue number",
    )

    p_plan = sub.add_parser("plan", help="Resume task planning using approved requirements and design only")
    p_plan.add_argument("--ticket", required=True)
    p_plan.add_argument("--repo", "-r", default=".")
    p_plan.add_argument("--review-repairs", type=int, choices=range(5), default=None, help="Explicitly extend the persisted semantic repair allowance (0-4)")

    p_app = sub.add_parser("approve", help="Supervisor sign-off: commit worktree changes, merge, and prune worktree")
    p_app.add_argument("ticket", help="Ticket ID")
    p_app.add_argument("--repo", "-r", default=".", help="Path to Git repository")
    app_sync = p_app.add_mutually_exclusive_group()
    app_sync.add_argument("--sync-remote", dest="sync_remote", action="store_true", default=None,
                          help="Push merged branch to remote origin")
    app_sync.add_argument("--no-sync-remote", dest="sync_remote", action="store_false",
                          help="Do not push merged branch")

    p_rej = sub.add_parser("reject", help="Supervisor rejection: bounce back to Dev with feedback")
    p_rej.add_argument("ticket", help="Ticket ID")
    p_rej.add_argument("--reason", required=True, help="Supervisor rejection feedback")
    p_rej.add_argument("--repo", "-r", default=".", help="Path to Git repository")
    p_rej.add_argument("--test-cmd", default=None, help="Deterministic test command")
    rej_sync = p_rej.add_mutually_exclusive_group()
    rej_sync.add_argument("--sync-remote", dest="sync_remote", action="store_true", default=None,
                          help="Enable remote synchronization on rejection cycle")
    rej_sync.add_argument("--no-sync-remote", dest="sync_remote", action="store_false",
                          help="Disable remote synchronization on rejection cycle")

    args = parser.parse_args()
    repo_path = Path(getattr(args, "repo", ".")).resolve()

    if args.subcommand == "doctor":
        return cmd_doctor(repo_path)

    if args.subcommand == "plan":
        orch = SSDLCOrchestrator(repo_path, args.ticket, sync_remote=False)
        print(orch.resume_task_planning(review_repairs=args.review_repairs))
        return 0

    if args.subcommand == "run":
        sync_remote = args.sync_remote
        issue_id = getattr(args, "issue", None)
        orch = SSDLCOrchestrator(repo_path, args.ticket, sync_remote=sync_remote, issue_id=issue_id)
        effective_tier = args.tier or os.environ.get("SSDLC_DEFAULT_TIER", "auto")
        orch.run_pipeline(
            args.goal,
            test_cmd=args.test_cmd,
            tier=effective_tier,
            from_phase=args.from_phase,
        )
        return 0

    if args.subcommand == "approve":
        sync_remote = args.sync_remote
        orch = SSDLCOrchestrator(repo_path, args.ticket, sync_remote=sync_remote)
        orch.approve_and_merge()
        return 0

    if args.subcommand == "reject":
        sync_remote = args.sync_remote
        orch = SSDLCOrchestrator(repo_path, args.ticket, sync_remote=sync_remote)
        orch.record_gate("SUPERVISOR_SIGNOFF", False, f"Rejected by supervisor: {args.reason}")
        orch.run_phase_dev(feedback=f"SUPERVISOR REJECTION: {args.reason}", attempt=99)
        orch.run_phase_review(attempt=99)
        orch.run_phase_qa(test_cmd=args.test_cmd, attempt=99)
        if orch.state.get("risk_tier") == "L" or "APPSEC_SUPPLY_CHAIN_REDTEAM" in orch.state.get("specialists_triggered", []):
            orch.run_phase_appsec(attempt=99)
        orch.compile_evidence_pack(ready_for_signoff=True)
        return 0

    return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
