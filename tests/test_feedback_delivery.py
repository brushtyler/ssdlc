"""GH uncertainty is reconciled, never converted into permission to repost."""
import copy
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from ssdlc_feedback import FeedbackError
from ssdlc_feedback_delivery import ProjectionOutbox, RemoteUncertain, projection, GitHubComments
from test_recovery_limits import policy
from test_feedback_delegation import grant
from test_feedback_workflow import config


class FakeComments:
    def __init__(self):
        self.comments, self.posts = [], 0
    def actor(self): return 123
    def list(self, target): return copy.deepcopy(self.comments)
    def post(self, target, body):
        self.posts += 1
        value = dict(id=self.posts, body=body, user={'id':123})
        self.comments.append(value)
        return value


class PublicStore:
    def __init__(self, ticket): self.ticket, self.rounds = ticket, 0
    def public_projection(self):
        return dict(schema_version=1, requests=[dict(request_id='a'*64, task_id='PRIVATE_TASK',
            status='REQUESTED', rounds=self.rounds, disposition='WAITING', origin='HOST_HINT',
            quota_reason=None, reduced_independence=None, raw_model_output='SECRET_PRIVATE_MODEL_TEXT')],
            role_calls=0, amendments=0, amendments_reserved=0, amendments_applied=0,
            source_path='/private/secrets')


class FeedbackDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.ticket = Path(temp.name) / 'PRIVATE_TICKET'; self.store = PublicStore(self.ticket)
        workflow = config(); workflow['github'] = dict(repo='owner/repo', issue=39)
        self.profile = policy(feedback_delegation=grant(), feedback_workflow=workflow,
            role_invocations={'03-dev':3,'feedback-planner':2,'feedback-reviewer':2,'feedback-po':1},
            phase_invocations={'03-dev':3,'feedback':5})
        self.outbox, self.backend = ProjectionOutbox(self.ticket), FakeComments()

    def test_selective_projection_and_exact_idempotent_own_publication(self):
        self.outbox.publish(self.store, self.profile, self.backend)
        journal = self.outbox.root / 'task-journal.jsonl'
        before = journal.read_bytes()
        self.outbox.publish(self.store, self.profile, self.backend)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(self.backend.posts, 1)
        body = self.backend.comments[0]['body']
        for secret in ('PRIVATE_TASK','PRIVATE_TICKET','SECRET_PRIVATE_MODEL_TEXT','/private/secrets'):
            self.assertNotIn(secret, body)
        self.assertEqual(self.outbox.summary(), {'operations':1,'sent':1,'unconfirmed':0})

    def test_crash_after_post_reconciles_remote_without_duplicate(self):
        def crash(point):
            if point == 'after_publication': raise OSError('lost ack')
        with self.assertRaises(OSError):
            self.outbox.publish(self.store, self.profile, self.backend, fault=crash)
        self.outbox.publish(self.store, self.profile, self.backend)
        self.assertEqual(self.backend.posts, 1)
        self.assertEqual(self.outbox.summary()['unconfirmed'], 0)

    def test_crash_before_post_never_blindly_reposts_or_skips_to_new_state(self):
        def crash(point):
            if point == 'before_publication': raise OSError('ambiguous dispatch boundary')
        with self.assertRaises(OSError):
            self.outbox.publish(self.store, self.profile, self.backend, fault=crash)
        with self.assertRaises(RemoteUncertain):
            self.outbox.publish(self.store, self.profile, self.backend)
        self.store.rounds += 1
        with self.assertRaises(RemoteUncertain):
            self.outbox.publish(self.store, self.profile, self.backend)
        self.assertEqual(self.backend.posts, 0)

    def test_foreign_marker_and_edited_own_comment_do_not_authorize_repost(self):
        def crash(point):
            if point == 'after_publication': raise OSError('lost ack')
        with self.assertRaises(OSError):
            self.outbox.publish(self.store, self.profile, self.backend, fault=crash)
        self.backend.comments[0]['user']['id'] = 999
        with self.assertRaises(RemoteUncertain):
            self.outbox.publish(self.store, self.profile, self.backend)
        self.backend.comments[0]['user']['id'] = 123
        self.outbox.publish(self.store, self.profile, self.backend)
        self.backend.comments[0]['body'] = 'changed remotely'
        with self.assertRaises(RemoteUncertain):
            self.outbox.publish(self.store, self.profile, self.backend)
        self.assertEqual(self.backend.posts, 1)

    def test_prior_uncertain_post_must_be_reconciled_before_new_projection(self):
        def crash(point):
            if point == 'after_publication': raise OSError('lost ack')
        with self.assertRaises(OSError):
            self.outbox.publish(self.store, self.profile, self.backend, fault=crash)
        self.store.rounds += 1
        self.outbox.publish(self.store, self.profile, self.backend)
        self.assertEqual(self.backend.posts, 2)
        self.assertEqual(self.outbox.summary(), {'operations':2,'sent':2,'unconfirmed':0})

    def test_partial_outbox_is_preserved_and_missing_gh_grant_makes_no_request(self):
        profile = copy.deepcopy(self.profile); del profile['feedback_workflow']['github']
        self.assertEqual(self.outbox.publish(self.store, profile, self.backend)['status'], 'NOT_AUTHORIZED')
        self.assertEqual(self.backend.posts, 0)
        self.outbox.publish(self.store, self.profile, self.backend)
        path = self.outbox.root / 'task-journal.jsonl'
        path.write_bytes(path.read_bytes() + b'{"partial":')
        before = path.read_bytes()
        with self.assertRaises(FeedbackError):
            self.outbox.publish(self.store, self.profile, self.backend)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.backend.posts, 1)

    def test_deleted_comment_author_is_not_an_owned_receipt_or_a_publication_failure(self):
        self.backend.comments.append(dict(id=99,body='an unrelated comment',user=None))
        self.outbox.publish(self.store,self.profile,self.backend)
        self.outbox.publish(self.store,self.profile,self.backend)
        self.assertEqual(self.backend.posts,1)

    def test_unexpected_api_shape_and_timeouts_are_typed_remote_uncertainty(self):
        backend=GitHubComments()
        with patch.object(backend,'_api',return_value={}):
            with self.assertRaises(RemoteUncertain): backend.actor()
            with self.assertRaises(RemoteUncertain): backend.list(self.profile['feedback_workflow']['github'])
        with patch('ssdlc_feedback_delivery.subprocess.run',side_effect=subprocess.TimeoutExpired('PRIVATE_AUTH_SENTINEL',30)):
            with self.assertRaises(RemoteUncertain) as failure: backend.actor()
        self.assertNotIn('PRIVATE_AUTH_SENTINEL',str(failure.exception))
