"""Deterministic plan-amendment checks; review remains a separate authority."""
import ast
from pathlib import Path
import re

from ssdlc_planning import validate_plan_review
from ssdlc_tasks import PlanError, digest


def ancestors(plan):
    tasks = {t['task_id']: t for t in plan['tasks']}
    result, visiting = {}, set()
    def visit(tid):
        if tid in visiting or tid not in tasks:
            raise PlanError('Amendment contains a cycle or unknown dependency')
        if tid in result:
            return result[tid]
        visiting.add(tid)
        inherited = set()
        for parent in tasks[tid]['depends_on']:
            inherited.add(parent)
            inherited.update(visit(parent))
        visiting.remove(tid)
        result[tid] = inherited
        return inherited
    for tid in tasks:
        visit(tid)
    return result


def validate_amendment(old, new, report, validator, requirements, quality, *, lineage=None, history=()):
    """Require reviewed obligations and preserve ordering through merges/splits.

    Lineage maps each old node to its replacement nodes. This validates DAG
    semantics only; execution allowances still need an explicit mapping, so an
    executor may refuse lineage changes it cannot account for safely.
    """
    if new['plan_revision'] != old['plan_revision'] + 1:
        raise PlanError('Amendment must advance the plan revision exactly once')
    ordered = validator(new)
    before, after = ancestors(old), ancestors(new)
    if lineage is None:
        if set(before) != set(after):
            raise PlanError('Task merges/splits require explicit lineage')
        lineage = {tid: [tid] for tid in before}
    if (not isinstance(lineage, dict) or set(lineage) != set(before)
            or any(not isinstance(nodes, list) or not nodes
                   or any(not isinstance(n, str) for n in nodes) or len(set(nodes)) != len(nodes)
                   for nodes in lineage.values())
            or set().union(*(set(nodes) for nodes in lineage.values())) != set(after)):
        raise PlanError('Incomplete task lineage')
    for child, predecessors in before.items():
        for parent in predecessors:
            for replacement in lineage[child]:
                for prerequisite in lineage[parent]:
                    if prerequisite != replacement and prerequisite not in after[replacement]:
                        raise PlanError('Amendment lost required transitive ordering')
    assessment = validate_plan_review(report, new, requirements, list(history), quality)
    if assessment['verdict'] != 'APPROVED':
        raise PlanError('Amendment has unresolved independent review findings')
    return {'old_plan_sha256': digest(old), 'new_plan_sha256': digest(new),
            'assessment': assessment, 'lineage': lineage,
            'ordered_task_ids': [t['task_id'] for t in ordered]}


def fixture_impact(root, changed_paths, symbols=()):
    """Conservative hints, not a complete dependency or coverage proof.

    Parse source as data only. A syntax error is recorded, never executed or
    interpreted as a valid acceptance failure. Symlink trees are excluded.
    """
    root = Path(root)
    modules = {Path(p).stem for p in changed_paths if p.endswith('.py')}
    needles = modules | set(symbols)
    hits, errors = [], []
    for path in sorted(root.rglob('*.py')):
        relative = path.relative_to(root)
        if (any(part in {'.git', '.ssdlc', '.tools', '__pycache__'} for part in relative.parts)
                or any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1))):
            continue
        try:
            source = path.read_text()
            tree = ast.parse(source)
        except (OSError, UnicodeError, SyntaxError):
            errors.append(relative.as_posix())
            continue
        references = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                references.add(node.id)
            elif isinstance(node, ast.Attribute):
                references.add(node.attr)
            elif isinstance(node, ast.ImportFrom) and node.module:
                references.update(node.module.split('.'))
            elif isinstance(node, ast.Import):
                references.update(part for name in node.names for part in name.name.split('.'))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                references.update(name for name in needles
                                  if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', node.value))
        matched = sorted(references & needles)
        if matched:
            hits.append({'path': relative.as_posix(), 'references': matched})
    return {'schema_version': 1, 'changed_paths': sorted(changed_paths), 'references': hits,
            'parse_errors': errors, 'complete_dependency_proof': False}


def changed_symbols(repo, snapshot_root, manifest):
    """Names of removed or signature-changed Python declarations for fixture hints."""
    def declarations(path):
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError, UnicodeError):
            return {}
        return {node.name: ast.dump(node.args, include_attributes=False)
                for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    result = set()
    for entry in manifest['source_files']:
        if entry['path'].endswith('.py'):
            old = declarations(Path(repo) / entry['path'])
            new = {} if entry.get('deleted') else declarations(Path(snapshot_root) / entry['path'])
            result.update(name for name, signature in old.items() if new.get(name) != signature)
    return sorted(result)
