"""Literal cumulative accounting for I1 continuations. No dispatch or source writes."""
import copy
import datetime as dt
import math

from ssdlc_tasks import DevelopmentBlocked, digest, validate_document
from ssdlc_transitions import TransitionError


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def reservation_catalogue(facts, opaque_floors):
    """Known reservations require exact one-to-one coverage, not inferred refunds."""
    origins, unknown = {}, []
    def add(ref, role, phase):
        if ref in origins: raise TransitionError('Duplicate historical reservation')
        origins[ref] = {'role': role, 'phase': phase}
    for tid, task in facts['tasks'].items():
        if type(task.get('attempts')) is not int or task['attempts'] < 0:
            raise TransitionError('Invalid charged task attempts')
        for index in range(1, task['attempts'] + 1):
            add(f'task:{tid}:{index}', '03-dev', '03-dev')
    for ref, session in facts['counters'].items():
        if isinstance(session.get('rounds'), list):
            for row in session['rounds']:
                number = row['round']
                add(f'{ref}:arch:{number}', '02-design-arch', '02-design')
                reviews = row.get('review_reserved', int('review' in row or 'legacy_review' in row))
                if type(reviews) is not int or not 0 <= reviews <= 2:
                    raise TransitionError('Ambiguous design review reservations')
                for index in range(1, reviews + 1):
                    add(f'{ref}:review:{number}:{index}', '02-design-sec', '02-design')
                if row.get('diagnosis_reserved'):
                    add(f'{ref}:diagnosis:{number}', '02-design-diagnosis', '02-design')
        elif isinstance(session.get('attempts'), list):
            for row in session['attempts']:
                number = row['attempt']
                if row.get('origin') != 'previous_archived_candidate':
                    add(f'{ref}:planner:{number}', '02-design-task-plan', '02-design')
                responses = [i for i in (1, 2) if f'review-response-{i}' in row]
                observed = max(responses, default=int('review' in row))
                if 'review_invocations' in row:
                    count = row['review_invocations']
                    if type(count) is not int or not observed <= count <= 2:
                        raise TransitionError('Invalid historical planning review reservations')
                elif row.get('status') == 'REVIEWING' or row.get('failure_kind') == 'REVIEW_UNCERTAIN':
                    # Legacy sessions without a durable reservation counter can
                    # have lost the second response. Charge the protocol ceiling
                    # and expose the conservative allocation for authorization.
                    count = 2
                    unknown.append(ref + ':review-reservation:' + str(number))
                else:
                    count = observed
                for index in range(1, count + 1):
                    add(f'{ref}:review:{number}:{index}', '02-design-task-plan-review', '02-design')
        else:
            floor = opaque_floors.get(ref)
            if (not isinstance(floor, dict) or set(floor) != {'count', 'role', 'phase', 'reason'}
                    or type(floor['count']) is not int or floor['count'] < 0 or not floor['reason']
                    or finite(session.get('used')) and floor['count'] < session['used']):
                raise TransitionError('Opaque historical counter needs an explicit conservative allocation')
            unknown.append(ref)
            for index in range(floor['count']):
                add(f'{ref}:opaque:{index}', floor['role'], floor['phase'])
    if set(opaque_floors) - set(facts['counters']):
        raise TransitionError('Unknown historical counter allocation')
    return origins, unknown


def _usage(row, mapping, decisions):
    usage = {'metered_usd': None, 'cli_estimate_usd': None}
    amount = row.get('cost_usd')
    if amount is not None and not finite(amount): raise TransitionError('Invalid historical cost observation')
    if amount is not None and amount > 0:
        kind = {'claude': 'cli_estimate_usd', 'openrouter': 'metered_usd'}.get(row.get('engine'))
        if kind is None:
            kind = mapping.get('usage_kind')
            if kind not in usage or not mapping.get('reason'):
                raise TransitionError('Unclassified positive historical cost needs explicit attribution')
            decisions['attributions'].append({'row_sha256': digest(row), 'kind': kind, 'reason': mapping['reason']})
        usage[kind] = amount
    return usage


def project_accounting(facts, request, *, state, now):
    """Return a cumulative native LimitsStore state plus explicit operator decisions."""
    profile = copy.deepcopy(request['profile'])
    validate_document('recovery-limits', profile)
    if any(not finite(profile[key]) for key in ('metered_usd', 'cli_estimate_usd')):
        raise TransitionError('Nonfinite historical profile')
    if not isinstance(facts.get('ledger'), list) or not isinstance(facts.get('counters'), dict):
        raise TransitionError('Missing historical accounting cannot become an empty run')
    if profile['transport_retries'] != 0:
        raise TransitionError('Unknown legacy transport consumption requires retries disabled, not reset')
    origins, opaque = reservation_catalogue(facts, request.get('opaque_floors', {}))
    decisions = {'unknown_limits': [], 'extensions': [], 'attributions': [], 'opaque_counters': opaque, 'unknown_usage': []}
    if 'historical_unknown_usage' in profile:
        raise TransitionError('Historical unknown usage summary is derived, not supplied')
    reserve = request.get('unknown_usage_reserve')
    if reserve is not None and (not isinstance(reserve, dict) or set(reserve) != {'metered_usd', 'cli_estimate_usd', 'reason'}
            or not isinstance(reserve['reason'], str) or not reserve['reason'].strip()
            or any(not finite(reserve[k]) or reserve[k] <= 0 for k in ('metered_usd', 'cli_estimate_usd'))):
        raise TransitionError('Unknown usage needs positive conservative reserves for both dollar caps and a reason')
    if profile.get('unknown_usage_reserve_model') == 'exclusive_cost_kind_v1':
        decisions['unknown_limits'].append('unknown_usage_reserve_model:exclusive_cost_kind_v1')
    old = facts.get('limits') or {}
    aliases = {'development_attempts': ('development_attempts', 'max_attempts', 'SSDLC_MAX_DEV_RETRIES'),
               'design_rounds': ('design_rounds', 'SSDLC_MAX_DESIGN_ROUNDS'),
               'invocations': ('invocations',), 'wall_seconds': ('wall_seconds',),
               'metered_usd': ('metered_usd',), 'cli_estimate_usd': ('cli_estimate_usd',),
               'invocation_timeout_seconds': ('invocation_timeout_seconds', 'SSDLC_ENGINE_TIMEOUT_SEC'),
               'design_diagnoses': ('design_diagnoses',), 'transport_retries': ('transport_retries',)}
    for key, names in aliases.items():
        values = [old[name] for name in names if name in old]
        if key == 'design_rounds':
            values.extend(session[name] for session in facts['counters'].values()
                          if isinstance(session.get('rounds'), list)
                          for name in ('round_limit', 'max_rounds') if name in session)
        if not values:
            decisions['unknown_limits'].append(key)
        elif not all(finite(value) for value in values) or len(set(values)) != 1:
            raise TransitionError('Conflicting historical limit evidence')
        elif profile[key] > values[0]:
            decisions['extensions'].append({'limit': key, 'old': values[0], 'new': profile[key]})
    for group in ('role_invocations', 'phase_invocations'):
        previous = old.get(group, {})
        if not isinstance(previous, dict): raise TransitionError('Invalid historical role/phase caps')
        for name, cap in profile[group].items():
            if name not in previous:
                decisions['unknown_limits'].append(group + ':' + name)
            elif not finite(previous[name]):
                raise TransitionError('Invalid historical role/phase cap')
            elif cap > previous[name]:
                decisions['extensions'].append({'limit': group + ':' + name, 'old': previous[name], 'new': cap})
    combined = request.get('combined_reported_usd_cap')
    if combined is not None and (not finite(combined) or combined <= 0):
        raise TransitionError('Invalid combined reported-cost cap')
    if 'budget' in old:
        if not finite(old['budget']): raise TransitionError('Invalid legacy budget')
        if combined is None: raise TransitionError('Historical aggregate budget cannot be silently removed')
        if combined > old['budget']:
            decisions['extensions'].append({'limit': 'combined_reported_usd_cap', 'old': old['budget'], 'new': combined})
    elif combined is not None:
        decisions['unknown_limits'].append('combined_reported_usd_cap')
    timing = request['time']
    started = timing['started_at']
    if not finite(started) or started > now: raise TransitionError('Invalid historical start time')
    created = state.get('created_at')
    try:
        known_start = dt.datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp() if isinstance(created, str) else None
    except (TypeError, ValueError):
        known_start = None
    if known_start is None:
        if timing.get('basis') != 'operator_attestation' or not timing.get('reason'):
            raise TransitionError('Unknown historical time needs explicit attestation')
        decisions['unknown_limits'].append('historical_started_at')
    elif started != known_start or timing.get('basis') != 'source_created_at':
        raise TransitionError('Historical elapsed time cannot reset on resume')
    invocations, covered = {}, set()
    rows = request['ledger_mapping']
    if len(rows) != len(facts['ledger']) or {r['index'] for r in rows} != set(range(len(rows))):
        raise TransitionError('Every historical ledger row must be mapped once')
    def insert(identity, descriptor, row, origin_refs):
        if (descriptor['role'] not in profile['role_invocations'] or descriptor['phase'] not in profile['phase_invocations']
                or len(origin_refs) > 1 or set(origin_refs) & covered):
            raise TransitionError('Duplicate or out-of-scope historical allocation')
        for origin in origin_refs:
            if origins.get(origin) != {'role': descriptor['role'], 'phase': descriptor['phase']}:
                raise TransitionError('Historical origin assigned to an incompatible role/phase')
        covered.update(origin_refs)
        invocations[identity] = {'role': descriptor['role'], 'phase': descriptor['phase'], 'engine': row.get('engine'),
            'status': 'historical', 'historical_outcome': 'fenced_unconfirmed',
            'source_row_sha256': digest(row), 'origins': origin_refs, 'usage': _usage(row, descriptor, decisions)}
        if all(value is None for value in invocations[identity]['usage'].values()):
            if reserve is None:
                raise TransitionError('Unknown historical usage requires an explicit conservative reserve')
            allocation = {k: reserve[k] for k in ('metered_usd', 'cli_estimate_usd')}
            invocations[identity]['conservative_usage'] = allocation
            decisions['unknown_usage'].append({'invocation': identity, 'source_row_sha256': digest(row),
                'engine': row.get('engine'), 'reserve': allocation, 'reason': reserve['reason']})
    for row in rows:
        source = facts['ledger'][row['index']]
        if row['sha256'] != digest(source): raise TransitionError('Historical ledger row changed')
        insert('ledger-' + str(row['index']), row, source, row.get('origins', []))
    for row in request.get('supplemental_invocations', []):
        origin = row['origin']
        if origin not in origins or origin in covered:
            raise TransitionError('Supplement duplicates or invents a charged origin')
        if not row.get('reason'): raise TransitionError('Supplement needs explicit attribution')
        insert('reservation-' + digest(origin), row, {'engine': row.get('engine'), 'cost_usd': None}, [origin])
    if covered != set(origins): raise TransitionError('Consumed historical reservations remain unmapped')
    if decisions['unknown_usage']:
        profile['historical_unknown_usage'] = {
            'invocations': len(decisions['unknown_usage']),
            **{kind: sum(row['reserve'][kind] for row in decisions['unknown_usage'])
               for kind in ('metered_usd', 'cli_estimate_usd')}}
        if profile.get('unknown_usage_reserve_model') == 'exclusive_cost_kind_v1':
            profile['historical_unknown_usage']['combined_usd'] = sum(
                max(row['reserve'].values()) for row in decisions['unknown_usage'])
        if any(not finite(value) for value in profile['historical_unknown_usage'].values()):
            raise TransitionError('Nonfinite cumulative unknown usage reserve')
        validate_document('recovery-limits', profile)
    initial = {'policy_sha256': '', 'started_at': started, 'deadline_at': started + profile['wall_seconds'],
               'invocations': invocations, 'transports': [], 'diagnoses': [],
               'historical': copy.deepcopy(facts), 'historical_transport_total': None,
               'combined_reported_usd_cap': combined}
    diagnoses = [identity for identity, row in invocations.items() if row['role'] == '02-design-diagnosis']
    initial['diagnoses'] = [{'subject': identity, 'status': 'historical'} for identity in diagnoses]
    return profile, initial, decisions, origins


def validate_decisions(authorization, proposal):
    if any(type(authorization.get(key)) is not bool for key in ('accept_unknown_history', 'authorize_extensions')):
        raise TransitionError('Reconciliation decisions require explicit booleans')
    decisions = proposal['decisions']
    if authorization.get('decisions_sha256') != digest(decisions):
        raise TransitionError('Authorization does not bind the reconciliation decisions')
    if (decisions['unknown_limits'] or decisions['opaque_counters'] or decisions['attributions'] or decisions.get('unknown_usage')) and authorization.get('accept_unknown_history') is not True:
        raise TransitionError('Unknown/ambiguous history requires explicit operator acceptance')
    if decisions['extensions'] and authorization.get('authorize_extensions') is not True:
        raise TransitionError('Historical limit extensions require explicit authorization')


def limit_sources(facts, sources):
    """Parse only explicitly named numeric fields; env files are data, not shell."""
    import json
    from pathlib import Path
    from ssdlc_tasks import file_hash
    from ssdlc_transitions import _regular
    effective = copy.deepcopy(facts)
    effective['limits'] = copy.deepcopy(facts.get('limits') or {})
    bound, payloads = [], {}
    names = set()
    allowed = {'development_attempts', 'design_rounds', 'invocations', 'wall_seconds',
               'metered_usd', 'cli_estimate_usd', 'budget'}
    for source in sources:
        name = source['name']
        if name in names or not name.isidentifier(): raise TransitionError('Invalid limit source identity')
        names.add(name)
        path = Path(source['path']).absolute()
        data = _regular(path.parent, path.name).read_bytes()
        if file_hash(path) != source['sha256']: raise TransitionError('Limit source changed')
        if source['format'] == 'json':
            document = json.loads(data)
            def value(key):
                result = document
                for component in key.removeprefix('/').split('/'):
                    result = result[component.replace('~1', '/').replace('~0', '~')]
                return result
        elif source['format'] == 'env':
            document = {}
            for line in data.decode().splitlines():
                line = line.strip().removeprefix('export ')
                if not line or line.startswith('#') or '=' not in line: continue
                key, raw = line.split('=', 1)
                document[key.strip()] = raw.strip().strip('"\'')
            value = lambda key: document[key]
        else:
            raise TransitionError('Unsupported limit source format')
        for key, reference in source['fields'].items():
            if key not in allowed: raise TransitionError('Unknown historical cap field')
            try:
                raw = value(reference)
                if isinstance(raw, bool): raise ValueError('Boolean is not a cap')
                number = float(raw)
            except (ValueError, TypeError, KeyError): raise TransitionError('Limit source is not an explicit numeric value') from None
            if not finite(number): raise TransitionError('Invalid source limit')
            if key in effective['limits'] and effective['limits'][key] != number:
                raise TransitionError('Conflicting historical limit sources')
            effective['limits'][key] = number
        bound.append(copy.deepcopy(source)); payloads['history/limit-sources/' + name] = data
    return effective, bound, payloads
