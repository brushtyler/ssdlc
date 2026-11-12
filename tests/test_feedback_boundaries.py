"""Real outside-manifest failure stops before another developer reservation."""
import unittest

import test_feedback_host_hint as fixtures
from ssdlc_recovery import LimitsStore
from ssdlc_tasks import TaskError, TaskJournal


class FeedbackBoundaryTests(unittest.TestCase):
    def test_host_scope_hint_intercepts_failed_capture_before_dispatch_and_preflight(self):
        fixture = fixtures.NativeHostHintTests(); fixture.initialize(); self.addCleanup(fixture.doCleanups)
        case, source = fixture.case, fixture.source
        before = LimitsStore(source['root'])._load(repair=False)[1]
        engine = case.scheduler(source['root'], lambda *args:self.fail('must not redispatch the developer'))
        engine.feedback_enabled=True
        engine._preflight=lambda:self.fail('feedback must precede another checkpoint preflight')
        with self.assertRaisesRegex(TaskError,'Host scope hypothesis'):
            engine.run(source['plan']['tasks'])
        self.assertEqual(case.calls,[1])
        record=TaskJournal(source['root']/'03-dev/tasks').peek()['tasks'][source['task_id']]
        self.assertEqual(record['attempts'],1)
        self.assertEqual(record['failed_attempts'][-1]['category'],'CHECK_FAILURE')
        self.assertEqual(LimitsStore(source['root'])._load(repair=False)[1],before)
