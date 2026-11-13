"""One real bounded I2 pipeline, with fake providers behind native reservations."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import ExitStack

import orchestrator as o
import test_reconciliation as fixtures
from ssdlc_controller import controller_snapshot, execution_contract
from ssdlc_tasks import digest, TaskJournal, atomic_json
from ssdlc_recovery import LimitsStore
from ssdlc_transitions import runtime_root
from test_gate_reports import finding
from test_full_checkpoint_review import framed


class NativeI2CorrectiveIntegrationTests(unittest.TestCase):
    def test_exclusive_native_admission_repair_and_fresh_gates(self):
        self.run_reserve_model('exclusive_cost_kind_v1')

    def test_historical_additive_native_admission_repair_and_fresh_gates(self):
        self.run_reserve_model(None)

    def run_reserve_model(self, reserve_model):
        class RuntimeFixture(fixtures.ReconciliationTests):
            def before_import(self):
                self.outer.update(goal='Synthetic native I2 corrective acceptance',
                                  project_id='repo', specialists_triggered=[])
                atomic_json(self.artifacts / 'state.json', self.outer)
        f = RuntimeFixture(); f.setUp(); self.addCleanup(f.doCleanups)
        f.controller = controller_snapshot(Path(o.__file__).resolve().parent)
        environment = {'mode': 'role_container', 'image_id': 'offline'}
        f.identity = dict(controller_sha256=digest(execution_contract(f.controller)), environment=environment)
        f.request['profile'].update(metered_usd=30, cli_estimate_usd=30, development_attempts=5,
                                   invocations=30)
        if reserve_model is not None:
            f.request['profile']['unknown_usage_reserve_model'] = reserve_model
        for role in ('03-dev', '04-review', '05-qa', '05b-appsec', '06-ops', '07-docs'):
            f.request['profile']['role_invocations'][role] = 8
            f.request['profile']['phase_invocations'][role] = 8
        f.request['combined_reported_usd_cap'] = 30
        f.request['unknown_usage_reserve'].update(metered_usd=0.1, cli_estimate_usd=0.1)
        f.select(f.stage())
        runtime = runtime_root(f.target); store = LimitsStore(runtime)
        envelope, before = copy.deepcopy(store._load(repair=False))
        journal_path = runtime / '03-dev/tasks/task-journal.jsonl'
        prefix = journal_path.read_bytes()
        developers, gates = [], []
        with patch.object(o.SSDLCOrchestrator, '_verification_environment', return_value=environment):
            orch = o.SSDLCOrchestrator(f.repo, f.target.name, sync_remote=False)
            def invoke_role(engine, model, system, user, cwd, *args, **kwargs):
                gate = {'04-review': 'review', '05-qa': 'qa', '05b-appsec': 'appsec'}[orch.state['current_phase']]
                gates.append((gate, orch.state['task_verified_head']))
                findings = [finding()] if gate == 'review' and sum(g[0] == 'review' for g in gates) == 1 else []
                return framed(gate, orch, findings), 0, 0, 0, 'fake'
            def run_codex(prompt, cwd, **kwargs):
                developers.append(prompt)
                name = 'app.py' if '"txn_id"' in prompt else 'next.py'
                (Path(cwd) / name).write_text('value = %d\n' % (len(developers) + 20))
                return 'TASK_DEV_STATUS: COMPLETE', 0
            def verify(argv, cwd, timeout, **kwargs):
                return ('1000' if argv == ['id', '-u'] else 'Ran 1 test\nOK'), '', 0
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {'SSDLC_EXECUTION_MODE': 'docker',
                    'SSDLC_BUDGET_CAP_USD': '0', 'SSDLC_MAX_DEV_RETRIES': '5'}))
                stack.enter_context(patch.object(o.EngineRunner, '_should_spawn_role_container', return_value=True))
                stack.enter_context(patch.object(o.SquadDispatcher, 'dispatch_role', return_value=('codex', 'fake', 'low', 'offline')))
                stack.enter_context(patch.object(o.SquadDispatcher, 'resolve_review_models', return_value=(['codex:reviewer'], 'low')))
                # Mock the provider bodies, retaining the actual reservation/finish wrapper.
                stack.enter_context(patch.object(o.EngineRunner, 'invoke_role', side_effect=o._supervised_dispatch(invoke_role)))
                stack.enter_context(patch.object(o.EngineRunner, 'run_codex', side_effect=o._supervised_dispatch(run_codex)))
                stack.enter_context(patch.object(o.EngineRunner, 'run_sandboxed_cmd', side_effect=verify))
                stack.enter_context(patch.object(orch, '_run_deterministic_scanners', return_value=(True, 'checks passed')))
                stack.enter_context(patch.object(orch, '_run_appsec_scans', return_value=[]))
                stack.enter_context(patch.object(orch, 'run_phase_ops'))
                stack.enter_context(patch.object(orch, 'run_phase_docs'))
                with self.assertLogs('ssdlc_corrective', level='INFO') as events:
                    evidence = orch.run_pipeline('native I2 bounded corrective integration', from_phase='03-dev')
        after_envelope, after = store._load(repair=False)
        self.assertEqual(orch.state['status'], 'AWAITING_SUPERVISOR_SIGNOFF')
        self.assertTrue(evidence.is_file())
        self.assertEqual(after_envelope, envelope)
        self.assertEqual(after['deadline_at'], before['deadline_at'])
        self.assertEqual({k: after['invocations'][k] for k in before['invocations']}, before['invocations'])
        self.assertEqual(len(after['invocations']) - len(before['invocations']), 6)
        self.assertEqual(len(developers), 2)
        self.assertEqual([gate for gate, head in gates], ['review', 'review', 'qa', 'appsec'])
        final = orch.state['task_verified_head']
        self.assertNotEqual(gates[0][1], final)
        self.assertTrue(all(head == final for gate, head in gates[1:]))
        self.assertTrue(journal_path.read_bytes().startswith(prefix))
        state = TaskJournal(journal_path.parent).peek()
        self.assertEqual(state['tasks']['T1']['attempts'], 2)
        self.assertEqual(state['tasks']['T2']['attempts'], 1)
        self.assertEqual(len(state['invalidations']), 1)
        self.assertEqual(state['invalidations'][0]['roots'], ['T1'])
        self.assertEqual(state['invalidations'][0]['closure'], ['T1', 'T2'])
        transaction_paths = list((runtime / 'bounce-txn').glob('*/transaction.json'))
        self.assertEqual(len(transaction_paths), 1)
        record = json.loads(transaction_paths[0].read_text())
        messages = [entry.getMessage() for entry in events.records]
        for stage in ('created', 'invalidated', 'repair_dispatched', 'revalidated', 'completed', 'reconciled', 'gates_rerun'):
            self.assertTrue(any('stage='+stage+' ' in message for message in messages), stage)
        for message in messages:
            self.assertIn('run='+f.target.name+' ', message)
            self.assertIn('txn='+record['txn_id'][:12]+' ', message)
            self.assertIn('preview='+record['preview']['revision'][:12]+' ', message)
        for name in ('corrective_transactions_created', 'corrective_transactions_executed',
                     'corrective_repair_attempts', 'corrective_gates_rerun'):
            self.assertEqual(orch.corrective_counters[name], 1, name)
        self.assertEqual(record['limits']['max_attempts'], 5)
        self.assertEqual(record['limits']['budget_cap_usd'], 30)
        self.assertAlmostEqual(record['limits']['spend_at_creation_usd'], store.accounted_total(before, envelope['policy']))
        self.assertTrue(any(row.get('conservative_usage') for row in before['invocations'].values()))
        self.assertTrue(any(all(value is None for value in row['usage'].values()) for row in before['invocations'].values()))
        self.assertEqual(runtime_root(f.target), runtime)
        self.assertEqual({ref: (f.original['root'] / ref).read_bytes() for ref in f.original_bytes}, f.original_bytes)


if __name__ == '__main__':
    unittest.main()
