"""Persistent pair design: immutable round evidence and bounded model reservations.

The caller holds the ticket lock and verifies upstream approvals. Changes to sealed
inputs require a new explicit revision; rerunning never resets consumed calls.
"""
import json
import os
from pathlib import Path

from ssdlc_planning import validate_plan_review
from ssdlc_tasks import PlanError, DevelopmentBlocked, atomic_json, confined, digest, file_hash, log_retry_reason


class DesignSession:
    def __init__(self, directory: Path, inputs: dict, architect, reviewer, budget,
                 artifact=lambda path: None, initial_limit=5, extend_limit=None,
                 progress_policy=False, diagnose=None, verify_evidence=lambda item: False):
        for limit in (initial_limit, extend_limit):
            if limit is not None and (type(limit) is not int or not 1 <= limit <= 8):
                raise PlanError('Design round limit must be an integer between 1 and 8')
        self.root = directory / 'design-session'
        self.root.mkdir(exist_ok=True)
        self.path = self.root / 'session.json'
        self.inputs = inputs
        self.architect = architect
        self.reviewer = reviewer
        self.budget = budget
        self.artifact = artifact
        self.initial_limit = initial_limit
        self.extend_limit = extend_limit
        self.progress_policy, self.diagnose = progress_policy, diagnose
        self.verify_evidence = verify_evidence

    def _save(self, session):
        atomic_json(self.path, session)
        self.artifact(self.path)

    def _evidence(self, session, record, field, content):
        path = self.root / f"round-{record['round']}-{field}.txt"
        if path.exists():
            if field == 'adr_delta' and self.progress_policy and path.read_text() == content:
                record[field], record[field + '_sha256'] = path.name, file_hash(path)
                self._save(session)
                self.artifact(path)
                return
            raise PlanError('Refusing to overwrite design evidence')
        with path.open('x') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        record[field] = path.name
        record[field + '_sha256'] = file_hash(path)
        self._save(session)
        self.artifact(path)

    def _load(self):
        if self.path.exists():
            session = json.loads(self.path.read_text())
            if session['inputs_sha256'] != digest(self.inputs):
                raise PlanError('Design inputs changed; explicit design revision required')
            for record in session['rounds']:
                for key in record:
                    if key.endswith('_sha256'):
                        field = key[:-len('_sha256')]
                        path = confined(self.root, record[field], protect=False)
                        if not path.is_file() or file_hash(path) != record[key]:
                            raise PlanError('Design evidence changed or disappeared')
        else:
            session = {'schema_version': '1.0', 'inputs_sha256': digest(self.inputs),
                       'round_limit': self.extend_limit or self.initial_limit, 'rounds': []}
        if self.path.exists() and session.get('progress_policy', False) != self.progress_policy:
            raise PlanError('Design recovery policy changed; explicit migration required')
        if self.progress_policy:
            session['progress_policy'] = True
            for record in session['rounds']:
                for item in record.get('progress', {}).get('material_evidence', []):
                    try:
                        valid = self.verify_evidence(item)
                    except OSError:
                        valid = False
                    if not valid:
                        raise DevelopmentBlocked('INTEGRITY', 'Design progress evidence changed or disappeared')
        inputs_path = self.root / 'inputs.json'
        if inputs_path.exists():
            if digest(json.loads(inputs_path.read_text())) != session['inputs_sha256']:
                raise PlanError('Design input evidence changed')
        elif self.path.exists():
            raise PlanError('Design input evidence disappeared')
        else:
            atomic_json(inputs_path, self.inputs)
            self.artifact(inputs_path)
        if self.extend_limit is not None and self.extend_limit != session['round_limit']:
            if self.extend_limit < session['round_limit']:
                raise PlanError('Cannot reduce persisted design round limit')
            session.setdefault('policy_updates', []).append({'previous_limit': session['round_limit'],
                                                            'round_limit': self.extend_limit,
                                                            'reason': 'Explicit design continuation'})
            session['round_limit'] = self.extend_limit
        self._save(session)
        return session

    def _history(self, session, before=None):
        history = []
        for record in session['rounds']:
            if before is not None and record['round'] >= before:
                break
            if 'review' in record or 'legacy_review' in record:
                field = 'review' if 'review' in record else 'legacy_review'
                text = (self.root / record[field]).read_text()
                history.append({'round': record['round'], 'adr_sha256': record.get('adr_sha256'),
                                'review': text[-4000:] if self.progress_policy else text,
                                **({'review_ref': str(self.root / record[field]),
                                    'adr_delta': (self.root / record['adr_delta']).read_text() if 'adr_delta' in record else '',
                                    'adr_ref': str(self.root / record['adr'])} if self.progress_policy else {}),
                                **({'assessment': record['assessment']} if 'assessment' in record else {})})
        if self.progress_policy:
            import copy
            history = copy.deepcopy(history)
            for entry in history:
                assessment = entry.get('assessment', {})
                if 'analysis' in assessment:
                    assessment['analysis'] = assessment['analysis'][-1000:]
                for finding in assessment.get('findings', []):
                    for key in ('message', 'evidence', 'requested_change'):
                        if key in finding:
                            finding[key] = finding[key][-400:]
        return history

    def import_legacy(self, rounds: list[dict]):
        """Explicitly copy upstream-verified legacy rounds; never infer their approval."""
        if self.path.exists() or not rounds:
            raise PlanError('Legacy import requires a new design session and existing evidence')
        # Reserve all slots and import content in one atomic snapshot. Files remain
        # immutable; an interrupted import is reconciled, never silently restarted.
        session = self._load()
        session.update(origin='explicit_legacy_import', import_complete=False)
        self._save(session)
        for index, source in enumerate(rounds, 1):
            record = {'round': index, 'status': 'ADR_READY', 'origin': 'legacy'}
            session['rounds'].append(record)
            self._evidence(session, record, 'adr', source['adr'])
            if source.get('review') is not None:
                self._evidence(session, record, 'legacy_review', source['review'])
                record['status'] = 'REJECTED'
            self._save(session)
        session['import_complete'] = True
        self._save(session)

    def _assessment(self, report, history):
        return validate_plan_review(report, {'tasks': []}, self.inputs['context']['requirement_catalogue']['required_ids'],
                                    history, self.inputs['context'].get('quality_contract'), schema_name='design-review')

    def _review(self, session, record, adr, history):
        for index in range(1, 3):
            response = f'review_response_{index}'
            if response not in record:
                if record.get('review_reserved', 0) >= index:
                    # The previous call may have completed externally, but there
                    # is no captured response. Its reservation stays consumed.
                    continue
                self.budget()
                record.update(status='REVIEWING', review_reserved=index)
                self._save(session)
                feedback = list(history)
                if index > 1 and 'review_error_1' in record:
                    feedback.append({'review_protocol_errors': (self.root / record['review_error_1']).read_text(),
                                     'invalid_response': (self.root / record['review_response_1']).read_text(),
                                     'instruction': 'Correct the review only; the ADR is unchanged.'})
                print(f"[02-design] Reviewing round {record['round']} (response {index}/2)", flush=True)
                report = self.reviewer(record['round'], adr, feedback)
                self._evidence(session, record, response, report)
                self.budget()
            report = (self.root / record[response]).read_text()
            try:
                assessment = self._assessment(report, history)
            except PlanError as exc:
                if f'review_error_{index}' not in record:
                    self._evidence(session, record, f'review_error_{index}', json.dumps(exc.diagnostics, indent=2))
                print(f'[02-design] Invalid review: {exc}; at most one correction on the same ADR.', flush=True)
                continue
            record['assessment'] = assessment
            self._evidence(session, record, 'review', report)
            record['status'] = 'APPROVED' if assessment['verdict'] == 'APPROVED' else 'REJECTED'
            self._save(session)
            return assessment
        record.update(status='BLOCKED', failure_kind='REVIEW_PROTOCOL')
        self._save(session)
        raise PlanError(f'Design review unavailable or invalid after two reserved calls; evidence: {self.root}')

    def _progress(self, session, record, history, assessment, adr):
        from ssdlc_recovery import progress, RecoveryStop
        prior = history[-1].get('assessment', {}) if history else {}
        previous_records = [r for r in session['rounds'] if r['round'] < record['round'] and r.get('progress_evaluated')]
        resolved_once = {ref for r in previous_records for ref in r.get('progress', {}).get('resolved_obligations', [])}
        for previous in previous_records:
            closed_refs = {ref for f in previous.get('assessment', {}).get('findings', [])
                           if f['status'] == 'RESOLVED' and f['severity'] == 'BLOCKING'
                           for key in ('requirement_refs', 'standard_refs', 'quality_check_refs') for ref in f.get(key, [])}
            # A resolved finding does not clear an obligation still blocked by
            # another finding in that same assessment.
            resolved_once.update(closed_refs - set(previous['progress']['open_obligations']))
        record['progress'] = progress(prior, assessment, resolved_once)
        seen = {e['sha256'] for r in session['rounds'] if r['round'] < record['round']
                for e in r.get('progress', {}).get('material_evidence', [])}
        catalogue = set(self.inputs['context']['requirement_catalogue']['required_ids'])
        material = [e for e in assessment.get('progress_evidence', [])
                    if e['sha256'] not in seen and set(e['obligation_refs']) <= catalogue
                    and set(e['obligation_refs']) & set(record['progress']['open_obligations']) and self.verify_evidence(e)]
        record['progress']['material_evidence'] = material
        record['progress']['material_progress'] |= bool(material)
        previous = previous_records[-1] if previous_records else {}
        streak = 0 if not history or record['progress']['material_progress'] else previous.get('stagnant_rounds', 0) + 1
        record['stagnant_rounds'] = streak
        if previous.get('diagnosis') and not record['progress']['material_progress']:
            record.update(status='BLOCKED', failure_kind='DESIGN_STALLED')
        elif streak >= 2:
            if 'diagnosis_reserved' in record and 'diagnosis' not in record:
                raise RecoveryStop('OUTCOME_UNCERTAIN', 'Design diagnosis reserved without a captured result')
            if 'diagnosis' not in record:
                if record['round'] >= session['round_limit']:
                    record.update(status='BLOCKED', failure_kind='DESIGN_STALLED')
                    self._save(session)
                    raise RecoveryStop('DESIGN_STALLED', 'No remaining authorized design round for a diagnosis repair')
                subject = digest(record['progress']['open_obligations'])
                record['diagnosis_reserved'] = subject
                self._save(session)
                if self.diagnose is None:
                    raise RecoveryStop('DESIGN_STALLED', 'No authorized design diagnosis handler')
                try:
                    self.budget()
                    diagnosis = self.diagnose(subject, adr, history)
                except RecoveryStop:
                    record.update(status='BLOCKED', failure_kind='DESIGN_STALLED')
                    self._save(session)
                    raise
                if not isinstance(diagnosis, str) or not diagnosis.strip():
                    raise RecoveryStop('OUTCOME_UNCERTAIN', 'Diagnosis response missing; reservation remains consumed')
                self._evidence(session, record, 'diagnosis', diagnosis)
                record['stagnant_rounds'] = 0
        record['progress_evaluated'] = True
        self._save(session)

    def _pending_diagnosis(self, session):
        previous = next((r for r in reversed(session['rounds']) if r.get('progress_evaluated')), {})
        return bool(previous.get('diagnosis'))

    def _stall(self, session, record, message):
        from ssdlc_recovery import RecoveryStop
        record.update(status='BLOCKED', failure_kind='DESIGN_STALLED')
        self._save(session)
        raise RecoveryStop('DESIGN_STALLED', message)

    def run(self):
        session = self._load()
        if session.get('import_complete') is False:
            raise PlanError('Legacy design import was interrupted; explicit reconciliation required')
        while True:
            record = session['rounds'][-1] if session['rounds'] else None
            if record and record['status'] == 'BLOCKED':
                if self.progress_policy and record.get('failure_kind') == 'LIMIT_REACHED':
                    from ssdlc_recovery import RecoveryStop
                    raise RecoveryStop('LIMIT_REACHED', 'No remaining authorized design round for a diagnosis repair')
                if self.progress_policy and record.get('failure_kind') == 'DESIGN_STALLED':
                    from ssdlc_recovery import RecoveryStop
                    raise RecoveryStop('DESIGN_STALLED', 'Design did not progress after the bounded diagnosis')
                raise PlanError(f"Design blocked: {record['failure_kind']}; evidence: {self.root}")
            if record and record['status'] == 'APPROVED':
                report = (self.root / record['review']).read_text()
                assessment = self._assessment(report, self._history(session, record['round']))
                if assessment['verdict'] != 'APPROVED':
                    raise PlanError('Persisted design has no approving review')
                return (self.root / record['adr']).read_text(), report
            if self.progress_policy and record and record['status'] == 'REJECTED' and not record.get('progress_evaluated'):
                self._progress(session, record, self._history(session, record['round']), record['assessment'],
                               (self.root / record['adr']).read_text())
                continue
            if record and record['status'] == 'ARCH_RESERVED' and 'adr' not in record:
                record['status'] = 'ARCH_UNCERTAIN'
                self._save(session)
            if not record or record['status'] in ('REJECTED', 'ARCH_UNCERTAIN'):
                if len(session['rounds']) >= session['round_limit']:
                    if self.progress_policy:
                        from ssdlc_recovery import RecoveryStop
                        record.update(status='BLOCKED', failure_kind='DESIGN_STALLED')
                        self._save(session)
                        raise RecoveryStop('DESIGN_STALLED', 'Authorized design rounds exhausted; no further dispatch')
                    raise PlanError(f"Design round limit exhausted ({session['round_limit']}); resume with an explicit higher --rounds limit; evidence: {self.root}")
                if self.progress_policy and record and record['status'] == 'ARCH_UNCERTAIN' and self._pending_diagnosis(session):
                    self._stall(session, record, 'Architect produced no confirmed ADR after diagnosis')
                history = self._history(session)
                adr = next(((self.root / r['adr']).read_text() for r in reversed(session['rounds']) if 'adr' in r), '')
                self.budget()
                record = {'round': len(session['rounds']) + 1, 'status': 'ARCH_RESERVED'}
                session['rounds'].append(record)
                self._save(session)
                print(f"[02-design] Architect round {record['round']}/{session['round_limit']}; preserving prior ADR and {len(history)} reviews.", flush=True)
                if self.progress_policy and len(session['rounds']) > 1:
                    prior_round = next((r for r in reversed(session['rounds'][:-1]) if r.get('progress_evaluated')), {})
                    if 'diagnosis' in prior_round:
                        history.append({'diagnosis': (self.root / prior_round['diagnosis']).read_text()[-8000:],
                                        'instruction': 'Apply the bounded diagnosis within approved obligations; next round must show material progress.'})
                draft = self.architect(record['round'], adr, history)
                if not draft.strip():
                    record['status'] = 'ARCH_UNCERTAIN'
                    self._save(session)
                    if self.progress_policy and self._pending_diagnosis(session):
                        self._stall(session, record, 'Architect returned an empty ADR after diagnosis')
                    raise PlanError('Architect returned an empty ADR; reserved round remains consumed')
                self._evidence(session, record, 'adr', draft)
                if self.progress_policy:
                    import difflib
                    delta = '\n'.join(difflib.unified_diff(adr.splitlines(), draft.splitlines(), n=2))
                    self._evidence(session, record, 'adr_delta', delta[:12000])
                record['status'] = 'ADR_READY'
                self._save(session)
                self.budget()
            # A captured ADR survives an interrupted architect call or review.
            history = self._history(session, record['round'])
            adr = (self.root / record['adr']).read_text()
            if self.progress_policy and 'adr_delta' not in record:
                import difflib
                previous = session['rounds'][-2] if len(session['rounds']) > 1 else {}
                before = (self.root / previous['adr']).read_text() if 'adr' in previous else ''
                delta = '\n'.join(difflib.unified_diff(before.splitlines(), adr.splitlines(), n=2))
                self._evidence(session, record, 'adr_delta', delta[:12000])
            # A crash after the review file was captured must not call a model again.
            if 'review' in record:
                assessment = self._assessment((self.root / record['review']).read_text(), history)
                record['assessment'] = assessment
                record['status'] = 'APPROVED' if assessment['verdict'] == 'APPROVED' else 'REJECTED'
                self._save(session)
            else:
                try:
                    assessment = self._review(session, record, adr, history)
                except PlanError:
                    if self.progress_policy:
                        self._stall(session, record, 'No valid design review; progress cannot be established')
                    raise
            if record['status'] == 'APPROVED':
                print(f"[02-design] APPROVED in round {record['round']}.", flush=True)
                continue
            blockers = [f for f in assessment['findings'] if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
            if any(f['category'] == 'ENVIRONMENT_PREREQUISITE' for f in blockers):
                record.update(status='BLOCKED', failure_kind='ENVIRONMENT_PREREQUISITE')
            elif self.progress_policy:
                self._progress(session, record, history, assessment, adr)
            elif history and history[-1].get('adr_sha256') == record['adr_sha256']:
                previous = history[-1].get('assessment', {}).get('findings', [])
                prior_ids = {f['finding_id'] for f in previous if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING'}
                if prior_ids == {f['finding_id'] for f in blockers}:
                    record.update(status='BLOCKED', failure_kind='STAGNATION')
            self._save(session)
            decision = (f"stopping: {record['failure_kind']}" if record['status'] == 'BLOCKED'
                        else f"next round {record['round'] + 1}/{session['round_limit']}" if record['round'] < session['round_limit']
                        else 'attempt limit exhausted; no next round')
            print(f"[02-design] Round {record['round']}: REVISION_REQUIRED; {decision}.", flush=True)
            log_retry_reason('02-design', (self.root / record['review']).read_text(), self.root / record['review'])

    def transcript(self):
        session = self._load()
        parts = ['# Pair Design & STRIDE Threat Modeling Transcript\n']
        for record in session['rounds']:
            for field in ('adr', 'legacy_review', 'review'):
                if field in record:
                    parts.append(f"## Round {record['round']} — {field}\n\n" + (self.root / record[field]).read_text())
        return '\n\n'.join(parts)
