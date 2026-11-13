"""Pinned offline DeepSeek tokenizer; visible file counts, not API/billing counts.

No weights, network, credential reads, transformers, or remote Python execution.
This alternative convention requires an explicit pre-calibration protocol decision.
"""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
from pathlib import Path
from .contracts import canonical_hash, write_json_atomic
from .tokenization import CountUnavailable

OFFICIAL_MODEL = 'deepseek-ai/DeepSeek-V4.1-Flash-1017'
OFFICIAL_REVISION = '1bae0e1088993b9b82de08f59de5be5a34e42170'
OFFICIAL_TOKENIZER_SHA256 = 'c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b'

class DeepSeekOfflineCounter:
    tokenizer_id = 'deepseek-official-local-tokenizer'
    convention = 'sum_per_complete_utf8_skill_file_no_added_bos_eos_no_chat_wrapper_v1'
    estimates_not_billing = True
    provider_counted = False

    def __init__(self, tokenizer_path, *, model_id, revision, expected_sha256,
                 expected_runtime_version, cache_dir=None, _fixture_backend=None):
        if not all((model_id,revision,expected_sha256,expected_runtime_version)):
            raise ValueError('Explicit model, revision, file digest, and runtime version required')
        path=Path(tokenizer_path)
        if path.is_symlink() or not path.is_file():raise CountUnavailable('Tokenizer must be a regular pinned file')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise CountUnavailable('Tokenizer artifact hash mismatch')
        parsed=json.loads(raw)
        if parsed.get('truncation') is not None or parsed.get('padding') is not None:
            raise CountUnavailable('Pinned tokenizer must not truncate or pad')
        self.evidence_kind='fixture' if _fixture_backend is not None else 'offline_tokenizer_measurement'
        if _fixture_backend is None:
            if (model_id,revision,expected_sha256)!=(OFFICIAL_MODEL,OFFICIAL_REVISION,OFFICIAL_TOKENIZER_SHA256):
                raise CountUnavailable('Unverified DeepSeek model/revision/artifact association')
            try:
                runtime_version=importlib.metadata.version('tokenizers')
                from tokenizers import Tokenizer
            except (ImportError,importlib.metadata.PackageNotFoundError) as e:
                raise CountUnavailable('Install the explicitly pinned tokenizers runtime') from e
            if runtime_version!=expected_runtime_version:raise CountUnavailable('Tokenizer runtime version mismatch')
            # Loading a JSON tokenizer executes no downloaded Python and cannot fetch weights.
            self._tokenizer=Tokenizer.from_str(raw.decode('utf-8'))
        else:
            runtime_version=_fixture_backend.runtime_version
            if runtime_version!=expected_runtime_version:raise CountUnavailable('Fixture runtime version mismatch')
            self._tokenizer=_fixture_backend
        self.model_id=model_id
        self.model_revision=revision
        self.revision=f'{revision}:{expected_sha256}:tokenizers-{runtime_version}'
        self.cache_dir=Path(cache_dir) if cache_dir else None
        self.provenance={'tokenizer_id':self.tokenizer_id,'model_id':model_id,'model_revision':revision,
                         'tokenizer_sha256':expected_sha256,'runtime':'tokenizers','runtime_version':runtime_version,
                         'convention':self.convention,'provider_counted':False,'estimates_not_billing':True,
                         'api_deployment_equivalence':'not_verified','evidence_kind':self.evidence_kind}

    def count_file(self,text):
        if not isinstance(text,str):raise TypeError('count_file expects complete UTF-8-decoded text')
        encoded=text.encode('utf-8')
        identity={**self.provenance,'text_sha256':hashlib.sha256(encoded).hexdigest(),'utf8_bytes':len(encoded)}
        key=canonical_hash(identity)
        path=self.cache_dir/(key+'.json') if self.cache_dir else None
        if path and path.exists():
            record=json.loads(path.read_text())
            if record.get('identity')!=identity or record.get('record_sha256')!=canonical_hash({k:v for k,v in record.items() if k!='record_sha256'}):
                raise CountUnavailable('Offline count-cache identity/integrity mismatch')
            count=record.get('tokens')
            if type(count) is not int or count<0:raise CountUnavailable('Invalid cached count')
            return count
        ids=self._tokenizer.encode(text,add_special_tokens=False).ids
        if any(type(token) is not int or token<0 for token in ids):raise CountUnavailable('Invalid tokenizer output')
        count=len(ids)
        if path:
            record={'identity':identity,'tokens':count,'token_ids_sha256':canonical_hash(ids)}
            record['record_sha256']=canonical_hash(record)
            write_json_atomic(path,record)
        return count

    __call__=count_file
