"""ADR-022: series style guide."""

import json

from typer.testing import CliRunner

from ncte.cli import app
from ncte.llm.fake import FakeLLM
from ncte.memory.style import SeriesStyle, load_style, save_style, style_payload

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_default_style_only_allows_notes():
    assert set(style_payload(SeriesStyle(), "ミナ")) == {"notes"}  # notes are on by default
    assert style_payload(SeriesStyle(notes="off"), "ミナ") is None


def test_payload_filters_voices():
    st = SeriesStyle(honorifics="omit", rules=["先輩一律譯「學姊」"], voices={"ミナ": "有禮貌的敬語", "リリア": "王族口吻"})
    p = style_payload(st, "「カイト、遅いですよ」とミナ")
    assert p["rules"] == ["先輩一律譯「學姊」"] and p["voices"] == {"ミナ": "有禮貌的敬語"} and "省略" in p["honorifics"]
    assert style_payload(st, "無名", ["リリア"])["voices"] == {"リリア": "王族口吻"}


def test_style_reaches_every_volume_request(conn, cfg, fixtures):
    from ncte.library import import_book

    book, _ = import_book(conn, cfg, fixtures["basic"])
    save_style(conn, book.id, SeriesStyle(rules=["保留語尾「ですよ」的禮貌感"], voices={"カイト": "粗魯的男生口吻"}))
    assert load_style(conn, book.id).rules == ["保留語尾「ですよ」的禮貌感"]
    fake = FakeLLM(responders=RESPONDERS)
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    payloads = [json.loads(c.messages[1]["content"]) for c in fake.calls if c.stage.startswith("translate")]
    body = [p for p in payloads if "known_headings" not in p]
    assert body and all(p["style"]["rules"] == ["保留語尾「ですよ」的禮貌感"] for p in body)
    # カイト's voice note reaches batches where カイト is named or present in the scene
    assert any(p["style"].get("voices") == {"カイト": "粗魯的男生口吻"} for p in body)


def test_style_cli(tmp_path, fixtures):
    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "import", epub]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "style", "set", epub, "--honorifics", "explicit", "--rule", "A", "--rule", "B",
                         "--voice", "ミナ=禮貌"])
    assert out.exit_code == 0, out.output
    assert r.invoke(app, ["-w", str(ws), "style", "set", epub, "--remove-rule", "1"]).exit_code == 0
    data = json.loads(r.invoke(app, ["-w", str(ws), "style", "show", epub, "--json"]).output)
    assert data == {"honorifics": "explicit", "rules": ["B"], "voices": {"ミナ": "禮貌"}, "notes": "inline"}
    bad = r.invoke(app, ["-w", str(ws), "style", "set", epub, "--honorifics", "polite"])
    assert bad.exit_code != 0 and "invalid style" in str(bad.exception)


def test_translator_notes_only_when_allowed_and_exported_in_parentheses(conn, cfg, fixtures, tmp_path):
    import json

    from ncte.exporter import export_book
    from ncte.memory.style import SeriesStyle, save_style

    def tr(payload, req):
        out = {"translations": []}
        for u in payload["units"]:
            note = "日文「鍵」也有關鍵之意" if payload.get("style", {}).get("notes") and "銀の鍵" in u["source"] else ""
            out["translations"].append({"unit_id": u["unit_id"], "translated_text": "譯" + u["unit_id"][-4:],
                                        "flags": [], "notes": [], "translator_note": note})
        return out

    from ncte.library import import_book

    book, _ = import_book(conn, cfg, fixtures["basic"])
    save_style(conn, book.id, SeriesStyle(notes="off"))
    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "translate": tr}), mode="context")
    assert conn.execute("SELECT COUNT(*) FROM translations WHERE translator_note IS NOT NULL").fetchone()[0] == 0
    save_style(conn, book.id, SeriesStyle(notes="inline"))
    fake = FakeLLM(responders={**RESPONDERS, "translate": tr})
    run_book(conn, cfg, fixtures["basic"], fake, mode="context", force=True)
    assert any(json.loads(c.messages[1]["content"]).get("style", {}).get("notes")
               for c in fake.calls if c.stage.startswith("translate"))
    noted = conn.execute("SELECT unit_id, translator_note FROM translations WHERE translator_note IS NOT NULL").fetchall()
    assert noted and noted[0]["translator_note"] == "日文「鍵」也有關鍵之意"
    out = tmp_path / "o.epub"
    export_book(conn, cfg, book, output=out, run_epubcheck=False)
    import zipfile
    blob = "".join(zipfile.ZipFile(out).read(n).decode("utf-8", "ignore") for n in zipfile.ZipFile(out).namelist()
                   if n.endswith("html"))
    assert "（註：日文「鍵」也有關鍵之意）" in blob


def test_wordplay_found_while_reading_reaches_translation(conn, cfg, fixtures):
    from ..scripted import memory_update, uid

    def mem(payload, req):
        out = memory_update(payload, req)
        u = uid(payload, "カイト、遅いですよ")
        if u:
            out["wordplay"] = [
                {"unit_id": u, "term": "カイト", "explanation": "與「凧（風箏）」同音", "note_zh": "日文「海斗」與風箏同音",
                 "evidence_quote": "カイト、遅いですよ"},
                {"unit_id": u, "term": "不存在", "explanation": "x", "note_zh": "", "evidence_quote": ""}]  # rejected
        return out

    seen = []

    def tr(payload, req):
        seen.extend(payload.get("wordplay", []))
        return {"translations": [{"unit_id": u["unit_id"], "translated_text": "譯", "flags": [], "notes": [],
                                  "translator_note": ""} for u in payload["units"]]}

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": mem, "translate": tr}),
             mode="context")
    rows = conn.execute("SELECT term, note_zh FROM reader_notes").fetchall()
    assert [tuple(r) for r in rows] == [("カイト", "日文「海斗」與風箏同音")]
    assert [w["term"] for w in seen] == ["カイト"] and seen[0]["note"] == "日文「海斗」與風箏同音"


def test_gender_with_evidence_reaches_character_cards(conn, cfg, fixtures):
    from ncte.memory import visibility as vis

    from ..scripted import known, memory_update, uid

    def mem(payload, req):
        out = memory_update(payload, req)
        u, kaito = uid(payload, "カイト、遅いですよ"), known(payload, "カイト")
        if u and kaito:
            out["genders"] = [
                {"character_ref": kaito, "gender": "male", "evidence_unit_ids": [u], "evidence_quote": "カイト、遅いですよ"},
                {"character_ref": kaito, "gender": "female", "evidence_unit_ids": [u], "evidence_quote": "彼女は"}]  # rejected
        return out

    cards = []

    def tr(payload, req):
        cards.extend(c for c in payload.get("characters", []) if c["name_ja"] == "カイト")
        return {"translations": [{"unit_id": u["unit_id"], "translated_text": "譯", "flags": [], "notes": [],
                                  "translator_note": ""} for u in payload["units"]]}

    book, _, _ = run_book(conn, cfg, fixtures["basic"],
                          FakeLLM(responders={**RESPONDERS, "memory_update": mem, "translate": tr}), mode="context")
    row = conn.execute("SELECT g.*, c.* FROM character_gender g JOIN characters c ON c.id=g.character_id").fetchone()
    assert row["gender"] == "male" and row["canonical_name"] == "カイト"
    assert any(c.get("gender") == "male" for c in cards)
    c = conn.execute("SELECT * FROM characters WHERE id=?", (row["character_id"],)).fetchone()
    before = vis.character_view(conn, book.id, c, row["ordinal"] - 1)
    assert before is None or "gender" not in before  # not shown before the evidence


def test_gender_conflict_and_human_override(conn, cfg, fixtures):
    from ncte.library import import_book
    from ncte.memory.repository import gender_at, set_gender, upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()
    cid = upsert_character(conn, book.id, "ヒカル", name_zh="光", aliases=[], unit_id=u[0], ordinal=u[1], origin="t")[0]
    hz = (99.0, 10**9)
    assert set_gender(conn, book.id, cid, "female", ordinal=u[1], quote="彼女", origin="llm") == "female"
    assert set_gender(conn, book.id, cid, "male", ordinal=u[1], quote="彼", origin="llm") == "conflict"
    assert gender_at(conn, cid, hz) is None
    set_gender(conn, book.id, cid, "male", ordinal=u[1], quote=None, origin="human")
    assert set_gender(conn, book.id, cid, "female", ordinal=u[1], quote="彼女", origin="llm") == "male"
    assert gender_at(conn, cid, hz) == "male"


def test_gender_scan_for_a_volume_already_read(conn, cfg, fixtures):
    from ..scripted import known, memory_update, uid

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def mem(payload, req):
        out = memory_update(payload, req)
        u, kaito = uid(payload, "カイト、遅いですよ"), known(payload, "カイト")
        out["facts"] = []  # a scan must not touch facts
        if u and kaito:
            out["genders"] = [{"character_ref": kaito, "gender": "male", "evidence_unit_ids": [u],
                               "evidence_quote": "カイト、遅いですよ"}]
        return out

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": mem}), mode="context",
             gender_only=True)
    assert conn.execute("SELECT gender FROM character_gender").fetchone()[0] == "male"
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == facts
