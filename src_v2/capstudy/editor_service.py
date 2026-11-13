"""Durable bridge from stateless editor responses to shared candidate records.

No provider is selected here, and no credentials are read. A caller supplies the
reviewed transport, counter, journal, frozen settings and admission budget.
"""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

from .bundles import (Bundle, load_bundle, save_bundle, sha256, split_front_matter,
    validate_candidate as validate_bundle)
from .contracts import (CANDIDATE_FIELDS, canonical_hash, sha256_file,
    validate_candidate as validate_record, validate_call, write_json_atomic)
from .dispatch import dispatch_editor
from .editor import check_capacity
from .journal import AdmissionBlocked
from .tokenization import bundle_count, CountUnavailable
from .compress.xing import STRATEGIES, parse_response, postprocess, render_prompt


def _now():
    return datetime.now(timezone.utc).isoformat()


def _generation_failures(response):
    """Recover generation validity from saved provider evidence, not bundle shape.

    A parseable files object cannot turn a truncated/refused/tool-call generation
    into a completed editor output. Reparse raw envelopes where available so a
    stale normalized status cannot mask their termination reason.
    """
    observations = [response]
    raw = response.get("raw_response")
    if isinstance(raw, dict) and ("choices" in raw or raw.get("type") == "result"):
        from .transports import parse_chat_completion, parse_claude_result
        parser = parse_chat_completion if "choices" in raw else parse_claude_result
        observations.append(parser(raw, response.get("requested_model_id")))
    failures = []
    for observation in observations:
        status = observation.get("generation_status", "completed")
        if status not in ("completed", "ineligible"):
            raise ValueError("Unresolved provider generation status")
        reason = observation.get("generation_failure_reason")
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise ValueError("Invalid provider generation failure reason")
        if status == "ineligible" or reason:
            failures.append(reason or "provider_generation_ineligible")
    return sorted(set(failures))


def _with_generation_failures(validation, failures):
    result = dict(validation)
    result["ineligibility_reasons"] = sorted(set(result["ineligibility_reasons"]) | set(failures))
    result["eligible"] = not result["ineligibility_reasons"]
    return result


class EditorService:
    def __init__(self, *, source: Bundle, family: str, campaign_id: str,
                 strategy_id: str, config_sha256: str, counter, tokenizer_id: str,
                 tokenizer_revision: str, journal, transport, artifact_root,
                 upper_charge_usd, payment_mode="api", included_usage_evidence=None,
                 cost_resolver=None, editor_counter=None, context_limit=None,
                 max_output_tokens=None, headroom=.10, evidence_kind="experiment",
                 rewrite_prompt_version="v1", pilot_attempt_namespace=None):
        if strategy_id not in STRATEGIES or evidence_kind not in ("experiment", "fixture"):
            raise ValueError("Unknown strategy or evidence kind")
        if rewrite_prompt_version not in ("v1", "v2"):
            raise ValueError("Unknown rewrite prompt version")
        if pilot_attempt_namespace not in (None, "editor-feasibility-v2"):
            raise ValueError("Unknown reviewed pilot attempt namespace")
        if pilot_attempt_namespace and rewrite_prompt_version != "v2":
            raise ValueError("Pilot v2 namespace requires prompt v2")
        if (evidence_kind == "fixture") != (payment_mode == "fixture"):
            raise ValueError("Fixture evidence and fixture payment mode must agree")
        if not all((family, campaign_id, config_sha256, tokenizer_id, tokenizer_revision)):
            raise ValueError("Source/campaign/config/tokenizer identities must be resolved")
        if isinstance(headroom, bool) or not isinstance(headroom, (int, float)) or not math.isfinite(headroom) or not 0 <= headroom < 1:
            raise ValueError("Headroom must be finite and in [0,1)")
        if evidence_kind == "experiment" and any(v is None for v in (editor_counter, context_limit, max_output_tokens)):
            raise ValueError("Real editing requires the frozen editor capacity counter and limits")
        if any(v is not None for v in (editor_counter, context_limit, max_output_tokens)):
            if editor_counter is None:
                raise ValueError("Capacity limits require the editor counter")
            # Validate settings without invoking the real counter or provider.
            check_capacity({}, lambda _: 0, context_limit, max_output_tokens, headroom)
        self.source, self.family, self.campaign_id = source, family, campaign_id
        self.strategy_id, self.config_sha256 = strategy_id, config_sha256
        self.rewrite_prompt_version = rewrite_prompt_version
        self.pilot_attempt_namespace = pilot_attempt_namespace
        self.counter, self.tokenizer_id, self.tokenizer_revision = counter, tokenizer_id, tokenizer_revision
        self.journal, self.transport = journal, transport
        self.root = Path(artifact_root).resolve()
        self.upper_charge_usd, self.payment_mode = upper_charge_usd, payment_mode
        self.included_usage_evidence, self.cost_resolver = included_usage_evidence, cost_resolver
        self.editor_counter, self.context_limit = editor_counter, context_limit
        self.max_output_tokens, self.headroom = max_output_tokens, headroom
        self.evidence_kind = evidence_kind
        self.capacity_settings = self._capacity_settings()
        self.source_hashes = source.hashes()
        self.original_tokens = bundle_count(source.files, counter)
        self.token_cap = self.original_tokens // 2
        self.root.mkdir(parents=True, exist_ok=True)

    def generate_initial(self):
        prompt = render_prompt(self.source, self.strategy_id, self.original_tokens, self.token_cap,
            f"{self.tokenizer_id}@{self.tokenizer_revision}; complete-file sum including frozen wrappers",
            prompt_version=self.rewrite_prompt_version)
        request = {"system": "Compress only the provided source bundle. Return the requested complete JSON files mapping.",
                   "prompt": prompt}
        return self._generate(request, "unchanged_rewrite", 0, initial_id=None, parent_id=None)

    def development_adapter(self, initial):
        """Return the edit(request, mode, index) adapter consumed by run_family.

        The adapter derives the parent from the exact current text; it never feeds
        an outcome-selected best candidate back to the retry editor.
        """
        initial = self.validate(copy.deepcopy(initial))
        if initial["attempt_index"] != 0 or initial["family"] != self.family:
            raise ValueError("Development must bind the shared generation-zero candidate")
        histories = {mode: [initial] for mode in ("feedback_repair", "retry_no_feedback")}
        def edit(request, mode, index):
            if mode not in histories or index not in (1, 2):
                raise ValueError("Only the two frozen additional edits per arm are allowed")
            context = request.get("bundle_context", {})
            if context.get("original") != self.source.files or context.get("initial") != initial.get("files"):
                raise ValueError("Editor context changed the shared source or initial candidate")
            if request.get("token_cap") != self.token_cap:
                raise ValueError("Editor request changed the frozen cap")
            if mode == "retry_no_feedback" and "dev_feedback" in request:
                raise ValueError("Retry request includes forbidden feedback")
            current = context.get("current")
            if "current_ref" in context:
                if context["current_ref"] not in ("initial", "original"):
                    raise ValueError("Unknown current text reference")
                current = context[context["current_ref"]]
            if index == 2 and not any(c["attempt_index"] == 1 for c in histories[mode]):
                raise ValueError("Second edit requires the first scheduled attempt")
            # Initial text is the prescribed first editing context; thereafter
            # only the latest structurally valid generated text may replace it.
            latest = initial
            for previous in sorted(histories[mode], key=lambda c: c["attempt_index"]):
                if 0 < previous["attempt_index"] < index and previous.get("structurally_valid") and previous.get("files") is not None:
                    latest = previous
            expected_current = latest.get("files") if latest.get("files") is not None else self.source.files
            if current != expected_current:
                raise ValueError("Current editor text must be the latest structurally valid text for this arm")
            parent = latest["candidate_id"]
            result = self._generate(request, mode, index, initial_id=initial["candidate_id"], parent_id=parent)
            if not any(c["candidate_id"] == result["candidate_id"] for c in histories[mode]):
                histories[mode].append(copy.deepcopy(result))
            return result
        return edit

    def _identity(self, method, index, initial_id, parent_id):
        result = {"schema_version": 1, "campaign_id": self.campaign_id, "family": self.family,
            "method": method, "strategy_id": self.strategy_id, "attempt_index": index,
            "initial_candidate_id": initial_id, "parent_candidate_id": parent_id,
            "source_bundle_sha256": self.source_hashes["source_bundle_sha256"],
            "config_sha256": self.config_sha256, "evidence_kind": self.evidence_kind}
        if self.pilot_attempt_namespace:
            result["pilot_attempt_namespace"] = self.pilot_attempt_namespace
        return result

    def _slot(self, method, index):
        """One attempt slot regardless of mutable context, parent or settings.

        Four initial calibration strategies legitimately occupy separate slots;
        each dev arm has only attempts 1 and 2 under its selected frozen strategy.
        """
        result = {"slot_version": 2, "campaign_id": self.campaign_id, "family": self.family,
            "method": method, "attempt_index": index, "evidence_kind": self.evidence_kind,
            "calibration_strategy": self.strategy_id if index == 0 else None}
        if self.pilot_attempt_namespace:
            if index != 0 or method != "unchanged_rewrite":
                raise ValueError("Feasibility namespace cannot run development edits")
            result["pilot_attempt_namespace"] = self.pilot_attempt_namespace
        return result

    def _capacity_settings(self):
        counter = self.editor_counter
        snapshot = {"context_limit": self.context_limit, "max_output_tokens": self.max_output_tokens,
            "headroom": self.headroom,
            "editor_counter": {"tokenizer_id": getattr(counter, "tokenizer_id", None),
                "revision": getattr(counter, "revision", None), "model_id": getattr(counter, "model_id", None)},
            "transport": {key: copy.deepcopy(getattr(self.transport, key, None)) for key in
                ("provider", "model_id", "base_url", "max_output_tokens", "decoding_settings")}}
        if self.evidence_kind == "experiment":
            if not all(snapshot["editor_counter"].values()):
                raise ValueError("Real editor counter needs tokenizer_id, revision and exact model_id")
            if snapshot["transport"]["model_id"] != snapshot["editor_counter"]["model_id"]:
                raise ValueError("Editor counter model differs from transport model")
            if snapshot["transport"]["max_output_tokens"] != self.max_output_tokens:
                raise ValueError("Capacity output limit differs from transport output limit")
            if not callable(getattr(self.transport, "plan", None)):
                raise ValueError("Real editing requires an inspectable transport plan")
        return snapshot

    def _capacity_plan(self, request):
        current = self._capacity_settings()
        if current != self.capacity_settings:
            raise AdmissionBlocked("Bound editor capacity/model/decoding settings changed")
        capacity_request, plan = request, None
        if callable(getattr(self.transport, "plan", None)):
            plan = self.transport.plan(request)
            if plan.get("executed") is not False:
                raise ValueError("Transport planning must not dispatch")
            payload = plan.get("payload")
            if self.evidence_kind == "experiment":
                if not isinstance(payload, dict) or payload.get("model") != current["transport"]["model_id"] or payload.get("max_tokens") != self.max_output_tokens:
                    raise ValueError("Actual transport plan differs from bound capacity/model settings")
            if isinstance(payload, dict):
                capacity_request = payload
        return capacity_request, {"settings": current, "transport_plan_sha256": canonical_hash(plan) if plan else None}

    def _generate(self, request, method, index, *, initial_id, parent_id):
        request = copy.deepcopy(request)
        if index:
            objective = STRATEGIES[self.strategy_id]["objective"]
            request["directive"] = request.get("directive", "") + f"\nFrozen strategy: {self.strategy_id}. Preservation objective: {objective}"
        identity = self._identity(method, index, initial_id, parent_id)
        slot = self._slot(method, index)
        capacity_request, capacity_binding = self._capacity_plan(request)
        candidate_id = "candidate-" + canonical_hash(slot)[:24]
        folder = self.root / "candidates" / candidate_id
        folder.mkdir(parents=True, exist_ok=True)
        binding = {"slot": slot, "identity": identity, "request_sha256": canonical_hash(request),
            "capacity_binding": capacity_binding}
        # The journal hashes the full execution binding while the transport sees
        # only editor text. Reusing a journal with a different artifact directory
        # cannot bypass the fixed attempt slot by changing model/parent/settings.
        journal_request = {"editor_request": request, "execution_binding": binding}
        binding_path = folder / "binding.json"
        if binding_path.exists():
            if json.loads(binding_path.read_text()) != binding:
                raise AdmissionBlocked("The same candidate attempt has different request bytes")
        else:
            write_json_atomic(binding_path, binding)
        record_path, seal_path = folder / "candidate.json", folder / "completion.json"
        if seal_path.exists():
            seal = json.loads(seal_path.read_text())
            if not record_path.is_file() or sha256_file(record_path) != seal["candidate_sha256"]:
                raise AdmissionBlocked("Persisted candidate record changed")
            return self.validate(json.loads(record_path.read_text()))
        write_json_atomic(folder / "request.json", request)
        attempt_path = folder / "attempt.json"
        if not attempt_path.exists():
            write_json_atomic(attempt_path, {**binding, "candidate_id": candidate_id,
                "scheduled_at": _now(), "evidence_kind": self.evidence_kind})
        started = json.loads(attempt_path.read_text())["scheduled_at"]
        record = {field: None for field in CANDIDATE_FIELDS}
        record.update({**identity, "candidate_id": candidate_id,
            "original_tokens": self.original_tokens, "token_cap": self.token_cap,
            "tokenizer_id": self.tokenizer_id, "tokenizer_revision": self.tokenizer_revision,
            "created_at": started, "call_ids": [], "prompt_sha256": canonical_hash(request),
            "request_path": str(folder / "request.json"), "eligible": False,
            "capacity_binding": capacity_binding,
            "files": None, "structurally_valid": False, "ineligibility_reasons": []})
        if self.editor_counter is not None:
            try:
                record["editor_request_tokens"] = check_capacity(capacity_request, self.editor_counter,
                    self.context_limit, self.max_output_tokens, self.headroom)
            except ValueError as exc:
                if str(exc) != "context_limit":
                    raise
                record["ineligibility_reasons"] = ["context_limit"]
                record["attempt_dispatched"] = False
                record["attempt_consumed"] = True
                return self._finish(record, folder)
        # schedule() is idempotent and exposes the job ID needed for provenance;
        # dispatch_editor performs the same identity check before claiming it.
        job = self.journal.schedule(slot, journal_request, self.upper_charge_usd,
            payment_mode=self.payment_mode, attempt_index=index,
            included_usage_evidence=self.included_usage_evidence)
        record["call_ids"] = [job["job_id"]]
        try:
            response = dispatch_editor(self.journal, slot, journal_request,
                lambda envelope: self.transport(envelope["editor_request"]),
                artifact_root=self.root, upper_charge_usd=self.upper_charge_usd,
                payment_mode=self.payment_mode, included_usage_evidence=self.included_usage_evidence,
                cost_resolver=self.cost_resolver)
        except BaseException as exc:
            write_json_atomic(folder / "incident.json", {"candidate_id": candidate_id,
                "job_id": job["job_id"], "error_type": type(exc).__name__,
                "journal_state": self.journal.get(job["job_id"])["state"],
                "experiment_outcome": "unresolved_infrastructure", "at": _now()})
            raise
        completed_job = self.journal.get(job["job_id"])
        record["response_path"] = completed_job["response_path"]
        record["response_sha256"] = completed_job["response_sha256"]
        record["attempt_dispatched"], record["attempt_consumed"] = True, True
        call = {"schema_version": 1, "call_id": job["job_id"], "request_id": response.get("request_id"),
            "campaign_id": self.campaign_id, "phase": "compression" if index == 0 else
                "repair_edit" if method == "feedback_repair" else "retry_edit",
            "family": self.family, "condition": method, "model_id": response.get("model_id"),
            "started_at": started, "completed_at": _now(), "usage": response.get("usage"),
            "currency": "USD", "pricing_version": response.get("pricing_version"),
            "known_charge": response.get("actual_incremental_charge_usd"),
            "accounted_charge_usd": response.get("accounted_charge_usd"),
            "cost_metadata": response.get("cost_metadata"), "evidence_kind": self.evidence_kind,
            "unavailable_fields": response.get("unavailable_fields") or
                ([] if response.get("actual_incremental_charge_usd") is not None else ["actual_incremental_charge_usd"])}
        validate_call(call)
        write_json_atomic(folder / "call.json", call)
        record["call_record_path"] = str(folder / "call.json")
        generation_failures = _generation_failures(response)
        record["generation_status"] = "ineligible" if generation_failures else "completed"
        record["generation_failure_reasons"] = generation_failures
        raw_text = response.get("response_text")
        try:
            if not isinstance(raw_text, str):
                raise ValueError("Response has no text")
            files = parse_response(raw_text, self.source)
        except (ValueError, TypeError):
            record["ineligibility_reasons"] = sorted(set(generation_failures + ["invalid_json_or_file_mapping"]))
            return self._finish(record, folder)
        write_json_atomic(folder / "raw_model_files.json", files)
        try:
            for text in files.values():
                text.encode("utf-8")
            processed = postprocess(self.source, files, self.strategy_id, self.counter)
        except (ValueError, UnicodeError):
            record["ineligibility_reasons"] = sorted(set(generation_failures + ["invalid_front_matter_or_utf8"]))
            return self._finish(record, folder)
        except CountUnavailable as exc:
            self._count_incident(folder, candidate_id, job["job_id"], exc)
            raise
        files = processed["files"]
        write_json_atomic(folder / "postprocessing.json", processed)
        try:
            validation = validate_bundle(self.source, files, self.counter, self.token_cap)
        except CountUnavailable as exc:
            self._count_incident(folder, candidate_id, job["job_id"], exc)
            raise
        destination = folder / "bundle"
        if destination.exists():
            # A prior crash may have left a complete immutable bundle. Reuse it
            # only when its bytes and modes exactly match this saved response.
            loaded = load_bundle(destination)
            if loaded.files != files or loaded.support != self.source.support or loaded.modes != self.source.modes:
                raise AdmissionBlocked("Previously materialized candidate bundle differs")
        else:
            staging = Path(tempfile.mkdtemp(prefix=".bundle-", dir=folder))
            try:
                save_bundle(self.source, files, staging / "tree")
                os.replace(staging / "tree", destination)
            finally:
                shutil.rmtree(staging)
        hashes = self.source.hashes(files)
        record.update({**_with_generation_failures(validation, generation_failures), "files": files, "bundle_path": str(destination),
            "instruction_sha256": hashes["instruction_sha256"], "support_sha256": hashes["support_sha256"],
            "file_sha256": hashes["file_sha256"], "file_modes": hashes["file_modes"],
            "bundle_sha256": hashes["source_bundle_sha256"],
            "structurally_valid": not bool(set(validation["ineligibility_reasons"]) - {"over_cap"})})
        return self._finish(record, folder)

    def _count_incident(self, folder, candidate_id, job_id, exc):
        write_json_atomic(folder / "incident.json", {"candidate_id": candidate_id, "job_id": job_id,
            "error_type": type(exc).__name__, "experiment_outcome": "unresolved_counting_infrastructure",
            "response_already_saved": True, "at": _now()})

    def _finish(self, record, folder):
        reasons = set(record["ineligibility_reasons"])
        record["source_diagnostics"] = {"token_count": record.get("candidate_tokens"),
            "token_cap": self.token_cap, "parse_error": "invalid_json_or_file_mapping" in reasons,
            "front_matter_valid": (not any("front_matter" in r for r in reasons)) if record.get("files") is not None else None,
            "code_fences_valid": ("newly_broken_wrapper" not in reasons) if record.get("files") is not None else None,
            "local_references_valid": ("newly_broken_local_reference" not in reasons) if record.get("files") is not None else None,
            "support_unchanged": record.get("bundle_path") is not None}
        record["evidence_sha256"] = {str(folder / name): sha256_file(folder / name)
            for name in ("request.json", "attempt.json", "binding.json", "call.json", "raw_model_files.json", "postprocessing.json")
            if (folder / name).is_file()}
        validate_record(record)
        write_json_atomic(folder / "candidate.json", record)
        write_json_atomic(folder / "completion.json", {"candidate_sha256": sha256_file(folder / "candidate.json"),
            "binding_sha256": sha256_file(folder / "binding.json"), "completed_at": _now()})
        return record

    def validate(self, candidate):
        validate_record(candidate)
        if not candidate.get("request_path"):
            raise ValueError("Service candidates require their saved editor request")
        saved_request = json.loads(Path(candidate["request_path"]).read_text())
        _, expected_capacity_binding = self._capacity_plan(saved_request)
        if candidate.get("capacity_binding") != expected_capacity_binding:
            raise ValueError("Candidate capacity/model binding differs from this service")
        for path, expected in candidate.get("evidence_sha256", {}).items():
            if sha256_file(path) != expected:
                raise ValueError("Candidate evidence bytes changed")
        if candidate.get("response_path") and sha256_file(candidate["response_path"]) != candidate.get("response_sha256"):
            raise ValueError("Saved provider response bytes changed")
        generation_failures = []
        if candidate.get("response_path"):
            response = json.loads(Path(candidate["response_path"]).read_text())
            generation_failures = _generation_failures(response)
            expected_status = "ineligible" if generation_failures else "completed"
            if candidate.get("generation_status") != expected_status or candidate.get("generation_failure_reasons") != generation_failures:
                raise ValueError("Candidate generation status differs from saved provider response")
            if not set(generation_failures).issubset(candidate["ineligibility_reasons"]):
                raise ValueError("Candidate discarded provider generation failure reasons")
        if any(candidate.get(k) != expected for k, expected in (
                ("family", self.family), ("campaign_id", self.campaign_id),
                ("config_sha256", self.config_sha256), ("strategy_id", self.strategy_id),
                ("source_bundle_sha256", self.source_hashes["source_bundle_sha256"]),
                ("token_cap", self.token_cap), ("tokenizer_id", self.tokenizer_id),
                ("tokenizer_revision", self.tokenizer_revision), ("evidence_kind", self.evidence_kind))):
            raise ValueError("Candidate identity differs from the bound frozen service")
        files = candidate.get("files")
        if files is None:
            if candidate["eligible"] or candidate.get("bundle_path") is not None or candidate.get("structurally_valid") is not False:
                raise ValueError("Unparsed candidate cannot be eligible or have a bundle")
            return candidate
        if not candidate.get("bundle_path"):
            raise ValueError("Parsed candidate must retain materialized bytes")
        bundle_validation = validate_bundle(self.source, files, self.counter, self.token_cap, candidate["bundle_path"])
        validation = _with_generation_failures(bundle_validation, generation_failures)
        hashes = self.source.hashes(files)
        for key in ("instruction_sha256", "support_sha256", "file_sha256", "file_modes"):
            if candidate.get(key) != hashes[key]:
                raise ValueError("Candidate manifest hash/mode differs from persisted bytes")
        for key in ("eligible", "candidate_tokens", "original_tokens", "ineligibility_reasons"):
            if candidate.get(key) != validation[key]:
                raise ValueError("Candidate eligibility/count record differs from independent validation")
        expected_structure = not bool(set(bundle_validation["ineligibility_reasons"]) - {"over_cap"})
        if candidate.get("structurally_valid") is not expected_structure:
            raise ValueError("Candidate structural validity differs from independent validation")
        return candidate
