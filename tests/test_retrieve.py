from rag.retrieve import build_messages, dedupe, rrf

PASSAGE = "Montgomery multiplication computes X Y R^-1 mod M without trial division by M " * 3


def row(id_, text="x"):
    return {"id": id_, "text": text, "reference": f"ref {id_}"}


def test_rrf_rewards_agreement_between_rankings():
    fused = rrf([row("a"), row("b"), row("c")], [row("c"), row("d")])
    assert [r["id"] for r in fused][:2] == ["c", "a"]  # c ranks well in both lists
    assert {r["id"] for r in fused} == {"a", "b", "c", "d"}


def test_dedupe_drops_near_copies_and_respects_limit():
    rows = [row("paper", PASSAGE), row("thesis", "In the thesis: " + PASSAGE), row("other", "Pollard rho on the PS3 " * 5)]
    kept = dedupe(rows, threshold=0.6, limit=5)
    assert [r["id"] for r in kept] == ["paper", "other"]
    assert len(dedupe(rows, threshold=0.6, limit=1)) == 1


def test_messages_number_sources_from_one():
    msgs = build_messages("What is R?", [row("a", "alpha"), row("b", "beta")])
    assert msgs[0]["role"] == "system"
    assert "[1] ref a\nalpha" in msgs[1]["content"] and "[2] ref b\nbeta" in msgs[1]["content"]
    assert msgs[1]["content"].endswith("Question: What is R?")
