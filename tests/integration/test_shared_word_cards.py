"""ADR-111: two cards the model gives one word that names someone (a name, a nickname, a description) may be one
person — 《悪魔公女》's demon was made a card twice in two requests, both with 「執事悪魔」, and nothing asked; the
later card is marked as maybe the other for the second look. A title (「母上」) is anyone in the role: not asked.
A card's speech profile shown to a request is of its lines before the request, placed in the series."""

import json

from ncte.memory import repository as repo
from ncte.memory import visibility as vis
from ncte.memory.db import utcnow

from .test_card_name import _book, _new, _read

TEXTS = ["　額に傷を持つ執事悪魔が現れた。", "　古い個体――執事悪魔は笑った。"]


def _may_be(conn, cid):
    return json.loads(conn.execute("SELECT may_be_json FROM characters WHERE id=?", (cid,)).fetchone()[0])


def test_a_new_card_with_a_known_card_s_description_may_be_that_person(conn, cfg, fixtures):
    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, TEXTS)
    a, _ = _read(conn, book, scenes[0], new_characters=[_new("執事悪魔", u1, ["執事悪魔"], [("執事悪魔", "description")])])
    b, _ = _read(conn, book, scenes[1], new_characters=[_new("古い個体", u2, ["古い個体", "執事悪魔"],
                                                             [("古い個体", "description"), ("執事悪魔", "description")])])
    assert b["new_1"] != a["new_1"]
    assert _may_be(conn, b["new_1"]) == [a["new_1"]] and _may_be(conn, a["new_1"]) == []


def test_another_form_a_known_card_has_marks_the_later_card(conn, cfg, fixtures):
    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, TEXTS)
    a, _ = _read(conn, book, scenes[0], new_characters=[_new("執事悪魔", u1, ["執事悪魔"], [("執事悪魔", "description")])])
    b, _ = _read(conn, book, scenes[1], new_characters=[_new("古い個体", u2, ["古い個体"], [("古い個体", "description")])])
    _read(conn, book, scenes[1], other_forms=[{"ref": a["new_1"], "ja": "古い個体", "kind": "description",
                                               "evidence_unit_ids": [u2["id"]]}])
    assert _may_be(conn, b["new_1"]) == [a["new_1"]]


def test_a_title_two_cards_have_is_not_asked(conn, cfg, fixtures):
    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, ["　母上のエマが笑った。", "　母上のリサが泣いた。"])
    a, _ = _read(conn, book, scenes[0], new_characters=[_new("エマ", u1, ["エマ", "母上"], [("エマ", "name"),
                                                                                            ("母上", "title")])])
    b, _ = _read(conn, book, scenes[1], new_characters=[_new("リサ", u2, ["リサ", "母上"], [("リサ", "name"),
                                                                                            ("母上", "title")])])
    assert _may_be(conn, b["new_1"]) == [] and _may_be(conn, a["new_1"]) == []


def test_the_speech_profile_shown_is_of_the_lines_before(conn, cfg, fixtures, tmp_path):
    from tests.integration.test_series import series_of_two

    b1, b2 = series_of_two(conn, cfg, fixtures, tmp_path)
    u1 = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1 OFFSET 3", (b1.id,)).fetchone()
    u2 = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1 OFFSET 3", (b2.id,)).fetchone()
    cid, _ = repo.upsert_character(conn, b1.id, "ミナ", name_zh=None, aliases=[], unit_id=u1["id"], ordinal=0,
                                   origin="test")
    for qid, b, u, said in (("x1", b1, u1, "「僕は行くよ」"), ("x2", b2, u2, "「わたくしは参りますわ」")):
        conn.execute("INSERT INTO utterances(id, book_id, unit_id, quote_index, quote_start, quote_end, quote_text, "
                     "status, resolution_source, updated_at) VALUES (?,?,?,0,0,1,?,'explicit','test',?)",
                     (qid, b.id, u["id"], said, utcnow()))
        repo.add_speech_example(conn, cid, u["id"], qid, said, "explicit")
    repo.refresh_profiles(conn, b1.id)
    assert vis.profile_at(conn, cid, u1["ordinal"] + 1, b1.id)["first_person"] == ["僕"]
    assert vis.profile_at(conn, cid, u1["ordinal"], b1.id) == {}
    assert set(vis.profile_at(conn, cid, u2["ordinal"] + 1, b2.id)["first_person"]) == {"僕", "わたくし"}


def _desc(name, unit, temp="new_1", **kw):
    return {**_new(name, unit, [name, "執事悪魔"], [(name, "description"), ("執事悪魔", "description")]),
            "temp_id": temp, **kw}


def test_every_card_with_the_word_is_asked_about_and_two_new_cards_in_one_answer_too(conn, cfg, fixtures):
    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, ["　額に傷を持つ執事悪魔と古い個体の執事悪魔。",
                                                         "　黒い執事悪魔が笑った。"])
    a, _ = _read(conn, book, scenes[0], new_characters=[_desc("額に傷を持つ", u1), _desc("古い個体", u1, "new_2")])
    assert _may_be(conn, a["new_2"]) == [a["new_1"]]
    c, _ = _read(conn, book, scenes[1], new_characters=[_desc("黒い", u2)])
    assert set(_may_be(conn, c["new_1"])) == {a["new_1"], a["new_2"]}


def test_a_group_or_a_card_of_another_time_with_the_word(conn, cfg, fixtures):
    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, TEXTS)
    a, _ = _read(conn, book, scenes[0], new_characters=[_new("執事悪魔", u1, ["執事悪魔"], [("執事悪魔", "description")])])
    b, _ = _read(conn, book, scenes[1], new_characters=[_desc("古い個体", u2, group=True),
                                                        _desc("古い", u2, "new_2", period="前世")])
    assert _may_be(conn, b["new_1"]) == []  # a group is not one of the people
    assert a["new_1"] in _may_be(conn, b["new_2"])  # the model's time is its reading: still asked


def test_the_second_look_is_shown_where_the_word_was_given(conn, cfg, fixtures):
    from ncte.analysis.verify import _names, same_person_items

    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, TEXTS)
    a, _ = _read(conn, book, scenes[0], new_characters=[_new("執事悪魔", u1, ["執事悪魔"], [("執事悪魔", "description")])])
    b, _ = _read(conn, book, scenes[1], new_characters=[_desc("古い個体", u2)])
    item = next(i for i in same_person_items(conn, book.id, _names(conn, book.id)) if i["ref"] == f"s:{b['new_1']}")
    assert a["new_1"] in item["candidates"]
    assert any("古い個体――執事悪魔" in p["text"] for p in item["linked_at"])


def test_a_card_of_another_time_read_again_is_still_asked_about(conn, cfg, fixtures):
    book, scenes, (u1, u2) = _book(conn, cfg, fixtures, TEXTS)
    a, _ = _read(conn, book, scenes[0], new_characters=[_new("執事悪魔", u1, ["執事悪魔"], [("執事悪魔", "description")])])
    answer = [_desc("古い個体", u2, period="前世")]
    b, _ = _read(conn, book, scenes[1], new_characters=answer)
    again, _ = _read(conn, book, scenes[1], new_characters=answer)
    assert again["new_1"] == b["new_1"] and a["new_1"] in _may_be(conn, b["new_1"])


def test_another_form_a_card_of_another_time_has_is_asked_about_not_linked(conn, cfg, fixtures):
    book, scenes, (u1, u2, u3) = _book(conn, cfg, fixtures, [*TEXTS, "　執事悪魔がまた笑った。"])
    a, _ = _read(conn, book, scenes[0], new_characters=[{**_desc("額に傷を持つ", u1), "period": "前世"}])
    b, _ = _read(conn, book, scenes[1], new_characters=[_new("古い個体", u2, ["古い個体"], [("古い個体", "description")])])
    _read(conn, book, scenes[2], other_forms=[{"ref": b["new_1"], "ja": "執事悪魔", "kind": "description",
                                               "evidence_unit_ids": [u3["id"]]}])
    assert a["new_1"] in _may_be(conn, b["new_1"])
    assert not conn.execute("SELECT 1 FROM entity_links WHERE term='執事悪魔' AND status='accepted'").fetchone()


def test_where_the_word_was_given_follows_a_merge_and_writes_no_name_link(conn, cfg, fixtures):
    from ncte.analysis.verify import _names, same_person_items

    book, scenes, (u1, u2, u3) = _book(conn, cfg, fixtures, [*TEXTS, "　執事悪魔――ミナが笑った。"])
    a, _ = _read(conn, book, scenes[0], new_characters=[_new("執事悪魔", u1, ["執事悪魔"], [("執事悪魔", "description")])])
    b, _ = _read(conn, book, scenes[1], new_characters=[_desc("古い個体", u2)])
    _read(conn, book, scenes[2], new_characters=[{**_new("ミナ", u3), "same_as_ref": a["new_1"]}])  # named: merged
    mina = conn.execute("SELECT id FROM characters WHERE canonical_name='ミナ'").fetchone()[0]
    assert mina in _may_be(conn, b["new_1"])
    item = next(i for i in same_person_items(conn, book.id, _names(conn, book.id)) if i["ref"] == f"s:{b['new_1']}")
    assert any("古い個体――執事悪魔" in p["text"] for p in item["linked_at"])
    assert not conn.execute("SELECT 1 FROM entity_links WHERE status='candidate'").fetchone()
