"""Pure gate findings contract.

validate_report(report, TrustedContext) returns a frozen NormalizedReport;
assess_report returns an Assessment containing findings, correction_roots and
impact_assessment_required, replanning_required, diagnosis_required and
optional_observations (finding IDs). to_dict() returns fresh plain data.
Execution is never authorized. An empty report does not grant gate approval.

Limits: 200 findings; IDs 64 characters; text 4000; reference arrays 50;
paths 512; references 128. The iterative preflight caps container depth at 6,
lists at 200, dicts at 32 keys, strings at 4000, keys at 128 and nodes at
50,000. Oversized data is rejected, never truncated.

Closed diagnostic codes: malformed_context, malformed_input, limit_exceeded,
missing_field, unknown_field, invalid_type, invalid_value, missing_evidence,
duplicate_id, duplicate_reference, stale_commit, unknown_task,
unknown_obligation, unknown_evidence, unsafe_path, misclassified_optional,
inconsistent_mandatory, inconsistent_gap, hypothesis_classification.
Diagnostics contain static messages and bounded escaped excerpts.
Validation enforces internal consistency, not semantic honesty of supplied
claims or evidence integrity beyond catalogue membership. The schema-only id
pattern accepts a trailing newline under Python's dollar-anchor semantics;
the API rejects it using fullmatch. Callers must supply stable inputs.
"""
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, fields
import json
from pathlib import Path
import re
import unicodedata

from jsonschema import Draft202012Validator

SCHEMA_VERSION = '1.0'
_SCHEMA = json.loads((Path(__file__).resolve().parent / 'schemas' /
                      'gate-findings.schema.json').read_text())
Draft202012Validator.check_schema(_SCHEMA)
_VALIDATOR = Draft202012Validator(_SCHEMA)
list(_VALIDATOR.iter_errors({}))


@dataclass(frozen=True)
class TrustedContext:
    expected_commit: str
    known_task_ids: Collection[str]
    obligation_ids: Collection[str]
    evidence_refs: Collection[str]


class FindingsError(Exception):
    def __init__(self, diagnostics):
        self.diagnostics = tuple(dict(d) for d in diagnostics)
        super().__init__('gate findings validation failed')


class _Plain:
    def to_dict(self):
        def plain(value):
            if isinstance(value, _Plain):
                return value.to_dict()
            if type(value) is tuple:
                return [plain(v) for v in value]
            return value
        return {f.name: plain(getattr(self, f.name)) for f in fields(self)}


@dataclass(frozen=True)
class NormalizedFinding(_Plain):
    id: str
    severity: str
    classification: str
    evidence_kind: str
    scenario: str
    rationale: str
    task_attribution: tuple[str, ...]
    file_refs: tuple[str, ...]
    obligation_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    violates_existing_obligation: bool
    introduces_regression: bool
    necessary_for_requested_correctness: bool


@dataclass(frozen=True)
class NormalizedReport(_Plain):
    schema_version: str
    reviewed_commit: str
    source_gate: str
    findings: tuple[NormalizedFinding, ...]


@dataclass(frozen=True)
class CorrectionRoot(_Plain):
    task_id: str
    finding_ids: tuple[str, ...]


@dataclass(frozen=True)
class Assessment(_Plain):
    reviewed_commit: str
    source_gate: str
    findings: tuple[NormalizedFinding, ...]
    correction_roots: tuple[CorrectionRoot, ...]
    impact_assessment_required: tuple[str, ...]
    replanning_required: tuple[str, ...]
    diagnosis_required: tuple[str, ...]
    optional_observations: tuple[str, ...]
    execution_authorized: bool = field(default=False, init=False)


def _excerpt(s):
    s = s.encode('unicode_escape').decode('ascii')
    return s if len(s) <= 128 else s[:125] + '...'


def _pointer(parts):
    return ''.join('/' + _excerpt(str(p)).replace('~', '~0').replace('/', '~1')[:128]
                   for p in parts)


_MESSAGES = {code: message for code, message in (
    ('malformed_context', 'invalid trusted context'),
    ('malformed_input', 'invalid input structure'),
    ('limit_exceeded', 'documented limit exceeded'),
    ('missing_field', 'required field missing'), ('unknown_field', 'unknown field'),
    ('invalid_type', 'invalid field type'), ('invalid_value', 'invalid field value'),
    ('missing_evidence', 'evidence required'), ('duplicate_id', 'duplicate finding id'),
    ('duplicate_reference', 'duplicate reference'), ('stale_commit', 'commit mismatch'),
    ('unknown_task', 'unknown task reference'),
    ('unknown_obligation', 'unknown obligation reference'),
    ('unknown_evidence', 'unknown evidence reference'), ('unsafe_path', 'unsafe lexical path'),
    ('misclassified_optional', 'optional finding asserts required work'),
    ('inconsistent_mandatory', 'mandatory finding requires violation or regression'),
    ('inconsistent_gap', 'gap claims inconsistent'),
    ('hypothesis_classification', 'hypothesis requires optional or uncertain classification'))}


class _Diagnostics:
    def __init__(self, report=None):
        self.report = report
        self.items = {}

    def add(self, code, parts=(), value=None, indices=None):
        index = parts[1] if len(parts) > 1 and parts[0] == 'findings' and type(parts[1]) is int else -1
        d = dict(code=code, path=_pointer(parts), message=_MESSAGES[code])
        if index >= 0 and type(self.report) is dict:
            fs = self.report.get('findings')
            if type(fs) is list and index < len(fs) and type(fs[index]) is dict:
                ident = fs[index].get('id')
                if type(ident) is str:
                    d['finding_id'] = _excerpt(ident)
        if type(value) is str:
            d['value'] = _excerpt(value)
        if code in ('unknown_task', 'unknown_obligation', 'unknown_evidence',
                    'duplicate_reference', 'misclassified_optional', 'inconsistent_gap'):
            d['field'] = parts[2]
        if indices is not None:
            d['indices'] = list(indices)
        # Display paths are escaped and bounded. Distinct raw fields can share a
        # display prefix; keep their identities separate and their order stable.
        identity = tuple((type(part).__name__, str(part)) for part in parts)
        self.items[(index, d['path'], code, d['message'], identity)] = d

    def raise_if_any(self):
        if self.items:
            raise FindingsError(self.items[k] for k in sorted(self.items))


def _context(context):
    d = _Diagnostics()
    if type(context) is not TrustedContext:
        d.add('malformed_context')
        d.raise_if_any()
    if type(context.expected_commit) is not str or not re.fullmatch('[0-9a-f]{40}', context.expected_commit):
        d.add('malformed_context', ('@context', 'expected_commit'))
    snapshots = []
    for name in ('known_task_ids', 'obligation_ids', 'evidence_refs'):
        value = getattr(context, name)
        if type(value) not in (set, frozenset, tuple, list) or any(
                type(v) is not str or not 1 <= len(v) <= 128 for v in value):
            d.add('malformed_context', ('@context', name))
        else:
            snapshots.append(frozenset(value))
    d.raise_if_any()
    return snapshots


def _walk(report, d):
    stack = [(report, (), 0)]
    nodes = 0
    while stack:
        value, path, depth = stack.pop()
        nodes += 1
        if nodes > 50000:
            d.add('limit_exceeded', path)
            break
        kind = type(value)
        if kind in (dict, list):
            if depth > 6 or len(value) > (32 if kind is dict else 200):
                d.add('limit_exceeded', path)
                continue
            if kind is dict:
                for key, child in value.items():
                    if type(key) is not str:
                        d.add('malformed_input', path)
                    elif len(key) > 128:
                        d.add('limit_exceeded', path, key)
                    else:
                        stack.append((child, path + (key,), depth + 1))
            else:
                stack.extend((v, path + (i,), depth + 1) for i, v in enumerate(value))
        elif kind is str:
            if len(value) > 4000:
                d.add('limit_exceeded', path, value)
        elif kind not in (bool, int, type(None)):
            d.add('malformed_input', path)
    d.raise_if_any()


def _schema_errors(report, d):
    for error in _VALIDATOR.iter_errors(report):
        path = tuple(error.path)
        keyword = error.validator
        if keyword == 'required':
            for name in error.validator_value:
                if name not in error.instance:
                    d.add('missing_field', path + (name,))
        elif keyword == 'additionalProperties':
            for name in error.instance.keys() - error.schema['properties'].keys():
                d.add('unknown_field', path + (name,), name)
        else:
            code = {'type': 'invalid_type', 'maxItems': 'limit_exceeded',
                    'maxLength': 'limit_exceeded', 'minItems': 'missing_evidence'}.get(keyword, 'invalid_value')
            if path == ('reviewed_commit',):
                code = 'invalid_type' if keyword == 'type' else 'invalid_value'
            d.add(code, path, error.instance)
    candidates = [(('reviewed_commit',), report.get('reviewed_commit'), '[0-9a-f]{40}')]
    fs = report.get('findings')
    if type(fs) is list:
        candidates.extend((('findings', i, 'id'), f.get('id'), '[A-Za-z0-9][A-Za-z0-9._-]*')
                          for i, f in enumerate(fs) if type(f) is dict)
    for path, value, pattern in candidates:
        if type(value) is str and not re.fullmatch(pattern, value) and not any(
                k[1] == _pointer(path) for k in d.items):
            d.add('invalid_value', path, value)


def _unsafe_path_reason(p):
    if not p:
        return 'empty'
    if any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp') or
           (unicodedata.category(c) == 'Zs' and c != ' ') for c in p):
        return 'character'
    if p != p.strip():
        return 'whitespace'
    if any(c in p for c in '\\\u2215\u2044\u29f8\u2216\u29f5\uff0f\uff3c\ufe68\u2024\uff0e\ufe52'):
        return 'separator'
    if unicodedata.normalize('NFKC', p) != p:
        return 'normalization'
    if ':' in p or p.startswith('/') or p.split('/')[0].startswith('~'):
        return 'prefix'
    if any(s in ('', '.', '..') for s in p.split('/')):
        return 'segment'
    return None


_CLAIMS = ('violates_existing_obligation', 'introduces_regression', 'necessary_for_requested_correctness')
_MANDATORY = 'mandatory_violation_or_regression'


def _semantic_checks(report, context, catalogues, d):
    commit = report.get('reviewed_commit')
    if type(commit) is str and re.fullmatch('[0-9a-f]{40}', commit) and commit != context.expected_commit:
        d.add('stale_commit', ('reviewed_commit',), commit)
    fs = report.get('findings')
    if type(fs) is not list:
        return
    seen_ids = {}
    for i, f in enumerate(fs):
        if type(f) is not dict:
            continue
        base = ('findings', i)
        ident = f.get('id')
        if type(ident) is str:
            if ident in seen_ids:
                d.add('duplicate_id', base + ('id',), ident, (seen_ids[ident], i))
            else:
                seen_ids[ident] = i
        for name, known, code in zip(('task_attribution', 'obligation_refs', 'evidence_refs', 'file_refs'),
                                     (*catalogues, None),
                                     ('unknown_task', 'unknown_obligation', 'unknown_evidence', None)):
            refs = f.get(name)
            if type(refs) is not list:
                continue
            seen = set()
            for j, ref in enumerate(refs):
                if type(ref) is not str:
                    continue
                path = base + (name, j)
                if ref in seen:
                    d.add('duplicate_reference', path, ref)
                seen.add(ref)
                if known is not None and ref not in known:
                    d.add(code, path, ref)
                if name == 'file_refs' and _unsafe_path_reason(ref):
                    d.add('unsafe_path', path, ref)
        classification = f.get('classification')
        if classification == 'optional_improvement':
            for claim in _CLAIMS:
                if f.get(claim) is True:
                    d.add('misclassified_optional', base + (claim,))
        if classification == _MANDATORY and all(f.get(c) is False for c in _CLAIMS[:2]):
            d.add('inconsistent_mandatory', base + ('classification',))
        if classification == 'necessary_gap':
            for claim, invalid in zip(_CLAIMS, (True, True, False)):
                if f.get(claim) is invalid:
                    d.add('inconsistent_gap', base + (claim,))
        if classification in (_MANDATORY, 'necessary_gap') and f.get('evidence_kind') == 'hypothesis':
            d.add('hypothesis_classification', base + ('classification',))


def _unexpected(exc):
    return FindingsError([dict(code='malformed_input', path='', message='unexpected validation failure')])


def validate_report(report: Mapping, context: TrustedContext) -> NormalizedReport:
    try:
        catalogues = _context(context)
        if not isinstance(report, Mapping):
            raise FindingsError([dict(code='malformed_input', path='', message=_MESSAGES['malformed_input'])])
        report = dict(report)
        d = _Diagnostics(report)
        _walk(report, d)
        _schema_errors(report, d)
        _semantic_checks(report, context, catalogues, d)
        d.raise_if_any()
        findings = tuple(NormalizedFinding(**{k: tuple(v) if type(v) is list else v
                                              for k, v in f.items()}) for f in report['findings'])
        return NormalizedReport(report['schema_version'], report['reviewed_commit'], report['source_gate'], findings)
    except FindingsError:
        raise
    except Exception as exc:
        raise _unexpected(exc) from exc


def assess_report(report: Mapping, context: TrustedContext) -> Assessment:
    try:
        normalized = validate_report(report, context)
        roots = {}
        buckets = [[], [], [], []]
        for f in normalized.findings:
            if f.classification == _MANDATORY:
                if len(f.task_attribution) == 1:
                    roots.setdefault(f.task_attribution[0], []).append(f.id)
                else:
                    buckets[0].append(f.id)
            else:
                index = ('necessary_gap', 'uncertain_needs_diagnosis', 'optional_improvement').index(f.classification)
                buckets[index + 1].append(f.id)
        return Assessment(normalized.reviewed_commit, normalized.source_gate, normalized.findings,
                          tuple(CorrectionRoot(t, tuple(roots[t])) for t in sorted(roots)),
                          *(tuple(b) for b in buckets))
    except FindingsError:
        raise
    except Exception as exc:
        raise _unexpected(exc) from exc
