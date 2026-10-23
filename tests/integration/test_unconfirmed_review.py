"""ADR-103 review round: a card marked as maybe several cards, a period card and a reveal, merged and read again."""

import json
from pathlib import Path

from ncte.analysis import verify
from ncte.analysis.scene import apply_scene_analysis, get_scene
from ncte.gui import data
from ncte.library import import_book
from ncte.llm.schemas import SceneAnalysisOutput, VerifyOutput
from ncte.memory import repository as repo
from ncte.memory import visibility as vis

from .test_unconfirmed_cards import _card, _past_mother, _read, _setup, _speaker


def test_the_page_shows_the_cards_a_card_may_be_only_from_where_they_are_known(conn, cfg, fixtures):
    """No card seen before the scene has 「来訪者」, two later cards do: the new card's possible matches carry where each
    is first known, and the page shows a match only once the reading position reaches it."""
    book, _ = import_book(conn, cfg, fixtures["basic"])
    later = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq DESC LIMIT 1", (book.id,)).fetchone()
    tail = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal DESC LIMIT 2",
                        (book.id, later["id"])).fetchall()
    ev = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%来訪者%'",
                      (book.id, later["id"])).fetchone()
    ids = [repo.upsert_character(conn, book.id, n, name_zh=None, aliases=["来訪者"], unit_id=t["id"],
                                 ordinal=t["ordinal"], origin="test")[0] for n, t in (("乙の母", tail[0]), ("丙の母", tail[1]))]
    made = _read(conn, book, later, ev, aliases_ja=["来訪者"])["new_1"]
    entry = data.characters(conn, book)["characters"][made]
    assert sorted(entry["may_be"], key=lambda m: m["id"]) == sorted(
        [{"id": i, "from": t["ordinal"]} for i, t in zip(ids, tail, strict=True)], key=lambda m: m["id"])
    assert entry["from"] < min(m["from"] for m in entry["may_be"])
    js = (Path(data.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "(p.may_be || []).filter(m => m.from <= upto)" in js  # cut by the reading position on the page


def test_a_name_revealed_for_a_past_lifes_card_keeps_its_time(conn, cfg, fixtures):
    """リリア revealed as the past life's 「母親」: the named card keeps the time (never a present person found by the
    words), the page still shows the time, and reading the past life's scene again finds that card, not a new one."""
    s = _past_mother(conn, cfg, fixtures)
    book, s1, last = s["book"], s["scenes"][1], s["scenes"][-1]
    past = _read(conn, book, s1, s["intro"], s["line"], other_person=True, period="前世")["new_1"]
    named = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%リリア%' ORDER BY "
                         "ordinal LIMIT 1", (book.id, last["id"])).fetchone()
    cid = _read(conn, book, last, named, name_ja="リリア", same_as_ref=past)["new_1"]
    c = _card(conn, cid)
    assert c["canonical_name"] == "リリア" and repo.period_of(c) == "前世"
    assert repo.card_label(c) == "リリア（前世）"
    assert cid not in repo.alias_map(repo.characters(conn, book.id)).values()
    n = conn.execute("SELECT COUNT(*) FROM characters WHERE book_id=?", (book.id,)).fetchone()[0]
    assert _read(conn, book, s1, s["intro"], s["line"], other_person=True, period="前世")["new_1"] == cid
    assert conn.execute("SELECT COUNT(*) FROM characters WHERE book_id=?", (book.id,)).fetchone()[0] == n
    assert _read(conn, book, s1, s["intro"])["new_1"] == s["mother"]  # 「母親」 alone: the present one


def _ask(conn, book_id, answers):  # (ref, answer) → every item answered in one pass, citing a paragraph
    items = verify.collect(conn, book_id)
    by = {i["ref"]: i for i in items}
    parsed = VerifyOutput.model_validate({"items": [
        {"ref": r, "answer": a, "reason": "x", "evidence": [next(iter(by[r]["_codes"]))]} for r, a in answers]})
    left: list[str] = []
    return verify.apply(conn, book_id, items, parsed, lambda *a: None, unapplied=left), left


def test_the_card_named_by_a_reveal_survives_a_same_person_merge_with_its_name_hidden_until_the_reveal(conn, cfg,
                                                                                                    fixtures):
    s = _setup(conn, cfg, fixtures)
    book, last = s["book"], s["scenes"][-1]
    named = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%リリア%' ORDER BY "
                         "ordinal LIMIT 1", (book.id, last["id"])).fetchone()
    cid = _read(conn, book, last, named, name_ja="リリア", same_as_ref=s["p"])["new_1"]
    assert set(repo.may_be(_card(conn, cid))) == {s["a"], s["b"]}
    stats, _ = _ask(conn, book.id, [(f"s:{cid}", s["a"])])
    assert stats["same_person_merged"] == 1
    assert _card(conn, s["a"]) is None  # the card known only by 「母親」 folds into the named one
    c = _card(conn, cid)
    assert c["canonical_name"] == "リリア" and c["label"] == "" and "母親" in json.loads(c["aliases_json"])
    assert _speaker(conn, s["line"]["id"]) == cid
    assert vis.character_view(conn, book.id, c, named["ordinal"] - 1)["name_ja"] == "母親"
    assert vis.character_view(conn, book.id, c, named["ordinal"])["name_ja"] == "リリア"
    assert repo.upsert_character(conn, book.id, "リリア", name_zh=None, aliases=[], unit_id=named["id"],
                                 ordinal=named["ordinal"], origin="test")[0] == cid


def test_a_labelled_card_absorbing_a_named_card_takes_its_name_and_loses_its_label(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    unit = conn.execute("SELECT * FROM units WHERE book_id=? ORDER BY ordinal DESC LIMIT 1", (book.id,)).fetchone()
    z, _ = repo.upsert_character(conn, book.id, "ゼナ", name_zh="婕娜", aliases=[], unit_id=unit["id"],
                                 ordinal=unit["ordinal"], origin="test")
    repo.merge_characters(conn, s["p"], z)
    p = _card(conn, s["p"])
    assert (p["canonical_name"], p["label"], p["display_name_zh"]) == ("ゼナ", "", "婕娜")
    assert "母親" in json.loads(p["aliases_json"])
    assert repo.upsert_character(conn, book.id, "ゼナ", name_zh=None, aliases=[], unit_id=unit["id"],
                                 ordinal=unit["ordinal"], origin="test")[0] == s["p"]
    assert vis.character_view(conn, book.id, p, unit["ordinal"] - 1)["name_ja"] == "母親"  # its name still later


def test_answers_in_one_pass_follow_earlier_merges_and_an_answer_with_nothing_left_to_do_stays_open(conn, cfg,
                                                                                                    fixtures):
    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    q, _ = repo.unconfirmed_card(conn, book.id, "母親", aliases=[], may_be=[s["p"]], unit_id=s["line"]["id"],
                                 ordinal=s["line"]["ordinal"], is_group=False, origin="test")
    conn.execute("UPDATE characters SET may_be_json=? WHERE id=?", (json.dumps([s["a"], s["b"], q]), s["p"]))
    stats, left = _ask(conn, book.id, [(f"s:{s['p']}", q), (f"s:{q}", s["p"])])
    assert stats["same_person_merged"] == 1  # the second answer names the card the first merged into its own
    assert left == [f"s:{q}"]
    assert not conn.execute("SELECT 1 FROM verify_answers WHERE ref=?", (f"s:{q}",)).fetchone()
    assert _card(conn, s["p"]) is None and _card(conn, q) is not None


def test_a_chain_of_answers_reaches_the_card_that_is_left(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    q, _ = repo.unconfirmed_card(conn, book.id, "母親", aliases=[], may_be=[s["p"], s["b"]], unit_id=s["line"]["id"],
                                 ordinal=s["line"]["ordinal"], is_group=False, origin="test")
    stats, left = _ask(conn, book.id, [(f"s:{s['p']}", s["a"]), (f"s:{q}", s["p"])])
    assert stats["same_person_merged"] == 2 and not left
    assert _card(conn, s["p"]) is None and _card(conn, q) is None and _card(conn, s["a"]) is not None


def _two_mothers(conn, book, scene, unit, **extra):
    out = {"pov": None, "location": None, "time": None, "participants": [], "actions": [], "props": [],
           "uncertainties": [], "utterances": [],
           "new_characters": [{"temp_id": t, "name_ja": "母親", "aliases_ja": [], "proposed_name_zh": "",
                               "evidence_unit_ids": [unit["id"]], **extra} for t in ("new_1", "new_2")]}
    refs: dict[str, str] = {}
    apply_scene_analysis(conn, book.id, get_scene(conn, book.id, scene["id"]), SceneAnalysisOutput.model_validate(out),
                         refs=refs)
    return refs


def test_two_people_read_in_one_scene_by_the_same_words_stay_two_cards_on_a_re_read(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    refs = _two_mothers(conn, s["book"], s["scenes"][1], s["intro"])
    assert refs["new_1"] != refs["new_2"]
    again = _two_mothers(conn, s["book"], s["scenes"][1], s["intro"])
    assert (again["new_1"], again["new_2"]) == (refs["new_1"], refs["new_2"])


def test_a_re_read_keeps_a_different_answer_unless_new_cards_could_be_her_and_adds_its_aliases(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    book, s1 = s["book"], s["scenes"][1]
    stats, _ = _ask(conn, book.id, [(f"s:{s['p']}", "different")])
    assert stats["same_person_different"] == 1
    _read(conn, book, s1, s["intro"], s["line"], aliases_ja=["路地の奥"])
    p = _card(conn, s["p"])
    assert not repo.may_be(p)  # the answer holds for the cards it was asked about
    assert "路地の奥" in json.loads(p["aliases_json"])
    assert conn.execute("SELECT ordinal FROM character_forms WHERE character_id=? AND form='路地の奥'",
                        (s["p"],)).fetchone()[0] == s["intro"]["ordinal"]
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (book.id, s["scenes"][0]["id"])).fetchone()
    c, _ = repo.upsert_character(conn, book.id, "継母", name_zh=None, aliases=["母親"], unit_id=first["id"],
                                 ordinal=first["ordinal"], origin="test")
    _read(conn, book, s1, s["intro"], s["line"])
    assert repo.may_be(_card(conn, s["p"])) == [c]  # someone not asked about: asked now


def test_a_glossary_entity_is_never_bound_to_a_card_known_only_by_other_cards_words(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    cid, _ = repo.unconfirmed_card(conn, book.id, "黒衣の女", aliases=[], may_be=[s["a"]], unit_id=s["intro"]["id"],
                                   ordinal=s["intro"]["ordinal"], is_group=False, origin="test")
    repo.ensure_glossary(conn, book.id, "黒衣の女", "黑衣女子", category="character", origin="test", first_unit_id=None)
    ent = conn.execute("SELECT e.character_id FROM entities e JOIN glossary g ON g.entity_id=e.id WHERE g.source_term="
                       "'黒衣の女'").fetchone()
    assert ent[0] is None and cid


def test_a_merge_rewriting_a_mark_bumps_the_marked_card(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    conn.execute("UPDATE characters SET updated_at='2000-01-01T00:00:00Z' WHERE id=?", (s["p"],))
    repo.merge_characters(conn, s["a"], s["b"])
    p = _card(conn, s["p"])
    assert repo.may_be(p) == [s["a"]] and p["updated_at"] != "2000-01-01T00:00:00Z"


def _reveal(conn, s, ref):
    book, last = s["book"], s["scenes"][-1]
    named = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%リリア%' ORDER BY "
                         "ordinal LIMIT 1", (book.id, last["id"])).fetchone()
    return _read(conn, book, last, named, name_ja="リリア", same_as_ref=ref)["new_1"], named


def _name_at(entry, at):
    return [n for n in entry["names"] if n["from"] <= at][-1]


def test_the_page_names_every_card_as_known_at_the_reading_position(conn, cfg, fixtures):
    """リリア revealed late for the 「母親」 card: before the reveal the page names the card (and the same card among
    another card's possible matches) 「母親」, from the reveal on 「リリア」."""
    s = _setup(conn, cfg, fixtures)
    cid, named = _reveal(conn, s, s["p"])
    entry = data.characters(conn, s["book"])["characters"][cid]
    early, late = _name_at(entry, s["line"]["ordinal"]), _name_at(entry, named["ordinal"])
    assert early["ja"] == "母親" and "リリア" not in early["aka"] and late["ja"] == "リリア"
    assert {m["id"] for m in entry["may_be"]} == {s["a"], s["b"]}  # a named card still marked is shown so
    js = (Path(data.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "nm: cid => d.characters[cid] ? cardName(d.characters[cid], upto) : cid" in js


def test_the_page_shows_the_time_of_a_named_card_of_another_time(conn, cfg, fixtures):
    s = _past_mother(conn, cfg, fixtures)
    past = _read(conn, s["book"], s["scenes"][1], s["intro"], s["line"], other_person=True, period="前世")["new_1"]
    cid, _ = _reveal(conn, s, past)
    assert data.characters(conn, s["book"])["characters"][cid]["tag"] == "前世"


def test_a_re_read_after_a_merge_finds_the_card_that_stayed(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    book, s1 = s["book"], s["scenes"][1]
    _ask(conn, book.id, [(f"s:{s['p']}", s["a"])])
    n = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
    assert _read(conn, book, s1, s["intro"], s["line"])["new_1"] == s["a"]
    assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == n
    assert _speaker(conn, s["line"]["id"]) == s["a"] and not repo.may_be(_card(conn, s["a"]))


def test_a_re_read_after_a_reveal_finds_the_named_card_and_keeps_its_answers(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    book, s1 = s["book"], s["scenes"][1]
    _ask(conn, book.id, [(f"s:{s['p']}", "different")])
    cid, _ = _reveal(conn, s, s["p"])
    c = _card(conn, cid)
    assert set(json.loads(c["not_same_json"])) == {s["a"], s["b"]} and not repo.may_be(c)
    assert _read(conn, book, s1, s["intro"], s["line"])["new_1"] == cid
    assert not repo.may_be(_card(conn, cid))


def test_a_card_is_never_its_own_candidate_and_no_empty_mark_is_left(conn, cfg, fixtures):
    """Only later cards have 「来訪者」: answered "different", the re-read leaves no mark — not even itself."""
    book, _ = import_book(conn, cfg, fixtures["basic"])
    later = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq DESC LIMIT 1", (book.id,)).fetchone()
    tail = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal DESC LIMIT 2",
                        (book.id, later["id"])).fetchall()
    ev = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%来訪者%'",
                      (book.id, later["id"])).fetchone()
    for n, t in (("乙の母", tail[0]), ("丙の母", tail[1])):
        repo.upsert_character(conn, book.id, n, name_zh=None, aliases=["来訪者"], unit_id=t["id"], ordinal=t["ordinal"],
                              origin="test")
    made = _read(conn, book, later, ev, aliases_ja=["来訪者"])["new_1"]
    _ask(conn, book.id, [(f"s:{made}", "different")])
    assert _read(conn, book, later, ev, aliases_ja=["来訪者"])["new_1"] == made
    assert repo.may_be(_card(conn, made)) == []


def test_a_name_finds_neither_a_card_of_another_time_nor_one_known_only_by_others_words(conn, cfg, fixtures):
    from ncte.memory.address import name_forms
    from ncte.memory.entities import character_for_form

    s = _past_mother(conn, cfg, fixtures)
    book = s["book"]
    past = _read(conn, book, s["scenes"][1], s["intro"], s["line"], other_person=True, period="前世")["new_1"]
    cid, named = _reveal(conn, s, past)
    assert repo.period_of(_card(conn, cid)) == "前世"
    assert character_for_form(conn, book.id, "リリア") is None
    repo.ensure_glossary(conn, book.id, "リリア", "莉莉亞", category="character", origin="test", first_unit_id=None)
    ent = conn.execute("SELECT e.character_id FROM entities e JOIN glossary g ON g.entity_id=e.id WHERE "
                       "g.source_term='リリア'").fetchone()
    assert ent[0] is None
    assert "リリア" not in name_forms(conn, book.id)[0]
    assert repo.upsert_character(conn, book.id, "リリア", name_zh=None, aliases=[], unit_id=named["id"],
                                 ordinal=named["ordinal"], origin="test")[0] != cid
