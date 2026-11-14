"""Fresh contracts cannot certify silent no-op commands as executed tests."""
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

import orchestrator as o
import test_bootstrap as bootstrap_fixtures
import test_candidate_evidence as candidate_fixtures
from replay.repository_fixtures import RepositoryTests
from ssdlc_tasks import TaskError, test_check_is_empty
from ssdlc_bootstrap import positive_test_count


class PositiveTestProofTests(unittest.TestCase):
    def test_all_skipped_real_unittest_baseline_is_not_sealed(self):
        f=bootstrap_fixtures.BootstrapTests();f.setUp();self.addCleanup(f.doCleanups)
        (f.repo/'.ssdlc/bootstrap-profile.json').unlink()
        tests=f.repo/'tests';tests.mkdir()
        (tests/'test_skip.py').write_text("import unittest\nclass Example(unittest.TestCase):\n @unittest.skip('fixture skip')\n def test_never_runs(self): raise AssertionError('unreachable')\n")
        policy=dict(schema_version='1.0',contract_revision=1,base_commit=f.base,sources=[],standards=[],
                    limitations=[],exceptions=[],baseline=[],checks=[dict(check_id='CHECK-TESTS',
                        argv=['python3','-m','unittest','discover','-s','tests'],
                        cwd='.',timeout_sec=5,kind='test',required=True)])
        (f.repo/'.ssdlc/quality-contract.json').write_text(json.dumps(policy))
        orch=f.contract_orchestrator();orch.check_budget_circuit_breaker=lambda:None
        def run(argv,cwd,timeout):
            result=subprocess.run(argv,cwd=cwd,capture_output=True,text=True,timeout=timeout)
            self.assertEqual(result.returncode,0)
            self.assertIn('skipped=1',result.stderr)
            return result.stdout,result.stderr,result.returncode
        with patch.object(o.EngineRunner,'run_sandboxed_cmd',side_effect=run),self.assertRaises(TaskError):
            orch._ensure_quality_contract()
        self.assertFalse((orch.dir_recon/'quality-contract.json').exists())
        self.assertEqual(json.loads((f.repo/'.ssdlc/quality-contract.json').read_text()),policy)

    def test_all_skipped_and_mixed_runner_summaries(self):
        cases=[('Ran 1 test\nOK (skipped=1)\n',0),
               ('Ran 3 tests\nOK (skipped=2)\n',1),
               ('Ran 1 test\nOK\nRan 2 tests\nOK (skipped=2)\n',0),
               ('Tests: 4 skipped, 4 total\n',0),
               ('Tests: 2 skipped, 1 passed, 3 total\n',1),
               ('Tests  2 skipped (2)\n',0),
               ('Tests: 4 total\n',None),
               ('# tests 2\n# pass 0\n# skipped 2\n',0),
               ('# tests 3\n# pass 1\n# skipped 2\n',1),
               ('# tests 2\n# skipped 2\n',0),
               ('# tests 2\n# cancelled 2\n',0)]
        for output,count in cases:
            with self.subTest(output=output):
                self.assertEqual(positive_test_count('',output),count)
                self.assertEqual(test_check_is_empty({'kind':'test'},
                    {'positive_test_count_required':True},'',output),count is None or count==0)

    def test_new_silent_test_baseline_is_not_sealed(self):
        f=bootstrap_fixtures.BootstrapTests();f.setUp();self.addCleanup(f.doCleanups)
        (f.repo/'.ssdlc/bootstrap-profile.json').unlink()
        policy=dict(schema_version='1.0',contract_revision=1,base_commit=f.base,sources=[],standards=[],
                    limitations=[],exceptions=[],baseline=[],checks=[dict(check_id='CHECK-TESTS',argv=['true'],
                        cwd='.',timeout_sec=5,kind='test',required=True)])
        (f.repo/'.ssdlc/quality-contract.json').write_text(json.dumps(policy))
        orch=f.contract_orchestrator();orch.check_budget_circuit_breaker=lambda:None
        def run(argv,cwd,timeout):
            result=subprocess.run(argv,cwd=cwd,capture_output=True,text=True,timeout=timeout)
            return result.stdout,result.stderr,result.returncode
        with patch.object(o.EngineRunner,'run_sandboxed_cmd',side_effect=run),self.assertRaises(TaskError):
            orch._ensure_quality_contract()
        self.assertFalse((orch.dir_recon/'quality-contract.json').exists())
        self.assertEqual(json.loads((f.repo/'.ssdlc/quality-contract.json').read_text()),policy)

    def test_scheduler_refuses_silent_current_tests_without_changing_old_contract_semantics(self):
        for new in (False,True):
            f=RepositoryTests();f.setUp();self.addCleanup(f.doCleanups)
            f.wt=f.worktree()
            if new:f.quality['positive_test_count_required']=True
            sealed=copy.deepcopy(f.quality)
            def developer(task,attempt,prompt):
                (f.wt/'app.py').write_text('value = 1\n')
                return dict(output='TASK_DEV_STATUS: COMPLETE',exit_code=0)
            def verify(*args):return ('Ran 1 test\nOK' if (f.wt/'app.py').read_text()=='value = 0\n' else ''),'',0
            engine=f.scheduler(developer,verify,max_attempts=1)
            if new:
                with self.assertRaises(TaskError):engine.run(f.plan['tasks'])
                self.assertTrue(engine.journal.peek()['tasks']['T1']['checks'][-1]['empty_suite'])
            else:self.assertEqual(engine.run(f.plan['tasks'])['tasks']['T1']['status'],'COMPLETED')
            self.assertEqual(f.quality,sealed)

    def test_candidate_collection_does_not_reuse_older_weak_proof_for_new_contract(self):
        f=candidate_fixtures.CandidateTests();f.setUp();self.addCleanup(f.doCleanups)
        f.quality['checks'][0]['kind']='test'
        old=f.collect().records[0]
        self.assertEqual(old['result']['status'],'passed')
        f.quality['positive_test_count_required']=True
        f.orch.state['artifacts']['00-recon/quality-contract.json']['sha256']='e'*64
        new=f.collect(gate='qa')
        self.assertEqual(new.records[0]['result']['status'],'failed')
        self.assertTrue(new.records[0]['result']['empty_suite'])
        self.assertNotEqual(new.records[0]['record_id'],old['record_id'])
        self.assertTrue(new.decisions)

    def test_reported_counts_use_final_summary_and_supported_nonpython_runners(self):
        cases=[('Ran 9 tests\n','Ran 0 tests\n',0),('nested 9 passed\n','Ran 1 test\n',1),
               ('Tests: 4 passed, 4 total','',4),('# tests 3','',3),('  2 passing (3ms)','',2),
               ('> npm test\n> true\n','',None),('=== no tests ran ===','',0)]
        for stdout,stderr,count in cases:
            with self.subTest(stdout=stdout):self.assertEqual(positive_test_count(stdout,stderr),count)
        contract={'positive_test_count_required':True}
        self.assertTrue(test_check_is_empty({'kind':'test'},contract,'',''))
        self.assertFalse(test_check_is_empty({'kind':'lint'},contract,'',''))
        self.assertTrue(test_check_is_empty({'kind':'test'},contract,'Ran 0 tests\n',''))
        self.assertFalse(test_check_is_empty({'kind':'test'},contract,'','Ran 1 test\nOK'))
