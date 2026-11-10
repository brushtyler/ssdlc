"""Read-only bounce proposals from host-verified evaluation data.

This module does not verify provenance or persist artifacts. Callers must supply
verified evaluations for one explicit phase/attempt and trusted quality inputs.
Stored assessments have no authority. No output grants approval or execution.
"""
import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from ssdlc_findings import FindingsError, assess_report
from ssdlc_gate_reports import build_trusted_context, bind_report, decide, ProtocolError
from ssdlc_tasks import PlanError, TaskError, TaskPlanValidator


class BounceError(Exception):
    """A stable reason code, without untrusted exception text."""

    def __init__(self, code):
        self.code = code
        self.reason_code = code
        super().__init__(code)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False) + '\n'


def select_response(evaluator_dir_listing):
    """Select a substantive response from filename -> decoded-record data.

    Diagnostics must be decoded JSON lists. Other companion values are unused.
    Invocation records are deliberately irrelevant to response selection.
    """
    if not isinstance(evaluator_dir_listing, Mapping):
        raise BounceError('malformed_record')
    records = evaluator_dir_listing
    ordinals = set()
    for name in records:
        if type(name) is not str:
            raise BounceError('malformed_record')
        if name.startswith('response-'):
            match = re.fullmatch(r'response-([12])\.md', name)
            if not match:
                raise BounceError('contradictory_evidence')
            ordinals.add(int(match[1]))
    if ordinals not in ({1}, {1, 2}):
        raise BounceError('contradictory_evidence')
    n = max(ordinals)
    diagnostics = records.get(f'diagnostics-{n}.json')
    if type(diagnostics) is not list:
        raise BounceError('malformed_record' if f'diagnostics-{n}.json' in records
                          else 'contradictory_evidence')
    if diagnostics != []:
        raise BounceError('contradictory_evidence')
    if n == 1:
        if any(name in records for name in ('prompt-2.json', 'reservation-2.json',
                                            'correction-record.json', 'diagnostics-2.json')):
            raise BounceError('contradictory_evidence')
    else:
        if not {'prompt-2.json', 'correction-record.json', 'diagnostics-1.json'} <= records.keys():
            raise BounceError('contradictory_evidence')
        if type(records['diagnostics-1.json']) is not list:
            raise BounceError('malformed_record')
        if not records['diagnostics-1.json']:
            raise BounceError('contradictory_evidence')
    return n, f'response-{n}.md'


def validate_controls(gate, controls, scanner_status=None):
    keys = {'review': set(), 'qa': {'det_ok'}, 'appsec': {'scanners'}}
    if (gate not in keys or type(controls) is not dict or
            set(controls) != keys[gate] or any(type(v) is not bool for v in controls.values())):
        raise BounceError('malformed_record')
    if gate == 'appsec':
        if (type(scanner_status) is not dict or type(scanner_status.get('scanners')) is not list or
                any(type(s) is not dict or type(s.get('status')) is not str
                    for s in scanner_status['scanners'])):
            raise BounceError('malformed_record')
        expected = all(s['status'] in ('passed', 'not_applicable') for s in scanner_status['scanners'])
        if controls['scanners'] != expected:
            raise BounceError('contradictory_evidence')
    return dict(controls)


def plan_bounce(*, repo, plan, requirements_text, quality, reviewed_commit,
                evaluations, phase, attempt, artifacts=None):
    """Return fresh canonicalizable proposal data, without changing caller state.

    Evaluations contain gate, identity, reviewer_ordinal, report, outcome_class,
    verdict, controls and evidence_refs; scanner_status is required for AppSec.
    Extra metadata (including assessment) is retained but never used as authority.
    The caller owns provenance verification; these are pure data-shape inputs.
    """
    result = dict(schema_version='1.0', phase=phase, attempt=attempt,
                  reviewed_commit=reviewed_commit, execution_authorized=False,
                  approved=False, executable=False, findings=[], evaluations=[],
                  candidates=[], impact_analysis=[], replanning=[], diagnosis=[],
                  optional_observations=[], blockers=[], repair_roots=[],
                  revalidation_tasks=[], proposals=[])

    def block(code, identity=None):
        item = {'code': code}
        if identity is not None:
            item['evaluation'] = identity
        result['blockers'].append(item)

    ordered = None
    try:
        ordered = TaskPlanValidator(Path(repo), requirements_text, quality,
                                    Path(artifacts) if artifacts is not None else None).validate(plan)
    except (PlanError, TaskError, OSError, ValueError, TypeError, KeyError):
        block('invalid_graph')
    roots = set()
    seen = set()
    for evaluation in evaluations:
        e = copy.deepcopy(evaluation)
        result['evaluations'].append({k: e[k] for k in (
            'gate', 'identity', 'reviewer_ordinal', 'outcome_class', 'verdict',
            'controls', 'evidence_refs', 'source_refs', 'source_hashes',
            'response_ordinal', 'telemetry_present') if k in e})
        identity = e.get('identity')
        try:
            gate, ordinal = e['gate'], e['reviewer_ordinal']
            if (gate not in ('review', 'qa', 'appsec') or type(identity) is not str or
                    not identity or type(ordinal) is not int or ordinal < 1):
                raise BounceError('malformed_record')
            panel_key = (gate, identity, ordinal)
            if panel_key in seen:
                raise BounceError('contradictory_evidence')
            seen.add(panel_key)
            outcome = e['outcome_class']
            if outcome not in ('approved', 'rejected_substantive'):
                result['diagnosis'].append({'evaluation': identity, 'code': outcome})
                raise BounceError('evaluation_diagnostic')
            controls = validate_controls(gate, e['controls'], e.get('scanner_status'))
            context = build_trusted_context(reviewed_commit, plan, requirements_text,
                                            quality, e['evidence_refs'])
            bind_report(e['report'], gate, context)
            assessment = assess_report(e['report'], context)
            ids = {}
            for f in assessment.findings:
                key = [gate, identity, ordinal, f.id]
                ids[f.id] = key
                result['findings'].append(dict(identity=key, finding=f.to_dict(),
                    provenance=dict(phase=phase, attempt=attempt, reviewed_commit=reviewed_commit)))
            for root in assessment.correction_roots:
                roots.add(root.task_id)
                result['candidates'].append(dict(task_id=root.task_id,
                    finding_identities=[ids[f] for f in root.finding_ids]))
            for target, field in (('impact_analysis', 'impact_assessment_required'),
                                  ('replanning', 'replanning_required'),
                                  ('diagnosis', 'diagnosis_required'),
                                  ('optional_observations', 'optional_observations')):
                result[target].extend(ids[f] for f in getattr(assessment, field))
            if not all(controls.values()):
                block('control_failed', identity)
            verdict = e['verdict']
            if verdict not in {'review': ('APPROVED', 'CHANGES_REQUESTED'),
                              'qa': ('PASSED', 'FAILED'),
                              'appsec': ('SECURE', 'VULNERABLE')}[gate]:
                raise BounceError('contradictory_evidence')
            negative = verdict in ('CHANGES_REQUESTED', 'FAILED', 'VULNERABLE')
            if negative and not assessment.correction_roots:
                block('negative_verdict_without_roots', identity)
            expected_approval, _ = decide(verdict, assessment, controls, True)
            if (outcome == 'approved') != expected_approval:
                block('contradictory_evidence', identity)
        except FindingsError as exc:
            codes = {d['code'] for d in exc.diagnostics}
            block('unknown_root' if 'unknown_task' in codes else 'report_invalid', identity)
        except ProtocolError:
            block('contradictory_evidence', identity)
        except BounceError as exc:
            block(exc.code, identity)
        except (KeyError, TypeError, ValueError, TaskError):
            block('malformed_record', identity)
    if ordered is not None:
        known = {t['task_id'] for t in ordered}
        if not roots <= known:
            block('unknown_root')
        if not any(b['code'] in ('invalid_graph', 'unknown_root') for b in result['blockers']):
            affected = set(roots)
            for task in ordered:
                if affected.intersection(task['depends_on']):
                    affected.add(task['task_id'])
            result['repair_roots'] = [t['task_id'] for t in ordered if t['task_id'] in roots]
            result['revalidation_tasks'] = [t['task_id'] for t in ordered
                                             if t['task_id'] in affected - roots]
    for field in ('impact_analysis', 'replanning', 'diagnosis'):
        if result[field]:
            block(field + '_required')
    result['status'] = 'blocked' if result['blockers'] else ('candidates' if roots else 'empty')
    for field in ('findings', 'evaluations', 'candidates', 'impact_analysis', 'replanning',
                  'diagnosis', 'optional_observations', 'blockers'):
        result[field].sort(key=lambda item: (item['identity'] if field == 'findings'
                                           else canonical_json(item)))
    result['revision'] = hashlib.sha256(canonical_json(result).encode('utf-8')).hexdigest()
    return result
