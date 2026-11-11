"""Private candidate receipts and disposable source copies (no execution authority).

The host owns this store. Hashes bind evidence, not an adversary able to replace
both receipts and their journal references. This is deliberately not a migration
API: historical RUNNING tasks without a receipt remain uncertain.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


class CandidateError(RuntimeError):
    pass


def validate_receipt(receipt):
    from jsonschema import Draft202012Validator, ValidationError
    schema = json.loads((Path(__file__).parent / 'schemas/candidate-capture.schema.json').read_text())
    try:
        Draft202012Validator(schema).validate(receipt)
    except ValidationError as exc:
        raise CandidateError('Malformed candidate receipt') from exc


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def git_environment():
    # Checkout must not activate host-configured filters or repository hooks.
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               GIT_ATTR_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    return env


def _git(root, *args):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', *args], cwd=root,
                            capture_output=True, text=True, env=git_environment())
    if result.returncode:
        raise CandidateError('Candidate Git operation failed: ' + args[0])
    return result.stdout.rstrip('\n')


def _path(root, name):
    path = Path(name)
    if (not name or path.is_absolute() or any(p in ('..', '.git') for p in path.parts)
            or '\\' in name or any(ord(c) < 32 for c in name)):
        raise CandidateError('Unsafe candidate path')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise CandidateError('Candidate symlinks require explicit reconciliation')
    if not current.resolve().is_relative_to(root.resolve()):
        raise CandidateError('Candidate path escapes source')
    return current


def inventory(root):
    """Do not follow symlinks or open sockets/devices supplied by role code."""
    if root.is_symlink() or not root.is_dir():
        raise CandidateError('Unsafe source root')
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        if Path(directory) == root and '.git' in dirs:
            dirs.remove('.git')
        for name in list(dirs):
            if (Path(directory) / name).is_symlink():
                dirs.remove(name)
                files.append(name)
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[relative] = {'kind': 'symlink', 'target': os.readlink(path)}
            elif stat.S_ISREG(info.st_mode):
                result[relative] = {'kind': 'file', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                    'executable': bool(info.st_mode & 0o111)}
            else:
                result[relative] = {'kind': 'unsupported'}
    return result


def generated(name):
    # Narrow Python adapter rule, independent of candidate-controlled .gitignore.
    return bool(re.fullmatch(r'(?:.*/)?__pycache__/[^/]+\.cpython-\d+(?:\.opt-[12])?\.pyc', name)
                or re.fullmatch(r'(?:.*/)?\.pytest_cache/(?:README\.md|CACHEDIR\.TAG|\.gitignore|v/cache/(?:nodeids|lastfailed|stepwise))', name))


def git_metadata(root):
    directory = root / '.git'
    if not directory.is_dir() or directory.is_symlink():
        raise CandidateError('Disposable checkout metadata changed')
    # The index may refresh stat information. Its tree, not its bytes, is bound.
    metadata = inventory(directory)
    metadata.pop('index', None)
    # write-tree can add content-addressed objects; the base tree/HEAD are checked.
    metadata = {k: v for k, v in metadata.items() if not k.startswith('objects/')}
    return metadata


def git_identity(root):
    return {'head': _git(root, 'rev-parse', 'HEAD'), 'tree': _git(root, 'write-tree'),
            'metadata': git_metadata(root)}


def _write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def checkout(source, destination, head):
    """Independent objects/config; no linked metadata or credential directories."""
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '-q',
                             '--no-local', '--no-checkout', '--depth=1', '--template=',
                             str(source.resolve()), str(destination)], capture_output=True, env=git_environment())
    if result.returncode:
        raise CandidateError('Cannot create disposable checkout')
    _git(destination, 'checkout', '-q', '--detach', head)
    _git(destination, 'remote', 'remove', 'origin')
    return inventory(destination)


def overlay(source, destination, names):
    for name in names:
        original, target = _path(source, name), _path(destination, name)
        if not original.exists():
            if target.exists():
                target.unlink()
        elif original.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original.read_bytes())
            target.chmod(0o755 if original.stat().st_mode & 0o111 else 0o644)
        else:
            raise CandidateError('Unsupported candidate source entry')


class CandidateStore:
    def __init__(self, attempt_dir):
        self.attempt = attempt_dir
        self.root = attempt_dir / 'capture'

    def capture(self, workspace, baseline, initial, metadata, binding, result, targets, protected):
        if self.root.exists():
            raise CandidateError('Candidate capture already exists')
        observed = inventory(workspace)
        observed_metadata = git_metadata(workspace)
        entries, caches, issues = {}, {}, []
        if observed_metadata != metadata['metadata']:
            # Do not execute Git with a role-modified configuration.
            issues.append('protected_metadata')
        elif git_identity(workspace) != metadata:
            issues.append('protected_metadata')
        for name in sorted(set(baseline) | set(observed) | set(initial)):
            before, after = baseline.get(name), observed.get(name)
            if before == after == initial.get(name):
                continue
            if before is None and name not in targets and after and after['kind'] == 'file' and generated(name):
                caches[name] = after
                continue
            entries[name] = {'before': before, 'starting': initial.get(name), 'after': after}
            if any(part in protected for part in Path(name).parts):
                issues.append('protected_path')
            if name not in targets:
                issues.append('scope_required')
            if after and after['kind'] != 'file':
                issues.append('unsafe_entry')
        temporary = Path(tempfile.mkdtemp(prefix='.capture-', dir=self.attempt))
        temporary.chmod(0o700)
        (temporary / 'blobs').mkdir()
        for name, entry in entries.items():
            after = entry['after']
            if after and after['kind'] == 'file':
                data = _path(workspace, name).read_bytes()
                if hashlib.sha256(data).hexdigest() != after['sha256']:
                    raise CandidateError('Source changed during capture')
                blob = temporary / 'blobs' / after['sha256']
                if not blob.exists():
                    _write(blob, data)
        output = result['output'].encode()
        _write(temporary / 'developer.log', output)
        receipt = {'schema_version': 1, 'binding': binding, 'producer': {k: v for k, v in result.items() if k != 'output'},
                   'output_sha256': hashlib.sha256(output).hexdigest(), 'entries': entries,
                   'payload_sha256': signature(entries),
                   'generated_artifacts': caches, 'issues': sorted(set(issues))}
        validate_receipt(receipt)
        if inventory(workspace) != observed or git_metadata(workspace) != observed_metadata:
            raise CandidateError('Source or metadata changed during capture')
        _write(temporary / 'receipt.json', (json.dumps(receipt, sort_keys=True) + '\n').encode())
        _sync(temporary / 'blobs')
        _sync(temporary)
        os.rename(temporary, self.root)
        _sync(self.attempt)
        return receipt

    def load(self, binding, expected_digest=None):
        if self.root.is_symlink() or not self.root.is_dir():
            raise CandidateError('Missing complete candidate capture; invocation outcome is uncertain')
        for name in ('receipt.json', 'developer.log', 'blobs'):
            if (self.root / name).is_symlink():
                raise CandidateError('Unsafe candidate evidence')
        receipt = json.loads((self.root / 'receipt.json').read_text())
        validate_receipt(receipt)
        if receipt.get('schema_version') != 1 or receipt.get('binding') != binding:
            raise CandidateError('Candidate binding changed')
        if signature(receipt['entries']) != receipt['payload_sha256']:
            raise CandidateError('Candidate source payload digest changed')
        if expected_digest is not None and signature(receipt) != expected_digest:
            raise CandidateError('Candidate receipt changed')
        output = (self.root / 'developer.log').read_bytes()
        if hashlib.sha256(output).hexdigest() != receipt['output_sha256']:
            raise CandidateError('Candidate output changed')
        for entry in receipt['entries'].values():
            after = entry['after']
            if after and after['kind'] == 'file':
                sha = after['sha256']
                if not re.fullmatch(r'[a-f0-9]{64}', sha):
                    raise CandidateError('Invalid candidate blob identity')
                blob = self.root / 'blobs' / sha
                if blob.is_symlink() or hashlib.sha256(blob.read_bytes()).hexdigest() != sha:
                    raise CandidateError('Candidate payload changed')
        return receipt, {'output': output.decode(), **receipt['producer']}

    def adopt(self, target, receipt, changed):
        if receipt['issues']:
            raise CandidateError('Candidate refused: ' + ', '.join(receipt['issues']))
        if not set(changed) <= receipt['entries'].keys():
            raise CandidateError('Authoritative source changed outside captured payload')
        observed = inventory(target)
        for name, entry in receipt['entries'].items():
            if observed.get(name) not in (entry['before'], entry['starting'], entry['after']):
                raise CandidateError('Authoritative source differs from captured payload')
        for name, entry in receipt['entries'].items():
            path = _path(target, name)
            after = entry['after']
            if after is None:
                if path.exists():
                    path.unlink()
                    _sync(path.parent)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                # A crash must not leave unknown temporary source files. The
                # store and authoritative worktree are on the same filesystem.
                fd, temporary = tempfile.mkstemp(prefix='.adopt-', dir=self.attempt)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write((self.root / 'blobs' / after['sha256']).read_bytes())
                    os.fchmod(stream.fileno(), 0o755 if after['executable'] else 0o644)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                _sync(path.parent)
