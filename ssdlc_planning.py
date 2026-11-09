"""Bounded, restartable plan repair. Callers own the ticket lock and gate review."""
import json
from pathlib import Path

from ssdlc_tasks import PlanError, atomic_json, digest, file_hash, parse_plan_output


class PlanRepair:
    def __init__(self, directory: Path, inputs: dict, validator, generate, review, budget):
        self.root = directory / 'task-planning'
        self.root.mkdir(exist_ok=True)
        self.path = self.root / 'session.json'
        self.inputs = inputs
        self.validator = validator
        self.generate = generate
        self.review = review
        self.budget = budget

    def run(self, initial_candidate=None):
        fingerprint = digest(self.inputs)
        session = json.loads(self.path.read_text()) if self.path.exists() else {
            'schema_version': '1.0', 'inputs_sha256': fingerprint, 'attempts': [],
        }
        if session['inputs_sha256'] != fingerprint:
            raise PlanError('Planning inputs changed; a new approved planning revision is required')
        if initial_candidate is not None and not session['attempts']:
            record = {'attempt': 1, 'status': 'CANDIDATE', 'origin': 'previous_archived_candidate'}
            session['attempts'].append(record)
            self._save_evidence(session, record, 'candidate', initial_candidate)
        for record in session['attempts']:
            for field in ('candidate', 'diagnostics', 'review'):
                if field in record:
                    path = self.root / record[field]
                    if not path.is_file() or file_hash(path) != record[field + '_sha256']:
                        raise PlanError('Planning evidence changed or disappeared')
        # At most one initial candidate and two repairs, including reserved calls
        # interrupted before their result was saved. Restarts do not reset the cap.
        while True:
            record = session['attempts'][-1] if session['attempts'] else None
            if record and record['status'] in ('CANDIDATE', 'VALID', 'REVIEWING', 'APPROVED'):
                candidate = (self.root / record['candidate']).read_text()
            else:
                if len(session['attempts']) >= 3:
                    raise PlanError('Plan repair exhausted: initial candidate plus two repairs')
                previous = None
                if record:
                    previous = {field: (self.root / record[field]).read_text()
                                for field in ('candidate', 'diagnostics', 'review') if field in record}
                record = {'attempt': len(session['attempts']) + 1, 'status': 'RESERVED'}
                session['attempts'].append(record)
                atomic_json(self.path, session)
                self.budget()
                candidate = self.generate(record['attempt'], self.inputs, previous)
                self._save_evidence(session, record, 'candidate', candidate)
                record['status'] = 'CANDIDATE'
                atomic_json(self.path, session)
                self.budget()
            try:
                print(f"[task-plan] Validating candidate {record['attempt']}/3", flush=True)
                plan = parse_plan_output(candidate)
                self.validator(plan)
            except PlanError as exc:
                print(f"[task-plan] Rejected candidate: {json.dumps(exc.diagnostics)}", flush=True)
                self._save_evidence(session, record, 'diagnostics', json.dumps(exc.diagnostics, indent=2))
                record['status'] = 'REJECTED'
                atomic_json(self.path, session)
                continue
            if record['status'] == 'APPROVED':
                return plan
            # An uncertain review is retried within the same persisted bound,
            # rather than issuing an unbounded extra call after each crash.
            if record['status'] == 'REVIEWING':
                record['status'] = 'REJECTED'
                atomic_json(self.path, session)
                continue
            record['status'] = 'REVIEWING'
            atomic_json(self.path, session)
            self.budget()
            print(f"[task-plan] Independent review of candidate {record['attempt']}/3", flush=True)
            report = self.review(plan)
            self._save_evidence(session, record, 'review', report)
            approved = report.strip().endswith('TASK_PLAN_STATUS: APPROVED')
            record['status'] = 'APPROVED' if approved else 'REJECTED'
            atomic_json(self.path, session)
            self.budget()
            if approved:
                return plan

    def _save_evidence(self, session, record, field, content):
        filename = f"attempt-{record['attempt']}-{field}.txt"
        path = self.root / filename
        if path.exists():
            raise PlanError('Refusing to overwrite planning evidence')
        path.write_text(content)
        record[field] = filename
        record[field + '_sha256'] = file_hash(path)
        atomic_json(self.path, session)
