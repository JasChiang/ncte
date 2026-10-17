"""fix_check judgement set (audit 2026-10-17 §5.1): the set is sound, requests look like a run's, scoring counts a
still-wrong fix that is closed (offline)."""

import json

import pytest

from ncte.eval.fix_check import OPEN_GOLD, load_fix_set, payloads, run_comparison, score
from ncte.llm.fake import FakeLLM
from ncte.llm.schemas import FixCheck


@pytest.mark.parametrize("name", ["fix_check_v1", "fix_check_v2"])
def test_the_bundled_sets_have_every_kind_and_paired_sources(name):
    fs = load_fix_set(name)
    gold = {c.gold for c in fs.cases}
    assert gold >= {"fixed", "still_wrong", "issue_invalid", "regressed"}
    assert len(fs.cases) == 40
    by_source: dict[str, set[str]] = {}
    for c in fs.cases:
        by_source.setdefault(c.source, set()).add(c.gold)
    assert sum(len(g) > 1 for g in by_source.values()) >= 2  # the same line, judged two ways
    for c in fs.cases:
        assert c.issues and c.before and c.after
        if c.gold == "issue_invalid":
            assert c.note or c.after == c.before


def test_requests_look_like_a_runs_and_name_only_the_people_in_them():
    fs = load_fix_set()
    ps = payloads(fs)
    assert [len(p["items"]) for p in ps] == [20, 20]
    assert {x["unit_id"] for p in ps for x in p["items"]} == {c.id for c in fs.cases}
    for p in ps:
        assert p["task"] == "fix_check"
        assert set(p["items"][0]) <= {"unit_id", "source", "before", "after", "issues", "around", "lines"}
        text = json.dumps(p["items"], ensure_ascii=False)
        assert all(ch["ja"] in text or ch["name"] in text for ch in p.get("characters", []))
    assert payloads(fs, 1) != ps  # another pass, another order


def test_closing_a_still_wrong_fix_is_the_counted_error():
    fs = load_fix_set()

    def check(cid: str, verdict: str, new_error: str = "") -> FixCheck:
        return FixCheck(unit_id=cid, verdict=verdict, new_error=new_error, reason="r")  # type: ignore[arg-type]

    perfect = {c.id: check(c.id, "fixed" if c.gold == "regressed" else c.gold, "x" if c.gold == "regressed" else "")
               for c in fs.cases}
    s = score(fs, perfect)
    assert s["false_settle_rate"] == 0 and s["exact"] == 1 and s["regressed_caught"] == 1
    assert s["undecided_caught"] == 1 and s["kept_open"] == []

    # a new error reported where "undecided" is the answer still keeps it open, and is accepted where listed
    loose = {**perfect, "child-gender": check("child-gender", "issue_invalid", "加了性別")}
    s = score(fs, loose)
    assert s["false_settle_rate"] == 0 and s["acceptable"] == 1 and s["exact"] < 1

    trusting = {**perfect, "you-inferred": check("you-inferred", "fixed")}  # a guessed gender taken as known
    s = score(fs, trusting)
    assert s["false_settled"] == ["you-inferred"] and s["false_settled_by_category"] == {"undecidable": 1}

    gullible = {c.id: check(c.id, "fixed") for c in fs.cases}
    s = score(fs, gullible)
    assert s["false_settle_rate"] == 1
    assert len(s["false_settled"]) == sum(c.gold in OPEN_GOLD for c in fs.cases)

    s = score(fs, {})  # nothing answered: open, not settled
    assert s["false_settle_rate"] == 0 and s["unchecked"] == 40 and s["settle_recall"] == 0


def test_the_comparison_runs_each_model_within_budget(cfg, tmp_path):
    fs = load_fix_set()
    gold = {c.id: c.gold for c in fs.cases}
    seen: list[dict] = []

    def judge(payload, req):
        seen.append(payload)
        return {"checks": [{"unit_id": i["unit_id"], "verdict": "fixed", "new_error": "", "reason": "r"}
                           for i in payload["items"]]}

    client = FakeLLM(models=["a/one", "b/two"], responders={"fix_check": judge})
    rep = run_comparison(fs, cfg, client, ["a/one", "b/two"], budget_usd=5, out_dir=tmp_path / "out", log=lambda m: None)
    assert [r["model"] for r in rep["runs"]] == ["a/one", "b/two"]
    assert all(r["reasoning"] == "high" for r in rep["runs"])  # as a run's fix check
    assert all(r["cost_usd"] > 0 for r in rep["runs"])
    assert rep["runs"][0]["false_settle_rate"] == 1
    assert len(seen) == 4 and {i["unit_id"] for p in seen for i in p["items"]} == set(gold)
    assert (tmp_path / "out" / "fix_check_results.json").exists()

    rep = run_comparison(fs, cfg, client, ["a/one"], budget_usd=0, out_dir=tmp_path / "none", log=lambda m: None)
    assert rep["runs"] == []  # nothing the budget cannot cover is sent


def test_a_fix_goes_back_as_a_run_sends_it_and_is_judged(cfg, tmp_path):
    """§5.2: each wrong translation goes back with its issues and current text; the judge sees the new text."""
    from ncte.eval.retranslate import distinct_cases, payload, run_retranslate_eval

    fs = load_fix_set("fix_check_v2")
    todo = distinct_cases(fs)
    assert len({(c.source, c.before) for c, _ in todo}) == len(todo)
    supported = {c.id for c, s in todo if s}
    assert "neg-still" not in supported and "give-still" in supported  # a v1 id is not here; v2's is
    assert not any(s for c, s in todo if c.category in ("bad_suggestion", "undecidable") or c.gold == "issue_invalid")

    p = payload(fs, next(c for c, _ in todo if c.id == "who-did-still"))
    fb = p["review_feedback"][0]
    assert fb["current_translation"] == "她說「你掉了東西喔」，然後交給了他。"
    assert fb["issues"][0]["issue_type"] == "mistranslation" and fb["issues"][0]["suggestion"].startswith("「妳")
    assert p["context_before"][0]["source"].startswith("　健太は")
    assert {c["name_ja"] for c in p["characters"]} == {"健太", "美咲"}
    assert p["scene"]["utterances"][0]["to"] == ["美咲"]

    sent: list[dict] = []

    def fixer(pl, req):
        sent.append(pl)
        return {"translations": [{"unit_id": u["unit_id"], "translated_text": pl["review_feedback"][0]["issues"][0]
                                  ["suggestion"] or "x", "flags": [], "notes": []} for u in pl["units"]]}

    judged: list[dict] = []

    def judge(pl, req):
        judged.append(pl)
        return {"checks": [{"unit_id": i["unit_id"], "verdict": "fixed", "new_error": "", "reason": "r"}
                           for i in pl["items"]]}

    client = FakeLLM(models=["t/one", "j/two"], responders={"translate": fixer, "fix_check": judge})
    r = run_retranslate_eval(fs, cfg, client, model="t/one", judge="j/two", budget_usd=5, out_dir=tmp_path,
                             log=lambda m: None)
    assert len(sent) == len(todo) and all("review_feedback" in pl for pl in sent)
    after = {i["unit_id"]: i["after"] for pl in judged for i in pl["items"]}
    assert after["give-still"] == "「要不要我幫你做便當？」美咲對健太說。"  # the judge sees the new text
    assert r["supported"]["n"] == len(supported) and r["supported"]["fixed_rate"] == 1
    assert r["unsupported"]["kept_original"] == 0
    assert (tmp_path / "retranslate_review.md").read_text(encoding="utf-8").count("\n") == len(todo) + 2

    r = run_retranslate_eval(fs, cfg, client, model="t/one", judge="j/two", budget_usd=0, out_dir=tmp_path / "b",
                             log=lambda m: None)
    assert r["supported"]["n"] == 0 and set(r["failed"].values()) == {"budget"}
