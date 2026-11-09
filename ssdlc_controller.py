"""Capture the executing controller before task development; unkeyed integrity only."""
import os
from pathlib import Path

from ssdlc_tasks import TaskError, digest, file_hash, git


def controller_snapshot(root: Path) -> dict:
    root = root.resolve()
    names = sorted(set(filter(None, git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0'))))
    contents = {}
    for name in names:
        path = root / name
        if path.is_symlink():
            contents[name] = {'symlink': os.readlink(path)}
        elif path.is_file():
            contents[name] = {'sha256': file_hash(path)}
        elif not path.exists():
            contents[name] = {'missing': True}
        else:
            raise TaskError(f'Unsupported controller entry: {name}')
    return {'root': str(root), 'head': git(root, 'rev-parse', 'HEAD'),
            'status': git(root, 'status', '--porcelain', '--untracked-files=normal'),
            'files_sha256': digest(contents)}


def assert_controller(root: Path, expected: dict) -> None:
    if controller_snapshot(root) != expected:
        raise TaskError('Executing controller changed since its pre-development snapshot; explicit revision required')
