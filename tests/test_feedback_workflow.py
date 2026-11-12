"""F3 native workflow with deterministic roles, real checkpoints and accounting."""
import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_scope_amendments as fixtures
from test_feedback_delegation import grant
import ssdlc_feedback_delegation as delegated
import ssdlc_feedback_workflow as flow
import orchestrator as orch_module
import ssdlc_supervisor as sup
from ssdlc_feedback import FeedbackStore, FeedbackError
from ssdlc_feedback_roles import FundedFeedbackRoles
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import inspect
from ssdlc_tasks import TaskError, digest, ticket_lock
from ssdlc_transitions import runtime_root


def config():
    return dict(schema_version=1, enabled=True, roles={
        'planner': dict(engine='claude', model='planner', effort='low'),
        'reviewer': dict(engine='agy', model='reviewer', effort='low'),
        'po': dict(engine='codex', model='product-owner', effort='low')})


class FeedbackWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.initialize()

    def initialize(self, cause='MISSING_TARGET'):
        original = fixtures.fixtures.ReconciliationTests.setUp
        def funded(f):
            original(f)
            f.request['profile']['feedback_delegation'] = grant()
            f.request['profile']['feedback_workflow'] = config()
            f.request['profile']['role_invocations'].update(
                {'feedback-planner': 2, 'feedback-reviewer': 2, 'feedback-po': 1})
            f.request['profile']['phase_invocations']['feedback'] = 5
        class WorkflowCase(fixtures.ScopeAmendmentTests):
            def scheduler(self, root, developer):
                def wrapped(*args):
                    value = developer(*args)
                    if 'SCOPE_REQUIRED' in value['output']:
                        request = dict(cause=cause, obligation_refs=args[0]['ac_refs'] + args[0]['sec_refs'],
                                       requested_files=['helper.py'] if cause == 'MISSING_TARGET' else [])
                        value['output'] = ('TASK_FEEDBACK_JSON: ' + json.dumps(request)
                                           + '\nTASK_DEV_STATUS: SCOPE_REQUIRED')
                    return value
                return super().scheduler(root, wrapped)
        self.case = f = WorkflowCase()
        with patch.object(fixtures.fixtures.ReconciliationTests, 'setUp', funded):
            f.initialize()
        self.addCleanup(f.doCleanups)
        self.source = f.source
        self.snapshot = self.source['envelope']['binding']['controller']
        self.identity = self.source['journal']['candidate_runtime']
        self.calls = []

    @contextlib.contextmanager
    def host(self):
        with (patch.object(delegated, 'assert_controller'),
              patch.object(orch_module, 'assert_controller'),
              patch.object(delegated, 'observe_environment', return_value=self.identity['environment']),
              patch.object(delegated, 'docker_quiescent', return_value=True)):
            yield

    def runner(self, source, limits, prompt, effort):
        case = json.loads(prompt.split('UNTRUSTED_CASE_JSON:\n')[1])['case']
        def call(metadata, invocation):
            self.calls.append((metadata['identity'], invocation))
            if metadata['identity'] == 'feedback-planner':
                response = dict(route='AMENDMENT', requested_files=['helper.py'], reason_code='required_helper')
            elif metadata['identity'] == 'feedback-po':
                response = dict(recommendation='OPERATOR_REQUIRED', reason_code='new_requirement')
            else:
                response = dict(proposal_sha256=case['proposal_sha256'], verdict='APPROVED',
                                coverage_preserved=True, reason_code='coverage_preserved')
            return dict(response=response, usage=dict(metered_usd=None, cli_estimate_usd=0.01))
        return call

    def run_flow(self, **kwargs):
        with self.host():
            return flow.run_feedback(self.case.ticket, self.case.repo, Path(__file__).parent.parent,
                                     self.snapshot, self.identity, runner_factory=self.runner, **kwargs)

    def test_native_bootstrap_and_funded_activation_without_human_attestation(self):
        before = copy.deepcopy(self.source['counters'])
        result = self.run_flow()
        self.assertEqual(result['disposition'], 'APPLIED')
        store = FeedbackStore(self.case.ticket)
        authority, state = store._read()
        self.assertEqual(authority['authorization']['contract'], 'native-feedback-recording-v1')
        self.assertNotIn('source_fenced', authority['authorization'])
        self.assertEqual(len(state['role_calls']), 2)
        self.assertEqual(self.case.calls, [1, 2])
        current = LimitsStore(Path(result['runtime']))._load(repair=False)[1]
        self.assertEqual(len(current['invocations']), len(before['invocations']) + 2)
        self.assertEqual(current['deadline_at'], before['deadline_at'])
        self.assertEqual(current['started_at'], before['started_at'])
        self.assertEqual(sup.pinned_root(self.case.repo, self.case.ticket.name,
            dict(authorized={'runtime_root': str(self.source['root'].relative_to(self.case.repo))})), Path(result['runtime']))
        from ssdlc_feedback_delivery import metrics
        summary = metrics(store)
        self.assertEqual(summary['role_count'], 2)
        self.assertTrue(all(c['duration_seconds'] >= 0 for c in summary['consultations']))
        self.assertTrue(summary['requests'][0]['coverage_preserved'])
        self.assertIsNotNone(summary['requests'][0]['activation_latency_seconds'])

    def test_captured_planner_return_resumes_without_another_planner_call(self):
        with self.host(), delegated.fenced(self.case.ticket, Path(__file__).parent.parent,
                                           self.snapshot, self.identity) as owner:
            store = flow.bootstrap(owner, self.source)
            row = store.submit(self.source, flow.intake(self.source), flow.developer_actor(self.source))
            roles = FundedFeedbackRoles(store, LimitsStore(self.source['root']), guard=owner.check)
            role = flow.role_actor(config(), row['request_id'], 'planner')
            prompt = flow.role_prompt(self.source, row, 'planner')
            def crash(point):
                if point == 'response_captured':
                    raise OSError('lost response before native finish')
            with self.assertRaises(OSError):
                roles.run(row['request_id'], 'planner', role, self.runner(self.source, roles.limits, prompt, 'low'), fault=crash)
        result = self.run_flow()
        self.assertEqual(result['disposition'], 'APPLIED')
        self.assertEqual([name for name, _ in self.calls], ['feedback-planner', 'feedback-reviewer'])

    def test_bootstrap_crash_replays_same_authority_and_no_model_call(self):
        with self.host(), delegated.fenced(self.case.ticket, Path(__file__).parent.parent,
                                           self.snapshot, self.identity) as owner:
            def crash(point):
                if point == 'policy_journal':
                    raise OSError('interrupted bootstrap')
            with self.assertRaises(OSError):
                flow.bootstrap(owner, self.source, fault=crash)
            store = flow.bootstrap(owner, self.source)
            state = store.inspect()
            self.assertEqual(state['role_calls'], [])
            self.assertEqual(state['requests'], {})
        self.assertEqual(self.calls, [])

    def test_requirement_conflict_uses_funded_consultative_po_only(self):
        self.initialize('REQUIREMENT_CONFLICT')
        before = self.source['parent_selector']
        result = self.run_flow()
        self.assertEqual(result['disposition'], 'OPERATOR_DECISION')
        self.assertEqual([name for name, _ in self.calls], ['feedback-po'])
        self.assertEqual(inspect(self.case.ticket)['parent_selector'], before)
        self.assertEqual(FeedbackStore(self.case.ticket).inspect()['amendments'], [])

    def test_read_only_cli_consumes_one_native_reservation_with_observed_usage(self):
        with self.host(), delegated.fenced(self.case.ticket, Path(__file__).parent.parent,
                                           self.snapshot, self.identity) as owner:
            store = flow.bootstrap(owner, self.source)
            row = store.submit(self.source, flow.intake(self.source), flow.developer_actor(self.source))
            limits = LimitsStore(self.source['root'])
            metadata = flow.role_actor(config(), row['request_id'], 'planner')
            response = dict(route='REJECTED', requested_files=[], reason_code='do_not_weaken_test')
            cli = subprocess.CompletedProcess(['claude'], 0, json.dumps({'result':json.dumps(response),'total_cost_usd':0.05}), '')
            runner = flow.ReadOnlyRoleRunner(self.source, limits, flow.role_prompt(self.source, row, 'planner'), 'low')
            before = len(limits._load(repair=False)[1]['invocations'])
            with (patch.object(orch_module.EngineRunner, '_should_spawn_role_container', return_value=True),
                  patch.object(orch_module.EngineRunner, '_wrap_in_role_container', side_effect=lambda cmd, **kw: cmd),
                  patch.object(orch_module.EngineRunner, '_run_role_command', return_value=cli) as provider):
                result = FundedFeedbackRoles(store, limits, guard=owner.check).run(row['request_id'], 'planner', metadata, runner)
            self.assertEqual(result, response)
            self.assertEqual(provider.call_count, 1)
            current = limits._load(repair=False)[1]
            self.assertEqual(len(current['invocations']), before + 1)
            paid = [v for v in current['invocations'].values() if v['role'] == 'feedback-planner']
            self.assertEqual(paid[0]['usage'], {'metered_usd':None,'cli_estimate_usd':0.05})
            receipt=store.inspect()['role_calls'][0]['receipt']['response']
            self.assertEqual(receipt['raw_output']['text'],json.dumps(response))

    def test_cli_format_error_retains_raw_response_and_known_cost_without_retry(self):
        with self.host(), delegated.fenced(self.case.ticket, Path(__file__).parent.parent,
                                           self.snapshot, self.identity) as owner:
            store=flow.bootstrap(owner,self.source)
            row=store.submit(self.source,flow.intake(self.source),flow.developer_actor(self.source))
            limits=LimitsStore(self.source['root']); roles=FundedFeedbackRoles(store,limits,guard=owner.check)
            metadata=flow.role_actor(config(),row['request_id'],'planner')
            runner=flow.ReadOnlyRoleRunner(self.source,limits,flow.role_prompt(self.source,row,'planner'),'low')
            cli=subprocess.CompletedProcess(['claude'],0,json.dumps({'result':'MALFORMED_PRIVATE_RESPONSE','total_cost_usd':0.05}),'')
            def provider(cmd,**kwargs):
                bundles=list(Path(kwargs['cwd']).glob('.feedback-context-*/input.json'))
                self.assertEqual(len(bundles),1)
                evidence=json.loads(bundles[0].read_text())
                self.assertEqual(evidence['plan'],self.source['plan'])
                self.assertTrue(evidence['evidence'])
                return cli
            with (patch.object(orch_module.EngineRunner,'_should_spawn_role_container',return_value=True),
                  patch.object(orch_module.EngineRunner,'_wrap_in_role_container',side_effect=lambda cmd,**kw:cmd),
                  patch.object(orch_module.EngineRunner,'_run_role_command',side_effect=provider) as native):
                result=roles.run(row['request_id'],'planner',metadata,runner)
                call=store.inspect()['role_calls'][0]
                self.assertEqual(result,{'protocol_error':'INVALID_JSON'})
                self.assertEqual(call['receipt']['response']['raw_output']['text'],'MALFORMED_PRIVATE_RESPONSE')
                self.assertEqual(roles.resume_response(call['operation_id']),result)
                with self.assertRaisesRegex(TaskError,'already reserved'):
                    roles.run(row['request_id'],'planner',metadata,runner)
                self.assertEqual(native.call_count,1)
            invocation=limits._load(repair=False)[1]['invocations'][call['native']['invocation_id']]
            self.assertEqual(invocation['status'],'returned')
            self.assertEqual(invocation['usage']['cli_estimate_usd'],0.05)

    def test_orchestrator_adopts_generation_then_runs_next_normal_attempt(self):
        orch = orch_module.SSDLCOrchestrator.__new__(orch_module.SSDLCOrchestrator)
        orch.repo_root, orch.ticket_id, orch.project_id = self.case.repo, self.case.ticket.name, 'offline'
        orch.ticket_lock_path = self.case.ticket / 'ticket.lock'
        orch.ssdlc_root = self.source['root']; orch._registry_lock = threading.RLock()
        orch.dir_recon, orch.dir_design, orch.dir_dev = (orch.ssdlc_root / n for n in ('00-recon','02-design','03-dev'))
        orch.worktree_dir, orch.state_file = orch.ssdlc_root / 'worktree', orch.ssdlc_root / 'state.json'
        orch.state = json.loads(orch.state_file.read_text())
        orch._ensure_task_plan = lambda: json.loads((orch.dir_design / 'plan.json').read_text())
        orch._ensure_controller_contract = lambda: flow.execution_contract(self.snapshot)
        orch._ensure_worktree = lambda: orch.worktree_dir
        orch._verification_environment = lambda *args: self.identity['environment']
        orch._task_mode = lambda: True
        def cli(cmd, **kwargs):
            Path(kwargs['cwd'], 'helper.py').write_text('value = 1\n')
            return subprocess.CompletedProcess(cmd, 0, 'TASK_DEV_STATUS: COMPLETE', '')
        previous = getattr(orch_module._dispatch_local, 'limits', None)
        orch_module._dispatch_local.limits = LimitsStore(self.source['root'])
        try:
            with (self.host(), ticket_lock(orch.ticket_lock_path),
                  patch.object(flow, 'ReadOnlyRoleRunner', side_effect=self.runner),
                  patch.object(orch_module.EngineRunner, '_should_spawn_role_container', return_value=True),
                  patch.object(orch_module.EngineRunner, '_wrap_in_role_container', side_effect=lambda cmd, **kw: cmd),
                  patch.object(orch_module.EngineRunner, '_run_role_command', side_effect=cli),
                  patch.object(orch_module.EngineRunner, 'run_sandboxed_cmd', return_value=('Ran 1 test\nOK','',0)),
                  patch.object(orch_module.SquadDispatcher, 'dispatch_role', return_value=('codex','offline','low','fixture'))):
                diff, _ = orch._run_task_development()
            self.assertNotEqual(orch.ssdlc_root, self.source['root'])
            self.assertEqual(orch.ssdlc_root, runtime_root(self.case.ticket))
            self.assertEqual(orch.state['tasks_status']['T2']['attempts'], 3)
            self.assertEqual(orch.state['tasks_status']['T2']['status'], 'COMPLETED')
            self.assertIn('helper.py', diff.read_text())
            orch.remote_sync=SimpleNamespace(enabled=False)
            orch._completion_disclosures=lambda:''
            orch.state.setdefault('goal','offline fixture')
            with (ticket_lock(orch.ticket_lock_path),
                  patch('ssdlc_feedback_delivery.metrics',side_effect=[{'sample':1},{'sample':2}])):
                hashes=[]
                for _ in range(2):
                    pack=orch.compile_evidence_pack(ready_for_signoff=False)
                    current=orch_module.sha256_file(orch.ssdlc_root/'feedback-summary.json')
                    hashes.append(current)
                    self.assertIn(current[:16]+'...',pack.read_text())
                    self.assertEqual(orch.state['artifacts']['feedback-summary.json']['sha256'],current)
                    self.assertNotIn('| [`EVIDENCE_PACK.md`]',pack.read_text())
            self.assertNotEqual(hashes[0],hashes[1])
        finally:
            orch_module._dispatch_local.limits = previous

    def test_pending_publication_is_observable_and_resume_finishes_without_models(self):
        saved = {}
        activate = flow.activate
        def interrupted(owner, preparation):
            saved['source'] = {ref:(self.source['root']/ref).read_bytes() for ref in inspect(self.case.ticket)['files']}
            def crash(point):
                if point == 'after_activation': raise OSError('lost selector acknowledgment')
            return activate(owner, preparation, fault=crash)
        with patch.object(flow, 'activate', side_effect=interrupted), self.assertRaises(OSError):
            self.run_flow()
        record = dict(authorized={'runtime_root':str(self.source['root'].relative_to(self.case.repo))})
        with self.assertRaises(TaskError): runtime_root(self.case.ticket)
        self.assertEqual(sup.pinned_root(self.case.repo, self.case.ticket.name, record, observe=True), self.source['root'])
        self.assertEqual(sup.peek_journal(self.case.repo, self.case.ticket.name, record)[0]['tasks']['T2']['attempts'], 2)
        with self.host(), patch('ssdlc_controller.controller_snapshot', return_value=self.snapshot):
            self.assertTrue(flow.recover_preparation(self.case.repo, self.case.ticket.name))
        current = runtime_root(self.case.ticket)
        self.assertEqual(sup.pinned_root(self.case.repo, self.case.ticket.name, record), current)
        self.assertEqual({ref:(self.source['root']/ref).read_bytes() for ref in saved['source']}, saved['source'])
        self.assertEqual(len(self.calls), 2)

    def test_rejected_request_replays_without_identical_developer_or_role_dispatch(self):
        original = self.runner
        def reject(source, limits, prompt, effort):
            run = original(source, limits, prompt, effort)
            def result(metadata, invocation):
                value = run(metadata, invocation)
                if metadata['identity'] == 'feedback-planner':
                    value['response'] = dict(route='REJECTED', requested_files=[], reason_code='fix_code_preserve_test')
                return value
            return result
        self.runner = reject
        self.assertEqual(self.run_flow()['disposition'], 'REJECTED_NO_RETRY')
        self.assertEqual(self.run_flow()['disposition'], 'REJECTED_NO_RETRY')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.case.calls, [1, 2])
        self.assertEqual(inspect(self.case.ticket)['journal']['tasks']['T2']['attempts'], 2)

    def test_unknown_response_is_not_retried(self):
        def failing(source, limits, prompt, effort):
            def run(metadata, invocation):
                self.calls.append((metadata['identity'], invocation))
                raise TimeoutError('unconfirmed provider outcome')
            return run
        self.runner = failing
        with self.assertRaises(TimeoutError): self.run_flow()
        with self.assertRaisesRegex(TaskError, 'Unresolved invocation'):
            self.run_flow()
        self.assertEqual(len(self.calls), 1)

    def test_prerequisite_cycles_and_unsupported_return_never_change_native_dag(self):
        from ssdlc_feedback_delivery import prerequisite, prerequisite_return
        self.initialize('MISSING_PREREQUISITE')
        original = self.runner
        def propose(source, limits, prompt, effort):
            run = original(source, limits, prompt, effort)
            def result(metadata, invocation):
                value = run(metadata, invocation)
                if metadata['identity'] == 'feedback-planner':
                    value['response'] = dict(route='PREREQUISITE', requested_files=[], reason_code='external_dependency')
                return value
            return result
        self.runner = propose
        result = self.run_flow()
        self.assertEqual(result['disposition'], 'BLOCKED_PREREQUISITE')
        store = FeedbackStore(self.case.ticket); state = store.inspect()
        child = state['requests'][result['request_id']]['prerequisite']['child']
        journal = (store.root / 'task-journal.jsonl').read_bytes()
        with self.assertRaisesRegex(TaskError, 'Circular'):
            prerequisite(store, result['request_id'], child, [[child,self.case.ticket.name]])
        self.assertFalse(prerequisite_return(store, result['request_id'], child)['execution_authorized'])
        self.assertEqual((store.root / 'task-journal.jsonl').read_bytes(), journal)
        self.assertEqual(inspect(self.case.ticket)['plan'], self.source['plan'])
        self.assertEqual(state['amendments'], [])

    def test_prepaid_capability_is_consumed_once(self):
        limits = LimitsStore(self.source['root'])
        invocation = limits.reserve('feedback-planner','feedback','claude')
        result = subprocess.CompletedProcess(['claude'],0,json.dumps({'result':'{}'}),'')
        with (patch.object(orch_module.EngineRunner, '_should_spawn_role_container', return_value=True),
              patch.object(orch_module.EngineRunner, '_wrap_in_role_container', side_effect=lambda cmd, **kw:cmd),
              patch.object(orch_module.EngineRunner, '_run_role_command', return_value=result) as provider):
            with orch_module._prepaid_feedback(limits, invocation, 'feedback-planner','claude'):
                orch_module.EngineRunner.run_claude('{}', self.source['root']/'worktree', require_container=True)
                with self.assertRaisesRegex(TaskError,'Prepaid feedback reservation'):
                    orch_module.EngineRunner.run_claude('{}', self.source['root']/'worktree', require_container=True)
        self.assertEqual(provider.call_count, 1)

    def test_feedback_container_omits_host_gh_config_and_cross_role_histories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source=root/'source'; source.mkdir()
            (root/'.ssdlc').mkdir()  # An unrelated ancestor must never become the feedback mount root.
            toolkit=root/'toolkit'; toolkit.mkdir(); (toolkit/'.ssdlc').mkdir()
            (toolkit/'.git').write_text('gitdir: /private/host/git\n')
            home=root/'home'; home.mkdir(); (home/'.claude').mkdir(); (home/'.gemini').mkdir(); (home/'.codex').mkdir()
            (home/'.claude.json').write_text('PRIVATE_GITHUB_MCP_SECRET')
            (home/'.claude/.credentials.json').write_text('subscription')
            (home/'.codex/auth.json').write_text('other_role_auth')
            old = getattr(orch_module._dispatch_local, 'prepaid', None)
            orch_module._dispatch_local.prepaid = {'engine':'claude'}
            try:
                with (patch.object(orch_module, '__file__', str(toolkit/'orchestrator.py')),
                      patch.object(Path, 'home', return_value=home),
                      patch.dict(os.environ, {'GH_TOKEN':'PRIVATE_GH_TOKEN','CODEX_HOME':'/private/host/codex'})):
                    argv = orch_module.EngineRunner._wrap_in_role_container(['claude'], source, stdin=True)
                combined=' '.join(argv)
                for secret in ('PRIVATE_GH_TOKEN','/private/host/git','/private/host/codex',str(home/'.claude.json'),str(home/'.codex/auth.json'),str(home/'.gemini')):
                    self.assertNotIn(secret, combined)
                self.assertIn('/opt/ssdlc/.ssdlc:rw', combined)
                self.assertIn(str(home/'.claude/.credentials.json'), combined)
                self.assertIn(str(source)+':ro', combined)
            finally:
                orch_module._dispatch_local.prepaid=old

    def test_native_lineage_exempts_only_runtime_fingerprint_and_preflight_includes_roles(self):
        from helpers.supervisor_fakes import FakeProbe
        before = self.source['root']
        result = self.run_flow(); after = Path(result['runtime'])
        old = dict(authorized=dict(runtime_root=str(before.relative_to(self.case.repo)), goal_sha256='goal',
            tier='M', test_cmd_sha256='test'), fingerprints=dict(config_sha256='config',controller='controller'))
        new = copy.deepcopy(old); new['authorized']['runtime_root']=str(after.relative_to(self.case.repo))
        advance = sup.native_advance(self.case.repo,self.case.ticket.name,old,new)
        self.assertTrue(advance)
        self.assertEqual(sup.fingerprint_changes(old,new,advance), [])
        new['fingerprints']['controller']='different-controller'
        self.assertEqual([r['field'] for r in sup.fingerprint_changes(old,new,advance)], ['controller'])
        probes={engine:FakeProbe.adapter(dict(status='ok',code='auth_verified')) for engine in ('codex','claude','agy')}
        result=sup.preflight(self.case.repo,self.case.ticket.name,{'PATH':'/usr/bin:/bin','SSDLC_EXECUTION_MODE':'local'},
            route_resolver=lambda *args:{'codex'}, which=lambda *args,**kw:'/fake/bin',probes=probes)
        self.assertTrue({'auth_codex','auth_claude','auth_agy'} <= {r['id'] for r in result['checks']})


if __name__ == '__main__':
    unittest.main()
