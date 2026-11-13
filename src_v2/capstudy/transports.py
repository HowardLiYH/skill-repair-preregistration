"""Stateless editor transports. Construction and planning never dispatch requests.

All live calls require a per-request admission callback. No OAuth credentials are
read by this module; Claude subscription authentication stays inside official CLI.
There are no automatic retries, continuations, tools or checkpoint downloads.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

EDITOR_KEYS = frozenset({"system", "bundle_context", "token_cap", "shared_dev_instructions",
    "source_diagnostics", "directive", "dev_feedback", "prompt"})


class TransportError(RuntimeError):
    def __init__(self, message, *, dispatched=False, evidence=None):
        super().__init__(message)
        self.dispatched = dispatched
        self.attempt_consumed = dispatched
        self.evidence = evidence or {}


def _messages(request):
    if not isinstance(request, dict) or set(request) - EDITOR_KEYS:
        raise TransportError("Only allowlisted editor text may enter a transport")
    if not isinstance(request.get("system"), str) or not request["system"].strip():
        raise TransportError("An explicit editor system instruction is required")
    payload = {k: v for k, v in request.items() if k != "system"}
    if not payload:
        raise TransportError("Editor request has no source input")
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TransportError("Editor text is not JSON serializable") from exc
    return [{"role": "system", "content": request["system"]}, {"role": "user", "content": text}]


def _admit(callback, provider, model_id, request):
    if callback is None:
        raise TransportError("Live dispatch disabled: no budget/isolation admission callback")
    evidence = callback({"provider": provider, "model_id": model_id, "request": request})
    if not isinstance(evidence, dict) or evidence.get("admitted") is not True:
        raise TransportError("Campaign admission refused editor dispatch")
    if not evidence.get("admission_id") or not evidence.get("evidence_refs"):
        raise TransportError("Admission needs an ID and reviewable evidence references")
    ceiling = evidence.get("maximum_incremental_charge_usd")
    if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not math.isfinite(ceiling) or ceiling < 0:
        raise TransportError("Admission needs a finite nonnegative maximum incremental charge")
    if ceiling == 0 and evidence.get("zero_incremental_cost_verified") is not True:
        raise TransportError("Zero-dollar admission requires verified no-extra-charge evidence")
    return evidence


def _usage(usage, *, convention):
    """Preserve provider fields, do not add cumulative/cache/reasoning subtotals."""
    if usage is None:
        return None
    if not isinstance(usage, dict):
        # The generation can be known even when billing metadata is malformed.
        # Retain the bytes/object rather than throwing away the whole response.
        return {"raw": usage, "convention": convention, "valid": False,
                "error": "malformed_provider_usage"}
    return {"raw": usage, "convention": convention, "valid": True}


def parse_claude_result(raw, requested_model):
    if not isinstance(raw, dict) or raw.get("type") != "result":
        raise TransportError("Claude CLI did not return a final result object", dispatched=True, evidence={"raw_response": raw})
    # Explicit model/output-limit terminations are completed, consumed attempts.
    # General execution errors still require infrastructure diagnosis.
    generation_failures = {
        "error_max_turns": "output_truncated",
        "error_max_budget_usd": "generation_budget_limit",
        "error_max_structured_output_retries": "invalid_structured_output",
    }
    reason = generation_failures.get(raw.get("subtype"))
    if reason is None and (raw.get("is_error") is True or raw.get("subtype") not in (None, "success")):
        raise TransportError("Claude CLI execution failed; diagnose infrastructure", dispatched=True, evidence={"raw_response": raw})
    if reason is None and not isinstance(raw.get("result"), str):
        reason = "invalid_response_text"
    models = list(raw.get("modelUsage", {})) if isinstance(raw.get("modelUsage"), dict) else []
    model_id = raw.get("model") or (models[0] if len(models) == 1 else None)
    return {"response_text": raw.get("result") if isinstance(raw.get("result"), str) else None,
        "generation_status": "ineligible" if reason else "completed", "generation_failure_reason": reason,
        "usage": _usage(raw.get("usage"), convention="claude_final_result_only_do_not_sum_snapshots"),
        "request_id": raw.get("request_id"), "model_id": model_id,
        "requested_model_id": requested_model, "session_id": raw.get("session_id"),
        "known_charge": None, "actual_incremental_charge_usd": None, "cost_available": False,
        "cost_metadata": {"client_estimated_usd": raw.get("total_cost_usd"),
            "billing_charge_usd": None, "reason": "CLI cost estimate is not a subscription invoice or verified API charge"},
        "raw_response": raw,
        "unavailable_fields": ["billing_charge_usd"] + ([] if model_id else ["returned_model_id"]) +
                              ([] if raw.get("request_id") else ["provider_request_id"])}


@dataclass
class ClaudeCodeTransport:
    model_id: str
    cli_path: str
    sandbox_parent: str | Path
    admission: object = None
    timeout_seconds: float = 300
    expected_cli_version: str = "2.1.296"
    run: object = subprocess.run

    def plan(self, request):
        messages = _messages(request)
        if not self.model_id or not Path(self.cli_path).is_absolute():
            raise TransportError("Exact model and absolute official CLI path required")
        settings = {"disableAllHooks": True, "disableClaudeAiConnectors": True,
            "disableBundledSkills": True, "disableSkillShellExecution": True,
            "disableWorkflows": True, "autoMemoryEnabled": False,
            "autoCompactEnabled": False, "permissions": {"deny": ["*"]}}
        command = [self.cli_path, "--print", "--output-format", "json", "--model", self.model_id,
            "--safe-mode", "--restricted", "--tools", "", "--disallowedTools", "*",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "", "--settings", json.dumps(settings, sort_keys=True),
            "--disable-slash-commands", "--no-chrome", "--no-session-persistence",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--max-turns", "1", "--system-prompt", messages[0]["content"]]
        return {"command": command, "stdin": messages[1]["content"], "executed": False,
            "expected_cli_version": self.expected_cli_version,
            "max_output_tokens_control": {"status": "unsupported_unverified", "value": None},
            "managed_policy_review_required": True}

    def __call__(self, request):
        plan = self.plan(request)
        evidence = _admit(self.admission, "claude-code-subscription", self.model_id, request)
        if evidence.get("billing_mode") != "subscription" or evidence.get("managed_policy_isolation_verified") is not True:
            raise TransportError("CLI dispatch requires subscription route and managed-policy isolation verification")
        if evidence.get("cli_version") != self.expected_cli_version:
            raise TransportError("CLI admission version differs from frozen transport version")
        # Keep normal OS authentication available to the official CLI, but discard
        # inherited API keys, OAuth tokens, provider routing, NODE_OPTIONS and proxies.
        env = {key: os.environ[key] for key in ("HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
        parent = Path(self.sandbox_parent).resolve()
        if not parent.is_dir():
            raise TransportError("Approved temporary sandbox parent must already exist")
        with tempfile.TemporaryDirectory(prefix="capstudy-editor-", dir=parent) as directory:
            Path(directory).chmod(0o700)
            try:
                completed = self.run(plan["command"], input=plan["stdin"], text=True,
                    capture_output=True, cwd=directory, env=env, timeout=self.timeout_seconds,
                    check=False, shell=False)
            except FileNotFoundError as exc:
                raise TransportError("Official Claude executable is unavailable") from exc
            except subprocess.TimeoutExpired as exc:
                raise TransportError("Editor timed out after process dispatch; no automatic retry", dispatched=True,
                    evidence={"stdout": _as_text(exc.stdout), "stderr": _as_text(exc.stderr)}) from exc
            except OSError as exc:
                raise TransportError("Editor process launch failed; dispatch status unknown", dispatched=True) from exc
            try:
                raw = json.loads(completed.stdout)
            except (ValueError, TypeError) as exc:
                raise TransportError("Editor output is not a final JSON result", dispatched=True,
                    evidence={"stdout": completed.stdout, "stderr": completed.stderr, "returncode": completed.returncode}) from exc
            try:
                result = parse_claude_result(raw, self.model_id)
            except TransportError as exc:
                exc.evidence.update(stderr=completed.stderr, returncode=completed.returncode)
                raise
            if evidence.get("included_only_billing_verified") is True and evidence.get("zero_incremental_cost_verified") is True:
                result["actual_incremental_charge_usd"] = 0.0
                result["cost_metadata"]["incremental_charge_basis"] = {
                    "kind": "verified_included_only_subscription", "evidence_refs": evidence["evidence_refs"]}
            result["transport_metadata"] = {"kind": "official_claude_cli", "cli_version": self.expected_cli_version,
                "admission_id": evidence["admission_id"], "fresh_cwd": True, "tools": [],
                "persistent_session": False, "stderr": completed.stderr,
                "process_returncode": completed.returncode,
                "max_output_tokens_control": plan["max_output_tokens_control"]}
            return result


def _as_text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def post_json_once(url, payload, api_key, timeout, *, max_request_bytes=None, max_response_bytes=None):
    """One official-endpoint request, no SDK retries or credential-bearing redirect."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        for bound in (max_request_bytes,max_response_bytes):
            if bound is not None and (type(bound) is not int or bound<=0):raise ValueError('Positive byte bound required')
        if max_request_bytes is not None and len(encoded)>max_request_bytes:
            raise ValueError('Formal editor request exceeds frozen byte bound')
        request = urllib.request.Request(url, data=encoded, method="POST",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    except (TypeError, ValueError) as exc:
        raise TransportError("Invalid HTTP request before dispatch", dispatched=False,
            evidence={"exception_type": type(exc).__name__}) from exc
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    response_opened = False
    try:
        with opener.open(request, timeout=timeout) as response:
            response_opened = True
            raw_body = response.read() if max_response_bytes is None else response.read(max_response_bytes+1)
            if max_response_bytes is not None and len(raw_body)>max_response_bytes:
                prefix=raw_body[:max_response_bytes]
                raise TransportError('Provider response exceeds frozen byte bound',dispatched=True,
                    evidence={'response_body':prefix.decode('utf-8',errors='replace'),'response_body_hex':prefix.hex(),
                              'response_truncated':True,'retained_response_bytes':len(prefix),
                              'http_status':getattr(response,'status',None),'billing_outcome':'unresolved'})
            try:
                return json.loads(raw_body.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise TransportError("Provider response is not valid JSON", dispatched=True,
                    evidence={"response_body": raw_body.decode("utf-8", errors="replace"),
                              "response_body_hex": raw_body.hex(),
                              "http_status": getattr(response, "status", None)}) from exc
    except urllib.error.HTTPError as exc:
        # A response status alone is insufficient evidence that generation never
        # began. Conservatively consume the dispatch; no hidden HTTP retry.
        try:
            body = exc.read() if max_response_bytes is None else exc.read(max_response_bytes+1)
            truncated=max_response_bytes is not None and len(body)>max_response_bytes
            if truncated:body=body[:max_response_bytes]
            body_evidence = {"response_body": body.decode("utf-8", errors="replace"),
                             "response_body_hex": body.hex()}
            if truncated:body_evidence.update(response_truncated=True,retained_response_bytes=len(body),billing_outcome="unresolved")
        except Exception as body_error:
            body_evidence = {"body_read_error": type(body_error).__name__}
        # Only non-secret response headers are retained. Never persist request
        # authorization headers or a credential-bearing request URL.
        safe_headers = {k: exc.headers.get(k) for k in
            ("request-id", "x-request-id", "retry-after", "content-type")
            if exc.headers is not None and exc.headers.get(k) is not None}
        raise TransportError("Provider returned HTTP error", dispatched=True,
            evidence={"http_status": exc.code, "response_headers": safe_headers, **body_evidence}) from exc
    except urllib.error.URLError as exc:
        # urllib's HTTPS handler performs certificate verification before sending
        # the HTTP request body. Recognize only its concrete verification exception,
        # not message substrings or arbitrary SSL errors. No automatic retry occurs.
        # A similar error while reading an already-open response is not pre-dispatch.
        reason = exc.reason
        verified_pre_dispatch = not response_opened and isinstance(reason, ssl.SSLCertVerificationError)
        evidence = {"exception_type": type(exc).__name__, "reason_type": type(reason).__name__,
                    "response_opened": response_opened}
        if verified_pre_dispatch:
            evidence.update(failure_stage="tls_certificate_verification",
                            http_request_sent=False,
                            confirmed_not_dispatched=True,
                            certificate_verify_code=getattr(reason, "verify_code", None),
                            certificate_verify_message=getattr(reason, "verify_message", None))
        raise TransportError(
            "TLS certificate verification failed before HTTP dispatch" if verified_pre_dispatch else
            "Provider connection outcome is uncertain; no automatic retry",
            dispatched=not verified_pre_dispatch, evidence=evidence) from exc
    except TransportError:
        raise
    except Exception as exc:
        raise TransportError("Provider response unavailable after dispatch", dispatched=True,
            evidence={"exception_type": type(exc).__name__}) from exc


def post_json_once_bounded(url,payload,api_key,timeout):
    """Prospective formal editor only; historical default remains unchanged."""
    from .dispatch_storage import REQUEST_LIMIT,RESPONSE_LIMIT
    return post_json_once(url,payload,api_key,timeout,
        max_request_bytes=REQUEST_LIMIT,max_response_bytes=RESPONSE_LIMIT)


def parse_chat_completion(raw, requested_model):
    if not isinstance(raw, dict) or not isinstance(raw.get("choices"), list) or len(raw["choices"]) != 1:
        raise TransportError("Expected exactly one completion", dispatched=True, evidence={"raw_response": raw})
    choice = raw["choices"][0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise TransportError("Malformed completion envelope", dispatched=True, evidence={"raw_response": raw})
    message = choice["message"]
    finish = choice.get("finish_reason")
    if finish == "length": reason = "output_truncated"
    elif finish == "content_filter" or message.get("refusal"): reason = "model_refusal"
    elif finish in ("tool_calls", "function_call") or message.get("tool_calls") or message.get("function_call"): reason = "tool_request"
    elif finish != "stop":
        raise TransportError("Unknown completion finish state", dispatched=True, evidence={"raw_response": raw})
    elif not isinstance(message.get("content"), str): reason = "invalid_response_text"
    else: reason = None
    return {"response_text": message.get("content") if isinstance(message.get("content"), str) else None,
        "generation_status": "ineligible" if reason else "completed", "generation_failure_reason": reason,
        "request_id": raw.get("id"), "model_id": raw.get("model"),
        "requested_model_id": requested_model,
        "usage": _usage(raw.get("usage"), convention="openai_compatible_prompt_tokens_include_cache_output_includes_reasoning"),
        "known_charge": None, "actual_incremental_charge_usd": None, "cost_available": False, "cost_metadata": {"billing_charge_usd": None,
            "reason": "Usage requires a frozen provider pricing/billing record; missing cost is not zero"},
        "system_fingerprint": raw.get("system_fingerprint"), "raw_response": raw,
        "unavailable_fields": ["billing_charge_usd"]}


@dataclass
class OpenAICompatibleTransport:
    provider: str
    model_id: str
    base_url: str
    credential_env_name: str
    max_output_tokens: int
    admission: object = None
    timeout_seconds: float = 300
    decoding_settings: dict | None = None
    post: object = post_json_once

    def plan(self, request):
        messages = _messages(request)
        parsed = urllib.parse.urlparse(self.base_url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443):
            raise TransportError("Official HTTPS API base URL required")
        if self.provider == "deepseek":
            valid = host == "api.deepseek.com" and parsed.path.rstrip("/") in ("", "/v1")
        elif self.provider == "qwen":
            valid_host = host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "cn-hongkong.dashscope.aliyuncs.com"} or bool(re.fullmatch(r"[a-zA-Z0-9-]+\.(?:ap-southeast-1|us-east-1|cn-beijing|cn-hongkong|ap-northeast-1)\.maas\.aliyuncs\.com", host))
            valid = valid_host and parsed.path.rstrip("/") == "/compatible-mode/v1"
        else:
            valid = False
        if not valid or not self.model_id or type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise TransportError("Approved provider, exact model, endpoint and output limit required")
        settings = dict(self.decoding_settings or {})
        allowed = {"temperature", "top_p", "seed", "response_format"}
        allowed |= {"thinking", "reasoning_effort"} if self.provider == "deepseek" else {"enable_thinking", "thinking_budget"}
        if set(settings) - allowed:
            raise TransportError("Non-allowlisted decoding settings; tools/messages/endpoints cannot be overridden")
        payload = {"model": self.model_id, "messages": messages, "max_tokens": self.max_output_tokens,
            "stream": False, **settings}
        return {"url": self.base_url.rstrip("/") + "/chat/completions", "payload": payload,
                "credential_env_name": self.credential_env_name, "executed": False}

    def __call__(self, request):
        plan = self.plan(request)
        evidence = _admit(self.admission, self.provider, self.model_id, request)
        if evidence.get("billing_mode") not in ("metered_api", "verified_free_api"):
            raise TransportError("API transport requires explicit provider billing admission")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.credential_env_name):
            raise TransportError("Credential must be an environment-variable name")
        key = os.environ.get(self.credential_env_name)
        if not key:
            raise TransportError("Provider credential is unavailable")
        raw = self.post(plan["url"], plan["payload"], key, self.timeout_seconds)
        result = parse_chat_completion(raw, self.model_id)
        result["transport_metadata"] = {"kind": "openai_compatible_https", "provider": self.provider,
            "admission_id": evidence["admission_id"], "url": plan["url"], "tools": [], "automatic_retries": 0}
        return result
