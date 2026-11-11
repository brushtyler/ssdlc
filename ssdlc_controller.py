"""Capture the executing controller before task development; unkeyed integrity only."""
import os
from pathlib import Path
import subprocess

from ssdlc_tasks import TaskError, digest, file_hash


def _snapshot_git(root, *args):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_CONFIG_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    proc = subprocess.run(['git', '--literal-pathspecs', '-c', 'core.excludesFile=',
                           '-c', 'core.fsmonitor=false', *args],
                          cwd=root, env=env, capture_output=True, text=True)
    if proc.returncode:
        raise TaskError('Controller snapshot git failed: ' + proc.stderr.strip())
    return proc.stdout.rstrip('\n')


def controller_snapshot(root: Path) -> dict:
    root = root.resolve()
    names = sorted(set(filter(None, _snapshot_git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0'))))
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
    return {'root': str(root), 'head': _snapshot_git(root, 'rev-parse', 'HEAD'),
            'status': _snapshot_git(root, 'status', '--porcelain', '--untracked-files=normal'),
            'files_sha256': digest(contents)}


def assert_controller(root: Path, expected: dict) -> None:
    if controller_snapshot(root) != expected:
        raise TaskError('Executing controller changed since its pre-development snapshot; explicit revision required')


def execution_contract(snapshot):
    name = '00-recon/controller-contract.json'
    return {'artifact_ref': name, 'snapshot': snapshot,
                'verification_owner': 'host orchestrator, outside target task commands',
                'guard_points': ['before development', 'before each developer invocation',
                                 'after each developer invocation', 'before and after each verification check'],
                'limitations': 'Unkeyed integrity checking at guard boundaries; not authentication or continuous containment. '
                               'Ignored controller files are excluded. No controller environment variables are required.'}
