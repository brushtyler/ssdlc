"""I2 host-owned limits and recovery policy. No provider or repository code here."""
import math
from pathlib import Path
import time
import uuid

from ssdlc_tasks import DevelopmentBlocked, TaskJournal, atomic_json, digest, ticket_lock, validate_document, TaskError


STOPS = {
    'LIMIT_REACHED': 'limits', 'OUTCOME_UNCERTAIN': 'uncertain_outcome',
    'GIT_LOCK_UNOWNED': 'operator_decision', 'DESIGN_STALLED': 'operator_decision',
    'ENVIRONMENT_DIAGNOSIS': 'transient_environment_provider',
}


class RecoveryStop(DevelopmentBlocked):
    def __init__(self, category, message):
        if category not in STOPS:
            raise ValueError('Unregistered recovery stop')
        super().__init__(category, message)


class LimitsStore:
    """Reservations survive loss of a response. Unknown outcomes block redispatch.

    Caller owns the ticket execution lock. A separate stable lock serializes
    policy/journal writes. The protected host reference, not a model, is authority.
    """
    def __init__(self, root, clock=time.time):
        self.root, self.clock = Path(root), clock
        self.journal = TaskJournal(self.root / 'limits')
        self.policy_path = self.root / 'limits/policy.json'

    def authorize(self, policy, binding):
        validate_document('recovery-limits', policy)
        if not all(math.isfinite(policy[k]) for k in ('metered_usd', 'cli_estimate_usd')):
            raise ValueError('Nonfinite limit')
        envelope = {'policy': policy, 'binding': binding}
        with ticket_lock(self.root / 'limits/limits.lock'):
            state = self.journal.load()
            if state is not None:
                self._load(envelope)
                return
            if self.journal.snapshot.exists():
                raise DevelopmentBlocked('INTEGRITY', 'Limit journal disappeared; authorization cannot reset consumption')
            if self.policy_path.exists():
                import json
                if json.loads(self.policy_path.read_text()) != envelope:
                    raise RecoveryStop('OUTCOME_UNCERTAIN', 'Interrupted authorization has different inputs')
            else:
                atomic_json(self.policy_path, envelope)
            now = self.clock()
            self.journal.append('LIMITS_AUTHORIZED', {'policy_sha256': digest(envelope),
                'started_at': now, 'deadline_at': now + policy['wall_seconds'],
                'invocations': {}, 'transports': [], 'diagnoses': []})

    def _load(self, expected=None, *, repair=True):
        import json
        if self.policy_path.is_symlink():
            raise DevelopmentBlocked('INTEGRITY', 'Limit policy is a symlink')
        try:
            envelope = json.loads(self.policy_path.read_text())
        except (OSError, ValueError):
            raise DevelopmentBlocked('INTEGRITY', 'Frozen policy missing or unreadable; cannot reset limits') from None
        try:
            validate_document('recovery-limits', envelope['policy'])
            state = self.journal.load() if repair else self.journal.peek()
        except TaskError:
            raise DevelopmentBlocked('INTEGRITY', 'Frozen limits policy/journal integrity failed') from None
        if (state is None or state['policy_sha256'] != digest(envelope)
                or expected is not None and envelope != expected):
            raise DevelopmentBlocked('INTEGRITY', 'Frozen limits/binding changed; explicit transition required')
        return envelope, state

    def remaining(self):
        _, state = self._load(repair=False)
        remaining = state['deadline_at'] - self.clock()
        if remaining <= 0:
            raise RecoveryStop('LIMIT_REACHED', 'Frozen ticket wall-time exhausted')
        return remaining

    def check(self, state, policy):
        if self.clock() < state['started_at']:
            raise RecoveryStop('OUTCOME_UNCERTAIN', 'Clock moved behind authorization; deadline cannot be extended')
        if self.clock() >= state['deadline_at']:
            raise RecoveryStop('LIMIT_REACHED', 'Frozen ticket wall-time exhausted')
        if any(v['status'] in ('reserved', 'uncertain') for v in state['invocations'].values()):
            raise RecoveryStop('OUTCOME_UNCERTAIN', 'Prior invocation has no confirmed outcome; no redispatch')
        for kind, cap in [('metered_usd', 'metered_usd'), ('cli_estimate_usd', 'cli_estimate_usd')]:
            known = [v.get('usage', {}).get(kind) for v in state['invocations'].values()]
            total = sum(x for x in known if x is not None)
            if total >= policy[cap]:
                raise RecoveryStop('LIMIT_REACHED', 'Frozen ' + kind + ' cap exhausted')

    def reserve(self, role, phase, engine):
        with ticket_lock(self.root / 'limits/limits.lock'):
            envelope, state = self._load()
            p = envelope['policy']
            self.check(state, p)
            invocations = list(state['invocations'].values())
            if role not in p['role_invocations'] or phase not in p['phase_invocations']:
                raise RecoveryStop('LIMIT_REACHED', 'Missing explicit role/phase invocation cap')
            if (len(invocations) >= p['invocations']
                    or sum(v['role'] == role for v in invocations) >= p['role_invocations'][role]
                    or sum(v['phase'] == phase for v in invocations) >= p['phase_invocations'][phase]):
                raise RecoveryStop('LIMIT_REACHED', 'Frozen invocation allowance exhausted')
            identity = uuid.uuid4().hex
            state['invocations'][identity] = {'role': role, 'phase': phase, 'engine': engine, 'status': 'reserved',
                'usage': {'metered_usd': None, 'cli_estimate_usd': None}}
            self.journal.append('INVOCATION_RESERVED', state)
            return identity

    def finish(self, identity, status, usage=None):
        if status not in ('returned', 'terminated', 'uncertain', 'not_dispatched'):
            raise ValueError('Invalid invocation outcome')
        usage = usage or {'metered_usd': None, 'cli_estimate_usd': None}
        if set(usage) != {'metered_usd', 'cli_estimate_usd'} or any(
                x is not None and (type(x) not in (float, int) or not math.isfinite(x) or x < 0) for x in usage.values()):
            raise ValueError('Invalid usage observation')
        with ticket_lock(self.root / 'limits/limits.lock'):
            _, state = self._load()
            old = state['invocations'][identity]
            if old['status'] not in ('reserved',):
                if old['status'] == status and old['usage'] == usage:
                    return
                raise RecoveryStop('OUTCOME_UNCERTAIN', 'Conflicting invocation reconciliation')
            old.update(status=status, usage=usage)
            self.journal.append('INVOCATION_RECONCILED', state)

    def transport(self, identity, code, *, non_dispatch_proven, jitter):
        """Reserve delay before a retry; uncertainty is never replay permission."""
        if code not in (429, 503, 504) or non_dispatch_proven is not True:
            raise RecoveryStop('OUTCOME_UNCERTAIN', 'Transport outcome does not prove safe replay')
        if type(jitter) not in (float, int) or not 0 <= jitter <= 1:
            raise ValueError('Invalid jitter')
        with ticket_lock(self.root / 'limits/limits.lock'):
            envelope, state = self._load()
            if state['invocations'][identity]['status'] != 'reserved':
                raise RecoveryStop('OUTCOME_UNCERTAIN', 'Transport has no active invocation reservation')
            count = sum(v['invocation_id'] == identity for v in state['transports'])
            policy = envelope['policy']
            if (len(state['transports']) >= policy['transport_retries'] or count >= policy['transport_retries']):
                raise RecoveryStop('LIMIT_REACHED', 'Persistent transport retry allowance exhausted')
            delay = min(30, 2 ** (count + 1)) + jitter
            if self.clock() + delay >= state['deadline_at']:
                raise RecoveryStop('LIMIT_REACHED', 'Transport retry would exceed the deadline')
            state['transports'].append({'invocation_id': identity, 'code': code, 'delay': delay,
                                        'not_before': self.clock() + delay, 'non_dispatch_proven': True})
            self.journal.append('TRANSPORT_RETRY_RESERVED', state)
            return delay

    def reserve_diagnosis(self, subject):
        with ticket_lock(self.root / 'limits/limits.lock'):
            envelope, state = self._load()
            self.check(state, envelope['policy'])
            if any(v['subject'] == subject for v in state['diagnoses']):
                raise RecoveryStop('DESIGN_STALLED', 'Diagnosis already reserved for these obligations')
            if len(state['diagnoses']) >= envelope['policy']['design_diagnoses']:
                raise RecoveryStop('DESIGN_STALLED', 'No explicitly authorized diagnosis allowance remains')
            state['diagnoses'].append({'subject': subject, 'status': 'reserved'})
            self.journal.append('DESIGN_DIAGNOSIS_RESERVED', state)


def require_unlocked_git(worktree):
    """Never remove an index lock without a host creation/ownership receipt."""
    from ssdlc_tasks import git
    lock = Path(git(Path(worktree), 'rev-parse', '--git-path', 'index.lock'))
    if not lock.is_absolute():
        lock = Path(worktree) / lock
    if lock.exists() or lock.is_symlink():
        raise RecoveryStop('GIT_LOCK_UNOWNED', 'Git index lock ownership is unproven; preserve it and fence writers')


def progress(previous, current):
    """Track obligations, not prose hashes, finding labels or token changes."""
    def obligations(assessment):
        return {ref for f in assessment.get('findings', []) if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING'
                for key in ('requirement_refs', 'standard_refs', 'quality_check_refs') for ref in f.get(key, [])}
    before, after = obligations(previous), obligations(current)
    resolved = before - after
    return {'resolved_obligations': sorted(resolved), 'open_obligations': sorted(after),
            'material_progress': bool(resolved), 'interpretation': 'Independent reviewer resolutions, not correctness proof'}


import subprocess


class QuiescentTimeout(subprocess.TimeoutExpired):
    """Only the trusted Docker adapter constructs this after removal + absence."""
    def __init__(self, timeout, output, stderr, fence):
        super().__init__(['ssdlc-role', 'terminated-container'], timeout, output=output, stderr=stderr)
        self.fence = fence


def observed_usage(engine, function, result):
    usage = {'metered_usd': None, 'cli_estimate_usd': None}
    if not isinstance(result, tuple):
        return usage
    if engine == 'claude':
        index = 2 if function == 'run_claude' else 1
        if len(result) > index and type(result[index]) in (float, int) and result[index] > 0:
            usage['cli_estimate_usd'] = result[index]
    # OpenRouter usage currently reports a default zero when absent. Until its
    # route returns typed telemetry, that zero cannot be promoted to known spend.
    if engine == 'openrouter':
        index = 1
        if len(result) > index and type(result[index]) in (float, int) and result[index] > 0:
            usage['metered_usd'] = result[index]
    return usage


def inspect_design_sources(repo, names):
    """Static source facts only: never import repository code or call a shell."""
    import ast
    from ssdlc_tasks import confined, file_hash, git
    files = []
    for name in names:
        path = confined(Path(repo), name)
        if not path.is_file() or path.suffix != '.py':
            continue
        entry = {'path': name, 'sha256': file_hash(path)}
        try:
            tree = ast.parse(path.read_text())
            entry['declarations'] = [{'name': node.name, 'line': node.lineno}
                for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        except (SyntaxError, UnicodeError):
            entry['parse_error'] = True
        files.append(entry)
    return {'schema_version': 1, 'kind': 'source_inspection', 'base_commit': git(Path(repo), 'rev-parse', 'HEAD'),
            'files': files, 'not_correctness_proof': True}
