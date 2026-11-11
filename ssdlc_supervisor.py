"""Linux, single-host lifecycle ownership. Never writes pipeline state.

Only the in-memory payload carries executable inputs. Durable records contain
identities, digests and closed-vocabulary lifecycle evidence.
"""
import argparse
import fcntl
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
import shutil
import tempfile
import math
import signal
import struct
import subprocess
import sys
import time
import uuid


class SupervisorIntegrityError(Exception):
    category = 'INTEGRITY'

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
    a.setdefault('entrypoint', 'run')
    if a['entrypoint'] not in {'run', 'plan', 'design'}:
        raise ValueError('unsupported_transition')
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
        if key == 'goal' and a['entrypoint'] == 'run' and not literal(v):
            raise ValueError('invalid_goal')
        if v is not None and (not isinstance(v, str) or '\0' in v or len(v.encode()) > limit):
            raise ValueError('inputs_too_large')
    if not literal(str(a.get('repo', '.'))):
        raise ValueError('invalid_repo')
    if a.get('issue') is not None or a.get('sync_remote') is True:
        raise ValueError('unsupported_transition')
    for key, low, high in [('review_repairs', 0, 4), ('rounds', 1, 8)]:
        v = a.get(key)
        if v is not None and (isinstance(v, bool) or not re.fullmatch(r'[0-9]+', str(v)) or not low <= int(v) <= high):
            raise ValueError('invalid_number')
        if v is not None:
            a[key] = int(v)
    if a['entrypoint'] != 'run':
        a['goal'] = None
        a['test_cmd'] = None
    a['repo'] = str(Path(a.get('repo', '.')).resolve())
    return a


def build_argv(entrypoint_id, validated_args):
    """Only this allow-list may construct controller commands (never a shell)."""
    if entrypoint_id not in {'orchestrator.run', 'orchestrator.plan', 'orchestrator.design'}:
        raise ValueError('unsupported_transition')
    a = validate_args(dict(validated_args, entrypoint=entrypoint_id.split('.')[1]))
    controller = str(Path(a.get('controller', Path(__file__).with_name('orchestrator.py'))).resolve())
    if a['entrypoint'] != 'run':
        argv = [sys.executable, controller, a['entrypoint'], '--ticket', a['ticket'], '--repo', a['repo']]
        key = 'review_repairs' if a['entrypoint'] == 'plan' else 'rounds'
        if a.get(key) is not None:
            argv += ['--' + key.replace('_', '-'), str(a[key])]
        return argv
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
        root.parent.mkdir(mode=0o700, exist_ok=True)
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



def canonical_root(repo, ticket):
    from ssdlc_transitions import runtime_root
    try:
        return runtime_root(Path(repo) / '.ssdlc' / ticket)
    except (OSError, ValueError, KeyError, RuntimeError):
        raise SupervisorIntegrityError('filesystem_integrity') from None


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
    if isinstance(d.get('authorized'), dict) and d['authorized'].get('entrypoint') in {'plan', 'design'}:
        shape['authorized']['goal_sha256'] = None if d['authorized'].get('goal_sha256') is None else str
        shape['authorized']['goal_len'] = None if d['authorized'].get('goal_len') is None else int
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
    if d['authorized']['entrypoint'] not in {'run', 'plan', 'design'} or d['authorized']['from_phase'] not in {'00-recon', '03-dev'} or d['authorized']['tier'] not in {'auto', 'S', 'M', 'L'}:
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['authorized']['test_cmd_authority'] != 'operator_flag' or 'test_cmd_sha256' not in d['authorized']:
        raise SupervisorIntegrityError('metadata_corrupt')
    for value in ([d['authorized']['goal_sha256']] if d['authorized']['goal_sha256'] is not None else []) + [d['fingerprints']['config_sha256'],
                  d['fingerprints']['controller']['sha256'], d['fingerprints']['controller']['path_sha256'],
                  d['fingerprints']['supervisor_py_sha256'], d['fingerprints']['inputs_sha256']]:
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
            raise SupervisorIntegrityError('metadata_corrupt')
    runtime = d['authorized'].get('runtime_root')
    if runtime is not None and (not isinstance(runtime, str) or runtime not in {'.ssdlc/' + d['ticket']} and
            not re.fullmatch(r'\.ssdlc/' + re.escape(d['ticket']) + r'/execution-runtimes/[a-f0-9]{32}', runtime)):
        raise SupervisorIntegrityError('metadata_corrupt')
    value = d['authorized']['test_cmd_sha256']
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)):
        raise SupervisorIntegrityError('metadata_corrupt')
    for key, low, high in [('review_repairs', 0, 4), ('rounds', 1, 8)]:
        value = d['authorized'].get(key)
        if value is not None and (type(value) is not int or not low <= value <= high):
            raise SupervisorIntegrityError('metadata_corrupt')
    snapshot = d['fingerprints']['controller'].get('snapshot_sha256')
    if snapshot is not None and (not isinstance(snapshot, str) or not re.fullmatch(r'[a-f0-9]{64}', snapshot)):
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
    for key, allowed in [('authorized', set(RUN_SHAPE['authorized']) | {'test_cmd_sha256', 'argv_id', 'compatibility_approved', 'review_repairs', 'rounds', 'containers_possible', 'task_mode', 'runtime_root'}),
                         ('fingerprints', set(RUN_SHAPE['fingerprints'])),
                         ('processes', {'worker', 'controller', 'descendants'}),
                         ('handoff', {'outcome', 'at'})]:
        if set(d[key]) - allowed:
            raise SupervisorIntegrityError('metadata_corrupt')
    if d['authorized'].get('argv_id') not in {'orchestrator.run', 'orchestrator.plan', 'orchestrator.design'}:
        raise SupervisorIntegrityError('metadata_corrupt')
    for key in ('containers_possible', 'task_mode'):
        if key in d['authorized'] and type(d['authorized'][key]) is not bool:
            raise SupervisorIntegrityError('metadata_corrupt')
    if d['resumes'] is not None and (not isinstance(d['resumes'], str) or not RUN_ID.fullmatch(d['resumes'])):
        raise SupervisorIntegrityError('metadata_corrupt')
    if d['completion_kind'] is not None and d['completion_kind'] not in {'run', 'plan', 'design'}:
        raise SupervisorIntegrityError('metadata_corrupt')
    for key in ('heartbeat', 'preflight', 'classification'):
        if d[key] is not None and not isinstance(d[key], dict):
            raise SupervisorIntegrityError('metadata_corrupt')
    if d['classification'] is not None:
        c = d['classification']
        if (set(c) != {'class', 'reason_code', 'row', 'terminal_bound', 'diagnostics'} or
                c.get('class') not in RECOVERY_MATRIX or c.get('reason_code') not in REASONS or
                c.get('row') not in {'1', '2', '3', '3b', '3c', '4', '5', '6', '7', '8'} or
                type(c.get('terminal_bound')) is not bool or not isinstance(c.get('diagnostics'), list) or
                any(not isinstance(x, dict) or set(x) != {'code'} or x['code'] not in
                    {'U1', 'U2', 'U3', 'U4', 'U5', 'U6', 'U7', 'earlier_task_failure'} for x in c['diagnostics'])):
            raise SupervisorIntegrityError('metadata_corrupt')
    if d['preflight'] is not None:
        p = d['preflight']
        if set(p) != {'at', 'checks'} or type(p['at']) not in (float, int) or not isinstance(p['checks'], list):
            raise SupervisorIntegrityError('metadata_corrupt')
        for c in p['checks']:
            if (not isinstance(c, dict) or set(c) != {'id', 'status', 'code'} or
                    c.get('status') not in {'ok', 'failed', 'unknown', 'unsupported', 'unverified'} or
                    c.get('code') not in REASONS or c.get('id') not in
                    DEPENDENCIES | {'auth_' + x for x in ENGINES} | {'engine', 'routes', 'quality_contract', 'path', 'disk'}):
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
        prior = c.get('preserved_terminal')
        if prior is not None and (not isinstance(prior, dict) or set(prior) != {'status', 'reason'} or
                prior['status'] not in TERMINAL - {'cancelled'} or prior['reason'] not in REASONS):
            raise SupervisorIntegrityError('metadata_corrupt')
        if c.get('executor') is not None and not _shape(c['executor'], IDENT_SHAPE):
            raise SupervisorIntegrityError('metadata_corrupt')
    return d


EVENT_TYPES = {'unsupported_transition', 'launch_pending', 'identity_published', 'shim_released',
               'handoff', 'reconcile', 'launch_abandoned', 'launch_retry', 'controller_exited',
               'cancel_requested', 'cancel_blocked', 'cancel_term', 'cancel_finished', 'started',
               'preflight', 'recovery', 'heartbeat', 'classified', 'completed', 'adopted', 'reauthorization'}
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
            if event and (event != 'heartbeat' or before != d['status']):
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


def goal_digest(a):
    return digest(a['goal']) if a.get('goal') is not None else None


def controller_snapshot_digest():
    # Use the canonical snapshot implementation in a read-only child. Git status
    # must not refresh the index, and environment overrides must not change the
    # parent's frozen configuration (or another dispatch thread's environment).
    root = Path(__file__).resolve().parent
    code = ('import json; from pathlib import Path; '
            'from ssdlc_controller import controller_snapshot; '
            'print(json.dumps(controller_snapshot(Path.cwd()), sort_keys=True))')
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=root,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS='0', PYTHONDONTWRITEBYTECODE='1'),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    if result.returncode:
        raise SupervisorIntegrityError('filesystem_integrity')
    return digest(json.dumps(json.loads(result.stdout), sort_keys=True))


def compatibility(a, env=None):
    from orchestrator import resume_compatibility
    return resume_compatibility(a['repo'], a['ticket'], a['entrypoint'], a['from_phase'], env)


def refusal(store, reason='unsupported_transition'):
    store.prepare()
    # One durable audit line is the sole refusal evidence. No lock file is created.
    fd = open_safe(store.root, str(Path(store.ticket) / 'audit.jsonl'), os.O_RDWR | os.O_APPEND, True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = json.dumps(dict(type='unsupported_transition', ts=float(store.clock.time()))).encode() + b'\n'
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)
    result = public({}, 'unsupported', 'unsupported_transition')
    result['next_action'] = 'inspect_compatibility'
    return result


def pinned_root(repo, ticket, record):
    root = canonical_root(repo, ticket)
    expected = record['authorized'].get('runtime_root', '.ssdlc/' + ticket)
    if str(root.relative_to(Path(repo))) != expected:
        raise SupervisorIntegrityError('fingerprint_mismatch')
    return root


def new_record(a, store):
    project_env = build_project_env(a['repo'], os.environ)
    controller = Path(a.get('controller', Path(__file__).with_name('orchestrator.py'))).resolve()
    authorized = dict(entrypoint=a['entrypoint'], from_phase=a['from_phase'], goal_sha256=goal_digest(a),
                      goal_len=len(a['goal'].encode()) if a.get('goal') is not None else None, tier=a['tier'],
                      test_cmd_sha256=digest(a['test_cmd']) if a.get('test_cmd') is not None else None,
                      test_cmd_authority='operator_flag', reauthorization=a.get('_reauthorization', []), argv_id='orchestrator.' + a['entrypoint'],
                      review_repairs=a.get('review_repairs'), rounds=a.get('rounds'), compatibility_approved=True,
                      containers_possible=project_env.get('SSDLC_EXECUTION_MODE', 'docker') != 'native',
                      runtime_root=str(canonical_root(a['repo'], a['ticket']).relative_to(Path(a['repo']))),
                      task_mode=bool(read_document(canonical_root(a['repo'], a['ticket']) / 'state.json').get('task_breakdown')) or project_env.get('SSDLC_ENABLE_TASK_BREAKDOWN', 'false').lower() == 'true')
    return dict(schema_version=1, revision=0, worker_generation=1, run_id=store.run_id, nonce=uuid.uuid4().hex,
        resumes=a.get('_resumes'), project=dict(key=digest(a['repo'])[:16], name='project'), ticket=a['ticket'],
        created_at=float(store.clock.time()), authorized=authorized,
        fingerprints=dict(config_sha256=config_digest(project_env),
                          controller=dict(path_sha256=digest(str(controller)), sha256=digest(controller.read_bytes()), snapshot_sha256=controller_snapshot_digest()),
                          supervisor_py_sha256=digest(Path(__file__).read_bytes()), inputs_sha256=digest(json.dumps(authorized, sort_keys=True))),
        base_commit_at_launch=git_read(a['repo'], 'rev-parse', 'HEAD'), launch_revoked=False, launches=[],
        processes=dict(worker=None, controller=None, descendants=[]), containers=[], status='starting', status_reason='bootstrap',
        completion_kind=None, handoff=dict(outcome=None, at=None), exit_evidence=dict(source='none', code=None, intent_id=None),
        deadlines=dict(hard_deadline=None, preflight_deadline=None, quiet_threshold_sec=300, frozen_limits=configured_limits(project_env), handoff_timeout_sec=a['handoff_timeout_sec'], payload_io_timeout_sec=a['payload_io_timeout_sec']),
        recovery=dict(allowance_total=a['recovery_allowance'], used=0, next_retry_at=None, backoff_sec=0, history=[]),
        counters=dict(heartbeats=0, retries=0, classifications={}), heartbeat=None, preflight=None, classification=None,
        journal_baseline=dict(failed_attempts={}, attempts={}, taken_at_launch=False), cancel=None,
        evidence_links=dict(state_json=str((canonical_root(a['repo'], a['ticket']) / 'state.json').relative_to(Path(a['repo']))),
                            task_journal=str((canonical_root(a['repo'], a['ticket']) / '03-dev/tasks/task-journal.jsonl').relative_to(Path(a['repo'])))), pending_events=[])


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
    # An owned worker before any controller intent is ordinary preflight/backoff.
    # A missing controller after an intent remains ambiguous and fails closed.
    owned_preflight = (states == [OWNED, GONE] and d['processes']['controller'] is None
                       and intent_for(d, 'controller') is None)
    if d.get('cancel') and d['cancel']['state'] in {'requested', 'signalling'}:
        out['status'], out['status_reason'] = 'cancelling', 'cancel_pending'
    elif d['status'] not in TERMINAL and (UNKNOWN in states or NOT_OWNED in states or (states != [OWNED, OWNED] and not owned_preflight)):
        out['status'], out['status_reason'] = 'reconcile', 'reconcile'
    return public(out)


def public(d, status=None, reason=None):
    state = status or d.get('status', 'blocked')
    return dict(run_id=d.get('run_id'), status=state, status_reason=reason or d.get('status_reason', 'reconcile'),
                cancellation_state=(d.get('cancel') or {}).get('state'),
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
    build_argv('orchestrator.' + a['entrypoint'], a)
    if not sys.platform.startswith('linux'):
        return public({}, 'unsupported', 'unsupported_platform')
    store = RunStore(a['repo'], a['ticket'], clock=clock, fault=fault)
    payload = json.dumps(dict(args=a, env=build_project_env(a['repo'], os.environ))).encode()
    if len(payload) > 1048576:
        raise ValueError('inputs_too_large')
    # A repeated start observes an owned live run before reconsidering the
    # compatibility of a *new* run. Its journal may have advanced since launch.
    if not a.get('_resumes'):
        try:
            existing = store.current()
        except FileNotFoundError:
            existing = None
        if existing is not None and view(existing)['status'] in {'running', 'starting'}:
            if any(identity_state(existing, role) == OWNED for role in ('worker', 'controller')):
                return public(existing, 'already_running', 'already_running')
    if not compatibility(a, build_project_env(a['repo'], os.environ))['ok']:
        return refusal(store)
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
            if a.get('_resumes'):
                if d is None or d['run_id'] != a['_resumes'] or view(d)['status'] in {'running', 'starting', 'cancelling'}:
                    return public(d or {}, 'blocked', 'reconcile')
                if any(identity_state(d, role) != GONE for role in ('worker', 'controller')):
                    return public(d, 'blocked', 'reconcile')
                candidate = new_record(a, store)
                changes = fingerprint_changes(d, candidate)
                if changes and not a.get('reauthorize_fingerprints'):
                    return public(d, 'blocked', 'fingerprint_mismatch')
                a['_reauthorization'] = reauthorization_entries(d, changes, store.clock)
                d = None
            if d is not None:
                result = view(d)
                if result['status'] == 'running':
                    return public(d, 'already_running', 'already_running')
                # Only proven unreleased bootstrap death can consume a retry.
                if d['status'] == 'starting' and d['handoff']['outcome'] is None:
                    d = reconcile(store)
                    if d['status_reason'] != 'inputs_required':
                        return public(store.handoff('blocked', 'reconcile'))
                    if (d['authorized']['goal_sha256'] != goal_digest(a) or
                        d['authorized']['test_cmd_sha256'] != (digest(a['test_cmd']) if a.get('test_cmd') is not None else None) or
                        d['fingerprints']['controller']['sha256'] != digest(Path(build_argv('orchestrator.' + a['entrypoint'], a)[1]).read_bytes()) or
                        d['fingerprints']['config_sha256'] != config_digest(build_project_env(a['repo'], os.environ))):
                        return public(store.handoff('blocked', 'fingerprint_mismatch'))
                    def retry(v):
                        v['worker_generation'] += 1
                        v.update(status='starting', status_reason='bootstrap')
                    d = store.update(retry, event='launch_retry')
                else:
                    if result['status'] in TERMINAL:
                        # A prior successful handoff belongs to that run, not
                        # this start request. Record an explicit refusal instead.
                        return refusal(store)
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


def fingerprint_changes(old, new):
    changes = []
    for section, fields in [('authorized', ('goal_sha256', 'tier', 'test_cmd_sha256', 'runtime_root')),
                            ('fingerprints', ('config_sha256', 'controller'))]:
        for field in fields:
            a, b = old[section].get(field), new[section].get(field)
            if a != b:
                changes.append(dict(field=field, old_sha256=digest(json.dumps(a, sort_keys=True)),
                                    new_sha256=digest(json.dumps(b, sort_keys=True))))
    return changes


def reauthorization_entries(old, changes, clock):
    return old['authorized']['reauthorization'] + [dict(at=float(clock.time()),
        old_sha=c['old_sha256'], new_sha=c['new_sha256'], fields=[c['field']]) for c in changes]


def resume(args, clock=None, fault=None):
    a = dict(vars(args)) if isinstance(args, argparse.Namespace) else dict(args)
    if a.get('goal_file'):
        a['goal'] = Path(a['goal_file']).read_text()
    a = validate_args(a)
    build_argv('orchestrator.' + a['entrypoint'], a)
    store = RunStore(a['repo'], a['ticket'], a.get('run_id'), clock, fault)
    if not compatibility(a, build_project_env(a['repo'], os.environ))['ok']:
        return refusal(store)
    old = store.read() if store.run_id else store.current()
    candidate = new_record(a, store)
    changes = fingerprint_changes(old, candidate)
    if changes and not a.get('reauthorize_fingerprints'):
        return public(old, 'blocked', 'fingerprint_mismatch')
    a['_resumes'] = old['run_id']
    if changes:
        a['_reauthorization'] = reauthorization_entries(old, changes, store.clock)
        store.append(str(Path(store.ticket) / 'audit.jsonl'), dict(type='reauthorization', ts=float(store.clock.time()), run_id=old['run_id']))
    return start(a, clock, fault)


def status(repo, ticket, run_id=None, proc=None, env=None, clock=None):
    if not sys.platform.startswith('linux'):
        return public({}, 'unsupported', 'unsupported_platform')
    store = RunStore(repo, ticket, run_id)
    try:
        return observed_status(store, store.read() if run_id else store.current(), proc, env, clock)
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
            store.update(release, (args.generation, 0) if args.role == 'worker' else None, True, locked=True)
        if args.role == 'controller':
            payload = json.loads(transfer(args.payload_fd, timeout=d['deadlines']['payload_io_timeout_sec']))
            os.close(args.payload_fd)
            a = validate_args(payload['args'])
            argv = build_argv('orchestrator.' + a['entrypoint'], a)
            if goal_digest(a) != d['authorized']['goal_sha256'] or config_digest(payload['env']) != d['fingerprints']['config_sha256'] or digest(Path(argv[1]).read_bytes()) != d['fingerprints']['controller']['sha256'] or controller_snapshot_digest() != d['fingerprints']['controller'].get('snapshot_sha256'):
                raise FencedOut()
            with store.locked():
                d = store.read()
                current_intent = intent_for(d, 'controller')
                if (d['launch_revoked'] or d.get('cancel') or d['status'] in TERMINAL or
                        not current_intent or current_intent['intent_id'] != args.intent or
                        current_intent['generation'] != args.generation):
                    raise FencedOut()
            env = payload['env']
            env.update(markers(d, 'controller', args.generation, args.intent))
            env.update({MARK + k: str(v) for k, v in dict(
                REPO=a['repo'], TICKET=a['ticket'], PROJECT_KEY=d['project']['key'],
                EVENTS=str(store.root / store.path('controller-events.jsonl')),
                CONTROLLER_SHA=d['fingerprints']['controller']['snapshot_sha256'],
                CONFIG_SHA=d['fingerprints']['config_sha256']).items()})
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
    if args.payload_fd is None:
        raise FencedOut()
    try:
        payload = json.loads(transfer(args.payload_fd, timeout=d['deadlines']['payload_io_timeout_sec']))
    finally:
        os.close(args.payload_fd)
    if payload.get('monitor_only'):
        if config_digest(payload['env']) != d['fingerprints']['config_sha256']:
            raise FencedOut()
        missing_exit_ticks = 0
        while True:
            result = monitor_tick(store, payload['env'], generation=args.generation)
            # Reconciliation forbids launches, not read-only observation. During
            # exit, /proc ownership can be briefly unknown before run_ended binds.
            if result['status'] in TERMINAL:
                return 0
            if (result['status'] == 'reconcile' and result.get('status_reason') == 'exit_evidence_unavailable' and
                    identity_state(store.read(), 'controller') == GONE):
                missing_exit_ticks += 1
                if missing_exit_ticks >= 3:
                    # Allow a late event snapshot to settle, then leave the
                    # persisted uncertain outcome for explicit reconciliation.
                    return 0
            else:
                missing_exit_ticks = 0
            store.clock.sleep(1)
    a = validate_args(payload['args'])
    argv = build_argv('orchestrator.' + a['entrypoint'], a)
    if goal_digest(a) != d['authorized']['goal_sha256'] or config_digest(payload['env']) != d['fingerprints']['config_sha256'] or digest(Path(argv[1]).read_bytes()) != d['fingerprints']['controller']['sha256'] or controller_snapshot_digest() != d['fingerprints']['controller'].get('snapshot_sha256'):
        store.handoff('blocked', 'fingerprint_mismatch', args.generation)
        return 11
    # The worker receives only markers at exec; frozen inputs stay in memory.
    def deadlines(v):
        if v['deadlines']['hard_deadline'] is None:
            v['deadlines']['hard_deadline'] = ticket_hard_deadline(a['repo'], a['ticket'], payload['env'], store.clock.time())
    store.update(deadlines, (args.generation, 0))
    if not preflight_loop(store, payload['env'], args.generation):
        return 10
    capture_journal_baseline(store, args.generation)
    store.fault.hit('cancel.revokes_pending_launch')
    if not compatibility(a, payload['env'])['ok']:
        store.handoff('blocked', 'unsupported_transition', args.generation)
        return 14
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
    while controller.poll() is None:
        monitor_tick(store, payload['env'], generation=args.generation)
        store.clock.sleep(1)
    code = controller.returncode
    def exited(v):
        v['exit_evidence'] = dict(source='parent_returncode', code=code, intent_id=intent)
    store.update(exited, (args.generation, 0), event='controller_exited')
    monitor_tick(store, payload['env'], generation=args.generation)
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
                    candidates.remove((ident, ppid, fd))
                    proc.close(fd)
                    progress = True
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
                # TERM can make a candidate a zombie while /proc/environ is
                # being read. Recheck its birth/state before declaring it an
                # unowned live session member; never signal a recycled PID.
                for item in pending[:]:
                    ident, _, fd = item
                    try:
                        fresh, state, _ = proc.stat(ident.pid)
                        gone = state == 'Z' or (
                            fresh.boot_id == ident.boot_id and fresh.start_ticks != ident.start_ticks)
                    except (FileNotFoundError, ProcessLookupError):
                        gone = True
                    if gone:
                        pending.remove(item)
                        candidates.remove(item)
                        proc.close(fd)
                        progress = True
                if progress:
                    continue
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


def cancel(repo, ticket, run_id=None, grace=5, clock=None, proc=None, fault=None, docker=None):
    if not sys.platform.startswith('linux'):
        return public({}, 'unsupported', 'unsupported_platform')
    if isinstance(grace, bool) or not isinstance(grace, (float, int)) or not 0 <= grace <= 300:
        raise ValueError('invalid_grace')
    store = RunStore(repo, ticket, run_id, clock, fault)
    proc = proc or Proc()
    d = store.read() if run_id else store.current()
    if d['status'] == 'cancelled' or (d.get('cancel') and d['cancel']['state'] == 'done'):
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
                prior = dict(status=v['status'], reason=v['status_reason']) if v['status'] in TERMINAL else None
                v['cancel'] = dict(epoch=1, requested_at=float(store.clock.time()), state='requested',
                                   term_at=None, kill_at=None, preserved_terminal=prior)
            v['cancel'].update(executor=executor, executor_token=token)
            v.update(status='cancelling', status_reason='cancel_requested', launch_revoked=True)
        d = store.update(request, event='cancel_requested', locked=True)
    try:
        store.fault.hit('cancel.after_intent_before_signal')
        if d['processes']['controller'] is None:
            store.fault.hit('cancel.worker_only_preflight')
        return _execute_cancel(store, d, grace, proc, token, docker)
    finally:
        # A real crash leaves the birth-bound lease for restart verification.
        # Graceful errors (including injected exceptions) release it safely.
        def release(v):
            if v['cancel'].get('executor_token') == token:
                v['cancel'].update(executor=None, executor_token=None)
        store.update(release)


def _execute_cancel(store, d, grace, proc, token, docker=None):
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
    if docker is None and d['authorized'].get('containers_possible', True):
        docker = DockerCancellation()
    try:
        for container in docker.list(d['run_id'], d['project']['key']) if docker is not None else []:
            labels = container.get('labels', {})
            if (labels.get('ssdlc.run_id') == d['run_id'] and
                    labels.get('ssdlc.project') == d['project']['key'] and
                    re.fullmatch(r'[a-f0-9]{64}', container.get('id', '')) and
                    container.get('state') in {'running', 'restarting', 'paused'}):
                docker.stop(container, grace)
                store.fault.hit('cancel.after_container_stop_before_record')
    except (OSError, ValueError, subprocess.SubprocessError):
        return blocked('docker_unavailable')
    def finish(v):
        prior = v['cancel'].get('preserved_terminal')
        if prior is None:
            v.update(status='cancelled', status_reason='cancelled', in_flight='uncertain')
        else:
            v.update(status=prior['status'], status_reason=prior['reason'])
            if prior['status'] != 'completed':
                v['in_flight'] = 'uncertain'
        v['cancel'].update(state='done', kill_at=float(store.clock.time()))
        if v['handoff']['outcome'] is None:
            v['handoff'] = dict(outcome='blocked', at=float(store.clock.time()))
    return public(store.update(finish, event='cancel_finished'))



# The table is both policy and the public recovery-matrix view.
RECOVERY_MATRIX = {
    'transient_environment_provider': dict(mode='auto', action='Retry read-only preflight within the persisted allowance.'),
    'authentication': dict(mode='operator-only', action='Refresh authentication out of band, then start or resume.'),
    'integrity': dict(mode='operator-only', action='Inspect evidence and reconcile integrity before resuming.'),
    'limits': dict(mode='operator-only', action='Explicitly raise the exhausted limit before resuming.'),
    'code_defect': dict(mode='operator-only', action='Configured attempts may be consumed. Explicitly raise the allowance if needed; request resume run from 03-dev, plan or design through orchestrator compatibility checks.'),
    'uncertain_outcome': dict(mode='operator-only', action='Inspect the invocation outcome and reconcile before any explicit resume.'),
    'operator_decision': dict(mode='operator-only', action='Resolve the scope or environment decision before explicit resume.'),
    'unsupported_transition': dict(mode='unsupported', action='Choose a supported orchestrator entrypoint after inspecting compatibility.'),
}
ENGINES = {'claude', 'codex', 'agy', 'openrouter'}
ROLE_KEYS_BY_ENTRYPOINT = {
    'run': ('00-recon', '01-po', '02-design-arch', '02-design-sec', '03-dev', '05-qa', '05b-appsec', '06-ops', '07-docs'),
    'plan': ('02-design-arch', '02-design-sec'),
    'design': ('02-design-arch', '02-design-sec'),
}
PROFILES = ('ALGORITHMIC', 'FRONTEND_UI', 'FULLSTACK', 'REFACTOR', 'SECURITY')
PROBE_CODES = {'auth_missing', 'auth_expired', 'auth_unverified', 'auth_unknown', 'auth_unsupported',
               'auth_verified', 'probe_unavailable', 'provider_unavailable', 'quota_unknown'}
SUMMARIES = {
    'auth_missing': 'Required authentication is missing.',
    'auth_expired': 'Required authentication has expired.',
    'auth_unverified': 'Credential presence has not been verified.',
    'auth_unknown': 'Authentication status is unknown.',
    'auth_unsupported': 'Native authentication verification is unsupported.',
    'auth_verified': 'Native authentication verification succeeded.',
    'probe_unavailable': 'Read-only native verification is unavailable.',
    'provider_unavailable': 'The provider is temporarily unavailable.',
    'quota_unknown': 'Quota verification is unavailable.',
}
DEPENDENCIES = {'git', 'docker', 'claude', 'codex', 'agy', 'pytest', 'npm', 'ruff', 'mypy'}
REASONS.update({'limit_reached', 'outcome_uncertain', 'git_lock_unowned', 'design_stalled', 'environment_diagnosis'})
REASONS.update(PROBE_CODES | {'preflight_ok', 'preflight_failed', 'docker_unavailable', 'path_unwritable',
    'disk_low', 'routes_unavailable', 'engine_unsupported', 'quality_contract_invalid', 'recovery_exhausted',
    'retry_scheduled', 'hard_deadline', 'completed', 'completion_mismatch', 'observing',
    'attempt_allowance_exhausted', 'terminal_check_failure', 'terminal_development_blocked',
    'integrity_failure', 'budget_exceeded', 'resource_limit', 'operator_decision', 'unsupported_transition',
    'terminal_unbound', 'cancel_pending'} | {x + '_missing' for x in DEPENDENCIES})


def build_project_env(repo, host_env):
    """Construct frozen configuration without activating isolation or writing files."""
    env = {k: v for k, v in host_env.items() if not k.startswith(MARK)}
    home = Path(env.get('HOME', str(Path.home())))
    for path, override in ((home / '.ssdlc/global.env', False), (Path(repo) / '.ssdlc/project.env', True)):
        if not path.is_file():
            continue
        for raw in path.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].strip()
            if '=' in line:
                k, v = line.split('=', 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if override or k not in env:
                    env[k] = v
    env = {k: v for k, v in env.items() if not k.startswith(MARK)}
    runtime = Path(repo).resolve() / '.ssdlc/.runtime'
    env.update(CODEX_HOME=str(runtime / 'codex_home'), CLAUDE_CONFIG_DIR=str(runtime / 'claude_home'),
               XDG_CACHE_HOME=str(runtime / 'cache'))
    # Freeze the requested locale rather than letting child Python startup
    # silently rewrite LC_CTYPE after the configuration fingerprint is taken.
    env.setdefault('PYTHONCOERCECLOCALE', '0')
    env.setdefault('SSDLC_PROJECT_ID', Path(repo).resolve().name)
    return env


def read_document(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def git_read(repo, *args):
    try:
        p = subprocess.run(['git', '-C', str(repo), *args], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                           env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
        return p.stdout.decode().strip() if p.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return ''


def route_child(repo, entrypoint, tier, ticket=''):
    """Read-only child. Real HOME, uid and PATH preserve Codex's predicate."""
    from orchestrator import SquadDispatcher, resolve_review_entry
    state = read_document(canonical_root(repo, ticket) / 'state.json') if ticket else {}
    plan = read_document(canonical_root(repo, ticket) / '02-design/plan.json') if ticket else {}
    allowed = None
    if ticket and (canonical_root(repo, ticket) / 'limits/policy.json').is_file():
        from ssdlc_recovery import LimitsStore
        allowed = LimitsStore(canonical_root(repo, ticket))._load(repair=False)[0]['binding'].get('allowed_phases')
    profiles = sorted({t.get('profile', 'ALGORITHMIC') for t in plan.get('tasks', [])}) if plan.get('tasks') else (
        PROFILES if state.get('task_breakdown') or os.environ.get('SSDLC_ENABLE_TASK_BREAKDOWN') == 'true'
        else (state.get('dev_profile', 'ALGORITHMIC'),))
    routes, panel = {}, []
    for t in ('S', 'M', 'L') if tier == 'auto' else (tier,):
        for role in ROLE_KEYS_BY_ENTRYPOINT[entrypoint]:
            if allowed is not None and ('02-design' if role.startswith('02-design') else role) not in allowed:
                continue
            if entrypoint == 'run' and role.startswith('02-design') and t == 'S':
                continue
            if role == '05b-appsec' and t != 'L' and 'APPSEC_SUPPLY_CHAIN_REDTEAM' not in state.get('specialists_triggered', []):
                continue
            for profile in profiles if role == '03-dev' else (state.get('dev_profile', 'ALGORITHMIC'),):
                engine = SquadDispatcher.dispatch_role(role, 'M' if role == '00-recon' else t, profile, 1)[0]
                routes.setdefault(role, []).append(engine)
        if entrypoint == 'run':
            for model in SquadDispatcher.resolve_review_models(t, attempt=1)[0]:
                engine, model = resolve_review_entry(model, os.environ.get('SSDLC_REVIEW_ENGINE', 'openrouter'))
                panel.append(dict(engine=engine, model=model))
    return dict(routes={k: sorted(set(v)) for k, v in routes.items()}, review_panel=panel)


def claude_credential_present(env):
    project = Path(env['CLAUDE_CONFIG_DIR']) / '.credentials.json'
    host = Path(env.get('HOME', str(Path.home()))) / '.claude/.credentials.json'
    return project.is_file() or (env.get('SSDLC_INHERIT_HOST_CLAUDE_AUTH', 'true').lower() == 'true' and host.is_file())


def resolve_routes(repo, entrypoint, tier, env, ticket=''):
    # Only an empty marker is materialized, outside project and host runtime.
    # The route child itself never creates files, including import bytecode.
    with tempfile.TemporaryDirectory(prefix='ssdlc-routes-') as temp:
        child_env = dict(env, CLAUDE_CONFIG_DIR=temp, PYTHONDONTWRITEBYTECODE='1')
        if claude_credential_present(env):
            fd = os.open(Path(temp) / '.credentials.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), '_routes', '--repo', str(repo),
                            '--entrypoint', entrypoint, '--tier', tier, '--ticket', ticket], env=child_env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        if p.returncode:
            raise ValueError('routes_unavailable')
        result = json.loads(p.stdout)
    engines = {e for values in result['routes'].values() for e in values}
    engines.update(x['engine'] for x in result['review_panel'])
    return engines


class NativeProbe:
    """Explicitly registered bounded read-only adapter. No built-in commands ship.

    Implementations must respect timeout and never login, infer, or write auth.
    Only normalized statuses/codes escape this interface; all other data dies in memory.
    """
    def check(self, engine, env, timeout):
        raise NotImplementedError()


class RefreshAdapter:
    """An authorized isolated auth_expired refresh; no implementation ships."""
    def refresh(self, engine, env, runtime_dirs, timeout):
        raise NotImplementedError()


def normalized_probe(value):
    if not isinstance(value, dict):
        return dict(status='unknown', code='auth_unknown', summary=SUMMARIES['auth_unknown'])
    code = value.get('code')
    status = value.get('status')
    if code not in PROBE_CODES or status not in {'ok', 'failed', 'unknown', 'unsupported', 'unverified'}:
        code, status = 'auth_unknown', 'unknown'
    # Presence cannot masquerade as native verification.
    if code == 'auth_unverified':
        status = 'unverified'
    elif code in {'auth_missing', 'auth_expired'}:
        status = 'failed'
    elif code == 'auth_verified' and status != 'ok':
        code, status = 'auth_unknown', 'unknown'
    return dict(status=status, code=code, summary=SUMMARIES[code])


def credential_present(engine, env):
    home = Path(env.get('HOME', str(Path.home())))
    if engine == 'openrouter':
        return bool(env.get('OPENROUTER_API_KEY', '').strip())
    if engine == 'claude':
        return bool(env.get('CLAUDE_CODE_OAUTH_TOKEN')) or claude_credential_present(env)
    if engine == 'codex':
        return (Path(env['CODEX_HOME']) / 'auth.json').is_file() or (
            env.get('SSDLC_INHERIT_HOST_CODEX_AUTH', 'true').lower() == 'true' and
            any(p.is_file() for p in ([home / '.codex/auth.json'] + ([Path('/root/.codex/auth.json')] if os.geteuid() == 0 else []))))
    return None  # No verified credential inventory for agy.


def quality_dependencies(repo, ticket):
    root = Path(repo)
    for path in (canonical_root(root, ticket) / '00-recon/quality-contract.json', root / '.ssdlc/quality-contract.json'):
        if path.is_file():
            contract = json.loads(path.read_text())
            checks = contract['checks']
            return {x for check in checks if check.get('required', True) for x in DEPENDENCIES
                    if x in check.get('argv', [])}
    needed = set()
    for path in (root / 'tests').rglob('test_*.py'):
        if re.search(r'^def test_|^import pytest|^from pytest', path.read_text(), re.M):
            needed.add('pytest')
    package = read_document(root / 'package.json')
    if any(x in package.get('scripts', {}) for x in ('test', 'lint', 'typecheck')):
        needed.add('npm')
    return needed


def preflight(repo, ticket, env, entrypoint='run', tier='auto', probes=None, refresh=None,
              route_resolver=None, which=None, runner=None, disk_usage=None):
    """Read-only scoped checks; adapters see a copy of only this project's env."""
    env, checks = dict(env), []
    probes = probes or {}
    which, runner = which or shutil.which, runner or subprocess.run
    def add(name, status, code):
        checks.append(dict(id=name, status=status, code=code))
    try:
        engines = (route_resolver or resolve_routes)(repo, entrypoint, tier, env, ticket)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        engines = set()
        add('routes', 'failed', 'routes_unavailable')
    tools = {'git'} | (engines & {'claude', 'codex', 'agy'})
    if env.get('SSDLC_EXECUTION_MODE', 'docker') == 'docker':
        tools.add('docker')
    try:
        tools.update(quality_dependencies(repo, ticket))
    except (ValueError, OSError, KeyError, TypeError):
        add('quality_contract', 'failed', 'quality_contract_invalid')
    for tool in sorted(tools):
        present = bool(which(tool, path=env.get('PATH', os.defpath)))
        add(tool, 'ok' if present else 'failed', 'preflight_ok' if present else tool + '_missing')
        if tool == 'docker' and present:
            try:
                p = runner(['docker', 'info'], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
                if p.returncode:
                    checks[-1].update(status='failed', code='docker_unavailable')
            except (OSError, subprocess.TimeoutExpired):
                checks[-1].update(status='failed', code='docker_unavailable')
    for engine in sorted(engines):
        if engine not in ENGINES:
            add('engine', 'unsupported', 'engine_unsupported')
            continue
        adapter = probes.get(engine)
        if adapter is not None and isinstance(adapter, NativeProbe):
            try:
                result = normalized_probe(adapter.check(engine, dict(env), timeout=10))
            except Exception:
                result = normalized_probe(dict(status='unknown', code='probe_unavailable'))
        else:
            present = credential_present(engine, env)
            result = normalized_probe(dict(status='failed' if present is False else 'unverified' if present else 'unsupported',
                code='auth_missing' if present is False else 'auth_unverified' if present else 'auth_unsupported'))
        if result['code'] == 'auth_expired' and isinstance(refresh, RefreshAdapter):
            try:
                runtime = Path(repo).resolve() / '.ssdlc/.runtime'
                dirs = {k: env[k] for k in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'XDG_CACHE_HOME')}
                if any(runtime not in Path(v).resolve().parents for v in dirs.values()):
                    raise ValueError('refresh_not_isolated')
                result = normalized_probe(refresh.refresh(engine, dict(env), dirs, timeout=10))
            except Exception:
                result = normalized_probe(dict(status='failed', code='auth_expired'))
        add('auth_' + engine, result['status'], result['code'])
    root = Path(repo)
    ancestor = root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    paths = [ancestor] + [p for p in (root / '.ssdlc', root / '.ssdlc/.runtime') if p.exists()]
    if any(not os.access(p, os.W_OK | os.X_OK) or p.stat().st_mode & 0o222 == 0 for p in paths):
        add('path', 'failed', 'path_unwritable')
    else:
        add('path', 'ok', 'preflight_ok')
    try:
        minimum = int(env.get('SSDLC_PREFLIGHT_MIN_FREE_MB', '100')) * 1024 * 1024
        free = (disk_usage or shutil.disk_usage)(ancestor).free
        add('disk', 'failed' if free < minimum else 'ok', 'disk_low' if free < minimum else 'preflight_ok')
    except (OSError, ValueError):
        add('disk', 'unknown', 'probe_unavailable')
    failures = [c for c in checks if c['status'] == 'failed']
    auth = next((c for c in failures if c['code'].startswith('auth_')), None)
    failure = auth or next(iter(failures), None)
    return dict(checks=checks, ok=not failures and all(c['code'] != 'engine_unsupported' for c in checks),
                code=failure['code'] if failure else 'preflight_failed' if any(c['code'] == 'engine_unsupported' for c in checks) else 'preflight_ok')


def peek_journal(repo, ticket, record=None):
    directory = (pinned_root(repo, ticket, record) if record else canonical_root(repo, ticket)) / '03-dev/tasks'
    if not directory.is_dir() or not (directory / 'task-journal.jsonl').is_file():
        return None, False
    from ssdlc_tasks import TaskJournal, TaskError
    try:
        return TaskJournal(directory).peek(), False
    except (TaskError, OSError, ValueError, KeyError, TypeError):
        return None, True


def capture_journal_baseline(store, generation):
    journal, corrupt = peek_journal(store.root.parent.parent, store.ticket)
    if corrupt:
        raise SupervisorIntegrityError('integrity_failure')
    tasks = (journal or {}).get('tasks', {})
    def save(v):
        v['journal_baseline'] = dict(failed_attempts={k: len(t.get('failed_attempts', [])) for k, t in tasks.items()},
            attempts={k: t.get('attempts', 0) for k, t in tasks.items()}, taken_at_launch=True)
    store.update(save, (generation, 0))


CATEGORIES = {'INTEGRITY', 'ENVIRONMENT_RESOURCE', 'SCOPE_REQUIRED', 'ENVIRONMENT_BLOCKED', 'PLAN_ERROR',
              'TASK_EXECUTION', 'CHECK_FAILURE', 'REPAIR_STALLED', 'DEVELOPER_PROTOCOL', 'DEVELOPER_INVOCATION',
              'AUTHENTICATION', 'AUTH_MISSING', 'AUTH_EXPIRED',
              'LIMIT_REACHED', 'OUTCOME_UNCERTAIN', 'GIT_LOCK_UNOWNED', 'DESIGN_STALLED', 'ENVIRONMENT_DIAGNOSIS'}
EXC_TYPES = {'TaskError', 'DevelopmentBlocked', 'ResourceLimitError', 'PlanError', 'RuntimeError', 'TimeoutError',
             'TimeoutExpired', 'ConnectionError', 'OSError', 'ValueError', 'SystemExit', 'KeyboardInterrupt',
             'SupervisorIntegrityError', 'FencedOut', 'Exception', 'RecoveryStop', 'QuiescentTimeout', 'NotDispatched'}
CONTROLLER_TYPES = {'shim_released', 'phase_started', 'phase_finished', 'failure', 'progress',
                    'dispatch_started', 'dispatch_finished', 'run_ended'}
PHASES = {'00-recon', '01-po', '02-design', '02-design-arch', '02-design-sec', '03-dev', '04-review',
          '05-qa', '05b-appsec', '06-ops', '07-docs', '08-supervisor'}


def valid_controller_event(x, d):
    allowed = {'seq', 'ts', 'run_id', 'role', 'gen', 'intent', 'type', 'phase', 'step', 'category',
               'exc_type', 'outcome', 'exit_code', 'dispatch_id', 'engine', 'task_id', 'attempt'}
    launch = intent_for(d, 'controller')
    if not isinstance(x, dict) or set(x) - allowed or not launch:
        return False
    if not (x.get('run_id') == d['run_id'] and x.get('role') == 'controller' and
            x.get('gen') == launch['generation'] and x.get('intent') == launch['intent_id']):
        return False
    if x.get('type') not in CONTROLLER_TYPES or type(x.get('seq')) is not int or x['seq'] < 1:
        return False
    if type(x.get('ts')) not in (int, float) or not math.isfinite(x['ts']):
        return False
    if 'phase' in x and x['phase'] not in PHASES:
        return False
    if 'step' in x and x['step'] not in {'started', 'finished', 'running', 'verification', 'development', 'review'}:
        return False
    if 'category' in x and x['category'] not in CATEGORIES:
        return False
    if 'exc_type' in x and x['exc_type'] is not None and x['exc_type'] not in EXC_TYPES:
        return False
    if 'engine' in x and x['engine'] not in ENGINES:
        return False
    if 'task_id' in x and (not isinstance(x['task_id'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', x['task_id'])):
        return False
    if 'attempt' in x and (type(x['attempt']) is not int or x['attempt'] < 1):
        return False
    if 'task_id' in x and 'attempt' not in x:
        return False
    if x['type'].startswith('dispatch_'):
        if not isinstance(x.get('dispatch_id'), str) or not RUN_ID.fullmatch(x['dispatch_id']):
            return False
        if x['type'] == 'dispatch_started' and x.get('engine') not in ENGINES:
            return False
        if x['type'] == 'dispatch_finished' and x.get('outcome') not in {'returned', 'raised'}:
            return False
    if x['type'] == 'failure' and (x.get('category') not in CATEGORIES or x.get('exc_type') not in EXC_TYPES):
        return False
    if 'exit_code' in x and type(x['exit_code']) is not int:
        return False
    return x['type'] != 'run_ended' or type(x.get('exit_code')) is int


def controller_events(store, d):
    records, malformed, seq = [], 0, 0
    try:
        fd = open_safe(store.root, store.path('controller-events.jsonl'), os.O_RDONLY)
    except FileNotFoundError:
        return [], 0
    with os.fdopen(fd, 'rb') as f:
        for line in f:
            try:
                x = json.loads(line)
                if not valid_controller_event(x, d) or x['seq'] <= seq:
                    raise ValueError()
                records.append(x)
                seq = x['seq']
            except (ValueError, UnicodeError, TypeError, KeyError):
                malformed += 1
    starts = [x['dispatch_id'] for x in records if x['type'] == 'dispatch_started']
    finishes = [x['dispatch_id'] for x in records if x['type'] == 'dispatch_finished']
    malformed += len(starts) - len(set(starts)) + len(finishes) - len(set(finishes))
    malformed += sum(x not in starts for x in finishes)
    return records, malformed


@dataclass(frozen=True)
class TerminalOutcome:
    dispatches: tuple
    last_dispatch: object
    final_failure: object
    dispatch_count: int


def terminal_outcome(events):
    """Join by dispatch identity, never by event position."""
    finished = {e['dispatch_id']: e for e in events if e['type'] == 'dispatch_finished'}
    dispatches = []
    for e in events:
        if e['type'] != 'dispatch_started':
            continue
        end = finished.get(e['dispatch_id'])
        if end is not None and end['seq'] <= e['seq']:
            end = None
        dispatches.append(dict(e, outcome=end['outcome'] if end else 'open', finished_seq=end['seq'] if end else None))
    failures = [e for e in events if e['type'] == 'failure']
    return TerminalOutcome(tuple(dispatches), dispatches[-1] if dispatches else None,
                           failures[-1] if failures else None, len(dispatches))


def ticket_hard_deadline(repo, ticket, env, now):
    deadline = configured_hard_deadline(env, now)
    root = canonical_root(repo, ticket)
    if (root / 'limits/policy.json').exists():
        from ssdlc_recovery import LimitsStore
        _, state = LimitsStore(root)._load(repair=False)
        deadline = min(deadline, state['deadline_at']) if deadline is not None else state['deadline_at']
    return deadline


def configured_hard_deadline(env, now):
    # Per-call dev timeouts do not bound a complete multi-task pipeline. Only
    # an explicitly configured run timeout authorizes deadline cancellation.
    value = env.get('SSDLC_SUPERVISOR_HARD_TIMEOUT')
    if value is None:
        return None
    seconds = int(value)
    if seconds <= 0:
        raise ValueError('invalid_hard_timeout')
    return float(now + seconds)


def configured_limits(env):
    return {key: int(env.get(key, default)) for key, default in (
        ('SSDLC_MAX_DEV_RETRIES', '3'), ('SSDLC_MAX_DESIGN_ROUNDS', '5'), ('SSDLC_DEV_TIMEOUT', '2400'))}


def classification_predicates(evidence):
    """Independent predicates used by the classifier and disjointness tests."""
    e = evidence
    outcome = terminal_outcome(e.get('events', []))
    final, last = outcome.final_failure or {}, outcome.last_dispatch or {}
    state, journal = e.get('state', {}), e.get('journal') or {}
    tasks, baseline = journal.get('tasks', {}), e.get('baseline', {}).get('failed_attempts', {})
    limit = configured_limits(e.get('env', {}))['SSDLC_MAX_DEV_RETRIES']
    new = {k: t for k, t in tasks.items() if len(t.get('failed_attempts', [])) > baseline.get(k, 0)}
    failed = {k: t for k, t in new.items() if t.get('status') == 'FAILED'}
    binding = False
    category = None
    if len(failed) == 1:
        task_id, task = next(iter(failed.items()))
        attempt = task['failed_attempts'][-1]
        category = attempt.get('category')
        binding = (attempt.get('attempt') == task.get('attempts') and last.get('outcome') == 'returned' and
                   last.get('task_id') == task_id and last.get('attempt') == task.get('attempts'))
    guards = []
    if any(x['outcome'] == 'open' for x in outcome.dispatches):
        guards.append('U1')
    finishes = [x for x in e.get('events', []) if x['type'] == 'dispatch_finished']
    if finishes and finishes[-1].get('outcome') == 'raised':
        guards.append('U2')
    for task_id, task in new.items():
        attempt = task['failed_attempts'][-1]
        if last.get('task_id') != task_id or last.get('attempt') != attempt.get('attempt'):
            guards.append('U3')
        if last.get('outcome') == 'returned' and attempt.get('category') not in {'CHECK_FAILURE', 'REPAIR_STALLED', 'DEVELOPER_PROTOCOL'}:
            guards.append('U7')
    failure = state.get('failure')
    canonical_failure = isinstance(failure, dict) and failure.get('category') in CATEGORIES
    ended = [x for x in e.get('events', []) if x['type'] == 'run_ended']
    if e.get('exit_code') not in (None, 0) and not final and not canonical_failure:
        guards.append('U4')
    if e.get('malformed') or final.get('category') not in CATEGORIES | {None}:
        guards.append('U5')
    if e.get('released') and not final and not ended and not canonical_failure:
        guards.append('U6')
    row3 = state.get('status') == 'HALTED_BUDGET_EXCEEDED' or final.get('category') == 'ENVIRONMENT_RESOURCE' or e.get('hard_deadline', False)
    capture_uncertain = journal.get('candidate_isolation') == 1 and any(
        t.get('status') == 'RUNNING' and not t.get('capture_sha256') for t in tasks.values())
    row3c = (not row3 and not capture_uncertain and final.get('exc_type') == 'TaskError' and outcome.dispatch_count == 0 and any(
        t.get('attempts', 0) >= limit and t.get('status') != 'COMPLETED' and k not in new for k, t in tasks.items()))
    consistent = ((category == 'CHECK_FAILURE' and final.get('exc_type') == 'TaskError' and
                   next(iter(failed.values())).get('attempts', 0) >= limit) if failed else False) or (
                   category in {'REPAIR_STALLED', 'DEVELOPER_PROTOCOL'} and final.get('exc_type') == 'DevelopmentBlocked' and final.get('category') == category)
    row6 = bool(not row3 and not row3c and not guards and binding and consistent and
                final.get('seq', 0) > (last.get('finished_seq') or 0))
    row4 = state.get('status') == 'ESCALATED_TO_SUPERVISOR' or (binding and final.get('category') in {'SCOPE_REQUIRED', 'ENVIRONMENT_BLOCKED'})
    return dict(row3=bool(row3), row3c=bool(row3c), row4=bool(row4), row6=row6,
                capture_uncertain=capture_uncertain, guards=sorted(set(guards)),
                diagnostics=[dict(code='earlier_task_failure') for _ in new])


def classify(evidence):
    """Pure rev-5 table. Raw output and exception messages are never inspected."""
    e = evidence
    p = classification_predicates(e)
    final = terminal_outcome(e.get('events', [])).final_failure or {}
    checks = e.get('preflight', {}).get('checks', [])
    auth = next((c for c in checks if c.get('status') == 'failed' and c.get('code') in {'auth_missing', 'auth_expired'}), None)
    if auth or final.get('category') in {'AUTH_MISSING', 'AUTH_EXPIRED', 'AUTHENTICATION'}:
        code = auth['code'] if auth else {'AUTH_MISSING': 'auth_missing', 'AUTH_EXPIRED': 'auth_expired', 'AUTHENTICATION': 'auth_unverified'}[final['category']]
        cls, row = 'authentication', '1'
    elif e.get('integrity') or e.get('journal_corrupt') or final.get('category') == 'INTEGRITY':
        cls, code, row = 'integrity', 'integrity_failure', '2'
    elif final.get('category') in {'LIMIT_REACHED', 'OUTCOME_UNCERTAIN', 'GIT_LOCK_UNOWNED', 'DESIGN_STALLED', 'ENVIRONMENT_DIAGNOSIS'}:
        from ssdlc_recovery import STOPS
        category = final['category']
        cls, code = STOPS[category], category.lower()
        row = '3' if cls == 'limits' else '5' if cls == 'uncertain_outcome' else '4' if cls == 'operator_decision' else '3b'
    elif p['row3']:
        code = 'hard_deadline' if e.get('hard_deadline') else 'resource_limit' if final.get('category') == 'ENVIRONMENT_RESOURCE' else 'budget_exceeded'
        cls, row = 'limits', '3'
    elif not e.get('controller_launched') and any(c.get('status') == 'failed' and c.get('code') in {'docker_unavailable', 'provider_unavailable', 'probe_unavailable'} for c in checks):
        cls, code, row = 'transient_environment_provider', 'preflight_failed', '3b'
    elif p['row3c']:
        cls, code, row = 'limits', 'attempt_allowance_exhausted', '3c'
    elif p['capture_uncertain']:
        cls, code, row = 'uncertain_outcome', 'terminal_unbound', '5'
    elif p['row4']:
        cls, code, row = 'operator_decision', 'operator_decision', '4'
    elif p['guards']:
        cls, code, row = 'uncertain_outcome', 'terminal_unbound', '5'
    elif p['row6']:
        cls, code, row = 'code_defect', 'terminal_check_failure' if final.get('exc_type') == 'TaskError' else 'terminal_development_blocked', '6'
    elif final.get('category') == 'PLAN_ERROR' and e.get('entrypoint') in {'plan', 'design'} and e.get('compatibility_approved'):
        cls, code, row = 'unsupported_transition', 'unsupported_transition', '7'
    else:
        cls, code, row = 'uncertain_outcome', 'terminal_unbound', '8'
    return dict(**{'class': cls}, reason_code=code, row=row, terminal_bound=row == '6',
                diagnostics=p['diagnostics'] + [dict(code=g) for g in p['guards']])


def evidence_for(store, d, env, events=None, malformed=0):
    repo = store.root.parent.parent
    env = dict(env)
    for key, value in d['deadlines'].get('frozen_limits', {}).items():
        env[key] = str(value)
    journal, corrupt = peek_journal(repo, store.ticket, d)
    if events is None:
        events, malformed = controller_events(store, d)
    return dict(state=read_document(pinned_root(repo, store.ticket, d) / 'state.json'), journal=journal,
        journal_corrupt=corrupt, events=events, malformed=malformed, baseline=d['journal_baseline'], env=env,
        preflight=d.get('preflight') or {}, controller_launched=any(x['role'] == 'controller' for x in d['launches']),
        exit_code=d['exit_evidence'].get('code'), entrypoint=d['authorized']['entrypoint'],
        compatibility_approved=d['authorized'].get('compatibility_approved', False),
        released=any(x['type'] == 'shim_released' and x.get('role') == 'controller' for x in store.events()),
        hard_deadline=d['deadlines'].get('hard_deadline') is not None and store.clock.time() > d['deadlines']['hard_deadline'])


def completion(store, d, state):
    repo = store.root.parent.parent
    base = d['base_commit_at_launch']
    if not base or state.get('base_commit') != base or git_read(repo, 'rev-parse', 'HEAD') != base:
        return False
    kind = d['authorized']['entrypoint']
    if kind == 'run':
        if state.get('status') != 'AWAITING_SUPERVISOR_SIGNOFF':
            return False
        # Legacy pipelines have no task checkpoint. Bind the decision to the
        # mode authorized at launch; old records without a mode remain strict.
        task_mode = d['authorized'].get('task_mode', True) or bool(state.get('task_breakdown')) or bool(state.get('task_verified_head'))
        if not task_mode:
            return True
        worktree = pinned_root(repo, store.ticket, d) / 'worktree'
        return (worktree.is_dir() and bool(state.get('task_verified_head')) and
                git_read(worktree, 'rev-parse', 'HEAD') == state['task_verified_head'] and
                git_read(worktree, 'status', '--porcelain') == '')
    if state.get('status') != {'plan': 'PLAN_READY', 'design': 'DESIGN_READY'}.get(kind):
        return False
    paths = ['02-design/plan.json'] if kind == 'plan' else ['02-design/adr.md'] + (
        [] if state.get('risk_tier', d['authorized']['tier']) == 'S' else ['02-design/threat-model.md'])
    for rel in paths:
        path = pinned_root(repo, store.ticket, d) / rel
        if not path.is_file() or digest(path.read_bytes()) != state.get('artifacts', {}).get(rel, {}).get('sha256'):
            return False
    return True


def exit_evidence(d, events):
    launch = intent_for(d, 'controller')
    current = d['exit_evidence']
    ended = [e for e in events if e['type'] == 'run_ended']
    if current['source'] == 'parent_returncode' and launch and current.get('intent_id') == launch['intent_id']:
        if ended and ended[-1]['exit_code'] != current['code']:
            return dict(source='none', code=None, intent_id=None)
        return current
    if ended and launch:
        return dict(source='run_ended', code=ended[-1]['exit_code'], intent_id=launch['intent_id'])
    return dict(source='none', code=None, intent_id=None)


def observed_status(store, d, proc=None, env=None, clock=None, *, events_snapshot=None):
    if clock:
        store.clock = clock
    env = dict(env) if env is not None else build_project_env(store.root.parent.parent, os.environ)
    # Cancellation owns the lifecycle even after TERM makes the controller
    # disappear. Missing exit evidence must not mask a resumable cancellation.
    if d.get('cancel') and (d['cancel']['state'] != 'done' or d['status'] != 'completed'):
        return status_fields(d, view(d, proc), store.clock.time())
    if d['status'] in TERMINAL - {'completed'}:
        # A recorded failure is bound to its original cause, not elapsed wall
        # time on a later read. Completed still verifies its current-commit proof.
        return status_fields(d, public(d), store.clock.time())
    if identity_state(d, 'controller', proc) != GONE or not intent_for(d, 'controller'):
        out = copy.deepcopy(d)
        if d['deadlines'].get('hard_deadline') is not None and store.clock.time() > d['deadlines']['hard_deadline'] and d['status'] not in TERMINAL:
            out.update(status='failed', status_reason='hard_deadline', classification=classify(dict(hard_deadline=True, env=env)))
            return status_fields(out, public(out), store.clock.time())
        return status_fields(out, view(out, proc), store.clock.time())
    events, malformed = events_snapshot if events_snapshot is not None else controller_events(store, d)
    out = copy.deepcopy(d)
    out['exit_evidence'] = exit_evidence(d, events)
    evidence = evidence_for(store, out, env, events, malformed)
    out['classification'] = classify(evidence)
    if out['exit_evidence']['code'] is None:
        out.update(status='reconcile', status_reason='exit_evidence_unavailable')
    elif out['exit_evidence']['code'] == 0:
        if completion(store, out, evidence['state']):
            out.update(status='completed', status_reason='completed', completion_kind=out['authorized']['entrypoint'], classification=None)
        else:
            out.update(status='reconcile', status_reason='completion_mismatch')
    elif out['status'] not in {'cancelled', 'operator_wait'}:
        out.update(status='operator_wait', status_reason=out['classification']['reason_code'])
    return status_fields(out, public(out), store.clock.time())


def status_fields(d, result, now):
    hb = d.get('heartbeat') or {}
    recovery = d.get('recovery') or {}
    result.update(state=result['status'], heartbeat_age=max(0, now - hb['at']) if 'at' in hb else None,
        progress={k: hb.get(k) for k in ('progress_at', 'progress_source')}, phase=hb.get('phase'),
        activity=hb.get('activity'), deadlines=d.get('deadlines', {}),
        recovery=dict(used=recovery.get('used', 0), remaining=max(0, recovery.get('allowance_total', 0) - recovery.get('used', 0)),
                      next_retry_at=recovery.get('next_retry_at')),
        preflight=d.get('preflight'), classification=d.get('classification'), counters=d.get('counters', {}),
        completion_kind=d.get('completion_kind'), diagnostics=hb.get('diagnostics', []))
    classification = d.get('classification')
    if result['status'] == 'cancelling':
        result['next_action'] = 'repeat_cancel'
    elif classification:
        result['next_action'] = RECOVERY_MATRIX[classification['class']]['action']
    elif result['status'] == 'operator_wait':
        result['next_action'] = RECOVERY_MATRIX['authentication']['action'] if result['status_reason'].startswith('auth_') else 'Inspect preflight checks before explicitly starting again.'
    result['deadline_remaining'] = {k: max(0, v - now) for k, v in d.get('deadlines', {}).items()
                                    if k in {'hard_deadline', 'preflight_deadline'} and type(v) in (int, float)}
    return result


def human_status(result):
    return (f"{result['status']}: {result['status_reason']}\n" +
            f"heartbeat_age={result.get('heartbeat_age')} phase={result.get('phase')} deadlines={json.dumps(result.get('deadlines', {}), sort_keys=True)} " +
            f"recovery={json.dumps(result.get('recovery', {}), sort_keys=True)}\n" +
            f"checks={json.dumps(result.get('preflight'), sort_keys=True)}\n" +
            f"classification={json.dumps(result.get('classification'), sort_keys=True)}\n" +
            f"next_action: {result['next_action']}")


class DockerObservation:
    def list(self, run_id, project, strict=False):
        try:
            p = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=ssdlc.run_id=' + run_id,
                                '--filter', 'label=ssdlc.project=' + project], timeout=10, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
            ids = [x for x in p.stdout.decode().split() if re.fullmatch(r'[a-f0-9]{12,64}', x)]
            if not ids:
                return []
            p = subprocess.run(['docker', 'inspect', *ids], timeout=10, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
            return [dict(id=c['Id'], name=c['Name'], labels=c['Config'].get('Labels') or {}, state=c['State']['Status'])
                    for c in json.loads(p.stdout)]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            if strict:
                raise OSError('docker_unavailable') from None
            return []

    def stop(self, container, grace):
        subprocess.run(['docker', 'stop', '-t', str(int(grace)), container['id']],
                       timeout=max(10, grace + 10), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


class DockerCancellation(DockerObservation):
    def list(self, run_id, project):
        return super().list(run_id, project, strict=True)


def monitor_tick(store, env, proc=None, docker=None, generation=None):
    """Observe progress; only a hard deadline or existing cancel intent runs cleanup."""
    d = store.read()
    proc = proc or Proc()
    now = store.clock.time()
    if generation is not None and generation != d['worker_generation']:
        raise FencedOut()
    if d.get('cancel'):
        if d['cancel']['state'] in {'requested', 'signalling'}:
            cancel(store.root.parent.parent, store.ticket, run_id=store.run_id, grace=5,
                   clock=store.clock, proc=proc, docker=docker)
            d = store.read()
        return status_fields(d, view(d, proc), store.clock.time())
    if d['status'] in TERMINAL:
        # Post-mortem observation cannot turn an earlier failure into a new
        # deadline failure. Resume only cleanup already authorized by a persisted
        # hard-deadline outcome (including a crash before its cancel intent).
        if d['status'] == 'failed' and d['status_reason'] == 'hard_deadline':
            cancel(store.root.parent.parent, store.ticket, run_id=store.run_id, grace=5,
                   clock=store.clock, proc=proc, docker=docker)
            d = store.read()
        return status_fields(d, public(d), store.clock.time())
    repo = store.root.parent.parent
    events, malformed = controller_events(store, d)
    tokens, descendants, containers = {}, [], []
    for role in ('worker', 'controller'):
        if identity_state(d, role, proc) != OWNED:
            continue
        leader = d['processes'][role]
        for pid in proc.pids():
            try:
                ident, state, _ = proc.stat(pid)
                if (state != 'Z' and ident.sid == leader['sid'] and ident.boot_id == leader['boot_id'] and
                        ident.uid == leader['uid'] and ident.start_ticks >= leader['start_ticks']):
                    descendants.append(dict(pid=pid, start_ticks=ident.start_ticks, role=role))
            except (OSError, ValueError):
                continue
    tokens['descendants'] = digest(json.dumps(descendants, sort_keys=True))
    if env.get('SSDLC_EXECUTION_MODE', 'docker') == 'docker':
        for c in (docker or DockerObservation()).list(d['run_id'], d['project']['key']):
            if (c.get('labels', {}).get('ssdlc.run_id') == d['run_id'] and
                    c.get('labels', {}).get('ssdlc.project') == d['project']['key'] and
                    isinstance(c.get('id'), str) and re.fullmatch(r'[a-f0-9]{12,64}', c['id'])):
                # Names and other provider-controlled text are not persisted.
                containers.append(dict(id=c['id'], name='owned_container',
                    labels={'ssdlc.run_id': d['run_id'], 'ssdlc.project': d['project']['key']},
                    state=c.get('state') if c.get('state') in {'running', 'exited', 'paused', 'created', 'dead', 'restarting'} else 'unknown'))
    tokens['containers'] = digest(json.dumps(containers, sort_keys=True))
    state_path = pinned_root(repo, store.ticket, d) / 'state.json'
    state = read_document(state_path)
    for name, path in [('state', state_path), ('journal', pinned_root(repo, store.ticket, d) / '03-dev/tasks/task-journal.jsonl')]:
        try:
            st = path.stat()
            if name != 'state' or state:
                tokens[name] = [st.st_size, st.st_mtime_ns]
        except FileNotFoundError:
            pass
    tokens['events'] = events[-1]['seq'] if events else 0
    old = d.get('heartbeat') or {}
    changed = tokens != old.get('tokens')
    progress_at = now if changed else old.get('progress_at', now)
    phase = state.get('current_phase') if state.get('current_phase') in PHASES else None
    activity = None
    for e in events:
        if e['type'] == 'dispatch_started':
            activity = {k: e[k] for k in ('engine', 'task_id', 'attempt') if k in e}
    diagnostics = []
    if malformed:
        diagnostics.append('controller_events_malformed')
    if state_path.is_file() and not state:
        diagnostics.append('state_malformed')
    if now - progress_at > d['deadlines'].get('quiet_threshold_sec', 300):
        diagnostics.append('suspect_silence')
    hb = dict(at=float(now), progress_at=float(progress_at), progress_source='structured' if changed else old.get('progress_source'),
              phase=phase, activity=activity, diagnostics=diagnostics, tokens=tokens)
    expired = d['deadlines'].get('hard_deadline') is not None and now > d['deadlines']['hard_deadline']
    # Decision and persisted exit evidence must use one event snapshot. A late
    # run_ended event is considered on the next tick, never half of this one.
    observed = observed_status(store, d, proc, env, events_snapshot=(events, malformed))
    def save(v):
        # CLI observers have no generation fence. Do not overwrite a newer
        # cancellation, terminal result or replacement worker with this snapshot.
        if v.get('cancel') or v['status'] in TERMINAL or v['worker_generation'] != d['worker_generation']:
            raise FencedOut()
        v['heartbeat'] = hb
        v['processes']['descendants'] = descendants
        v['containers'] = containers
        v['counters']['heartbeats'] += 1
        if expired:
            v.update(status='failed', status_reason='hard_deadline')
            v['classification'] = classify(dict(hard_deadline=True, env=env))
        elif identity_state(d, 'controller', proc) == GONE and intent_for(d, 'controller'):
            v.update(status=observed['status'], status_reason=observed['status_reason'], completion_kind=observed['completion_kind'])
            v['classification'] = observed['classification']
            v['exit_evidence'] = exit_evidence(d, events)
        elif (v['status'] == 'reconcile' and v['status_reason'] == 'reconcile' and
              v.get('classification') is None and
              all(identity_state(v, role, proc) == OWNED for role in ('worker', 'controller'))):
            # Fresh proof of the same two process births resolves a transient
            # observation gap. No dispatch, retry, signal or approval follows.
            v.update(status='running', status_reason='observing')
        elif observed['status'] == 'reconcile' and v['status'] not in TERMINAL and v['status'] != 'reconcile':
            v.update(status='reconcile', status_reason='reconcile')
        if v.get('classification'):
            cls = v['classification']['class']
            v['counters']['classifications'][cls] = v['counters']['classifications'].get(cls, 0) + 1
    if observed['status'] == 'completed':
        store.fault.hit('complete.observed_before_state')
    store.fault.hit('classify.before_persist')
    try:
        result = store.update(save, (generation, 0) if generation else None, event='heartbeat')
    except FencedOut:
        if generation is not None:
            raise
        current = store.read()
        return status_fields(current, view(current, proc), store.clock.time())
    if expired:
        # A hard deadline authorizes only the existing, ownership-checked cleanup.
        # Persist failure first so cancellation cannot replace the limits outcome.
        cancel(repo, store.ticket, run_id=store.run_id, grace=5, clock=store.clock, proc=proc, docker=docker)
        result = store.read()
        return status_fields(result, view(result, proc), store.clock.time())
    return status_fields(result, public(result), now)


def schedule_recovery(store, classification, generation, base=1, cap=60):
    d = store.read()
    if (RECOVERY_MATRIX[classification['class']]['mode'] != 'auto' or
            any(x['role'] == 'controller' for x in d['launches'])):
        return False
    def save(v):
        r = v['recovery']
        if r['used'] >= r['allowance_total']:
            v.update(status='operator_wait', status_reason='recovery_exhausted')
            r['next_retry_at'] = None
            if v['handoff']['outcome'] is None:
                v['handoff'] = dict(outcome='operator_wait', at=float(store.clock.time()))
        else:
            delay = min(base * 2 ** r['used'], cap)
            r['used'] += 1
            r['backoff_sec'] = delay
            r['next_retry_at'] = float(store.clock.time() + delay)
            r['history'].append(dict(used=r['used'], next_retry_at=r['next_retry_at'], code='retry_scheduled'))
            v['counters']['retries'] += 1
            v.update(status='starting', status_reason='retry_scheduled')
        v['classification'] = classification
    d = store.update(save, (generation, 0), event='recovery')
    store.fault.hit('recovery.after_counter_before_sleep')
    return d['status_reason'] == 'retry_scheduled'


def preflight_loop(store, env, generation, check=None):
    check = check or preflight
    while True:
        d = store.read()
        delay = max(0, (d['recovery'].get('next_retry_at') or 0) - store.clock.time())
        while delay > 0:
            store.clock.sleep(min(delay, 1))
            d = store.read()
            if d['worker_generation'] != generation or d.get('cancel') or d['launch_revoked']:
                raise FencedOut()
            delay = max(0, (d['recovery'].get('next_retry_at') or 0) - store.clock.time())
        if d['worker_generation'] != generation or d.get('cancel') or d['launch_revoked']:
            raise FencedOut()
        if d['deadlines'].get('hard_deadline') is not None and store.clock.time() > d['deadlines']['hard_deadline']:
            def expired(v):
                v.update(status='failed', status_reason='hard_deadline', classification=classify(dict(hard_deadline=True, env=env)))
                if v['handoff']['outcome'] is None:
                    v['handoff'] = dict(outcome='failed', at=float(store.clock.time()))
            store.update(expired, (generation, 0), event='classified')
            return False
        result = check(store.root.parent.parent, store.ticket, env, d['authorized']['entrypoint'], d['authorized']['tier'])
        def save(v):
            v['preflight'] = dict(at=float(store.clock.time()), checks=result['checks'])
        d = store.update(save, (generation, 0))
        if result['ok']:
            def ready(v):
                v['recovery']['next_retry_at'] = None
                if v.get('classification') and v['classification']['row'] == '3b':
                    v['classification'] = None
            store.update(ready, (generation, 0))
            return True
        classification = classify(dict(preflight=result, controller_launched=False, env=env))
        if classification['class'] == 'transient_environment_provider':
            if schedule_recovery(store, classification, generation):
                continue
            return False
        def wait(v):
            v['classification'] = classification
        store.update(wait, (generation, 0))
        store.handoff('operator_wait', result['code'], generation)
        return False


def restart_worker(store, goal=None, test_cmd=None, env=None, proc=None):
    proc = proc or Proc()
    repo = store.root.parent.parent
    env = dict(env) if env is not None else build_project_env(repo, os.environ)
    with lock(store.root, str(Path(store.ticket) / 'ownership.lock'), False):
        with store.locked():
            d = store.read()
            if d.get('cancel') or d['launch_revoked'] or d['status'] in {'completed', 'cancelled', 'failed'}:
                return public(d, 'blocked', 'reconcile')
            worker_state, controller_state = identity_state(d, 'worker', proc), identity_state(d, 'controller', proc)
            if worker_state == OWNED:
                return public(d, 'already_running', 'already_running')
            if worker_state == UNKNOWN or controller_state in {UNKNOWN, NOT_OWNED}:
                return public(d, 'blocked', 'reconcile')
            # Unpublished candidates are ambiguous; never duplicate them.
            for pid in proc.pids():
                try:
                    ident, state, _ = proc.stat(pid)
                    if ident.uid == os.getuid() and state != 'Z':
                        candidate = proc.environ(pid)
                        if candidate.get(MARK + 'RUN_ID') == d['run_id'] and candidate.get(MARK + 'ROLE') == 'worker':
                            return public(d, 'blocked', 'reconcile')
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except OSError:
                    return public(d, 'blocked', 'reconcile')
            precontroller = not any(x['role'] == 'controller' and x['state'] != 'abandoned' for x in d['launches'])
            if not precontroller and controller_state != OWNED:
                return public(d, 'reconcile', 'exit_evidence_unavailable')
            payload = json.dumps(dict(monitor_only=True, env=env)).encode()
            if not precontroller and config_digest(env) != d['fingerprints']['config_sha256']:
                return public(d, 'blocked', 'fingerprint_mismatch')
            if precontroller:
                if goal is None and d['authorized']['entrypoint'] == 'run':
                    return public(d, 'blocked', 'inputs_required')
                a = validate_args(dict(repo=str(repo), ticket=store.ticket, goal=goal, test_cmd=test_cmd,
                                       tier=d['authorized']['tier'], from_phase=d['authorized']['from_phase'],
                                       entrypoint=d['authorized']['entrypoint'], review_repairs=d['authorized'].get('review_repairs'),
                                       rounds=d['authorized'].get('rounds')))
                if (goal_digest(a) != d['authorized']['goal_sha256'] or
                    (digest(test_cmd) if test_cmd is not None else None) != d['authorized']['test_cmd_sha256'] or
                    config_digest(env) != d['fingerprints']['config_sha256'] or
                    digest(Path(build_argv('orchestrator.' + a['entrypoint'], a)[1]).read_bytes()) != d['fingerprints']['controller']['sha256'] or
                    controller_snapshot_digest() != d['fingerprints']['controller'].get('snapshot_sha256')):
                    return public(d, 'blocked', 'fingerprint_mismatch')
                payload = json.dumps(dict(args=a, env=env)).encode()
            def bump(v):
                v['worker_generation'] += 1
                v['processes']['worker'] = None
                for x in v['launches']:
                    if x['role'] == 'worker':
                        x['state'] = 'abandoned'
                v.update(status='starting' if precontroller else 'running', status_reason='bootstrap' if precontroller else 'observing')
                if precontroller:
                    # This generation owns a new preflight-to-controller handoff.
                    # Preserve recovery allowance/history, but not the old wait.
                    v['handoff'] = dict(outcome=None, at=None)
                    v['classification'] = None
            d = store.update(bump, event='adopted', locked=True)
            store.fault.hit('adopt.after_generation_bump_before_spawn')
            _, pipe, _ = spawn(store, 'worker', d['worker_generation'], payload is not None)
        if pipe is not None:
            try:
                transfer(pipe, payload, d['deadlines']['payload_io_timeout_sec'], store.clock)
            finally:
                os.close(pipe)
    return status_fields(store.read(), public(store.read()), store.clock.time())


def monitor(repo, ticket, run_id=None, timeout=30, interval=1, restart=False, goal=None, goal_file=None,
            test_cmd=None, clock=None, proc=None, docker=None, env=None):
    if not (0 <= timeout <= 86400 and 0 < interval <= 3600):
        raise ValueError('invalid_interval')
    store = RunStore(repo, ticket, run_id, clock)
    d = store.read() if run_id else store.current()
    if goal_file is not None:
        goal = Path(goal_file).read_text()
    env = dict(env) if env is not None else build_project_env(repo, os.environ)
    if restart:
        return restart_worker(store, goal, test_cmd, env, proc)
    deadline = store.clock.time() + timeout
    while True:
        result = monitor_tick(store, env, proc, docker)
        if result['status'] in TERMINAL or store.clock.time() >= deadline:
            return result
        store.clock.sleep(min(interval, deadline - store.clock.time()))

def cli(args, parser):
    try:
        command = args.subcommand
        if command == 'start' or command == 'run':
            result = start(args)
        elif command == 'resume':
            result = resume(args)
        elif command == 'status':
            if getattr(args, 'recovery_matrix', False):
                print(json.dumps(RECOVERY_MATRIX, sort_keys=True) if args.json else '\n'.join(
                    f"{k}: {v['mode']}; {v['action']}" for k, v in RECOVERY_MATRIX.items()))
                return 0
            if not args.ticket:
                parser.error('--ticket is required')
            result = status(args.repo, args.ticket, args.run_id)
        elif command == 'monitor':
            result = monitor(args.repo, args.ticket, args.run_id, args.timeout, args.interval,
                             args.restart_worker, args.goal, args.goal_file, args.test_cmd)
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
    print(json.dumps(result, sort_keys=True) if args.json else human_status(result))
    return exit_code(result)


def _main():
    p = argparse.ArgumentParser()
    if len(sys.argv) > 1 and sys.argv[1] == '_routes':
        r = argparse.ArgumentParser()
        r.add_argument('command')
        r.add_argument('--repo', required=True)
        r.add_argument('--ticket', default='')
        r.add_argument('--entrypoint', choices=tuple(ROLE_KEYS_BY_ENTRYPOINT), required=True)
        r.add_argument('--tier', choices=['auto', 'S', 'M', 'L'], required=True)
        a = r.parse_args()
        print(json.dumps(route_child(a.repo, a.entrypoint, a.tier, a.ticket), sort_keys=True))
        return 0
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
