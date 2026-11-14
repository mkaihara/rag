#!/usr/bin/env python3
"""Split the corpus Markdown into section-aware chunks for retrieval.

Each document is split along its heading structure. A section becomes one
chunk if it fits the token budget; longer sections are split between
paragraphs, never inside a display equation ($$...$$), a table, a list item
or a code block. Reference lists (References / Bibliography sections) are
left out of the index.

Every chunk carries its context: document title, authors, source file,
section path (e.g. "Preliminaries > Montgomery Multiplication") and, where
the Markdown has page markers, the page numbers. `embed_text` prefixes the
chunk with the title and section path so the embedding sees that context.

Output is JSON Lines, one chunk per line.

Usage:
    uv run --with tiktoken --with pyyaml scripts/chunk_md.py <data_dir> [out.jsonl]
    (default out: <data_dir>/../index/chunks.jsonl)
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import tiktoken
import yaml

TARGET_TOKENS = 700  # pack paragraphs up to about this size
MAX_TOKENS = 1000  # never exceed unless a single block is larger

# Sections whose content is excluded from the index (matched on the heading).
EXCLUDED_SECTIONS = re.compile(r"^\s*(references?|bibliography|literature cited)\s*$", re.I)

# Confirmed metadata for documents whose Markdown lacks a parseable title or
# author list (keyed by data subfolder). These take precedence over the file.
DOC_OVERRIDES = {
    "ModMulDiv_Bipartite_Pipelined": {
        "title": "Pipelined Bipartite Modular Multiplication",
        "authors": ["Marcelo E. Kaihara", "Naofumi Takagi"],
    },
    "ECC_RSA_KeySizes_md": {
        "authors": ["Joppe W. Bos", "Marcelo E. Kaihara", "Thorsten Kleinjung", "Arjen K. Lenstra", "Peter L. Montgomery"],
    },
    "ModMulDiv_Euclidean": {
        "authors": ["Marcelo E. Kaihara", "Naofumi Takagi"],
    },
    "Pollard_rho_Flash_Informatique_md": {
        "authors": ["Joppe W. Bos", "Marcelo E. Kaihara", "Thorsten Kleinjung", "Arjen K. Lenstra", "Dag Arne Osvik"],
    },
    "Pollard_rho_Journal_md": {
        "authors": ["Joppe W. Bos", "Marcelo E. Kaihara", "Thorsten Kleinjung", "Arjen K. Lenstra"],
    },
    "SelectedRNSBases_ARITH19": {
        "authors": ["Jean-Claude Bajard", "Marcelo Kaihara", "Thomas Plantard"],
    },
}

# Documents kept in data/ but left out of the index (by data subfolder).
EXCLUDED_DOCS: set[str] = set()

ENC = tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    return len(ENC.encode(text))


@dataclass
class Section:
    path: list[str]
    lines: list[tuple[str, int | None]] = field(default_factory=list)  # (line, page)


@dataclass
class Document:
    doc_id: str
    source: str
    title: str
    authors: list[str]
    sections: list[Section]


# --------------------------------------------------------------------------
# Parsing


def clean_inline(text: str) -> str:
    """Strip emphasis markers and collapse whitespace (for titles/headings)."""
    text = re.sub(r"\*\*|__|(?<!\\)\*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, text[m.end():]


AFFILIATION = re.compile(r"universit|department|school|laborator|degree|thesis|@|\d{4}|japan", re.I)


def pick_title(raw: str | None) -> str | None:
    """Front-matter titles may include institution/degree/date lines; keep the title."""
    if not raw:
        return None
    lines = [clean_inline(l) for l in str(raw).splitlines() if clean_inline(l)]
    candidates = [l for l in lines if not AFFILIATION.search(l)] or lines
    return max(candidates, key=len) if len(lines) > 1 else lines[0]


def pick_authors(raw) -> list[str]:
    """Authors may be a list of names or one block with names and affiliation."""
    if not raw:
        return []
    entries = raw if isinstance(raw, list) else [raw]
    names: list[str] = []
    for entry in entries:
        first_line = clean_inline(str(entry).strip().splitlines()[0])
        names += [n.strip() for n in re.split(r"\s+and\s+|,", first_line) if n.strip()]
    return names


HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
PAGE_MARKER = re.compile(r"<!--\s*(?:page|p\.)\s*(\d+)\s*-->", re.I)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def parse_document(path: Path, data: Path) -> Document:
    text = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(text)
    doc_id = path.parent.name

    override = DOC_OVERRIDES.get(doc_id, {})
    title = override.get("title") or pick_title(meta.get("title"))
    authors = override.get("authors") or pick_authors(meta.get("author"))

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (level, heading)
    current = Section(path=[])
    page: int | None = None
    in_fence = False
    title_heading_level: int | None = None

    for line in body.splitlines():
        # Page markers are tracked and removed; other comments just removed.
        for m in PAGE_MARKER.finditer(line):
            page = int(m.group(1))
        line = HTML_COMMENT.sub("", line).rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
        h = None if in_fence else HEADING.match(line)
        if h:
            level, heading = len(h.group(1)), clean_inline(h.group(2))
            # Documents without front matter use their first heading as title.
            if title is None and not stack and not sections and heading.lower() not in {"abstract", "introduction"}:
                title, title_heading_level = heading, level
                continue
            if level == title_heading_level:  # deeper headings start at the title level
                level += 1
            sections.append(current)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            current = Section(path=[h for _, h in stack])
            continue
        current.lines.append((line, page))
    sections.append(current)

    # Drop excluded sections (and their subsections) and empty sections.
    kept = []
    for s in sections:
        if any(EXCLUDED_SECTIONS.match(h) for h in s.path):
            continue
        if not any(l.strip() for l, _ in s.lines):
            continue
        kept.append(s)

    return Document(
        doc_id=doc_id,
        source=str(path.relative_to(data)),
        title=title or doc_id.replace("_", " "),
        authors=authors,
        sections=kept,
    )


# --------------------------------------------------------------------------
# Chunking


def split_blocks(lines: list[tuple[str, int | None]]) -> list[tuple[str, int | None, int | None]]:
    """Group lines into blocks separated by blank lines, keeping display math,
    code fences and tables intact. Returns (text, first_page, last_page)."""
    blocks = []
    buf: list[tuple[str, int | None]] = []
    in_math = in_fence = False

    def flush():
        if buf and any(l.strip() for l, _ in buf):
            pages = [p for _, p in buf if p is not None]
            blocks.append(("\n".join(l for l, _ in buf).strip("\n"), pages[0] if pages else None, pages[-1] if pages else None))
        buf.clear()

    for line, page in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and stripped.count("$$") % 2 == 1:
            in_math = not in_math
        if not stripped and not in_math and not in_fence:
            flush()
            continue
        buf.append((line, page))
    flush()
    return blocks


def chunk_section(blocks: list[tuple[str, int | None, int | None]]) -> list[list[tuple[str, int | None, int | None]]]:
    """Pack consecutive blocks into chunks of about TARGET_TOKENS."""
    chunks: list[list] = []
    cur: list = []
    cur_tokens = 0
    for block in blocks:
        n = count_tokens(block[0])
        if cur and (cur_tokens + n > MAX_TOKENS or cur_tokens >= TARGET_TOKENS):
            chunks.append(cur)
            cur, cur_tokens = [], 0
        cur.append(block)
        cur_tokens += n
    if cur:
        # Avoid a tiny trailing chunk: merge it into the previous one if it fits.
        tail = sum(count_tokens(b[0]) for b in cur)
        if chunks and tail < TARGET_TOKENS // 4 and tail + sum(count_tokens(b[0]) for b in chunks[-1]) <= MAX_TOKENS:
            chunks[-1].extend(cur)
        else:
            chunks.append(cur)
    return chunks


MIN_LEAD_TOKENS = 120  # shorter section lead-ins are merged into their first subsection


def merge_short_leads(sections: list[Section]) -> list[Section]:
    """A parent section often has a one-sentence lead-in before its first
    subsection. On its own it makes a near-empty chunk, so prepend it to the
    first subsection (whose path already includes the parent heading)."""
    merged: list[Section] = []
    i = 0
    while i < len(sections):
        s = sections[i]
        nxt = sections[i + 1] if i + 1 < len(sections) else None
        is_parent = nxt is not None and nxt.path[: len(s.path)] == s.path and len(nxt.path) > len(s.path)
        if is_parent and s.path and count_tokens("\n".join(l for l, _ in s.lines)) < MIN_LEAD_TOKENS:
            sections[i + 1] = Section(path=nxt.path, lines=s.lines + [("", None)] + nxt.lines)
        else:
            merged.append(s)
        i += 1
    return merged


def build_chunks(doc: Document) -> list[dict]:
    records = []
    for section in merge_short_leads(doc.sections):
        parts = chunk_section(split_blocks(section.lines))
        for i, part in enumerate(parts, 1):
            text = "\n\n".join(b[0] for b in part)
            pages = [p for b in part for p in (b[1], b[2]) if p is not None]
            section_label = " > ".join(section.path) if section.path else "Front matter"
            records.append(
                {
                    "id": f"{doc.doc_id}#{len(records):04d}",
                    "doc_id": doc.doc_id,
                    "source": doc.source,
                    "title": doc.title,
                    "authors": doc.authors,
                    "section_path": section.path,
                    "section": section_label,
                    "part": f"{i}/{len(parts)}",
                    "pages": [min(pages), max(pages)] if pages else None,
                    "tokens": count_tokens(text),
                    "text": text,
                    "embed_text": f"Document: {doc.title}\nSection: {section_label}\n\n{text}",
                }
            )
    return records


def main() -> None:
    data = Path(sys.argv[1] if len(sys.argv) > 1 else "data").resolve()
    out = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else data.parent / "index" / "chunks.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    all_records = []
    for path in sorted(data.glob("*/*.md")):
        if path.parent.name in EXCLUDED_DOCS:
            print(f"{path.parent.name:36} excluded")
            continue
        doc = parse_document(path, data)
        records = build_chunks(doc)
        all_records += records
        tokens = [r["tokens"] for r in records]
        print(f"{doc.doc_id:36} {len(records):4} chunks  {sum(tokens):7} tokens  max {max(tokens):5}  | {doc.title[:60]}")

    with out.open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    sizes = Counter(min(r["tokens"] // 250, 4) for r in all_records)
    buckets = ["<250", "250-499", "500-749", "750-999", ">=1000"]
    print(f"\n{len(all_records)} chunks, {sum(r['tokens'] for r in all_records)} tokens -> {out}")
    print("size distribution: " + ", ".join(f"{buckets[k]}: {sizes.get(k, 0)}" for k in range(5)))


if __name__ == "__main__":
    main()
