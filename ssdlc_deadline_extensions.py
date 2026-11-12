"""Explicit deadline deltas; elapsed history and the original anchor survive."""
import copy
import math
import re

from ssdlc_tasks import TaskError, digest


CAPABILITY = 'operator-deadline-extension-v1'


def specification(source, profile, deadline_at):
    old = source['counters']['deadline_at']
    if (type(deadline_at) not in (int, float) or not math.isfinite(deadline_at)
            or deadline_at <= old or not float(deadline_at - old).is_integer()):
        raise TaskError('Deadline extension requires an exact positive integral-second delta')
    previous = source['envelope']['policy']['wall_seconds']
    if profile['wall_seconds'] != previous + int(deadline_at - old):
        raise TaskError('Wall allowance must increase by exactly the deadline delta')
    return dict(capability=CAPABILITY, started_at=source['counters']['started_at'],
        old_deadline_at=old, new_deadline_at=deadline_at,
        old_wall_seconds=previous, new_wall_seconds=profile['wall_seconds'],
        source_policy_sha256=digest(source['envelope']))


def accounting_anchor(envelope):
    """Validate the bound chain, retaining the original historical anchor."""
    anchor = copy.deepcopy(envelope['binding']['accounting_anchor'])
    entries = envelope['binding'].get('deadline_extensions', [])
    if not isinstance(entries, list) or len(entries) > 1000:
        raise TaskError('Invalid deadline extension chain')
    previous_wall = None
    fields = {'capability', 'started_at', 'old_deadline_at', 'new_deadline_at',
              'old_wall_seconds', 'new_wall_seconds', 'source_policy_sha256'}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != fields
                or entry['capability'] != CAPABILITY
                or entry['started_at'] != anchor['started_at']
                or entry['old_deadline_at'] != anchor['deadline_at']
                or any(type(entry[k]) not in (int, float) or not math.isfinite(entry[k])
                       for k in ('old_deadline_at', 'new_deadline_at'))
                or any(type(entry[k]) is not int or entry[k] < 1
                       for k in ('old_wall_seconds', 'new_wall_seconds'))
                or entry['new_deadline_at'] <= entry['old_deadline_at']
                or entry['new_wall_seconds'] - entry['old_wall_seconds'] != entry['new_deadline_at'] - entry['old_deadline_at']
                or previous_wall is not None and entry['old_wall_seconds'] != previous_wall
                or not isinstance(entry['source_policy_sha256'], str)
                or not re.fullmatch('[a-f0-9]{64}', entry['source_policy_sha256'])):
            raise TaskError('Deadline extension differs from its bound historical chain')
        anchor['deadline_at'] = entry['new_deadline_at']
        previous_wall = entry['new_wall_seconds']
    if entries and previous_wall != envelope['policy']['wall_seconds']:
        raise TaskError('Deadline extension wall allowance differs from policy')
    return anchor
