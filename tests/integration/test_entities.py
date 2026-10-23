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


def test_words_of_an_earlier_card_mean_it_only_when_the_model_says_nothing_otherwise(conn, cfg, fixtures):
    """ADR-101: a new character whose words are not in its evidence but are a card's seen in an earlier scene is that
    card — the program's shortcut, on the record (``matched_by_words``). The model's own word on who this is wins: a
    past life's 「母親」 (``other_person``, ``period``) or a group is not merged into the card; and words two cards have
    are not for the program to choose between (handled as any words not in the evidence: rejected here)."""
    from ncte.analysis.scene import apply_scene_analysis
    from ncte.llm.schemas import SceneAnalysisOutput
    from ncte.memory import repository as repo

    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (book.id, scenes[0]["id"])).fetchone()
    later = scenes[-1]
    ev = conn.execute("SELECT id FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                      (book.id, later["id"])).fetchone()[0]
    mother, _ = repo.upsert_character(conn, book.id, "母親", name_zh=None, aliases=["義母上"], unit_id=first["id"],
                                      ordinal=first["ordinal"], origin="test")
    for name in ("男甲", "男乙"):
        repo.upsert_character(conn, book.id, name, name_zh=None, aliases=["黒衣の男"], unit_id=first["id"],
                              ordinal=first["ordinal"], origin="test")

    def read(**nc: object) -> tuple[dict[str, str], dict[str, object]]:
        out = {"pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [],
               "props": [], "uncertainties": [],
               "new_characters": [{"temp_id": "new_1", "aliases_ja": [], "proposed_name_zh": "",
                                   "evidence_unit_ids": [ev], **nc}]}
        refs: dict[str, str] = {}
        sc = conn.execute("SELECT * FROM scenes WHERE id=?", (later["id"],)).fetchone()
        stats = apply_scene_analysis(conn, book.id, sc, SceneAnalysisOutput.model_validate(out), refs=refs)
        state = json.loads(conn.execute("SELECT state_json FROM scenes WHERE id=?", (later["id"],)).fetchone()[0])
        return refs, {**stats, "matched": state.get("matched_by_words", [])}

    refs, got = read(name_ja="母親")
    assert refs["new_1"] == mother and got["matched_by_words"] == 1
    assert got["matched"] == [{"ref": "new_1", "name_ja": "母親", "character_id": mother}]
    refs, got = read(name_ja="義母上")
    assert refs["new_1"] == mother
    for other in ({"other_person": True}, {"period": "前世"}, {"other_person": True, "period": "前世"}, {"group": True}):
        refs, got = read(name_ja="母親", **other)
        assert refs.get("new_1") != mother and not got["matched"] and got["rejected"] == 1, other
    refs, got = read(name_ja="黒衣の男")
    assert "new_1" not in refs and not got["matched"] and got["rejected"] == 1
    reasons = [r[0] for r in conn.execute("SELECT reason FROM memory_rejections WHERE book_id=? ORDER BY id", (book.id,))]
    assert reasons[-1].endswith("the words name several known characters")
    assert reasons[0].endswith("the words name a known character the model says this is not")


def test_a_known_card_reached_through_an_alias_follows_the_models_word_on_who_it_is(conn, cfg, fixtures):
    """The book's words among the aliases (ADR-101) may be an earlier card's too: a past life's 「母親」 known in the
    scene by 「来訪者」 — the present mother's other words — is not merged into her; it gets a card of its own with its
    time (as a past one named in its evidence does), and one the model reads as a group where the card is a person is
    not taken for that card."""
    from ncte.analysis.scene import apply_scene_analysis
    from ncte.llm.schemas import SceneAnalysisOutput
    from ncte.memory import repository as repo

    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (book.id, scenes[0]["id"])).fetchone()
    later = scenes[-1]
    ev = conn.execute("SELECT id FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%来訪者%'",
                      (book.id, later["id"])).fetchone()[0]
    mother, _ = repo.upsert_character(conn, book.id, "母親", name_zh=None, aliases=["来訪者"], unit_id=first["id"],
                                      ordinal=first["ordinal"], origin="test")

    def read(**nc: object) -> dict[str, str]:
        out = {"pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [],
               "props": [], "uncertainties": [],
               "new_characters": [{"temp_id": "new_1", "name_ja": "母親", "aliases_ja": ["来訪者"],
                                   "proposed_name_zh": "", "evidence_unit_ids": [ev], **nc}]}
        refs: dict[str, str] = {}
        sc = conn.execute("SELECT * FROM scenes WHERE id=?", (later["id"],)).fetchone()
        apply_scene_analysis(conn, book.id, sc, SceneAnalysisOutput.model_validate(out), refs=refs)
        return refs

    assert read()["new_1"] == mother  # nothing said otherwise: the card with those words
    for other in ({"period": "前世"}, {"other_person": True}):
        got = read(**other)["new_1"]
        assert got != mother, other
        assert conn.execute("SELECT canonical_name FROM characters WHERE id=?", (got,)).fetchone()[0].startswith("来訪者（")
    assert read(group=True).get("new_1") != mother


def test_words_two_cards_have_are_not_for_the_program_to_pick_between(conn, cfg, fixtures):
    """Two earlier cards both known as 「来訪者」: a new person known in the scene by those words (standing in for a
    name the model gave that is not in the evidence, or as the name itself) is not put on either card — rejected, with
    the reason; one the model says is someone else (another time) still gets a card of its own."""
    from ncte.analysis.scene import apply_scene_analysis
    from ncte.llm.schemas import SceneAnalysisOutput
    from ncte.memory import repository as repo

    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (book.id, scenes[0]["id"])).fetchone()
    later = scenes[-1]
    ev = conn.execute("SELECT id FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%来訪者%'",
                      (book.id, later["id"])).fetchone()[0]
    mothers = {repo.upsert_character(conn, book.id, n, name_zh=None, aliases=["来訪者"], unit_id=first["id"],
                                     ordinal=first["ordinal"], origin="test")[0] for n in ("甲の母", "乙の母")}

    def read(**nc: object) -> dict[str, str]:
        out = {"pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [],
               "props": [], "uncertainties": [],
               "new_characters": [{"temp_id": "new_1", "aliases_ja": ["来訪者"], "proposed_name_zh": "",
                                   "evidence_unit_ids": [ev], **nc}]}
        refs: dict[str, str] = {}
        sc = conn.execute("SELECT * FROM scenes WHERE id=?", (later["id"],)).fetchone()
        apply_scene_analysis(conn, book.id, sc, SceneAnalysisOutput.model_validate(out), refs=refs)
        return refs

    for name in ("母親", "来訪者"):
        assert "new_1" not in read(name_ja=name), name
    reasons = [r[0] for r in conn.execute("SELECT reason FROM memory_rejections WHERE book_id=? ORDER BY id",
                                          (book.id,))]
    assert reasons[0].startswith("the words name several known characters (an alias")
    assert reasons[1] == "the words name several known characters"
    assert read(name_ja="母親", period="前世")["new_1"] not in mothers


def test_only_cards_seen_before_the_scene_decide_whose_words_these_are(conn, cfg, fixtures):
    """A card first known later in the book (a re-read) that also has 「来訪者」 makes the words neither ambiguous nor
    its own here: the card seen before the scene is the one. A time given alone (「前世」) is someone else, as in the
    other cases: a card of its own, even where the words are two cards'."""
    import sqlite3

    from ncte.analysis.scene import apply_scene_analysis
    from ncte.llm.schemas import SceneAnalysisOutput
    from ncte.memory import repository as repo

    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (book.id, scenes[0]["id"])).fetchone()
    later = scenes[-1]
    last = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal DESC LIMIT 1",
                        (book.id, later["id"])).fetchone()
    ev = conn.execute("SELECT id FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%来訪者%'",
                      (book.id, later["id"])).fetchone()[0]

    def card(name: str, at: sqlite3.Row) -> str:
        return repo.upsert_character(conn, book.id, name, name_zh=None, aliases=["来訪者"], unit_id=at["id"],
                                     ordinal=at["ordinal"], origin="test")[0]

    def read(**nc: object) -> dict[str, str]:
        out = {"pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [],
               "props": [], "uncertainties": [],
               "new_characters": [{"temp_id": "new_1", "aliases_ja": ["来訪者"], "proposed_name_zh": "",
                                   "evidence_unit_ids": [ev], **nc}]}
        refs: dict[str, str] = {}
        sc = conn.execute("SELECT * FROM scenes WHERE id=?", (later["id"],)).fetchone()
        apply_scene_analysis(conn, book.id, sc, SceneAnalysisOutput.model_validate(out), refs=refs)
        return refs

    seen, ahead = card("甲の母", first), card("乙の母", last)
    assert last["ordinal"] > later["start_ordinal"]
    assert read(name_ja="母親")["new_1"] == seen
    assert read(name_ja="来訪者")["new_1"] == seen
    assert read(name_ja="来訪者", period="前世")["new_1"] not in (seen, ahead)
    other = card("丙の母", first)  # two cards seen before the scene: a time alone still names someone else
    assert read(name_ja="来訪者", period="前世")["new_1"] not in (seen, ahead, other)


def test_words_only_cards_known_later_have_are_one_of_them_only_when_one_has_them(conn, cfg, fixtures):
    """No card seen before the scene has 「来訪者」: one card first known later with it is that person found earlier (a
    re-read); two are not for the program to pick between — rejected, as for two cards seen before."""
    import sqlite3

    from ncte.analysis.scene import apply_scene_analysis
    from ncte.llm.schemas import SceneAnalysisOutput
    from ncte.memory import repository as repo

    book, _ = import_book(conn, cfg, fixtures["basic"])
    later = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq DESC LIMIT 1", (book.id,)).fetchone()
    tail = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal DESC LIMIT 2",
                        (book.id, later["id"])).fetchall()
    ev = conn.execute("SELECT id FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%来訪者%'",
                      (book.id, later["id"])).fetchone()[0]

    def card(name: str, at: sqlite3.Row) -> str:
        return repo.upsert_character(conn, book.id, name, name_zh=None, aliases=["来訪者"], unit_id=at["id"],
                                     ordinal=at["ordinal"], origin="test")[0]

    def read() -> dict[str, str]:
        out = {"pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [],
               "props": [], "uncertainties": [],
               "new_characters": [{"temp_id": "new_1", "name_ja": "母親", "aliases_ja": ["来訪者"],
                                   "proposed_name_zh": "", "evidence_unit_ids": [ev]}]}
        refs: dict[str, str] = {}
        sc = conn.execute("SELECT * FROM scenes WHERE id=?", (later["id"],)).fetchone()
        apply_scene_analysis(conn, book.id, sc, SceneAnalysisOutput.model_validate(out), refs=refs)
        return refs

    one = card("乙の母", tail[0])
    assert all(t["ordinal"] > later["start_ordinal"] for t in tail)
    assert read()["new_1"] == one
    conn.execute("UPDATE characters SET known_from_unit_id=?, known_from_ordinal=? WHERE id=?",
                 (tail[0]["id"], tail[0]["ordinal"], one))  # known later again, as before the read
    card("丙の母", tail[1])
    assert "new_1" not in read()
    assert conn.execute("SELECT reason FROM memory_rejections WHERE book_id=? ORDER BY id DESC LIMIT 1",
                        (book.id,)).fetchone()[0].startswith("the words name several known characters")
