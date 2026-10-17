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
    cid, _ = upsert_character(conn, book.id, "サクヤ", name_zh="咲夜", aliases=["謎の少女"], unit_id=early["id"],
                              ordinal=early["ordinal"], origin="t")
    ensure_glossary(conn, book.id, "謎の少女", "謎之少女", category="character", origin="t", first_unit_id=early["id"])
    ensure_glossary(conn, book.id, "サクヤ", "咲夜", category="character", origin="t", first_unit_id=late["id"])
    conn.execute("INSERT INTO entity_links(book_id, term, target_term, form_type, origin, status, revealed_ordinal, "
                 "created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (book.id, "謎の少女", "サクヤ", "alias", "llm", "accepted", late["ordinal"], utcnow()))
    c = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()

    before = vis.character_view(conn, book.id, c, late["ordinal"] - 1)
    assert before == {"name_ja": "謎の少女", "name_zh": "謎之少女", "aliases_ja": []}
    after = vis.character_view(conn, book.id, c, late["ordinal"])
    assert after == {"name_ja": "サクヤ", "name_zh": "咲夜", "aliases_ja": ["謎の少女"]}
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
    cfg.context.naming = "review"  # the pause for a person's choice (not the default)
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
    cfg.context.naming = "review"  # the pause for a person's choice (not the default)

    def one_char(payload, req):
        return {"names": [{"ref": "カイト", "zh": "凱", "reason": "簡短", "alternatives": []}], "address": []}

    fake = FakeLLM(responders={**RESPONDERS, "naming": one_char})
    book, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "awaiting_names"
    assert [p["chosen_zh"] for p in naming.pending(conn, book.id)] == ["凱"]


def test_no_pause_takes_a_two_character_alternative_for_a_lone_character(conn, cfg, fixtures, low_thresholds):
    def one_char(payload, req):
        return {"names": [{"ref": "カイト", "zh": "凱", "reason": "簡短",
                           "alternatives": [{"zh": "海斗", "reason": "音譯"}]}], "address": []}

    fake = FakeLLM(responders={**RESPONDERS, "naming": one_char})
    book, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")  # default: no pause
    assert s.status == "completed"
    assert conn.execute("SELECT target_term FROM glossary WHERE source_term='カイト'").fetchone()[0] == "海斗"


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
    cfg.context.naming = "review"  # the pause for a person's choice (not the default)

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
    assert naming.term_pattern("ミツ").search("ミツが笑う") is not None
    assert naming.term_pattern("シロ").search("ミツが笑う") is None
    assert naming.term_pattern("たかぎちゃん").search("ところでたかぎちゃん") is not None
    assert naming.term_pattern("たかぎ").search("たかぎは笑う") is not None


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
    cfg.context.naming = "review"  # the pause for a person's choice (not the default)
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
    mom, friend, a = mk("ママ", "媽媽"), mk("ヒノ", "小林"), mk("アダチ", "水野")
    conn.execute("UPDATE characters SET aliases_json='[\"みずっちー\"]' WHERE id=?", (a,))  # the nickname is アダチ's
    for sp, form in ((mom, "アダチちゃん"), (friend, "みずっちー"), (friend, "みずっち")):
        upsert(conn, book.id, sp, a, form, ordinal=us[1]["ordinal"], unit_id=us[1]["id"], origin="llm",
               certainty="inferred")
    ensure_glossary(conn, book.id, "みずっちー", "水野親", category="character", origin="t", first_unit_id=us[1]["id"])
    payload = naming.build_naming_payload(conn, book.id)
    payload["names"] = [{"ref": "みずっちー", "ja": "みずっちー", "zh": "水野親", "category": "character", "count": 1}]
    out = NamingOutput.model_validate({
        "names": [{"ref": "みずっちー", "zh": "小水野", "reason": "", "alternatives": []}],
        "address": [{"ref": x["ref"], "zh": "小水野", "reason": "", "alternatives": []} for x in payload["address"]]})
    naming.apply_naming(conn, book.id, payload, out)
    pend = {p["label"]: p["reason"] for p in naming.pending(conn, book.id) if p["kind"] == "address"}
    assert set(pend) == {"媽媽 → 水野：アダチちゃん", "小林 → 水野：みずっちー", "小林 → 水野：みずっち"}
    assert "同譯為「小水野」" in pend["媽媽 → 水野：アダチちゃん"]
    # deciding the nickname as a name also settles the same form in the address table
    pid = next(p["id"] for p in naming.pending(conn, book.id, include_done=True) if p["ref"] == "みずっちー")
    naming.decide(conn, book.id, pid, "水野兒")
    row = conn.execute("SELECT form_zh, zh_origin FROM address_terms WHERE form_ja='みずっちー'").fetchone()
    assert tuple(row) == ("水野兒", "human")
    assert "小林 → 水野：みずっちー" not in {p["label"] for p in naming.pending(conn, book.id)}


def test_name_decision_only_syncs_address_terms_for_that_person(conn, cfg, fixtures):
    from ncte.llm.schemas import NamingOutput
    from ncte.memory.address import upsert

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    mk = lambda n, zh: upsert_character(conn, book.id, n, name_zh=zh, aliases=[], unit_id=us[0]["id"],  # noqa: E731
                                        ordinal=us[0]["ordinal"], origin="t")[0]
    friend, a, mom = mk("ヒノ", "小林"), mk("アダチ", "水野"), mk("アダチさん", "水野媽媽")
    ensure_glossary(conn, book.id, "アダチさん", "水野媽媽", category="character", origin="t", first_unit_id=us[0]["id"])
    upsert(conn, book.id, friend, a, "アダチさん", ordinal=us[1]["ordinal"], unit_id=us[1]["id"], origin="llm",
           certainty="inferred", form_zh="水野同學")
    upsert(conn, book.id, friend, mom, "アダチさん", ordinal=us[2]["ordinal"], unit_id=us[2]["id"], origin="llm",
           certainty="inferred", form_zh="阿姨")
    payload = {"names": [{"ref": "アダチさん", "ja": "アダチさん", "zh": "水野媽媽", "category": "character", "count": 9}],
               "address": []}
    naming.apply_naming(conn, book.id, payload, NamingOutput.model_validate(
        {"names": [{"ref": "アダチさん", "zh": "水野太太", "reason": "", "alternatives": [{"zh": "水野媽媽", "reason": ""}]}]}))
    pid = next(p["id"] for p in naming.pending(conn, book.id, include_done=True) if p["ref"] == "アダチさん")
    naming.decide(conn, book.id, pid, "水野太太")
    rows = {r[0]: r[1] for r in conn.execute("SELECT addressee_id, form_zh FROM address_terms WHERE form_ja='アダチさん'")}
    assert rows == {a: "水野同學", mom: "水野太太"}  # the classmate's 「アダチさん」 for アダチ is untouched


def test_honorific_named_character_gets_a_fix_proposal(conn, cfg, fixtures):
    """A character named 「アダチさん」 (someone's mother) that overlaps アダチ: naming proposes renaming her and
    sharing the form; deciding it renames, records the shared form and drops the name proposal."""
    from ncte.llm.schemas import NamingOutput
    from ncte.memory.address import name_forms

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    text = conn.execute("SELECT plain_text FROM units WHERE id=?", (us[1]["id"],)).fetchone()[0]
    conn.execute("UPDATE units SET plain_text=? WHERE id=?", (text + "「アダチさん、こんにちは」", us[1]["id"]))
    a, _ = upsert_character(conn, book.id, "アダチ", name_zh="水野", aliases=[], unit_id=us[0]["id"],
                            ordinal=us[0]["ordinal"], origin="t")
    mom, _ = upsert_character(conn, book.id, "アダチさん", name_zh="水野太太", aliases=[], unit_id=us[1]["id"],
                              ordinal=us[1]["ordinal"], origin="t")
    ensure_glossary(conn, book.id, "アダチさん", "水野太太", category="character", origin="t", first_unit_id=us[1]["id"])
    payload = naming.build_naming_payload(conn, book.id)
    assert [c["ref"] for c in payload["characters_to_check"]] == [mom]
    assert "アダチさん" not in {n["ref"] for n in payload["names"]}  # not proposed as an ordinary name
    assert "_also_id" not in json.dumps(naming.model_view(payload), ensure_ascii=False)
    out = NamingOutput.model_validate({"characters": [{"ref": mom, "verdict": "different", "name_ja": "アダチの母",
                                                       "name_zh": "水野媽媽", "reason": "對母親的稱呼"}]})
    naming.apply_naming(conn, book.id, payload, out)
    p = next(x for x in naming.pending(conn, book.id) if x["kind"] == "character")
    assert p["options"][0]["zh"].startswith("改名為「水野媽媽」") and p["options"][-1]["zh"] == "維持現狀"
    naming.decide(conn, book.id, p["id"], p["options"][0]["zh"])
    c = conn.execute("SELECT canonical_name, display_name_zh, aliases_json FROM characters WHERE id=?", (mom,)).fetchone()
    assert (c[0], c[1]) == ("アダチの母", "水野媽媽") and "アダチさん" in json.loads(c[2])
    fmap, ambiguous = name_forms(conn, book.id)
    assert "アダチさん" in ambiguous and "アダチさん" not in fmap  # never attributed automatically any more


def test_pronouns_rendered_alike_are_not_collisions(conn, cfg, fixtures):
    from ncte.memory.address import upsert

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    mk = lambda n: upsert_character(conn, book.id, n, name_zh=n, aliases=[], unit_id=us[0]["id"],  # noqa: E731
                                    ordinal=us[0]["ordinal"], origin="t")[0]
    a, b = mk("ヒノ"), mk("ナガフジ")
    for form in ("あんた", "お前"):
        rid = upsert(conn, book.id, a, b, form, ordinal=us[1]["ordinal"], unit_id=us[1]["id"], origin="llm",
                     certainty="inferred", form_zh="妳")
        conn.execute("INSERT INTO naming_proposals(book_id, kind, ref, label, chosen_zh, options_json, status, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (book.id, "address", str(rid), form, "妳", "[]", "applied", "t"))
    assert naming.flag_collisions(conn, book.id) == 0


def test_rename_reaches_existing_translations(conn, cfg, fixtures):
    from ncte.translation.rename import affected_units, replace_rendering

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    u = conn.execute("SELECT id FROM units WHERE book_id=? AND plain_text LIKE '%カイト%' LIMIT 1", (book.id,)).fetchone()[0]
    conn.execute("UPDATE translations SET text='海斗來了' WHERE unit_id=?", (u,))
    units = affected_units(conn, book.id, form="カイト", old_zh="海斗")
    assert u in {x["unit_id"] for x in units}
    assert replace_rendering(conn, units, "海斗", "凱特") >= 1
    t = conn.execute("SELECT text FROM translations WHERE unit_id=? ORDER BY id DESC LIMIT 1", (u,)).fetchone()[0]
    assert t == "凱特來了"
    # the translator wrote the name where the source only implies him: found too, for a person's name
    v = conn.execute("SELECT id FROM units WHERE book_id=? AND kind='text' AND plain_text NOT LIKE '%カイト%' LIMIT 1",
                     (book.id,)).fetchone()[0]
    conn.execute("UPDATE translations SET text='凱特笑了' WHERE unit_id=?", (v,))
    assert v not in {x["unit_id"] for x in affected_units(conn, book.id, form="カイト", old_zh="凱特")}
    assert v in {x["unit_id"] for x in affected_units(conn, book.id, form="カイト", old_zh="凱特", added=True)}


def test_address_decision_leaves_the_bare_name_alone(conn, cfg, fixtures, low_thresholds):
    cfg.context.naming = "review"  # the pause for a person's choice (not the default)

    def addr(payload, req):
        return {"names": [], "address": [{"ref": a["ref"], "zh": "阿海", "reason": "", "alternatives": [{"zh": "海斗", "reason": ""}]}
                                          for a in payload["address"] if a["ja"] == "カイト"]}

    fake = FakeLLM(responders={**RESPONDERS, "naming": addr})
    book, _, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    conn.execute("UPDATE glossary SET verified=0, origin='memory_update' WHERE source_term='カイト'")
    p = next(x for x in naming.pending(conn, book.id) if x["kind"] == "address")
    naming.decide(conn, book.id, p["id"], "阿海")
    # カイト is the person's own name (narration uses it too): one speaker's 「阿海」 does not rename him
    assert conn.execute("SELECT target_term FROM glossary WHERE source_term='カイト'").fetchone()[0] != "阿海"
    assert conn.execute("SELECT form_zh FROM address_terms WHERE id=?", (int(p["ref"]),)).fetchone()[0] == "阿海"


def test_given_name_decision_reaches_the_full_name(conn, cfg, fixtures):
    from ncte.llm.schemas import NamingOutput

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    cid = upsert_character(conn, book.id, "ノエル・ランメル", name_zh="諾艾兒·朗梅爾", aliases=["ノエル"],
                           unit_id=us[0]["id"], ordinal=us[0]["ordinal"], origin="t")[0]
    for ja, zh in (("ノエル", "諾艾兒"), ("ノエル・ランメル", "諾艾兒·朗梅爾"), ("ノエル商会", "諾艾兒商會")):
        ensure_glossary(conn, book.id, ja, zh, category="character", origin="t", first_unit_id=us[0]["id"])
    payload = {"names": [{"ref": "ノエル", "ja": "ノエル", "zh": "諾艾兒", "category": "character", "count": 9}],
               "address": []}
    for i, (ja, zh, origin) in enumerate((("ノエル様", "諾艾兒大人", "llm"), ("ランメル学院長", "朗梅爾學院長", "llm"),
                                          ("ノエルちゃん", "諾艾兒醬", "human"))):
        conn.execute("INSERT INTO address_terms(book_id, speaker_id, addressee_id, form_ja, form_zh, zh_origin, origin, "
                     "certainty, first_ordinal, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (book.id, f"c_s{i}", cid, ja, zh, origin, "t", "explicit", 1, "t", "t"))
    naming.apply_naming(conn, book.id, payload, NamingOutput.model_validate(
        {"names": [{"ref": "ノエル", "zh": "諾艾兒", "reason": "", "alternatives": [{"zh": "艾莉絲", "reason": ""}]}]}))
    pid = next(p["id"] for p in naming.pending(conn, book.id, include_done=True) if p["ref"] == "ノエル")
    naming.decide(conn, book.id, pid, "艾莉潔")
    assert conn.execute("SELECT display_name_zh FROM characters WHERE id=?", (cid,)).fetchone()[0] == "艾莉潔·朗梅爾"
    assert conn.execute("SELECT target_term FROM glossary WHERE source_term='ノエル・ランメル'").fetchone()[0] == "艾莉潔·朗梅爾"
    assert conn.execute("SELECT target_term FROM glossary WHERE source_term='ノエル商会'").fetchone()[0] == "艾莉潔商會"
    addr = dict(conn.execute("SELECT form_ja, form_zh FROM address_terms").fetchall())
    assert addr == {"ノエル様": "艾莉潔大人", "ランメル学院長": "朗梅爾學院長", "ノエルちゃん": "諾艾兒醬"}  # a person's choice stays


def test_family_name_change_reaches_address_forms(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["basic"])
    conn.execute("INSERT INTO address_terms(book_id, speaker_id, addressee_id, form_ja, form_zh, zh_origin, origin, "
                 "certainty, first_ordinal, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (book.id, "c_a", "c_b", "ヴァイス卿", "福里登卿", "llm", "t", "explicit", 1, "t", "t"))
    naming._sync_address(conn, book.id, "カイル・ヴァイス", "凱爾·福里登", "凱爾·弗里登")
    assert conn.execute("SELECT form_zh FROM address_terms").fetchone()[0] == "弗里登卿"


def test_address_rendering_replaces_a_contradicting_unconfirmed_glossary_entry():
    from ncte.translation.context_pipeline import drop_address_forms

    gl = [{"ja": "たかぎちゃん", "zh": "高木醬", "verified": False}, {"ja": "高木", "zh": "高木", "verified": True},
          {"ja": "みずっち", "zh": "阿達", "verified": True}]
    addr = [{"speaker": "小林", "to": "高木", "ja": "たかぎちゃん", "zh": "小高木"},
            {"speaker": "小林", "to": "水野", "ja": "みずっち", "zh": "小安"}]
    assert [g["ja"] for g in drop_address_forms(gl, addr)] == ["高木", "みずっち"]  # a confirmed entry stays


def test_retranslate_takes_a_reasoning_effort(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    import sqlite3

    db = next((ws / "series").glob("*/ncte.db"))
    uid = sqlite3.connect(db).execute("SELECT id FROM units WHERE translatable=1 LIMIT 1").fetchone()[0]
    base = ["-w", str(ws), "retranslate", epub, "--fake", "--mode", "context", "--unit-id", uid]
    assert r.invoke(app, [*base, "--reasoning-effort", "high"]).exit_code == 0
    assert r.invoke(app, [*base, "--reasoning-effort", "default"]).exit_code == 0
    bad = r.invoke(app, [*base, "--reasoning-effort", "xhigh"])  # the fake model offers low/medium/high
    assert "not supported" in bad.output or bad.exit_code != 0


def test_scene_the_filter_refuses_is_read_in_parts(conn, cfg, fixtures):
    """A content filter that refuses one passage must not cost the whole scene its reading (水野 vol 2 regression)."""
    from ncte.errors import ContentBlockedError

    class ReadFilter(FakeLLM):
        def complete(self, req):
            if req.stage in ("scene_analyze", "memory_update") and "頭をかいた" in req.messages[-1]["content"]:
                raise ContentBlockedError("HTTP 403 content filter: fake PROHIBITED_CONTENT")
            return super().complete(req)

    fake = ReadFilter(responders=RESPONDERS)
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    assert not conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND status='failed'", (rid,)).fetchall()
    blocked = [r[0] for r in conn.execute("SELECT u.plain_text FROM qa_findings f JOIN units u ON u.id=f.unit_id "
                                          "WHERE f.run_id=? AND f.type='read_blocked'", (rid,))]
    assert blocked and len(blocked) <= 2 * 4  # the passage and a few neighbours, for both read steps
    assert any("頭をかいた" in t for t in blocked)
    read = [p for p in fake.payloads if p.get("task") == "scene_analyze"]
    assert any(len(p["units"]) < max(len(q["units"]) for q in read) for p in read)  # it was read in parts


def test_a_scene_is_read_knowing_the_story_so_far(conn, cfg, fixtures):
    """An untagged line opening a scene, or a promise remembered from an earlier scene, needs what came before: the
    read step gets the last summaries and the end of the scene before (context only, never evidence)."""
    fake = FakeLLM(responders=RESPONDERS)
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    read = [p for p in fake.payloads if p.get("task") == "scene_analyze"]
    assert len(read) >= 2
    assert "previous_scene_end" not in read[0] and "story_so_far" not in read[0]
    later = [p for p in read[1:] if "previous_scene_end" in p]
    assert later and all(p["previous_scene_end"] for p in later)
    assert any(p.get("story_so_far") for p in read[1:])
    for p in later:  # the end of another scene, not this one's
        assert not set(p["previous_scene_end"]) & {u["source"] for u in p["units"]}


def test_story_context_the_filter_refuses_is_left_out(conn, cfg, fixtures):
    """The story so far may be what a content filter refuses: the scene is then read without it, not lost."""
    from ncte.errors import ContentBlockedError

    class ContextFilter(FakeLLM):
        def complete(self, req):
            if req.stage == "scene_analyze" and '"previous_scene_end"' in req.messages[-1]["content"]:
                raise ContentBlockedError("HTTP 403 content filter: fake PROHIBITED_CONTENT")
            return super().complete(req)

    fake = ContextFilter(responders=RESPONDERS)
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    assert not conn.execute("SELECT 1 FROM qa_findings WHERE run_id=? AND type='read_blocked'", (rid,)).fetchall()
    assert not conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND stage='scene_analyze' AND status='failed'",
                            (rid,)).fetchall()
