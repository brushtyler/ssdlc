"""Revalidate explicit, source-bound saved review objects; never dispatch or infer."""
import json
from pathlib import Path
from ssdlc_tasks import file_hash
from ssdlc_gate_reports import extract_report,bind_report,decide
from ssdlc_findings import assess_report

def verify_recorded_reviews(bundle,proposal,context,transport,parser):
    bundle=Path(bundle);outcomes=[];reports={}
    expected=dict(commit=context.expected_commit,task_ids=sorted(context.known_task_ids),
                  obligations=sorted(context.obligation_ids),evidence=sorted(context.evidence_refs))
    schema0=json.loads((Path(proposal['controller']['root'])/'schemas/gate-findings.schema.json').read_text())
    for row in proposal['recorded_reviews']:
        directory=bundle/row['directory'];outcome=json.loads((directory/'outcome.json').read_text())
        for ref,sha in outcome['manifest'].items():
            path=(directory/ref).resolve()
            if not path.is_relative_to(directory.resolve()) or file_hash(path)!=sha:
                raise RuntimeError('Saved review manifest mismatch')
        if outcome['engine']!=row['engine'] or outcome['gate']!='review' or outcome['reviewed_commit']!=context.expected_commit:
            raise RuntimeError('Saved review source/provider differs')
        inputs=json.loads((directory/'input.json').read_text())
        if inputs['decision_inputs']['context']!=expected or inputs['decision_inputs']['fingerprint']!=proposal['recorded_review_proposal_sha256']:
            raise RuntimeError('Saved review trusted context differs')
        usage=proposal['accounting_floor']['invocations'][row['invocation']]
        if usage['engine']!=row['engine'] or usage['phase']!='04-review' or usage['status']!='returned':
            raise RuntimeError('Saved review has no matching consumed invocation')
        raw=json.loads((bundle/row['raw']).read_text());typed=raw['structured_output'] if row['engine']=='claude' else raw
        if row['engine']=='claude':
            if raw.get('is_error') is True or raw['total_cost_usd']!=usage['usage']['cli_estimate_usd']:
                raise RuntimeError('Saved Claude result/observed cost changed')
        schema=transport.response_schema(schema0,'review',context.expected_commit,
            known_task_ids=context.known_task_ids,obligations=context.obligation_ids,
            evidence_refs=context.evidence_refs,source_files=proposal['allowed_source_files'])
        nonce=outcome['identity'][:16];text=transport.rendered(typed,schema,nonce)
        report=extract_report(text,nonce)
        if report!=typed['report']:raise RuntimeError('Recorded explicit findings changed')
        bind_report(report,'review',context)
        approved,reasons=decide(parser(text),assess_report(report,context),inputs['decision_inputs']['controls'],True)
        if not approved:raise RuntimeError('Recorded assessment did not approve: '+str(reasons))
        if row['engine']=='codex':
            if outcome['approved'] is not True or outcome['outcome_class']!='approved' or text!=(directory/'response-1.md').read_text():
                raise RuntimeError('Approved native Codex output changed')
            mode='preserved-native-approval'
        else:
            old=(directory/'response-1.md').read_text()
            diagnostics=json.loads((directory/'diagnostics-1.json').read_text())
            if outcome['approved'] is not False or not any(d.get('code')=='verdict_invalid' for d in diagnostics):
                raise RuntimeError('Unexpected original Claude failure')
            if extract_report(old,nonce)!=typed['report'] or parser(old) is not None:
                raise RuntimeError('Original Claude framing does not match declared format failure')
            if typed['human_report'].rstrip().splitlines()[-1].strip()!=typed['verdict'] or old.count('REVIEW_STATUS:')!=2:
                raise RuntimeError('Repair is limited to an identical trailing verdict duplicate')
            mode='host-protocol-repair-of-explicit-saved-verdict'
        reports[row['engine']]=text
        outcomes.append(dict(gate='review',engine=row['engine'],model=outcome['model'],approved=True,
            outcome_class='approved',mode=mode,original_native_approved=outcome['approved'],
            original_outcome_sha256=file_hash(directory/'outcome.json'),raw_sha256=file_hash(bundle/row['raw']),
            native_invocation=row['invocation'],new_provider_calls=0,reviewed_commit=context.expected_commit,
            explicit_report_unchanged=True,explicit_verdict=typed['verdict']))
    if sorted(r['engine'] for r in outcomes)!=['claude','codex']:raise RuntimeError('Exactly two independent saved reviews required')
    return outcomes,reports
