"""ADR-019: entities with several name forms (official / nickname / alias …)."""

import json

import pytest
from typer.testing import CliRunner

from ncte.cli import app
from ncte.epub.units import InlineSpec  # noqa: F401
from ncte.library import import_book
from ncte.llm.fake import FakeLLM
from ncte.memory.entities import detect_links, entity_forms, link
from ncte.memory.repository import ensure_glossary, set_glossary
from ncte.qa.rules import check_translation
from ncte.translation.cache import glossary_entries, is_cached
from ncte.translation.context_builder import glossary_payload

from ..helpers import run_book
from ..scripted import RESPONDERS


def book_with_terms(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["basic"])
    ensure_glossary(conn, book.id, "クロガネ", "黑鐵", category="organization", origin="memory_update",
                    first_unit_id=None)
    ensure_glossary(conn, book.id, "黒鬼", "黑鬼", category="organization", origin="memory_update", first_unit_id=None)
    return book


def test_pattern_links_official_name_and_nickname(conn, cfg, fixtures):
    book = book_with_terms(conn, cfg, fixtures)
    rows = [{"id": "u_x", "ordinal": 5, "plain_text": "　三人組のユニット、クロガネ。通称黒《くろ》鬼《おに》の登場だ。"}]
    assert detect_links(conn, book.id, rows) == [("黒鬼", "クロガネ", "accepted")]
    forms = {f["ja"]: f["form"] for fs in entity_forms(conn, book.id).values() if len(fs) > 1 for f in fs}
    assert forms == {"クロガネ": "official", "黒鬼": "nickname"}
    ev = conn.execute("SELECT origin, evidence_unit_id, revealed_ordinal FROM entity_links").fetchone()
    assert tuple(ev) == ("pattern", "u_x", 5)


def test_unknown_nickname_becomes_candidate(conn, cfg, fixtures):
    book = book_with_terms(conn, cfg, fixtures)
    rows = [{"id": "u_y", "ordinal": 7, "plain_text": "　クロガネ、略してＫＧは強い。"}]
    assert detect_links(conn, book.id, rows) == [("ＫＧ", "クロガネ", "candidate")]


def test_translation_payload_shows_forms_only_after_reveal(conn, cfg, fixtures):
    book = book_with_terms(conn, cfg, fixtures)
    link(conn, book.id, "黒鬼", "クロガネ", "nickname", origin="pattern", ordinal=100,
         note="the fans' nickname")
    gl = glossary_entries(conn, book.id)
    before = glossary_payload(gl, "クロガネが来た", upto=(1.0, 50), revealed={"黒鬼": (1.0, 100)})
    after = glossary_payload(gl, "黒鬼が来た", upto=(1.0, 150), revealed={"黒鬼": (1.0, 100)})
    assert "same_entity_as" not in before[0]  # the nickname is not revealed yet
    assert after[0]["form"] == "nickname" and after[0]["note"] == "the fans' nickname"
    assert after[0]["same_entity_as"] == [{"ja": "クロガネ", "zh": "黑鐵", "form": "official"}]
    assert "may_refer_to" not in after[0]


def test_qa_form_substitution_and_offensive(conn, cfg, fixtures):
    book = book_with_terms(conn, cfg, fixtures)
    link(conn, book.id, "黒鬼", "クロガネ", "nickname", origin="human")
    gl = glossary_entries(conn, book.id)

    def types(src, text):
        return {f.type for f in check_translation(unit_id="u", kind="text", source_text=src, plain_text=src,
                                                  inline_map={}, ruby_count=0, text=text, glossary=gl)}

    assert "name_form_substitution" in types("クロガネのメンバーも。", "就連黑鬼的成員也是。")
    assert "name_form_substitution" not in types("クロガネのメンバーも。", "就連黑鐵的成員也是。")
    assert "offensive_term" in types("黒鬼じゃん！", "是黑鬼耶！")


def test_llm_name_links_need_verbatim_evidence(conn, cfg, fixtures):
    def memory(payload, req):
        out = RESPONDERS["memory_update"](payload, req)
        units = {u["unit_id"]: u["source"] for u in payload["units"]}
        uid = next((k for k, v in units.items() if "カイト、遅いですよ" in v), None)
        if uid:
            out["name_links"] = [
                {"term": "カイト", "term_zh": "海斗", "same_as": "ミナ", "form_type": "alias", "note": "",
                 "evidence_unit_ids": [uid], "evidence_quote": "カイトはミナの兄"},  # fabricated quote
                {"term": "リリア", "term_zh": "莉莉亞", "same_as": "ミナ", "form_type": "alias", "note": "",
                 "evidence_unit_ids": [uid], "evidence_quote": "カイト、遅いですよ"},  # term not in the scene
            ]
        return out

    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": memory}),
                            mode="context")
    reasons = [r[0] for r in conn.execute("SELECT reason FROM memory_rejections WHERE stage='memory_update'")]
    assert "name link evidence_quote not found verbatim" in reasons
    assert "name link terms must occur verbatim in the scene (or be known forms)" in reasons
    assert conn.execute("SELECT COUNT(*) FROM entity_links WHERE status='accepted'").fetchone()[0] == 0


def test_prompt_revision_does_not_invalidate_cache():
    row = {"status": "succeeded", "prompt_version": "translate/v1#aaaa", "model_id": "m", "mode": "baseline",
           "glossary_version": "g"}
    assert is_cached(row, prompt_version="translate/v1#bbbb", model="m", mode="baseline", gl_hash="g")  # type: ignore[arg-type]
    assert not is_cached(row, prompt_version="translate/v2#cccc", model="m", mode="baseline", gl_hash="g")  # type: ignore[arg-type]


def test_names_cli(tmp_path, fixtures):
    ws = tmp_path / "ws"
    epub = str(fixtures["basic"])
    r = CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake"]).exit_code == 0
    assert r.invoke(app, ["-w", str(ws), "glossary", "set", epub, "ミナ", "米娜", "--category", "character"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "names", "link", epub, "ミナっち", "--to", "ミナ", "--zh", "小米娜"])
    assert out.exit_code == 0, out.output
    data = json.loads(r.invoke(app, ["-w", str(ws), "names", "list", epub, "--json"]).output)
    forms = {f["ja"]: (f["form"], f["zh"]) for e in data["entities"] for f in e["forms"]}
    assert forms["ミナっち"] == ("nickname", "小米娜") and forms["ミナ"] == ("official", "米娜")
    assert r.invoke(app, ["-w", str(ws), "names", "unlink", epub, "ミナっち"]).exit_code == 0
    assert r.invoke(app, ["-w", str(ws), "names", "detect", epub]).exit_code == 0
    # a nickname two people share
    assert r.invoke(app, ["-w", str(ws), "glossary", "set", epub, "カイト", "海斗", "--category", "character"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "names", "link", epub, "ミナっち", "--to", "カイト", "--shared",
                         "--note", "both were called that"])
    assert out.exit_code == 0, out.output
    data = json.loads(r.invoke(app, ["-w", str(ws), "names", "list", epub, "--json", "--all"]).output)
    assert [x["term"] for e in data["entities"] for x in e["shared"]] == ["ミナっち"]


def test_backfill_groups_character_aliases(conn, cfg, fixtures):
    from ncte.memory.entities import backfill_entities
    from ncte.memory.repository import upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    upsert_character(conn, book.id, "ミナ", name_zh="米娜", aliases=["ミナちゃん"], unit_id="u", ordinal=1, origin="t")
    set_glossary(conn, book.id, "ミナ", "米娜", category="character")
    set_glossary(conn, book.id, "ミナちゃん", "小米娜", category="character")
    assert backfill_entities(conn, book.id) == 1
    fs = [f for f in entity_forms(conn, book.id).values() if len(f) == 2][0]
    assert {f["ja"]: f["form"] for f in fs} == {"ミナ": "official", "ミナちゃん": "alias"}


def test_linking_name_forms_merges_characters(conn, cfg, fixtures):
    """Real-book regression: a character and its full name stayed two characters after the forms were linked."""
    from ncte.memory.entities import reconcile_characters
    from ncte.memory.repository import add_speech_example, upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal", (book.id,)).fetchall()
    short, _ = upsert_character(conn, book.id, "ミナ", name_zh="米娜", aliases=[], unit_id=u[1][0], ordinal=u[1][1], origin="t")
    full, _ = upsert_character(conn, book.id, "星野ミナ", name_zh="星野米娜", aliases=[], unit_id=u[0][0],
                               ordinal=u[0][1], origin="t")
    conn.execute("INSERT INTO utterances(id, book_id, unit_id, quote_index, quote_start, quote_end, quote_text, "
                 "speaker_id_nullable, status, resolution_source, updated_at) VALUES ('q1',?,?,0,0,1,'「」',?, 'explicit',"
                 "'program','t')", (book.id, u[2][0], full))
    add_speech_example(conn, full, u[2][0], "q1", "「」", "explicit")
    set_glossary(conn, book.id, "ミナ", "米娜", category="character")
    set_glossary(conn, book.id, "星野ミナ", "星野米娜", category="character")
    link(conn, book.id, "星野ミナ", "ミナ", "full", origin="human")
    rows = conn.execute("SELECT * FROM characters").fetchall()
    assert [r["id"] for r in rows] == [short]  # one character per person …
    assert "星野ミナ" in json.loads(rows[0]["aliases_json"])  # … the full name became an alias
    assert rows[0]["known_from_ordinal"] == u[0][1]  # earliest appearance kept
    assert conn.execute("SELECT speaker_id_nullable FROM utterances WHERE id='q1'").fetchone()[0] == short
    assert conn.execute("SELECT character_id FROM speech_examples").fetchone()[0] == short
    assert reconcile_characters(conn, book.id) == 0  # idempotent


def test_ambiguous_form_does_not_fuse_two_people(conn, cfg, fixtures):
    """Real book: 「しまちゃん」 was both sisters' nickname; an automatic link must not merge the two entities."""
    book, _ = import_book(conn, cfg, fixtures["basic"])
    for ja, zh in [("島村", "島村"), ("しまむら", "島村"), ("しまむら妹", "島村妹妹"), ("しょーさん", "小小姐"),
                   ("しまちゃん", "小島")]:
        ensure_glossary(conn, book.id, ja, zh, category="character", origin="memory_update", first_unit_id=None)
    assert link(conn, book.id, "しまむら", "島村", "nickname", origin="llm") == "accepted"
    assert link(conn, book.id, "しょーさん", "しまむら妹", "nickname", origin="llm") == "accepted"
    assert link(conn, book.id, "しまちゃん", "島村", "nickname", origin="llm") == "accepted"
    # the same form now claimed for the sister: two established entities → a shared form, not a merge
    assert link(conn, book.id, "しまちゃん", "しまむら妹", "nickname", origin="llm", ordinal=40) == "shared"
    groups = sorted(sorted(f["ja"] for f in fs) for fs in entity_forms(conn, book.id).values())
    assert sorted(["しまむら", "しまちゃん", "島村"]) in groups and sorted(["しまむら妹", "しょーさん"]) in groups
    # the translation is told who it may refer to – once that use has been revealed
    gl = glossary_entries(conn, book.id)
    early = glossary_payload(gl, "しまちゃん", upto=(1.0, 10))
    late = glossary_payload(gl, "しまちゃん", upto=(1.0, 50))
    assert "may_refer_to" not in early[0]
    assert late[0]["may_refer_to"] == [{"ja": "島村", "zh": "島村"}, {"ja": "しまむら妹", "zh": "島村妹妹"}]
    # the form is ambiguous for address-term attribution
    from ncte.memory.address import name_forms

    fmap, ambiguous = name_forms(conn, book.id)
    assert "しまちゃん" in ambiguous and "しまちゃん" not in fmap
    # a human may still merge; the sense then points into the form's own entity and disappears
    assert link(conn, book.id, "しまむら妹", "島村", "alias", origin="human") == "accepted"
    assert conn.execute("SELECT count(*) FROM form_senses").fetchone()[0] == 0


def test_entity_label_follows_official_form_and_unlink_drops_alias(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["basic"])
    ensure_glossary(conn, book.id, "しまむら（妹）", "島村（妹妹）", category="term", origin="memory_update",
                    first_unit_id=None)
    ensure_glossary(conn, book.id, "島村", "島村", category="character", origin="memory_update", first_unit_id=None)
    link(conn, book.id, "島村", "しまむら（妹）", "official", origin="llm")  # wrong direction, as in the real book
    eid = conn.execute("SELECT entity_id FROM glossary WHERE source_term='島村'").fetchone()[0]
    assert tuple(conn.execute("SELECT label_ja, kind FROM entities WHERE id=?", (eid,)).fetchone()) == ("島村", "character")
    conn.execute("INSERT INTO characters(id, book_id, canonical_name, display_name_zh, aliases_json, "
                 "known_from_ordinal, origin, created_at, updated_at) VALUES ('c_s', ?, '島村', '島村', ?, 0, 'llm', '', '')",
                 (book.id, json.dumps(["しまむら（妹）"], ensure_ascii=False)))
    conn.execute("INSERT INTO characters(id, book_id, canonical_name, display_name_zh, aliases_json, "
                 "known_from_ordinal, origin, created_at, updated_at) VALUES ('c_m', ?, '妹', '妹妹', ?, 0, 'llm', '', '')",
                 (book.id, json.dumps(["しまむら（妹）"], ensure_ascii=False)))
    from ncte.memory.entities import unlink

    unlink(conn, book.id, "しまむら（妹）")
    assert json.loads(conn.execute("SELECT aliases_json FROM characters WHERE id='c_s'").fetchone()[0]) == []
    # the sister's own character (not part of 島村's entity) keeps the alias
    assert json.loads(conn.execute("SELECT aliases_json FROM characters WHERE id='c_m'").fetchone()[0]) == ["しまむら（妹）"]
    assert conn.execute("SELECT entity_id FROM glossary WHERE source_term='しまむら（妹）'").fetchone()[0] != eid


@pytest.mark.parametrize("by_model", [True, False])
def test_revealed_name_merges_the_described_character(conn, cfg, fixtures, by_model):
    from ncte.memory import visibility as vis

    from ..scripted import known, scene_analyze, uid, utt

    def analyze(payload, req):
        out = scene_analyze(payload, req)
        if uid(payload, "フードをかぶった少女が立っていた"):
            out["new_characters"].append({"temp_id": "new_7", "name_ja": "フードをかぶった少女", "aliases_ja": [],
                                          "proposed_name_zh": "戴兜帽的少女",
                                          "evidence_unit_ids": [uid(payload, "フードをかぶった少女が立っていた")]})
            u = utt(payload, "その鍵をどこで手に入れたの", "new_7", "inferred", ("action",), "フードをかぶった少女が立っていた")
            out["utterances"] = [x for x in [*out["utterances"], u] if x]
        hood = known(payload, "フードをかぶった少女")
        for nc in out["new_characters"]:
            if nc["name_ja"] == "リリア" and hood:
                if by_model:
                    nc["same_as_ref"] = hood
                else:  # the model only lists the description as an alias: the program links them
                    nc["aliases_ja"] = ["王女", "フードをかぶった少女"]
        return out

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "scene_analyze": analyze}),
                          mode="context")
    if not by_model:  # the description without a glossary entry must still be shown before the reveal
        conn.execute("DELETE FROM glossary WHERE source_term='フードをかぶった少女'")
    rows = conn.execute("SELECT * FROM characters WHERE canonical_name IN ('リリア', 'フードをかぶった少女')").fetchall()
    assert [r["canonical_name"] for r in rows] == ["リリア"]
    lilia = rows[0]
    assert "フードをかぶった少女" in json.loads(lilia["aliases_json"])
    line = conn.execute("SELECT u.ordinal, ut.speaker_id_nullable FROM utterances ut JOIN units u ON u.id=ut.unit_id "
                        "WHERE ut.quote_text LIKE '%その鍵をどこで%'").fetchone()
    assert line[1] == lilia["id"]
    # before the reveal the character is still only 「フードをかぶった少女」 (no spoiler)
    assert vis.character_view(conn, book.id, lilia, line[0])["name_ja"] == "フードをかぶった少女"
    reveal = conn.execute("SELECT ordinal FROM units WHERE plain_text LIKE '%王女リリアだった%'").fetchone()[0]
    assert vis.character_view(conn, book.id, lilia, reveal)["name_ja"] == "リリア"


def test_merge_carries_gender_and_address_terms(conn, cfg, fixtures):
    from ncte.memory.address import upsert
    from ncte.memory.repository import gender_at, merge_characters, set_gender, upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal", (book.id,)).fetchall()
    mk = lambda n: upsert_character(conn, book.id, n, name_zh=n, aliases=[], unit_id=us[0][0], ordinal=us[0][1],  # noqa: E731
                                    origin="t")[0]
    desc, named, other = mk("黒髪の女性"), mk("アルマ"), mk("レイド")
    set_gender(conn, book.id, desc, "female", ordinal=us[0][1], quote="女性", origin="llm")
    for who in (desc, named):
        upsert(conn, book.id, other, who, "お前", ordinal=us[1][1], unit_id=us[1][0], origin="llm", certainty="inferred")
    merge_characters(conn, named, desc)
    assert gender_at(conn, named, (99.0, 10**9)) == "female"
    rows = conn.execute("SELECT addressee_id, uses FROM address_terms WHERE form_ja='お前'").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(named, 2)]


def test_generic_alias_does_not_merge():
    from ncte.analysis.scene import _revealed

    known = [{"id": "c1", "book_id": "b", "canonical_name": "少女", "known_from_ordinal": 10, "aliases_json": "[]"}]
    assert _revealed(None, "b", known, None, ["少女"], 20) is None
    known[0]["canonical_name"] = "黒髪の女性"
    assert _revealed(None, "b", known, None, ["黒髪の女性"], 20) == "c1"
    assert _revealed(None, "b", known, None, ["黒髪の女性"], 2000) is None  # introduced long before: may be someone else


def test_aliases_the_model_calls_common_nouns_are_not_kept(conn, cfg, fixtures):
    from ..scripted import scene_analyze

    def analyze(payload, req):
        out = scene_analyze(payload, req)
        for nc in out["new_characters"]:
            if nc["name_ja"] == "カイト":
                nc["aliases_ja"] = ["市場", "寝坊"]
                nc["alias_kinds"] = [{"ja": "市場", "kind": "description"}, {"ja": "寝坊", "kind": "common"}]
        return out

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "scene_analyze": analyze}), mode="context")
    al = json.loads(conn.execute("SELECT aliases_json FROM characters WHERE canonical_name='カイト'").fetchone()[0])
    assert "寝坊" not in al and "市場" in al  # not in the word list: the model's label decides
