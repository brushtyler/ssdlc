"""Read-only, explicitly requested greenfield eligibility and registered policy."""
import copy
import fnmatch
import json
import logging
import re
from pathlib import Path

from ssdlc_tasks import PlanError, digest, git, validate_document

CODES = ('profile_invalid', 'not_registered', 'existing_tests', 'dirty_tree',
         'not_initial_commit', 'tree_mismatch', 'policy_conflict', 'interrupted', 'window_spent')
COUNTERS = {'bootstrap_eligible': 0, 'bootstrap_ineligible': 0}


class BootstrapError(PlanError):
    def __init__(self, code):
        self.code = code
        super().__init__('bootstrap_ineligible: ' + code)


PROFILES = {
    'python-unittest': {'argv': ['python3', '-m', 'unittest', 'discover', '-s', 'tests'], 'window_tasks': 1},
    'python-pytest': {'argv': ['python3', '-m', 'pytest', 'tests'], 'window_tasks': 1},
}


def _log_run_id(value):
    text = str(value)
    return text if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', text) else '-'


def _ineligible(code, run_id='-'):
    COUNTERS['bootstrap_ineligible'] += 1
    logging.getLogger(__name__).warning('bootstrap stage=eligibility decision=ineligible reason=%s run=%s txn=- preview=- tasks=-', code, _log_run_id(run_id))
    return BootstrapError(code)


def load_profile(repo, *, run_id='-'):
    path = Path(repo) / '.ssdlc/bootstrap-profile.json'
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 4096:
            raise BootstrapError('profile_invalid')
        profile = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(profile, dict) and profile.get('profile_id') not in PROFILES:
            raise BootstrapError('not_registered')
        validate_document('bootstrap-profile', profile)
        return profile
    except BootstrapError as exc:
        raise _ineligible(exc.code, run_id) from None
    except (OSError, ValueError, PlanError):
        raise _ineligible('profile_invalid', run_id) from None


def policy_sha256(profile_id):
    if profile_id not in PROFILES:
        raise BootstrapError('not_registered')
    return digest(PROFILES[profile_id])


def profile_checks(profile_id):
    policy_sha256(profile_id)
    return [dict(check_id='CHECK-TESTS', argv=copy.deepcopy(PROFILES[profile_id]['argv']),
                 cwd='.', timeout_sec=300, kind='test', required=True, candidate_proof=True)]


def decide(repo, base_commit, profile, *, run_id='-'):
    repo = Path(repo)
    try:
        validate_document('bootstrap-profile', profile)
        policy_hash = policy_sha256(profile['profile_id'])
        if git(repo, 'rev-list', '--count', 'HEAD') != '1' or git(repo, 'rev-parse', 'HEAD') != base_commit:
            raise BootstrapError('not_initial_commit')
        if git(repo, 'status', '--porcelain') or git(repo, 'ls-files', '-o', '--exclude-standard'):
            raise BootstrapError('dirty_tree')
        tree = git(repo, 'rev-parse', 'HEAD^{tree}')
        if tree != git(repo, 'rev-parse', base_commit + '^{tree}'):
            raise BootstrapError('tree_mismatch')
        if (repo / '.ssdlc/quality-contract.json').exists():
            raise BootstrapError('policy_conflict')
        from orchestrator import _discover_checks
        paths = git(repo, 'ls-files', '-z').split('\x00')
        patterns = ('test_*.py', '*_test.*', '*.test.*', '*.spec.*')
        if _discover_checks(repo) or any(fnmatch.fnmatch(Path(p).name, pattern) for p in paths for pattern in patterns):
            raise BootstrapError('existing_tests')
        result = dict(profile_id=profile['profile_id'], profile_sha256=digest(profile),
                      policy_sha256=policy_hash, initial_commit=base_commit, initial_tree=tree,
                      window_tasks=1, baseline_status='absent')
    except PlanError as exc:
        code = exc.code if isinstance(exc, BootstrapError) else 'profile_invalid'
        raise _ineligible(code, run_id) from None
    COUNTERS['bootstrap_eligible'] += 1
    logging.getLogger(__name__).info('bootstrap stage=eligibility decision=eligible reason=eligible run=%s txn=- preview=- tasks=-', _log_run_id(run_id))
    return result


def positive_test_count(stdout, stderr):
    """Use the final runner summary, excluding tests reported as skipped."""
    text = stdout + '\n' + stderr
    patterns = (
        ('unittest', r'(?m)^Ran (\d{1,12}) tests?\b'),
        ('pytest', r'(?m)^\s*=*\s*(\d{1,12}) passed\b'),
        ('jest', r'(?m)^\s*Tests:?\s+(\d{1,12})\b[^\r\n]*'),
        ('tap', r'(?m)^# tests (\d{1,12})\b'),
        ('mocha', r'(?m)^\s*(\d{1,12}) passing\b'),
        ('pytest', r'(?m)^collected (0) items?\b'),
    )
    counts = [(match.start(), int(match.group(1)), kind, match.end()) for kind, pattern in patterns
              for match in re.finditer(pattern, text)]
    empty = [match.start() for match in re.finditer(r'(?m)^=*\s*no tests ran\b', text)]
    if empty and (not counts or max(empty) > max(row[0] for row in counts)):
        return 0
    if not counts:
        return None
    position, total, kind, end = max(counts, key=lambda item: item[0])
    tail = text[end:]
    if kind == 'unittest':
        skipped = re.search(r'(?m)^(?:OK|FAILED)\s*\([^\r\n)]*\bskipped=(\d{1,12})\b', tail)
        return max(0, total - int(skipped.group(1))) if skipped else total
    if kind == 'jest':
        summary = text[position:end]
        passed = re.search(r'\b(\d{1,12}) passed\b', summary)
        if passed:
            return int(passed.group(1))
        return 0 if re.search(r'\b(?:skipped|pending|todo|failed)\b', summary) else None
    if kind == 'tap':
        passed = re.search(r'(?m)^# pass (\d{1,12})\b', tail)
        if passed:
            return min(total, int(passed.group(1)))
        skipped = sum(int(match.group(1)) for match in re.finditer(
            r'(?m)^# (?:skip(?:ped)?|cancelled|todo) (\d{1,12})\b', tail))
        return max(0, total - skipped)
    return total
