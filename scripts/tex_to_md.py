#!/usr/bin/env python3
"""Convert the LaTeX papers and theses in the corpus to Markdown.

Each document's main .tex file becomes a Markdown file next to it, with
mathematics kept as LaTeX ($...$ inline, $$...$$ display) so it can be
rendered with KaTeX/MathJax and indexed as text.

Usage:
    uv run --with pypandoc-binary scripts/tex_to_md.py <data_dir>
"""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pypandoc


@dataclass
class Doc:
    folder: str
    main: str | None  # main .tex (None = build from `parts`)
    out: str
    title: str | None = None
    parts: list[tuple[str | None, str]] = field(default_factory=list)  # (section heading, file)
    bbl: str | None = None


DOCS = [
    Doc("ModMul_Bipartite_CHES", "Bipartite_Modular_Multiplication.tex", "Bipartite_Modular_Multiplication.md"),
    Doc("ModMulDiv_Binary_ARITH16", "180Kaihara.tex", "180Kaihara.md"),
    Doc("ModMulDiv_Binary_TC", "paper.tex", "paper.md"),
    Doc("ModMulDiv_Bipartite_Pipelined", "Pipelined_Bipartite_VLD.tex", "Pipelined_Bipartite_VLD.md"),
    Doc("ModMulDiv_Euclidean", "paper.tex", "paper.md"),
    Doc("Montomery_Cell", "foldedmonmul.tex", "foldedmonmul.md", bbl="foldedmonmul.bbl"),
    Doc("Pollard_rho_SHARCS", "sharcs.tex", "sharcs.md", bbl="sharcs.bbl"),
    Doc("SelectedRNSBases_ARITH19", "BKP2008-v16.tex", "BKP2008-v16.md"),
    Doc("PhD_thesis", "thesis.tex", "thesis.md", bbl="thesis.bbl"),
    # The Master's thesis main file holds only the title pages; the chapter
    # files are standalone fragments without top-level headings.
    Doc(
        "Master_thesis",
        None,
        "thesis.md",
        title="A VLSI Algorithm for Modular Multiplication/Division (Master's Thesis, Nagoya University, 2003)",
        parts=[
            ("Abstract", "abstract/body.tex"),
            ("Introduction", "Introduction.tex"),
            ("Preliminaries", "Preliminaries.tex"),
            ("VLSI Algorithm", "VLSIalgorithm.tex"),
            ("Circuit Design", "CircuitDesign.tex"),
            ("Conclusion", "Conclusion.tex"),
        ],
        bbl="thesis.bbl",
    ),
]


def read_tex(path: Path) -> str:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_comments(tex: str) -> str:
    # Drop % comments (not \%). Old files carry Japanese/EUC junk in comments.
    return re.sub(r"(?<!\\)%.*", "", tex)


# Commands that only affect layout or reference missing assets.
DROP_PATTERNS = [
    r"\\includegraphics(\[[^\]]*\])?\{[^}]*\}",
    r"\\epsfig\{[^}]*\}",
    r"\\psfig\{[^}]*\}",
    r"\\usebackgroundtemplate\{[^{}]*(\{[^}]*\})?[^}]*\}",
    r"\\(tableofcontents|listoffigures|listoftables|printindex|makeindex|titlepage|cleardoublepage|clearpage|newpage)\b",
    r"\\bibliographystyle\{[^}]*\}",
    r"\\addcontentsline\{[^}]*\}\{[^}]*\}\{[^}]*\}",
    # Line-spacing environments (setspace/doublespace) confuse pandoc's parser.
    r"\\begin\{(spacing|doublespace|singlespace|onehalfspace)\}(\{[^}]*\})?",
    r"\\end\{(spacing|doublespace|singlespace|onehalfspace)\}",
    # The papers define \qed with a TeX primitive box pandoc cannot parse.
    r"\\newcommand\{\\qed\}\{\\hbox\{\\rule\[[^\]]*\]\{[^}]*\}\{[^}]*\}\}\}",
]

REPLACEMENTS = [
    (r"\\qed\b", r"$\\square$"),
    # Pandoc drops the "Abstract"/"Keywords" labels; make them headings/text.
    (r"\\begin\{(abstract|summary)\}", r"\\section*{Abstract}"),
    (r"\\end\{(abstract|summary)\}", ""),
    (r"\\begin\{keywords\}", r"\\textbf{Keywords:} "),
    (r"\\end\{keywords\}", ""),
    (r"\\keywords\{", r"\\textbf{Keywords:} {"),
]


def preprocess(tex: str, folder: Path, bbl: str | None) -> str:
    tex = strip_comments(tex)
    # Inline \include/\input so the cleanup below applies to the included files too.
    def inline(m: re.Match) -> str:
        name = m.group(2).strip()
        p = folder / (name if name.endswith(".tex") else name + ".tex")
        return strip_comments(read_tex(p)) if p.exists() else ""
    for _ in range(3):
        tex = re.sub(r"\\(include|input)\{([^}]*)\}", inline, tex)
    for pat in DROP_PATTERNS:
        tex = re.sub(pat, "", tex)
    for pat, repl in REPLACEMENTS:
        tex = re.sub(pat, repl, tex)
    # Bibliography: use the inline thebibliography if present, else the .bbl.
    has_inline_bib = "\\begin{thebibliography}" in tex
    bbl_text = strip_comments(read_tex(folder / bbl)) if bbl and not has_inline_bib else ""
    if re.search(r"\\bibliography\{[^}]*\}", tex):
        tex = re.sub(r"\\bibliography\{[^}]*\}", lambda _m: bbl_text, tex, count=1)
        tex = re.sub(r"\\bibliography\{[^}]*\}", "", tex)
    elif bbl_text:
        tex = tex.replace("\\end{document}", bbl_text + "\n\\end{document}")
    return number_citations(tex)


def number_citations(tex: str) -> str:
    """Render \\cite and \\bibitem as plain numbered references, e.g. [3, 7].

    Pandoc's Markdown writer would otherwise emit pandoc-citation syntax or
    drop the citations, and the bibliography entries would lose their numbers.
    """
    # Harvard (author-year) bibliographies, used by the PhD thesis:
    # \harvarditem[Short]{Full authors}{year}{key}  ->  [Short, year]
    harvard = re.compile(r"\\harvarditem\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\{([^}]*)\}")
    keys: dict[str, int | str] = {}
    for m in harvard.finditer(tex):
        who = re.sub(r"\s+", " ", (m.group(1) or m.group(2)).replace("~", " ")).strip()
        keys.setdefault(m.group(4).strip(), f"{who}, {m.group(3).strip()}")
    if keys:
        tex = re.sub(r"\\vspace\{[^}]*\}", "", tex)
        tex = harvard.sub(lambda m: f"\n\n[{keys[m.group(4).strip()]}] ", tex)
    for m in re.finditer(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", tex):
        keys.setdefault(m.group(1).strip(), len(keys) + 1)

    def cite(m: re.Match) -> str:
        nums = [str(keys.get(k.strip(), k.strip())) for k in m.group(2).split(",")]
        if m.group(1):
            nums.append(m.group(1))
        return "[" + ", ".join(nums) + "]"

    tex = re.sub(r"\\cite[pt]?\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}", cite, tex)
    tex = re.sub(
        r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}",
        lambda m: f"\n\n[{keys[m.group(1).strip()]}] ",
        tex,
    )
    tex = re.sub(r"\\begin\{thebibliography\}\{[^}]*\}", r"\\section*{References}", tex)
    tex = tex.replace("\\end{thebibliography}", "")
    return tex.replace("\\newblock", " ")


def build_from_parts(doc: Doc, folder: Path) -> str:
    body = []
    for heading, fname in doc.parts:
        body.append(f"\\section{{{heading}}}\n" + strip_comments(read_tex(folder / fname)))
    return (
        "\\documentclass{article}\n"
        f"\\title{{{doc.title}}}\n\\author{{Marcelo E. Kaihara}}\n"
        "\\begin{document}\n\\maketitle\n" + "\n\n".join(body) + "\n\\end{document}\n"
    )


# Pandoc Markdown, minus everything that would leave HTML, attributes or
# pandoc-only syntax in the output. Math stays as $...$ / $$...$$.
MD_FORMAT = "markdown" + "".join(
    f"-{ext}"
    for ext in [
        "raw_html", "native_divs", "native_spans", "fenced_divs", "bracketed_spans",
        "header_attributes", "link_attributes", "inline_code_attributes",
        "fenced_code_attributes", "implicit_figures", "raw_attribute", "raw_tex",
        "citations", "smart", "auto_identifiers", "escaped_line_breaks",
        "subscript", "superscript", "simple_tables", "multiline_tables",
    ]
)


NUMBERED_EQ_ENVS = ("eqnarray", "align", "gather", "multline")


def label_numbers(tex: str) -> dict[str, str]:
    """Reproduce LaTeX's numbering for \\label targets.

    Pandoc cannot number labels that sit inside algorithm floats, \\caption or
    sectioning arguments, or equations wrapped in \\fbox/\\parbox; it emits
    them as [[label]](#label). This walks the (comment-free) source the way
    LaTeX's counters do: sections, equations (row by row in eqnarray-style
    environments), tables, figures and algorithms, reset per chapter when the
    document has chapters, and records the counter a \\label attaches to.
    """
    chaptered = "\\chapter" in tex
    ch = sec = sub = subsub = eq = table = figure = algo = 0
    numbers: dict[str, str] = {}
    current = ""

    def pre(n: int) -> str:
        return f"{ch}.{n}" if chaptered else str(n)

    token = re.compile(
        r"\\(chapter|section|subsection|subsubsection)(\*?)\s*(?:\[[^\]]*\])?\s*\{"
        r"|\\begin\{(equation|table|figure|algorithm|" + "|".join(NUMBERED_EQ_ENVS) + r")(\*?)\}"
        r"|\\label\{([^}]*)\}"
    )
    pos = 0
    while (m := token.search(tex, pos)) is not None:
        pos = m.end()
        if m.group(1):  # sectioning
            if m.group(2):
                continue
            kind = m.group(1)
            if kind == "chapter":
                ch += 1
                sec = sub = subsub = eq = table = figure = 0
                current = str(ch)
            elif kind == "section":
                sec += 1
                sub = subsub = 0
                current = pre(sec)
            elif kind == "subsection":
                sub += 1
                subsub = 0
                current = f"{pre(sec)}.{sub}"
            else:
                subsub += 1
                current = f"{pre(sec)}.{sub}.{subsub}"
        elif m.group(3):  # environment
            env, starred = m.group(3), m.group(4)
            if starred:
                continue
            if env == "equation":
                eq += 1
                current = pre(eq)
            elif env == "table":
                table += 1
                current = pre(table)
            elif env == "figure":
                figure += 1
                current = pre(figure)
            elif env == "algorithm":
                algo += 1
                current = str(algo)
            else:  # eqnarray-style: one number per row unless \nonumber
                end = tex.find(f"\\end{{{env}}}", pos)
                body = tex[pos:end if end != -1 else len(tex)]
                for row in re.split(r"\\\\", body):
                    if re.search(r"\\(nonumber|notag)\b", row):
                        continue
                    eq += 1
                    for lab in re.findall(r"\\label\{([^}]*)\}", row):
                        numbers.setdefault(lab.strip(), pre(eq))
                if end != -1:
                    pos = end
        else:
            numbers.setdefault(m.group(5).strip(), current)
    return numbers


def postprocess(md: str, labels: dict[str, str]) -> str:
    # Internal cross-references: keep the visible number, drop the anchor link.
    # Pandoc emits labels it could not number as [[label]](#label); use the
    # number computed from the LaTeX source, or the bare label as a last resort.
    md = re.sub(
        r"\[\\?\[([^\]]*?)\\?\]\]\(#[^)]*\)",
        lambda m: labels.get(m.group(1).replace("\\", ""), m.group(1)),
        md,
    )
    md = re.sub(r"\[([^\]]*)\]\(#[^)]*\)", r"\1", md)
    # Pandoc escapes brackets in our "[n]" / "[Author, year]" markers; unescape them.
    md = re.sub(r"\\\[([^\]\n]{1,120}?)\\\]", r"[\1]", md)
    # Headings left empty by unnumbered front-matter pages.
    md = re.sub(r"^#+\s*$", "", md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


def convert(doc: Doc, data: Path) -> Path:
    folder = data / doc.folder
    tex = build_from_parts(doc, folder) if doc.main is None else read_tex(folder / doc.main)
    tex = preprocess(tex, folder, doc.bbl)
    with tempfile.NamedTemporaryFile("w", suffix=".tex", dir=folder, delete=False, encoding="utf-8") as f:
        f.write(tex)
        tmp = Path(f.name)
    try:
        md = pypandoc.convert_file(
            str(tmp),
            to=MD_FORMAT,
            format="latex",
            extra_args=["--wrap=none", "--standalone"],
            cworkdir=str(folder),
        )
    finally:
        tmp.unlink()
    out = folder / doc.out
    out.write_text(postprocess(md, label_numbers(tex)), encoding="utf-8")
    return out


def main() -> None:
    data = Path(sys.argv[1] if len(sys.argv) > 1 else "data").resolve()
    for doc in DOCS:
        try:
            out = convert(doc, data)
            print(f"ok    {out.relative_to(data)}  ({out.stat().st_size // 1024} KB)")
        except Exception as e:  # keep going; report at the end
            print(f"FAIL  {doc.folder}: {str(e).strip()[:500]}")


if __name__ == "__main__":
    main()
