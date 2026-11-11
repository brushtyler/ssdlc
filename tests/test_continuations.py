"""I1 integration replay: real Git/journal, semantic verification, no providers."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from test_task_bootstrap import RepositoryTests
from ssdlc_continuations import (activate_generation, inspect_source, prepare_generation, verify_import, main, amendment_subject, impact_report)
from ssdlc_tasks import TaskError, TaskJournal, TaskScheduler, atomic_json, digest, file_hash, git
from ssdlc_transitions import TransitionError, runtime_root


class ContinuationTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.context = {'01-po/requirements.md': '# Requirements\nAC-1 SEC-1\n',
                        '02-design/adr.md': '# Design\n', '02-design/threat-model.md': '# Threats\n'}
        self.plan['tasks'].append(self.task('T2', ['AC-1'], [], ['next.py'], ['T1']))
        self.wt = self.worktree()
        scheduler = TaskScheduler(self.wt, self.artifacts / '03-dev/tasks', self.plan, self.quality,
                                  self.context, lambda *a: (_ for _ in ()).throw(TaskError('legacy interruption')),
                                  lambda *a: ('Ran 1 test\nOK', '', 0))
        with self.assertRaises(TaskError):
            scheduler.run(self.plan['tasks'])
        (self.artifacts / '03-dev/tasks/T1/attempt-1/developer.log').write_text('unconfirmed historical output')
        for ref, content in self.context.items():
            path = self.artifacts / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        atomic_json(self.artifacts / '00-recon/controller-contract.json', {'old_controller': 'frozen'})
        atomic_json(self.artifacts / '00-recon/quality-contract.json', self.quality)
        atomic_json(self.artifacts / '02-design/plan.json', self.plan)
        atomic_json(self.artifacts / '02-design/task-planning/session.json', {'used': 5, 'limits': {'repairs': 4}})
        self.outer = {'status': 'ESCALATED_TO_SUPERVISOR', 'ticket_id': 'TKT-1',
                      'base_commit': self.base, 'task_breakdown': True,
                      'ledger': [{'engine': 'claude', 'cost_usd': 9.26}],
                      'frozen_limits': {'max_attempts': 2, 'budget': 10},
                      'gate_history': [{'old_proof': True}], 'artifacts': {}}
        for path in self.artifacts.rglob('*'):
            if path.is_file() and 'worktree' not in path.parts and not '03-dev' in path.parts:
                ref = path.relative_to(self.artifacts).as_posix()
                self.outer['artifacts'][ref] = {'path': ref, 'sha256': file_hash(path)}
        atomic_json(self.artifacts / 'state.json', self.outer)
        self.prefix = (self.artifacts / '03-dev/tasks/task-journal.jsonl').read_bytes()
        self.new_plan = copy.deepcopy(self.plan)
        self.new_plan['plan_revision'] += 1
        self.snapshot = Path(self.tmp.name) / 'resolved-source'
        self.snapshot.mkdir()
        (self.snapshot / 'app.py').write_text('value = 1\n')
        self.manifest = {'schema_version': 1, 'base_commit': self.base, 'source_files': [
            {'path': 'app.py', 'sha256': file_hash(self.snapshot / 'app.py'), 'executable': False,
             'role': 'implementation', 'rationale': 'Preserved T1, reconciled with the target base'}]}
        self.identity = {'controller_sha256': 'c' * 64, 'environment': {'image_id': 'offline'}}
        self.target = self.repo / '.ssdlc/TKT-CONTINUATION'
        self.checks = []

    def verify(self, argv, cwd, timeout, workspace):
        self.checks.append(workspace)
        self.assertNotEqual(workspace, self.wt)
        self.assertTrue((workspace / 'app.py').read_text().startswith('value = '))
        return 'Ran 1 test\nOK', '', 0

    def prepare(self, **kwargs):
        args = dict(task_id='T1', plan=self.new_plan, quality=self.quality, context=self.context,
                    runtime_identity=self.identity, controller_snapshot={'new_controller': 'frozen'},
                    snapshot_root=self.snapshot, snapshot_manifest=self.manifest,
                    review=json.dumps({'verdict': 'APPROVED', 'findings': []}), verifier=self.verify)
        args.update(kwargs)
        if 'review' not in kwargs:
            subject = amendment_subject(inspect_source(self.artifacts), args['plan'], args['quality'],
                args['context'], args['snapshot_manifest'], impact_report(self.repo, self.snapshot, self.manifest))
            args['review'] = {'subject_sha256': digest(subject), 'reviewer': 'independent offline fixture',
                              'report': {'verdict': 'APPROVED', 'findings': []}}
            if subject['scope_diff']:
                args['review']['scope_diff'] = subject['scope_diff']
        return prepare_generation(self.artifacts, self.target, self.repo, **args)

    def authorize(self, prepared):
        p = prepared['proposal']
        result = {'schema_version': 1, 'transition_id': p['transition_id'], 'proposal_sha256': digest(p),
                'authorized': True, 'source_fenced': True,
                'old_controller_sha256': p['old_controller_sha256'], 'new_controller_sha256': p['new_controller_sha256']}
        if any(v['added'] for v in p.get('scope_diff', {}).values()):
            result['scope_diff_sha256'] = digest(p['scope_diff'])
        return result

    def activate(self, prepared):
        return activate_generation(self.target, prepared['preparation'], self.authorize(prepared))

    def verify_import(self):
        return verify_import(self.repo, 'TKT-CONTINUATION', verifier=self.verify,
                             runtime_identity=self.identity, controller_guard=lambda: None)

    def test_import_fresh_checks_no_model_no_reset_unknown_producer(self):
        before = inspect_source(self.artifacts)
        prepared = self.prepare()
        self.assertFalse(prepared['execution_authorized'])
        with self.assertRaisesRegex(TransitionError, 'pending'):
            runtime_root(self.target)
        projection = self.activate(prepared)
        self.assertEqual(projection['ledger'], self.outer['ledger'])
        self.assertEqual(projection['limits'], self.outer['frozen_limits'])
        root = runtime_root(self.target)
        new_journal = root / '03-dev/tasks/task-journal.jsonl'
        self.assertTrue(new_journal.read_bytes().startswith(self.prefix))
        receipt = json.loads((root / '03-dev/tasks/T1/attempt-1/capture/receipt.json').read_text())
        self.assertIsNone(receipt['producer']['exit_code'])
        self.assertEqual(receipt['producer']['termination'], 'imported')
        result = self.verify_import()
        self.assertEqual(result['completed'], ['T1'])
        self.assertEqual(result['pending'], ['T2'])
        self.assertEqual(result['model_dispatches'], 0)
        state = TaskJournal(root / '03-dev/tasks').peek()
        self.assertEqual(state['tasks']['T1']['attempts'], 1)
        self.assertEqual(state['tasks']['T2']['attempts'], 0)
        self.assertEqual(state['tasks']['T1']['completion_confirmation'], 'unconfirmed_import')
        self.assertIsNone(state['tasks']['T1']['producer']['exit_code'])
        self.assertEqual(state['tasks']['T1']['origin_record'], before['journal']['tasks']['T1'])
        count = len(self.checks)
        self.assertEqual(self.verify_import()['completed'], ['T1'])
        self.assertEqual(len(self.checks), count)
        self.assertEqual(self.activate(prepared), projection)
        self.assertEqual(inspect_source(self.artifacts), before)
        self.assertFalse(json.loads((root / 'state.json').read_text()).get('task_verified_head'))

    def test_failed_baseline_never_activates(self):
        with self.assertRaisesRegex(TransitionError, 'baseline failed'):
            self.prepare(verifier=lambda *a: ('Ran 1 test\nFAILED', '', 1))
        self.assertFalse((self.target / 'continuations/active.json').exists())
        self.assertEqual((self.artifacts / '03-dev/tasks/task-journal.jsonl').read_bytes(), self.prefix)

    def test_empty_baseline_never_activates(self):
        with self.assertRaisesRegex(TransitionError, 'baseline failed'):
            self.prepare(verifier=lambda *a: ('Ran 0 tests\nOK', '', 0))

    def test_mutating_baseline_never_activates(self):
        def mutate(argv, cwd, timeout, workspace):
            (workspace / 'app.py').write_text('changed')
            return 'Ran 1 test\nOK', '', 0
        with self.assertRaisesRegex(TransitionError, 'changed source'):
            self.prepare(verifier=mutate)

    def test_unknown_source_metadata_is_not_repaired(self):
        atomic_json(self.artifacts / 'state.json', {**self.outer, 'frozen_limits': None})
        self.assertIsNone(inspect_source(self.artifacts)['facts']['limits'])

    def test_new_base_and_scope_require_explicit_snapshot(self):
        self.manifest['base_commit'] = 'a' * 40
        with self.assertRaisesRegex(TransitionError, 'new base'):
            self.prepare()

    def test_activation_rejects_seal_tampering_and_original_changes(self):
        prepared = self.prepare()
        prep = Path(prepared['preparation'])
        seal = json.loads((prep / 'runtime-seal.json').read_text())
        seal.pop('state.json')
        atomic_json(prep / 'runtime-seal.json', seal)
        with self.assertRaisesRegex(TransitionError, 'seal changed'):
            self.activate(prepared)

    def test_source_mutation_refuses_activation(self):
        prepared = self.prepare()
        (self.artifacts / '03-dev/tasks/T1/attempt-1/developer.log').write_text('changed')
        with self.assertRaisesRegex(TransitionError, 'Source changed'):
            self.activate(prepared)

    def test_import_verification_failure_keeps_candidate_and_stops(self):
        prepared = self.prepare()
        self.activate(prepared)
        with patch.object(self, 'verify', return_value=('Ran 1 test\nFAILED', '', 1)):
            result = self.verify_import()
        self.assertEqual(result['status'], 'CONTINUATION_CHECKS_FAILED')
        root = runtime_root(self.target)
        state = TaskJournal(root / '03-dev/tasks').peek()
        self.assertEqual(state['tasks']['T1']['attempts'], 1)
        self.assertTrue((root / '03-dev/tasks/T1/attempt-1/capture/receipt.json').is_file())
        self.assertEqual(self.verify_import()['model_dispatches'], 0)

    def test_changed_runtime_and_controller_block_verification(self):
        prepared = self.prepare()
        self.activate(prepared)
        self.identity = {**self.identity, 'environment': {'image_id': 'changed'}}
        with self.assertRaises(TaskError):
            self.verify_import()
        state = json.loads((runtime_root(self.target) / 'state.json').read_text())
        self.assertEqual(state['status'], 'CONTINUATION_BLOCKED')

    def test_native_model_entrypoints_refuse_active_or_preparing_generation(self):
        from orchestrator import resume_compatibility, SSDLCOrchestrator
        # The canonical ticket path is used by production launchers.
        self.target = self.artifacts
        prepared = self.prepare()
        self.assertFalse(resume_compatibility(self.repo, 'TKT-1', 'run', '03-dev')['ok'])
        self.activate(prepared)
        self.assertFalse(resume_compatibility(self.repo, 'TKT-1', 'run', '03-dev')['ok'])
        with self.assertRaisesRegex(TaskError, 'verification only'):
            SSDLCOrchestrator(self.repo, 'TKT-1', 'test')

    def test_cli_refuses_invalid_ticket_before_constructor(self):
        with self.assertRaises(SystemExit):
            main(['continuation-inspect', '--ticket', '../invalid', '--source', str(self.artifacts)])

    def test_review_is_bound_to_snapshot_and_impact(self):
        with self.assertRaisesRegex(TransitionError, 'review does not bind'):
            self.prepare(review={'subject_sha256': 'a' * 64, 'reviewer': 'independent',
                                 'report': {'verdict': 'APPROVED', 'findings': []}})
        self.assertFalse((self.target / 'continuations').exists())

    def test_future_acceptance_file_is_not_executed_as_baseline(self):
        # I1 structural validation accepts an unborn acceptance file; only the
        # quality baseline is invoked before its implementation exists.
        self.new_plan['tasks'][0]['target_files'].append('test_new.py')
        self.new_plan['tasks'][0]['verification']['checks'][0]['argv'] = ['python3', '-m', 'unittest', 'test_new']
        prepared = self.prepare()
        self.assertEqual(len(self.checks), 1)
        self.activate(prepared)
        self.assertEqual(self.verify_import()['completed'], ['T1'])

    def test_tier_s_requires_no_fabricated_threat_model(self):
        path = self.artifacts / '02-design/threat-model.md'
        path.unlink()
        self.outer['artifacts'].pop('02-design/threat-model.md')
        self.outer['risk_tier'] = 'S'
        atomic_json(self.artifacts / 'state.json', self.outer)
        self.context['02-design/threat-model.md'] = ''
        # Rebind a native fixture to the real S context, retaining its attempt.
        journal = TaskJournal(self.artifacts / '03-dev/tasks')
        old = journal.load()
        old['inputs_sha256'] = digest({'plan': self.plan, 'quality': self.quality, 'context': self.context})
        journal.append('S_FIXTURE_CONTEXT', old)
        prepared = self.prepare()
        self.activate(prepared)
        self.assertFalse((runtime_root(self.target) / '02-design/threat-model.md').exists())
        self.assertEqual(self.verify_import()['completed'], ['T1'])

    def test_activation_crash_replays_same_generation(self):
        prepared = self.prepare()
        with self.assertRaises(OSError):
            activate_generation(self.target, prepared['preparation'], self.authorize(prepared),
                fault=lambda p: (_ for _ in ()).throw(OSError('crash')) if p == 'after_activation' else None)
        projection = self.activate(prepared)
        self.assertEqual(projection['transition_id'], prepared['proposal']['transition_id'])
        self.assertEqual(self.verify_import()['completed'], ['T1'])

    def test_guard_interruption_resumes_captured_without_attempt_reset(self):
        prepared = self.prepare()
        self.activate(prepared)
        def stop_after_adoption(argv, cwd, timeout, workspace):
            raise TaskError('environment changed during verification')
        with self.assertRaises(TaskError):
            verify_import(self.repo, 'TKT-CONTINUATION', verifier=stop_after_adoption,
                          runtime_identity=self.identity, controller_guard=lambda: None)
        self.assertEqual(self.verify_import()['completed'], ['T1'])
        state = TaskJournal(runtime_root(self.target) / '03-dev/tasks').peek()
        self.assertEqual(state['tasks']['T1']['attempts'], 1)
        self.assertEqual(len(state['tasks']['T1']['capture_verifications']), 2)

    def test_repeated_verification_interruptions_exhaust_persistent_bound(self):
        prepared = self.prepare()
        self.activate(prepared)
        for attempt in range(3):
            with self.assertRaises(TaskError):
                verify_import(self.repo, 'TKT-CONTINUATION', verifier=lambda *a: (_ for _ in ()).throw(TaskError('stop')),
                              runtime_identity=self.identity, controller_guard=lambda: None)
        with self.assertRaisesRegex(TaskError, 'allowance exhausted'):
            self.verify_import()
        state = TaskJournal(runtime_root(self.target) / '03-dev/tasks').peek()
        self.assertEqual(state['tasks']['T1']['attempts'], 1)
        self.assertEqual(len(state['tasks']['T1']['capture_verifications']), 3)

    def test_review_baseline_candidate_proofs_do_not_become_current_gate_history(self):
        prepared = self.prepare()
        self.activate(prepared)
        root = runtime_root(self.target)
        state = json.loads((root / 'state.json').read_text())
        self.assertEqual(state['gate_history'], [])
        self.assertEqual(json.loads((root / 'history/state.json').read_text())['gate_history'], self.outer['gate_history'])
        old_log = root / 'history/03-dev/tasks/T1/attempt-1/developer.log'
        self.assertEqual(old_log.read_text(), 'unconfirmed historical output')
        quality = json.loads((root / '00-recon/quality-contract.json').read_text())
        self.assertNotEqual(quality['baseline'], self.quality['baseline'])
        self.assertEqual(state['ledger'], self.outer['ledger'])

    def test_native_cli_preview_prepare_activate_verify_never_constructs_model_controller(self):
        import contextlib
        import io
        from orchestrator import main as orchestrator_main, EngineRunner, SSDLCOrchestrator
        from ssdlc_controller import execution_contract
        self.target = self.artifacts
        files = Path(self.tmp.name) / 'inputs'
        files.mkdir()
        for name, document in [('plan', self.new_plan), ('quality', self.quality), ('manifest', self.manifest)]:
            atomic_json(files / (name + '.json'), document)
        common = ['--repo', str(self.repo), '--ticket', 'TKT-1']
        prepare = ['--source', str(self.artifacts), '--task', 'T1', '--plan', str(files / 'plan.json'),
                   '--quality', str(files / 'quality.json'), '--snapshot', str(self.snapshot),
                   '--manifest', str(files / 'manifest.json')]
        def invoke(words):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = orchestrator_main(words)
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue().splitlines()[-1])
        with patch.object(SSDLCOrchestrator, '__init__', side_effect=AssertionError('No model controller')):
            preview = invoke(['continuation-preview'] + common + prepare)
            atomic_json(files / 'review.json', {'subject_sha256': preview['subject_sha256'],
                'reviewer': 'independent offline fixture', 'report': {'verdict': 'APPROVED', 'findings': []}})
            with patch.object(EngineRunner, '_should_spawn_role_container', return_value=True), \
                 patch.object(SSDLCOrchestrator, '_verification_environment', return_value=self.identity['environment'] | {'mode': 'role_container'}), \
                 patch('ssdlc_controller.controller_snapshot', return_value={'new_controller': 'frozen'}), \
                 patch('ssdlc_controller.assert_controller'), \
                 patch.object(EngineRunner, 'run_sandboxed_cmd', side_effect=lambda argv, cwd, timeout, verification_worktree: self.verify(argv, cwd, timeout, verification_worktree)):
                prepared = invoke(['continuation-prepare'] + common + prepare + ['--review', str(files / 'review.json')])
                prep = Path(prepared['preparation'])
                proposal = json.loads((prep / 'proposal.json').read_text())
                atomic_json(files / 'authorization.json', self.authorize({'proposal': proposal}))
                invoke(['continuation-activate'] + common + ['--preparation', str(prep), '--authorization', str(files / 'authorization.json')])
                result = invoke(['continuation-verify'] + common)
                self.assertEqual(result['completed'], ['T1'])
                self.assertEqual(result['model_dispatches'], 0)

    def test_missing_attribution_and_completed_history_are_explicit_refusals(self):
        from ssdlc_continuations import import_projection
        source = inspect_source(self.artifacts)
        source['journal']['tasks']['T1']['attempts'] = 0
        with self.assertRaisesRegex(TaskError, 'attributed'):
            import_projection(source, plan=self.new_plan, quality=self.quality, context=self.context,
                              runtime_identity=self.identity, task_id='T1')
        source['journal']['tasks']['T1'].update(status='COMPLETED', attempts=1)
        with self.assertRaisesRegex(TaskError, 'Completed-checkpoint'):
            import_projection(source, plan=self.new_plan, quality=self.quality, context=self.context,
                              runtime_identity=self.identity, task_id='T1')

    def test_dirty_target_and_capture_version_migration_refuse(self):
        (self.repo / 'app.py').write_text('unrelated uncommitted source')
        with self.assertRaisesRegex(TaskError, 'must be clean'):
            self.prepare()
        self.assertEqual(self.checks, [])
        from ssdlc_continuations import import_projection
        source = inspect_source(self.artifacts)
        source['journal']['candidate_isolation'] = 1
        with self.assertRaisesRegex(TaskError, 'verification allowances cannot reset'):
            import_projection(source, plan=self.new_plan, quality=self.quality, context=self.context,
                              runtime_identity=self.identity, task_id='T1')

    def test_native_refusal_has_durable_audit_without_pending_generation(self):
        import contextlib
        import io
        from orchestrator import EngineRunner
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            code = main(['continuation-verify', '--repo', str(self.repo), '--ticket', 'TKT-1'])
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(output.getvalue())['audit_recorded'])
        records = list((self.artifacts / 'continuation-refusals').glob('*.json'))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())['model_dispatches'], 0)
        self.assertFalse((self.artifacts / 'continuations').exists())
