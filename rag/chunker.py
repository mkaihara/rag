#!/usr/bin/env python3
"""Split the corpus Markdown into section-aware chunks for retrieval.

Each document is split along its heading structure. A section becomes one
chunk if it fits the token budget; longer sections are split between
paragraphs, never inside a display equation ($$...$$), a table, a list item
or a code block. Reference lists (References / Bibliography sections) are
left out of the index.

Chunk sizes come from the active profile in config.yaml (RAG_PROFILE or
--profile). A block larger than the profile's max_tokens is split anyway:
tables into row groups rendered as text ("Row 12: A=..., B=..."), with the
table caption repeated in every group; algorithm listings and other long
blocks at line boundaries. Only a single oversized display equation is kept
whole.

Every chunk carries its context: document title, authors, source file,
section path (e.g. "Preliminaries > Montgomery Multiplication"), the page
numbers where the Markdown has page markers, and a short citation string
(`reference`). `embed_text` prefixes the chunk with title, authors and
section path so the embedding sees that context.

Output is JSON Lines, one chunk per line, plus a manifest.json with the
chunking parameters.

Usage:
    uv run python -m rag.chunker <data_dir> [--profile local|cloud] [--out out.jsonl]
    (default out: <data_dir>/../index/<profile>/chunks.jsonl)

The indexer (rag.ingest) calls chunk_corpus() directly; the CLI is for
inspecting the chunks.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import tiktoken
import yaml

from rag.config import ChunkParams, load_settings

# Sections whose content is excluded from the index (matched on the heading).
EXCLUDED_SECTIONS = re.compile(r"^\s*(references?|bibliography|literature cited)\s*$", re.I)

# Confirmed metadata for documents whose Markdown lacks a parseable title or
# author list (keyed by data subfolder). These take precedence over the file.
DOC_OVERRIDES = {
    "ECC_RSA_KeySizes": {
        "authors": ["Joppe W. Bos", "Marcelo E. Kaihara", "Thorsten Kleinjung", "Arjen K. Lenstra", "Peter L. Montgomery"],
    },
    "PhD_thesis": {
        "authors": ["Marcelo E. Kaihara"],
    },
    "Pollard_rho_Flash_Informatique": {
        "authors": ["Joppe W. Bos", "Marcelo E. Kaihara", "Thorsten Kleinjung", "Arjen K. Lenstra", "Dag Arne Osvik"],
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
    return meta, text[m.end() :]


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


Block = tuple[str, "int | None", "int | None"]  # (text, first_page, last_page)


def split_blocks(lines: list[tuple[str, int | None]]) -> list[Block]:
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


# --------------------------------------------------------------------------
# Oversized blocks: tables -> row groups as text, other blocks -> line groups


def pipe_positions(line: str) -> list[int]:
    """Positions of cell separators '|' outside $...$ math and backslash escapes."""
    pos, in_math, i = [], False, 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c == "$":
            in_math = not in_math
        elif c == "|" and not in_math:
            pos.append(i)
        i += 1
    return pos


def is_table(text: str) -> bool:
    lines = text.splitlines()
    return len(lines) >= 2 and all(l.lstrip().startswith(("|", "+")) for l in lines)


def parse_table(text: str) -> tuple[list[str], list[list[str]]] | None:
    """Parse a pandoc pipe or grid table into (column names, body rows).
    Grid-table header cells may span several body columns (e.g. a value and
    its decimal form); those body cells are merged under the one header."""
    lines = [l.rstrip() for l in text.splitlines()]
    if lines[0].startswith("+"):  # grid table: rows between +---+ separators
        groups, cur = [], []
        for l in lines:
            if l.startswith("+"):
                if cur:
                    groups.append(cur)
                cur = []
            else:
                cur.append(l)
        if cur:
            groups.append(cur)
        rows = []
        for g in groups:
            bounds = pipe_positions(g[0])
            cells = [
                (a, " ".join(l[a + 1 : b].strip() for l in g if len(l) > a).strip())
                for a, b in zip(bounds, bounds[1:], strict=False)
            ]
            rows.append(cells)
    else:  # pipe table: header, alignment line, body
        body = [l for l in lines[2:] if l.strip()]
        rows = []
        for l in [lines[0]] + body:
            bounds = pipe_positions(l)
            rows.append([(k, l[a + 1 : b].strip()) for k, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False))])
        # pipe tables have no spans: key cells by column index, not character position
    if len(rows) < 2:
        return None
    header, body_rows = rows[0], rows[1:]
    starts = [a for a, _ in header]
    names = [n or f"col{j + 1}" for j, (_, n) in enumerate(header)]
    out = []
    for row in body_rows:
        vals = [""] * len(names)
        for a, v in row:
            j = max([k for k, s in enumerate(starts) if s <= a], default=0)
            vals[j] = f"{vals[j]} {v}".strip()
        out.append(vals)
    return names, out


CAPTION_AFTER = re.compile(r"^:\s+(.*)", re.S)  # pandoc ": caption" after a table
CAPTION_BEFORE = re.compile(r"^\*\*Table\s+\d+\.?\*\*\s*(.*)", re.S)  # "**Table 2** ..." before


def table_caption(blocks: list[Block], i: int) -> str | None:
    if i + 1 < len(blocks) and (m := CAPTION_AFTER.match(blocks[i + 1][0])):
        return clean_inline(m.group(1))
    if i > 0 and CAPTION_BEFORE.match(blocks[i - 1][0]):
        caption = clean_inline(blocks[i - 1][0])
        return caption if len(caption) <= 300 else caption[:300].rsplit(" ", 1)[0] + " ..."
    return None


def split_table(block: Block, caption: str | None, params: ChunkParams) -> list[Block] | None:
    parsed = parse_table(block[0])
    if not parsed:
        return None
    names, rows = parsed
    lines = [f"Row {n}: " + ", ".join(f"{k}={v}" for k, v in zip(names, vals, strict=True) if v) for n, vals in enumerate(rows, 1)]
    title = caption or "Table"
    groups, cur, cur_tokens = [], [], 0
    for n, line in enumerate(lines, 1):
        t = count_tokens(line)
        if cur and cur_tokens + t > params.target_tokens:
            groups.append(cur)
            cur, cur_tokens = [], 0
        cur.append((n, line))
        cur_tokens += t
    if cur:
        groups.append(cur)
    return [
        (f"{title} (table rows {g[0][0]}-{g[-1][0]} of {len(rows)})\n" + "\n".join(l for _, l in g), block[1], block[2])
        for g in groups
    ]


SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")


def split_sentences(text: str) -> list[str]:
    """Split prose at sentence ends that are outside $...$ math."""
    out, start = [], 0
    for m in SENTENCE_END.finditer(text):
        if text[start : m.start()].replace("\\$", "").count("$") % 2 == 0 and "$$" not in text[start : m.start()]:
            out.append(text[start : m.start()])
            start = m.end()
    out.append(text[start:])
    return [s for s in out if s.strip()]


def split_lines(block: Block, params: ChunkParams) -> list[Block]:
    """Split a long block (e.g. an algorithm listing) at line boundaries,
    never inside $$ display math. Continuation pieces repeat the first line."""
    lines = block[0].splitlines()
    head = re.sub(r"[̀-ͯ­]", "", lines[0]).strip()  # drop stray combining overlines
    units, cur, in_display, in_inline = [], [], False, False
    for l in lines:  # units: single lines, or whole multi-line $$...$$ / $...$ spans
        cur.append(l)
        unescaped = l.replace("\\$", "")
        if unescaped.count("$$") % 2 == 1:
            in_display = not in_display
        if unescaped.replace("$$", "").count("$") % 2 == 1:
            in_inline = not in_inline
        if not in_display and not in_inline:
            units.append("\n".join(cur))
            cur = []
    if cur:
        units.append("\n".join(cur))
    # A single long line (a one-line paragraph) is split into sentences.
    units = [s for u in units for s in (split_sentences(u) if count_tokens(u) > params.target_tokens else [u])]
    pieces, buf, buf_tokens = [], [], 0
    for u in units:
        t = count_tokens(u)
        if buf and buf_tokens + t > params.target_tokens:
            pieces.append(buf)
            # Listings repeat their title line; prose (one long line) does not.
            buf, buf_tokens = ([f"{head} (continued)"], count_tokens(head) + 3) if len(head) <= 120 else ([], 0)
        buf.append(u)
        buf_tokens += t
    pieces.append(buf)
    sep = "\n" if len(lines) > 1 else " "
    return [(sep.join(p), block[1], block[2]) for p in pieces]


def split_oversized(blocks: list[Block], params: ChunkParams) -> list[Block]:
    out = []
    for i, block in enumerate(blocks):
        text = block[0]
        if count_tokens(text) <= params.max_tokens:
            out.append(block)
        elif is_table(text) and (rows := split_table(block, table_caption(blocks, i), params)):
            out += rows
        elif text.startswith("$$") and text.rstrip().endswith("$$") and text.count("$$") == 2:
            out.append(block)  # one display equation: splitting would break it
        else:
            out += split_lines(block, params)
    return out


# --------------------------------------------------------------------------
# Packing


def chunk_section(blocks: list[Block], params: ChunkParams) -> list[list[Block]]:
    """Pack consecutive blocks into chunks of about target_tokens."""
    chunks: list[list] = []
    cur: list = []
    cur_tokens = 0
    for block in blocks:
        n = count_tokens(block[0])
        if cur and (cur_tokens + n > params.max_tokens or cur_tokens >= params.target_tokens):
            chunks.append(cur)
            cur, cur_tokens = [], 0
        cur.append(block)
        cur_tokens += n
    if cur:
        # Avoid a tiny trailing chunk: merge it into the previous one if it fits.
        tail = sum(count_tokens(b[0]) for b in cur)
        if chunks and tail < params.target_tokens // 4 and tail + sum(count_tokens(b[0]) for b in chunks[-1]) <= params.max_tokens:
            chunks[-1].extend(cur)
        else:
            chunks.append(cur)
    return chunks


def merge_short_leads(sections: list[Section], params: ChunkParams) -> list[Section]:
    """A parent section often has a one-sentence lead-in before its first
    subsection. On its own it makes a near-empty chunk, so prepend it to the
    first subsection (whose path already includes the parent heading)."""
    merged: list[Section] = []
    for i, s in enumerate(sections):
        nxt = sections[i + 1] if i + 1 < len(sections) else None
        is_parent = nxt is not None and nxt.path[: len(s.path)] == s.path and len(nxt.path) > len(s.path)
        if is_parent and s.path and count_tokens("\n".join(l for l, _ in s.lines)) < params.min_lead_tokens:
            sections[i + 1] = Section(path=nxt.path, lines=s.lines + [("", None)] + nxt.lines)
        else:
            merged.append(s)
    return merged


def short_authors(authors: list[str]) -> str:
    last = [a.split()[-1] for a in authors]
    if len(last) <= 2:
        return " and ".join(last)
    return f"{last[0]} et al."


def build_chunks(doc: Document, params: ChunkParams) -> list[dict]:
    records = []
    for section in merge_short_leads(doc.sections, params):
        parts = chunk_section(split_oversized(split_blocks(section.lines), params), params)
        for i, part in enumerate(parts, 1):
            text = "\n\n".join(b[0] for b in part)
            pages = [p for b in part for p in (b[1], b[2]) if p is not None]
            page_range = [min(pages), max(pages)] if pages else None
            section_label = " > ".join(section.path) if section.path else "Front matter"
            reference = f'{short_authors(doc.authors)}, "{doc.title}", {section_label}'.lstrip(", ")
            if page_range:
                reference += f", p. {page_range[0]}" + (f"-{page_range[1]}" if page_range[1] != page_range[0] else "")
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
                    "pages": page_range,
                    "reference": reference,
                    "tokens": count_tokens(text),
                    "text": text,
                    "embed_text": (f"Document: {doc.title}\nAuthors: {', '.join(doc.authors)}\nSection: {section_label}\n\n{text}"),
                }
            )
    return records


def chunk_corpus(data: Path, params: ChunkParams, log=print) -> list[dict]:
    """Chunk every data/<doc>/*.md file. Returns the chunk records."""
    all_records = []
    for path in sorted(data.glob("*/*.md")):
        if path.parent.name in EXCLUDED_DOCS:
            log(f"{path.parent.name:36} excluded")
            continue
        doc = parse_document(path, data)
        records = build_chunks(doc, params)
        all_records += records
        tokens = [r["tokens"] for r in records]
        log(f"{doc.doc_id:36} {len(records):4} chunks  {sum(tokens):7} tokens  max {max(tokens):5}  | {doc.title[:60]}")
    if not all_records:
        raise SystemExit(f"no Markdown documents found under {data}/*/")
    return all_records


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data_dir", nargs="?", default="data")
    ap.add_argument("--profile", help="config.yaml profile (default: $RAG_PROFILE or default_profile)")
    ap.add_argument("--out", help="output JSONL (default: <data_dir>/../index/<profile>/chunks.jsonl)")
    args = ap.parse_args()

    settings = load_settings(args.profile)
    profile, params = settings.profile, settings.chunking
    data = Path(args.data_dir).resolve()
    out = Path(args.out).resolve() if args.out else data.parent / "index" / profile / "chunks.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"profile {profile}: target {params.target_tokens}, max {params.max_tokens} tokens\n")

    all_records = chunk_corpus(data, params)

    with out.open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    manifest = {
        "profile": profile,
        "tokenizer": "tiktoken o200k_base",
        **params.__dict__,
        "chunks": len(all_records),
        "tokens": sum(r["tokens"] for r in all_records),
        "max_chunk_tokens": max(r["tokens"] for r in all_records),
    }
    (out.parent / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    step = params.max_tokens // 4
    sizes = Counter(min(r["tokens"] // step, 4) for r in all_records)
    buckets = [f"<{step}", f"{step}-{2 * step - 1}", f"{2 * step}-{3 * step - 1}", f"{3 * step}-{4 * step - 1}", f">={4 * step}"]
    print(f"\n{len(all_records)} chunks, {manifest['tokens']} tokens -> {out}")
    print("size distribution: " + ", ".join(f"{buckets[k]}: {sizes.get(k, 0)}" for k in range(5)))
    for r in sorted(all_records, key=lambda r: -r["tokens"])[:3]:
        print(f"  largest: {r['tokens']:5}  {r['id']}  {r['section'][:60]}")


if __name__ == "__main__":
    main()
