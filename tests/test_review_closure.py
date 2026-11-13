"""Regressions for independently observed final-review gaps."""
import contextlib
import copy
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import orchestrator as o
from ssdlc_tasks import (TaskError, TaskJournal, TaskScheduler,
                         diagnostic_excerpt, file_hash, log_retry_reason)
import test_repair_prompt as repair_fixtures
import test_unified_execution as execution_fixtures


class ReviewClosureTests(unittest.TestCase):
    def fixture(self, case):
        instance=case()
        self.addCleanup(instance.doCleanups)
        instance.setUp()
        return instance

    def test_every_tier_requires_appsec_without_a_specialist_trigger(self):
        f=self.fixture(execution_fixtures.UnifiedExecutionTests)
        orch=f.make_orchestrator();f.seed_inputs(orch)
        wt=orch._ensure_worktree()
        orch.state['task_verified_head']=o.git(wt,'rev-parse','HEAD')
        orch.state['specialists_triggered']=[]
        for tier in ['S','M','L']:
            for secure in [False,True]:
                with self.subTest(tier=tier,secure=secure):
                    orch.state['risk_tier']=tier;calls=[]
                    def gate(name,passed=True):
                        def run(*args,**kwargs):
                            calls.append(name);return passed,'fixture'
                        return run
                    with patch.object(o._dispatch_local,'limits',None,create=True), \
                         patch.dict(os.environ,{'SSDLC_MAX_DEV_RETRIES':'1','SSDLC_SUPERVISED_RUN_ID':''}), \
                         patch.object(orch,'_reconcile_open_txn',return_value=None), \
                         patch.object(orch,'_ensure_task_plan'), \
                         patch.object(orch,'check_budget_circuit_breaker'), \
                         patch.object(orch,'_stop_on_terminal_gate'), \
                         patch.object(orch,'run_phase_dev',side_effect=gate('dev')), \
                         patch.object(orch,'run_phase_review',side_effect=gate('review')), \
                         patch.object(orch,'run_phase_qa',side_effect=gate('qa')), \
                         patch.object(orch,'run_phase_appsec',side_effect=gate('appsec',secure)), \
                         patch.object(orch,'run_phase_ops'),patch.object(orch,'run_phase_docs'), \
                         patch.object(orch,'compile_evidence_pack',return_value=Path('evidence.md')) as pack:
                        orch._run_pipeline('offline',from_phase='03-dev')
                    self.assertEqual(calls,['dev','review','qa','appsec'])
                    pack.assert_called_once_with(ready_for_signoff=secure)

    def test_console_redaction_precedes_excerpt_truncation(self):
        sentinel='controller-credential-' * 200
        output=io.StringIO()
        with patch.dict(os.environ, {'REVIEW_SECRET_TOKEN':sentinel}), contextlib.redirect_stdout(output):
            excerpt=diagnostic_excerpt('start '+sentinel+' end',120)
            log_retry_reason('review',excerpt,Path('private-check.log'),max_chars=120)
        self.assertNotIn('controller-credential-',output.getvalue())
        self.assertIn('[REDACTED]',output.getvalue())
        self.assertLess(len(output.getvalue()),240)

    def test_corrective_failed_check_console_redacts_but_keeps_private_evidence(self):
        f=self.fixture(repair_fixtures.RepairPromptTests)
        f.engine.invalidate(['T1'],'corrective:console-test')
        sentinel='check-credential-' * 8
        invocations=[]
        def verify(*args):
            invocations.append(1)
            return 'Ran 1 test\n'+sentinel,'',int(len(invocations)==1)
        f.engine.verifier=verify
        output=io.StringIO()
        with patch.dict(os.environ,{'REVIEW_SECRET_TOKEN':sentinel}),contextlib.redirect_stdout(output):
            f.engine.repair(f.plan['tasks'],['T1'],{},txn_id='console-test',on_step=lambda *args:None)
        self.assertNotIn(sentinel,output.getvalue())
        self.assertIn('[REDACTED]',output.getvalue())
        failure=f.engine.journal.peek()['tasks']['T1']['failed_attempts'][-1]
        check=failure['checks'][0];path=f.engine.directory/check['log_ref']
        self.assertIn(sentinel,path.read_text())
        self.assertEqual(file_hash(path),check['sha256'])

    def test_corrective_developer_blocker_console_is_redacted(self):
        f=self.fixture(repair_fixtures.RepairPromptTests)
        f.engine.invalidate(['T1'],'corrective:console-test')
        sentinel='developer-credential-' * 8
        def develop(task,attempt,prompt):
            (f.wt/'app.py').write_text('value = 2\n')
            return dict(output=sentinel+'\nTASK_DEV_STATUS: ENVIRONMENT_BLOCKED',exit_code=0)
        f.engine.developer=develop
        output=io.StringIO()
        with patch.dict(os.environ,{'REVIEW_SECRET_TOKEN':sentinel}),contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(TaskError,'repair_failed'):
                f.engine.repair(f.plan['tasks'],['T1'],{},txn_id='console-test',on_step=lambda *args:None)
        self.assertNotIn(sentinel,output.getvalue())
        self.assertIn('[REDACTED]',output.getvalue())
        self.assertIn('ENVIRONMENT_BLOCKED',output.getvalue())

    def test_pre_isolation_sealed_history_keeps_its_execution_mode(self):
        f=self.fixture(execution_fixtures.UnifiedExecutionTests)
        orch=f.make_orchestrator();f.seed_inputs(orch)
        wt=orch._ensure_worktree();orch._ensure_controller_contract()
        def old_develop(task,attempt,prompt):
            (wt/'app.py').write_text('value = 1\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE',exit_code=0)
        # This produces the actual pre-isolation serialized shape: no isolation
        # marker or candidate runtime, with verified checks and a sealed digest.
        old=TaskScheduler(wt,orch.dir_dev/'tasks',f.plan,f.quality,
            o.build_task_context(orch.ssdlc_root),old_develop,
            lambda *args:('Ran 1 test\nOK','',0),isolate_candidates=False)
        old.run(f.plan['tasks'])
        saved=old.journal.peek();self.assertNotIn('candidate_isolation',saved)
        path=old.journal.path;prefix=path.read_bytes()
        quality=orch.dir_recon/'quality-contract.json';sealed=quality.read_bytes()
        ledger=copy.deepcopy(orch.state['ledger'])
        with patch.object(o._dispatch_local,'limits',None,create=True), \
             patch.object(o.EngineRunner,'run_codex',side_effect=AssertionError('new developer dispatch')), \
             patch.object(o.EngineRunner,'run_sandboxed_cmd',return_value=('Ran 1 test\nOK','',0)):
            orch._run_task_development()
        current=TaskJournal(orch.dir_dev/'tasks').peek()
        self.assertNotIn('candidate_isolation',current)
        self.assertNotIn('candidate_runtime',current)
        self.assertEqual(current['tasks']['T1']['attempts'],saved['tasks']['T1']['attempts'])
        self.assertTrue(path.read_bytes().startswith(prefix))
        self.assertEqual(quality.read_bytes(),sealed)
        self.assertEqual(orch.state['ledger'],ledger)

