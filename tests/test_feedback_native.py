"""Inspect real native scope-recovery fixtures without changing their history."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_scope_amendments as fixtures
from ssdlc_feedback import main
from ssdlc_feedback_analysis import native_preview, path_classes
from ssdlc_reconciliation import _files
from ssdlc_tasks import TaskError, digest


class NativeFeedbackTests(unittest.TestCase):
    def initialize(self, attempts=3):
        f = fixtures.ScopeAmendmentTests()
        f.initialize(attempt_cap=attempts)
        self.addCleanup(f.doCleanups)
        self.source, self.fixture = f.source, f
        task = next(t for t in self.source['plan']['tasks'] if t['task_id'] == self.source['task_id'])
        ref = next(ref for ref in self.source['files'] if ref.endswith('/developer.log'))
        self.request = dict(schema_version=1, cause='MISSING_TARGET',
            obligation_refs=task['ac_refs'] + task['sec_refs'], requested_files=['app.py'],
            evidence_refs={ref: self.source['files'][ref]})

    def test_completed_source_owner_is_not_silently_delegated(self):
        self.initialize()
        before = _files(self.source['root'])
        result = native_preview(self.source, self.request)
        self.assertEqual(result['path_classes']['app.py'], dict(kind='COMPLETED_SOURCE', owners=['T1']))
        self.assertIn('OWNER_REVALIDATION_REQUIRED', result['stop_reasons'])
        self.assertIn('FEEDBACK_ALLOWANCE_UNAVAILABLE', result['stop_reasons'])
        self.assertEqual(result['attempts_remaining'], 1)
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['model_dispatches'], 0)
        self.assertEqual(_files(self.source['root']), before)
        self.assertEqual(self.fixture.calls, [1, 2])

    def test_attempts_exhausted_is_detected_before_any_consultation(self):
        self.initialize(attempts=2)
        result = native_preview(self.source, self.request)
        self.assertEqual(result['attempts_remaining'], 0)
        self.assertIn('ATTEMPTS_EXHAUSTED', result['stop_reasons'])
        self.assertEqual(result['model_dispatches'], 0)

    def test_new_path_is_distinguished_from_completed_source(self):
        self.initialize()
        result = path_classes(self.source, ['brand_new.py', 'app.py'])
        self.assertEqual(result['brand_new.py'], dict(kind='NEW_FILE', owners=[]))
        self.assertEqual(result['app.py']['owners'], ['T1'])

    def test_diagnosis_never_invokes_repairing_journal_load(self):
        self.initialize()
        before = _files(self.source['root'])
        with patch('ssdlc_tasks.TaskJournal.load', side_effect=AssertionError('diagnosis must not repair')):
            result = native_preview(self.source, self.request)
        self.assertEqual(result['model_dispatches'], 0)
        self.assertEqual(_files(self.source['root']), before)

    def test_partial_native_accounting_is_preserved_and_refused(self):
        self.initialize()
        path = self.source['root'] / 'limits/task-journal.jsonl'
        with path.open('ab') as output:
            output.write(b'{"partial":')
        before = path.read_bytes()
        with self.assertRaisesRegex(TaskError, 'Incomplete accounting'):
            native_preview(self.source, self.request)
        self.assertEqual(path.read_bytes(), before)

    def test_native_cli_diagnose_and_status_never_dispatch_or_initialize_authority(self):
        self.initialize()
        request = self.fixture.repo / 'offline-feedback-request.json'
        request.write_text(json.dumps(self.request))
        argv = ['feedback-diagnose', '--repo', str(self.fixture.repo), '--ticket', self.fixture.ticket.name,
                '--request', str(request)]
        before = _files(self.source['root'])
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=AssertionError('no models')) as dispatch:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(argv), 0)
            self.assertEqual(json.loads(output.getvalue())['model_dispatches'], 0)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['feedback-status', '--repo', str(self.fixture.repo),
                                       '--ticket', self.fixture.ticket.name]), 2)
            dispatch.assert_not_called()
        self.assertEqual(_files(self.source['root']), before)
        self.assertFalse((self.fixture.ticket / 'feedback').exists())


if __name__ == '__main__':
    unittest.main()
