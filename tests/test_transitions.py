import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ssdlc_tasks import digest
from ssdlc_transitions import TransitionError, TransitionStore, source_binding


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'historical'
        self.source.mkdir()
        (self.source / 'journal.jsonl').write_text('historical journal\n')
        self.old = {'ledger': [{'cost_usd': 1.25}], 'limits': {'attempts': 3},
                    'counters': {'design_rounds': 6}, 'tasks': {'T1': {'attempts': 1, 'status': 'RUNNING'}}}
        self.new = copy.deepcopy(self.old)
        self.new['tasks']['T1']['status'] = 'UNVERIFIED_IMPORT'
        self.artifacts = {'original-state.json': b'original bytes'}
        self.proposal = {'transition_id': 'a' * 32, 'old_controller_sha256': 'b' * 64,
                         'new_controller_sha256': 'c' * 64, 'projection_sha256': digest(self.new),
                         'source_files': source_binding(self.source, ['journal.jsonl']),
                         'artifacts': {k: hashlib.sha256(v).hexdigest() for k, v in self.artifacts.items()}}
        self.authorization = {'schema_version': 1, 'transition_id': 'a' * 32,
                              'proposal_sha256': digest(self.proposal), 'authorized': True,
                              'source_fenced': True, 'old_controller_sha256': 'b' * 64,
                              'new_controller_sha256': 'c' * 64}

    def publish(self, fault=lambda point: None):
        return TransitionStore(self.root / 'transitions', fault).publish(
            self.source, self.proposal, self.authorization, self.old, self.new, self.artifacts)

    def test_success_preserves_source_and_consumed_allowances(self):
        before = (self.source / 'journal.jsonl').read_bytes()
        self.assertEqual(self.publish(), self.new)
        self.assertEqual(self.publish(), self.new)
        self.assertEqual((self.source / 'journal.jsonl').read_bytes(), before)

    def test_crash_boundaries_are_idempotent(self):
        for point in ('intent_published', 'generation_staged', 'before_activation', 'after_activation'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                store = TransitionStore(Path(tmp), lambda p: (_ for _ in ()).throw(OSError('crash')) if p == point else None)
                with self.assertRaises(OSError):
                    store.publish(self.source, self.proposal, self.authorization, self.old, self.new, self.artifacts)
                store.fault = lambda p: None
                self.assertEqual(store.publish(self.source, self.proposal, self.authorization, self.old, self.new, self.artifacts), self.new)

    def test_refuses_counter_reset_before_publication(self):
        self.new['tasks']['T1']['attempts'] = 0
        with self.assertRaisesRegex(TransitionError, 'attempts'):
            self.publish()
        self.assertFalse((self.root / 'transitions').exists())

    def test_refuses_changed_authorization(self):
        self.authorization['source_fenced'] = False
        with self.assertRaisesRegex(TransitionError, 'authorization'):
            self.publish()

    def test_refuses_source_mutation(self):
        (self.source / 'journal.jsonl').write_text('changed')
        with self.assertRaisesRegex(TransitionError, 'Historical source'):
            self.publish()

    def test_active_payload_tampering_is_detected(self):
        self.publish()
        (self.root / 'transitions' / ('a' * 32) / 'original-state.json').write_text('changed')
        with self.assertRaisesRegex(TransitionError, 'artifacts changed'):
            TransitionStore(self.root / 'transitions').load()

    def test_changed_staged_payload_is_not_overwritten(self):
        with self.assertRaises(OSError):
            self.publish(lambda p: (_ for _ in ()).throw(OSError('crash')) if p == 'generation_staged' else None)
        (self.root / 'transitions' / ('a' * 32) / 'original-state.json').write_text('changed')
        with self.assertRaisesRegex(TransitionError, 'changed'):
            self.publish()

    def test_source_symlink_is_refused(self):
        (self.source / 'journal.jsonl').unlink()
        (self.source / 'journal.jsonl').symlink_to(self.root / 'outside')
        with self.assertRaisesRegex(TransitionError, 'Symlink'):
            self.publish()

    def test_scope_children_compare_exact_parent_and_cannot_roll_back(self):
        self.publish()
        store = TransitionStore(self.root / 'transitions')
        parent = json.loads((store.root / 'active.json').read_text())
        first, first_auth = None, None
        for identity in ('d' * 32, 'e' * 32):
            proposal = dict(self.proposal, transition_id=identity,
                            authorization_contract='scope-amendment-v1', parent_selector=parent)
            auth = dict(self.authorization, transition_id=identity, proposal_sha256=digest(proposal))
            store.publish(self.source, proposal, auth, self.old, self.new, self.artifacts)
            if first is None: first, first_auth = proposal, auth
            parent = json.loads((store.root / 'active.json').read_text())
        with self.assertRaisesRegex(TransitionError, 'Another generation'):
            store.publish(self.source, first, first_auth, self.old, self.new, self.artifacts)
        self.assertEqual(json.loads((store.root / 'active.json').read_text()), parent)
