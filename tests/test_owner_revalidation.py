"""Real native amendments/captures/commits; offline semantic checks and roles."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_scope_amendments as fixtures
import ssdlc_owner_revalidation as owner
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import subject, prepare, activate
from ssdlc_tasks import TaskError, TaskJournal, digest, git
from ssdlc_transitions import runtime_root


class OwnerRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixtures.ScopeAmendmentTests()
        f.setUp(); self.addCleanup(f.doCleanups)
        self.failing_owner = False
        self.observed = []

    def selected(self):
        f = self.fixture
        staged = f.stage(); activate(f.ticket, staged['preparation'], f.auth(staged))
        return runtime_root(f.ticket)

    def scheduler(self, root):
        f = self.fixture
        def develop(task, attempt, prompt, workspace):
            self.observed.append(('developer', attempt))
            (workspace / 'app.py').write_text('value = 3\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0, quiescent=True)
        scheduler = f.scheduler(root, develop)
        original = scheduler._verify
        def verify(task, record, directory, **kwargs):
            if kwargs.get('event') == 'OWNER_CHECK_FINISHED':
                self.observed.append(('owner', task['task_id'], (scheduler.wt / 'app.py').read_text()))
                if self.failing_owner:
                    old = scheduler.verifier
                    scheduler.verifier = lambda *a: ('Ran 1 test\nFAILED (failures=1)', '', 1)
                    try: return original(task, record, directory, **kwargs)
                    finally: scheduler.verifier = old
            return original(task, record, directory, **kwargs)
        scheduler._verify = verify
        return scheduler

    def test_subject_lists_actual_completed_owner_and_unchanged_check_contract(self):
        f = self.fixture
        value = subject(f.source, f.plan, f.reason)['owner_revalidation']
        self.assertEqual(value['files']['app.py']['owners'], ['T1'])
        self.assertEqual(value['owners']['T1']['task'], f.source['plan']['tasks'][0])
        self.assertEqual(value['owners']['T1']['commit'], f.source['journal']['tasks']['T1']['commit'])
        self.assertEqual(value['base_head'], f.source['journal']['expected_head'])

    def test_developer_context_summarizes_owner_authority_without_changing_it(self):
        root=self.selected();scheduler=self.scheduler(root)
        scheduler.state=scheduler.journal.peek()
        record=scheduler.state['tasks']['T2'];before=copy.deepcopy(record['scope_reconciliation'])
        context=scheduler._repair_context(record)
        summary=context['scope_amendment']['owner_revalidation']
        self.assertEqual(summary['owner_tasks'],['T1'])
        self.assertEqual(summary['contract_sha256'],digest(before['owner_revalidation']['contract']))
        self.assertLess(len(json.dumps(summary)),2000)
        self.assertEqual(record['scope_reconciliation'],before)

    def test_later_scope_addition_keeps_previously_admitted_owner_obligations(self):
        from ssdlc_scope_amendments import inspect
        from ssdlc_tasks import atomic_json
        root=self.selected()
        state=json.loads((root/'state.json').read_text());state['status']='FAILED'
        atomic_json(root/'state.json',state)
        source=inspect(self.fixture.ticket)
        plan=copy.deepcopy(source['plan']);plan['plan_revision']+=1
        plan['tasks'][1]['target_files'].append('extra.py')
        value=subject(source,plan,'Add an unrelated target while preserving earlier owner constraints')
        self.assertEqual(value['scope_diff'],{'T2':{'added':['extra.py'],'removed':[]}})
        self.assertEqual(value['owner_revalidation']['files']['app.py']['owners'],['T1'])
        self.assertEqual(set(value['owner_revalidation']['owners']),{'T1'})

    def test_missing_false_or_wrong_coverage_review_refuses_before_preparation(self):
        f = self.fixture
        before = (f.ticket / 'execution/active.json').read_bytes()
        for coverage in (None, dict(f.review['owner_coverage'], coverage_preserved=False),
                         dict(f.review['owner_coverage'], contract_sha256='0' * 64)):
            review = copy.deepcopy(f.review)
            if coverage is None: review.pop('owner_coverage')
            else: review['owner_coverage'] = coverage
            with self.subTest(coverage=coverage), self.assertRaises(TaskError):
                prepare(f.ticket, f.repo, f.plan, review, f.reason,
                    runtime_identity=f.identity, controller_snapshot=f.new_controller)
        self.assertEqual((f.ticket / 'execution/active.json').read_bytes(), before)

    def test_new_candidate_gets_fresh_owner_checks_before_commit(self):
        root = self.selected(); scheduler = self.scheduler(root)
        physical = []
        verifier = scheduler.verifier
        def counted(argv, *args):
            physical.append(tuple(argv))
            return verifier(argv, *args)
        scheduler.verifier = counted
        old = copy.deepcopy(TaskJournal(root / '03-dev/tasks').peek()['tasks']['T1'])
        counters = copy.deepcopy(LimitsStore(root)._load(repair=False)[1])
        result = scheduler.run(self.fixture.plan['tasks'])
        self.assertEqual(self.observed, [('developer', 3), ('owner', 'T1', 'value = 3\n')])
        self.assertEqual(result['tasks']['T1'], old)
        receipt = result['tasks']['T2']['owner_revalidations'][-1]
        self.assertEqual(receipt['status'], 'PASSED')
        self.assertEqual(receipt['candidate_tree'], git(root / 'worktree', 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual(receipt['capture_sha256'], result['tasks']['T2']['capture_sha256'])
        self.assertTrue(all(c['exit_code'] == 0 for c in receipt['owners']['T1']['checks']))
        # Preflight + the introducing task + only the owner's distinct checks.
        # Fixture commands intentionally have equal argv but different IDs;
        # only the exact global check is shared, never a historical preflight.
        self.assertEqual(len(physical), len(scheduler.quality['checks'])
            + len(scheduler._check_plan(self.fixture.plan['tasks'][1]))
            + len(self.fixture.plan['tasks'][0]['verification']['checks']))
        own_global = next(c for c in result['tasks']['T2']['checks'] if c['check_id']=='CHECK-GLOBAL')
        owner_global = next(c for c in receipt['owners']['T1']['checks'] if c['check_id']=='CHECK-GLOBAL')
        self.assertEqual(owner_global,own_global)
        self.assertNotIn('preflight/',owner_global['log_ref'])
        self.assertEqual(LimitsStore(root)._load(repair=False)[1], counters)
        before = scheduler.journal.path.read_bytes()
        scheduler.run(self.fixture.plan['tasks'])
        self.assertEqual(scheduler.journal.path.read_bytes(), before)

    def test_failed_owner_invalidates_it_and_dependents_without_accepting_candidate(self):
        root = self.selected(); scheduler = self.scheduler(root); self.failing_owner = True
        before = git(root / 'worktree', 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(TaskError, 'OWNER_REVALIDATION_FAILED'):
            scheduler.run(self.fixture.plan['tasks'])
        state = scheduler.journal.peek()
        self.assertEqual(state['tasks']['T1']['status'], 'INVALIDATED')
        self.assertEqual(state['tasks']['T2']['status'], 'FAILED')
        self.assertEqual(state['owner_revalidation_blocked']['closure'], ['T1', 'T2'])
        self.assertEqual(git(root / 'worktree', 'rev-parse', 'HEAD'), before)
        self.assertEqual(git(root / 'worktree', 'status', '--porcelain'), '')
        calls = list(self.observed)
        with self.assertRaisesRegex(TaskError, 'OWNER_REVALIDATION_FAILED'):
            self.scheduler(root).run(self.fixture.plan['tasks'])
        self.assertEqual(self.observed, calls)

    def test_native_development_entrypoint_enforces_owner_proof_and_charges_one_call(self):
        import orchestrator
        root=self.selected();before=LimitsStore(root)._load(repair=False)[1]
        calls=[]
        def provider(argv,**kwargs):
            calls.append(1)
            (Path(kwargs['cwd'])/'app.py').write_text('value = 3\n')
            return subprocess.CompletedProcess(argv,0,'TASK_DEV_STATUS: COMPLETE','')
        with (patch.object(orchestrator.ProjectRuntimeIsolation,'activate',return_value={'project_id':'offline'}),
              patch.object(orchestrator.ProjectRuntimeIsolation,'ensure_trusted_path'),
              patch.object(orchestrator,'assert_controller'),
              patch.object(orchestrator.SSDLCOrchestrator,'_verification_environment',return_value=self.fixture.identity['environment']),
              patch.object(orchestrator.EngineRunner,'_should_spawn_role_container',return_value=True),
              patch.object(orchestrator.EngineRunner,'_wrap_in_role_container',side_effect=lambda argv,**kwargs:argv),
              patch.object(orchestrator.EngineRunner,'_run_role_command',side_effect=provider),
              patch.object(orchestrator.EngineRunner,'run_sandboxed_cmd',return_value=('Ran 1 test\nOK','',0)),
              patch.object(orchestrator.SquadDispatcher,'dispatch_role',return_value=('codex','offline','low','fixture'))):
            native=orchestrator.SSDLCOrchestrator(self.fixture.repo,self.fixture.ticket.name,sync_remote=False)
            native.run_phase_dev()
        self.assertEqual(calls,[1])
        state=TaskJournal(root/'03-dev/tasks').peek()
        self.assertEqual(state['tasks']['T2']['status'],'COMPLETED')
        self.assertEqual(state['tasks']['T2']['owner_revalidations'][-1]['status'],'PASSED')
        self.assertEqual(len(LimitsStore(root)._load(repair=False)[1]['invocations']),len(before['invocations'])+1)

    def test_tampered_owner_log_refuses_a_completed_checkpoint(self):
        root = self.selected(); scheduler = self.scheduler(root)
        state = scheduler.run(self.fixture.plan['tasks'])
        check = state['tasks']['T2']['owner_revalidations'][-1]['owners']['T1']['checks'][0]
        (root / '03-dev/tasks' / check['log_ref']).write_text('changed proof')
        with self.assertRaisesRegex(TaskError, 'evidence'):
            self.scheduler(root).run(self.fixture.plan['tasks'])

    def test_owner_verifier_mutation_refuses_commit_and_retains_capture(self):
        root = self.selected(); scheduler = self.scheduler(root)
        original = scheduler._verify
        def mutate(task, record, directory, **kwargs):
            results = original(task, record, directory, **kwargs)
            if kwargs.get('event') == 'OWNER_CHECK_FINISHED':
                (root / 'worktree/app.py').write_text('mutated by check\n')
            return results
        scheduler._verify = mutate
        with self.assertRaisesRegex(TaskError, 'Owner verifier changed'):
            scheduler.run(self.fixture.plan['tasks'])
        row = scheduler.journal.peek()['tasks']['T2']
        self.assertEqual(row['status'], 'VERIFYING')
        self.assertTrue(row['capture_sha256'])
        self.assertNotEqual(git(root / 'worktree', 'rev-parse', 'HEAD'), row.get('commit'))

    def test_crash_inside_owner_checks_reuses_capture_without_developer_redispatch(self):
        root = self.selected(); scheduler = self.scheduler(root)
        save = scheduler._save
        def crash(event):
            save(event)
            if event == 'OWNER_CHECK_FINISHED': raise OSError('crash after owner check')
        scheduler._save = crash
        with self.assertRaisesRegex(OSError, 'crash'): scheduler.run(self.fixture.plan['tasks'])
        result = self.scheduler(root).run(self.fixture.plan['tasks'])
        self.assertEqual([x for x in self.observed if x[0] == 'developer'], [('developer', 3)])
        self.assertEqual(result['tasks']['T2']['attempts'], 3)
        self.assertEqual([r['status'] for r in result['tasks']['T2']['owner_revalidations']],
                         ['INTERRUPTED', 'PASSED'])

    def test_commit_intent_recovery_requires_retained_owner_proof(self):
        root = self.selected(); scheduler = self.scheduler(root)
        save = scheduler._save
        def crash(event):
            save(event)
            if event == 'COMMIT_INTENT': raise OSError('crash before commit')
        scheduler._save = crash
        with self.assertRaises(OSError): scheduler.run(self.fixture.plan['tasks'])
        row = scheduler.journal.peek()['tasks']['T2']
        check = row['owner_revalidations'][-1]['owners']['T1']['checks'][0]
        (root / '03-dev/tasks' / check['log_ref']).unlink()
        with self.assertRaisesRegex(TaskError, 'evidence'):
            self.scheduler(root).run(self.fixture.plan['tasks'])
        self.assertEqual([x for x in self.observed if x[0] == 'developer'], [('developer', 3)])


class CoverageGuardTests(unittest.TestCase):
    def test_changed_test_needs_exact_review_for_removed_assertions_and_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Path(directory);path=repo/'tests/test_old.py';path.parent.mkdir()
            original='def test_old():\n    assert value == 1\n'
            path.write_text(original)
            for args in [('init','-q'),('config','user.name','Offline fixture'),
                         ('config','user.email','fixture@example.invalid'),('add','.'),('commit','-qm','base')]:
                subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True)
            value=dict(base_head=git(repo,'rev-parse','HEAD'),files={'tests/test_old.py':dict(
                kind='BASELINE_TEST',owners=[],sha256=hashlib.sha256(original.encode()).hexdigest())})
            review=dict(contract_sha256=digest(value),coverage_preserved=True,approved_test_changes=[])
            scheduler=SimpleNamespace(wt=repo)
            for text in ('def test_old():\n    pass\n','@pytest.mark.xfail\n'+original):
                path.write_text(text)
                self.assertFalse(owner.coverage_ok(scheduler,value,review,['tests/test_old.py']))
                exception=dict(path='tests/test_old.py',old_sha256=value['files']['tests/test_old.py']['sha256'],
                    new_sha256=hashlib.sha256(text.encode()).hexdigest(),reason='Reviewed exact coverage change')
                approved=dict(review,approved_test_changes=[exception])
                self.assertTrue(owner.coverage_ok(scheduler,value,approved,['tests/test_old.py']))
                path.write_text(text+'\n# changed after review\n')
                self.assertFalse(owner.coverage_ok(scheduler,value,approved,['tests/test_old.py']))

    def test_assertion_removal_skip_and_xfail_are_not_preserved_syntax(self):
        original = owner._coverage_facts('def test_case():\n    assert value == 1\n')
        removed = owner._coverage_facts('def test_case():\n    pass\n')
        self.assertTrue(original[0] - removed[0])
        for marker in ('unittest.skip("reason")', 'unittest.expectedFailure', 'pytest.mark.xfail',
                       'pytest.mark.skipif(condition, reason="reason")'):
            changed = owner._coverage_facts('@' + marker + '\ndef test_case():\n    assert value == 1\n')
            self.assertTrue(changed[2] - original[2])

    def test_coverage_exceptions_require_exact_path_hash_and_reason(self):
        value = dict(files={'tests/test_old.py':dict(kind='COMPLETED_TEST',sha256='1'*64)})
        coverage = dict(contract_sha256=digest(value),coverage_preserved=True,approved_test_changes=[])
        change = dict(path='tests/test_old.py',old_sha256='1'*64,new_sha256='2'*64,reason='Reviewed equivalent assertion')
        owner.validate_review(value, dict(coverage,approved_test_changes=[change]))
        for field, wrong in [('path','elsewhere.py'),('old_sha256','0'*64),('new_sha256','unknown'),('reason','')]:
            with self.subTest(field=field), self.assertRaises(TaskError):
                owner.validate_review(value,dict(coverage,approved_test_changes=[dict(change,**{field:wrong})]))


class OwnerFeedbackTests(unittest.TestCase):
    def initialize(self, enabled):
        import test_reconciliation as reconciliation
        from test_feedback_delegation import grant
        from test_feedback_workflow import config
        original = reconciliation.ReconciliationTests.setUp
        def funded(f):
            original(f)
            policy = f.request['profile']
            policy['feedback_delegation'] = grant()
            policy['feedback_delegation']['allowed_files'].append('app.py')
            if enabled: policy['feedback_delegation']['owner_revalidation'] = True
            policy['feedback_workflow'] = config()
            policy['role_invocations'].update({'feedback-planner':2,'feedback-reviewer':2,'feedback-po':1})
            policy['phase_invocations']['feedback'] = 5
        class TypedScope(fixtures.ScopeAmendmentTests):
            def scheduler(self, root, developer):
                def wrapped(*args):
                    result = developer(*args)
                    if 'SCOPE_REQUIRED' in result['output']:
                        result['output'] = 'TASK_FEEDBACK_JSON: ' + json.dumps(dict(cause='MISSING_TARGET',
                            obligation_refs=args[0]['ac_refs'] + args[0]['sec_refs'], requested_files=['app.py'])) + '\nTASK_DEV_STATUS: SCOPE_REQUIRED'
                    return result
                return super().scheduler(root, wrapped)
        self.f = f = TypedScope()
        with patch.object(reconciliation.ReconciliationTests, 'setUp', funded): f.initialize()
        self.addCleanup(f.doCleanups)
        self.calls = []
        self.wrong_coverage = False

    def run_flow(self):
        import ssdlc_feedback_delegation as delegation
        import ssdlc_feedback_workflow as flow
        import orchestrator
        source = self.f.source
        def runner(source, limits, prompt, effort):
            case = json.loads(prompt.split('UNTRUSTED_CASE_JSON:\n')[1])['case']
            def invoke(actor, identity):
                self.calls.append(actor['identity'])
                if actor['identity']=='feedback-planner':
                    response = dict(route='AMENDMENT',requested_files=['app.py'],reason_code='native_owner_revalidation')
                else:
                    response = dict(proposal_sha256=case['proposal_sha256'],verdict='APPROVED',coverage_preserved=True,
                        reason_code='owner_obligations_preserved',owner_coverage=dict(
                            contract_sha256='0'*64 if self.wrong_coverage else case['owner_coverage_contract_sha256'],
                            coverage_preserved=True,approved_test_changes=[]))
                return dict(response=response,usage=dict(metered_usd=None,cli_estimate_usd=0.01))
            return invoke
        with (patch.object(delegation,'assert_controller'),patch.object(orchestrator,'assert_controller'),
              patch.object(delegation,'observe_environment',return_value=source['journal']['candidate_runtime']['environment']),
              patch.object(delegation,'docker_quiescent',return_value=True)):
            return flow.run_feedback(self.f.ticket,self.f.repo,Path(__file__).parent.parent,
                source['envelope']['binding']['controller'],source['journal']['candidate_runtime'],runner_factory=runner)

    def test_opt_in_feedback_amendment_carries_reviewed_owners_into_native_verification(self):
        from ssdlc_feedback import FeedbackStore
        self.initialize(True)
        original = copy.deepcopy(self.f.source['counters'])
        result = self.run_flow()
        self.assertEqual(result['disposition'],'APPLIED')
        root=runtime_root(self.f.ticket)
        state=TaskJournal(root/'03-dev/tasks').peek()
        contract=state['tasks']['T2']['scope_reconciliation']['owner_revalidation']['contract']
        self.assertEqual(set(contract['owners']),{'T1'})
        self.assertEqual(self.calls,['feedback-planner','feedback-reviewer'])
        def repair(task,attempt,prompt,workspace):
            (workspace/'app.py').write_text('value = 3\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE',exit_code=0,quiescent=True)
        state=self.f.scheduler(root,repair).run(json.loads((root/'02-design/plan.json').read_text())['tasks'])
        self.assertEqual(state['tasks']['T2']['owner_revalidations'][-1]['status'],'PASSED')
        current=LimitsStore(root)._load(repair=False)[1]
        self.assertEqual(current['deadline_at'],original['deadline_at'])
        self.assertEqual(len(current['invocations']),len(original['invocations'])+2)
        self.assertEqual(len(FeedbackStore(self.f.ticket).inspect()['amendments']),1)

    def test_old_grant_remains_operator_only_despite_reviewed_owner_coverage(self):
        self.initialize(False)
        self.assertEqual(self.run_flow()['disposition'],'OWNER_REVALIDATION_REQUIRED')
        self.assertEqual(runtime_root(self.f.ticket),self.f.source['root'])
        self.assertEqual(self.calls,['feedback-planner','feedback-reviewer'])

    def test_wrong_owner_coverage_is_captured_but_never_activated_or_reinferred(self):
        self.initialize(True);self.wrong_coverage=True
        with self.assertRaisesRegex(TaskError,'owner coverage'): self.run_flow()
        calls=list(self.calls);counters=LimitsStore(self.f.source['root'])._load(repair=False)[1]
        with self.assertRaisesRegex(TaskError,'owner coverage'): self.run_flow()
        self.assertEqual(self.calls,calls)
        self.assertEqual(LimitsStore(self.f.source['root'])._load(repair=False)[1],counters)

    def operator_bridge(self, *, changed_evidence=False):
        from ssdlc_feedback import FeedbackStore
        import ssdlc_feedback_delegation as delegation
        import ssdlc_feedback_workflow as flow
        import orchestrator
        f=self.f;source=f.source
        with (patch.object(delegation,'assert_controller'),patch.object(orchestrator,'assert_controller'),
              patch.object(delegation,'observe_environment',return_value=source['journal']['candidate_runtime']['environment']),
              patch.object(delegation,'docker_quiescent',return_value=True)):
            with delegation.fenced(f.ticket,Path(__file__).parent.parent,
                source['envelope']['binding']['controller'],source['journal']['candidate_runtime']) as session:
                store=flow.bootstrap(session,source)
                row=store.submit(source,flow.intake(source),flow.developer_actor(source))
        if changed_evidence:
            from ssdlc_scope_amendments import inspect
            (source['root']/'operator-observation.json').write_text('{"stopped": true}\n')
            source=f.source=inspect(f.ticket)
            with self.assertRaisesRegex(TaskError,'exact operator reconciliation'):
                subject(source,f.plan,f.reason,feedback_request_id=row['request_id'])
        value=subject(source,f.plan,f.reason,feedback_request_id=row['request_id'],
                      reconcile_feedback_evidence=changed_evidence)
        review=dict(f.review,subject_sha256=digest(value))
        staged=prepare(f.ticket,f.repo,f.plan,review,f.reason,runtime_identity=f.identity,
            controller_snapshot=f.new_controller,feedback_request_id=row['request_id'],
            reconcile_feedback_evidence=changed_evidence)
        # The flag is an explicit input to prepare too, not inferred from review.
        return staged,row,FeedbackStore(f.ticket)

    def test_operator_bridge_preserves_pending_request_and_admits_new_controller_without_models(self):
        from ssdlc_feedback_roles import FundedFeedbackRoles
        from ssdlc_feedback_delegation import descendant_of
        self.initialize(False);staged,row,store=self.operator_bridge()
        before=copy.deepcopy(self.f.source['counters'])
        activate(self.f.ticket,staged['preparation'],self.f.auth(staged))
        root=runtime_root(self.f.ticket);state=store.inspect()
        self.assertEqual(state['requests'][row['request_id']],row)
        self.assertEqual(store.disposition(row['request_id']),'APPLIED')
        self.assertEqual(len(state['owner_amendments']),1)
        self.assertEqual(store.public_projection()['operator_owner_amendments'],1)
        self.assertEqual(state['role_calls'],[])
        envelope,counters=FundedFeedbackRoles(store,LimitsStore(root),guard=lambda:None)._authority()
        self.assertEqual(envelope['binding']['controller'],self.f.new_controller)
        self.assertEqual(counters['invocations'],before['invocations'])
        self.assertEqual(counters['deadline_at'],before['deadline_at'])
        self.assertTrue(descendant_of(self.f.ticket,root,self.f.source['root']))
        snapshot=(store.root/'task-journal.jsonl').read_bytes()
        activate(self.f.ticket,staged['preparation'],self.f.auth(staged))
        self.assertEqual((store.root/'task-journal.jsonl').read_bytes(),snapshot)

    def test_operator_selector_crash_fences_until_receipt_replay_without_models(self):
        self.initialize(False);staged,row,store=self.operator_bridge()
        def crash(point):
            if point=='owner_selector_published': raise OSError('lost owner receipt')
        with self.assertRaises(OSError):
            activate(self.f.ticket,staged['preparation'],self.f.auth(staged),fault=crash)
        with self.assertRaisesRegex(TaskError,'reconciliation pending'): runtime_root(self.f.ticket)
        activate(self.f.ticket,staged['preparation'],self.f.auth(staged))
        self.assertEqual(len(store.inspect()['owner_amendments']),1)
        self.assertEqual(store.inspect()['role_calls'],[])
        self.assertEqual(runtime_root(self.f.ticket).name,staged['proposal']['transition_id'])

    def test_operator_receipt_consumes_the_same_allowance_as_later_delegation(self):
        from ssdlc_feedback_delegation import require_capacity
        self.initialize(False);staged,row,store=self.operator_bridge()
        activate(self.f.ticket,staged['preparation'],self.f.auth(staged))
        state=store.inspect();profile=copy.deepcopy(self.f.source['envelope']['policy'])
        profile['feedback_delegation']['max_amendments']=1
        later=copy.deepcopy(row);later['request_id']='later-request'
        later['request']['requested_files']=['other.py']
        later['path_classes']={'other.py':dict(kind='NEW_FILE',owners=[])}
        with self.assertRaisesRegex(TaskError,'quota exhausted'):
            require_capacity(state,later,profile)
        with self.assertRaisesRegex(TaskError,'allowance exhausted'):
            owner._capacity(state,'T2',['other.py'],profile['feedback_delegation'])
        self.assertEqual(store.inspect()['role_calls'],[])

    def test_operator_explicitly_reconciles_only_registered_evidence_with_same_semantic_inputs(self):
        self.initialize(False);staged,row,store=self.operator_bridge(changed_evidence=True)
        recipe=json.loads((Path(staged['preparation'])/'recipe.json').read_text())
        reconciliation=recipe['subject']['owner_feedback']['source_reconciliation']
        self.assertNotEqual(reconciliation['old_source_files_sha256'],reconciliation['current_source_files_sha256'])
        activate(self.f.ticket,staged['preparation'],self.f.auth(staged))
        self.assertEqual(store.inspect()['requests'][row['request_id']],row)
        self.assertEqual(store.inspect()['role_calls'],[])
        self.assertEqual(runtime_root(self.f.ticket).name,staged['proposal']['transition_id'])

    def test_evidence_reconciliation_never_waives_changed_counters_capture_or_request_log(self):
        self.initialize(False);staged,row,store=self.operator_bridge()
        source=self.f.source
        changed=copy.deepcopy(source)
        changed['counters']['deadline_at']+=1
        with self.assertRaisesRegex(TaskError,'semantic inputs must match'):
            owner.feedback_binding(self.f.ticket,changed,['app.py'],row['request_id'],reconcile_evidence=True)
        changed=copy.deepcopy(source)
        changed['journal']['tasks']['T2']['capture_sha256']='0'*64
        with self.assertRaisesRegex(TaskError,'semantic inputs must match'):
            owner.feedback_binding(self.f.ticket,changed,['app.py'],row['request_id'],reconcile_evidence=True)
        changed=copy.deepcopy(source)
        ref=next(iter(row['request']['evidence_refs']))
        changed['files'][ref]='0'*64
        with self.assertRaisesRegex(TaskError,'evidence'):
            owner.feedback_binding(self.f.ticket,changed,['app.py'],row['request_id'],reconcile_evidence=True)

    def test_operator_bridge_rejects_changed_feedback_before_selector_publication(self):
        self.initialize(False);staged,row,store=self.operator_bridge()
        state=store.inspect();state['requests'][row['request_id']]['rounds']+=1
        store._append('TEST_REQUEST_CHANGED',state)
        before=(self.f.ticket/'execution/active.json').read_bytes()
        with self.assertRaisesRegex(TaskError,'changed after coverage review'):
            activate(self.f.ticket,staged['preparation'],self.f.auth(staged))
        self.assertEqual((self.f.ticket/'execution/active.json').read_bytes(),before)
        self.assertEqual(store.inspect()['role_calls'],[])
