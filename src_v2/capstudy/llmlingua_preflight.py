"""Local CPU LLMLingua source-only pilot; no task answers, graders or model APIs."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import socket
import time
from .bundles import load_bundle, save_bundle, split_front_matter, validate_candidate
from .compress.llmlingua2 import RATES, llmlingua_compress
from .contracts import canonical_hash, write_json_atomic
from .deepseek_tokenization import (DeepSeekOfflineCounter, OFFICIAL_MODEL,
    OFFICIAL_REVISION, OFFICIAL_TOKENIZER_SHA256)
from .llmlingua_cpu_setup import CODE_REVISION, MODEL, MODEL_REVISION, PINS

ROOT=Path(__file__).resolve().parents[2]
FAMILIES=('weighted-gdp-calculation','organize-messy-files')
OPTIONS={'use_context_level_filter':False,'use_token_level_filter':True,
    'target_token':-1,'force_tokens':['\n'],'force_reserve_digit':False,
    'drop_consecutive':False,'return_word_label':True,'token_to_word':'mean'}


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def no_network():
    """Block Python socket access while loading/inferencing local artifacts."""
    originals={name:getattr(socket,name) for name in ('create_connection','getaddrinfo')}
    methods={name:getattr(socket.socket,name) for name in ('connect','connect_ex')}
    def blocked(*args,**kwargs):raise RuntimeError('Network access prohibited during local compressor inference')
    try:
        for name in originals:setattr(socket,name,blocked)
        for name in methods:setattr(socket.socket,name,blocked)
        yield
    finally:
        for name,value in originals.items():setattr(socket,name,value)
        for name,value in methods.items():setattr(socket.socket,name,value)


def verify_environment(manifest_path):
    manifest_path=Path(manifest_path).resolve(); root=manifest_path.parent
    manifest=json.loads(manifest_path.read_text())
    if (manifest['code_revision'],manifest['checkpoint'],manifest['checkpoint_revision'])!=(CODE_REVISION,MODEL,MODEL_REVISION):
        raise ValueError('Unreviewed code/checkpoint identity')
    checkpoint=Path(manifest['checkpoint_path'])
    if checkpoint.parent!=root or checkpoint.name!=MODEL.split('/')[-1]:
        raise ValueError('Local checkpoint path must retain released BERT identifier')
    for relative,record in manifest['checkpoint_files'].items():
        if sha(checkpoint/relative)!=record['sha256']:raise ValueError('Checkpoint artifact hash mismatch')
    for relative,record in manifest['source_files'].items():
        if sha(root/'source'/relative)!=record['sha256']:raise ValueError('Released source hash mismatch')
        if relative.startswith('llmlingua/') and relative.endswith('.py'):
            installed=importlib.metadata.distribution('llmlingua').locate_file(relative)
            if sha(installed)!=record['sha256']:raise ValueError('Installed released code hash mismatch')
    for pin in PINS+['torch==2.6.0+cpu']:
        name,version=pin.split('==')
        if importlib.metadata.version(name)!=version:raise ValueError('Pinned dependency changed: '+name)
    installed={d.metadata['Name']:d.version for d in importlib.metadata.distributions()}
    if installed!=manifest['runtime']['packages']:raise ValueError('Installed environment differs from prepared package inventory')
    for relative,digest in manifest['tiktoken_cache_files'].items():
        if sha(root/relative)!=digest:raise ValueError('Tokenizer cache changed')
    return manifest


def released_encoding(compressor,text):
    # A forced newline becomes one added token before released chunking. Count
    # that transformation, plus its token->text->token round trip, up front.
    transformed=text.replace('\n',compressor.added_tokens[0])
    original_tokens=compressor.tokenizer.tokenize(transformed)
    normalized=compressor.tokenizer.convert_tokens_to_string(original_tokens)
    tokens=compressor.tokenizer.tokenize(normalized)
    ids=compressor.tokenizer.convert_tokens_to_ids(['[CLS]',*tokens,'[SEP]'])
    return len(original_tokens)+2,ids


def run(manifest_path,output,threads=2,maximum_seconds=1800):
    if platform.system()!='Linux':raise ValueError('Remote Linux-only pilot')
    if type(threads) is not int or not 1<=threads<=2:raise ValueError('Pilot allows one or two CPU threads')
    if type(maximum_seconds) is not int or maximum_seconds<=0:raise ValueError('Positive wall-time bound required')
    output=Path(output).resolve()
    if output.exists():raise ValueError('New persistent pilot directory required')
    output.mkdir(parents=True,mode=0o700)
    manifest_path=Path(manifest_path).resolve();environment=verify_environment(manifest_path)
    environment_root=manifest_path.parent
    os.environ.update({'CUDA_VISIBLE_DEVICES':'','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
        'HF_HUB_DISABLE_TELEMETRY':'1','HF_HUB_DISABLE_XET':'1','DO_NOT_TRACK':'1',
        'TIKTOKEN_CACHE_DIR':str(environment_root/'tiktoken-cache'),
        'HF_HOME':str(environment_root/'hf-home'),'OMP_NUM_THREADS':str(threads),'MKL_NUM_THREADS':str(threads),
        'TOKENIZERS_PARALLELISM':'false','RAYON_NUM_THREADS':str(threads)})
    record={'schema_version':1,'evidence_kind':'source_only_local_compressor_pilot',
        'formal_candidate_claimed':False,'executed_model':False,'provider_calls':0,
        'checkpoint':MODEL,'checkpoint_revision':MODEL_REVISION,'code_revision':CODE_REVISION,
        'environment_manifest_sha256':sha(manifest_path),'threads':threads,'maximum_seconds':maximum_seconds,
        'options':OPTIONS,'rates':list(RATES),'device':'cpu','batch_size':1,'dtype':'float32',
        'chunk_policy':'lossless_character_spans_max_forced_and_roundtrip_BERT_count_512_including_CLS_SEP_v1',
        'network_policy':'local_files_only,trust_remote_code_false,offline_flags,Python_socket_block',
        'upstream_savings_field_is_not_actual_cost':True,'families':[]}
    record['implementation_sha256']={relative:sha(ROOT/relative) for relative in (
        'src/capstudy/llmlingua_preflight.py','src/capstudy/compress/llmlingua2.py',
        'src/capstudy/bundles.py','src/capstudy/tokenization.py','src/capstudy/deepseek_tokenization.py')}
    record['host_runtime']={'platform':platform.platform(),'python':platform.python_version()}
    write_json_atomic(output/'pending.json',record)
    started=time.monotonic()
    try:
        with no_network():
            import torch
            from llmlingua import PromptCompressor
            if torch.version.cuda is not None or '+cpu' not in torch.__version__:
                raise ValueError('CPU-only PyTorch build required')
            torch.set_num_threads(threads);torch.set_num_interop_threads(1)
            compressor=PromptCompressor(model_name=environment['checkpoint_path'],use_llmlingua2=True,
                device_map='cpu',model_config={'revision':MODEL_REVISION,'local_files_only':True,
                    'trust_remote_code':False,'use_safetensors':True},llmlingua2_config={'max_batch_size':1})
            if compressor.max_seq_len!=512 or any(p.device.type!='cpu' for p in compressor.model.parameters()):
                raise ValueError('Unexpected released input window/device')
            if any(p.dtype!=torch.float32 for p in compressor.model.parameters()):
                raise ValueError('Pilot requires actual float32 model parameters')
            compressor.model.eval()
            counter=DeepSeekOfflineCounter(ROOT/'private_artifacts/external/deepseek-tokenizer/tokenizer.json',
                model_id=OFFICIAL_MODEL,revision=OFFICIAL_REVISION,expected_sha256=OFFICIAL_TOKENIZER_SHA256,
                expected_runtime_version='0.22.2')
            record.update(executed_model=True,parameter_count=sum(p.numel() for p in compressor.model.parameters()),
                model_max_position_embeddings=compressor.max_position_embeddings,tokenizer_class=type(compressor.tokenizer).__name__,
                counter_provenance=counter.provenance)
            captured=[]
            def observe(module,args,kwargs):
                ids=kwargs['input_ids'].detach().cpu();mask=kwargs['attention_mask'].detach().cpu().bool()
                if ids.shape[0]!=1 or ids.shape[1]>512:raise ValueError('Unreviewed model batch/window')
                captured.append(ids[0][mask[0]].tolist())
            hook=compressor.model.register_forward_pre_hook(observe,with_kwargs=True)
            try:
                for family in FAMILIES:
                    source=load_bundle(ROOT/'milestone 1/data/shared/skills/human_authored'/family)
                    folder=output/family;folder.mkdir()
                    if any(token in text for token in ['[CLS]','[SEP]','[PAD]','[UNK]','[MASK]',*compressor.added_tokens] for text in source.files.values()):
                        raise ValueError('Literal reserved-token collision in source')
                    events=[]
                    def encode(text):
                        direct,ids=released_encoding(compressor,text)
                        return range(max(direct,len(ids)))
                    def compress(text,rate):
                        if time.monotonic()-started>=maximum_seconds:raise TimeoutError('CPU pilot wall limit reached')
                        direct,expected=released_encoding(compressor,text)
                        if max(direct,len(expected))>512:raise ValueError('Input would exceed released window')
                        captured.clear();began=time.monotonic()
                        response=compressor.compress_prompt(text,rate=rate,**OPTIONS)
                        if captured!=[expected]:raise ValueError('Actual model token IDs differ from complete expected input; possible truncation')
                        event={'rate':rate,'input_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                            'expected_model_tokens':len(expected),'expected_ids_sha256':canonical_hash(expected),
                            'actual_forward_batches':len(captured),'actual_ids_sha256':canonical_hash(captured[0]),
                            'complete_token_coverage_verified':True,'seconds':time.monotonic()-began}
                        events.append(event)
                        with (folder/'forward_coverage.jsonl').open('a') as handle:handle.write(json.dumps(event,sort_keys=True)+'\n')
                        return response
                    def persist(index,attempt):
                        write_json_atomic(folder/('rate-'+format(attempt['rate'],'.2f')+'.json'),attempt)
                        print(json.dumps({'family':family,'rate':attempt['rate'],'tokens':attempt['candidate_tokens'],
                            'eligible':attempt['eligible'],'reasons':attempt['ineligibility_reasons']}),flush=True)
                    result=llmlingua_compress(source,counter,compress,encode,512,on_attempt=persist)
                    if len(events)!=len(result['coverage'])*len(RATES):raise ValueError('Not every source chunk ran at every declared rate')
                    for path,text in source.files.items():
                        _,body=split_front_matter(text)
                        chunks=[c for c in result['coverage'] if c['path']==path]
                        if ''.join(c['text'] for c in chunks)!=body or (chunks and chunks[-1]['end']!=len(body)):
                            raise ValueError('Source body coverage lost its tail')
                    if result['eligible']:
                        bundle=save_bundle(source,result['files'],folder/'pilot-selected-bundle')
                        validate=validate_candidate(source,result['files'],counter,result['token_cap'],bundle)
                        if not validate['eligible']:raise ValueError('Saved bundle verification disagrees')
                    write_json_atomic(folder/'result.json',result)
                    row={key:result[key] for key in ('selected_rate','eligible','ineligibility_reasons','candidate_tokens','original_tokens','token_cap')}
                    row.update(family=family,source_hashes=source.hashes(),body_chunk_count=len(result['coverage']),
                        source_characters=sum(len(v) for v in source.files.values()),model_forward_batches=len(events),
                        full_source_coverage_verified=True,all_declared_rates_completed=True,
                        result_sha256=sha(folder/'result.json'),rate_results=[{k:a[k] for k in ('rate','candidate_tokens','eligible','ineligibility_reasons','duration_seconds')} for a in result['attempts']])
                    record['families'].append(row);write_json_atomic(output/'progress.json',record)
            finally:hook.remove()
        record['completed']=True
    except BaseException as exc:
        record.update(completed=False,error_category=type(exc).__name__,error_reason=str(exc))
        raise
    finally:
        record.update(duration_seconds=time.monotonic()-started,peak_resident_memory_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        write_json_atomic(output/'result.json',record)
    return record


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--environment-manifest',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--execute',action='store_true')
    p.add_argument('--threads',type=int,default=2);p.add_argument('--maximum-seconds',type=int,default=1800)
    a=p.parse_args(argv)
    if not a.execute:print(json.dumps({'executed':False,'families':FAMILIES,'rates':RATES,'options':OPTIONS}));return
    result=run(a.environment_manifest,a.output,a.threads,a.maximum_seconds)
    print(json.dumps({'completed':result['completed'],'families':len(result['families']),'output':str(a.output)}))

if __name__=='__main__':main()
