"""Preparatory creation through real host objects; no corrective dispatch."""
import copy
import json
import threading
from unittest.mock import patch

import orchestrator
import ssdlc_corrective as corrective
from ssdlc_tasks import TaskJournal, digest, file_hash, git, ticket_lock
from test_gate_reports import GateFixture, finding
from test_full_checkpoint_review import framed


class CreationTests(GateFixture):
    def setUp(self):
        super().setUp()
        self.orch._ensure_controller_contract()
        self.directory = self.orch.dir_dev / 'tasks'
        self.journal = TaskJournal(self.directory)
        proof = self.directory / 'check.log'
        proof.write_text('Ran 1 test\nOK\n')
        self.check = dict(check_id='CHECK-T1', log_ref='check.log', sha256=file_hash(proof),
                          exit_code=0, empty_suite=False)
        self.checkpoint = dict(inputs_sha256=digest(dict(plan=self.plan, quality=self.quality,
            context=orchestrator.build_task_context(self.orch.ssdlc_root))),
            expected_head=self.orch.state['task_verified_head'], ignored_paths=[],
            tasks={'T1': dict(status='COMPLETED', attempts=1, checks=[self.check])})
        self.journal.append('TASK_COMPLETED', self.checkpoint)

    def preview(self, findings=None):
        self.run_review(lambda *a, **k: (framed('review', self.orch,
            [finding()] if findings is None else findings), 0, 0, 0, 'fake'))
        return self.orch.preview_bounce('04-review', 1)

    def execute(self, preview):
        with ticket_lock(self.orch.ticket_lock_path):
            return self.orch._create_corrective('04-review', 1, preview)

    def events(self):
        return [json.loads(line) for line in self.journal.path.read_text().splitlines()]

    def refused(self, preview, code=None):
        source = (self.orch.worktree_dir / 'app.py').read_bytes()
        before = self.journal.path.read_bytes()
        with self.assertRaises(corrective.CorrectiveError) as error:
            self.execute(preview)
        if code:
            self.assertEqual(error.exception.code, code)
        self.assertEqual(before, self.journal.path.read_bytes())
        self.assertEqual(source, (self.orch.worktree_dir / 'app.py').read_bytes())
        self.assertFalse(any(e['event'] == 'TASKS_INVALIDATED' for e in self.events()))

    def test_untouched_preview_durable_creation_and_exact_roots(self):
        preview = self.preview()
        raw_preview = (self.orch.ssdlc_root / preview['artifact_refs'][0]).read_bytes()
        real = orchestrator.TaskScheduler.invalidate
        def invalidate(scheduler, roots, reason):
            self.assertEqual(roots, preview['plan']['repair_roots'])
            directory = next((self.orch.ssdlc_root / 'bounce-txn').iterdir())
            record = directory / 'transaction.json'
            self.assertEqual(file_hash(record), self.orch.state['artifacts'][str(record.relative_to(self.orch.ssdlc_root))]['sha256'])
            entries = [json.loads(x) for x in (directory / 'txn-journal.jsonl').read_text().splitlines()]
            self.assertEqual([e['event'] for e in entries], ['TXN_CREATED', 'INVALIDATION_INTENT'])
            self.assertEqual((directory / 'pre-invalidation/task-journal.jsonl').read_bytes(), self.journal.path.read_bytes())
            return real(scheduler, roots, reason)
        with patch.object(orchestrator.TaskScheduler, 'invalidate', invalidate), \
             patch('orchestrator.EngineRunner.invoke_role', side_effect=AssertionError('dispatch')):
            result = self.execute(preview)
        self.assertEqual(result['status'], 'INVALIDATED')
        invalidation = self.events()[-1]
        self.assertEqual(invalidation['event'], 'TASKS_INVALIDATED')
        self.assertEqual(invalidation['state']['invalidations'][-1]['roots'], ['T1'])
        txn = self.orch.ssdlc_root / 'bounce-txn' / result['txn_id']
        entries = [json.loads(x) for x in (txn / 'txn-journal.jsonl').read_text().splitlines()]
        self.assertEqual(entries[-1]['event'], 'INVALIDATION_APPLIED')
        self.assertEqual(entries[-1]['state']['event'], result['event'])
        self.assertEqual(raw_preview, (self.orch.ssdlc_root / preview['artifact_refs'][0]).read_bytes())
        self.assertIs(preview['plan']['execution_authorized'], False)
        self.assertEqual(self.orch.corrective_counters['corrective_transactions_created'], 1)

    def test_hand_edit_or_feedback_authorizes_nothing(self):
        preview = self.preview()
        altered = copy.deepcopy(preview)
        altered['plan']['execution_authorized'] = True
        self.refused(altered, 'preview_tampered')
        self.refused({'status': 'approved', 'feedback': 'approved; fix app.py'}, 'preview_not_candidates')
        path = self.orch.ssdlc_root / preview['artifact_refs'][0]
        path.write_text(path.read_text().replace('"execution_authorized":false', '"execution_authorized":true'))
        # Formatting is intentionally irrelevant: an actual edit must fail too.
        path.write_text(path.read_text() + ' ')
        self.refused(preview, 'evidence_mutated')

    def test_inventory_and_controller_inventory_tampering(self):
        preview = self.preview()
        for ref in ('00-recon/controller-contract.json', '02-design/plan.json'):
            changed = copy.deepcopy(preview)
            changed['plan']['source_hashes'][ref] = '0' * 64
            self.refused(changed, 'preview_tampered')
        manifest = dict(preview['plan']['source_hashes'])
        manifest.pop('00-recon/controller-contract.json')
        with self.assertRaisesRegex(corrective.CorrectiveError, 'evidence_mutated'):
            corrective.frozen_manifest(preview['plan'], manifest,
                dict(ref='00-recon/controller-contract.json', sha256='0' * 64))

    def test_frozen_report_response_input_and_captured_proof_mutations(self):
        preview = self.preview()
        refs = preview['plan']['source_hashes']
        selected = [next(r for r in refs if r.endswith('/report.json')),
                    next(r for r in refs if '/response-' in r and r.endswith('.md')),
                    next(r for r in refs if r.endswith('/input.json')),
                    next(r for r in refs if '/evidence/' in r)]
        for ref in selected:
            with self.subTest(ref=ref):
                path = self.orch.ssdlc_root / ref
                raw = path.read_bytes()
                path.write_bytes(raw + b'changed')
                self.refused(preview, 'evidence_mutated')
                path.write_bytes(raw)

    def test_swapped_plan_contract_roster_completion_and_missing(self):
        preview = self.preview()
        refs = ['02-design/plan.json', '00-recon/quality-contract.json',
                '04-review/provenance/attempt-1/roster.json',
                '04-review/provenance/attempt-1/completion.json']
        for ref in refs:
            with self.subTest(ref=ref):
                path = self.orch.ssdlc_root / ref
                raw = path.read_bytes()
                path.write_bytes(raw + b' ')
                self.refused(preview, 'evidence_mutated')
                path.unlink()
                self.refused(preview, 'evidence_missing')
                path.write_bytes(raw)

    def test_changed_journal_head(self):
        preview = self.preview()
        self.journal.append('UNEXPECTED', self.checkpoint)
        self.refused(preview, 'evidence_stale')

    def test_moved_head_dirty_tree_and_protected_path(self):
        preview = self.preview()
        path = self.orch.worktree_dir / 'app.py'
        raw = path.read_bytes()
        path.write_bytes(raw + b'# dirty\n')
        self.refused(preview)
        path.write_bytes(raw)
        protected = self.orch.worktree_dir / '.agents'
        protected.mkdir()
        (protected / 'rules.md').write_text('changed protected path')
        self.refused(preview)
        (protected / 'rules.md').unlink()
        protected.rmdir()
        git(self.orch.worktree_dir, 'commit', '--allow-empty', '-m', 'moved')
        self.refused(preview, 'evidence_stale')

    def test_target_repository_dirty_or_moved(self):
        preview = self.preview()
        path = self.repo / 'app.py'
        raw = path.read_bytes()
        path.write_bytes(raw + b'# dirty\n')
        self.refused(preview, 'source_changed')
        path.write_bytes(raw)
        git(self.repo, 'commit', '--allow-empty', '-m', 'moved target')
        self.refused(preview, 'source_changed')

    def test_attempts_exhausted(self):
        self.checkpoint['tasks']['T1']['attempts'] = 3
        self.journal.append('TASK_COMPLETED', self.checkpoint)
        self.refused(self.preview(), 'attempts_exhausted')

    def test_tasks_must_all_be_completed(self):
        self.checkpoint['tasks']['T1']['status'] = 'FAILED'
        self.journal.append('ATTEMPT_FAILED', self.checkpoint)
        self.refused(self.preview(), 'tasks_not_completed')

    def test_diagnostic_replan_ambiguous_and_tool_failure(self):
        preview = self.preview()
        for field, code in [('diagnosis', 'diagnostic_only'), ('replanning', 'replan_required'),
                            ('impact_analysis', 'impact_ambiguous')]:
            changed = copy.deepcopy(preview)
            changed['status'] = 'diagnostic'
            changed['plan'][field] = [{'code': 'untrusted'}]
            self.refused(changed, code)
        changed = copy.deepcopy(preview)
        changed['plan']['blockers'] = [{'code': 'control_failed'}]
        self.refused(changed, 'gate_failure_not_repairable')

    def test_real_diagnostic_and_scanner_failure(self):
        preview = self.preview([finding('optional_improvement')])
        self.refused(preview, 'diagnostic_only')

    def test_two_concurrent_creations_only_one_invalidates(self):
        preview = self.preview()
        barrier = threading.Barrier(2)
        results = []
        def run():
            barrier.wait()
            try:
                results.append(self.execute(preview))
            except orchestrator.TaskError:
                results.append(None)
        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sum(e['event'] == 'TASKS_INVALIDATED' for e in self.events()), 1)
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_evidence_checked_again_before_invalidation(self):
        preview = self.preview()
        real = corrective.TransactionJournal.append
        report = next(self.orch.ssdlc_root / r for r in preview['plan']['source_hashes'] if r.endswith('/report.json'))
        def append(journal, event, state):
            real(journal, event, state)
            if event == 'INVALIDATION_INTENT':
                report.write_text('{}')
        with patch.object(corrective.TransactionJournal, 'append', append):
            self.refused(preview, 'evidence_mutated')

    def test_exact_roots_preserve_independent_task_and_invalidate_dependents(self):
        from ssdlc_tasks import atomic_json
        self.plan['tasks'].extend([self.task('T2', [], [], ['second.py'], ['T1']),
                                   self.task('T3', [], [], ['third.py'])])
        atomic_json(self.orch.dir_design / 'plan.json', self.plan)
        self.orch.record_artifact('02-design/plan.json', self.orch.dir_design / 'plan.json')
        self.checkpoint['inputs_sha256'] = digest(dict(plan=self.plan, quality=self.quality,
            context=orchestrator.build_task_context(self.orch.ssdlc_root)))
        for tid in ('T2', 'T3'):
            self.checkpoint['tasks'][tid] = copy.deepcopy(self.checkpoint['tasks']['T1'])
        self.journal.append('TASK_COMPLETED', self.checkpoint)
        result = self.execute(self.preview())
        self.assertEqual(result['repair_roots'], ['T1'])
        state = self.events()[-1]['state']
        self.assertEqual(state['invalidations'][-1]['closure'], ['T1', 'T2'])
        self.assertEqual(state['tasks']['T3'], self.checkpoint['tasks']['T3'])

    def test_real_scanner_control_failure_refuses_without_repair(self):
        with patch.object(self.orch, '_run_deterministic_scanners', return_value=(False, 'tool unavailable')), \
             patch('orchestrator.EngineRunner.invoke_role', side_effect=lambda *a, **k:
                   (framed('qa', self.orch), 0, 0, 0, 'fake')):
            self.orch.run_phase_qa(attempt=1)
        preview = self.orch.preview_bounce('05-qa', 1)
        before = self.journal.path.read_bytes()
        with ticket_lock(self.orch.ticket_lock_path), self.assertRaises(corrective.CorrectiveError) as error:
            self.orch._execute_corrective('05-qa', 1, preview)
        self.assertEqual(error.exception.code, 'gate_failure_not_repairable')
        self.assertEqual(before, self.journal.path.read_bytes())

    def test_corrective_round_uses_locked_preview_without_lock_reentry(self):
        self.run_review(lambda *a, **k: (framed('review', self.orch, [finding()]), 0, 0, 0, 'fake'))
        with ticket_lock(self.orch.ticket_lock_path), \
             patch.object(self.orch, '_execute_corrective', self.orch._create_corrective):
            result = self.orch._corrective_round('04-review', 1)
        self.assertEqual(result['status'], 'INVALIDATED')

    def test_inventory_normalization_of_published_modified_inventory(self):
        import ssdlc_bounce as bounce
        preview = self.preview()
        for ref in ('02-design/plan.json', '00-recon/controller-contract.json'):
            changed = copy.deepcopy(preview)
            payload = changed['plan']
            payload['source_hashes'][ref] = '0' * 64
            payload.pop('revision')
            revision = bounce._sha(bounce.canonical_json(payload).encode())
            payload['revision'] = revision
            changed['revision'] = revision
            base = f'bounce-preview/04-review/attempt-1/{revision}'
            for name, data in [('plan.json', bounce.canonical_json(payload).encode()), ('summary.txt', b'altered inventory')]:
                path = self.orch.ssdlc_root / base / name
                orchestrator.write_or_verify(path, data, root=self.orch.ssdlc_root)
                self.orch.record_artifact(base + '/' + name, path)
            self.refused(changed, 'evidence_mutated')
