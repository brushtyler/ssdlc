"""Existing I2 gets no new grant, role cap or controller without exact admission."""
import copy
import json
from pathlib import Path
import unittest

import test_scope_amendments as fixtures
from test_feedback_delegation import grant
from test_feedback_workflow import config
import ssdlc_feedback_admission as admission
from ssdlc_recovery import LimitsStore
from ssdlc_tasks import TaskError, TaskJournal, digest
from ssdlc_transitions import runtime_root


class FeedbackAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.case = f = fixtures.ScopeAmendmentTests(); f.initialize(); self.addCleanup(f.doCleanups)
        self.source = f.source
        self.profile = copy.deepcopy(self.source['envelope']['policy'])
        self.profile['feedback_delegation'] = grant(); self.profile['feedback_workflow'] = config()
        self.profile['role_invocations'].update({'feedback-planner':2,'feedback-reviewer':2,'feedback-po':1})
        self.profile['phase_invocations']['feedback'] = 5

    def stage(self, **kwargs):
        return admission.prepare(self.case.ticket, self.case.repo, self.profile, 'Enable opt-in feedback without increasing total caps',
            runtime_identity=self.case.identity, controller_snapshot=self.case.new_controller, **kwargs)

    def auth(self, staged):
        p = staged['proposal']
        return dict(schema_version=1, transition_id=p['transition_id'], proposal_sha256=digest(p), authorized=True,
            source_fenced=True, old_controller_sha256=p['old_controller_sha256'], new_controller_sha256=p['new_controller_sha256'],
            profile_sha256=p['profile_sha256'])

    def test_profile_admission_preserves_attempts_deadline_history_and_scope_refusal(self):
        original = {ref:(self.source['root']/ref).read_bytes() for ref in self.source['files']}
        staged = self.stage()
        p = staged['proposal']
        self.assertEqual(p['authorization_contract'], admission.CONTRACT)
        self.assertEqual(p['scope_diff'], {})
        with self.assertRaises(TaskError): runtime_root(self.case.ticket)
        result = admission.activate(self.case.ticket, staged['preparation'], self.auth(staged))
        root = runtime_root(self.case.ticket)
        envelope, counters = LimitsStore(root)._load(repair=False)
        self.assertEqual(envelope['policy'], self.profile)
        self.assertEqual(counters, dict(self.source['counters'], policy_sha256=digest(envelope)))
        self.assertEqual(json.loads((root/'02-design/plan.json').read_text()), self.source['plan'])
        state = TaskJournal(root/'03-dev/tasks').peek()
        self.assertEqual(state['tasks']['T2']['attempts'], 2)
        self.assertNotIn('scope_reconciliation', state['tasks']['T2'])
        self.assertEqual({ref:(self.source['root']/ref).read_bytes() for ref in original}, original)
        with self.assertRaisesRegex(TaskError,'SCOPE_REQUIRED'):
            self.case.scheduler(root, lambda *args:self.fail('no implicit developer retry')).run(self.source['plan']['tasks'])
        self.assertEqual(admission.activate(self.case.ticket, staged['preparation'], self.auth(staged)), result)

    def test_no_global_extension_or_existing_role_reallocation(self):
        before = copy.deepcopy(self.profile)
        for field in ('wall_seconds','invocations','metered_usd','cli_estimate_usd','development_attempts','invocation_timeout_seconds'):
            self.profile = copy.deepcopy(before); self.profile[field] += 1
            with self.subTest(field=field), self.assertRaises(TaskError): self.stage()
        self.profile = copy.deepcopy(before); self.profile['role_invocations']['03-dev'] += 1
        with self.assertRaises(TaskError): self.stage()

    def test_profile_exact_hash_cannot_be_replaced_by_scope_authorization(self):
        staged = self.stage(); auth = self.auth(staged)
        del auth['profile_sha256']
        with self.assertRaises(TaskError): admission.activate(self.case.ticket, staged['preparation'], auth)
        auth = self.auth(staged); auth['profile_sha256'] = '0'*64
        with self.assertRaises(TaskError): admission.activate(self.case.ticket, staged['preparation'], auth)

    def test_published_selector_crash_replays_without_reset(self):
        staged = self.stage()
        def crash(point):
            if point == 'after_activation': raise OSError('lost activation response')
        with self.assertRaises(OSError):
            admission.activate(self.case.ticket, staged['preparation'], self.auth(staged), fault=crash)
        admission.activate(self.case.ticket, staged['preparation'], self.auth(staged))
        root = runtime_root(self.case.ticket)
        self.assertEqual(TaskJournal(root/'03-dev/tasks').peek()['tasks']['T2']['attempts'], 2)
        self.assertEqual(len(LimitsStore(root)._load(repair=False)[1]['invocations']), len(self.source['counters']['invocations']))
