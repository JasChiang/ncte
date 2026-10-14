"""ADR-023: read the whole volume first, name, then translate."""

from ncte.library import import_book
from ncte.memory import visibility as vis
from ncte.memory.db import utcnow
from ncte.memory.repository import ensure_glossary, upsert_character


def _units(conn, book_id):
    return conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal",
                        (book_id,)).fetchall()


def test_character_names_follow_the_reveal(conn, cfg, fixtures):
    """A character merged after an identity reveal keeps its earlier name before the reveal."""
    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    early, late = us[1], us[5]
    cid, _ = upsert_character(conn, book.id, "かぐや", name_zh="輝夜", aliases=["謎の少女"], unit_id=early["id"],
                              ordinal=early["ordinal"], origin="t")
    ensure_glossary(conn, book.id, "謎の少女", "謎之少女", category="character", origin="t", first_unit_id=early["id"])
    ensure_glossary(conn, book.id, "かぐや", "輝夜", category="character", origin="t", first_unit_id=late["id"])
    conn.execute("INSERT INTO entity_links(book_id, term, target_term, form_type, origin, status, revealed_ordinal, "
                 "created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (book.id, "謎の少女", "かぐや", "alias", "llm", "accepted", late["ordinal"], utcnow()))
    c = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()

    before = vis.character_view(conn, book.id, c, late["ordinal"] - 1)
    assert before == {"name_ja": "謎の少女", "name_zh": "謎之少女", "aliases_ja": []}
    after = vis.character_view(conn, book.id, c, late["ordinal"])
    assert after == {"name_ja": "かぐや", "name_zh": "輝夜", "aliases_ja": ["謎の少女"]}
    assert vis.character_view(conn, book.id, c, early["ordinal"] - 1) is None


# ---------------------------------------------------------------- naming (pass 2)
import json  # noqa: E402

import pytest  # noqa: E402

from ncte.analysis import naming  # noqa: E402
from ncte.llm.fake import FakeLLM  # noqa: E402

from ..helpers import resume_run, run_book  # noqa: E402
from ..scripted import RESPONDERS  # noqa: E402


def naming_responder(payload, req):
    refs = {n["ref"] for n in payload["names"]}
    out = {"names": [], "address": []}
    if "カイト" in refs:
        out["names"].append({"ref": "カイト", "zh": "海斗", "reason": "沿用漢字感的譯名",
                             "alternatives": [{"zh": "凱特", "reason": "音譯"}]})
    if "銀の鍵" in refs:
        out["names"].append({"ref": "銀の鍵", "zh": "銀之鑰", "reason": "道具名", "alternatives": []})
    out["names"].append({"ref": "not_asked", "zh": "x", "reason": "", "alternatives": []})  # rejected
    return out


@pytest.fixture
def low_thresholds(monkeypatch):
    monkeypatch.setattr(naming, "REVIEW_NAME_COUNT", 1)
    monkeypatch.setattr(naming, "REVIEW_ADDRESS_USES", 1)


def test_naming_pauses_for_choices_then_translates(conn, cfg, fixtures, low_thresholds):
    fake = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "awaiting_names"
    stages = [c.stage.split(":")[0] for c in fake.calls]
    assert "naming" in stages and "translate" not in stages  # read, named, nothing translated yet
    left = naming.pending(conn, book.id)
    assert [(p["label"], [o["zh"] for o in p["options"]]) for p in left] == [("カイト", ["海斗", "凱特"])]
    applied = {p["ref"]: p["status"] for p in naming.pending(conn, book.id, include_done=True)}
    assert applied == {"カイト": "pending", "銀の鍵": "applied"}  # no alternatives → applied without asking

    naming.decide(conn, book.id, left[0]["id"], "凱特")
    g = conn.execute("SELECT target_term, verified FROM glossary WHERE source_term='カイト'").fetchone()
    assert tuple(g) == ("凱特", 1)
    assert conn.execute("SELECT display_name_zh FROM characters WHERE canonical_name='カイト'").fetchone()[0] == "凱特"

    fake2 = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    s2 = resume_run(conn, cfg, rid, fake2)
    assert s2.status == "completed"
    tr = [json.loads(c.messages[1]["content"]) for c in fake2.calls if c.stage.startswith("translate")]
    kaito = [g for p in tr for g in p.get("glossary", []) if g["ja"] == "カイト"]
    assert kaito and all(g["zh"] == "凱特" and g["verified"] for g in kaito)
    assert "naming" not in [c.stage for c in fake2.calls]  # not proposed again


def test_naming_auto_does_not_pause(conn, cfg, fixtures, low_thresholds):
    cfg.context.naming = "auto"
    fake = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    assert [p["label"] for p in naming.pending(conn, book.id)] == ["カイト"]  # still listed for a later look


def test_naming_off_and_legacy_flow_skip_it(conn, cfg, fixtures):
    cfg.context.naming = "off"
    fake = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    _, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed" and "naming" not in [c.stage for c in fake.calls]


def test_single_character_name_needs_a_decision(conn, cfg, fixtures, low_thresholds):
    def one_char(payload, req):
        return {"names": [{"ref": "カイト", "zh": "凱", "reason": "簡短", "alternatives": []}], "address": []}

    fake = FakeLLM(responders={**RESPONDERS, "naming": one_char})
    book, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "awaiting_names"
    assert [p["chosen_zh"] for p in naming.pending(conn, book.id)] == ["凱"]


def test_human_renderings_are_not_proposed_or_overwritten(conn, cfg, fixtures, low_thresholds):
    from ncte.library import import_book
    from ncte.memory.repository import set_glossary

    book, _ = import_book(conn, cfg, fixtures["basic"])
    set_glossary(conn, book.id, "カイト", "阿凱", category="character")
    fake = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    _, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    payload = next(json.loads(c.messages[1]["content"]) for c in fake.calls if c.stage == "naming")
    assert "カイト" not in {n["ref"] for n in payload["names"]}
    assert conn.execute("SELECT target_term FROM glossary WHERE source_term='カイト'").fetchone()[0] == "阿凱"
    assert s.status == "completed"  # nothing left to decide (カイト rejected as not asked)


def test_naming_payload_has_counts_and_samples(conn, cfg, fixtures):
    fake = FakeLLM(responders=RESPONDERS)
    book, _, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    p = naming.build_naming_payload(conn, book.id)
    kaito = next(n for n in p["names"] if n["ref"] == "カイト")
    assert kaito["count"] >= 3 and kaito["samples"] and all("カイト" in s for s in kaito["samples"])


def test_cli_proposals_and_decide(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "names", "proposals", epub])
    assert out.exit_code == 0 and "no proposals waiting" in out.output
    bad = r.invoke(app, ["-w", str(ws), "names", "decide", epub, "--id", "1"])
    assert bad.exit_code != 0
    out = r.invoke(app, ["-w", str(ws), "names", "propose", epub, "--fake"])
    assert out.exit_code == 0, out.output


def test_address_renderings_proposed_per_speaker(conn, cfg, fixtures, low_thresholds):
    def addr(payload, req):
        return {"names": [], "address": [
            {"ref": a["ref"], "zh": "阿海", "reason": "同伴間親暱", "alternatives": [{"zh": "海斗", "reason": "直呼"}]}
            for a in payload["address"] if a["ja"] == "カイト"]}

    fake = FakeLLM(responders={**RESPONDERS, "naming": addr})
    book, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "awaiting_names"
    p = naming.pending(conn, book.id)
    assert [x["label"] for x in p] == ["米娜 → 海斗：カイト"]
    row = conn.execute("SELECT form_zh, zh_origin FROM address_terms WHERE form_ja='カイト'").fetchone()
    assert tuple(row) == ("阿海", "naming")
    naming.decide(conn, book.id, p[0]["id"], "海斗")
    row = conn.execute("SELECT form_zh, zh_origin FROM address_terms WHERE form_ja='カイト'").fetchone()
    assert tuple(row) == ("海斗", "human")


def test_kana_names_do_not_count_inside_words():
    assert naming.term_pattern("のん").search("このままのんびり") is None
    assert naming.term_pattern("のん").search("イラスト：のん") is not None
    assert naming.term_pattern("ヤシロ").search("ヤシロが笑う") is not None
    assert naming.term_pattern("シロ").search("ヤシロが笑う") is None


def test_address_forms_only_from_characters_known_by_then(conn, cfg, fixtures):
    from ncte.memory.address import name_forms

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    upsert_character(conn, book.id, "カイト", name_zh="海斗", aliases=[], unit_id=us[0]["id"], ordinal=us[0]["ordinal"],
                     origin="t")
    upsert_character(conn, book.id, "カイトさん", name_zh="海斗媽媽", aliases=[], unit_id=us[6]["id"],
                     ordinal=us[6]["ordinal"], origin="t")
    early, _ = name_forms(conn, book.id, us[3]["ordinal"])
    assert "カイトさん" not in early and "カイト" in early
    late, _ = name_forms(conn, book.id, us[6]["ordinal"])
    assert "カイトさん" in late


# ---------------------------------------------------------------- book matter (ADR-024)
def test_classify_front_story_back():
    from ncte.epub.matter import classify

    hrefs = ["cover.xhtml", "p0.html", "toc.xhtml", "c1.html", "c2.html", "p9.html", "end.html", "x.html"]
    labels = {"cover.xhtml": "表紙", "toc.xhtml": "目次", "c1.html": "第一章", "c2.html": "第二章", "end.html": "奥付"}
    first = {"p9.html": "あとがき"}
    assert classify(hrefs, labels, first) == {
        "cover.xhtml": "front", "p0.html": "front", "toc.xhtml": "front", "c1.html": "story", "c2.html": "story",
        "p9.html": "back", "end.html": "back", "x.html": "back"}
    # no TOC labels: file names decide
    assert classify(["p-cover.xhtml", "p-caution.xhtml", "p-001.xhtml", "p-colophon.xhtml"], {}) == {
        "p-cover.xhtml": "front", "p-caution.xhtml": "front", "p-001.xhtml": "story", "p-colophon.xhtml": "back"}


def test_book_matter_scenes_are_translated_not_analysed(conn, cfg, fixtures, monkeypatch):
    import ncte.epub.matter as matter

    book, _ = import_book(conn, cfg, fixtures["basic"])
    hrefs = [r[0] for r in conn.execute("SELECT resource_href, MIN(ordinal) o FROM units WHERE book_id=? AND "
                                        "spine_index IS NOT NULL GROUP BY resource_href ORDER BY o", (book.id,))]
    monkeypatch.setattr(matter, "story_hrefs", lambda c, b: set(hrefs[:-1]))  # last file = colophon
    fake = FakeLLM(responders=RESPONDERS)
    _, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    last_scenes = {r[0] for r in conn.execute("SELECT DISTINCT scene_id FROM units WHERE book_id=? AND resource_href=?",
                                              (book.id, hrefs[-1]))}
    analysed = {r[0] for r in conn.execute("SELECT target_id FROM jobs WHERE run_id=? AND stage IN "
                                           "('scene_analyze','memory_update') AND status='succeeded'", (rid,))}
    assert last_scenes and not (last_scenes & analysed)
    untranslated = conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND stage='translate' AND status NOT IN "
                                "('succeeded','needs_review','skipped')", (rid,)).fetchone()[0]
    assert untranslated == 0


def test_proposals_keep_examples_out_of_the_request(conn, cfg, fixtures, low_thresholds):
    fake = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    book, _, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    sent = next(json.loads(c.messages[1]["content"]) for c in fake.calls if c.stage == "naming")
    assert "_context" not in json.dumps(sent, ensure_ascii=False)
    p = naming.pending(conn, book.id)[0]
    assert p["context"] and all("カイト" in c["text"] and c["at"] for c in p["context"])


def test_pronoun_address_always_reviewed_when_frequent(conn, cfg, fixtures, low_thresholds):
    from ncte.llm.schemas import NamingOutput
    from ncte.memory.address import upsert

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    a, _ = upsert_character(conn, book.id, "ミナ", name_zh="米娜", aliases=[], unit_id=us[0]["id"],
                            ordinal=us[0]["ordinal"], origin="t")
    b, _ = upsert_character(conn, book.id, "カイト", name_zh="海斗", aliases=[], unit_id=us[0]["id"],
                            ordinal=us[0]["ordinal"], origin="t")
    upsert(conn, book.id, a, b, "あなた", ordinal=us[1]["ordinal"], unit_id=us[1]["id"], origin="llm",
           certainty="inferred")
    payload = naming.build_naming_payload(conn, book.id)
    item = next(x for x in payload["address"] if x["ja"] == "あなた")
    assert item["pronoun"] is True
    out = NamingOutput.model_validate({"address": [{"ref": item["ref"], "zh": "你", "reason": "", "alternatives": []}]})
    naming.apply_naming(conn, book.id, payload, out)
    assert [p["label"] for p in naming.pending(conn, book.id)] == ["米娜 → 海斗：あなた"]


def test_forms_rendered_alike_are_flagged_and_names_sync_address(conn, cfg, fixtures, low_thresholds):
    from ncte.llm.schemas import NamingOutput
    from ncte.memory.address import upsert

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    mk = lambda n, zh: upsert_character(conn, book.id, n, name_zh=zh, aliases=[], unit_id=us[0]["id"],  # noqa: E731
                                        ordinal=us[0]["ordinal"], origin="t")[0]
    mom, friend, a = mk("ママ", "媽媽"), mk("ヒノ", "日野"), mk("アダチ", "安達")
    for sp, form in ((mom, "アダチちゃん"), (friend, "あだっちー"), (friend, "あだっち")):
        upsert(conn, book.id, sp, a, form, ordinal=us[1]["ordinal"], unit_id=us[1]["id"], origin="llm",
               certainty="inferred")
    ensure_glossary(conn, book.id, "あだっちー", "安達親", category="character", origin="t", first_unit_id=us[1]["id"])
    payload = naming.build_naming_payload(conn, book.id)
    payload["names"] = [{"ref": "あだっちー", "ja": "あだっちー", "zh": "安達親", "category": "character", "count": 1}]
    out = NamingOutput.model_validate({
        "names": [{"ref": "あだっちー", "zh": "小安達", "reason": "", "alternatives": []}],
        "address": [{"ref": x["ref"], "zh": "小安達", "reason": "", "alternatives": []} for x in payload["address"]]})
    naming.apply_naming(conn, book.id, payload, out)
    pend = {p["label"]: p["reason"] for p in naming.pending(conn, book.id) if p["kind"] == "address"}
    assert set(pend) == {"媽媽 → 安達：アダチちゃん", "日野 → 安達：あだっちー", "日野 → 安達：あだっち"}
    assert "同譯為「小安達」" in pend["媽媽 → 安達：アダチちゃん"]
    # deciding the nickname as a name also settles the same form in the address table
    pid = next(p["id"] for p in naming.pending(conn, book.id, include_done=True) if p["ref"] == "あだっちー")
    naming.decide(conn, book.id, pid, "安達兒")
    row = conn.execute("SELECT form_zh, zh_origin FROM address_terms WHERE form_ja='あだっちー'").fetchone()
    assert tuple(row) == ("安達兒", "human")
    assert "日野 → 安達：あだっちー" not in {p["label"] for p in naming.pending(conn, book.id)}
