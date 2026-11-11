"""Versioned contracts and sequential, journaled task execution.

The journal is authoritative; its atomic JSON snapshot is only a projection.
This bootstrap deliberately rejects subgoals and automatic plan migrations.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from collections.abc import Collection, Mapping
import contextlib
import copy
import fcntl
import errno
import stat
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from ssdlc_catalogues import CatalogueError, heading_anchors, requirement_catalogue

SCHEMA_DIR = Path(__file__).resolve().parent / 'schemas'
PROTECTED = {'.git', '.ssdlc', '.agents', '.codex', '.aws'}


class TaskError(RuntimeError):
    """An execution or recovery condition requiring explicit handling."""


class _RevalidationStop(TaskError):
    kind = 'stop'

    def __init__(self, msg, task_id=None, check_id=None, path=None):
        self.task_id, self.check_id, self.path = task_id, check_id, path
        super().__init__(f"{msg} task {task_id!r} check {check_id!r}: ({path})")


class _VerifierMutation(_RevalidationStop):
    kind = 'mutation'


class _EvidenceChanged(_RevalidationStop):
    kind = 'evidence_changed'


class _EvidenceCollision(_RevalidationStop):
    kind = 'collision'


class _EvidencePathError(_RevalidationStop):
    kind = 'path'


class ResourceLimitError(TaskError):
    """A container hit a resource limit; code correction is not indicated."""

    category = 'ENVIRONMENT_RESOURCE'


class DevelopmentBlocked(TaskError):
    """The development loop cannot repair this condition inside its approved scope."""

    def __init__(self, category: str, message: str):
        super().__init__(f'{category}: {message}')
        self.category = category


class PlanError(TaskError):
    """A plan or contract cannot be safely executed."""

    def __init__(self, message: str, diagnostics: list[dict] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or [{'code': 'INVALID_PLAN', 'message': message}]


def parse_plan_output(output: str) -> dict:
    """Accept JSON or one JSON/unlabelled fenced block, never choose among alternatives."""
    text = output.strip()
    fences = list(re.finditer(r'^```([^\n`]*)\n(.*?)^```[ \t]*$', text, re.M | re.S))
    if fences:
        if len(fences) != 1 or fences[0][1].strip().lower() not in ('', 'json'):
            raise PlanError('Ambiguous fenced plan output', [{'code': 'AMBIGUOUS_OUTPUT', 'field': '$'}])
        outside = text[:fences[0].start()] + text[fences[0].end():]
        for start in re.finditer(r'[\[{]', outside):
            try:
                json.JSONDecoder().raw_decode(outside[start.start():])
            except ValueError:
                continue
            raise PlanError('Ambiguous plan output outside fence', [{'code': 'AMBIGUOUS_OUTPUT', 'field': '$'}])
        text = fences[0][2].strip()
    try:
        def unique_object(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError(f'Duplicate JSON key: {key}')
                obj[key] = value
            return obj
        def invalid_constant(value):
            raise ValueError(f'Non-finite JSON constant: {value}')
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except ValueError as exc:
        raise PlanError(str(exc), [{'code': 'INVALID_JSON', 'field': '$', 'message': str(exc)}]) from exc
    if not isinstance(value, dict):
        raise PlanError('Plan must be an object', [{'code': 'INVALID_SCHEMA', 'field': '$'}])
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def log_retry_reason(label: str, report: str, path: Path, max_chars: int = 3000) -> None:
    """Expose the actual critique, with a bounded excerpt and full evidence path."""
    text = report.strip() or '(Reviewer returned an empty report.)'
    print(f'[{label}] Full report: {path}', flush=True)
    print(text[:max_chars], flush=True)
    if len(text) > max_chars:
        print(f'[{label}] Excerpt truncated; see the full report above.', flush=True)


def diagnostic_excerpt(text: str, limit: int) -> str:
    """Keep the start and final traceback within an explicit prompt/log budget."""
    if len(text) <= limit:
        return text
    marker = '\n[... excerpt truncated; full log retained ...]\n'
    if limit <= len(marker):
        return text[-limit:] if limit else ''
    head = (limit - len(marker)) // 3
    tail = limit - len(marker) - head
    return text[:head] + marker + text[-tail:]


def developer_blocker(output: str) -> str | None:
    """A completion claim requests verification; it never grants completion."""
    markers = re.findall(r'^[ \t]*TASK_DEV_STATUS:[ \t]*(\w+)[ \t]*$', output, re.M)
    if not markers:
        return None
    final = output.strip().splitlines()[-1].strip()
    if len(markers) != 1 or not re.fullmatch(r'TASK_DEV_STATUS:[ \t]*' + re.escape(markers[0]), final):
        return 'DEVELOPER_PROTOCOL'
    if markers[0] in ('COMPLETE', 'COMPLETED', 'IMPLEMENTED', 'SUCCESS'):
        return None
    return markers[0] if markers[0] in ('SCOPE_REQUIRED', 'ENVIRONMENT_BLOCKED') else 'DEVELOPER_PROTOCOL'


def test_suite_is_empty(stdout: str, stderr: str = '') -> bool:
    """Use the final runner summary, rather than quoted/nested empty-suite output.

    Unittest writes its real summary to stderr. Earlier stdout can contain test
    fixtures that exercise the empty-suite guard, including their literal logs.
    """
    summaries = list(re.finditer(r'^Ran\s+(?P<unittest>\d+)\s+tests?\b[^\n]*$'
                                r'|^collected\s+(?P<collected>\d+)\s+items?\b[^\n]*$'
                                r'|^=*\s*(?P<pytest>\d+)\s+(?:passed|failed|skipped|xfailed|xpassed|errors?)\b[^\n]*$'
                                r'|^(?:=+\s*)?(?P<empty>no tests ran)\b[^\n]*$', stdout + '\n' + stderr, re.M))
    if not summaries:
        return False
    final = summaries[-1]
    return final['empty'] is not None or (final['unittest'] or final['collected'] or final['pytest']) == '0'


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_document(name: str, document: Any) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise PlanError('Task breakdown requires requirements.txt in the SSDLC Python environment.') from exc
    schema = json.loads((SCHEMA_DIR / f'{name}.schema.json').read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda e: str(list(e.path)))
    if errors:
        issues = [{'code': 'INVALID_SCHEMA', 'field': list(error.path), 'message': error.message} for error in errors]
        raise PlanError(f'{name}: {errors[0].message}', issues)


def confined(root: Path, relative: str, *, protect: bool = True) -> Path:
    """Accept literal repository paths, including new files, without symlink escapes."""
    path = Path(relative)
    if (not relative or path.is_absolute() or '..' in path.parts or '\\' in relative
            or any(ord(ch) < 32 for ch in relative) or relative.startswith(':')
            or any(ch in relative for ch in '*?[]')):
        raise PlanError(f'Unsafe path: {relative!r}')
    if protect and any(part in PROTECTED for part in path.parts):
        raise PlanError(f'Protected path: {relative}')
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise PlanError(f'Path escapes repository: {relative}')
    return resolved


def requirement_ids(text: str) -> set[str]:
    try:
        return set(requirement_catalogue(text)['required_ids'])
    except CatalogueError as exc:
        raise PlanError(str(exc), exc.diagnostics) from exc


def validate_advisory(document: dict, artifacts: Path) -> dict[str, str]:
    """Validate a projection's provenance and return evidence hashes for recording."""
    validate_document('advisory-proposals', document)
    prefix = document['project_id'] + '/' + document['ticket_id'] + '/'
    seen = set()
    hashes = {}
    for proposal in document['proposals']:
        if not proposal['id'].startswith(prefix) or proposal['id'] in seen:
            raise PlanError('Duplicate or unqualified advisory proposal ID')
        seen.add(proposal['id'])
        for ref in proposal['source_artifact_refs'] + proposal['evidence_refs']:
            path = confined(artifacts, ref.split('#')[0], protect=False)
            if not path.is_file():
                raise PlanError(f'Missing advisory evidence: {ref}')
            hashes[ref] = file_hash(path)
        for name in proposal['target_modules']:
            confined(artifacts, name)
    return hashes


class TaskPlanValidator:
    def __init__(self, repo: Path, requirements: str, quality: dict, artifacts: Path | None = None):
        self.repo = repo.resolve()
        try:
            self.requirement_catalogue = requirement_catalogue(requirements)
        except CatalogueError as exc:
            raise PlanError(str(exc), exc.diagnostics) from exc
        self.requirements = set(self.requirement_catalogue['required_ids'])
        self.quality = quality
        self.artifacts = artifacts

    def validate(self, plan: dict) -> list[dict]:
        validate_document('quality-contract', self.quality)
        validate_document('plan', plan)
        self._validate_references(plan)
        if plan['base_commit'] != self.quality['base_commit']:
            raise PlanError('Plan and quality contract have different base commits')
        if not any(ref.startswith('AC-') for ref in self.requirements):
            raise PlanError('No acceptance criteria found in requirements')
        self._validate_quality()
        from ssdlc_grader import protected_paths
        grader_paths = protected_paths(self.repo, plan['base_commit'])
        if grader_paths:
            from ssdlc_grader import guard_baseline
            guard_baseline()
        tasks = {t['task_id']: t for t in plan['tasks']}
        if len(tasks) != len(plan['tasks']):
            raise PlanError('Duplicate task_id')
        checks = {c['check_id'] for c in self.quality['checks']}
        standards = {s['standard_id'] for s in self.quality['standards']}
        for task in tasks.values():
            deps = set(task['depends_on'])
            if not deps <= tasks.keys() or task['task_id'] in deps:
                raise PlanError(f'Unknown or self dependency: {task["task_id"]}')
            for path in task['target_files']:
                confined(self.repo, path)
                path = Path(path).as_posix()
                if path in grader_paths or any(path.startswith(p) for p in grader_paths if p.endswith('/')):
                    raise PlanError('Replay grader baseline requires an explicit reviewed revision, outside candidate tasks')
                if path == '.' or (self.repo / path).is_dir():
                    raise PlanError('target_files must name files, not directories')
            if not set(task['standard_refs']) <= standards:
                raise PlanError('Unknown standard reference')
            # Applicable standards cannot be omitted merely by leaving standard_refs empty.
            from fnmatch import fnmatchcase
            applicable = {s['standard_id'] for s in self.quality['standards']
                          if any(fnmatchcase(path, pattern) for path in task['target_files'] for pattern in s['scope'])}
            if not applicable <= set(task['standard_refs']):
                raise PlanError(f'Missing applicable standards: {applicable - set(task["standard_refs"])}')
            verify = task['verification']
            if not set(verify['quality_check_refs']) <= checks:
                raise PlanError('Unknown quality check reference')
            if not checks <= set(verify['quality_check_refs']):
                raise PlanError('All required quality checks must apply in the bootstrap')
            if verify['mode'] == 'automated' and not verify['checks']:
                raise PlanError('An automated task needs executable checks')
            if verify['mode'] == 'review_only' and not (verify.get('reason') and verify.get('review_criteria')):
                raise PlanError('review_only needs a reason and review criteria')
            if task['kind'] == 'subgoal' and not task.get('subgoal_reason'):
                raise PlanError('Subgoal needs a reason')
            ids = [c['check_id'] for c in verify['checks']]
            if len(set(ids)) != len(ids):
                raise PlanError('Duplicate task check_id')
            for check in verify['checks']:
                self._check_command(check)
        pending = dict(tasks)
        ordered = []
        done: set[str] = set()
        while pending:
            ready = sorted(k for k, t in pending.items() if set(t['depends_on']) <= done)
            if not ready:
                raise PlanError('Cycle in task dependencies')
            for key in ready:
                ordered.append(pending.pop(key))
                done.add(key)
        return ordered

    def _check_command(self, check: dict) -> None:
        cwd = confined(self.repo, check['cwd'])
        if not cwd.is_dir():
            raise PlanError(f'Check directory does not exist: {check["cwd"]}')
        if any('\x00' in arg for arg in check['argv']):
            raise PlanError('NUL in command arguments')

    def _validate_references(self, plan: dict) -> None:
        """Report all ownership, coverage and contract defects in one repair round."""
        issues = []
        covered = set()
        for task in plan['tasks']:
            location = {'task_id': task['task_id']}
            if task['kind'] != 'task' or task['verification']['mode'] != 'automated':
                issues.append({**location, 'code': 'UNSUPPORTED_EXECUTION_MODE',
                               'field': 'kind/verification.mode',
                               'message': 'Bootstrap requires kind=task and automated verification'})
            checked = {'ac_refs': set(), 'sec_refs': set()}
            for owner, prefix, where in [(task, '', location)] + [
                (check, 'verification.checks.', {**location, 'check_id': check['check_id']})
                for check in task['verification']['checks']
            ]:
                for field, kind in [('ac_refs', 'AC-'), ('sec_refs', 'SEC-')]:
                    refs = set(owner[field])
                    unknown = sorted(refs - self.requirements)
                    if unknown:
                        issues.append({**where, 'code': 'UNKNOWN_REQUIREMENT_REF',
                                       'field': prefix + field, 'unknown_refs': unknown})
                    wrong = sorted(ref for ref in refs if not ref.startswith(kind))
                    if wrong:
                        issues.append({**where, 'code': 'WRONG_REQUIREMENT_KIND',
                                       'field': prefix + field, 'offending_refs': wrong})
                    valid = {ref for ref in refs & self.requirements if ref.startswith(kind)}
                    if owner is task:
                        covered |= valid
                    else:
                        outside = sorted(refs - set(task[field]))
                        if outside:
                            issues.append({**where, 'code': 'CHECK_REFS_OUTSIDE_TASK',
                                           'field': prefix + field, 'offending_refs': outside,
                                           'allowed_refs': sorted(task[field])})
                        checked[field] |= valid & set(task[field])
            missing = sorted(set(task['ac_refs'] + task['sec_refs']) - (checked['ac_refs'] | checked['sec_refs']))
            if task['verification']['mode'] == 'automated' and missing:
                issues.append({**location, 'code': 'MISSING_VERIFICATION_REF',
                               'field': 'verification.checks', 'missing_refs': missing})
            for ref in task['contract_refs']:
                try:
                    self._artifact_ref(ref)
                except PlanError as exc:
                    issues.append({**location, 'code': 'INVALID_CONTRACT_REF', 'field': 'contract_refs',
                                   'ref': ref, 'message': str(exc),
                                   'available_refs': self.available_contract_refs()})
        missing = sorted(self.requirements - covered)
        if missing:
            issues.append({'code': 'UNCOVERED_REQUIREMENTS', 'field': 'tasks', 'missing_refs': missing,
                           'coverage_policy': self.requirement_catalogue['coverage_policy']})
        if issues:
            messages = ['Invalid task requirement/check mappings']
            if missing:
                messages.append(f'Uncovered requirements: {missing}')
            messages.extend(i['message'] for i in issues if 'message' in i)
            raise PlanError('; '.join(messages), issues)

    def available_contract_refs(self) -> list[str]:
        """Published design files and anchors, relative to 02-design (no aliases)."""
        if self.artifacts is None:
            return []
        root = self.artifacts / '02-design'
        refs = []
        for path in sorted(root.glob('*.md')):
            # Do not catalogue symlinks that escape the approved artifact directory.
            try:
                target = confined(root, path.name, protect=False)
            except PlanError:
                continue
            if target.is_file():
                refs.append(path.name)
                refs.extend(path.name + '#' + anchor for anchor in heading_anchors(target.read_text()))
        return refs

    def _artifact_ref(self, ref: str) -> None:
        path, _, anchor = ref.partition('#')
        if self.artifacts is None:
            raise PlanError('Contract references need an artifact root')
        target = confined(self.artifacts / '02-design', path, protect=False)
        if not target.is_file():
            raise PlanError(f'Missing contract: {ref}')
        if anchor:
            if anchor not in heading_anchors(target.read_text()):
                raise PlanError(f'Missing contract anchor: {ref}')

    def _validate_quality(self) -> None:
        sources = {s['source_id'] for s in self.quality['sources']}
        if len(sources) != len(self.quality['sources']):
            raise PlanError('Duplicate quality source_id')
        for source in self.quality['sources']:
            path = confined(self.repo, source['path'], protect=False)
            if not path.is_file() or file_hash(path) != source['sha256']:
                raise PlanError(f'Quality source changed or disappeared: {source["path"]}')
        for std in self.quality['standards']:
            if not set(std['source_refs']) <= sources:
                raise PlanError('Unknown standard source')
        for field, key in [('standards', 'standard_id'), ('checks', 'check_id')]:
            ids = [item[key] for item in self.quality[field]]
            if len(ids) != len(set(ids)):
                raise PlanError(f'Duplicate {key}')
        for check in self.quality['checks']:
            self._check_command(check)
        baseline_ids = [b['check_id'] for b in self.quality['baseline']]
        if set(baseline_ids) != {c['check_id'] for c in self.quality['checks']} or len(baseline_ids) != len(set(baseline_ids)):
            raise PlanError('Baseline must cover every quality check once')
        if any(b['exit_code'] != 0 for b in self.quality['baseline']):
            raise PlanError('Bootstrap requires a passing baseline; regression baselines need Step 3 policy')
        if self.artifacts:
            for baseline in self.quality['baseline']:
                log = confined(self.artifacts, baseline['log_ref'], protect=False)
                if not log.is_file() or file_hash(log) != baseline['sha256']:
                    raise PlanError('Missing or altered baseline evidence')


@contextlib.contextmanager
def ticket_lock(path: Path):
    """OS-held locks disappear when the process exits; stale files are harmless."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TaskError('Another process is running this ticket') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class TaskJournal:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'task-journal.jsonl'
        self.snapshot = directory / 'tasks-state.json'
        self.sequence = 0
        self.previous = ''

    def load(self) -> dict | None:
        state = None
        self.sequence, self.previous = 0, ''
        if self.path.exists():
            data = self.path.read_bytes()
            if data and not data.endswith(b'\n'):
                # Only an incomplete tail is recoverable; retain it for audit.
                boundary = data.rfind(b'\n') + 1
                tail = data[boundary:]
                (self.directory / f'interrupted-tail-{hashlib.sha256(tail).hexdigest()}.bin').write_bytes(tail)
                with self.path.open('r+b') as stream:
                    stream.truncate(boundary)
                    stream.flush()
                    os.fsync(stream.fileno())
                data = data[:boundary]
            state, self.sequence, self.previous = self._replay(data)
        if state is None and self.snapshot.exists():
            raise TaskError('Task journal is missing; snapshot cannot authorize completed work')
        if state is not None:
            atomic_json(self.snapshot, state)
        return state

    @staticmethod
    def _replay(data: bytes) -> tuple[dict | None, int, str]:
        """Verify the hash chain and return its last state and append position."""
        state = None
        sequence, previous = 0, ''
        for line in data.splitlines():
            try:
                event = json.loads(line)
                signature = event.pop('sha256')
                if (signature != digest(event) or event['sequence'] != sequence + 1
                        or event['previous'] != previous):
                    raise ValueError('broken hash chain')
            except (ValueError, KeyError, TypeError) as exc:
                raise TaskError('Corrupt task journal; refusing recovery') from exc
            sequence, previous = event['sequence'], signature
            state = event['state']
        return state, sequence, previous

    def peek(self) -> dict | None:
        """Read the journal state without repairing files or changing append position."""
        if not self.path.exists():
            return None
        data = self.path.read_bytes()
        data = data[:data.rfind(b'\n') + 1]
        return self._replay(data)[0]

    def append(self, kind: str, state: dict) -> None:
        event = {'sequence': self.sequence + 1, 'previous': self.previous, 'event': kind, 'state': state}
        signature = digest(event)
        event['sha256'] = signature
        with self.path.open('ab') as stream:
            stream.write((json.dumps(event, sort_keys=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        self.sequence += 1
        self.previous = signature
        atomic_json(self.snapshot, state)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(['git', '--literal-pathspecs', *args], cwd=repo, capture_output=True, text=True)
    if proc.returncode:
        raise TaskError(f'git {args[0]} failed: {proc.stderr.strip()}')
    return proc.stdout.rstrip('\n')


@dataclass
class RevalidationResult:
    passed: list[str]
    failed: list[dict]
    blocked: list[dict]
    unchanged: list[str]
    finalized_run: int | None = None

    @property
    def complete(self):
        return not self.failed and not self.blocked


class TaskScheduler:
    """Bootstrap executor: strict manifests, existing routing, no recursive work."""
    def __init__(self, worktree: Path, directory: Path, plan: dict, quality: dict,
                 context: dict[str, str], developer: Callable, verifier: Callable,
                 guard: Callable = lambda: None, budget: Callable = lambda: None,
                 max_attempts: int = 3, isolate_candidates: bool = False,
                 runtime_identity: dict | None = None, recover_timeouts: bool = False,
                 historical_capture_authority=None, dispatch_budget=None, historical_task_floors=None,
                 scope_reconciliations=None):
        self.wt = worktree
        self.directory = directory
        self.plan = plan
        self.quality = quality
        self.context = context
        self.developer = developer
        self.verifier = verifier
        self.guard = guard
        self.budget = budget
        self.max_attempts = max_attempts
        self.isolate_candidates = isolate_candidates
        self.recover_timeouts = recover_timeouts
        self.historical_task_floors = copy.deepcopy(historical_task_floors or {})
        self.historical_capture_authority = historical_capture_authority
        self.scope_reconciliations = copy.deepcopy(scope_reconciliations or {})
        self.dispatch_budget = dispatch_budget or budget
        self.runtime_identity = copy.deepcopy(runtime_identity or {})
        from ssdlc_grader import protected_paths
        self.grader_paths = protected_paths(worktree, plan['base_commit'])
        if self.grader_paths:
            from ssdlc_grader import guard_baseline
            guard_baseline()
        if max_attempts < 1:
            raise PlanError('max_attempts must be positive')
        self.journal = TaskJournal(directory)
        self.state: dict = {}

    def _save(self, event: str) -> None:
        self.journal.append(event, self.state)

    def _assert_candidate_runtime(self, state):
        recorded = {tid: row['scope_reconciliation'] for tid, row in state['tasks'].items()
                    if 'scope_reconciliation' in row}
        if recorded != self.scope_reconciliations:
            raise DevelopmentBlocked('INTEGRITY', 'Scope reconciliation has no transition authority')
        for tid, floor in self.historical_task_floors.items():
            record = state['tasks'][tid]
            if record['attempts'] < floor['attempts']:
                raise DevelopmentBlocked('INTEGRITY', 'Historical development attempts reset')
            for key in ('failed_attempts', 'capture_verifications', 'reconciliation_required'):
                prefix = floor.get(key, [])
                if record.get(key, [])[:len(prefix)] != prefix:
                    raise DevelopmentBlocked('INTEGRITY', 'Historical scheduler recovery allowance reset')
        historical = state.get('historical_capture_bindings')
        if historical and (self.historical_capture_authority is None or digest(historical) != self.historical_capture_authority):
            raise DevelopmentBlocked('INTEGRITY', 'Historical capture bindings have no transition authority')
        if state.get('bounded_recovery', 0) != int(self.recover_timeouts):
            raise DevelopmentBlocked('INTEGRITY', 'Bounded recovery mode changed; explicit migration required')
        mode = state.get('candidate_isolation', 0)
        if type(mode) is not int or mode != int(self.isolate_candidates):
            raise TaskError('Candidate isolation mode changed; explicit controller migration is required')
        if self.isolate_candidates and state.get('candidate_runtime') != self.runtime_identity:
            raise TaskError('Candidate runtime identity changed; explicit controller migration is required')

    def _plan_index(self) -> dict[str, dict]:
        """Index the upstream-validated plan with consistent malformed-plan errors."""
        try:
            return {task['task_id']: task for task in self.plan['tasks']}
        except (KeyError, TypeError) as exc:
            raise TaskError('Malformed plan') from exc

    def _closure(self, roots: set[str]) -> tuple[list[str], dict[str, int]]:
        tasks = self._plan_index()
        order = {tid: index for index, tid in enumerate(tasks)}
        reverse = {tid: [] for tid in tasks}
        try:
            for tid, task in tasks.items():
                for dependency in task['depends_on']:
                    if dependency not in tasks:
                        raise TaskError('Plan has unknown dependency')
                    reverse[dependency].append(tid)
        except (KeyError, TypeError) as exc:
            raise TaskError('Malformed plan') from exc
        visited = set(roots)
        pending = deque(roots)
        while pending:
            for dependent in reverse[pending.popleft()]:
                if dependent not in visited:
                    visited.add(dependent)
                    pending.append(dependent)
        return [tid for tid in tasks if tid in visited], order

    def _require_all_completed(self, state: dict) -> None:
        tasks = self._plan_index()
        for tid in tasks:
            if tid not in state['tasks']:
                raise TaskError(f"Task '{tid}' has no scheduler state")
        for tid in state['tasks']:
            if tid not in tasks:
                raise TaskError(f"State task '{str(tid)[:80]}' is not in the plan")
        for tid in tasks:
            status = state['tasks'][tid].get('status')
            if status != 'COMPLETED':
                raise TaskError(f"Cannot invalidate: task '{tid}' is {status}; "
                                'only fully COMPLETED plans can be invalidated')

    def invalidate(self, task_ids, reason) -> list[str]:
        """Invalidate selected tasks and their dependents in one journal event.

        SHA-256 chaining is integrity-only, not authentication; edits to preexisting
        ignored-file contents are not detected. Plan DAG validity is an upstream
        precondition. Discard the instance after an exception.
        Rollback: a journal containing TASKS_INVALIDATED is not guaranteed to load
        in pre-change code; restore the pre-change journal and snapshot from backup.
        """
        if (not isinstance(task_ids, Collection)
                or isinstance(task_ids, (str, bytes, bytearray, Mapping)) or not task_ids):
            raise TaskError('task_ids must be a non-empty collection of task ID strings (not a string or mapping)')
        ids = list(task_ids)
        if not isinstance(reason, str) or not reason.strip():
            raise TaskError('reason must be a non-empty string')
        if any(not isinstance(tid, str) or not tid for tid in ids):
            raise TaskError('task_ids must contain only non-empty strings')
        known = self._plan_index()
        unknown = sorted(set(ids) - known.keys())
        if unknown:
            raise TaskError(f'Unknown task ID: {unknown[0][:80]!r}')
        roots = set(ids)
        closure, order = self._closure(roots)
        with ticket_lock(self.directory / 'scheduler.lock'):
            peeked = self.journal.peek()
            if peeked is None:
                raise TaskError('No scheduler state to invalidate')
            self._assert_candidate_runtime(peeked)
            inputs = digest({'plan': self.plan, 'quality': self.quality, 'context': self.context})
            if peeked['inputs_sha256'] != inputs:
                raise TaskError('Inputs changed: explicit plan migration/invalidation is required')
            self._require_all_completed(peeked)
            state = self.journal.load()
            self.state = state
            for tid, record in state['tasks'].items():
                if 'protected_snapshot' in record and self._protected() != record['protected_snapshot']:
                    raise TaskError('Protected worktree paths changed; reconciliation required')
                if not self._evidence_valid(record, tid):
                    raise TaskError('Completed task evidence is missing or altered')
            self._assert_clean()
            self.state = copy.deepcopy(state)
            for tid in closure:
                self.state['tasks'][tid]['status'] = 'INVALIDATED'
            self.state.setdefault('invalidations', []).append(
                {'roots': sorted(roots, key=order.__getitem__), 'closure': closure, 'reason': reason.strip()})
            self._save('TASKS_INVALIDATED')
        return closure

    def _require_revalidatable(self, state):
        self._assert_candidate_runtime(state)
        tasks = self._plan_index()
        if set(tasks) != set(state['tasks']):
            raise TaskError('Cannot revalidate: plan and state task sets differ')
        for tid, record in state['tasks'].items():
            if record.get('status') not in ('COMPLETED', 'INVALIDATED'):
                raise TaskError(f"Cannot revalidate: task '{tid}' is {record.get('status')}")
        if state['inputs_sha256'] != digest({'plan': self.plan, 'quality': self.quality,
                                             'context': self.context}):
            raise TaskError('Inputs changed: explicit plan migration/invalidation is required')

    def _validate_revalidation_graph(self, state):
        tasks = self._plan_index()
        remaining = {}
        for tid, task in tasks.items():
            dependencies = task['depends_on']
            if any(dep not in tasks for dep in dependencies):
                raise TaskError('Plan has unknown dependency')
            remaining[tid] = set(dependencies)
            for dep in dependencies:
                if (state['tasks'][tid]['status'] == 'COMPLETED'
                        and state['tasks'][dep]['status'] != 'COMPLETED'):
                    raise TaskError(f"Inconsistent state: task '{tid}' is COMPLETED but "
                                    f"dependency '{dep}' is {state['tasks'][dep]['status']}")
        order = []
        while remaining:
            tid = next((tid for tid, deps in remaining.items() if not deps), None)
            if tid is None:
                raise TaskError('Plan dependency graph is not acyclic')
            order.append(tid)
            del remaining[tid]
            for deps in remaining.values():
                deps.discard(tid)
        return order

    def revalidate(self) -> RevalidationResult:
        """Restore invalidated tasks using verification only, preserving provenance.

        Hashes and fingerprints are unkeyed integrity checks, not authentication.
        Ignored-file contents are not verified. The lock does not exclude arbitrary
        external writers; TOCTOU windows remain. Integrity coverage is I1-I5:
        original checks, failed-attempt checks and diffs, global preflight logs,
        and revalidation checks. Uncovered artifacts include developer.log,
        manifest.json, successful implementation.diff, unreferenced partial logs,
        interrupted-tail backups and quality-contract baseline logs.
        Discard the instance after any exception. For rollback restore the prior
        journal and snapshot from backup; older code may not validate this evidence.
        """
        with ticket_lock(self.directory / 'scheduler.lock'):
            self.guard()
            peeked = self.journal.peek()
            if peeked is None:
                raise TaskError('No scheduler state to revalidate')
            self._require_revalidatable(peeked)
            self.state = copy.deepcopy(self.journal.load())
            self._require_revalidatable(self.state)
            order = self._validate_revalidation_graph(self.state)
            for tid, record in self.state['tasks'].items():
                numbers, directories = set(), set()
                for attempt in record.get('revalidations', []):
                    n, directory = attempt['attempt'], attempt['dir']
                    if n in numbers or directory in directories:
                        raise TaskError(f"Duplicate revalidation attempt {n} for task '{tid}'")
                    numbers.add(n)
                    directories.add(directory)
                if ('protected_snapshot' in record
                        and self._protected() != record['protected_snapshot']):
                    raise TaskError('Protected worktree paths changed; reconciliation required')
            problems = self._inventory_problems(self.state)
            if problems:
                problem = problems[0]
                raise TaskError(f"Evidence invalid for task '{problem['task_id']}' check "
                                f"'{problem['check_id']}': {problem['reason']} ({problem['path']})")
            self._assert_clean()
            root_stat = self.directory.stat()
            self._revalidation_root_identity = (str(self.directory.resolve()), root_stat.st_dev, root_stat.st_ino)
            head = self.state['expected_head']
            tree = git(self.wt, 'rev-parse', 'HEAD^{tree}')
            protected, ignored = self._protected(), self._ignored()
            tasks = self._plan_index()
            visit = [tid for tid in order if self.state['tasks'][tid]['status'] == 'INVALIDATED']
            result = RevalidationResult([], [], [], [tid for tid in order if tid not in visit])
            runs = self.state.get('revalidation_runs', [])
            if not visit:
                if any(a['status'] == 'RUNNING' for record in self.state['tasks'].values()
                       for a in record.get('revalidations', [])):
                    raise TaskError('Completed task has a RUNNING revalidation attempt')
                for open_run in runs:
                    if open_run['status'] == 'RUNNING':
                        open_run['status'] = 'COMPLETE'
                        open_run['finalized_on_recovery'] = True
                        open_run['cursor'] = dict(task_id=None, attempt=None, check_id=None,
                                                  head=head, tree=tree, dir=None)
                        self._save('REVALIDATION_FINISHED')
                        result.finalized_run = open_run['run']
                return result
            # An incomplete invocation is never resumed in its old evidence directory.
            opened = False
            for run in runs:
                if run['status'] == 'RUNNING':
                    run['status'] = 'INTERRUPTED'
                    run['cursor'] = dict(task_id=None, attempt=None, check_id=None,
                                         head=run['head'], tree=run['tree'], dir=None)
                    opened = True
            for record in self.state['tasks'].values():
                for attempt in record.get('revalidations', []):
                    if attempt['status'] == 'RUNNING':
                        attempt['status'] = 'INTERRUPTED'
                        opened = True
            if opened:
                self._save('REVALIDATION_INTERRUPTED')
            run = dict(run=max((r['run'] for r in runs), default=0) + 1,
                       head=head, tree=tree, roots=visit, status='RUNNING', blocked={})
            self.state.setdefault('revalidation_runs', []).append(run)

            def save(event, tid=None, attempt=None, check_id=None):
                run['cursor'] = dict(task_id=tid, attempt=attempt['attempt'] if attempt else None,
                                     check_id=check_id, head=head, tree=tree,
                                     dir=attempt['dir'] if attempt else None)
                self._save(event)

            save('REVALIDATION_STARTED')
            for tid in visit:
                record = self.state['tasks'][tid]
                blocked = [dep for dep in tasks[tid]['depends_on']
                           if self.state['tasks'][dep]['status'] != 'COMPLETED']
                if blocked:
                    result.blocked.append(dict(task_id=tid, blocked_by=blocked))
                    run['blocked'][tid] = blocked
                    save('REVALIDATION_TASK_BLOCKED', tid)
                    continue
                attempts = record.setdefault('revalidations', [])
                n = max((a['attempt'] for a in attempts), default=0) + 1
                while os.path.lexists(self.directory / tid / f'revalidate-{n}'):
                    n += 1
                directory = self.directory / tid / f'revalidate-{n}'
                attempt = dict(attempt=n, run=run['run'], status='RUNNING', head=head,
                               tree=tree, dir=str(directory.relative_to(self.directory)),
                               started_checks=[], checks=[])
                attempts.append(attempt)
                save('REVALIDATION_TASK_STARTED', tid, attempt)

                def before(index, check_id):
                    attempt['started_checks'].append(dict(index=index, check_id=check_id))
                    save('REVALIDATION_CHECK_STARTED', tid, attempt, check_id)

                def gate(index=None, check=None):
                    check_id = check['check_id'] if check else None
                    if (git(self.wt, 'rev-parse', 'HEAD') != head or self._changed()
                            or self._protected() != protected or self._ignored() != ignored):
                        raise _VerifierMutation('Verifier changed worktree', tid, check_id)
                    problems = self._inventory_problems(self.state)
                    if problems:
                        p = problems[0]
                        raise _EvidenceChanged(f"Evidence changed for task {p['task_id']!r}: {p['reason']}",
                                               tid, check_id, p['path'])
                    self._assert_evidence_dir(directory, tid, check_id)

                try:
                    try:
                        self._make_attempt_dir(directory)
                    except _RevalidationStop as exc:
                        raise type(exc)('Cannot create evidence directory', tid, path=exc.path) from exc
                    checks = self._verify(tasks[tid], attempt, directory,
                                          event='REVALIDATION_CHECK_FINISHED',
                                          before_check=before, after_check=gate, exclusive=True)
                    gate()
                except _RevalidationStop as exc:
                    attempt['status'] = 'ABORTED'
                    attempt['abort'] = dict(kind=exc.kind, check_id=exc.check_id,
                                            path=exc.path, message=str(exc))
                    run['status'] = 'ABORTED'
                    save('REVALIDATION_ABORTED', tid, attempt, exc.check_id)
                    raise
                if ([c['check_id'] for c in checks] == self._expected_check_ids(tid)
                        and all(c['exit_code'] == 0 and not c['empty_suite'] for c in checks)):
                    attempt['status'] = 'PASSED'
                    record['restored_by'] = n
                    record['status'] = 'COMPLETED'
                    result.passed.append(tid)
                    save('REVALIDATION_TASK_PASSED', tid, attempt)
                else:
                    attempt['status'] = 'FAILED'
                    diagnostics = [dict(c, outcome=('empty_suite' if c['empty_suite'] else
                                   f"exit:{c['exit_code']}" if c['exit_code'] else 'passed')) for c in checks]
                    result.failed.append(dict(task_id=tid, attempt=n,
                                              evidence_dir=attempt['dir'], checks=diagnostics))
                    save('REVALIDATION_TASK_FAILED', tid, attempt)
            run['status'] = 'COMPLETE' if result.complete else 'INCOMPLETE'
            save('REVALIDATION_FINISHED')
            return result

    def _changed(self) -> list[str]:
        out = git(self.wt, '-c', 'status.renames=false', 'status', '--porcelain=v1', '-z', '--untracked-files=all')
        return [line[3:] for line in out.split('\x00') if line]

    def _protected(self) -> dict:
        result = {}
        for name in PROTECTED | self.grader_paths:
            root = self.wt / name
            if root.is_symlink():
                result[name] = ['symlink', os.readlink(root)]
            elif root.is_file():
                result[name] = file_hash(root)
            elif root.is_dir():
                for path in sorted(root.rglob('*')):
                    if path.is_symlink():
                        result[str(path.relative_to(self.wt))] = ['symlink', os.readlink(path)]
                    elif path.is_file():
                        result[str(path.relative_to(self.wt))] = file_hash(path)
        return result

    def _assert_tree(self) -> None:
        if git(self.wt, 'rev-parse', 'HEAD') != self.state['expected_head']:
            raise TaskError('Unexpected worktree HEAD; reconcile external changes before resuming')

    def _assert_clean(self) -> None:
        self._assert_tree()
        if self._ignored() != self.state['ignored_paths']:
            raise TaskError('Ignored worktree files changed; explicit reconciliation required')
        if self._changed():
            raise TaskError('Interrupted or externally modified worktree is dirty; preserve and reconcile it before resuming')

    def _source_signature(self, changed: list[str]) -> str:
        contents = {}
        for name in changed:
            path = self.wt / name
            contents[name] = (os.readlink(path) if path.is_symlink() else
                              file_hash(path) if path.is_file() else None)
        return digest({'diff': git(self.wt, 'diff', 'HEAD', '--binary'), 'contents': contents})

    def _ignored(self) -> list[str]:
        return sorted(path for path in git(self.wt, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z').split('\x00') if path)

    def _check_plan(self, task: dict) -> list[dict]:
        return task['verification']['checks'] + self.quality['checks']

    def _expected_check_ids(self, task_id) -> list[str]:
        return [check['check_id'] for check in self._check_plan(self._plan_index()[task_id])]

    def _assert_evidence_dir(self, path, task_id=None, check_id=None):
        try:
            expected_root = getattr(self, '_revalidation_root_identity', None)
            root_stat = self.directory.stat()
            actual_root = (str(self.directory.resolve()), root_stat.st_dev, root_stat.st_ino)
            if expected_root is not None and actual_root != expected_root:
                raise _EvidencePathError('Evidence root changed during verification', task_id, check_id, str(self.directory))
            relative = path.relative_to(self.directory)
            current = self.directory
            for part in relative.parts:
                current = current / part
                if not stat.S_ISDIR(os.lstat(current).st_mode):
                    raise _EvidencePathError('Invalid evidence directory', task_id, check_id, str(current))
        except _RevalidationStop:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise _EvidencePathError('Invalid evidence directory', task_id, check_id, str(path)) from exc

    def _make_attempt_dir(self, path):
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise _EvidenceCollision('Evidence directory exists', path=str(path)) from exc
        self._assert_evidence_dir(path)

    def _verify(self, task: dict, record: dict, attempt_dir: Path, event='CHECK_FINISHED',
                before_check=None, after_check=None, exclusive=False) -> list[dict]:
        checks = self._check_plan(task)
        results = []
        for index, check in enumerate(checks):
            self.budget()
            self.guard()
            cwd = confined(self.wt, check['cwd'])
            if before_check is not None:
                before_check(index, check['check_id'])
            try:
                if self.isolate_candidates:
                    out, err, rc = self._isolated_check(check, cwd)
                else:
                    out, err, rc = self.verifier(check['argv'], cwd, check['timeout_sec'])
            except subprocess.TimeoutExpired as exc:
                out, err, rc = '', f'Verification timeout: {exc}', 124
            log = attempt_dir / f'check-{index}.log'
            if exclusive:
                self._assert_evidence_dir(attempt_dir, task.get('task_id'), check['check_id'])
                try:
                    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                    with os.fdopen(fd, 'w') as stream:
                        stream.write(out + '\n' + err)
                        stream.flush()
                        os.fsync(stream.fileno())
                    directory_fd = os.open(attempt_dir, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        raise _EvidenceCollision('Evidence log exists', task.get('task_id'),
                                                 check['check_id'], str(log)) from exc
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise _EvidencePathError('Invalid evidence path', task.get('task_id'),
                                                 check['check_id'], str(log)) from exc
                    raise
            else:
                log.write_text(out + '\n' + err)
            empty = test_suite_is_empty(out, err)
            results.append({'check_id': check['check_id'], 'exit_code': rc, 'empty_suite': empty,
                            'log_ref': str(log.relative_to(self.directory)), 'sha256': file_hash(log)})
            record['checks'] = results
            self._save(event)
            if after_check is not None:
                after_check(index, results[-1])
            self.guard()
            self.budget()
        return results

    def _resolve_evidence(self, ref, expected_sha):
        try:
            path = confined(self.directory, ref, protect=False)
            if not path.is_file():
                return path, 'missing_file'
            return path, None if file_hash(path) == expected_sha else 'hash_mismatch'
        except PlanError:
            return None, 'unsafe_or_escaping_path'
        except RuntimeError:
            return None, 'symlink_loop'
        except OSError as exc:
            return None, 'unreadable:' + errno.errorcode.get(exc.errno, str(exc.errno))
        except (UnicodeDecodeError, ValueError):
            return None, 'malformed_reference'

    def _evidence_problems(self, record, task_id=None):
        problems = []

        def problem(reason, check_id=None, path=None):
            problems.append(dict(task_id=task_id, check_id=check_id, path=path, reason=reason))

        def inspect(item, ref_key='log_ref', sha_key='sha256'):
            check_id, ref = None, None
            try:
                if not isinstance(item, dict):
                    raise TypeError
                check_id, ref = item.get('check_id'), item[ref_key]
                sha = item[sha_key]
                if not isinstance(ref, str) or not isinstance(sha, str):
                    raise TypeError
                _, reason = self._resolve_evidence(ref, sha)
                if reason:
                    problem(reason, check_id, ref)
            except (KeyError, TypeError):
                problem('malformed_record', check_id, ref)

        try:
            if not isinstance(record, dict):
                raise TypeError
            checks = record.get('checks', [])
            if not isinstance(checks, list):
                raise TypeError
            if not checks:
                problem('missing_checks')
            for check in checks:
                if not isinstance(check, dict):
                    raise TypeError
                inspect(check)
            if record.get('capture_sha256'):
                from ssdlc_candidates import CandidateStore, CandidateError
                try:
                    directory = self.directory / task_id / f"attempt-{record['attempts']}"
                    CandidateStore(directory).load(self._capture_binding(self._plan_index()[task_id], record),
                                                   record['capture_sha256'])
                except (CandidateError, OSError, ValueError, KeyError, TypeError):
                    problem('capture_changed')
            for attempt in record.get('failed_attempts', []):
                for check in attempt['checks']:
                    inspect(check)
                inspect(attempt, 'diff_ref', 'diff_sha256')
                if attempt.get('capture_sha256'):
                    from ssdlc_candidates import CandidateStore, CandidateError
                    try:
                        directory = self.directory / task_id / f"attempt-{attempt['attempt']}"
                        binding_record = dict(record, attempts=attempt['attempt'], start_commit=attempt['base_commit'],
                                              invocation_id=attempt['invocation_id'])
                        CandidateStore(directory).load(self._capture_binding(self._plan_index()[task_id], binding_record),
                                                       attempt['capture_sha256'])
                    except (CandidateError, OSError, ValueError, KeyError, TypeError):
                        problem('capture_changed')
            numbers, directories = set(), set()
            attempts = record.get('revalidations', [])
            for attempt in attempts:
                number, directory = attempt['attempt'], attempt['dir']
                if (type(number) is not int or number <= 0 or number in numbers
                        or not isinstance(directory, str) or directory in directories):
                    problem('invalid_attempt_identity', path=directory)
                numbers.add(number)
                directories.add(directory)
                for check in attempt['checks']:
                    inspect(check)
            if record.get('status') == 'COMPLETED' and record.get('restored_by') is not None:
                supporting = [a for a in attempts if a['attempt'] == record['restored_by']]
                if len(supporting) != 1:
                    problem('invalid_supporting_attempt')
                else:
                    attempt = supporting[0]
                    expected = self._expected_check_ids(task_id)
                    if (attempt['status'] != 'PASSED'
                            or [c['check_id'] for c in attempt['checks']] != expected
                            or [c['check_id'] for c in attempt['started_checks']] != expected
                            or any(c['exit_code'] != 0 or c['empty_suite'] for c in attempt['checks'])
                            or attempt['head'] != self.state.get('expected_head') or not attempt['tree']):
                        problem('invalid_supporting_attempt', path=attempt['dir'])
        except (PlanError, OSError, RuntimeError, KeyError, TypeError, ValueError, UnicodeDecodeError):
            problem('malformed_record')
        return problems

    def _evidence_valid(self, record, task_id=None) -> bool:
        return not self._evidence_problems(record, task_id)

    def _validate_preflights(self, state):
        problems = []
        try:
            for attempt in state.get('preflights', []):
                # Empty interrupted preflights are allowed; recorded logs are not optional.
                checks = attempt['checks']
                if not isinstance(checks, list):
                    raise TypeError
                if checks:
                    problems.extend(self._evidence_problems({'checks': checks}))
        except (KeyError, TypeError, ValueError):
            problems.append(dict(task_id=None, check_id=None, path=None, reason='malformed_record'))
        return problems

    def _inventory_problems(self, state):
        problems = []
        try:
            for task_id, record in state['tasks'].items():
                problems.extend(self._evidence_problems(record, task_id))
        except (KeyError, TypeError, AttributeError):
            problems.append(dict(task_id=None, check_id=None, path=None, reason='malformed_record'))
        return problems + self._validate_preflights(state)

    def _preflight(self) -> None:
        """Run required checks at the clean checkpoint in the actual task worktree."""
        self._assert_clean()
        preflights = self.state.setdefault('preflights', [])
        record = {'head': self.state['expected_head'], 'status': 'RUNNING', 'checks': []}
        preflights.append(record)
        directory = self.directory / 'preflight' / f'run-{len(preflights)}'
        self._save('PREFLIGHT_STARTED')
        directory.mkdir(parents=True, exist_ok=False)
        protected = self._protected()
        print(f'[task-dev] Checkpoint preflight in {self.wt}; no developer attempts consumed.', flush=True)
        results = self._verify({'verification': {'checks': []}}, record, directory)
        if self._protected() != protected or self._changed() or self._ignored() != self.state['ignored_paths']:
            record['status'] = 'MUTATED'
            self._save('PREFLIGHT_BLOCKED')
            raise DevelopmentBlocked('BASELINE_MUTATION', f'Preflight changed source or metadata; evidence: {directory}')
        self._assert_tree()
        failed = [r for r in results if r['exit_code'] or r['empty_suite']]
        record['status'] = 'FAILED' if failed else 'PASSED'
        self._save('PREFLIGHT_BLOCKED' if failed else 'PREFLIGHT_PASSED')
        if failed:
            self._log_failed_checks(failed)
            raise DevelopmentBlocked('ENVIRONMENT_DIAGNOSIS' if self.recover_timeouts else 'BASELINE_MISMATCH',
                                     f'Required checks fail at the clean checkpoint; inspect {directory}')

    def _log_failed_checks(self, results: list[dict]) -> None:
        for check in results:
            if check['exit_code'] or check['empty_suite']:
                path = confined(self.directory, check['log_ref'], protect=False)
                print(f"[task-dev] Failed {check['check_id']}: exit={check['exit_code']}, empty_suite={check['empty_suite']}", flush=True)
                log_retry_reason('task-dev', diagnostic_excerpt(path.read_text(), 3000), path)

    def _repair_context(self, record: dict) -> dict | None:
        failures = record.get('failed_attempts', [])
        if not failures:
            return None
        latest = failures[-1]
        context = copy.deepcopy(latest)
        if self._scope_reconciled(record):
            context['scope_amendment'] = copy.deepcopy(record['scope_reconciliation'])
        context['history'] = [{k: item[k] for k in ('attempt', 'category', 'checks', 'diff_ref', 'diff_sha256')}
                              for item in failures]
        remaining = 24000
        for check in context['checks']:
            if not check['exit_code'] and not check['empty_suite']:
                continue
            path = confined(self.directory, check['log_ref'], protect=False)
            if not path.is_file() or file_hash(path) != check['sha256']:
                raise TaskError('Repair verification evidence changed or disappeared')
            text = path.read_text()
            limit = min(6000, remaining)
            check['log_excerpt'] = diagnostic_excerpt(text, limit)
            check['log_truncated'] = len(text) > limit
            remaining -= len(check['log_excerpt'])
        context['instruction'] = ('Repair the restored previous candidate in place. Preserve working behavior and tests. '
                                  'The log excerpts are bounded; full logs remain at their recorded evidence references. '
                                  'Do not weaken checks or expand scope to pass verification.')
        return context

    def _scope_reconciled(self, record):
        authority = record.get('scope_reconciliation')
        if not authority or self.scope_reconciliations.get(authority.get('task_id')) != authority:
            return False
        failures = record.get('failed_attempts', [])
        return bool(failures and failures[-1]['category'] == 'SCOPE_REQUIRED'
                    and digest(failures[-1]) == authority['failure_sha256']
                    and digest(self.plan) == authority['plan_sha256'])

    def _restore_candidate(self, task: dict, record: dict, *, protocol_recovery=False) -> None:
        if not record.get('failed_attempts'):
            return
        candidate = record['failed_attempts'][-1]
        if (candidate['category'] != 'CHECK_FAILURE' and not self._scope_reconciled(record)
                and not (protocol_recovery and candidate['category'] == 'DEVELOPER_PROTOCOL')):
            raise DevelopmentBlocked(candidate['category'], 'Previous attempt needs explicit reconciliation')
        self._assert_clean()
        path = confined(self.directory, candidate['diff_ref'], protect=False)
        if not path.is_file() or file_hash(path) != candidate['diff_sha256']:
            raise TaskError('Repair patch changed or disappeared')
        tree = candidate['tree']
        if not re.fullmatch(r'[a-f0-9]{40,64}', tree) or candidate['base_commit'] != self.state['expected_head']:
            raise TaskError('Repair candidate has an unexpected base or tree')
        changed = sorted(p for p in git(self.wt, 'diff', '--no-renames', '--name-only', '-z', candidate['base_commit'], tree).split('\x00') if p)
        if changed != sorted(candidate['changed_files']) or not set(changed) <= set(task['target_files']):
            raise TaskError('Repair candidate exceeds its approved manifest')
        if git(self.wt, 'diff', '--binary', candidate['base_commit'], tree) != path.read_text():
            raise TaskError('Repair tree differs from archived patch')
        for name in changed:
            confined(self.wt, name)
        if changed:
            git(self.wt, 'restore', '--source', tree, '--staged', '--worktree', '--', *changed)
        for name in changed:
            confined(self.wt, name)
        if sorted(self._changed()) != changed:
            raise TaskError('Restored repair candidate differs from archived paths')

    def _recover_protocol_candidate(self, task: dict, record: dict) -> None:
        """Explicitly verify an archived ready candidate; do not rerun its developer."""
        candidate = record.get('failed_attempts', [{}])[-1]
        if record['status'] == 'COMPLETED' and record.get('protocol_recoveries'):
            return
        if record['status'] != 'FAILED' or candidate.get('category') != 'DEVELOPER_PROTOCOL' or candidate.get('checks'):
            raise TaskError('Protocol recovery requires an unverified archived protocol failure')
        original = confined(self.directory, f"{task['task_id']}/attempt-{candidate['attempt']}/developer.log", protect=False)
        output = original.read_text()
        if (record.get('failure') != json.dumps(candidate['checks']) + '\n' + output
                or record.get('producer', {}).get('exit_code') != 0 or developer_blocker(output) is not None
                or not re.search(r'TASK_DEV_STATUS:', output)):
            raise TaskError('Archived developer response does not request normal verification')
        recoveries = record.setdefault('protocol_recoveries', [])
        recovery = {'attempt': candidate['attempt'], 'status': 'RESERVED',
                    'developer_log_ref': str(original.relative_to(self.directory)), 'developer_log_sha256': file_hash(original),
                    'candidate_tree': candidate['tree']}
        directory = self.directory / task['task_id'] / f'protocol-recovery-{len(recoveries) + 1}'
        recoveries.append(recovery)
        self._save('PROTOCOL_RECOVERY_RESERVED')
        directory.mkdir(parents=True, exist_ok=False)
        self._restore_candidate(task, record, protocol_recovery=True)
        protected, changed = self._protected(), self._changed()
        signature = self._source_signature(changed)
        record.update(status='VERIFYING', checks=[])
        self._save('PROTOCOL_RECOVERY_VERIFYING')
        print(f"[task-dev] Verifying archived {task['task_id']} attempt {candidate['attempt']}; no developer invocation.", flush=True)
        results = self._verify(task, record, directory)
        self._assert_tree()
        if (self._protected() != protected or self._changed() != changed or self._source_signature(changed) != signature
                or self._ignored() != self.state['ignored_paths'] or git(self.wt, 'write-tree') != candidate['tree']):
            raise TaskError('Verification changed the recovered candidate or metadata')
        if all(not c['exit_code'] and not c['empty_suite'] for c in results):
            recovery['status'] = 'PASSED'
            record.update(status='COMMIT_INTENT', candidate_tree=candidate['tree'], changed_files=changed,
                          commit_message=f'feat({self.plan["ticket_id"]}/{task["task_id"]}): {task["title"]}\n\nSSDLC-Attempt: {candidate["attempt"]}')
            self._save('COMMIT_INTENT')
            self._complete_intent(task, record)
            return
        recovery['status'] = 'FAILED'
        record['failed_attempts'].append({**copy.deepcopy(candidate), 'category': 'CHECK_FAILURE', 'checks': results,
                                         'origin': 'explicit_protocol_verification'})
        record.update(status='FAILED', failure=json.dumps(results) + '\n' + output)
        self._save('PROTOCOL_RECOVERY_FAILED')
        self._rollback_attempt(record, changed)
        self._log_failed_checks(results)

    def _rollback_attempt(self, record: dict, changed: list[str]) -> None:
        for path in changed:
            tracked = subprocess.run(['git', '--literal-pathspecs', 'cat-file', '-e', f'{record["start_commit"]}:{path}'], cwd=self.wt, capture_output=True).returncode == 0
            if tracked:
                git(self.wt, 'restore', '--source', record['start_commit'], '--staged', '--worktree', '--', path)
            else:
                git(self.wt, 'reset', '--', path)
                confined(self.wt, path)
                target = self.wt / path
                if target.is_file() or target.is_symlink():
                    target.unlink()
        if self._changed():
            raise TaskError('Attempt rollback left unexpected changes')
        self._save('ATTEMPT_RESTORED')

    def _complete_intent(self, task: dict, record: dict) -> None:
        parent, tree = record['start_commit'], record['candidate_tree']
        if self._protected() != record['protected_snapshot']:
            raise TaskError('Protected worktree paths changed since task execution')
        head = git(self.wt, 'rev-parse', 'HEAD')
        if not self._evidence_valid(record, task['task_id']):
            raise TaskError('Missing verification evidence for checkpoint recovery')
        if head == parent:
            # A crash before git commit: only the exact verified index may be committed.
            if (git(self.wt, 'write-tree') != tree or git(self.wt, 'diff', '--name-only')
                    or set(self._changed()) != set(record['changed_files'])):
                raise TaskError('Interrupted checkpoint has unexpected working tree changes')
            if self.isolate_candidates:
                git(self.wt, '-c', 'core.hooksPath=/dev/null', 'commit', '-m', record['commit_message'])
            else:
                git(self.wt, 'commit', '-m', record['commit_message'])
            head = git(self.wt, 'rev-parse', 'HEAD')
        if (git(self.wt, 'rev-parse', 'HEAD^') != parent or git(self.wt, 'rev-parse', 'HEAD^{tree}') != tree
                or record['commit_message'] != git(self.wt, 'log', '-1', '--format=%B') or self._changed()):
            raise TaskError('Checkpoint differs from verified content; manual reconciliation required')
        record.update(status='COMPLETED', commit=head)
        self.state['expected_head'] = head
        self._save('TASK_COMPLETED')

    def _developer_prompt(self, task, record):
        repair = self._repair_context(record)
        return ('Implement only this task, reusing project conventions. Do not commit, change Git metadata, '
                  'or modify protected SSDLC artifacts. Stay within target_files. '
                  'Finish all source and test edits before starting verification. Do not edit files while '
                  'a test command is running; the suite checks controller integrity and concurrent edits invalidate it. '
                  'If repair context is supplied, the previous failed candidate is already restored: repair it in place. '
                  'When ready for independent verification, end with TASK_DEV_STATUS: COMPLETE. '
                  'If completion requires changes outside target_files, end with TASK_DEV_STATUS: SCOPE_REQUIRED. '
                  'For a missing environment prerequisite, end with TASK_DEV_STATUS: ENVIRONMENT_BLOCKED. '
                  'Explain the blocker and do not bypass it, weaken checks or expand scope.\n'
                  + json.dumps(task, indent=2) + '\nPROJECT QUALITY CONTRACT:\n'
                  + json.dumps(self.quality, indent=2) + '\nAPPROVED CONTEXT:\n'
                  + json.dumps(self.context, indent=2) + '\nDEPENDENCY CHECKPOINTS:\n'
                  + json.dumps({dep: self.state['tasks'][dep]['commit'] for dep in task['depends_on']})
                  + '\nREPAIR CONTEXT:\n' + json.dumps(repair, indent=2)
                  + ('\nPREVIOUS FAILURE:\n' + record.get('failure', '') if repair is None else ''))

    def _capture_binding(self, task, record):
        historical = self.state.get('historical_capture_bindings', {}).get(task['task_id'], {}).get(str(record['attempts']))
        if historical is not None:
            if self.historical_capture_authority is None or digest(self.state['historical_capture_bindings']) != self.historical_capture_authority:
                raise DevelopmentBlocked('INTEGRITY', 'Unbound historical capture provenance')
            if record['start_commit'] != historical['base'] or record['invocation_id'] != historical['invocation_id']:
                raise DevelopmentBlocked('INTEGRITY', 'Historical capture reservation identity changed')
            return copy.deepcopy(historical)
        return {'task_id': task['task_id'], 'attempt': record['attempts'],
                'base': record['start_commit'], 'inputs_sha256': self.state['inputs_sha256'],
                'protected_sha256': digest(record['protected_snapshot']), 'runtime_version': 1,
                'invocation_id': record['invocation_id'], 'plan_sha256': digest(self.plan),
                'quality_contract_sha256': digest(self.quality),
                'controller_sha256': self.runtime_identity.get('controller_sha256', digest(self.runtime_identity)),
                'runtime_identity': self.runtime_identity}

    def _attempt_directory(self, task, record):
        directory = self.directory / task['task_id'] / f"attempt-{record['attempts']}"
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {'schema_version': '1.0', 'task_id': task['task_id'], 'plan_revision': self.plan['plan_revision'],
                    'manifest_revision': 1, 'target_files': task['target_files'], 'reason': 'Approved plan'}
        manifest['sha256'] = digest(manifest)
        validate_document('task-manifest', manifest)
        path = directory / 'manifest.json'
        if path.exists() and self.isolate_candidates:
            if path.is_symlink() or json.loads(path.read_text()) != manifest:
                raise TaskError('Reserved task manifest changed')
        else:
            atomic_json(path, manifest)
        return directory

    def _resume_reserved(self, task, record):
        """A reserved invocation has never crossed TASK_STARTED/provider dispatch."""
        self._assert_tree()
        self.guard()
        self.dispatch_budget()
        if self._ignored() != self.state['ignored_paths']:
            raise TaskError('Ignored source changed before reserved dispatch')
        if self._changed():
            previous = record.get('failed_attempts', [])
            if (not previous or git(self.wt, 'write-tree') != previous[-1]['tree']
                    or git(self.wt, 'diff', '--name-only')
                    or sorted(self._changed()) != sorted(previous[-1]['changed_files'])):
                raise TaskError('Reserved dispatch source needs explicit reconciliation')
        else:
            self._restore_candidate(task, record)
        attempt_dir = self._attempt_directory(task, record)
        if (attempt_dir / 'capture').exists():
            return self._resume_capture(task, record)
        workspace = attempt_dir / 'developer-workspace'
        if os.path.lexists(workspace):
            if workspace.is_symlink() or not workspace.is_dir():
                raise TaskError('Reserved workspace is unsafe')
            # Preserve interrupted host preparation, never silently delete it.
            os.rename(workspace, attempt_dir / ('interrupted-preparation-' + uuid.uuid4().hex))
        result = self._capture_developer(task, record, self._developer_prompt(task, record), attempt_dir)
        self._finish_attempt(task, record, result, attempt_dir)

    def _capture_developer(self, task, record, prompt, attempt_dir):
        from ssdlc_candidates import CandidateStore, CandidateError, checkout, git_identity, inventory, overlay
        from ssdlc_recovery import NotDispatched
        workspace = attempt_dir / 'developer-workspace'
        try:
            baseline = checkout(self.wt, workspace, record['start_commit'])
            overlay(self.wt, workspace, self._changed())
            initial = inventory(workspace)
            metadata = git_identity(workspace)
            record['status'] = 'RUNNING'
            self._save('TASK_STARTED')
            try:
                result = self.developer(task, record['attempts'], prompt, workspace)
                result = dict(result, termination='returned')
            except NotDispatched:
                record['status'] = 'DISPATCH_RESERVED'
                self._save('DISPATCH_PROVEN_NOT_STARTED')
                raise
            except subprocess.TimeoutExpired as exc:
                from ssdlc_recovery import QuiescentTimeout
                if not self.recover_timeouts or not isinstance(exc, QuiescentTimeout):
                    raise DevelopmentBlocked('OUTCOME_UNCERTAIN', 'Timeout writers are not confirmed fenced; no redispatch') from None
                decode = lambda text: text.decode(errors='replace') if isinstance(text, bytes) else text or ''
                result = {'output': decode(exc.output) + '\n' + decode(exc.stderr), 'exit_code': None,
                          'termination': 'timeout', 'quiescent': True, 'fence': exc.fence}
            if result.get('quiescent') is not True:
                raise DevelopmentBlocked('DEVELOPER_INVOCATION', 'Developer writers are not confirmed quiescent; no redispatch')
            store = CandidateStore(attempt_dir)
            try:
                store.capture(workspace, baseline, initial, metadata, self._capture_binding(task, record),
                              result, set(task['target_files']), PROTECTED | self.grader_paths)
            except (CandidateError, OSError):
                from ssdlc_candidates import _write
                output = attempt_dir / 'rejected-output.log'
                _write(output, result['output'].encode())
                rejection = {'schema_version': 1, 'category': 'INTEGRITY',
                    'binding': self._capture_binding(task, record), 'producer': {k:v for k,v in result.items() if k != 'output'},
                    'output_ref': str(output.relative_to(self.directory)), 'output_sha256': file_hash(output)}
                path = attempt_dir / 'capture-rejection.json'
                atomic_json(path, rejection)
                record.update(status='CANDIDATE_CAPTURED', capture_rejection={
                    'ref': str(path.relative_to(self.directory)), 'sha256': file_hash(path)})
                self._save('CAPTURE_INTEGRITY_REJECTED')
                raise DevelopmentBlocked('INTEGRITY', 'Capture failed; output and rejection preserved') from None
            return self._load_capture(task, record, store)
        except CandidateError as exc:
            raise DevelopmentBlocked('INTEGRITY', str(exc)) from exc

    def _load_capture(self, task, record, store):
        from ssdlc_candidates import signature
        receipt, result = store.load(self._capture_binding(task, record), record.get('capture_sha256'))
        record.update(status='CANDIDATE_CAPTURED', capture_sha256=signature(receipt),
                      producer=receipt['producer'])
        self._save('CANDIDATE_CAPTURED')
        # The immutable receipt precedes every secondary guard and source import.
        self.guard()
        self._assert_tree()
        if self._protected() != record['protected_snapshot'] or self._ignored() != self.state['ignored_paths']:
            raise TaskError('Authoritative source metadata changed before candidate adoption')
        if receipt['issues'] == ['scope_required']:
            raise DevelopmentBlocked('SCOPE_REQUIRED', 'scope_required: captured candidate exceeds the approved manifest')
        store.adopt(self.wt, receipt, self._changed())
        return result

    def _resume_capture(self, task, record):
        from ssdlc_candidates import CandidateStore, CandidateError
        attempt_dir = self.directory / task['task_id'] / f"attempt-{record['attempts']}"
        orphan = attempt_dir / 'capture-rejection.json'
        if not record.get('capture_rejection') and orphan.exists():
            try:
                path = confined(self.directory, str(orphan.relative_to(self.directory)), protect=False)
                rejection = json.loads(path.read_text())
                output = confined(self.directory, rejection['output_ref'], protect=False)
                if (rejection['schema_version'] != 1 or rejection['category'] != 'INTEGRITY'
                        or rejection['binding'] != self._capture_binding(task, record)
                        or rejection['producer'].get('quiescent') is not True
                        or file_hash(output) != rejection['output_sha256']):
                    raise ValueError()
            except (OSError, ValueError, KeyError, TypeError, TaskError):
                raise DevelopmentBlocked('INTEGRITY', 'Malformed orphan capture refusal; no redispatch') from None
            record.update(status='CANDIDATE_CAPTURED', capture_rejection={
                'ref': str(path.relative_to(self.directory)), 'sha256': file_hash(path)})
            self._save('CAPTURE_REJECTION_RECOVERED')
        if record.get('capture_rejection'):
            meta = record['capture_rejection']
            path = confined(self.directory, meta['ref'], protect=False)
            rejection = json.loads(path.read_text())
            output = confined(self.directory, rejection['output_ref'], protect=False)
            if (file_hash(path) != meta['sha256'] or rejection['binding'] != self._capture_binding(task, record)
                    or file_hash(output) != rejection['output_sha256']):
                raise DevelopmentBlocked('INTEGRITY', 'Capture rejection evidence changed')
            raise DevelopmentBlocked('INTEGRITY', 'Prior capture was rejected; no redispatch')
        try:
            result = self._load_capture(task, record, CandidateStore(attempt_dir))
        except CandidateError as exc:
            category = ('DEVELOPER_INVOCATION' if record['status'] == 'RUNNING'
                        and not record.get('capture_sha256') and not (attempt_dir / 'capture').exists()
                        else 'INTEGRITY')
            raise DevelopmentBlocked(category, str(exc)) from exc
        print(f"[task-dev] Resuming captured {task['task_id']} attempt {record['attempts']}; no developer dispatch.", flush=True)
        self._finish_attempt(task, record, result, attempt_dir)

    def _reconcile_failed_capture(self, task, record):
        """Finish a recorded rollback only from known, preserved source states."""
        from ssdlc_candidates import CandidateStore, CandidateError
        self._assert_tree()
        attempt_dir = self.directory / task['task_id'] / f"attempt-{record['attempts']}"
        try:
            store = CandidateStore(attempt_dir)
            receipt, _ = store.load(self._capture_binding(task, record), record['capture_sha256'])
            store.adopt(self.wt, receipt, self._changed())
        except CandidateError as exc:
            raise DevelopmentBlocked('INTEGRITY', str(exc)) from exc
        self._rollback_attempt(record, record['changed_files'])

    def _isolated_check(self, check, cwd):
        from ssdlc_candidates import CandidateError, checkout, git_identity, git_metadata, inventory, overlay
        parent = self.directory / '.verification'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='check-', dir=parent) as name:
            workspace = Path(name) / 'source'
            try:
                checkout(self.wt, workspace, self.state['expected_head'])
                overlay(self.wt, workspace, self._changed())
                before = inventory(workspace)
                metadata = git_identity(workspace)
                result = self.verifier(check['argv'], workspace / cwd.relative_to(self.wt),
                                       check['timeout_sec'], workspace)
                after = inventory(workspace)
                if (any(after.get(path) != entry for path, entry in before.items())
                        or git_metadata(workspace) != metadata['metadata']
                        or git_identity(workspace) != metadata):
                    raise _VerifierMutation('Verification changed source in disposable workspace')
                return result
            except CandidateError as exc:
                raise DevelopmentBlocked('INTEGRITY', str(exc)) from exc

    def _finish_attempt(self, task, record, result, attempt_dir):
        attempt = record['attempts']
        log = attempt_dir / 'developer.log'
        data = result['output'].encode()
        if not log.exists() or log.read_bytes() != data:
            fd, temporary = tempfile.mkstemp(prefix='.developer-log-', dir=attempt_dir)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, log)
            directory_fd = os.open(attempt_dir, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
        record['producer'] = {k: v for k, v in result.items() if k != 'output'}
        self.guard()
        self._assert_tree()
        if self._protected() != record['protected_snapshot']:
            raise TaskError('Developer modified protected paths; preserving attempt')
        if self._ignored() != self.state['ignored_paths']:
            raise TaskError('Developer added or removed ignored files outside the verified source manifest')
        changed = self._changed()
        if not set(changed) <= set(task['target_files']):
            raise TaskError('Task exceeded approved manifest; scope reconciliation is required')
        for path in changed:
            confined(self.wt, path)
        timed_out = result.get('termination') == 'timeout'
        imported = result.get('termination') == 'imported'
        if imported and record.get('import_proposal_sha256') != result.get('origin_proposal_sha256'):
            raise DevelopmentBlocked('INTEGRITY', 'Imported candidate has no bound transition authority')
        blocker = None if imported or timed_out else developer_blocker(result['output'])
        if imported:
            record['completion_confirmation'] = 'unconfirmed_import'
        if timed_out:
            record['completion_confirmation'] = 'unconfirmed_completion'
        elif not imported:
            record.pop('completion_confirmation', None)
        if not imported and not timed_out and result['exit_code'] != 0:
            blocker = blocker or 'DEVELOPER_INVOCATION'
        if not changed and not blocker:
            raise TaskError('No diff: independent evidence of an already-satisfied task is required')
        record.update(status='VERIFYING', changed_files=changed)
        self._save('VERIFYING')
        source_signature = self._source_signature(changed)
        verification_dir = attempt_dir
        if self.isolate_candidates and not blocker:
            runs = record.setdefault('capture_verifications', [])
            current_runs = [r for r in runs if r['attempt'] == attempt]
            if len(current_runs) >= 3:
                raise DevelopmentBlocked('ENVIRONMENT_BLOCKED', 'Captured candidate verification allowance exhausted')
            verification_dir = attempt_dir / f'verification-{len(current_runs) + 1}'
            runs.append({'attempt': attempt, 'dir': str(verification_dir.relative_to(self.directory))})
            self._save('CAPTURE_VERIFICATION_STARTED')
            verification_dir.mkdir(exist_ok=False)
        results = self._verify(task, record, verification_dir) if not blocker else []
        self._assert_tree()
        if (self._protected() != record['protected_snapshot'] or self._changed() != changed
                or self._source_signature(changed) != source_signature
                or self._ignored() != self.state['ignored_paths']):
            raise TaskError('Verification changed source or metadata')
        # Capture all changes, including new files, without staging unknown paths.
        if self.recover_timeouts:
            from ssdlc_recovery import require_unlocked_git
            require_unlocked_git(self.wt)
        if changed:
            indexed = set(git(self.wt, 'ls-files', '-z').split('\x00'))
            stage = [name for name in changed if os.path.lexists(self.wt / name) or name in indexed]
            if stage:
                git(self.wt, 'add', '--', *stage)
        tree = git(self.wt, 'write-tree')
        diff = git(self.wt, 'diff', '--cached', '--binary')
        (attempt_dir / 'implementation.diff').write_text(diff)
        if not blocker and all(r['exit_code'] == 0 and not r['empty_suite'] for r in results):
            record.update(status='COMMIT_INTENT', candidate_tree=tree,
                          commit_message=f'feat({self.plan["ticket_id"]}/{task["task_id"]}): {task["title"]}\n\nSSDLC-Attempt: {attempt}')
            self._save('COMMIT_INTENT')
            self._complete_intent(task, record)
            return
        # Preserve failed attempt before restoring only its declared paths.
        record['failure'] = json.dumps(results) + '\n' + result['output']
        environment_failure = self.recover_timeouts and any(r['exit_code'] in (124, 125, 126, 127) or r['empty_suite'] for r in results)
        category = blocker or ('ENVIRONMENT_DIAGNOSIS' if environment_failure else 'CHECK_FAILURE')
        candidate = {'attempt': attempt, 'category': category, 'base_commit': record['start_commit'],
                     'tree': tree, 'changed_files': changed,
                     'diff_ref': str((attempt_dir / 'implementation.diff').relative_to(self.directory)),
                     'diff_sha256': file_hash(attempt_dir / 'implementation.diff'), 'checks': copy.deepcopy(results),
                     'developer_output': result['output'][-8000:]}
        if self.isolate_candidates:
            candidate['capture_sha256'] = record['capture_sha256']
            candidate['invocation_id'] = record['invocation_id']
        prior = record.setdefault('failed_attempts', [])
        outcomes = lambda checks: [(c['check_id'], c['exit_code'], c['empty_suite']) for c in checks]
        if (category == 'CHECK_FAILURE' and prior and prior[-1]['category'] == 'CHECK_FAILURE'
                and prior[-1]['attempt'] < attempt
                and prior[-1]['tree'] == tree and outcomes(prior[-1]['checks']) == outcomes(results)):
            candidate['category'] = category = 'REPAIR_STALLED'
        prior.append(candidate)
        record['status'] = 'FAILED'
        self._save('ATTEMPT_FAILED')
        self._rollback_attempt(record, changed)
        self._log_failed_checks(results)
        if category != 'CHECK_FAILURE':
            path = attempt_dir / 'developer.log'
            log_retry_reason('task-dev ' + category, diagnostic_excerpt(result['output'], 3000), path)
            raise DevelopmentBlocked(category, f'Task {task["task_id"]}; evidence: {attempt_dir}')

    def run(self, ordered: list[dict], feedback: str = '', *, recover_protocol_task=None,
            verify_captured_only=False) -> dict:
        with ticket_lock(self.directory / 'scheduler.lock'):
            return self._run(ordered, feedback, recover_protocol_task=recover_protocol_task,
                             verify_captured_only=verify_captured_only)

    def _run(self, ordered: list[dict], feedback: str, *, recover_protocol_task=None,
             verify_captured_only=False) -> dict:
        if any(t['kind'] != 'task' or t['verification']['mode'] != 'automated' for t in ordered):
            raise TaskError('Bootstrap supports automated tasks only; subgoals/review_only require later increments')
        self.guard()
        inputs = digest({'plan': self.plan, 'quality': self.quality, 'context': self.context})
        peeked = self.journal.peek()
        if peeked is not None:
            invalid = [tid for tid, rec in peeked['tasks'].items() if rec.get('status') == 'INVALIDATED']
            if invalid:
                raise TaskError('Invalidated work cannot be run until a future revalidation API exists: '
                                + ', '.join(t[:80] for t in invalid))
        state = self.journal.load()
        if state is None:
            if self._changed() or git(self.wt, 'rev-parse', 'HEAD') != self.plan['base_commit']:
                raise TaskError('A new scheduler requires a clean worktree at base_commit; migrate existing work explicitly')
            state = {'schema_version': '1.0', 'inputs_sha256': inputs, 'expected_head': self.plan['base_commit'],
                     'ignored_paths': self._ignored(),
                     'tasks': {t['task_id']: {'status': 'PENDING', 'attempts': 0} for t in ordered}}
            if self.recover_timeouts:
                state['attempt_limit'] = self.max_attempts
                state['bounded_recovery'] = 1
            if self.isolate_candidates:
                if self.wt.stat().st_dev != self.directory.stat().st_dev:
                    raise PlanError('Candidate worktree and evidence must share a filesystem for atomic adoption')
                state['candidate_isolation'] = 1
                state['candidate_runtime'] = self.runtime_identity
            self.state = state
            self._save('INITIALIZED')
        self.state = state
        if state['inputs_sha256'] != inputs:
            raise TaskError('Inputs changed: explicit plan migration/invalidation is required')
        self._assert_candidate_runtime(state)
        from ssdlc_recovery import require_unlocked_git
        require_unlocked_git(self.wt)
        if 'attempt_limit' in state and state['attempt_limit'] != self.max_attempts:
            raise DevelopmentBlocked('LIMIT_REACHED', 'Persistent task attempt limit cannot change on resume')
        for task in ordered:
            record = state['tasks'][task['task_id']]
            if 'protected_snapshot' in record and self._protected() != record['protected_snapshot']:
                raise TaskError('Protected worktree paths changed; reconciliation required')
            if record['status'] == 'COMMIT_INTENT':
                self._complete_intent(task, record)
            elif self.isolate_candidates and record['status'] == 'DISPATCH_RESERVED':
                if verify_captured_only:
                    raise DevelopmentBlocked('ENVIRONMENT_BLOCKED', 'Verification-only continuation cannot dispatch a reserved developer')
                self._resume_reserved(task, record)
            elif self.isolate_candidates and record['status'] in ('RUNNING', 'CANDIDATE_CAPTURED', 'VERIFYING'):
                self._resume_capture(task, record)
            elif self.isolate_candidates and record['status'] == 'FAILED' and self._changed():
                self._reconcile_failed_capture(task, record)
        self._assert_clean()
        if verify_captured_only:
            if feedback or recover_protocol_task is not None:
                raise TaskError('Verification-only continuation cannot change execution scope')
            for task in ordered:
                record = state['tasks'][task['task_id']]
                if record['status'] == 'COMPLETED' and not self._evidence_valid(record, task['task_id']):
                    raise TaskError('Completed continuation evidence changed')
            return copy.deepcopy(state)
        if feedback:
            # Targeted bounce belongs to Step 3. Never silently skip rejected completed tasks.
            raise TaskError('Review feedback requires explicit invalidation, unavailable in the bootstrap scheduler')
        pending = [t for t in ordered if state['tasks'][t['task_id']]['status'] != 'COMPLETED']
        if recover_protocol_task is not None and recover_protocol_task not in state['tasks']:
            raise TaskError('Unknown task for protocol recovery')
        for task in pending:
            record = state['tasks'][task['task_id']]
            if record.get('reconciliation_required'):
                raise DevelopmentBlocked(record['reconciliation_required'][-1], 'Historical attempt needs explicit reconciliation')
            recovering = task['task_id'] == recover_protocol_task
            if (record.get('failed_attempts') and record['failed_attempts'][-1]['category'] != 'CHECK_FAILURE'
                    and not recovering and not self._scope_reconciled(record)):
                raise DevelopmentBlocked(record['failed_attempts'][-1]['category'], 'Previous attempt needs explicit reconciliation')
            if record['attempts'] >= self.max_attempts and not recovering:
                raise TaskError(f'Task {task["task_id"]} exhausted its persistent attempt limit')
        if pending:
            self._preflight()
        if recover_protocol_task:
            task = next(t for t in ordered if t['task_id'] == recover_protocol_task)
            if not all(state['tasks'][dep]['status'] == 'COMPLETED' for dep in task['depends_on']):
                raise TaskError('Protocol recovery dependency has not completed')
            self._recover_protocol_candidate(task, state['tasks'][recover_protocol_task])
        for task in ordered:
            record = state['tasks'][task['task_id']]
            if record['status'] == 'COMPLETED':
                if not self._evidence_valid(record, task['task_id']):
                    raise TaskError('Completed task evidence is missing or altered')
                continue
            if any(state['tasks'][dep]['status'] != 'COMPLETED' for dep in task['depends_on']):
                raise TaskError('Dependency has not completed')
            while record['attempts'] < self.max_attempts:
                self._assert_tree()
                self.guard()
                self.dispatch_budget()
                repair = self._repair_context(record)
                self._restore_candidate(task, record)
                record['attempts'] += 1
                attempt = record['attempts']
                record.update(status='RUNNING', start_commit=state['expected_head'], checks=[],
                              protected_snapshot=self._protected())
                if self.isolate_candidates:
                    record.pop('capture_sha256', None)
                if self.isolate_candidates:
                    record.update(status='DISPATCH_RESERVED', invocation_id=uuid.uuid4().hex)
                    record.pop('producer', None)
                    self._save('DISPATCH_RESERVED')
                else:
                    self._save('TASK_STARTED')
                attempt_dir = self._attempt_directory(task, record)
                prompt = self._developer_prompt(task, record)
                print(f"[task-dev] {task['task_id']} attempt {attempt}/{self.max_attempts}: "
                      + (f"repairing archived attempt {repair['attempt']}" if repair else 'implementing from verified checkpoint'), flush=True)
                if self.isolate_candidates:
                    result = self._capture_developer(task, record, prompt, attempt_dir)
                else:
                    result = self.developer(task, attempt, prompt)
                self._finish_attempt(task, record, result, attempt_dir)
                if record['status'] == 'COMPLETED':
                    break
            if record['status'] != 'COMPLETED':
                raise TaskError(f'Task {task["task_id"]} exhausted its persistent attempt limit')
        self.guard()
        return copy.deepcopy(state)
