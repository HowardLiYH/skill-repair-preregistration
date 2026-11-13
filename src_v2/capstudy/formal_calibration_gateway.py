"""Optional, explicitly approved calibration binding; no listener or key discovery.

The draft scope is PENDING. A funded pilot ledger is never scope permission.
Physical requests remain on that same ledger, with a distinct scientific ID.
"""
from __future__ import annotations
import copy
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import platform
import re
from .contracts import canonical_hash,sha256_file
from .compress.xing import STRATEGIES
from .source_only_generation import FAMILIES
from .runner import canonical_hash as runner_hash,execution_config_hash
from .journal import AdmissionBlocked,money

PHASE='formal_calibration_gateway'
ACCOUNTING_CAMPAIGN='deepseek-calibration-pilot'
OFFICIAL_ENDPOINT='https://api.deepseek.com/anthropic/v1/messages'
TRAIN_IDS=tuple(family+'-1' for family in FAMILIES)
CONDITIONS=('full_skill','no_skill','unchanged_rewrite')


def _path(value,base):
    if not isinstance(value,str) or not value:raise ValueError('Explicit existing evidence path required')
    path=Path(value);return path if path.is_absolute() else Path(base)/path


def _load(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Existing regular evidence file required')
    raw=path.read_bytes()
    return json.loads(raw),hashlib.sha256(raw).hexdigest()


def _bound(reference,base):
    if not isinstance(reference,dict):raise ValueError('Hashed evidence reference required')
    path=_path(reference.get('path'),base);obj,digest=_load(path)
    if digest!=reference.get('sha256'):raise ValueError('Referenced approval/gate/freeze evidence changed')
    return obj,path


def _verify_files(rows,base):
    if not isinstance(rows,list) or not rows:raise ValueError('Nonempty underlying evidence required')
    for row in rows:
        path=_path(row.get('path'),base)
        if path.is_symlink() or not path.is_file() or sha256_file(path)!=row.get('sha256'):
            raise ValueError('Underlying gate/freeze evidence changed')


def scope_fingerprint(descriptor):
    return canonical_hash({key:value for key,value in descriptor.items() if key!='scope_approval'})


def validate_scope(descriptor_path,base_config_path):
    """Read only. No approval, budget, readiness, or freeze state is created."""
    descriptor_path=Path(descriptor_path).resolve();base_config_path=Path(base_config_path).resolve()
    scope,scope_sha=_load(descriptor_path)
    approval=scope.get('scope_approval',{})
    if (approval.get('status')!='approved_formal_calibration'
        or approval.get('scope_sha256')!=scope_fingerprint(scope)
        or any(not isinstance(approval.get(key),str) or not approval[key].strip()
               for key in ('approved_by','approved_at','approval_reference'))):
        raise AdmissionBlocked('Concrete formal-calibration scope approval is PENDING or does not match this descriptor; pilot funding is not permission')
    base,base_sha=_load(base_config_path)
    if '_repo_root' in base or runner_hash(base)!=canonical_hash(base):
        raise ValueError('Formal config requires host-independent JSON and canonicalization agreement')
    if (scope.get('schema_version')!=1 or scope.get('phase')!='formal_calibration'
        or scope.get('model_id')!='deepseek-flash' or scope.get('upstream_url')!=OFFICIAL_ENDPOINT
        or scope.get('allowed_instance_ids')!=list(TRAIN_IDS) or scope.get('conditions')!=list(CONDITIONS)
        or scope.get('strategy_ids')!=list(STRATEGIES) or scope.get('maximum_logical_cells')!=48
        or scope.get('maximum_physical_trials_per_cell')!=1
        or scope.get('additional_budget_usd')!=0 or scope.get('provider_key_location')!='mac_only'
        or scope.get('bind')!='127.0.0.1' or scope.get('maximum_listener_lifetime_seconds')!=7200):
        raise ValueError('Unsupported formal calibration scope or weakened transport policy')
    kind=scope.get('evidence_kind','experiment')
    if kind not in ('experiment','fixture'):raise ValueError('Invalid scope evidence kind')
    science=scope.get('scientific_campaign_id')
    if not isinstance(science,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',science) or science==ACCOUNTING_CAMPAIGN:
        raise ValueError('Distinct explicit scientific campaign identity required')
    accounting=scope.get('accounting',{})
    if accounting.get('campaign_id')!=ACCOUNTING_CAMPAIGN or money(accounting.get('budget_usd'))!=5:
        raise ValueError('Only the one existing $5 accounting campaign is supported')
    if (base.get('campaign_id')!=science or base.get('executor',{}).get('provider')!='deepseek'
        or base['executor'].get('model_id')!='deepseek-flash' or scope.get('base_config_sha256')!=runner_hash(base)):
        raise ValueError('Scientific base configuration binding differs')
    manifest,manifest_path=_bound(scope.get('train_manifest'),descriptor_path.parent)
    # A JSON review projection avoids loading task inputs; the original JSONL
    # hash and source paths must be among the projection's underlying evidence.
    if (manifest.get('instance_ids')!=list(TRAIN_IDS) or manifest.get('split')!='train'
        or manifest.get('evidence_kind')!=kind):raise ValueError('Exact eight-train-instance source projection required')
    _verify_files(manifest.get('evidence_files'),manifest_path.parent)
    train=manifest.get('train_jsonl',{})
    train_path=_path(train.get('path'),manifest_path.parent)
    if (train_path.is_symlink() or not train_path.is_file() or train_path.parent.name!='train'
        or train_path.name!='instances.jsonl' or sha256_file(train_path)!=train.get('sha256')):
        raise ValueError('Original train JSONL evidence required')
    rows=[json.loads(line) for line in train_path.read_text().splitlines() if line.strip()]
    if (len(rows)!=8 or sorted(row.get('instance_id','') for row in rows)!=list(TRAIN_IDS)
        or sorted(row.get('family','') for row in rows)!=list(FAMILIES)
        or any(row.get('split')!='train' for row in rows)):
        raise ValueError('Original manifest is not the exact eight train records')
    for gate in ('G1','G2'):
        checked,path=_bound(scope.get('gates',{}).get(gate),descriptor_path.parent)
        if checked.get('gate_id')!=gate or checked.get('passed') is not True or checked.get('evidence_kind')!=kind:
            raise ValueError('Required gate has not passed')
        if gate=='G2' and checked.get('families')!=list(FAMILIES):raise ValueError('G2 must cover all eight families')
        _verify_files(checked.get('evidence_files'),path.parent)
    freeze,freeze_path=_bound(scope.get('base_freeze'),descriptor_path.parent)
    if (freeze.get('frozen') is not True or freeze.get('phase')!='formal_calibration'
        or freeze.get('base_config_sha256')!=runner_hash(base) or freeze.get('evidence_kind')!=kind):
        raise ValueError('Calibration base configuration is not frozen')
    _verify_files(freeze.get('protocol_files'),freeze_path.parent)
    if science=='formal-calibration-v2':
        _validate_restart(scope,base,descriptor_path.parent)
    elif scope.get('source_generation_adoption') or base.get('source_generation_adoption'):
        raise ValueError('Source adoption is restricted to the reviewed v2 campaign')
    return scope,{'descriptor_path':str(descriptor_path),'descriptor_sha256':scope_sha,
        'base_config_path':str(base_config_path),'base_config_file_sha256':base_sha,
        'base_config_sha256':runner_hash(base),'base_execution_config_sha256':execution_config_hash(base)}


def _validate_restart(scope,base,descriptor_parent):
    from .source_adoption import load_manifest,ORIGIN,DESTINATION
    manifest=load_manifest(base,verify_origin=True)
    declared=scope.get('source_generation_adoption')
    expected={'manifest':{'path':base['source_generation_adoption']['manifest_path'],
                         'sha256':base['source_generation_adoption']['manifest_sha256']},
              'origin_scope':manifest['origin_scope'],'origin_base_config':manifest['origin_base_config']}
    if declared!=expected or 'source_only_generation' in scope:
        raise ValueError('Runtime restart adopts the complete original matrix and cannot generate again')
    policy=scope.get('restart_policy',{})
    ids=policy.get('prior_physical_trial_ids',[])
    if (set(policy)!={'origin_scientific_campaign_id','prior_physical_trial_ids','maximum_total_physical_trials',
                     'maximum_new_physical_trials','prior_outcomes_adopted'}
        or policy.get('origin_scientific_campaign_id')!=ORIGIN
        or policy.get('maximum_total_physical_trials')!=48 or policy.get('maximum_new_physical_trials')!=26
        or policy.get('prior_outcomes_adopted') is not False or len(ids)!=3 or len(set(ids))!=3
        or any(not isinstance(x,str) or not re.fullmatch(r'cal-[0-9a-f]{32}',x) for x in ids)):
        raise ValueError('Restart must preserve three prior attempts and bound26 fresh physical trials within48')
    if sum(row['eligible'] is True for row in manifest['cells'])+16!=26:
        raise ValueError('Reviewed restart must preserve10 eligible/22 infeasible cells')
    amendment,_=_bound(scope.get('scientific_amendment'),descriptor_parent)
    required={'status':'approved_runtime_restart','reviewed_by':['Howard/root','Audit Agent'],
        'origin_scientific_campaign_id':ORIGIN,'destination_scientific_campaign_id':DESTINATION,
        'prior_physical_trial_ids':ids,'source_adoption_manifest_sha256':base['source_generation_adoption']['manifest_sha256']}
    if any(amendment.get(k)!=v for k,v in required.items()):
        raise ValueError('Prospective root/Audit scientific amendment required; general funding is insufficient')
    return policy


def _restart_registration_guard(journal,binding,existing):
    if binding['scientific_campaign_id']!='formal-calibration-v2':return
    scope,_=validate_scope(binding['descriptor_path'],binding['base_config_path'])
    policy=scope['restart_policy'];prior=[];current=[]
    for row in journal.connection.execute("SELECT value FROM metadata WHERE key LIKE 'formal_calibration_physical:%'"):
        b=json.loads(row['value'])['binding']
        if b['scientific_campaign_id']==policy['origin_scientific_campaign_id']:prior.append(b['physical_trial_id'])
        elif b['scientific_campaign_id']==binding['scientific_campaign_id']:current.append(b['physical_trial_id'])
        else:raise AdmissionBlocked('Unexpected additional formal campaign consumes unreviewed task allowance')
    if sorted(prior)!=sorted(policy['prior_physical_trial_ids']):
        raise AdmissionBlocked('Historical physical registrations differ from reviewed three-attempt disposition')
    count=len(current)+(0 if existing else 1)
    if count>policy['maximum_new_physical_trials'] or count+len(prior)>policy['maximum_total_physical_trials']:
        raise AdmissionBlocked('Shared authorization physical-trial allowance exhausted')


def build_execution_binding(*,descriptor_path,base_config_path,trial_config_path,
                            physical_trial_id,instance_id,condition,strategy_id=None,
                            candidate_id=None,candidate_sha256=None):
    scope,proof=validate_scope(descriptor_path,base_config_path)
    config_path=Path(trial_config_path).resolve();config,config_sha=_load(config_path)
    if '_repo_root' in config or runner_hash(config)!=canonical_hash(config):
        raise ValueError('Formal config requires host-independent JSON and canonicalization agreement')
    if not isinstance(physical_trial_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',physical_trial_id):
        raise ValueError('Explicit safe physical trial ID required')
    if instance_id not in TRAIN_IDS or condition not in CONDITIONS:raise ValueError('Only approved calibration train cells are supported')
    if condition=='unchanged_rewrite':
        if strategy_id not in STRATEGIES:raise ValueError('Calibration rewrite requires one declared strategy')
    elif strategy_id is not None:raise ValueError('Reference controls cannot use a rewrite strategy')
    if condition=='no_skill':
        if candidate_id is not None or candidate_sha256 is not None:raise ValueError('No-skill must not bind a candidate')
    else:
        if not isinstance(candidate_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',candidate_sha256):
            raise ValueError('Explicit full/rewritten bundle hash required')
        if condition=='full_skill' and candidate_id is not None:raise ValueError('Full-skill source control has no generated candidate ID')
        if condition=='unchanged_rewrite' and (not isinstance(candidate_id,str) or not candidate_id):
            raise ValueError('Rewrite candidate identity required')
    if (config.get('campaign_id')!=scope['scientific_campaign_id']
        or 'pilot_trial_id' in config
        or config.get('physical_trial_id') not in (None,physical_trial_id)
        or execution_config_hash(config)!=proof['base_execution_config_sha256']):
        raise ValueError('Physical trial configuration differs from the frozen science/runtime base')
    if scope['scientific_campaign_id']=='formal-calibration-v2' and condition=='unchanged_rewrite':
        from .source_adoption import load_manifest,bound,verify_candidate
        from .runner import tree_hash
        manifest=load_manifest(config)
        matches=[row for row in manifest['cells'] if row['candidate_id']==candidate_id]
        if len(matches)!=1:raise ValueError('Rewrite is absent from the adopted original matrix')
        row=matches[0];candidate,_=bound(row['record'])
        verify_candidate(config,candidate,row['record']['sha256'],manifest=manifest)
        if (not candidate['eligible'] or candidate['family']!=instance_id[:-2]
            or candidate['strategy_id']!=strategy_id or tree_hash(candidate['bundle_path'])!=candidate_sha256):
            raise ValueError('Central rewrite binding differs from exact eligible adopted source bytes')
    mapping=config.get('runtime',{}).get('deepseek_mapping',{})
    from .deepseek_gateway import DEFAULT_TOOLS
    if (mapping.get('route')!='private-deepseek-anthropic-relay' or mapping.get('model_id')!='deepseek-flash'
        or not isinstance(mapping.get('allowed_tools'),list) or not set(mapping['allowed_tools'])<=DEFAULT_TOOLS
        or type(mapping.get('max_output_tokens')) is not int or not 0<mapping['max_output_tokens']<=16384
        or not isinstance(mapping.get('relay_url_sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',mapping['relay_url_sha256'])):
        raise ValueError('Frozen private DeepSeek runtime mapping required')
    binding={'schema_version':1,'phase':PHASE,'scientific_campaign_id':scope['scientific_campaign_id'],
        'accounting_campaign_id':ACCOUNTING_CAMPAIGN,'evidence_kind':scope.get('evidence_kind','experiment'),
        'accounting_journal_path':str(_path(scope['accounting']['journal_path'],Path(proof['descriptor_path']).parent).resolve()),
        'physical_trial_id':physical_trial_id,'instance_id':instance_id,'family':instance_id[:-2],
        'split':'train','condition':condition,'strategy_id':strategy_id,'candidate_id':candidate_id,
        'candidate_sha256':candidate_sha256,'physical_attempt_index':1,
        'trial_config_path':str(config_path),'trial_config_file_sha256':config_sha,
        'trial_config_sha256':runner_hash(config),'execution_config_sha256':execution_config_hash(config),
        'mapping_sha256':canonical_hash(mapping),**proof}
    binding['binding_sha256']=canonical_hash(binding)
    return binding


def validate_execution_binding(binding,*,journal=None,transport=None,relay_token=None):
    if not isinstance(binding,dict):raise ValueError('Explicit formal execution binding required')
    wanted=build_execution_binding(descriptor_path=binding.get('descriptor_path'),
        base_config_path=binding.get('base_config_path'),trial_config_path=binding.get('trial_config_path'),
        physical_trial_id=binding.get('physical_trial_id'),instance_id=binding.get('instance_id'),
        condition=binding.get('condition'),strategy_id=binding.get('strategy_id'),
        candidate_id=binding.get('candidate_id'),candidate_sha256=binding.get('candidate_sha256'))
    if wanted!=binding:raise ValueError('Formal trial execution binding changed')
    if journal is None:return binding
    scope,_=validate_scope(binding['descriptor_path'],binding['base_config_path'])
    accounting=scope['accounting'];expected_path=_path(accounting.get('journal_path'),Path(binding['descriptor_path']).parent).resolve()
    metadata=dict(journal.connection.execute('SELECT key,value FROM metadata'))
    if (Path(journal.path).resolve()!=expected_path or metadata.get('campaign_id')!=ACCOUNTING_CAMPAIGN
        or metadata.get('evidence_kind')!=binding['evidence_kind'] or money(metadata.get('budget_usd'))!=5
        or journal.budget!=5):raise AdmissionBlocked('Formal calls must use the unchanged existing central $5 journal')
    from .deepseek_gateway import OfficialTransport,UPSTREAM_URL
    if UPSTREAM_URL!=OFFICIAL_ENDPOINT:raise ValueError('Official endpoint changed')
    if binding['evidence_kind']=='experiment' and (platform.system()!='Darwin' or type(transport) is not OfficialTransport):
        raise AdmissionBlocked('Formal experiment transport must be the official transport on the Mac key host')
    if not isinstance(relay_token,str) or not re.fullmatch(r'capstudy-relay-[A-Za-z0-9_-]{32,}',relay_token):
        raise ValueError('Dedicated ephemeral relay token required; never an upstream credential')
    return binding


def formal_dispatch_storage(binding,artifact_root):
    scope,_=validate_scope(binding['descriptor_path'],binding['base_config_path'])
    policy=scope.get('dispatch_storage_policy')
    if policy is None and binding['evidence_kind']=='fixture':return None
    from .dispatch_storage import validate_storage_policy
    return validate_storage_policy(policy,evidence_kind=binding['evidence_kind'],artifact_root=artifact_root)


def register_trial_binding(journal,binding,relay_token,artifact_root):
    """Bind one physical execution per approved logical cell, on the same ledger."""
    cell={key:binding[key] for key in ('scientific_campaign_id','instance_id','condition','strategy_id')}
    token_sha=hashlib.sha256(relay_token.encode()).hexdigest()
    record={'binding':binding,'relay_token_sha256':token_sha,'artifact_root':str(Path(artifact_root).resolve())}
    keys=['formal_calibration_cell:'+canonical_hash(cell),
        'formal_calibration_physical:'+canonical_hash({'trial_id':binding['physical_trial_id']}),
        'formal_calibration_token:'+token_sha]
    with journal.transaction():
        existing=[]
        for key in keys:
            previous=journal.connection.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
            if previous:
                old=json.loads(previous['value'])
                if {k:v for k,v in old.items() if k not in ('registered_at','expires_at')}!=record:
                    raise AdmissionBlocked('Formal logical cell/physical trial/token already has another immutable binding')
                existing.append(old)
        _restart_registration_guard(journal,binding,existing)
        if existing:
            if any(row!=existing[0] for row in existing):raise AdmissionBlocked('Formal registration copies disagree')
            record=existing[0]
        else:
            now=datetime.now(timezone.utc)
            record.update(registered_at=now.isoformat(),expires_at=(now+timedelta(seconds=7200)).isoformat())
        try:
            registered=datetime.fromisoformat(record['registered_at']);expires=datetime.fromisoformat(record['expires_at'])
            if (registered.tzinfo is None or expires.tzinfo is None or expires-registered!=timedelta(seconds=7200)
                or datetime.now(timezone.utc)>=expires):raise ValueError('expired')
        except (ValueError,KeyError):raise AdmissionBlocked('Formal trial relay lifetime expired or changed; restart cannot extend it') from None
        for key in keys:
            journal.connection.execute('INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)',(key,json.dumps(record,sort_keys=True)))
    return record


def create_formal_gateway(*,journal,artifact_root,transport,relay_token,execution_binding):
    """Construct only after scope validation; does not bind a socket or call API."""
    from .deepseek_gateway import BudgetedGateway
    validate_execution_binding(execution_binding,journal=journal,transport=transport,relay_token=relay_token)
    config,_=_load(execution_binding['trial_config_path'])
    mapping=config['runtime']['deepseek_mapping']
    return BudgetedGateway(journal,artifact_root,transport,relay_token=relay_token,
        trial_id=execution_binding['physical_trial_id'],allowed_tools=mapping['allowed_tools'],
        execution_binding=execution_binding)


def formal_server_state(gateway,*,port,lifetime_seconds,started_at,stopped_at=None):
    """Construct lifecycle evidence only; the caller still owns a bounded server."""
    binding=gateway.execution_binding
    validate_execution_binding(binding,journal=gateway.journal,transport=gateway.transport,relay_token=gateway.relay_token)
    if type(port) is not int or not 1024<=port<=65535 or type(lifetime_seconds) is not int or not 1<=lifetime_seconds<=7200:
        raise ValueError('Bounded nonprivileged loopback lifecycle required')
    if not isinstance(started_at,str) or not started_at:raise ValueError('Explicit lifecycle start time required')
    return {'trial_id':binding['physical_trial_id'],'bind':'127.0.0.1','port':port,'budget_usd':5,
        'lifetime_seconds':lifetime_seconds,'started_at':started_at,'stopped_at':stopped_at,
        'listening':stopped_at is None,'execution_binding':copy.deepcopy(binding),
        'accounting_campaign_id':ACCOUNTING_CAMPAIGN,'provider_key_location':'mac_only',
        'registered_at':gateway.formal_registration['registered_at'],'expires_at':gateway.formal_registration['expires_at'],
        'relay_token_sha256':hashlib.sha256(gateway.relay_token.encode()).hexdigest()}
