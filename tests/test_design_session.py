import copy
import json
import tempfile
import unittest
from pathlib import Path

from ssdlc_design import DesignSession
from ssdlc_tasks import PlanError
from replay.repository_fixtures import finding, review






class DesignSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = {'context': {'requirement_catalogue': {'required_ids': ['AC-1', 'SEC-2']}, 'quality_contract': {}}}
        self.arch_calls, self.review_calls = [], []

    def session(self, reviews=(), **overrides):
        answers = iter(reviews)
        def architect(rnd, adr, history):
            self.arch_calls.append((rnd, adr, copy.deepcopy(history)))
            return f'# ADR round {rnd}'
        def reviewer(rnd, adr, history):
            self.review_calls.append((rnd, adr, copy.deepcopy(history)))
            return next(answers)
        args = dict(directory=self.root, inputs=self.inputs, architect=architect, reviewer=reviewer, budget=lambda: None)
        args.update(overrides)
        return DesignSession(**args)

    def state(self):
        return json.loads((self.root/'design-session/session.json').read_text())

    def test_resume_continues_previous_adr_and_cumulative_reviews_with_explicit_limit(self):
        with self.assertRaisesRegex(PlanError, 'round limit exhausted'):
            self.session([review([finding()])], initial_limit=1).run()
        for _ in range(2):
            with self.assertRaisesRegex(PlanError, 'round limit exhausted'):
                self.session(initial_limit=8).run()
        self.assertEqual(len(self.arch_calls), 1)
        closed = finding(status='RESOLVED', evidence='ADR now includes every failed-attempt log')
        new = finding('D2', new_issue_reason='MISSED_EARLIER', message='Exception translation is incomplete')
        done = {**new, 'status': 'RESOLVED', 'evidence': 'Confinement errors are now normalized into the stop diagnostic'}
        result = self.session([review([closed, new]), review([closed, done])], extend_limit=3).run()
        self.assertEqual(result[0], '# ADR round 3')
        self.assertEqual(self.arch_calls[1][1], '# ADR round 1')
        self.assertEqual(self.arch_calls[2][1], '# ADR round 2')
        self.assertEqual([len(c[2]) for c in self.review_calls], [0, 1, 2])
        self.assertIn('failed_attempts', self.arch_calls[2][2][0]['assessment']['findings'][0]['evidence'])
        self.assertEqual(self.state()['policy_updates'][0]['previous_limit'], 1)
        before = len(self.arch_calls), len(self.review_calls)
        self.session().run()
        self.assertEqual((len(self.arch_calls), len(self.review_calls)), before)

    def test_captured_adr_survives_interruption_without_regeneration(self):
        def budget():
            if self.arch_calls:
                raise RuntimeError('interrupted after ADR capture')
        with self.assertRaisesRegex(RuntimeError, 'after ADR'):
            self.session(budget=budget).run()
        self.assertEqual(self.state()['rounds'][0]['status'], 'ADR_READY')
        self.session([review()]).run()
        self.assertEqual(len(self.arch_calls), 1)
        self.assertEqual(self.review_calls[0][1], '# ADR round 1')

    def test_uncertain_architect_reservation_remains_consumed(self):
        def crash(*args):
            self.arch_calls.append(args)
            raise RuntimeError('architect interrupted')
        with self.assertRaises(RuntimeError):
            self.session(architect=crash).run()
        self.session([review()]).run()
        self.assertEqual([r['round'] for r in self.state()['rounds']], [1, 2])
        self.assertEqual(self.state()['rounds'][0]['status'], 'ARCH_UNCERTAIN')
        self.assertEqual(self.review_calls[0][0], 2)

    def test_review_interruption_reuses_adr_but_consumes_reserved_response(self):
        def crash(*args):
            self.review_calls.append(args)
            raise RuntimeError('review interrupted')
        with self.assertRaises(RuntimeError):
            self.session(reviewer=crash).run()
        self.session([review()]).run()
        self.assertEqual(len(self.arch_calls), 1)
        self.assertEqual(self.state()['rounds'][0]['review_reserved'], 2)
        self.assertEqual(self.state()['rounds'][0]['status'], 'APPROVED')

    def test_two_uncertain_reviews_cannot_trigger_an_unbounded_third_call(self):
        def crash(*args):
            self.review_calls.append(args)
            raise RuntimeError('uncertain')
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.session(reviewer=crash).run()
        for _ in range(2):
            with self.assertRaises(PlanError):
                self.session([review()]).run()
        self.assertEqual(len(self.review_calls), 2)
        self.assertEqual(len(self.arch_calls), 1)

    def test_invalid_review_is_corrected_once_on_the_same_adr_and_evidence_is_verified(self):
        self.session(['DESIGN_STATUS: APPROVED', review()]).run()
        self.assertEqual(len(self.arch_calls), 1)
        self.assertEqual(len(self.review_calls), 2)
        self.assertIn('review_protocol_errors', self.review_calls[1][2][-1])
        record = self.state()['rounds'][0]
        (self.root/'design-session'/record['review_response_1']).write_text('tampered')
        with self.assertRaisesRegex(PlanError, 'evidence changed'):
            self.session().run()

    def test_approval_cannot_drop_a_prior_open_blocker(self):
        with self.assertRaisesRegex(PlanError, 'review unavailable or invalid'):
            self.session([review([finding()]), review(), review()]).run()
        self.assertEqual(self.state()['rounds'][-1]['failure_kind'], 'REVIEW_PROTOCOL')
        self.assertNotEqual(self.state()['rounds'][-1]['status'], 'APPROVED')

    def test_identical_adr_and_unresolved_findings_stop_as_stagnation(self):
        with self.assertRaisesRegex(PlanError, 'STAGNATION'):
            self.session([review([finding()])] * 2, architect=lambda *args: '# identical ADR').run()
        count = len(self.review_calls)
        with self.assertRaisesRegex(PlanError, 'STAGNATION'):
            self.session().run()
        self.assertEqual(len(self.review_calls), count)

    def test_environment_prerequisite_does_not_start_another_architect_round(self):
        with self.assertRaisesRegex(PlanError, 'ENVIRONMENT_PREREQUISITE'):
            self.session([review([finding(category='ENVIRONMENT_PREREQUISITE')])]).run()
        self.assertEqual(len(self.arch_calls), 1)

    def test_changed_inputs_or_missing_evidence_block_before_calls(self):
        self.session([review()]).run()
        self.inputs['context']['requirements'] = 'changed'
        with self.assertRaisesRegex(PlanError, 'inputs changed'):
            self.session().run()
        self.inputs['context'].pop('requirements')
        (self.root/'design-session/inputs.json').unlink()
        with self.assertRaisesRegex(PlanError, 'input evidence disappeared'):
            self.session().run()
        self.assertEqual(len(self.arch_calls), 1)

    def test_limits_are_bounded_and_cannot_be_reduced(self):
        self.session([review()], initial_limit=2).run()
        with self.assertRaisesRegex(PlanError, 'Cannot reduce'):
            self.session(extend_limit=1).run()
        for value in (0, 9, True):
            with self.subTest(value=value), self.assertRaises(PlanError):
                self.session(extend_limit=value)

    def test_legacy_import_preserves_drafts_and_critiques_and_consumes_rounds(self):
        rounds = [{'adr': f'# Legacy {i}', 'review': f'Legacy review {i}: retained evidence and confinement blockers'}
                  for i in (1, 2, 3)]
        session = self.session(initial_limit=3)
        session.import_legacy(rounds)
        with self.assertRaisesRegex(PlanError, 'round limit exhausted'):
            session.run()
        self.assertEqual(self.arch_calls, [])
        self.session([review()], extend_limit=5).run()
        self.assertEqual(self.arch_calls[0][0], 4)
        self.assertEqual(self.arch_calls[0][1], '# Legacy 3')
        self.assertEqual(len(self.arch_calls[0][2]), 3)
        self.assertEqual(self.state()['origin'], 'explicit_legacy_import')

    def test_import_interruption_is_not_silently_treated_as_a_fresh_session(self):
        session = self.session()
        original = session._evidence
        def crash(*args):
            raise RuntimeError('interrupted import')
        session._evidence = crash
        with self.assertRaises(RuntimeError):
            session.import_legacy([{'adr': '# old', 'review': 'old critique'}])
        session._evidence = original
        with self.assertRaisesRegex(PlanError, 'import was interrupted'):
            session.run()
        self.assertEqual(self.arch_calls, [])

    def test_captured_approval_after_state_write_crash_is_reused(self):
        session = self.session([review()])
        original = session._save
        def crash(state):
            if state['rounds'] and 'review' in state['rounds'][-1] and state['rounds'][-1]['status'] == 'REVIEWING':
                original(state)
                raise RuntimeError('crash after captured review')
            original(state)
        session._save = crash
        with self.assertRaises(RuntimeError):
            session.run()
        self.session().run()
        self.assertEqual(len(self.arch_calls), 1)
        self.assertEqual(len(self.review_calls), 1)


from unittest.mock import patch
from orchestrator import SSDLCOrchestrator
from ssdlc_tasks import atomic_json, file_hash, git
from test_task_bootstrap import RepositoryTests


class DesignResumeTests(RepositoryTests):
    def seed(self):
        runtime = self.repo/'.ssdlc/.runtime'
        with patch('orchestrator.ProjectRuntimeIsolation.activate', return_value={
            'project_id': 'test', 'codex_home': str(runtime/'codex'), 'claude_home': str(runtime/'claude')
        }), patch('orchestrator.get_cli_version', return_value='TEST'):
            orch = SSDLCOrchestrator(self.repo, 'TKT-1', sync_remote=False)
        orch.state.update(task_breakdown=True, risk_tier='M', base_commit=self.base)
        for name, text in [('00-recon/context-and-blast-radius.md', 'Affected modules: app.py'),
                           ('01-po/requirements.md', 'AC-1: Behavior\nSEC-2: Retained evidence integrity')]:
            path = orch.ssdlc_root/name
            path.write_text(text)
            orch.record_artifact(name, path)
        atomic_json(orch.dir_recon/'quality-contract.json', self.quality)
        orch.record_artifact('00-recon/quality-contract.json', orch.dir_recon/'quality-contract.json')
        for gate in ('RECON_BLAST_RADIUS_GATE', 'PO_SPEC_GATE'):
            orch.record_gate(gate, True, 'Approved')
        return orch

    def dispatcher(self, role, *args, **kwargs):
        return ('mock', 'architect' if role.endswith('arch') else 'reviewer', 'low', 'test')

    def test_resume_only_design_supplies_source_and_continues_without_resetting(self):
        orch = self.seed()
        replies = iter(['# Original ADR', review([finding()]), '# Repaired ADR',
                        review([finding(status='RESOLVED', evidence='ADR enumerates failed attempts')])])
        payloads = []
        def invoke(engine, model, system, user, *args, **kwargs):
            self.assertEqual(orch.state['status'], 'IN_PROGRESS')
            self.assertNotIn('failure', orch.state)
            payload = json.loads(user)
            payloads.append(payload)
            source = payload['context']['source_evidence'][0]
            self.assertEqual(source['path'], 'app.py')
            self.assertEqual(source['sha256'], file_hash(self.repo/'app.py'))
            self.assertIn('1: value = 0', source['source'])
            return next(replies), 0, 0, 0, 'test'
        with patch('orchestrator.SquadDispatcher.dispatch_role', side_effect=self.dispatcher), patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke), patch.object(orch, 'run_phase_dev') as dev, patch.object(orch, 'run_phase_po') as po:
            with self.assertRaisesRegex(PlanError, 'round limit exhausted'):
                orch.resume_pair_design(round_limit=1)
            with self.assertRaisesRegex(PlanError, 'has not been independently approved'):
                orch._ensure_task_plan()
            result = orch.resume_pair_design(round_limit=3)
            dev.assert_not_called()
            po.assert_not_called()
        self.assertEqual(payloads[2]['previous_adr'], '# Original ADR')
        self.assertEqual(len(payloads[3]['review_history']), 1)
        self.assertEqual(orch.state['status'], 'DESIGN_READY')
        self.assertFalse(orch.worktree_dir.exists())
        self.assertEqual(result[0].read_text(), '# Repaired ADR')
        self.assertTrue(result[1].read_text().endswith('DESIGN_STATUS: APPROVED\n'))
        self.assertTrue(orch.state['gate_history'][-1]['passed'])

    def test_changed_requirements_or_unapproved_upstream_cannot_invoke_models(self):
        orch = self.seed()
        (orch.dir_po/'requirements.md').write_text('Changed')
        with patch('orchestrator.EngineRunner.invoke_role') as invoke:
            with self.assertRaisesRegex(PlanError, 'input changed'):
                orch.resume_pair_design()
            invoke.assert_not_called()
        orch.record_gate('PO_SPEC_GATE', False, 'Revision required')
        with self.assertRaisesRegex(PlanError, 'approved upstream gate'):
            orch.resume_pair_design()

    def test_existing_plan_prevents_design_rewrite(self):
        orch = self.seed()
        (orch.dir_design/'plan.json').write_text('{}')
        with patch('orchestrator.EngineRunner.invoke_role') as invoke:
            with self.assertRaisesRegex(PlanError, 'Planning or execution already started'):
                orch.resume_pair_design()
            invoke.assert_not_called()

    def test_actual_three_failed_rounds_are_imported_and_repaired_from_latest_draft(self):
        orch = self.seed()
        fixture = json.loads((Path(__file__).parent/'fixtures/rejected_pair_design_r5.json').read_text())
        directory = orch.dir_design/'rounds/legacy-r5'
        directory.mkdir(parents=True)
        for index, row in enumerate(fixture['rounds'], 1):
            for field, name in [('adr', f'adr-round-{index}.md'), ('review', f'security-review-round-{index}.md')]:
                path = directory/name
                path.write_text(row[field])
                orch.record_artifact(str(path.relative_to(orch.ssdlc_root)), path)
        original = {p.name: file_hash(p) for p in directory.iterdir()}
        seen = []
        def invoke(engine, model, system, user, *args, **kwargs):
            payload = json.loads(user)
            seen.append(payload)
            if model == 'architect':
                self.assertEqual(payload['previous_adr'], fixture['rounds'][-1]['adr'])
                self.assertEqual(len(payload['review_history']), 3)
                self.assertIn('failed_attempts', payload['review_history'][-1]['review'])
                self.assertIn('PlanError', payload['review_history'][-1]['review'])
                self.assertIn('timeout_sec', payload['review_history'][-1]['review'])
                return '# Repaired from actual round three', 0, 0, 0, 'test'
            return review(), 0, 0, 0, 'test'
        with patch('orchestrator.SquadDispatcher.dispatch_role', side_effect=self.dispatcher), patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
            orch.resume_pair_design(round_limit=5, import_legacy='rounds/legacy-r5')
        session = json.loads((orch.dir_design/'design-session/session.json').read_text())
        self.assertEqual(session['rounds'][-1]['round'], 4)
        self.assertEqual(session['round_limit'], 5)
        self.assertEqual(len(seen), 2)
        self.assertEqual({p.name: file_hash(p) for p in directory.iterdir()}, original)

    def test_changed_legacy_evidence_is_rejected_before_any_model_call(self):
        orch = self.seed()
        path = orch.dir_design/'rounds/old/adr-round-1.md'
        path.parent.mkdir(parents=True)
        path.write_text('# Original')
        orch.record_artifact(str(path.relative_to(orch.ssdlc_root)), path)
        path.write_text('# Unregistered modification')
        with patch('orchestrator.SquadDispatcher.dispatch_role', side_effect=self.dispatcher), patch('orchestrator.EngineRunner.invoke_role') as invoke:
            with self.assertRaisesRegex(PlanError, 'unregistered or changed'):
                orch.resume_pair_design(import_legacy='rounds/old')
            invoke.assert_not_called()

    def test_legacy_projection_cannot_silently_start_a_new_design_from_scratch(self):
        orch = self.seed()
        path = orch.dir_design/'adr.md'
        path.write_text('# Legacy ADR')
        orch.record_artifact('02-design/adr.md', path)
        with patch('orchestrator.SquadDispatcher.dispatch_role', side_effect=self.dispatcher), patch('orchestrator.EngineRunner.invoke_role') as invoke:
            with self.assertRaisesRegex(PlanError, 'explicit --import-legacy'):
                orch.resume_pair_design()
            invoke.assert_not_called()

    def test_partial_legacy_rounds_require_explicit_import_even_without_projections(self):
        orch = self.seed()
        path = orch.dir_design/'rounds/unfinished/adr-round-1.md'
        path.parent.mkdir(parents=True)
        path.write_text('# Captured before review interruption')
        orch.record_artifact(str(path.relative_to(orch.ssdlc_root)), path)
        with patch('orchestrator.SquadDispatcher.dispatch_role', side_effect=self.dispatcher), patch('orchestrator.EngineRunner.invoke_role') as invoke:
            with self.assertRaisesRegex(PlanError, 'explicit --import-legacy'):
                orch.resume_pair_design()
            invoke.assert_not_called()

    def test_same_model_cannot_review_its_own_design(self):
        orch = self.seed()
        with patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('mock', 'same-model', 'low', 'test')), patch('orchestrator.EngineRunner.invoke_role') as invoke:
            with self.assertRaisesRegex(PlanError, 'independent reviewer'):
                orch.resume_pair_design()
            invoke.assert_not_called()
