"""Pinned conservative Markdown block segmenter, capstudy-markdown-v1.

Offsets are Unicode character indices; callers also persist byte offsets. Code,
tables and contiguous multiline lists are never split by the sentence segmenter.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    kind: str
    text: str


def markdown_blocks(body: str) -> list[Block]:
    lines = body.splitlines(keepends=True)
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    offsets.append(position)
    blocks, i = [], 0
    while i < len(lines):
        start, first = i, lines[i]
        stripped = first.strip()
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)", first)
        if fence:
            kind, marker = "code", fence.group(1)
            i += 1
            while i < len(lines):
                close = re.match(r"^ {0,3}(" + re.escape(marker[0]) + r"{" + str(len(marker)) + r",})\s*$", lines[i])
                i += 1
                if close:
                    break
        elif re.match(r"^ {0,3}#{1,6}\s", first):
            kind, i = "heading", i + 1
        elif not stripped:
            kind = "whitespace"
            while i < len(lines) and not lines[i].strip():
                i += 1
        else:
            kind = "list" if re.match(r"^\s*(?:[-+*]|\d+[.)])\s", first) else "table" if "|" in first else "prose"
            i += 1
            while i < len(lines) and lines[i].strip():
                if re.match(r"^ {0,3}(?:#{1,6}\s|`{3,}|~{3,})", lines[i]):
                    break
                i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        blocks.append(Block(offsets[start], offsets[i], kind, body[offsets[start]:offsets[i]]))
    return blocks
