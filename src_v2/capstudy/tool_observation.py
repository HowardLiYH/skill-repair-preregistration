"""Explicit, audit-bound text-only observations for the prospective v2 runtime."""
import copy
import hashlib
import json
import os
from pathlib import Path

POLICY = 'explicit-text-only-tool-result-error-v2'
MAX_BYTES = 1_048_576
NOTICE = ('Text-only executor cannot consume document/image tool output. '
          'The original binary result was retained in the private audit record and '
          'was not sent to the model. Use available local tools to extract text; '
          'if extraction is unavailable or loses required information, report that limitation.')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def adapt(raw):
    """Return (adapted_bytes, change_records), without mutating input or side effects."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Bounded bytes request required')
    body = json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(body, dict) or not isinstance(body.get('messages'), list):
        raise ValueError('Request messages required')
    changed = copy.deepcopy(body)
    changes = []
    for mi, message in enumerate(changed['messages']):
        if not isinstance(message, dict):
            raise ValueError('Malformed message')
        content = message.get('content')
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise ValueError('Malformed message content')
        for bi, block in enumerate(content):
            if not isinstance(block, dict):
                raise ValueError('Malformed block')
            kind = block.get('type')
            if kind in ('document', 'image'):
                raise ValueError('Only local tool-result binary blocks are adaptable')
            if kind != 'tool_result':
                if kind not in ('text', 'tool_use'):
                    raise ValueError('Unknown content type')
                continue
            if message.get('role') != 'user' or not isinstance(block.get('tool_use_id'), str) or not block['tool_use_id']:
                raise ValueError('User tool-result identity required')
            nested = block.get('content', '')
            if isinstance(nested, str):
                continue
            if not isinstance(nested, list):
                raise ValueError('Malformed tool-result content')
            binary = []
            for ni, item in enumerate(nested):
                if not isinstance(item, dict):
                    raise ValueError('Malformed nested block')
                nk = item.get('type')
                if nk == 'text' and isinstance(item.get('text'), str):
                    continue
                if nk not in ('document', 'image') or not isinstance(item.get('source'), dict):
                    raise ValueError('Unknown or malformed nested content')
                binary.append((ni, nk))
            if binary:
                # Retain surrounding text and relative block order. Every binary
                # block is replaced visibly, never silently removed or decoded.
                for ni, nk in binary:
                    nested[ni] = {'type': 'text', 'text': NOTICE + ' Omitted block type: ' + nk + '.'}
                block['is_error'] = True
                changes.append({'message_index': mi, 'content_index': bi,
                                'tool_use_id': block['tool_use_id'],
                                'replaced_blocks': [{'index': ni, 'type': nk} for ni, nk in binary]})
    if not changes:
        return raw, []
    encoded = json.dumps(changed, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError('Adapted request exceeds byte cap')
    return encoded, changes


def policy_descriptor():
    return {'policy': POLICY, 'implementation_sha256': digest(Path(__file__).read_bytes())}


def validate_policy(policy):
    if policy != policy_descriptor():
        raise ValueError('Frozen tool-observation policy or implementation differs')
    return dict(policy)


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Bounded regular observation file required')
    return path.read_bytes()


def _write(path, data):
    if len(data) > MAX_BYTES:
        raise ValueError('Observation file exceeds cap')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if _read(path) != data:
        raise OSError('Observation evidence readback differs')


def preserve(raw, adapted, provider_encoded, changes, root, policy):
    """Called inside shared disk admission BEFORE any ledger claim or dispatch.

    Per-wire-input directory; exact replays validate and reuse immutable bytes.
    Different originals with the same provider payload keep distinct provenance.
    Text identity aliases original/adapted storage, without losing either hash.
    """
    validate_policy(policy)
    root = Path(root).absolute()
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise ValueError('Symlink in observation path')
    root.mkdir(mode=0o700, exist_ok=True)
    out = root / digest(raw)
    payloads = {'original.json': raw}
    adapted_name = 'original.json' if adapted == raw else 'adapted.json'
    payloads[adapted_name] = adapted
    provider_name = next((name for name, data in payloads.items() if data == provider_encoded), 'provider.json')
    payloads[provider_name] = provider_encoded
    metadata = {'policy': dict(policy), 'original': {'file': 'original.json', 'sha256': digest(raw)},
                'adapted': {'file': adapted_name, 'sha256': digest(adapted)},
                'provider': {'file': provider_name, 'sha256': digest(provider_encoded)},
                'changes': changes}
    meta = json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode()
    if len(meta) > MAX_BYTES:
        raise ValueError('Observation metadata exceeds cap')
    if out.exists():
        if out.is_symlink() or not out.is_dir() or _read(out/'manifest.json') != meta:
            raise ValueError('Incomplete or changed observation record; no automatic recovery')
        if {p.name for p in out.iterdir()} != set(payloads) | {'manifest.json'}:
            raise ValueError('Observation inventory differs')
        for name, data in payloads.items():
            if _read(out/name) != data:
                raise ValueError('Replayed observation bytes differ')
    else:
        out.mkdir(mode=0o700)
        for name, data in payloads.items():
            _write(out/name, data)
        _write(out/'manifest.json', meta)
        for directory in (out, root):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return {'path': str(out/'manifest.json'), 'sha256': digest(meta),
            'original_sha256': digest(raw), 'adapted_sha256': digest(adapted),
            'provider_sha256': digest(provider_encoded), 'policy': dict(policy)}


def verify(reference, *, root, policy, provider_body, allowed_tools):
    """Recompute transformation and exact normalized provider bytes independently."""
    from .deepseek_gateway import prepare_request
    validate_policy(policy)
    if not isinstance(reference, dict) or reference.get('policy') != policy:
        raise ValueError('Observation reference/policy missing')
    path = Path(reference.get('path', ''))
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('Unsafe observation reference')
    if (path.parent.parent != Path(root).absolute() or path.name != 'manifest.json'
            or path.parent.name != reference.get('original_sha256')):
        raise ValueError('Observation record outside exact request root')
    meta_bytes = _read(path)
    if digest(meta_bytes) != reference.get('sha256'):
        raise ValueError('Observation manifest hash differs')
    meta = json.loads(meta_bytes)
    if set(meta) != {'policy', 'original', 'adapted', 'provider', 'changes'} or meta['policy'] != policy:
        raise ValueError('Observation manifest shape/policy differs')
    values = {}
    for key in ('original', 'adapted', 'provider'):
        row = meta[key]
        if set(row) != {'file', 'sha256'} or row['file'] not in ('original.json','adapted.json','provider.json'):
            raise ValueError('Unexpected observation filename')
        data = _read(path.parent/row['file'])
        if digest(data) != row['sha256'] or row['sha256'] != reference.get(key+'_sha256'):
            raise ValueError('Observation payload hash differs')
        values[key] = data
    adapted, changes = adapt(values['original'])
    normalized, encoded = prepare_request(adapted, allowed_tools=allowed_tools)
    if (adapted != values['adapted'] or changes != meta['changes']
            or encoded != values['provider'] or normalized != provider_body):
        raise ValueError('Observation transformation or provider input differs')
    return dict(reference)
