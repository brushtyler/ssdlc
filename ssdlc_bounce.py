"""Read-only bounce proposals from host-verified evaluation data.

The evidence reader verifies one explicit host phase/attempt without writes.
The pure planner also accepts already verified evaluations and trusted inputs.
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


# These records are controller-owned; hashes provide integrity, not authentication.
PHASES = {'04-review': 'review', '05-qa': 'qa', '05b-appsec': 'appsec'}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def request_identity(request):
    """Describe the public evaluator identity before invoking an evaluator."""
    from ssdlc_gate_reports import canon, slot_key, evaluation_identity
    schema = json.loads((Path(__file__).parent / 'schemas/gate-findings.schema.json').read_text())
    c = request.context
    binding = dict(version=2, fingerprint=request.fingerprint,
        system_prompt_sha256=_sha(request.system_prompt.encode()),
        user_prompt_sha256=_sha(request.user_prompt.encode()), schema_sha256=_sha(canon(schema)),
        reasoning_effort=request.reasoning_effort, controls=request.controls,
        context=dict(commit=c.expected_commit, task_ids=sorted(c.known_task_ids),
                     obligations=sorted(c.obligation_ids), evidence=sorted(c.evidence_refs)))
    fp = _sha(canon(binding))
    slot = slot_key(request.gate, request.engine, request.model, request.ordinal,
                    request.attempt, request.reviewed_commit)
    return dict(gate=request.gate, engine=request.engine, model=request.model,
                ordinal=request.ordinal, attempt=request.attempt,
                reviewed_commit=request.reviewed_commit, fingerprint=fp,
                identity=evaluation_identity(slot, fp), slot=slot)


def parse_final_verdict(gate, text):
    """Same strict final-line contract as the public bridge parsers."""
    marker, values = {'review': ('REVIEW_STATUS', ('APPROVED', 'CHANGES_REQUESTED')),
                      'qa': ('QA_STATUS', ('PASSED', 'FAILED')),
                      'appsec': ('APPSEC_STATUS', ('SECURE', 'VULNERABLE'))}[gate]
    if len(re.findall(r'\b' + marker + r'\s*:', text)) != 1:
        return None
    final = text.rstrip().splitlines()[-1].strip() if text.strip() else ''
    return next((v for v in values if final == marker + ': ' + v), None)


def _safe_path(root, name):
    from ssdlc_tasks import confined
    root = Path(root).absolute()
    path = Path(name)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise BounceError('contradictory_evidence') from exc
    if '..' in path.parts or '\\' in str(path):
        raise BounceError('unsafe_path')
    # lstat checks each existing ancestor; dangling symlinks also reject.
    import stat
    for ancestor in (root, *root.parents):
        try:
            if stat.S_ISLNK(ancestor.lstat().st_mode):
                raise BounceError('unsafe_path')
        except FileNotFoundError:
            pass
    current = root
    if root.is_symlink():
        raise BounceError('contradictory_evidence')
    for part in path.parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise BounceError('contradictory_evidence')
        except FileNotFoundError:
            pass
    return confined(root, str(path), protect=False)


def _verify_recorded_evaluation(*, root, registry, directory, expected, context):
    """Read-only equivalent of evaluator integrity, plus persisted outcome checks.

    Uses only public controller APIs. Unlike live _integrity this never reconciles
    registrations. Every retained evaluator file must already be registered.
    """
    from ssdlc_gate_reports import canon, evaluation_identity, slot_key, verify_evidence, extract_report
    root = Path(root).absolute()
    directory = _safe_path(root, directory)
    records, hashes = {}, {}
    for path in sorted(directory.rglob('*')):
        path = _safe_path(root, path)
        if path.is_file() and not path.name.startswith('.gate-'):
            name = str(path.relative_to(directory))
            data = path.read_bytes()
            ref = str(path.relative_to(root))
            if ref not in registry or registry[ref]['sha256'] != _sha(data):
                raise BounceError('integrity_mismatch')
            hashes[name] = _sha(data)
            # assessment is integrity-covered, but has no interpretive authority.
            records[name] = (json.loads(data) if name.endswith('.json') and name != 'assessment.json'
                             else data.decode() if not name.startswith('evidence/') else data)
    prefix = str(directory.relative_to(root)) + '/'
    if any(ref.startswith(prefix) and ref[len(prefix):] not in hashes for ref in registry):
        raise BounceError('integrity_mismatch')
    if not {'input.json', 'outcome.json', 'prompt-1.json'} <= records.keys():
        raise BounceError('incomplete_evidence')
    metadata, outcome = records['input.json'], records['outcome.json']
    if type(metadata) is not dict or type(outcome) is not dict:
        raise BounceError('malformed_record')
    binding = metadata['decision_inputs']
    if type(binding) is not dict:
        raise BounceError('malformed_record')
    gate = expected['gate']
    controls = validate_controls(gate, binding.get('controls'), records.get('scanner-status.json'))
    fp = _sha(canon(binding))
    slot = slot_key(gate, metadata['engine'], metadata['model'], metadata['ordinal'],
                    metadata['attempt'], metadata['reviewed_commit'])
    identity = evaluation_identity(slot, fp)
    for key in ('gate', 'engine', 'model', 'attempt', 'reviewed_commit', 'fingerprint', 'identity'):
        if metadata[key] != expected[key] or outcome[key] != expected[key]:
            raise BounceError('contradictory_evidence')
    if (metadata['ordinal'] != expected['ordinal'] or expected['slot'] != slot or
            identity != expected['identity'] or fp != metadata['fingerprint'] or
            outcome['evidence_catalogue_sha256'] != _sha(canon(metadata))):
        raise BounceError('contradictory_evidence')
    manifest = outcome['manifest']
    if type(manifest) is not dict or set(manifest) != set(hashes) - {'outcome.json'}:
        raise BounceError('integrity_mismatch')
    if any(hashes[name] != digest for name, digest in manifest.items()):
        raise BounceError('integrity_mismatch')
    entries = metadata['evidence']
    if type(entries) is not list:
        raise BounceError('malformed_record')
    for entry in entries:
        for key in ('source', 'captured'):
            path = _safe_path(root, entry[key])
            ref = str(path.relative_to(root))
            if ref not in registry or registry[ref]['sha256'] != entry['sha256']:
                raise BounceError('integrity_mismatch')
        if _safe_path(root, entry['captured']).parent != directory / 'evidence':
            raise BounceError('contradictory_evidence')
    verify_evidence(entries)
    if gate == 'appsec':
        status_entries = [e for e in entries if e['ref'] == 'ev/sca-status']
        if len(status_entries) != 1 or records.get('scanner-status.json') != json.loads(
                _safe_path(root, status_entries[0]['captured']).read_text()):
            raise BounceError('contradictory_evidence')
    expected_context = dict(commit=context.expected_commit, task_ids=sorted(context.known_task_ids),
                            obligations=sorted(context.obligation_ids), evidence=sorted(context.evidence_refs))
    if binding['context'] != expected_context or {e['ref'] for e in entries} != set(context.evidence_refs):
        raise BounceError('contradictory_evidence')
    prompt = records['prompt-1.json']
    schema = json.loads((Path(__file__).parent / 'schemas/gate-findings.schema.json').read_text())
    if (binding['schema_sha256'] != _sha(canon(schema)) or
            _sha(prompt['system'].encode()) != binding['system_prompt_sha256'] or
            metadata['system_prompt_sha256'] != binding['system_prompt_sha256'] or
            metadata['user_prompt_sha256'] != binding['user_prompt_sha256']):
        raise BounceError('contradictory_evidence')
    # The original user prompt precedes the evaluator's trusted-context suffix.
    from ssdlc_gate_reports import untrusted_data
    suffix = '\n' + untrusted_data('trusted context', dict(source_gate={
        'review': 'code_review', 'qa': 'qa', 'appsec': 'appsec'}[gate], **expected_context))
    # The evaluator uses commit/task_ids/obligations/evidence, exactly as above.
    positions = [m.start() for m in re.finditer(re.escape(suffix), prompt['user'])]
    if not any(_sha(prompt['user'][:p].encode()) == binding['user_prompt_sha256'] for p in positions):
        raise BounceError('contradictory_evidence')
    record = dict(gate=gate, identity=identity, reviewer_ordinal=metadata['ordinal'],
        outcome_class=outcome['outcome_class'], verdict=outcome['verdict'], controls=controls,
        evidence_refs=sorted(context.evidence_refs), evidence_state='verified',
        source_refs={n: str((directory / n).relative_to(root)) for n in hashes}, source_hashes=hashes,
        scanner_status=records.get('scanner-status.json'))
    if outcome['outcome_class'] in ('approved', 'rejected_substantive'):
        n, response = select_response(records)
        telemetry = f'invocation-{n}.json'
        if telemetry in records and type(records[telemetry]) is not dict:
            raise BounceError('malformed_record')
        report = records['report.json']
        bind_report(report, gate, context)
        if extract_report(records[response], identity[:16]) != report:
            raise BounceError('contradictory_evidence')
        verdict = parse_final_verdict(gate, records[response])
        if verdict is None or verdict != outcome['verdict']:
            raise BounceError('contradictory_evidence')
        approved, reasons = decide(verdict, assess_report(report, context), controls, True)
        if (type(outcome['approved']) is not bool or outcome['approved'] != approved or
                (outcome['outcome_class'] == 'approved') != approved):
            raise BounceError('contradictory_evidence')
        record.update(report=report, response_ordinal=n, telemetry_present=telemetry in records,
                      informational=[] if telemetry in records else ['telemetry_missing'])
    elif outcome['approved'] is not False:
        raise BounceError('contradictory_evidence')
    return record


def verify_recorded_evaluation(**kwargs):
    """Translate persisted-record errors while preserving deliberate crashes."""
    try:
        return _verify_recorded_evaluation(**kwargs)
    except BounceError:
        raise
    except ProtocolError as exc:
        raise BounceError('contradictory_evidence' if exc.code == 'binding_gate'
                          else 'report_invalid') from exc
    except FindingsError as exc:
        raise BounceError('report_invalid') from exc
    except (TaskError, OSError) as exc:
        raise BounceError('integrity_mismatch') from exc
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise BounceError('malformed_record') from exc


def read_evidence(*, root, state, repo, worktree, phase, attempt):
    """D12 boundary: read exact host phase records or return a blocked result.

    No provider, scanner, scheduler, registration or state writer is constructed.
    BaseException deliberately escapes. No older attempt can replace a newer one.
    """
    from ssdlc_tasks import git
    from ssdlc_gate_reports import canon
    failure = dict(status='blocked', execution_authorized=False, evaluations=[], blockers=[])
    try:
        if phase not in PHASES or type(attempt) is not int or attempt < 1:
            raise BounceError('malformed_record')
        root = Path(root).absolute()
        registry = state['artifacts']
        source_hashes = {}
        base = root / phase / 'provenance'
        rosters = list(base.glob('attempt-*/roster.json'))
        if not rosters:
            failure['status'] = 'rejected-legacy'
            raise BounceError('rejected-legacy')
        if not state.get('task_breakdown'):
            failure['status'] = 'rejected-legacy'
            raise BounceError('rejected-legacy')
        if any(int(p.parent.name.removeprefix('attempt-')) > attempt for p in rosters):
            raise BounceError('superseded_attempt')

        def registered(name):
            path = _safe_path(root, name)
            ref = str(path.relative_to(root))
            if ref not in registry or not path.is_file() or _sha(path.read_bytes()) != registry[ref]['sha256']:
                raise BounceError('integrity_mismatch')
            source_hashes[ref] = registry[ref]['sha256']
            return path

        def checkpoint(head):
            if (head != state.get('task_verified_head') or git(worktree, 'rev-parse', 'HEAD') != head
                    or git(worktree, 'status', '--porcelain')):
                raise BounceError('stale_checkpoint')
            if (root / '03-dev/implementation.diff').read_text() != git(
                    worktree, 'diff', '--binary', state['base_commit'], 'HEAD') + '\n':
                raise BounceError('stale_checkpoint')

        roster_path = base / f'attempt-{attempt}/roster.json'
        completion_path = roster_path.with_name('completion.json')
        if not roster_path.is_file() or not completion_path.is_file():
            raise BounceError('incomplete_evidence')
        roster = json.loads(registered(roster_path).read_text())
        completion = json.loads(registered(completion_path).read_text())
        if (roster['phase'] != phase or roster['attempt'] != attempt or
                completion['roster_sha256'] != _sha(canon(roster)) or
                completion['phase'] != phase or completion['attempt'] != attempt):
            raise BounceError('contradictory_evidence')
        members = roster['evaluators']
        if (type(members) is not list or not members or
                [m['ordinal'] for m in members] != list(range(1, len(members) + 1)) or
                (phase != '04-review' and len(members) != 1) or
                len(completion['evaluators']) != len(members)):
            raise BounceError('incomplete_evidence')
        head = roster['reviewed_commit']
        checkpoint(head)
        plan = json.loads(registered('02-design/plan.json').read_text())
        quality = json.loads(registered('00-recon/quality-contract.json').read_text())
        requirements = registered('01-po/requirements.md').read_text()
        if plan['base_commit'] != state['base_commit'] or plan['ticket_id'] != state['ticket_id']:
            raise BounceError('contradictory_evidence')
        TaskPlanValidator(Path(repo), requirements, quality, root).validate(plan)
        evaluations = []
        for member, finished in zip(members, completion['evaluators']):
            if finished['evaluator'] != member or member['gate'] != PHASES[phase] or member['attempt'] != attempt or member['reviewed_commit'] != head:
                raise BounceError('contradictory_evidence')
            directory = root / phase / 'evaluations' / member['slot'][:16]
            metadata = json.loads(registered(directory / 'input.json').read_text())
            refs = [e['ref'] for e in metadata['evidence']]
            required = {'ev/requirements', 'ev/plan', 'ev/quality-contract', 'ev/diff'}
            if phase == '05-qa':
                required.add('ev/qa-deterministic')
            if phase == '05b-appsec':
                required.add('ev/sca-status')
            if len(refs) != len(set(refs)) or not required <= set(refs):
                raise BounceError('incomplete_evidence')
            sources = {'ev/requirements': '01-po/requirements.md', 'ev/plan': '02-design/plan.json',
                       'ev/quality-contract': '00-recon/quality-contract.json', 'ev/diff': '03-dev/implementation.diff',
                       'ev/adr': '02-design/adr.md', 'ev/threat-model': '02-design/threat-model.md',
                       'ev/qa-deterministic': f'05-qa/deterministic-checks-a{attempt}.md',
                       'ev/sca-status': '05b-appsec/scanner-status.json'}
            for entry in metadata['evidence']:
                if entry['ref'] in sources and _safe_path(root, entry['source']) != root / sources[entry['ref']]:
                    raise BounceError('contradictory_evidence')
            context = build_trusted_context(head, plan, requirements, quality, refs)
            record = verify_recorded_evaluation(root=root, registry=registry, directory=directory,
                                                 expected=member, context=context)
            for name, ref in record['source_refs'].items():
                source_hashes[ref] = record['source_hashes'][name]
            for entry in metadata['evidence']:
                for key in ('source', 'captured'):
                    ref = str(_safe_path(root, entry[key]).relative_to(root))
                    source_hashes[ref] = entry['sha256']
            outcome_path = registered(directory / 'outcome.json')
            if (finished['outcome_class'] != record['outcome_class'] or
                    finished['outcome_ref'] != str(outcome_path.relative_to(root)) or
                    finished['outcome_sha256'] != _sha(outcome_path.read_bytes())):
                raise BounceError('contradictory_evidence')
            evaluations.append(record)
        checkpoint(head)
        return dict(status='verified', execution_authorized=False, evidence_state='verified',
                    evaluations=evaluations, plan=plan, quality=quality, requirements_text=requirements,
                    reviewed_commit=head, phase=phase, attempt=attempt, blockers=[],
                    source_hashes=source_hashes)
    except BounceError as exc:
        code = exc.code
    except ProtocolError as exc:
        code = 'contradictory_evidence' if exc.code == 'binding_gate' else 'report_invalid'
    except FindingsError:
        code = 'report_invalid'
    except (PlanError, TaskError, OSError):
        code = 'integrity_mismatch'
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        code = 'malformed_record'
    failure['blockers'] = [{'code': code}]
    return failure


INTEGRITY_LIMITS = ('Hashes are unkeyed integrity checks, not authentication. '
                    'No continuous containment is claimed. '
                    'This preview grants no approval or execution authority.')


def preview_snapshot(root, state):
    """Hash immutable ticket bytes and semantic state, excluding preview refs.

    The worktree is included: neither ignored source files nor raw projections
    may be changed by publication. Lock files are coordination, not evidence.
    """
    root = Path(root)
    frozen = copy.deepcopy(state)
    frozen.pop('updated_at', None)
    frozen['artifacts'] = {k: v for k, v in frozen.get('artifacts', {}).items()
                           if not k.startswith('bounce-preview/')}
    files = {}
    for path in root.rglob('*'):
        rel = path.relative_to(root)
        if rel.parts[0] == 'bounce-preview' or str(rel) in ('state.json', 'ticket.lock'):
            continue
        if path.is_symlink():
            import os
            files[str(rel)] = {'symlink': os.readlink(path)}
        elif path.is_file():
            files[str(rel)] = _sha(path.read_bytes())
    return _sha(canonical_json(dict(state=frozen, files=files)).encode())


def publish_preview(*, root, state, proposal, sources, verify, record_artifact):
    """Publish under the caller's ticket lock; verify before blessing any files.

    Deliberate BaseException fault injections propagate at every persistence
    boundary. Files left by an interruption are immutable recovery inputs.
    """
    from ssdlc_gate_reports import write_or_verify
    root = Path(root).absolute()
    payload = copy.deepcopy(proposal)
    payload.pop('revision', None)
    payload.update(source_hashes=sources, integrity_limits=INTEGRITY_LIMITS)
    revision = _sha(canonical_json(payload).encode())
    payload['revision'] = revision
    base = f"bounce-preview/{payload['phase']}/attempt-{payload['attempt']}/{revision}"
    data = canonical_json(payload).encode()
    summary = ('Bounce preview\nexecution_authorized=false\n'
               f"Revision: {revision}\nStatus: {payload['status']}\n"
               f"Repair roots: {', '.join(payload['repair_roots']) or '(none)'}\n"
               f"Revalidation tasks: {', '.join(payload['revalidation_tasks']) or '(none)'}\n"
               'Reasons: ' + ', '.join(sorted({b['code'] for b in payload['blockers']})) + '\n'
               + INTEGRITY_LIMITS + '\nSources (ticket-relative ref, SHA-256):\n'
               + ''.join(f'{ref} {digest}\n' for ref, digest in sorted(sources.items()))).encode()
    intended = [(base + '/plan.json', data), (base + '/summary.txt', summary)]
    paths = []
    existed = []
    registered = []
    # Inspect both paths before the first write, including registered-but-missing.
    for ref, content in intended:
        path = _safe_path(root, ref)
        meta = state['artifacts'].get(ref)
        present = path.exists()
        if meta and not present:
            raise BounceError('registered_artifact_missing')
        if present and (not path.is_file() or path.read_bytes() != content):
            raise BounceError('archive_conflict')
        if meta and (meta.get('sha256') != _sha(content) or meta.get('path') != ref):
            raise BounceError('archive_conflict')
        paths.append(path)
        existed.append(present)
        registered.append(bool(meta))
    verify()
    for (ref, content), path, present in zip(intended, paths, existed):
        if not present:
            write_or_verify(path, content, root=root)
    # A failed post-publication source check leaves files unregistered.
    verify()
    for (ref, content), path in zip(intended, paths):
        if _safe_path(root, ref).read_bytes() != content:
            raise BounceError('archive_conflict')
    for (ref, _), path, known in zip(intended, paths, registered):
        if not known:
            record_artifact(ref, path)
    verify()
    for (ref, content), path in zip(intended, paths):
        if _safe_path(root, ref).read_bytes() != content:
            raise BounceError('archive_conflict')
    return dict(status=('ready-for-later-executor' if payload['status'] == 'candidates'
                        else 'diagnostic'), execution_authorized=False,
                revision=revision, plan=payload, artifact_refs=[r for r, _ in intended],
                publication=('reused' if all(registered) else
                             'recovered' if any(existed) else 'created'))
