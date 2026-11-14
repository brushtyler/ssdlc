"""Typed provider transport; preserve explicit findings, verdict and telemetry."""
import copy,json,re
from pathlib import Path
from jsonschema import Draft202012Validator

def response_schema(report_schema,gate,commit,*,known_task_ids=None,obligations=None,evidence_refs=None,source_files=None):
    report=copy.deepcopy(report_schema);report.pop('$schema',None)
    report['properties']['reviewed_commit']={'const':commit}
    report['properties']['source_gate']={'const':{'review':'code_review','qa':'qa','appsec':'appsec'}[gate]}
    items=report['properties']['findings']['items']['properties']
    for name,values in [('task_attribution',known_task_ids),('obligation_refs',obligations),('evidence_refs',evidence_refs),('file_refs',source_files)]:
        if values is not None:items[name]['items']['enum']=sorted(values)
    verdict={'review':['REVIEW_STATUS: APPROVED','REVIEW_STATUS: CHANGES_REQUESTED'],'qa':['QA_STATUS: PASSED','QA_STATUS: FAILED'],'appsec':['APPSEC_STATUS: SECURE','APPSEC_STATUS: VULNERABLE']}[gate]
    result=dict(type='object',additionalProperties=False,required=['report','human_report','verdict'],properties=dict(report=report,human_report=dict(type='string',minLength=1),verdict=dict(enum=verdict)))
    if gate=='qa' and obligations is not None:
        required=sorted(value for value in obligations if re.fullmatch(r'(?:AC|SEC|NFR)-[0-9]+',value))
        if not required:raise ValueError('QA requires an explicit obligation catalogue')
        row=dict(type='object',additionalProperties=False,required=['id','status','evidence'],properties=dict(id=dict(type='string',enum=required),status=dict(enum=['PASS','FAIL','UNPROVEN']),evidence=dict(type='string',minLength=1)))
        result['required'].append('requirement_results')
        result['properties']['requirement_results']=dict(type='array',minItems=len(required),maxItems=len(required),items=row)
    return result

def provider_schema(schema):
    """Use portable constrained-decoding shape; host retains every native constraint."""
    if isinstance(schema,list):return [provider_schema(value) for value in schema]
    if not isinstance(schema,dict):return schema
    ignored={'$schema','title','description','minLength','maxLength','minItems','maxItems','pattern'}
    result={key:provider_schema(value) for key,value in schema.items() if key not in ignored and key!='const'}
    if 'const' in schema:result['enum']=[schema['const']]
    return result

def rendered(report,schema,nonce):
    Draft202012Validator(schema).validate(report)
    matrix=''
    if 'requirement_results' in schema['properties']:
        rows=report['requirement_results'];expected=set(schema['properties']['requirement_results']['items']['properties']['id']['enum'])
        if len(rows)!=len(expected) or {row['id'] for row in rows}!=expected:
            raise ValueError('QA matrix must cover every original obligation exactly once')
        if report['verdict']=='QA_STATUS: PASSED' and any(row['status']!='PASS' for row in rows):
            raise ValueError('Unproven/failed mandatory QA rows cannot authorize approval')
        matrix='\n\n| Requirement | Status | Source/test evidence |\n| --- | --- | --- |\n'
        for row in rows:
            evidence=row['evidence'].replace('|',r'\|').replace('\n',' ')
            matrix+='| '+row['id']+' | '+row['status']+' | '+evidence+' |\n'
    human=report['human_report'].rstrip()
    # A typed verdict is already explicit. Remove only its identical final
    # duplicate from the human section; never repair a contradiction.
    lines=human.splitlines()
    if lines and lines[-1].strip()==report['verdict']:
        human='\n'.join(lines[:-1]).rstrip()
    if re.search(r'\b(?:REVIEW_STATUS|QA_STATUS|APPSEC_STATUS)\s*:',human):
        raise ValueError('Unexpected or contradictory human verdict marker')
    return (f'-----BEGIN SSDLC GATE REPORT {nonce}-----\n'+json.dumps(report['report'],sort_keys=True)+f'\n-----END SSDLC GATE REPORT {nonce}-----\n'+human+matrix+'\n'+report['verdict'])

def normalize_stdout(raw,schema,nonce,engine='claude'):
    value=json.loads(raw)
    if engine=='codex':return rendered(value,schema,nonce)
    if not isinstance(value,dict) or value.get('is_error') is True or 'structured_output' not in value:
        raise ValueError('CLI structured_output missing; prose is never converted to findings')
    result=copy.deepcopy(value);result['result']=rendered(value['structured_output'],schema,nonce)
    return json.dumps(result)

def install(native,request,user,root,report_schema,*,schema_file=None,source_files=None):
    context=request.context
    schema=response_schema(report_schema,request.gate,request.reviewed_commit,
        known_task_ids=context.known_task_ids,obligations=context.obligation_ids,
        evidence_refs=context.evidence_refs,source_files=source_files)
    if schema_file is not None:
        schema_file=Path(schema_file)
        if schema_file.is_symlink() or json.loads(schema_file.read_text())!=provider_schema(schema):
            raise RuntimeError('CLI schema file differs from exact trusted context')
    if request.engine=='codex' and schema_file is None:raise RuntimeError('Codex schema file required')
    nonce=re.search(r'-----BEGIN SSDLC GATE REPORT ([a-f0-9]+)-----',user).group(1)
    wrap=native.EngineRunner._wrap_in_role_container;run=native.EngineRunner._run_role_command
    def wrapped(cmd,*a,**kw):
        if cmd[:2]==['claude','-p']:cmd=[*cmd,'--json-schema',json.dumps(provider_schema(schema),separators=(',',':'))]
        elif cmd[:2]==['codex','exec']:
            if cmd[-1]!='-':raise RuntimeError('Unexpected Codex stdin contract')
            cmd=[*cmd[:-1],'--output-schema',str(schema_file),cmd[-1]]
        else:raise RuntimeError('Unexpected provider command in typed transport')
        return wrap(cmd,*a,**kw)
    def executed(cmd,*a,**kw):
        proc=run(cmd,*a,**kw)
        prefix='raw-structured-'+request.gate+'-'+request.engine+'-'+str(request.ordinal)+'-'
        index=1+len(list(Path(root).glob(prefix+'*.json')))
        target=Path(root)/(prefix+str(index)+'.json')
        with target.open('x') as stream:stream.write(proc.stdout or '')
        if proc.returncode==0:
            try:proc.stdout=normalize_stdout(proc.stdout or '',schema,nonce,request.engine)
            except Exception as exc:
                if request.engine=='claude':
                    # Invalid structure never discards an observed CLI cost.
                    try:original=json.loads(proc.stdout or '')
                    except ValueError:original={}
                    if not isinstance(original,dict):original={}
                    original['result']='STRUCTURED_OUTPUT_INVALID: '+type(exc).__name__
                    proc.stdout=json.dumps(original)
                else:proc.stdout='STRUCTURED_OUTPUT_INVALID: '+type(exc).__name__
                (Path(root)/(prefix+str(index)+'-failure.json')).write_text(json.dumps(dict(kind=type(exc).__name__,approved=False)))
        return proc
    native.EngineRunner._wrap_in_role_container=staticmethod(wrapped)
    native.EngineRunner._run_role_command=staticmethod(executed)
    def restore():
        native.EngineRunner._wrap_in_role_container=staticmethod(wrap)
        native.EngineRunner._run_role_command=staticmethod(run)
    return restore
