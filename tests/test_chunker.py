from rag.chunker import parse_table, split_blocks, split_oversized, split_sentences
from rag.config import ChunkParams

SMALL = ChunkParams(target_tokens=40, max_tokens=60, min_lead_tokens=10)


def test_pipe_table_rows_and_empty_headers():
    names, rows = parse_table("|  | $A$ | $B$ |\n|:--|--:|--:|\n| 1 | $x|y$ | 3 |\n| 2 | 4 | 5 |")
    assert names == ["col1", "$A$", "$B$"]
    assert rows == [["1", "$x|y$", "3"], ["2", "4", "5"]]  # '|' inside math is not a separator


def test_grid_table_header_spanning_two_columns_merges_cells():
    table = (
        "+:----+:-----+:----+\n"
        "|     | $A$        |\n"
        "+-----+------+-----+\n"
        "| r1  | 1011 | (11)|\n"
        "+-----+------+-----+"
    )
    names, rows = parse_table(table)
    assert names == ["col1", "$A$"]
    assert rows == [["r1", "1011 (11)"]]


def test_oversized_table_becomes_row_groups_with_caption():
    header = "| f | area |\n|:--|--:|\n"
    body = "".join(f"| {i}00MHz | {i}.5mm² |\n" for i in range(1, 30))
    blocks = split_blocks([(line, 3) for line in (header + body + "\n: Synthesis results").splitlines()])
    out = split_oversized(blocks, SMALL)
    groups = [b for b in out if "(table rows" in b[0]]
    assert len(groups) > 1
    assert groups[0][0].startswith("Synthesis results (table rows 1-")
    assert "Row 12: f=1200MHz, area=12.5mm²" in "\n".join(g[0] for g in groups)
    assert all(g[1] == 3 for g in groups)  # page numbers carried over


def test_sentences_never_split_inside_math():
    text = "First $a. B$ stays. Second sentence. Third."
    assert split_sentences(text) == ["First $a. B$ stays.", "Second sentence.", "Third."]


def test_multiline_inline_math_is_not_split():
    lines = ["**[Algorithm 1]**"] + [f"step {i} of the listing with some words" for i in range(12)]
    lines += ["$\\begin{array}{l}", "x = 1 \\\\", "y = 2", "\\end{array}$"]
    block = ("\n".join(lines), None, None)
    pieces = split_oversized([block], SMALL)
    assert len(pieces) > 1
    for text, _, _ in pieces:
        assert text.replace("\\$", "").count("$") % 2 == 0
    assert pieces[1][0].startswith("**[Algorithm 1]** (continued)")
