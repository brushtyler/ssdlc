"""Helpers for documentation contract tests.

Documentation contracts check that the docs state the system's real limits and
non-guarantees. They match normalized phrases, not exact formatting, and read the
pages a reader can reach, not a fixed file:

- ``user_facing_docs`` is README.md plus every Markdown page under docs/ reachable
  from it through relative links. Internal plans are excluded even when linked, so a
  limit cannot count as documented only because an internal plan mentions it.
- ``plan_section`` is one section of an internal plan, checked on its own.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_LINK = re.compile(r'\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
_HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


class Corpus:
    """Normalized text of one or more documents, with their names for messages."""

    def __init__(self, label, texts):
        self.label = label
        self.names = tuple(texts)
        self.text = normalize('\n'.join(texts.values()))

    def missing(self, phrases):
        return [phrase for phrase in phrases if normalize(phrase) not in self.text]

    def missing_issues(self, numbers):
        return [number for number in numbers if not mentions_issue(self.text, number)]


def normalize(text):
    """Lower case, drop inline-code and emphasis markers, collapse whitespace."""
    return ' '.join(re.sub(r'[`*]', '', text).lower().split())


def mentions_issue(text, number):
    """True when ``#<number>`` appears as a whole reference (``#1`` is not ``#14``).

    A following ``-`` or word character is excluded, so heading anchors such as
    ``#12-stato`` do not count as ``#12``.
    """
    return re.search(rf'(?<![\w#])#{int(number)}(?![\w-])', text) is not None


def local_links(text):
    """Relative link targets without fragments; anchors, URLs and mailto are skipped."""
    targets = []
    for target in _LINK.findall(text):
        if target.startswith('#') or re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I):
            continue
        path = target.split('#', 1)[0]
        if path:
            targets.append(path)
    return targets


def user_facing_docs(root=ROOT):
    """README.md plus the Markdown pages under docs/ reachable from it."""
    root = Path(root).resolve()
    docs = root / 'docs'
    readme = root / 'README.md'
    texts, queue, visited = {}, [readme], set()
    while queue:
        page = queue.pop(0)
        if page in visited:
            continue
        visited.add(page)
        text = page.read_text()
        texts[page.relative_to(root).as_posix()] = text
        for target in local_links(text):
            linked = (page.parent / target).resolve()
            if (linked.suffix == '.md' and linked.is_relative_to(docs)
                    and linked.is_file() and linked not in visited):
                queue.append(linked)
    return Corpus('user-facing docs', texts)


def plan_section(name, heading, root=ROOT):
    """The section of ``name`` under ``heading`` up to the next heading of its level."""
    lines = (Path(root) / name).read_text().splitlines()
    start = level = None
    fenced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if not match:
            continue
        if start is None:
            if match.group(2) == heading.lstrip('#').strip():
                start, level = index, len(match.group(1))
        elif len(match.group(1)) <= level:
            return Corpus(f'{name} § {heading}', {name: '\n'.join(lines[start:index])})
    if start is None:
        raise AssertionError(f'{name}: heading not found: {heading!r}')
    return Corpus(f'{name} § {heading}', {name: '\n'.join(lines[start:])})


def assert_documented(case, corpus, phrases=(), issues=()):
    """Fail with every missing phrase and issue reference, and the pages searched."""
    missing = corpus.missing(phrases) + [f'#{n}' for n in corpus.missing_issues(issues)]
    if missing:
        case.fail(f'{corpus.label} ({", ".join(corpus.names)}) is missing: {missing}')
