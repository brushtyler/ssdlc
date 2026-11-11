"""Host-owned replay baseline. Candidate tasks cannot revise their own grader."""
import hashlib
import json
from pathlib import Path
import subprocess

MANIFEST = 'schemas/recovery-grader-baseline.json'
REQUIRED = frozenset({
    'ssdlc_grader.py', MANIFEST, '.github/workflows/test.yml',
    'tests/replay/benchmark.py', 'tests/replay/fixtures/i2-recovery.json',
    'tests/replay/README.md', 'tests/replay/__init__.py',
    'tests/replay/test_candidate_capture.py', 'tests/replay/fixtures/issue22-cache.json',
    'tests/replay/test_timeout_recovery.py', 'tests/test_design_recovery.py',
    'tests/test_design_session.py', 'tests/test_task_bootstrap.py',
    'tests/test_recovery_limits.py', 'tests/test_recovery_benchmark.py',
})


def protected_paths(repo, base):
    """Identify self-development from immutable Git entries, not candidate bytes."""
    for name in ('orchestrator.py', 'ssdlc_tasks.py', 'tests/replay/benchmark.py'):
        check = subprocess.run(['git', 'cat-file', '-e', base + ':' + name], cwd=repo,
                               capture_output=True)
        if check.returncode: return frozenset()
    return REQUIRED | {'tests/replay/'}


def verify_baseline(root=None):
    root = Path(root or Path(__file__).parent)
    if (root / MANIFEST).is_symlink(): raise RuntimeError('Replay baseline manifest is a symlink')
    manifest = json.loads((root / MANIFEST).read_text())
    files = manifest['files']
    if (manifest.get('schema_version') != 1 or not manifest.get('baseline_id')
            or set(files) != REQUIRED - {MANIFEST}):
        raise RuntimeError('Replay baseline membership changed; explicit reviewed baseline revision required')
    for name, expected in files.items():
        path = root
        for part in Path(name).parts:
            path = path / part
            if path.is_symlink(): raise RuntimeError('Replay baseline contains a symlink')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Replay grader/fake changed without reviewed baseline revision: ' + name)
    return manifest['baseline_id']


def guard_baseline():
    from ssdlc_tasks import DevelopmentBlocked
    try:
        return verify_baseline()
    except (OSError, ValueError, KeyError, RuntimeError):
        raise DevelopmentBlocked('INTEGRITY', 'Replay grader baseline changed; reviewed revision required') from None
