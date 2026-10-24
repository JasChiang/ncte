"""ADR-103 實測三: one path for a new person whose words cards already have, whatever the model wrote in other_person
and period; a card shown by the words it was given before its name; gender evidence by every id reference; a
question may name anyone known."""

import json

from ncte.analysis import gender_gaps, verify
from ncte.analysis.scene import apply_scene_analysis, get_scene
from ncte.library import import_book
from ncte.llm.schemas import SceneAnalysisOutput, VerifyOutput
from ncte.memory import repository as repo
from ncte.memory import visibility as vis
from ncte.memory.db import utcnow

from .test_unconfirmed_cards import _card, _speaker
from .test_unconfirmed_realrun2 import _mothers


def _scene(conn, s, new, lines=()):
    out = {"pov": None, "location": None, "time": None, "participants": [], "actions": [], "props": [],
           "uncertainties": [],
           "new_characters": [{"proposed_name_zh": "", "evidence_unit_ids": [s["intro"]["id"]], "aliases_ja": [], **nc}
                              for nc in new],
           "utterances": [{"unit_id": u["id"], "qi": 0, "status": "inferred", "speaker": who, "conflicts": [],
                           "candidates": [{"ref": who, "types": ["continuity"], "evidence": [u["id"]], "note": ""}]}
                          for u, who in lines]}
    refs: dict[str, str] = {}
    apply_scene_analysis(conn, s["book"].id, get_scene(conn, s["book"].id, s["scenes"][1]["id"]),
                         SceneAnalysisOutput.model_validate(out), refs=refs)
    return refs


def test_people_the_model_calls_another_person_with_any_time_are_two_marked_cards(conn, cfg, fixtures):
    """Luka's mother and Mio's mother, both 「母親」, both other_person with the time 「本場景」: two cards (the
    proposal's order), both marked as maybe the cards with the words, each asked in the second look; read again, the
    same two."""
    s = _mothers(conn, cfg, fixtures)
    new = [{"temp_id": t, "name_ja": "母親", "other_person": True, "period": "本場景"} for t in ("new_1", "new_2")]
    refs = _scene(conn, s, new)
    one, two = _card(conn, refs["new_1"]), _card(conn, refs["new_2"])
    assert one["id"] != two["id"]
    for c in (one, two):
        assert set(repo.may_be(c)) == {s["a"], s["b"]} and c["label"] and repo.period_of(c) == "本場景"
    again = _scene(conn, s, new)
    assert (again["new_1"], again["new_2"]) == (one["id"], two["id"])
    items = verify.collect(conn, s["book"].id)
    asked = {i["ref"]: i for i in items if i["kind"] == "same_person"}
    assert {f"s:{one['id']}", f"s:{two['id']}"} <= set(asked)


def test_a_past_lifes_mother_is_one_of_the_marked_cards_and_can_be_answered_different(conn, cfg, fixtures):
    s = _mothers(conn, cfg, fixtures)
    past = _card(conn, _scene(conn, s, [{"temp_id": "new_1", "name_ja": "母親", "period": "前世"}])["new_1"])
    assert set(repo.may_be(past)) == {s["a"], s["b"]} and repo.period_of(past) == "前世"
    assert repo.upsert_character(conn, s["book"].id, "母親", name_zh=None, aliases=[], unit_id=s["intro"]["id"],
                                 ordinal=s["intro"]["ordinal"], origin="test")[0] == s["a"]  # never found by 母親
    items = verify.collect(conn, s["book"].id)
    item = next(i for i in items if i["ref"] == f"s:{past['id']}")
    parsed = VerifyOutput.model_validate({"items": [{"ref": item["ref"], "answer": "different", "reason": "x",
                                                     "evidence": [next(iter(item["_codes"]))]}]})
    verify.apply(conn, s["book"].id, items, parsed, lambda *a: None)
    assert not repo.may_be(_card(conn, past["id"])) and repo.period_of(_card(conn, past["id"])) == "前世"


def test_a_card_named_later_is_shown_before_by_a_common_word_it_was_given_which_finds_no_one(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    rows = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND kind='text' ORDER BY ordinal",
                        (book.id, scenes[1]["id"])).fetchall()
    intro = next(r for r in rows if "少女が立っていた" in r["plain_text"])
    line = next(r for r in rows if "どこで手に入れたの" in r["plain_text"])
    named = next(r for r in rows if "誰だ、お前" in r["plain_text"])
    conn.execute("UPDATE units SET plain_text='　路地の奥に、母親が立っていた。' WHERE id=?", (intro["id"],))
    conn.execute("UPDATE units SET plain_text='　その人はイレナと名乗った。' WHERE id=?", (named["id"],))
    s = {"book": book, "scenes": scenes, "intro": named}
    refs = _scene(conn, s, [{"temp_id": "new_1", "name_ja": "イレナ", "aliases_ja": ["母親"],
                             "alias_kinds": [{"ja": "母親", "kind": "common"}]}], [(line, "new_1")])
    c = _card(conn, refs["new_1"])
    assert "母親" not in json.loads(c["aliases_json"]) and _speaker(conn, line["id"]) == c["id"]
    at_line = vis.character_view(conn, book.id, c, line["ordinal"])
    assert at_line is not None and at_line["name_ja"] == "母親" and "イレナ" not in at_line["aliases_ja"]
    assert repo.alias_map(repo.characters(conn, book.id)).get("母親") != c["id"]  # the word finds no one by her


def test_gender_evidence_includes_where_the_memory_refers_to_the_card_by_id(conn, cfg, fixtures, monkeypatch):
    """「うちの娘」 said by her mother: no shared word finds it, the relation the memory step noted there does."""
    monkeypatch.setattr(gender_gaps, "MIN_LINES", 1)
    s = _mothers(conn, cfg, fixtures)
    refs = _scene(conn, s, [{"temp_id": "new_1", "name_ja": "母親"}], [(s["line"], "new_1")])
    p = refs["new_1"]
    conn.execute("INSERT INTO relation_notes(book_id, scene_id, a_id, b_id, relation_zh, a_is, b_is, kind, "
                 "evidence_unit_ids_json, evidence_quote, first_ordinal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (s["book"].id, None, s["a"], p, "母女", "母親", "女兒", "identity", json.dumps([s["after"]["id"]]),
                  "うちの娘", s["after"]["ordinal"], utcnow()))
    got = {c["id"]: c for c in gender_gaps.candidates(conn, s["book"].id)}
    assert any(at == s["after"]["ordinal"] and "referred" in how for at, how, _ in got[p]["sentences"])


def test_a_reading_s_question_may_name_anyone_known_not_only_the_scene_s_people(conn, cfg, fixtures):
    s = _mothers(conn, cfg, fixtures)
    _scene(conn, s, [{"temp_id": "new_1", "name_ja": "母親"}], [(s["line"], "new_1")])
    state = json.loads(get_scene(conn, s["book"].id, s["scenes"][1]["id"])["state_json"])
    state["uncertainties"] = [{"unit_ids": [s["intro"]["id"]], "question": "この手紙を書いたのは誰か"}]
    conn.execute("UPDATE scenes SET state_json=? WHERE id=?", (json.dumps(state, ensure_ascii=False),
                                                               s["scenes"][1]["id"]))
    item = next(i for i in verify.collect(conn, s["book"].id) if i["kind"] == "question")
    assert s["b"] in item["candidates"]  # known by then, though not in the scene
