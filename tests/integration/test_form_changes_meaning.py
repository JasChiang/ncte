"""ADR-103 (user's decision 2026-10-24): a form may belong to several cards, each from where the model gave it to that
card. 「母上」 is the hero's late birth mother early in the story and his stepmother from a later chapter: earlier
paragraphs find the birth mother by it, later ones both, and then the program picks neither."""

import json

from ncte.analysis.scene import apply_scene_analysis, get_scene
from ncte.library import import_book
from ncte.llm.schemas import SceneAnalysisOutput
from ncte.memory import repository as repo
from ncte.memory import visibility as vis
from ncte.memory.address import name_forms
from ncte.memory.entities import character_for_form


def _book(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (book.id, scenes[0]["id"])).fetchone()
    birth, _ = repo.upsert_character(conn, book.id, "実母", name_zh=None, aliases=["母上"], unit_id=first["id"],
                                     ordinal=first["ordinal"], origin="test")
    step, _ = repo.upsert_character(conn, book.id, "義母", name_zh=None, aliases=[], unit_id=first["id"],
                                    ordinal=first["ordinal"], origin="test")
    units = []
    for sc in scenes[1:4]:
        u = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND kind='text' ORDER BY ordinal LIMIT 1",
                         (book.id, sc["id"])).fetchone()
        conn.execute("UPDATE units SET plain_text='　母上が微笑んだ。' WHERE id=?", (u["id"],))
        units.append(conn.execute("SELECT * FROM units WHERE id=?", (u["id"],)).fetchone())
    return book, scenes, birth, step, units


def _read(conn, book, scene, unit, **parts):
    out = {"pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [],
           "props": [], "uncertainties": [], "new_characters": [], **parts}
    refs: dict[str, str] = {}
    stats = apply_scene_analysis(conn, book.id, get_scene(conn, book.id, scene["id"]),
                                 SceneAnalysisOutput.model_validate(out), refs=refs)
    return refs, stats


def _proposal(unit):
    return {"new_characters": [{"temp_id": "new_1", "name_ja": "母上", "aliases_ja": [], "proposed_name_zh": "",
                                "evidence_unit_ids": [unit["id"]]}]}


def test_a_form_given_to_a_second_card_later_names_it_from_there_on_only(conn, cfg, fixtures):
    book, scenes, birth, step, (u1, u2, u3) = _book(conn, cfg, fixtures)
    # chapter 2: the model says this 「母上」 is the stepmother — kept, from here
    _, stats = _read(conn, book, scenes[2], u2, other_forms=[{"ref": step, "ja": "母上", "kind": "title",
                                                               "evidence_unit_ids": [u2["id"]]}])
    assert stats["rejected"] == 0 and "母上" in json.loads(conn.execute(
        "SELECT aliases_json FROM characters WHERE id=?", (step,)).fetchone()[0])
    card = conn.execute("SELECT * FROM characters WHERE id=?", (step,)).fetchone()
    assert "母上" not in vis.character_view(conn, book.id, card, u1["ordinal"])["aliases_ja"]
    assert "母上" in vis.character_view(conn, book.id, card, u2["ordinal"])["aliases_ja"]
    b = conn.execute("SELECT * FROM characters WHERE id=?", (birth,)).fetchone()
    assert "母上" in vis.character_view(conn, book.id, b, u1["ordinal"])["aliases_ja"]  # hers stays visible early
    # earlier: the birth mother is the one the word finds; later, both have it — the program picks neither
    refs, _ = _read(conn, book, scenes[1], u1, **_proposal(u1))
    assert refs["new_1"] == birth
    refs, _ = _read(conn, book, scenes[3], u3, **_proposal(u3))
    marked = conn.execute("SELECT * FROM characters WHERE id=?", (refs["new_1"],)).fetchone()
    assert marked["id"] not in (birth, step) and set(repo.may_be(marked)) == {birth, step}
    early, _ = name_forms(conn, book.id, u1["ordinal"])
    late, ambiguous = name_forms(conn, book.id, u3["ordinal"])
    assert early.get("母上") == birth and "母上" not in late and "母上" in ambiguous
    assert character_for_form(conn, book.id, "母上") is None  # no position given: two cards have it, none picked
