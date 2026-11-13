"""Config-ready 8 x 4 initial-generation adapter; no production CLI or credentials.

The caller supplies the existing central accounting journal and reviewed provider
and count boundaries. Scientific campaign identity never creates a new budget.
"""
from __future__ import annotations
import copy
import json
import platform
from pathlib import Path
import re
from .bundles import load_bundle,validate_candidate as validate_bundle
from .contracts import canonical_hash,sha256_file,write_json_atomic
from .compress.xing import STRATEGIES
from .editor_service import EditorService
from .journal import AdmissionBlocked,money
from .tokenization import bundle_count

FAMILIES=tuple(sorted(('dbscan-parameter-tuning','earthquake-plate-calculation',
    'financial-analysis','organize-messy-files','schedule-planning',
    'stock-data-visualization','temperature-simulation','weighted-gdp-calculation')))


class SourceOnlyMatrixGenerator:
    def __init__(self,*,data_root,train_manifest,train_manifest_sha256,
                 scientific_campaign_id,accounting_campaign_id,accounting_journal_path,
                 generation_config_sha256,artifact_root,journal,counter,transport,
                 editor_counter,context_limit,max_output_tokens,rewrite_prompt_version,
                 upper_charge_usd,payment_mode='api',cost_resolver=None,
                 included_usage_evidence=None,headroom=.10,evidence_kind='experiment',generation_admission=None,planning_only=False):
        if type(planning_only) is not bool:raise ValueError('Explicit planning-only boolean required')
        self.planning_only=planning_only
        if evidence_kind=='experiment' and generation_admission is None and not planning_only:
            raise AdmissionBlocked('Explicit approved formal generation scope is required before construction or dispatch')
        self.generation_admission=copy.deepcopy(generation_admission)
        for value in (scientific_campaign_id,accounting_campaign_id):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',value):
                raise ValueError('Explicit safe scientific and accounting campaign IDs required')
        if scientific_campaign_id==accounting_campaign_id:
            raise ValueError('Formal scientific campaign must be distinct from the existing accounting campaign')
        if not isinstance(generation_config_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',generation_config_sha256):
            raise ValueError('Immutable generation protocol SHA-256 required')
        if rewrite_prompt_version not in ('v1','v2'):raise ValueError('Explicit supported rewrite prompt revision required')
        if evidence_kind not in ('experiment','fixture'):raise ValueError('Unsupported evidence kind')
        self.data_root=Path(data_root).resolve();self.train_manifest=Path(train_manifest).resolve()
        self.root=Path(artifact_root).resolve();self.journal=journal
        self.scientific_campaign_id=scientific_campaign_id;self.counter=counter;self.transport=transport
        self.cost_resolver=cost_resolver
        if self.train_manifest!=self.data_root/'train/instances.jsonl':
            raise ValueError('Use the exact train manifest, not dev/test or arbitrary inputs')
        if sha256_file(self.train_manifest)!=train_manifest_sha256:raise ValueError('Train manifest differs from configured source')
        self.train_manifest_sha256=train_manifest_sha256
        if Path(journal.path).resolve()!=Path(accounting_journal_path).resolve():
            raise ValueError('Injected journal is not the explicitly bound existing central ledger')
        metadata={row['key']:row['value'] for row in journal.connection.execute('SELECT key,value FROM metadata')}
        if metadata.get('campaign_id')!=accounting_campaign_id or metadata.get('evidence_kind')!=evidence_kind:
            raise ValueError('Existing central ledger campaign/evidence binding differs')
        if money(metadata.get('budget_usd'))!=journal.budget:raise ValueError('Central accounting budget metadata differs')
        if (evidence_kind=='fixture')!=(payment_mode=='fixture'):raise ValueError('Fixture and payment modes must agree')
        tokenizer_id=getattr(counter,'tokenizer_id',None);tokenizer_revision=getattr(counter,'revision',None)
        if not all(isinstance(x,str) and x for x in (tokenizer_id,tokenizer_revision)):
            raise ValueError('Bound counting identity/revision required')
        records=[json.loads(line) for line in self.train_manifest.read_text().splitlines() if line.strip()]
        if len(records)!=8 or sorted(row.get('family','') for row in records)!=list(FAMILIES):
            raise ValueError('Exactly one train record per declared family is required')
        self.sources={};source_rows=[]
        for row in sorted(records,key=lambda r:r['family']):
            family=row['family'];relative='shared/skills/human_authored/'+family
            if row.get('split')!='train' or row.get('instance_id')!=family+'-1' or row.get('skill_bundle_path')!=relative:
                raise ValueError('Train record does not map to its exact family source bundle')
            path=self.data_root/relative
            if path.resolve()!=path or not path.is_dir():raise ValueError('Source bundle must be the original nonsymlink family directory')
            source=load_bundle(path)
            if sorted(row.get('skill_documents',[]))!=[relative+'/'+name for name in sorted(source.files)]:
                raise ValueError('Train skill-document inventory differs from complete source bundle')
            self.sources[family]=source
            source_rows.append({'family':family,'train_instance_id':row['instance_id'],
                'skill_bundle_path':relative,'skill_paths':sorted(source.files),
                'hashes':source.hashes(),'original_tokens':bundle_count(source.files,counter)})
        accounting={'journal_path':str(Path(journal.path).resolve()),'campaign_id':accounting_campaign_id,
            'budget_usd':metadata['budget_usd'],'evidence_kind':evidence_kind}
        editor_binding={'provider':getattr(transport,'provider',None),'model_id':getattr(transport,'model_id',None),
            'base_url':getattr(transport,'base_url',None),'decoding_settings':copy.deepcopy(getattr(transport,'decoding_settings',None)),
            'max_output_tokens':max_output_tokens,'context_limit':context_limit,'headroom':headroom,
            'editor_counter':{key:getattr(editor_counter,key,None) for key in ('tokenizer_id','revision','model_id')},
            'payment_mode':payment_mode,'upper_charge_usd':str(money(upper_charge_usd)),
            'rewrite_prompt_version':rewrite_prompt_version,
            'included_usage_evidence':copy.deepcopy(included_usage_evidence)}
        protocol_root=Path(__file__).resolve().parents[2]
        prompt_name='rewrite_bundle.txt' if rewrite_prompt_version=='v1' else 'rewrite_bundle_v2.txt'
        protocol_files=[Path(__file__).parent/name for name in ('source_only_generation.py','editor_service.py',
            'editor.py','formal_generation.py','editor_pilot.py','connection_pilot.py','dispatch_storage.py','formal_calibration_gateway.py','dispatch.py','journal.py','contracts.py','bundles.py','tokenization.py','deepseek_tokenization.py','transports.py',
            'compress/xing.py','compress/blocks.py')]+[protocol_root/'prompts'/prompt_name]
        self.binding={'schema_version':1,'purpose':'formal_calibration_source_only_generation',
            'evidence_kind':evidence_kind,'scientific_campaign_id':scientific_campaign_id,
            'accounting':accounting,'generation_config_sha256':generation_config_sha256,
            'artifact_root':str(self.root),'train_manifest_path':str(self.train_manifest),
            'train_manifest_sha256':train_manifest_sha256,'source_families':source_rows,
            'strategies':list(STRATEGIES),'expected_cells':32,'pilot_reuse_allowed':False,
            'tokenizer':{'id':tokenizer_id,'revision':tokenizer_revision,
                'provenance':copy.deepcopy(getattr(counter,'provenance',None))},'editor':editor_binding,
            'implementation_sha256':{str(path.relative_to(protocol_root)):sha256_file(path) for path in protocol_files},
            'excluded_editor_inputs':['task instructions','input assets','solutions','native tests','grader feedback','pilot responses']}
        self.binding['generation_admission']=copy.deepcopy(self.generation_admission)
        self.binding['generation_admission_proof']=None if planning_only else self._generation_admission_proof()
        self.binding['binding_sha256']=canonical_hash(self.binding)
        self.services={}
        if planning_only:return
        if self.binding['generation_admission_proof'] is not None:
            from .dispatch_storage import prepare_storage_root
            prepare_storage_root(self.binding['generation_admission_proof']['dispatch_storage_policy'])
        # A central metadata registration prevents a second artifact root or changed
        # source/model/config from creating another version of the same campaign.
        registration='source_only_generation:'+scientific_campaign_id
        existing=self.root/'generation_binding.json'
        if self.root.exists() and (not existing.is_file() or json.loads(existing.read_text())!=self.binding):
            raise AdmissionBlocked('Artifact root is not this exact immutable generation campaign; pilot roots cannot be imported')
        with journal.transaction():
            prior=journal.connection.execute('SELECT value FROM metadata WHERE key=?',(registration,)).fetchone()
            if prior and json.loads(prior['value'])!=self.binding:
                raise AdmissionBlocked('Scientific campaign is already bound to a different source/config/artifact root')
            if not prior:
                journal.connection.execute('INSERT INTO metadata(key,value) VALUES(?,?)',
                    (registration,json.dumps(self.binding,sort_keys=True)))
        if not self.root.exists():
            self.root.mkdir(parents=True,exist_ok=False);write_json_atomic(existing,self.binding)
        self.services={}
        for family in FAMILIES:
            for strategy in STRATEGIES:
                self.services[family,strategy]=EditorService(source=self.sources[family],family=family,
                    campaign_id=scientific_campaign_id,strategy_id=strategy,config_sha256=generation_config_sha256,
                    counter=counter,tokenizer_id=tokenizer_id,tokenizer_revision=tokenizer_revision,
                    journal=journal,transport=transport,artifact_root=self.root/family/strategy,
                    upper_charge_usd=upper_charge_usd,payment_mode=payment_mode,
                    included_usage_evidence=included_usage_evidence,cost_resolver=cost_resolver,
                    editor_counter=editor_counter,context_limit=context_limit,max_output_tokens=max_output_tokens,
                    headroom=headroom,evidence_kind=evidence_kind,rewrite_prompt_version=rewrite_prompt_version,
                    pilot_attempt_namespace=None)

    def _generation_admission_proof(self):
        if self.generation_admission is None:
            if self.binding['evidence_kind']=='experiment':raise AdmissionBlocked('Formal generation admission missing')
            return None
        from .formal_calibration_gateway import validate_scope,_bound,ACCOUNTING_CAMPAIGN
        admission=self.generation_admission
        if not isinstance(admission,dict) or set(admission)!={'descriptor_path','base_config_path'}:
            raise AdmissionBlocked('Exact descriptor/base configuration references required')
        scope,proof=validate_scope(admission['descriptor_path'],admission['base_config_path'])
        accounting=self.binding['accounting']
        declared=scope['accounting'];base=Path(proof['descriptor_path']).parent
        journal_path=Path(declared['journal_path'])
        if not journal_path.is_absolute():journal_path=base/journal_path
        if (scope['scientific_campaign_id']!=self.scientific_campaign_id
                or scope.get('evidence_kind','experiment')!=self.binding['evidence_kind']
                or accounting['campaign_id']!=ACCOUNTING_CAMPAIGN or money(accounting['budget_usd'])!=5
                or journal_path.resolve()!=Path(accounting['journal_path']).resolve()):
            raise AdmissionBlocked('Generation must use approved science scope and exact existing $5 ledger')
        generation=scope.get('source_only_generation')
        if (not isinstance(generation,dict) or generation.get('maximum_candidates')!=32
                or generation.get('attempt_index')!=0 or generation.get('source_only') is not True
                or generation.get('generation_config_sha256')!=self.binding['generation_config_sha256']):
            raise AdmissionBlocked('Scope does not explicitly approve the frozen 32 source-only generation slots')
        from .dispatch_storage import validate_storage_policy
        disk=validate_storage_policy(scope.get('dispatch_storage_policy'),
            evidence_kind=self.binding['evidence_kind'],artifact_root=self.root)
        protocol,path=_bound(generation.get('protocol'),base)
        expected={k:v for k,v in self.binding.items() if k not in (
            'generation_config_sha256','binding_sha256','generation_admission','generation_admission_proof')}
        if (protocol!={'schema_version':1,'purpose':'formal_source_only_generation','generation_binding':expected,
                          'dispatch_storage_policy':disk}
                or canonical_hash(protocol)!=self.binding['generation_config_sha256']):
            raise AdmissionBlocked('Immutable generation protocol differs from actual source/editor/count/accounting binding')
        if self.binding['evidence_kind']=='experiment':
            from .transports import OpenAICompatibleTransport,post_json_once_bounded
            if type(self.transport) is not OpenAICompatibleTransport or self.transport.post is not post_json_once_bounded:
                raise AdmissionBlocked('Formal generation requires the reviewed bounded official editor transport')
            editor=self.binding['editor']
            if (platform.system()!='Darwin' or editor['provider']!='deepseek' or editor['model_id']!='deepseek-flash'
                    or editor['base_url'].rstrip('/') not in ('https://api.deepseek.com','https://api.deepseek.com/v1')
                    or editor['payment_mode']!='api'):
                raise AdmissionBlocked('Approved generation uses DeepSeek Flash API on the Mac key host')
        return {**proof,'generation_protocol_path':str(path.resolve()),
                'generation_protocol_file_sha256':sha256_file(path),
                'generation_config_sha256':canonical_hash(protocol),'dispatch_storage_policy':disk}

    def plan(self):
        return {'executed':False,'planning_only':self.planning_only,'binding':copy.deepcopy(self.binding),
            'cells':[{'family':family,'strategy_id':strategy,'attempt_index':0,
                'artifact_root':str(self.root/family/strategy)} for family in FAMILIES for strategy in STRATEGIES]}

    def _assert_binding(self):
        if self.planning_only:raise AdmissionBlocked('Planning-only preview cannot dispatch or register generation')
        if (self.generation_admission!=self.binding['generation_admission']
                or self._generation_admission_proof()!=self.binding['generation_admission_proof']):
            raise AdmissionBlocked('Frozen generation admission changed')
        if sha256_file(self.train_manifest)!=self.train_manifest_sha256:
            raise AdmissionBlocked('Train source manifest changed after campaign binding')
        if json.loads((self.root/'generation_binding.json').read_text())!=self.binding:
            raise AdmissionBlocked('Generation campaign binding changed')
        metadata={row['key']:row['value'] for row in self.journal.connection.execute('SELECT key,value FROM metadata')}
        expected=self.binding['accounting']
        if (metadata.get('campaign_id')!=expected['campaign_id'] or metadata.get('budget_usd')!=expected['budget_usd']
            or metadata.get('evidence_kind')!=expected['evidence_kind'] or money(expected['budget_usd'])!=self.journal.budget
            or str(Path(self.journal.path).resolve())!=expected['journal_path']):
            raise AdmissionBlocked('Central accounting identity/budget changed')
        if json.loads(metadata.get('source_only_generation:'+self.scientific_campaign_id,'null'))!=self.binding:
            raise AdmissionBlocked('Central campaign registration changed')
        current={'id':getattr(self.counter,'tokenizer_id',None),'revision':getattr(self.counter,'revision',None),
            'provenance':copy.deepcopy(getattr(self.counter,'provenance',None))}
        if current!=self.binding['tokenizer']:raise AdmissionBlocked('Frozen counting identity/provenance changed')
        protocol_root=Path(__file__).resolve().parents[2]
        if any(sha256_file(protocol_root/path)!=digest for path,digest in self.binding['implementation_sha256'].items()):
            raise AdmissionBlocked('Bound generation code or prompt bytes changed')

    def generate_cell(self,family,strategy_id):
        self._assert_binding()
        proof=self.binding['generation_admission_proof']
        if proof is not None:
            from .dispatch_storage import dispatch_storage_guard
            with dispatch_storage_guard(proof['dispatch_storage_policy'],evidence_kind=self.binding['evidence_kind'],artifact_root=self.root):
                return self._generate_cell(family,strategy_id)
        return self._generate_cell(family,strategy_id)

    def _generate_cell(self,family,strategy_id):
        self._assert_binding()
        if (family,strategy_id) not in self.services:raise ValueError('Cell is outside the fixed 8 x 4 matrix')
        source=self.sources[family]
        if load_bundle(source.root).hashes()!=source.hashes():raise AdmissionBlocked('Original source bundle changed')
        expected_source=next(row for row in self.binding['source_families'] if row['family']==family)
        if bundle_count(source.files,self.counter)!=expected_source['original_tokens']:
            raise AdmissionBlocked('Bound original-source count changed before generation')
        service=self.services[family,strategy_id]
        editor=self.binding['editor']
        if (service.source is not source or service.family!=family or service.source_hashes!=expected_source['hashes']
            or service.original_tokens!=expected_source['original_tokens'] or service.token_cap!=expected_source['original_tokens']//2
            or service.root!=self.root/family/strategy_id or service.campaign_id!=self.scientific_campaign_id
            or service.strategy_id!=strategy_id or service.config_sha256!=self.binding['generation_config_sha256']
            or service.evidence_kind!=self.binding['evidence_kind'] or service.pilot_attempt_namespace is not None
            or service.rewrite_prompt_version!=editor['rewrite_prompt_version'] or service.journal is not self.journal
            or service.counter is not self.counter or service.transport is not self.transport or service.cost_resolver is not self.cost_resolver
            or service.payment_mode!=editor['payment_mode'] or str(money(service.upper_charge_usd))!=editor['upper_charge_usd']
            or service.included_usage_evidence!=editor['included_usage_evidence']):
            raise AdmissionBlocked('Bound generation service identity/accounting changed')
        candidate=service.validate(copy.deepcopy(service.generate_initial()))
        # Recount persisted bytes separately from the service's in-memory file map.
        original=bundle_count(source.files,self.counter)
        if candidate['original_tokens']!=original or candidate['token_cap']!=original//2:
            raise AdmissionBlocked('Independent source recount disagrees')
        measured=None
        if candidate.get('bundle_path'):
            actual=load_bundle(candidate['bundle_path'])
            validation=validate_bundle(source,actual.files,self.counter,original//2,actual.root)
            measured=bundle_count(actual.files,self.counter)
            if measured!=candidate['candidate_tokens'] or not set(validation['ineligibility_reasons']).issubset(candidate['ineligibility_reasons']):
                raise AdmissionBlocked('Independent persisted candidate recount/eligibility disagrees')
            if candidate['eligible'] and not validation['eligible']:
                raise AdmissionBlocked('Independent bundle check rejects eligible candidate')
        if candidate.get('pilot_attempt_namespace') or candidate['campaign_id']!=self.scientific_campaign_id:
            raise AdmissionBlocked('Pilot or other scientific campaign candidate cannot enter formal matrix')
        candidate_path=Path(candidate['request_path']).with_name('candidate.json')
        receipt={'schema_version':1,'binding_sha256':self.binding['binding_sha256'],
            'scientific_campaign_id':self.scientific_campaign_id,'accounting':self.binding['accounting'],
            'family':family,'strategy_id':strategy_id,'candidate_id':candidate['candidate_id'],
            'candidate_path':str(candidate_path),'candidate_sha256':sha256_file(candidate_path),
            'independent_source_tokens':original,'independent_candidate_tokens':measured,
            'eligible':candidate['eligible'],'ineligibility_reasons':candidate['ineligibility_reasons']}
        path=service.root/'cell.json'
        if path.exists():
            if json.loads(path.read_text())!=receipt:raise AdmissionBlocked('Completed matrix cell receipt changed')
        else:write_json_atomic(path,receipt)
        return candidate

    def generate_all(self):
        candidates=[self.generate_cell(family,strategy) for family in FAMILIES for strategy in STRATEGIES]
        if len({row['candidate_id'] for row in candidates})!=32:raise AdmissionBlocked('Candidate attempt identity collision')
        result={'schema_version':1,'binding_sha256':self.binding['binding_sha256'],
            'scientific_campaign_id':self.scientific_campaign_id,'accounting':self.binding['accounting'],
            'completed':True,'candidate_ids':[row['candidate_id'] for row in candidates],
            'cells':[{'family':row['family'],'strategy_id':row['strategy_id'],'candidate_id':row['candidate_id'],
                'eligible':row['eligible'],'candidate_tokens':row['candidate_tokens'],
                'ineligibility_reasons':row['ineligibility_reasons']} for row in candidates]}
        path=self.root/'generation_completed.json'
        if path.exists():
            if json.loads(path.read_text())!=result:raise AdmissionBlocked('Completed matrix summary changed')
        else:write_json_atomic(path,result)
        return {'summary':result,'candidates':candidates}
