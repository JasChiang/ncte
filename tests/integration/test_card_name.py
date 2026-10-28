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
