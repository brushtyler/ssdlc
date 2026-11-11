"""Sanitized historical #22 shape: 25 rows, six design/four planning rounds.

Values are synthetic except the aggregate CLI estimate. No historical prompts,
provider outputs, machine paths, credentials, or source files are embedded.
"""
import copy
import datetime
import json
from pathlib import Path
import tempfile
import unittest

from ssdlc_historical import project_accounting, reservation_catalogue, limit_sources, validate_decisions
from ssdlc_tasks import digest, file_hash, TaskError


def historical_fixture():
    facts = {'ledger': [{'engine': 'claude' if i < 10 else 'codex',
                        'cost_usd': 9.264434 if i == 0 else 0.0} for i in range(25)],
             'limits': {'max_attempts': 2, 'budget': 10, 'design_rounds': 6},
             'counters': {
                '02-design/design-session/session.json': {'round_limit': 6, 'rounds': [
                    {'round': i, 'review_reserved': 2 if i in (1, 5) else 1} for i in range(1, 7)]},
                '02-design/task-planning/session.json': {'max_attempts': 4, 'attempts': [
                    {'attempt': i, 'review_invocations': 1, 'review-response-1': {'archived': True}} for i in range(1, 5)]}},
             'tasks': {tid: {'attempts': int(tid == 'T01'), 'status': 'RUNNING' if tid == 'T01' else 'PENDING'}
                       for tid in ['T01', 'T02'] + ['T' + str(i).zfill(2) for i in range(4, 14)]}}
    origins, _ = reservation_catalogue(facts, {})
    roles = {r['role']: 50 for r in origins.values()}; roles['04-review'] = 10
    phases = {r['phase']: 50 for r in origins.values()}; phases['04-review'] = 10
    request = {'profile': {'schema_version': 1, 'profile_id': 'sanitized-issue22-replay',
        'invocations': 100, 'wall_seconds': 10000, 'role_invocations': roles, 'phase_invocations': phases,
        'transport_retries': 0, 'design_diagnoses': 1, 'metered_usd': 10, 'cli_estimate_usd': 10,
        'development_attempts': 2, 'design_rounds': 6, 'invocation_timeout_seconds': 1800},
        'opaque_floors': {}, 'supplemental_invocations': [], 'combined_reported_usd_cap': 10,
        'unknown_usage_reserve': {'metered_usd': 0.001, 'cli_estimate_usd': 0.001, 'reason': 'Synthetic offline allowance, not measured billing'},
        'time': {'started_at': 100, 'basis': 'source_created_at'}, 'ledger_mapping': []}
    for i, row in enumerate(facts['ledger']):
        origin = list(origins)[i] if i < len(origins) else None
        descriptor = origins[origin] if origin else {'role': '04-review', 'phase': '04-review'}
        request['ledger_mapping'].append({'index': i, 'sha256': digest(row), **descriptor,
                                         'origins': [origin] if origin else []})
    state = {'created_at': datetime.datetime.fromtimestamp(100, datetime.timezone.utc).isoformat()}
    return facts, request, state


class HistoricalAccountingTests(unittest.TestCase):
    def setUp(self): self.facts, self.request, self.state = historical_fixture()

    def project(self): return project_accounting(self.facts, self.request, state=self.state, now=1000)

    def test_issue22_shape_all_consumed_rounds_and_unknown_usage_survive(self):
        profile, counters, decisions, origins = self.project()
        self.assertEqual(len(counters['invocations']), 25)
        self.assertEqual(len(origins), 23)
        rows = list(counters['invocations'].values())
        self.assertEqual(sum(row['role'] == '02-design-arch' for row in rows), 6)
        self.assertEqual(sum(row['role'] == '02-design-task-plan' for row in rows), 4)
        self.assertEqual(sum(row['role'] == '03-dev' for row in rows), 1)
        self.assertEqual(sum(row['usage']['cli_estimate_usd'] or 0 for row in rows), 9.264434)
        self.assertTrue(all(row['usage']['metered_usd'] is None for row in rows))
        self.assertEqual(counters['deadline_at'], 10100)
        self.assertEqual(counters['historical'], self.facts)

    def test_unknown_caps_and_extensions_need_separate_explicit_decisions(self):
        self.request['profile']['development_attempts'] = 3
        _, _, decisions, _ = self.project()
        proposal = {'decisions': decisions}
        auth = {'decisions_sha256': digest(decisions), 'accept_unknown_history': False, 'authorize_extensions': False}
        with self.assertRaisesRegex(TaskError, 'Unknown/ambiguous'): validate_decisions(auth, proposal)
        auth['accept_unknown_history'] = True
        with self.assertRaisesRegex(TaskError, 'extensions'): validate_decisions(auth, proposal)
        auth['authorize_extensions'] = True
        validate_decisions(auth, proposal)
        for invalid in (1, 'true', None):
            auth['authorize_extensions'] = invalid
            with self.assertRaises(TaskError): validate_decisions(auth, proposal)

    def test_duplicate_row_and_duplicate_origin_and_missing_row_refused(self):
        for mutation in ('index', 'origin', 'missing'):
            with self.subTest(mutation=mutation):
                original = copy.deepcopy(self.request)
                if mutation == 'index': self.request['ledger_mapping'][1]['index'] = 0
                elif mutation == 'origin':
                    self.request['ledger_mapping'][1] = dict(self.request['ledger_mapping'][0], index=1,
                                                            sha256=digest(self.facts['ledger'][1]))
                else: self.request['ledger_mapping'].pop()
                with self.assertRaises(TaskError): self.project()
                self.request = original

    def test_cannot_change_cost_identity_or_refund_ledger(self):
        self.facts['ledger'][0]['cost_usd'] = 0
        with self.assertRaisesRegex(TaskError, 'row changed'): self.project()

    def test_missing_retry_history_cannot_create_fresh_retry_allowance(self):
        self.request['profile']['transport_retries'] = 1
        with self.assertRaisesRegex(TaskError, 'retries disabled'): self.project()

    def test_budget_cannot_disappear_or_silently_change(self):
        self.request['combined_reported_usd_cap'] = None
        with self.assertRaisesRegex(TaskError, 'silently removed'): self.project()
        self.request['combined_reported_usd_cap'] = 15
        decisions = self.project()[2]
        self.assertIn({'limit': 'combined_reported_usd_cap', 'old': 10, 'new': 15}, decisions['extensions'])

    def test_old_time_and_waits_cannot_reset(self):
        self.request['time']['started_at'] = 1000
        with self.assertRaisesRegex(TaskError, 'cannot reset'): self.project()
        self.state['created_at'] = 123  # malformed legacy metadata also needs attestation
        with self.assertRaisesRegex(TaskError, 'attestation'): self.project()
        self.request['time'].update(basis='operator_attestation', reason='Explicit unknown historic start')
        self.assertIn('historical_started_at', self.project()[2]['unknown_limits'])

    def test_opaque_counters_need_conservative_allocation(self):
        self.facts['counters'] = {'02-design/opaque/session.json': {'used': 6}}
        with self.assertRaisesRegex(TaskError, 'conservative allocation'): self.project()
        self.request['opaque_floors'] = {'02-design/opaque/session.json': {
            'count': 5, 'role': '02-design-arch', 'phase': '02-design', 'reason': 'too small'}}
        with self.assertRaisesRegex(TaskError, 'conservative allocation'): self.project()

    def test_external_provenance_is_hashed_parsed_as_data_and_conflicts_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'old.env'
            path.write_text('SSDLC_MAX_DEV_RETRIES=2\nSECRET_NOT_EXPORTED=$(false)\n')
            sources = [{'name': 'old_config', 'path': str(path), 'sha256': file_hash(path), 'format': 'env',
                        'fields': {'development_attempts': 'SSDLC_MAX_DEV_RETRIES'}}]
            effective, bound, payloads = limit_sources(self.facts, sources)
            self.assertEqual(effective['limits']['development_attempts'], 2)
            self.assertEqual(bound, sources)
            self.assertEqual(list(payloads), ['history/limit-sources/old_config'])
            path.write_text('SSDLC_MAX_DEV_RETRIES=3\n')
            with self.assertRaisesRegex(TaskError, 'changed'): limit_sources(self.facts, sources)
            sources[0]['sha256'] = file_hash(path)
            effective = limit_sources(self.facts, sources)[0]
            with self.assertRaisesRegex(TaskError, 'Conflicting'): project_accounting(effective, self.request, state=self.state, now=1000)

    def test_known_session_design_cap_is_not_misreported_as_unknown(self):
        self.facts['limits'].pop('design_rounds')
        self.request['profile']['design_rounds'] = 7
        decisions = self.project()[2]
        self.assertNotIn('design_rounds', decisions['unknown_limits'])
        self.assertIn({'limit': 'design_rounds', 'old': 6, 'new': 7}, decisions['extensions'])

    def test_planning_lost_second_response_still_preserves_reserved_invocation(self):
        ref = '02-design/task-planning/session.json'
        self.facts['counters'][ref]['attempts'][0] = {'attempt': 1, 'status': 'REVIEWING', 'review_invocations': 2}
        origins, unknown = reservation_catalogue(self.facts, {})
        self.assertIn(ref + ':review:1:2', origins)
        self.assertEqual(unknown, [])
        self.facts['counters'][ref]['attempts'][0].pop('review_invocations')
        origins, unknown = reservation_catalogue(self.facts, {})
        self.assertIn(ref + ':review:1:2', origins)
        self.assertIn(ref + ':review-reservation:1', unknown)
