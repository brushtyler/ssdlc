"""GitHub text: the AGENTS.md attribution header, hidden markers, review rendering."""

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


def read_markers(text, kind):
    found = []
    for match in _MARKER.finditer(text or ''):
        if match.group('kind') != kind:
            continue
        try:
            found.append(json.loads(base64.b64decode(match.group('payload'), validate=True)))
        except (ValueError, json.JSONDecodeError):
            continue
    return found


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
    return head + '\n' + '\n'.join(parts) + '\n\n' + marker('review', review) + '\n'


def render_fix(fix, head, round_no):
    parts = [f'## Correzioni per il round {round_no}', '', fix['summary'].strip()]
    if fix['responses']:
        parts += ['', '| Finding | Azione | Nota |', '|---|---|---|']
        for r in fix['responses']:
            note = r['note'].replace('|', '\\|').replace('\n', ' ')
            parts.append(f'| {r["finding_id"]} | {_ACTION_TEXT[r["action"]]} | {note} |')
    payload = {'round': round_no, 'head_sha': fix['head_sha'], 'responses': fix['responses']}
    return head + '\n' + '\n'.join(parts) + '\n\n' + marker('fix', payload) + '\n'


def render_escalation(reason, detail=''):
    body = dispatcher_header() + '\n## Serve un intervento umano\n\n' + reason.strip() + '\n'
    if detail:
        body += '\n```text\n' + detail.strip()[-6000:].replace('```', "'''") + '\n```\n'
    return body
