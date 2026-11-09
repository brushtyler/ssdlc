"""Deterministic catalogues shared by task planning and validation."""
from __future__ import annotations

import re

ID = r'(?:AC|SEC)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*'
MENTION = re.compile(r'(?<![\w-])' + ID + r'(?![\w-])')


class CatalogueError(ValueError):
    def __init__(self, diagnostics):
        super().__init__('Invalid requirement definitions')
        self.diagnostics = diagnostics


def prose_lines(text):
    """Yield numbered Markdown lines outside fenced code (including tilde fences)."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        yield number, line


def requirement_catalogue(text: str) -> dict:
    """Definitions are headings, list entries, table rows or ID-prefixed lines.

    Mentions in prose never create obligations. Compact ID-only lists remain
    supported for programmatic callers. Hierarchy is descriptive, not coverage.
    """
    definitions = {}
    mentions = set()
    issues = []
    for number, line in prose_lines(text):
        mentions.update(MENTION.findall(re.sub(r'`[^`]*`', '', line)))
        stripped = line.strip()
        # A bare list of identifiers is an explicit declaration, not prose.
        tokens = MENTION.findall(stripped)
        if tokens and not re.sub(ID, '', stripped).strip(' ,;'):
            ids = tokens
        else:
            prefix = re.sub(r'^(?:#{1,6}\s+|[-+*]\s+|\d+[.)]\s+|\|\s*)', '', stripped)
            formatted = prefix != stripped or prefix.startswith(('**', '`', '__'))
            prefix = re.sub(r'^[*`_]+', '', prefix)
            token = re.match(r'((?:AC|SEC)-[^\s:*`|)]+)', prefix, re.I)
            candidate = token[1].rstrip('.') if token else ''
            suffix = prefix[token.end():] if token else ''
            declaration = bool(token and (formatted or not suffix or suffix.startswith((':', '.', ')', '|', '*', '`'))
                                          or suffix.lstrip().startswith('(')))
            if declaration and not re.fullmatch(ID, candidate):
                issues.append({'code': 'INVALID_REQUIREMENT_DEFINITION', 'ref': candidate, 'source_line': number})
            ids = [candidate] if declaration and re.fullmatch(ID, candidate) else []
        for identifier in ids:
            if identifier in definitions:
                issues.append({'code': 'DUPLICATE_REQUIREMENT_DEFINITION', 'ref': identifier,
                               'source_lines': [definitions[identifier]['source_line'], number]})
            else:
                definitions[identifier] = {'id': identifier, 'kind': identifier.split('-')[0],
                                           'source_line': number, 'definition': stripped}
    if issues:
        raise CatalogueError(issues)
    for identifier, entry in definitions.items():
        parents = [other for other in definitions if identifier.startswith(other)
                   and re.fullmatch(r'(?:[a-z]|-[A-Za-z0-9]+)', identifier[len(other):])]
        entry['parent_id'] = max(parents, key=len) if parents else None
    return {'coverage_policy': 'explicit_each_defined_id',
            'required_ids': sorted(definitions),
            'ac_refs': sorted(r for r in definitions if r.startswith('AC-')),
            'sec_refs': sorted(r for r in definitions if r.startswith('SEC-')),
            'definitions': [definitions[r] for r in sorted(definitions)],
            'undefined_mentions': sorted(mentions - definitions.keys())}


def heading_anchors(text: str) -> list[str]:
    """The contract anchor convention: ATX headings, punctuation removed, spaces hyphenated."""
    anchors = []
    used = set()
    for _, line in prose_lines(text):
        match = re.match(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', line)
        if not match:
            continue
        stem = re.sub(r'[^\w -]', '', match[1].lower()).replace(' ', '-')
        anchor, suffix = stem, 0
        while anchor in used:
            suffix += 1
            anchor = f'{stem}-{suffix}'
        used.add(anchor)
        anchors.append(anchor)
    return anchors
