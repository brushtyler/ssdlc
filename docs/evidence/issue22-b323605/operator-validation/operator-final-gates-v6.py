"""Operator-owned final integration validation, using the native gate evaluator.

This is not a migration of historical task receipts. Its independent budget is
an allocation of the preserved parent's remaining accounting room. It cannot
write candidate code, dispatch developers, or change the parent's authority.
"""
import argparse
from pathlib import Path
import json
import os
import re
import sys
import tempfile
import time
import math
import importlib.util
import subprocess

os.umask(0o077)
parser=argparse.ArgumentParser()
parser.add_argument('--controller',required=True)
parser.add_argument('--bundle')
parser.add_argument('--authorization')
parser.add_argument('--self-test',action='store_true')
parser.add_argument('--verify-only',action='store_true')
args=parser.parse_args()
sys.path.insert(0,str(Path(args.controller).resolve()))
import orchestrator as native
from ssdlc_controller import assert_controller,controller_snapshot
from ssdlc_gate_reports import (GateEvaluator,EvaluationRequest,build_trusted_context,
    UNTRUSTED_DATA_INSTRUCTION,untrusted_data,canon)
from ssdlc_recovery import LimitsStore,RecoveryStop
from ssdlc_findings import TrustedContext
from ssdlc_tasks import atomic_json,digest,file_hash,git,ticket_lock

PHASE={'review':'04-review','qa':'05-qa','appsec':'05b-appsec'}
PARSER={'review':native.parse_review_verdict,'qa':native.parse_qa_verdict,'appsec':native.parse_appsec_verdict}
VERDICT={'review':'REVIEW_STATUS: APPROVED','qa':'QA_STATUS: PASSED','appsec':'APPSEC_STATUS: SECURE'}

def stamp(message):
    print(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())+' '+message,flush=True)


def read(path):return json.loads(Path(path).read_text())


class ValidationJob:
    def __init__(self,root,proposal,*,guard,simulation=False):
        self.root=Path(root);self.proposal=proposal;self.guard=guard;self.simulation=simulation
        self.store=LimitsStore(proposal.get('accounting_root',root))
        path=self.root/'validation-state.json'
        self.state=read(path) if path.exists() else dict(artifacts={},ledger=[],gates=[],native_calls=[])
        self.root.mkdir(parents=True,exist_ok=True)
        self.last_invocation=None

    def save(self):atomic_json(self.root/'validation-state.json',self.state)

    def register(self,ref,path):
        meta=dict(sha256=file_hash(Path(path)))
        if ref in self.state['artifacts'] and self.state['artifacts'][ref]!=meta:
            raise RuntimeError('Registered final validation evidence changed')
        self.state['artifacts'][ref]=meta;self.save()

    def record_ledger(self,details,telemetry):
        # Keep native observed usage authoritative. Native CLI adapters return
        # numeric placeholders for unreported costs; these are not observed zero.
        counters=self.store._load(repair=False)[1]
        row=counters['invocations'].get(self.last_invocation,{})
        self.state['ledger'].append(dict(details=details,telemetry=telemetry,
            native_invocation=self.last_invocation,observed_usage=row.get('usage')))
        self.save()

    def record_gate(self,name,passed,details):
        self.state['gates'].append(dict(gate=name,passed=passed,details=details));self.save()

    def budget(self):
        self.guard()
        envelope,counters=self.store._load(repair=False)
        self.store.check(counters,envelope['policy'])
        known=sum(value for row in counters['invocations'].values() for value in row.get('usage',{}).values() if value is not None)
        if known>=self.proposal['allocation_usd']:
            raise RecoveryStop('LIMIT_REACHED','Final validation allocation exhausted')
        return self.proposal['allocation_usd']-known

    def invoke(self,request,system,user):
        if any(row['gate']==request.gate and row['engine']==request.engine for row in self.state['native_calls']):
            raise RecoveryStop('LIMIT_REACHED','One call per gate/provider; no automatic protocol redispatch')
        remaining=self.budget();before=self.store._load(repair=False)[1]['invocations']
        phase=PHASE[request.gate]
        stamp(f"{phase} {request.engine}:{request.model}; native calls={len(before)}; source={request.reviewed_commit[:12]}")
        old=getattr(native._dispatch_local,'limits',None)
        native._dispatch_local.limits=self.store
        try:
            with native._dispatch_context(phase=phase):
                if self.simulation:
                    nonce=re.search(r'-----BEGIN SSDLC GATE REPORT ([a-f0-9]+)-----',user).group(1)
                    report=dict(schema_version='1.0',source_gate={'review':'code_review','qa':'qa','appsec':'appsec'}[request.gate],
                                reviewed_commit=request.reviewed_commit,findings=[])
                    text=f'-----BEGIN SSDLC GATE REPORT {nonce}-----\n'+json.dumps(report)+f'\n-----END SSDLC GATE REPORT {nonce}-----\nSIMULATION ONLY\n'+VERDICT[request.gate]
                    if request.engine=='claude':
                        def run_claude():return text,0,0.25,1,1
                        result=native._supervised_dispatch(run_claude)()
                        return result[0],result[2],result[3],result[4],'offline'
                    def run_codex():return text,0
                    result=native._supervised_dispatch(run_codex)()
                    return result[0],0,0,0,'offline'
                if not native.EngineRunner._should_spawn_role_container():
                    raise RuntimeError('Final roles require Docker; local fallback forbidden')
                prompt=system+'\n\n---\n\n'+user
                cwd=Path(self.proposal['source_root'])
                if request.engine=='claude':
                    call_cap=math.floor(min(0.55,remaining)*100)/100
                    if call_cap<0.01:
                        raise RecoveryStop('LIMIT_REACHED','Less than one cent remains in the reported-cost allocation')
                    spec=importlib.util.spec_from_file_location('final_structured_transport',Path(__file__).with_name('final-structured-transport-v6.py'))
                    transport=importlib.util.module_from_spec(spec);spec.loader.exec_module(transport)
                    schema=read(Path(args.controller)/'schemas/gate-findings.schema.json')
                    restore=transport.install(native,request,user,self.root,schema,source_files=self.proposal['allowed_source_files'],schema_file=Path(self.proposal['role_input_root'])/'schemas'/('gate-'+request.gate+'.json'))
                    try:
                        prompt='Return the schema-enforced object with report, human_report and verdict. The host renders framing; preserve all findings explicitly.\n'+prompt
                        text,rc,cost,tin,tout=native.EngineRunner.run_claude(prompt,cwd=cwd,read_only=True,
                            model=request.model,reasoning_effort=request.reasoning_effort,
                            max_budget_usd=call_cap,require_container=True)
                    finally:restore()
                elif request.engine=='codex':
                    spec=importlib.util.spec_from_file_location('final_structured_transport',Path(__file__).with_name('final-structured-transport-v6.py'))
                    transport=importlib.util.module_from_spec(spec);spec.loader.exec_module(transport)
                    schema=read(Path(args.controller)/'schemas/gate-findings.schema.json')
                    restore=transport.install(native,request,user,self.root,schema,source_files=self.proposal['allowed_source_files'],schema_file=Path(self.proposal['role_input_root'])/'schemas'/('gate-'+request.gate+'.json'))
                    try:
                        prompt='Return only the schema-enforced report, human_report and verdict JSON object. File references are plain repository file paths; put line numbers in human_report or scenario. The host renders protocol markers.\n'+prompt
                        text,rc=native.EngineRunner.run_codex(prompt,cwd=cwd,sandbox='read-only',
                            model=request.model,reasoning_effort=request.reasoning_effort,require_container=True)
                    finally:restore()
                    cost,tin,tout=0,0,0
                else:raise RuntimeError('Provider route outside final validation authorization')
                if rc:raise RuntimeError(f'{request.engine} exited with code {rc}; retained native usage/outcome must be inspected')
                return text,cost,tin,tout,'issue22-final-validation'
        finally:
            native._dispatch_local.limits=old
            after=self.store._load(repair=False)[1]['invocations']
            added=set(after)-set(before)
            if len(added)==1:
                self.last_invocation=next(iter(added))
                self.state['native_calls'].append(dict(invocation=self.last_invocation,gate=request.gate,
                    engine=request.engine,model=request.model,source=request.reviewed_commit))
                self.save()
            stamp(f'{phase} invocation returned; native calls={len(after)}')

    def evaluate(self,request):
        self.guard()
        evaluator=GateEvaluator(gate=request.gate,base_dir=self.root/PHASE[request.gate],ssdlc_root=self.root,
            invoke=self.invoke,check_budget=self.budget,record_artifact=self.register,
            record_ledger=self.record_ledger,record_gate=self.record_gate,verify_source=self.guard,
            artifacts=self.state['artifacts'],ledger=self.state['ledger'],gates=self.state['gates'])
        return evaluator.evaluate(request)


def self_test():
    with tempfile.TemporaryDirectory(prefix='ssdlc-final-gates-selftest-') as temporary:
        root=Path(temporary);source='a'*40
        proposal=dict(allocation_usd=1,source_root=str(root))
        job=ValidationJob(root,proposal,guard=lambda:None,simulation=True)
        policy=dict(schema_version=1,profile_id='final-validation-simulation',invocations=8,wall_seconds=300,
            role_invocations={p:4 for p in PHASE.values()},phase_invocations={p:4 for p in PHASE.values()},
            transport_retries=0,design_diagnoses=0,metered_usd=1,cli_estimate_usd=1,
            development_attempts=1,design_rounds=1,invocation_timeout_seconds=30)
        job.store.authorize(policy,dict(kind='operator-final-validation-selftest'))
        p=root/'evidence.txt';p.write_text('SIMULATION ONLY')
        evidence=[dict(ref='ev/test',source=str(p),registered_hash=file_hash(p))]
        context=build_trusted_context(source,dict(tasks=[dict(task_id='T1')]),'AC-1: Contract\nSEC-1: Security\n',{},['ev/test'])
        for gate,engine,ordinal in [('review','claude',1),('review','codex',2),('qa','claude',1),('appsec','claude',1)]:
            request=EvaluationRequest(gate,engine,'offline',ordinal,1,source,UNTRUSTED_DATA_INSTRUCTION,
                'SIMULATION ONLY',context,evidence,'b'*64,PARSER[gate],controls={'synthetic':True})
            assert job.evaluate(request).approved
        counters=job.store._load(repair=False)[1];assert len(counters['invocations'])==4
        assert sum(v for row in counters['invocations'].values() for v in row['usage'].values() if v is not None)==0.75
        assert sum(all(v is None for v in row['usage'].values()) for row in counters['invocations'].values())==1
        before=len(counters['invocations'])
        assert job.evaluate(request).approved
        assert len(job.store._load(repair=False)[1]['invocations'])==before
        print(json.dumps(dict(result='PASS',fake_native_invocations=4,provider_calls=0,
                             observed_cli_estimate=0.75,unknown_calls=1,replay_dispatches=0)))


def execute():
    if not args.bundle or (not args.authorization and not args.verify_only):
        raise SystemExit('Exact bundle and explicit operator authorization required; providers disabled')
    bundle=Path(args.bundle).resolve();proposal=read(bundle/'proposal.json')
    authority=None if args.verify_only else read(args.authorization);subject=digest(proposal)
    if not args.verify_only and authority != dict(proposal_sha256=subject,source_fenced=True,
                         approved=True,kind='operator-final-integration-validation'):
        raise SystemExit('Authorization does not match this exact proposal; providers disabled')
    if Path(args.controller).resolve()!=Path(proposal['controller']['root']):
        raise SystemExit('Executing controller path differs from proposal')
    if file_hash(Path(__file__))!=proposal['script_sha256']:
        raise SystemExit('Operator script changed after proposal')
    source=Path(proposal['source_root']);canonical=Path(proposal['canonical'])
    parent=LimitsStore(Path(proposal['parent_runtime']))
    expected_accounting=Path(proposal['continuation_accounting_root'])
    if Path(proposal['accounting_root']).resolve()!=expected_accounting.resolve():
        raise RuntimeError('Revision accounting root differs from staged cumulative projection')
    old_accounting=LimitsStore(Path(proposal['predecessor_accounting_root']))
    accounting=LimitsStore(Path(proposal['accounting_root']))
    execution=bundle/'execution'
    def guard():
        assert_controller(Path(args.controller),proposal['controller'])
        assert_controller(source,proposal['source_snapshot'])
        if file_hash(canonical/'execution/active.json')!=proposal['parent_selector_sha256']:
            raise RuntimeError('Historical parent selector changed')
        envelope,counters=parent._load(repair=False)
        if digest(envelope)!=proposal['parent_envelope_sha256'] or digest(counters)!=proposal['parent_counters_sha256']:
            raise RuntimeError('Historical parent limits or counters changed')
        if len(counters['invocations'])+proposal['max_calls']>envelope['policy']['invocations']:
            raise RuntimeError('Cumulative parent invocation ceiling exceeded')
        if time.time()>=proposal['validation_deadline_at']:
            raise RuntimeError('Explicit external-validation deadline reached')
        old_envelope,old_state=old_accounting._load(repair=False)
        if digest(old_envelope)!=proposal['predecessor_envelope_sha256'] or digest(old_state)!=proposal['predecessor_counters_sha256']:
            raise RuntimeError('Sealed predecessor accounting changed')
        if file_hash(old_accounting.journal.path)!=proposal['predecessor_journal_sha256']:
            raise RuntimeError('Sealed predecessor journal changed')
        for name,sha in proposal['role_input_files'].items():
            if file_hash(Path(proposal['role_input_root'])/name)!=sha:
                raise RuntimeError('Read-only role input changed')
        if file_hash(Path(__file__).with_name('final-structured-transport-v6.py'))!=proposal['structured_transport_sha256']:
            raise RuntimeError('Structured transport changed')
        if file_hash(Path(__file__).with_name('final-recorded-reviews-v6.py'))!=proposal['recorded_reviews_helper_sha256']:
            raise RuntimeError('Recorded review verifier changed')
        for ref,sha in proposal['preserved_v5_files'].items():
            if file_hash(Path(proposal['recorded_review_bundle'])/ref)!=sha:
                raise RuntimeError('Original V5 artifacts changed')
        environment=proposal['runner_environment']
        if environment.get('role_repository_root') and native.EngineRunner._find_repo_root(source)!=Path(environment['role_repository_root']):
            raise RuntimeError('Role repository root changed')
        if native.EngineRunner.IMAGE_NAME!=environment['image_name']:
            raise RuntimeError('Native runner image route changed')
        image_id=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',environment['image_name']],text=True).strip()
        if image_id!=environment['image_id']:
            raise RuntimeError('Native runner image changed')
        for name,expected in environment['resource_options'].items():
            default={'SSDLC_CONTAINER_PIDS_LIMIT':'1024','SSDLC_CONTAINER_MEMORY':'4g','SSDLC_CONTAINER_CPUS':'2'}[name]
            if os.environ.get(name,default)!=expected:
                raise RuntimeError('Native runner resource limits changed')
        if file_hash(Path(__file__))!=proposal['script_sha256'] or digest(read(bundle/'proposal.json'))!=subject:
            raise RuntimeError('Operator validation authority changed')
        accounting_envelope,accounting_state=accounting._load(repair=False)
        if digest(accounting_envelope)!=proposal['accounting_envelope_sha256']:
            raise RuntimeError('Original validation accounting policy changed')
        observed=sum(value for row in accounting_state['invocations'].values()
                     for value in row.get('usage',{}).values() if value is not None)
        if observed>proposal['allocation_usd']:
            raise RecoveryStop('LIMIT_REACHED','Observed usage exceeded the cumulative validation allocation')
        floor=proposal['accounting_floor']
        if any(accounting_state.get(key)!=value for key,value in floor.items() if key!='invocations'):
            raise RuntimeError('Original accounting deadline or recovery metadata changed')
        if any(accounting_state['invocations'].get(key)!=value for key,value in floor['invocations'].items()):
            raise RuntimeError('A previous validation invocation was changed or removed')
        state_path=execution/'validation-state.json'
        state=read(state_path) if state_path.exists() else dict(native_calls=[])
        frame_calls={row['invocation'] for row in state['native_calls']
                     if row['source']==proposal['source_head']}
        if set(accounting_state['invocations'])!=set(floor['invocations'])|frame_calls:
            raise RuntimeError('Validation accounting has calls outside this authorized frame')
        for ref,expected in proposal['files'].items():
            path=(bundle/ref).resolve()
            if not path.is_relative_to(bundle) or file_hash(path)!=expected:
                raise RuntimeError('Frozen validation input changed: '+ref)
        # Confirm the scanner dispositions still bind exactly to this source.
        for finding in read(bundle/'evidence/gitleaks-disposition.json')['findings']:
            path=(source/finding['file']).resolve()
            if not path.is_relative_to(source) or file_hash(path)!=finding['file_sha256']:
                raise RuntimeError('Scanner disposition is no longer source-bound')
    with ticket_lock(canonical/'ticket.lock'):
        guard()
        # One allocation only: another bundle cannot double-spend the same
        # untouched parent room. This record is outside historical run state.
        allocation=Path(__file__).parent/'final-validation-allocation.json'
        if not allocation.exists() or read(allocation)!=proposal['original_allocation_binding']:
            raise RuntimeError('Original allocation is missing or changed; cannot allocate again')
        root=execution;job=ValidationJob(root,proposal,guard=guard)
        if not job.store.policy_path.exists():
            raise RuntimeError('Original native accounting is missing; never reset it')
        policy,counters=job.store._load(repair=False)
        if (policy['binding']['proposal_sha256']!=proposal['accounting_original_proposal_sha256']
                or counters['deadline_at']!=proposal['validation_deadline_at']
                or policy['policy']['invocations']!=proposal['max_calls']
                or policy['policy']['cli_estimate_usd']!=proposal['allocation_usd']):
            raise RuntimeError('Validation limits are not bound to original cumulative authority')
        evidence=[]
        for ref in proposal['files']:
            if ref.startswith('evidence/'):
                evidence.append(dict(ref='ev/'+Path(ref).name,source=str(bundle/ref),registered_hash=proposal['files'][ref]))
        context=build_trusted_context(proposal['source_head'],read(bundle/'scope/plan.json'),
            (bundle/'scope/requirements.md').read_text(),read(bundle/'scope/quality-contract.json'),[e['ref'] for e in evidence])
        # The ordinary catalogue covers functional/security IDs. This final
        # operator assessment also validates every original NFR, without
        # changing a historical plan, quality contract or task receipt.
        required=set(proposal['required_obligation_ids'])
        declared=set(re.findall(r'(?m)^\s*-\s+\*\*((?:AC|SEC|NFR)-[0-9]+)\*\*',
            (bundle/'scope/requirements.md').read_text()))
        expected={f'{prefix}-{number}' for prefix,count in [('AC',39),('SEC',12),('NFR',7)] for number in range(1,count+1)}
        if required!=expected or declared!=expected:
            raise RuntimeError('Final validation must preserve all58 original obligations')
        context=TrustedContext(context.expected_commit,context.known_task_ids,
            frozenset(context.obligation_ids|required),context.evidence_refs)
        spec=importlib.util.spec_from_file_location('recorded_reviews',Path(__file__).with_name('final-recorded-reviews-v6.py'))
        recorded=importlib.util.module_from_spec(spec);spec.loader.exec_module(recorded)
        spec=importlib.util.spec_from_file_location('transport_v6',Path(__file__).with_name('final-structured-transport-v6.py'))
        transport=importlib.util.module_from_spec(spec);spec.loader.exec_module(transport)
        outcomes,reports=recorded.verify_recorded_reviews(bundle,proposal,context,transport,PARSER['review'])
        if proposal['rows']!=[r for r in proposal['rows'] if r['gate'] in ('qa','appsec')] or [r['gate'] for r in proposal['rows']]!=['qa','appsec']:
            raise RuntimeError('Only remaining QA and AppSec dispatches are authorized')
        if args.verify_only:
            stamp('PASS: exact source, scripts, environment, parent history, cumulative9/11 accounting and both recorded reviews; provider_calls=0')
            return
        for engine,text in reports.items():
            path=root/('preserved-review-'+engine+'.md')
            if path.exists() and path.read_text()!=text:raise RuntimeError('Recorded review receipt changed')
            if not path.exists():path.write_text(text)
            job.register('preserved-review-'+engine+'.md',path)
        atomic_json(root/'recorded-reviews-receipt.json',dict(source=proposal['source_head'],outcomes=outcomes,provider_calls=0))
        for row in proposal['rows']:
            guard()
            request=EvaluationRequest(row['gate'],row['engine'],row['model'],row['ordinal'],1,
                proposal['source_head'],UNTRUSTED_DATA_INSTRUCTION,(bundle/row['prompt']).read_text(),
                context,evidence,subject,PARSER[row['gate']],reasoning_effort=proposal['reasoning_effort'],
                controls=dict(external_source_verified=True,cumulative_authority_verified=True,
                    historical_task_receipts_not_used_as_source_proof=True))
            result=job.evaluate(request)
            outcome=dict(gate=row['gate'],engine=row['engine'],model=row['model'],
                approved=result.approved,outcome_class=result.outcome_class,artifacts=result.artifact_refs)
            outcomes.append(outcome);atomic_json(root/'outcomes-summary.json',dict(source=proposal['source_head'],outcomes=outcomes))
            stamp(json.dumps(outcome,sort_keys=True))
            if not result.approved:
                raise RuntimeError('Independent final gate did not approve; no developer or automatic repair authorized')
        guard()
        atomic_json(root/'evaluation-completion.json',dict(source=proposal['source_head'],
            proposal_sha256=subject,independent_gates_approved=True,
            host_requirement_audit_and_sanitized_publication_still_required=True,
            parent_unchanged=True,outcomes=outcomes))
        stamp('Independent final evaluations complete; requirement audit and publication remain.')


if args.self_test:
    self_test()
else:
    execute()
