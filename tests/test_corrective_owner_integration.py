"""Native captured repair preserves admitted owner checks across transactions."""
import copy
import unittest
from unittest.mock import patch

import test_scope_amendments as fixtures
from ssdlc_scope_amendments import activate
from ssdlc_tasks import TaskJournal, TaskError, digest
from ssdlc_transitions import runtime_root
from ssdlc_corrective import verify_lineage, CorrectiveError


class CorrectiveOwnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = f = fixtures.ScopeAmendmentTests()
        f.initialize(attempt_cap=6);self.addCleanup(f.doCleanups)
        staged=f.stage();activate(f.ticket,staged['preparation'],f.auth(staged))
        self.root=runtime_root(f.ticket);self.calls=[]
        def develop(task,attempt,prompt,workspace):
            self.calls.append((task['task_id'],attempt))
            (workspace/'app.py').write_text('value = %d\n' % (attempt+10))
            return dict(output='TASK_DEV_STATUS: COMPLETE',exit_code=0,quiescent=True)
        self.engine=f.scheduler(self.root,develop)
        self.engine.run(f.plan['tasks'])
        self.limits=dict(max_attempts=6,gate_round_ceiling=6,budget_cap_usd=10,spend_at_creation_usd=0)

    def repair(self, roots, txn):
        e=self.engine
        head=TaskJournal.head(e.journal.path)
        anchor=dict(head,event='TXN_CREATED',txn_id=txn)
        closure=e._closure(set(roots))[0]
        observed=[];save=e._save
        def guarded(kind):
            save(kind)
            verify_lineage(e.journal.path,anchor,closure,roots,self.limits)
            observed.append(kind)
        with patch.object(e,'_save',side_effect=guarded):
            e.invalidate(roots,'corrective:'+txn)
            e.repair(self.f.plan['tasks'],roots,{},txn_id=txn,on_step=lambda *args,**kwargs:None)
        return anchor,closure,observed

    def test_actual_owner_checks_are_verified_after_every_append_and_replay_rejects_tampering(self):
        anchor,closure,events=self.repair(['T2'],'one')
        self.assertIn('OWNER_REVALIDATION_STARTED',events)
        self.assertIn('OWNER_CHECK_FINISHED',events)
        self.assertEqual(self.engine.state['tasks']['T2']['owner_revalidations'][-1]['status'],'PASSED')
        self.assertEqual(verify_lineage(self.engine.journal.path,anchor,closure,['T2'],self.limits)['state'],'F_PASSED')
        from ssdlc_corrective import walk_lineage
        initial,entries=TaskJournal.replay_range(self.engine.journal.path,anchor['sequence'],anchor['sha256'])
        bad=copy.deepcopy(entries)
        event=next(x for x in bad if x['event']=='OWNER_CHECK_FINISHED')
        event['state']['tasks']['T1']['attempts']+=1
        with self.assertRaises(CorrectiveError):walk_lineage(dict(anchor,state=initial),bad,closure,['T2'],self.limits)

    def test_successive_transactions_accept_new_verified_owner_without_rewriting_attribution(self):
        frozen=copy.deepcopy(self.engine.scope_reconciliations['T2']['owner_revalidation'])
        self.repair(['T1'],'owner-repair')
        self.assertNotEqual(self.engine.state['tasks']['T1']['commit'],frozen['contract']['owners']['T1']['commit'])
        self.repair(['T2'],'introducer-repair')
        self.assertEqual(self.engine.scope_reconciliations['T2']['owner_revalidation'],frozen)
        self.assertEqual(self.engine.state['tasks']['T2']['owner_revalidations'][-1]['status'],'PASSED')

    def test_failed_owner_stops_before_commit_or_another_dispatch(self):
        verify=self.engine._verify
        def failing(task,record,directory,**kwargs):
            if kwargs.get('event')=='OWNER_CHECK_FINISHED':
                old=self.engine.verifier
                self.engine.verifier=lambda *args:('Ran 1 test\nFAILED (failures=1)','',1)
                try:return verify(task,record,directory,**kwargs)
                finally:self.engine.verifier=old
            return verify(task,record,directory,**kwargs)
        before=len(self.calls)
        with patch.object(self.engine,'_verify',side_effect=failing),self.assertRaises(TaskError):
            self.repair(['T2'],'failed-owner')
        self.assertEqual(len(self.calls),before+1)
        self.assertEqual(self.engine.state['tasks']['T1']['status'],'INVALIDATED')
        self.assertEqual(self.engine.state['owner_revalidation_blocked']['task_id'],'T2')
