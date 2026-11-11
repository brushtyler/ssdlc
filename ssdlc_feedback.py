"""Host-owned feedback ledger. Requests are data, never execution authority.

The ledger is separate from disposable candidates and execution generations.
Its hash chain detects accidental corruption; it does not authenticate a hostile
host that can replace both authority and history. No provider or GitHub calls
are made by this module.
"""
import copy
import json
import math
from pathlib import Path
import re
import time

from ssdlc_tasks import TaskError, TaskJournal, atomic_json, digest, file_hash, ticket_lock
from ssdlc_transitions import _regular
from ssdlc_feedback_analysis import actor, independence


class FeedbackError(TaskError):
    pass


ROUTES = {'AMENDMENT', 'PREREQUISITE', 'ADVISORY', 'REJECTED', 'INCONCLUSIVE'}
CAUSES = {'MISSING_TARGET', 'MISSING_PREREQUISITE', 'IMPROVEMENT',
          'REQUIREMENT_CONFLICT', 'INCONCLUSIVE'}
ROLES = {'diagnosis', 'planner', 'reviewer'}
HASH = re.compile(r'[a-f0-9]{64}')
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}')


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise FeedbackError('Malformed feedback document')


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise FeedbackError('Invalid feedback digest')


def _identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise FeedbackError('Invalid host role identity')


def _integer(value, minimum=1, maximum=1000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise FeedbackError('Invalid feedback allowance')


def literal_path(path):
    """Refuse aliases rather than silently changing a reviewed scope diff."""
    if (not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', path)
            or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/'))):
        raise FeedbackError('Feedback requires a canonical confined literal path')
    return path


def _unique(values, validator, *, maximum=100):
    if not isinstance(values, list) or len(values) > maximum:
        raise FeedbackError('Invalid feedback list')
    for value in values:
        validator(value)
    if len(set(values)) != len(values):
        raise FeedbackError('Duplicate feedback values')


def validate_policy(policy):
    _object(policy, ('schema_version', 'capability', 'ticket', 'source_binding_sha256',
                     'controller_sha256', 'limits_sha256', 'allowed_files',
                     'max_requests', 'max_amendments', 'max_added_files',
                     'max_amendments_per_task', 'max_rounds', 'role_calls', 'expires_at',
                     'require_strong_independence'))
    if type(policy['schema_version']) is not int or policy['schema_version'] != 1:
        raise FeedbackError('Unsupported feedback policy')
    if policy['capability'] != 'record-only-v1':
        raise FeedbackError('Recording policy cannot grant model dispatch or activation')
    _identity(policy['ticket'])
    for key in ('source_binding_sha256', 'controller_sha256', 'limits_sha256'):
        _hash(policy[key])
    _unique(policy['allowed_files'], literal_path)
    for key in ('max_requests', 'max_amendments', 'max_amendments_per_task', 'max_added_files', 'max_rounds'):
        _integer(policy[key])
    if type(policy['require_strong_independence']) is not bool:
        raise FeedbackError('Explicit independence policy required')
    _object(policy['role_calls'], ROLES)
    for value in policy['role_calls'].values():
        _integer(value, minimum=0)
    if (type(policy['expires_at']) not in (float, int)
            or not math.isfinite(policy['expires_at']) or policy['expires_at'] <= 0):
        raise FeedbackError('Invalid feedback deadline')


def source_binding(source):
    """Bind the exact native stopped source, including attempts and candidate."""
    row = source['journal']['tasks'][source['task_id']]
    failure = row['failed_attempts'][-1]
    if failure['category'] != 'SCOPE_REQUIRED' or not failure.get('capture_sha256'):
        raise FeedbackError('Feedback requires a captured explicit scope refusal')
    return dict(task_id=source['task_id'], parent_selector=source['parent_selector'],
                plan_sha256=digest(source['plan']), source_files_sha256=digest(source['files']),
                failure_sha256=digest(failure), capture_sha256=failure['capture_sha256'],
                counters_sha256=digest(source['counters']))


def validate_request(source, request):
    _object(request, ('schema_version', 'cause', 'obligation_refs', 'requested_files', 'evidence_refs'))
    if type(request['schema_version']) is not int or request['schema_version'] != 1 or request['cause'] not in CAUSES:
        raise FeedbackError('Invalid feedback request')
    _unique(request['obligation_refs'], _identity)
    task = next(t for t in source['plan']['tasks'] if t['task_id'] == source['task_id'])
    if not request['obligation_refs'] or not set(request['obligation_refs']) <= set(task['ac_refs'] + task['sec_refs']):
        raise FeedbackError('Feedback obligations must belong to the stopped task')
    _unique(request['requested_files'], literal_path)
    if request['cause'] == 'MISSING_TARGET' and not request['requested_files']:
        raise FeedbackError('Missing-target request has no target')
    refs = request['evidence_refs']
    if not isinstance(refs, dict) or not refs or len(refs) > 20:
        raise FeedbackError('Feedback requires registered evidence')
    for ref, sha in refs.items():
        literal_path(ref); _hash(sha)
        if source['files'].get(ref) != sha or file_hash(_regular(source['root'], ref)) != sha:
            raise FeedbackError('Unregistered or changed feedback evidence')


class FeedbackStore:
    """Canonical policy, requests and role intents, outside candidate writes.

    All mutations serialize on one stable lock. Interrupted journal tails are
    refused, never trimmed automatically: an unknown role result must not be
    converted into permission for another invocation.
    """
    def __init__(self, ticket, clock=time.time):
        self.ticket = Path(ticket).absolute()
        self.root = self.ticket / 'feedback'
        self.clock = clock
        self._paths()

    def _paths(self):
        for path in (self.root, *self.root.parents):
            if path.is_symlink():
                raise FeedbackError('Symlink in feedback authority')
        if self.root.exists():
            for name in ('authority.json', 'task-journal.jsonl', 'tasks-state.json', 'feedback.lock'):
                if (self.root / name).is_symlink():
                    raise FeedbackError('Symlink in feedback ledger')

    def _read(self):
        self._paths()
        authority = json.loads(_regular(self.root, 'authority.json').read_text())
        validate_policy(authority['policy'])
        expected = dict(schema_version=1, policy_sha256=digest(authority['policy']),
                        authorized=True, source_fenced=True)
        if not exact_authorization(authority.get('authorization'), expected):
            raise FeedbackError('Feedback policy has no exact operator authority')
        path = self.root / 'task-journal.jsonl'
        if not path.exists():
            if authority['status'] == 'INTENT':
                return authority, None
            raise FeedbackError('Missing feedback history cannot reset allowances')
        raw = _regular(self.root, path.name).read_bytes()
        if not raw or not raw.endswith(b'\n'):
            raise FeedbackError('Incomplete feedback history requires reconciliation')
        state = TaskJournal._replay(raw)[0]
        if state['policy_sha256'] != digest(authority['policy']):
            raise FeedbackError('Feedback policy changed')
        return authority, state

    def _append(self, kind, state):
        journal = TaskJournal(self.root)
        journal.load()
        journal.append(kind, state)

    def authorize(self, policy, authorization, *, fault=lambda point: None):
        validate_policy(policy)
        expected = dict(schema_version=1, policy_sha256=digest(policy), authorized=True, source_fenced=True)
        if not exact_authorization(authorization, expected) or policy['ticket'] != self.ticket.name:
            raise FeedbackError('Exact fenced feedback policy authorization required')
        if self.clock() >= policy['expires_at']:
            raise FeedbackError('Feedback policy expired')
        self._paths()
        with ticket_lock(self.root / 'feedback.lock'):
            path = self.root / 'authority.json'
            if path.exists():
                authority, state = self._read()
                if authority['policy'] != policy:
                    raise FeedbackError('Frozen feedback policy cannot be replaced')
            else:
                if (self.root / 'task-journal.jsonl').exists() or (self.root / 'tasks-state.json').exists():
                    raise FeedbackError('Orphan feedback history requires reconciliation')
                authority = dict(policy=copy.deepcopy(policy), authorization=authorization, status='INTENT')
                atomic_json(path, authority)
                state = None
            fault('policy_intent')
            if state is None:
                state = dict(policy_sha256=digest(policy), requests={}, role_calls=[], amendments=[], added_files=[])
                self._append('FEEDBACK_AUTHORIZED', state)
            fault('policy_journal')
            if authority['status'] != 'ACTIVE':
                authority['status'] = 'ACTIVE'
                atomic_json(path, authority)
            return copy.deepcopy(state)

    def inspect(self):
        authority, state = self._read()
        if authority['status'] != 'ACTIVE' or state is None:
            raise FeedbackError('Feedback authorization is incomplete')
        return copy.deepcopy(state)

    def _active(self):
        authority, state = self._read()
        if authority['status'] != 'ACTIVE' or state is None:
            raise FeedbackError('Feedback authorization is incomplete')
        if self.clock() >= authority['policy']['expires_at']:
            raise FeedbackError('Feedback policy expired')
        return authority['policy'], copy.deepcopy(state)

    def submit(self, source, request, developer, *, failed_tests=(), impact=None):
        actor(developer); validate_request(source, request)
        request = copy.deepcopy(request)
        for field in ('obligation_refs', 'requested_files'):
            request[field] = sorted(request[field])
        binding = source_binding(source)
        _unique(list(failed_tests), _identity)
        # Attempt IDs and generated prose do not disguise a repeated cause.
        checks = sorted({check['check_id'] for failed in source['journal']['tasks'][source['task_id']]['failed_attempts']
                         for check in failed.get('checks', []) if check['exit_code'] != 0 or check['empty_suite']})
        cause = dict(ticket=self.ticket.name, task_id=binding['task_id'], parent_plan=binding['plan_sha256'],
                     files=sorted(request['requested_files']), failed_checks=checks, failed_tests=sorted(failed_tests))
        identity = digest(cause)
        with ticket_lock(self.root / 'feedback.lock'):
            policy, state = self._active()
            if identity in state['requests']:
                existing = state['requests'][identity]
                if existing['binding'] != binding:
                    raise FeedbackError('Repeated cause with changed source requires explicit reconciliation')
                if existing['request'] != request:
                    raise FeedbackError('Repeated cause with changed classification or evidence requires reconciliation')
                return copy.deepcopy(existing)
            # Later generations require an admitted native lineage, not merely
            # a caller asserting a new parent. Native integration owns it.
            if digest(binding) != policy['source_binding_sha256']:
                raise FeedbackError('Feedback source is outside authorized lineage')
            if len(state['requests']) >= policy['max_requests']:
                raise FeedbackError('Feedback request allowance exhausted')
            row = dict(request_id=identity, request=copy.deepcopy(request), binding=binding,
                       developer=copy.deepcopy(developer), status='REQUESTED', rounds=0, decisions=[],
                       failed_tests=sorted(failed_tests), impact=copy.deepcopy(impact),
                       occurrences=[dict(binding=binding, developer=copy.deepcopy(developer))])
            state['requests'][identity] = row
            self._append('FEEDBACK_REQUESTED', state)
            return copy.deepcopy(row)

    def propose(self, identity, proposal, planner, source):
        actor(planner)
        _object(proposal, ('route', 'requested_files', 'reason_code'))
        if proposal['route'] not in ROUTES:
            raise FeedbackError('Unknown feedback route')
        _identity(proposal['reason_code']); _unique(proposal['requested_files'], literal_path)
        with ticket_lock(self.root / 'feedback.lock'):
            policy, state = self._active()
            row = state['requests'][identity]
            if row['status'] != 'REQUESTED' or row['binding'] != source_binding(source):
                raise FeedbackError('Proposal requires a fresh requested source')
            if not independence(planner, row['developer'])['distinct_roles']:
                raise FeedbackError('Developer cannot plan its own scope expansion')
            if row['rounds'] >= policy['max_rounds']:
                raise FeedbackError('Feedback round allowance exhausted')
            files = proposal['requested_files']
            if not set(files) <= set(row['request']['requested_files']):
                raise FeedbackError('Planner expanded the requested scope')
            if proposal['route'] == 'AMENDMENT':
                if row['request']['cause'] != 'MISSING_TARGET' or not files:
                    raise FeedbackError('Only an existing-obligation missing target can be amended')
                if not set(files) <= set(policy['allowed_files']):
                    raise FeedbackError('Amendment is outside delegated paths')
            elif proposal['route'] == 'ADVISORY' and row['request']['cause'] != 'IMPROVEMENT':
                raise FeedbackError('A blocking scope request cannot be downgraded to advisory')
            elif files:
                raise FeedbackError('Non-amendment route cannot authorize file changes')
            row.update(status='PROPOSED', planner=copy.deepcopy(planner), proposal=copy.deepcopy(proposal))
            row['rounds'] += 1
            row['proposal_sha256'] = digest(dict(binding=row['binding'], proposal=proposal, planner=planner))
            self._append('FEEDBACK_PROPOSED', state)
            return copy.deepcopy(row)

    def review(self, identity, review, reviewer, source):
        actor(reviewer)
        _object(review, ('proposal_sha256', 'verdict', 'coverage_preserved', 'reason_code'))
        _hash(review['proposal_sha256']); _identity(review['reason_code'])
        if review['verdict'] not in ('APPROVED', 'REJECTED', 'INCONCLUSIVE') or type(review['coverage_preserved']) is not bool:
            raise FeedbackError('Invalid independent feedback review')
        with ticket_lock(self.root / 'feedback.lock'):
            _, state = self._active()
            row = state['requests'][identity]
            if row['status'] != 'PROPOSED' or row['binding'] != source_binding(source):
                raise FeedbackError('Review requires a fresh proposal')
            quality = independence(row['developer'], row['planner'], reviewer)
            if not quality['distinct_roles']:
                raise FeedbackError('Independent reviewer identity required')
            if review['proposal_sha256'] != row['proposal_sha256']:
                raise FeedbackError('Review does not bind the exact proposal')
            if review['verdict'] == 'APPROVED' and not review['coverage_preserved']:
                raise FeedbackError('Approval cannot weaken obligation coverage')
            row.update(status='REVIEWED', reviewer=copy.deepcopy(reviewer), review=copy.deepcopy(review),
                       independence=quality)
            row['decisions'].append(dict(proposal=copy.deepcopy(row['proposal']), review=copy.deepcopy(review)))
            self._append('FEEDBACK_REVIEWED', state)
            return copy.deepcopy(row)

    def disposition(self, identity):
        row = self.inspect()['requests'][identity]
        if row['status'] not in ('REVIEWED', 'APPLIED'):
            return 'WAITING'
        if row['review']['verdict'] != 'APPROVED':
            return 'OPERATOR_DECISION'
        authority, _ = self._read()
        if authority['policy']['require_strong_independence'] and row['independence']['reduced_independence']:
            return 'OPERATOR_DECISION'
        return {'AMENDMENT': 'NATIVE_AMENDMENT_REQUIRED', 'PREREQUISITE': 'BLOCKED_PREREQUISITE',
                'ADVISORY': 'BACKLOG_ONLY', 'REJECTED': 'REJECTED_NO_RETRY',
                'INCONCLUSIVE': 'OPERATOR_DECISION'}[row['proposal']['route']]

    def reserve_role(self, identity, role, metadata):
        """Persist intent before a future funded adapter can launch a role.

        A returned intent is not a dispatch token. Only its newly-created
        caller may proceed; replay must use the receipt or reconcile. Native
        cumulative accounting remains a separate mandatory dispatch gate.
        """
        actor(metadata)
        if role not in ROLES:
            raise FeedbackError('Unknown feedback role')
        with ticket_lock(self.root / 'feedback.lock'):
            policy, state = self._active()
            row = state['requests'][identity]
            expected_status = 'PROPOSED' if role == 'reviewer' else 'REQUESTED'
            if row['status'] != expected_status:
                raise FeedbackError('Role does not match the feedback stage')
            actors = [row['developer']]
            if role == 'reviewer':
                actors.append(row['planner'])
            quality = independence(*actors, metadata)
            if not quality['distinct_roles']:
                raise FeedbackError('Role context must be independent')
            if role == 'reviewer' and policy['require_strong_independence'] and quality['reduced_independence']:
                raise FeedbackError('Reduced independence requires operator decision')
            operation = digest(dict(request_id=identity, role=role, round=row['rounds'],
                                    proposal_sha256=row.get('proposal_sha256')))
            previous = next((r for r in state['role_calls'] if r['operation_id'] == operation), None)
            if previous is not None:
                raise FeedbackError('Role already reserved; reuse its receipt or reconcile, never redispatch')
            if any(r['status'] == 'INTENT' for r in state['role_calls']):
                raise FeedbackError('Unknown feedback outcome requires reconciliation')
            if sum(r['role'] == role for r in state['role_calls']) >= policy['role_calls'][role]:
                raise FeedbackError('Named feedback role allowance exhausted')
            call = dict(operation_id=operation, request_id=identity, role=role,
                        actor=copy.deepcopy(metadata), status='INTENT')
            state['role_calls'].append(call)
            self._append('FEEDBACK_ROLE_INTENT', state)
            return copy.deepcopy(call)

    def record_role_result(self, operation, response):
        """Retain a response before interpreting it, including after deadline."""
        _hash(operation)
        if not isinstance(response, dict):
            raise FeedbackError('Structured feedback role response required')
        try:
            encoded = json.dumps(response, sort_keys=True, allow_nan=False)
        except (ValueError, TypeError):
            raise FeedbackError('Malformed feedback role response') from None
        if len(encoded) > 16000:
            raise FeedbackError('Feedback role response exceeds the bound')
        with ticket_lock(self.root / 'feedback.lock'):
            authority, state = self._read()
            if authority['status'] != 'ACTIVE' or state is None:
                raise FeedbackError('Feedback authorization is incomplete')
            call = next((r for r in state['role_calls'] if r['operation_id'] == operation), None)
            if call is None:
                raise FeedbackError('Role response has no durable intent')
            receipt = dict(response=copy.deepcopy(response), response_sha256=digest(response))
            if call['status'] == 'RETURNED':
                if call['receipt'] != receipt:
                    raise FeedbackError('Conflicting role response requires reconciliation')
                return copy.deepcopy(call)
            call.update(status='RETURNED', receipt=receipt)
            self._append('FEEDBACK_ROLE_RETURNED', state)
            return copy.deepcopy(call)

    def public_projection(self):
        """Only enums, counts and opaque IDs; never raw evidence or AI prose."""
        state = self.inspect()
        return dict(schema_version=1, requests=[dict(request_id=key, task_id=row['binding']['task_id'],
                    status=row['status'], rounds=row['rounds'], disposition=self.disposition(key),
                    reduced_independence=row.get('independence', {}).get('reduced_independence'))
                    for key, row in sorted(state['requests'].items())],
                    role_calls=len(state['role_calls']), amendments=len(state['amendments']))


def exact_authorization(value, expected):
    return (isinstance(value, dict) and value == expected
            and type(value.get('schema_version')) is int
            and value.get('authorized') is True and value.get('source_fenced') is True)


def main(argv=None):
    """Offline inspection only; no role dispatch, approval or runtime migration."""
    import argparse
    import sys
    from ssdlc_scope_amendments import inspect as inspect_scope
    from ssdlc_feedback_analysis import native_preview
    parser = argparse.ArgumentParser(description='Host feedback diagnostics and canonical status')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('diagnose', 'status'):
        command = commands.add_parser('feedback-' + name)
        command.add_argument('--repo', default='.')
        command.add_argument('--ticket', required=True)
        if name == 'diagnose':
            command.add_argument('--request', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.ticket) or args.ticket in {'.', '..'}:
        parser.error('Invalid feedback ticket')
    ticket = Path(args.repo).resolve() / '.ssdlc' / args.ticket
    try:
        if args.command == 'feedback-status':
            result = FeedbackStore(ticket).public_projection()
        else:
            request = json.loads(Path(args.request).read_text())
            with ticket_lock(ticket / 'ticket.lock'):
                source = inspect_scope(ticket)
                with ticket_lock(source['root'] / '03-dev/tasks/scheduler.lock'):
                    result = native_preview(source, request)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, TaskError, ValueError, KeyError, TypeError):
        print(json.dumps(dict(status='FEEDBACK_REFUSED', execution_authorized=False, model_dispatches=0)), file=sys.stderr)
        return 2
