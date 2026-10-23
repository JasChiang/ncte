"""ADR-103: a new person known only by words several cards have (two unnamed mothers, each 「母親」) gets a card of its
own — named by the book's words, labelled only to tell it apart, marked as maybe one of those cards — and the second
look asks the model which, or whether someone else."""

import json
import sqlite3
from typing import Any

from ncte.analysis import verify
from ncte.analysis.scene import apply_scene_analysis, build_analysis_payload, get_scene
from ncte.library import import_book
from ncte.llm.schemas import SceneAnalysisOutput, VerifyOutput
from ncte.memory import repository as repo
from ncte.memory import visibility as vis
from ncte.memory.retrieval import memory_context
from ncte.translation.cache import glossary_entries

LABEL = "（3）"  # the third card known by 「母親」: the program's label, never the book's words


def _setup(conn: sqlite3.Connection, cfg: Any, fixtures: Any) -> dict[str, Any]:
    """Two mothers seen in the first scene: one named 「母親」, one 「義母」 whom the book also calls 「母親」. The second
    scene opens with 「母親」 and her line; the model reads her as someone new."""
    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (book.id, scenes[0]["id"])).fetchone()
    a, _ = repo.upsert_character(conn, book.id, "母親", name_zh="母親", aliases=[], unit_id=first["id"],
                                 ordinal=first["ordinal"], origin="test")
    b, _ = repo.upsert_character(conn, book.id, "義母", name_zh="繼母", aliases=["母親"], unit_id=first["id"],
                                 ordinal=first["ordinal"], origin="test")
    repo.ensure_glossary(conn, book.id, "母親", "母親", category="character", origin="test", first_unit_id=first["id"])
    s1 = scenes[1]
    rows = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND kind='text' ORDER BY ordinal",
                        (book.id, s1["id"])).fetchall()
    intro = next(r for r in rows if "少女が立っていた" in r["plain_text"])
    line = next(r for r in rows if "どこで手に入れたの" in r["plain_text"])
    conn.execute("UPDATE units SET plain_text='　路地の奥に、母親が立っていた。' WHERE id=?", (intro["id"],))
    out = {"pov": None, "location": None, "time": None, "participants": [], "actions": [], "props": [],
           "uncertainties": [],
           "new_characters": [{"temp_id": "new_1", "name_ja": "母親", "aliases_ja": [], "proposed_name_zh": "母親",
                               "evidence_unit_ids": [intro["id"]]}],
           "utterances": [{"unit_id": line["id"], "qi": 0, "status": "inferred", "speaker": "new_1", "conflicts": [],
                           "candidates": [{"ref": "new_1", "types": ["continuity"], "evidence": [intro["id"]],
                                           "note": ""}]}]}
    refs: dict[str, str] = {}
    stats = apply_scene_analysis(conn, book.id, get_scene(conn, book.id, s1["id"]),
                                 SceneAnalysisOutput.model_validate(out), refs=refs)
    return {"book": book, "scenes": scenes, "a": a, "b": b, "p": refs["new_1"], "stats": stats, "line": line,
            "intro": intro}


def _card(conn: sqlite3.Connection, cid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()


def _speaker(conn: sqlite3.Connection, unit_id: str) -> str | None:
    return conn.execute("SELECT speaker_id_nullable FROM utterances WHERE unit_id=?", (unit_id,)).fetchone()[0]


def test_a_new_person_known_by_two_cards_words_gets_a_labelled_marked_card(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    p = _card(conn, s["p"])
    assert s["p"] not in (s["a"], s["b"]) and s["stats"]["unconfirmed"] == 1 and s["stats"]["rejected"] == 0
    assert not conn.execute("SELECT 1 FROM memory_rejections").fetchone()
    # named by the book's words; the label only tells it apart where a person picks among cards
    assert (p["canonical_name"], p["label"], json.loads(p["aliases_json"])) == ("母親", "3", [])
    assert set(repo.may_be(p)) == {s["a"], s["b"]}
    assert repo.card_label(p) == "母親" + LABEL
    assert _speaker(conn, s["line"]["id"]) == s["p"]  # its line keeps a speaker
    state = json.loads(get_scene(conn, s["book"].id, s["scenes"][1]["id"])["state_json"])
    assert state["unconfirmed_cards"][0]["character_id"] == s["p"]
    # the card the name finds is still the one named so
    assert repo.upsert_character(conn, s["book"].id, "母親", name_zh=None, aliases=[], unit_id=s["intro"]["id"],
                                 ordinal=s["intro"]["ordinal"], origin="test")[0] == s["a"]


def test_the_label_is_never_a_name_a_glossary_term_or_sent_to_a_model(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    forms = [f for c in repo.characters(conn, book.id) for f in [c["canonical_name"], *json.loads(c["aliases_json"])]]
    assert not any(LABEL in f or "3" in f for f in forms)
    assert not any(LABEL in (g["source_term"] + g["target_term"]) for g in glossary_entries(conn, book.id))
    assert not conn.execute("SELECT 1 FROM entity_links WHERE instr(term, '（') > 0 OR instr(target_term, '（') > 0"
                            ).fetchone()
    # the translator: cards with the same name told apart by where each was first seen, the line by its card's id
    s1 = get_scene(conn, book.id, s["scenes"][1]["id"])
    rows = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal",
                        (book.id, s1["id"])).fetchall()
    ctx = memory_context(conn, book.id, scene=s1, batch_rows=rows)
    assert LABEL not in json.dumps(ctx, ensure_ascii=False)
    cards = {c["character_id"]: c for c in ctx["characters"]}
    assert cards[s["p"]]["name_ja"] == cards[s["a"]]["name_ja"] == "母親"
    assert cards[s["p"]]["first_seen"].startswith("路地の奥に、母親") and cards[s["a"]]["first_seen"]
    assert "first_seen" not in cards[s["b"]]  # 「義母」: its own name tells it apart
    line = next(u for u in ctx["scene"]["utterances"] if u["unit_id"] == s["line"]["id"])
    assert line["speaker"] == "母親" and line["speaker_id"] == s["p"]
    # the next scene's reading and the second look: no label either
    payload = build_analysis_payload(conn, book.id, get_scene(conn, book.id, s["scenes"][2]["id"]), [])
    assert LABEL not in json.dumps(payload, ensure_ascii=False)
    known = {c["character_id"]: c for c in payload["known_characters"]}
    assert known[s["p"]]["first_seen"] and known[s["a"]]["first_seen"]
    items = verify.collect(conn, book.id)
    assert LABEL not in json.dumps([{k: v for k, v in i.items() if not k.startswith("_")} for i in items],
                                   ensure_ascii=False)


def _ask(conn: sqlite3.Connection, s: dict[str, Any], answer: str, evidence: list[str] | None = None) -> dict[str, int]:
    items = verify.collect(conn, s["book"].id)
    item = next(i for i in items if i["ref"] == f"s:{s['p']}")
    assert item["kind"] == "same_person" and set(item["candidates"]) == {s["a"], s["b"]}
    assert not any(i["ref"] == f"i:{s['p']}" for i in items)  # asked this instead of the open identity question
    codes = evidence if evidence is not None else [next(iter(item["_codes"]))]
    parsed = VerifyOutput.model_validate({"items": [{"ref": item["ref"], "answer": answer, "reason": "x",
                                                     "evidence": codes}]})
    return verify.apply(conn, s["book"].id, items, parsed, lambda *a: None)


def test_the_second_look_answering_same_merges_the_card_with_its_lines(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    assert _ask(conn, s, s["a"], evidence=[])["ignored"] == 1  # no paragraph cited: nothing changes, asked again
    assert repo.may_be(_card(conn, s["p"]))
    stats = _ask(conn, s, s["a"])
    assert stats["same_person_merged"] == 1
    assert _card(conn, s["p"]) is None and _speaker(conn, s["line"]["id"]) == s["a"]
    a = _card(conn, s["a"])
    assert a["label"] == "" and not repo.may_be(a) and "3" not in a["aliases_json"]
    state = json.loads(get_scene(conn, s["book"].id, s["scenes"][1]["id"])["state_json"])
    assert state["unconfirmed_cards"][0]["character_id"] == s["a"]  # the record follows the card


def test_the_second_look_answering_different_clears_the_mark(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    assert _ask(conn, s, "unknown").get("same_person_open") == 1
    assert repo.may_be(_card(conn, s["p"]))  # unanswered: still marked
    conn.execute("DELETE FROM verify_answers")  # (an unknown is asked again once its material changes)
    assert _ask(conn, s, "different")["same_person_different"] == 1
    p = _card(conn, s["p"])
    assert p is not None and not repo.may_be(p) and _speaker(conn, s["line"]["id"]) == s["p"]
    assert p["label"] == "3"  # still one of three cards called 「母親」
    assert not any(i["ref"] == f"s:{s['p']}" for i in verify.collect(conn, s["book"].id))


def test_a_name_revealed_later_is_linked_only_from_the_reveal(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    book, last = s["book"], s["scenes"][-1]
    named = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%リリア%' ORDER BY "
                         "ordinal LIMIT 1", (book.id, last["id"])).fetchone()  # where the book first names her
    out = {"pov": None, "location": None, "time": None, "participants": [], "actions": [], "props": [],
           "uncertainties": [], "utterances": [],
           "new_characters": [{"temp_id": "new_1", "name_ja": "リリア", "aliases_ja": [], "proposed_name_zh": "莉莉亞",
                               "evidence_unit_ids": [named["id"]], "same_as_ref": s["p"]}]}
    refs: dict[str, str] = {}
    apply_scene_analysis(conn, book.id, get_scene(conn, book.id, last["id"]), SceneAnalysisOutput.model_validate(out),
                         refs=refs)
    c = _card(conn, refs["new_1"])
    assert _card(conn, s["p"]) is None and c["canonical_name"] == "リリア" and c["label"] == ""
    assert _speaker(conn, s["line"]["id"]) == c["id"]
    assert set(repo.may_be(c)) == {s["a"], s["b"]}  # a name says what she is called, not which mother she is
    before = vis.character_view(conn, book.id, c, named["ordinal"] - 1)
    after = vis.character_view(conn, book.id, c, named["ordinal"])
    assert before is not None and before["name_ja"] == "母親" and "リリア" not in before["aliases_ja"]
    assert after is not None and after["name_ja"] == "リリア"
    names_early = vis.visible_names(conn, book.id, s["line"]["ordinal"])
    assert names_early[c["id"]] == "母親" and "リリア" not in json.dumps(names_early, ensure_ascii=False)


def test_rebuilding_the_card_table_keeps_every_card_and_what_points_at_it(conn, cfg, fixtures, tmp_path, monkeypatch):
    """Migration 37 rebuilds ``characters`` (its key becomes the name and the label) with foreign keys off, as SQLite
    requires; lines and genders still point at their cards, and the keys are checked again afterwards."""
    from ncte.memory import db

    old = [m for m in db.MIGRATIONS if m[0] <= 36]
    monkeypatch.setattr(db, "MIGRATIONS", old)
    path = tmp_path / "old.db"
    c = db.connect(path)
    book, _ = import_book(c, cfg, fixtures["basic"])
    unit = c.execute("SELECT * FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()
    c.execute("INSERT INTO characters(id, book_id, canonical_name, aliases_json, known_from_unit_id, known_from_ordinal, "
              "origin, created_at, updated_at) VALUES ('c_000000000001', ?, '母親', '[]', ?, ?, 'test', 'x', 'x')",
              (book.id, unit["id"], unit["ordinal"]))
    c.execute("INSERT INTO character_gender(character_id, gender, book_id, ordinal, origin, updated_at) VALUES "
              "('c_000000000001', 'female', ?, ?, 'llm', 'x')", (book.id, unit["ordinal"]))
    c.close()
    monkeypatch.undo()
    c = db.connect(path)
    try:
        row = c.execute("SELECT * FROM characters WHERE id='c_000000000001'").fetchone()
        assert (row["canonical_name"], row["label"], row["may_be_json"]) == ("母親", "", "[]")
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
        assert c.execute("SELECT gender FROM character_gender WHERE character_id='c_000000000001'").fetchone()[0] == "female"
        c.execute("INSERT INTO characters(id, book_id, canonical_name, aliases_json, known_from_ordinal, origin, "
                  "created_at, updated_at, label) VALUES ('c_000000000002', ?, '母親', '[]', 1, 'test', 'x', 'x', '2')",
                  (book.id,))
        try:
            c.execute("INSERT INTO characters(id, book_id, canonical_name, aliases_json, known_from_ordinal, origin, "
                      "created_at, updated_at) VALUES ('c_000000000003', ?, '母親', '[]', 1, 'test', 'x', 'x')",
                      (book.id,))
            raise AssertionError("two cards named 「母親」 without a label")
        except sqlite3.IntegrityError:
            pass
    finally:
        c.close()
