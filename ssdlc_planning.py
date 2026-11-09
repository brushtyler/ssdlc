"""Bounded, restartable plan repair. Callers own the ticket lock and gate review."""
import json
from pathlib import Path

from ssdlc_tasks import PlanError, atomic_json, digest, file_hash, parse_plan_output, log_retry_reason


class PlanRepair:
    def __init__(self, directory: Path, inputs: dict, validator, generate, review, budget, review_repairs=None):
        self.root = directory / 'task-planning'
        self.root.mkdir(exist_ok=True)
        self.path = self.root / 'session.json'
        self.inputs = inputs
        self.validator = validator
        self.generate = generate
        self.review = review
        self.budget = budget
        if review_repairs is not None and (type(review_repairs) is not int or not 0 <= review_repairs <= 4):
            raise PlanError('review_repairs must be an integer between 0 and 4')
        self.review_repairs = review_repairs

    def run(self, initial_candidate=None):
        fingerprint = digest(self.inputs)
        existing = self.path.exists()
        session = json.loads(self.path.read_text()) if existing else {
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
        # Old sessions retain their original total bound unless explicitly extended.
        limits = session.setdefault('limits', {'structural_candidates': 3, 'review_repairs': 0 if existing else 2})
        if self.review_repairs is not None and self.review_repairs != limits['review_repairs']:
            if self.review_repairs < limits['review_repairs']:
                raise PlanError('Cannot reduce persisted planning limits during resume')
            session.setdefault('policy_updates', []).append({
                'previous_review_repairs': limits['review_repairs'], 'review_repairs': self.review_repairs,
                'reason': 'Explicit planning resume configuration',
            })
            limits['review_repairs'] = self.review_repairs
        atomic_json(self.path, session)
        while True:
            record = session['attempts'][-1] if session['attempts'] else None
            if record and record['status'] in ('CANDIDATE', 'VALID', 'REVIEWING', 'APPROVED'):
                candidate = (self.root / record['candidate']).read_text()
            else:
                semantic = bool(record and (record.get('review') or record.get('failure_kind') == 'REVIEW_UNCERTAIN'))
                used_semantic = sum(r.get('repair_kind') == 'SEMANTIC' for r in session['attempts'])
                used_structural = len(session['attempts']) - used_semantic
                if semantic and limits['review_repairs']:
                    if used_semantic >= limits['review_repairs']:
                        raise PlanError('Semantic plan repair exhausted')
                    repair_kind = 'SEMANTIC'
                else:
                    if used_structural >= limits['structural_candidates']:
                        raise PlanError('Structural plan repair exhausted: initial candidate plus two repairs')
                    repair_kind = 'STRUCTURAL'
                previous = None
                if record:
                    previous = {field: (self.root / record[field]).read_text()
                                for field in ('candidate', 'diagnostics', 'review') if field in record}
                    if not previous:
                        print('[task-plan] Previous invocation was reserved but no result was captured; retry consumes a new slot.', flush=True)
                record = {'attempt': len(session['attempts']) + 1, 'status': 'RESERVED', 'repair_kind': repair_kind}
                session['attempts'].append(record)
                atomic_json(self.path, session)
                self.budget()
                print(f"[task-plan] Generating candidate {record['attempt']} ({repair_kind.lower()} allowance)", flush=True)
                candidate = self.generate(record['attempt'], self.inputs, previous)
                self._save_evidence(session, record, 'candidate', candidate)
                record['status'] = 'CANDIDATE'
                atomic_json(self.path, session)
                self.budget()
            try:
                print(f"[task-plan] Validating candidate {record['attempt']}", flush=True)
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
                record['failure_kind'] = 'REVIEW_UNCERTAIN'
                atomic_json(self.path, session)
                continue
            record['status'] = 'REVIEWING'
            atomic_json(self.path, session)
            self.budget()
            print(f"[task-plan] Independent review of candidate {record['attempt']}", flush=True)
            report = self.review(plan)
            self._save_evidence(session, record, 'review', report)
            approved = report.strip().endswith('TASK_PLAN_STATUS: APPROVED')
            record['status'] = 'APPROVED' if approved else 'REJECTED'
            atomic_json(self.path, session)
            if not approved:
                print(f"[task-plan] Candidate {record['attempt']}: semantic review requested changes; a retry requires remaining allowance and budget.", flush=True)
                log_retry_reason('task-plan', report, self.root / record['review'])
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
