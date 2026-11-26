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
    """True when the text contains any of the evidence item's quotes."""
    body = normalize(text)
    for quote in item["quotes"]:
        if normalize(quote) in body:
            return True
    return False


def evidence_ranks(sources: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[int | None]:
    """1-based rank of the first source matching each evidence item, or None."""
    ranks = []
    for item in evidence:
        first_rank = None
        for rank, source in enumerate(sources, start=1):
            if relevant(source["text"], item):
                first_rank = rank
                break
        ranks.append(first_rank)
    return ranks


def retrieval_metrics(sources: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, float]:
    ranks = evidence_ranks(sources, evidence)
    found = []
    for rank in ranks:
        if rank is not None:
            found.append(rank)

    # Reciprocal rank of the best-placed evidence item (0 if none was found).
    if found:
        reciprocal_rank = 1.0 / min(found)
    else:
        reciprocal_rank = 0.0
    metrics = {"rr": reciprocal_rank}

    # Recall@k: the share of evidence items found within the top k sources.
    for k in KS:
        within_k = 0
        for rank in ranks:
            if rank is not None and rank <= k:
                within_k += 1
        metrics[f"recall@{k}"] = within_k / len(ranks)
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

    def blank(match):
        return " " * len(match.group())

    # Same length as the answer, so positions in one are positions in the other.
    masked = _MATH.sub(blank, answer)

    # Every sentence end as (where the sentence stops, where the next starts);
    # the end of the answer closes the last sentence.
    boundaries = []
    for match in _SENTENCE_END.finditer(masked):
        boundaries.append((match.start(), match.end()))
    boundaries.append((len(masked), len(masked)))

    out = []
    start = 0
    for end, next_start in boundaries:
        part = masked[start:end]
        if part.strip():
            # "[2]", "[1, 3]" or "[1; 3]": every number counts, each once, in order.
            cites = []
            for inside_brackets in _CITE.findall(part):
                for number in re.split(r"\s*[,;]\s*", inside_brackets):
                    n = int(number)
                    if n not in cites:
                        cites.append(n)
            out.append({"text": answer[start:end].strip(), "cites": cites})
        start = next_start
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
                sources = []
                for source in event["sources"]:
                    kept = {}
                    for field in ("n", "id", "title", "section", "similarity", "text"):
                        kept[field] = source.get(field)
                    sources.append(kept)
                record["sources"] = sources
            elif kind == "token":
                pieces.append(event["text"])
            elif kind == "done":
                record["truncated"] = event.get("truncated")
                record["timings"] = event.get("timings")
            elif kind == "error":
                record["error"] = event.get("message")
        record["answer"] = "".join(pieces)
    return record


class RateLimited(Exception):
    pass


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def cmd_ask(args: argparse.Namespace) -> None:
    url = args.url.rstrip("/")
    out = RESULTS / f"{args.name}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()  # ids of questions already answered without error
    if args.resume and out.exists():
        previous = read_jsonl(out)
        for row in previous:
            if row.get("type") == "answer" and not row.get("error"):
                done.add(row["id"])
        # keep only good answers, so failed ones are asked again
        kept_lines = []
        for row in previous:
            if row.get("type") != "answer" or row["id"] in done:
                kept_lines.append(json.dumps(row, ensure_ascii=False) + "\n")
        out.write_text("".join(kept_lines), encoding="utf-8")
    elif out.exists():
        sys.exit(f"{out} exists; pass --resume to continue it, or delete it")

    timeout = httpx.Timeout(connect=30, read=args.timeout, write=30, pool=30)
    with httpx.Client(timeout=timeout) as client:
        if not out.exists():
            info = client.get(f"{url}/api/info").raise_for_status().json()
            header = {"type": "info", "name": args.name, "url": url, "date": time.strftime("%Y-%m-%d"), "info": info}
            append_jsonl(out, header)

        all_questions = load_questions()
        if args.limit:
            all_questions = all_questions[: args.limit]
        questions = []
        for q in all_questions:
            if q["id"] not in done:
                questions.append(q)

        for i, q in enumerate(questions):
            if i > 0:
                time.sleep(args.delay)

            # Up to 6 tries; after a 429, wait a minute for the limit to reset.
            record = None
            for _attempt in range(6):
                try:
                    t0 = time.monotonic()
                    record = ask_one(client, url, q["question"])
                    break
                except RateLimited:
                    print(f"{q['id']}: rate limited, waiting 60 s", flush=True)
                    time.sleep(60)
            if record is None:
                sys.exit("still rate limited after 6 tries")

            row = {"type": "answer", "id": q["id"], "question": q["question"]}
            row.update(record)
            append_jsonl(out, row)
            if record["error"]:
                status = record["error"]
            else:
                status = f"{len(record['answer'])} chars, {len(record['sources'])} sources"
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
    source_blocks = []
    for n in sorted(sources):
        s = sources[n]
        source_blocks.append(f"[{n}] {s['title']}, {s['section']}\n{s['text']}")
    src = "\n\n".join(source_blocks)

    sentence_lines = []
    for i, s in enumerate(sentences, start=1):
        cited_numbers = []
        for n in s["cites"]:
            cited_numbers.append(str(n))
        sentence_lines.append(f"{i}. {s['text']}  (cites {', '.join(cited_numbers)})")
    lines = "\n".join(sentence_lines)

    user = f"Question: {question}\n\nCited sources:\n\n{src}\n\nSentences to check:\n{lines}"
    return [{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": user}]


def parse_json(text: str) -> dict[str, Any]:
    # Remove a ```json ... ``` fence if the model added one anyway.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def call_judge(client: httpx.Client, cfg: ModelConfig, messages: list[dict[str, str]]) -> dict[str, Any]:
    body = openrouter_body(cfg, messages)
    body["stream"] = False  # one JSON reply, not a stream
    r = client.post(f"{cfg.base_url}/chat/completions", headers=_headers(cfg), json=body)
    _check(r)
    reply = r.json()["choices"][0]["message"]["content"]
    return parse_json(reply)


def judge_record(client: httpx.Client, cfg: ModelConfig, record: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per (sentence, cited source): valid, and the judge's verdict."""
    numbered = {}
    for source in record["sources"]:
        numbered[source["n"]] = source

    sentences = []
    for sentence in cited_sentences(record["answer"]):
        if sentence["cites"]:
            sentences.append(sentence)

    # A citation is valid when its number is one of the returned sources.
    pairs = []
    for i, sentence in enumerate(sentences, start=1):
        for n in sentence["cites"]:
            pairs.append({"sentence": i, "text": sentence["text"], "source": n, "valid": n in numbered})

    cited = {}
    for pair in pairs:
        if pair["valid"]:
            cited[pair["source"]] = numbered[pair["source"]]

    if cited:
        reply = call_judge(client, cfg, judge_messages(record["question"], sentences, cited))
        by_pair = {}
        for verdict in reply["verdicts"]:
            by_pair[(verdict["sentence"], verdict["source"])] = verdict
        for pair in pairs:
            verdict = by_pair.get((pair["sentence"], pair["source"]))
            if pair["valid"] and verdict:
                pair["verdict"] = verdict["verdict"]
                pair["reason"] = verdict.get("reason", "")

    # Pairs without a verdict: None if the judge skipped them, "invalid" if
    # the cited number was not a returned source.
    for pair in pairs:
        if "verdict" not in pair:
            if pair["valid"]:
                pair["verdict"] = None
            else:
                pair["verdict"] = "invalid"
    return pairs


def review_markdown(name: str, records: list[dict[str, Any]], questions: dict[str, dict[str, Any]]) -> str:
    out = [
        f"# Review: {name}\n",
        "For each question, replace `?` after **Grade:** with `correct`, `partial` or `incorrect`.",
        "For questions the papers do not answer, `correct` means the system said so.\n",
    ]
    for r in records:
        q = questions[r["id"]]
        out.append(f"## {r['id']}: {r['question']}\n")
        out.append(f"**Expected:** {q['answer'].strip()}\n")
        answer = r["answer"].strip()
        if not answer:
            answer = "(no answer: " + str(r.get("error")) + ")"
        out.append(f"**Answer:**\n\n{answer}\n")
        if r.get("truncated"):
            out.append("*(stopped at the length limit)*\n")
        out.append("**Sources:**\n")
        for s in r["sources"]:
            out.append(f"- [{s['n']}] {s['title']}, {s['section']}")
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
                counts = {"supported": 0, "partial": 0, "unsupported": 0, "invalid": 0}
                for pair in row["citations"]:
                    if pair["verdict"] in counts:
                        counts[pair["verdict"]] += 1
                print(f"{row['id']}: {counts}", flush=True)
            append_jsonl(out, row)

    answers = []
    for row in rows:
        if row.get("type") == "answer":
            answers.append(row)
    review = path.with_name(path.stem + ".review.md")
    if review.exists():
        print(f"kept your existing {review}")
    else:
        questions = {}
        for q in load_questions():
            questions[q["id"]] = q
        review.write_text(review_markdown(path.stem, answers, questions), encoding="utf-8")
        print(f"wrote {review}: fill in the grades")
    print(f"wrote {out}")


# --------------------------------------------------------------------------
# report


def read_grades(path: Path) -> dict[str, str]:
    """The hand grades in a review file, by question id."""
    grades = {}
    current = None  # the question whose section we are in
    if not path.exists():
        return grades
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"## (q\d+):", line)
        if heading:
            current = heading.group(1)
            continue
        grade_line = re.match(r"\*\*Grade:\*\*\s*(\w+)", line)
        if grade_line and current:
            grade = grade_line.group(1).lower()
            if grade in GRADES:
                grades[current] = grade
    return grades


def summarize(rows: list[dict[str, Any]], questions: dict[str, dict[str, Any]], grades: dict[str, str]) -> dict[str, Any]:
    info = {}
    for row in rows:
        if row.get("type") == "info":
            info = row
            break

    # Questions with evidence are answerable; the others test refusals.
    answers = []
    answerable = []
    unanswerable = []
    for row in rows:
        if row.get("type") != "answer":
            continue
        answers.append(row)
        if questions[row["id"]]["evidence"]:
            answerable.append(row)
        else:
            unanswerable.append(row)

    per_q = {}
    for r in answerable:
        per_q[r["id"]] = retrieval_metrics(r["sources"], questions[r["id"]]["evidence"])

    def mean(values):
        if not values:
            return float("nan")
        return sum(values) / len(values)

    reciprocal_ranks = []
    for metrics in per_q.values():
        reciprocal_ranks.append(metrics["rr"])
    recall = {}
    for k in KS:
        values = []
        for metrics in per_q.values():
            values.append(metrics[f"recall@{k}"])
        recall[k] = mean(values)

    def timing(key):
        """Median of one timing over the answers that have it (0 if none)."""
        values = []
        for r in answers:
            timings = r.get("timings")
            if timings and timings.get(key):
                values.append(timings[key])
        if not values:
            values = [0]
        return statistics.median(values)

    # Citation pairs from the judge, and what it said about them.
    pairs = []
    for r in answers:
        pairs.extend(r.get("citations", []))
    valid = 0
    verdicts = {"supported": 0, "partial": 0, "unsupported": 0}
    for pair in pairs:
        if pair["verdict"] != "invalid":
            valid += 1
        if pair["verdict"] in verdicts:
            verdicts[pair["verdict"]] += 1

    judged = False
    judge = None
    for r in answers:
        if "citations" in r:
            judged = True
    for r in answers:
        if r.get("judge"):
            judge = r["judge"]
            break

    # Hand grades.
    grade_counts = {}
    for g in GRADES:
        grade_counts[g] = 0
    graded = 0
    for r in answerable:
        grade = grades.get(r["id"])
        if grade in grade_counts:
            grade_counts[grade] += 1
        if r["id"] in grades:
            graded += 1
    refused = 0
    refusal_graded = 0
    for r in unanswerable:
        if grades.get(r["id"]) == "correct":
            refused += 1
        if r["id"] in grades:
            refusal_graded += 1

    truncated = 0
    errors = 0
    for r in answers:
        if r.get("truncated"):
            truncated += 1
        if r.get("error"):
            errors += 1

    return {
        "info": info,
        "n": len(answerable),
        "mrr": mean(reciprocal_ranks),
        "recall": recall,
        "per_question": per_q,
        "pairs": len(pairs),
        "valid": valid,
        "verdicts": verdicts,
        "judged": judged,
        "judge": judge,
        "grades": grade_counts,
        "graded": graded,
        "refused": refused,
        "refusal_graded": refusal_graded,
        "unanswerable": len(unanswerable),
        "first_token_s": timing("first_token_ms") / 1000,
        "total_s": timing("total_ms") / 1000,
        "truncated": truncated,
        "errors": errors,
    }


def frac(a: int, b: int) -> str:
    """ "3/4 (75%)", or "–" when there is nothing to count."""
    if not b:
        return "–"
    return f"{a}/{b} ({a / b:.0%})"


def report_markdown(names: list[str], stats: list[dict[str, Any]]) -> str:
    def judged_share(s, verdict):
        if not s["judged"]:
            return "–"
        return frac(s["verdicts"][verdict], s["valid"])

    # One table row per metric: (label, one value per results file).
    rows = []
    rows.append(("Questions with evidence", [str(s["n"]) for s in stats]))
    rows.append(("MRR@6", [f"{s['mrr']:.2f}" for s in stats]))
    for k in KS:
        rows.append((f"Recall@{k}", [f"{s['recall'][k]:.2f}" for s in stats]))
    rows.append(("Citations pointing to a returned source", [frac(s["valid"], s["pairs"]) for s in stats]))
    rows.append(("Citations supported (judge)", [judged_share(s, "supported") for s in stats]))
    rows.append(("Citations partly supported", [judged_share(s, "partial") for s in stats]))
    rows.append(("Citations unsupported", [judged_share(s, "unsupported") for s in stats]))
    rows.append(("Answers correct (graded by hand)", [frac(s["grades"]["correct"], s["graded"]) for s in stats]))
    rows.append(("Answers partly correct", [frac(s["grades"]["partial"], s["graded"]) for s in stats]))
    rows.append(("Says when the papers don't answer", [frac(s["refused"], s["refusal_graded"]) for s in stats]))
    rows.append(("Median time to first word", [f"{s['first_token_s']:.1f} s" for s in stats]))
    rows.append(("Median time per answer", [f"{s['total_s']:.1f} s" for s in stats]))
    rows.append(("Answers cut at the length limit", [str(s["truncated"]) for s in stats]))

    head = "| | " + " | ".join(names) + " |\n|---|" + "---|" * len(names)
    lines = ["## Results\n", head]
    for label, values in rows:
        lines.append(f"| {label} | " + " | ".join(values) + " |")

    lines.append("\n## Setup\n")
    for name, s in zip(names, stats, strict=True):
        i = s["info"].get("info", {})
        judge = s["judge"]
        line = (
            f"- **{name}** ({s['info'].get('date', '?')}): LLM `{i.get('llm')}`, embeddings `{i.get('embedding')}`, "
            f"top {i.get('top_k')} sources, {i.get('index', {}).get('chunks', '?')} chunks"
        )
        if judge:
            line += f"; citations judged by `{judge}`"
        lines.append(line)

    ids = sorted(stats[0]["per_question"])
    lines.append("\n## Rank of the first relevant source\n")
    lines.append("| Question | " + " | ".join(names) + " |")
    lines.append("|---|" + "---|" * len(names))
    for qid in ids:
        cells = []
        for s in stats:
            m = s["per_question"].get(qid)
            if m is None:
                cells.append("–")
            elif m["rr"]:
                rank = round(1 / m["rr"])
                cells.append(f"{rank}")
            else:
                cells.append("not in top 6")
        lines.append(f"| {qid} | " + " | ".join(cells) + " |")
    lines.append(
        f"\nWith {stats[0]['n']} questions, a difference of one or two questions is within noise. Retrieval metrics compare "
        "the embedding model and chunking; the answer metrics compare the whole systems."
    )
    return "\n".join(lines) + "\n"


def cmd_report(args: argparse.Namespace) -> None:
    questions = {}
    for q in load_questions():
        questions[q["id"]] = q
    names = []
    stats = []
    for f in args.files:
        path = Path(f)
        name = path.name.split(".")[0]  # "cloud.judged.jsonl" -> "cloud"
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
    if args.command == "ask":
        cmd_ask(args)
    elif args.command == "judge":
        cmd_judge(args)
    elif args.command == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
