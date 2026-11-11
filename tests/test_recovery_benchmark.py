"""The existing CI test job must grade the pinned replay corpus on every PR."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from replay.benchmark import main


class RecoveryBenchmarkGate(unittest.TestCase):
    def test_pinned_recovery_corpus(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            output = Path(tmp) / 'report.json'
            self.assertEqual(main(['--output', str(output)]), 0)
            report = json.loads(output.read_text())
        self.assertEqual(report['corpus'], 'i2-recovery-v1')
        self.assertEqual(len(report['cases']), 9)
        self.assertEqual(report['failed_cases'], 0)
        self.assertEqual(sum(c['total_interventions_required'] for c in report['cases']), 6)
        self.assertEqual(sum(c['avoidable_interventions'] for c in report['cases']), 0)
        self.assertEqual(sum(c['escaped_seeded_defects'] for c in report['cases']), 0)
        self.assertFalse(any(c['ready_pr'] for c in report['cases']))
