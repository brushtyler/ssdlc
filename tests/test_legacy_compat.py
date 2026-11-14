"""Compatibility reads never create execution authority or mutate history."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import orchestrator as o
import ssdlc_supervisor as supervisor
from ssdlc_design import read_history_rounds
from ssdlc_tasks import TaskJournal, digest, PlanError
from replay.repository_fixtures import RepositoryTests


class CompatibilityTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.root = self.repo / '.ssdlc/OLD'
        self.root.mkdir(parents=True)
        self.state = {'task_breakdown': True, 'artifacts': {}, 'ledger': [{'cost_usd': 2}]}
        self.save()

    def save(self):
        (self.root / 'state.json').write_text(json.dumps(self.state))

    def snapshot(self):
        return {str(p.relative_to(self.repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                if p.is_file() else None for p in self.repo.rglob('*')}

    def seal(self, empty_adr=False):
        plan, quality = copy.deepcopy(self.plan), copy.deepcopy(self.quality)
        plan['ticket_id'] = 'OLD'
        for name, value in [('02-design/plan.json', plan), ('00-recon/quality-contract.json', quality)]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
            self.state['artifacts'][name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if empty_adr:
            path = self.root / '02-design/adr.md'
            path.write_text('')
            self.state['artifacts']['02-design/adr.md'] = {'sha256': hashlib.sha256(b'').hexdigest()}
        context = o.build_task_context(self.root)
        TaskJournal(self.root / '03-dev/tasks').append('INITIALIZED', {
            'inputs_sha256': digest(dict(plan=plan, quality=quality, context=context)),
            'expected_head': 'abc', 'tasks': {'T1': {'status': 'COMPLETED', 'attempts': 2}}})
        self.state.update(task_verified_head='abc', status='AWAITING_SUPERVISOR_SIGNOFF')
        self.save()

    def test_refusals_before_every_constructor(self):
        for variant in ({}, {'task_breakdown': True}, {'approved': True}, {'task_engine_v1': True}):
            self.state.update(variant)
            if variant.get('task_engine_v1'):
                (self.root / 'worktree').mkdir(exist_ok=True)
            self.save()
            before = self.snapshot()
            for words in [['run', '--ticket', 'OLD', '--goal', 'g'], ['approve', 'OLD'],
                          ['reject', 'OLD', '--reason', 'no'], ['plan', '--ticket', 'OLD'],
                          ['design', '--ticket', 'OLD'], ['design', '--ticket', 'OLD', '--import-legacy', 'rounds/x'],
                          ['po', '--ticket', 'OLD'],
                          ['resume', '--ticket', 'OLD', '--entrypoint', 'run', '--goal', 'g']]:
                with patch.object(o, 'SSDLCOrchestrator', side_effect=AssertionError('constructor')) as ctor:
                    # Parser uses class constants, so preserve these without constructing.
                    ctor.PHASE_ORDER = ['00-recon', '03-dev']
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(o._foreground_main(words + ['--repo', str(self.repo)]), 23)
                self.assertEqual(before, self.snapshot())
            args = dict(repo=str(self.repo), ticket='OLD', goal='g', tier='L')
            for method in (supervisor.start, supervisor.resume):
                result = method(args)
                self.assertEqual(result['status_reason'], 'legacy_not_executable')
                self.assertEqual(supervisor.exit_code(result), 23)
                self.assertEqual(before, self.snapshot())

    def test_schema_invalid_registered_inputs_are_refused_before_mutation(self):
        self.seal()
        plan_path = self.root / '02-design/plan.json'
        quality_path = self.root / '00-recon/quality-contract.json'
        originals = {p: p.read_bytes() for p in (plan_path, quality_path)}
        journal_path = self.root / '03-dev/tasks/task-journal.jsonl'
        original_journal = journal_path.read_bytes()
        for path, invalid in ((plan_path, {}), (quality_path, {}),
                              (plan_path, dict(json.loads(originals[plan_path]), tasks=[])),
                              (quality_path, dict(json.loads(originals[quality_path]), checks=[]))):
            with self.subTest(document=path.name, invalid=invalid):
                for target, data in originals.items():
                    target.write_bytes(data)
                path.write_text(json.dumps(invalid))
                for target in originals:
                    name = str(target.relative_to(self.root))
                    self.state['artifacts'][name]['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
                self.save()
                # Bind even the malformed input into an otherwise valid chain;
                # registry and digest agreement must not substitute for schema.
                journal_path.unlink()
                TaskJournal(self.root / '03-dev/tasks').append('INITIALIZED', {
                    'inputs_sha256': digest(dict(plan=json.loads(plan_path.read_bytes()),
                                                quality=json.loads(quality_path.read_bytes()),
                                                context=o.build_task_context(self.root))),
                    'expected_head': 'abc',
                    'tasks': {'T1': {'status': 'COMPLETED', 'attempts': 2}}})
                before = self.snapshot()
                for command in ('run', 'resume', 'approve', 'reject', 'plan', 'design', 'po'):
                    self.assertEqual(o.execution_compatibility(self.repo, 'OLD', command)['code'],
                                     'legacy_not_executable')
                for words in (['run', '--ticket', 'OLD', '--goal', 'g'], ['approve', 'OLD'],
                              ['reject', 'OLD', '--reason', 'no'], ['plan', '--ticket', 'OLD'],
                              ['design', '--ticket', 'OLD'], ['po', '--ticket', 'OLD']):
                    with patch.object(o, 'SSDLCOrchestrator', side_effect=AssertionError('constructor')) as ctor:
                        ctor.PHASE_ORDER = ['00-recon', '03-dev']
                        with contextlib.redirect_stderr(io.StringIO()):
                            self.assertEqual(o._foreground_main(words + ['--repo', str(self.repo)]), 23)
                for method in (supervisor.start, supervisor.resume):
                    result = method(dict(repo=str(self.repo), ticket='OLD', goal='g', tier='L'))
                    self.assertEqual(result['status_reason'], 'legacy_not_executable')
                    self.assertEqual(supervisor.exit_code(result), 23)
                self.assertEqual(before, self.snapshot())
                self.assertFalse((self.root / 'ticket.lock').exists())
                self.assertFalse(list(self.repo.rglob('audit.jsonl')))
        journal_path.write_bytes(original_journal)

    def test_context_and_sealed_history_are_read_only(self):
        self.seal()
        before = self.snapshot()
        for command in ('run', 'reject'):
            self.assertEqual(o.execution_compatibility(self.repo, 'OLD', command)['cls'], 'sealed_task')
        self.assertFalse(o.execution_compatibility(self.repo, 'OLD', 'approve')['ok'])
        self.assertEqual(before, self.snapshot())
        original = o.build_task_context(self.root)
        with patch.object(o, 'build_task_context', return_value={name: '' for name in original}):
            self.assertFalse(o.execution_compatibility(self.repo, 'OLD', 'run')['ok'])
        self.assertEqual(o.OPTIONAL_FALLBACK, '(Skipped in Tier S)')
        self.assertEqual(o.SSDLCOrchestrator._read_optional.__defaults__[0], o.OPTIONAL_FALLBACK)

    def test_empty_context_and_symlink(self):
        self.seal(empty_adr=True)
        self.assertTrue(o.execution_compatibility(self.repo, 'OLD', 'run')['ok'])
        path = self.root / '02-design/adr.md'
        path.unlink()
        path.symlink_to(self.root / 'state.json')
        self.assertFalse(o.execution_compatibility(self.repo, 'OLD', 'run')['ok'])

    def test_journal_matrix_and_approval(self):
        self.seal()
        journal = self.root / '03-dev/tasks/task-journal.jsonl'
        original = journal.read_bytes()
        for data in (b'', b'{', original.replace(b'INITIALIZED', b'FORGED')):
            journal.write_bytes(data)
            before = self.snapshot()
            self.assertFalse(o.execution_compatibility(self.repo, 'OLD', 'run')['ok'])
            self.assertEqual(before, self.snapshot())
        journal.write_bytes(original)
        self.state['task_verified_head'] = 'forged'
        self.save()
        self.assertFalse(o.execution_compatibility(self.repo, 'OLD', 'approve')['ok'])

    def test_missing_state_with_old_execution_evidence_is_not_a_new_run(self):
        self.seal()
        (self.root/'state.json').unlink()
        before=self.snapshot()
        for command in ('run','start','approve','reject','plan','design'):
            result=o.execution_compatibility(self.repo,'OLD',command)
            self.assertFalse(result['ok'])
            self.assertEqual(result['reason'],'missing_state_with_execution_evidence')
        self.assertEqual(before,self.snapshot())
        self.assertFalse((self.root/'state.json').exists())

    def test_new_and_pre_execution(self):
        self.assertEqual(o.execution_compatibility(self.repo, 'NEW', 'resume')['code'], 'no_such_run')
        self.assertTrue(o.execution_compatibility(self.repo, 'NEW', 'run')['ok'])
        self.state['task_engine_v1'] = True
        self.save()
        self.assertEqual(o.execution_compatibility(self.repo, 'OLD', 'design')['cls'], 'pre_execution')
        self.assertFalse(o.startup_preflight(self.repo, 'OLD', 'run', {'from_phase': '03-dev'})['ok'])

    def test_symlinked_ticket_and_dangling_state_are_refused(self):
        (self.repo / '.ssdlc/ALIAS').symlink_to(self.root, target_is_directory=True)
        self.assertFalse(o.execution_compatibility(self.repo, 'ALIAS', 'run')['ok'])
        (self.root / 'state.json').unlink()
        (self.root / 'state.json').symlink_to(self.repo / 'absent-state')
        self.assertFalse(o.execution_compatibility(self.repo, 'OLD', 'run')['ok'])

    def test_read_only_status_and_monitor_without_supervisor_records(self):
        before = self.snapshot()
        for result in (supervisor.status(self.repo, 'OLD'), supervisor.monitor(self.repo, 'OLD', timeout=0)):
            self.assertEqual(supervisor.exit_code(result), 0)
            self.assertEqual(result['status'], 'historical')
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / 'ticket.lock').exists())

    def test_history_import_confined_and_read_only(self):
        directory = self.root / '02-design/rounds/x'
        directory.mkdir(parents=True)
        path = directory / 'adr-round-1.md'
        path.write_text('historical design')
        self.state['artifacts']['02-design/rounds/x/adr-round-1.md'] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.save()
        before = self.snapshot()
        self.assertEqual(read_history_rounds(self.repo, 'OLD', 'rounds/x'), [{'adr': 'historical design'}])
        self.assertEqual(before, self.snapshot())
        for rel in ('../OLD', '/tmp', 'rounds/../x'):
            with self.assertRaises(PlanError):
                read_history_rounds(self.repo, 'OLD', rel)
        (directory.parent / 'escape').symlink_to(directory, target_is_directory=True)
        with self.assertRaises(PlanError):
            read_history_rounds(self.repo, 'OLD', 'rounds/escape')
