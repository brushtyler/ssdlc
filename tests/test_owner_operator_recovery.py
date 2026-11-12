"""Operator recovery retains funded decisions; exceptions never extend delegation."""
import copy
import difflib
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import test_owner_revalidation as owners
import ssdlc_owner_revalidation as recovery
from ssdlc_feedback import FeedbackStore
from ssdlc_feedback_workflow import intake,developer_actor
from ssdlc_recovery import LimitsStore
from ssdlc_scope_amendments import inspect,subject,prepare,activate
from ssdlc_tasks import TaskError,atomic_json,digest,TaskJournal
from ssdlc_transitions import runtime_root


class OperatorRecoveryTests(unittest.TestCase):
    def initialize(self, **kwargs):
        self.case=case=owners.OwnerFeedbackTests()
        case.initialize(False,**kwargs)
        self.addCleanup(case.doCleanups)
        return case

    def test_operator_reconciles_exact_funded_roles_plus_changed_stop_evidence(self):
        case=self.initialize()
        self.assertEqual(case.run_flow()['disposition'],'OWNER_REVALIDATION_REQUIRED')
        f=case.f;store=FeedbackStore(f.ticket)
        row=next(iter(store.inspect()['requests'].values()))
        (f.source['root']/'stop-observation.json').write_text('{"status":"failed"}\n')
        source=inspect(f.ticket)
        with self.assertRaisesRegex(TaskError,'exact operator reconciliation'):
            subject(source,f.plan,f.reason,feedback_request_id=row['request_id'])
        value=subject(source,f.plan,f.reason,feedback_request_id=row['request_id'],reconcile_feedback_evidence=True)
        self.assertEqual(len(value['owner_feedback']['source_reconciliation']['captured_consultations']),2)
        before=copy.deepcopy(source['counters']);calls=list(case.calls)
        review=dict(f.review,subject_sha256=digest(value))
        staged=prepare(f.ticket,f.repo,f.plan,review,f.reason,runtime_identity=f.identity,
            controller_snapshot=f.new_controller,feedback_request_id=row['request_id'],reconcile_feedback_evidence=True)
        activate(f.ticket,staged['preparation'],f.auth(staged))
        current=LimitsStore(runtime_root(f.ticket))._load(repair=False)[1]
        self.assertEqual(current['invocations'],before['invocations'])
        self.assertEqual(current['deadline_at'],before['deadline_at'])
        self.assertEqual(case.calls,calls)
        self.assertEqual(store.inspect()['requests'][row['request_id']],row)

    def test_unrelated_native_invocation_cannot_hide_in_funded_reconciliation(self):
        case=self.initialize();case.run_flow();f=case.f;store=FeedbackStore(f.ticket)
        row=next(iter(store.inspect()['requests'].values()))
        limits=LimitsStore(f.source['root']);identity=limits.reserve('03-dev','03-dev','codex')
        limits.finish(identity,'returned')
        source=inspect(f.ticket)
        with self.assertRaisesRegex(TaskError,'unrelated or uncaptured'):
            recovery.feedback_binding(f.ticket,source,['app.py'],row['request_id'],reconcile_evidence=True)

    def test_observed_usage_cannot_differ_from_captured_role_usage(self):
        case=self.initialize();case.run_flow();f=case.f;store=FeedbackStore(f.ticket)
        state=store.inspect();row=next(iter(state['requests'].values()))
        source=inspect(f.ticket);changed=copy.deepcopy(source)
        identity=next(c['native']['invocation_id'] for c in state['role_calls'])
        changed['counters']['invocations'][identity]['usage']['cli_estimate_usd']=100
        with self.assertRaisesRegex(TaskError,'captured funded consultations'):
            recovery.feedback_binding(f.ticket,changed,['app.py'],row['request_id'],reconcile_evidence=True)

    def test_accounting_prefix_is_not_waived_by_explicit_evidence_reconciliation(self):
        case=self.initialize();case.run_flow();f=case.f;store=FeedbackStore(f.ticket)
        row=next(iter(store.inspect()['requests'].values()))
        (f.source['root']/'stop-observation.json').write_text('{}')
        source=inspect(f.ticket)
        path=source['root']/'limits/task-journal.jsonl';raw=path.read_bytes()
        path.write_bytes(b' '+raw)
        with self.assertRaisesRegex(TaskError,'accounting prefix changed'):
            recovery.feedback_binding(f.ticket,source,['app.py'],row['request_id'],reconcile_evidence=True)

    def test_second_manual_amendment_needs_exact_allowance_and_preserves_auto_limits(self):
        from ssdlc_feedback_delegation import require_capacity
        case=self.initialize(attempt_cap=4,owner_limit=1)
        first,original,store=case.operator_bridge()
        activate(case.f.ticket,first['preparation'],case.f.auth(first))
        root=runtime_root(case.f.ticket);case.f.requested_paths=['other.py']
        def missing(task,attempt,prompt,workspace):
            limits=LimitsStore(root);call=limits.reserve('03-dev','03-dev','codex');limits.finish(call,'returned')
            return dict(output='TASK_DEV_STATUS: SCOPE_REQUIRED',exit_code=0,quiescent=True,engine='codex',model='offline')
        plan=json.loads((root/'02-design/plan.json').read_text())
        with self.assertRaisesRegex(TaskError,'SCOPE_REQUIRED'):
            case.f.scheduler(root,missing).run(plan['tasks'])
        outer=json.loads((root/'state.json').read_text());outer['status']='FAILED';atomic_json(root/'state.json',outer)
        source=inspect(case.f.ticket)
        row=store.submit(source,intake(source),developer_actor(source))
        plan=copy.deepcopy(source['plan']);plan['plan_revision']+=1;plan['tasks'][1]['target_files'].append('other.py')
        with self.assertRaisesRegex(TaskError,'allowance exhausted'):
            subject(source,plan,'One additional manual owner scope amendment',feedback_request_id=row['request_id'])
        value=subject(source,plan,'One additional manual owner scope amendment',
            feedback_request_id=row['request_id'],operator_owner_limit=2)
        review=dict(subject_sha256=digest(value),reviewer='independent operator scope fixture',
            report=dict(verdict='APPROVED',findings=[]),scope_diff=value['scope_diff'],
            owner_coverage=dict(contract_sha256=digest(value['owner_revalidation']),coverage_preserved=True,approved_test_changes=[]))
        staged=prepare(case.f.ticket,case.f.repo,plan,review,value['reason'],runtime_identity=case.f.identity,
            controller_snapshot=case.f.new_controller,feedback_request_id=row['request_id'],operator_owner_limit=2)
        auth=case.f.auth(staged)
        with self.assertRaisesRegex(TaskError,'exact separate operator authorization'):
            activate(case.f.ticket,staged['preparation'],auth)
        auth['owner_allowance_sha256']='0'*64
        with self.assertRaisesRegex(TaskError,'exact separate operator authorization'):
            activate(case.f.ticket,staged['preparation'],auth)
        auth['owner_allowance_sha256']=staged['proposal']['owner_allowance_sha256']
        before=copy.deepcopy(source['counters'])
        def crash(point):
            if point=='owner_selector_published': raise OSError('lost allowance receipt')
        with self.assertRaisesRegex(OSError,'lost allowance receipt'):
            activate(case.f.ticket,staged['preparation'],auth,fault=crash)
        with self.assertRaisesRegex(TaskError,'reconciliation pending'):
            runtime_root(case.f.ticket)
        activate(case.f.ticket,staged['preparation'],auth)
        selected=runtime_root(case.f.ticket);envelope,counters=LimitsStore(selected)._load(repair=False)
        self.assertEqual(envelope['policy']['feedback_delegation']['max_amendments_per_task'],1)
        self.assertEqual(counters['invocations'],before['invocations'])
        self.assertEqual(counters['deadline_at'],before['deadline_at'])
        self.assertEqual(TaskJournal(selected/'03-dev/tasks').peek()['tasks']['T2']['attempts'],3)
        state=store.inspect();self.assertEqual(len(state['owner_amendments']),2)
        self.assertEqual(state['requests'][original['request_id']],original)
        self.assertEqual(state['requests'][row['request_id']],row)
        self.assertEqual(state['owner_amendments'][-1]['operator_allowance']['manual_limit'],2)
        self.assertEqual(store.public_projection()['operator_owner_allowances'],1)
        later=copy.deepcopy(row);later['request_id']='another-request'
        with self.assertRaises(TaskError): require_capacity(state,later,envelope['policy'])
        snapshot=(store.root/'task-journal.jsonl').read_bytes()
        activate(case.f.ticket,staged['preparation'],auth)
        self.assertEqual((store.root/'task-journal.jsonl').read_bytes(),snapshot)

    def test_manual_override_cannot_expand_global_or_file_allowance(self):
        grant=dict(max_amendments=2,max_amendments_per_task=1,max_added_files=2)
        state=dict(amendments=[],owner_amendments=[dict(task_id='T12',paths=['one.py'])])
        self.assertEqual(recovery._capacity(state,'T12',['two.py'],grant,operator_owner_limit=2)['manual_limit'],2)
        for requested in (True,1,3,100):
            with self.subTest(requested=requested),self.assertRaises(TaskError):
                recovery._capacity(state,'T12',['two.py'],grant,operator_owner_limit=requested)
        with self.assertRaisesRegex(TaskError,'allowance exhausted'):
            recovery._capacity(state,'T12',['two.py','three.py'],grant,operator_owner_limit=2)


class ReviewedPatchTests(unittest.TestCase):
    def test_patch_is_delivered_but_only_applied_in_disposable_review_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'tests/test_old.py';path.parent.mkdir()
            old='def test_old():\n    assert value == 1\n';new='def test_old():\n    assert value == 2\n';path.write_text(old)
            patch=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='a/tests/test_old.py',tofile='b/tests/test_old.py'))
            value=dict(files={'tests/test_old.py':dict(kind='COMPLETED_TEST',sha256=hashlib.sha256(old.encode()).hexdigest())},owners={})
            change=dict(path='tests/test_old.py',old_sha256=value['files']['tests/test_old.py']['sha256'],
                new_sha256=hashlib.sha256(new.encode()).hexdigest(),reason='Reviewed changed expectation',patch=patch)
            review=dict(contract_sha256=digest(value),coverage_preserved=True,approved_test_changes=[change])
            recovery.validate_patches(value,review,root)
            self.assertEqual(path.read_text(),old)
            summary=recovery.prompt_summary(dict(contract=value,coverage_review=review))
            self.assertEqual(summary['reviewed_patches'][0]['patch'],patch)
            wrong=dict(review,approved_test_changes=[dict(change,new_sha256='0'*64)])
            with self.assertRaisesRegex(TaskError,'approved new hash'):
                recovery.validate_patches(value,wrong,root)
            escaped=dict(review,approved_test_changes=[dict(change,patch=patch.replace('b/tests/test_old.py','b/elsewhere.py'))])
            with self.assertRaisesRegex(TaskError,'exact declared test file'):
                recovery.validate_review(value,escaped)

    def test_reviewed_patch_bundle_has_a_serialized_size_and_count_bound(self):
        old='def test_old():\n    assert value == 1\n';new=old+'#' * 5000+'\n'
        patch=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='a/tests/test_old.py',tofile='b/tests/test_old.py'))
        value=dict(files={'tests/test_old.py':dict(kind='COMPLETED_TEST',sha256=hashlib.sha256(old.encode()).hexdigest())},owners={})
        change=dict(path='tests/test_old.py',old_sha256=value['files']['tests/test_old.py']['sha256'],
            new_sha256=hashlib.sha256(new.encode()).hexdigest(),reason='Reviewed test addition',patch=patch)
        review=dict(contract_sha256=digest(value),coverage_preserved=True,approved_test_changes=[change])
        with self.assertRaisesRegex(TaskError,'bounded prompt allowance'):
            recovery.validate_review(value,review)
