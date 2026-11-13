"""Source-only importance ranking; exact sentence/block spans and heading costs."""
from dataclasses import asdict, dataclass
import json
import re
from ..bundles import Bundle, split_front_matter, validate_candidate
from ..tokenization import bundle_count
from .blocks import markdown_blocks

SEGMENTER_VERSION = "capstudy-conservative-sentence-block-v1"


@dataclass(frozen=True)
class Unit:
    unit_id: str
    path: str
    start: int
    end: int
    byte_start: int
    byte_end: int
    text: str
    ancestor_ids: tuple[str, ...]
    kind: str


def segment(source: Bundle) -> tuple[list[Unit], dict[str, Unit]]:
    units, headings = [], {}
    for path in sorted(source.files):
        text = source.files[path]
        prefix, body = split_front_matter(text)
        ancestors = []
        for block in markdown_blocks(body):
            start, end = len(prefix) + block.start, len(prefix) + block.end
            if block.kind == "heading":
                level = len(re.match(r"^ {0,3}(#+)", block.text).group(1))
                ancestors = [(n, h) for n, h in ancestors if n < level]
                uid = f"{path}:{start}:{end}"
                headings[uid] = Unit(uid, path, start, end, len(text[:start].encode()),
                                     len(text[:end].encode()), text[start:end], (), "heading")
                ancestors.append((level, uid))
                continue
            spans = [(start, end)]
            if block.kind == "prose":
                # Conservative English punctuation rule, deliberately fixed/versioned.
                # No external downloaded model; abbreviations can remain separate units.
                boundaries = [start] + [start + m.end() for m in re.finditer(r"[.!?][\"')\]]*\s+(?=[A-Z])", block.text)] + [end]
                spans = list(zip(boundaries[:-1], boundaries[1:]))
            for a, b in spans:
                if text[a:b].strip():
                    uid = f"{path}:{a}:{b}"
                    units.append(Unit(uid, path, a, b, len(text[:a].encode()), len(text[:b].encode()),
                                      text[a:b], tuple(h for _, h in ancestors), block.kind))
    return units, headings


def ranking_prompt(source: Bundle) -> str:
    units, headings = segment(source)
    return ("Rank every unit_id exactly once for reusable procedural importance. "
            "Return JSON {\"ranked_ids\": [all IDs in most-to-least important order]}. "
            "Do not rewrite text, solve tasks or add IDs. This is source-only ranking.\n" +
            json.dumps({"segmenter": SEGMENTER_VERSION, "units": [asdict(u) for u in units],
                        "headings": {k: asdict(v) for k, v in headings.items()}}, ensure_ascii=False))


def sentence_extract(source: Bundle, ranked_ids: list[str], counter, token_cap=None) -> dict:
    units, headings = segment(source)
    by_id = {u.unit_id: u for u in units}
    if not isinstance(ranked_ids, list) or any(not isinstance(x, str) for x in ranked_ids) or len(ranked_ids) != len(by_id) or set(ranked_ids) != set(by_id):
        raise ValueError("Ranking must be a complete permutation: no unknown, duplicate or missing IDs")
    cap = bundle_count(source.files, counter) // 2 if token_cap is None else token_cap
    prefixes = {p: split_front_matter(t)[0] for p, t in source.files.items()}
    def reconstruct(ids):
        kept = {i: by_id[i] for i in ids}
        for uid in ids:
            for ancestor in by_id[uid].ancestor_ids:
                kept[ancestor] = headings[ancestor]
        return {p: prefixes[p] + "".join(u.text for u in sorted(kept.values(), key=lambda x: x.start) if u.path == p)
                for p in prefixes}
    chosen, files = [], prefixes
    if bundle_count(files, counter) <= cap:
        for uid in ranked_ids:
            proposed = reconstruct(chosen + [uid])
            if bundle_count(proposed, counter) <= cap:
                chosen.append(uid)
                files = proposed
    return {"method": "sentence_extraction", "files": files, "selected_ids": chosen,
            "segmenter_version": SEGMENTER_VERSION, "units": [asdict(by_id[u]) for u in chosen],
            **validate_candidate(source, files, counter, cap)}
