"""Regressions found in the first real-book paid sample (2026-10-13)."""

import json

import pytest

from ncte.analysis.scene import PRONOUNS
from ncte.config import Config
from ncte.errors import NCTEError
from ncte.llm.cost import Pricing
from ncte.qa.rules import check_translation
from ncte.translation.cache import relevant_glossary
from ncte.translation.orchestrator import check_reasoning_effort

G = [{"source_term": "蒼依", "target_term": "蒼依", "verified": True, "category": "character"}]


def test_glossary_matches_names_with_ruby():
    assert relevant_glossary(G, "⟦1⟧「⟦/1⟧蒼《あお》依《い》⟦2⟧！」⟦/2⟧") == G


def test_no_false_added_name_for_ruby_names():
    f = check_translation(unit_id="u", kind="text", source_text="蒼《あお》依《い》の狙い、すっげー",
                          plain_text="蒼《あお》依《い》の狙い、すっげー", inline_map={}, ruby_count=1,
                          text="蒼依的瞄準太神了吧。", glossary=G)
    assert not [x for x in f if x.type in ("possible_added_name", "glossary_mismatch")]


def test_pronouns_are_not_names():
    assert {"私", "俺", "あなた", "彼女"} <= PRONOUNS


def test_reasoning_effort_validated_and_sent():
    p = Pricing.from_model_json({"id": "m", "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                                 "supported_parameters": ["reasoning", "structured_outputs"],
                                 "reasoning": {"supported_efforts": ["high", "medium", "low"]}})
    cfg = Config()
    cfg.openrouter.reasoning_effort = "low"
    check_reasoning_effort(cfg, p)
    cfg.openrouter.reasoning_effort = "none"
    with pytest.raises(NCTEError):
        check_reasoning_effort(cfg, p)


def test_reasoning_effort_in_request_and_recorded(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM

    from ..helpers import run_book

    cfg.openrouter.reasoning_effort = "low"
    fake = FakeLLM()
    _, rid, s = run_book(conn, cfg, fixtures["basic"], fake, limit=2)
    assert fake.calls[0].extra == {"reasoning": {"effort": "low"}}
    assert {r[0] for r in conn.execute("SELECT reasoning_effort FROM llm_requests WHERE run_id=?", (rid,))} == {"low"}


def test_dry_run_calibration(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM
    from ncte.translation.dryrun import dry_run
    from ncte.translation.orchestrator import RunOptions

    from ..helpers import run_book

    cfg.translation.unit_target_japanese_chars = 100
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM())
    plan = dry_run(conn, cfg, book, RunOptions(model=cfg.openrouter.model, force=True), FakeLLM(), log=lambda m: None)
    assert plan.calibrated_cost_usd is not None and plan.calibrated_cost_usd > 0
    assert json.dumps(plan.calibration)  # serialisable, per stage


def test_generic_terms_are_not_glossary_terms():
    from ncte.lexicon import acceptable_glossary_term

    assert not any(acceptable_glossary_term(t) for t in ["男", "母", "姫", "先生", "お客さん", "スタッフ",
                                                       "おねえちゃん", "姉ちゃん", "ママ"])
    assert acceptable_glossary_term("ソラっち") and acceptable_glossary_term("天野ソラ")


def test_auto_glossary_entries_do_not_invalidate_cache(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM
    from ncte.memory.repository import ensure_glossary, set_glossary

    from ..helpers import run_book

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM())
    ensure_glossary(conn, book.id, "カイト", "海斗", category="character", origin="memory_update", first_unit_id=None)
    _, _, s = run_book(conn, cfg, fixtures["basic"], FakeLLM())
    assert "succeeded" not in s.counts  # an automatic candidate never forces a retranslation
    set_glossary(conn, book.id, "カイト", "凱特", category="character")  # a human decision does
    _, _, s = run_book(conn, cfg, fixtures["basic"], FakeLLM())
    assert s.counts.get("succeeded", 0) >= 3


def test_reviewer_without_issues_clears_program_flag(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM, default_translate
    from ncte.translation.cache import latest_texts

    from ..helpers import run_book

    def short(payload, req):
        out = default_translate(payload, req)
        for t in out["translations"]:
            if "朝の市場" in next(u["source"] for u in payload["units"] if u["unit_id"] == t["unit_id"]):
                t["translated_text"] = "早上。"
        return out

    cfg.review.automatic_review = "flagged_only"
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={"translate": short}))
    uid = conn.execute("SELECT id FROM units WHERE plain_text LIKE '%朝の市場%'").fetchone()[0]
    assert latest_texts(conn, book.id)[uid]["status"] == "succeeded"
    st = {r[0] for r in conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='too_short'", (uid,))}
    assert st == {"cleared_by_review"}
