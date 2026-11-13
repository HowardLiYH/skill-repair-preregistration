"""Opt-in CPU execution bound to a completed source-only compatibility pilot.

No downloads, API calls, or fallback models. This uses the pilot's pinned released
implementation and token-coverage policy; it does not import its candidate text.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import time
from ..contracts import canonical_hash
from ..llmlingua_preflight import (ROOT, FAMILIES, OPTIONS, no_network,
    released_encoding, verify_environment, sha)
from ..llmlingua_cpu_setup import MODEL, MODEL_REVISION, CODE_REVISION
from .llmlingua2 import RATES, llmlingua_compress
from ..deepseek_tokenization import (DeepSeekOfflineCounter,OFFICIAL_MODEL,
    OFFICIAL_REVISION,OFFICIAL_TOKENIZER_SHA256)

PILOT_IMPLEMENTATIONS=('src/capstudy/llmlingua_preflight.py','src/capstudy/compress/llmlingua2.py',
    'src/capstudy/bundles.py','src/capstudy/tokenization.py','src/capstudy/deepseek_tokenization.py')


def configured_path(value, base):
    if not isinstance(value,str) or not value:
        raise ValueError('Explicit artifact path required')
    path=Path(value)
    return path if path.is_absolute() else Path(base)/path


def verified_policy(config, base):
    """Verify the exact pilot evidence before importing any model libraries."""
    if not isinstance(config,dict) or config.get('id')!='llmlingua2-validated-cpu-v1':
        raise ValueError('Explicit validated CPU compressor configuration required')
    result_path=configured_path(config.get('preflight_result_path'),base)
    manifest_path=configured_path(config.get('environment_manifest_path'),base)
    if sha(result_path)!=config.get('preflight_result_sha256'):
        raise ValueError('CPU preflight result hash mismatch')
    result=json.loads(result_path.read_text())
    expected={'evidence_kind':'source_only_local_compressor_pilot','completed':True,'executed_model':True,'formal_candidate_claimed':False,
        'provider_calls':0,'checkpoint':MODEL,'checkpoint_revision':MODEL_REVISION,
        'code_revision':CODE_REVISION,'options':OPTIONS,'rates':list(RATES),
        'device':'cpu','batch_size':1,'dtype':'float32',
        'chunk_policy':'lossless_character_spans_max_forced_and_roundtrip_BERT_count_512_including_CLS_SEP_v1',
        'network_policy':'local_files_only,trust_remote_code_false,offline_flags,Python_socket_block'}
    if any(result.get(key)!=value or type(result.get(key)) is not type(value) for key,value in expected.items()):
        raise ValueError('CPU preflight did not establish the supported execution policy')
    expected_counter={'tokenizer_id':DeepSeekOfflineCounter.tokenizer_id,'model_id':OFFICIAL_MODEL,
        'model_revision':OFFICIAL_REVISION,'tokenizer_sha256':OFFICIAL_TOKENIZER_SHA256,
        'runtime':'tokenizers','runtime_version':'0.22.2','convention':DeepSeekOfflineCounter.convention,
        'provider_counted':False,'estimates_not_billing':True,'api_deployment_equivalence':'not_verified',
        'evidence_kind':'offline_tokenizer_measurement'}
    if result.get('counter_provenance')!=expected_counter:
        raise ValueError('CPU preflight lacks the verified offline count convention/artifact')
    if type(result.get('threads')) is not int or not 1<=result['threads']<=2:
        raise ValueError('Unverified CPU thread setting')
    if type(result.get('maximum_seconds')) is not int or result['maximum_seconds']<=0:
        raise ValueError('Unverified CPU wall-time setting')
    families=result.get('families',[])
    if [row.get('family') for row in families]!=list(FAMILIES):
        raise ValueError('Required CPU preflight source families missing')
    for row in families:
        if row.get('full_source_coverage_verified') is not True or row.get('all_declared_rates_completed') is not True:
            raise ValueError('Incomplete CPU preflight coverage')
        if [r.get('rate') for r in row.get('rate_results',[])]!=list(RATES):
            raise ValueError('CPU preflight rate evidence incomplete')
        if type(row.get('body_chunk_count')) is not int or row['body_chunk_count']<=0 or row.get('model_forward_batches')!=row['body_chunk_count']*len(RATES):
            raise ValueError('CPU preflight actual forward coverage incomplete')
    if set(result.get('implementation_sha256',{}))!=set(PILOT_IMPLEMENTATIONS):
        raise ValueError('CPU preflight implementation binding missing')
    for relative,digest in result['implementation_sha256'].items():
        path=Path(relative)
        if path.is_absolute() or '..' in path.parts or sha(ROOT/path)!=digest:
            raise ValueError('CPU preflight implementation changed')
    if sha(manifest_path)!=config.get('environment_manifest_sha256') or config['environment_manifest_sha256']!=result.get('environment_manifest_sha256'):
        raise ValueError('CPU environment differs from successful preflight')
    binding={**expected,'threads':result['threads'],'maximum_seconds':result['maximum_seconds'],
        'environment_manifest_sha256':config['environment_manifest_sha256'],
        'preflight_result_sha256':config['preflight_result_sha256'],
        'validated_cpu_adapter_sha256':sha(__file__)}
    binding['policy_sha256']=canonical_hash(binding)
    return manifest_path,result,binding


@contextmanager
def cpu_compressor(manifest_path, policy):
    """Use exactly the compatible CPU settings; restore environment on exit."""
    if platform.system()!='Linux':raise ValueError('Validated CPU execution requires Linux')
    environment=verify_environment(manifest_path)
    root=Path(manifest_path).resolve().parent;threads=policy['threads']
    settings={'CUDA_VISIBLE_DEVICES':'','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
        'HF_HUB_DISABLE_TELEMETRY':'1','HF_HUB_DISABLE_XET':'1','DO_NOT_TRACK':'1',
        'TIKTOKEN_CACHE_DIR':str(root/'tiktoken-cache'),'HF_HOME':str(root/'hf-home'),
        'OMP_NUM_THREADS':str(threads),'MKL_NUM_THREADS':str(threads),
        'TOKENIZERS_PARALLELISM':'false','RAYON_NUM_THREADS':str(threads)}
    before={key:os.environ.get(key) for key in settings}
    try:
        os.environ.update(settings)
        with no_network():
            import torch
            from llmlingua import PromptCompressor
            if torch.version.cuda is not None or '+cpu' not in torch.__version__:
                raise ValueError('CPU-only PyTorch build required')
            torch.set_num_threads(threads)
            if torch.get_num_interop_threads()!=1:torch.set_num_interop_threads(1)
            compressor=PromptCompressor(model_name=environment['checkpoint_path'],use_llmlingua2=True,
                device_map='cpu',model_config={'revision':MODEL_REVISION,'local_files_only':True,
                    'trust_remote_code':False,'use_safetensors':True},llmlingua2_config={'max_batch_size':1})
            if compressor.max_seq_len!=512 or any(p.device.type!='cpu' or p.dtype!=torch.float32 for p in compressor.model.parameters()):
                raise ValueError('Actual model window/device/dtype differs from verified CPU policy')
            compressor.model.eval()
            yield compressor
    finally:
        for key,value in before.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


def compress_validated_cpu(source,counter,config,base,*,on_attempt=None):
    manifest,policy,binding=verified_policy(config,base)
    if not isinstance(getattr(counter,'provenance',None),dict) or counter.provenance!=policy['counter_provenance']:
        raise ValueError('Counter differs from successful CPU preflight convention/artifact')
    started=time.monotonic();events=[]
    with cpu_compressor(manifest,policy) as compressor:
        if any(token in text for token in ['[CLS]','[SEP]','[PAD]','[UNK]','[MASK]',*compressor.added_tokens] for text in source.files.values()):
            raise ValueError('Literal reserved-token collision in source')
        captured=[]
        def observe(module,args,kwargs):
            ids=kwargs['input_ids'].detach().cpu();mask=kwargs['attention_mask'].detach().cpu().bool()
            if ids.shape[0]!=1 or ids.shape[1]>512:raise ValueError('Unreviewed model batch/window')
            captured.append(ids[0][mask[0]].tolist())
        hook=compressor.model.register_forward_pre_hook(observe,with_kwargs=True)
        try:
            def encode(text):
                direct,ids=released_encoding(compressor,text)
                return range(max(direct,len(ids)))
            def compress(text,rate):
                if time.monotonic()-started>=policy['maximum_seconds']:raise TimeoutError('Validated CPU wall-time limit reached')
                direct,expected=released_encoding(compressor,text)
                if max(direct,len(expected))>512:raise ValueError('Input would exceed released window')
                captured.clear();began=time.monotonic()
                response=compressor.compress_prompt(text,rate=rate,**OPTIONS)
                if captured!=[expected]:raise ValueError('Actual model input differs from complete source chunk')
                events.append({'rate':rate,'input_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                    'expected_model_tokens':len(expected),'expected_ids_sha256':canonical_hash(expected),
                    'actual_ids_sha256':canonical_hash(captured[0]),'actual_forward_batches':1,
                    'complete_token_coverage_verified':True,'seconds':time.monotonic()-began})
                return response
            result=llmlingua_compress(source,counter,compress,encode,512,on_attempt=on_attempt)
            if len(events)!=len(result['coverage'])*len(RATES):raise ValueError('Incomplete all-rate source coverage')
        finally:hook.remove()
    result.update(compressor_provenance=binding,forward_coverage=events,
        duration_seconds=time.monotonic()-started,provider_calls=0,
        upstream_savings_field_is_not_actual_cost=True)
    return result
