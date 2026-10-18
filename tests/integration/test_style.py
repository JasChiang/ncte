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
    assert data == {"honorifics": "explicit", "rules": ["B"], "voices": {"ミナ": "禮貌"}, "notes": "inline",
                    "auto": False}  # a person set it
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
                # no word in the line says so: the model reads it as an inference
                {"character_ref": kaito, "gender": "male", "certainty": "inferred", "evidence_unit_ids": [u],
                 "evidence_quote": "カイト、遅いですよ"},
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
    # no word in 「カイト、遅いですよ」 says he is male: kept, but only as an inference
    assert row["gender"] == "male" and row["certainty"] == "inferred" and row["canonical_name"] == "カイト"
    assert any(c.get("gender") == "male (inferred)" for c in cards)
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


def test_gender_ranks(conn, cfg, fixtures):
    from ncte.library import import_book
    from ncte.memory.repository import gender_view, set_gender, upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()
    cid = upsert_character(conn, book.id, "ソフィ", name_zh="蘇菲", aliases=[], unit_id=u[0], ordinal=u[1], origin="t")[0]
    hz = (99.0, 10**9)
    set_gender(conn, book.id, cid, "male", ordinal=u[1], quote="俺", origin="llm", certainty="inferred")
    assert gender_view(conn, cid, hz) == ("male", "inferred")
    set_gender(conn, book.id, cid, "female", ordinal=u[1], quote="黒髪の女性", origin="llm")  # explicit replaces it
    assert gender_view(conn, cid, hz) == ("female", "explicit")
    set_gender(conn, book.id, cid, "male", ordinal=u[1], quote="俺", origin="llm", certainty="inferred")
    assert gender_view(conn, cid, hz) == ("female", "explicit")  # an inference never overrides the text


def test_the_model_chooses_a_house_style_after_reading_unless_a_person_set_one(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM
    from ncte.memory.style import SeriesStyle, load_style, save_style

    from ..helpers import run_book
    from ..scripted import RESPONDERS

    asked = []

    def house(payload, req):
        asked.append(payload)
        ref = payload["characters"][0]["ref"]
        return {"honorifics": "explicit", "honorifics_reason": "t", "rules": ["身分敬稱「様」譯作「大人」"],
                "habits": [{"ref": ref, "habit_ja": "〜っす", "render_zh": "〜啦", "note": ""}]}

    llm = FakeLLM(responders={**RESPONDERS, "house_style": house})
    book, _, _ = run_book(conn, cfg, fixtures["basic"], llm, mode="context")
    assert asked and "address_forms" in asked[0] and asked[0]["characters"][0]["lines"]
    st = load_style(conn, book.id)
    assert st.auto and st.honorifics == "explicit" and st.rules == ["身分敬稱「様」譯作「大人」"]
    assert any("〜っす" in v for v in st.voices.values())
    save_style(conn, book.id, SeriesStyle(honorifics="omit"))  # a person's choice
    run_book(conn, cfg, fixtures["basic"], llm, mode="context", force=True)
    assert load_style(conn, book.id).honorifics == "omit"  # kept


def test_house_style_sees_the_chosen_names_and_the_current_translation(conn, cfg, fixtures):
    """Names are naming's choice and a habit keeps the book's rendering: once a book is translated, the house style
    request carries the glossary and each line's paragraph as translated now."""
    from ncte.analysis.house_style import request
    from ncte.llm.fake import FakeLLM

    from ..helpers import run_book
    from ..scripted import RESPONDERS

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    conn.execute("INSERT OR REPLACE INTO glossary(book_id, source_term, target_term, updated_at) VALUES (?,?,?,?)",
                 (book.id, "あだ名", "小名", "2026-10-16"))
    req, _ = request(conn, book.id)
    assert "あだ名→小名" in req["names"]
    lines = [ln for c in req["characters"] for ln in c["lines"]]
    assert lines and all("ja" in ln for ln in lines) and any(ln.get("zh") for ln in lines)


def test_house_style_shows_a_line_for_each_repeated_ending():
    """A habit is seen whatever lines the spread picks: each repeated ending brings one example line."""
    from ncte.analysis.house_style import _pick

    said = [(f"「普通の台詞その{i}です」", f"u{i}") for i in range(60)]
    for i in (5, 6, 7, 8):  # a habit the evenly spread lines miss
        said[i] = (f"「行くっすよ{i}っす」", f"u{i}")
    picked = [q for q, _ in _pick(said)]
    assert any(q.endswith("っす」") for q in picked) and len(picked) <= 12 + 8


def test_a_toc_entry_matches_a_paragraph_with_the_same_source(conn, cfg, fixtures):
    """A book's contents page or a title set as a styled paragraph (「⟦1⟧タイトル⟦/1⟧」): the TOC entry with the same
    source reuses its translation, and the GUI shows it while the TOC itself is not translated (a sample)."""
    from ncte.gui.data import chapters
    from ncte.library import resolve_book
    from ncte.llm.fake import FakeLLM
    from ncte.translation.context_builder import heading_hints

    from ..helpers import run_book
    from ..scripted import RESPONDERS

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    toc = conn.execute("SELECT * FROM units WHERE book_id=? AND kind IN ('nav','ncx') AND plain_text LIKE '第一章%' LIMIT 1",
                       (book.id,)).fetchone()
    body = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='text' AND spine_index IS NOT NULL ORDER BY ordinal "
                        "LIMIT 1", (book.id,)).fetchone()
    assert toc and body
    conn.execute("UPDATE units SET plain_text=?, source_text=? WHERE id=?", (toc["plain_text"], f"⟦1⟧{toc['plain_text']}⟦/1⟧",
                                                                             body["id"]))
    conn.execute("UPDATE translations SET text='⟦1⟧章的標題⟦/1⟧', source_hash=(SELECT source_hash FROM units WHERE id=?) "
                 "WHERE unit_id=?", (body["id"], body["id"]))
    assert {"ja": toc["plain_text"], "zh": "章的標題"} in heading_hints(conn, book.id)
    conn.execute("DELETE FROM translations WHERE unit_id IN (SELECT id FROM units WHERE book_id=? AND kind IN "
                 "('nav','ncx')) OR unit_id IN (SELECT id FROM units WHERE book_id=? AND kind='heading')", (book.id, book.id))
    titles = [c["title"] for c in chapters(conn, resolve_book(conn, book.id))]
    assert "章的標題" in titles
