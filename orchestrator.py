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
import contextlib
import functools
import threading
import inspect
import datetime as dt
import hashlib
import json
import logging
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
from ssdlc_design import DesignSession
from ssdlc_gate_reports import (GateEvaluator, EvaluationRequest, GateTerminalError,
    build_trusted_context, fingerprint, UNTRUSTED_DATA_INSTRUCTION, untrusted_data,
    ProtocolError, ScannerResult, scanner_status_json, discover_manifest_scopes, write_or_verify, canon)
import ssdlc_gate_reports as candidate_reports

OPS_DOCS_TEXT_CAP = 60000
OPS_DOCS_COVERAGE_NOTE = "When full_audit_coverage_permitted is false, you must not claim complete audit coverage."
import ssdlc_bootstrap as bootstrap
import ssdlc_bounce as bounce
from ssdlc_bounce import request_identity
from ssdlc_catalogues import CatalogueError, requirement_catalogue
from ssdlc_requirements import prepare_requirements
from ssdlc_controller import assert_controller, controller_snapshot, execution_contract
from ssdlc_transitions import runtime_root

from ssdlc_tasks import (
    PlanError, TaskError, DevelopmentBlocked, ResourceLimitError, TaskPlanValidator, TaskScheduler, TaskJournal, atomic_json,
    confined, digest, file_hash, git, ticket_lock, validate_document, log_retry_reason, diagnostic_excerpt, test_suite_is_empty, parse_plan_output,
)


_dispatch_local = threading.local()


@contextlib.contextmanager
def _dispatch_context(task_id=None, attempt=None, phase=None):
    previous = getattr(_dispatch_local, 'context', {})
    _dispatch_local.context = dict(previous, **{k: v for k, v in
        dict(task_id=task_id, attempt=attempt, phase=phase).items() if v is not None})
    try:
        yield
    finally:
        _dispatch_local.context = previous


def _supervision_event(event_type, **fields):
    if not os.environ.get('SSDLC_SUPERVISED_RUN_ID'):
        return
    import ssdlc_supervisor as sup
    import fcntl
    env = os.environ
    try:
        allowed = {'phase', 'step', 'category', 'exc_type', 'outcome', 'exit_code',
                   'dispatch_id', 'engine', 'task_id', 'attempt'}
        if set(fields) - allowed:
            return False
        # Keep terminal evidence for ordinary or custom exception classes while
        # never persisting an arbitrary class name or exception message.
        if isinstance(fields.get('exc_type'), str) and fields['exc_type'] not in sup.EXC_TYPES:
            fields['exc_type'] = 'Exception'
        if (not sup.RUN_ID.fullmatch(env.get('SSDLC_SUPERVISED_RUN_ID', '')) or
                not sup.RUN_ID.fullmatch(env.get('SSDLC_SUPERVISED_INTENT', '')) or
                int(env['SSDLC_SUPERVISED_GEN']) < 1):
            return False
        record = dict(type=event_type, seq=1, ts=time.time(),
            run_id=env['SSDLC_SUPERVISED_RUN_ID'], role=env['SSDLC_SUPERVISED_ROLE'],
            gen=int(env['SSDLC_SUPERVISED_GEN']), intent=env['SSDLC_SUPERVISED_INTENT'], **fields)
        dummy = dict(run_id=record['run_id'], launches=[dict(role='controller',
            generation=record['gen'], intent_id=record['intent'], state='published')])
        if not sup.valid_controller_event(record, dummy):
            return
        path = Path(env['SSDLC_SUPERVISED_EVENTS'])
        # The launcher supplies the repository boundary separately from the path.
        root = Path(env['SSDLC_SUPERVISED_REPO']) / '.ssdlc/.supervisor'
        relative = path.relative_to(root)
        fd = sup.open_safe(root, str(relative), os.O_RDWR | os.O_APPEND, True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with os.fdopen(os.dup(fd), 'r') as stream:
                seq = 0
                for line in stream:
                    try:
                        prior = json.loads(line)
                        if sup.valid_controller_event(prior, dummy):
                            seq = max(seq, prior['seq'])
                    except (ValueError, AttributeError):
                        pass
            record['seq'] = seq + 1
            size = os.lseek(fd, 0, os.SEEK_END)
            prefix = b'\n' if size and os.pread(fd, 1, size - 1) != b'\n' else b''
            data = prefix + json.dumps(record, sort_keys=True).encode() + b'\n'
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
            return True
        finally:
            os.close(fd)
    except (OSError, ValueError, KeyError, TypeError, sup.SupervisorIntegrityError):
        # A missing finish leaves an open dispatch; exception paths keep their
        # original exception. The dispatcher requires a durable start below.
        return False


def _supervision_fault(point):
    """Injectable crash boundary; inert in production."""
    pass


def _bounded_entrypoint(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        from ssdlc_recovery import LimitsStore, bounded_mode
        mode, recorded_mode, _ = bounded_mode(self.ssdlc_root)
        previous = getattr(_dispatch_local, 'limits', None)
        if previous is not None and previous.root == self.ssdlc_root:
            previous.guard_entrypoint(fn.__name__)
            return fn(self, *args, **kwargs)
        if (mode is not None or recorded_mode is not None or (self.ssdlc_root / 'limits/policy.json').exists()
                or (self.ssdlc_root / 'limits/policy.json').is_symlink()
                or (self.ssdlc_root / 'limits/task-journal.jsonl').exists()):
            store = LimitsStore(self.ssdlc_root)
            with ticket_lock(self.ssdlc_root / 'limits/limits.lock'):
                envelope, _ = store._load()
            assert_controller(Path(__file__).resolve().parent, envelope['binding']['controller'])
            store.guard_entrypoint(fn.__name__)
            if envelope['binding']['ticket'] != self.ticket_id:
                raise TaskError('Limit policy belongs to another ticket')
            if os.environ.get('SSDLC_AUTONOMOUS', '').lower() == 'true':
                from ssdlc_recovery import validate_launch_caps
                validate_launch_caps(envelope['policy'], fn.__name__, envelope['binding'].get('allowed_phases'))
            _dispatch_local.limits = store
        elif os.environ.get('SSDLC_AUTONOMOUS', '').lower() == 'true':
            from ssdlc_recovery import RecoveryStop
            raise RecoveryStop('LIMIT_REACHED', 'Autonomous launch requires an explicitly authorized limits profile')
        try:
            return fn(self, *args, **kwargs)
        finally:
            _dispatch_local.limits = previous
    return wrapped


def _supervised_dispatch(fn):
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        limits = getattr(_dispatch_local, 'limits', None)
        supervised = bool(os.environ.get('SSDLC_SUPERVISED_RUN_ID'))
        if (not supervised and limits is None) or getattr(_dispatch_local, 'depth', 0):
            return fn(*args, **kwargs)
        bound = signature.bind(*args, **kwargs).arguments
        engine = bound.get('engine', fn.__name__.removeprefix('run_'))
        if isinstance(engine, str):
            engine = engine.lower().strip()
        if fn.__name__ == 'call_openrouter' or engine not in {'agy', 'claude', 'codex'}:
            engine = 'openrouter'
        fields = dict(getattr(_dispatch_local, 'context', {}))
        if 'phase' in bound:
            from ssdlc_supervisor import PHASES
            phase = bound['phase']
            matches = [p for p in PHASES if isinstance(phase, str) and (phase == p or phase.startswith(p + '-'))]
            if matches:
                fields['phase'] = max(matches, key=len)
        requested = bound.get('phase', fields.get('phase', ''))
        role = requested
        phase = fields.get('phase', requested)
        if limits is not None:
            policy = limits._load(repair=False)[0]['policy']
            role = next((r for r in sorted(policy['role_invocations'], key=len, reverse=True)
                         if requested == r or requested.startswith(r + '-')), requested)
            phase = next((p for p in sorted(policy['phase_invocations'], key=len, reverse=True)
                          if requested == p or requested.startswith(p + '-')), requested)
        reservation = limits.reserve(role, phase, engine) if limits is not None else None
        dispatch_id = uuid.uuid4().hex
        if supervised and not _supervision_event('dispatch_started', dispatch_id=dispatch_id, engine=engine, **fields):
            if limits is not None:
                from ssdlc_recovery import NotDispatched
                limits.finish(reservation, 'not_dispatched')
                raise NotDispatched('INTEGRITY', 'Supervisor dispatch intent unavailable; engine not launched')
            from ssdlc_supervisor import SupervisorIntegrityError
            raise SupervisorIntegrityError('filesystem_integrity')
        _supervision_fault('dispatch.after_started_before_engine')
        _dispatch_local.depth = 1
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            _supervision_event('dispatch_finished', dispatch_id=dispatch_id, outcome='raised', exc_type=type(exc).__name__)
            if limits is not None:
                from ssdlc_recovery import NotDispatched, QuiescentTimeout
                outcome = ('not_dispatched' if isinstance(exc, NotDispatched) else
                           'terminated' if isinstance(exc, QuiescentTimeout) else 'uncertain')
                limits.finish(reservation, outcome)
            raise
        else:
            if limits is not None:
                from ssdlc_recovery import observed_usage
                limits.finish(reservation, 'returned', observed_usage(engine, fn.__name__, result))
            _supervision_fault('dispatch.after_engine_before_finished')
            _supervision_event('dispatch_finished', dispatch_id=dispatch_id, outcome='returned')
            return result
        finally:
            _dispatch_local.depth = 0
    wrapped._supervised_dispatch = True
    return wrapped


def _supervised_phase(phase):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with _dispatch_context(phase=phase):
                _supervision_event('phase_started', phase=phase)
                if args and isinstance(getattr(args[0], 'ssdlc_root', None), Path):
                    result = _bounded_entrypoint(fn)(*args, **kwargs)
                else:
                    result = fn(*args, **kwargs)
                _supervision_event('phase_finished', phase=phase)
                return result
        return wrapped
    return decorate


def _supervised_preflight():
    if not os.environ.get('SSDLC_SUPERVISED_RUN_ID'):
        return True
    import ssdlc_supervisor as sup
    try:
        env = os.environ
        store = sup.RunStore(env['SSDLC_SUPERVISED_REPO'], env['SSDLC_SUPERVISED_TICKET'], env['SSDLC_SUPERVISED_RUN_ID'])
        d = store.read()
        current = store.current()
        launch = sup.intent_for(d, 'controller')
        snapshot_sha256 = sup.controller_snapshot_digest()
        ok = (current['run_id'] == env['SSDLC_SUPERVISED_RUN_ID'] and
              env.get('SSDLC_SUPERVISED_ROLE') == 'controller' and
              d['nonce'] == env.get('SSDLC_SUPERVISED_NONCE') and snapshot_sha256 == env['SSDLC_SUPERVISED_CONTROLLER_SHA'] and
              sup.config_digest(env) == env['SSDLC_SUPERVISED_CONFIG_SHA'] and
              launch['intent_id'] == env['SSDLC_SUPERVISED_INTENT'] and
              launch['generation'] == int(env['SSDLC_SUPERVISED_GEN']) and
              launch['state'] == 'published' and not d['launch_revoked'] and not d['cancel'])
        if ok:
            sup.pinned_root(env['SSDLC_SUPERVISED_REPO'], env['SSDLC_SUPERVISED_TICKET'], d)
            return True
    except (OSError, ValueError, KeyError, TypeError, TaskError, subprocess.SubprocessError, sup.SupervisorIntegrityError):
        pass
    _supervision_event('failure', category='INTEGRITY', exc_type='SupervisorIntegrityError')
    return False


def _resume_git(repo, *args):
    proc = subprocess.run(['git', '--no-optional-locks', *args], cwd=repo,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    if proc.returncode:
        raise PlanError('Resume baseline is unavailable')
    return proc.stdout.rstrip('\n')


def _resume_plan_refusal(repo, root, state):
    if (root / "worktree").exists() or ((root / "03-dev") / "tasks/task-journal.jsonl").exists():
        raise PlanError("Execution already started; explicit plan migration is required")
    gates = [g for g in state.get("gate_history", []) if g['gate'] == 'PAIR_DESIGN_GATE']
    if not gates or not gates[-1]['passed']:
        raise PlanError("Design has not been approved")
    required = ["00-recon/quality-contract.json", "01-po/requirements.md"]
    if state.get("risk_tier") != "S":
        required += ["02-design/adr.md", "02-design/threat-model.md"]
    for name in required:
        meta = state.get("artifacts", {}).get(name)
        path = root / name
        if not meta or not path.is_file() or file_hash(path) != meta['sha256']:
            raise PlanError(f"Planning input is missing, unapproved or changed: {name}")


def _resume_design_refusal(repo, root, state, read_git=None):
    read_git = read_git or _resume_git
    if (root / "worktree").exists() or ((root / "02-design") / 'plan.json').exists():
        raise PlanError('Planning or execution already started; explicit design migration required')
    for gate in ('RECON_BLAST_RADIUS_GATE', 'PO_SPEC_GATE'):
        history = [g for g in state.get('gate_history', []) if g['gate'] == gate]
        if not history or not history[-1]['passed']:
            raise PlanError(f'Design requires approved upstream gate: {gate}')
    required = ['00-recon/context-and-blast-radius.md', '01-po/requirements.md']
    required.append('00-recon/quality-contract.json')
    for name in required:
        meta = state.get('artifacts', {}).get(name)
        path = root / name
        if not meta or not path.is_file() or file_hash(path) != meta['sha256']:
            raise PlanError(f'Design input changed or disappeared: {name}')
    if read_git(repo, 'rev-parse', 'HEAD') != state['base_commit'] or read_git(repo, 'status', '--porcelain'):
        raise PlanError('Design requires the unchanged clean target baseline')


def _resume_run_refusal(root, state, from_phase, baseline_exists=True):
    if from_phase not in ('00-recon', '03-dev'):
        raise TaskError('Bootstrap task mode resumes from 03-dev; skipping later gates requires Step 3 attestations')
    if from_phase == '03-dev' and not baseline_exists:
        raise TaskError('Resume requires baseline state')
    if from_phase == '00-recon' and (root / '03-dev/tasks/task-journal.jsonl').exists():
        raise TaskError('Existing task execution must resume with --from-phase 03-dev to preserve its approved inputs')



LEGACY_COUNTERS = {'legacy_refusals': 0}


def legacy_message(ticket):
    return (f'Run {ticket} is a legacy run and is not executable. Start a new run: '
            'ssdlc run --ticket <NEW_TICKET> --goal "<goal>" --repo <repo>')


def execution_compatibility(repo_root, ticket, command):
    """Classify persisted execution evidence without locks or writable objects."""
    result = dict(ok=False, cls='legacy', code='legacy_not_executable', reason='incompatible_state')
    try:
        if not isinstance(ticket, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', ticket) or ticket in {'.', '..'}:
            return dict(result, code='unsupported_transition', reason='invalid_ticket')
        repo = Path(repo_root).resolve()
        root = repo / '.ssdlc' / ticket
        root = runtime_root(root)
        if any(path.is_symlink() for path in [root / 'state.json', root, *root.parents]):
            raise PlanError('Symlinked execution root')
        def read(name):
            path = root / name
            if any(part.is_symlink() for part in [root, *path.parents, path]):
                raise PlanError('Symlinked execution evidence')
            return path.read_bytes()
        if not (root / 'state.json').exists():
            return dict(ok=command in {'run', 'start'}, cls='new_run',
                        code='compatible' if command in {'run', 'start'} else 'no_such_run', reason='no_such_run')
        state = json.loads(read('state.json'))
        def registered(name):
            data = read(name)
            if hashlib.sha256(data).hexdigest() != state.get('artifacts', {}).get(name, {}).get('sha256'):
                raise PlanError('Unregistered execution evidence')
            return json.loads(data)
        if state.get('task_breakdown') is True:
            try:
                quality = registered('00-recon/quality-contract.json')
                plan = registered('02-design/plan.json')
                validate_document('quality-contract', quality)
                validate_document('plan', plan)
                for name in CONTEXT_NAMES:
                    path = root / name
                    if any(part.is_symlink() for part in [path, *path.parents]):
                        raise PlanError('Symlinked context')
                    if path.exists() and hashlib.sha256(read(name)).hexdigest() != state.get('artifacts', {}).get(name, {}).get('sha256'):
                        raise PlanError('Changed context')
                data = read('03-dev/tasks/task-journal.jsonl')
                journal, sequence, _ = TaskJournal._replay(data[:data.rfind(b'\n') + 1])
                if not journal or sequence < 1 or journal.get('inputs_sha256') != digest(dict(plan=plan, quality=quality, context=build_task_context(root))):
                    raise PlanError('Unsealed task inputs')
                if command == 'approve' and (state.get('status') != 'AWAITING_SUPERVISOR_SIGNOFF' or
                        not journal.get('tasks') or any(t.get('status') != 'COMPLETED' for t in journal['tasks'].values()) or
                        not state.get('task_verified_head') or state['task_verified_head'] != journal.get('expected_head')):
                    raise PlanError('Incomplete task approval')
                return dict(ok=True, cls='sealed_task', code='compatible', reason='compatible')
            except (OSError, ValueError, KeyError, TypeError, AttributeError, TaskError):
                pass
        if (state.get('task_engine_v1') is True and
                not any((root / name).exists() or (root / name).is_symlink() for name in
                        ('worktree', '03-dev/tasks', '03-dev/implementation.diff')) and
                command in {'run', 'start', 'plan', 'design', 'po'}):
            return dict(ok=True, cls='pre_execution', code='compatible', reason='compatible')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, TaskError):
        pass
    return result


def _discover_checks(repo):
    repo = Path(repo)
    checks = []
    test_files = list((repo / "tests").rglob("test_*.py"))
    if test_files:
        pytest_style = any(re.search(r"^def test_|^import pytest|^from pytest", p.read_text(), re.M) for p in test_files)
        argv = ["python3", "-m", "pytest", "tests"] if pytest_style else ["python3", "-m", "unittest", "discover", "-s", "tests"]
        checks.append({"check_id": "CHECK-TESTS", "argv": argv, "cwd": ".", "timeout_sec": 300, "kind": "test", "required": True})
    package = repo / "package.json"
    if package.exists():
        scripts = json.loads(package.read_text()).get("scripts", {})
        for script, kind in (("test", "test"), ("lint", "lint"), ("typecheck", "types")):
            if script in scripts:
                checks.append({"check_id": f"CHECK-NPM-{script.upper()}", "argv": ["npm", "run", script], "cwd": ".", "timeout_sec": 300, "kind": kind, "required": True})
    pyproject = ((repo / "pyproject.toml").read_text() if (repo / "pyproject.toml").exists() else "")
    for marker, name, argv, kind in (
        ("[tool.ruff", "RUFF", ["python3", "-m", "ruff", "check", "."], "lint"),
        ("[tool.mypy", "MYPY", ["python3", "-m", "mypy", "."], "types"),
    ):
        if marker in pyproject:
            checks.append({"check_id": f"CHECK-{name}", "argv": argv, "cwd": ".", "timeout_sec": 300, "kind": kind, "required": True})
    return checks


def startup_preflight(repo_root, ticket, command, args=None):
    result = execution_compatibility(repo_root, ticket, command)
    phase = args.get('from_phase') if isinstance(args, dict) else getattr(args, 'from_phase', None)
    if result['cls'] == 'pre_execution' and command in {'run', 'start'} and phase not in {None, '00-recon'}:
        result = dict(ok=False, cls='legacy', code='legacy_not_executable', reason='execution_not_started')
    if result['ok'] and result['cls'] in {'new_run', 'pre_execution'}:
        sealed = Path(repo_root) / '.ssdlc' / str(ticket) / '00-recon/quality-contract.json'
        if not sealed.exists():
            try:
                profile = bootstrap.load_profile(Path(repo_root))
                if profile is not None:
                    base = git(Path(repo_root), 'rev-parse', 'HEAD')
                    state_path = Path(repo_root) / '.ssdlc' / str(ticket) / 'state.json'
                    if state_path.exists():
                        base = json.loads(state_path.read_text())['base_commit']
                    result['bootstrap'] = bootstrap.decide(Path(repo_root), base, profile)
            except bootstrap.BootstrapError as exc:
                return dict(ok=False, cls=result['cls'], code='bootstrap_ineligible', reason=exc.code)
    return result


def resume_compatibility(repo_root, ticket, entrypoint, from_phase=None, env=None):
    """Read-only transition check; never activates runtime or constructs a controller."""
    if not isinstance(ticket, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', ticket) or ticket in {'.', '..'}:
        return dict(ok=False, code='unsupported_transition', reason='invalid_ticket')
    compatible = startup_preflight(repo_root, ticket, entrypoint, {'from_phase': from_phase})
    if not compatible['ok']:
        return compatible
    repo = Path(repo_root).resolve()
    root = repo / '.ssdlc' / ticket
    try:
        resolved = runtime_root(root)
        if resolved != root:
            from ssdlc_reconciliation import authorized
            if not authorized(root, resolved) or entrypoint != 'run' or from_phase != '03-dev':
                raise TaskError('Continuation execution requires exact run-from-03 authorization')
            root = resolved
        path = root / 'state.json'
        state = json.loads(path.read_text()) if path.is_file() else {}
        if entrypoint == 'run':
            _resume_run_refusal(root, state, from_phase or '00-recon', path.is_file())
        elif entrypoint in {'plan', 'design'}:
            globals()['_resume_' + entrypoint + '_refusal'](repo, root, state)
        else:
            raise PlanError('Unsupported resume entrypoint')
        return dict(ok=True, code='compatible', reason='compatible')
    except (TaskError, OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return dict(ok=False, code='unsupported_transition', reason='incompatible_state')


OPTIONAL_FALLBACK = "(Skipped in Tier S)"
CONTEXT_NAMES = ("01-po/requirements.md", "02-design/adr.md", "02-design/threat-model.md")
_OBSOLETE_FLAG_WARNED = False


def build_task_context(ssdlc_root: Path) -> Dict[str, str]:
    """Read the exact context values sealed by the task scheduler."""
    return {name: (ssdlc_root / name).read_text(encoding="utf-8")
            if (ssdlc_root / name).exists() else OPTIONAL_FALLBACK for name in CONTEXT_NAMES}


def _warn_obsolete_task_flag() -> None:
    global _OBSOLETE_FLAG_WARNED
    if 'SSDLC_ENABLE_TASK_BREAKDOWN' in os.environ and not _OBSOLETE_FLAG_WARNED:
        _OBSOLETE_FLAG_WARNED = True
        logging.getLogger(__name__).warning(
            'obsolete-flag name=SSDLC_ENABLE_TASK_BREAKDOWN action=ignored')


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_exists(path: Path) -> bool:
    """Check path existence safely, suppressing PermissionError/OSError for non-root users."""
    try:
        return path.exists()
    except (OSError, PermissionError):
        return False


def safe_is_file(path: Path) -> bool:
    """Check if path is a file safely, suppressing PermissionError/OSError for non-root users."""
    try:
        return path.is_file()
    except (OSError, PermissionError):
        return False


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


def parse_qa_verdict(report: str) -> Optional[str]:
    """QA approval requires one exact final marker, never a quoted substring."""
    markers = re.findall(r'\bQA_STATUS\s*:', report)
    final = report.rstrip().splitlines()[-1].strip() if report.strip() else ''
    if len(markers) == 1:
        for verdict in ('PASSED', 'FAILED'):
            if final == f'QA_STATUS: {verdict}':
                return verdict
    return None


def parse_appsec_verdict(report: str) -> Optional[str]:
    """Require exactly one AppSec marker on the final line."""
    markers = re.findall(r'\bAPPSEC_STATUS\s*:', report)
    final = report.rstrip().splitlines()[-1].strip() if report.strip() else ''
    if len(markers) == 1:
        for verdict in ('SECURE', 'VULNERABLE'):
            if final == f'APPSEC_STATUS: {verdict}':
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
        if not os.environ.get("SSDLC_SUPERVISED_RUN_ID") and global_env.exists():
            load_env_file(global_env, override=False)

        # 2. Load project-specific .ssdlc/project.env (takes precedence)
        if not os.environ.get("SSDLC_SUPERVISED_RUN_ID"):
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
            candidates = [Path.home() / ".codex" / "auth.json"]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                candidates.append(Path("/root/.codex/auth.json"))
            host_auth = next((p for p in candidates if safe_is_file(p) and p != (self.codex_home / "auth.json")), None)
            proj_auth = self.codex_home / "auth.json"
            if host_auth and (not proj_auth.exists() or host_auth.stat().st_mtime > proj_auth.stat().st_mtime):
                shutil.copy2(host_auth, proj_auth)
                os.chmod(proj_auth, 0o600)

        # Auto-inherit host Claude auth by default if present (unless set to false)
        if os.environ.get("SSDLC_INHERIT_HOST_CLAUDE_AUTH", "true").lower() == "true":
            candidates_claude = [Path.home() / ".claude.json"]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                candidates_claude.append(Path("/root/.claude.json"))
            host_claude_json = next(
                (p for p in candidates_claude if safe_is_file(p) and p != (self.claude_home / ".claude.json")), None
            )
            if host_claude_json:
                for dest in (self.claude_home / ".claude.json", self.runtime_dir / ".claude.json"):
                    if not dest.exists() or host_claude_json.stat().st_mtime > dest.stat().st_mtime:
                        shutil.copy2(host_claude_json, dest)
                        os.chmod(dest, 0o600)
            host_claude_cred = Path.home() / ".claude" / ".credentials.json"
            proj_claude_cred = self.claude_home / ".credentials.json"
            if safe_is_file(host_claude_cred) and (
                not proj_claude_cred.exists()
                or host_claude_cred.stat().st_mtime > proj_claude_cred.stat().st_mtime
            ):
                shutil.copy2(host_claude_cred, proj_claude_cred)
                os.chmod(proj_claude_cred, 0o600)

        # Auto-inherit git user identity from host ~/.gitconfig
        gitconfig_candidates = [Path.home() / ".gitconfig"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            gitconfig_candidates.append(Path("/root/.gitconfig"))
        for hg in gitconfig_candidates:
            if safe_is_file(hg) and hg != (self.runtime_dir / ".gitconfig"):
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
        details: Optional[Dict[str, Any]] = None,
    ):
        self.details = details
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
        result = {
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

        if self.details is not None:
            result["details"] = self.details
        return result


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
        codex_candidates = [Path.home() / ".codex" / "auth.json"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            codex_candidates.append(Path("/root/.codex/auth.json"))
        return bool(
            any(safe_is_file(p) for p in codex_candidates)
            or os.environ.get("SSDLC_INHERIT_HOST_CODEX_AUTH", "true").lower() == "true"
        ) and bool(shutil.which("codex"))

    @classmethod
    def has_agy(cls) -> bool:
        agy_candidates = [
            Path.home() / ".local" / "bin" / "agy",
            Path("/opt/host-bin/agy"),
        ]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            agy_candidates.append(Path("/root/.local/bin/agy"))
        return bool(
            any(safe_exists(p) for p in agy_candidates)
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


def resolve_review_entry(entry: str, default_engine: str = "openrouter") -> Tuple[str, str]:
    """Resolve the qualified/unqualified rule shared by both review paths."""
    return tuple(entry.split(":", 1)) if ":" in entry and not entry.startswith("http") else (default_engine, entry)


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
            f"{repo_root}:{repo_root}:" + ("rw" if writable_worktree is not None and writable_worktree.resolve() == repo_root else "ro"),
            "-v",
            f"{ssdlc_home}:/opt/ssdlc:ro",
        ]
        if os.environ.get('SSDLC_SUPERVISED_RUN_ID'):
            docker_cmd[6:6] = ['--label', 'ssdlc.run_id=' + os.environ['SSDLC_SUPERVISED_RUN_ID'],
                               '--label', 'ssdlc.project=' + os.environ['SSDLC_SUPERVISED_PROJECT_KEY']]
        # Linked checkouts point outside their source directory for Git objects
        # and worktree metadata. Keep that metadata readable, with the same
        # read-only protection as a normal checkout's .git directory.
        git_mounts = set()
        for checkout in (repo_root, ssdlc_home):
            if (checkout / '.git').is_file():
                common = Path(git(checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
                if common not in git_mounts:
                    docker_cmd.extend(['-v', f'{common}:{common}:ro'])
                    git_mounts.add(common)
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
            if (cwd.resolve() != repo_root and
                    (writable_worktree is None or not cwd.resolve().is_relative_to(writable_worktree.resolve()))):
                docker_cmd.extend(["-v", f"{cwd.resolve()}:{cwd.resolve()}:ro"])
            if verification_scratch is not None:
                docker_cmd.extend(["-v", f"{verification_scratch}:{verification_scratch}:rw"])
            for key, value in (verification_env or {}).items():
                docker_cmd.extend(["-e", f"{key}={value}"])
        if not str(repo_root).startswith("/tmp/"):
            docker_cmd.extend(["--tmpfs", "/tmp:rw,exec,size=512m"])
        if safe_exists(Path("/usr/local/bin/codex")):
            docker_cmd.extend(["-v", "/usr/local/bin/codex:/opt/host-bin/codex:ro"])
        host_agy = Path.home() / ".local" / "bin" / "agy"
        if safe_exists(host_agy):
            docker_cmd.extend(["-v", f"{host_agy}:/opt/host-bin/agy:ro"])

        # Auto-mount host subscriptions into container as read-only (zero host pollution)
        # Claude Max auth
        host_claude_json = Path.home() / ".claude.json"
        if pass_ai_keys and safe_is_file(host_claude_json):
            docker_cmd.extend(["-v", f"{host_claude_json}:/root/.claude.json:ro"])
        host_claude_cred = Path.home() / ".claude" / ".credentials.json"
        if pass_ai_keys and safe_is_file(host_claude_cred):
            docker_cmd.extend([
                "-v", f"{host_claude_cred}:/root/.claude/.credentials.json:ro",
                "--tmpfs", "/root/.cache/claude-cli-nodejs:rw",
                "--tmpfs", "/root/.claude/backups:rw",
                "--tmpfs", "/root/.claude/cache:rw",
                "--tmpfs", "/root/.claude/sessions:rw",
            ])

        # Codex Plus/Pro auth
        host_codex_auth = Path.home() / ".codex" / "auth.json"
        if pass_ai_keys and safe_is_file(host_codex_auth):
            docker_cmd.extend(["-v", f"{host_codex_auth}:/root/.codex/auth.json:ro"])
        host_codex_ver = Path.home() / ".codex" / "version.json"
        if pass_ai_keys and safe_is_file(host_codex_ver):
            docker_cmd.extend(["-v", f"{host_codex_ver}:/root/.codex/version.json:ro"])

        # agy Gemini Flash auth & ephemeral tmpfs mounts
        host_gemini = Path.home() / ".gemini"
        if auth_profile == "agy":
            # AGY is an AI role, not an untrusted verifier. Give it only its own
            # copied OAuth store, writable for refresh without touching host auth.
            host_token = host_gemini / "antigravity-cli" / "antigravity-oauth-token"
            agy_home = runtime_dir / "agy_auth"
            token = agy_home / "antigravity-cli" / "antigravity-oauth-token"
            if not safe_is_file(host_token) and not safe_is_file(token):
                from ssdlc_recovery import NotDispatched
                raise NotDispatched('AUTH_MISSING', 'AGY authentication is unavailable; configure host login before selecting agy')
            token.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(agy_home, 0o700)
            os.chmod(token.parent, 0o700)
            if safe_is_file(host_token) and (not token.exists() or host_token.stat().st_mtime > token.stat().st_mtime):
                shutil.copy2(host_token, token)
            os.chmod(token, 0o600)
            docker_cmd.extend(["-v", f"{agy_home}:/root/.gemini:rw"])
        elif pass_ai_keys and safe_exists(host_gemini):
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
            if wt_resolved != repo_root:
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
        limits = getattr(_dispatch_local, 'limits', None)
        if limits is not None:
            from ssdlc_recovery import RecoveryStop, NotDispatched
            try:
                timeout = min(timeout, limits.remaining(), limits._load(repair=False)[0]['policy']['invocation_timeout_seconds'])
            except RecoveryStop as exc:
                raise NotDispatched(exc.category, 'Role command was not launched: ' + str(exc)) from None
        cwd = Path(cwd)
        resource_root = EngineRunner._find_repo_root(cwd)
        docker = cmd[:2] == ["docker", "run"] and "--name" in cmd
        name = cmd[cmd.index("--name") + 1] if docker else None
        timed_out = False
        cleanup_failed = False
        proc = None
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                                  text=True, timeout=timeout, input=input)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            if name:
                try:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=True)
                    absence = subprocess.run(['docker', 'ps', '-a', '--filter', 'name=^/' + name + '$',
                                              '--format', '{{.Names}}'], capture_output=True, text=True, timeout=15, check=True)
                    if absence.stdout.strip():
                        cleanup_failed = True
                except (OSError, subprocess.SubprocessError):
                    # Cleanup failure must not reveal the original credential-bearing
                    # exception through an implicit chained traceback.
                    cleanup_failed = True
            # Docker argv contains AI credentials passed with -e. Never expose it
            # through exception messages, repr(), chained tracebacks or state.json.
            if name and not cleanup_failed:
                from ssdlc_recovery import QuiescentTimeout
                raise QuiescentTimeout(timeout, exc.output, exc.stderr,
                    {'container': name, 'removed': True, 'absence_confirmed': True}) from None
            raise subprocess.TimeoutExpired(
                ["ssdlc-role", name or "local"], timeout,
                output=exc.output, stderr=exc.stderr,
            ) from None
        except FileNotFoundError:
            from ssdlc_recovery import NotDispatched
            raise NotDispatched('ENVIRONMENT_BLOCKED', 'Role executable unavailable; command not launched') from None
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
                path = resource_root / ".ssdlc/.runtime/resources" / (name + ".json")
                failed = any(int(metrics.get(key, "0")) > 0 for key in
                             ("pids.events.max", "memory.events.oom", "memory.events.oom_kill"))
                atomic_json(path, {"container": name, "cwd": str(cwd), "timed_out": timed_out,
                                   "cleanup_failed": cleanup_failed,
                                   "exit_code": None if proc is None else proc.returncode,
                                   "telemetry_available": bool(metrics), "counters": metrics,
                                   "resource_failure": failed, **({"run_id": os.environ["SSDLC_SUPERVISED_RUN_ID"]}
                                       if os.environ.get("SSDLC_SUPERVISED_RUN_ID") else {})})
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
        verification_worktree: Optional[Path] = None,
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
                    writable_worktree=verification_worktree,
                )
                env = None
            else:
                if verification_worktree is not None:
                    raise DevelopmentBlocked('ENVIRONMENT_BLOCKED', 'Disposable verification requires Docker; local fallback is refused')
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
                    out, err, rc = EngineRunner.run_sandboxed_cmd(
                        cmd, cwd, remaining, _resource_retry=False, verification_worktree=verification_worktree)
                    return out, f"Environment resource retry; first-attempt evidence: {proc.resource_evidence}\n" + err, rc
                return proc.stdout or "", (proc.stderr or "") + f"\nENVIRONMENT_RESOURCE: {proc.resource_evidence}", 125
        return proc.stdout or "", proc.stderr or "", proc.returncode

    @staticmethod
    @_supervised_dispatch
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
            if getattr(_dispatch_local, 'limits', None) is not None:
                from ssdlc_recovery import NotDispatched
                raise NotDispatched('AUTH_MISSING', 'Bounded API route cannot silently fall back to another provider')
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

        limits = getattr(_dispatch_local, 'limits', None)
        if limits is not None and not os.environ.get('SSDLC_SUPERVISED_RUN_ID'):
            from ssdlc_recovery import NotDispatched
            raise NotDispatched('LIMIT_REACHED', 'Bounded HTTP inference requires the detached supervisor hard deadline')
        from ssdlc_recovery import RecoveryStop, NotDispatched
        try:
            http_timeout = min(300, limits.remaining()) if limits is not None else 300
        except RecoveryStop as exc:
            from ssdlc_recovery import NotDispatched
            raise NotDispatched(exc.category, 'HTTP inference was not launched: ' + str(exc)) from None
        req = urllib.request.Request(url, data=req_bytes, headers=headers, method="POST")
        attempts = 1 if limits is not None else 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                if limits is not None:
                    raise RecoveryStop('OUTCOME_UNCERTAIN', 'Inference transport has no proven non-dispatch/idempotency; no automatic replay') from None
                retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in (429, 503, 504)
                if not retryable or attempt + 1 == attempts:
                    raise
                import random
                time.sleep(2 ** attempt + random.uniform(0, 1))

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
    @_supervised_dispatch
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
    @_supervised_dispatch
    def run_codex(
        prompt: str,
        cwd: Path,
        sandbox: str = "read-only",
        model: str = "",
        reasoning_effort: str = "medium",
        use_openrouter: bool = False,
        writable_worktree: Optional[Path] = None,
        require_container: bool = False,
    ) -> Tuple[str, int]:
        use_container = EngineRunner._should_spawn_role_container()
        if require_container and not use_container:
            from ssdlc_recovery import NotDispatched
            raise NotDispatched('ENVIRONMENT_BLOCKED', 'Developer Docker runtime is unavailable; local fallback is refused')
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
    @_supervised_dispatch
    def run_claude(
        prompt: str,
        cwd: Path,
        read_only: bool = True,
        use_openrouter: bool = False,
        writable_worktree: Optional[Path] = None,
        model: str = "",
        reasoning_effort: str = "medium",
        max_budget_usd: float = 0.0,
        require_container: bool = False,
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

        use_container = EngineRunner._should_spawn_role_container()
        if require_container and not use_container:
            from ssdlc_recovery import NotDispatched
            raise NotDispatched('ENVIRONMENT_BLOCKED', 'Developer Docker runtime is unavailable; local fallback is refused')
        if use_container:
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
    @_supervised_dispatch
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
        self._registry_lock = threading.RLock()
        self._gate_terminal = None
        self.repo_root = repo_root.resolve()
        self.isolation = ProjectRuntimeIsolation(self.repo_root)
        self.iso_meta = self.isolation.activate()
        self.project_id = self.iso_meta["project_id"]
        self.ticket_id = ticket_id

        self.ssdlc_root = runtime_root(self.repo_root / ".ssdlc" / ticket_id)
        self.ticket_lock_path = self.repo_root / '.ssdlc' / ticket_id / 'ticket.lock'
        if self.ssdlc_root != self.repo_root / '.ssdlc' / ticket_id:
            from ssdlc_reconciliation import authorized
            if not authorized(self.repo_root / '.ssdlc' / ticket_id, self.ssdlc_root):
                raise TaskError('I1 continuation permits verification only; use continuation-verify. Model dispatch needs I2 authorization.')
            if not (self.ssdlc_root / 'state.json').is_file():
                raise DevelopmentBlocked('INTEGRITY', 'Authorized execution state is missing; initialization is forbidden')
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

    def preview_bounce(self, phase: str, attempt: int) -> dict:
        """Host-only preview of one exact task gate; never run pipeline roles."""
        counters = getattr(self, 'preview_counters', None)
        if counters is None:
            counters = self.preview_counters = dict(created=0, reused=0, recovered=0,
                                                    blocked_by_reason={})
        result = dict(status='blocked', execution_authorized=False, blockers=[])
        try:
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', self.ticket_id)
                    or phase not in bounce.PHASES or type(attempt) is not int or attempt < 1):
                raise bounce.BounceError('malformed_record')
            # Acquire before loading. Do not initialize or migrate legacy state.
            bounce._safe_path(self.ssdlc_root, 'ticket.lock')
            with ticket_lock(getattr(self, 'ticket_lock_path', self.ssdlc_root / 'ticket.lock')):
                self.state = json.loads(bounce._safe_path(self.ssdlc_root, 'state.json').read_text())
                initial = bounce.read_evidence(root=self.ssdlc_root, state=self.state,
                    repo=self.repo_root, worktree=self.worktree_dir, phase=phase, attempt=attempt)
                if initial['status'] != 'verified':
                    result = {k: initial[k] for k in ('status', 'execution_authorized', 'blockers')}
                else:
                    controller_root = Path(__file__).resolve().parent
                    if controller_root == self.worktree_dir.resolve():
                        raise bounce.BounceError('controller_unpinned')
                    contract_ref = '00-recon/controller-contract.json'
                    contract_path = bounce._safe_path(self.ssdlc_root, contract_ref)
                    meta = self.state['artifacts'].get(contract_ref)
                    if not meta or not contract_path.is_file() or file_hash(contract_path) != meta['sha256']:
                        raise bounce.BounceError('controller_integrity')
                    controller = json.loads(contract_path.read_text())
                    assert_controller(controller_root, controller)
                    repo_before = (git(self.repo_root, 'rev-parse', 'HEAD'),
                                   git(self.repo_root, 'status', '--porcelain'),
                                   git(self.repo_root, 'diff', '--binary', 'HEAD'))
                    frozen = bounce.preview_snapshot(self.ssdlc_root, self.state)
                    # The revision binds consumed evidence, not unrelated phases
                    # that may register their own outputs between previews.
                    sources = dict(initial['source_hashes'])
                    sources[contract_ref] = meta['sha256']

                    def verify():
                        fresh = json.loads(bounce._safe_path(self.ssdlc_root, 'state.json').read_text())
                        assert_controller(controller_root, controller)
                        if repo_before != (git(self.repo_root, 'rev-parse', 'HEAD'),
                                           git(self.repo_root, 'status', '--porcelain'),
                                           git(self.repo_root, 'diff', '--binary', 'HEAD')):
                            raise bounce.BounceError('source_changed')
                        if bounce.preview_snapshot(self.ssdlc_root, fresh) != frozen:
                            raise bounce.BounceError('snapshot_mismatch')
                        evidence = bounce.read_evidence(root=self.ssdlc_root, state=fresh,
                            repo=self.repo_root, worktree=self.worktree_dir, phase=phase, attempt=attempt)
                        if evidence != initial:
                            raise bounce.BounceError('postcheck_failed')

                    proposal = bounce.plan_bounce(repo=self.repo_root, artifacts=self.ssdlc_root,
                        plan=initial['plan'], requirements_text=initial['requirements_text'],
                        quality=initial['quality'], reviewed_commit=initial['reviewed_commit'],
                        evaluations=initial['evaluations'], phase=phase, attempt=attempt)
                    result = bounce.publish_preview(root=self.ssdlc_root, state=self.state,
                        proposal=proposal, sources=sources, verify=verify,
                        record_artifact=self.record_artifact)
                    counters[result['publication']] += 1
        except bounce.BounceError as exc:
            result = dict(status='blocked', execution_authorized=False, blockers=[{'code': exc.code}])
        except bounce.FindingsError:
            result = dict(status='blocked', execution_authorized=False, blockers=[{'code': 'report_invalid'}])
        except ProtocolError as exc:
            code = 'contradictory_evidence' if exc.code == 'binding_gate' else 'report_invalid'
            result = dict(status='blocked', execution_authorized=False, blockers=[{'code': code}])
        except (TaskError, PlanError, OSError):
            result = dict(status='blocked', execution_authorized=False, blockers=[{'code': 'integrity_mismatch'}])
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            result = dict(status='blocked', execution_authorized=False, blockers=[{'code': 'malformed_record'}])
        plan = result.get('plan', {})
        reasons = sorted({b['code'] for b in result.get('blockers', plan.get('blockers', []))})
        for code in reasons:
            counts = counters['blocked_by_reason']
            counts[code] = counts.get(code, 0) + 1
        # Selectors are validated before logging. No exception text or finding text.
        logging.getLogger(__name__).info(
            'bounce-preview ticket=%s phase=%s attempt=%s revision=%s status=%s reasons=%s roots=%d dependents=%d blockers=%d',
            self.ticket_id if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', self.ticket_id) else 'invalid',
            phase if phase in bounce.PHASES else 'invalid', attempt if type(attempt) is int else 0,
            result.get('revision', '-'), result['status'], ','.join(reasons),
            len(plan.get('repair_roots', [])), len(plan.get('revalidation_tasks', [])), len(reasons))
        return result

    def _ensure_quality_contract(self) -> Dict[str, Any]:
        target = self.dir_recon / "quality-contract.json"
        if target.exists():
            meta = self.state.get("artifacts", {}).get("00-recon/quality-contract.json")
            if not meta or sha256_file(target) != meta["sha256"]:
                raise PlanError("Quality snapshot is unregistered or changed; explicit migration required")
            return json.loads(target.read_text())
        if "00-recon/quality-contract.json" in self.state.get("artifacts", {}):
            raise PlanError("Registered quality contract disappeared; refusing regeneration")
        profile = bootstrap.load_profile(self.repo_root)
        binding = bootstrap.decide(self.repo_root, self.state['base_commit'], profile) if profile is not None else None
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
            checks = _discover_checks(self.repo_root)
            if binding:
                checks = bootstrap.profile_checks(binding["profile_id"])
            if not checks:
                raise PlanError("No project checks discovered. Configure .ssdlc/quality-contract.json before starting task execution")
            contract = {"schema_version": "1.0", "contract_revision": 1, "base_commit": self.state["base_commit"],
                        "sources": sources, "standards": standards, "checks": checks, "baseline": [],
                        "limitations": ["Automatic discovery covers Python tests, configured Ruff/Mypy and npm test/lint/typecheck; provide an explicit contract for other tools."], "exceptions": []}
        if binding:
            contract['bootstrap'] = binding
        for check in contract["checks"]:
            if check.get("required"):
                check["candidate_proof"] = True
        if sum(c.get("required", False) and c.get("candidate_proof", False) for c in contract["checks"]) > candidate_reports.MAX_CANDIDATE_CHECKS:
            raise PlanError("More than 32 candidate checks cannot be sealed")
        contract.setdefault("limitations", []).append(
            "QA shell commands are recorded separately and are not typed-check equivalents")
        validate_document("quality-contract", contract)
        if binding:
            atomic_json(target, contract)
            self.record_artifact("00-recon/quality-contract.json", target)
            return contract
        baseline_dir = self.dir_recon / "baseline"
        baseline_dir.mkdir(exist_ok=True)
        print(f"[baseline] Running {len(contract['checks'])} initial check(s) before 00-recon; "
              "output is captured until each check completes.", flush=True)
        for check in contract["checks"]:
            self.check_budget_circuit_breaker()
            cwd = confined(self.repo_root, check["cwd"])
            print(f"[baseline] {check['check_id']}: running (timeout {check['timeout_sec']}s)...", flush=True)
            started = time.monotonic()
            out, err, rc = EngineRunner.run_sandboxed_cmd(check["argv"], cwd, check["timeout_sec"])
            log = baseline_dir / f"{check['check_id']}.log"
            log.write_text(out + "\n" + err)
            contract["baseline"].append({"check_id": check["check_id"], "exit_code": rc,
                                         "log_ref": str(log.relative_to(self.ssdlc_root)), "sha256": sha256_file(log)})
            empty_suite = test_suite_is_empty(out, err)
            status = 'FAILED' if rc or empty_suite else 'PASSED'
            print(f"[baseline] {check['check_id']}: {status} in {time.monotonic() - started:.1f}s "
                  f"(exit {rc}{', empty test suite' if empty_suite else ''}); log: {log}", flush=True)
            if rc or empty_suite:
                raise PlanError(f"Baseline check {check['check_id']} failed or ran no tests; inspect {log}")
        atomic_json(target, contract)
        self.record_artifact("00-recon/quality-contract.json", target)
        return contract

    def _ensure_task_plan(self, review_repairs=None, recover_review=False) -> Dict[str, Any]:
        self.state["task_breakdown"] = True
        self.state.update(current_phase='02-design', current_step='task-plan')
        self._save_state()
        quality = self._ensure_quality_contract()
        plan_path = self.dir_design / "plan.json"
        if self.state.get('execution_reconciliation'):
            from ssdlc_reconciliation import authorized
            if not authorized(self.repo_root / '.ssdlc' / self.ticket_id, self.ssdlc_root):
                raise PlanError('Execution reconciliation is not selected')
            # Admission reviewed the amended plan and retained old design proofs
            # as history. Do not demand/recreate an old global design gate.
            meta = self.state.get('artifacts', {}).get('02-design/plan.json')
            if not meta or not plan_path.is_file() or file_hash(plan_path) != meta['sha256']:
                raise PlanError('Reconciled plan changed; regeneration is forbidden')
            plan = json.loads(plan_path.read_text())
            if plan['ticket_id'] != self.ticket_id or plan['base_commit'] != self.state['base_commit']:
                raise PlanError('Reconciled plan identity changed')
            TaskPlanValidator(self.repo_root, (self.dir_po / 'requirements.md').read_text(),
                              quality, self.ssdlc_root).validate(plan)
            return plan
        design_session = self.dir_design / 'design-session/session.json'
        if design_session.exists():
            name = '02-design/design-session/session.json'
            meta = self.state.get('artifacts', {}).get(name)
            gates = [g for g in self.state.get('gate_history', []) if g['gate'] == 'PAIR_DESIGN_GATE']
            session = json.loads(design_session.read_text())
            if (not meta or file_hash(design_session) != meta['sha256'] or not gates or not gates[-1]['passed']
                    or not session['rounds'] or session['rounds'][-1]['status'] != 'APPROVED'):
                raise PlanError('Persistent pair design has not been independently approved')
            for filename in ('adr.md', 'threat-model.md'):
                path = self.dir_design / filename
                meta = self.state.get('artifacts', {}).get('02-design/' + filename)
                if not meta or not path.is_file() or file_hash(path) != meta['sha256']:
                    raise PlanError('Approved design projection changed or disappeared')
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
        controller = self._ensure_controller_contract()
        engine, model, effort, _ = SquadDispatcher.dispatch_role(
            "02-design-arch", self.state.get("risk_tier", "M"), self.state.get("dev_profile", "ALGORITHMIC")
        )
        context = {"requirements": requirements,
                   "requirement_catalogue": validator.requirement_catalogue,
                   "available_contract_refs": validator.available_contract_refs(),
                   "execution_contract": controller,
                   "adr": self._read_optional(self.dir_design / "adr.md", "Tier S: lightweight plan"),
                   "threat_model": self._read_optional(self.dir_design / "threat-model.md", "See SEC requirements"),
                   "quality_contract": quality, "repo_context": self._gather_repo_context(),
                   "ticket_id": self.ticket_id, "base_commit": self.state["base_commit"]}
        schema = (Path(__file__).parent / "schemas" / "plan.schema.json").read_text()
        prompt = ("Produce only a JSON task plan matching this schema. Use exact literal file paths, cover EVERY AC and SEC, "
                  "and reference all required quality checks and applicable standards. Each requirement must map to executable checks. "
                  "Use kind=task and automated verification only: this bootstrap has no subgoal or review_only engine. "
                  "Preserve the supplied base_commit, ticket_id and quality_contract_ref. Do not reduce scope or security constraints.\n" + schema)
        prompt += "\nTier S requires exactly one plan node covering all requirements."
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
            if self.state.get("risk_tier") == "S" and len(plan["tasks"]) != 1:
                raise PlanError("Tier S requires exactly one plan node")
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
        review_schema = (Path(__file__).parent / "schemas" / "task-plan-review.schema.json").read_text()
        review_prompt = (
            "Review the entire plan against approved requirements and design in one comprehensive pass. "
            "Return only JSON matching the supplied review schema. Keep finding_id stable across revisions; "
            "explicitly resolve or retain every prior open blocker using evidence from the current plan. "
            "Open blocking findings need an exact approved AC/SEC, standard or required quality-check reference, "
            "concrete evidence and a bounded requested change. "
            "Resolved structural/schema diagnostics may have empty obligation references; do not invent IDs to annotate them. "
            "Optional improvements are ADVISORY and cannot block approval. Do not introduce new scope or require "
            "implementation detail beyond the approved behavior. Identify new findings as INTRODUCED_BY_REVISION or "
            "MISSED_EARLIER and explain why in evidence. Review planned behavioral assertions, not code that does not exist yet. "
            "Reject label-only checks and tests deferred beyond the task implementing the behavior. "
            "Use the supplied execution_contract for controller prerequisites; never invent environment variables. "
            "Infrastructure prerequisites belong to ENVIRONMENT_PREREQUISITE, not speculative target code changes.\n" + review_schema)
        prompt += ("\nUse repair.history to preserve all earlier fixes. Avoid unrelated rewrites during repair. "
                   "Honor execution_contract: the host harness captures and verifies the executing controller. "
                   "Do not create controller-capture tasks or assume undeclared environment variables. "
                   "For crash safety, enumerate persistence boundaries from the approved design and specify "
                   "the fault injection and expected recovery for each; justify equivalence when grouping boundaries.")
        def review(plan, history):
            from ssdlc_amendments import fixture_impact
            impact = fixture_impact(self.repo_root, sorted({p for t in plan['tasks'] for p in t['target_files']}))
            impact_path = self.dir_design / 'task-planning' / ('fixture-impact-' + digest(plan) + '.json')
            if impact_path.exists() and json.loads(impact_path.read_text()) != impact:
                raise PlanError('Fixture impact changed on the same plan; explicit revision required')
            atomic_json(impact_path, impact)
            self.record_artifact(str(impact_path.relative_to(self.ssdlc_root)), impact_path)
            t0 = time.time()
            out, cost, tin, tout, trace = EngineRunner.invoke_role(
                rev_engine, rev_model,
                review_prompt,
                json.dumps({"context": context, "plan": plan, "review_history": history, "fixture_impact": impact}), self.repo_root,
                self.project_id, self.ticket_id, "02-design-task-plan-review", reasoning_effort=rev_effort,
            )
            self.record_ledger(CostAndTelemetryEntry(
                "02-design-task-plan-review", "Plan Reviewer", rev_engine, rev_model, "REVIEWED",
                time.time() - t0, cost, tin, tout, trace, rev_effort))
            return out
        repair = PlanRepair(self.dir_design,
                            {"context": context, "schema": schema, "prompt": prompt, "review_prompt": review_prompt,
                             "planner": [engine, model, effort], "reviewer": [rev_engine, rev_model, rev_effort]},
                            validate, generate, review, self.check_budget_circuit_breaker, review_repairs=review_repairs,
                            structured_reviews=True)
        try:
            if recover_review:
                repair.recover_rejected_review()
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

    def _candidate_fault(self, point):
        """Inert persistence-boundary hook for offline crash tests."""

    def _history_fault(self, point):
        """Inert history persistence-boundary hook for offline recovery tests."""

    def _build_history_snapshot(self, phase, attempt):
        cr = candidate_reports
        quality = json.loads((self.dir_recon / 'quality-contract.json').read_text())
        if not cr.convergence_enabled(quality):
            return None
        ref = f'history/{phase}-attempt-{attempt}.json'
        path = self.ssdlc_root / ref
        try:
            cr.reconcile_candidate_store(self.ssdlc_root, self.state['artifacts'], self._register_new)
            if path.exists():
                snapshot = cr.validate_history_snapshot(cr.history_read(self.ssdlc_root, ref, self.state['artifacts']))
                if snapshot['phase'] != phase or snapshot['attempt'] != attempt:
                    raise ValueError('history identity mismatch')
                return snapshot['block']
            self._history_fault('history.before_snapshot')
            plan = self._gate_plan()
            snapshot = cr.history_snapshot(self.ssdlc_root, phase, attempt, self.state['artifacts'],
                [t['task_id'] for t in plan['tasks']],
                git(self.worktree_dir, 'diff', '--name-only', self.state['base_commit'], 'HEAD').splitlines(),
                dict(commit=self.state['task_verified_head'], tree=git(self.worktree_dir, 'rev-parse', 'HEAD^{tree}')))
            cr.validate_history_snapshot(snapshot)
            write_or_verify(path, canon(snapshot), root=self.ssdlc_root)
            self._history_fault('history.after_snapshot_before_register')
            self._register_new(ref, path)
            self._history_fault('history.after_register_before_roster')
            return json.loads(canon(snapshot))['block']
        except (TaskError, ValueError, KeyError, OSError) as exc:
            raise GateTerminalError('integrity_blocked', '', 'history snapshot invalid') from exc

    def _diagnosis_fault(self, point):
        """Inert persistence-boundary hook for diagnosis recovery tests."""

    def _diagnose_gate(self, requests, results, candidate, wt, phase):
        if not candidate_reports.convergence_enabled(candidate.quality):
            return
        tree = git(wt, 'rev-parse', 'HEAD^{tree}')
        for request, result in zip(requests, results):
            if result.assessment is None:
                continue
            findings = {f.id: f for f in result.assessment.findings}
            for finding_id in sorted(result.assessment.diagnosis_required):
                finding = findings[finding_id].to_dict()
                provider = dict(engine=request.engine, model=request.model, reasoning_effort=request.reasoning_effort)
                identity = request_identity(request)['identity']
                def invoke(system, user):
                    return EngineRunner.invoke_role(request.engine, request.model,
                        system + '\n' + UNTRUSTED_DATA_INSTRUCTION,
                        user,
                        wt, self.project_id, self.ticket_id, f'{phase}-diag', sandbox='read-only',
                        reasoning_effort=request.reasoning_effort)
                def ledger(details):
                    if details['transition'] == 'result':
                        previous = [entry for entry in self.state.get('ledger', [])
                                    if entry.get('details', {}).get('diagnosis_request_id') == details['diagnosis_request_id']
                                    and entry.get('details', {}).get('transition') == 'result']
                        if previous:
                            expected = (details['result_sha256'], round(details['cost_usd'], 6),
                                        details['tokens_in'], details['tokens_out'],
                                        details['producer']['engine'], details['producer']['model'])
                            if len(previous) != 1 or (previous[0]['details'].get('result_sha256'),
                                    previous[0]['cost_usd'], previous[0]['tokens_in'], previous[0]['tokens_out'],
                                    previous[0]['engine'], previous[0]['model']) != expected:
                                raise TaskError('Diagnosis ledger differs from its immutable result')
                            return
                    producer = details.get('producer', provider)
                    self.record_ledger(CostAndTelemetryEntry(f'{phase}-diag', 'Diagnosis', producer['engine'],
                        producer['model'], details['transition'], details.get('duration_sec', 0),
                        cost_usd=details.get('cost_usd', 0), tokens_in=details.get('tokens_in', 0),
                        tokens_out=details.get('tokens_out', 0), reasoning_effort=producer['reasoning_effort'],
                        details=details))
                candidate_reports.run_diagnosis(self.ssdlc_root, gate=request.gate, finding=finding,
                    reviewed_commit=request.reviewed_commit, tree=tree, evaluation_identity=identity,
                    attempt=request.attempt, candidate_records=[r['record_id'] for r in candidate.records],
                    provider=provider, invoke=invoke, register=self._register_new, artifacts=self.state['artifacts'],
                    check_budget=self.check_budget_circuit_breaker, record_gate=self.record_gate,
                    ledger=ledger, fault=self._diagnosis_fault, candidate_block=candidate.block)
                self._history_fault('history.after_diagnosis_register')

    def _register_new(self, ref, path):
        with self._registry_lock:
            meta = self.state.get('artifacts', {}).get(ref)
            if meta:
                if not path.is_file() or file_hash(path) != meta['sha256']:
                    raise TaskError('Retained evidence artifact missing or changed')
            else:
                self.record_artifact(ref, path)

    def _controller_revision(self):
        ref = '00-recon/controller-contract.json'
        try:
            data = candidate_reports._candidate_read(self.ssdlc_root, ref, 'record')
            if hashlib.sha256(data).hexdigest() != self.state['artifacts'][ref]['sha256']:
                raise ValueError()
            snapshot = json.loads(data)
            assert_controller(Path(__file__).resolve().parent, snapshot)
            return {k: snapshot[k] for k in ('head', 'files_sha256')}
        except (TaskError, candidate_reports.ProtocolError, OSError, ValueError, KeyError):
            raise GateTerminalError('integrity_blocked', '', 'controller_unavailable')

    def _verification_environment(self, argv):
        container = EngineRunner._should_spawn_role_container()
        result = dict(mode='role_container' if container else 'local', platform=sys.platform, reuse_eligible=True)
        try:
            if container:
                probe = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', EngineRunner.IMAGE_NAME],
                                       capture_output=True, text=True, timeout=30)
                if probe.returncode or not re.fullmatch(r'sha256:[0-9a-f]{64}', probe.stdout.strip()):
                    raise ValueError()
                result.update(image_name=EngineRunner.IMAGE_NAME, image_id=probe.stdout.strip(),
                              limits=dict(pids=os.environ.get('SSDLC_CONTAINER_PIDS_LIMIT', '1024'),
                                          memory=os.environ.get('SSDLC_CONTAINER_MEMORY', '4g'),
                                          cpus=os.environ.get('SSDLC_CONTAINER_CPUS', '2')),
                              no_new_privileges=True,
                              verification_env=dict(PYTEST_ADDOPTS_base=os.environ.get('PYTEST_ADDOPTS', '')))
            else:
                tool = shutil.which(argv[0])
                if not tool or not Path(tool).is_file():
                    raise ValueError()
                names = ('PATH', 'PYTHONPATH', 'PYTHONHASHSEED', 'VIRTUAL_ENV', 'NODE_OPTIONS',
                         'NODE_PATH', 'LANG', 'LC_ALL', 'CI')
                result.update(python=dict(version=sys.version, executable_sha256=file_hash(Path(sys.executable))),
                              tool=dict(argv0=argv[0], resolved_realpath=str(Path(tool).resolve()), sha256=file_hash(Path(tool))),
                              env=dict({k: os.environ[k] for k in names if k in os.environ},
                                       PYTEST_ADDOPTS_base=os.environ.get('PYTEST_ADDOPTS', '')))
                # Interpreter and PATH hashes do not bind mutable site-packages or
                # other dependencies outside the source tree. Keep current proof,
                # but collect again for another gate rather than assume equivalence.
                result.update(reuse_eligible=False, reason='local_dependencies_unpinned')
        except (OSError, ValueError, subprocess.SubprocessError):
            result.update(reuse_eligible=False, reason='environment_identity_unavailable')
        return result

    def _candidate_privilege(self, wt):
        out, err, rc = EngineRunner.run_sandboxed_cmd(['id', '-u'], wt, 30)
        if rc != 0 or re.fullmatch(r'[0-9]+', out.strip()) is None:
            raise GateTerminalError('integrity_blocked', '', 'privilege_probe_malformed')
        return 'root' if int(out.strip()) == 0 else 'non-root'

    def _collect_candidate_evidence(self, gate, attempt, wt, quality):
        cr = candidate_reports
        if not cr.convergence_enabled(quality):
            return cr.CandidateEvidence(quality=quality)
        checks = [c for c in quality['checks'] if c.get('required') and c.get('candidate_proof') is True]
        if len(checks) > cr.MAX_CANDIDATE_CHECKS:
            raise GateTerminalError('integrity_blocked', '', 'too_many_candidate_checks')
        with self._registry_lock:
            cr.reconcile_candidate_store(self.ssdlc_root, self.state['artifacts'], self._register_new)
        privilege = self._candidate_privilege(wt)
        probe_ref = 'candidate-evidence/probes/' + uuid.uuid4().hex + '.json'
        probe_path = self.ssdlc_root / probe_ref
        cr.write_exclusive(probe_path, canon(dict(kind='privilege_probe', privilege_mode=privilege)), root=self.ssdlc_root)
        self._register_new(probe_ref, probe_path)
        source = dict(commit=git(wt, 'rev-parse', 'HEAD'), tree=git(wt, 'rev-parse', 'HEAD^{tree}'))
        if source['commit'] != self.state['task_verified_head'] or git(wt, 'status', '--porcelain'):
            raise GateTerminalError('integrity_blocked', '', 'candidate_source_unverified')
        for check in checks:
            if len(list((self.ssdlc_root / 'candidate-evidence/records').glob(check['check_id'] + '-*'))) > cr.MAX_RECORD_FILES_PER_CHECK:
                raise GateTerminalError('integrity_blocked', check['check_id'], 'record_store_overflow')
        snapshot_ref = f'candidate-evidence/catalogue/{gate}-attempt-{attempt}.json'
        snapshot_path = confined(self.ssdlc_root, snapshot_ref, protect=False)
        if snapshot_path.exists():
            try:
                data = cr._candidate_read(self.ssdlc_root, snapshot_ref, 'record')
                if cr._digest(data) != self.state['artifacts'][snapshot_ref]['sha256']:
                    raise ValueError()
                snapshot = json.loads(data)
                if cr._digest(snapshot['block'].encode()) != snapshot['block_sha256']:
                    raise ValueError()
                retained = []
                for check in checks:
                    ref = snapshot['record_refs'][check['check_id']]
                    expected = dict(source=source, controller=self._controller_revision(),
                                    quality_contract_sha256=self.state['artifacts']['00-recon/quality-contract.json']['sha256'],
                                    check={k: check[k] for k in ('check_id', 'argv', 'cwd', 'timeout_sec')},
                                    environment=self._verification_environment(check['argv']), privilege_mode=privilege)
                    record, rejection = cr.verify_candidate_record(self.ssdlc_root, ref, expected, self.state['artifacts'])
                    if rejection:
                        raise ValueError()
                    retained.append(record)
                result = cr.candidate_catalogue(self.ssdlc_root, quality, retained, [], self.state['artifacts'])
                if result.controls != snapshot['controls']:
                    raise ValueError()
                result.block = snapshot['block']
                result.decisions = snapshot['decisions']
                return result
            except (KeyError, ValueError, cr.ProtocolError):
                raise GateTerminalError('integrity_blocked', '', 'candidate_snapshot_mismatch')
        records, rejected, decisions = [], [], []
        for check in sorted(checks, key=lambda c: c['check_id']):
            command = {k: check[k] for k in ('check_id', 'argv', 'cwd', 'timeout_sec')}
            expected = dict(source=source, controller=self._controller_revision(),
                            quality_contract_sha256=self.state['artifacts']['00-recon/quality-contract.json']['sha256'],
                            check=command, environment=self._verification_environment(check['argv']), privilege_mode=privilege)
            paths = sorted((self.ssdlc_root / 'candidate-evidence/records').glob(check['check_id'] + '-*'))
            if len(paths) > cr.MAX_RECORD_FILES_PER_CHECK:
                raise GateTerminalError('integrity_blocked', check['check_id'], 'record_store_overflow')
            accepted, considered = [], []
            for path in paths:
                ref = str(path.relative_to(self.ssdlc_root))
                record, rejection = cr.verify_candidate_record(self.ssdlc_root, ref, expected, self.state['artifacts'])
                if rejection:
                    rejection = dict(record_id=path.stem[-32:], code=rejection['code'], fields=rejection['fields'])
                    rejected.append(rejection)
                    considered.append(cr.canon(rejection).decode()[:300])
                else:
                    accepted.append(record)
            if len({r['result']['status'] for r in accepted}) > 1:
                raise GateTerminalError('integrity_blocked', '', 'conflicting_records')
            reuse = bool(accepted and expected['environment']['reuse_eligible'])
            if reuse:
                record = min(accepted, key=lambda r: (r['provenance']['produced_at'], r['record_id']))
            else:
                self._controller_revision()
                self._candidate_fault('evidence.before_check')
                try:
                    out, err, rc = EngineRunner.run_sandboxed_cmd(check['argv'], confined(wt, check['cwd']), check['timeout_sec'])
                except subprocess.TimeoutExpired:
                    out, err, rc = '', 'Check timed out', 124
                self._candidate_fault('evidence.after_check_before_log')
                self._controller_revision()
                changed = source != dict(commit=git(wt, 'rev-parse', 'HEAD'), tree=git(wt, 'rev-parse', 'HEAD^{tree}')) or bool(git(wt, 'status', '--porcelain'))
                if not changed and hasattr(self, 'dir_dev'):
                    try:
                        self._verified_task_review_input((self.dir_dev / 'implementation.diff').read_text())
                    except TaskError:
                        changed = True
                if changed:
                    decision = dict(schema_version='1.0', kind='candidate_evidence_decision', gate=gate,
                                    attempt=attempt, check_id=check['check_id'], decision='invalidated',
                                    record_id='0'*32, binding_sha256=cr._digest(canon(expected)),
                                    considered=considered, considered_total=len(paths),
                                    declined='source_mutated_during_check')
                    self._write_candidate_decision(decision)
                    self.record_gate('CANDIDATE_CHECK_' + check['check_id'], False, 'source_mutated_during_check')
                    raise GateTerminalError('integrity_blocked', check['check_id'], 'Source changed during candidate check; proof discarded')
                nonce = uuid.uuid4().hex
                ref = f"candidate-evidence/records/{check['check_id']}-{nonce}.json"
                log_ref = f'candidate-evidence/logs/{nonce}.log'
                raw = (out + '\n' + err).encode()
                log = raw[-cr.LOG_STORE_MAX_BYTES:]
                empty = test_suite_is_empty(out, err)
                body = dict(expected, schema_version='1.0', kind='candidate_check_record', run_nonce=nonce,
                            result=dict(exit_code=rc, timed_out=rc == 124, empty_suite=empty,
                                        status='passed' if rc == 0 and not empty else 'failed'),
                            log=dict(ref=log_ref, sha256=hashlib.sha256(log).hexdigest(), stored_bytes=len(log),
                                     total_bytes=len(raw), truncated=len(raw) != len(log)),
                            provenance=dict(producer='orchestrator._collect_candidate_evidence', produced_at=utc_now(),
                                            artifact_path=ref, gate=gate, attempt=attempt))
                record = cr.register_candidate_record(self.ssdlc_root, body, log, self._register_new, self._candidate_fault)
            records.append(record)
            decision = dict(schema_version='1.0', kind='candidate_evidence_decision', gate=gate, attempt=attempt,
                            check_id=check['check_id'], decision='reused' if reuse else 'executed',
                            record_id=record['record_id'], binding_sha256=hashlib.sha256(canon(expected)).hexdigest(),
                            considered=considered, considered_total=len(paths))
            if not expected['environment']['reuse_eligible']:
                decision['declined'] = 'environment_identity_unavailable'
            self._write_candidate_decision(decision)
            decisions.append(decision)
        result = cr.candidate_catalogue(self.ssdlc_root, quality, records, rejected, self.state['artifacts'])
        result.decisions = decisions
        self._candidate_fault('evidence.after_decision_before_snapshot')
        snapshot = dict(binding_sha256=cr._digest(canon([r['record_id'] for r in records])), block=result.block, block_sha256=cr._digest(result.block.encode()), controls=result.controls,
                        decisions=decisions, record_refs={r['check']['check_id']: r['provenance']['artifact_path'] for r in records})
        write_or_verify(snapshot_path, canon(snapshot), root=self.ssdlc_root)
        self._register_new(snapshot_ref, snapshot_path)
        self._candidate_fault('evidence.after_snapshot_before_dispatch')
        return result

    def _write_candidate_decision(self, decision):
        ref = 'candidate-evidence/decisions/' + candidate_reports._digest(canon(decision))[:32] + '.json'
        path = self.ssdlc_root / ref
        write_or_verify(path, canon(decision), root=self.ssdlc_root)
        self._register_new(ref, path)

    def _verify_candidate_evidence(self, candidate, wt):
        for record in candidate.records:
            expected = {k: record[k] for k in ('check', 'environment', 'privilege_mode')}
            expected.update(source=dict(commit=git(wt, 'rev-parse', 'HEAD'), tree=git(wt, 'rev-parse', 'HEAD^{tree}')),
                            controller=self._controller_revision(),
                            quality_contract_sha256=file_hash(self.dir_recon / 'quality-contract.json'))
            _, rejection = candidate_reports.verify_candidate_record(self.ssdlc_root, record['provenance']['artifact_path'], expected, self.state['artifacts'])
            if rejection:
                raise TaskError('Verified checkpoint changed during review; approval withheld')

    def _ensure_controller_contract(self) -> dict:
        """Persist the host baseline before planning/development, never inside a task."""
        name = '00-recon/controller-contract.json'
        path = self.ssdlc_root / name
        root = Path(__file__).resolve().parent
        meta = self.state.get('artifacts', {}).get(name)
        if path.exists():
            if not meta or file_hash(path) != meta['sha256']:
                raise TaskError('Unregistered or changed controller contract')
            snapshot = json.loads(path.read_text())
            assert_controller(root, snapshot)
        else:
            if meta or (self.dir_dev / 'tasks/task-journal.jsonl').exists():
                raise TaskError('Missing controller contract; explicit revision required')
            snapshot = controller_snapshot(root)
            atomic_json(path, snapshot)
            self.record_artifact(name, path)
        return execution_contract(snapshot)

    @_bounded_entrypoint
    def resume_task_planning(self, review_repairs=None, recover_review=False) -> Path:
        """Resume only planning against sealed upstream artifacts; never execute code."""
        with ticket_lock(getattr(self, "ticket_lock_path", self.ssdlc_root / "ticket.lock")):
            self.state = self._load_or_init_state()
            _resume_plan_refusal(self.repo_root, self.ssdlc_root, self.state)
            self.state.pop("failure", None)
            self.state.update(current_phase="02-design", current_step="task-plan", status="IN_PROGRESS")
            self._save_state()
            try:
                self._ensure_task_plan(review_repairs=review_repairs, recover_review=recover_review)
            except Exception as exc:
                self.state.update(status="FAILED", failure={"phase": "task-plan", "message": str(exc)})
                self._save_state()
                raise
            self.state['status'] = 'PLAN_READY'
            self.state.pop('failure', None)
            self._save_state()
            return self.dir_design / 'plan.json'

    @_supervised_phase('03-dev')
    def run_phase_dev(self, recover_protocol_task=None) -> Tuple[Path, List[str]]:
        # Keep the native, bounded phase entrypoint while using one task engine.
        return self._run_task_development(recover_protocol_task)

    def _run_task_development(self, recover_protocol_task=None) -> Tuple[Path, List[str]]:
        plan = self._ensure_task_plan()
        controller = self._ensure_controller_contract()
        quality = json.loads((self.dir_recon / "quality-contract.json").read_text())
        wt = self._ensure_worktree()
        context = build_task_context(self.ssdlc_root)
        validator = TaskPlanValidator(self.repo_root, context["01-po/requirements.md"], quality, self.ssdlc_root)
        ordered = validator.validate(plan)
        runtime_environment = self._verification_environment(quality['checks'][0]['argv'])
        protected = {name: dict(meta) for name, meta in self.state.get("artifacts", {}).items()
                     if name.startswith(("00-recon/", "01-po/", "02-design/"))}
        def guard():
            assert_controller(Path(__file__).resolve().parent, controller['snapshot'])
            if self._verification_environment(quality['checks'][0]['argv']) != runtime_environment:
                raise DevelopmentBlocked('ENVIRONMENT_BLOCKED', 'Candidate execution environment changed; explicit reconciliation required')
            validator.validate(plan)
            for name, meta in protected.items():
                path = self.ssdlc_root / meta["path"]
                if not path.is_file() or sha256_file(path) != meta["sha256"]:
                    raise TaskError(f"Protected artifact changed or disappeared: {name}")
        def develop(task, attempt, prompt, workspace):
            if not EngineRunner._should_spawn_role_container():
                from ssdlc_recovery import NotDispatched
                raise NotDispatched('ENVIRONMENT_BLOCKED', 'Candidate isolation requires Docker role execution')
            engine, model, effort, reason = SquadDispatcher.dispatch_role(
                "03-dev", self.state.get("risk_tier", "M"), task["profile"], attempt=attempt,
            )
            t0 = time.time()
            use_or = os.environ.get("SSDLC_DEV_USE_OPENROUTER", "false").lower() == "true"
            cost, tin, tout = 0.0, 0, 0
            with _dispatch_context(task_id=task["task_id"], attempt=attempt, phase="03-dev"):
                if engine == "claude":
                    tier_budget = {"S": 1.0, "M": 2.5, "L": 8.0}.get(self.state.get("risk_tier", "M"), 2.5)
                    dev_budget = float(os.environ.get("SSDLC_DEV_BUDGET_USD") or tier_budget)
                    out, rc, cost, tin, tout = EngineRunner.run_claude(
                        prompt, cwd=workspace, read_only=False, use_openrouter=use_or,
                        writable_worktree=workspace, model=model, reasoning_effort=effort,
                        max_budget_usd=dev_budget,
                        require_container=True,
                    )
                elif engine == "codex":
                    out, rc = EngineRunner.run_codex(
                        prompt, cwd=workspace, sandbox="workspace-write", model=model,
                        reasoning_effort=effort, use_openrouter=use_or, writable_worktree=workspace,
                        require_container=True,
                    )
                else:
                    raise TaskError(f"Unsupported bootstrap developer engine: {engine}")
            self.record_ledger(CostAndTelemetryEntry(
                f"03-dev-{task['task_id']}-a{attempt}", "Task Developer", engine, model,
                "GENERATED" if rc == 0 else f"RC_{rc}", time.time() - t0, cost, tin, tout,
                f"{self.project_id}:{self.ticket_id}:{task['task_id']}", effort,
            ))
            return {"output": out, "exit_code": rc, "engine": engine, "model": model,
                    "effort": effort, "reason": reason, "quiescent": True}
        def verify(argv, cwd, timeout, workspace):
            if not EngineRunner._should_spawn_role_container():
                raise DevelopmentBlocked('ENVIRONMENT_BLOCKED', 'Candidate verification requires Docker role execution')
            return EngineRunner.run_sandboxed_cmd(argv, cwd, timeout, verification_worktree=workspace)
        limits = getattr(_dispatch_local, 'limits', None)
        def verification_budget():
            if limits is None:
                return self.check_budget_circuit_breaker()
            envelope, counters = limits._load(repair=False)
            limits.check(counters, envelope['policy'], for_dispatch=False)
        def dispatch_budget():
            if limits is None:
                return self.check_budget_circuit_breaker()
            limits.require_capacity('03-dev', '03-dev')
        history_authority = limits._load(repair=False)[0]['binding'].get('historical_capture_bindings_sha256') if limits is not None else None
        scheduler = TaskScheduler(
            wt, self.dir_dev / "tasks", plan, quality, context, develop,
            verify, guard, verification_budget, dispatch_budget=dispatch_budget,
            historical_task_floors=(limits._load(repair=False)[0]['binding'].get('task_floors', {}) if limits else {}),
            historical_capture_authority=history_authority,
            max_attempts=(getattr(_dispatch_local, "limits")._load(repair=False)[0]["policy"]["development_attempts"]
                          if getattr(_dispatch_local, "limits", None) is not None else int(os.environ.get("SSDLC_MAX_DEV_RETRIES", "3"))),
            bootstrap=quality.get("bootstrap"),
            isolate_candidates=True,
            recover_timeouts=getattr(_dispatch_local, 'limits', None) is not None,
            runtime_identity={'controller_sha256': digest(controller),
                              'environment': runtime_environment},
        )
        self.state["current_phase"] = "03-dev"
        self.state["current_step"] = "task-development"
        self.state["status"] = "IN_PROGRESS"
        self._save_state()
        result = scheduler.run(ordered, recover_protocol_task=recover_protocol_task)
        if result.get("bootstrap", {}).get("status") == "PROVEN":
            self.state["bootstrap"] = {"status": "post-bootstrap", "proven_head": result["bootstrap"]["proven_head"]}
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
            "task_engine_v1": True,
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
        from ssdlc_recovery import bind_bounded_state
        bind_bounded_state(self.ssdlc_root, self.state)
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
    @_supervised_phase('00-recon')
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
    def _validate_po_requirements(self) -> Path:
        """Validate before PO approval; archive a lossless format revision."""
        try:
            return self._apply_po_validation()
        except (PlanError, CatalogueError, OSError, ValueError, KeyError, TypeError) as exc:
            self.record_gate('PO_SPEC_GATE', False, 'PO validation failed; canonical specification is not approved')
            if isinstance(exc, PlanError):
                raise
            raise PlanError('PO validation failed: invalid or unavailable recorded evidence') from exc

    def _apply_po_validation(self) -> Path:
        req_file = self.dir_po / 'requirements.md'
        ref = '01-po/requirements.md'
        if any(path.is_symlink() for path in (self.ssdlc_root, self.dir_po, req_file)):
            raise PlanError('PO validation rejects symlinked control artifacts')
        meta = self.state.get('artifacts', {}).get(ref)
        current = req_file.read_bytes()
        current_hash = hashlib.sha256(current).hexdigest()
        intent_ref = self.state.get('po_validation_ref')

        def retained(name):
            path = confined(self.ssdlc_root, name, protect=False)
            entry = self.state['artifacts'].get(name)
            if not entry or entry['path'] != name or not path.is_file() or file_hash(path) != entry['sha256']:
                raise PlanError('PO validation evidence is missing or changed')
            return path

        def publish(path, data):
            write_or_verify(path, data, root=self.ssdlc_root)
            name = str(path.relative_to(self.ssdlc_root))
            if name in self.state['artifacts']:
                retained(name)
            else:
                self.record_artifact(name, path)

        intent = None
        if intent_ref:
            previous = json.loads(retained(intent_ref).read_text())
            raw = retained(previous['original_ref']).read_bytes()
            canonical = retained(previous['normalized_ref']).read_bytes()
            normalized, mapping, catalogue = prepare_requirements(raw.decode('utf-8'))
            if (previous.get('schema_version') != '1.0'
                    or catalogue['required_ids'] != previous['required_ids']
                    or file_hash(retained(previous['original_ref'])) != previous['original_sha256']
                    or hashlib.sha256(canonical).hexdigest() != previous['normalized_sha256']
                    or normalized.encode('utf-8') != canonical or mapping != previous['id_mapping']):
                raise PlanError('PO validation intent is inconsistent')
            if current_hash in (previous['original_sha256'], previous['normalized_sha256']):
                intent = previous
        if not meta or (current_hash != meta['sha256'] and not (
                intent and current_hash == intent['normalized_sha256']
                and meta['sha256'] == intent['original_sha256'])):
            raise PlanError('PO requirements changed outside a recorded format revision')
        if intent is None:
            directory = self.dir_po / 'validation' / current_hash
            publish(directory / 'original.md', current)
            try:
                normalized, mapping, catalogue = prepare_requirements(current.decode('utf-8'))
            except CatalogueError as exc:
                publish(directory / 'diagnostics.json', canon(list(exc.diagnostics)))
                summary = '; '.join(f"{d['code']} {str(d.get('ref', d.get('kind', '')))[:80]} "
                                    f"line {d.get('source_line', d.get('source_lines', '?'))}"
                                    for d in exc.diagnostics[:8])
                raise PlanError('PO requirement validation failed: ' + summary, exc.diagnostics) from exc
            canonical = normalized.encode('utf-8')
            publish(directory / 'normalized.md', canonical)
            intent = dict(schema_version='1.0', original_ref=str((directory / 'original.md').relative_to(self.ssdlc_root)),
                normalized_ref=str((directory / 'normalized.md').relative_to(self.ssdlc_root)),
                original_sha256=current_hash, normalized_sha256=hashlib.sha256(canonical).hexdigest(),
                id_mapping=mapping, required_ids=catalogue['required_ids'])
            intent_ref = str((directory / 'intent.json').relative_to(self.ssdlc_root))
            publish(directory / 'intent.json', canon(intent))
            self.state['po_validation_ref'] = intent_ref
            self._save_state()
        # The registered intent and immutable originals precede the canonical
        # write, so restart can reconcile this exact interrupted replacement.
        if current != canonical:
            descriptor, name = tempfile.mkstemp(prefix='.po-', dir=self.dir_po)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(canonical)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(req_file)
            finally:
                temporary.unlink(missing_ok=True)
        if self.state['artifacts'][ref]['sha256'] != intent['normalized_sha256']:
            self.record_artifact(ref, req_file)
        details = f"Validated requirement definitions; {intent_ref}; {intent['normalized_sha256']}"
        previous_gates = [g for g in self.state.get('gate_history', []) if g['gate'] == 'PO_SPEC_GATE']
        if not previous_gates or not previous_gates[-1]['passed'] or previous_gates[-1]['details'] != details:
            self.record_gate('PO_SPEC_GATE', True, details)
        print(f"[01-po] Validated {len(intent['required_ids'])} requirement definitions; "
              f"{len(intent['id_mapping'])} explicit ID format mapping(s); evidence: {intent_ref}", flush=True)
        return req_file

    def resume_po_validation(self) -> Path:
        """Repair/validate recorded PO syntax only; never call a provider."""
        with ticket_lock(getattr(self, 'ticket_lock_path', self.ssdlc_root / 'ticket.lock')):
            self.state = self._load_or_init_state()
            if (self.state.get('current_phase') not in ('01-po', '02-design')
                    or self.worktree_dir.exists() or (self.dir_design / 'plan.json').exists()
                    or (self.dir_design / 'design-session/session.json').exists()
                    or (self.dir_dev / 'tasks/task-journal.jsonl').exists()
                    or any(e.get('phase', '').startswith(('02-design', '03-dev'))
                           for e in self.state.get('ledger', []))):
                raise PlanError('PO validation resume requires a task ticket before design/planning/development')
            if (git(self.repo_root, 'rev-parse', 'HEAD') != self.state['base_commit']
                    or git(self.repo_root, 'status', '--porcelain')):
                raise PlanError('PO validation resume requires the unchanged clean target baseline')
            try:
                result = self._validate_po_requirements()
            except (PlanError, CatalogueError) as exc:
                self.state.update(status='FAILED', failure=dict(phase='01-po', type=type(exc).__name__,
                                  category='PO_VALIDATION', message=str(exc)))
                self._save_state()
                raise
            if self.state.get('status') != 'PO_READY' or self.state.get('failure'):
                self.state.pop('failure', None)
                self.state.update(status='PO_READY', current_phase='01-po', current_step='po-validation')
                self._save_state()
            return result

    @_supervised_phase('01-po')
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
        sys_prompt += ("\nRequirement IDs must match (AC|SEC)-[A-Za-z0-9]+(-[A-Za-z0-9]+)*. "
                       "Use AC-1-1 or AC-1a for explicit child definitions, never AC-1.1. "
                       "Declare each ID exactly once. Prose mentions are not definitions. "
                       "Keep ordinary subcases as prose if they are not separate named obligations.")
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
                "GENERATED",
                dur,
                cost,
                tin,
                tout,
                trace_user,
                reasoning_effort=effort,
            )
        )

        self._validate_po_requirements()
        out = req_file.read_text(encoding='utf-8')
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
    def _valid_design_progress_evidence(self, item):
        ref = item['artifact_ref']
        meta = self.state.get('artifacts', {}).get(ref)
        if not ref.startswith('02-design/design-session/source-inspection-') or not meta or meta['sha256'] != item['sha256']:
            return False
        path = confined(self.ssdlc_root, ref, protect=False)
        if file_hash(path) != item['sha256']:
            raise DevelopmentBlocked('INTEGRITY', 'Registered design inspection changed')
        report = json.loads(path.read_text())
        return (report.get('purpose') == 'progress_evidence' and report['kind'] == 'source_inspection'
                and report['base_commit'] == self.state['base_commit'] and bool(report['files'])
                and all(file_hash(confined(self.repo_root, f['path'])) == f['sha256'] for f in report['files']))

    def _design_sources(self, recon_text: str) -> list[dict]:
        """Supply bounded, numbered source evidence for the modules named by recon."""
        remaining = 80000
        sources = []
        extensions = {'.py', '.ts', '.tsx', '.js', '.jsx', '.go', '.rs', '.java', '.kt', '.cs', '.rb', '.php', '.c', '.h'}
        for name in git(self.repo_root, 'ls-files', '-z').split('\x00'):
            if not name or name not in recon_text or Path(name).suffix not in extensions:
                continue
            path = confined(self.repo_root, name)
            if not path.is_file():
                continue
            text = path.read_text()
            numbered = '\n'.join(f'{i}: {line}' for i, line in enumerate(text.splitlines(), 1))
            limit = min(50000, remaining)
            sources.append({'path': name, 'sha256': file_hash(path), 'truncated': len(numbered) > limit,
                            'source': diagnostic_excerpt(numbered, limit)})
            remaining -= min(limit, len(numbered))
            if remaining <= 0 or len(sources) == 4:
                break
        return sources

    @_supervised_phase('02-design')
    def run_phase_pair_design(self, round_limit=None, import_legacy=None) -> Tuple[Path, Path]:
        """Run/resume pair design; the pipeline or resume entry point holds the lock."""
        print(f'\n==> [{self.project_id} | Phase 2: 02-design] Persistent Pair Design...')
        self.state.update(current_phase='02-design', current_step='pair-design')
        self._save_state()
        recon_text = (self.dir_recon / 'context-and-blast-radius.md').read_text()
        if self.state.get('po_validation_ref'):
            self._validate_po_requirements()
        req_text = (self.dir_po / 'requirements.md').read_text()
        risk = self.state.get('risk_tier', 'M')
        profile = self.state.get('dev_profile', 'ALGORITHMIC')
        arch_engine, arch_model, _, _ = SquadDispatcher.dispatch_role('02-design-arch', risk, profile)
        sec_engine, sec_model, _, _ = SquadDispatcher.dispatch_role('02-design-sec', risk, profile)
        if not arch_model or not sec_model or arch_model.split('/')[-1] == sec_model.split('/')[-1]:
            raise PlanError('Pair design requires an independent reviewer')
        quality_path = self.dir_recon / 'quality-contract.json'
        quality = json.loads(quality_path.read_text()) if quality_path.exists() else {}
        try:
            catalogue = requirement_catalogue(req_text)
        except CatalogueError as exc:
            raise PlanError(str(exc), exc.diagnostics) from exc
        context = {'ticket_id': self.ticket_id, 'base_commit': self.state.get('base_commit'),
                   'recon': recon_text, 'requirements': req_text, 'requirement_catalogue': catalogue,
                   'quality_contract': quality, 'source_evidence': self._design_sources(recon_text)}
        arch_prompt = (
            'Draft or repair a complete ADR against the approved AC/SEC and quality obligations. '
            'Preserve previous correct decisions and explicitly address all prior open findings. '
            'Use exact files, interfaces, state transitions, error handling and meaningful behavioral tests. '
            'Verify API signatures, persisted state keys, exception types and all retained evidence stores from source. '
            'Cite source file/line evidence for interface and state claims; read full source when excerpts are truncated. '
            'Do not invent state fields or assume the journal contains a plan. Inventory existing evidence, including '
            'failed attempts and checkpoint preflight logs when present; explicitly define which inventory the '
            'approved integrity requirement covers. Enumerate persistence boundaries and precise fault injection points. '
            'Respect the documented threat model and scope; do not invent stronger external-writer guarantees. '
            'Return the ADR as Markdown. History contains prior reviews; the previous ADR is the repair starting point.')
        schema = (Path(__file__).parent / 'schemas/design-review.schema.json').read_text()
        review_prompt = (
            'Review the entire ADR comprehensively using STRIDE, the approved requirements, quality contract, '
            'source evidence and cumulative history. Return only JSON matching the review schema. '
            'Use analysis for STRIDE assessment, source-contract checks, races and stated limits. '
            'Use task_ids=[] because implementation tasks do not exist yet. '
            'Keep finding_id stable and explicitly resolve or retain every earlier open blocker using evidence. '
            'Open blockers must cite approved AC/SEC, standards or checks and identify a concrete source/design contradiction. '
            'Resolved schema diagnostics may have empty obligation references; never invent IDs for them. '
            'Classify optional improvements as ADVISORY; do not expand scope or turn documented limitations into blockers. '
            'Explain newly introduced or previously missed findings. Inspect source to verify signatures, state shapes, '
            'all retained evidence and exception boundaries; do not demand implementation code before design approval. '
            'Optional progress_evidence must cite only the host progress_evidence_catalogue with exact hash and approved obligation IDs; model prose and rewritten ADRs are not material evidence. '
            'Legacy prose reviews remain relevant: account for their unresolved concerns in the first structured review.\n' + schema)
        routing = {role: [engine, model, [SquadDispatcher.resolve_reasoning_effort(role, risk, attempt=i) for i in range(1, 9)]]
                   for role, engine, model in [('02-design-arch', arch_engine, arch_model), ('02-design-sec', sec_engine, sec_model)]}
        def invoke(role, engine, model, system, user, rnd):
            effort = routing[role][2][rnd - 1] if role in routing else routing['02-design-sec'][2][0]
            print(f'[02-design] {role} round {rnd}: {engine}:{model}, reasoning={effort}', flush=True)
            start = time.time()
            out, cost, tin, tout, trace = EngineRunner.invoke_role(
                engine, model, system, json.dumps(user, indent=2), self.repo_root,
                self.project_id, self.ticket_id, f'{role}-r{rnd}', reasoning_effort=effort)
            self.record_ledger(CostAndTelemetryEntry(f'{role}-r{rnd}', role, engine, model, 'GENERATED',
                                                    time.time() - start, cost, tin, tout, trace, effort))
            return out
        def architect(rnd, previous, history):
            return invoke('02-design-arch', arch_engine, arch_model, arch_prompt,
                          {'context': context, 'previous_adr': previous, 'review_history': history}, rnd)
        def reviewer(rnd, adr, history):
            catalogue = [dict(artifact_ref=ref, sha256=meta['sha256'], report=json.loads((self.ssdlc_root / meta['path']).read_text()))
                for ref, meta in self.state.get('artifacts', {}).items()
                if ref.startswith('02-design/design-session/source-inspection-')
                and json.loads((self.ssdlc_root / meta['path']).read_text()).get('purpose') == 'progress_evidence']
            return invoke('02-design-sec', sec_engine, sec_model, review_prompt,
                          {'context': dict(context, progress_evidence_catalogue=catalogue), 'adr': adr, 'review_history': history}, rnd)
        def artifact(path):
            self.record_artifact(str(path.relative_to(self.ssdlc_root)), path)
        limits = getattr(_dispatch_local, 'limits', None)
        def diagnose(subject, previous, history):
            limits.reserve_diagnosis(subject)
            from ssdlc_recovery import inspect_design_sources
            report = inspect_design_sources(self.repo_root, [e['path'] for e in context['source_evidence']])
            path = self.dir_design / 'design-session' / ('source-inspection-' + subject + '.json')
            report['purpose'] = 'diagnosis_context'
            atomic_json(path, report)
            artifact(path)
            return invoke('02-design-diagnosis', sec_engine, sec_model,
                          'Diagnose stalled design against approved obligations. Return a bounded repair proposal; do not change requirements, run code, approve the design or allocate calls.',
                          {'context': dict(context, source_inspection=report), 'adr': previous, 'history': history}, 1)
        def verify_progress_evidence(item):
            return self._valid_design_progress_evidence(item)
        bounded_policy = limits._load(repair=False)[0]['policy'] if limits is not None else None
        if bounded_policy and round_limit is not None and round_limit != bounded_policy['design_rounds']:
            from ssdlc_recovery import RecoveryStop
            raise RecoveryStop('LIMIT_REACHED', 'Design round extension requires a new explicit limit authorization')
        session = DesignSession(self.dir_design,
                                {'context': context, 'architect_prompt': arch_prompt, 'review_prompt': review_prompt,
                                 'routing': routing}, architect, reviewer, self.check_budget_circuit_breaker, artifact,
                                initial_limit=bounded_policy['design_rounds'] if bounded_policy else int(os.environ.get('SSDLC_MAX_DESIGN_ROUNDS', '5')), extend_limit=round_limit,
                                progress_policy=limits is not None, diagnose=diagnose if limits is not None else None,
                                verify_evidence=verify_progress_evidence)
        if import_legacy:
            from ssdlc_design import read_history_rounds
            rounds = read_history_rounds(self.repo_root, self.ticket_id, import_legacy)
            session.import_legacy(rounds)
        elif not session.path.exists() and any(name in ('02-design/adr.md', '02-design/pair-dialogue.md') or name.startswith('02-design/rounds/')
                                               for name in self.state.get('artifacts', {})):
            raise PlanError('Existing design needs explicit --import-legacy from its verified round directory')
        try:
            adr, review = session.run()
        except Exception as exc:
            self.record_gate('PAIR_DESIGN_GATE', False, str(exc))
            raise
        assessment = parse_plan_output(review)
        threat_model = '# STRIDE & Systems Review\n\n' + assessment['analysis'] + '\n\n## Findings\n\n'
        threat_model += '\n\n'.join(f"- {f['finding_id']} [{f['status']}, {f['severity']}]: {f['message']}\n  Evidence: {f['evidence']}"
                                    for f in assessment['findings']) or 'No findings.'
        threat_model += '\n\nDESIGN_STATUS: APPROVED\n'
        for name, text in [('adr.md', adr), ('threat-model.md', threat_model), ('pair-dialogue.md', session.transcript())]:
            path = self.dir_design / name
            meta = self.state.get('artifacts', {}).get('02-design/' + name)
            if path.exists() and (not meta or file_hash(path) != meta['sha256']):
                raise PlanError('Existing design projection is unregistered or changed')
            path.write_text(text)
            artifact(path)
        self.record_gate('PAIR_DESIGN_GATE', True, 'Persistent ADR and independently approved structured review')
        return self.dir_design / 'adr.md', self.dir_design / 'threat-model.md'

    @_bounded_entrypoint
    def resume_pair_design(self, round_limit=None, import_legacy=None) -> Tuple[Path, Path]:
        """Resume design only against sealed upstream artifacts; never execute code."""
        with ticket_lock(getattr(self, 'ticket_lock_path', self.ssdlc_root / 'ticket.lock')):
            self.state = self._load_or_init_state()
            _resume_design_refusal(self.repo_root, self.ssdlc_root, self.state, read_git=git)
            self.state.update(status='IN_PROGRESS', current_phase='02-design', current_step='pair-design')
            self.state.pop('failure', None)
            self._save_state()
            try:
                result = self.run_phase_pair_design(round_limit, import_legacy)
            except Exception as exc:
                self.state.update(status='FAILED', failure={'phase': '02-design', 'message': str(exc)})
                self._save_state()
                raise
            self.state.update(status='DESIGN_READY')
            self.state.pop('failure', None)
            self._save_state()
            return result

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

    def _read_optional(self, path: Path, fallback: str = OPTIONAL_FALLBACK) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else fallback

    def _verified_task_review_input(self, diff_text: str) -> Tuple[Path, int]:
        try:
            diff_limit = int(os.environ.get('SSDLC_REVIEW_MAX_DIFF_CHARS', '200000'))
        except ValueError as exc:
            raise TaskError('SSDLC_REVIEW_MAX_DIFF_CHARS must be a positive integer') from exc
        if not 1 <= diff_limit <= 1000000:
            raise TaskError('SSDLC_REVIEW_MAX_DIFF_CHARS must be between 1 and 1000000')
        if len(diff_text) > diff_limit:
            raise TaskError(f'Full review diff ({len(diff_text)} chars) exceeds configured review budget ({diff_limit}); no truncation performed')
        if (not self.worktree_dir.is_dir() or not self.state.get('task_verified_head')
                or git(self.worktree_dir, 'rev-parse', 'HEAD') != self.state['task_verified_head']
                or git(self.worktree_dir, 'status', '--porcelain')):
            raise TaskError('Code review requires the clean verified task checkpoint')
        expected_diff = git(self.worktree_dir, 'diff', '--binary', self.state['base_commit'], 'HEAD') + '\n'
        if diff_text != expected_diff:
            raise TaskError('Review diff differs from the verified cumulative checkpoint')
        return self.worktree_dir, diff_limit

    def _gate_plan(self):
        path = self.dir_design / 'plan.json'
        if not path.is_file() or '02-design/plan.json' not in self.state.get('artifacts', {}):
            raise TaskError('Gate requires a registered task plan')
        phase, step = self.state.get('current_phase'), self.state.get('current_step')
        try:
            return self._ensure_task_plan()
        finally:
            self.state.update(current_phase=phase, current_step=step)
            self._save_state()

    def _stop_on_terminal_gate(self):
        if self._gate_terminal is not None:
            phase, outcome = self._gate_terminal
            raise GateTerminalError(outcome, diagnosis=f'Terminal gate outcome in {phase}')

    def _phase_roster(self, phase, attempt, requests):
        roster = dict(version=1, phase=phase, attempt=attempt,
                      reviewed_commit=requests[0].reviewed_commit,
                      evaluators=[request_identity(r) for r in requests])
        path = self.ssdlc_root / phase / 'provenance' / f'attempt-{attempt}' / 'roster.json'
        try:
            write_or_verify(path, canon(roster), root=self.ssdlc_root)
            ref = str(path.relative_to(self.ssdlc_root))
            meta = self.state['artifacts'].get(ref)
            if meta and meta['sha256'] != file_hash(path):
                raise TaskError('Registered phase roster changed')
            if not meta:
                self.record_artifact(ref, path)
        except (TaskError, OSError) as exc:
            raise GateTerminalError('integrity_blocked', diagnosis='Phase roster already used with different inputs; use a new gate attempt') from exc
        return roster

    def _phase_completion(self, roster):
        # No completion is published for an open evaluator (including a budget
        # exception or deliberate crash). Resume must finish the same roster.
        members = []
        for member in roster['evaluators']:
            path = (self.ssdlc_root / roster['phase'] / 'evaluations' /
                    member['slot'][:16] / 'outcome.json')
            if not path.is_file():
                return
            try:
                outcome = json.loads(path.read_text())
                outcome_class = outcome['outcome_class']
            except (OSError, ValueError, KeyError, TypeError):
                # Preserve the evaluator's terminal error. Invalid evidence must
                # never acquire a completion record or mask GateTerminalError.
                return
            members.append(dict(evaluator=member, outcome_class=outcome_class,
                                outcome_ref=str(path.relative_to(self.ssdlc_root)),
                                outcome_sha256=file_hash(path)))
        completion = dict(version=1, phase=roster['phase'], attempt=roster['attempt'],
                          roster_sha256=hashlib.sha256(canon(roster)).hexdigest(), evaluators=members)
        path = (self.ssdlc_root / roster['phase'] / 'provenance' /
                f"attempt-{roster['attempt']}" / 'completion.json')
        try:
            write_or_verify(path, canon(completion), root=self.ssdlc_root)
            ref = str(path.relative_to(self.ssdlc_root))
            meta = self.state['artifacts'].get(ref)
            if meta and meta['sha256'] != file_hash(path):
                raise TaskError('Registered phase completion changed')
            if not meta:
                self.record_artifact(ref, path)
        except (TaskError, OSError) as exc:
            raise GateTerminalError('integrity_blocked', diagnosis='Phase completion differs') from exc

    def _task_review_gate(self, attempt, review_models, effort, engine, system, user, diff):
        plan = self._gate_plan()
        wt, limit = self._verified_task_review_input(diff)
        head = self.state['task_verified_head']
        quality = json.loads((self.dir_recon / 'quality-contract.json').read_text())
        candidate = self._collect_candidate_evidence('review', attempt, wt, quality)
        evidence = []
        inventory = [('ev/requirements', '01-po/requirements.md'), ('ev/plan', '02-design/plan.json'),
                     ('ev/quality-contract', '00-recon/quality-contract.json'), ('ev/diff', '03-dev/implementation.diff')]
        for ref, name in [('ev/adr', '02-design/adr.md'), ('ev/threat-model', '02-design/threat-model.md')]:
            if (self.ssdlc_root / name).exists():
                inventory.append((ref, name))
        for ref, name in inventory:
            path = self.ssdlc_root / name
            meta = self.state.get('artifacts', {}).get(name, {})
            evidence.append(dict(ref=ref, source=str(path), registered_hash=meta.get('sha256')))
        requirements = (self.dir_po / 'requirements.md').read_text()
        evidence.extend(candidate.entries)
        quality = candidate.quality
        context = build_trusted_context(head, plan, requirements, quality, [e['ref'] for e in evidence])
        system += '\n' + UNTRUSTED_DATA_INSTRUCTION
        user += '\n' + untrusted_data('approved task plan', plan)
        user += '\n' + untrusted_data('quality contract', quality)
        if candidate.block:
            user += '\n' + candidate_reports.CANDIDATE_EVIDENCE_NOTE + '\n' + untrusted_data('candidate evidence catalogue', candidate.block)
        history = self._build_history_snapshot('04-review', attempt)
        if history is not None:
            user += '\n' + untrusted_data('gate history', history)
        catalogue = requirement_catalogue(requirements)
        definitions = [(d['id'], hashlib.sha256(d['definition'].encode()).hexdigest()) for d in catalogue['definitions']]
        definitions += [(s['standard_id'], hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest()) for s in quality.get('standards', [])]
        fp = fingerprint(hashlib.sha256(diff.encode()).hexdigest(), file_hash(self.dir_design / 'plan.json'),
                         definitions, [(e['ref'], file_hash(Path(e['source']))) for e in evidence],
                         hashlib.sha256(system.encode()).hexdigest())
        run = self.dir_review / 'runs' / uuid.uuid4().hex
        run.mkdir(parents=True)
        atomic_json(run / 'inputs.json', dict(verified_head=head, review_cwd=str(wt), diff_chars=len(diff),
                    diff_char_budget=limit, system_prompt=system, user_prompt=user, review_models=review_models))
        self.record_artifact(str((run / 'inputs.json').relative_to(self.ssdlc_root)), run / 'inputs.json')
        results, terminals = [], []
        diagnosis_pairs = []

        def verify():
            self._verify_candidate_evidence(candidate, wt)
            if self.state.get('task_verified_head') != head:
                raise TaskError('Verified checkpoint changed during review; approval withheld')
            try:
                self._verified_task_review_input((self.dir_dev / 'implementation.diff').read_text())
            except TaskError as exc:
                raise TaskError('Verified checkpoint changed during review; approval withheld') from exc

        def invoke(request, sys_text, user_text):
            return EngineRunner.invoke_role(request.engine, request.model, sys_text, user_text, wt,
                       self.project_id, self.ticket_id, f'04-review-a{attempt}', sandbox='read-only', reasoning_effort=effort)

        def ledger(details, telemetry):
            outcome = details['outcome_class']
            status = {'integrity_blocked': 'INPUT_CHANGED', 'interrupted': 'ENGINE_ERROR',
                      'protocol_error': 'INVALID_VERDICT', 'approved': 'APPROVED',
                      'rejected_substantive': 'CHANGES_REQUESTED'}.get(outcome, outcome)
            self.record_ledger(CostAndTelemetryEntry(f'04-review-a{attempt}', 'Code Reviewer', details['engine'],
                    details['model'], status, telemetry.get('duration_sec', 0), reasoning_effort=effort,
                    details=details, **{k: v for k, v in telemetry.items() if k != 'duration_sec'}))

        requests = []
        for index, model in enumerate(review_models, 1):
            effective_engine, effective_model = resolve_review_entry(model, engine)
            requests.append(EvaluationRequest('review', effective_engine, effective_model, index,
                attempt, head, system, user, context, evidence, fp, parse_review_verdict, effort, candidate.controls))
        roster = self._phase_roster('04-review', attempt, requests)
        for index, request in enumerate(requests, 1):
            evaluator = GateEvaluator(gate='review', base_dir=self.dir_review, ssdlc_root=self.ssdlc_root,
                invoke=invoke, check_budget=self.check_budget_circuit_breaker, record_artifact=self.record_artifact,
                record_ledger=ledger, record_gate=self.record_gate, verify_source=verify,
                artifacts=self.state['artifacts'], ledger=self.state['ledger'], gates=self.state['gate_history'])
            try:
                result = evaluator.evaluate(request)
                results.append(result)
                diagnosis_pairs.append((request, result))
                raw = result.response_text
                if result.outcome_class not in ('approved', 'rejected_substantive'):
                    terminals.append(GateTerminalError(result.outcome_class, evaluator.identity, result.diagnostics))
            except GateTerminalError as exc:
                terminals.append(exc)
                responses = sorted(evaluator.directory.glob('response-*.md'))
                raw = responses[-1].read_text() if responses else str(exc)
            path = run / f'reviewer-{index}.md'
            path.write_text(raw)
            self.record_artifact(str(path.relative_to(self.ssdlc_root)), path)
            if index == 1 and history is not None:
                self._history_fault('history.after_first_reviewer_complete')
        self._phase_completion(roster)
        approved = len(results) == len(review_models) and all(r.approved for r in results)
        report = '\n\n'.join(r.response_text + '\n' + r.summary for r in results)
        for name in (f'review-report-a{attempt}.md', 'review-report.md'):
            path = self.dir_review / name
            path.write_text(report)
            self.record_artifact(str(path.relative_to(self.ssdlc_root)), path)
        self.record_gate(f'CODE_REVIEW_A{attempt}', approved,
                         '; '.join(r.summary + '; ' + ', '.join(r.artifact_refs) for r in results))
        for diag_request, diag_result in diagnosis_pairs:
            self._diagnose_gate([diag_request], [diag_result], candidate, wt, '04-review')
        if terminals:
            raise terminals[0]
        return approved, report

    @_supervised_phase('04-review')
    def run_phase_review(self, attempt: int = 1) -> Tuple[bool, str]:
        self._gate_terminal = None
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

        _, diff_limit = self._verified_task_review_input(diff_text)
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
            f"GIT DIFF TO REVIEW:\n```diff\n{diff_text}\n```"
        )

        return self._task_review_gate(attempt, review_models, rev_effort, review_engine,
                                      sys_prompt, user_prompt, diff_text)

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
                f"## [{'PASS' if test_passed else 'FAIL'}] shell_command (`{effective_test_cmd}` — exit code {t_rc})\n"
                f"```text\n{t_out + t_err}\n```\n"
            )
        else:
            logs.append("## [INFO] Test Runner\nNo custom `--test-cmd` provided or auto-detected.\n")

        return overall_ok, "\n".join(logs)

    def _run_appsec_scans(self, wt):
        results = []
        for scanner, scopes in discover_manifest_scopes(wt).items():
            if not scopes:
                results.append(ScannerResult(scanner + ':.', 'not_applicable', False,
                                             reason='manifest scope absent'))
            for scope, manifests in sorted(scopes.items()):
                identity = scanner + ':' + scope
                reason = ''
                required = True
                if scanner == 'npm':
                    lock = bool(manifests & {'package-lock.json', 'npm-shrinkwrap.json'})
                    try:
                        obj = json.loads((wt / scope / 'package.json').read_text()) if 'package.json' in manifests else {}
                        if not isinstance(obj, dict):
                            raise ValueError('manifest must be an object')
                        deps = any(isinstance(obj.get(k), dict) and obj[k] for k in
                                   ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'))
                    except (OSError, ValueError):
                        reason = 'malformed manifest'
                        deps = True
                    required = bool(deps or lock)
                    if required and not lock and not reason:
                        reason = 'dependency scope present without lockfile; npm audit cannot run'
                    tool, command = 'npm', ['npm', 'audit', '--audit-level=high']
                else:
                    tool, command = 'pip-audit', ['pip-audit']
                if not required:
                    results.append(ScannerResult(identity, 'not_applicable', False, reason='dependency scope absent', scope=scope))
                    continue
                if not reason and not (shutil.which(tool) or EngineRunner._should_spawn_role_container()):
                    reason = 'required scanner tool unavailable'
                if reason:
                    results.append(ScannerResult(identity, 'unknown', True, reason=reason, scope=scope))
                    continue
                try:
                    out, err, rc = EngineRunner.run_sandboxed_cmd(command, cwd=wt / scope, timeout=120)
                    results.append(ScannerResult(identity, 'passed' if rc == 0 else 'failed', True, rc, out + err, scope=scope))
                except Exception as exc:
                    results.append(ScannerResult(identity, 'unknown', True, log=str(exc), reason='scanner execution failed', scope=scope))
        return results

    def _task_verification_gate(self, gate, attempt, engine, model, effort, diff, system, user,
                                controls, extra_evidence, scanners=None):
        controls = dict(controls)
        phase = {'qa': '05-qa', 'appsec': '05b-appsec'}[gate]
        base = self.dir_qa if gate == 'qa' else self.dir_appsec
        try:
            wt, limit = self._verified_task_review_input(diff)
        except TaskError:
            self._gate_terminal = (phase, 'integrity_blocked')
            self.record_ledger(CostAndTelemetryEntry(f'{phase}-a{attempt}', gate, engine, model,
                                                   'INPUT_CHANGED', 0, reasoning_effort=effort))
            self.record_gate(f'{gate.upper()}_VERIFICATION_A{attempt}', False, 'Verified checkpoint changed during scanners')
            return False, 'Verified checkpoint changed during scanners; approval withheld'
        head = self.state['task_verified_head']
        quality = json.loads((self.dir_recon / 'quality-contract.json').read_text())
        candidate = self._collect_candidate_evidence(gate, attempt, wt, quality)
        controls.update(candidate.controls)
        plan = self._gate_plan()
        inventory = [('ev/requirements', self.dir_po / 'requirements.md'),
                     ('ev/plan', self.dir_design / 'plan.json'),
                     ('ev/quality-contract', self.dir_recon / 'quality-contract.json'),
                     ('ev/diff', self.dir_dev / 'implementation.diff')] + extra_evidence
        for ref, name in [('ev/adr', 'adr.md'), ('ev/threat-model', 'threat-model.md')]:
            if (self.dir_design / name).exists():
                inventory.append((ref, self.dir_design / name))
        evidence = [dict(ref=ref, source=str(path), registered_hash=self.state['artifacts'].get(
                    str(path.relative_to(self.ssdlc_root)), {}).get('sha256')) for ref, path in inventory]
        requirements = (self.dir_po / 'requirements.md').read_text()
        evidence.extend(candidate.entries)
        quality = candidate.quality
        context = build_trusted_context(head, plan, requirements, quality, [e['ref'] for e in evidence])
        system += '\n' + UNTRUSTED_DATA_INSTRUCTION
        user += '\n' + untrusted_data('approved task plan', plan)
        user += '\n' + untrusted_data('quality contract', quality)
        if candidate.block:
            user += '\n' + candidate_reports.CANDIDATE_EVIDENCE_NOTE + '\n' + untrusted_data('candidate evidence catalogue', candidate.block)
        history = self._build_history_snapshot(phase, attempt)
        if history is not None:
            user += '\n' + untrusted_data('gate history', history)
        definitions = [(d['id'], hashlib.sha256(d['definition'].encode()).hexdigest())
                       for d in requirement_catalogue(requirements)['definitions']]
        definitions += [(s['standard_id'], hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest())
                        for s in quality.get('standards', [])]
        fp = fingerprint(hashlib.sha256(diff.encode()).hexdigest(), file_hash(self.dir_design / 'plan.json'),
                         definitions, [(e['ref'], file_hash(Path(e['source']))) for e in evidence],
                         hashlib.sha256(system.encode()).hexdigest())
        run = base / 'runs' / uuid.uuid4().hex
        run.mkdir(parents=True)
        atomic_json(run / 'inputs.json', dict(verified_head=head, qa_cwd=str(wt), review_cwd=str(wt),
                                            system_prompt=system, user_prompt=user))
        self.record_artifact(str((run / 'inputs.json').relative_to(self.ssdlc_root)), run / 'inputs.json')

        def verify():
            self._verify_candidate_evidence(candidate, wt)
            if self.state.get('task_verified_head') != head:
                raise TaskError('Verified checkpoint changed')
            self._verified_task_review_input((self.dir_dev / 'implementation.diff').read_text())

        def invoke(request, sys_text, user_text):
            return EngineRunner.invoke_role(engine, model, sys_text, user_text, wt, self.project_id,
                        self.ticket_id, f'{phase}-a{attempt}', sandbox='read-only', reasoning_effort=effort)

        def ledger(details, telemetry):
            status = {'integrity_blocked': 'INPUT_CHANGED', 'protocol_error': 'INVALID_VERDICT',
                      'interrupted': 'ENGINE_ERROR', 'approved': 'PASSED' if gate == 'qa' else 'SECURE',
                      'rejected_substantive': 'FAILED' if gate == 'qa' else 'VULNERABLE'}[details['outcome_class']]
            self.record_ledger(CostAndTelemetryEntry(f'{phase}-a{attempt}', gate, engine, model, status,
                telemetry.get('duration_sec', 0), reasoning_effort=effort, details=details,
                **{k: v for k, v in telemetry.items() if k != 'duration_sec'}))

        evaluator = GateEvaluator(gate=gate, base_dir=base, ssdlc_root=self.ssdlc_root, invoke=invoke,
            check_budget=self.check_budget_circuit_breaker, record_artifact=self.record_artifact,
            record_ledger=ledger, record_gate=self.record_gate, verify_source=verify,
            artifacts=self.state['artifacts'], ledger=self.state['ledger'], gates=self.state['gate_history'])
        request = EvaluationRequest(gate, engine, model, 1, attempt, head, system, user,
            context, evidence, fp, parse_qa_verdict if gate == 'qa' else parse_appsec_verdict, effort, controls)
        try:
            roster = self._phase_roster(phase, attempt, [request])
        except GateTerminalError:
            self._gate_terminal = (phase, 'integrity_blocked')
            return False, 'Phase roster differs; approval withheld'
        terminal = None
        result = None
        try:
            result = evaluator.evaluate(request)
            approved, raw = result.approved, result.response_text
            if result.outcome_class not in ('approved', 'rejected_substantive'):
                terminal = GateTerminalError(result.outcome_class, evaluator.identity, result.diagnostics)
        except GateTerminalError as exc:
            terminal = exc
            approved = False
            responses = sorted(evaluator.directory.glob('response-*.md'))
            raw = responses[-1].read_text() if responses else str(exc)
        if history is not None:
            self._history_fault('history.after_first_reviewer_complete')
        try:
            self._phase_completion(roster)
        except GateTerminalError:
            self._gate_terminal = (phase, 'integrity_blocked')
            return False, 'Phase completion differs; approval withheld'
        if result is not None:
            self._diagnose_gate([request], [result], candidate, wt, phase)
        for path in (run / 'response.md', base / ('qa-traceability-matrix.md' if gate == 'qa' else 'redteam-audit.md')):
            path.write_text(raw)
            self.record_artifact(str(path.relative_to(self.ssdlc_root)), path)
        if terminal:
            if terminal.outcome_class == 'integrity_blocked':
                self._gate_terminal = (phase, 'integrity_blocked')
                return False, raw
            raise terminal
        return approved, raw

    @_supervised_phase('05-qa')
    def run_phase_qa(self, test_cmd: Optional[str] = None, attempt: int = 1) -> Tuple[bool, str]:
        self._gate_terminal = None
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
        wt, _ = self._verified_task_review_input(self._read_optional(self.dir_dev / 'implementation.diff', ''))
        det_ok, det_logs = self._run_deterministic_scanners(wt, test_cmd)
        det_log_path = self.dir_qa / f"deterministic-checks-a{attempt}.md"
        det_log_path.write_text("# QA deterministic reports (shell_command)\n\n" + det_logs, encoding="utf-8")
        self.record_artifact(f"05-qa/deterministic-checks-a{attempt}.md", det_log_path)

        req_text = self._read_optional(self.dir_po / "requirements.md")
        tm_text = self._read_optional(self.dir_design / "threat-model.md")
        diff_text = self._read_optional(self.dir_dev / "implementation.diff", "")

        ui_qa_rules = ""
        qa_cwd = self.repo_root
        qa_cwd = wt
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
            "QA_STATUS: PASSED\n"
            "or\n"
            "QA_STATUS: FAILED\n"
            "Use PASSED only when all ACs, SEC items and deterministic checks pass. "
            "Emit exactly one verdict marker as the final standalone line, without annotations or code fences."
        )
        user_prompt = (
            f"PO REQUIREMENTS:\n{req_text}\n\n"
            f"THREAT MODEL:\n{tm_text}\n\n"
            f"DETERMINISTIC TEST & SCAN LOGS:\n{det_logs}\n\n"
            f"IMPLEMENTATION DIFF:\n```diff\n{diff_text}\n```"
        )

        return self._task_verification_gate('qa', attempt, qa_engine, qa_model, qa_effort,
            diff_text, sys_prompt, user_prompt, {'det_ok': det_ok}, [('ev/qa-deterministic', det_log_path)])

    @_supervised_phase('05b-appsec')
    def run_phase_appsec(self, attempt: int = 1) -> Tuple[bool, str]:
        self._gate_terminal = None
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

        wt, limit = self._verified_task_review_input(diff_text)
        scanners = self._run_appsec_scans(wt)
        text = '\n'.join(f'## {r.id}: {r.status}\n{r.reason}\n{r.log}' for r in scanners)
        evidence = []
        for index, result in enumerate(scanners):
            path = self.dir_appsec / f'scanner-{index}-a{attempt}.log'
            path.write_text(result.log + '\n' + result.reason)
            self.record_artifact(str(path.relative_to(self.ssdlc_root)), path)
            evidence.append((f'ev/sca-{result.id}', path))
        for name in (f'supply-chain-sca-a{attempt}.md', 'supply-chain-sca.md'):
            path = self.dir_appsec / name
            path.write_text(text)
            self.record_artifact(str(path.relative_to(self.ssdlc_root)), path)
        status = self.dir_appsec / 'scanner-status.json'
        status.write_text(scanner_status_json(scanners))
        self.record_artifact('05b-appsec/scanner-status.json', status)
        evidence.append(('ev/sca-status', status))
        excerpt = text if len(text) <= limit else text[:limit // 2] + '\n[excerpt; full logs: ev/sca-status and scanner evidence]\n' + text[-limit // 2:]
        return self._task_verification_gate('appsec', attempt, appsec_engine, appsec_model, appsec_effort,
            diff_text, 'You are the AppSec auditor. Analyze security and dependencies. End with exactly one final '
            'APPSEC_STATUS: SECURE or APPSEC_STATUS: VULNERABLE line.',
            f'REQUIREMENTS:\n{req_text}\nTHREAT MODEL:\n{tm_text}\nSCA:\n{excerpt}\nCOMPLETE DIFF:\n{diff_text}',
            {'scanners': all(r.status in ('passed', 'not_applicable') for r in scanners)}, evidence, scanners)

    def _ops_docs_context(self, phase, diff_text, texts):
        wt, _ = self._verified_task_review_input(diff_text)
        disclosure = dict(schema_version='1.0', phase=phase,
                          verified_commit=git(wt, 'rev-parse', 'HEAD'),
                          tree=git(wt, 'rev-parse', 'HEAD^{tree}'),
                          diff=dict(included_chars=len(diff_text), total_chars=len(diff_text), complete=True),
                          inputs=[dict(name=name, included_chars=min(len(value), OPS_DOCS_TEXT_CAP),
                                       total_chars=len(value), truncated=len(value) > OPS_DOCS_TEXT_CAP)
                                  for name, value in texts.items()])
        disclosure['full_audit_coverage_permitted'] = not any(i['truncated'] for i in disclosure['inputs'])
        validate_document('context-disclosure', disclosure)
        ref = phase + '/context-disclosure.json'
        path = confined(self.ssdlc_root, ref, protect=False)
        candidate_reports.write_or_verify(path, canon(disclosure), root=self.ssdlc_root)
        self._register_new(ref, path)
        block = untrusted_data('verified candidate context', dict(
            commit=disclosure['verified_commit'], tree=disclosure['tree'], worktree_clean=True))
        block += '\n' + untrusted_data('context disclosure', disclosure)
        block += '\n' + untrusted_data('implementation diff', diff_text)
        for name, value in texts.items():
            block += '\n' + untrusted_data(name, value[:OPS_DOCS_TEXT_CAP])
        return wt, block, disclosure

    def _ops_docs_enabled(self):
        return candidate_reports.convergence_enabled(self._ensure_quality_contract())

    def _verify_ops_docs_after(self, diff_text):
        fresh = git(self.worktree_dir, 'diff', '--binary', self.state['base_commit'], 'HEAD') + '\n'
        self._verified_task_review_input(fresh)
        if fresh != diff_text or self._read_optional(self.dir_dev / 'implementation.diff', '') != diff_text:
            raise TaskError('Verified checkpoint diff changed during Ops/Docs invocation')

    @_supervised_phase('06-ops')
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

        verified_context = self._ops_docs_enabled()
        role_cwd = self.repo_root
        if verified_context:
            role_cwd, user_prompt, disclosure = self._ops_docs_context('06-ops', diff_text, {'ADR': adr_text})
            sys_prompt += '\n' + UNTRUSTED_DATA_INSTRUCTION + '\n' + OPS_DOCS_COVERAGE_NOTE

        t0 = time.time()
        ops_out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
            ops_engine,
            ops_model,
            sys_prompt,
            user_prompt,
            role_cwd,
            self.project_id,
            self.ticket_id,
            "06-ops",
            sandbox="read-only",
            reasoning_effort=ops_effort,
        )
        if verified_context:
            self._verify_ops_docs_after(diff_text)
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
    @_supervised_phase('07-docs')
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

        verified_context = self._ops_docs_enabled()
        role_cwd = self.repo_root
        if verified_context:
            role_cwd, user_prompt, disclosure = self._ops_docs_context('07-docs', diff_text, {'PO requirements': req_text})
            sys_prompt += '\n' + UNTRUSTED_DATA_INSTRUCTION + '\n' + OPS_DOCS_COVERAGE_NOTE

        t0 = time.time()
        docs_out, cost, tin, tout, trace_user = EngineRunner.invoke_role(
            docs_engine,
            docs_model,
            sys_prompt,
            user_prompt,
            role_cwd,
            self.project_id,
            self.ticket_id,
            "07-docs",
            sandbox="read-only",
            reasoning_effort=docs_effort,
        )
        if verified_context:
            self._verify_ops_docs_after(diff_text)
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
    def _completion_disclosures(self):
        tasks = self.state.get('tasks_status', {})
        path = self.dir_dev / 'tasks/task-journal.jsonl'
        if path.exists():
            tasks = (TaskJournal(path.parent).peek() or {}).get('tasks', {})
        rows = [f"- `{tid}` attempt {row['attempts']}: `{row['completion_confirmation']}`; independent checks, not producer confirmation, justify the checkpoint."
                for tid, row in sorted(tasks.items()) if row.get('completion_confirmation')]
        return ('\nProducer completion disclosures:\n\n' + '\n'.join(rows) + '\n') if rows else ''

    @_supervised_phase('08-supervisor')
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

        # QA shell_command reports remain separate from typed contract checks.
        artifact_rows = []
        for name, meta in sorted(self.state["artifacts"].items()):
            artifact_rows.append(
                f"| {'shell_command ' if name.startswith('05-qa/deterministic-checks-') else ''}[`{name}`](./{meta['path']}) | `{meta['sha256'][:16]}...` | {meta['size_bytes']} B | {meta['recorded_at']} |"
            )

        gate_rows = []
        for g in self.state["gate_history"]:
            badge = "PASS" if g["passed"] else "FAIL"
            gate_rows.append(f"| `{g['gate']}` | **{badge}** | {g['details']} | {g['timestamp']} |")

        specialists_str = ", ".join(self.state.get("specialists_triggered", [])) or "None"
        iso = self.state.get("isolation", {})
        vers = self.state.get("cli_versions", {})

        completion_disclosures = self._completion_disclosures()
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
{completion_disclosures}
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

To **REJECT** and record feedback (free text authorizes no repair):
```bash
./ssdlc reject {self.ticket_id} --reason "Describe required adjustment"
```
"""
        pack_path = self.ssdlc_root / "EVIDENCE_PACK.md"
        pack_path.write_text(pack_md, encoding="utf-8")
        self.record_artifact("EVIDENCE_PACK.md", pack_path)
        print(f"    [OK] Evidence Pack ready at: {pack_path.relative_to(self.repo_root)}")

        if self.remote_sync.enabled and (ready_for_signoff):
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

            if (git(wt, "status", "--porcelain") or self._get_head_sha(wt) != self.state.get("task_verified_head")):
                raise TaskError("Refusing to publish changes outside verified checkpoints")

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
        limits = getattr(_dispatch_local, 'limits', None)
        if limits is not None:
            envelope, state = limits._load(repair=False)
            limits.check(state, envelope['policy'])
            return
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

    @_bounded_entrypoint
    def run_pipeline(
        self,
        goal: str,
        test_cmd: Optional[str] = None,
        tier: str = "auto",
        from_phase: str = "00-recon",
    ) -> Path:
        _warn_obsolete_task_flag()
        if self.ssdlc_root != self.repo_root / '.ssdlc' / self.ticket_id and from_phase != '03-dev':
            raise DevelopmentBlocked('SCOPE_REQUIRED', 'Reconciled execution must resume from 03-dev')
        with ticket_lock(getattr(self, "ticket_lock_path", self.ssdlc_root / "ticket.lock")):
            # Reload state after acquiring the lock, not from a potentially stale constructor.
            self.state = self._load_or_init_state()
            self.state["task_breakdown"] = True
            self._save_state()
            if os.environ.get("SSDLC_EXECUTION_MODE", "docker") == "docker" and not EngineRunner._should_spawn_role_container():
                raise TaskError("Task mode in Docker requires role-container isolation. Run the controller on the host with the runner image available, or explicitly select local mode.")
            self._ensure_quality_contract()
            try:
                return self._run_pipeline(goal, test_cmd, tier, from_phase)
            except TaskError as exc:
                _supervision_event('failure', category=getattr(exc, 'category', 'TASK_EXECUTION'), exc_type=type(exc).__name__)
                failed_phase = self.state.get('current_phase')
                failed_step = self.state.get('current_step')
                failed_status = self.state.get('status')
                self.state['failure'] = {'phase': failed_phase, 'type': type(exc).__name__,
                                         'category': getattr(exc, 'category', 'TASK_EXECUTION'), 'message': str(exc)}
                self.record_gate("TASK_EXECUTION_GATE", False, str(exc))
                try:
                    self.compile_evidence_pack(ready_for_signoff=False)
                finally:
                    status = ('HALTED_BUDGET_EXCEEDED' if failed_status == 'HALTED_BUDGET_EXCEEDED'
                              else 'ESCALATED_TO_SUPERVISOR' if self.state.get('status') == 'ESCALATED_TO_SUPERVISOR'
                              else 'FAILED')
                    self.state.update(status=status, current_phase=failed_phase, current_step=failed_step)
                    self._save_state()
                raise

    def _run_pipeline(
        self,
        goal: str,
        test_cmd: Optional[str] = None,
        tier: str = "auto",
        from_phase: str = "00-recon",
    ) -> Path:
        _resume_run_refusal(self.ssdlc_root, self.state, from_phase)
        quality_path = self.ssdlc_root / '00-recon' / 'quality-contract.json'
        if quality_path.exists() and candidate_reports.convergence_enabled(json.loads(quality_path.read_text())):
            candidate_reports.reconcile_candidate_store(self.ssdlc_root, self.state['artifacts'], self._register_new)
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

        self._ensure_task_plan()

        frozen = getattr(_dispatch_local, 'limits', None)
        max_retries = (frozen._load(repair=False)[0]['policy']['development_attempts'] if frozen is not None
                       else int(os.environ.get('SSDLC_MAX_DEV_RETRIES', '3')))
        passed_all = False

        if start_idx <= self.PHASE_ORDER.index("05b-appsec"):
            for attempt in range(1, max_retries + 1):
                if attempt > 1:
                    raise TaskError("Gate rejection requires explicit invalidation; free text authorizes no repair")
                if attempt > 1 or start_idx <= self.PHASE_ORDER.index("03-dev"):
                    self.run_phase_dev()
                    self.check_budget_circuit_breaker()
                else:
                    print("    [RESUME] Skipping initial 03-dev execution")

                if attempt > 1 or start_idx <= self.PHASE_ORDER.index("04-review"):
                    rev_ok, rev_report = self.run_phase_review(attempt=attempt)
                    self._stop_on_terminal_gate()
                    self.check_budget_circuit_breaker()
                    if not rev_ok:
                        continue
                else:
                    print("    [RESUME] Skipping initial 04-review execution")

                if attempt > 1 or start_idx <= self.PHASE_ORDER.index("05-qa"):
                    qa_ok, qa_report = self.run_phase_qa(test_cmd=test_cmd, attempt=attempt)
                    self._stop_on_terminal_gate()
                    self.check_budget_circuit_breaker()
                    if not qa_ok:
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
                    self._stop_on_terminal_gate()
                    self.check_budget_circuit_breaker()
                    if not appsec_ok:
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

        if passed_all:
            if (self._get_head_sha(self.worktree_dir) != self.state.get("task_verified_head")
                    or git(self.worktree_dir, "status", "--porcelain")):
                raise TaskError("Code changed after task verification; global gates cannot authorize delivery")
        return self.compile_evidence_pack(ready_for_signoff=passed_all)

    def approve_and_merge(self) -> None:
        with ticket_lock(getattr(self, "ticket_lock_path", self.ssdlc_root / "ticket.lock")):
            self.state = self._load_or_init_state()
            self._approve_and_merge()

    def _approve_and_merge(self) -> None:
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
        disclosures = self._completion_disclosures()
        if disclosures:
            print(disclosures, flush=True)
        wt = self.worktree_dir
        branch = self.state["dev_branch"]

        git(self.repo_root, "merge", "--ff-only", self.state["task_verified_head"])

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


def _foreground_main(argv=None) -> int:
    _warn_obsolete_task_flag()
    parser = argparse.ArgumentParser(description="Project-Isolated Multi-CLI SSDLC Orchestrator",
        epilog="Stopped-run migration: continuation-inspect / continuation-preview / continuation-prepare / continuation-activate / continuation-verify (each has --help).")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_doc = sub.add_parser("doctor", help="Check project isolation, CLI versions, and API key fingerprints")
    p_doc.add_argument("--repo", "-r", default=".", help="Path to project repository")

    p_run = sub.add_parser("run", help="Run the risk-tiered task engine for a ticket")
    p_run.add_argument("--ticket", "-t", required=True, help="Ticket ID (e.g., TKT-001-auth)")
    p_run.add_argument("--goal", "-g", required=True, help="High-level requirement or feature goal")
    p_run.add_argument("--tier", default=None, choices=["auto", "S", "M", "L"], help="Risk tier: auto, S, M, or L")
    p_run.add_argument("--repo", "-r", default=".", help="Path to Git repository (default: current dir)")
    p_run.add_argument("--test-cmd", default=None, help="QA shell command (recorded separately from typed contract checks)")
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

    # Additive detached lifecycle commands; legacy foreground dispatch stays below.
    p_run.add_argument('--detach', action='store_true', help='Hand off to the local supervisor')
    p_run.add_argument('--json', action='store_true', help='Structured supervisor result')
    p_run.add_argument('--recovery-allowance', default='3')
    p_start = sub.add_parser('start', help='Start a detached, locally supervised run')
    p_start.add_argument('--ticket', '-t', required=True)
    p_start.add_argument('--goal', '-g', required=True)
    p_start.add_argument('--tier', default='auto', choices=['auto', 'S', 'M', 'L'])
    p_start.add_argument('--repo', '-r', default='.')
    p_start.add_argument('--test-cmd')
    p_start.add_argument('--from-phase', default='00-recon', choices=['00-recon', '03-dev'])
    p_start.add_argument('--recovery-allowance', default='3')
    p_start.add_argument('--json', action='store_true')
    p_resume = sub.add_parser('resume', help='Start a new compatible supervised run')
    p_resume.add_argument('--ticket', required=True)
    p_resume.add_argument('--repo', '-r', default='.')
    p_resume.add_argument('--run-id')
    p_resume.add_argument('--entrypoint', choices=['run', 'plan', 'design'], required=True)
    p_resume.add_argument('--from-phase', choices=['00-recon', '03-dev'], default='03-dev')
    p_resume.add_argument('--tier', choices=['auto', 'S', 'M', 'L'], default='auto')
    goals = p_resume.add_mutually_exclusive_group()
    goals.add_argument('--goal')
    goals.add_argument('--goal-file')
    p_resume.add_argument('--test-cmd')
    p_resume.add_argument('--review-repairs', type=int, choices=range(5))
    p_resume.add_argument('--rounds', type=int, choices=range(1, 9))
    p_resume.add_argument('--reauthorize-fingerprints', action='store_true')
    p_resume.add_argument('--recovery-allowance', default='3')
    for p_handoff in (p_run, p_start, p_resume):
        p_handoff.add_argument('--handoff-timeout-sec', default='30', help='Maximum detached handoff wait, 1-3600 seconds')
    p_resume.add_argument('--json', action='store_true')
    for command in ('status', 'cancel', 'monitor'):
        p_lifecycle = sub.add_parser(command, help=command + ' a supervised run')
        p_lifecycle.add_argument('--ticket', required=command != 'status', default=None)
        p_lifecycle.add_argument('--repo', '-r', default='.')
        p_lifecycle.add_argument('--run-id')
        p_lifecycle.add_argument('--json', action='store_true')
        if command == 'status':
            p_lifecycle.add_argument('--recovery-matrix', action='store_true')
        if command == 'monitor':
            p_lifecycle.add_argument('--interval', type=float, default=1)
            p_lifecycle.add_argument('--timeout', type=float, default=30)
            p_lifecycle.add_argument('--restart-worker', '--adopt', action='store_true')
            goals = p_lifecycle.add_mutually_exclusive_group()
            goals.add_argument('--goal')
            goals.add_argument('--goal-file')
            p_lifecycle.add_argument('--test-cmd')
        if command == 'cancel':
            p_lifecycle.add_argument('--grace', type=float, default=5)

    p_plan = sub.add_parser("plan", help="Resume task planning using approved requirements and design only")
    p_plan.add_argument("--ticket", required=True)
    p_plan.add_argument("--repo", "-r", default=".")
    p_plan.add_argument("--review-repairs", type=int, choices=range(5), default=None, help="Explicitly extend the persisted semantic repair allowance (0-4)")
    p_plan.add_argument('--recover-review', action='store_true', help='Explicitly revalidate an archived rejected review after a validator fix, preserving attempt counts')

    p_po = sub.add_parser('po', help='Validate existing task-mode PO requirements without invoking a model')
    p_po.add_argument('--ticket', required=True)
    p_po.add_argument('--repo', '-r', default='.')

    p_design = sub.add_parser('design', help='Resume pair design from its persisted ADR and review history')
    p_design.add_argument('--ticket', required=True)
    p_design.add_argument('--repo', '-r', default='.')
    p_design.add_argument('--rounds', type=int, choices=range(1, 9), default=None,
                          help='Explicit total round limit (1-8), preserving consumed rounds')
    p_design.add_argument('--import-from-ticket', default=None)
    p_design.add_argument('--import-legacy', default=None,
                          help='Import a verified legacy directory relative to 02-design, e.g. rounds/UUID')

    p_app = sub.add_parser("approve", help="Supervisor sign-off: commit worktree changes, merge, and prune worktree")
    p_app.add_argument("ticket", help="Ticket ID")
    p_app.add_argument("--repo", "-r", default=".", help="Path to Git repository")
    app_sync = p_app.add_mutually_exclusive_group()
    app_sync.add_argument("--sync-remote", dest="sync_remote", action="store_true", default=None,
                          help="Push merged branch to remote origin")
    app_sync.add_argument("--no-sync-remote", dest="sync_remote", action="store_false",
                          help="Do not push merged branch")

    p_rej = sub.add_parser("reject", help="Supervisor rejection: record rejection; free text authorizes no repair")
    p_rej.add_argument("ticket", help="Ticket ID")
    p_rej.add_argument("--reason", required=True, help="Supervisor rejection feedback")
    p_rej.add_argument("--repo", "-r", default=".", help="Path to Git repository")
    p_rej.add_argument("--test-cmd", default=None, help="Deterministic test command")
    rej_sync = p_rej.add_mutually_exclusive_group()
    rej_sync.add_argument("--sync-remote", dest="sync_remote", action="store_true", default=None,
                          help="Enable remote synchronization on rejection cycle")
    rej_sync.add_argument("--no-sync-remote", dest="sync_remote", action="store_false",
                          help="Disable remote synchronization on rejection cycle")

    p_preview = sub.add_parser('bounce-preview', help='Prepare a preview; grants no execution authority')
    p_preview.add_argument('--ticket', required=True)
    p_preview.add_argument('--repo', '-r', default='.')
    p_preview.add_argument('--phase', required=True, choices=tuple(bounce.PHASES))
    p_preview.add_argument('--attempt', required=True, type=int)

    for p_limits in (p_run, p_start, p_plan, p_design):
        p_limits.add_argument('--limits-profile', help='Explicit operator limits JSON for a fresh ticket; frozen on first launch')

    args = parser.parse_args(argv)
    if args.subcommand in {'run', 'start', 'resume', 'approve', 'reject', 'plan', 'design'}:
        command = getattr(args, 'entrypoint', 'run') if args.subcommand == 'resume' else args.subcommand
        importing = args.subcommand == 'design' and getattr(args, 'import_from_ticket', None)
        check = startup_preflight(args.repo, args.ticket, 'run' if importing else command, args)
        if importing and check['cls'] not in {'new_run', 'pre_execution'}:
            check = dict(ok=False, code='legacy_not_executable')
        if not check['ok']:
            if check.get('reason') == 'invalid_ticket':
                parser.error('invalid ticket ID')
            if check['code'] == 'legacy_not_executable':
                LEGACY_COUNTERS['legacy_refusals'] += 1
                logging.getLogger(__name__).warning('legacy-refusal ticket=%s command=%s', args.ticket, command)
                print(legacy_message(args.ticket), file=sys.stderr)
            else:
                print(check['code'], file=sys.stderr)
            return 24 if check['code'] == 'bootstrap_ineligible' else 23
    repo_path = Path(getattr(args, 'repo', '.')).resolve()
    if args.subcommand in {"run", "start", "plan", "design"} and getattr(args, "limits_profile", None):
        from ssdlc_recovery import LimitsStore
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.ticket) or args.ticket in {".", ".."}:
            parser.error("limits authorization requires a literal ticket ID")
        root = repo_path / ".ssdlc" / args.ticket
        store = LimitsStore(root)
        state_path = root / "state.json"
        if not store.policy_path.exists() and state_path.exists():
            raise TaskError("Existing ticket accounting requires an explicit migration; no fresh limits profile")
        store.authorize(json.loads(Path(args.limits_profile).read_text()),
            {"ticket": args.ticket, "base_commit": git(repo_path, "rev-parse", "HEAD"),
             "controller": controller_snapshot(Path(__file__).resolve().parent)})

    if args.subcommand in {'start', 'status', 'cancel', 'monitor', 'resume'} or (args.subcommand == 'run' and args.detach):
        import ssdlc_supervisor
        return ssdlc_supervisor.cli(args, parser)
    repo_path = Path(getattr(args, "repo", ".")).resolve()

    if args.subcommand == 'po':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.ticket):
            parser.error('po validation requires a literal ticket ID')
        if not (repo_path / '.ssdlc' / args.ticket / 'state.json').is_file():
            parser.error('po validation requires an existing ticket')
        orch = SSDLCOrchestrator(repo_path, args.ticket, sync_remote=False)
        print(orch.resume_po_validation())
        return 0

    if args.subcommand == 'bounce-preview':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.ticket) or args.attempt < 1:
            parser.error('preview requires a literal ticket and a positive attempt')
        # Avoid constructor side effects (runtime activation, state initialization,
        # CLI version probes). Preview needs only the persisted public host state.
        orch = SSDLCOrchestrator.__new__(SSDLCOrchestrator)
        orch.repo_root, orch.ticket_id = repo_path, args.ticket
        orch.ssdlc_root = repo_path / '.ssdlc' / args.ticket
        orch.state_file = orch.ssdlc_root / 'state.json'
        orch.worktree_dir = orch.ssdlc_root / 'worktree'
        if not orch.ssdlc_root.is_dir():
            print(json.dumps(dict(status='rejected-legacy', execution_authorized=False,
                                  blockers=[{'code': 'rejected-legacy'}])))
            return 20
        result = orch.preview_bounce(args.phase, args.attempt)
        print(bounce.canonical_json(result), end='')
        return (0 if result['status'] == 'ready-for-later-executor' else
                10 if result['status'] == 'diagnostic' else 20)

    if args.subcommand == "doctor":
        return cmd_doctor(repo_path)

    if args.subcommand == "plan":
        orch = SSDLCOrchestrator(repo_path, args.ticket, sync_remote=False)
        print(orch.resume_task_planning(review_repairs=args.review_repairs, recover_review=args.recover_review))
        return 0

    if args.subcommand == 'design' and args.import_from_ticket:
        from ssdlc_design import read_history_rounds, DesignSession
        rounds = read_history_rounds(repo_path, args.import_from_ticket, args.import_legacy)
        orch = SSDLCOrchestrator(repo_path, args.ticket, sync_remote=False)
        session = DesignSession(orch.dir_design, {'origin_ticket': args.import_from_ticket}, None, None,
                                lambda: None, lambda path: orch.record_artifact(str(path.relative_to(orch.ssdlc_root)), path))
        session.import_legacy(rounds)
        return 0

    if args.subcommand == 'design':
        orch = SSDLCOrchestrator(repo_path, args.ticket, sync_remote=False)
        print(orch.resume_pair_design(round_limit=args.rounds, import_legacy=args.import_legacy))
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
        print("Rejection recorded. Free text authorizes no repair or execution.")
        return 0

    return 0

    return 0


def _supervised_exit_code(value) -> int:
    # sys.exit(None) succeeds; bool is an int for Python's exit semantics, but
    # lifecycle evidence requires an actual int. Non-numeric messages mean 1.
    return 0 if value is None else int(value) if isinstance(value, int) else 1


def main(argv=None) -> int:
    words = sys.argv[1:] if argv is None else argv
    if words and words[0].startswith('continuation-limits-'):
        from ssdlc_reconciliation import main as reconciliation_main
        return reconciliation_main(words)
    if words and words[0].startswith('continuation-'):
        from ssdlc_continuations import main as continuation_main
        return continuation_main(words)
    supervised = bool(os.environ.get('SSDLC_SUPERVISED_RUN_ID')) and bool(words) and words[0] in {'run', 'plan', 'design'}
    if not supervised:
        return _foreground_main(argv)
    # Reject persisted legacy evidence before lifecycle logging can write events.
    def word_value(flag, default=None):
        aliases = {'--ticket': '-t', '--repo': '-r'}
        for name in (flag, aliases.get(flag)):
            if name in words and words.index(name) + 1 < len(words):
                return words[words.index(name) + 1]
        return default
    check = startup_preflight(word_value('--repo', '.'), word_value('--ticket'), words[0],
                              {'from_phase': word_value('--from-phase', '00-recon')})
    if word_value('--ticket') is not None and not check['ok']:
        if check['code'] == 'legacy_not_executable':
            LEGACY_COUNTERS['legacy_refusals'] += 1
            logging.getLogger(__name__).warning('legacy-refusal ticket=%s command=%s', word_value('--ticket'), words[0])
            print(legacy_message(word_value('--ticket')), file=sys.stderr)
        return 24 if check['code'] == 'bootstrap_ineligible' else 23
    code, exc_type = 1, None
    try:
        if not _supervised_preflight():
            code = 21
            return code
        result = _foreground_main(argv)
        code = _supervised_exit_code(result)
        return result
    except BaseException as exc:
        exc_type = type(exc).__name__
        code = _supervised_exit_code(exc.code) if isinstance(exc, SystemExit) else 1
        if code != 0:
            _supervision_event('failure', category=getattr(exc, 'category', 'PLAN_ERROR' if isinstance(exc, PlanError) else 'TASK_EXECUTION'), exc_type=exc_type)
        raise
    finally:
        _supervision_event('run_ended', exit_code=code, exc_type=exc_type)


if __name__ == "__main__":
    sys.exit(main())
