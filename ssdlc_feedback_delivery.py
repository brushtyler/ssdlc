"""Host-only sanitized GH projections and non-executing prerequisite proposals."""
import copy
import json
from pathlib import Path
import re
import subprocess

from ssdlc_feedback import FeedbackError, FeedbackStore
from ssdlc_tasks import TaskJournal, digest, ticket_lock
from ssdlc_transitions import _regular


class RemoteUncertain(FeedbackError):
    pass


def projection(store):
    value = store.public_projection()
    clean = {key: value[key] for key in ('schema_version', 'role_calls', 'amendments', 'amendments_reserved', 'amendments_applied')}
    for key in ('repair_returns', 'operator_repair_returns'):
        if key in value:
            if type(value[key]) is not int or value[key] < 0:
                raise FeedbackError('Noncanonical public repair count refused')
            clean[key] = value[key]
    clean['requests'] = []
    statuses = {'REQUESTED','PROPOSED','REVIEWED','APPLIED'}
    dispositions = {'WAITING','APPLIED','OPERATOR_DECISION','OWNER_REVALIDATION_REQUIRED','LIMIT_REACHED',
                    'NATIVE_AMENDMENT_REQUIRED','BLOCKED_PREREQUISITE','BACKLOG_ONLY','REJECTED_NO_RETRY',
                    'NATIVE_REPAIR_RETURN_REQUIRED','IN_SCOPE_RETURN_APPLIED'}
    quotas = {None,'TICKET_AMENDMENTS_EXHAUSTED','TASK_AMENDMENTS_EXHAUSTED','ADDED_FILES_EXHAUSTED'}
    for row in value['requests']:
        if (not re.fullmatch('[a-f0-9]{64}', row['request_id']) or row['status'] not in statuses
                or row['disposition'] not in dispositions or row['origin'] not in {'DEVELOPER','HOST_HINT','UNKNOWN'}
                or row.get('quota_reason') not in quotas):
            raise FeedbackError('Noncanonical public feedback projection refused')
        clean['requests'].append({key: row[key] for key in ('request_id','status','rounds','disposition','origin',
                                                            'quota_reason','reduced_independence')})
    return clean


def metrics(store):
    state = store.inspect(); now = store.clock()
    requests = []
    for identity, row in state['requests'].items():
        created, reviewed, applied = row.get('created_at'), row.get('reviewed_at'), row.get('applied_at')
        elapsed = lambda end: end - created if created is not None and end is not None and end >= created else None
        requests.append(dict(request_id=identity, disposition=store.disposition(identity),
            cause=row['request']['cause'], route=row.get('proposal',{}).get('route'),
            verdict=row.get('review',{}).get('verdict'), coverage_preserved=row.get('review',{}).get('coverage_preserved'),
            scope_additions=len(row.get('proposal',{}).get('requested_files',[])),
            old_plan_sha256=row['binding']['plan_sha256'], proposal_sha256=row.get('proposal_sha256'),
            reviewed_subject_sha256=row.get('amendment_subject_sha256'), new_plan_sha256=row.get('new_plan_sha256'),
            impact_attribution=row.get('impact', {}).get('status', 'UNKNOWN'),
            review_latency_seconds=elapsed(reviewed), activation_latency_seconds=elapsed(applied),
            observed_wait_seconds=elapsed(now) if store.disposition(identity) in {
                'OPERATOR_DECISION','OWNER_REVALIDATION_REQUIRED','BLOCKED_PREREQUISITE'} else None))
    calls = []
    for call in state['role_calls']:
        start, end = call.get('reserved_at'), call.get('returned_at')
        calls.append(dict(role=call['role'], status=call['status'],
            engine=call['actor']['engine'], model_sha256=digest(call['actor']['model']),
            context_sha256=digest(call['actor']['context_id']),
            duration_seconds=end-start if start is not None and end is not None and end >= start else None,
            usage=call.get('receipt', {}).get('response', {}).get('usage')))
    return dict(requests=requests, consultations=calls, role_count=len(calls),
                repair_returns=len(state.get('repair_returns', [])),
                operator_repair_returns=sum(r['operator'] for r in state.get('repair_returns', [])),
                source='host timestamps and captured usage; unknown values remain null',
                operator_decisions_required=sum(r['disposition'] in {'OPERATOR_DECISION','OWNER_REVALIDATION_REQUIRED'} for r in requests))


class GitHubComments:
    """Credentials stay in host gh. Responses are untrusted remote observations."""
    def _api(self, args, payload=None):
        command = ['gh', 'api', *args]
        if payload is not None:
            command += ['--input', '-']
        try:
            result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                    text=True, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise RemoteUncertain('GitHub operation could not be confirmed') from None
        if result.returncode:
            raise RemoteUncertain('GitHub operation could not be confirmed')
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise RemoteUncertain('GitHub returned an unsupported response') from None

    def actor(self):
        result = self._api(['user'])
        value = result.get('id') if isinstance(result, dict) else None
        if type(value) is not int or value < 1:
            raise RemoteUncertain('GitHub actor is not identified')
        return value

    def list(self, target):
        # gh 2.46 supports --paginate but not --slurp. Read explicit bounded
        # pages so one JSON document is decoded per call on both old/new CLIs.
        comments = []
        for number in range(1, 101):
            page = self._api([f"repos/{target['repo']}/issues/{target['issue']}/comments?per_page=100&page={number}"])
            if (not isinstance(page, list) or len(page) > 100
                    or any(not isinstance(row, dict) for row in page)):
                raise RemoteUncertain('GitHub comments could not be decoded')
            comments.extend(page)
            if len(page) < 100:
                return comments
        raise RemoteUncertain('Complete GitHub comment history exceeds the observation bound; no publication')

    def post(self, target, body):
        return self._api([f"repos/{target['repo']}/issues/{target['issue']}/comments", '--method', 'POST'], {'body': body})


def _author(comment):
    user = comment.get('user')
    return user.get('id') if isinstance(user,dict) else None


class ProjectionOutbox:
    def __init__(self, ticket):
        self.root = Path(ticket) / 'feedback' / 'publication'
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise FeedbackError('Symlink in publication authority')

    def _state(self):
        path = self.root / 'task-journal.jsonl'
        if not path.exists():
            if (self.root / 'tasks-state.json').exists():
                raise FeedbackError('Publication journal disappeared')
            return dict(schema_version=1, operations={})
        data = _regular(self.root, path.name).read_bytes()
        if not data or not data.endswith(b'\n'):
            raise FeedbackError('Incomplete publication history requires reconciliation')
        return TaskJournal._replay(data)[0]

    def _save(self, kind, state):
        journal = TaskJournal(self.root); journal.load(); journal.append(kind, state)

    def publish(self, feedback, profile, backend=None, *, fault=lambda point: None):
        from ssdlc_feedback_workflow import workflow
        config = workflow(profile)
        if not config or 'github' not in config:
            return dict(status='NOT_AUTHORIZED')
        target = config['github']
        data = projection(feedback)
        identity = digest(dict(target=target, ticket_sha256=digest(str(feedback.ticket)), data=data))
        marker = '<!-- ssdlc-feedback:' + identity + ' -->'
        body = marker + '\nSSDLC feedback projection (host-generated; no execution authority).\n```json\n' + json.dumps(data, sort_keys=True) + '\n```'
        backend = backend or GitHubComments()
        with ticket_lock(self.root / 'publication.lock'):
            state = self._state()
            actor = backend.actor()
            for key, previous in state['operations'].items():
                if key == identity or previous['status'] in {'SENT', 'SUPERSEDED_NOT_DISPATCHED'}:
                    continue
                if previous['status'] == 'PREPARED':
                    previous['status'] = 'SUPERSEDED_NOT_DISPATCHED'
                    self._save('PUBLICATION_NOT_DISPATCHED', state)
                    continue
                found = [c for c in backend.list(previous['target'])
                         if _author(c) == previous['actor'] and c.get('body') == previous['body']]
                if len(found) != 1 or type(found[0].get('id')) is not int:
                    raise RemoteUncertain('An earlier remote outcome must be reconciled before a new publication')
                previous.update(status='SENT', remote_id=found[0]['id'])
                self._save('PUBLICATION_RECONCILED', state)
            row = state['operations'].get(identity)
            if row is None:
                row = dict(target=target, body=body, body_sha256=digest(body), actor=actor, status='PREPARED')
                state['operations'][identity] = row
                self._save('PUBLICATION_PREPARED', state)
            if row['actor'] != actor or row['target'] != target or row['body_sha256'] != digest(body):
                raise RemoteUncertain('GitHub publication identity changed')
            comments = backend.list(target)
            matches = [c for c in comments if _author(c) == actor and c.get('body') == body]
            if len(matches) > 1:
                raise RemoteUncertain('Duplicate remote publications require reconciliation')
            if len(matches) == 1:
                if type(matches[0].get('id')) is not int:
                    raise RemoteUncertain('Invalid remote publication identity')
                if row['status'] != 'SENT' or row.get('remote_id') != matches[0]['id']:
                    row.update(status='SENT', remote_id=matches[0]['id'])
                    self._save('PUBLICATION_RECONCILED', state)
                return copy.deepcopy(row)
            if row['status'] != 'PREPARED':
                raise RemoteUncertain('Prior publication outcome is unknown or remote evidence changed; no repost')
            # Even a crash immediately before POST is ambiguous on restart.
            row['status'] = 'DISPATCH_RESERVED'
            self._save('PUBLICATION_DISPATCH_RESERVED', state)
            fault('before_publication')
            try:
                result = backend.post(target, body)
            except BaseException:
                # Intent survives; reconciliation may observe the own exact
                # comment later. Absence is never proof permitting another POST.
                raise
            fault('after_publication')
            if (not isinstance(result,dict) or type(result.get('id')) is not int or result.get('body') != body
                    or _author(result) != actor):
                raise RemoteUncertain('GitHub return does not confirm the exact own publication')
            row.update(status='SENT', remote_id=result['id'])
            self._save('PUBLICATION_SENT', state)
            return copy.deepcopy(row)

    def summary(self):
        values = list(self._state()['operations'].values())
        return dict(operations=len(values), sent=sum(r['status'] == 'SENT' for r in values),
                    unconfirmed=sum(r['status'] not in {'SENT', 'SUPERSEDED_NOT_DISPATCHED'} for r in values))


def prerequisite(store, request_id, child, edges):
    """A reviewed blocking proposal; never create a task, GH issue or allowance."""
    if not isinstance(child, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', child):
        raise FeedbackError('Invalid prerequisite identity')
    if not isinstance(edges, list) or len(edges) > 100:
        raise FeedbackError('Bounded prerequisite edges required')
    parent = store.ticket.name
    graph = {}
    for pair in [*edges, [parent, child]]:
        if (not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(v, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', v) for v in pair)):
            raise FeedbackError('Invalid prerequisite edge')
        graph.setdefault(pair[0], set()).add(pair[1])
    visiting, visited = set(), set()
    def visit(node):
        if node in visiting:
            raise FeedbackError('Circular prerequisite or wait dependency refused')
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, ()):
            visit(dependency)
        visiting.remove(node); visited.add(node)
    for node in graph:
        visit(node)
    with ticket_lock(store.root / 'feedback.lock'):
        _, state = store._active()
        row = state['requests'][request_id]
        if store.disposition(request_id) != 'BLOCKED_PREREQUISITE':
            raise FeedbackError('A prerequisite needs a reviewed blocking proposal')
        value = dict(child=child, edges=sorted([list(edge) for edge in {(a,b) for a, targets in graph.items() for b in targets}]),
                     status='NATIVE_INTAKE_REQUIRED', execution_authorized=False)
        if row.get('prerequisite') is not None:
            if row['prerequisite'] != value:
                raise FeedbackError('Prerequisite proposal changed; explicit reconciliation required')
            return value
        row['prerequisite'] = value
        store._append('FEEDBACK_PREREQUISITE_PROPOSED', state)
        return value


def prerequisite_return(store, request_id, child):
    row = store.inspect()['requests'][request_id]
    if row.get('prerequisite', {}).get('child') != child:
        raise FeedbackError('Unbound prerequisite return refused')
    return dict(status='NATIVE_DAG_TRANSITION_REQUIRED', execution_authorized=False,
                parent_unchanged=True, request_id=request_id)
