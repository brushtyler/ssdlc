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


def _ineligible(code):
    COUNTERS['bootstrap_ineligible'] += 1
    logging.getLogger(__name__).warning('bootstrap stage=eligibility decision=ineligible reason=%s', code)
    return BootstrapError(code)


def load_profile(repo):
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
        raise _ineligible(exc.code) from None
    except (OSError, ValueError, PlanError):
        raise _ineligible('profile_invalid') from None


def policy_sha256(profile_id):
    if profile_id not in PROFILES:
        raise BootstrapError('not_registered')
    return digest(PROFILES[profile_id])


def profile_checks(profile_id):
    policy_sha256(profile_id)
    return [dict(check_id='CHECK-TESTS', argv=copy.deepcopy(PROFILES[profile_id]['argv']),
                 cwd='.', timeout_sec=300, kind='test', required=True, candidate_proof=True)]


def decide(repo, base_commit, profile):
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
        raise _ineligible(code) from None
    COUNTERS['bootstrap_eligible'] += 1
    logging.getLogger(__name__).info('bootstrap stage=eligibility decision=eligible reason=eligible')
    return result


def positive_test_count(stdout, stderr):
    text = stdout + '\n' + stderr
    counts = [int(n) for n in re.findall(r'(?m)^Ran (\d+) tests?\b', text)]
    counts += [int(n) for n in re.findall(r'\b(\d+) passed\b', text)]
    if counts:
        return max(counts)
    if re.search(r'\bno tests ran\b|\bcollected 0 items\b', text):
        return 0
    return None
