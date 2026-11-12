"""Turns a reviewer's structured output into a consistent review record.

The reviewer proposes a verdict; devloop enforces the rules that make the
verdict machine-actionable:

* ``approve`` only with no blocking finding and nothing left unresolved;
* ``request_changes`` only with at least one open finding to fix;
* every blocking finding of earlier rounds is accounted for, and one the
  reviewer does not assess stays open.
"""

from .schema import load_schema, validate

VERDICTS = ('approve', 'request_changes', 'needs_human')


def _with_ids(findings, round_no, start):
    return [dict(f, id=f'R{round_no}-{start + i}') for i, f in enumerate(findings)]


def open_findings(reviews):
    """Blocking findings still open after the latest review, oldest first.

    Includes the latest review's blocking findings and any earlier finding it
    marked unresolved.
    """
    if not reviews:
        return []
    by_id = {}
    for review in reviews:
        for finding in review['blocking_findings']:
            by_id[finding['id']] = finding
    latest = reviews[-1]
    carried = [by_id[p['id']] for p in latest['previous_findings']
               if p['status'] == 'unresolved' and p['id'] in by_id]
    return carried + list(latest['blocking_findings'])


def normalize_review(raw, *, previous_open, round_no, max_rounds, head_sha, ci, reviewer):
    """Validate ``raw`` against the review schema and return a review record."""
    validate(raw, load_schema('review'))
    notes = []
    blocking = _with_ids(raw['blocking_findings'], round_no, 1)
    non_blocking = _with_ids(raw['non_blocking_findings'], round_no, len(blocking) + 1)
    known = {f['id'] for f in previous_open}
    assessed = {p['id']: p for p in raw['previous_findings'] if p['id'] in known}
    previous = []
    for finding in previous_open:
        previous.append(assessed.get(finding['id']) or {
            'id': finding['id'], 'status': 'unresolved', 'note': 'Non valutato dal reviewer.'})
    if len(assessed) < len(known):
        notes.append('Il reviewer non ha valutato tutti i finding precedenti: quelli mancanti restano aperti.')
    unresolved = [p for p in previous if p['status'] == 'unresolved']

    verdict = raw['verdict']
    human_reason = raw['human_reason']
    if verdict == 'approve' and (blocking or unresolved):
        verdict = 'request_changes'
        notes.append('Verdetto corretto da approve a request_changes: restano finding bloccanti aperti.')
    elif verdict == 'request_changes' and not (blocking or unresolved):
        verdict = 'needs_human'
        human_reason = human_reason or 'Il reviewer ha chiesto modifiche senza indicare finding bloccanti.'
        notes.append('Verdetto incoerente: request_changes senza finding bloccanti.')
    elif verdict == 'needs_human' and not human_reason.strip():
        human_reason = 'Il reviewer non ha indicato la decisione richiesta.'

    return {
        'source': 'agent', 'reviewer': reviewer, 'verdict': verdict,
        'summary': raw['summary'], 'human_reason': human_reason,
        'blocking_findings': blocking, 'non_blocking_findings': non_blocking,
        'previous_findings': previous, 'checks_performed': raw['checks_performed'],
        'round': round_no, 'max_rounds': max_rounds, 'head_sha': head_sha, 'ci': ci,
        'note': ' '.join(notes),
    }


def ci_failure_review(*, failed_checks, log_tail, previous_open, round_no, max_rounds, head_sha):
    """A review record that sends a PR with failing CI back to the implementer."""
    names = ', '.join(failed_checks) or 'CI'
    finding = {'id': f'R{round_no}-1', 'file': '', 'line': 0,
               'title': f'CI fallita: {names}',
               'detail': log_tail.strip() or 'Log non disponibile.',
               'suggested_fix': 'Correggere la causa del fallimento e far passare la CI.'}
    return {
        'source': 'ci', 'reviewer': 'ci', 'verdict': 'request_changes',
        'summary': f'La CI non è verde ({names}). La review del codice riprende dopo la correzione.',
        'human_reason': '', 'blocking_findings': [finding], 'non_blocking_findings': [],
        'previous_findings': [{'id': f['id'], 'status': 'unresolved',
                               'note': 'Non valutato: CI fallita.'} for f in previous_open],
        'checks_performed': [], 'round': round_no, 'max_rounds': max_rounds,
        'head_sha': head_sha, 'ci': 'fail', 'note': '',
    }
