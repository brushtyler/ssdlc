"""Conservative host hints and feedback policy classes; never edit a test.

Test output is untrusted. A localized failure only suggests an impact-analysis
omission; it cannot prove that changing the failed test is the correct repair.
"""
from pathlib import Path
import re

from ssdlc_tasks import TaskError, digest, file_hash, git
from ssdlc_transitions import _regular


def actor(value):
    if (not isinstance(value, dict) or set(value) != {'identity', 'engine', 'model', 'context_id'}
            or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}', v)
                   for v in value.values())):
        raise TaskError('Host-stamped feedback actor metadata required')
    return value


def independence(*actors):
    """Different strings are not proof of different reasoning/model families."""
    values = [actor(a) for a in actors]
    distinct = (len({a['identity'] for a in values}) == len(values)
                and len({a['context_id'] for a in values}) == len(values))
    models = [(a['engine'], a['model']) for a in values]
    reduced = len(set(models)) != len(models) or any('unknown' in pair for pair in models)
    return dict(distinct_roles=distinct, reduced_independence=reduced)


def unittest_failures(text, known_files):
    """Recognize unittest failure blocks only when a unique test file exists.

    There is deliberately no inference from an arbitrary traceback path. The
    module reported by unittest must match a tracked test path uniquely. A
    truncated block, summary mismatch or unsupported format stays unresolved.
    """
    failures = []
    paths = [p for p in known_files if p.endswith('.py')]
    pattern = r'^(FAIL|ERROR): ([A-Za-z0-9_]+) \(([A-Za-z0-9_.]+)\)\s*$'
    for found in re.finditer(pattern, text, re.M):
        _, method, qualified = found.groups()
        if len(qualified) > 300:
            return None
        candidates = []
        for path in paths:
            module = path[:-3].replace('/', '.')
            aliases = {module, Path(path).stem}
            if any(qualified.startswith(alias + '.') for alias in aliases):
                candidates.append(path)
        if len(candidates) != 1 or not qualified.endswith('.' + method):
            return None
        failures.append(dict(test_id=qualified, path=candidates[0]))
    summary = re.findall(r'^FAILED \(([^\n]+)\)\s*$', text, re.M)
    if not failures or len(summary) != 1:
        return None
    counts = re.findall(r'(?:failures|errors)=(\d+)', summary[0])
    if not counts or sum(map(int, counts)) != len(failures):
        return None
    return sorted(failures, key=lambda item: (item['path'], item['test_id']))


def classify_failures(checks, read_log, known_files, target_files, protected):
    """Return an auditable hint from failed checks, retaining unresolved cases."""
    failures, unresolved = [], []
    failed = [c for c in checks if c['exit_code'] != 0 or c['empty_suite']]
    for check in failed:
        text = read_log(check)
        located = unittest_failures(text, known_files) if text is not None else None
        if located is None:
            unresolved.append(check['check_id'])
        else:
            failures.extend(dict(row, check_id=check['check_id']) for row in located)
    targets, protected = set(target_files), set(protected)
    if any(f['path'] in protected or f['path'].startswith('tests/replay/') for f in failures):
        classification = 'PROTECTED'
    elif unresolved or not failed:
        classification = 'UNRESOLVED'
    else:
        outside = [f['path'] not in targets for f in failures]
        classification = 'ALL_OUTSIDE' if all(outside) else 'MIXED' if any(outside) else 'INSIDE'
    return dict(classification=classification, failures=failures, unresolved_checks=sorted(unresolved),
                scope_candidate=classification == 'ALL_OUTSIDE', authorization=False)


def failure_hint(source, task):
    """Inspect only registered check logs, without executing candidate code."""
    row = source['journal']['tasks'][task['task_id']]
    last = row['failed_attempts'][-1]
    # SCOPE_REQUIRED may follow an unchanged captured CHECK_FAILURE candidate.
    checks = last.get('checks', [])
    if not checks:
        for failure in reversed(row['failed_attempts'][:-1]):
            if failure.get('tree') != last.get('tree'):
                break
            if failure.get('checks'):
                checks = failure['checks']
                break
    def read_log(check):
        ref = '03-dev/tasks/' + check['log_ref']
        if source['files'].get(ref) != check['sha256']:
            return None
        path = _regular(source['root'], ref)
        if file_hash(path) != check['sha256']:
            return None
        return path.read_text(errors='replace')
    from ssdlc_grader import protected_paths
    wt = source['root'] / 'worktree'
    known = git(wt, '--no-optional-locks', 'ls-files', '-z').split('\0')
    return classify_failures(checks, read_log, known, task['target_files'],
                             protected_paths(wt, source['plan']['base_commit']))


def path_classes(source, paths):
    """Baseline tests and completed ownership are explicit policy boundaries."""
    from ssdlc_grader import protected_paths
    wt, base = source['root'] / 'worktree', source['plan']['base_commit']
    baseline = set(git(wt, '--no-optional-locks', 'ls-tree', '-r', '--name-only', base).splitlines())
    protected = set(protected_paths(wt, base))
    result = {}
    for path in paths:
        # Reuse the same confined literal validation as request admission.
        from ssdlc_feedback import literal_path
        literal_path(path)
        current = wt
        for part in Path(path).parts:
            current = current / part
            if current.is_symlink():
                raise TaskError('Symlink in feedback target')
        owners = sorted(tid for tid, row in source['journal']['tasks'].items()
                        if row['status'] == 'COMPLETED' and path in row.get('changed_files', []))
        is_test = (any(p in {'tests', 'test'} for p in Path(path).parts)
                   or Path(path).name.startswith('test_') or Path(path).name.endswith('_test.py'))
        kind = ('PROTECTED' if path in protected or path.startswith('tests/replay/') else
                'COMPLETED_TEST' if owners and is_test else 'COMPLETED_SOURCE' if owners else
                'BASELINE_TEST' if path in baseline and is_test else
                'UNOWNED_SOURCE' if path in baseline else 'NEW_FILE')
        result[path] = dict(kind=kind, owners=owners)
    return result


def impact_attribution(report, report_sha256, paths):
    """Absence of a usable historical report is not evidence of unpredictability."""
    if report is None:
        return dict(status='UNKNOWN', report_sha256=None, detectable_files=[], unmatched_files=sorted(paths))
    if (not isinstance(report, dict) or digest(report) != report_sha256
            or not isinstance(report.get('references'), list)):
        raise TaskError('Historical impact report digest or structure changed')
    references = {row['path'] for row in report['references']}
    detected = sorted(set(paths) & references)
    return dict(status='DETECTABLE' if detected else 'NOT_IN_REPORT', report_sha256=report_sha256,
                detectable_files=detected, unmatched_files=sorted(set(paths) - references),
                complete_dependency_proof=False)


def native_preview(source, request):
    """Pre-consultation checks. This is neither approval nor a dispatcher."""
    import json
    from ssdlc_feedback import validate_request, source_binding
    from ssdlc_recovery import LimitsStore
    validate_request(source, request)
    tid = source['task_id']
    task = next(t for t in source['plan']['tasks'] if t['task_id'] == tid)
    profile = source['envelope']['policy']
    remaining = profile['development_attempts'] - source['journal']['tasks'][tid]['attempts']
    classes = path_classes(source, request['requested_files'])
    reasons = []
    if remaining <= 0:
        reasons.append('ATTEMPTS_EXHAUSTED')
    for item in classes.values():
        reason = {'PROTECTED': 'PROTECTED_PATH', 'COMPLETED_TEST': 'COMPLETED_COVERAGE_REVIEW_REQUIRED',
                  'COMPLETED_SOURCE': 'OWNER_REVALIDATION_REQUIRED',
                  'BASELINE_TEST': 'BASELINE_COVERAGE_REVIEW_REQUIRED'}.get(item['kind'])
        if reason and reason not in reasons:
            reasons.append(reason)
    budget = LimitsStore(source['root'])
    try:
        budget.check(source['counters'], profile)
    except TaskError as exc:
        reasons.append(getattr(exc, 'category', 'LIMIT_REACHED'))
    roles = {}
    for role in ('diagnosis', 'planner', 'reviewer'):
        try:
            budget.require_capacity('feedback-' + role, 'feedback')
        except TaskError:
            roles[role] = False
        else:
            roles[role] = True
    if not all(roles.values()):
        reasons.append('FEEDBACK_ALLOWANCE_UNAVAILABLE')
    report, sha = None, None
    ref = '02-design/fixture-impact.json'
    if ref in source['files']:
        path = _regular(source['root'], ref)
        if file_hash(path) != source['files'][ref]:
            raise TaskError('Historical impact evidence changed')
        report = json.loads(path.read_text())
        sha = digest(report)
    return dict(schema_version=1, source_binding=source_binding(source), task_id=tid,
                attempts_remaining=remaining, path_classes=classes, role_capacity=roles,
                stop_reasons=reasons, delegation_candidate=not reasons,
                failure_hint=failure_hint(source, task),
                planner_feedback=impact_attribution(report, sha, request['requested_files']),
                execution_authorized=False, model_dispatches=0)
