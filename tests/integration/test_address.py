"""ADR-022: address terms – who calls whom what, with the rendering to keep."""

import json

from ncte.llm.fake import FakeLLM
from ncte.memory.address import find_forms

from ..helpers import run_book
from ..scripted import RESPONDERS, known, memory_update, uid


def rows(conn):
    names = {r[0]: r[1] for r in conn.execute("SELECT id, canonical_name FROM characters")}
    return {(names[r["speaker_id"]], names[r["addressee_id"]], r["form_ja"]): dict(r)
            for r in conn.execute("SELECT * FROM address_terms")}


def test_generic_role_with_honorific_is_not_a_name(conn, cfg, fixtures):
    from ncte.library import import_book
    from ncte.memory.address import name_forms
    from ncte.memory.repository import upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    upsert_character(conn, book.id, "店員さん", name_zh="店員", aliases=[], unit_id="u", ordinal=1, origin="t")
    upsert_character(conn, book.id, "ミナ", name_zh="米娜", aliases=["ミナさん"], unit_id="u", ordinal=1, origin="t")
    fmap, _ = name_forms(conn, book.id)
    assert "店員さん" not in fmap and {"ミナ", "ミナさん"} <= set(fmap)


def test_find_forms_longest_match_and_honorifics():
    forms = {"高木": "c1", "たかぎ": "c1", "たかぎちゃん": "c1", "水野": "c2"}
    assert find_forms("「高木さん、水野は？」", forms) == [("高木さん", "c1"), ("水野", "c2")]
    assert find_forms("たかぎちゃん", forms) == [("たかぎちゃん", "c1")]
    assert find_forms("高《たか》木《ぎ》くん", forms) == [("高木くん", "c1")]  # ruby readings ignored
    assert set(find_forms("「リナ嬢、セレナ」", {"リナ": "c3", "セレナ": "c4"})) == {("リナ嬢", "c3"), ("セレナ", "c4")}


def test_a_form_the_model_reads_is_linked_to_its_line(conn, cfg, fixtures):
    """ADR-059: the memory model reports 「カイト」 in ミナ's line, with the line as evidence; the program links it to that
    line (a stated speaker: explicit) and counts the line once, however often the scene's memory is read."""
    from ncte.analysis.memory_update import apply_memory_update
    from ncte.llm.schemas import MemoryUpdateOutput

    book, _, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    assert s.status == "completed"
    r = rows(conn)[("ミナ", "カイト", "カイト")]
    assert (r["certainty"], r["uses"], r["origin"], r["form_zh"]) == ("explicit", 1, "llm", "海斗")
    line = conn.execute("SELECT id, scene_id FROM utterances WHERE quote_text LIKE '%遅いですよ%'").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM address_observations WHERE utterance_id=?", (line[0],)).fetchone()[0] == 1
    scene = conn.execute("SELECT * FROM scenes WHERE id=?", (line[1],)).fetchone()
    payload = {"units": [{"unit_id": u, "source": ""} for u in
                         [x[0] for x in conn.execute("SELECT id FROM units WHERE scene_id=?", (line[1],))]],
               "known_characters": [{"character_id": c[0], "name_ja": c[1]} for c in
                                    conn.execute("SELECT id, canonical_name FROM characters")]}
    again = MemoryUpdateOutput.model_validate(memory_update({**payload, "units": [
        {"unit_id": u, "source": t} for u, t in conn.execute("SELECT id, plain_text FROM units WHERE scene_id=?",
                                                             (line[1],))]}, None))
    apply_memory_update(conn, book.id, scene, again)
    assert rows(conn)[("ミナ", "カイト", "カイト")]["uses"] == 1


def test_translation_payload_carries_earlier_address_terms(conn, cfg, fixtures):
    fake = FakeLLM(responders=RESPONDERS)
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    payloads = [json.loads(c.messages[1]["content"]) for c in fake.calls if c.stage.startswith("translate")]
    first = next(p for p in payloads if any("遅いですよ" in u["source"] for u in p["units"]))
    assert "address_terms" not in first  # nothing was known before the first scene
    later = [p for p in payloads if p.get("address_terms")]
    # in order of first use, which the prompt says: no volume of first use (``translation.compact_payload``)
    assert later and later[0]["address_terms"][0] == {"speaker": "米娜", "to": "海斗", "ja": "カイト", "uses": 1,
                                                       "zh": "海斗", "note": "直呼名字"}


def test_llm_rendering_and_validation(conn, cfg, fixtures):
    def mem(payload, req):
        out = memory_update(payload, req)
        mina, kaito, u = known(payload, "ミナ"), known(payload, "カイト"), uid(payload, "カイト、遅いですよ")
        if mina and kaito and u:
            out["address_terms"] = [
                {"speaker_ref": mina, "addressee_ref": kaito, "term": "カイト", "term_zh": "阿海", "note": "直呼名字",
                 "evidence_unit_ids": [u], "evidence_quote": "カイト、遅いですよ"},
                {"speaker_ref": mina, "addressee_ref": kaito, "term": "カイトくん", "term_zh": "海斗君", "note": "",
                 "evidence_unit_ids": [u], "evidence_quote": "カイト、遅いですよ"},  # term not in the quote
                {"speaker_ref": "c_nobody", "addressee_ref": kaito, "term": "カイト", "term_zh": "x", "note": "",
                 "evidence_unit_ids": [u], "evidence_quote": "カイト、遅いですよ"}]
        return out

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": mem}), mode="context")
    r = rows(conn)
    assert r[("ミナ", "カイト", "カイト")]["form_zh"] == "阿海" and r[("ミナ", "カイト", "カイト")]["note"] == "直呼名字"
    assert ("ミナ", "カイト", "カイトくん") not in r
    assert conn.execute("SELECT count(*) FROM memory_rejections WHERE reason LIKE 'address term%'").fetchone()[0] == 2


def test_forms_follow_a_line_whose_speaker_changes(conn, cfg, fixtures):
    """A person sets the speaker of a line: the form recorded on that line under the old speaker goes with the new
    one. A speaker set to unknown keeps the forms (the memory model read them with the context); a change on another
    line of the scene touches nothing here. Only the data moves (the text is not searched again)."""
    from ncte.analysis.speaker import set_speaker_human

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    ids = {r[1]: r[0] for r in conn.execute("SELECT id, canonical_name FROM characters")}
    utt, sid = conn.execute("SELECT id, scene_id FROM utterances WHERE quote_text LIKE '%遅いですよ%'").fetchone()
    other = conn.execute("SELECT id FROM utterances WHERE scene_id=? AND id!=? LIMIT 1", (sid, utt)).fetchone()
    if other:
        set_speaker_human(conn, other[0], None)
    assert ("ミナ", "カイト", "カイト") in rows(conn)  # another line's change leaves this form alone
    set_speaker_human(conn, utt, ids["カイト"])
    assert ("カイト", "カイト", "カイト") in rows(conn) and ("ミナ", "カイト", "カイト") not in rows(conn)
    set_speaker_human(conn, utt, None)
    assert ("カイト", "カイト", "カイト") in rows(conn)  # unknown now: what was read is kept

def test_cli_address_list_and_set(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    import sqlite3

    db = sqlite3.connect(next(ws.glob("series/*/ncte.db")))
    book_id = db.execute("SELECT id FROM books").fetchone()[0]
    for cid, ja, zh in [("c_m", "ミナ", "米娜"), ("c_k", "カイト", "海斗")]:  # the default fake creates no characters
        db.execute("INSERT INTO characters(id, book_id, canonical_name, display_name_zh, known_from_ordinal, origin, "
                   "created_at, updated_at) VALUES (?,?,?,?,0,'test','','')", (cid, book_id, ja, zh))
    db.commit()
    db.close()
    out = r.invoke(app, ["-w", str(ws), "names", "address-set", epub, "--speaker", "ミナ", "--to", "カイト",
                         "--ja", "カイト", "--zh", "阿海"])
    assert out.exit_code == 0, out.output
    data = json.loads(r.invoke(app, ["-w", str(ws), "names", "address", epub, "--json"]).output)
    assert {(d["speaker"], d["ja"], d["zh"], d["zh_origin"]) for d in data} >= {("米娜", "カイト", "阿海", "human")}
    bad = r.invoke(app, ["-w", str(ws), "names", "address-set", epub, "--speaker", "誰", "--to", "カイト",
                         "--ja", "x", "--zh", "y"])
    assert bad.exit_code != 0 and "unknown character" in str(bad.exception)


def test_pronoun_address_kept_without_the_models_rendering(conn, cfg, fixtures):
    def mem(payload, req):
        out = memory_update(payload, req)
        mina, kaito, u = known(payload, "ミナ"), known(payload, "カイト"), uid(payload, "誰だ、お前")
        if mina and kaito and u:
            out["address_terms"] = [{"speaker_ref": kaito, "addressee_ref": mina, "term": "お前", "term_zh": "你",
                                     "note": "", "evidence_unit_ids": [u], "evidence_quote": "誰だ、お前"}]
        return out

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": mem}), mode="context")
    row = conn.execute("SELECT form_zh, origin FROM address_terms WHERE form_ja='お前'").fetchone()
    assert row is not None and row["form_zh"] is None  # 你/妳/您 is decided by naming, not copied



def test_decided_rendering_reaches_the_scene_of_first_use(conn, cfg, fixtures):
    from ncte.memory.address import context_items

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    r = conn.execute("SELECT a.*, u.ordinal, u.scene_id FROM address_terms a JOIN units u ON u.id=a.first_unit_id "
                     "WHERE form_ja='カイト'").fetchone()
    start = conn.execute("SELECT start_ordinal FROM scenes WHERE id=?", (r["scene_id"],)).fetchone()[0]
    names = {r["speaker_id"]: "米娜", r["addressee_id"]: "海斗"}
    text = "「カイト、遅いですよ」"
    assert context_items(conn, book.id, start, text, set(), names, upto=r["ordinal"]) == []  # not decided: no
    conn.execute("UPDATE address_terms SET form_zh='阿海', zh_origin='human' WHERE id=?", (r["id"],))
    items = context_items(conn, book.id, start, text, set(), names, upto=r["ordinal"])
    assert [(i["ja"], i["zh"]) for i in items] == [("カイト", "阿海")]
    assert context_items(conn, book.id, start, text, set(), names, upto=r["ordinal"] - 1) == []  # not before it


def test_the_model_says_who_a_form_is_for(conn, cfg, fixtures):
    # 「アダチさん」 said to アダチ's mother: the memory model read the scene and says who is meant (no program guess)
    from ncte.memory.repository import upsert_character

    def mem(payload, req):
        out = memory_update(payload, req)
        u, mina, mom = uid(payload, "カイト、遅いですよ"), known(payload, "ミナ"), known(payload, "カイトの母")
        if u and mina and mom:
            out["address_terms"] = [{"speaker_ref": mina, "addressee_ref": mom, "term": "カイト", "term_zh": "海斗媽媽",
                                     "note": "", "evidence_unit_ids": [u], "evidence_quote": "カイト、遅いですよ"}]
        return out

    from ncte.library import import_book

    book, _ = import_book(conn, cfg, fixtures["basic"])
    u0 = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()
    mom = upsert_character(conn, book.id, "カイトの母", name_zh="海斗的母親", aliases=["カイト"], unit_id=u0[0],
                           ordinal=u0[1], origin="t")[0]  # her family name is a form of address for her too
    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": mem}), mode="context")
    line = conn.execute("SELECT id FROM utterances WHERE quote_text LIKE '%カイト、遅いですよ%'").fetchone()[0]
    to = conn.execute("SELECT a.addressee_id FROM address_observations o JOIN address_terms a ON a.id=o.address_id "
                      "WHERE o.utterance_id=?", (line,)).fetchall()
    assert [r[0] for r in to] == [mom]


def test_nickname_with_honorific_builds_on_the_decided_spelling(conn, cfg, fixtures):
    from ncte.library import import_book
    from ncte.memory.address import decided_variants, upsert, variant_key
    from ncte.memory.repository import upsert_character

    assert variant_key("みずのっつぃーくん") == variant_key("みずのっついー") == variant_key("みずのっつぃー")
    assert variant_key("水野ちゃん") != variant_key("みずっちー")
    book, _ = import_book(conn, cfg, fixtures["basic"])
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()
    hino, adachi = (upsert_character(conn, book.id, n, name_zh=n, aliases=[], unit_id=u[0], ordinal=u[1], origin="t")[0]
                    for n in ("小林", "水野"))
    upsert(conn, book.id, hino, adachi, "みずのっつぃー", ordinal=u[1], unit_id=u[0], origin="human", certainty="human",
           form_zh="水野仔～～")
    upsert(conn, book.id, hino, adachi, "みずのっつぃーくん", ordinal=u[1], unit_id=u[0], origin="llm", certainty="inferred")
    assert decided_variants(conn, hino, adachi, "みずのっつぃーくん") == [{"ja": "みずのっつぃー", "zh": "水野仔～～"}]



def test_a_title_talked_about_or_ones_own_is_not_a_way_of_calling(conn, cfg, fixtures):
    """「『賢者』が死んだ？」 is about her, 「『英雄』と呼ばれているからには」 is his own title: the second look says so,
    and cards, house style and relations count only the calls."""
    from ncte.analysis import house_style
    from ncte.library import import_book
    from ncte.memory.address import calls_by, set_usage, upsert
    from ncte.memory.repository import upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    hero = upsert_character(conn, book.id, "レイド", name_zh="雷德", aliases=[], unit_id="u", ordinal=1, origin="t")[0]
    sage = upsert_character(conn, book.id, "エルリア", name_zh="艾爾莉雅", aliases=[], unit_id="u", ordinal=1, origin="t")[0]
    ids = {"call": upsert(conn, book.id, hero, sage, "エルリア", ordinal=2, unit_id=None, origin="llm", certainty="explicit"),
           "about": upsert(conn, book.id, hero, sage, "賢者", ordinal=3, unit_id=None, origin="llm", certainty="explicit"),
           "own": upsert(conn, book.id, hero, hero, "英雄", ordinal=4, unit_id=None, origin="llm", certainty="explicit")}
    row = {k: conn.execute("SELECT * FROM address_terms WHERE id=?", (v,)).fetchone() for k, v in ids.items()}
    set_usage(conn, book.id, row["about"], "mention", "談論")
    assert [k for k, r in row.items() if calls_by(conn, r)] == ["call"]
    req, _ = house_style.request(conn, book.id)
    assert req["address_forms"] == ["エルリア×1"]


def test_a_form_the_model_says_is_talk_about_someone_is_not_a_call(conn, cfg, fixtures):
    """The model reads 「カイト」 in ミナ's line as talking about him (not calling him): recorded as a mention, so the
    cards, the house style and relations do not count it as how she calls him."""
    from ncte.memory.address import calls_by

    def mem(payload, req):
        out = memory_update(payload, req)
        for a in out.get("address_terms", []):
            a["usage"] = "mention"
        return out

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": mem}), mode="context")
    row = conn.execute("SELECT * FROM address_terms WHERE form_ja='カイト'").fetchone()
    assert row is not None and not calls_by(conn, row)


def test_a_persons_usage_judgment_survives_a_merge(conn, cfg, fixtures):
    from ncte.library import import_book
    from ncte.memory.address import calls_by, set_usage, upsert, usage_of
    from ncte.memory.repository import merge_characters, upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()
    mk = lambda n: upsert_character(conn, book.id, n, name_zh=n, aliases=[], unit_id=u[0], ordinal=u[1], origin="t")[0]  # noqa: E731
    a, b, b2 = mk("甲"), mk("乙"), mk("乙'")
    rid = upsert(conn, book.id, a, b, "乙さん", ordinal=u[1], unit_id=u[0], origin="llm", certainty="inferred")
    row = conn.execute("SELECT * FROM address_terms WHERE id=?", (rid,)).fetchone()
    set_usage(conn, book.id, row, "address", "t", origin="llm")
    assert calls_by(conn, row)
    # a person's judgment on a card merged away is kept over the model's on the kept card
    rid2 = upsert(conn, book.id, a, b2, "乙さん", ordinal=u[1], unit_id=u[0], origin="llm", certainty="inferred")
    set_usage(conn, book.id, conn.execute("SELECT * FROM address_terms WHERE id=?", (rid2,)).fetchone(), "mention", "人", origin="human")
    merge_characters(conn, b, b2)
    assert usage_of(conn, conn.execute("SELECT * FROM address_terms WHERE addressee_id=?", (b,)).fetchone()) == "mention"
