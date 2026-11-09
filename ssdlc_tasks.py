"""Versioned contracts and sequential, journaled task execution.

The journal is authoritative; its atomic JSON snapshot is only a projection.
This bootstrap deliberately rejects subgoals and automatic plan migrations.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

SCHEMA_DIR = Path(__file__).resolve().parent / 'schemas'
PROTECTED = {'.git', '.ssdlc', '.agents', '.codex', '.aws'}


class TaskError(RuntimeError):
    """An execution or recovery condition requiring explicit handling."""


class ResourceLimitError(TaskError):
    """A container hit a resource limit; code correction is not indicated."""

    category = 'ENVIRONMENT_RESOURCE'


class PlanError(TaskError):
    """A plan or contract cannot be safely executed."""

    def __init__(self, message: str, diagnostics: list[dict] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or [{'code': 'INVALID_PLAN', 'message': message}]


def parse_plan_output(output: str) -> dict:
    """Accept JSON or one JSON/unlabelled fenced block, never choose among alternatives."""
    text = output.strip()
    fences = re.findall(r'^```([^\n`]*)\n(.*?)^```[ \t]*$', text, re.M | re.S)
    if fences:
        if len(fences) != 1 or fences[0][0].strip().lower() not in ('', 'json'):
            raise PlanError('Ambiguous fenced plan output', [{'code': 'AMBIGUOUS_OUTPUT', 'field': '$'}])
        text = fences[0][1].strip()
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise PlanError(str(exc), [{'code': 'INVALID_JSON', 'field': '$', 'message': str(exc)}]) from exc
    if not isinstance(value, dict):
        raise PlanError('Plan must be an object', [{'code': 'INVALID_SCHEMA', 'field': '$'}])
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    return set(re.findall(r'(?<![\w-])(?:AC|SEC)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*', text))


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
        self.requirements = requirement_ids(requirements)
        self.quality = quality
        self.artifacts = artifacts

    def validate(self, plan: dict) -> list[dict]:
        validate_document('quality-contract', self.quality)
        validate_document('plan', plan)
        issues = []
        for task in plan['tasks']:
            refs = set(task['ac_refs'] + task['sec_refs'])
            checked = {ref for c in task['verification']['checks'] for ref in c['ac_refs'] + c['sec_refs']}
            missing = sorted(refs - checked)
            if task['verification']['mode'] == 'automated' and missing:
                issues.append({'code': 'MISSING_VERIFICATION_REF', 'task_id': task['task_id'],
                               'field': 'verification.checks', 'missing_refs': missing})
            unknown = sorted(refs - self.requirements)
            if unknown:
                issues.append({'code': 'UNKNOWN_REQUIREMENT_REF', 'task_id': task['task_id'],
                               'field': 'ac_refs/sec_refs', 'unknown_refs': unknown})
        if issues:
            raise PlanError('Invalid task requirement/check mappings', issues)
        if plan['base_commit'] != self.quality['base_commit']:
            raise PlanError('Plan and quality contract have different base commits')
        if not any(ref.startswith('AC-') for ref in self.requirements):
            raise PlanError('No acceptance criteria found in requirements')
        self._validate_quality()
        tasks = {t['task_id']: t for t in plan['tasks']}
        if len(tasks) != len(plan['tasks']):
            raise PlanError('Duplicate task_id')
        checks = {c['check_id'] for c in self.quality['checks']}
        standards = {s['standard_id'] for s in self.quality['standards']}
        covered: set[str] = set()
        for task in tasks.values():
            deps = set(task['depends_on'])
            if not deps <= tasks.keys() or task['task_id'] in deps:
                raise PlanError(f'Unknown or self dependency: {task["task_id"]}')
            for path in task['target_files']:
                confined(self.repo, path)
                if path == '.' or (self.repo / path).is_dir():
                    raise PlanError('target_files must name files, not directories')
            refs = set(task['ac_refs'] + task['sec_refs'])
            if not refs <= self.requirements:
                raise PlanError(f'Unknown requirement references: {refs - self.requirements}')
            if any(not r.startswith('AC-') for r in task['ac_refs']) or any(not r.startswith('SEC-') for r in task['sec_refs']):
                raise PlanError('AC and SEC references are in the wrong field')
            covered |= refs
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
            checked = set()
            for check in verify['checks']:
                self._check_command(check)
                check_refs = set(check['ac_refs'] + check['sec_refs'])
                if not check_refs <= refs:
                    raise PlanError('Check references requirements outside its task')
                checked |= check_refs
            if verify['mode'] == 'automated' and not refs <= checked:
                raise PlanError('Each task requirement needs an executable verification reference')
            for ref in task['contract_refs']:
                self._artifact_ref(ref)
        if covered != self.requirements:
            raise PlanError(f'Uncovered requirements: {self.requirements - covered}')
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

    def _artifact_ref(self, ref: str) -> None:
        path, _, anchor = ref.partition('#')
        if self.artifacts is None:
            raise PlanError('Contract references need an artifact root')
        target = confined(self.artifacts / '02-design', path, protect=False)
        if not target.is_file():
            raise PlanError(f'Missing contract: {ref}')
        if anchor:
            headings = re.findall(r'^#+\s+(.+)$', target.read_text(), re.M)
            slugs = {re.sub(r'[^\w -]', '', h.lower()).replace(' ', '-') for h in headings}
            if anchor not in slugs:
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


class TaskScheduler:
    """Bootstrap executor: strict manifests, existing routing, no recursive work."""
    def __init__(self, worktree: Path, directory: Path, plan: dict, quality: dict,
                 context: dict[str, str], developer: Callable, verifier: Callable,
                 guard: Callable = lambda: None, budget: Callable = lambda: None,
                 max_attempts: int = 3):
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
        if max_attempts < 1:
            raise PlanError('max_attempts must be positive')
        self.journal = TaskJournal(directory)
        self.state: dict = {}

    def _save(self, event: str) -> None:
        self.journal.append(event, self.state)

    def _changed(self) -> list[str]:
        out = git(self.wt, '-c', 'status.renames=false', 'status', '--porcelain=v1', '-z', '--untracked-files=all')
        return [line[3:] for line in out.split('\x00') if line]

    def _protected(self) -> dict:
        result = {}
        for name in PROTECTED:
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

    def _verify(self, task: dict, record: dict, attempt_dir: Path) -> list[dict]:
        checks = task['verification']['checks'] + self.quality['checks']
        results = []
        for index, check in enumerate(checks):
            self.budget()
            cwd = confined(self.wt, check['cwd'])
            try:
                out, err, rc = self.verifier(check['argv'], cwd, check['timeout_sec'])
            except subprocess.TimeoutExpired as exc:
                out, err, rc = '', f'Verification timeout: {exc}', 124
            log = attempt_dir / f'check-{index}.log'
            log.write_text(out + '\n' + err)
            empty = bool(re.search(r'Ran 0 tests\b|\bno tests ran\b|\bcollected 0 items\b', out + err))
            results.append({'check_id': check['check_id'], 'exit_code': rc, 'empty_suite': empty,
                            'log_ref': str(log.relative_to(self.directory)), 'sha256': file_hash(log)})
            record['checks'] = results
            self._save('CHECK_FINISHED')
            self.guard()
            self.budget()
        return results

    def _evidence_valid(self, record: dict) -> bool:
        for result in record.get('checks', []):
            path = confined(self.directory, result['log_ref'], protect=False)
            if not path.is_file() or file_hash(path) != result['sha256']:
                return False
        return bool(record.get('checks'))

    def _complete_intent(self, task: dict, record: dict) -> None:
        parent, tree = record['start_commit'], record['candidate_tree']
        if self._protected() != record['protected_snapshot']:
            raise TaskError('Protected worktree paths changed since task execution')
        head = git(self.wt, 'rev-parse', 'HEAD')
        if not self._evidence_valid(record):
            raise TaskError('Missing verification evidence for checkpoint recovery')
        if head == parent:
            # A crash before git commit: only the exact verified index may be committed.
            if (git(self.wt, 'write-tree') != tree or git(self.wt, 'diff', '--name-only')
                    or set(self._changed()) != set(record['changed_files'])):
                raise TaskError('Interrupted checkpoint has unexpected working tree changes')
            git(self.wt, 'commit', '-m', record['commit_message'])
            head = git(self.wt, 'rev-parse', 'HEAD')
        if (git(self.wt, 'rev-parse', 'HEAD^') != parent or git(self.wt, 'rev-parse', 'HEAD^{tree}') != tree
                or record['commit_message'] != git(self.wt, 'log', '-1', '--format=%B') or self._changed()):
            raise TaskError('Checkpoint differs from verified content; manual reconciliation required')
        record.update(status='COMPLETED', commit=head)
        self.state['expected_head'] = head
        self._save('TASK_COMPLETED')

    def run(self, ordered: list[dict], feedback: str = '') -> dict:
        with ticket_lock(self.directory / 'scheduler.lock'):
            return self._run(ordered, feedback)

    def _run(self, ordered: list[dict], feedback: str) -> dict:
        if any(t['kind'] != 'task' or t['verification']['mode'] != 'automated' for t in ordered):
            raise TaskError('Bootstrap supports automated tasks only; subgoals/review_only require later increments')
        self.guard()
        inputs = digest({'plan': self.plan, 'quality': self.quality, 'context': self.context})
        state = self.journal.load()
        if state is None:
            if self._changed() or git(self.wt, 'rev-parse', 'HEAD') != self.plan['base_commit']:
                raise TaskError('A new scheduler requires a clean worktree at base_commit; migrate existing work explicitly')
            state = {'schema_version': '1.0', 'inputs_sha256': inputs, 'expected_head': self.plan['base_commit'],
                     'ignored_paths': self._ignored(),
                     'tasks': {t['task_id']: {'status': 'PENDING', 'attempts': 0} for t in ordered}}
            self.state = state
            self._save('INITIALIZED')
        self.state = state
        if state['inputs_sha256'] != inputs:
            raise TaskError('Inputs changed: explicit plan migration/invalidation is required')
        for task in ordered:
            record = state['tasks'][task['task_id']]
            if 'protected_snapshot' in record and self._protected() != record['protected_snapshot']:
                raise TaskError('Protected worktree paths changed; reconciliation required')
            if record['status'] == 'COMMIT_INTENT':
                self._complete_intent(task, record)
        self._assert_clean()
        if feedback:
            # Targeted bounce belongs to Step 3. Never silently skip rejected completed tasks.
            raise TaskError('Review feedback requires explicit invalidation, unavailable in the bootstrap scheduler')
        for task in ordered:
            record = state['tasks'][task['task_id']]
            if record['status'] == 'COMPLETED':
                if not self._evidence_valid(record):
                    raise TaskError('Completed task evidence is missing or altered')
                continue
            if any(state['tasks'][dep]['status'] != 'COMPLETED' for dep in task['depends_on']):
                raise TaskError('Dependency has not completed')
            while record['attempts'] < self.max_attempts:
                self._assert_tree()
                self.guard()
                self.budget()
                record['attempts'] += 1
                attempt = record['attempts']
                record.update(status='RUNNING', start_commit=state['expected_head'],
                              protected_snapshot=self._protected())
                self._save('TASK_STARTED')
                attempt_dir = self.directory / task['task_id'] / f'attempt-{attempt}'
                attempt_dir.mkdir(parents=True, exist_ok=True)
                manifest = {'schema_version': '1.0', 'task_id': task['task_id'], 'plan_revision': self.plan['plan_revision'],
                            'manifest_revision': 1, 'target_files': task['target_files'], 'reason': 'Approved plan'}
                manifest['sha256'] = digest(manifest)
                validate_document('task-manifest', manifest)
                atomic_json(attempt_dir / 'manifest.json', manifest)
                protected = self._protected()
                prompt = ('Implement only this task, reusing project conventions. Do not commit, change Git metadata, '
                          'or modify protected SSDLC artifacts. Stay within target_files; report any required scope change.\n'
                          + json.dumps(task, indent=2) + '\nPROJECT QUALITY CONTRACT:\n'
                          + json.dumps(self.quality, indent=2) + '\nAPPROVED CONTEXT:\n'
                          + json.dumps(self.context, indent=2) + '\nDEPENDENCY CHECKPOINTS:\n'
                          + json.dumps({dep: state['tasks'][dep]['commit'] for dep in task['depends_on']})
                          + '\nPREVIOUS FAILURE:\n' + record.get('failure', ''))
                result = self.developer(task, attempt, prompt)
                (attempt_dir / 'developer.log').write_text(result['output'])
                record['producer'] = {k: v for k, v in result.items() if k != 'output'}
                self.guard()
                self._assert_tree()
                if self._protected() != protected:
                    raise TaskError('Developer modified protected paths; preserving attempt')
                if self._ignored() != state['ignored_paths']:
                    raise TaskError('Developer added or removed ignored files outside the verified source manifest')
                changed = self._changed()
                if not set(changed) <= set(task['target_files']):
                    raise TaskError('Task exceeded approved manifest; scope reconciliation is required')
                for path in changed:
                    confined(self.wt, path)
                if not changed:
                    raise TaskError('No diff: independent evidence of an already-satisfied task is required')
                record.update(status='VERIFYING', changed_files=changed)
                self._save('VERIFYING')
                source_signature = self._source_signature(changed)
                results = self._verify(task, record, attempt_dir)
                self._assert_tree()
                if (self._protected() != protected or self._changed() != changed
                        or self._source_signature(changed) != source_signature
                        or self._ignored() != state['ignored_paths']):
                    raise TaskError('Verification changed source or metadata')
                # Capture all changes, including new files, without staging unknown paths.
                git(self.wt, 'add', '--', *changed)
                tree = git(self.wt, 'write-tree')
                diff = git(self.wt, 'diff', '--cached', '--binary')
                (attempt_dir / 'implementation.diff').write_text(diff)
                if result['exit_code'] == 0 and all(r['exit_code'] == 0 and not r['empty_suite'] for r in results):
                    record.update(status='COMMIT_INTENT', candidate_tree=tree,
                                  commit_message=f'feat({self.plan["ticket_id"]}/{task["task_id"]}): {task["title"]}\n\nSSDLC-Attempt: {attempt}')
                    self._save('COMMIT_INTENT')
                    self._complete_intent(task, record)
                    break
                # Preserve failed attempt before restoring only its declared paths.
                record['failure'] = json.dumps(results) + '\n' + result['output']
                record['status'] = 'FAILED'
                self._save('ATTEMPT_FAILED')
                for path in changed:
                    tracked = subprocess.run(['git', '--literal-pathspecs', 'cat-file', '-e', f'{record["start_commit"]}:{path}'], cwd=self.wt, capture_output=True).returncode == 0
                    if tracked:
                        git(self.wt, 'restore', '--source', record['start_commit'], '--staged', '--worktree', '--', path)
                    else:
                        git(self.wt, 'reset', '--', path)
                        confined(self.wt, path)
                        # Unlink the new directory entry, never its symlink target.
                        target = self.wt / path
                        if target.is_file() or target.is_symlink():
                            target.unlink()
                if self._changed():
                    raise TaskError('Attempt rollback left unexpected changes')
                self._save('ATTEMPT_RESTORED')
            if record['status'] != 'COMPLETED':
                raise TaskError(f'Task {task["task_id"]} exhausted its persistent attempt limit')
        self.guard()
        return copy.deepcopy(state)
