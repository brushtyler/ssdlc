"""Linux, single-host lifecycle ownership. Never writes pipeline state.

Only the in-memory payload carries executable inputs. Durable records contain
identities, digests and closed-vocabulary lifecycle evidence.
"""
import argparse
import contextlib
import copy
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import time
import uuid


class SupervisorIntegrityError(Exception):
    def __init__(self, code='filesystem_integrity'):
        self.code = code
        super().__init__(code)


class FencedOut(Exception):
    pass


class InjectedCrash(BaseException):
    pass


class FaultInjector:
    def hit(self, name):
        pass


class Clock:
    def time(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


TICKET = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
RUN_ID = re.compile(r'[a-f0-9]{32}\Z')
TERMINAL = {'completed', 'cancelled', 'failed', 'blocked', 'operator_wait'}
MARK = 'SSDLC_SUPERVISED_'
OWNED, GONE, NOT_OWNED, UNKNOWN = 'OWNED', 'GONE', 'NOT_OWNED', 'UNKNOWN'


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def literal(value):
    return isinstance(value, str) and bool(value) and not value.startswith('-') and '\0' not in value


def validate_args(args):
    a = dict(vars(args)) if isinstance(args, argparse.Namespace) else dict(args)
    if not isinstance(a.get('ticket'), str) or not TICKET.fullmatch(a['ticket']) or a['ticket'] in {'.', '..'}:
        raise ValueError('invalid_ticket')
    if a.get('tier') is None:
        a['tier'] = 'auto'
    if a['tier'] not in {'auto', 'S', 'M', 'L'}:
        raise ValueError('invalid_tier')
    if a.get('from_phase', '00-recon') not in {'00-recon', '03-dev'}:
        raise ValueError('unsupported_transition')
    a.setdefault('from_phase', '00-recon')
    for key, low, high, default in [('recovery_allowance', 0, 100, 3),
                                    ('handoff_timeout_sec', 1, 3600, 30),
                                    ('payload_io_timeout_sec', 1, 300, 30)]:
        v = a.get(key, default)
        if isinstance(v, bool) or not re.fullmatch(r'[0-9]+', str(v)) or not low <= int(v) <= high:
            raise ValueError('invalid_number')
        a[key] = int(v)
    for key, limit in [('goal', 65536), ('test_cmd', 262144)]:
        v = a.get(key)
        if key == 'goal' and not literal(v):
            raise ValueError('invalid_goal')
        if v is not None and (not isinstance(v, str) or '\0' in v or len(v.encode()) > limit):
            raise ValueError('inputs_too_large')
    if not literal(str(a.get('repo', '.'))):
        raise ValueError('invalid_repo')
    if a.get('issue') is not None or a.get('sync_remote') is True:
        raise ValueError('unsupported_transition')
    a['repo'] = str(Path(a.get('repo', '.')).resolve())
    return a


def build_argv(entrypoint_id, validated_args):
    """Only this allow-list may construct controller commands (never a shell)."""
    if entrypoint_id != 'orchestrator.run':
        raise ValueError('unsupported_transition')
    a = validate_args(validated_args)
    controller = str(Path(a.get('controller', Path(__file__).with_name('orchestrator.py'))).resolve())
    argv = [sys.executable, controller, 'run', '--ticket', a['ticket'], '--goal', a['goal'],
            '--tier', a['tier'], '--repo', a['repo'], '--from-phase', a['from_phase']]
    if a.get('test_cmd') is not None:
        argv += ['--test-cmd', a['test_cmd']]
    return argv + ['--no-sync-remote']


def _safe_path(root, rel='.'):
    """Validate every existing component without following links.

    The caller supplies the trusted supervisor root. Parents outside it are
    checked for links/ownership; restrictive modes apply inside the root only.
    """
    root = Path(os.path.abspath(root))
    rel = Path(rel)
    if rel.is_absolute() or '..' in rel.parts:
        raise SupervisorIntegrityError()
    target = root / rel
    for p in list(reversed(target.parents)) + [target]:
        try:
            st = p.lstat()
        except FileNotFoundError:
            continue
        import stat
        if stat.S_ISLNK(st.st_mode):
            raise SupervisorIntegrityError()
        inside = p == root or root in p.parents
        if inside:
            expected = 0o700 if stat.S_ISDIR(st.st_mode) else 0o600
            if st.st_uid != os.getuid() or st.st_mode & 0o7777 != expected:
                raise SupervisorIntegrityError()
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise SupervisorIntegrityError()
        if p != target and not stat.S_ISDIR(st.st_mode):
            raise SupervisorIntegrityError()
    if os.path.commonpath([os.path.realpath(target), str(root)]) != str(root):
        raise SupervisorIntegrityError()
    return target


def mkdirs(root, rel='.'):
    root = Path(root)
    # .ssdlc itself is an existing project directory and is not chmodded.
    if not root.parent.exists():
        root.parent.mkdir(mode=0o700)
    target = _safe_path(root, rel)
    for p in [root] + [root.joinpath(*Path(rel).parts[:i]) for i in range(1, len(Path(rel).parts) + 1)]:
        _safe_path(root, p.relative_to(root))
        try:
            os.mkdir(p, 0o700)
        except FileExistsError:
            if not p.is_dir():
                raise SupervisorIntegrityError()
        _safe_path(root, p.relative_to(root))
    return target


def open_safe(root, rel, flags, create=False):
    p = _safe_path(root, rel)
    fd = os.open(p, flags | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    st = os.fstat(fd)
    import stat
    if st.st_uid != os.getuid() or st.st_mode & 0o7777 != 0o600 or not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise SupervisorIntegrityError()
    return fd


@contextlib.contextmanager
def lock(root, rel, blocking=True, fault=None):
    import fcntl
    if fault:
        fault.hit('lock.before_acquire')
    fd = open_safe(root, rel, os.O_RDWR, True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def read_json(root, rel):
    try:
        fd = open_safe(root, rel, os.O_RDONLY)
        with os.fdopen(fd) as f:
            data = json.load(f)
    except (ValueError, UnicodeError):
        raise SupervisorIntegrityError('metadata_corrupt') from None
    if not isinstance(data, dict):
        raise SupervisorIntegrityError('metadata_corrupt')
    if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise SupervisorIntegrityError('metadata_version')
    return data


def atomic_json(root, rel, value, fault=None):
    fault = fault or FaultInjector()
    p = _safe_path(root, rel)
    tmp = str(Path(rel).parent / ('.tmp-' + uuid.uuid4().hex))
    fd = open_safe(root, tmp, os.O_WRONLY | os.O_EXCL, True)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, sort_keys=True, separators=(',', ':'))
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        fault.hit('atomic.after_tmp_before_replace')
        _safe_path(root, rel)
        os.replace(_safe_path(root, tmp), p)
        fault.hit('atomic.after_replace_before_dirsync')
        dfd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            os.unlink(_safe_path(root, tmp))
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class Ident:
    pid: int
    boot_id: str
    start_ticks: int
    pgid: int
    sid: int
    uid: int


class Proc:
    def stat(self, pid):
        p = Path('/proc') / str(pid)
        raw = (p / 'stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        return Ident(int(raw.split(' ', 1)[0]), Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                     int(fields[19]), int(fields[2]), int(fields[3]), p.stat().st_uid), fields[0], int(fields[1])

    def environ(self, pid):
        raw = (Path('/proc') / str(pid) / 'environ').read_bytes()
        return dict(x.decode(errors='surrogateescape').split('=', 1) for x in raw.split(b'\0') if b'=' in x)

    def pids(self):
        return [int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit()]

    def born_after(self, ident, at):
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        uptime = float(Path('/proc/uptime').read_text().split()[0])
        hz = os.sysconf('SC_CLK_TCK')
        return ident.boot_id == boot and time.time() - uptime + ident.start_ticks / hz >= at - .05

    def pidfd_open(self, pid):
        return os.pidfd_open(pid)

    def send(self, fd, sig):
        signal.pidfd_send_signal(fd, sig)

    def close(self, fd):
        os.close(fd)


def verify(ident, nonce, role, gen, proc=None, intent_id=None, run_id=None):
    proc = proc or Proc()
    ident = Ident(**ident) if isinstance(ident, dict) else ident
    try:
        actual, state, _ = proc.stat(ident.pid)
        if state == 'Z':
            return GONE
        if actual != ident:
            return NOT_OWNED
        env = proc.environ(ident.pid)
        expected = {'NONCE': nonce, 'ROLE': role, 'GEN': str(gen)}
        if intent_id is not None:
            expected['INTENT'] = intent_id
        if run_id is not None:
            expected['RUN_ID'] = run_id
        return OWNED if all(hmac.compare_digest(env.get(MARK + k, ''), v) for k, v in expected.items()) else NOT_OWNED
    except (FileNotFoundError, ProcessLookupError):
        return GONE
    except (OSError, ValueError, IndexError):
        return UNKNOWN


# Strict recursive shape validation keeps mistyped records from acquiring authority.
def _shape(value, template):
    if template is None:
        return value is None
    if isinstance(template, type):
        return type(value) is template
    if isinstance(template, dict):
        return isinstance(value, dict) and all(k in value and _shape(value[k], v) for k, v in template.items())
    return False


IDENT_SHAPE = dict(pid=int, boot_id=str, start_ticks=int, pgid=int, sid=int, uid=int)
RUN_SHAPE = dict(schema_version=int, revision=int, worker_generation=int, run_id=str, nonce=str,
                 project=dict(key=str, name=str), ticket=str, created_at=(float),
                 authorized=dict(entrypoint=str, from_phase=str, goal_sha256=str, goal_len=int,
                                 tier=str, test_cmd_authority=str, reauthorization=list),
                 fingerprints=dict(config_sha256=str, controller=dict(path_sha256=str, sha256=str),
                                   supervisor_py_sha256=str, inputs_sha256=str),
                 base_commit_at_launch=str, launch_revoked=bool, launches=list,
                 processes=dict(worker=object, controller=object, descendants=list), containers=list,
                 status=str, status_reason=str, handoff=dict(outcome=object, at=object),
                 deadlines=dict(handoff_timeout_sec=int, payload_io_timeout_sec=int),
                 recovery=dict(allowance_total=int, used=int, history=list),
                 counters=dict(heartbeats=int, retries=int, classifications=dict),
                 evidence_links=dict, pending_events=list)


def validate_record(d):
    # Nullable identity/handoff fields are validated separately.
    shape = copy.deepcopy(RUN_SHAPE)
    shape.pop('processes')
    shape.pop('handoff')
    if not _shape(d, shape):
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['worker_generation'] < 1 or d['revision'] < 0:
        raise SupervisorIntegrityError('metadata_corrupt')
    if not RUN_ID.fullmatch(d['run_id']) or not RUN_ID.fullmatch(d['nonce']) or not TICKET.fullmatch(d['ticket']):
        raise SupervisorIntegrityError('metadata_corrupt')
    if not _shape(d.get('processes'), dict(descendants=list)):
        raise SupervisorIntegrityError('metadata_corrupt')
    for role in ('worker', 'controller'):
        if role not in d['processes'] or (d['processes'][role] is not None and not _shape(d['processes'][role], IDENT_SHAPE)):
            raise SupervisorIntegrityError('metadata_corrupt')
    if not isinstance(d.get('handoff'), dict) or d['handoff'].get('outcome') not in {None, 'launched', 'blocked', 'operator_wait', 'failed'}:
        raise SupervisorIntegrityError('metadata_corrupt')
    if 'at' not in d['handoff'] or (d['handoff']['at'] is not None and type(d['handoff']['at']) not in (int, float)):
        raise SupervisorIntegrityError('metadata_corrupt')
    for x in d['launches']:
        if not _shape(x, dict(intent_id=str, role=str, generation=int, state=str, at=float)) or x['role'] not in {'worker', 'controller'} or x['state'] not in {'pending', 'spawned', 'published', 'abandoned'}:
            raise SupervisorIntegrityError('metadata_corrupt')
    if d.get('cancel') is not None and not _shape(d['cancel'], dict(epoch=int, requested_at=float, state=str)):
        raise SupervisorIntegrityError('metadata_corrupt')
    for x in d['pending_events']:
        try:
            validate_event(x)
        except (ValueError, TypeError):
            raise SupervisorIntegrityError('metadata_corrupt') from None
    required = {'resumes', 'completion_kind', 'exit_evidence', 'heartbeat', 'preflight',
                'classification', 'journal_baseline', 'cancel'}
    if not required <= set(d):
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['status'] not in {'starting', 'running', 'completed', 'cancelled', 'failed',
                           'blocked', 'operator_wait', 'cancelling', 'reconcile'}:
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['status_reason'] not in REASONS:
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['authorized']['entrypoint'] != 'run' or d['authorized']['from_phase'] not in {'00-recon', '03-dev'} or d['authorized']['tier'] not in {'auto', 'S', 'M', 'L'}:
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['authorized']['test_cmd_authority'] != 'operator_flag' or 'test_cmd_sha256' not in d['authorized']:
        raise SupervisorIntegrityError('metadata_corrupt')
    for value in [d['authorized']['goal_sha256'], d['fingerprints']['config_sha256'],
                  d['fingerprints']['controller']['sha256'], d['fingerprints']['controller']['path_sha256'],
                  d['fingerprints']['supervisor_py_sha256'], d['fingerprints']['inputs_sha256']]:
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
            raise SupervisorIntegrityError('metadata_corrupt')
    value = d['authorized']['test_cmd_sha256']
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)):
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['recovery']['used'] < 0 or d['recovery']['used'] > d['recovery']['allowance_total']:
        raise SupervisorIntegrityError('metadata_corrupt')
    for x in d['processes']['descendants']:
        if not _shape(x, dict(pid=int, start_ticks=int, role=str)) or x['role'] not in {'worker', 'controller'}:
            raise SupervisorIntegrityError('metadata_corrupt')
    if not _shape(d['exit_evidence'], dict(source=str)) or d['exit_evidence']['source'] not in {'none', 'parent_returncode', 'run_ended'}:
        raise SupervisorIntegrityError('metadata_corrupt')
    if set(d) - (set(RUN_SHAPE) | required | {'in_flight'}):
        raise SupervisorIntegrityError('metadata_corrupt')
    for key, allowed in [('authorized', set(RUN_SHAPE['authorized']) | {'test_cmd_sha256', 'argv_id'}),
                         ('fingerprints', set(RUN_SHAPE['fingerprints'])),
                         ('processes', {'worker', 'controller', 'descendants'}),
                         ('handoff', {'outcome', 'at'})]:
        if set(d[key]) - allowed:
            raise SupervisorIntegrityError('metadata_corrupt')
    if d['authorized'].get('argv_id') != 'orchestrator.run':
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['resumes'] is not None and (not isinstance(d['resumes'], str) or not RUN_ID.fullmatch(d['resumes'])):
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['completion_kind'] is not None and d['completion_kind'] not in {'run', 'plan', 'design'}:
        raise SupervisorIntegrityError('metadata_corrupt')
    for key in ('heartbeat', 'preflight', 'classification'):
        if d[key] is not None and not isinstance(d[key], dict):
            raise SupervisorIntegrityError('metadata_corrupt')
    if not _shape(d['journal_baseline'], dict(failed_attempts=dict, attempts=dict, taken_at_launch=bool)):
        raise SupervisorIntegrityError('metadata_corrupt')
    if not {'code', 'intent_id'} <= set(d['exit_evidence']):
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['exit_evidence']['code'] is not None and type(d['exit_evidence']['code']) is not int:
        raise SupervisorIntegrityError('metadata_corrupt')
    for x in d['launches']:
        if not RUN_ID.fullmatch(x['intent_id']) or x['generation'] < 1:
            raise SupervisorIntegrityError('metadata_corrupt')
    if d.get('cancel'):
        c = d['cancel']
        if c['state'] not in {'requested', 'signalling', 'blocked', 'done'} or c['epoch'] < 1:
            raise SupervisorIntegrityError('metadata_corrupt')
        if c.get('executor') is not None and not _shape(c['executor'], IDENT_SHAPE):
            raise SupervisorIntegrityError('metadata_corrupt')
    return d


EVENT_TYPES = {'unsupported_transition', 'launch_pending', 'identity_published', 'shim_released',
               'handoff', 'reconcile', 'launch_abandoned', 'launch_retry', 'controller_exited',
               'cancel_requested', 'cancel_blocked', 'cancel_term', 'cancel_finished', 'started'}
REASONS = {'bootstrap', 'launched', 'reconcile', 'uncertain_outcome', 'inputs_required',
           'inputs_incomplete', 'fingerprint_mismatch', 'handoff_timeout', 'exit_evidence_unavailable',
           'cancel_requested', 'cancelled', 'pidfd_unsupported', 'unowned_session_member', 'identity_unknown'}


def validate_event(record):
    allowed = {'event_id', 'ts', 'run_id', 'project', 'ticket', 'type', 'from', 'to',
               'reason_code', 'recovery', 'role', 'gen', 'intent'}
    if not isinstance(record, dict) or set(record) - allowed or record.get('type') not in EVENT_TYPES:
        raise ValueError('invalid_event')
    if 'ts' in record and type(record['ts']) not in (int, float):
        raise ValueError('invalid_event')
    if 'role' in record and record['role'] not in {'worker', 'controller'}:
        raise ValueError('invalid_event')
    if 'gen' in record and (type(record['gen']) is not int or record['gen'] < 1):
        raise ValueError('invalid_event')
    for key, pattern in [('event_id', r'[a-f0-9]{32}:[0-9]+:[a-z_:0-9]+'),
                         ('run_id', r'[a-f0-9]{32}'), ('intent', r'[a-f0-9]{32}'),
                         ('project', r'[a-f0-9]{16}'), ('ticket', r'[A-Za-z0-9][A-Za-z0-9_.-]*')]:
        if key in record and (not isinstance(record[key], str) or not re.fullmatch(pattern, record[key])):
            raise ValueError('invalid_event')
    for key in ('from', 'to'):
        if key in record and record[key] not in {'starting', 'running', 'completed', 'cancelled', 'failed', 'blocked', 'operator_wait', 'cancelling', 'reconcile'}:
            raise ValueError('invalid_event')
    if 'reason_code' in record and record['reason_code'] not in REASONS:
        raise ValueError('invalid_event')
    if 'recovery' in record and (not _shape(record['recovery'], dict(used=int, remaining=int)) or set(record['recovery']) != {'used', 'remaining'}):
        raise ValueError('invalid_event')


class RunStore:
    def __init__(self, repo, ticket, run_id=None, clock=None, fault=None):
        if not isinstance(ticket, str) or not TICKET.fullmatch(ticket) or ticket in {'.', '..'}:
            raise ValueError('invalid_ticket')
        if run_id is not None and not RUN_ID.fullmatch(run_id):
            raise ValueError('invalid_run_id')
        self.root = Path(repo).resolve() / '.ssdlc' / '.supervisor'
        self.ticket, self.run_id = ticket, run_id
        self.clock, self.fault = clock or Clock(), fault or FaultInjector()

    @property
    def rel(self):
        return str(Path(self.ticket) / 'runs' / self.run_id)

    def path(self, filename):
        return str(Path(self.rel) / filename)

    def prepare(self):
        mkdirs(self.root, self.ticket)

    def current(self):
        d = read_json(self.root, str(Path(self.ticket) / 'current.json'))
        if not isinstance(d.get('run_id'), str) or not RUN_ID.fullmatch(d['run_id']):
            raise SupervisorIntegrityError('metadata_corrupt')
        self.run_id = d['run_id']
        return self.read()

    def read(self):
        try:
            d = validate_record(read_json(self.root, self.path('run.json')))
        except (TypeError, KeyError, ValueError, IndexError):
            raise SupervisorIntegrityError('metadata_corrupt') from None
        if d['run_id'] != self.run_id or d['ticket'] != self.ticket:
            raise SupervisorIntegrityError('metadata_corrupt')
        return d

    def create(self, data):
        mkdirs(self.root, self.rel)
        if _safe_path(self.root, self.path('run.json')).exists():
            self.read()
            raise SupervisorIntegrityError('metadata_corrupt')
        validate_record(data)
        atomic_json(self.root, self.path('run.json'), data, self.fault)
        atomic_json(self.root, str(Path(self.ticket) / 'current.json'), dict(schema_version=1, run_id=self.run_id), self.fault)

    def locked(self):
        return lock(self.root, self.path('run.lock'))

    def append(self, rel, record):
        validate_event(record)
        fd = open_safe(self.root, rel, os.O_RDWR | os.O_APPEND, True)
        try:
            # Preserve a torn tail; the next record starts on its own line.
            n = os.lseek(fd, 0, os.SEEK_END)
            prefix = b'' if not n or os.pread(fd, 1, n - 1) == b'\n' else b'\n'
            b = prefix + json.dumps(record, sort_keys=True, separators=(',', ':')).encode() + b'\n'
            while b:
                b = b[os.write(fd, b):]
            os.fsync(fd)
        finally:
            os.close(fd)

    def events(self):
        try:
            fd = open_safe(self.root, self.path('events.jsonl'), os.O_RDONLY)
        except FileNotFoundError:
            return []
        records = []
        with os.fdopen(fd, 'rb') as f:
            for line in f:
                try:
                    x = json.loads(line)
                    if isinstance(x, dict) and isinstance(x.get('event_id'), str):
                        records.append(x)
                except (ValueError, UnicodeError):
                    pass
        return records

    def replay(self, d):
        known = {e['event_id'] for e in self.events()}
        for e in d['pending_events']:
            if e['event_id'] not in known:
                self.append(self.path('events.jsonl'), e)
                known.add(e['event_id'])
        if d['pending_events']:
            self.fault.hit('event.after_append_before_clear')
            d['pending_events'] = []
            atomic_json(self.root, self.path('run.json'), d, self.fault)

    def update(self, fn, fence=None, authority=False, event=None, locked=False):
        with contextlib.nullcontext() if locked else self.locked():
            d = self.read()
            if fence is not None:
                gen, epoch = fence
                if d['worker_generation'] != gen or (d.get('cancel') or {}).get('epoch', 0) != epoch:
                    raise FencedOut()
            if authority and (d['launch_revoked'] or d.get('cancel') or d['status'] in TERMINAL):
                raise FencedOut()
            self.replay(d)
            before = d['status']
            fn(d)
            d['revision'] += 1
            if event:
                # No free-form fields are accepted from callers/probes.
                if not re.fullmatch(r'[a-z_]+', event):
                    raise ValueError('invalid_event')
                d['pending_events'].append(dict(event_id=f"{d['run_id']}:{d['revision']}:0",
                    ts=float(self.clock.time()), run_id=d['run_id'], project=d['project']['key'], ticket=d['ticket'],
                    type=event, **{'from': before, 'to': d['status']}, reason_code=d['status_reason'],
                    recovery={'used': d['recovery']['used'], 'remaining': d['recovery']['allowance_total'] - d['recovery']['used']}))
            validate_record(d)
            atomic_json(self.root, self.path('run.json'), d, self.fault)
            self.fault.hit('event.after_state_before_append')
            self.replay(d)
            return d

    def handoff(self, outcome, reason, generation=None):
        with self.locked():
            d = self.read()
            if generation is not None and (d['worker_generation'] != generation or d['launch_revoked'] or d.get('cancel')):
                raise FencedOut()
            if d['handoff']['outcome'] is not None:
                return d
            def apply(v):
                v['handoff'] = dict(outcome=outcome, at=float(self.clock.time()))
                v['status'] = 'running' if outcome == 'launched' else outcome
                v['status_reason'] = reason
                if generation is None:
                    v['launch_revoked'] = True
            result = self.update(apply, fence=(generation, 0) if generation is not None else None,
                                 authority=generation is not None, event='handoff', locked=True)
            self.fault.hit('handoff.worker_wins' if generation is not None else 'handoff.timeout_wins')
            return result


def config_digest(env):
    return digest(json.dumps(sorted((k, digest(v)) for k, v in env.items()
                                   if not k.startswith(MARK) and k not in {'PWD', 'SHLVL', '_'})))


def new_record(a, store):
    controller = Path(a.get('controller', Path(__file__).with_name('orchestrator.py'))).resolve()
    authorized = dict(entrypoint='run', from_phase=a['from_phase'], goal_sha256=digest(a['goal']),
                      goal_len=len(a['goal'].encode()), tier=a['tier'],
                      test_cmd_sha256=digest(a['test_cmd']) if a.get('test_cmd') is not None else None,
                      test_cmd_authority='operator_flag', reauthorization=[], argv_id='orchestrator.run')
    return dict(schema_version=1, revision=0, worker_generation=1, run_id=store.run_id, nonce=uuid.uuid4().hex,
        resumes=None, project=dict(key=digest(a['repo'])[:16], name='project'), ticket=a['ticket'],
        created_at=float(store.clock.time()), authorized=authorized,
        fingerprints=dict(config_sha256=config_digest(os.environ),
                          controller=dict(path_sha256=digest(str(controller)), sha256=digest(controller.read_bytes())),
                          supervisor_py_sha256=digest(Path(__file__).read_bytes()), inputs_sha256=digest(json.dumps(authorized, sort_keys=True))),
        base_commit_at_launch='', launch_revoked=False, launches=[],
        processes=dict(worker=None, controller=None, descendants=[]), containers=[], status='starting', status_reason='bootstrap',
        completion_kind=None, handoff=dict(outcome=None, at=None), exit_evidence=dict(source='none', code=None, intent_id=None),
        deadlines=dict(hard_deadline=None, preflight_deadline=None, quiet_threshold_sec=300, handoff_timeout_sec=a['handoff_timeout_sec'], payload_io_timeout_sec=a['payload_io_timeout_sec']),
        recovery=dict(allowance_total=a['recovery_allowance'], used=0, next_retry_at=None, backoff_sec=0, history=[]),
        counters=dict(heartbeats=0, retries=0, classifications={}), heartbeat=None, preflight=None, classification=None,
        journal_baseline=dict(failed_attempts={}, attempts={}, taken_at_launch=False), cancel=None,
        evidence_links=dict(state_json=f".ssdlc/{a['ticket']}/state.json", task_journal=f".ssdlc/{a['ticket']}/03-dev/tasks/task-journal.jsonl"), pending_events=[])


def markers(d, role, gen, intent):
    return {MARK + k: str(v) for k, v in dict(RUN_ID=d['run_id'], NONCE=d['nonce'], ROLE=role, GEN=gen, INTENT=intent).items()}


def intent_for(d, role):
    return next((x for x in reversed(d['launches']) if x['role'] == role and x['state'] != 'abandoned'), None)


def identity_state(d, role, proc=None):
    ident, intent = d['processes'][role], intent_for(d, role)
    if ident is None:
        return GONE if intent is None else UNKNOWN
    if intent is None:
        return UNKNOWN
    return verify(ident, d['nonce'], role, intent['generation'], proc, intent['intent_id'], d['run_id'])


def view(d, proc=None):
    out = copy.deepcopy(d)
    states = [identity_state(d, role, proc) for role in ('worker', 'controller')]
    if d.get('cancel') and d['cancel']['state'] != 'done':
        out['status'], out['status_reason'] = 'cancelling', 'cancel_pending'
    elif d['status'] not in TERMINAL and (UNKNOWN in states or NOT_OWNED in states or states != [OWNED, OWNED]):
        out['status'], out['status_reason'] = 'reconcile', 'reconcile'
    return public(out)


def public(d, status=None, reason=None):
    state = status or d.get('status', 'blocked')
    return dict(run_id=d.get('run_id'), status=state, status_reason=reason or d.get('status_reason', 'reconcile'),
                handoff=d.get('handoff', dict(outcome='blocked', at=None)),
                next_action='inspect_and_reconcile' if state in {'blocked', 'reconcile', 'failed'} else
                            'repeat_cancel' if state == 'cancelling' else 'status')


def exit_code(result):
    return {'operator_wait': 10, 'blocked': 11, 'reconcile': 11, 'failed': 12,
            'already_running': 13, 'locked': 13, 'unsupported': 14}.get(result['status'], 0)


def transfer(fd, data=None, timeout=30, clock=None, limit=1048576):
    """One absolute deadline covers framing and the entire payload."""
    clock = clock or Clock()
    deadline = clock.monotonic() + timeout
    os.set_blocking(fd, False)
    buf = bytearray() if data is None else memoryview(struct.pack('!I', len(data)) + data)
    wanted = None
    if data is not None and len(data) > limit:
        raise ValueError('inputs_too_large')
    while True:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        r, w, _ = select.select([fd] if data is None else [], [] if data is None else [fd], [], min(remaining, .1))
        if not r and not w:
            continue
        try:
            if data is not None:
                n = os.write(fd, buf)
                buf = buf[n:]
                if not buf:
                    return
            else:
                chunk = os.read(fd, min(65536, (wanted + 4 - len(buf)) if wanted is not None else 4 - len(buf)))
                if not chunk:
                    raise EOFError()
                buf.extend(chunk)
                if len(buf) >= 4 and wanted is None:
                    wanted = struct.unpack('!I', buf[:4])[0]
                    if wanted > limit:
                        raise ValueError('inputs_too_large')
                if wanted is not None and len(buf) == wanted + 4:
                    return bytes(buf[4:])
        except BlockingIOError:
            continue


def spawn(store, role, gen, payload=False, proc=None):
    """Caller holds run.lock across pending, spawn and identity publication."""
    proc = proc or Proc()
    intent = uuid.uuid4().hex
    rec = dict(intent_id=intent, role=role, generation=gen, state='pending', at=float(store.clock.time()), abandoned_at=None)
    store.update(lambda d: d['launches'].append(rec), (gen, 0), True, 'launch_pending', locked=True)
    store.fault.hit('launch.after_intent_before_pipe' if role == 'worker' else 'controller.after_intent_before_spawn')
    go_r, go_w = os.pipe()
    payload_r, payload_w = os.pipe() if payload else (None, None)
    child = None
    try:
        store.fault.hit('launch.after_pipe_before_spawn')
        d = store.read()
        env = dict(PATH=os.defpath, **markers(d, role, gen, intent))
        cmd = [sys.executable, str(Path(__file__).resolve()), '_boot', '--repo', str(store.root.parent.parent),
               '--ticket', store.ticket, '--run-id', store.run_id, '--role', role, '--generation', str(gen),
               '--intent', intent, '--go-fd', str(go_r)]
        if payload:
            cmd += ['--payload-fd', str(payload_r)]
        child = subprocess.Popen(cmd, env=env, start_new_session=True, close_fds=True,
                    pass_fds=tuple(fd for fd in (go_r, payload_r) if fd is not None),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def spawned(v):
            v['launches'][-1]['state'] = 'spawned'
        store.update(spawned, (gen, 0), True, locked=True)
        store.fault.hit('launch.after_spawn_before_identity' if role == 'worker' else 'controller.after_spawn_before_identity')
        ident, state, _ = proc.stat(child.pid)
        def publish(v):
            v['launches'][-1]['state'] = 'published'
            v['processes'][role] = asdict(ident)
            if role == 'controller' and v['handoff']['outcome'] is None:
                # Publication and caller-timeout are the same serialized CAS.
                v['handoff'] = dict(outcome='launched', at=float(store.clock.time()))
                v.update(status='running', status_reason='launched')
        store.update(publish, (gen, 0), True, 'identity_published', locked=True)
        store.fault.hit('launch.after_identity_before_go')
        os.write(go_w, b'GO')
        store.fault.hit('launch.after_go_before_exec')
        return child, payload_w, intent
    except BaseException:
        if payload_w is not None:
            os.close(payload_w)
        raise
    finally:
        os.close(go_r)
        os.close(go_w)
        if payload_r is not None:
            os.close(payload_r)


def reconcile(store, proc=None):
    """Mutating recovery requires ownership.lock then run.lock.

    Ambiguity never authorizes a launch. The returned allowance decision must
    still be followed by operator input resupply; no payload is reconstructed.
    """
    proc = proc or Proc()
    with store.locked():
        d = store.read()
        abandoned = []
        events = store.events()
        for launch in d['launches']:
            if launch['state'] == 'abandoned':
                continue
            role = launch['role']
            if d['processes'][role] is not None:
                state = identity_state(d, role, proc)
                if state == OWNED:
                    continue
                if state != GONE:
                    return store.update(lambda v: v.update(status='blocked', status_reason='reconcile'), event='reconcile', locked=True)
                released = any(e.get('type') == 'shim_released' and e.get('intent') == launch['intent_id'] for e in events)
                if released:
                    return store.update(lambda v: v.update(status='blocked', status_reason='uncertain_outcome'), event='reconcile', locked=True)
            else:
                candidates = []
                for pid in proc.pids():
                    try:
                        ident, state, _ = proc.stat(pid)
                        if ident.uid != os.getuid() or state == 'Z':
                            continue
                        env = proc.environ(pid)
                        if all(env.get(k) == v for k, v in markers(d, role, launch['generation'], launch['intent_id']).items()):
                            candidates.append(ident)
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    except OSError:
                        return store.update(lambda v: v.update(status='blocked', status_reason='reconcile'), event='reconcile', locked=True)
                if len(candidates) > 1:
                    return store.update(lambda v: v.update(status='blocked', status_reason='reconcile'), event='reconcile', locked=True)
                if len(candidates) == 1:
                    if not proc.born_after(candidates[0], launch['at']):
                        return store.update(lambda v: v.update(status='blocked', status_reason='reconcile'), event='reconcile', locked=True)
                    def adopt(v):
                        next(x for x in v['launches'] if x['intent_id'] == launch['intent_id'])['state'] = 'published'
                        v['processes'][role] = asdict(candidates[0])
                    store.update(adopt, locked=True, event='identity_published')
                    continue
            abandoned.append(launch['intent_id'])
        if abandoned:
            def abandon(v):
                for x in v['launches']:
                    if x['intent_id'] in abandoned:
                        x.update(state='abandoned', abandoned_at=float(store.clock.time()))
                        v['processes'][x['role']] = None
                if v['recovery']['used'] < v['recovery']['allowance_total']:
                    v['recovery']['used'] += 1
                    v.update(status='blocked', status_reason='inputs_required')
                else:
                    v.update(status='blocked', status_reason='reconcile')
            return store.update(abandon, event='launch_abandoned', locked=True)
        return store.read()


def start(args, clock=None, fault=None):
    a = validate_args(args)  # validation must precede filesystem/lock side effects
    build_argv('orchestrator.run', a)
    if not sys.platform.startswith('linux'):
        return public({}, 'unsupported', 'unsupported_platform')
    store = RunStore(a['repo'], a['ticket'], clock=clock, fault=fault)
    payload = json.dumps(dict(args=a, env={k: v for k, v in os.environ.items() if not k.startswith(MARK)})).encode()
    if len(payload) > 1048576:
        raise ValueError('inputs_too_large')
    journal = Path(a['repo']) / '.ssdlc' / a['ticket'] / '03-dev/tasks/task-journal.jsonl'
    state = Path(a['repo']) / '.ssdlc' / a['ticket'] / 'state.json'
    if (a['from_phase'] == '00-recon' and journal.exists()) or (a['from_phase'] == '03-dev' and not state.is_file()):
        store.prepare()
        with lock(store.root, str(Path(store.ticket) / 'audit.lock')):
            store.append(str(Path(store.ticket) / 'audit.jsonl'),
                         dict(type='unsupported_transition', ts=float(store.clock.time())))
        return public({}, 'unsupported', 'unsupported_transition')
    store.fault.hit('lock.before_acquire')
    store.prepare()
    try:
        with lock(store.root, str(Path(store.ticket) / 'ownership.lock'), False):
            store.fault.hit('start.after_lock_before_intent')
            try:
                read_json(store.root, str(Path(store.ticket) / 'current.json'))
            except FileNotFoundError:
                d = None
            else:
                try:
                    d = store.current()
                except FileNotFoundError:
                    raise SupervisorIntegrityError('metadata_corrupt') from None
            if d is not None:
                result = view(d)
                if result['status'] == 'running':
                    return public(d, 'already_running', 'already_running')
                # Only proven unreleased bootstrap death can consume a retry.
                if d['status'] == 'starting' and d['handoff']['outcome'] is None:
                    d = reconcile(store)
                    if d['status_reason'] != 'inputs_required':
                        return public(store.handoff('blocked', 'reconcile'))
                    if (d['authorized']['goal_sha256'] != digest(a['goal']) or
                        d['authorized']['test_cmd_sha256'] != (digest(a['test_cmd']) if a.get('test_cmd') is not None else None) or
                        d['fingerprints']['controller']['sha256'] != digest(Path(build_argv('orchestrator.run', a)[1]).read_bytes()) or
                        d['fingerprints']['config_sha256'] != config_digest(os.environ)):
                        return public(store.handoff('blocked', 'fingerprint_mismatch'))
                    def retry(v):
                        v['worker_generation'] += 1
                        v.update(status='starting', status_reason='bootstrap')
                    d = store.update(retry, event='launch_retry')
                else:
                    return result
            else:
                store.run_id = uuid.uuid4().hex
                store.create(new_record(a, store))
                d = store.read()
            with store.locked():
                _, pipe, _ = spawn(store, 'worker', d['worker_generation'], True)
            try:
                store.fault.hit('launch.payload_partial')
                transfer(pipe, payload, a['payload_io_timeout_sec'], store.clock)
            except (TimeoutError, EOFError, BrokenPipeError, OSError):
                return public(store.handoff('blocked', 'inputs_incomplete'))
            finally:
                os.close(pipe)
            deadline = store.clock.monotonic() + a['handoff_timeout_sec']
            while store.clock.monotonic() < deadline:
                d = store.read()
                if d['handoff']['outcome'] is not None:
                    return public(d)
                store.clock.sleep(.02)
            return public(store.handoff('blocked', 'handoff_timeout'))
    except BlockingIOError:
        return public({}, 'locked', 'locked')


def status(repo, ticket, run_id=None, proc=None):
    if not sys.platform.startswith('linux'):
        return public({}, 'unsupported', 'unsupported_platform')
    store = RunStore(repo, ticket, run_id)
    try:
        return view(store.read() if run_id else store.current(), proc)
    except FileNotFoundError:
        return public({}, 'blocked', 'run_not_found')


def boot(args):
    store = RunStore(args.repo, args.ticket, args.run_id)
    try:
        ready, _, _ = select.select([args.go_fd], [], [], getattr(args, 'shim_ttl', 30))
        if not ready or os.read(args.go_fd, 2) != b'GO':
            return 0
        os.close(args.go_fd)
        with store.locked():
            d = store.read()
            launch = intent_for(d, args.role)
            if not launch or launch['intent_id'] != args.intent or launch['generation'] != args.generation:
                return 0
            def release(v):
                v['pending_events'].append(dict(event_id=f"{v['run_id']}:{v['revision'] + 1}:shim:{args.role}",
                    ts=float(store.clock.time()), type='shim_released', intent=args.intent, role=args.role,
                    gen=args.generation, run_id=v['run_id']))
            store.update(release, (args.generation, 0), True, locked=True)
        if args.role == 'controller':
            payload = json.loads(transfer(args.payload_fd, timeout=d['deadlines']['payload_io_timeout_sec']))
            os.close(args.payload_fd)
            a = validate_args(payload['args'])
            argv = build_argv('orchestrator.run', a)
            if digest(a['goal']) != d['authorized']['goal_sha256'] or config_digest(payload['env']) != d['fingerprints']['config_sha256'] or digest(Path(argv[1]).read_bytes()) != d['fingerprints']['controller']['sha256']:
                raise FencedOut()
            with store.locked():
                d = store.read()
                if d['launch_revoked'] or d.get('cancel') or d['worker_generation'] != args.generation:
                    raise FencedOut()
            env = payload['env']
            env.update(markers(d, 'controller', args.generation, args.intent))
            os.execve(argv[0], argv, env)
        store.fault.hit('launch.payload_reader_stalled')
        return worker(store, args)
    except FencedOut:
        return 0
    except BaseException:
        try:
            store.handoff('blocked', 'inputs_incomplete', args.generation)
        except BaseException:
            pass
        return 11


def worker(store, args):
    d = store.read()
    try:
        payload = json.loads(transfer(args.payload_fd, timeout=d['deadlines']['payload_io_timeout_sec']))
    finally:
        os.close(args.payload_fd)
    a = validate_args(payload['args'])
    argv = build_argv('orchestrator.run', a)
    if digest(a['goal']) != d['authorized']['goal_sha256'] or config_digest(payload['env']) != d['fingerprints']['config_sha256'] or digest(Path(argv[1]).read_bytes()) != d['fingerprints']['controller']['sha256']:
        store.handoff('blocked', 'fingerprint_mismatch', args.generation)
        return 11
    # The worker receives only markers at exec; frozen inputs stay in memory.
    store.fault.hit('cancel.revokes_pending_launch')
    with store.locked():
        controller, pipe, intent = spawn(store, 'controller', args.generation, True)
    try:
        transfer(pipe, json.dumps(payload).encode(), d['deadlines']['payload_io_timeout_sec'])
    except (TimeoutError, OSError, EOFError):
        store.handoff('blocked', 'inputs_incomplete', args.generation)
        return 11
    finally:
        os.close(pipe)
    store.handoff('launched', 'launched', args.generation)
    code = controller.wait()
    # T1 cannot attest canonical completion. Even exit zero requires T2 evidence.
    def exited(v):
        v['exit_evidence'] = dict(source='parent_returncode', code=code, intent_id=intent)
        v.update(status='blocked', status_reason='exit_evidence_unavailable' if code == 0 else 'uncertain_outcome')
    store.update(exited, (args.generation, 0), True, 'controller_exited')
    return 0


def _admit_session(d, role, proc):
    leader = d['processes'][role]
    if leader is None:
        return [], None
    leader = Ident(**leader)
    admitted, candidates = [], []
    try:
        for pid in proc.pids():
            try:
                ident, state, ppid = proc.stat(pid)
                if state == 'Z' or ident.sid != leader.sid or ident.boot_id != leader.boot_id:
                    continue
                fd = proc.pidfd_open(pid)
                try:
                    fresh, state, ppid = proc.stat(pid)
                    if fresh != ident or state == 'Z':
                        proc.close(fd)
                        continue
                    if ident.uid != leader.uid or ident.start_ticks < leader.start_ticks:
                        proc.close(fd)
                        continue
                    candidates.append((ident, ppid, fd))
                except BaseException:
                    proc.close(fd)
                    raise
            except (FileNotFoundError, ProcessLookupError):
                continue
        pending = candidates[:]
        ids = set()
        while pending:
            progress = False
            for ident, ppid, fd in pending[:]:
                try:
                    env = proc.environ(ident.pid)
                    launch = intent_for(d, role)
                    matched = launch is not None and all(hmac.compare_digest(env.get(k, ''), v)
                        for k, v in markers(d, role, launch['generation'], launch['intent_id']).items())
                except (FileNotFoundError, ProcessLookupError):
                    pending.remove((ident, ppid, fd))
                    proc.close(fd)
                    continue
                except OSError:
                    matched = False
                known = any(x.get('pid') == ident.pid and x.get('start_ticks') == ident.start_ticks for x in d['processes']['descendants'])
                owned_leader = ident == leader and identity_state(d, role, proc) == OWNED
                if owned_leader or matched or known or ppid in ids:
                    admitted.append((ident, fd))
                    ids.add(ident.pid)
                    pending.remove((ident, ppid, fd))
                    progress = True
            if not progress:
                for _, _, fd in pending:
                    proc.close(fd)
                for _, fd in admitted:
                    proc.close(fd)
                return [], 'unowned_session_member'
        return admitted, None
    except OSError:
        for _, _, fd in candidates:
            try:
                proc.close(fd)
            except OSError:
                pass
        return [], 'identity_unknown'


def cancel(repo, ticket, run_id=None, grace=5, clock=None, proc=None, fault=None):
    if not sys.platform.startswith('linux'):
        return public({}, 'unsupported', 'unsupported_platform')
    if isinstance(grace, bool) or not isinstance(grace, (float, int)) or not 0 <= grace <= 300:
        raise ValueError('invalid_grace')
    store = RunStore(repo, ticket, run_id, clock, fault)
    proc = proc or Proc()
    d = store.read() if run_id else store.current()
    if d['status'] == 'cancelled':
        return public(d)
    # A durable executor lease bridges the independent lock sections. It is
    # reclaimed only after the prior process birth identity is proven gone.
    executor = asdict(Proc().stat(os.getpid())[0])
    token = uuid.uuid4().hex
    with store.locked():
        d = store.read()
        old = (d.get('cancel') or {}).get('executor')
        if old is not None:
            try:
                actual, state, _ = Proc().stat(old['pid'])
                if state != 'Z' and asdict(actual) == old:
                    return public(d)
            except (FileNotFoundError, ProcessLookupError):
                pass
            except OSError:
                return public(d, 'blocked', 'identity_unknown')
        def request(v):
            if not v.get('cancel'):
                v['cancel'] = dict(epoch=1, requested_at=float(store.clock.time()), state='requested', term_at=None, kill_at=None)
            v['cancel'].update(executor=executor, executor_token=token)
            v.update(status='cancelling', status_reason='cancel_requested', launch_revoked=True)
        d = store.update(request, event='cancel_requested', locked=True)
    try:
        store.fault.hit('cancel.after_intent_before_signal')
        if d['processes']['controller'] is None:
            store.fault.hit('cancel.worker_only_preflight')
        return _execute_cancel(store, d, grace, proc, token)
    finally:
        # A real crash leaves the birth-bound lease for restart verification.
        # Graceful errors (including injected exceptions) release it safely.
        def release(v):
            if v['cancel'].get('executor_token') == token:
                v['cancel'].update(executor=None, executor_token=None)
        store.update(release)


def _execute_cancel(store, d, grace, proc, token):
    def blocked(reason):
        def apply(v):
            v.update(status='blocked', status_reason=reason)
            v['cancel']['state'] = 'blocked'
        return public(store.update(apply, event='cancel_blocked'))

    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        return blocked('pidfd_unsupported')

    def signal_sessions(sig):
        # Fully admit both sessions before any signal. Leaders may have exited;
        # descendants are still bounded by their own session's recorded birth.
        sessions = []
        with lock(store.root, store.path('cancel.lock'), False):
            try:
                for role in ('controller', 'worker'):
                    members, error = _admit_session(d, role, proc)
                    if error:
                        return error
                    sessions.extend(members)
                worker_pid = (d['processes']['worker'] or {}).get('pid')
                sessions.sort(key=lambda item: item[0].pid == worker_pid)
                for ident, fd in sessions:
                    if ident.pid == os.getpid():
                        continue  # A worker executor never signals itself.
                    try:
                        proc.send(fd, sig)
                    except ProcessLookupError:
                        pass
                return None
            finally:
                for _, fd in sessions:
                    proc.close(fd)

    if d['cancel']['term_at'] is None:
        try:
            error = signal_sessions(signal.SIGTERM)
        except BlockingIOError:
            return public(d)
        if error:
            return blocked(error)
        def term(v):
            v['cancel'].update(term_at=float(store.clock.time()), state='signalling')
        d = store.update(term, event='cancel_term')
    store.fault.hit('cancel.after_term_before_kill')
    store.clock.sleep(max(0, d['cancel']['term_at'] + grace - store.clock.time()))
    try:
        error = signal_sessions(signal.SIGKILL)
    except BlockingIOError:
        return public(d)
    if error:
        return blocked(error)
    store.fault.hit('cancel.after_kill_before_record')
    def finish(v):
        v.update(status='cancelled', status_reason='cancelled', in_flight='uncertain')
        v['cancel'].update(state='done', kill_at=float(store.clock.time()))
        if v['handoff']['outcome'] is None:
            v['handoff'] = dict(outcome='blocked', at=float(store.clock.time()))
    return public(store.update(finish, event='cancel_finished'))


def cli(args, parser):
    try:
        command = args.subcommand
        if command == 'start' or command == 'run':
            result = start(args)
        elif command == 'status':
            result = status(args.repo, args.ticket, args.run_id)
        else:
            result = cancel(args.repo, args.ticket, args.run_id, args.grace)
    except ValueError:
        parser.error('invalid supervisor arguments')
    except SupervisorIntegrityError as e:
        result = public({}, 'blocked', e.code)
    except FileNotFoundError:
        result = public({}, 'blocked', 'run_not_found')
    except OSError:
        result = public({}, 'blocked', 'filesystem_unavailable')
    print(json.dumps(result, sort_keys=True) if args.json else f"{result['status']}: {result['status_reason']}")
    return exit_code(result)


def _main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['_boot'])
    for name in ('repo', 'ticket', 'run-id', 'role', 'intent'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--generation', type=int, required=True)
    p.add_argument('--go-fd', type=int, required=True)
    p.add_argument('--payload-fd', type=int)
    p.add_argument('--shim-ttl', type=float, default=30)
    return boot(p.parse_args())


if __name__ == '__main__':
    sys.exit(_main())
