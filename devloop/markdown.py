"""GitHub text: the AGENTS.md attribution header, hidden markers, review rendering.

Markers are the dispatcher's control records, and they share a GitHub account with text written
by models (summaries, findings, notes, PR bodies, logs). Two rules keep the two apart:

- every published body goes through ``publishable``, which neutralizes HTML comments in the
  text, so the text cannot contain a marker at all;
- ``read_marker`` accepts one marker per body, only as its last line, only of the expected kind
  and only with a valid structure.
"""

import base64
import json
import re

UNKNOWN = 'non noto'
_EFFORT_LABELS = {'xhigh': 'Extra High', 'max': 'Max'}
_MARKER = re.compile(r'<!-- devloop:(?P<kind>[a-z]+):v1 (?P<payload>[A-Za-z0-9+/=]+) -->')
_VERSION = re.compile(r'\d+\.\d+(?:\.\d+)?')

DISPATCHER = {'agent': 'devloop dispatcher', 'model': 'nessuno (testo generato dal dispatcher)',
              'effort': 'n/a'}


def parse_version(text):
    match = _VERSION.search(text or '')
    return match.group(0) if match else None


def effort_label(effort):
    effort = (effort or '').strip().lower()
    if not effort:
        return UNKNOWN
    return _EFFORT_LABELS.get(effort, effort.title())


def header(agent_name, cli_version, model_label, effort):
    """The header AGENTS.md requires on every text published to GitHub."""
    agent = f'{agent_name} (CLI v{cli_version})' if cli_version else f'{agent_name} (CLI {UNKNOWN})'
    return ('> [!NOTE]\n'
            f'> **Agent:** {agent}\n'
            f'> **Model:** {model_label or UNKNOWN}\n'
            f'> **Reasoning Level:** {effort_label(effort)}\n')


def dispatcher_header():
    return ('> [!NOTE]\n'
            f'> **Agent:** {DISPATCHER["agent"]}\n'
            f'> **Model:** {DISPATCHER["model"]}\n'
            f'> **Reasoning Level:** {DISPATCHER["effort"]}\n')


def marker(kind, payload):
    data = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return f'<!-- devloop:{kind}:v1 {base64.b64encode(data).decode()} -->'


def neutralize(text):
    """Text that cannot open an HTML comment, and therefore cannot carry a marker."""
    return (text or '').replace('<!--', '&lt;!--')


def publishable(body, kind=None, payload=None):
    """The only way devloop builds a body for GitHub: neutralized text plus, optionally, the
    controller's marker as the last line."""
    body = neutralize(body).rstrip('\n') + '\n'
    if kind is not None:
        body += '\n' + marker(kind, payload) + '\n'
    return body


def _is_int(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _list_of_dicts(value):
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _valid_review(r):
    return (r.get('verdict') in _VERDICT_TEXT and _is_int(r.get('round'), 1)
            and isinstance(r.get('head_sha'), str) and isinstance(r.get('summary'), str)
            and r.get('source') in ('agent', 'ci')
            and all(_list_of_dicts(r.get(k)) for k in ('blocking_findings',
                                                        'non_blocking_findings',
                                                        'previous_findings'))
            and all(isinstance(f.get('id'), str) for f in r['blocking_findings']))


def _valid_fix(f):
    return (_is_int(f.get('round'), 1) and isinstance(f.get('head_sha'), str)
            and _list_of_dicts(f.get('responses')))


def _valid_pr(m):
    return _is_int(m.get('issue'), 1) and all(isinstance(m.get(k), str)
                                              for k in ('implementer', 'reviewer'))


_VALID = {'review': _valid_review, 'fix': _valid_fix, 'pr': _valid_pr}


def read_marker(text, kind):
    """The record of ``kind`` carried by ``text``, or None.

    Only the last non-empty line counts, so a marker quoted or embedded in the text is ignored.
    """
    lines = (text or '').strip().splitlines()
    match = _MARKER.fullmatch(lines[-1].strip()) if lines else None
    if not match or match.group('kind') != kind:
        return None
    try:
        data = json.loads(base64.b64decode(match.group('payload'), validate=True))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and _VALID[kind](data) else None


_VERDICT_TEXT = {
    'approve': 'Approvata',
    'request_changes': 'Modifiche richieste',
    'needs_human': 'Serve una decisione umana',
}
_STATUS_TEXT = {'resolved': 'risolto', 'unresolved': 'non risolto', 'withdrawn': 'ritirato'}
_ACTION_TEXT = {'fixed': 'corretto', 'disputed': 'contestato', 'not_applicable': 'non applicabile'}


def _location(finding):
    if not finding['file']:
        return ''
    line = f':{finding["line"]}' if finding['line'] > 0 else ''
    return f' `{finding["file"]}{line}`'


def _findings(findings):
    lines = []
    for f in findings:
        lines.append(f'- **{f["id"]}**{_location(f)}: {f["title"]}')
        if f['detail']:
            lines.append(f'  {f["detail"]}'.replace('\n', '\n  '))
        if f['suggested_fix']:
            lines.append(f'  *Correzione suggerita:* {f["suggested_fix"]}'.replace('\n', '\n  '))
    return '\n'.join(lines)


def render_review(review, head):
    """Markdown body (after the header) for a normalized review record."""
    parts = [f'## Review automatica, round {review["round"]}/{review["max_rounds"]}', '',
             f'**Verdetto:** {_VERDICT_TEXT[review["verdict"]]}  ',
             f'**Commit:** `{review["head_sha"][:12]}` · **CI:** {review["ci"]}']
    if review.get('note'):
        parts += ['', f'> {review["note"]}']
    parts += ['', review['summary'].strip()]
    if review['verdict'] == 'needs_human' and review['human_reason']:
        parts += ['', '### Decisione richiesta', '', review['human_reason'].strip()]
    if review['blocking_findings']:
        parts += ['', '### Bloccanti', '', _findings(review['blocking_findings'])]
    if review['non_blocking_findings']:
        parts += ['', '### Non bloccanti', '', _findings(review['non_blocking_findings'])]
    if review['previous_findings']:
        parts += ['', '### Finding dei round precedenti', '', '| ID | Stato | Nota |', '|---|---|---|']
        for p in review['previous_findings']:
            note = p['note'].replace('|', '\\|').replace('\n', ' ')
            parts.append(f'| {p["id"]} | {_STATUS_TEXT[p["status"]]} | {note} |')
    if review['checks_performed']:
        parts += ['', '<details><summary>Controlli eseguiti</summary>', '']
        parts += [f'- {c}' for c in review['checks_performed']]
        parts += ['', '</details>']
    return publishable(head + '\n' + '\n'.join(parts), 'review', review)


def render_fix(fix, head, round_no):
    parts = [f'## Correzioni per il round {round_no}', '', fix['summary'].strip()]
    if fix['responses']:
        parts += ['', '| Finding | Azione | Nota |', '|---|---|---|']
        for r in fix['responses']:
            note = r['note'].replace('|', '\\|').replace('\n', ' ')
            parts.append(f'| {r["finding_id"]} | {_ACTION_TEXT[r["action"]]} | {note} |')
    payload = {'round': round_no, 'head_sha': fix['head_sha'], 'responses': fix['responses']}
    return publishable(head + '\n' + '\n'.join(parts), 'fix', payload)


def render_escalation(reason, detail=''):
    body = dispatcher_header() + '\n## Serve un intervento umano\n\n' + reason.strip() + '\n'
    if detail:
        body += '\n```text\n' + detail.strip()[-6000:].replace('```', "'''") + '\n```\n'
    return publishable(body)
