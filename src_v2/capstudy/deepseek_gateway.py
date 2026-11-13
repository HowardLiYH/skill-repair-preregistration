"""Budgeted Anthropic-protocol relay core for the scoped DeepSeek pilot.

No command-line launcher, credential discovery, or automatic model dispatch.
The orchestrator must supply the ONE existing campaign Journal, a private
artifact directory outside the solver, and a trial-scoped relay token. Real
credentials belong only in OfficialTransport, never in the solver environment.
Responses are buffered and accounted before being released to the client.
"""
from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
from decimal import Decimal
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.request

from .contracts import canonical_hash, write_json_atomic
from .journal import AdmissionBlocked, Journal

UPSTREAM_URL = 'https://api.deepseek.com/anthropic/v1/messages'
MODEL = 'deepseek-flash'
MODEL_VERSIONS = {MODEL, 'DeepSeek-V4.1-Flash-1017'}
MAX_INPUT_TOKENS = 1_048_576  # Conservative ceiling above published 1M context.
MAX_OUTPUT_TOKENS = 16_384
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 16 * 1_048_576
RESERVATION_USD = Decimal('0.3342336')
PRICING_URL = 'https://api-docs.deepseek.com/quick_start/pricing/'
DEFAULT_TOOLS = frozenset({'Bash', 'Read', 'Write', 'Edit', 'MultiEdit', 'Glob',
                           'Grep', 'LS', 'NotebookEdit', 'NotebookRead',
                           'TodoRead', 'TodoWrite', 'Skill'})


class RequestRejected(ValueError):
    """Known rejection before a provider call; no reservation is needed."""


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: dict
    body: bytes
    truncated: bool = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OfficialTransport:
    """One HTTP request, default TLS verification, no redirects/proxies/retries."""
    def __init__(self, api_key: str, *, timeout_seconds=120):
        if not isinstance(api_key, str) or not api_key or any(c.isspace() for c in api_key):
            raise ValueError('A valid upstream credential is required')
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def __call__(self, body: bytes) -> UpstreamResponse:
        request = urllib.request.Request(UPSTREAM_URL, data=body, method='POST', headers={
            'Authorization': 'Bearer ' + self._api_key,
            'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'})
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as error:
            # Includes redirects. Preserve its status/body; never follow Location.
            response = error
        with response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            return UpstreamResponse(response.status, dict(response.headers),
                                    raw[:MAX_RESPONSE_BYTES], len(raw) > MAX_RESPONSE_BYTES)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequestRejected('Duplicate JSON fields are prohibited')
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(RequestRejected('Nonfinite JSON')))
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise RequestRejected('Malformed JSON') from error


def _content(value, *, text_only=False):
    if isinstance(value, str):
        return
    if not isinstance(value, list):
        raise RequestRejected('Content must be text or text/tool blocks')
    for block in value:
        if not isinstance(block, dict):
            raise RequestRejected('Malformed content block')
        kind = block.get('type')
        if text_only and kind != 'text':
            raise RequestRejected('System content must contain only text')
        if kind == 'text':
            if not isinstance(block.get('text'), str):
                raise RequestRejected('Text block requires text')
        elif kind == 'tool_use':
            if not isinstance(block.get('input'), dict):
                raise RequestRejected('Tool input must be an object')
        elif kind == 'tool_result':
            _content(block.get('content', ''))
        else:
            raise RequestRejected('Only text and local tool blocks are allowed')


def prepare_request(raw: bytes, *, allowed_tools=DEFAULT_TOOLS):
    if not set(allowed_tools) <= DEFAULT_TOOLS:
        raise RequestRejected('Tool policy cannot enable network or subagent tools')
    if not isinstance(raw, bytes) or len(raw) > MAX_REQUEST_BYTES:
        raise RequestRejected('Request exceeds byte limit')
    body = _json(raw)
    allowed = {'model', 'max_tokens', 'messages', 'system', 'tools', 'tool_choice',
               'stream', 'temperature', 'top_p', 'top_k', 'stop_sequences',
               'metadata', 'thinking', 'output_config'}
    if not isinstance(body, dict) or set(body) - allowed:
        raise RequestRejected('Unsupported request fields')
    if body.get('model') != MODEL:
        raise RequestRejected('Only the explicit deepseek-flash model is permitted')
    requested = body.get('max_tokens')
    if type(requested) is not int or requested <= 0:
        raise RequestRejected('Positive integer max_tokens is required')
    if 'stream' in body and type(body['stream']) is not bool:
        raise RequestRejected('stream must be boolean')
    messages = body.get('messages')
    if not isinstance(messages, list) or not messages:
        raise RequestRejected('Messages are required')
    for message in messages:
        # DeepSeek explicitly supports interleaved system messages on /messages:
        # https://api-docs.deepseek.com/guides/tool_calls/
        # Keep their role, content and order intact; do not flatten into system.
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant', 'system'):
            raise RequestRejected('Only user/assistant/system messages are permitted')
        if set(message) - {'role', 'content'}:
            raise RequestRejected('Unsupported message fields')
        _content(message.get('content'), text_only=message['role'] == 'system')
    _content(body.get('system', ''), text_only=True)
    tool_list = body.get('tools', [])
    if not isinstance(tool_list, list):
        raise RequestRejected('Tools must be a list')
    for tool in tool_list:
        if (not isinstance(tool, dict) or tool.get('name') not in allowed_tools
                or set(tool) - {'name', 'description', 'input_schema', 'cache_control'}
                or not isinstance(tool.get('input_schema'), dict)):
            raise RequestRejected('Only allowlisted local tool definitions are permitted')
    # Never rely on Claude's thinking-disable setting: third-party requests may
    # simply omit thinking, whereas DeepSeek's default is thinking enabled.
    body['thinking'] = {'type': 'disabled'}
    body['max_tokens'] = min(requested, MAX_OUTPUT_TOKENS)
    body.pop('output_config', None)  # Cannot override thinking/effort at provider.
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise RequestRejected('Normalized request exceeds byte limit')
    return body, encoded


def _account(usage, output_cap):
    if not isinstance(usage, dict):
        raise ValueError('Missing usage')
    # Unlike chat-completion prompt_tokens, this route reports uncached input
    # separately from cache reads. An absent read count is not a known zero.
    required = ('input_tokens', 'output_tokens', 'cache_read_input_tokens')
    if any(key not in usage for key in required):
        raise ValueError('Final usage lacks token counts')
    counts = {key: usage.get(key, 0) for key in
              ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')}
    if any(type(v) is not int or v < 0 for v in counts.values()):
        raise ValueError('Invalid token counts')
    if (counts['cache_creation_input_tokens'] != 0
            or counts['input_tokens'] + counts['cache_read_input_tokens'] > MAX_INPUT_TOKENS
            or counts['output_tokens'] > output_cap):
        raise ValueError('Usage exceeds admitted bounds or has unpriced categories')
    if usage.get('service_tier', 'standard') not in ('standard', None):
        raise ValueError('Unexpected service tier requires review')
    if any(key not in {*counts, 'service_tier'} and value not in (None, 0, {})
           for key, value in usage.items()):
        raise ValueError('Unexpected usage category requires review')
    return (Decimal(counts['input_tokens']) * Decimal('.30')
            + Decimal(counts['cache_read_input_tokens']) * Decimal('.006')
            + Decimal(counts['output_tokens']) * Decimal('1.20')) / Decimal(1_000_000)


def response_accounting(response: UpstreamResponse, *, streaming, output_cap):
    """Return measured-usage peak-tariff upper charge; missing evidence is unknown."""
    if response.status != 200 or response.truncated:
        raise ValueError('Non-success or incomplete response needs reconciliation')
    if not streaming:
        obj = _json(response.body)
        if obj.get('type') != 'message' or obj.get('model') not in MODEL_VERSIONS:
            raise ValueError('Unexpected provider message/model')
        _content(obj.get('content', []))
        return _account(obj.get('usage'), output_cap), obj.get('id'), obj.get('usage')
    # SSE final message_delta usage is cumulative. Do NOT sum message_start
    # input counts, which double-counts inputs on the DeepSeek endpoint.
    events = []
    for block in response.body.decode('utf-8').replace('\r\n', '\n').split('\n\n'):
        data = '\n'.join(line[5:].lstrip() for line in block.split('\n') if line.startswith('data:'))
        if data:
            events.append(_json(data))
    starts = [e for e in events if e.get('type') == 'message_start']
    stops = [e for e in events if e.get('type') == 'message_stop']
    deltas = [e for e in events if e.get('type') == 'message_delta' and 'usage' in e]
    if (len(starts) != 1 or len(stops) != 1 or not deltas
            or events[-1].get('type') != 'message_stop'
            or starts[0].get('message', {}).get('model') not in MODEL_VERSIONS):
        raise ValueError('Incomplete or unrecognized provider stream')
    for event in events:
        if event.get('type') == 'error':
            raise ValueError('Provider stream error')
        if event.get('type') == 'content_block_start':
            _content([event.get('content_block')])
    usage = deltas[-1]['usage']
    return _account(usage, output_cap), starts[0]['message'].get('id'), usage


class BudgetedGateway:
    def __init__(self, journal: Journal, artifact_root, transport, *, relay_token,
                 trial_id, allowed_tools=DEFAULT_TOOLS, execution_binding=None, tool_observation_policy=None):
        if not isinstance(relay_token, str) or len(relay_token) < 32:
            raise ValueError('A private trial token of at least 32 characters is required')
        self.journal, self.transport = journal, transport
        self.artifact_root = Path(artifact_root)
        self.execution_binding=copy.deepcopy(execution_binding)
        self.tool_observation_policy=copy.deepcopy(tool_observation_policy)
        self.formal_output_cap=None
        if self.execution_binding is not None:
            from .formal_calibration_gateway import validate_execution_binding,register_trial_binding
            validate_execution_binding(self.execution_binding,journal=journal,transport=transport,relay_token=relay_token)
            if trial_id!=self.execution_binding['physical_trial_id']:raise ValueError('Formal physical trial identity differs')
            runtime=json.loads(Path(self.execution_binding['trial_config_path']).read_text())['runtime']
            mapping=runtime['deepseek_mapping']
            configured=runtime.get('tool_observation')
            if self.tool_observation_policy is not None and self.tool_observation_policy!=configured:
                raise ValueError('Formal observation policy differs')
            self.tool_observation_policy=configured
            if set(allowed_tools)!=set(mapping['allowed_tools']):raise ValueError('Formal gateway tools differ from frozen policy')
            self.formal_output_cap=mapping['max_output_tokens']
            marker=self.artifact_root/'formal_binding.json'
            if self.artifact_root.exists() and (not marker.is_file() or json.loads(marker.read_text())!=self.execution_binding):
                raise AdmissionBlocked('Formal artifact root cannot import pilot or another physical trial')
            from .formal_calibration_gateway import formal_dispatch_storage
            disk=formal_dispatch_storage(self.execution_binding,self.artifact_root)
            if disk is not None:
                from .dispatch_storage import prepare_storage_root
                prepare_storage_root(disk)
            self.formal_registration=register_trial_binding(journal,self.execution_binding,relay_token,self.artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifact_root.chmod(0o700)
        if self.execution_binding is not None:
            marker=self.artifact_root/'formal_binding.json'
            if not marker.exists():write_json_atomic(marker,self.execution_binding)
        self.relay_token, self.trial_id = relay_token, trial_id
        if not set(allowed_tools) <= DEFAULT_TOOLS:
            raise ValueError('Tool policy cannot enable network or subagent tools')
        self.allowed_tools = allowed_tools
        if self.tool_observation_policy is not None:
            from .tool_observation import validate_policy
            validate_policy(self.tool_observation_policy)

    def handle(self, path, raw, *, authorization):
        if self.execution_binding is not None:
            from .formal_calibration_gateway import formal_dispatch_storage
            disk=formal_dispatch_storage(self.execution_binding,self.artifact_root)
            if disk is not None:
                from .dispatch_storage import dispatch_storage_guard
                with dispatch_storage_guard(disk,evidence_kind=self.execution_binding['evidence_kind'],artifact_root=self.artifact_root):
                    return self._handle(path,raw,authorization=authorization)
        return self._handle(path,raw,authorization=authorization)

    def _handle(self, path, raw, *, authorization):
        # Exact beta query observed from pinned Claude CLI 2.1.296 in the offline
        # capture. It never changes the fixed upstream URL or enables new fields.
        if path not in ('/v1/messages', '/anthropic/v1/messages', '/v1/messages?beta=true'):
            raise RequestRejected('Only the messages endpoint is enabled')
        if not isinstance(authorization, str) or not hmac.compare_digest(
                authorization, 'Bearer ' + self.relay_token):
            raise RequestRejected('Invalid relay token')
        allowed_tools=self.allowed_tools
        if self.execution_binding is not None:
            from .formal_calibration_gateway import validate_execution_binding,register_trial_binding
            validate_execution_binding(self.execution_binding,journal=self.journal,transport=self.transport,relay_token=self.relay_token)
            self.formal_registration=register_trial_binding(self.journal,self.execution_binding,self.relay_token,self.artifact_root)
            if json.loads((self.artifact_root/'formal_binding.json').read_text())!=self.execution_binding:
                raise AdmissionBlocked('Formal artifact binding changed')
            mapping=json.loads(Path(self.execution_binding['trial_config_path']).read_text())['runtime']['deepseek_mapping']
            if canonical_hash(mapping)!=self.execution_binding['mapping_sha256']:
                raise AdmissionBlocked('Frozen formal mapping changed')
            allowed_tools=mapping['allowed_tools']
        observation=None
        observed_raw=raw
        if self.tool_observation_policy is not None:
            from .tool_observation import adapt,validate_policy
            validate_policy(self.tool_observation_policy)
            raw,changes=adapt(raw)
        body, encoded = prepare_request(raw, allowed_tools=allowed_tools)
        if self.execution_binding is not None and body['max_tokens']>mapping['max_output_tokens']:
            raise RequestRejected('Request exceeds frozen formal output policy')
        if self.tool_observation_policy is not None:
            from .tool_observation import preserve
            try:
                observation=preserve(observed_raw,raw,encoded,changes,self.artifact_root/'observations',self.tool_observation_policy)
            except (ValueError,OSError):
                raise AdmissionBlocked('Observation evidence incomplete; no dispatch') from None
        request_hash = canonical_hash(body)
        identity = ({'phase': 'executor_pilot_gateway', 'trial_id': self.trial_id,
                    'request_sha256': request_hash} if self.execution_binding is None else
                    {'phase':self.execution_binding['phase'],'trial_id':self.trial_id,
                     'request_sha256':request_hash,'execution_binding':self.execution_binding})
        if self.tool_observation_policy is not None:
            identity['tool_observation_policy']=self.tool_observation_policy
        job = self.journal.schedule_and_claim(identity, body, RESERVATION_USD,
                                             worker='deepseek-gateway', reuse_completed=True)
        if job['state'] == 'completed':
            # Client transport retries must not buy another generation. The same
            # normalized request within one trial reuses exact accounted bytes.
            # A legitimate independent trial receives a distinct trial_id.
            record = self.journal.completed_response(job['job_id'])
            if record.get('trial_id') != self.trial_id or record.get('request_sha256') != request_hash:
                raise AdmissionBlocked('Saved gateway response identity mismatch')
            if self.execution_binding is not None and record.get('execution_binding')!=self.execution_binding:
                raise AdmissionBlocked('Saved formal gateway binding differs')
            if observation is not None:
                from .tool_observation import verify
                verify(record.get('tool_observation'),root=self.artifact_root/'observations',
                       policy=self.tool_observation_policy,provider_body=body,allowed_tools=allowed_tools)
            try:
                saved = UpstreamResponse(record['status'], record['headers'],
                    base64.b64decode(record['body_base64'], validate=True), record['truncated'])
                charge, _, _ = response_accounting(saved, streaming=body.get('stream', False),
                                                   output_cap=body['max_tokens'])
                if charge != Decimal(record['accounted_upper_charge_usd']) or charge != Decimal(job['charge']):
                    raise ValueError('Saved accounting changed')
            except (ValueError, TypeError, KeyError):
                raise AdmissionBlocked('Saved gateway response/accounting invalid') from None
            self.journal.event(job['job_id'], 'gateway_response_reused',
                               {'trial_id': self.trial_id, 'request_sha256': request_hash,
                                'additional_dispatch': False, **({'tool_observation':observation} if observation else {})})
            return UpstreamResponse(saved.status, {'content-type':
                'text/event-stream' if body.get('stream') else 'application/json'}, saved.body)
        directory = self.artifact_root / job['job_id']
        dispatched = False
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
            write_json_atomic(directory / 'request.json', body)
            if observation is not None:
                self.journal.event(job['job_id'],'gateway_tool_observation',observation)
            dispatched = True
            response = self.transport(encoded)  # Exactly one physical dispatch.
            if not isinstance(response, UpstreamResponse):
                raise TypeError('Invalid transport response')
            kept_headers = {key.lower(): value for key, value in response.headers.items()
                            if key.lower() in ('content-type', 'request-id', 'x-request-id')}
            record = {'schema_version': 1, 'job_id': job['job_id'], 'trial_id': self.trial_id,
                      'request_sha256': canonical_hash(body), 'status': response.status,
                      'headers': kept_headers, 'body_base64': base64.b64encode(response.body).decode(),
                      'truncated': response.truncated, 'actual_invoice_charge_usd': None,
                      'accounting_basis': 'observed_usage_at_published_peak_tariff_upper_bound',
                      'pricing_url': PRICING_URL, 'pricing_date': '2026-11-06'}
            if observation is not None:record['tool_observation']=observation
            if self.execution_binding is not None:record['execution_binding']=self.execution_binding
            try:
                charge, provider_id, usage = response_accounting(
                    response, streaming=body.get('stream', False), output_cap=body['max_tokens'])
                record.update(accounted_upper_charge_usd=str(charge), usage=usage)
            except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                charge, provider_id = None, kept_headers.get('x-request-id', kept_headers.get('request-id'))
                record.update(accounted_upper_charge_usd=None, usage_validation='unresolved')
            path = directory / 'response.json'
            write_json_atomic(path, record)
            self.journal.complete(job['job_id'], path, charge, provider_request_id=provider_id)
        except BaseException as error:
            # Do not serialize exception text: a transport/library error could
            # include credential headers. Unknown failures retain reservation.
            # A disk failure can prevent file evidence, but the durable journal
            # still records known no-dispatch versus an uncertain remote call.
            try:
                incident_path = directory / 'incident.json'
                if incident_path.exists():
                    incident_path = directory / ('incident-' + str(len(list(directory.glob('incident*.json')))) + '.json')
                write_json_atomic(incident_path, {'error_type': type(error).__name__,
                    'dispatch_outcome': 'unknown' if dispatched else 'not_dispatched', 'automatic_retry': False})
            except OSError:
                pass
            if self.journal.get(job['job_id'])['state'] == 'running':
                self.journal.incident(job['job_id'], 'Gateway/transport failure; inspect private evidence',
                                      confirmed_not_dispatched=not dispatched)
            raise AdmissionBlocked('Gateway dispatch uncertain; explicit reconciliation required') from None
        if charge is None:
            raise AdmissionBlocked('Provider usage unresolved; response retained privately; dispatch blocked')
        # Never reflect authorization, provider cookies, redirect headers, etc.
        return UpstreamResponse(response.status, {'content-type':
            'text/event-stream' if body.get('stream') else 'application/json'}, response.body)


def validate_listener_address(gateway,address):
    if gateway.execution_binding is not None and (address[0]!='127.0.0.1' or type(address[1]) is not int or not 0<=address[1]<=65535):
        raise ValueError('Formal relay listener must use IPv4 loopback')


def make_server(gateway: BudgetedGateway, address=('127.0.0.1', 0)):
    """Construct (do not start) a single-threaded relay; lifecycle is external.

    Integration must enforce network isolation: solver egress ONLY to this
    relay, no provider key, no Docker socket and no evaluator/oracle mounts.
    """
    validate_listener_address(gateway,address)
    class LocalHTTPServer(HTTPServer):
        def server_bind(self):
            # HTTPServer normally reverse-resolves the bind address. This
            # loopback relay needs no DNS and must start on offline hosts too.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *_args):
            pass  # Never log request credentials or benchmark text to console.

        def do_POST(self):
            self.connection.settimeout(30)
            try:
                lengths = self.headers.get_all('Content-Length', [])
                auth = self.headers.get_all('Authorization', [])
                if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or len(auth) != 1:
                    raise RequestRejected('Ambiguous or unsupported request framing')
                length = int(lengths[0])
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise RequestRejected('Request byte limit exceeded')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise RequestRejected('Incomplete request')
                response = gateway.handle(self.path, raw, authorization=auth[0])
            except (RequestRejected, ValueError):
                self.send_error(400, 'Request rejected before provider dispatch')
                return
            except AdmissionBlocked:
                self.send_error(503, 'Budget or evidence gate blocked; no automatic retries')
                return
            self.send_response(response.status)
            self.send_header('Content-Type', response.headers['content-type'])
            self.send_header('Content-Length', str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

    return LocalHTTPServer(address, Handler)
