"""ADR-019: entities with several name forms (official / nickname / alias …)."""

import json

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
