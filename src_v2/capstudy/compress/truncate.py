"""Round-robin head truncation at complete Markdown block boundaries."""
from ..bundles import Bundle, split_front_matter, validate_candidate
from ..tokenization import bundle_count
from .blocks import markdown_blocks


def head_truncate(source: Bundle, counter, token_cap: int | None = None) -> dict:
    original = bundle_count(source.files, counter)
    cap = original // 2 if token_cap is None else token_cap
    split = {p: split_front_matter(t) for p, t in source.files.items()}
    files = {p: prefix for p, (prefix, _) in split.items()}
    blocks = {p: markdown_blocks(body) for p, (_, body) in split.items()}
    selected = {p: [] for p in files}
    if bundle_count(files, counter) <= cap:
        stopped = set()
        while len(stopped) < len(files):
            for path in sorted(files):
                if path in stopped:
                    continue
                index = len(selected[path])
                if index >= len(blocks[path]):
                    stopped.add(path)
                    continue
                block = blocks[path][index]
                proposed = dict(files)
                proposed[path] += block.text
                if bundle_count(proposed, counter) <= cap:
                    files = proposed
                    prefix = split[path][0]
                    selected[path].append({"body_start": block.start, "body_end": block.end,
                        "file_start": len(prefix) + block.start, "file_end": len(prefix) + block.end,
                        "byte_start": len((prefix + split[path][1][:block.start]).encode()),
                        "byte_end": len((prefix + split[path][1][:block.end]).encode())})
                else:
                    stopped.add(path)
    return {"method": "head_truncation", "files": files, "selected_offsets": selected,
            **validate_candidate(source, files, counter, cap)}
