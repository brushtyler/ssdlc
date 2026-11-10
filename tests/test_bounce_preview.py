"""Preview API/CLI and every archive persistence boundary in temporary repos."""
import contextlib
import copy
import io
import json
from pathlib import Path
from unittest.mock import patch

import orchestrator
import ssdlc_bounce as bounce
import ssdlc_gate_reports as gates
import test_bounce_evidence as evidence_tests
from test_gate_reports import GateFixture, Crash, finding
from test_full_checkpoint_review import framed


class PreviewTests(GateFixture):
    verify_gate = evidence_tests.EvidenceTests.verify_gate
    rewrite = evidence_tests.EvidenceTests.rewrite
    reseal = evidence_tests.EvidenceTests.reseal

    def setUp(self):
        super().setUp()
        self.orch._ensure_controller_contract()

    def gate(self, findings=None, attempt=1):
        return self.run_review(lambda *a, **k: (
            framed('review', self.orch, [finding()] if findings is None else findings),
            0, 0, 0, 'fake'), attempt=attempt)

    def preview(self, phase='04-review', attempt=1):
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=AssertionError('provider invoked')), \
             patch.object(self.orch, '_run_deterministic_scanners', side_effect=AssertionError('scanner invoked')), \
             patch.object(self.orch, '_run_appsec_scans', side_effect=AssertionError('scanner invoked')):
            return self.orch.preview_bounce(phase, attempt)

    def restart(self):
        # A new host instance, with no constructor, provider or scheduler work.
        fresh = orchestrator.SSDLCOrchestrator.__new__(orchestrator.SSDLCOrchestrator)
        for attr in ('repo_root', 'ticket_id', 'ssdlc_root', 'state_file', 'worktree_dir'):
            setattr(fresh, attr, getattr(self.orch, attr))
        self.orch = fresh

    def files(self):
        return sorted((self.orch.ssdlc_root / 'bounce-preview').rglob('*.*'))

    def cli(self, phase='04-review', attempt=1, extra=()):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = orchestrator.main(['bounce-preview', '--repo', str(self.repo),
                '--ticket', self.orch.ticket_id, '--phase', phase, '--attempt', str(attempt), *extra])
        return code, json.loads(stream.getvalue())

    def assert_blocked(self, value, code=None):
        self.assertEqual(value['status'], 'blocked', value)
        self.assertFalse(value['execution_authorized'])
        self.assertNotIn('plan', value)
        if code:
            self.assertEqual(value['blockers'], [{'code': code}])

    def test_api_restart_m1_no_writes_and_snapshots(self):
        self.gate()
        state = copy.deepcopy(self.orch.state)
        before = bounce.preview_snapshot(self.orch.ssdlc_root, state)
        head = orchestrator.git(self.orch.worktree_dir, 'rev-parse', 'HEAD')
        first = self.preview()
        self.assertEqual(first['status'], 'ready-for-later-executor')
        self.assertFalse(first['execution_authorized'])
        self.assertFalse(first['plan']['execution_authorized'])
        self.assertFalse(first['plan']['approved'])
        self.assertEqual(first['plan']['repair_roots'], ['T1'])
        self.assertEqual(self.orch.preview_counters['created'], 1)
        self.assertEqual(before, bounce.preview_snapshot(self.orch.ssdlc_root, self.orch.state))
        raw = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in
               [self.orch.state_file, *self.files()]}
        with patch.object(self.orch, 'record_artifact', side_effect=AssertionError('registration write')), \
             patch.object(gates, 'write_or_verify', side_effect=AssertionError('artifact write')):
            second = self.preview()
        self.assertEqual(second['publication'], 'reused')
        self.assertEqual(self.orch.preview_counters['reused'], 1)
        self.restart()
        with patch.object(self.orch, 'record_artifact', side_effect=AssertionError('registration write')), \
             patch.object(gates, 'write_or_verify', side_effect=AssertionError('artifact write')):
            third = self.preview()
        self.assertEqual(third['revision'], first['revision'])
        self.assertEqual(third['publication'], 'reused')
        self.assertEqual(raw, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in raw})
        self.assertEqual(head, orchestrator.git(self.orch.worktree_dir, 'rev-parse', 'HEAD'))
        payload = json.loads(self.files()[0].read_text())
        revision = payload.pop('revision')
        self.assertEqual(revision, bounce._sha(bounce.canonical_json(payload).encode()))
        summary = self.files()[1].read_text()
        for text in ('execution_authorized=false', revision, 'unkeyed integrity checks',
                     'not authentication', 'No continuous containment', '01-po/requirements.md'):
            self.assertIn(text, summary)
        self.assertNotIn(str(self.repo), summary)
        self.assertNotIn(str(self.repo), json.dumps(payload))

    def test_changed_verified_inputs_new_revision_preserves_old(self):
        self.gate()
        first = self.preview()
        old = {p: p.read_bytes() for p in self.files()}
        self.gate([finding('optional_improvement')], attempt=2)
        second = self.preview(attempt=2)
        self.assertEqual(second['status'], 'diagnostic')
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual(old, {p: p.read_bytes() for p in old})
        self.assert_blocked(self.preview(attempt=1), 'superseded_attempt')

    def test_unrelated_artifacts_do_not_change_or_block_existing_preview(self):
        self.gate()
        first = self.preview()
        original = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.files()}
        unrelated = self.orch.dir_qa / 'unrelated-later-output.md'
        unrelated.write_text('A later phase registered independent output')
        ref = str(unrelated.relative_to(self.orch.ssdlc_root))
        self.orch.record_artifact(ref, unrelated)
        for change in ('registered', 'missing', 'changed'):
            with self.subTest(change=change):
                if change == 'missing':
                    unrelated.unlink()
                elif change == 'changed':
                    unrelated.write_text('Unrelated output changed')
                self.restart()
                second = self.preview()
                self.assertEqual(second['revision'], first['revision'])
                self.assertEqual(second['publication'], 'reused')
                self.assertNotIn(ref, second['plan']['source_hashes'])
                self.assertEqual(original, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in original})

    def test_cli_exit_contract_and_no_authority(self):
        self.gate()
        code, value = self.cli()
        self.assertEqual(code, 0)
        self.assertFalse(value['execution_authorized'])
        self.gate([], attempt=2)
        code, value = self.cli(attempt=2)
        self.assertEqual(code, 10)
        self.assertEqual(value['status'], 'diagnostic')
        self.assertFalse(value['plan']['execution_authorized'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            orchestrator.main(['bounce-preview'])
        self.assertEqual(error.exception.code, 2)

    def test_control_failed_empty_and_negative_exit_ten(self):
        self.verify_gate('qa', det=False)
        code, value = self.cli('05-qa')
        self.assertEqual(code, 10)
        self.assertEqual(value['status'], 'diagnostic')
        self.assertIn({'code': 'control_failed', 'evaluation': value['plan']['evaluations'][0]['identity']},
                      value['plan']['blockers'])
        self.assertFalse(value['plan']['repair_roots'])

    def test_legacy_rejected_without_plan(self):
        value = self.preview()
        self.assertEqual(value['status'], 'rejected-legacy')
        self.assertNotIn('plan', value)
        code, value = self.cli()
        self.assertEqual(code, 20)
        self.assertFalse((self.orch.ssdlc_root / 'bounce-preview').exists())
        self.assertEqual(self.orch.preview_counters['blocked_by_reason']['rejected-legacy'], 1)

    def test_t3_c1_f24_main_no_archive_and_safe_log(self):
        self.verify_gate('qa', det=False)
        slot = next((self.orch.dir_qa / 'evaluations').iterdir())
        data = json.loads((slot / 'input.json').read_text())
        data['decision_inputs']['controls'] = {'det_ok': True}
        self.rewrite(slot / 'input.json', data)
        self.reseal(slot)
        with self.assertLogs('orchestrator', level='INFO') as logs:
            code, value = self.cli('05-qa')
        self.assertEqual(code, 20)
        self.assert_blocked(value, 'contradictory_evidence')
        self.assertFalse((self.orch.ssdlc_root / 'bounce-preview').exists())
        text = '\n'.join(logs.output)
        self.assertIn('contradictory_evidence', text)
        for forbidden in (str(self.repo), 'arbitrary output', 'det_ok', 'A concrete defect'):
            self.assertNotIn(forbidden, text)

    def test_missing_evidence_blocks_and_counts(self):
        self.gate()
        (self.slots()[0] / 'report.json').unlink()
        self.assert_blocked(self.preview(), 'integrity_mismatch')
        self.assertEqual(self.orch.preview_counters['blocked_by_reason']['integrity_mismatch'], 1)
        self.assertFalse(self.files())

    def test_tampered_evidence_blocks(self):
        self.gate()
        (self.slots()[0] / 'report.json').write_text('{}')
        self.assert_blocked(self.preview(), 'integrity_mismatch')
        self.assertFalse(self.files())

    def test_dirty_checkpoint_between_gate_and_preview(self):
        self.gate()
        (self.orch.worktree_dir / 'app.py').write_text('dirty')
        self.assert_blocked(self.preview(), 'stale_checkpoint')
        self.assertFalse(self.files())

    def test_moved_checkpoint_between_gate_and_preview(self):
        self.gate()
        orchestrator.git(self.orch.worktree_dir, 'commit', '--allow-empty', '-m', 'moved')
        self.assert_blocked(self.preview(), 'stale_checkpoint')
        self.assertFalse(self.files())

    def crash_write(self, boundary):
        real = gates.write_or_verify
        written = []
        def write(path, data, **kwargs):
            if boundary == 'P1':
                raise Crash()
            if boundary == 'P3' and path.name == 'plan.json':
                return  # model the symmetric summary-first persistence order
            real(path, data, **kwargs)
            written.append(path)
            if boundary == 'P2' and path.name == 'plan.json':
                raise Crash()
            if boundary in ('P3', 'P4') and path.name == 'summary.txt':
                raise Crash()
        with patch.object(gates, 'write_or_verify', side_effect=write), self.assertRaises(Crash):
            self.preview()
        return written

    def recovery(self, boundary, names):
        self.gate()
        self.crash_write(boundary)
        self.assertEqual({p.name for p in self.files()}, set(names))
        old = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.files()}
        self.restart()
        result = self.preview()
        self.assertEqual(result['publication'], 'created' if boundary == 'P1' else 'recovered')
        self.assertEqual(self.orch.preview_counters[result['publication']], 1)
        self.assertEqual(old, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in old})
        self.assertEqual({p.name for p in self.files()}, {'plan.json', 'summary.txt'})
        self.assertEqual(len([r for r in self.orch.state['artifacts'] if r.startswith('bounce-preview/')]), 2)
        return result

    def test_p1_before_first_write(self):
        self.recovery('P1', [])

    def test_p2_json_only(self):
        self.recovery('P2', ['plan.json'])

    def test_p3_summary_only(self):
        self.recovery('P3', ['summary.txt'])

    def test_p4_both_unregistered(self):
        self.gate()
        self.crash_write('P4')
        self.restart()
        with patch.object(gates, 'write_or_verify', side_effect=AssertionError('rewritten')), \
             patch.object(self.orch, 'record_artifact', wraps=self.orch.record_artifact) as register:
            result = self.preview()
        self.assertEqual(result['publication'], 'recovered')
        self.assertEqual(register.call_count, 2)

    def test_p5_between_registrations(self):
        self.gate()
        real = self.orch.record_artifact
        def register(ref, path):
            real(ref, path)
            raise Crash()
        with patch.object(self.orch, 'record_artifact', side_effect=register), self.assertRaises(Crash):
            self.preview()
        self.restart()
        with patch.object(self.orch, 'record_artifact', wraps=self.orch.record_artifact) as register:
            result = self.preview()
        self.assertEqual(result['publication'], 'recovered')
        self.assertEqual(register.call_count, 1)

    def test_p6_conflict_never_overwritten(self):
        self.gate()
        self.crash_write('P2')
        path = self.files()[0]
        path.write_text('conflicting old evidence')
        self.restart()
        self.assert_blocked(self.preview(), 'archive_conflict')
        self.assertEqual(path.read_text(), 'conflicting old evidence')
        self.assertEqual(len(self.files()), 1)

    def test_p7_registered_missing_never_regenerated(self):
        self.gate()
        self.preview()
        path = self.files()[0]
        path.unlink()
        self.restart()
        self.assert_blocked(self.preview(), 'registered_artifact_missing')
        self.assertFalse(path.exists())

    def test_p8_post_publication_check_never_blessed(self):
        self.gate()
        source = self.orch.worktree_dir / 'app.py'
        original = source.read_bytes()
        real = gates.write_or_verify
        def mutate(path, data, **kwargs):
            real(path, data, **kwargs)
            if path.name == 'summary.txt':
                source.write_text('changed after write')
        with patch.object(gates, 'write_or_verify', side_effect=mutate):
            self.assert_blocked(self.preview(), 'snapshot_mismatch')
        self.assertFalse(any(r.startswith('bounce-preview/') for r in self.orch.state['artifacts']))
        self.assertEqual(len(self.files()), 2)
        source.write_bytes(original)
        self.restart()
        result = self.preview()
        self.assertEqual(result['publication'], 'recovered')
        self.assertEqual(result['status'], 'ready-for-later-executor')

    def test_p8_post_registration_failure_is_not_returned_as_success(self):
        self.gate()
        source = self.orch.worktree_dir / 'app.py'
        original = source.read_bytes()
        real = self.orch.record_artifact
        def mutate(ref, path):
            real(ref, path)
            if path.name == 'summary.txt':
                source.write_text('changed during registration')
        with patch.object(self.orch, 'record_artifact', side_effect=mutate):
            self.assert_blocked(self.preview(), 'snapshot_mismatch')
        self.restart()
        self.assert_blocked(self.preview(), 'stale_checkpoint')
        source.write_bytes(original)
        self.assertEqual(self.preview()['publication'], 'reused')

    def test_p8_controller_postcheck_failure_reverified_on_restart(self):
        self.gate()
        real = orchestrator.assert_controller
        calls = []
        def check(root, expected):
            calls.append(root)
            if len(calls) == 3:
                raise orchestrator.TaskError('controller changed')
            real(root, expected)
        with patch('orchestrator.assert_controller', side_effect=check):
            self.assert_blocked(self.preview(), 'integrity_mismatch')
        self.assertEqual(len(self.files()), 2)
        self.assertFalse(any(r.startswith('bounce-preview/') for r in self.orch.state['artifacts']))
        self.restart()
        self.assertEqual(self.preview()['publication'], 'recovered')

    def test_diagnostic_protocol_outcome_cli_is_not_success(self):
        with self.assertRaises(gates.GateTerminalError):
            self.run_review(lambda *a, **k: ('bad protocol', 0, 0, 0, 'fake'))
        code, value = self.cli()
        self.assertEqual(code, 10)
        self.assertEqual(value['status'], 'diagnostic')
        self.assertFalse(value['plan']['repair_roots'])
        self.assertTrue(value['plan']['diagnosis'])
        self.assertFalse(value['execution_authorized'])

    def test_p9_symlink_rejected_before_writes(self):
        self.gate()
        outside = self.repo / 'outside'
        outside.mkdir()
        (self.orch.ssdlc_root / 'bounce-preview').symlink_to(outside, target_is_directory=True)
        self.assert_blocked(self.preview())
        self.assertEqual(list(outside.iterdir()), [])

    def test_p9_traversal_selector_and_ticket_rejected(self):
        self.gate()
        self.assert_blocked(self.preview('../escape'), 'malformed_record')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.cli(extra=('--ticket', '../escape'))
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.files())
        with self.assertRaises(bounce.BounceError):
            bounce._safe_path(self.orch.ssdlc_root, 'bounce-preview/../escape')

    def test_controller_contract_missing_is_not_recaptured(self):
        self.gate()
        (self.orch.ssdlc_root / '00-recon/controller-contract.json').unlink()
        self.assert_blocked(self.preview(), 'controller_integrity')
        self.assertFalse(self.files())

    def test_protocol_translation_and_baseexception(self):
        self.gate()
        with patch.object(bounce, 'plan_bounce', side_effect=gates.ProtocolError('binding_gate', 'secret')):
            self.assertEqual(self.cli()[0], 20)
        with patch.object(bounce, 'plan_bounce', side_effect=gates.ProtocolError('invalid', 'secret')):
            self.assert_blocked(self.preview(), 'report_invalid')
        with patch.object(bounce, 'plan_bounce', side_effect=Crash()), self.assertRaises(Crash):
            self.preview()
        self.assertFalse(self.files())

    def test_docs_required_facts_and_limits(self):
        root = Path(orchestrator.__file__).parent
        for name in ('README.md', 'PLAN_TASK_ROUTING_AND_SUBGOALS.md'):
            text = (root / name).read_text()
            if name.startswith('PLAN_'):
                text = text.split('## 12. Stato del bootstrap progressivo', 1)[1]
            for fact in ('Step 3b.2', 'PR #13', '397 tests after review fixes',
                         'only prepares targeted bounce', 'repair execution',
                         'cumulative recovery', 'revalidation integration',
                         'repeated global gates', 'advanced-target delivery',
                         'GH issue #14', 'not implemented here',
                         'unkeyed integrity checks', 'not authentication'):
                self.assertIn(fact, text)
