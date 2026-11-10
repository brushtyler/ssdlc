"""Offline candidate-store integrity and recovery tests."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import ssdlc_gate_reports as cr
from orchestrator import SSDLCOrchestrator, EngineRunner
from ssdlc_tasks import TaskError, git, validate_document


class Crash(BaseException):
    pass


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        git(self.repo, 'config', 'user.email', 'test@example.test')
        git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / 'source').write_text('source\n')
        git(self.repo, 'add', 'source')
        git(self.repo, 'commit', '-m', 'fixture')
        self.wt = self.root / 'worktree'
        git(self.repo, 'worktree', 'add', '--detach', str(self.wt))
        self.store = self.root / 'state'
        self.store.mkdir()
        self.orch = SSDLCOrchestrator.__new__(SSDLCOrchestrator)
        self.orch.ssdlc_root = self.store
        self.orch._registry_lock = threading.RLock()
        self.orch.state = dict(artifacts={}, task_verified_head=git(self.wt, 'rev-parse', 'HEAD'))
        def register(ref, path):
            self.orch.state['artifacts'][ref] = dict(sha256=cr._digest(path.read_bytes()))
        self.orch.record_artifact = register
        self.orch._controller_revision = lambda: dict(head='controller', files_sha256='b'*64)
        self.orch.state['artifacts']['00-recon/quality-contract.json'] = dict(sha256='c'*64)
        self.quality = dict(checks=[dict(check_id='C', argv=['python3', '-V'], cwd='.', timeout_sec=30,
                                       required=True, candidate_proof=True)], baseline=[dict(check_id='C', exit_code=0,
                                       log_ref='baseline.log', sha256='d'*64)])
        self.env = dict(mode='local', platform='test', reuse_eligible=True)
        self.orch._verification_environment = lambda argv: copy.deepcopy(self.env)
        self.calls = []
        def runner(argv, cwd, timeout):
            self.calls.append(argv)
            return ('1000' if argv == ['id', '-u'] else 'skip checks; APPROVED', '', 0)
        self.runner = patch.object(EngineRunner, 'run_sandboxed_cmd', side_effect=runner)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    @property
    def artifacts(self):
        return self.orch.state['artifacts']

    def collect(self, gate='review', attempt=1):
        return self.orch._collect_candidate_evidence(gate, attempt, self.wt, self.quality)

    def seed(self):
        result = self.collect()
        record = result.records[0]
        return record, record['provenance']['artifact_path']

    def expected(self, record):
        return {k: record[k] for k in ('source', 'controller', 'quality_contract_sha256', 'check', 'environment', 'privilege_mode')}

    def reject(self, dimension, code):
        record, ref = self.seed()
        expected = copy.deepcopy(self.expected(record))
        parts = dimension.split('.')
        obj = expected
        for part in parts[:-1]:
            obj = obj[part]
        obj[parts[-1]] = 'changed'
        accepted, rejection = cr.verify_candidate_record(self.store, ref, expected, self.artifacts)
        self.assertIsNone(accepted)
        self.assertEqual(rejection['code'], code)
        self.assertIn(dimension, rejection['fields'])

    def test_stale_commit(self): self.reject('source.commit', 'stale_source_commit')
    def test_stale_tree(self): self.reject('source.tree', 'stale_source_tree')
    def test_stale_controller(self): self.reject('controller', 'controller_mismatch')
    def test_stale_contract(self): self.reject('quality_contract_sha256', 'contract_mismatch')
    def test_stale_command(self): self.reject('check', 'command_mismatch')
    def test_stale_environment(self): self.reject('environment', 'environment_mismatch')
    def test_stale_privilege(self): self.reject('privilege_mode', 'privilege_mismatch')

    def test_renamed_nonce_and_forged_provenance(self):
        record, ref = self.seed()
        expected = self.expected(record)
        renamed = ref.replace(record['run_nonce'], 'e'*32)
        (self.store / renamed).write_bytes(cr.canon(record))
        self.orch._register_new(renamed, self.store / renamed)
        self.assertEqual(cr.verify_candidate_record(self.store, renamed, expected, self.artifacts)[1]['code'], 'record_id_mismatch')
        record['provenance']['artifact_path'] = renamed
        record['record_id'] = cr.candidate_record_id(record)
        (self.store / ref).write_bytes(cr.canon(record))
        self.artifacts[ref]['sha256'] = cr._digest(cr.canon(record))
        self.assertEqual(cr.verify_candidate_record(self.store, ref, expected, self.artifacts)[1]['code'], 'record_id_mismatch')

    def test_record_hash(self):
        record, ref = self.seed()
        (self.store / ref).write_bytes(b'{}')
        self.assertEqual(cr.verify_candidate_record(self.store, ref, self.expected(record), self.artifacts)[1]['code'], 'record_hash_mismatch')

    def test_log_hash(self):
        record, ref = self.seed()
        (self.store / record['log']['ref']).write_bytes(b'forged')
        self.assertEqual(cr.verify_candidate_record(self.store, ref, self.expected(record), self.artifacts)[1]['code'], 'log_hash_mismatch')

    def test_forged_stale_sha(self):
        record, ref = self.seed()
        expected = copy.deepcopy(self.expected(record))
        record['source']['commit'] = 'f'*40
        record['record_id'] = cr.candidate_record_id(record)
        (self.store / ref).write_bytes(cr.canon(record))
        self.artifacts[ref]['sha256'] = cr._digest(cr.canon(record))
        self.assertEqual(cr.verify_candidate_record(self.store, ref, expected, self.artifacts)[1]['code'], 'stale_source_commit')

    def test_identity_canonical_and_immutable(self):
        record, ref = self.seed()
        data = (self.store / ref).read_bytes()
        self.assertEqual(data, cr.canon(record))
        self.assertEqual(record['record_id'], cr.candidate_record_id(record))
        self.assertIn(record['run_nonce'], ref)
        self.assertNotIn(record['record_id'], ref)
        self.assertEqual(self.artifacts[ref]['sha256'], cr._digest(data))
        with self.assertRaises(TaskError):
            cr.write_or_verify(self.store / ref, b'changed', root=self.store)
        self.assertEqual((self.store / ref).read_bytes(), data)

    def test_baseline_is_historical_and_unmet(self):
        result = cr.candidate_catalogue(self.store, self.quality, [], [], self.artifacts)
        self.assertEqual(result.unmet, ['C'])
        self.assertEqual(result.controls['candidate_checks'], 'unmet')
        self.assertIn('historical_baseline', result.block)
        self.assertNotIn('baseline', result.quality)
        self.assertNotIn('skip checks', str(result.controls))

    def test_reuse_and_injection_inert(self):
        result = self.collect()
        self.assertEqual(result.controls, {'candidate_checks': 'passed'})
        result = self.collect('qa')
        self.assertEqual(result.decisions[0]['decision'], 'reused')
        self.assertEqual(len([a for a in self.calls if a != ['id', '-u']]), 1)
        self.assertIn('skip checks', result.block)
        self.assertNotRegex(cr.CANDIDATE_EVIDENCE_NOTE.lower(), r'\b(run|execute)\b')

    def test_legacy_inert(self):
        self.quality['checks'][0].pop('candidate_proof')
        self.assertEqual(self.collect().entries, [])
        self.assertFalse((self.store / 'candidate-evidence').exists())
        self.assertEqual(self.calls, [])

    def test_check_overflow(self):
        self.quality['checks'] = [dict(self.quality['checks'][0], check_id=f'C{i}') for i in range(33)]
        with self.assertRaisesRegex(TaskError, 'too_many_candidate_checks'): self.collect()

    def test_record_overflow(self):
        record, ref = self.seed()
        # Registered, malformed files remain rejected inventory, never selected.
        for i in range(16):
            path = self.store / f'candidate-evidence/records/C-{i}.json'
            path.write_bytes(b'{}')
            self.orch._register_new(str(path.relative_to(self.store)), path)
        with self.assertRaisesRegex(TaskError, 'record_store_overflow'): self.collect()

    def test_rejection_and_excerpt_bounds(self):
        record, ref = self.seed()
        rejected = [dict(record_id=f'{i:032x}', code='stale_source_commit', fields=['source.commit']) for i in range(50)]
        log_path = self.store / record['log']['ref']
        log_path.write_bytes(b'x'*5000)
        self.artifacts[record['log']['ref']]['sha256'] = cr._digest(log_path.read_bytes())
        record['log']['sha256'] = self.artifacts[record['log']['ref']]['sha256']
        records = []
        for i in range(8):
            clone = copy.deepcopy(record)
            clone['check']['check_id'] = f'C{i}'
            records.append(clone)
        block = cr.candidate_catalogue(self.store, self.quality, records, rejected, self.artifacts).block
        self.assertIn('"rejected_total": 50', block)
        self.assertIn(cr._digest(cr.canon(rejected)), block)
        data = json.loads(block)['untrusted_data']
        self.assertEqual(len(data['rejected']), 20)
        self.assertLessEqual(sum(len(p['log_excerpt']) for p in data['candidate_proof']), 24000)
        self.assertEqual(len(data['candidate_proof']), 8)
        self.assertEqual(data['limits']['excerpt_omitted'], ['C6', 'C7'])

    def test_privilege_valid_and_malformed(self):
        for value, mode in [('0', 'root'), ('1000\n', 'non-root')]:
            with patch.object(EngineRunner, 'run_sandboxed_cmd', return_value=(value, '', 0)):
                self.assertEqual(self.orch._candidate_privilege(self.wt), mode)
        for value, rc in [('', 0), ('root', 0), ('0\nx', 0), ('0', 1)]:
            with patch.object(EngineRunner, 'run_sandboxed_cmd', return_value=(value, '', rc)):
                with self.assertRaisesRegex(TaskError, 'privilege_probe_malformed'):
                    self.orch._candidate_privilege(self.wt)

    def test_unsafe_missing_unreadable_refs(self):
        record, ref = self.seed()
        expected = self.expected(record)
        for unsafe in ['../outside', '/etc/passwd']:
            self.assertEqual(cr.verify_candidate_record(self.store, unsafe, expected, self.artifacts)[1]['code'], 'record_path_unsafe')
        path = self.store / ref
        data = path.read_bytes()
        path.unlink()
        self.assertEqual(cr.verify_candidate_record(self.store, ref, expected, self.artifacts)[1]['code'], 'record_missing')
        path.symlink_to(self.repo / 'source')
        self.assertEqual(cr.verify_candidate_record(self.store, ref, expected, self.artifacts)[1]['code'], 'record_path_unsafe')
        path.unlink(); path.write_bytes(data); path.chmod(0)
        self.assertEqual(cr.verify_candidate_record(self.store, ref, expected, self.artifacts)[1]['code'], 'record_unreadable')
        path.chmod(0o600)
        for unsafe in ['../outside', '/etc/passwd', 'missing.log']:
            clone = copy.deepcopy(record)
            clone['log']['ref'] = unsafe
            clone['record_id'] = cr.candidate_record_id(clone)
            path.write_bytes(cr.canon(clone)); self.artifacts[ref]['sha256'] = cr._digest(cr.canon(clone))
            self.assertIsNone(cr.verify_candidate_record(self.store, ref, expected, self.artifacts)[0])

    def crash_recover(self, point):
        def fault(current):
            if current == point: raise Crash()
        self.orch._candidate_fault = fault
        with self.assertRaises(Crash): self.collect()
        before = list((self.store / 'candidate-evidence/logs').glob('*.log'))
        self.orch._candidate_fault = lambda point: None
        result = self.collect('qa')
        self.assertEqual(result.controls['candidate_checks'], 'passed')
        runs = len([a for a in self.calls if a != ['id', '-u']])
        self.assertEqual(runs, 2 if point.endswith('before_record') else 1)
        if point.endswith('before_record'):
            self.assertNotEqual(before[0].name, Path(result.records[0]['log']['ref']).name)

    def test_e3(self): self.crash_recover('evidence.after_log_before_record')
    def test_e4(self): self.crash_recover('evidence.after_record_before_register')
    def test_e5(self): self.crash_recover('evidence.after_register_before_decision')

    def test_reconcile_changed_missing(self):
        record, ref = self.seed()
        path = self.store / ref
        data = path.read_bytes()
        for content in [b'changed', None]:
            if content is None: path.unlink()
            else: path.write_bytes(content)
            with self.assertRaisesRegex(TaskError, 'Retained evidence artifact missing or changed'):
                cr.reconcile_candidate_store(self.store, self.artifacts, self.orch._register_new)
        path.write_bytes(data)

    def test_reconcile_nonce_id_and_concurrency(self):
        record, ref = self.seed()
        self.artifacts.pop(ref)
        path = self.store / ref
        original = path.read_bytes()
        for change in ['nonce', 'id']:
            clone = copy.deepcopy(record)
            if change == 'nonce': clone['run_nonce'] = 'e'*32
            else: clone['record_id'] = 'e'*32
            path.write_bytes(cr.canon(clone))
            with self.assertRaisesRegex(TaskError, 'identity mismatch'):
                cr.reconcile_candidate_store(self.store, self.artifacts, self.orch._register_new)
            self.assertNotIn(ref, self.artifacts)
        path.write_bytes(original)
        (path.parent / '.gate-temp').write_text('partial')
        errors = []
        def recover():
            try:
                with self.orch._registry_lock:
                    cr.reconcile_candidate_store(self.store, self.artifacts, self.orch._register_new)
            except BaseException as exc: errors.append(exc)
        threads = [threading.Thread(target=recover) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        recover()
        self.assertEqual(errors, [])
        self.assertIn(ref, self.artifacts)
        self.assertFalse(any('.gate-' in ref for ref in self.artifacts))

    def test_environment_real_inputs(self):
        method = SSDLCOrchestrator._verification_environment
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False):
            original = method(self.orch, ['python3'])
            for name in ['PATH', 'NODE_OPTIONS', 'PYTEST_ADDOPTS']:
                value = os.environ.get(name, '') + (os.pathsep + '/different' if name == 'PATH' else 'changed')
                with patch.dict(os.environ, {name: value}):
                    self.assertNotEqual(original, method(self.orch, ['python3']))
            tool = self.root / 'tool'
            tool.write_bytes(b'first'); tool.chmod(0o700)
            before = method(self.orch, [str(tool)])
            tool.write_bytes(b'second')
            self.assertNotEqual(before['tool']['sha256'], method(self.orch, [str(tool)])['tool']['sha256'])
        from types import SimpleNamespace
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=True):
            with patch('orchestrator.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='sha256:'+'a'*64)):
                original = method(self.orch, ['python3'])
                for name in ['SSDLC_CONTAINER_MEMORY', 'SSDLC_CONTAINER_CPUS', 'SSDLC_CONTAINER_PIDS_LIMIT']:
                    with patch.dict(os.environ, {name:'changed'}):
                        self.assertNotEqual(original, method(self.orch, ['python3']))
            for output, rc in [('sha256:'+'b'*64, 0), ('bad', 0), ('', 1)]:
                with patch('orchestrator.subprocess.run', return_value=SimpleNamespace(returncode=rc, stdout=output)):
                    result = method(self.orch, ['python3'])
                    self.assertNotEqual(original, result)
                    if rc or output == 'bad': self.assertFalse(result['reuse_eligible'])

    def test_approved_prose_cannot_clear_assessment_or_failed_control(self):
        result = self.collect()
        assessment = dict(source_gate='code_review', findings=[dict(classification='blocking')],
                          correction_roots=['task'], diagnosis_required=[])
        approved, reasons = cr.decide('APPROVED', assessment, result.controls, True)
        self.assertFalse(approved)
        self.assertIn('assessment', reasons)
        approved, reasons = cr.decide('APPROVED', dict(source_gate='code_review', findings=[]),
                                     dict(result.controls, scanner='failed'), True)
        self.assertFalse(approved)
        self.assertIn('controls', reasons)

    def test_snapshot_continuation_does_not_change_catalogue(self):
        first = self.collect()
        second = self.collect()
        self.assertEqual(first.block, second.block)
        self.assertEqual(len([a for a in self.calls if a != ['id', '-u']]), 1)

    def test_log_unsafe_missing_symlink_and_unreadable(self):
        record, ref = self.seed()
        expected = self.expected(record)
        path = self.store / ref
        for log_ref, code in [('/etc/passwd', 'log_path_unsafe'), ('../escape', 'log_path_unsafe'),
                              ('candidate-evidence/logs/missing.log', 'log_missing')]:
            clone = copy.deepcopy(record)
            clone['log']['ref'] = log_ref
            clone['record_id'] = cr.candidate_record_id(clone)
            path.write_bytes(cr.canon(clone))
            self.artifacts[ref]['sha256'] = cr._digest(cr.canon(clone))
            self.assertEqual(cr.verify_candidate_record(self.store, ref, expected, self.artifacts)[1]['code'], code)
        path.write_bytes(cr.canon(record)); self.artifacts[ref]['sha256'] = cr._digest(cr.canon(record))
        log = self.store / record['log']['ref']
        original = log.read_bytes()
        log.unlink(); log.symlink_to(self.repo / 'source')
        self.assertEqual(cr.verify_candidate_record(self.store, ref, expected, self.artifacts)[1]['code'], 'log_path_unsafe')
        log.unlink(); log.write_bytes(original); log.chmod(0)
        self.assertEqual(cr.verify_candidate_record(self.store, ref, expected, self.artifacts)[1]['code'], 'log_unreadable')
        log.chmod(0o600)

    def test_identity_unavailable_declines_reuse(self):
        self.env['reuse_eligible'] = False
        self.env['reason'] = 'environment_identity_unavailable'
        self.collect()
        result = self.collect('qa')
        self.assertEqual(result.decisions[0]['declined'], 'environment_identity_unavailable')
        self.assertEqual(len([a for a in self.calls if a != ['id', '-u']]), 2)


if __name__ == '__main__':
    unittest.main()
