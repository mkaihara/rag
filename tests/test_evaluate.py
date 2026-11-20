import json
from pathlib import Path

import httpx
import pytest

from rag import evaluate
from rag.chunker import chunk_corpus
from rag.config import REPO_ROOT, load_settings
from rag.evaluate import cited_sentences, evidence_ranks, normalize, read_grades, relevant, retrieval_metrics

QUESTIONS = evaluate.load_questions()
QUOTES = [(q["id"], quote) for q in QUESTIONS for item in q["evidence"] for quote in item["quotes"]]


# --------------------------------------------------------------------------
# The gold set


def test_question_ids_are_unique_and_complete():
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids)) == 15
    assert all(q["question"].strip() and q["answer"].strip() for q in QUESTIONS)


@pytest.fixture(scope="module")
def corpus() -> str:
    return normalize("\n".join(p.read_text(encoding="utf-8") for p in sorted((REPO_ROOT / "data").glob("*/*.md"))))


@pytest.mark.parametrize("qid, quote", QUOTES)
def test_every_quote_is_in_the_papers(corpus, qid, quote):
    assert normalize(quote) in corpus, f"{qid}: quote not found in data/"


@pytest.fixture(scope="module", params=["local", "cloud"])
def chunks(request) -> list[str]:
    return [c["text"] for c in chunk_corpus(REPO_ROOT / "data", load_settings(request.param).chunking, log=lambda *_: None)]


def test_every_evidence_item_fits_inside_one_chunk(chunks):
    """Otherwise a quote split across two chunks could never match a source."""
    missing = [
        (q["id"], item["quotes"][0][:40])
        for q in QUESTIONS
        for item in q["evidence"]
        if not any(relevant(text, item) for text in chunks)
    ]
    assert not missing


# --------------------------------------------------------------------------
# Matching and retrieval metrics


def test_normalize_ignores_case_markup_spacing_and_curly_quotes():
    assert normalize("LACAL’s  *birthday\nparadox*") == normalize("lacal's birthday paradox")


def src(*texts):
    return [{"n": i, "text": t} for i, t in enumerate(texts, 1)]


def test_ranks_and_metrics():
    evidence = [{"quotes": ["alpha beta"]}, {"quotes": ["gamma", "delta epsilon"]}]
    sources = src("nothing here", "the Alpha  beta case", "x", "y", "DELTA epsilon!", "z")
    assert evidence_ranks(sources, evidence) == [2, 5]
    m = retrieval_metrics(sources, evidence)
    assert m["rr"] == 0.5
    assert (m["recall@1"], m["recall@3"], m["recall@6"]) == (0.0, 0.5, 1.0)


def test_metrics_when_nothing_relevant_is_retrieved():
    m = retrieval_metrics(src("a", "b"), [{"quotes": ["zzz"]}])
    assert m == {"rr": 0.0, "recall@1": 0.0, "recall@3": 0.0, "recall@6": 0.0}


# --------------------------------------------------------------------------
# Citations


def test_cited_sentences_split_and_numbers():
    answer = "NFS is fastest [1]. It dates from 1989 [2][4].\n- Also used for SNFS [1, 3]."
    assert cited_sentences(answer) == [
        {"text": "NFS is fastest [1].", "cites": [1]},
        {"text": "It dates from 1989 [2][4].", "cites": [2, 4]},
        {"text": "- Also used for SNFS [1, 3].", "cites": [1, 3]},
    ]


def test_brackets_and_periods_inside_math_are_ignored():
    answer = "The interval is $[0, M-1]$ and $x = 1.5 [2]$ holds. So $$Z \\in [1]$$ is fine [3]."
    out = cited_sentences(answer)
    assert [s["cites"] for s in out] == [[], [3]]
    assert out[0]["text"].startswith("The interval is $[0, M-1]$")


def test_sentences_without_citations_are_kept_with_none():
    assert cited_sentences("The sources do not say.") == [{"text": "The sources do not say.", "cites": []}]


# --------------------------------------------------------------------------
# ask, judge, report


def test_ask_one_reads_the_event_stream():
    events = [
        {"type": "sources", "sources": [{"n": 1, "id": "d#1", "title": "T", "section": "S", "text": "body", "pages": [1, 2]}]},
        {"type": "token", "text": "Hello "},
        {"type": "token", "text": "[1]."},
        {"type": "done", "truncated": False, "timings": {"total_ms": 900}},
    ]
    body = "".join(json.dumps(e) + "\n" for e in events)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body)))
    record = evaluate.ask_one(client, "http://app", "Q?")
    assert record["answer"] == "Hello [1]."
    assert record["sources"] == [{"n": 1, "id": "d#1", "title": "T", "section": "S", "similarity": None, "text": "body"}]
    assert record["timings"] == {"total_ms": 900} and record["truncated"] is False and record["error"] is None


def test_ask_one_signals_rate_limit():
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429, text="slow down")))
    with pytest.raises(evaluate.RateLimited):
        evaluate.ask_one(client, "http://app", "Q?")


def test_judge_record_marks_invalid_citations_and_maps_verdicts(monkeypatch):
    sent = {}

    def fake_call(client, cfg, messages):
        sent["user"] = messages[1]["content"]
        return {"verdicts": [{"sentence": 1, "source": 1, "verdict": "supported", "reason": "states it"}]}

    monkeypatch.setattr(evaluate, "call_judge", fake_call)
    record = {
        "question": "Q?",
        "answer": "NFS is fastest [1][7].",
        "sources": [{"n": 1, "title": "T", "section": "S", "text": "NFS"}],
    }
    pairs = evaluate.judge_record(None, None, record)
    assert [(p["source"], p["verdict"]) for p in pairs] == [(1, "supported"), (7, "invalid")]
    assert "[1] T, S\nNFS" in sent["user"] and "[7]" not in sent["user"].split("Sentences")[0]


def test_parse_json_accepts_code_fences():
    assert evaluate.parse_json('```json\n{"verdicts": []}\n```') == {"verdicts": []}


def test_grades_are_read_from_the_review_file(tmp_path: Path):
    review = tmp_path / "x.review.md"
    review.write_text("## q01: A?\n\n**Grade:** correct\n\n## q02: B?\n\n**Grade:** ?\n\n## q05: C?\n**Grade:** Partial\n")
    assert read_grades(review) == {"q01": "correct", "q05": "partial"}


def test_report_end_to_end(tmp_path, monkeypatch):
    qs = [
        {"id": "q01", "question": "A?", "answer": "a", "evidence": [{"quotes": ["needle"]}]},
        {"id": "q02", "question": "B?", "answer": "b", "evidence": []},
    ]
    rows = [
        {
            "type": "info",
            "name": "x",
            "date": "2026-11-20",
            "info": {"llm": "m", "embedding": "e", "top_k": 6, "index": {"chunks": 9}},
        },
        {
            "type": "answer",
            "id": "q01",
            "question": "A?",
            "answer": "Yes [2].",
            "sources": src("hay", "the needle"),
            "timings": {"first_token_ms": 1000, "total_ms": 3000},
            "citations": [{"sentence": 1, "source": 2, "valid": True, "verdict": "supported"}],
            "judge": "j",
        },
        {"type": "answer", "id": "q02", "question": "B?", "answer": "Not in the sources.", "sources": src("hay"), "timings": None},
    ]
    s = evaluate.summarize(rows, {q["id"]: q for q in qs}, {"q01": "correct", "q02": "correct"})
    assert s["n"] == 1 and s["mrr"] == 0.5 and s["recall"][3] == 1.0
    assert s["verdicts"]["supported"] == 1 and s["refused"] == 1 and s["grades"]["correct"] == 1
    text = evaluate.report_markdown(["x"], [s])
    assert "| MRR@6 | 0.50 |" in text and "| q01 | 2 |" in text and "judged by `j`" in text
