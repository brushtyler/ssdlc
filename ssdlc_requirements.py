"""Conservative PO-format adaptation; the shared catalogue remains strict."""
import re

from ssdlc_catalogues import CatalogueError, prose_lines, requirement_catalogue


def prepare_requirements(text):
    """Rename only unambiguous dotted declaration IDs, preserving all prose.

    The caller must archive the original and mapping before publishing this
    representation. Ambiguous/colliding or other invalid IDs are never repaired.
    Fenced code is not rewritten; this is not a generic Markdown reformatter.
    """
    mapping = {}
    normalized = text
    try:
        catalogue = requirement_catalogue(text)
    except CatalogueError as original:
        for diagnostic in original.diagnostics:
            identifier = diagnostic.get('ref', '')
            if (diagnostic['code'] != 'INVALID_REQUIREMENT_DEFINITION'
                    or not re.fullmatch(r'(?:AC|SEC)-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*', identifier)
                    or '.' not in identifier):
                raise
            mapping[identifier] = identifier.replace('.', '-')
        if len(set(mapping.values())) != len(mapping):
            raise CatalogueError([{'code': 'AMBIGUOUS_REQUIREMENT_RENAME'}])
        pattern = re.compile(r'(?<![\w.-])(?:' + '|'.join(
            re.escape(ref) for ref in sorted(mapping, key=len, reverse=True)) + r')(?![\w-]|\.[A-Za-z0-9])')
        lines = text.splitlines(keepends=True)
        for number, _ in prose_lines(text):
            lines[number - 1] = pattern.sub(lambda match: mapping[match[0]], lines[number - 1])
        normalized = ''.join(lines)
        # This also rejects aliases colliding with existing canonical IDs and
        # unsupported representations that could not be rewritten exactly.
        catalogue = requirement_catalogue(normalized)
    missing = [kind for kind, key in (('AC', 'ac_refs'), ('SEC', 'sec_refs')) if not catalogue[key]]
    if missing:
        raise CatalogueError([{'code': 'MISSING_REQUIREMENT_DEFINITIONS', 'kind': kind} for kind in missing])
    return normalized, mapping, catalogue
