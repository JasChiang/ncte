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


def test_find_forms_longest_match_and_honorifics():
    forms = {"島村": "c1", "しまむら": "c1", "しまむらちゃん": "c1", "安達": "c2"}
    assert find_forms("「島村さん、安達は？」", forms) == [("島村さん", "c1"), ("安達", "c2")]
    assert find_forms("しまむらちゃん", forms) == [("しまむらちゃん", "c1")]
    assert find_forms("島《しま》村《むら》くん", forms) == [("島村くん", "c1")]  # ruby readings ignored


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
