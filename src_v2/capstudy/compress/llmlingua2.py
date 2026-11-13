"""LLMLingua-2 adapter with explicit source coverage and executor-counted fitting.

The transport must call the pinned released PromptCompressor; fixtures may inject
a fake. A real checkpoint is never downloaded or selected implicitly.
"""
from dataclasses import asdict, dataclass
import time
from ..bundles import Bundle, split_front_matter, validate_candidate, sha256
from ..tokenization import bundle_count

RATES = tuple(x / 100 for x in range(50, 0, -5))


@dataclass(frozen=True)
class Chunk:
    path: str
    start: int
    end: int
    text: str
    sha256: str
    tokenizer_tokens: int


def covered_chunks(path: str, body: str, encode, maximum_tokens: int) -> list[Chunk]:
    """Split losslessly at character boundaries; each chunk is independently counted.

    The compressor's own encoder/window is used, including its special tokens.
    Bisection does not assume monotonic count for validity: every chosen chunk is
    checked, and all source spans including the final tail must be contiguous.
    """
    if maximum_tokens <= 0:
        raise ValueError("Positive tested compressor input window required")
    chunks, start = [], 0
    while start < len(body):
        low, high, best = 1, len(body) - start, 0
        while low <= high:
            middle = (low + high) // 2
            if len(encode(body[start:start + middle])) <= maximum_tokens:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if not best:
            raise ValueError("A source character cannot fit the tested compressor window")
        end = start + best
        text = body[start:end]
        count = len(encode(text))
        if count > maximum_tokens:
            raise ValueError("Chunk count exceeds window")
        chunks.append(Chunk(path, start, end, text, sha256(text), count))
        start = end
    if "".join(c.text for c in chunks) != body:
        raise AssertionError("Lossless coverage invariant failed")
    return chunks


def llmlingua_compress(source: Bundle, counter, compress_chunk, encode,
                      maximum_tokens: int, token_cap=None, *, on_attempt=None) -> dict:
    cap = bundle_count(source.files, counter) // 2 if token_cap is None else token_cap
    prefixes, chunks = {}, []
    for path, text in sorted(source.files.items()):
        prefix, body = split_front_matter(text)
        prefixes[path] = prefix
        chunks.extend(covered_chunks(path, body, encode, maximum_tokens))
    attempts, winners = [], []
    for index, rate in enumerate(RATES):
        started, files, raw = time.monotonic(), dict(prefixes), []
        for chunk in chunks:
            response = compress_chunk(chunk.text, rate)
            if not isinstance(response, dict) or not isinstance(response.get("compressed_prompt"), str):
                raise ValueError("LLMLingua returned no compressed_prompt; retain infrastructure incident")
            files[chunk.path] += response["compressed_prompt"]
            raw.append({"path": chunk.path, "source_start": chunk.start, "source_end": chunk.end,
                        "response": response})
        validation = validate_candidate(source, files, counter, cap)
        attempt = {"rate": rate, "files": files, "raw_compressor_outputs": raw,
                   "duration_seconds": time.monotonic() - started, **validation}
        attempts.append(attempt)
        if on_attempt is not None:
            on_attempt(index, attempt)
        if validation["eligible"]:
            winners.append((validation["candidate_tokens"], -index, attempt))
    winner = max(winners, key=lambda x: x[:2])[2] if winners else None
    return {"method": "llmlingua2", "selected_rate": winner["rate"] if winner else None,
            "files": winner["files"] if winner else None, "attempts": attempts,
            "coverage": [asdict(c) for c in chunks], "eligible": bool(winner),
            "ineligibility_reasons": [] if winner else ["no_structurally_valid_rate_within_cap"],
            "candidate_tokens": winner["candidate_tokens"] if winner else None,
            "original_tokens": bundle_count(source.files, counter), "token_cap": cap}


def load_released_compressor(checkpoint: str, revision: str, device: str):
    from pathlib import Path
    if not checkpoint or not revision or revision in ("main", "latest"):
        raise ValueError("Pinned checkpoint revision and explicit device required")
    if not Path(checkpoint).is_dir():
        raise ValueError("Only an existing local checkpoint is allowed; downloads are disabled")
    from llmlingua import PromptCompressor
    compressor = PromptCompressor(model_name=checkpoint, use_llmlingua2=True,
        device_map=device, model_config={"revision": revision, "local_files_only": True})
    return compressor
