"""Deterministic lifecycle adapters. No subprocess or network side effects."""
from dataclasses import asdict
import signal

from ssdlc_supervisor import FaultInjector, InjectedCrash, Ident, MARK


class FakeClock:
    def __init__(self, now=1000.):
        self.now = float(now)
        self.sleeps = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class CrashAt(FaultInjector):
    def __init__(self, point):
        self.point = point
        self.hits = []

    def hit(self, name):
        self.hits.append(name)
        if name == self.point:
            self.point = None
            raise InjectedCrash(name)


class FakeProc:
    def __init__(self):
        self.entries = {}
        self.signals = []
        self.closed = []
        self.after_open = None
        self.opened = []

    def add(self, pid, nonce='a' * 32, role='worker', gen=1, intent='b' * 32,
            run_id='c' * 32, ticks=100, sid=None, pgid=None, ppid=1,
            state='S', uid=None, boot='boot'):
        import os
        ident = Ident(pid, boot, ticks, pgid or pid, sid or pid, os.getuid() if uid is None else uid)
        env = {MARK + k: str(v) for k, v in dict(NONCE=nonce, ROLE=role, GEN=gen,
                                                 INTENT=intent, RUN_ID=run_id).items()}
        self.entries[pid] = [ident, state, ppid, env]
        return ident

    def stat(self, pid):
        if pid not in self.entries:
            raise FileNotFoundError()
        x = self.entries[pid]
        if isinstance(x, BaseException):
            raise x
        return tuple(x[:3])

    def environ(self, pid):
        self.stat(pid)
        x = self.entries[pid][3]
        if isinstance(x, BaseException):
            raise x
        return x.copy()

    def pids(self):
        return list(self.entries)

    def born_after(self, ident, at):
        return ident.boot_id == 'boot'

    def pidfd_open(self, pid):
        ident = self.stat(pid)[0]
        fd = (pid, ident.start_ticks)
        self.opened.append(fd)
        if self.after_open:
            self.after_open(pid)
        return fd

    def send(self, fd, sig):
        pid, birth = fd
        if pid not in self.entries or self.stat(pid)[0].start_ticks != birth:
            raise ProcessLookupError()
        self.signals.append((pid, sig))
        if sig == signal.SIGKILL:
            del self.entries[pid]

    def close(self, fd):
        self.closed.append(fd)


class FakeDocker:
    def __init__(self):
        self.containers = []
        self.stopped = []

    def list(self, run_id, project):
        return [c for c in self.containers if c['labels'].get('ssdlc.run_id') == run_id
                and c['labels'].get('ssdlc.project') == project]

    def stop(self, container):
        self.stopped.append(container['id'])


class FakeProbe:
    """Factory for an explicitly registered NativeProbe; no real native calls."""
    @staticmethod
    def adapter(result=None, error=None):
        from ssdlc_supervisor import NativeProbe
        class Adapter(NativeProbe):
            def __init__(self):
                self.calls = []

            def check(self, engine, env, timeout):
                self.calls.append((engine, dict(env), timeout))
                if error:
                    raise error
                return result
        return Adapter()


class FakeRefresh:
    @staticmethod
    def adapter(result=None, error=None):
        from ssdlc_supervisor import RefreshAdapter
        class Adapter(RefreshAdapter):
            def __init__(self):
                self.calls = []

            def refresh(self, engine, env, runtime_dirs, timeout):
                self.calls.append((engine, dict(env), dict(runtime_dirs), timeout))
                if error:
                    raise error
                return result
        return Adapter()


def write_controller_events(path, run, events):
    """Emit the closed typed schema, bound to this controller's launch identity."""
    import json
    import os
    from ssdlc_supervisor import intent_for
    launch = intent_for(run, 'controller')
    records = []
    for seq, event in enumerate(events, 1):
        records.append(dict(seq=seq, ts=1000., run_id=run['run_id'], role='controller',
                            gen=launch['generation'], intent=launch['intent_id'], **event))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        for record in records:
            stream.write(json.dumps(record) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return records
