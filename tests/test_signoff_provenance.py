"""Approval must consume verified final-candidate gate provenance."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import orchestrator as o
import test_legacy_compat as legacy
import test_unified_integration as journeys
from ssdlc_tasks import TaskError


class SignoffProvenanceTests(unittest.TestCase):
    def fixture(self, case):
        instance=case()
        self.addCleanup(instance.doCleanups)
        instance.setUp()
        return instance

    def test_ready_status_without_gate_receipts_is_refused_before_constructor(self):
        f=self.fixture(legacy.CompatibilityTests)
        f.seal()
        before=f.snapshot()
        self.assertFalse(o.execution_compatibility(f.repo,'OLD','approve')['ok'])
        with patch.object(o,'SSDLCOrchestrator',side_effect=AssertionError('constructor')) as ctor:
            ctor.PHASE_ORDER=['00-recon','03-dev']
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(o._foreground_main(['approve','OLD','--repo',str(f.repo)]),23)
        self.assertEqual(before,f.snapshot())

    def test_full_native_provenance_admits_approval(self):
        f=self.fixture(journeys.StartupJourneys)
        f.new_run()
        orch=f.active_orch
        o.verify_signoff_evidence(orch.repo_root,orch.ssdlc_root,orch.state,orch.worktree_dir)
        before={str(p.relative_to(orch.ssdlc_root)):p.read_bytes() for p in orch.ssdlc_root.rglob('*') if p.is_file()}
        self.assertTrue(o.execution_compatibility(orch.repo_root,orch.ticket_id,'approve')['ok'])
        after={str(p.relative_to(orch.ssdlc_root)):p.read_bytes() for p in orch.ssdlc_root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_tampered_or_incomplete_phase_never_falls_back_to_old_approval(self):
        f=self.fixture(journeys.StartupJourneys)
        f.new_run()
        orch=f.active_orch
        for phase in ('04-review','05-qa','05b-appsec'):
            completion=orch.ssdlc_root/phase/'provenance/attempt-1/completion.json'
            original=completion.read_bytes()
            with self.subTest(phase=phase,case='tamper'):
                completion.write_bytes(original+b' ')
                with self.assertRaisesRegex(TaskError,'approval evidence'):
                    o.verify_signoff_evidence(orch.repo_root,orch.ssdlc_root,orch.state,orch.worktree_dir)
                self.assertFalse(o.execution_compatibility(orch.repo_root,orch.ticket_id,'approve')['ok'])
                with patch.object(o,'git',wraps=o.git) as command:
                    with self.assertRaisesRegex(TaskError,'approval evidence'):
                        orch._approve_and_merge()
                    self.assertFalse(any(c.args[1:2]==('merge',) for c in command.call_args_list))
                completion.write_bytes(original)
            with self.subTest(phase=phase,case='newer-incomplete'):
                pending=completion.parent.parent/'attempt-2/roster.json'
                pending.parent.mkdir()
                pending.write_bytes(completion.with_name('roster.json').read_bytes())
                with self.assertRaisesRegex(TaskError,'approval evidence'):
                    o.verify_signoff_evidence(orch.repo_root,orch.ssdlc_root,orch.state,orch.worktree_dir)
                pending.unlink();pending.parent.rmdir()
