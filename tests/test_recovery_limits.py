import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import urllib.error

from ssdlc_recovery import LimitsStore, RecoveryStop, STOPS, observed_usage
from ssdlc_tasks import TaskError, atomic_json


def policy(**overrides):
    p = {'schema_version': 1, 'profile_id': 'offline-i2-explicit', 'invocations': 10,
         'wall_seconds': 1000, 'role_invocations': {'03-dev': 3, '02-design-arch': 5},
         'phase_invocations': {'03-dev': 3, '02-design': 5}, 'transport_retries': 2,
         'design_diagnoses': 1, 'metered_usd': 10, 'cli_estimate_usd': 10,
         'development_attempts': 2, 'design_rounds': 5, 'invocation_timeout_seconds': 30}
    p.update(overrides)
    return p


class LimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 100
        self.clock = lambda: self.now
        self.store = LimitsStore(self.root, self.clock)
        self.binding = {'ticket': 'T1', 'controller': 'offline'}
        self.store.authorize(policy(), self.binding)

    def reserve(self, engine='codex', store=None):
        return (store or self.store).reserve('03-dev', '03-dev', engine)

    def test_reserved_lost_response_blocks_every_provider_and_remains_charged(self):
        identity = self.reserve()
        resumed = LimitsStore(self.root, self.clock)
        for engine in ('claude', 'codex', 'openrouter'):
            with self.assertRaisesRegex(RecoveryStop, 'no confirmed outcome'):
                self.reserve(engine, resumed)
        self.assertEqual(len(resumed._load()[1]['invocations']), 1)
        self.assertEqual(resumed._load()[1]['invocations'][identity]['status'], 'reserved')

    def test_finish_is_exactly_once_and_cannot_change_uncertainty(self):
        identity = self.reserve()
        self.store.finish(identity, 'uncertain')
        self.store.finish(identity, 'uncertain')
        with self.assertRaises(RecoveryStop):
            self.store.finish(identity, 'returned')
        with self.assertRaises(RecoveryStop): self.reserve()

    def test_role_and_total_limits_survive_resume_without_environment_defaults(self):
        for _ in range(3):
            identity = self.reserve(); self.store.finish(identity, 'returned')
        with self.assertRaisesRegex(RecoveryStop, 'allowance exhausted'):
            self.reserve(store=LimitsStore(self.root, self.clock))
        with self.assertRaises(TaskError):
            self.store.authorize(policy(invocations=100), self.binding)
        self.store.authorize(policy(), self.binding)
        self.assertEqual(len(self.store._load()[1]['invocations']), 3)

    def test_wall_time_does_not_reset_on_resume(self):
        self.now = 1101
        with self.assertRaisesRegex(RecoveryStop, 'wall-time'): self.reserve()
        self.store.authorize(policy(), self.binding)
        self.assertEqual(self.store._load()[1]['deadline_at'], 1100)

    def test_cost_kinds_are_separate_unknown_is_not_reported_zero(self):
        identity = self.reserve('claude')
        self.store.finish(identity, 'returned', {'metered_usd': None, 'cli_estimate_usd': 10})
        with self.assertRaisesRegex(RecoveryStop, 'cli_estimate'): self.reserve()
        self.assertIsNone(observed_usage('codex', 'invoke_role', ('text', 0, 0, 0))['metered_usd'])
        self.assertEqual(observed_usage('claude', 'run_claude', ('text', 0, 1.2)),
                         {'metered_usd': None, 'cli_estimate_usd': 1.2})
        self.assertIsNone(observed_usage('claude', 'run_claude', ('text', 0, 0))['cli_estimate_usd'])

    def test_invalid_telemetry_cannot_refund_or_reset_a_reservation(self):
        identity = self.reserve()
        for cost in (-1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                self.store.finish(identity, 'returned', {'metered_usd': cost, 'cli_estimate_usd': None})
        self.assertEqual(self.store._load()[1]['invocations'][identity]['status'], 'reserved')

    def test_missing_role_cap_refuses_without_invocation(self):
        with self.assertRaises(RecoveryStop): self.store.reserve('other', '03-dev', 'codex')
        self.assertEqual(self.store._load()[1]['invocations'], {})

    def test_safe_transport_reservations_survive_crash_and_are_bounded(self):
        identity = self.reserve()
        self.assertEqual(self.store.transport(identity, 429, non_dispatch_proven=True, jitter=.25), 2.25)
        resumed = LimitsStore(self.root, self.clock)
        self.assertEqual(resumed.transport(identity, 503, non_dispatch_proven=True, jitter=.5), 4.5)
        with self.assertRaisesRegex(RecoveryStop, 'retry allowance'):
            resumed.transport(identity, 504, non_dispatch_proven=True, jitter=0)
        self.assertEqual(len(resumed._load()[1]['transports']), 2)

    def test_uncertain_transport_and_authentication_never_retry(self):
        identity = self.reserve()
        for code, proven in ((429, False), (503, False), (504, False), (401, True), (500, True)):
            with self.assertRaises(RecoveryStop):
                self.store.transport(identity, code, non_dispatch_proven=proven, jitter=0)
        self.assertEqual(self.store._load()[1]['transports'], [])

    def test_only_explicit_diagnosis_allowance_can_be_reserved_once(self):
        self.store.reserve_diagnosis('obligations')
        with self.assertRaises(RecoveryStop): self.store.reserve_diagnosis('obligations')
        with self.assertRaises(RecoveryStop): self.store.reserve_diagnosis('another')
        self.assertEqual(len(self.store._load()[1]['diagnoses']), 1)

    def test_policy_tampering_and_partial_journal_are_not_silently_reset(self):
        envelope, _ = self.store._load()
        envelope['policy']['invocations'] += 1
        atomic_json(self.store.policy_path, envelope)
        with self.assertRaises(TaskError): self.reserve()

    def test_native_dispatch_is_accounted_once_across_nested_cli_adapter(self):
        from orchestrator import _supervised_dispatch, _dispatch_local, _dispatch_context
        @_supervised_dispatch
        def run_codex(prompt, cwd): return 'text', 0
        @_supervised_dispatch
        def invoke_role(engine, phase): return run_codex('prompt', self.root)
        old = getattr(_dispatch_local, 'limits', None)
        _dispatch_local.limits = self.store
        try:
            with _dispatch_context(phase='03-dev'):
                invoke_role('codex', '03-dev-T1')
        finally: _dispatch_local.limits = old
        state = self.store._load()[1]
        self.assertEqual(len(state['invocations']), 1)
        self.assertEqual(next(iter(state['invocations'].values()))['status'], 'returned')

    def test_http_inference_does_not_repeat_a_post_on_lost_response(self):
        from orchestrator import EngineRunner
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'offline-sentinel'}), \
             patch('orchestrator.urllib.request.urlopen', side_effect=urllib.error.URLError('lost response')) as request:
            with self.assertRaisesRegex(RecoveryStop, 'no automatic replay'):
                EngineRunner.call_openrouter('model', 'system', 'user', 'p', 't', '03-dev')
        self.assertEqual(request.call_count, 1)

    def test_supervisor_deadline_uses_original_ticket_authorization(self):
        from ssdlc_supervisor import ticket_hard_deadline
        repo = self.root / 'repo'; target = repo / '.ssdlc/T1'
        store = LimitsStore(target, self.clock); store.authorize(policy(), self.binding)
        self.assertEqual(ticket_hard_deadline(repo, 'T1', {}, 500), 1100)
        self.assertEqual(ticket_hard_deadline(repo, 'T1', {'SSDLC_SUPERVISOR_HARD_TIMEOUT': '10'}, 500), 510)

    def test_new_stop_registry_uses_existing_eight_classes_and_precedence(self):
        from ssdlc_supervisor import classify, RECOVERY_MATRIX, CATEGORIES, REASONS
        for category, cls in STOPS.items():
            self.assertIn(cls, RECOVERY_MATRIX)
            self.assertIn(category, CATEGORIES); self.assertIn(category.lower(), REASONS)
            event = {'type': 'failure', 'category': category, 'exc_type': 'RecoveryStop', 'seq': 1}
            evidence = {'events': [event]}
            self.assertEqual(classify(evidence)['class'], cls)
            self.assertEqual(classify({**evidence, 'integrity': True})['class'], 'integrity')

    def test_deleted_policy_never_bypasses_existing_consumption(self):
        from orchestrator import _bounded_entrypoint
        from types import SimpleNamespace
        self.store.policy_path.unlink()
        @_bounded_entrypoint
        def run(self): self.fail('Must not execute')
        owner = SimpleNamespace(ssdlc_root=self.root, ticket_id='T1')
        with self.assertRaisesRegex(TaskError, 'policy missing'):
            run(owner)

    def test_direct_phase_calls_use_the_frozen_profile(self):
        from orchestrator import _supervised_dispatch, _supervised_phase
        from types import SimpleNamespace
        @_supervised_dispatch
        def run_codex(prompt, cwd): return 'text', 0
        @_supervised_phase('03-dev')
        def phase(owner): return run_codex('prompt', self.root)
        owner = SimpleNamespace(ssdlc_root=self.root, ticket_id='T1')
        with patch('orchestrator.assert_controller'), patch('ssdlc_recovery.LimitsStore', return_value=self.store):
            phase(owner)
        self.assertEqual(len(self.store._load()[1]['invocations']), 1)

    def test_role_subprocess_timeout_is_clamped_to_original_deadline(self):
        from orchestrator import EngineRunner, _dispatch_local
        self.now = 1095
        previous = getattr(_dispatch_local, 'limits', None); _dispatch_local.limits = self.store
        try:
            with patch('orchestrator.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as call:
                EngineRunner._run_role_command(['true'], cwd=self.root, env=None, timeout=300)
            self.assertEqual(call.call_args.kwargs['timeout'], 5)
        finally: _dispatch_local.limits = previous
