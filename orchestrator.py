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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
            [path, "--version"],
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

        # Load project-specific .ssdlc/project.env
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

        # Optionally link read-only host auth.json ONLY if SSDLC_INHERIT_HOST_CODEX_AUTH=true
        if os.environ.get("SSDLC_INHERIT_HOST_CODEX_AUTH", "false").lower() == "true":
            host_auth = Path.home() / ".codex" / "auth.json"
            proj_auth = self.codex_home / "auth.json"
            if host_auth.exists() and not proj_auth.exists():
                shutil.copy2(host_auth, proj_auth)
                os.chmod(proj_auth, 0o600)

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


class DynamicRoutingPolicy:
    """
    Dynamically computes reasoning effort ('low' | 'medium' | 'high' | 'xhigh'),
    engine routing, and model selection based on:
      1. Phase & Role
      2. Risk Tier ('S' | 'M' | 'L')
      3. Task Dev Profile ('ALGORITHMIC' | 'FULLSTACK' | 'SECURITY' | 'REFACTOR' | 'DOCS')
      4. Retry Attempt (escalates reasoning effort on each bounce)
    """

    EFFORT_LADDER = ["low", "medium", "high", "xhigh"]

    BASE_EFFORT_MATRIX: Dict[str, Dict[str, str]] = {
        "00-recon":       {"S": "low",    "M": "medium", "L": "medium"},
        "01-po":          {"S": "low",    "M": "medium", "L": "high"},
        "02-design-arch": {"S": "low",    "M": "medium", "L": "high"},
        "02-design-sec":  {"S": "medium", "M": "high",   "L": "xhigh"},
        "03-dev":         {"S": "low",    "M": "medium", "L": "high"},
        "04-review":      {"S": "medium", "M": "high",   "L": "high"},
        "05-qa":          {"S": "medium", "M": "medium", "L": "high"},
        "05b-appsec":     {"S": "high",   "M": "high",   "L": "xhigh"},
        "06-ops":         {"S": "low",    "M": "medium", "L": "high"},
        "07-docs":        {"S": "low",    "M": "low",    "L": "medium"},
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
        # Escalate +1 step per retry bounce (attempt 2 -> +1, attempt 3+ or 99 -> +2/max)
        if attempt > 1:
            bump = 2 if attempt >= 3 else (attempt - 1)
            idx = min(len(cls.EFFORT_LADDER) - 1, idx + bump)
        return cls.EFFORT_LADDER[idx]

    @classmethod
    def resolve_dev_engine_and_model(
        cls, risk_tier: str, dev_profile: str, attempt: int = 1
    ) -> Tuple[str, str, str]:
        configured_engine = os.environ.get("SSDLC_DEV_ENGINE", "auto").lower().strip()
        configured_model = os.environ.get("SSDLC_DEV_MODEL", "").strip()
        escalation_model = os.environ.get("SSDLC_DEV_ESCALATION_MODEL", "").strip()
        effort = cls.resolve_reasoning_effort("03-dev", risk_tier, attempt=attempt)

        # Automatic engine selection based on 00-recon DEV_PROFILE when SSDLC_DEV_ENGINE="auto"
        if configured_engine in ("", "auto"):
            profile = (dev_profile or "ALGORITHMIC").upper()
            has_claude_auth = bool(
                os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
            ) and bool(shutil.which("claude"))
            if profile in ("FULLSTACK", "REFACTOR", "DOCS") and has_claude_auth:
                engine = "claude"
            else:
                engine = "codex"
        else:
            engine = configured_engine

        model = configured_model
        if attempt >= 2 and escalation_model:
            model = escalation_model

        return engine, model, effort

    @classmethod
    def resolve_review_models(cls, risk_tier: str, attempt: int = 1) -> Tuple[List[str], str]:
        models_csv = os.environ.get(
            "SSDLC_REVIEW_MODELS", "deepseek/deepseek-r1,anthropic/claude-3.7-sonnet"
        )
        all_models = [m.strip() for m in models_csv.split(",") if m.strip()]
        effort = cls.resolve_reasoning_effort("04-review", risk_tier, attempt=attempt)
        # In Tier S on Attempt 1, a single reviewer suffices; on bounce or Tier M/L, run full panel
        if (risk_tier or "M").upper() == "S" and attempt == 1 and len(all_models) > 1:
            return [all_models[0]], effort
        return all_models, effort


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
        runtime_dir = repo_root / ".ssdlc" / ".runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        ssdlc_home = Path(__file__).resolve().parent

        pids_limit = os.environ.get("SSDLC_CONTAINER_PIDS_LIMIT", "512")
        mem_limit = os.environ.get("SSDLC_CONTAINER_MEMORY", "4g")
        cpus_limit = os.environ.get("SSDLC_CONTAINER_CPUS", "2")

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(pids_limit),
            "--memory",
            str(mem_limit),
            "--cpus",
            str(cpus_limit),
            "--entrypoint",
            inner_cmd[0],
            "-w",
            str(cwd.resolve()),
            "-v",
            f"{repo_root}:{repo_root}:ro",
            "-v",
            f"{runtime_dir}:{runtime_dir}:rw",
            "-v",
            f"{ssdlc_home}:/opt/ssdlc:ro",
            "-v",
            "ssdlc-shared-uv-cache:/root/.cache/uv",
            "-v",
            "ssdlc-shared-npm-cache:/root/.npm",
        ]
        if not str(repo_root).startswith("/tmp/"):
            docker_cmd.extend(["--tmpfs", "/tmp:rw,exec,size=512m"])
        if Path("/usr/local/bin/codex").exists():
            docker_cmd.extend(["-v", "/usr/local/bin/codex:/opt/host-bin/codex:ro"])
        host_agy = Path.home() / ".local" / "bin" / "agy"
        if host_agy.exists():
            docker_cmd.extend(["-v", f"{host_agy}:/opt/host-bin/agy:ro"])

        if writable_worktree is not None:
            wt_resolved = writable_worktree.resolve()
            docker_cmd.extend(["-v", f"{wt_resolved}:{wt_resolved}:rw"])
            # Mount ONLY .git/worktrees/<wt_name> :rw so git status/diff inside the worktree works
            # while .git/hooks, .git/config, and .git/refs remain kernel-enforced :ro!
            wt_git_meta = repo_root / ".git" / "worktrees" / wt_resolved.name
            if wt_git_meta.is_dir():
                docker_cmd.extend(["-v", f"{wt_git_meta}:{wt_git_meta}:rw"])

        base_env_keys = [
            "SSDLC_PROJECT_ID",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "XDG_CACHE_HOME",
        ]
        if pass_ai_keys:
            base_env_keys.extend(
                [
                    "OPENROUTER_API_KEY",
                    "OPENAI_API_KEY",
                    "ANTHROPIC_API_KEY",
                    "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_AUTH_TOKEN",
                ]
            )

        for env_key in base_env_keys:
            val = os.environ.get(env_key)
            if val:
                docker_cmd.extend(["-e", f"{env_key}={val}"])

        docker_cmd.append(EngineRunner.IMAGE_NAME)
        docker_cmd.extend(inner_cmd[1:])
        return docker_cmd

    @staticmethod
    def run_sandboxed_cmd(
        cmd: List[str],
        cwd: Path,
        timeout: int = 300,
    ) -> Tuple[str, str, int]:
        """
        Executes a deterministic test or scanner command inside the read-only role container
        (with AI API keys stripped and /tmp on tmpfs) when Docker role isolation is enabled.
        """
        if EngineRunner._should_spawn_role_container():
            exec_cmd = EngineRunner._wrap_in_role_container(
                cmd,
                cwd=cwd,
                writable_worktree=None,
                pass_ai_keys=False,
            )
            env = None
        else:
            exec_cmd = cmd
            env = {k: v for k, v in os.environ.items() if k not in (
                "OPENROUTER_API_KEY",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
            )}

        proc = subprocess.run(
            exec_cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
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
            out, _ = EngineRunner.run_claude(full_prompt, cwd=Path.cwd(), read_only=True)
            return out, 0.0, 0, 0
        if shutil.which("codex"):
            out, _ = EngineRunner.run_codex(
                full_prompt,
                cwd=Path.cwd(),
                sandbox="read-only",
                reasoning_effort=reasoning_effort,
            )
            return out, 0.0, 0, 0
        if shutil.which("claude"):
            out, _ = EngineRunner.run_claude(full_prompt, cwd=Path.cwd(), read_only=True)
            return out, 0.0, 0, 0
        raise RuntimeError(
            "Neither OPENROUTER_API_KEY nor local 'codex'/'claude' CLI is available."
        )

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
            "--sandbox",
            effective_sandbox,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
        ]
        if use_openrouter and os.environ.get("OPENROUTER_API_KEY"):
            cmd.extend(["-c", 'model_provider="openrouter"'])
        if model:
            cmd.extend(["-m", model])
        cmd.append(prompt)
        if use_container:
            cmd = EngineRunner._wrap_in_role_container(
                cmd, cwd=cwd, writable_worktree=writable_worktree
            )
        dev_timeout = int(os.environ.get("SSDLC_DEV_TIMEOUT", "2400"))
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=dev_timeout,
        )
        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return combined.strip(), proc.returncode

    @staticmethod
    def run_claude(
        prompt: str,
        cwd: Path,
        read_only: bool = True,
        use_openrouter: bool = False,
        writable_worktree: Optional[Path] = None,
    ) -> Tuple[str, int]:
        env = os.environ.copy()
        env["IS_SANDBOX"] = "1"
        if use_openrouter and env.get("OPENROUTER_API_KEY"):
            env["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api"
            env["ANTHROPIC_AUTH_TOKEN"] = env["OPENROUTER_API_KEY"]
            env["ANTHROPIC_API_KEY"] = env["OPENROUTER_API_KEY"]
        cmd = ["claude", "-p", prompt]
        if not read_only:
            cmd.append("--dangerously-skip-permissions")
        if EngineRunner._should_spawn_role_container():
            cmd = EngineRunner._wrap_in_role_container(
                cmd,
                cwd=cwd,
                writable_worktree=(None if read_only else writable_worktree),
            )
        dev_timeout = int(os.environ.get("SSDLC_DEV_TIMEOUT", "2400"))
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=dev_timeout,
        )
        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return combined.strip(), proc.returncode

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
        if engine == "openrouter":
            return EngineRunner.call_openrouter(
                model,
                system_prompt,
                user_prompt,
                project_id,
                ticket_id,
                phase,
                reasoning_effort=reasoning_effort,
            )
        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
        if engine == "claude":
            out, rc = EngineRunner.run_claude(
                full_prompt, cwd=cwd, read_only=(sandbox == "read-only")
            )
            if rc != 0:
                raise RuntimeError(f"claude exited with code {rc}:\n{out}")
            return out, 0.0, 0, 0, trace_user
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

    def __init__(self, repo_root: Path, ticket_id: str, enabled: bool = False):
        self.repo_root = repo_root.resolve()
        self.ticket_id = ticket_id
        env_enabled = os.environ.get("SSDLC_SYNC_REMOTE", "false").lower() in ("true", "1", "yes")
        self.enabled = enabled or env_enabled
        self.provider = "generic"
        self.host = ""
        self.owner = ""
        self.repo = ""
        self.remote_url = ""
        self.token = ""
        self._detect_remote()

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
            r"(?:git@[\w.-]*github[\w.-]*:|https?://[\w.-]*github[\w.-]*/)([\w.-]+)/([\w.-]+?)(?:\.git)?$",
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
            r"(?:git@([\w.-]*gitlab[\w.-]*):|https?://([\w.-]*gitlab[\w.-]*)/)([\w.-]+)/([\w.-]+?)(?:\.git)?$",
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
            cmd = ["git", "push", "-u", "origin", branch]
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
        sync_remote: bool = False,
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
        self.state_file.write_text(
            json.dumps(self.state, indent=2), encoding="utf-8"
        )

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
        return (
            f"Project ID: {self.project_id}\n"
            f"Repository Root: {self.repo_root}\n"
            f"Recent Commits:\n{recent_commits}\n\n"
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

        engine = os.environ.get("SSDLC_RECON_ENGINE", "openrouter")
        model = os.environ.get("SSDLC_RECON_MODEL", "google/gemini-2.5-pro")
        effort = DynamicRoutingPolicy.resolve_reasoning_effort("00-recon", "M", attempt=1)
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
            "   - `DEV_PROFILE: ALGORITHMIC` (Backend logic, concurrency, data structures, CLI, APIs)\n"
            "   - `DEV_PROFILE: FULLSTACK` (UI, frontend/backend integration, multi-file CRUD)\n"
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
        mp = re.search(r"DEV_PROFILE:\s*(ALGORITHMIC|FULLSTACK|SECURITY|REFACTOR|DOCS)\b", out)
        if mp:
            dev_profile = mp.group(1)

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
        effort = DynamicRoutingPolicy.resolve_reasoning_effort("01-po", risk_tier, attempt=1)
        print(f"\n==> [{self.project_id} | Phase 1: 01-po] Product Owner defining Stories & ACs (reasoning={effort})...")
        self.state["current_phase"] = "01-po"
        self._save_state()

        engine = os.environ.get("SSDLC_PO_ENGINE", "openrouter")
        model = os.environ.get("SSDLC_PO_MODEL", "anthropic/claude-3.7-sonnet")
        recon_text = (self.dir_recon / "context-and-blast-radius.md").read_text(encoding="utf-8")

        sys_prompt = (
            "You are the Product Owner (PO) in a strict Secure Software Development Lifecycle (SSDLC).\n"
            "Using the Codebase Recon report and user goal, produce a rigorous Markdown specification:\n"
            "1. ## Overview & Business Goal\n"
            "2. ## User Stories\n"
            "3. ## Testable Acceptance Criteria (numbered AC-1, AC-2, ... with Given/When/Then)\n"
            "4. ## Security Requirements & Abuse Cases (numbered SEC-1, SEC-2, ...)\n"
            "5. ## Non-Functional & Observability Requirements (logging, metrics, rollback expectations)\n"
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

        arch_engine = os.environ.get("SSDLC_DESIGN_ARCH_ENGINE", "openrouter")
        arch_model = os.environ.get("SSDLC_DESIGN_ARCH_MODEL", "anthropic/claude-3.7-sonnet")
        sec_engine = os.environ.get("SSDLC_DESIGN_SEC_ENGINE", "openrouter")
        sec_model = os.environ.get("SSDLC_DESIGN_SEC_MODEL", "deepseek/deepseek-r1")
        max_rounds = int(os.environ.get("SSDLC_MAX_DESIGN_ROUNDS", "3"))

        dialogue_log: List[str] = ["# Pair Design & STRIDE Threat Modeling Transcript\n"]
        current_adr = ""
        current_threat_model = ""
        approved = False

        risk_tier = self.state.get("risk_tier", "M")
        for rnd in range(1, max_rounds + 1):
            arch_effort = DynamicRoutingPolicy.resolve_reasoning_effort("02-design-arch", risk_tier, attempt=rnd)
            sec_effort = DynamicRoutingPolicy.resolve_reasoning_effort("02-design-sec", risk_tier, attempt=rnd)
            print(f"    -> Round {rnd}/{max_rounds}: Architect ({arch_engine}:{arch_model}, reasoning={arch_effort}) proposing ADR...")
            arch_sys = (
                "You are the Lead Systems Architect in a Pair Design session.\n"
                "Draft or revise a complete Architecture Decision Record (ADR) that satisfies all PO Acceptance "
                "Criteria (AC-*) and Security Requirements (SEC-*), and resolves any critiques from the Security Reviewer.\n"
                "Include: Exact file paths to create/modify, interfaces, DB/state impact, error handling, and test strategy."
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
            True,
            f"Completed in {rnd} round(s) (explicit approval={approved}).",
        )
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
            cmd = ["git", "worktree", "add", "-b", branch, str(self.worktree_dir), "HEAD"]
        proc = subprocess.run(cmd, cwd=str(self.repo_root), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create git worktree: {proc.stderr}")
        self.isolation.ensure_trusted_path(self.worktree_dir)
        return self.worktree_dir

    def _read_optional(self, path: Path, fallback: str = "(Skipped in Tier S)") -> str:
        return path.read_text(encoding="utf-8") if path.exists() else fallback

    def run_phase_dev(self, feedback: str = "", attempt: int = 1) -> Tuple[Path, List[str]]:
        risk_tier = self.state.get("risk_tier", "M")
        dev_profile = self.state.get("dev_profile", "ALGORITHMIC")
        dev_engine, dev_model, dev_effort = DynamicRoutingPolicy.resolve_dev_engine_and_model(
            risk_tier, dev_profile, attempt=attempt
        )
        print(
            f"\n==> [{self.project_id} | Phase 3: 03-dev] Developer ({dev_engine}:{dev_model or 'default'}, "
            f"reasoning={dev_effort}, profile={dev_profile}) in isolated worktree (Attempt {attempt})..."
        )
        self.state["current_phase"] = "03-dev"
        self._save_state()

        wt = self._ensure_worktree()
        recon_text = self._read_optional(self.dir_recon / "context-and-blast-radius.md")
        req_text = self._read_optional(self.dir_po / "requirements.md")
        adr_text = self._read_optional(self.dir_design / "adr.md")
        tm_text = self._read_optional(self.dir_design / "threat-model.md")

        use_or = os.environ.get("SSDLC_DEV_USE_OPENROUTER", "false").lower() == "true"

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
        )
        if feedback:
            prompt += f"\n\nCRITICAL FEEDBACK FROM PREVIOUS REVIEW/QA/APPSEC GATE (MUST FIX):\n{feedback}\n"

        t0 = time.time()
        in_container = Path("/.dockerenv").exists() or os.environ.get("SSDLC_EXECUTION_MODE", "docker") == "docker"
        dev_sandbox = "danger-full-access" if in_container else "workspace-write"
        if dev_engine == "claude":
            out, rc = EngineRunner.run_claude(
                prompt, cwd=wt, read_only=False, use_openrouter=use_or, writable_worktree=wt
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
        review_models, rev_effort = DynamicRoutingPolicy.resolve_review_models(risk_tier, attempt=attempt)
        print(
            f"\n==> [{self.project_id} | Phase 4: 04-review] Multi-Model Code Reviewer "
            f"({len(review_models)} model(s), reasoning={rev_effort})..."
        )
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

        review_engine = os.environ.get("SSDLC_REVIEW_ENGINE", "openrouter")

        sys_prompt = (
            "You are a Principal Code Reviewer in an SSDLC pipeline.\n"
            "Audit the Git Diff strictly against:\n"
            "1. The agreed Architecture Decision Record (ADR) — check for design drift.\n"
            "2. The Threat Model — verify every required security mitigation is implemented.\n"
            "3. Code quality — race conditions, error handling, resource leaks, maintainability."
            f"{specialist_instructions}\n\n"
            "Output a structured Markdown review with:\n"
            "- ### ADR Conformance Checklist\n"
            "- ### Threat Model Mitigation Checklist\n"
            "- ### Specialist Lens Audit (DB / Perf / SRE if active)\n"
            "- ### Findings (Critical / Major / Minor)\n"
            "At the very end, output EXACTLY one of:\n"
            "REVIEW_STATUS: APPROVED (if zero Critical or Major defects exist)\n"
            "or\n"
            "REVIEW_STATUS: CHANGES_REQUESTED (if any Critical or Major defect exists)"
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
            print(f"    -> Running Reviewer model: {mdl} (reasoning={rev_effort})...")
            t0 = time.time()
            rev_out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
                review_engine,
                mdl,
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
            passed = "REVIEW_STATUS: APPROVED" in rev_out
            if not passed:
                all_approved = False
            self.record_ledger(
                CostAndTelemetryEntry(
                    f"04-review-a{attempt}",
                    "Code Reviewer",
                    review_engine,
                    mdl,
                    "APPROVED" if passed else "CHANGES_REQUESTED",
                    dur,
                    cost,
                    tin,
                    tout,
                    trace_user,
                    reasoning_effort=rev_effort,
                )
            )
            combined_reviews.append(f"## Reviewer: `{mdl}` ({'APPROVED' if passed else 'CHANGES_REQUESTED'}, reasoning={rev_effort})\n\n{rev_out}\n\n---\n")

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
        qa_effort = DynamicRoutingPolicy.resolve_reasoning_effort("05-qa", risk_tier, attempt=attempt)
        print(f"\n==> [{self.project_id} | Phase 5: 05-qa] QA & Verifier (reasoning={qa_effort}) running deterministic checks + AC Matrix...")
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

        qa_engine = os.environ.get("SSDLC_QA_ENGINE", "openrouter")
        qa_model = os.environ.get("SSDLC_QA_MODEL", "anthropic/claude-3.7-sonnet")

        sys_prompt = (
            "You are the Lead QA & Verification Engineer in an SSDLC pipeline.\n"
            "Verify the implementation diff and deterministic test logs against EVERY Acceptance Criterion (AC-*) "
            "and Security Requirement (SEC-*) in `01-po/requirements.md`.\n"
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
        appsec_effort = DynamicRoutingPolicy.resolve_reasoning_effort("05b-appsec", risk_tier, attempt=attempt)
        print(f"\n==> [{self.project_id} | Phase 5b: 05b-appsec] AppSec Red-Teamer & SCA Auditor (reasoning={appsec_effort})...")
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

        appsec_engine = os.environ.get("SSDLC_APPSEC_ENGINE", "openrouter")
        appsec_model = os.environ.get("SSDLC_APPSEC_MODEL", "deepseek/deepseek-r1")

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
        ops_effort = DynamicRoutingPolicy.resolve_reasoning_effort("06-ops", risk_tier, attempt=1)
        print(f"\n==> [{self.project_id} | Phase 6: 06-ops] DevSecOps checking Release Readiness & Rollback (reasoning={ops_effort})...")
        self.state["current_phase"] = "06-ops"
        self._save_state()

        adr_text = self._read_optional(self.dir_design / "adr.md")
        diff_text = self._read_optional(self.dir_dev / "implementation.diff", "")

        ops_engine = os.environ.get("SSDLC_OPS_ENGINE", "openrouter")
        ops_model = os.environ.get("SSDLC_OPS_MODEL", "anthropic/claude-3.7-sonnet")

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
        docs_effort = DynamicRoutingPolicy.resolve_reasoning_effort("07-docs", risk_tier, attempt=1)
        print(f"\n==> [{self.project_id} | Phase 7: 07-docs] Documentation Steward updating changelog & docs (reasoning={docs_effort})...")
        self.state["current_phase"] = "07-docs"
        self._save_state()

        req_text = self._read_optional(self.dir_po / "requirements.md")
        diff_text = self._read_optional(self.dir_dev / "implementation.diff", "")

        docs_engine = os.environ.get("SSDLC_DOCS_ENGINE", "openrouter")
        docs_model = os.environ.get("SSDLC_DOCS_MODEL", "anthropic/claude-3.7-sonnet")

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

        if self.remote_sync.enabled:
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

            if wt.exists():
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

    def run_pipeline(
        self,
        goal: str,
        test_cmd: Optional[str] = None,
        tier: str = "auto",
        from_phase: str = "00-recon",
    ) -> Path:
        start_idx = self.PHASE_ORDER.index(from_phase) if from_phase in self.PHASE_ORDER else 0

        if start_idx <= self.PHASE_ORDER.index("00-recon"):
            _, active_tier = self.run_phase_recon(goal, requested_tier=tier)
        else:
            active_tier = self.state.get("risk_tier", tier if tier != "auto" else "M")
            print(f"    [RESUME] Skipping 00-recon (using existing risk_tier={active_tier})")

        if start_idx <= self.PHASE_ORDER.index("01-po"):
            self.run_phase_po(goal)
        else:
            print("    [RESUME] Skipping 01-po")

        if start_idx <= self.PHASE_ORDER.index("02-design"):
            if active_tier in ("M", "L"):
                self.run_phase_pair_design()
            else:
                self.record_gate("PAIR_DESIGN_GATE", True, "Skipped in Tier S (low-risk fast track).")
        else:
            print("    [RESUME] Skipping 02-design")

        max_retries = int(os.environ.get("SSDLC_MAX_DEV_RETRIES", "3"))
        feedback = ""
        passed_all = False

        if start_idx <= self.PHASE_ORDER.index("05b-appsec"):
            for attempt in range(1, max_retries + 1):
                if attempt > 1 or start_idx <= self.PHASE_ORDER.index("03-dev"):
                    self.run_phase_dev(feedback=feedback, attempt=attempt)
                else:
                    print("    [RESUME] Skipping initial 03-dev execution")

                if attempt > 1 or start_idx <= self.PHASE_ORDER.index("04-review"):
                    rev_ok, rev_report = self.run_phase_review(attempt=attempt)
                    if not rev_ok:
                        feedback = f"Code Reviewer rejected the diff (Attempt {attempt}):\n{rev_report}"
                        continue
                else:
                    print("    [RESUME] Skipping initial 04-review execution")

                if attempt > 1 or start_idx <= self.PHASE_ORDER.index("05-qa"):
                    qa_ok, qa_report = self.run_phase_qa(test_cmd=test_cmd, attempt=attempt)
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
                    if not appsec_ok:
                        feedback = f"AppSec Red-Teamer rejected the implementation (Attempt {attempt}):\n{appsec_report}"
                        continue

                passed_all = True
                break
        else:
            passed_all = True

        if passed_all and active_tier in ("M", "L"):
            if start_idx <= self.PHASE_ORDER.index("06-ops"):
                self.run_phase_ops()
            if start_idx <= self.PHASE_ORDER.index("07-docs"):
                self.run_phase_docs()

        return self.compile_evidence_pack(ready_for_signoff=passed_all)

    def approve_and_merge(self) -> None:
        if self.state.get("status") not in ("AWAITING_SUPERVISOR_SIGNOFF", "ESCALATED_TO_SUPERVISOR"):
            raise RuntimeError(f"Ticket {self.ticket_id} is in status {self.state.get('status')}, cannot approve.")
        wt = self.worktree_dir
        branch = self.state["dev_branch"]
        if wt.exists():
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

        subprocess.run(["git", "merge", "--no-ff", branch, "-m", f"Merge SSDLC verified branch {branch}"], cwd=str(self.repo_root), check=True)
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
    print("  - Key Fingerprints   :")
    print(f"      * OPENROUTER_API_KEY: {fingerprint_secret(os.environ.get('OPENROUTER_API_KEY', ''))}")
    print(f"      * OPENAI_API_KEY    : {fingerprint_secret(os.environ.get('OPENAI_API_KEY', ''))}")
    print(f"      * ANTHROPIC_API_KEY : {fingerprint_secret(os.environ.get('ANTHROPIC_API_KEY', ''))}")

    remote_sync = RemoteSyncManager(repo, "doctor")
    token_status = "EXISTS" if remote_sync.token else "UNSET"
    print("  - Remote Sync Status :")
    print(f"      * Remote URL        : {remote_sync.remote_url or 'NONE'}")
    print(f"      * Provider          : {remote_sync.provider} ({remote_sync.owner}/{remote_sync.repo if remote_sync.owner else '-'})")
    print(f"      * API Token         : {token_status}")
    print(f"      * Sync Remote Flag  : {'ENABLED' if remote_sync.enabled else 'DISABLED (pass --sync-remote or SSDLC_SYNC_REMOTE=true)'}")
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
    p_run.add_argument(
        "--sync-remote",
        action="store_true",
        default=False,
        help="Enable automatic remote Git push and Issue/PR synchronization",
    )
    p_run.add_argument(
        "--issue",
        type=int,
        default=None,
        help="Link run to an existing remote Issue number",
    )

    p_app = sub.add_parser("approve", help="Supervisor sign-off: commit worktree changes, merge, and prune worktree")
    p_app.add_argument("ticket", help="Ticket ID")
    p_app.add_argument("--repo", "-r", default=".", help="Path to Git repository")
    p_app.add_argument(
        "--sync-remote",
        action="store_true",
        default=False,
        help="Push merged branch to remote origin",
    )

    p_rej = sub.add_parser("reject", help="Supervisor rejection: bounce back to Dev with feedback")
    p_rej.add_argument("ticket", help="Ticket ID")
    p_rej.add_argument("--reason", required=True, help="Supervisor rejection feedback")
    p_rej.add_argument("--repo", "-r", default=".", help="Path to Git repository")
    p_rej.add_argument("--test-cmd", default=None, help="Deterministic test command")
    p_rej.add_argument(
        "--sync-remote",
        action="store_true",
        default=False,
        help="Enable automatic remote synchronization on rejection cycle",
    )

    args = parser.parse_args()
    repo_path = Path(getattr(args, "repo", ".")).resolve()

    if args.subcommand == "doctor":
        return cmd_doctor(repo_path)

    if args.subcommand == "run":
        sync_remote = getattr(args, "sync_remote", False)
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
        sync_remote = getattr(args, "sync_remote", False)
        orch = SSDLCOrchestrator(repo_path, args.ticket, sync_remote=sync_remote)
        orch.approve_and_merge()
        return 0

    if args.subcommand == "reject":
        sync_remote = getattr(args, "sync_remote", False)
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
