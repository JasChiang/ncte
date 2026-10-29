"""A card the model made for a person known by a title or a description (『英雄』) takes the person's name when the model
gives one (レイド・フリーデン): the name is the card's from where it is given, the title stays an alias. A real-book
spot check (英雄と賢者の転生婚, 2026-10-29) kept the protagonists' cards as 「英雄」「賢者」 with the full names only
among the aliases."""

import json

from ncte.analysis.scene import apply_scene_analysis, get_scene
from ncte.library import import_book
from ncte.llm.schemas import SceneAnalysisOutput
from ncte.memory import repository as repo
from ncte.memory import visibility as vis


def _book(conn, cfg, fixtures, texts):
    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()[:len(texts)]
    units = []
    for sc, text in zip(scenes, texts, strict=True):
        u = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND kind='text' ORDER BY ordinal LIMIT 1",
                         (book.id, sc["id"])).fetchone()
        conn.execute("UPDATE units SET plain_text=?, source_text=? WHERE id=?", (text, text, u["id"]))
        units.append(conn.execute("SELECT * FROM units WHERE id=?", (u["id"],)).fetchone())
    return book, scenes, units


def _read(conn, book, scene, **parts):
    out = {"pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [],
           "props": [], "uncertainties": [], "new_characters": [], **parts}
    refs: dict[str, str] = {}
    stats = apply_scene_analysis(conn, book.id, get_scene(conn, book.id, scene["id"]),
                                 SceneAnalysisOutput.model_validate(out), refs=refs)
    return refs, stats


def _new(name, unit, aliases=(), kinds=()):
    return {"temp_id": "new_1", "name_ja": name, "aliases_ja": list(aliases), "proposed_name_zh": "",
            "evidence_unit_ids": [unit["id"]], "alias_kinds": [{"ja": a, "kind": k} for a, k in kinds]}


def _cards(conn, book):
    return {r["canonical_name"]: r for r in conn.execute("SELECT * FROM characters WHERE book_id=?", (book.id,))}


TEXTS = ["　戦場の最中、『英雄』は大剣を担ぎ上げた。", "　『英雄』――レイド・フリーデンは笑った。"]


def test_a_card_known_by_a_title_takes_the_name_the_model_gives_later(conn, cfg, fixtures):
    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, TEXTS)
    refs, _ = _read(conn, book, scenes[0], new_characters=[_new("英雄", u1, ["英雄"], [("英雄", "title")])])
    hero = refs["new_1"]
    assert _cards(conn, book)["英雄"]["name_kind"] == "title"
    _read(conn, book, scenes[1], other_forms=[{"ref": hero, "ja": "レイド・フリーデン", "kind": "name",
                                               "evidence_unit_ids": [u2["id"]]}])
    cards = _cards(conn, book)
    assert "英雄" not in cards and len(cards) == 1  # one person, known by the name
    card = cards["レイド・フリーデン"]
    assert card["name_kind"] == "name" and "英雄" in json.loads(card["aliases_json"])
    assert card["known_from_ordinal"] == u1["ordinal"]  # the person is still known from the first scene
    # the name is not shown before it is given; the title is
    early = vis.character_view(conn, book.id, card, u1["ordinal"])
    late = vis.character_view(conn, book.id, card, u2["ordinal"])
    assert "レイド・フリーデン" not in json.dumps(early, ensure_ascii=False)
    assert "英雄" in json.dumps(early, ensure_ascii=False)
    assert "レイド・フリーデン" in json.dumps(late, ensure_ascii=False)


def test_a_title_and_a_name_given_in_the_same_scene_make_a_card_named_by_the_name(conn, cfg, fixtures):
    book, scenes, (u1,) = _book(conn, cfg, fixtures, ["　『英雄』――レイド・フリーデンは大剣を担ぎ上げた。"])
    _read(conn, book, scenes[0], new_characters=[_new("英雄", u1, ["英雄", "レイド・フリーデン"],
                                                       [("英雄", "title"), ("レイド・フリーデン", "name")])])
    cards = _cards(conn, book)
    assert set(cards) == {"レイド・フリーデン"} and cards["レイド・フリーデン"]["name_kind"] == "name"
    assert "英雄" in json.loads(cards["レイド・フリーデン"]["aliases_json"])


def test_a_card_known_by_a_name_keeps_it_when_another_name_is_given(conn, cfg, fixtures):
    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, ["　レイドは大剣を担ぎ上げた。", "　レイド・フリーデンは笑った。"])
    refs, _ = _read(conn, book, scenes[0], new_characters=[_new("レイド", u1, [], [("レイド", "name")])])
    _read(conn, book, scenes[1], other_forms=[{"ref": refs["new_1"], "ja": "レイド・フリーデン", "kind": "name",
                                               "evidence_unit_ids": [u2["id"]]}])
    cards = _cards(conn, book)
    assert set(cards) == {"レイド"} and "レイド・フリーデン" in json.loads(cards["レイド"]["aliases_json"])


def test_a_title_card_does_not_take_a_name_another_card_has(conn, cfg, fixtures):
    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, TEXTS)
    other, _ = repo.upsert_character(conn, book.id, "レイド・フリーデン", name_zh=None, aliases=[], unit_id=u1["id"],
                                     ordinal=u1["ordinal"], origin="test")
    refs, _ = _read(conn, book, scenes[0], new_characters=[_new("英雄", u1, ["英雄"], [("英雄", "title")])])
    _read(conn, book, scenes[1], other_forms=[{"ref": refs["new_1"], "ja": "レイド・フリーデン", "kind": "name",
                                               "evidence_unit_ids": [u2["id"]]}])
    cards = _cards(conn, book)
    assert set(cards) == {"英雄", "レイド・フリーデン"} and cards["レイド・フリーデン"]["id"] == other


def test_a_card_known_by_a_role_word_is_shown_with_where_it_was_first_seen(conn, cfg, fixtures):
    """ADR-110: 「給仕」 is whoever holds the post. A card known by such words is shown to a later scene's reading with
    where it was first seen, so the model can tell whether this servant is that one; a card known by a name is not."""
    from ncte.analysis.scene import build_analysis_payload

    book, scenes, (u1, u2, _) = _book(conn, cfg, fixtures, ["　すれ違った給仕に頭を下げられた。", "　ミナが笑った。",
                                                            "　給仕が飲み物を運んでくる。"])
    refs, _ = _read(conn, book, scenes[0], new_characters=[_new("給仕", u1, ["給仕"], [("給仕", "description")])])
    named, _ = _read(conn, book, scenes[1], new_characters=[_new("ミナ", u2, [], [("ミナ", "name")])])
    payload = build_analysis_payload(conn, book.id, get_scene(conn, book.id, scenes[2]["id"]), [])
    known = {c["character_id"]: c for c in payload["known_characters"]}
    assert known[refs["new_1"]]["first_seen"].startswith("すれ違った給仕")
    assert "first_seen" not in known[named["new_1"]]


def test_a_title_card_does_not_take_a_name_another_card_has_through_its_entity(conn, cfg, fixtures):
    """ロセト・アイン is a form of ロセト's (a link, not a card's name): giving it to 『英雄』 is the model saying the
    hero is called that, not the program's to decide the two are one — the hero keeps its card."""
    from ncte.memory.entities import link

    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, ["　『英雄』は大剣を担ぎ上げた。", "　ロセト・アインは笑った。"])
    rosete, _ = repo.upsert_character(conn, book.id, "ロセト", name_zh=None, aliases=[], unit_id=u1["id"],
                                      ordinal=u1["ordinal"], origin="test")
    for term, zh in (("ロセト", "羅塞特"), ("ロセト・アイン", "羅塞特・艾恩")):
        repo.ensure_glossary(conn, book.id, term, zh, category="character", origin="test", first_unit_id=u1["id"])
    assert link(conn, book.id, "ロセト・アイン", "ロセト", "alias", origin="test", ordinal=u1["ordinal"]) == "accepted"
    refs, _ = _read(conn, book, scenes[0], new_characters=[_new("英雄", u1, ["英雄"], [("英雄", "title")])])
    _read(conn, book, scenes[1], other_forms=[{"ref": refs["new_1"], "ja": "ロセト・アイン", "kind": "name",
                                               "evidence_unit_ids": [u2["id"]]}])
    cards = _cards(conn, book)
    assert set(cards) == {"英雄", "ロセト"} and cards["ロセト"]["id"] == rosete


def test_a_second_card_by_a_role_word_takes_the_name_given_later(conn, cfg, fixtures):
    """ADR-110: another 「給仕」 (``other_person``) is a labelled card; it too takes the name the model gives later."""
    book, scenes, (u1, u2, u3) = _book(conn, cfg, fixtures, ["　給仕が頭を下げた。", "　別の給仕が飲み物を運んできた。",
                                                             "　給仕のミナは笑った。"])
    _read(conn, book, scenes[0], new_characters=[_new("給仕", u1, ["給仕"], [("給仕", "description")])])
    refs, _ = _read(conn, book, scenes[1], new_characters=[{**_new("給仕", u2, ["給仕"], [("給仕", "description")]),
                                                            "other_person": True}])
    second = refs["new_1"]
    first = conn.execute("SELECT id FROM characters WHERE book_id=? AND canonical_name='給仕' AND label=''",
                         (book.id,)).fetchone()[0]
    assert first != second
    assert conn.execute("SELECT name_kind FROM characters WHERE id=?", (second,)).fetchone()[0] == "description"
    _read(conn, book, scenes[2], other_forms=[{"ref": second, "ja": "ミナ", "kind": "name",
                                               "evidence_unit_ids": [u3["id"]]}])
    mina = _cards(conn, book)["ミナ"]
    assert "給仕" in json.loads(mina["aliases_json"])
    assert conn.execute("SELECT COUNT(*) FROM characters WHERE book_id=? AND canonical_name='給仕'",
                        (book.id,)).fetchone()[0] == 1  # the first servant is still her own card


def test_tell_apart_does_not_show_an_impression_written_in_a_later_volume(conn, cfg, fixtures, tmp_path):
    from ncte.memory.db import utcnow
    from tests.integration.test_series import series_of_two

    b1, b2 = series_of_two(conn, cfg, fixtures, tmp_path)
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal DESC LIMIT 1", (b1.id,)).fetchone()
    cid, _ = repo.upsert_character(conn, b1.id, "給仕", name_zh=None, aliases=[], unit_id=u["id"], ordinal=0,
                                   origin="test")
    conn.execute("INSERT INTO character_impressions(character_id, book_id, since_ordinal, personality, voice, role, "
                 "evidence_json, revealed_ordinal, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (cid, b2.id, 1, "", "", "其實是間諜", "[]", 1, utcnow()))
    card = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
    assert "earlier_model_reading" not in vis.tell_apart(conn, b1.id, [card], u["ordinal"])[cid]
    assert vis.tell_apart(conn, b2.id, [card], 5)[cid]["earlier_model_reading"] == ["其實是間諜"]
