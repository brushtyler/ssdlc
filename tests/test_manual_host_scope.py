"""Manual host-hint admission needs exact review, not funded feedback roles."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import test_reconciliation as fixtures
from ssdlc_controller import execution_contract
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import inspect, subject, prepare, activate
from ssdlc_tasks import TaskError, TaskJournal, TaskScheduler, atomic_json, digest, file_hash
from ssdlc_transitions import runtime_root


class ManualHostScopeTests(unittest.TestCase):
    def setUp(self):
        class OwnedFixture(fixtures.ReconciliationTests):
            def before_import(self):
                path = self.snapshot / 'tests/test_contract.py'
                path.parent.mkdir(); path.write_text('def test_contract():\n    assert 1 == 1\n')
                self.manifest['source_files'].append(dict(path='tests/test_contract.py', role='acceptance_test',
                    rationale='Completed owner contract', sha256=file_hash(path), executable=False))
                self.new_plan['tasks'][0]['target_files'].append('tests/test_contract.py')
        self.f = f = OwnedFixture(); f.setUp(); self.addCleanup(f.doCleanups)
        f.identity['environment'] = dict(mode='role_container', image_id='offline')
        f.select(f.stage()); self.ticket, self.repo = f.target, f.repo
        self.root = runtime_root(self.ticket); self.calls = []
        def developer(task, attempt, prompt, workspace):
            self.calls.append(attempt)
            store = LimitsStore(self.root); identity = store.reserve('03-dev','03-dev','codex'); store.finish(identity,'returned')
            (workspace/'next.py').write_text('value = 2\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE',exit_code=0,quiescent=True)
        def verifier(argv, cwd, timeout, workspace):
            if (workspace/'next.py').exists():
                return ('FAIL: test_contract (test_contract.ContractTests.test_contract)\n'
                        'Traceback (most recent call last):\nAssertionError: obsolete documentation contract\n'
                        'Ran 1 test in 0.01s\nFAILED (failures=1)\n'), '', 1
            return f.verify(argv,cwd,timeout,workspace)
        with self.assertRaisesRegex(TaskError, 'Host scope hypothesis'):
            self.scheduler(self.root,developer,verifier).run(f.new_plan['tasks'])
        outer=json.loads((self.root/'state.json').read_text());outer['status']='FAILED';atomic_json(self.root/'state.json',outer)
        self.source=inspect(self.ticket,allow_host_hints=True)
        self.plan=copy.deepcopy(self.source['plan']);self.plan['plan_revision']+=1
        self.plan['tasks'][1]['target_files'].append('tests/test_contract.py')
        self.reason='Preapproved manual correction of a reviewed documentation contract, with frozen limits.'
        self.controller={'manual_host_scope':'frozen'}
        self.identity=dict(controller_sha256=digest(execution_contract(self.controller)),environment=f.identity['environment'])
        self.future='def test_contract():\n    assert 2 == 2\n'

    def scheduler(self,root,developer,verifier=None):
        envelope,_=LimitsStore(root)._load(repair=False);state=TaskJournal(root/'03-dev/tasks').peek()
        return TaskScheduler(root/'worktree',root/'03-dev/tasks',
            json.loads((root/'02-design/plan.json').read_text()),json.loads((root/'00-recon/quality-contract.json').read_text()),
            self.f.context,developer,verifier or self.f.verify,isolate_candidates=True,recover_timeouts=True,
            feedback_enabled=True,max_attempts=envelope['policy']['development_attempts'],runtime_identity=state['candidate_runtime'],
            historical_capture_authority=envelope['binding']['historical_capture_bindings_sha256'],
            historical_task_floors=envelope['binding'].get('task_floors'),scope_reconciliations=envelope['binding'].get('scope_reconciliations'))

    def review(self):
        value=subject(self.source,self.plan,self.reason);owners=value['owner_revalidation']
        return dict(subject_sha256=digest(value),reviewer='independent operator preapproval',
            report=dict(verdict='APPROVED',findings=[]),scope_diff=value['scope_diff'],
            owner_coverage=dict(contract_sha256=digest(owners),coverage_preserved=True,
                approved_test_changes=[dict(path='tests/test_contract.py',old_sha256=owners['files']['tests/test_contract.py']['sha256'],
                    new_sha256=hashlib.sha256(self.future.encode()).hexdigest(),reason='Equivalent reviewed assertion')]))

    def stage(self,review=None):
        return prepare(self.ticket,self.repo,self.plan,review if review is not None else self.review(),self.reason,
                       runtime_identity=self.identity,controller_snapshot=self.controller)

    def auth(self,staged):
        p=staged['proposal']
        return dict(schema_version=1,transition_id=p['transition_id'],proposal_sha256=digest(p),authorized=True,
            source_fenced=True,old_controller_sha256=p['old_controller_sha256'],new_controller_sha256=p['new_controller_sha256'],
            scope_diff_sha256=digest(p['scope_diff']))

    def test_manual_review_resumes_check_failure_without_consultation_or_limit_extension(self):
        original={ref:(self.root/ref).read_bytes() for ref in self.source['files']}
        with patch('ssdlc_feedback.FeedbackStore._active',side_effect=AssertionError('Expired recording must not be used')):
            staged=self.stage();selected=activate(self.ticket,staged['preparation'],self.auth(staged))
        root=runtime_root(self.ticket);envelope,counters=LimitsStore(root)._load(repair=False)
        self.assertEqual(envelope['policy'],self.source['envelope']['policy'])
        self.assertEqual(counters,dict(self.source['counters'],policy_sha256=digest(envelope)))
        self.assertEqual(self.calls,[1])
        def developer(task,attempt,prompt,workspace):
            self.calls.append(attempt);self.assertEqual((workspace/'next.py').read_text(),'value = 2\n')
            store=LimitsStore(root);identity=store.reserve('03-dev','03-dev','codex');store.finish(identity,'returned')
            (workspace/'tests/test_contract.py').write_text(self.future)
            return dict(output='TASK_DEV_STATUS: COMPLETE',exit_code=0,quiescent=True)
        state=self.scheduler(root,developer).run(self.plan['tasks'])
        self.assertEqual(self.calls,[1,2]);self.assertEqual(state['tasks']['T2']['status'],'COMPLETED')
        self.assertEqual(state['tasks']['T2']['owner_revalidations'][-1]['status'],'PASSED')
        after=LimitsStore(root)._load(repair=False)[1]
        self.assertEqual(len(after['invocations']),len(counters['invocations'])+1)
        self.assertEqual(after['deadline_at'],counters['deadline_at'])
        self.assertEqual({ref:(self.root/ref).read_bytes() for ref in original},original)
        self.assertEqual(activate(self.ticket,staged['preparation'],self.auth(staged)),selected)

    def test_missing_review_or_wrong_scope_authorization_cannot_activate(self):
        review=self.review();review['subject_sha256']='0'*64
        with self.assertRaisesRegex(TaskError,'Independent review'):self.stage(review)
        staged=self.stage();auth=self.auth(staged);auth['scope_diff_sha256']='0'*64
        with self.assertRaises(TaskError):activate(self.ticket,staged['preparation'],auth)
        self.assertEqual(json.loads((self.ticket/'execution/active.json').read_text()),self.source['parent_selector'])

    def test_unresolved_or_protected_hint_is_not_manual_authority(self):
        for classification in ('MIXED','PROTECTED','UNRESOLVED','INSIDE'):
            with self.subTest(classification=classification),patch('ssdlc_feedback_analysis.failure_hint',return_value={'classification':classification}):
                with self.assertRaisesRegex(TaskError,'all failed checks'):self.stage()
        self.assertFalse((self.ticket/'execution/pending.json').exists())
