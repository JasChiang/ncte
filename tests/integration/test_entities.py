"""ADR-019: entities with several name forms (official / nickname / alias …)."""

import json

from typer.testing import CliRunner

from ncte.cli import app
from ncte.epub.units import InlineSpec  # noqa: F401
from ncte.library import import_book
from ncte.llm.fake import FakeLLM
from ncte.memory.entities import entity_forms, link
from ncte.memory.repository import ensure_glossary, set_glossary
from ncte.qa.rules import check_translation
from ncte.translation.cache import glossary_entries, is_cached
from ncte.translation.context_builder import glossary_payload

from ..helpers import run_book
from ..scripted import RESPONDERS


def book_with_terms(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["basic"])
    ensure_glossary(conn, book.id, "ハガネ", "黑鐵", category="organization", origin="memory_update",
                    first_unit_id=None)
    ensure_glossary(conn, book.id, "黒鬼", "黑鬼", category="organization", origin="memory_update", first_unit_id=None)
    return book


def test_translation_payload_shows_forms_only_after_reveal(conn, cfg, fixtures):
    book = book_with_terms(conn, cfg, fixtures)
    link(conn, book.id, "黒鬼", "ハガネ", "nickname", origin="llm", ordinal=100,
         note="the fans' nickname")
    gl = glossary_entries(conn, book.id)
    before = glossary_payload(gl, "ハガネが来た", upto=(1.0, 50), revealed={"黒鬼": (1.0, 100)})
    after = glossary_payload(gl, "黒鬼が来た", upto=(1.0, 150), revealed={"黒鬼": (1.0, 100)})
    assert "same_entity_as" not in before[0]  # the nickname is not revealed yet
    assert after[0]["form"] == "nickname" and after[0]["note"] == "the fans' nickname"
    assert after[0]["same_entity_as"] == [{"ja": "ハガネ", "zh": "黑鐵", "form": "official"}]
    assert "may_refer_to" not in after[0]


def test_qa_form_substitution_and_offensive(conn, cfg, fixtures):
    book = book_with_terms(conn, cfg, fixtures)
    link(conn, book.id, "黒鬼", "ハガネ", "nickname", origin="human")
    gl = glossary_entries(conn, book.id)

    def types(src, text):
        return {f.type for f in check_translation(unit_id="u", kind="text", source_text=src, plain_text=src,
                                                  inline_map={}, ruby_count=0, text=text, glossary=gl)}

    assert "name_form_substitution" in types("ハガネのメンバーも。", "就連黑鬼的成員也是。")
    assert "name_form_substitution" not in types("ハガネのメンバーも。", "就連黑鐵的成員也是。")
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
    for ja, zh in [("高木", "高木"), ("たかぎ", "高木"), ("たかぎ妹", "高木妹妹"), ("たかさん", "小小姐"),
                   ("しまちゃん", "小島")]:
        ensure_glossary(conn, book.id, ja, zh, category="character", origin="memory_update", first_unit_id=None)
    assert link(conn, book.id, "たかぎ", "高木", "nickname", origin="llm") == "accepted"
    assert link(conn, book.id, "たかさん", "たかぎ妹", "nickname", origin="llm") == "accepted"
    assert link(conn, book.id, "しまちゃん", "高木", "nickname", origin="llm") == "accepted"
    # the same form now claimed for the sister: two established entities → a shared form, not a merge
    assert link(conn, book.id, "しまちゃん", "たかぎ妹", "nickname", origin="llm", ordinal=40) == "shared"
    groups = sorted(sorted(f["ja"] for f in fs) for fs in entity_forms(conn, book.id).values())
    assert sorted(["たかぎ", "しまちゃん", "高木"]) in groups and sorted(["たかぎ妹", "たかさん"]) in groups
    # the translation is told who it may refer to – once that use has been revealed
    gl = glossary_entries(conn, book.id)
    early = glossary_payload(gl, "しまちゃん", upto=(1.0, 10))
    late = glossary_payload(gl, "しまちゃん", upto=(1.0, 50))
    assert "may_refer_to" not in early[0]
    assert late[0]["may_refer_to"] == [{"ja": "高木", "zh": "高木"}, {"ja": "たかぎ妹", "zh": "高木妹妹"}]
    # the form is ambiguous for address-term attribution
    from ncte.memory.address import name_forms

    fmap, ambiguous = name_forms(conn, book.id)
    assert "しまちゃん" in ambiguous and "しまちゃん" not in fmap
    # a human may still merge; the sense then points into the form's own entity and disappears
    assert link(conn, book.id, "たかぎ妹", "高木", "alias", origin="human") == "accepted"
    assert conn.execute("SELECT count(*) FROM form_senses").fetchone()[0] == 0


def test_entity_label_follows_official_form_and_unlink_drops_alias(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["basic"])
    ensure_glossary(conn, book.id, "たかぎ（妹）", "高木（妹妹）", category="term", origin="memory_update",
                    first_unit_id=None)
    ensure_glossary(conn, book.id, "高木", "高木", category="character", origin="memory_update", first_unit_id=None)
    link(conn, book.id, "高木", "たかぎ（妹）", "official", origin="llm")  # wrong direction, as in the real book
    eid = conn.execute("SELECT entity_id FROM glossary WHERE source_term='高木'").fetchone()[0]
    assert tuple(conn.execute("SELECT label_ja, kind FROM entities WHERE id=?", (eid,)).fetchone()) == ("高木", "character")
    conn.execute("INSERT INTO characters(id, book_id, canonical_name, display_name_zh, aliases_json, "
                 "known_from_ordinal, origin, created_at, updated_at) VALUES ('c_s', ?, '高木', '高木', ?, 0, 'llm', '', '')",
                 (book.id, json.dumps(["たかぎ（妹）"], ensure_ascii=False)))
    conn.execute("INSERT INTO characters(id, book_id, canonical_name, display_name_zh, aliases_json, "
                 "known_from_ordinal, origin, created_at, updated_at) VALUES ('c_m', ?, '妹', '妹妹', ?, 0, 'llm', '', '')",
                 (book.id, json.dumps(["たかぎ（妹）"], ensure_ascii=False)))
    from ncte.memory.entities import unlink

    unlink(conn, book.id, "たかぎ（妹）")
    assert json.loads(conn.execute("SELECT aliases_json FROM characters WHERE id='c_s'").fetchone()[0]) == []
    # the sister's own character (not part of 高木's entity) keeps the alias
    assert json.loads(conn.execute("SELECT aliases_json FROM characters WHERE id='c_m'").fetchone()[0]) == ["たかぎ（妹）"]
    assert conn.execute("SELECT entity_id FROM glossary WHERE source_term='たかぎ（妹）'").fetchone()[0] != eid


def test_revealed_name_merges_the_described_character(conn, cfg, fixtures):
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
                nc["same_as_ref"] = hood  # the model says who she is (the program does not match descriptions)
        return out

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "scene_analyze": analyze}),
                          mode="context")
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


def test_an_unnamed_person_is_known_by_the_books_words_and_revealed_later(conn, cfg, fixtures):
    """ADR-101: a person with no name, given a label of the model's own (「謎の少女」, not in the book) and the book's
    words among the aliases, is kept under those words — with their lines; a later scene that gives the words again
    without them in its evidence means that card; the name revealed later is linked to it and shown only from there
    (ADR-032). A person none of whose words is in the book is still rejected."""
    from ncte.memory import visibility as vis

    from ..scripted import known, scene_analyze, uid, utt

    def analyze(payload, req):
        out = scene_analyze(payload, req)
        if uid(payload, "フードをかぶった少女が立っていた"):
            out["new_characters"] += [
                {"temp_id": "new_7", "name_ja": "謎の少女", "aliases_ja": ["フードをかぶった少女", "少女"],
                 "alias_kinds": [{"ja": "フードをかぶった少女", "kind": "description"}, {"ja": "少女", "kind": "common"}],
                 "proposed_name_zh": "神祕少女", "evidence_unit_ids": [uid(payload, "フードをかぶった少女が立っていた")]},
                {"temp_id": "new_8", "name_ja": "謎の男", "aliases_ja": ["黒衣の男"], "proposed_name_zh": "神祕男子",
                 "evidence_unit_ids": [uid(payload, "フードをかぶった少女が立っていた")]}]
            u = utt(payload, "その鍵をどこで手に入れたの", "new_7", "inferred", ("action",), "フードをかぶった少女が立っていた")
            out["utterances"] = [x for x in [*out["utterances"], u] if x]
        if uid(payload, "そろそろ、帰りましょうか"):  # the next chapter: the words are in the scene before, not here
            out["new_characters"].append({"temp_id": "new_1", "name_ja": "フードをかぶった少女", "aliases_ja": [],
                                          "proposed_name_zh": "", "evidence_unit_ids": [uid(payload, "待って")]})
            out["utterances"] = [x for x in [*out["utterances"], utt(payload, "待って", "new_1", "inferred", ("rhythm",))]
                                 if x]
        hood = known(payload, "フードをかぶった少女")
        for nc in out["new_characters"]:
            if nc["name_ja"] == "リリア" and hood:
                nc["same_as_ref"] = hood
        return out

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "scene_analyze": analyze}),
                          mode="context")
    names = [r[0] for r in conn.execute("SELECT canonical_name FROM characters WHERE book_id=?", (book.id,))]
    assert "謎の少女" not in names and "フードをかぶった少女" not in names and "リリア" in names
    assert "謎の男" not in names and "黒衣の男" not in names  # no words of the book: the hallucination guard
    lilia = conn.execute("SELECT * FROM characters WHERE canonical_name='リリア'").fetchone()
    assert json.loads(lilia["aliases_json"]).count("フードをかぶった少女") == 1
    assert "謎の少女" not in json.loads(lilia["aliases_json"]) and "少女" not in json.loads(lilia["aliases_json"])
    assert conn.execute("SELECT 1 FROM glossary WHERE source_term IN ('謎の少女', 'フードをかぶった少女') OR "
                        "target_term='神祕少女'").fetchone() is None  # the label's rendering is not the words'
    lines = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT ut.quote_text, u.ordinal, ut.speaker_id_nullable FROM utterances ut JOIN units u ON u.id=ut.unit_id")}
    asked = next(v for k, v in lines.items() if "その鍵をどこで" in k)
    wait = next(v for k, v in lines.items() if k.startswith("「待って"))
    assert asked[1] == wait[1] == lilia["id"]
    # before the reveal she is only the words the book used for her (no spoiler); from the reveal, リリア
    assert vis.character_view(conn, book.id, lilia, wait[0])["name_ja"] == "フードをかぶった少女"
    reveal = conn.execute("SELECT ordinal FROM units WHERE plain_text LIKE '%王女リリアだった%'").fetchone()[0]
    assert vis.character_view(conn, book.id, lilia, reveal)["name_ja"] == "リリア"
    reasons = [json.loads(r[0]).get("name_ja") for r in conn.execute(
        "SELECT item_json FROM memory_rejections WHERE reason LIKE 'name_ja not found verbatim%'")]
    assert "謎の男" in reasons and "謎の少女" not in reasons and "フードをかぶった少女" not in reasons


def test_merge_carries_gender_and_address_terms(conn, cfg, fixtures):
    from ncte.memory.address import upsert
    from ncte.memory.repository import gender_at, merge_characters, set_gender, upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal", (book.id,)).fetchall()
    mk = lambda n: upsert_character(conn, book.id, n, name_zh=n, aliases=[], unit_id=us[0][0], ordinal=us[0][1],  # noqa: E731
                                    origin="t")[0]
    desc, named, other = mk("黒髪の女性"), mk("ソフィ"), mk("カイル")
    set_gender(conn, book.id, desc, "female", ordinal=us[0][1], quote="女性", origin="llm")
    for who in (desc, named):
        upsert(conn, book.id, other, who, "お前", ordinal=us[1][1], unit_id=us[1][0], origin="llm", certainty="inferred")
    merge_characters(conn, named, desc)
    assert gender_at(conn, named, (99.0, 10**9)) == "female"
    rows = conn.execute("SELECT addressee_id, uses FROM address_terms WHERE form_ja='お前'").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(named, 2)]


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


def test_merge_leaves_no_reference_to_the_dropped_card(conn, cfg, fixtures):
    """Every column that names a character follows the merge: whose belief a fact is, who a line is said to, who
    calls whom what (the usage judged by the second look) — none points at the deleted card."""
    from ncte.memory.db import utcnow
    from ncte.memory.repository import merge_characters, upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal", (book.id,)).fetchall()
    mk = lambda n: upsert_character(conn, book.id, n, name_zh=n, aliases=[], unit_id=us[0][0], ordinal=us[0][1],  # noqa: E731
                                    origin="t")[0]
    desc, named, other = mk("黒髪の女性"), mk("ソフィ"), mk("カイル")
    conn.execute("INSERT INTO facts(id, book_id, subject, predicate, object, fact_type, certainty, fact_json, "
                 "evidence_unit_ids_json, evidence_quote, first_revealed_unit_id, first_revealed_ordinal, source_viewpoint, "
                 "created_at) VALUES ('f1',?,?,?,?,?,?,'{}',?,?,?,?,?,?)", (book.id, other, "は", "王", "character_belief",
                                                                          "inferred", "[]", "",
                                                    us[0][0], us[0][1], desc, utcnow()))
    conn.execute("INSERT INTO utterances(id, book_id, unit_id, quote_index, quote_start, quote_end, quote_text, status, "
                 "resolution_source, updated_at, addressee_ids_json) VALUES ('q1',?,?,0,0,1,'「x」','unknown','none',?,?)",
                 (book.id, us[0][0], utcnow(), json.dumps([desc, other])))
    conn.execute("INSERT INTO address_usage(book_id, speaker_id, addressee_id, form_ja, usage, origin, created_at) VALUES "
                 "(?,?,?,?,?,?,?)", (book.id, other, desc, "お前", "address", "llm", utcnow()))
    merge_characters(conn, named, desc)
    assert conn.execute("SELECT source_viewpoint FROM facts WHERE id='f1'").fetchone()[0] == named
    assert json.loads(conn.execute("SELECT addressee_ids_json FROM utterances WHERE id='q1'").fetchone()[0]) == [named, other]
    assert conn.execute("SELECT addressee_id FROM address_usage WHERE speaker_id=?", (other,)).fetchone()[0] == named
