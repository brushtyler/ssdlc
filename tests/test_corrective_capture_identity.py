"""Corrective lineage preserves reservations, runtime authority and old history."""
import copy
import unittest
import test_corrective_lineage as fixtures
from ssdlc_corrective import CorrectiveError


class NativeCaptureIdentityTests(unittest.TestCase):
    def trace(self, *, historical=False):
        trace=fixtures.Trace()
        trace.state.update(candidate_isolation=1,candidate_runtime={'controller_sha256':'c'*64},
                           bounded_recovery=1,attempt_limit=3,historical_capture_bindings={})
        rec=trace.state['tasks']['A']
        if historical:
            rec.update(attempts=1,invocation_id='a'*32,failed_floor=1,
                       failed_attempts=[dict(attempt=1,category='CHECK_FAILURE',invocation_id='a'*32)])
        trace.anchor['state']=copy.deepcopy(trace.state)
        trace.open()
        return trace

    def reserve(self, trace, identity='b'*32):
        rec=trace.state['tasks']['A']
        rec.update(status='DISPATCH_RESERVED',attempts=rec['attempts']+1,checks=[],invocation_id=identity,
                   start_commit='h'*40,protected_snapshot={'contract':'c'*64})
        trace.emit('DISPATCH_RESERVED')
        return rec

    def test_unchanged_native_reservation_start_capture_and_verification_are_accepted(self):
        trace=self.trace();rec=self.reserve(trace)
        rec['status']='RUNNING';trace.emit('TASK_STARTED')
        rec.update(status='CANDIDATE_CAPTURED',capture_sha256='d'*64);trace.emit('CANDIDATE_CAPTURED')
        trace.verifying()
        self.assertEqual(trace.walk()['state'],'R_VER')

    def test_changed_identity_at_start_or_capture_is_refused(self):
        for boundary in ('TASK_STARTED','CANDIDATE_CAPTURED'):
            trace=self.trace();rec=self.reserve(trace)
            rec['status']='RUNNING'
            if boundary=='CANDIDATE_CAPTURED':
                trace.emit('TASK_STARTED');rec['status']='CANDIDATE_CAPTURED'
            rec['invocation_id']='e'*32;trace.emit(boundary)
            with self.subTest(boundary=boundary),self.assertRaises(CorrectiveError):trace.walk()

    def test_duplicate_or_malformed_reservation_is_refused(self):
        for identity in ('a'*32,'short',None):
            trace=self.trace(historical=True);self.reserve(trace,identity)
            with self.subTest(identity=identity),self.assertRaises(CorrectiveError):trace.walk()

    def test_runtime_or_inherited_scope_authority_cannot_change_during_repair(self):
        for field in ('candidate_runtime','historical_capture_bindings','attempt_limit'):
            trace=self.trace();rec=self.reserve(trace)
            trace.state[field]={'changed':'value'};rec['status']='RUNNING';trace.emit('TASK_STARTED')
            with self.subTest(field=field),self.assertRaises(CorrectiveError):trace.walk()
        trace=self.trace();rec=self.reserve(trace)
        rec['scope_reconciliation']={'subject_sha256':'e'*64};rec['status']='RUNNING';trace.emit('TASK_STARTED')
        with self.assertRaises(CorrectiveError):trace.walk()

    def test_historical_failure_content_not_only_length_is_preserved(self):
        trace=self.trace(historical=True);rec=self.reserve(trace)
        rec['failed_attempts'][0]['category']='SCOPE_REQUIRED';rec['status']='RUNNING';trace.emit('TASK_STARTED')
        with self.assertRaises(CorrectiveError):trace.walk()
