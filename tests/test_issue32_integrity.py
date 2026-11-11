"""Regression checks for issue #32, without live providers."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_historical_accounting import historical_fixture
from test_task_bootstrap import RepositoryTests
from ssdlc_controller import controller_snapshot
from ssdlc_historical import project_accounting, validate_decisions
from ssdlc_recovery import LimitsStore, RecoveryStop
from ssdlc_tasks import TaskError, atomic_json, digest, file_hash, git

ROOT = Path(__file__).resolve().parents[1]


class UnknownUsageTests(unittest.TestCase):
    def setUp(self):
        self.facts, self.request, self.state = historical_fixture()
        # Eliminate every unrelated uncertainty: usage alone must need acceptance.
        self.facts['limits'].update(copy.deepcopy(self.request['profile']))

    def project(self):
        return project_accounting(self.facts, self.request, state=self.state, now=1000)

    def test_unknown_usage_alone_requires_hash_bound_acceptance(self):
        profile, initial, decisions, _ = self.project()
        self.assertEqual(decisions['unknown_limits'], [])
        self.assertEqual(len(decisions['unknown_usage']), 24)
        self.assertEqual(profile['historical_unknown_usage']['invocations'], 24)
        self.assertAlmostEqual(profile['historical_unknown_usage']['metered_usd'], 0.024)
        auth = {'decisions_sha256': digest(decisions), 'accept_unknown_history': False, 'authorize_extensions': False}
        with self.assertRaisesRegex(TaskError, 'Unknown/ambiguous'):
            validate_decisions(auth, {'decisions': decisions})
        auth['accept_unknown_history'] = True
        validate_decisions(auth, {'decisions': decisions})
        changed = copy.deepcopy(decisions)
        changed['unknown_usage'][0]['reserve']['metered_usd'] = 2
        with self.assertRaisesRegex(TaskError, 'does not bind'):
            validate_decisions(auth, {'decisions': changed})
        self.assertIsNone(initial['invocations']['ledger-1']['usage']['metered_usd'])

    def test_missing_null_and_default_zero_never_become_free_history(self):
        for variant in ('missing', None, 0):
            with self.subTest(cost=variant):
                if variant == 'missing': self.facts['ledger'][1].pop('cost_usd', None)
                else: self.facts['ledger'][1]['cost_usd'] = variant
                self.request['ledger_mapping'][1]['sha256'] = digest(self.facts['ledger'][1])
                saved = self.request.pop('unknown_usage_reserve', None)
                with self.assertRaisesRegex(TaskError, 'explicit conservative reserve'):
                    self.project()
                self.request['unknown_usage_reserve'] = saved
                self.assertEqual(len(self.project()[2]['unknown_usage']), 24)

    def test_reservations_consume_both_dollar_caps_and_combined_cap(self):
        profile, initial, _, _ = self.project()
        for cap in ('metered_usd', 'cli_estimate_usd', 'combined_reported_usd_cap'):
            with self.subTest(cap=cap):
                p, counters = copy.deepcopy(profile), copy.deepcopy(initial)
                if cap == 'combined_reported_usd_cap': counters[cap] = 9.3
                else: p[cap] = 0.02 if cap == 'metered_usd' else 9.28
                with tempfile.TemporaryDirectory() as tmp:
                    store = LimitsStore(Path(tmp), lambda: 1000)
                    with self.assertRaisesRegex(RecoveryStop, 'cap exhausted'):
                        store.check(counters, p)
                    # Verification does not dispatch paid inference.
                    store.check(counters, p, for_dispatch=False)
        initial['invocations']['ledger-1'].pop('conservative_usage')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RecoveryStop, 'no conservative dollar reserve'):
                LimitsStore(Path(tmp), lambda: 1000).check(initial, profile)

    def test_exclusive_kind_reserve_is_counted_once_combined_but_fully_per_kind(self):
        self.request['profile']['unknown_usage_reserve_model'] = 'exclusive_cost_kind_v1'
        profile, initial, decisions, _ = self.project()
        self.assertIn('unknown_usage_reserve_model:exclusive_cost_kind_v1', decisions['unknown_limits'])
        self.assertAlmostEqual(profile['historical_unknown_usage']['combined_usd'], 0.024)
        self.assertIsNone(initial['invocations']['ledger-1']['usage']['cli_estimate_usd'])
        initial['combined_reported_usd_cap'] = 9.30  # Known 9.264434 + 24 * .001, not twice.
        with tempfile.TemporaryDirectory() as tmp:
            store = LimitsStore(Path(tmp), lambda: 1000)
            store.check(initial, profile)
            legacy = dict(profile); legacy.pop('unknown_usage_reserve_model')
            with self.assertRaisesRegex(RecoveryStop, 'combined reported-cost cap exhausted'):
                store.check(initial, legacy)
            for kind, threshold in [('metered_usd', 0.02), ('cli_estimate_usd', 9.28)]:
                cap = dict(profile, **{kind: threshold})
                with self.subTest(kind=kind), self.assertRaisesRegex(RecoveryStop, 'cap exhausted'):
                    store.check(initial, cap)
            initial['combined_reported_usd_cap'] = 9.28
            with self.assertRaisesRegex(RecoveryStop, 'combined reported-cost cap exhausted'):
                store.check(initial, profile)
            # Unequal channel allocations reserve the maximum, never an arbitrary average.
            initial['combined_reported_usd_cap'] = 9.30
            initial['invocations']['ledger-1']['conservative_usage']['metered_usd'] = 0.02
            with self.assertRaisesRegex(RecoveryStop, 'combined reported-cost cap exhausted'):
                store.check(initial, profile)

    def test_nonpositive_nonfinite_or_unexplained_reserve_refused(self):
        for value in (0, -1, float('nan'), float('inf'), True):
            with self.subTest(value=value):
                self.request['unknown_usage_reserve']['metered_usd'] = value
                with self.assertRaisesRegex(TaskError, 'positive conservative'): self.project()
        self.request['unknown_usage_reserve'] = {'metered_usd': 1, 'cli_estimate_usd': 1, 'reason': '   '}
        with self.assertRaisesRegex(TaskError, 'positive conservative'): self.project()


class PathAndControllerTests(RepositoryTests):
    def test_normalized_targets_cannot_alias_the_grader(self):
        for path in ('orchestrator.py', 'ssdlc_tasks.py', 'tests/replay/benchmark.py'):
            p = self.repo / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('# self repo\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'self development fixture')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.plan['base_commit'] = self.quality['base_commit'] = self.base
        for path in ('./tests/replay/benchmark.py', 'tests//replay/benchmark.py', './ssdlc_grader.py',
                     'tests/replay/../replay/benchmark.py', 'tests/replay/new.py', './ssdlc_replay_gate.py'):
            with self.subTest(path=path):
                self.plan['tasks'][0]['target_files'] = [path]
                with self.assertRaises(TaskError): self.validator().validate(self.plan)

    def test_global_ignores_home_and_injected_config_do_not_change_snapshot(self):
        local = self.repo / '.claude/settings.local.json'
        local.parent.mkdir(); local.write_text('{}\n')
        before = controller_snapshot(self.repo)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ignore = home / 'ignore'; ignore.write_text('**/.claude/settings.local.json\n')
            config = home / '.gitconfig'; config.write_text('[core]\n\texcludesFile = ' + str(ignore) + '\n')
            with patch.dict(os.environ, {'HOME': tmp, 'XDG_CONFIG_HOME': str(home / 'xdg'),
                    'GIT_CONFIG_GLOBAL': str(config), 'GIT_CONFIG_COUNT': '1',
                    'GIT_CONFIG_KEY_0': 'core.excludesFile', 'GIT_CONFIG_VALUE_0': str(ignore)}):
                self.assertEqual(before, controller_snapshot(self.repo))
                local.write_text('{"changed":true}\n')
                self.assertNotEqual(before, controller_snapshot(self.repo))
        # Repository-owned ignore rules still apply.
        (self.repo / '.gitignore').write_text('.ssdlc/\n.claude/\n')
        ignored = controller_snapshot(self.repo)
        local.write_text('changed again\n')
        self.assertEqual(ignored, controller_snapshot(self.repo))

    def test_only_explicit_progress_reports_count(self):
        from orchestrator import SSDLCOrchestrator
        root = self.repo / '.ssdlc/TKT-1'
        ref = '02-design/design-session/source-inspection-check.json'
        path = root / ref
        report = {'kind': 'source_inspection', 'base_commit': self.base,
                  'files': [{'path': 'app.py', 'sha256': file_hash(self.repo / 'app.py')}]}
        orch = SSDLCOrchestrator.__new__(SSDLCOrchestrator)
        orch.repo_root, orch.ssdlc_root = self.repo, root
        for purpose, expected in ((None, False), ('diagnosis_context', False), ('other', False), ('progress_evidence', True)):
            if purpose is None: report.pop('purpose', None)
            else: report['purpose'] = purpose
            atomic_json(path, report)
            orch.state = {'base_commit': self.base, 'artifacts': {ref: {'sha256': file_hash(path)}}}
            self.assertEqual(orch._valid_design_progress_evidence({'artifact_ref': ref, 'sha256': file_hash(path)}), expected)


class IsolatedGateTests(unittest.TestCase):
    def test_new_discovery_module_cannot_patch_the_isolated_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('.git', '.ssdlc', '__pycache__', '.tools'))
            marker = Path(tmp) / 'imported'
            (root / 'tests/test_aaa_helper.py').write_text(
                'from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("imported")\n'
                'import replay.benchmark\nreplay.benchmark.run_case = lambda name: {}\n')
            # Weaken production behavior while leaving grader pins untouched.
            production = root / 'ssdlc_recovery.py'
            production.write_text(production.read_text() + '\ndef progress(*a, **kw):\n    return {"material_progress": True}\n')
            output = Path(tmp) / 'report.json'
            proc = subprocess.run([sys.executable, '-I', str(root / 'ssdlc_replay_gate.py'),
                                   '--output', str(output)], capture_output=True, text=True, timeout=120,
                                  env=dict(os.environ, PYTHONPATH=str(root / 'tests')))
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse(marker.exists(), 'The isolated gate imported a discovery-only module')
            if output.exists(): self.assertGreater(json.loads(output.read_text())['failed_cases'], 0)
