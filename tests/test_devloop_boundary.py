"""devloop is an independent tool: it may import only the standard library and itself."""

import ast
import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / 'devloop'


def imported_roots(path):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split('.')[0], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split('.')[0], node.lineno


class DevloopBoundaryTests(unittest.TestCase):
    def test_only_stdlib_and_devloop_imports(self):
        sources = sorted(PACKAGE.rglob('*.py'))
        self.assertTrue(sources)
        allowed = set(sys.stdlib_module_names) | {'devloop'}
        for path in sources:
            for root, line in imported_roots(path):
                with self.subTest(file=path.name, line=line, module=root):
                    self.assertIn(root, allowed,
                                  f'{path.name}:{line} imports {root!r}; devloop must not depend '
                                  'on ssdlc modules or third-party packages')

    def test_detects_forbidden_imports(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / 'probe.py'
            probe.write_text('import json\nfrom orchestrator import x\nimport ssdlc_tasks.sub\n'
                             'from . import sibling\n')
            self.assertEqual([r for r, _ in imported_roots(probe)],
                             ['json', 'orchestrator', 'ssdlc_tasks'])


if __name__ == '__main__':
    unittest.main()
