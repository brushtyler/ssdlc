"""Bounded, restartable plan repair. Callers own the ticket lock and gate review."""
import json
from pathlib import Path

from ssdlc_tasks import PlanError, atomic_json, digest, file_hash, parse_plan_output, log_retry_reason, validate_document


def validate_plan_review(report: str, plan: dict, requirements: list[str], history: list[dict], quality=None) -> dict:
    """Require attributable findings and explicit dispositions of earlier blockers."""
    result = parse_plan_output(report)
    validate_document('task-plan-review', result)
    findings = result['findings']
    ids = [f['finding_id'] for f in findings]
    if len(ids) != len(set(ids)):
        raise PlanError('Duplicate review finding_id')
    task_ids = {t['task_id'] for t in plan['tasks']}
    quality = quality or {}
    standards = {s['standard_id'] for s in quality.get('standards', [])}
    checks = {c['check_id'] for c in quality.get('checks', [])}
    assessed = [h['assessment'] for h in history if 'assessment' in h]
    previous = assessed[-1]['findings'] if assessed else []
    old_ids = {f['finding_id'] for h in history for f in h.get('assessment', {}).get('findings', [])}
    old_findings = {f['finding_id']: f for assessment in assessed for f in assessment['findings']}
    outstanding = {f['finding_id'] for f in previous if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING'}
    if not outstanding <= set(ids):
        raise PlanError('Review omitted prior blocking findings: ' + ', '.join(sorted(outstanding - set(ids))))
    for finding in findings:
        old = old_findings.get(finding['finding_id'])
        if old and (any(set(old.get(field, [])) != set(finding.get(field, []))
                        for field in ('requirement_refs', 'standard_refs', 'quality_check_refs'))
                    or old['category'] != finding['category'] or old['severity'] != finding['severity']):
            raise PlanError('Review reassigned an existing finding ID; resolve it explicitly and use a new ID')
        if not set(finding['requirement_refs']) <= set(requirements) or not set(finding['task_ids']) <= task_ids:
            raise PlanError('Review references unknown requirements or tasks')
        if not set(finding.get('standard_refs', [])) <= standards or not set(finding.get('quality_check_refs', [])) <= checks:
            raise PlanError('Review references unknown standards or quality checks')
        if finding['severity'] == 'BLOCKING':
            if (not any(finding.get(field) for field in ('requirement_refs', 'standard_refs', 'quality_check_refs'))
                    or finding['category'] == 'IMPROVEMENT'):
                raise PlanError('Blocking finding needs an approved requirement or quality obligation and cannot be an optional improvement')
        if history and finding['finding_id'] not in old_ids and finding['new_issue_reason'] == 'INITIAL_REVIEW':
            raise PlanError('New review finding must explain introduction by revision or earlier omission')
    blockers = [f for f in findings if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
    if (result['verdict'] == 'APPROVED') != (not blockers):
        raise PlanError('Review verdict contradicts its open blocking findings')
    return result


class PlanRepair:
    def __init__(self, directory: Path, inputs: dict, validator, generate, review, budget, review_repairs=None,
                 structured_reviews=False):
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
        self.structured_reviews = structured_reviews

    def _history(self, session):
        return [{**{k: record[k] for k in ('attempt', 'status', 'candidate_sha256') if k in record},
                 **{field: (self.root / record[field]).read_text() for field in ('diagnostics', 'review') if field in record},
                 **({'assessment': record['assessment']} if 'assessment' in record else {})}
                for record in session['attempts'] if 'review' in record or 'diagnostics' in record]

    def _structured_review(self, session, record, plan, history):
        feedback = history
        for attempt in range(1, 3):
            record['review_invocations'] = attempt
            atomic_json(self.path, session)
            self.budget()
            report = self.review(plan, feedback)
            self._save_evidence(session, record, f'review-response-{attempt}', report)
            self.budget()
            try:
                assessment = validate_plan_review(report, plan, self.inputs['context']['requirement_catalogue']['required_ids'], history,
                                                  self.inputs['context'].get('quality_contract'))
                return report, assessment
            except PlanError as exc:
                errors = json.dumps(exc.diagnostics, indent=2)
                self._save_evidence(session, record, f'review-error-{attempt}', errors)
                if attempt == 2:
                    self._save_evidence(session, record, 'review', report)
                    self._save_evidence(session, record, 'diagnostics', errors)
                    record.update(status='REJECTED', failure_kind='REVIEW_PROTOCOL')
                    atomic_json(self.path, session)
                    raise PlanError('Invalid review protocol after one correction: ' + str(exc)) from exc
                print(f'[task-plan] Review protocol invalid: {exc}; correcting review once on the same candidate.', flush=True)
                feedback = history + [{'attempt': record['attempt'], 'invalid_response': report,
                                       'review_protocol_errors': exc.diagnostics,
                                       'instruction': 'Correct only the review response against the schema and history; the plan is unchanged.'}]

    def run(self, initial_candidate=None):
        fingerprint = digest({'inputs': self.inputs, 'review_protocol': 'structured-v1'}) if self.structured_reviews else digest(self.inputs)
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
            for hash_field in [key for key in record if key.endswith('_sha256')]:
                field = hash_field[:-len('_sha256')]
                path = self.root / record[field]
                if not path.is_file() or file_hash(path) != record[hash_field]:
                    raise PlanError('Planning evidence changed or disappeared')
        # Old sessions retain their original total bound unless explicitly extended.
        default_repairs = 0 if existing else 4 if self.structured_reviews else 2
        if not existing and self.review_repairs is not None:
            default_repairs = self.review_repairs
        limits = session.setdefault('limits', {'structural_candidates': 3, 'review_repairs': default_repairs})
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
            if record and record.get('failure_kind') in ('REVIEW_PROTOCOL', 'ENVIRONMENT_PREREQUISITE', 'STAGNATION'):
                blockers = [f for f in record.get('assessment', {}).get('findings', [])
                            if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
                raise PlanError('Task planning needs intervention: ' + record['failure_kind']
                                + f"; report: {self.root / record['review']}",
                                [{'code': record['failure_kind'], **finding} for finding in blockers] or None)
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
                    previous['history'] = self._history(session)
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
                if self.structured_reviews:
                    history = self._history(session)[:-1]
                    result = validate_plan_review((self.root / record['review']).read_text(), plan,
                                                  self.inputs['context']['requirement_catalogue']['required_ids'], history,
                                                  self.inputs['context'].get('quality_contract'))
                    if result['verdict'] != 'APPROVED':
                        raise PlanError('Persisted candidate has no approving review')
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
            history = self._history(session)
            if self.structured_reviews:
                report, assessment = self._structured_review(session, record, plan, history)
                record['assessment'] = assessment
                approved = assessment['verdict'] == 'APPROVED'
                blockers = [f for f in assessment['findings'] if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
                if any(f['category'] == 'ENVIRONMENT_PREREQUISITE' for f in blockers):
                    record['failure_kind'] = 'ENVIRONMENT_PREREQUISITE'
                # Stable IDs make three unchanged blocker sets a bounded stall, not another blind rewrite.
                signatures = [sorted(f['finding_id'] for f in h['assessment']['findings']
                                     if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING')
                              for h in history if 'assessment' in h]
                current = sorted(f['finding_id'] for f in blockers)
                if current and len(signatures) >= 2 and signatures[-2:] == [current, current]:
                    record['failure_kind'] = 'STAGNATION'
            else:
                report = self.review(plan)
                approved = report.strip().endswith('TASK_PLAN_STATUS: APPROVED')
            self._save_evidence(session, record, 'review', report)
            record['status'] = 'APPROVED' if approved else 'REJECTED'
            atomic_json(self.path, session)
            if not approved:
                if record.get('failure_kind'):
                    print(f"[task-plan] Stopping: {record['failure_kind']}; no further candidate generation.", flush=True)
                else:
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
