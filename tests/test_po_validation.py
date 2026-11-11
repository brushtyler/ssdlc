"""PO approval and deterministic, archived format recovery through real APIs."""
import contextlib
import io
import json
from unittest import TestCase
from unittest.mock import patch

import orchestrator
from ssdlc_catalogues import CatalogueError, requirement_catalogue
from ssdlc_requirements import prepare_requirements
from ssdlc_tasks import PlanError, file_hash
import test_task_bootstrap as bootstrap


RAW = ('# Requirements\n### AC-1: Parent\n'
       '- **AC-1.1 (handoff)** Preserve every clause and refer to AC-1.1.\n'
       '- **AC-1.2:** Refuse uncertain ownership.\n'
       '### SEC-1: Safety\n- **SEC-1.1:** Never expose credentials.\n')


class PreparationTests(TestCase):
    def test_explicit_child_ids_preserve_all_content_and_references(self):
        result, mapping, catalogue = prepare_requirements(RAW)
        expected = RAW.replace('AC-1.1', 'AC-1-1').replace('AC-1.2', 'AC-1-2').replace('SEC-1.1', 'SEC-1-1')
        self.assertEqual(result, expected)
        self.assertEqual(mapping, {'AC-1.1': 'AC-1-1', 'AC-1.2': 'AC-1-2', 'SEC-1.1': 'SEC-1-1'})
        self.assertEqual(catalogue['required_ids'], ['AC-1', 'AC-1-1', 'AC-1-2', 'SEC-1', 'SEC-1-1'])
        self.assertEqual(prepare_requirements(result)[0:2], (result, {}))
        with self.assertRaises(CatalogueError):
            requirement_catalogue(RAW)

    def test_fences_and_undeclared_prefixes_are_not_rewritten(self):
        extra = ('Prose mentions AC-1.10, which is not AC-1.1.\n'
                 '```python\nx = "AC-1.1"\n```\n~~~\nAC-1.1\n~~~\n')
        result, _, _ = prepare_requirements(RAW + extra)
        self.assertIn('Prose mentions AC-1.10, which is not AC-1-1.', result)
        self.assertIn('```python\nx = "AC-1.1"\n```', result)
        self.assertIn('~~~\nAC-1.1\n~~~', result)

    def test_canonical_collision_and_ambiguous_aliases_block(self):
        for text in (RAW + '\nAC-1-1: Different definition\n',
                     'AC-1.1-2: One\nAC-1-1.2: Two\nSEC-1: Safety'):
            with self.subTest(text=text), self.assertRaises(CatalogueError):
                prepare_requirements(text)

    def test_duplicates_other_malformed_ids_and_missing_families_block(self):
        for text in ('AC-1: One\nAC-1: Two\nSEC-1: Safety',
                     '### ac-1: Wrong case\nSEC-1: Safety',
                     'AC-1_bad: Unsupported\nSEC-1: Safety',
                     'We mention AC-1 and SEC-1 but define neither.',
                     'AC-1: No security definitions'):
            with self.subTest(text=text), self.assertRaises(CatalogueError):
                prepare_requirements(text)


class POTests(bootstrap.RepositoryTests):
    make_orchestrator = bootstrap.OrchestratorBootstrapTests.make_orchestrator

    def setUp(self):
        super().setUp()
        self.orch = self.make_orchestrator()
        self.orch.state.update(task_breakdown=True, risk_tier='M', current_phase='01-po')
        (self.orch.dir_recon / 'context-and-blast-radius.md').write_text('Recon')
        self.orch._save_state()

    def generate(self, text):
        with patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('fake', 'po', 'low', 'test')), \
             patch('orchestrator.EngineRunner.invoke_role', return_value=(text, .14, 10, 20, 'fake')) as engine:
            path = self.orch.run_phase_po('Goal')
        return path, engine

    def seed(self, text=RAW):
        path = self.orch.dir_po / 'requirements.md'
        path.write_text(text)
        self.orch.record_artifact('01-po/requirements.md', path)
        self.orch.state['ledger'] = [{'phase': '01-po', 'cost_usd': .14, 'status': 'PASS'}]
        self.orch.state.update(current_phase='02-design', status='FAILED',
                               failure={'phase': '02-design', 'message': 'Invalid requirement definitions'})
        self.orch._save_state()
        return path

    def test_actual_po_archives_raw_then_approves_strict_canonical_spec(self):
        path, engine = self.generate(RAW)
        self.assertEqual(engine.call_count, 1)
        self.assertEqual(path.read_text(), prepare_requirements(RAW)[0])
        self.assertEqual(self.orch.state['ledger'][0]['status'], 'GENERATED')
        self.assertTrue(self.orch.state['gate_history'][-1]['passed'])
        record = json.loads((self.orch.ssdlc_root / self.orch.state['po_validation_ref']).read_text())
        self.assertEqual((self.orch.ssdlc_root / record['original_ref']).read_text(), RAW)
        self.assertEqual(record['normalized_sha256'], file_hash(path))
        for ref, meta in self.orch.state['artifacts'].items():
            self.assertEqual(file_hash(self.orch.ssdlc_root / ref), meta['sha256'])

    def test_po_cannot_pass_with_only_mentions_or_duplicate_definitions(self):
        for text in ('Mentions AC-1 and SEC-1, no definitions.', 'AC-1: First\nAC-1: Second\nSEC-1: Safety'):
            with self.subTest(text=text), self.assertRaises(PlanError):
                self.generate(text)
            self.assertFalse(self.orch.state['gate_history'][-1]['passed'])
            self.assertEqual((self.orch.dir_po / 'requirements.md').read_text(), text)
        self.assertEqual(len(self.orch.state['ledger']), 2)

    def test_resume_is_model_free_preserves_cost_and_is_idempotent(self):
        path = self.seed()
        ledger = list(self.orch.state['ledger'])
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=AssertionError('AI call')):
            self.orch.resume_po_validation()
            files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.orch.dir_po.rglob('*') if p.is_file()}
            state = self.orch.state_file.read_bytes()
            self.orch = self.make_orchestrator()
            self.orch.resume_po_validation()
        self.assertEqual(files, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})
        self.assertEqual(self.orch.state_file.read_bytes(), state)
        self.assertEqual(self.orch.state['ledger'], ledger)
        self.assertEqual(self.orch.state['status'], 'PO_READY')
        self.assertNotIn('failure', self.orch.state)
        self.assertEqual(path.read_text(), prepare_requirements(RAW)[0])

    def test_crash_after_replacement_reconciles_only_registered_intent(self):
        path = self.seed()
        class Crash(BaseException):
            pass
        record = self.orch.record_artifact
        def interrupted(ref, target):
            if ref == '01-po/requirements.md':
                raise Crash()
            record(ref, target)
        with patch.object(self.orch, 'record_artifact', side_effect=interrupted), self.assertRaises(Crash):
            self.orch.resume_po_validation()
        self.assertEqual(path.read_text(), prepare_requirements(RAW)[0])
        self.orch = self.make_orchestrator()
        with patch('orchestrator.EngineRunner.invoke_role', side_effect=AssertionError('AI call')):
            self.orch.resume_po_validation()
        self.assertEqual(file_hash(path), self.orch.state['artifacts']['01-po/requirements.md']['sha256'])
        self.assertEqual(len(self.orch.state['ledger']), 1)

    def test_external_mutation_and_tampered_archive_do_not_get_approved(self):
        path = self.seed()
        path.write_text('AC-1: External change\nSEC-1: Safety')
        with self.assertRaisesRegex(PlanError, 'outside a recorded'):
            self.orch.resume_po_validation()
        path.write_text(RAW)
        self.orch.resume_po_validation()
        record = json.loads((self.orch.ssdlc_root / self.orch.state['po_validation_ref']).read_text())
        (self.orch.ssdlc_root / record['original_ref']).write_text('altered')
        with self.assertRaisesRegex(PlanError, 'evidence is missing or changed'):
            self.orch.resume_po_validation()
        self.assertFalse(self.orch.state['gate_history'][-1]['passed'])
        with patch('orchestrator.EngineRunner.invoke_role') as engine:
            with self.assertRaises(PlanError):
                self.orch.run_phase_pair_design()
        engine.assert_not_called()

    def test_resume_refuses_after_design_started(self):
        self.seed()
        session = self.orch.dir_design / 'design-session/session.json'
        session.parent.mkdir()
        session.write_text('{}')
        with self.assertRaisesRegex(PlanError, 'before design'):
            self.orch.resume_po_validation()

    def test_cli_uses_existing_spec_without_provider_or_legacy_changes(self):
        self.seed()
        with patch('orchestrator.SSDLCOrchestrator', return_value=self.orch), \
             patch('orchestrator.EngineRunner.invoke_role', side_effect=AssertionError('AI call')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(orchestrator.main(['po', '--repo', str(self.repo), '--ticket', 'TKT-1']), 0)
        self.orch.state.update(task_breakdown=False, po_validation_ref=None)
        with patch.dict('os.environ', {'SSDLC_ENABLE_TASK_BREAKDOWN': 'false'}):
            _, engine = self.generate('AC-1: Required behavior\nSEC-1: Required security')
        self.assertEqual(engine.call_count, 1)
        self.assertTrue(self.orch.state['gate_history'][-1]['passed'])
