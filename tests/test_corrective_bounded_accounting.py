"""Corrective records preserve native I2 accounting, including unknown reserves."""
import copy
import json
import unittest
from unittest.mock import patch

import orchestrator
import test_reconciliation as recovery
import test_corrective_creation as creation
from ssdlc_recovery import LimitsStore
from ssdlc_transitions import runtime_root
from ssdlc_tasks import TaskError


class BoundedCorrectiveAccountingTests(unittest.TestCase):
    def setUp(self):
        self.f=f=recovery.ReconciliationTests();f.setUp();self.addCleanup(f.doCleanups)
        f.request['profile'].update(metered_usd=30,cli_estimate_usd=30,development_attempts=5,
                                  unknown_usage_reserve_model='exclusive_cost_kind_v1')
        f.request['combined_reported_usd_cap']=30
        f.request['unknown_usage_reserve'].update(metered_usd=0.1,cli_estimate_usd=0.1)
        staged=f.stage();f.select(staged)
        self.store=LimitsStore(runtime_root(f.target))
        self.c=c=creation.CreationTests();c.setUp();self.addCleanup(c.doCleanups)

    def test_native_accounting_view_and_creation_component_preserve_reserves(self):
        preview=self.c.preview()
        before=self.store._load(repair=False)
        unknown=[row for row in before[1]['invocations'].values() if all(v is None for v in row['usage'].values())]
        self.assertTrue(unknown)
        # A null legacy placeholder must neither crash creation nor erase native reserves.
        self.c.orch.state['ledger'].append({'cost_usd':None})
        with patch.object(orchestrator._dispatch_local,'limits',self.store,create=True):
            self.c.orch.check_budget_circuit_breaker()
            effective=self.c.orch._corrective_limits()
            spend=self.c.orch._corrective_accounted_spend()
            self.assertEqual(effective['max_attempts'],5)
            self.assertEqual(effective['budget_cap_usd'],30)
            self.assertGreater(spend,9)
            self.assertEqual(spend,self.store.accounted_total(before[1],before[0]['policy']))
        # Exercise creation independently with the computed native view. These
        # fixtures are different tickets; borrowing historical task bindings
        # across them would correctly fail integrity verification.
        with patch.object(self.c.orch,'_corrective_limits',return_value=effective), \
             patch.object(self.c.orch,'_corrective_accounted_spend',return_value=spend):
            result=self.c.execute(preview)
        record=json.loads((self.c.orch.ssdlc_root/'bounce-txn'/result['txn_id']/'transaction.json').read_text())
        self.assertEqual(record['limits']['max_attempts'],5)
        self.assertEqual(record['limits']['budget_cap_usd'],30)
        self.assertEqual(record['limits']['spend_at_creation_usd'],spend)
        self.assertEqual(self.store._load(repair=False),before)
        self.assertTrue(all(all(v is None for v in row['usage'].values()) for row in unknown))

    def test_old_frozen_transaction_can_tighten_native_policy_but_environment_cannot_raise_it(self):
        original=self.store._load(repair=False)
        txn={'limits':{'max_attempts':3,'gate_round_ceiling':2,'budget_cap_usd':9}}
        with patch.object(orchestrator._dispatch_local,'limits',self.store,create=True), \
             patch.object(self.c.orch,'_open_or_any_txns',return_value=[txn]), \
             patch.dict('os.environ',{'SSDLC_MAX_DEV_RETRIES':'999','SSDLC_BUDGET_CAP_USD':'0'}):
            limits=self.c.orch._corrective_limits()
            self.assertEqual(limits,dict(max_attempts=3,gate_round_ceiling=2,budget_cap_usd=9))
            with self.assertRaises(TaskError):self.c.orch.check_budget_circuit_breaker()
        self.assertEqual(self.store._load(repair=False),original)

    def test_exclusive_and_additive_profiles_keep_their_different_reserve_semantics(self):
        envelope,state=self.store._load(repair=False)
        policy=copy.deepcopy(envelope['policy'])
        exclusive=self.store.accounted_total(state,policy)
        policy.pop('unknown_usage_reserve_model')
        additive=self.store.accounted_total(state,policy)
        self.assertGreater(additive,exclusive)
