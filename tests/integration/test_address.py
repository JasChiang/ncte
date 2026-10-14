"""ADR-022: address terms – who calls whom what, with the rendering to keep."""

import json

from ncte.llm.fake import FakeLLM
from ncte.memory.address import find_forms, harvest_book

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
    forms = {"島村": "c1", "しまむら": "c1", "しまむらちゃん": "c1", "安達": "c2"}
    assert find_forms("「島村さん、安達は？」", forms) == [("島村さん", "c1"), ("安達", "c2")]
    assert find_forms("しまむらちゃん", forms) == [("しまむらちゃん", "c1")]
    assert find_forms("島《しま》村《むら》くん", forms) == [("島村くん", "c1")]  # ruby readings ignored
    assert set(find_forms("「ミリス嬢、エルリア」", {"ミリス": "c3", "エルリア": "c4"})) == {("ミリス嬢", "c3"), ("エルリア", "c4")}


def test_program_harvest_from_resolved_quotes(conn, cfg, fixtures):
    book, _, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    assert s.status == "completed"
    r = rows(conn)[("ミナ", "カイト", "カイト")]
    assert (r["certainty"], r["uses"], r["origin"]) == ("explicit", 1, "program")  # 「カイト、遅いですよ」とミナ
    assert r["form_zh"] == "海斗" and r["zh_origin"] == "glossary"
    # idempotent: harvesting again does not count the same quote twice
    harvest_book(conn, book.id)
    assert rows(conn)[("ミナ", "カイト", "カイト")]["uses"] == 1


def test_translation_payload_carries_earlier_address_terms(conn, cfg, fixtures):
    fake = FakeLLM(responders=RESPONDERS)
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    payloads = [json.loads(c.messages[1]["content"]) for c in fake.calls if c.stage.startswith("translate")]
    first = next(p for p in payloads if any("遅いですよ" in u["source"] for u in p["units"]))
    assert "address_terms" not in first  # nothing was known before the first scene
    later = [p for p in payloads if p.get("address_terms")]
    assert later and later[0]["address_terms"][0] == {"speaker": "米娜", "to": "海斗", "ja": "カイト", "uses": 1,
                                                       "since": "第1集", "zh": "海斗"}


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


def test_human_speaker_change_reharvests(conn, cfg, fixtures):
    from ncte.analysis.speaker import set_speaker_human

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    utt = conn.execute("SELECT id FROM utterances WHERE quote_text LIKE '%遅いですよ%'").fetchone()[0]
    set_speaker_human(conn, utt, None)  # the speaker is confirmed unknown → the observation is withdrawn
    assert ("ミナ", "カイト", "カイト") not in rows(conn)


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


def test_memory_model_reassigns_the_name_owner_guess(conn, cfg, fixtures):
    # 「アダチさん」 said to アダチ's mother is counted for アダチ by the program (the name's owner); the memory
    # model read the scene and says otherwise
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
    mom = upsert_character(conn, book.id, "カイトの母", name_zh="海斗的母親", aliases=[], unit_id=u0[0], ordinal=u0[1],
                           origin="t")[0]
    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": mem}), mode="context")
    line = conn.execute("SELECT id FROM utterances WHERE quote_text LIKE '%カイト、遅いですよ%'").fetchone()[0]
    to = conn.execute("SELECT a.addressee_id FROM address_observations o JOIN address_terms a ON a.id=o.address_id "
                      "WHERE o.utterance_id=?", (line,)).fetchall()
    assert [r[0] for r in to] == [mom]
