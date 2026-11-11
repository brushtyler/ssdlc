"""Live ownership receipts for borrowing an already-held native lock.

Receipts are process/thread-local objects, not JSON capabilities. They never
reacquire a lock, remove a lock file or turn file existence into ownership.
"""
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import threading


_owned = threading.local()


def _error(message):
    from ssdlc_tasks import TaskError
    return TaskError(message)


@dataclass(frozen=True)
class LockLease:
    path: Path
    fd: int
    pid: int
    thread: int
    device: int
    inode: int

    def _identity(self, path):
        expected = Path(path).absolute()
        if (self.path != expected or self.pid != os.getpid() or self.thread != threading.get_ident()
                or getattr(_owned, 'locks', {}).get(expected) is not self):
            raise _error('Borrowed lock has no current process/thread ownership')
        try:
            opened, linked = os.fstat(self.fd), os.stat(expected, follow_symlinks=False)
        except OSError:
            raise _error('Borrowed lock ownership disappeared') from None
        if (not stat.S_ISREG(linked.st_mode) or not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (self.device, self.inode)
                or (linked.st_dev, linked.st_ino) != (self.device, self.inode)):
            raise _error('Borrowed lock identity changed')
        return self

    def validate(self, path):
        self._identity(path)
        # A still-open descriptor is not evidence that flock was not unlocked
        # or downgraded. Linux exposes the actual FLOCK on this descriptor.
        try:
            info = Path('/proc/self/fdinfo', str(self.fd)).read_text()
        except OSError:
            raise _error('Kernel lock ownership cannot be observed') from None
        for line in info.splitlines():
            words = line.split()
            if (len(words) >= 9 and words[0] == 'lock:' and words[2:5] == ['FLOCK', 'ADVISORY', 'WRITE']
                    and words[5] == str(self.pid) and words[-2:] == ['0', 'EOF']):
                return self
        raise _error('Kernel exclusive lock ownership is absent')


@contextlib.contextmanager
def record_lock(path, fd):
    path = Path(path).absolute()
    locks = getattr(_owned, 'locks', None)
    if locks is None:
        _owned.locks = locks = {}
    if path in locks:
        raise _error('Native lock registration is not reentrant')
    metadata = os.fstat(fd)
    lease = LockLease(path, fd, os.getpid(), threading.get_ident(), metadata.st_dev, metadata.st_ino)
    locks[path] = lease
    try:
        # Ordinary ticket locks keep their existing POSIX behavior; borrowing
        # for an in-run transition additionally requires observable Linux proof.
        lease._identity(path)
        yield lease
    finally:
        if locks.get(path) is lease:
            del locks[path]


def current_lock(path):
    path = Path(path).absolute()
    lease = getattr(_owned, 'locks', {}).get(path)
    return lease.validate(path) if lease is not None else None


@contextlib.contextmanager
def transition_lock(path, held=()):
    """Borrow only a specific live lease; ordinary locks stay nonreentrant."""
    path = Path(path).absolute()
    matches = [lease for lease in held if isinstance(lease, LockLease) and lease.path == path]
    if len(matches) > 1:
        raise _error('Ambiguous native lock ownership')
    if matches:
        yield matches[0].validate(path)
    else:
        from ssdlc_tasks import ticket_lock
        with ticket_lock(path) as lease:
            yield lease
