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
    lines = []
    for line in str(raw).splitlines():
        cleaned = clean_inline(line)
        if cleaned:
            lines.append(cleaned)
    if len(lines) == 1:
        return lines[0]

    # Prefer lines that do not look like an affiliation, a degree or a date.
    candidates = []
    for line in lines:
        if not AFFILIATION.search(line):
            candidates.append(line)
    if not candidates:
        candidates = lines

    # The longest candidate (the first one, if several are equally long).
    longest = candidates[0]
    for line in candidates:
        if len(line) > len(longest):
            longest = line
    return longest


def pick_authors(raw) -> list[str]:
    """Authors may be a list of names or one block with names and affiliation."""
    if not raw:
        return []
    if isinstance(raw, list):
        entries = raw
    else:
        entries = [raw]
    names: list[str] = []
    for entry in entries:
        # Only the first line has names; the lines below are the affiliation.
        first_line = clean_inline(str(entry).strip().splitlines()[0])
        for name in re.split(r"\s+and\s+|,", first_line):
            name = name.strip()
            if name:
                names.append(name)
    return names


HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
PAGE_MARKER = re.compile(r"<!--\s*(?:page|p\.)\s*(\d+)\s*-->", re.I)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def parse_document(path: Path, data: Path) -> Document:
    text = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(text)
    doc_id = path.parent.name

    override = DOC_OVERRIDES.get(doc_id, {})
    title = override.get("title")
    if not title:
        title = pick_title(meta.get("title"))
    authors = override.get("authors")
    if not authors:
        authors = pick_authors(meta.get("author"))

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (level, heading) of the open headings
    current = Section(path=[])
    page: int | None = None
    in_fence = False
    title_heading_level: int | None = None

    for line in body.splitlines():
        # Page markers are tracked and removed; other comments just removed.
        for marker in PAGE_MARKER.finditer(line):
            page = int(marker.group(1))
        line = HTML_COMMENT.sub("", line).rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence

        # A "#" line inside a code fence is code, not a heading.
        heading_match = None
        if not in_fence:
            heading_match = HEADING.match(line)
        if not heading_match:
            current.lines.append((line, page))
            continue

        level = len(heading_match.group(1))
        heading = clean_inline(heading_match.group(2))

        # Documents without front matter use their first heading as title.
        is_first_heading = not stack and not sections
        if title is None and is_first_heading and heading.lower() not in {"abstract", "introduction"}:
            title = heading
            title_heading_level = level
            continue
        if level == title_heading_level:  # deeper headings start at the title level
            level += 1

        # Close the current section and open a new one under its parents.
        sections.append(current)
        while stack:
            last_level = stack[-1][0]
            if last_level < level:
                break
            stack.pop()
        stack.append((level, heading))
        heading_path = []
        for _level, open_heading in stack:
            heading_path.append(open_heading)
        current = Section(path=heading_path)
    sections.append(current)

    # Drop excluded sections (and their subsections) and empty sections.
    kept = []
    for section in sections:
        is_excluded = False
        for heading in section.path:
            if EXCLUDED_SECTIONS.match(heading):
                is_excluded = True
        has_text = False
        for line, _page in section.lines:
            if line.strip():
                has_text = True
        if is_excluded or not has_text:
            continue
        kept.append(section)

    if not title:
        title = doc_id.replace("_", " ")
    return Document(
        doc_id=doc_id,
        source=str(path.relative_to(data)),
        title=title,
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
    in_math = False
    in_fence = False

    def flush():
        texts = []
        pages = []
        for line, page in buf:
            texts.append(line)
            if page is not None:
                pages.append(page)
        has_text = False
        for line in texts:
            if line.strip():
                has_text = True
        if has_text:
            block_text = "\n".join(texts).strip("\n")
            first_page = None
            last_page = None
            if pages:
                first_page = pages[0]
                last_page = pages[-1]
            blocks.append((block_text, first_page, last_page))
        buf.clear()

    for line, page in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and stripped.count("$$") % 2 == 1:
            in_math = not in_math
        # A blank line ends a block, unless it is inside math or a code fence.
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
    positions = []
    in_math = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\":  # skip the escaped character too
            i += 2
            continue
        if c == "$":
            in_math = not in_math
        elif c == "|" and not in_math:
            positions.append(i)
        i += 1
    return positions


def is_table(text: str) -> bool:
    """Every line starts with "|" (pipe table) or "+" (grid table)."""
    lines = text.splitlines()
    if len(lines) < 2:
        return False
    for line in lines:
        if not line.lstrip().startswith(("|", "+")):
            return False
    return True


def parse_table(text: str) -> tuple[list[str], list[list[str]]] | None:
    """Parse a pandoc pipe or grid table into (column names, body rows).
    Grid-table header cells may span several body columns (e.g. a value and
    its decimal form); those body cells are merged under the one header."""
    lines = []
    for line in text.splitlines():
        lines.append(line.rstrip())

    # Each row is a list of (key, cell text). A grid-table key is the
    # character position where the cell starts; a pipe-table key is the
    # column number.
    rows = []
    if lines[0].startswith("+"):
        # Grid table: a row is the group of lines between +---+ separators.
        groups = []
        group = []
        for line in lines:
            if line.startswith("+"):
                if group:
                    groups.append(group)
                group = []
            else:
                group.append(line)
        if group:
            groups.append(group)

        for group in groups:
            bounds = pipe_positions(group[0])
            cells = []
            for left, right in zip(bounds, bounds[1:], strict=False):
                # A cell may wrap over several lines: join its pieces.
                pieces = []
                for line in group:
                    if len(line) > left:
                        pieces.append(line[left + 1 : right].strip())
                cell_text = " ".join(pieces).strip()
                cells.append((left, cell_text))
            rows.append(cells)
    else:
        # Pipe table: header line, alignment line, then the body lines.
        # Pipe tables have no spans: key cells by column index, not character position.
        table_lines = [lines[0]]
        for line in lines[2:]:
            if line.strip():
                table_lines.append(line)
        for line in table_lines:
            bounds = pipe_positions(line)
            cells = []
            column = 0
            for left, right in zip(bounds, bounds[1:], strict=False):
                cells.append((column, line[left + 1 : right].strip()))
                column += 1
            rows.append(cells)

    if len(rows) < 2:
        return None
    header = rows[0]
    body_rows = rows[1:]

    starts = []
    names = []
    for j, (start, name) in enumerate(header):
        starts.append(start)
        if not name:
            name = f"col{j + 1}"
        names.append(name)

    out = []
    for row in body_rows:
        values = [""] * len(names)
        for key, value in row:
            # The cell belongs to the last header column starting at or before it.
            column = 0
            for k, start in enumerate(starts):
                if start <= key:
                    column = k
            values[column] = f"{values[column]} {value}".strip()
        out.append(values)
    return names, out


CAPTION_AFTER = re.compile(r"^:\s+(.*)", re.S)  # pandoc ": caption" after a table
CAPTION_BEFORE = re.compile(r"^\*\*Table\s+\d+\.?\*\*\s*(.*)", re.S)  # "**Table 2** ..." before


def table_caption(blocks: list[Block], i: int) -> str | None:
    # A pandoc caption (": ...") right after the table.
    if i + 1 < len(blocks):
        after = CAPTION_AFTER.match(blocks[i + 1][0])
        if after:
            return clean_inline(after.group(1))
    # A "**Table 2** ..." paragraph right before it, shortened to about 300 characters.
    if i > 0 and CAPTION_BEFORE.match(blocks[i - 1][0]):
        caption = clean_inline(blocks[i - 1][0])
        if len(caption) <= 300:
            return caption
        cut_at_word = caption[:300].rsplit(" ", 1)[0]
        return cut_at_word + " ..."
    return None


def split_table(block: Block, caption: str | None, params: ChunkParams) -> list[Block] | None:
    parsed = parse_table(block[0])
    if not parsed:
        return None
    names, rows = parsed

    # One text line per row: "Row 3: A=..., B=..." (empty cells left out).
    lines = []
    for row_number, values in enumerate(rows, start=1):
        cells = []
        for name, value in zip(names, values, strict=True):
            if value:
                cells.append(f"{name}={value}")
        lines.append(f"Row {row_number}: " + ", ".join(cells))

    # Pack the rows into groups of about target_tokens.
    groups = []
    group = []  # (row number, line)
    group_tokens = 0
    for row_number, line in enumerate(lines, start=1):
        line_tokens = count_tokens(line)
        if group and group_tokens + line_tokens > params.target_tokens:
            groups.append(group)
            group = []
            group_tokens = 0
        group.append((row_number, line))
        group_tokens += line_tokens
    if group:
        groups.append(group)

    # Every group starts with the caption and the rows it holds.
    title = caption or "Table"
    first_page, last_page = block[1], block[2]
    pieces = []
    for group in groups:
        first_row = group[0][0]
        last_row = group[-1][0]
        heading = f"{title} (table rows {first_row}-{last_row} of {len(rows)})"
        group_lines = []
        for _row_number, line in group:
            group_lines.append(line)
        pieces.append((heading + "\n" + "\n".join(group_lines), first_page, last_page))
    return pieces


SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")


def split_sentences(text: str) -> list[str]:
    """Split prose at sentence ends that are outside $...$ math."""
    sentences = []
    start = 0
    for match in SENTENCE_END.finditer(text):
        sentence = text[start : match.start()]
        # An odd number of unescaped "$" means the break is inside math.
        dollars = sentence.replace("\\$", "").count("$")
        inside_math = dollars % 2 == 1 or "$$" in sentence
        if not inside_math:
            sentences.append(sentence)
            start = match.end()
    sentences.append(text[start:])

    result = []
    for sentence in sentences:
        if sentence.strip():
            result.append(sentence)
    return result


def split_lines(block: Block, params: ChunkParams) -> list[Block]:
    """Split a long block (e.g. an algorithm listing) at line boundaries,
    never inside $$ display math. Continuation pieces repeat the first line."""
    lines = block[0].splitlines()
    head = re.sub(r"[̀-ͯ­]", "", lines[0]).strip()  # drop stray combining overlines

    # Units: single lines, or whole multi-line $$...$$ / $...$ spans.
    units = []
    unit_lines = []
    in_display = False
    in_inline = False
    for line in lines:
        unit_lines.append(line)
        unescaped = line.replace("\\$", "")
        if unescaped.count("$$") % 2 == 1:
            in_display = not in_display
        if unescaped.replace("$$", "").count("$") % 2 == 1:
            in_inline = not in_inline
        if not in_display and not in_inline:
            units.append("\n".join(unit_lines))
            unit_lines = []
    if unit_lines:
        units.append("\n".join(unit_lines))

    # A single long line (a one-line paragraph) is split into sentences.
    small_units = []
    for unit in units:
        if count_tokens(unit) > params.target_tokens:
            small_units.extend(split_sentences(unit))
        else:
            small_units.append(unit)

    # Pack the units into pieces of about target_tokens.
    pieces = []
    piece = []
    piece_tokens = 0
    for unit in small_units:
        unit_tokens = count_tokens(unit)
        if piece and piece_tokens + unit_tokens > params.target_tokens:
            pieces.append(piece)
            # Listings repeat their title line; prose (one long line) does not.
            if len(head) <= 120:
                piece = [f"{head} (continued)"]
                piece_tokens = count_tokens(head) + 3
            else:
                piece = []
                piece_tokens = 0
        piece.append(unit)
        piece_tokens += unit_tokens
    pieces.append(piece)

    if len(lines) > 1:
        separator = "\n"
    else:
        separator = " "
    result = []
    for piece in pieces:
        result.append((separator.join(piece), block[1], block[2]))
    return result


def split_oversized(blocks: list[Block], params: ChunkParams) -> list[Block]:
    out = []
    for i, block in enumerate(blocks):
        text = block[0]
        if count_tokens(text) <= params.max_tokens:
            out.append(block)
            continue
        if is_table(text):
            row_groups = split_table(block, table_caption(blocks, i), params)
            if row_groups:
                out.extend(row_groups)
                continue
        is_one_equation = text.startswith("$$") and text.rstrip().endswith("$$") and text.count("$$") == 2
        if is_one_equation:
            out.append(block)  # one display equation: splitting would break it
            continue
        out.extend(split_lines(block, params))
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
        too_big = cur_tokens + n > params.max_tokens
        full_enough = cur_tokens >= params.target_tokens
        if cur and (too_big or full_enough):
            chunks.append(cur)
            cur = []
            cur_tokens = 0
        cur.append(block)
        cur_tokens += n
    if not cur:
        return chunks

    # Avoid a tiny trailing chunk: merge it into the previous one if it fits.
    tail_tokens = cur_tokens
    if chunks and tail_tokens < params.target_tokens // 4:
        previous_tokens = 0
        for block in chunks[-1]:
            previous_tokens += count_tokens(block[0])
        if tail_tokens + previous_tokens <= params.max_tokens:
            chunks[-1].extend(cur)
            return chunks
    chunks.append(cur)
    return chunks


def merge_short_leads(sections: list[Section], params: ChunkParams) -> list[Section]:
    """A parent section often has a one-sentence lead-in before its first
    subsection. On its own it makes a near-empty chunk, so prepend it to the
    first subsection (whose path already includes the parent heading)."""
    merged: list[Section] = []
    # The loop reads sections[i] as it goes, so a lead-in prepended to
    # sections[i + 1] is seen on the next round.
    for i, s in enumerate(sections):
        nxt = None
        if i + 1 < len(sections):
            nxt = sections[i + 1]

        # The next section is a subsection of this one.
        is_parent = False
        if nxt is not None:
            starts_with_our_path = nxt.path[: len(s.path)] == s.path
            is_deeper = len(nxt.path) > len(s.path)
            is_parent = starts_with_our_path and is_deeper

        if is_parent and s.path:
            lead_lines = []
            for line, _page in s.lines:
                lead_lines.append(line)
            is_short = count_tokens("\n".join(lead_lines)) < params.min_lead_tokens
            if is_short:
                blank_line = ("", None)
                sections[i + 1] = Section(path=nxt.path, lines=s.lines + [blank_line] + nxt.lines)
                continue
        merged.append(s)
    return merged


def short_authors(authors: list[str]) -> str:
    """ "Bos and Kaihara", or "Bos et al." for three or more authors."""
    last_names = []
    for author in authors:
        last_names.append(author.split()[-1])
    if len(last_names) <= 2:
        return " and ".join(last_names)
    return f"{last_names[0]} et al."


def build_chunks(doc: Document, params: ChunkParams) -> list[dict]:
    records = []
    for section in merge_short_leads(doc.sections, params):
        blocks = split_blocks(section.lines)
        blocks = split_oversized(blocks, params)
        parts = chunk_section(blocks, params)

        if section.path:
            section_label = " > ".join(section.path)
        else:
            section_label = "Front matter"

        for i, part in enumerate(parts, start=1):
            block_texts = []
            pages = []
            for block_text, first_page, last_page in part:
                block_texts.append(block_text)
                if first_page is not None:
                    pages.append(first_page)
                if last_page is not None:
                    pages.append(last_page)
            text = "\n\n".join(block_texts)

            page_range = None
            if pages:
                page_range = [min(pages), max(pages)]

            # e.g. 'Bos et al., "Title", Section > Subsection, p. 3-4'. With
            # no authors, lstrip removes the leading ", ".
            reference = f'{short_authors(doc.authors)}, "{doc.title}", {section_label}'.lstrip(", ")
            if page_range:
                first, last = page_range
                if first == last:
                    reference += f", p. {first}"
                else:
                    reference += f", p. {first}-{last}"

            authors_line = ", ".join(doc.authors)
            embed_text = f"Document: {doc.title}\nAuthors: {authors_line}\nSection: {section_label}\n\n{text}"
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
                    "embed_text": embed_text,
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
        all_records.extend(records)

        tokens = []
        for record in records:
            tokens.append(record["tokens"])
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
    profile = settings.profile
    params = settings.chunking
    data = Path(args.data_dir).resolve()
    if args.out:
        out = Path(args.out).resolve()
    else:
        out = data.parent / "index" / profile / "chunks.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"profile {profile}: target {params.target_tokens}, max {params.max_tokens} tokens\n")

    all_records = chunk_corpus(data, params)

    with out.open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    token_counts = []
    for r in all_records:
        token_counts.append(r["tokens"])
    manifest = {"profile": profile, "tokenizer": "tiktoken o200k_base"}
    for name, value in params.__dict__.items():
        manifest[name] = value
    manifest["chunks"] = len(all_records)
    manifest["tokens"] = sum(token_counts)
    manifest["max_chunk_tokens"] = max(token_counts)
    (out.parent / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Size distribution in five buckets of max_tokens / 4 each; the last is open-ended.
    step = params.max_tokens // 4
    sizes = Counter()
    for tokens in token_counts:
        bucket = min(tokens // step, 4)
        sizes[bucket] += 1
    buckets = [f"<{step}", f"{step}-{2 * step - 1}", f"{2 * step}-{3 * step - 1}", f"{3 * step}-{4 * step - 1}", f">={4 * step}"]
    print(f"\n{len(all_records)} chunks, {manifest['tokens']} tokens -> {out}")
    counts = []
    for k in range(5):
        counts.append(f"{buckets[k]}: {sizes.get(k, 0)}")
    print("size distribution: " + ", ".join(counts))

    def token_count(record):
        return record["tokens"]

    largest = sorted(all_records, key=token_count, reverse=True)
    for r in largest[:3]:
        print(f"  largest: {r['tokens']:5}  {r['id']}  {r['section'][:60]}")


if __name__ == "__main__":
    main()
