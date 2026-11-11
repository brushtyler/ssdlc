"""Funded role consultations, using the existing cumulative LimitsStore.

There is deliberately no default provider adapter or dispatch CLI here. A host
integration must supply a guarded, read-only adapter; tests use semantic fakes.
Recording policy alone cannot fund a call or bypass allowed native phases.
"""
import copy
import json
import math

from ssdlc_controller import execution_contract
from ssdlc_feedback import FeedbackError, ROLES
from ssdlc_recovery import NotDispatched, QuiescentTimeout
from ssdlc_tasks import TaskJournal, digest, ticket_lock
from ssdlc_transitions import _regular


def _usage(value):
    if (not isinstance(value, dict) or set(value) != {'metered_usd', 'cli_estimate_usd'}
            or any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0)
                   for v in value.values())):
        raise FeedbackError('Invalid observed feedback usage')
    return value


class FundedFeedbackRoles:
    def __init__(self, feedback, limits, *, guard):
        self.feedback, self.limits, self.guard = feedback, limits, guard

    def _authority(self):
        self.guard()
        raw = _regular(self.limits.root, 'limits/task-journal.jsonl').read_bytes()
        if not raw.endswith(b'\n'):
            raise FeedbackError('Incomplete native accounting requires reconciliation before consultation')
        authority, _ = self.feedback._read()
        envelope, counters = self.limits._load(repair=False)
        p = authority['policy']
        if (p['limits_sha256'] != digest(envelope)
                or p['ticket'] != envelope['binding']['ticket']
                or p['controller_sha256'] != digest(execution_contract(envelope['binding']['controller']))
                or p['expires_at'] > counters['deadline_at']):
            raise FeedbackError('Feedback consultation differs from frozen native authority')
        return envelope, counters

    def run(self, request_id, role, metadata, runner, *, fault=lambda point: None):
        """Exactly one newly reserved read-only consultation, never a retry.

        runner receives host-stamped role metadata and an opaque invocation ID,
        and returns {response: object, usage: typed observation}. It must not
        reserve another invocation. An exception without trusted non-dispatch
        or confirmed termination evidence retains native uncertainty.
        """
        if role not in ROLES:
            raise FeedbackError('Unknown feedback role')
        with ticket_lock(self.feedback.root / 'consultation.lock'):
            envelope, _ = self._authority()
            record = self.feedback.inspect()['requests'][request_id]
            raw = _regular(self.limits.root, '03-dev/tasks/task-journal.jsonl').read_bytes()
            if not raw.endswith(b'\n'):
                raise FeedbackError('Incomplete task history before consultation')
            tasks = TaskJournal._replay(raw)[0]
            current = tasks['tasks'].get(record['binding']['task_id'])
            plan = json.loads(_regular(self.limits.root, '02-design/plan.json').read_text())
            if (not current or current.get('status') != 'FAILED'
                    or current.get('attempts') != record['binding']['attempt']
                    or not current.get('failed_attempts')
                    or digest(current['failed_attempts'][-1]) != record['binding']['failure_sha256']
                    or digest(plan) != record['binding']['plan_sha256']):
                raise FeedbackError('Task or plan changed before consultation')
            # A useful developer attempt must remain before any planner spend.
            source_attempt = record['binding'].get('attempt')
            if source_attempt is None:
                raise FeedbackError('Feedback request has no bound development attempt')
            if source_attempt >= envelope['policy']['development_attempts']:
                raise FeedbackError('No useful development attempt remains')
            self.limits.require_capacity('feedback-' + role, 'feedback')
            call = self.feedback.reserve_role(request_id, role, metadata)
            fault('feedback_intent')
            invocation = self.limits.reserve('feedback-' + role, 'feedback', metadata['engine'])
            fault('native_reservation')
            self.feedback.bind_invocation(call['operation_id'], invocation, digest(envelope))
            fault('reservation_bound')
            self.guard()
            try:
                result = runner(copy.deepcopy(metadata), invocation)
            except BaseException as exc:
                status = ('not_dispatched' if isinstance(exc, NotDispatched) else
                          'terminated' if isinstance(exc, QuiescentTimeout) else 'uncertain')
                self.limits.finish(invocation, status)
                raise
            # An absent/invalid response does not authorize a blind inference.
            if not isinstance(result, dict) or set(result) != {'response', 'usage'}:
                self.limits.finish(invocation, 'returned')
                raise FeedbackError('Malformed returned feedback response; operator reconciliation required')
            try:
                usage = _usage(result['usage'])
                # Validate the complete envelope before preserving it as typed
                # observed usage. Strings and nonfinite numbers stay untrusted.
                encoded = json.dumps(result, allow_nan=False)
                if len(encoded) > 16000 or not isinstance(result['response'], dict):
                    raise FeedbackError('Malformed or oversized feedback response')
            except (TypeError, ValueError, FeedbackError):
                self.limits.finish(invocation, 'returned')
                raise FeedbackError('Malformed returned feedback data; no automatic retry') from None
            receipt = self.feedback.record_role_result(call['operation_id'], result)
            fault('response_captured')
            self.limits.finish(invocation, 'returned', usage)
            fault('native_returned')
            self.guard()
            return copy.deepcopy(receipt['receipt']['response']['response'])

    def resume_response(self, operation):
        """Reconcile a captured return; it cannot call a provider."""
        with ticket_lock(self.feedback.root / 'consultation.lock'):
            envelope, counters = self._authority()
            state = self.feedback.inspect()
            call = next((row for row in state['role_calls'] if row['operation_id'] == operation), None)
            if call is None or call['status'] != 'RETURNED' or 'native' not in call:
                raise FeedbackError('Unconfirmed consultation outcome; no redispatch')
            binding = call['native']
            if binding['limits_sha256'] != digest(envelope):
                raise FeedbackError('Consultation runtime changed')
            invocation = counters['invocations'].get(binding['invocation_id'])
            if (invocation is None or invocation['role'] != 'feedback-' + call['role']
                    or invocation['phase'] != 'feedback' or invocation['engine'] != call['actor']['engine']):
                raise FeedbackError('Consultation receipt has no matching native reservation')
            result = call['receipt']['response']
            if digest(result) != call['receipt']['response_sha256']:
                raise FeedbackError('Captured consultation response changed')
            _usage(result['usage'])
            if invocation['status'] not in {'reserved', 'returned'}:
                raise FeedbackError('Native consultation has a conflicting terminal outcome')
            self.limits.finish(binding['invocation_id'], 'returned', result['usage'])
            return copy.deepcopy(result['response'])
