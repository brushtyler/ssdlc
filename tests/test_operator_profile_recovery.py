"""Exhausted I2 owner tasks recover only through exact fresh operator consent."""
import copy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_owner_revalidation as owners
from ssdlc_feedback import FeedbackStore
from ssdlc_feedback_workflow import intake, developer_actor
from ssdlc_operator_recovery import required_profile, subject, CONTRACT
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import inspect, prepare, activate
from ssdlc_tasks import TaskError, TaskJournal, atomic_json, digest
from ssdlc_transitions import runtime_root


class OperatorProfileRecoveryTests(unittest.TestCase):
    def initialize(self, *, two_receipts=False):
        case = owners.OwnerFeedbackTests()
        case.initialize(False, attempt_cap=4 if two_receipts else 3, owner_limit=1)
        self.addCleanup(case.doCleanups)
        self.case, self.f = case, case.f
        self.store = FeedbackStore(self.f.ticket)
        if two_receipts:
            first, _, _ = case.operator_bridge()
            activate(self.f.ticket, first['preparation'], self.f.auth(first))
            self.missing(['other.py'])
            source = inspect(self.f.ticket)
            row = self.store.submit(source, intake(source), developer_actor(source))
            plan = copy.deepcopy(source['plan']); plan['plan_revision'] += 1
            plan['tasks'][1]['target_files'] += ['other.py']
            from ssdlc_scope_amendments import subject as ordinary_subject
            value = ordinary_subject(source, plan, self.f.reason,
                feedback_request_id=row['request_id'], operator_owner_limit=2)
            staged = prepare(self.f.ticket, self.f.repo, plan, self.review(value), self.f.reason,
                runtime_identity=self.f.identity, controller_snapshot=self.f.new_controller,
                feedback_request_id=row['request_id'], operator_owner_limit=2)
            auth = self.f.auth(staged)
            auth['owner_allowance_sha256'] = staged['proposal']['owner_allowance_sha256']
            activate(self.f.ticket, staged['preparation'], auth)
            self.missing(['third.py', 'fourth.py'])
            self.source = inspect(self.f.ticket)
            self.row = self.store.submit(self.source, intake(self.source), developer_actor(self.source))
        else:
            self.assertEqual(case.run_flow()['disposition'], 'OWNER_REVALIDATION_REQUIRED')
            source = inspect(self.f.ticket)
            row = next(iter(self.store.inspect()['requests'].values()))
            from ssdlc_scope_amendments import subject as ordinary_subject
            value = ordinary_subject(source, self.f.plan, self.f.reason,
                feedback_request_id=row['request_id'], reconcile_feedback_evidence=True)
            staged = prepare(self.f.ticket, self.f.repo, self.f.plan, self.review(value), self.f.reason,
                runtime_identity=self.f.identity, controller_snapshot=self.f.new_controller,
                feedback_request_id=row['request_id'], reconcile_feedback_evidence=True)
            activate(self.f.ticket, staged['preparation'], self.f.auth(staged))
            self.missing(['other.py'])
            self.source = inspect(self.f.ticket)
            self.row = self.store.submit(self.source, intake(self.source), developer_actor(self.source))
        self.paths = self.row['request']['requested_files']
        self.plan = copy.deepcopy(self.source['plan']); self.plan['plan_revision'] += 1
        self.plan['tasks'][1]['target_files'] += self.paths
        self.profile = required_profile(self.source, self.paths, self.store.inspect())
        self.owner_limit = 3 if two_receipts else 2

    def missing(self, paths):
        self.f.requested_paths = paths
        root = runtime_root(self.f.ticket)
        def developer(task, attempt, prompt, workspace):
            store = LimitsStore(root)
            invocation = store.reserve('03-dev', '03-dev', 'codex')
            store.finish(invocation, 'returned')
            return dict(output='TASK_DEV_STATUS: SCOPE_REQUIRED', exit_code=0,
                        quiescent=True, engine='codex', model='offline')
        with self.assertRaisesRegex(TaskError, 'SCOPE_REQUIRED'):
            self.f.scheduler(root, developer).run(json.loads((root/'02-design/plan.json').read_text())['tasks'])
        outer = json.loads((root/'state.json').read_text()); outer['status'] = 'FAILED'
        atomic_json(root/'state.json', outer)

    def value(self, profile=None):
        return subject(self.source, self.plan, self.f.reason,
            profile=self.profile if profile is None else profile,
            feedback_request_id=self.row['request_id'], reconcile_feedback_evidence=True,
            operator_owner_limit=self.owner_limit)

    def review(self, value):
        return dict(subject_sha256=digest(value), reviewer='independent operator scope fixture',
            report=dict(verdict='APPROVED', findings=[]), scope_diff=value['scope_diff'],
            owner_coverage=dict(contract_sha256=digest(value['owner_revalidation']),
                                coverage_preserved=True, approved_test_changes=[]))

    def stage(self):
        value = self.value()
        staged = prepare(self.f.ticket, self.f.repo, self.plan, self.review(value), self.f.reason,
            runtime_identity=self.f.identity, controller_snapshot=self.f.new_controller,
            feedback_request_id=self.row['request_id'], reconcile_feedback_evidence=True,
            operator_owner_limit=self.owner_limit, operator_recovery_profile=self.profile)
        auth = self.f.auth(staged)
        auth.update(profile_sha256=staged['proposal']['profile_sha256'],
            operator_recovery_sha256=staged['proposal']['operator_recovery_sha256'], authorize_extensions=True)
        if 'owner_allowance_sha256' in staged['proposal']:
            auth['owner_allowance_sha256'] = staged['proposal']['owner_allowance_sha256']
        return staged, auth

    def test_exhausted_task_recovers_with_funded_history_and_fresh_owner_checks(self):
        self.initialize()
        original = copy.deepcopy(self.source['counters'])
        calls = list(self.case.calls)
        authority = (self.store.root/'authority.json').read_bytes()
        staged, auth = self.stage()
        self.assertEqual(staged['proposal']['authorization_contract'], CONTRACT)
        activate(self.f.ticket, staged['preparation'], auth)
        root = runtime_root(self.f.ticket)
        envelope, counters = LimitsStore(root)._load(repair=False)
        self.assertEqual(envelope['policy'], self.profile)
        self.assertEqual(counters['invocations'], original['invocations'])
        self.assertEqual(counters['deadline_at'], original['deadline_at'])
        self.assertEqual(self.case.calls, calls)
        self.assertEqual((self.store.root/'authority.json').read_bytes(), authority)
        self.assertEqual(self.store.inspect()['requests'][self.row['request_id']], self.row)
        from ssdlc_feedback_roles import FundedFeedbackRoles
        admitted, _ = FundedFeedbackRoles(self.store, LimitsStore(root), guard=lambda:None)._authority()
        self.assertEqual(admitted['policy'], self.profile)
        self.assertEqual(TaskJournal(root/'03-dev/tasks').peek()['tasks']['T2']['attempts'], 3)
        import orchestrator
        dispatches = []
        def provider(argv, **kwargs):
            dispatches.append(1)
            (Path(kwargs['cwd'])/'app.py').write_text('value = 3\n')
            return subprocess.CompletedProcess(argv,0,'TASK_DEV_STATUS: COMPLETE','')
        with (patch.object(orchestrator.ProjectRuntimeIsolation,'activate',return_value={'project_id':'offline'}),
              patch.object(orchestrator.ProjectRuntimeIsolation,'ensure_trusted_path'),
              patch.object(orchestrator,'assert_controller'),
              patch.object(orchestrator.SSDLCOrchestrator,'_verification_environment',return_value=self.f.identity['environment']),
              patch.object(orchestrator.EngineRunner,'_should_spawn_role_container',return_value=True),
              patch.object(orchestrator.EngineRunner,'_wrap_in_role_container',side_effect=lambda argv,**kwargs:argv),
              patch.object(orchestrator.EngineRunner,'_run_role_command',side_effect=provider),
              patch.object(orchestrator.EngineRunner,'run_sandboxed_cmd',return_value=('Ran 1 test\nOK','',0)),
              patch.object(orchestrator.SquadDispatcher,'dispatch_role',return_value=('codex','offline','low','fixture'))):
            native=orchestrator.SSDLCOrchestrator(self.f.repo,self.f.ticket.name,sync_remote=False)
            native.run_phase_dev()
        self.assertEqual(dispatches,[1])
        state = TaskJournal(root/'03-dev/tasks').peek()
        self.assertEqual(state['tasks']['T2']['attempts'],4)
        self.assertEqual(state['tasks']['T2']['status'], 'COMPLETED')
        self.assertEqual(state['tasks']['T2']['owner_revalidations'][-1]['status'], 'PASSED')
        self.assertEqual(self.store.public_projection()['operator_owner_recoveries'], 1)
        after = LimitsStore(root)._load(repair=False)[1]
        self.assertEqual(len(after['invocations']), len(original['invocations'])+1)
        self.assertTrue(all(after['invocations'][k]==v for k,v in original['invocations'].items()))

    def test_exhausted_scope_and_file_capacity_requires_separate_consent_and_replays_once(self):
        self.initialize(two_receipts=True)
        self.assertEqual(self.source['journal']['tasks']['T2']['attempts'], 4)
        self.assertEqual(self.profile['development_attempts'], 5)
        self.assertEqual(self.profile['feedback_delegation']['max_amendments'], 3)
        self.assertEqual(self.profile['feedback_delegation']['max_added_files'], 4)
        self.assertEqual(self.profile['feedback_delegation']['max_amendments_per_task'], 1)
        staged, auth = self.stage()
        for field in ('profile_sha256','operator_recovery_sha256','authorize_extensions','owner_allowance_sha256'):
            wrong = copy.deepcopy(auth)
            wrong.pop(field)
            with self.subTest(field=field), self.assertRaises(TaskError):
                activate(self.f.ticket, staged['preparation'], wrong)
            self.assertEqual(json.loads((self.f.ticket/'execution/active.json').read_text())['transition_id'],
                             self.source['projection']['transition_id'])
        def crash(point):
            if point=='owner_selector_published': raise OSError('lost profile recovery receipt')
        with self.assertRaisesRegex(OSError, 'lost profile recovery receipt'):
            activate(self.f.ticket, staged['preparation'], auth, fault=crash)
        with self.assertRaisesRegex(TaskError, 'reconciliation pending'):
            runtime_root(self.f.ticket)
        activate(self.f.ticket, staged['preparation'], auth)
        root = runtime_root(self.f.ticket)
        state = self.store.inspect()
        self.assertEqual(len(state['owner_amendments']), 3)
        self.assertEqual(state['owner_amendments'][-1]['operator_recovery']['consumed_attempts'], 4)
        self.assertEqual(TaskJournal(root/'03-dev/tasks').peek()['tasks']['T2']['attempts'],4)
        self.assertEqual(LimitsStore(root)._load(repair=False)[1]['invocations'],self.source['counters']['invocations'])
        before = (self.store.root/'task-journal.jsonl').read_bytes()
        activate(self.f.ticket, staged['preparation'], auth)
        self.assertEqual((self.store.root/'task-journal.jsonl').read_bytes(), before)

    def test_profile_cannot_expand_money_time_invocations_roles_or_unrequested_paths(self):
        self.initialize()
        def more_role(p): p['role_invocations']['feedback-planner'] += 1
        def more_path(p): p['feedback_delegation']['allowed_files'].append('unrequested.py')
        def more_automatic(p): p['feedback_delegation']['max_amendments_per_task'] += 1
        for key in ('metered_usd','cli_estimate_usd','invocations','wall_seconds',
                    'invocation_timeout_seconds','transport_retries','development_attempts'):
            changed = copy.deepcopy(self.profile); changed[key] += 1
            with self.subTest(field=key), self.assertRaises(TaskError):
                self.value(changed)
        for change in (more_role, more_path, more_automatic):
            changed = copy.deepcopy(self.profile); change(changed)
            with self.subTest(change=change.__name__), self.assertRaises(TaskError):
                self.value(changed)

    def test_unexhausted_task_cannot_obtain_an_early_extra_attempt(self):
        self.initialize()
        source = copy.deepcopy(self.source);source['journal']['tasks']['T2']['attempts'] -= 1
        with self.assertRaisesRegex(TaskError,'exhausted development attempt'):
            required_profile(source,self.paths,self.store.inspect())

    def test_changed_profile_cannot_borrow_scope_only_authorization(self):
        self.initialize();staged,auth=self.stage()
        path=Path(staged['preparation'])/'recipe.json'
        recipe=json.loads(path.read_text());recipe['envelope']['policy']['cli_estimate_usd']+=1
        atomic_json(path,recipe)
        with self.assertRaisesRegex(TaskError,'profile/capability'):
            activate(self.f.ticket,staged['preparation'],auth)
        self.assertEqual(json.loads((self.f.ticket/'execution/active.json').read_text())['transition_id'],
                         self.source['projection']['transition_id'])
