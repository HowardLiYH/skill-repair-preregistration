"""Immutable bundle I/O, byte-preserving metadata, hashes and shared eligibility."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from .tokenization import bundle_count, CountUnavailable


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def valid_path(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and "\\" not in path and str(p) == path


def split_front_matter(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return "", text
    for i, line in enumerate(lines[1:], 1):
        if line.rstrip("\r\n") in ("---", "..."):
            return "".join(lines[:i + 1]), "".join(lines[i + 1:])
    raise ValueError("Unterminated source front matter")


@dataclass(frozen=True)
class Bundle:
    root: Path
    files: dict[str, str]
    support: dict[str, bytes]
    modes: dict[str, int]

    def hashes(self, files=None):
        files = self.files if files is None else files
        entries = {p: {"sha256": sha256(v), "mode": self.modes[p]}
                   for p, v in {**self.support, **files}.items()}
        digest = lambda subset: sha256(json.dumps({p: entries[p] for p in sorted(subset)}, sort_keys=True))
        return {"source_bundle_sha256": digest(entries), "instruction_sha256": digest(files),
                "support_sha256": digest(self.support),
                "file_sha256": {p: e["sha256"] for p, e in sorted(entries.items())},
                "file_modes": {p: e["mode"] for p, e in sorted(entries.items())}}


def load_bundle(root: str | Path) -> Bundle:
    root = Path(root).resolve()
    files, support, modes = {}, {}, {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink not permitted in immutable bundle: {path}")
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        modes[rel] = stat.S_IMODE(path.stat().st_mode)
        raw = path.read_bytes()
        if path.name == "SKILL.md":
            files[rel] = raw.decode("utf-8")
        else:
            support[rel] = raw
    if not files:
        raise ValueError("Bundle has no SKILL.md files")
    return Bundle(root, files, support, modes)


def reattach_front_matter(original: str, revised: str) -> str:
    prefix, _ = split_front_matter(original)
    _, body = split_front_matter(revised)
    return prefix + body


def fence_errors(text: str) -> list[str]:
    opened = None
    for line in text.splitlines():
        m = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if not m:
            continue
        fence, tail = m.groups()
        if opened is None:
            opened = (fence[0], len(fence))
        elif fence[0] == opened[0] and len(fence) >= opened[1] and not tail.strip():
            opened = None
    return ["unclosed_code_fence"] if opened else []


def broken_local_links(text: str, path: str, available: set[str]) -> set[str]:
    from posixpath import normpath, join, dirname
    from urllib.parse import unquote
    broken = set()
    for target in re.findall(r"\]\(([^)\s]+)(?:\s+[^)]*)?\)", text):
        target = unquote(target.split("#")[0])
        if not target or re.match(r"^[a-zA-Z][\w+.-]*:", target) or target.startswith("/"):
            continue
        resolved = normpath(join(dirname(path), target))
        if resolved not in available and not any(p.startswith(resolved.rstrip("/") + "/") for p in available):
            broken.add(target)
    return broken


def validate_candidate(source: Bundle, files: dict[str, str] | None, counter, token_cap: int,
                       candidate_root: str | Path | None = None) -> dict:
    reasons, source_issues = [], {}
    original_tokens = bundle_count(source.files, counter)
    tokens = None
    if files is None:
        reasons.append("invalid_json")
    elif not isinstance(files, dict) or set(files) != set(source.files) or any(
            not valid_path(p) or not isinstance(t, str) for p, t in files.items()):
        reasons.append("missing_skill_file_or_unexpected_path")
    else:
        available = set(source.files) | set(source.support)
        for path, text in files.items():
            try:
                text.encode("utf-8")
                if split_front_matter(text)[0] != split_front_matter(source.files[path])[0]:
                    reasons.append("invalid_front_matter")
            except (ValueError, UnicodeError):
                reasons.append("invalid_front_matter_or_utf8")
            prior = fence_errors(source.files[path])
            old_links = broken_local_links(source.files[path], path, available)
            source_issues[path] = {"wrapper_errors": prior, "broken_links": sorted(old_links)}
            if set(fence_errors(text)) - set(prior):
                reasons.append("newly_broken_wrapper")
            if broken_local_links(text, path, available) - old_links:
                reasons.append("newly_broken_local_reference")
        tokens = bundle_count(files, counter)
        if tokens > token_cap:
            reasons.append("over_cap")
    if candidate_root is not None:
        actual = load_bundle(candidate_root)
        if actual.files != files or actual.support != source.support or actual.modes != source.modes:
            reasons.append("support_file_changed_or_bundle_mismatch")
    return {"eligible": not reasons, "ineligibility_reasons": sorted(set(reasons)),
            "original_tokens": original_tokens, "candidate_tokens": tokens, "token_cap": token_cap,
            "source_format_issues": source_issues}


def save_bundle(source: Bundle, files: dict[str, str], destination: str | Path) -> Path:
    if set(files) != set(source.files) or any(not valid_path(p) for p in files):
        raise ValueError("Candidate paths must exactly match source")
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=False)
    for path, data in {**source.support, **files}.items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        target.chmod(source.modes[path])
    return dest
