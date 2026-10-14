"""M5 acceptance: injected defects are blocked or flagged; selective review; human edits; reports."""

import json
import zipfile

import pytest

from ncte.errors import NCTEError
from ncte.exporter import export_book
from ncte.llm.fake import FakeLLM, default_translate
from ncte.memory.repository import set_glossary
from ncte.reports.cost_report import cost_report
from ncte.reports.html_report import render_html
from ncte.reports.qa_report import qa_report
from ncte.translation.cache import latest_texts
from ncte.translation.edits import apply_human_edit

from ..helpers import run_book


def defective(payload, req):
    """Translate normally, then inject one defect per marker sentence."""
    out = default_translate(payload, req)
    src = {u["unit_id"]: u["source"] for u in payload["units"]}
    reviewing = "review_feedback" in payload
    for t in out["translations"]:
        s = src[t["unit_id"]]
        if reviewing:
            continue  # the corrective retranslation is clean
        if "朝の市場" in s:
            t["translated_text"] = "早上。"  # omission
        elif "腕を組んで" in s:
            t["translated_text"] = t["translated_text"].replace("米娜", "米奈")  # wrong name
        elif "頭をかいた" in s:
            t["translated_text"] = "海斗說：" + t["translated_text"]  # fine (カイト in source) – control
        elif s.startswith("「ありがとうございます"):
            t["translated_text"] = "米娜說：「謝謝，我會好好珍惜的」"  # adds a speaker name not in the source
        elif s.startswith("「なくしませんよ") or s.startswith("「本当だろうな"):
            t["translated_text"] = "「這句話被重複翻譯了喔」"  # duplicated paragraph
    return out


def review_responder(payload, req):
    issues = []
    for item in payload["items"]:
        issues.append({"unit_id": item["unit_id"], "issue_type": "omission" if "早上" in item["translation"] else "other",
                       "severity": "high", "evidence": f"原文「{item['source'][:10]}」", "suggestion": "完整翻譯",
                       "needs_retranslation": True})
    issues.append({"unit_id": "u_not_in_batch", "issue_type": "other", "severity": "high", "evidence": "x",
                   "suggestion": "x", "needs_retranslation": True})
    return {"issues": issues}


@pytest.fixture
def seeded(conn, cfg, fixtures):
    from ncte.library import import_book

    book, _ = import_book(conn, cfg, fixtures["basic"])
    set_glossary(conn, book.id, "ミナ", "米娜", category="character")
    set_glossary(conn, book.id, "カイト", "海斗", category="character")
    return book


def findings(conn, rid):
    return {(r["unit_id"], r["type"]) for r in conn.execute("SELECT * FROM qa_findings WHERE run_id=?", (rid,))}


def text_of(conn, book, needle):
    return conn.execute("SELECT id FROM units WHERE book_id=? AND plain_text LIKE ?", (book.id, f"%{needle}%")).fetchone()[0]


def test_injected_defects_are_detected(conn, cfg, fixtures, seeded):
    cfg.review.automatic_review = "off"
    fake = FakeLLM(responders={"translate": defective}, faults={"translate": ["bad_placeholder"] * 0})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake)
    f = findings(conn, rid)
    assert (text_of(conn, book, "朝の市場"), "too_short") in f
    assert (text_of(conn, book, "腕を組んで"), "glossary_mismatch") in f
    assert (text_of(conn, book, "ありがとうございます"), "possible_added_name") in f
    assert (text_of(conn, book, "頭をかいた"), "possible_added_name") not in f
    dup = {u for u, t in f if t == "duplicate_translation"}
    assert dup & {text_of(conn, book, "なくしませんよ"), text_of(conn, book, "本当だろうな")}
    statuses = {r["unit_id"]: r["status"] for r in conn.execute("SELECT * FROM translations WHERE run_id=?", (rid,))}
    assert statuses[text_of(conn, book, "朝の市場")] == "needs_review"
    rep = qa_report(conn, book_id=book.id, run_id=rid)
    assert len(rep["needs_review"]) >= 4


def test_broken_placeholder_is_blocked_and_original_kept(conn, cfg, fixtures):
    fake = FakeLLM(faults={"translate": ["bad_placeholder"] * 40})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, unit_ids=None)
    em_unit = conn.execute("SELECT id FROM units WHERE book_id=? AND source_text LIKE '%⟦1⟧絶対に⟦/1⟧%'", (book.id,)).fetchone()[0]
    # every batch's first unit is corrupted each time → those units eventually fail and keep Japanese
    failed = {r[0] for r in conn.execute("SELECT target_id FROM jobs WHERE run_id=? AND status='failed'", (rid,))}
    assert failed
    out = export_book(conn, cfg, book, run_epubcheck=False)
    assert out.validation.ok and out.stats["untranslated_kept_original"] == len(failed)
    if em_unit in failed:
        ch2 = zipfile.ZipFile(out.result.output_path).read("OEBPS/Text/ch2.xhtml").decode()
        assert "<em>絶対に</em>" in ch2


def test_selective_review_fixes_flagged_units_once(conn, cfg, fixtures, seeded):
    cfg.review.automatic_review = "flagged_only"
    fake = FakeLLM(responders={"translate": defective, "review": review_responder})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake)
    review_calls = [p for p, c in zip(fake.payloads, fake.calls, strict=True) if c.stage == "review"]
    reviewed = {i["unit_id"] for p in review_calls for i in p["items"]}
    flagged = {r[0] for r in conn.execute("SELECT unit_id FROM qa_findings WHERE run_id=? AND severity='high' AND "
                                          "type NOT LIKE 'review%' AND type NOT LIKE 'after_review%'", (rid,))}
    assert reviewed == flagged  # only flagged units are reviewed
    total = conn.execute("SELECT COUNT(*) FROM units WHERE book_id=? AND kind IN ('text','heading') AND translatable=1",
                         (book.id,)).fetchone()[0]
    assert len(reviewed) < total
    # omission fixed by the corrective retranslation
    best = latest_texts(conn, book.id)
    omitted = text_of(conn, book, "朝の市場")
    assert best[omitted]["status"] == "succeeded" and "review_retranslation" in best[omitted]["flags_json"]
    assert best[omitted]["text"] != "早上。"
    # the issue for a unit outside the batch was rejected, and each unit was reviewed only once
    assert any(t == "review_rejected_issue" for _, t in findings(conn, rid))
    jobs = conn.execute("SELECT target_id, COUNT(*) c FROM jobs WHERE run_id=? AND stage='review' GROUP BY target_id",
                        (rid,)).fetchall()
    assert all(j["c"] == 1 for j in jobs)
    assert len(review_calls) == 1


def test_review_modes(conn, cfg, fixtures, seeded):
    cfg.review.automatic_review = "all"
    fake = FakeLLM(responders={"review": lambda p, r: {"issues": []}})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake)
    items = sum(len(p["items"]) for p, c in zip(fake.payloads, fake.calls, strict=True) if c.stage == "review")
    body = conn.execute("SELECT COUNT(*) FROM units WHERE book_id=? AND kind IN ('text','heading') AND translatable=1",
                        (book.id,)).fetchone()[0]
    assert items == body
    cfg.review.automatic_review = "flagged_only"
    fake2 = FakeLLM()
    run_book(conn, cfg, fixtures["basic"], fake2, force=True)
    assert not any(c.stage == "review" for c in fake2.calls)  # clean output → nothing flagged → no review cost


def test_context_mode_review_per_scene(conn, cfg, fixtures, seeded):
    cfg.review.automatic_review = "flagged_only"
    fake = FakeLLM(responders={"translate": defective, "review": review_responder})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    stages = [c.stage.split(":")[0] for c in fake.calls]
    # read first (ADR-023): every scene is analysed and remembered before the first translation …
    first_tr = stages.index("translate")
    assert {"scene_analyze", "memory_update"}.isdisjoint(stages[first_tr:])
    # … and review still runs per scene, between the translations of consecutive scenes
    first_review = stages.index("review")
    assert "translate" in stages[first_review:]


def test_context_mode_interleaved_when_read_first_off(conn, cfg, fixtures, seeded):
    cfg.review.automatic_review = "flagged_only"
    cfg.context.read_first = False
    fake = FakeLLM(responders={"translate": defective, "review": review_responder})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    stages = [c.stage for c in fake.calls]
    first_review = stages.index("review")
    assert stages[first_review - 1] in ("memory_update", "translate")
    assert "scene_analyze" in stages[first_review:]


def test_human_edit(conn, cfg, fixtures):
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM())
    uid = conn.execute("SELECT id FROM units WHERE book_id=? AND source_text LIKE '%⟦1⟧絶対に⟦/1⟧%'", (book.id,)).fetchone()[0]
    with pytest.raises(NCTEError):
        apply_human_edit(conn, book.id, uid, "「明天一定要早點來」", force=True)  # placeholder dropped: never allowed
    with pytest.raises(NCTEError):
        apply_human_edit(conn, book.id, uid, "　それに、もう一つ⟦1⟧絶対に⟦/1⟧」")  # untranslated → rejected
    apply_human_edit(conn, book.id, uid, "　還有，明天⟦1⟧一定⟦/1⟧要早點來。」", reason="editor")
    run_book(conn, cfg, fixtures["basic"], FakeLLM(), force=True)  # machine rerun
    assert latest_texts(conn, book.id)[uid]["status"] == "human"
    out = export_book(conn, cfg, book, run_epubcheck=False)
    ch2 = zipfile.ZipFile(out.result.output_path).read("OEBPS/Text/ch2.xhtml").decode()
    assert "還有，明天<em>一定</em>要早點來。」" in ch2
    assert conn.execute("SELECT COUNT(*) FROM human_edits WHERE unit_id=?", (uid,)).fetchone()[0] == 1


def test_reports(conn, cfg, fixtures, seeded):
    cfg.review.automatic_review = "flagged_only"
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={"translate": defective,
                                                                             "review": review_responder}))
    cost = cost_report(conn, run_id=rid)
    assert set(cost["by_stage"]) == {"translate", "review"}
    assert cost["metrics"]["usd_per_10k_ja_chars"] > 0 and cost["totals"]["cost_reported_usd"] > 0
    qa = qa_report(conn, book_id=book.id, run_id=rid)
    html = render_html("t", cost, qa)
    assert "<table>" in html and "review" in html and "&lt;" not in "".join(json.dumps(qa))


def test_review_command_on_existing_translations(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws = tmp_path / "ws"
    epub = str(fixtures["basic"])
    runner = CliRunner()
    assert runner.invoke(app, ["-w", str(ws), "translate", epub, "--fake"]).exit_code == 0
    r = runner.invoke(app, ["-w", str(ws), "review", epub, "--scope", "all", "--fake"])
    assert r.exit_code == 0, r.output
    assert "review: {'reviewed':" in r.output


def test_recheck_keeps_reviewer_decisions(conn, cfg, fixtures, seeded):
    from ncte.qa.recheck import recheck_book

    cfg.review.automatic_review = "flagged_only"
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={"translate": defective,
                                                                             "review": lambda p, r: {"issues": []}}))
    cleared = conn.execute("SELECT COUNT(*) FROM qa_findings WHERE status='cleared_by_review'").fetchone()[0]
    assert cleared > 0
    stats = recheck_book(conn, book.id)
    assert stats["newly_flagged"] == 0
    assert conn.execute("SELECT COUNT(*) FROM translations WHERE status='needs_review'").fetchone()[0] == 0
