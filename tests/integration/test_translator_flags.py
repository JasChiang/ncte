"""The translator's own doubts are read by someone (ADR-067): a subject left out, an uncertain speaker or term goes to
the scene's rule check with the source and the lines around it; only what the check finds real goes on to review.
Ruby-meaning and wordplay flags and the translator's notes are shown on the side-by-side page."""

from ncte.llm.fake import FakeLLM, default_translate

from ..helpers import run_book
from ..scripted import RESPONDERS

FLAGS = {"銀の鍵をミナに": ("subject_omitted_kept", "交出鑰匙的人原文沒寫"), "朝の市場": ("term_uncertain", "朝の市場 → 早市")}


def flagging(payload, req):
    out = default_translate(payload, req)
    for t, u in zip(out["translations"], payload["units"], strict=True):
        for needle, (flag, note) in FLAGS.items():
            if needle in u["source"] and "review_feedback" not in payload:
                t["flags"], t["notes"] = [flag], [note]
    return out


def test_the_translators_doubts_are_checked_with_context_and_only_real_ones_reviewed(conn, cfg, fixtures):
    asked, reviewed = [], []

    def confirm(payload, req):
        asked.append(payload)
        return {"checks": [{"unit_id": i["unit_id"], "rule": i["rule"], "real": i["rule"].endswith("subject_omitted_kept"),
                            "reason": "前後文看得出是誰" if i["rule"].endswith("subject_omitted_kept") else "早市說得通"}
                           for i in payload["items"]]}

    def review(payload, req):
        reviewed.append(payload)
        return {"issues": []}

    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={
        **RESPONDERS, "translate": flagging, "confirm": confirm, "review": review}), mode="context")
    items = [i for p in asked for i in p["items"]]
    rules = {i["rule"] for i in items}
    assert {"model_flag:subject_omitted_kept", "model_flag:term_uncertain"} <= rules
    sub = next(i for i in items if i["rule"] == "model_flag:subject_omitted_kept")
    assert "交出鑰匙的人原文沒寫" in sub["evidence"] and sub["around"] and "source" in sub
    status = dict(conn.execute("SELECT type, status FROM qa_findings WHERE run_id=? AND type LIKE 'model_flag:%'",
                               (rid,)).fetchall())
    assert status["model_flag:term_uncertain"] == "dismissed_by_check"
    # the real one went on to review, which saw the flag
    seen = {i["unit_id"]: i for p in reviewed for i in p["items"]}
    sub_review = seen[sub["unit_id"]]
    assert any("model_flag:subject_omitted_kept" in f for f in sub_review["flags"])
    term_unit = next(i["unit_id"] for i in items if i["rule"] == "model_flag:term_uncertain")
    assert term_unit not in seen


def test_the_program_rules_and_the_translators_flags_are_named_apart():
    from ncte.qa.rules import CONFIRM_RULES
    from ncte.translation.orchestrator import CONFIRM_MODEL_FLAGS

    assert all(f.startswith("model_flag:") for f in CONFIRM_MODEL_FLAGS)
    assert not set(CONFIRM_MODEL_FLAGS) & set(CONFIRM_RULES)
    assert {"model_flag:subject_omitted_kept", "model_flag:speaker_uncertain",
            "model_flag:term_uncertain"} == set(CONFIRM_MODEL_FLAGS)


def test_ruby_and_wordplay_flags_show_on_the_side_by_side_page(conn, cfg, fixtures):
    from ncte.gui import data

    def marked(payload, req):
        out = default_translate(payload, req)
        for t, u in zip(out["translations"], payload["units"], strict=True):
            if "朝の市場" in u["source"]:
                t["flags"] = ["ruby_meaning_kept", "wordplay"]
        return out

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "translate": marked}),
                          mode="context")
    shown = [u for u in data.units(conn, book)["units"] if u["translator_flags"]]
    assert len(shown) == 1 and shown[0]["translator_flags"] == ["ruby_meaning_kept", "wordplay"]
