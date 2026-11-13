"""Fixed-cap, whole-bundle adaptation of Skill_EE's four source objectives.

Upstream bba4b9170aebc06923b8fc563d4e883a6910cfe1. This adapter uses
our JSON mapping, common 50% cap and idempotent complete-block anchors.
It is not Xing's trained selector or a verbatim reproduction of postprocessing.
"""
import json
import re
from pathlib import Path
from ..bundles import Bundle, split_front_matter, reattach_front_matter, sha256
from .blocks import markdown_blocks

STRATEGIES = {
    "source_native_compact": {
        "objective": "Preserve the source author's organization, local terminology, and operational details while removing redundant prose.",
        "sections": ["YAML metadata", "Original section outline, merged only when redundant", "Operational details", "Validation and risks"],
        "max_anchor_blocks": 1, "max_anchor_chars": 1800, "upstream_ratio": .72},
    "workflow": {
        "objective": "Make ordered procedures, validation checks, constraints, failure modes, and recovery cues explicit and easy to follow.",
        "sections": ["YAML metadata", "When to use", "Inputs and outputs", "Core workflow", "Tools and APIs", "Validation checks", "Pitfalls and constraints", "Compact examples"],
        "max_anchor_blocks": 1, "max_anchor_chars": 2200, "upstream_ratio": .62},
    "api_code": {
        "objective": "Preserve imports, API calls, object construction patterns, commands, minimal executable snippets, and non-obvious implementation details.",
        "sections": ["YAML metadata", "Capability and use cases", "Required artifacts", "API/tool patterns", "Implementation anchors", "Workflow", "Validation checks", "Pitfalls"],
        "max_anchor_blocks": 2, "max_anchor_chars": 4200, "upstream_ratio": .82},
    "rule_formula": {
        "objective": "Preserve coordinate conventions, formulas, thresholds, ranking rules, tie-breaks, schemas, units, constants, and compact examples that define correctness.",
        "sections": ["YAML metadata", "When to use", "Exact definitions and invariants", "Decision rules or formulas", "Minimal implementation anchors", "Workflow", "Validation checks", "Failure modes"],
        "max_anchor_blocks": 2, "max_anchor_chars": 3600, "upstream_ratio": .78},
}


def output_schema(source: Bundle) -> dict:
    """Exact file-map shape; caller must not assume a provider enforces it."""
    return {"type": "object", "additionalProperties": False, "required": ["files"],
        "properties": {"files": {"type": "object", "additionalProperties": False,
            "required": sorted(source.files),
            "properties": {path: {"type": "string"} for path in sorted(source.files)}}}}


def render_prompt(source: Bundle, strategy_id: str, original_tokens: int, token_cap: int,
                  tokenizer_description: str, template_path: str | Path | None = None,
                  *, prompt_version: str = "v1") -> str:
    if prompt_version not in ("v1", "v2"):
        raise ValueError("Unknown rewrite prompt version")
    if template_path is not None and prompt_version != "v1":
        raise ValueError("Choose an explicit template or a built-in revision, not both")
    strategy = STRATEGIES[strategy_id]
    filename = "rewrite_bundle.txt" if prompt_version == "v1" else "rewrite_bundle_v2.txt"
    template_path = Path(template_path) if template_path else Path(__file__).resolve().parents[3] / "prompts" / filename
    fields = dict(strategy_id=strategy_id,
        strategy_objective=strategy["objective"], strategy_sections="; ".join(strategy["sections"]),
        original_tokens=original_tokens, token_cap=token_cap, tokenizer_description=tokenizer_description,
        original_files_json=json.dumps(source.files, ensure_ascii=False, sort_keys=True))
    if prompt_version == "v2":
        fields.update(output_schema_json=json.dumps(output_schema(source), ensure_ascii=False, sort_keys=True),
            output_example_json=json.dumps({"files": {"example/SKILL.md": "---\nname: example\n---\n# Procedure\nShort reusable procedure.\n"}}),
            mandatory_anchors_json=json.dumps({path: [block.text for block in select_anchors(text, strategy_id)]
                for path, text in sorted(source.files.items())}, ensure_ascii=False, sort_keys=True))
    return template_path.read_text().format(**fields)


def parse_response(raw: str, source: Bundle) -> dict[str, str]:
    cleaned = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    parsed = json.loads(cleaned, object_pairs_hook=unique)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), dict):
        raise ValueError("Expected a complete files mapping")
    files = parsed["files"]
    if set(files) != set(source.files) or any(not isinstance(v, str) for v in files.values()):
        raise ValueError("Missing/unexpected paths or non-text file in response")
    return files


def code_terms(text):
    terms = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b", text))
    terms.update(re.findall(r"`([^`\n]{3,80})`", text))
    return terms


def select_anchors(source_text, strategy_id):
    policy = STRATEGIES[strategy_id]
    _, body = split_front_matter(source_text)
    candidates = []
    for block in markdown_blocks(body):
        if block.kind != "code":
            continue
        score = 5 * bool(re.search(r"\b(import|from|class|def|function|const|let|var|new|curl|python|pytest|npm)\b", block.text))
        score += 3 * bool(re.search(r"\b(must|critical|required|never|assert|validate|verify|threshold)\b", block.text, re.I))
        score += bool(code_terms(block.text))
        candidates.append((-score, block.start, block))
    budget, result = policy["max_anchor_chars"], []
    for neg_score, _, block in sorted(candidates):
        if neg_score == 0 or len(result) == policy["max_anchor_blocks"]:
            continue
        # Skip rather than slice a code block: no broken wrappers or invented repairs.
        if len(block.text) <= budget:
            result.append(block)
            budget -= len(block.text)
    return result


def postprocess(source: Bundle, files: dict[str, str], strategy_id: str, counter=None) -> dict:
    if set(source.files) != set(files):
        raise ValueError("Postprocessing requires a complete parsed bundle")
    result, insertions, audits = {}, [], {}
    for path in sorted(files):
        raw = files[path]
        text = reattach_front_matter(source.files[path], raw)
        before = text
        terms = code_terms(source.files[path])
        missing = sorted(terms - code_terms(text))
        audits[path] = {"missing_code_terms": missing, "semantic_eligibility_filter": False}
        for anchor in select_anchors(source.files[path], strategy_id):
            # A fixed source-selected list, independent of prior postprocessing passes.
            if anchor.text.strip() not in text:
                text = text.rstrip() + "\n\n" + anchor.text.rstrip() + "\n"
                insertions.append({"path": path, "anchor_source_body_start": anchor.start,
                    "anchor_source_body_end": anchor.end, "anchor_sha256": sha256(anchor.text),
                    "inserted_text": anchor.text})
        result[path] = text
        if counter is not None:
            count = counter.count_file if hasattr(counter, "count_file") else counter
            audits[path]["added_tokens_after_metadata_and_anchors"] = count(text) - count(raw)
            audits[path]["anchor_added_tokens"] = count(text) - count(before)
    return {"files": result, "anchor_insertions": insertions, "source_audits": audits,
            "policy_version": "whole-source-block-idempotent-v1"}
