"""Evaluate a running deployment over HTTP: retrieval ranks and citations.

    python -m rag.evaluate ask --url https://ask-the-papers.fly.dev --name cloud
    python -m rag.evaluate ask --url http://localhost:8000 --name local --resume
    OPENROUTER_API_KEY=... python -m rag.evaluate judge eval/results/cloud.jsonl
    python -m rag.evaluate report eval/results/cloud.judged.jsonl eval/results/local.judged.jsonl

ask     posts every question in eval/questions.yaml to /api/ask and stores the
        returned sources, answer and timings in eval/results/<name>.jsonl.
judge   asks one fixed LLM whether each cited source supports the sentence that
        cites it, and writes <name>.judged.jsonl plus <name>.review.md, where the
        answers are graded by hand.
report  computes the metrics and writes eval/results/summary.md.

A retrieved source is relevant to an evidence item when its text contains one
of the item's quotes (see normalize), so the gold set is independent of how
each profile chunks the papers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from rag.config import OPENROUTER_URL, REPO_ROOT, ModelConfig
from rag.providers import _check, _headers, openrouter_body

QUESTIONS = REPO_ROOT / "eval" / "questions.yaml"
RESULTS = REPO_ROOT / "eval" / "results"
KS = (1, 3, 6)
JUDGE_MODEL = "anthropic/claude-sonnet-5.5"
GRADES = ("correct", "partial", "incorrect")


def load_questions(path: Path = QUESTIONS) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Relevance and retrieval metrics

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def normalize(text: str) -> str:
    text = text.translate(_QUOTES)
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def relevant(text: str, item: dict[str, Any]) -> bool:
    body = normalize(text)
    return any(normalize(q) in body for q in item["quotes"])


def evidence_ranks(sources: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[int | None]:
    """1-based rank of the first source matching each evidence item, or None."""
    ranks = []
    for item in evidence:
        ranks.append(next((i for i, s in enumerate(sources, 1) if relevant(s["text"], item)), None))
    return ranks


def retrieval_metrics(sources: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, float]:
    ranks = evidence_ranks(sources, evidence)
    found = [r for r in ranks if r is not None]
    metrics = {"rr": 1.0 / min(found) if found else 0.0}
    for k in KS:
        metrics[f"recall@{k}"] = sum(1 for r in ranks if r is not None and r <= k) / len(ranks)
    return metrics


# --------------------------------------------------------------------------
# Citations

_MATH = re.compile(r"\$\$.*?\$\$|\$[^$\n]*\$", re.S)
_CITE = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z$\\(])|\n+")


def cited_sentences(answer: str) -> list[dict[str, Any]]:
    """Split an answer into sentences and the source numbers each one cites.

    Math is blanked out first, so brackets and periods inside $...$ are
    neither citations nor sentence ends; the returned text is the original.
    """
    masked = _MATH.sub(lambda m: " " * len(m.group()), answer)
    out, start = [], 0
    for m in [*_SENTENCE_END.finditer(masked), None]:
        end = m.start() if m else len(masked)
        part = masked[start:end]
        if part.strip():
            numbers = [int(n) for c in _CITE.findall(part) for n in re.split(r"\s*[,;]\s*", c)]
            out.append({"text": answer[start:end].strip(), "cites": list(dict.fromkeys(numbers))})
        if m:
            start = m.end()
    return out


# --------------------------------------------------------------------------
# ask


def ask_one(client: httpx.Client, url: str, question: str) -> dict[str, Any]:
    record: dict[str, Any] = {"sources": [], "answer": "", "truncated": None, "timings": None, "error": None}
    with client.stream("POST", f"{url}/api/ask", json={"question": question}) as r:
        if r.status_code == 429:
            raise RateLimited
        r.raise_for_status()
        pieces = []
        for line in r.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event.get("type")
            if kind == "sources":
                record["sources"] = [
                    {k: s.get(k) for k in ("n", "id", "title", "section", "similarity", "text")} for s in event["sources"]
                ]
            elif kind == "token":
                pieces.append(event["text"])
            elif kind == "done":
                record["truncated"], record["timings"] = event.get("truncated"), event.get("timings")
            elif kind == "error":
                record["error"] = event.get("message")
        record["answer"] = "".join(pieces)
    return record


class RateLimited(Exception):
    pass


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def cmd_ask(args: argparse.Namespace) -> None:
    url = args.url.rstrip("/")
    out = RESULTS / f"{args.name}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if args.resume and out.exists():
        done = {r["id"] for r in read_jsonl(out) if r.get("type") == "answer" and not r.get("error")}
        # keep only good answers, so failed ones are asked again
        kept = [r for r in read_jsonl(out) if r.get("type") != "answer" or r["id"] in done]
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8")
    elif out.exists():
        sys.exit(f"{out} exists; pass --resume to continue it, or delete it")

    timeout = httpx.Timeout(connect=30, read=args.timeout, write=30, pool=30)
    with httpx.Client(timeout=timeout) as client:
        if not out.exists():
            info = client.get(f"{url}/api/info").raise_for_status().json()
            append_jsonl(out, {"type": "info", "name": args.name, "url": url, "date": time.strftime("%Y-%m-%d"), "info": info})
        questions = [q for q in load_questions()[: args.limit or None] if q["id"] not in done]
        for i, q in enumerate(questions):
            if i:
                time.sleep(args.delay)
            for _attempt in range(6):
                try:
                    t0 = time.monotonic()
                    record = ask_one(client, url, q["question"])
                    break
                except RateLimited:
                    print(f"{q['id']}: rate limited, waiting 60 s", flush=True)
                    time.sleep(60)
            else:
                sys.exit("still rate limited after 6 tries")
            append_jsonl(out, {"type": "answer", "id": q["id"], "question": q["question"], **record})
            status = record["error"] or f"{len(record['answer'])} chars, {len(record['sources'])} sources"
            print(f"{q['id']}: {time.monotonic() - t0:5.0f} s  {status}", flush=True)
    print(f"wrote {out}")


# --------------------------------------------------------------------------
# judge

JUDGE_PROMPT = """You check citations in answers produced by a retrieval-augmented system \
over research papers. For each numbered sentence below, and each source it cites, decide \
whether that source supports the sentence:

- "supported": the source states or directly implies everything the sentence attributes to it.
- "partial": the source supports part of the sentence, or the sentence overstates it.
- "unsupported": the source does not support the sentence.

Judge only against the cited source text, not your own knowledge. Formulas count as \
supported when they are mathematically the same as in the source.

Reply with JSON only, no prose and no code fence:
{"verdicts": [{"sentence": <number>, "source": <number>, "verdict": "supported|partial|unsupported", "reason": "<a few words>"}]}"""


def judge_messages(question: str, sentences: list[dict[str, Any]], sources: dict[int, dict[str, Any]]) -> list[dict[str, str]]:
    src = "\n\n".join(f"[{n}] {s['title']}, {s['section']}\n{s['text']}" for n, s in sorted(sources.items()))
    lines = "\n".join(f"{i}. {s['text']}  (cites {', '.join(map(str, s['cites']))})" for i, s in enumerate(sentences, 1))
    user = f"Question: {question}\n\nCited sources:\n\n{src}\n\nSentences to check:\n{lines}"
    return [{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": user}]


def parse_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def call_judge(client: httpx.Client, cfg: ModelConfig, messages: list[dict[str, str]]) -> dict[str, Any]:
    body = {**openrouter_body(cfg, messages), "stream": False}
    r = client.post(f"{cfg.base_url}/chat/completions", headers=_headers(cfg), json=body)
    _check(r)
    return parse_json(r.json()["choices"][0]["message"]["content"])


def judge_record(client: httpx.Client, cfg: ModelConfig, record: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per (sentence, cited source): valid, and the judge's verdict."""
    numbered = {s["n"]: s for s in record["sources"]}
    sentences = [s for s in cited_sentences(record["answer"]) if s["cites"]]
    pairs = [
        {"sentence": i, "text": s["text"], "source": n, "valid": n in numbered}
        for i, s in enumerate(sentences, 1)
        for n in s["cites"]
    ]
    cited = {p["source"]: numbered[p["source"]] for p in pairs if p["valid"]}
    if cited:
        verdicts = call_judge(client, cfg, judge_messages(record["question"], sentences, cited))["verdicts"]
        by_pair = {(v["sentence"], v["source"]): v for v in verdicts}
        for p in pairs:
            v = by_pair.get((p["sentence"], p["source"]))
            if p["valid"] and v:
                p["verdict"], p["reason"] = v["verdict"], v.get("reason", "")
    for p in pairs:
        p.setdefault("verdict", None if p["valid"] else "invalid")
    return pairs


def review_markdown(name: str, records: list[dict[str, Any]], questions: dict[str, dict[str, Any]]) -> str:
    out = [
        f"# Review: {name}\n",
        "For each question, replace `?` after **Grade:** with `correct`, `partial` or `incorrect`.",
        "For questions the papers do not answer, `correct` means the system said so.\n",
    ]
    for r in records:
        q = questions[r["id"]]
        out += [f"## {r['id']}: {r['question']}\n", f"**Expected:** {q['answer'].strip()}\n"]
        out += [f"**Answer:**\n\n{r['answer'].strip() or '(no answer: ' + str(r.get('error')) + ')'}\n"]
        if r.get("truncated"):
            out.append("*(stopped at the length limit)*\n")
        out.append("**Sources:**\n")
        out += [f"- [{s['n']}] {s['title']}, {s['section']}" for s in r["sources"]]
        out.append("\n**Grade:** ?\n")
    return "\n".join(out)


def cmd_judge(args: argparse.Namespace) -> None:
    path = Path(args.file)
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("set OPENROUTER_API_KEY")
    cfg = ModelConfig("openrouter", args.model, OPENROUTER_URL, key, {"temperature": 0, "max_tokens": 4000})
    rows = read_jsonl(path)
    out = path.with_name(path.stem + ".judged.jsonl")
    out.write_text("", encoding="utf-8")
    with httpx.Client(timeout=httpx.Timeout(120, connect=10)) as client:
        for row in rows:
            if row.get("type") == "answer" and not row.get("error"):
                row["citations"] = judge_record(client, cfg, row)
                row["judge"] = args.model
                counts = {
                    v: sum(1 for p in row["citations"] if p["verdict"] == v)
                    for v in ("supported", "partial", "unsupported", "invalid")
                }
                print(f"{row['id']}: {counts}", flush=True)
            append_jsonl(out, row)
    answers = [r for r in rows if r.get("type") == "answer"]
    review = path.with_name(path.stem + ".review.md")
    if review.exists():
        print(f"kept your existing {review}")
    else:
        review.write_text(review_markdown(path.stem, answers, {q["id"]: q for q in load_questions()}), encoding="utf-8")
        print(f"wrote {review}: fill in the grades")
    print(f"wrote {out}")


# --------------------------------------------------------------------------
# report


def read_grades(path: Path) -> dict[str, str]:
    grades, current = {}, None
    if not path.exists():
        return grades
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"## (q\d+):", line):
            current = m.group(1)
        elif (m := re.match(r"\*\*Grade:\*\*\s*(\w+)", line)) and current and m.group(1).lower() in GRADES:
            grades[current] = m.group(1).lower()
    return grades


def summarize(rows: list[dict[str, Any]], questions: dict[str, dict[str, Any]], grades: dict[str, str]) -> dict[str, Any]:
    answers = [r for r in rows if r.get("type") == "answer"]
    answerable = [r for r in answers if questions[r["id"]]["evidence"]]
    unanswerable = [r for r in answers if not questions[r["id"]]["evidence"]]
    per_q = {r["id"]: retrieval_metrics(r["sources"], questions[r["id"]]["evidence"]) for r in answerable}
    pairs = [p for r in answers for p in r.get("citations", [])]
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")  # noqa: E731
    timing = lambda k: statistics.median([r["timings"][k] for r in answers if r.get("timings") and r["timings"].get(k)] or [0])  # noqa: E731
    info = next((r for r in rows if r.get("type") == "info"), {})
    return {
        "info": info,
        "n": len(answerable),
        "mrr": mean([m["rr"] for m in per_q.values()]),
        "recall": {k: mean([m[f"recall@{k}"] for m in per_q.values()]) for k in KS},
        "per_question": per_q,
        "pairs": len(pairs),
        "valid": sum(p["verdict"] != "invalid" for p in pairs),
        "verdicts": {v: sum(p["verdict"] == v for p in pairs) for v in ("supported", "partial", "unsupported")},
        "judged": any("citations" in r for r in answers),
        "judge": next((r["judge"] for r in answers if r.get("judge")), None),
        "grades": {g: sum(grades.get(r["id"]) == g for r in answerable) for g in GRADES},
        "graded": sum(r["id"] in grades for r in answerable),
        "refused": sum(grades.get(r["id"]) == "correct" for r in unanswerable),
        "refusal_graded": sum(r["id"] in grades for r in unanswerable),
        "unanswerable": len(unanswerable),
        "first_token_s": timing("first_token_ms") / 1000,
        "total_s": timing("total_ms") / 1000,
        "truncated": sum(bool(r.get("truncated")) for r in answers),
        "errors": sum(bool(r.get("error")) for r in answers),
    }


def frac(a: int, b: int) -> str:
    return f"{a}/{b} ({a / b:.0%})" if b else "–"


def report_markdown(names: list[str], stats: list[dict[str, Any]]) -> str:
    rows = [
        ("Questions with evidence", [str(s["n"]) for s in stats]),
        ("MRR@6", [f"{s['mrr']:.2f}" for s in stats]),
        *[(f"Recall@{k}", [f"{s['recall'][k]:.2f}" for s in stats]) for k in KS],
        ("Citations pointing to a returned source", [frac(s["valid"], s["pairs"]) for s in stats]),
        ("Citations supported (judge)", [frac(s["verdicts"]["supported"], s["valid"]) if s["judged"] else "–" for s in stats]),
        ("Citations partly supported", [frac(s["verdicts"]["partial"], s["valid"]) if s["judged"] else "–" for s in stats]),
        ("Citations unsupported", [frac(s["verdicts"]["unsupported"], s["valid"]) if s["judged"] else "–" for s in stats]),
        ("Answers correct (graded by hand)", [frac(s["grades"]["correct"], s["graded"]) for s in stats]),
        ("Answers partly correct", [frac(s["grades"]["partial"], s["graded"]) for s in stats]),
        ("Says when the papers don't answer", [frac(s["refused"], s["refusal_graded"]) for s in stats]),
        ("Median time to first word", [f"{s['first_token_s']:.1f} s" for s in stats]),
        ("Median time per answer", [f"{s['total_s']:.1f} s" for s in stats]),
        ("Answers cut at the length limit", [str(s["truncated"]) for s in stats]),
    ]
    head = "| | " + " | ".join(names) + " |\n|---|" + "---|" * len(names)
    lines = ["## Results\n", head, *[f"| {label} | " + " | ".join(vals) + " |" for label, vals in rows]]
    lines += ["\n## Setup\n"]
    for name, s in zip(names, stats, strict=True):
        i = s["info"].get("info", {})
        judge = s["judge"]
        lines.append(
            f"- **{name}** ({s['info'].get('date', '?')}): LLM `{i.get('llm')}`, embeddings `{i.get('embedding')}`, "
            f"top {i.get('top_k')} sources, {i.get('index', {}).get('chunks', '?')} chunks"
            + (f"; citations judged by `{judge}`" if judge else "")
        )
    ids = sorted(stats[0]["per_question"])
    lines += ["\n## Rank of the first relevant source\n", "| Question | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for qid in ids:
        cells = []
        for s in stats:
            m = s["per_question"].get(qid)
            cells.append("–" if m is None else (f"{round(1 / m['rr'])}" if m["rr"] else "not in top 6"))
        lines.append(f"| {qid} | " + " | ".join(cells) + " |")
    lines.append(
        f"\nWith {stats[0]['n']} questions, a difference of one or two questions is within noise. Retrieval metrics compare "
        "the embedding model and chunking; the answer metrics compare the whole systems."
    )
    return "\n".join(lines) + "\n"


def cmd_report(args: argparse.Namespace) -> None:
    questions = {q["id"]: q for q in load_questions()}
    names, stats = [], []
    for f in args.files:
        path = Path(f)
        name = path.name.split(".")[0]
        grades = read_grades(path.with_name(name + ".review.md"))
        names.append(name)
        stats.append(summarize(read_jsonl(path), questions, grades))
    text = report_markdown(names, stats)
    (RESULTS / "summary.md").write_text(text, encoding="utf-8")
    print(text)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("ask", help="ask every question of a running deployment")
    a.add_argument("--url", required=True)
    a.add_argument("--name", required=True, help="results go to eval/results/<name>.jsonl")
    a.add_argument("--delay", type=float, default=11, help="seconds between questions (cloud limit: 6 per minute)")
    a.add_argument("--timeout", type=float, default=600, help="seconds to wait for one answer")
    a.add_argument("--limit", type=int, default=0, help="ask only the first N questions")
    a.add_argument("--resume", action="store_true", help="continue an interrupted run")
    j = sub.add_parser("judge", help="judge the citations of a results file")
    j.add_argument("file")
    j.add_argument("--model", default=JUDGE_MODEL)
    r = sub.add_parser("report", help="compare judged results files")
    r.add_argument("files", nargs="+")
    args = p.parse_args()
    {"ask": cmd_ask, "judge": cmd_judge, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    main()
