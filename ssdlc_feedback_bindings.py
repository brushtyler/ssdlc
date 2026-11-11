"""Separate reviewed technical inputs from precisely charged consultations.

A reviewer changes accounting through its own invocation. This relation permits
only that recorded advance; it never relabels a review as covering changed code,
contracts, task history, old costs, controller identity or budget policy.
"""
import copy
import hashlib

from ssdlc_feedback import FeedbackError, literal_path
from ssdlc_feedback_roles import _usage
from ssdlc_scope_amendments import subject
from ssdlc_tasks import TaskJournal, digest
from ssdlc_transitions import _regular


ACCOUNTING_REFS = frozenset({'limits/task-journal.jsonl', 'limits/tasks-state.json'})


def technical_files(source):
    return {ref: sha for ref, sha in source['files'].items() if ref not in ACCOUNTING_REFS}


def technical_subject(source, plan, reason):
    reviewed = subject(source, plan, reason)
    for change in reviewed['scope_diff'].values():
        for path in change['added']:
            literal_path(path)
    reviewed.pop('counters_sha256')
    reviewed['source_files_sha256'] = digest(technical_files(source))
    reviewed['compatibility'] = 'feedback-technical-subject-v1'
    reviewed['accounting_authority_sha256'] = digest(source['envelope'])
    return reviewed


def accounting_anchor(source):
    ref = 'limits/task-journal.jsonl'
    data = _regular(source['root'], ref).read_bytes()
    if not data.endswith(b'\n'):
        raise FeedbackError('Incomplete native accounting requires reconciliation')
    sha = hashlib.sha256(data).hexdigest()
    if source['files'].get(ref) != sha:
        raise FeedbackError('Accounting changed before the consultation anchor')
    return dict(schema_version=1, technical_files_sha256=digest(technical_files(source)),
                counters=copy.deepcopy(source['counters']), envelope_sha256=digest(source['envelope']),
                journal_size=len(data), journal_sha256=sha)


def validate_accounting_advance(anchor, current, calls):
    """Exact prior facts plus exactly the captured, matching feedback returns."""
    if (anchor['envelope_sha256'] != digest(current['envelope'])
            or anchor['technical_files_sha256'] != digest(technical_files(current))):
        raise FeedbackError('Technical inputs or accounting authority changed during consultation')
    before, after = anchor['counters'], current['counters']
    if {k: v for k, v in before.items() if k != 'invocations'} != {k: v for k, v in after.items() if k != 'invocations'}:
        raise FeedbackError('Consultation changed cumulative accounting anchors')
    old, new = before['invocations'], after['invocations']
    if any(new.get(key) != value for key, value in old.items()):
        raise FeedbackError('Historical invocation or usage changed')
    additions = {}
    for call in calls:
        if call.get('status') != 'RETURNED' or 'native' not in call:
            raise FeedbackError('Consultation outcome is not captured')
        identity = call['native']['invocation_id']
        if identity in additions or identity in old:
            raise FeedbackError('Consultation receipt duplicated or outside this accounting advance')
        if call['native']['limits_sha256'] != anchor['envelope_sha256']:
            raise FeedbackError('Consultation belongs to another limits authority')
        result = call['receipt']['response']
        if digest(result) != call['receipt']['response_sha256']:
            raise FeedbackError('Captured consultation response changed')
        usage = _usage(result['usage'])
        row = new.get(identity)
        if (row is None or row['status'] != 'returned' or row['usage'] != usage
                or row['role'] != 'feedback-' + call['role'] or row['phase'] != 'feedback'
                or row['engine'] != call['actor']['engine']):
            raise FeedbackError('Native invocation does not match the captured feedback return')
        additions[identity] = copy.deepcopy(row)
    if set(new) - set(old) != set(additions):
        raise FeedbackError('Unrelated invocation during consultation')
    data = _regular(current['root'], 'limits/task-journal.jsonl').read_bytes()
    if (not data.endswith(b'\n') or hashlib.sha256(data).hexdigest()
            != current['files'].get('limits/task-journal.jsonl') or TaskJournal._replay(data)[0] != after):
        raise FeedbackError('Current native accounting changed or is incomplete')
    if hashlib.sha256(data[:anchor['journal_size']]).hexdigest() != anchor['journal_sha256']:
        raise FeedbackError('Accounting journal prefix changed')
    return dict(schema_version=1, envelope_sha256=anchor['envelope_sha256'],
                anchor_sha256=digest(anchor), current_counters_sha256=digest(after),
                charged_invocations=sorted(additions), technical_files_sha256=anchor['technical_files_sha256'])
