"""The existing CI test job must grade the pinned replay corpus on every PR."""
import subprocess
import sys
import json
from pathlib import Path
import tempfile
import unittest



class RecoveryBenchmarkGate(unittest.TestCase):
    def test_pinned_recovery_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'report.json'
            result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve().parents[1] / 'ssdlc_replay_gate.py'),
                                     '--output', str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
        self.assertEqual(report['corpus'], 'i2-recovery-v2')
        self.assertEqual(len(report['cases']), 12)
        self.assertEqual(report['failed_cases'], 0)
        self.assertEqual(sum(c['total_interventions_required'] for c in report['cases']), 9)
        self.assertEqual(sum(c['avoidable_interventions'] for c in report['cases']), 0)
        self.assertEqual(sum(c['escaped_seeded_defects'] for c in report['cases']), 0)
        self.assertTrue(all('ready_pr' not in c and 'live_model_calls' not in c for c in report['cases']))
        self.assertEqual(report['baseline_id'], 'i2-fixtures-v4')
