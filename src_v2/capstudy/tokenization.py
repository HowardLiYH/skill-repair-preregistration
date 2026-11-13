"""Persisted Section 5.5 provider counting. Estimates are never billed usage."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from typing import Callable


class CountUnavailable(RuntimeError):
    pass


class CountTokensCounter:
    """Inject a transport(request)->raw response, or use a previously populated cache.

    Network transport is deliberately supplied by the campaign coordinator; constructing
    this object never makes a call. API credentials must not enter persisted requests.
    """
    tokenizer_id = "anthropic-count-tokens"
    convention = "sum_per_file_single_user_message_including_overhead"
    estimates_not_billing = True

    def __init__(self, model_id: str, cache_dir: str | Path,
                 transport: Callable | None = None, api_version="2023-06-01"):
        if not model_id:
            raise ValueError("Exact executor model ID is required")
        self.model_id, self.cache_dir = model_id, Path(cache_dir)
        self.transport, self.api_version = transport, api_version
        self.revision = f"{api_version}:per-file-marker-v1"

    def count_file(self, text: str) -> int:
        request = {"model": self.model_id, "messages": [
            {"role": "user", "content": "SKILL.md text:\n" + text}]}
        identity = {"api_version": self.api_version, "shape_version": "per-file-marker-v1",
                    "request": request}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.cache_dir / (key + ".json")
        if path.exists():
            record = json.loads(path.read_text())
            if record.get("identity") != identity:
                raise CountUnavailable("Count cache identity mismatch")
            response = record["response"]
        else:
            if self.transport is None:
                raise CountUnavailable(f"No verified provider count cached for {key}")
            response = self.transport(request)
            self._validate(response)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(f".{os.getpid()}.tmp")
            temp.write_text(json.dumps({"identity": identity, "response": response,
                                       "estimates_not_billing": True}, indent=2))
            temp.replace(path)
        return self._validate(response)

    @staticmethod
    def _validate(response):
        value = response.get("input_tokens") if isinstance(response, dict) else None
        if type(value) is not int or value < 0:
            raise CountUnavailable("Provider count response lacks a nonnegative integer input_tokens")
        return value

    __call__ = count_file


def bundle_count(files: dict[str, str], counter) -> int:
    count = counter.count_file if hasattr(counter, "count_file") else counter
    values = [count(files[path]) for path in sorted(files)]
    if any(type(v) is not int or v < 0 for v in values):
        raise CountUnavailable("Unavailable or invalid per-file count")
    return sum(values)
