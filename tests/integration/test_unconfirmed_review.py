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


def test_a_reveal_naming_a_card_that_already_has_the_name_merges_into_it(conn, cfg, fixtures):
    """リリア already has a card, known from the first scene: revealed as the 「母親」 card, that card is merged into the
    named one (lines, marks), and リリア stays visible from where it was already known, not only from the reveal."""
    s = _setup(conn, cfg, fixtures)
    book, first_scene = s["book"], s["scenes"][0]
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (book.id, first_scene["id"])).fetchone()
    lilia, _ = repo.upsert_character(conn, book.id, "リリア", name_zh="莉莉亞", aliases=[], unit_id=first["id"],
                                     ordinal=first["ordinal"], origin="test")
    cid, named = _reveal(conn, s, s["p"])
    assert cid == lilia and _card(conn, s["p"]) is None
    assert _speaker(conn, s["line"]["id"]) == lilia
    assert set(repo.may_be(_card(conn, lilia))) == {s["a"], s["b"]}
    assert vis.character_view(conn, book.id, _card(conn, lilia), first["ordinal"])["name_ja"] == "リリア"


def _reveal_pairs(conn, s, pairs):
    """One reading of the last scene naming existing cards, each 'the same as' another (the model's answer)."""
    book, last = s["book"], s["scenes"][-1]
    unit = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND plain_text LIKE '%リリア%' ORDER BY "
                        "ordinal LIMIT 1", (book.id, last["id"])).fetchone()
    out = {"pov": None, "location": None, "time": None, "participants": [], "actions": [], "props": [],
           "uncertainties": [], "utterances": [],
           "new_characters": [{"temp_id": f"new_{i}", "name_ja": name, "aliases_ja": [], "proposed_name_zh": "",
                               "evidence_unit_ids": [unit["id"]], "same_as_ref": ref}
                              for i, (name, ref) in enumerate(pairs)]}
    refs: dict[str, str] = {}
    apply_scene_analysis(conn, book.id, get_scene(conn, book.id, last["id"]), SceneAnalysisOutput.model_validate(out),
                         refs=refs)
    return refs


def _named(conn, s, name):
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (s["book"].id, s["scenes"][0]["id"])).fetchone()
    return repo.upsert_character(conn, s["book"].id, name, name_zh=None, aliases=[], unit_id=first["id"],
                                 ordinal=first["ordinal"], origin="test")[0]


def test_reveals_in_one_reading_follow_each_others_merges_and_never_crash(conn, cfg, fixtures):
    """A is said to be B and B to be A in the same answer: one card, no error. A chain (B is the card known by 「母親」,
    A is B): everything ends on one card."""
    s = _setup(conn, cfg, fixtures)
    a = _named(conn, s, "リリア")
    b = _named(conn, s, "王女")
    conn.execute("UPDATE units SET plain_text=plain_text || '王女。' WHERE id=(SELECT id FROM units WHERE book_id=? AND "
                 "scene_id=? AND plain_text LIKE '%リリア%' ORDER BY ordinal LIMIT 1)", (s["book"].id, s["scenes"][-1]["id"]))
    refs = _reveal_pairs(conn, s, [("リリア", b), ("王女", a)])
    left = {x for x in (a, b) if _card(conn, x) is not None}
    assert len(left) == 1 and refs["new_0"] == refs["new_1"] == next(iter(left))


def test_a_chain_of_reveals_in_one_reading_ends_on_one_card(conn, cfg, fixtures):
    s2 = _setup(conn, cfg, fixtures)
    c = _named(conn, s2, "リリア")
    d = _named(conn, s2, "王女")
    conn.execute("UPDATE units SET plain_text=plain_text || '王女。' WHERE id=(SELECT id FROM units WHERE book_id=? AND "
                 "scene_id=? AND plain_text LIKE '%リリア%' ORDER BY ordinal LIMIT 1)", (s2["book"].id, s2["scenes"][-1]["id"]))
    refs = _reveal_pairs(conn, s2, [("リリア", d), ("王女", s2["p"])])  # 王女 is merged away before its own reveal
    kept = {x for x in (c, d, s2["p"]) if _card(conn, x) is not None}
    assert len(kept) == 1 and refs["new_0"] == refs["new_1"] == next(iter(kept))
    assert _speaker(conn, s2["line"]["id"]) == next(iter(kept))


def test_the_command_line_finds_a_card_by_name_only_by_the_rule_and_never_picks_among_several(conn, cfg, fixtures):
    import pytest

    from ncte.cli_memory import resolve_character
    from ncte.errors import NCTEError

    s = _past_mother(conn, cfg, fixtures)
    book = s["book"]
    past, _ = repo.other_time_card(conn, book.id, "前母", "前世", unit_id=s["intro"]["id"],
                                   ordinal=s["intro"]["ordinal"], origin="test")
    with pytest.raises(NCTEError, match=past):
        resolve_character(conn, book.id, "前母")  # only a card of another time: chosen by id
    assert resolve_character(conn, book.id, past) == past
    assert resolve_character(conn, book.id, "母親") == s["mother"]
    two, _ = repo.upsert_character(conn, book.id, "継母", name_zh="母親", aliases=[], unit_id=s["intro"]["id"],
                                   ordinal=s["intro"]["ordinal"], origin="test")
    with pytest.raises(NCTEError, match=two):
        resolve_character(conn, book.id, "母親")  # two cards called so: the user chooses


def test_unlinking_an_alias_takes_it_only_from_the_card_the_entity_is_bound_to(conn, cfg, fixtures):
    from ncte.memory.entities import link, unlink

    s = _past_mother(conn, cfg, fixtures)
    book = s["book"]
    first = conn.execute("SELECT * FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()
    mina, _ = repo.upsert_character(conn, book.id, "ミナ", name_zh="米娜", aliases=["ミナっち"], unit_id=first["id"],
                                    ordinal=first["ordinal"], origin="test")
    past, _ = repo.other_time_card(conn, book.id, "ミナ", "前世", unit_id=first["id"], ordinal=first["ordinal"],
                                   origin="test")
    conn.execute("UPDATE characters SET aliases_json=? WHERE id=?", (json.dumps(["ミナっち"]), past))
    repo.ensure_glossary(conn, book.id, "ミナ", "米娜", category="character", origin="test", first_unit_id=None)
    repo.ensure_glossary(conn, book.id, "ミナっち", "小米", category="character", origin="test", first_unit_id=None)
    link(conn, book.id, "ミナっち", "ミナ", "nickname", origin="test")
    unlink(conn, book.id, "ミナっち")
    assert "ミナっち" not in json.loads(_card(conn, mina)["aliases_json"])
    assert json.loads(_card(conn, past)["aliases_json"]) == ["ミナっち"]


def test_the_codex_keys_by_card_and_shows_a_time_as_the_page_does(conn, cfg, fixtures):
    from ncte.reports.codex import build_index, build_relations

    s = _past_mother(conn, cfg, fixtures)
    book = s["book"]
    past = _read(conn, book, s["scenes"][1], s["intro"], s["line"], other_person=True, period="前世")["new_1"]
    nodes = {n["id"]: n for n in build_relations(conn, book.id)["nodes"]}
    assert nodes[past]["label"] == "母親（前世）" and past != s["mother"]
    only, _ = repo.other_time_card(conn, book.id, "前母", "前世", unit_id=s["intro"]["id"],
                                   ordinal=s["intro"]["ordinal"], origin="test")
    conn.execute("UPDATE characters SET speech_profile_json=? WHERE id=?",
                 (json.dumps({"n_examples": 3, "first_person": ["私"]}), only))
    conn.execute("UPDATE units SET plain_text='　前母が来た。' WHERE id=?", (s["intro"]["id"],))
    repo.ensure_glossary(conn, book.id, "前母", "前母", category="character", origin="test", first_unit_id=None)
    entry = next(e for e in build_index(conn, book.id) if e["name_ja"] == "前母")
    assert "speech" not in entry


def test_backfill_groups_only_the_aliases_of_a_card_a_name_finds(conn, cfg, fixtures):
    from ncte.memory.entities import backfill_entities

    s = _past_mother(conn, cfg, fixtures)
    book = s["book"]
    past, _ = repo.other_time_card(conn, book.id, "母親", "前世", unit_id=s["intro"]["id"],
                                   ordinal=s["intro"]["ordinal"], origin="test")
    conn.execute("UPDATE characters SET aliases_json=? WHERE id=?", (json.dumps(["旧母"]), past))
    repo.ensure_glossary(conn, book.id, "旧母", "舊母", category="character", origin="test", first_unit_id=None)
    backfill_entities(conn, book.id)
    ent = {r[0]: r[1] for r in conn.execute("SELECT source_term, entity_id FROM glossary WHERE source_term IN "
                                            "('母親','旧母')")}
    assert ent["母親"] != ent["旧母"]


def test_an_identity_answer_against_a_different_answer_of_the_same_pass_merges_nothing(conn, cfg, fixtures):
    """The new 「母親」 is answered not to be 母親 or 義母, and in the same pass 母親's identity is answered as that new
    card: the answers disagree, so nothing merges and the identity is asked again without that card (ADR-107)."""
    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    conn.execute("INSERT INTO relation_notes(book_id, scene_id, a_id, b_id, relation_zh, kind, evidence_unit_ids_json, "
                 "evidence_quote, first_ordinal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (book.id, s["scenes"][0]["id"], s["a"], s["b"], "x", "family", "[]", "", 1, "t"))  # 母親 is asked about
    p = repo.upsert_character(conn, book.id, "来訪者", name_zh=None, aliases=["母親"], unit_id=s["intro"]["id"],
                              ordinal=s["intro"]["ordinal"], origin="test")[0]
    conn.execute("UPDATE characters SET may_be_json=? WHERE id=?", (json.dumps([s["a"], s["b"]]), p))
    assert p in next(i for i in verify.collect(conn, book.id) if i["ref"] == f"i:{s['a']}")["candidates"]
    items = verify.collect(conn, book.id)
    code = next(iter(next(i for i in items if i["ref"] == f"s:{p}")["_codes"]))
    parsed = VerifyOutput.model_validate({"items": [
        {"ref": f"s:{p}", "answer": "different", "reason": "x", "evidence": [code]},
        {"ref": f"i:{s['a']}", "answer": p, "reason": "x"}]})
    left: list[str] = []
    stats = verify.apply(conn, book.id, items, parsed, lambda *a: None, unapplied=left)
    assert stats["same_person_different"] == 1 and stats["identities_contradicted"] == 1
    assert "identities_merged" not in stats and f"i:{s['a']}" in left
    assert _card(conn, s["a"]) is not None and _card(conn, p) is not None
    again = next(i for i in verify.collect(conn, book.id) if i["ref"] == f"i:{s['a']}")
    assert p not in again["candidates"] and s["b"] in again["candidates"]


def test_a_same_person_answer_reaching_a_card_it_is_not_through_a_merge_of_the_same_pass_merges_nothing(conn, cfg,
                                                                                                      fixtures):
    """Q was answered not to be 母親; the new P may be 母親 and Q may be P. One answer makes P 母親, the next makes Q
    P — that is 母親 now, which Q is not: Q stays its own card and is asked again (ADR-107, review)."""
    s = _setup(conn, cfg, fixtures)
    book, q = s["book"], s["p"]
    p = repo.upsert_character(conn, book.id, "来訪者", name_zh=None, aliases=["母親"], unit_id=s["intro"]["id"],
                              ordinal=s["intro"]["ordinal"], origin="test")[0]
    conn.execute("UPDATE characters SET may_be_json=? WHERE id=?", (json.dumps([s["a"]]), p))
    conn.execute("UPDATE characters SET may_be_json=?, not_same_json=? WHERE id=?",
                 (json.dumps([p]), json.dumps([s["a"]]), q))
    items = verify.collect(conn, book.id)
    by = {i["ref"]: i for i in items}
    parsed = VerifyOutput.model_validate({"items": [
        {"ref": f"s:{r}", "answer": x, "reason": "x", "evidence": [next(iter(by[f"s:{r}"]["_codes"]))]}
        for r, x in ((p, s["a"]), (q, p))]})
    left: list[str] = []
    stats = verify.apply(conn, book.id, items, parsed, lambda *a: None, unapplied=left)
    assert stats["same_person_merged"] == 1 and stats["identities_contradicted"] == 1 and f"s:{q}" in left
    assert _card(conn, q) is not None and _card(conn, s["a"]) is not None


def test_a_group_answer_reaching_a_group_it_is_not_through_a_merge_of_the_same_pass_merges_nothing(conn, cfg,
                                                                                                fixtures):
    """Groups G1 and G2 were answered not to be one; G3 is answered the same as G2, then G1 the same as G3 — that is
    G2 now: G1 stays its own group and is asked again (ADR-107, review)."""
    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    g1, g2, g3 = (repo.upsert_character(conn, book.id, n, name_zh=None, aliases=[], unit_id=s["intro"]["id"],
                                        ordinal=s["intro"]["ordinal"], origin="test")[0] for n in ("甲組", "乙組", "丙組"))
    conn.execute("UPDATE characters SET is_group=1 WHERE id IN (?,?,?)", (g1, g2, g3))
    conn.execute("UPDATE characters SET not_same_json=? WHERE id=?", (json.dumps([g2]), g1))
    for g in (g1, g2, g3):  # each has a member, so each is asked
        conn.execute("INSERT INTO group_members(group_id, member_id, book_id, reason, created_at) VALUES (?,?,?,?,?)",
                     (g, s["a"], book.id, "x", "t"))
    items = verify.collect(conn, book.id)
    assert g2 not in next(i for i in items if i["ref"] == f"p:{g1}")["candidates"]
    parsed = VerifyOutput.model_validate({"items": [
        {"ref": f"p:{g3}", "answer": f"same:{g2}", "reason": "x"},
        {"ref": f"p:{g1}", "answer": f"same:{g3}", "reason": "x"}]})
    left: list[str] = []
    stats = verify.apply(conn, book.id, items, parsed, lambda *a: None, unapplied=left)
    assert stats["groups_merged"] == 1 and stats["identities_contradicted"] == 1 and f"p:{g1}" in left
    assert _card(conn, g1) is not None and _card(conn, g2) is not None


def _two_mothers_called_hahaue(conn, cfg, fixtures):
    """トーネの母親 (her daughter calls her 「母上」) and 実母 (another child's dead mother, also 「母上」) — ADR-108."""
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    at = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                      (book.id, s["scenes"][0]["id"])).fetchone()  # both known before the scene that is read
    cards = {}
    for name in ("トーネの母親", "実母"):
        cards[name] = repo.upsert_character(conn, book.id, name, name_zh=None, aliases=[], unit_id=at["id"],
                                            ordinal=at["ordinal"], origin="test")[0]
        set_glossary(conn, book.id, name, name, category="character")
    set_glossary(conn, book.id, "母上", "母親大人", category="character")
    assert link(conn, book.id, "母上", "トーネの母親", "title", origin="llm", ordinal=1) == "accepted"
    conn.execute("UPDATE characters SET aliases_json=? WHERE id=?", (json.dumps(["母上"]), cards["トーネの母親"]))
    return s, cards


def test_a_shared_word_finds_no_card_and_a_scene_naming_it_merges_nothing(conn, cfg, fixtures):
    from ncte.memory.entities import character_for_form, link

    s, cards = _two_mothers_called_hahaue(conn, cfg, fixtures)
    book = s["book"]
    assert link(conn, book.id, "母上", "実母", "title", origin="llm", ordinal=2) == "shared"
    assert character_for_form(conn, book.id, "母上") is None
    conn.execute("UPDATE units SET plain_text='　路地の奥に、母上が立っていた。' WHERE id=?", (s["intro"]["id"],))
    refs = _read(conn, book, s["scenes"][1], s["intro"], name_ja="母上", same_as_ref=cards["実母"])
    assert all(_card(conn, c) is not None for c in cards.values()) and refs["new_1"] == cards["実母"]


def test_a_reverse_link_records_the_word_as_shared(conn, cfg, fixtures):
    from ncte.memory.entities import is_shared, link

    s, cards = _two_mothers_called_hahaue(conn, cfg, fixtures)
    book = s["book"]
    assert link(conn, book.id, "実母", "母上", "title", origin="llm", ordinal=2) == "shared"
    assert is_shared(conn, book.id, "母上") and not is_shared(conn, book.id, "実母")
    assert all(_card(conn, c) is not None for c in cards.values())


def test_backfill_does_not_join_two_cards_by_a_word_both_have(conn, cfg, fixtures):
    from ncte.memory.entities import backfill_entities, link, reconcile_characters

    s, cards = _two_mothers_called_hahaue(conn, cfg, fixtures)
    book = s["book"]
    link(conn, book.id, "母上", "実母", "title", origin="llm", ordinal=2)
    conn.execute("UPDATE characters SET aliases_json='[\"母上\"]' WHERE id IN (?,?)", tuple(cards.values()))
    backfill_entities(conn, book.id)
    reconcile_characters(conn, book.id)
    assert all(_card(conn, c) is not None for c in cards.values())


def test_a_link_naming_two_cards_is_asked_and_the_second_look_merges_them(conn, cfg, fixtures):
    """The model linking two cards' names may mean they are one person or only that the word names the other here
    (「母親」, the present mother's card, said of トーネの母親): the later card is marked maybe the other and the second
    look's answer merges them (ADR-108 spot check)."""
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    s = _setup(conn, cfg, fixtures)
    book, at = s["book"], s["intro"]
    ids = []
    for i, (name, form) in enumerate((("ロセト", "ロセト様"), ("銅の杖の先生", "銅の杖"))):
        ids.append(repo.upsert_character(conn, book.id, name, name_zh=None, aliases=[], unit_id=at["id"],
                                         ordinal=at["ordinal"] + i, origin="test")[0])
        set_glossary(conn, book.id, name, name, category="character")
        set_glossary(conn, book.id, form, form, category="character")
        assert link(conn, book.id, form, name, "alias", origin="llm") == "accepted"
    assert link(conn, book.id, "ロセト", "銅の杖の先生", "alias", origin="llm") == "asked"
    assert all(_card(conn, c) is not None for c in ids) and repo.may_be(_card(conn, ids[1])) == [ids[0]]
    stats, _ = _ask(conn, book.id, [(f"s:{ids[1]}", ids[0])])
    assert stats["same_person_merged"] == 1
    assert conn.execute("SELECT count(*) FROM characters WHERE canonical_name IN ('ロセト','銅の杖の先生')").fetchone()[0] == 1


def test_a_card_named_by_a_shared_word_is_not_merged_by_it(conn, cfg, fixtures):
    from ncte.memory.entities import link, reconcile_characters

    s, cards = _two_mothers_called_hahaue(conn, cfg, fixtures)
    book, at = s["book"], s["intro"]
    link(conn, book.id, "母上", "実母", "title", origin="llm", ordinal=2)
    other = repo.upsert_character(conn, book.id, "母上", name_zh=None, aliases=[], unit_id=at["id"], ordinal=at["ordinal"],
                                  origin="test")[0]
    reconcile_characters(conn, book.id)
    assert all(_card(conn, c) is not None for c in (*cards.values(), other))


def test_backfill_does_not_join_a_card_named_by_another_cards_alias(conn, cfg, fixtures):
    """No link at all: 母上 is one card's name and the other card's alias. Backfill is not the model saying they are
    one person (ADR-108, review)."""
    from ncte.memory.entities import backfill_entities, reconcile_characters
    from ncte.memory.repository import set_glossary

    s = _setup(conn, cfg, fixtures)
    book, at = s["book"], s["intro"]
    a = repo.upsert_character(conn, book.id, "母上", name_zh=None, aliases=[], unit_id=at["id"], ordinal=at["ordinal"],
                              origin="test")[0]
    b = repo.upsert_character(conn, book.id, "実母", name_zh=None, aliases=["母上"], unit_id=at["id"],
                              ordinal=at["ordinal"], origin="test")[0]
    for name in ("母上", "実母"):
        set_glossary(conn, book.id, name, name, category="character")
    backfill_entities(conn, book.id)
    reconcile_characters(conn, book.id)
    assert _card(conn, a) is not None and _card(conn, b) is not None


def test_a_link_from_a_card_named_by_a_shared_word_merges_no_one_else(conn, cfg, fixtures):
    """A card named 「母上」 shares its entity with トーネの母親; the model linking 「母上」 to リリア must not merge
    トーネの母親 with リリア (ADR-108, third review). A person's link does merge."""
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    s, cards = _two_mothers_called_hahaue(conn, cfg, fixtures)
    book, at = s["book"], s["intro"]
    conn.execute("UPDATE characters SET aliases_json='[]' WHERE id=?", (cards["トーネの母親"],))
    link(conn, book.id, "母上", "実母", "title", origin="llm", ordinal=2)
    named = repo.upsert_character(conn, book.id, "母上", name_zh=None, aliases=[], unit_id=at["id"], ordinal=at["ordinal"],
                                  origin="test")[0]
    lilia = repo.upsert_character(conn, book.id, "リリア", name_zh=None, aliases=[], unit_id=at["id"],
                                  ordinal=at["ordinal"], origin="test")[0]
    set_glossary(conn, book.id, "リリア", "莉莉亞", category="character")
    assert link(conn, book.id, "母上", "リリア", "alias", origin="llm") == "shared"
    assert all(_card(conn, c) is not None for c in (*cards.values(), named, lilia))
    assert link(conn, book.id, "母上", "リリア", "alias", origin="human") == "accepted"
    assert _card(conn, named) is None or _card(conn, lilia) is None


def test_a_shared_word_said_to_be_a_known_card_is_that_card_even_when_several_cards_have_it(conn, cfg, fixtures):
    from ncte.memory.entities import link

    s, cards = _two_mothers_called_hahaue(conn, cfg, fixtures)
    book = s["book"]
    link(conn, book.id, "母上", "実母", "title", origin="llm", ordinal=2)
    conn.execute("UPDATE characters SET aliases_json=? WHERE id=?", (json.dumps(["母上"]), s["b"]))  # 義母 too
    conn.execute("UPDATE units SET plain_text='　路地の奥に、母上が立っていた。' WHERE id=?", (s["intro"]["id"],))
    n = conn.execute("SELECT count(*) FROM characters").fetchone()[0]
    refs = _read(conn, book, s["scenes"][1], s["intro"], name_ja="母上", same_as_ref=cards["実母"])
    assert refs["new_1"] == cards["実母"] and conn.execute("SELECT count(*) FROM characters").fetchone()[0] == n


def test_a_shared_word_linked_to_a_name_without_a_card_does_not_move_the_card_it_named(conn, cfg, fixtures):
    """「母上」 already names トーネの母親 and 実母; the model linking it to リリア (only a glossary word) must not move
    トーネの母親 into リリア, or a later scene saying リリア is 実母 merges the two mothers (ADR-108, fourth review)."""
    from ncte.memory.entities import is_shared, link
    from ncte.memory.repository import set_glossary

    s, cards = _two_mothers_called_hahaue(conn, cfg, fixtures)
    book = s["book"]
    assert link(conn, book.id, "母上", "実母", "title", origin="llm", ordinal=2) == "shared"
    set_glossary(conn, book.id, "リリア", "莉莉亞", category="character")
    assert link(conn, book.id, "母上", "リリア", "alias", origin="llm", ordinal=3) == "shared"
    assert link(conn, book.id, "リリア", "母上", "alias", origin="llm", ordinal=3) == "shared"
    assert is_shared(conn, book.id, "母上") and not is_shared(conn, book.id, "リリア")
    conn.execute("UPDATE units SET plain_text='　路地の奥に、リリアが立っていた。' WHERE id=?", (s["intro"]["id"],))
    lilia = _read(conn, book, s["scenes"][1], s["intro"], name_ja="リリア", same_as_ref=cards["実母"])["new_1"]
    # リリア is 実母 (the reveal folds 実母's card into リリア's); トーネの母親 stays her own card
    assert _card(conn, cards["トーネの母親"]) is not None and lilia != cards["トーネの母親"]
    assert _card(conn, lilia) is not None


def test_a_persons_link_of_two_cards_merges_them_though_a_word_of_theirs_names_a_third(conn, cfg, fixtures):
    """Cards 母上, 実母 and 義母; 実母 and 義母 are both called 母上. A person linking 実母 to 母上 says those two cards
    are one; 母上 still names 義母 too (ADR-108, fourth review)."""
    from ncte.memory.entities import backfill_entities, is_shared, link
    from ncte.memory.repository import set_glossary

    s = _setup(conn, cfg, fixtures)
    book, at = s["book"], s["intro"]
    ids = {}
    for name, aliases in (("母上", []), ("実母", ["母上"]), ("義母", ["母上"])):
        ids[name] = repo.upsert_character(conn, book.id, name, name_zh=None, aliases=aliases, unit_id=at["id"],
                                          ordinal=at["ordinal"], origin="test")[0]
        set_glossary(conn, book.id, name, name, category="character")
    backfill_entities(conn, book.id)
    assert link(conn, book.id, "実母", "母上", "alias", origin="human") == "accepted"
    assert (_card(conn, ids["実母"]) is None) != (_card(conn, ids["母上"]) is None)
    assert _card(conn, ids["義母"]) is not None and is_shared(conn, book.id, "母上")


def _three_mothers(conn, cfg, fixtures, *, backfill=True):
    """Cards 母上, 実母 and 義母; 義母 (and 実母 when backfilled) is called 母上 too."""
    from ncte.memory.entities import backfill_entities
    from ncte.memory.repository import set_glossary

    s = _setup(conn, cfg, fixtures)
    book, at = s["book"], s["intro"]
    ids = {}
    for name, aliases in (("母上", []), ("実母", ["母上"] if backfill else []), ("義母", ["母上"])):
        ids[name] = repo.upsert_character(conn, book.id, name, name_zh=None, aliases=aliases, unit_id=at["id"],
                                          ordinal=at["ordinal"], origin="test")[0]
        set_glossary(conn, book.id, name, name, category="character")
    if backfill:
        backfill_entities(conn, book.id)
    return s, ids


def test_a_word_another_card_has_as_an_alias_linked_to_a_card_merges_nothing(conn, cfg, fixtures):
    """「母上」 is a card's name and 義母's alias (no shared record yet): the model linking it to 実母 does not merge the
    「母上」 card into 実母 (ADR-108, fifth review)."""
    from ncte.memory.entities import link

    s, ids = _three_mothers(conn, cfg, fixtures, backfill=False)
    assert link(conn, s["book"].id, "母上", "実母", "title", origin="llm", ordinal=2) == "shared"
    assert all(_card(conn, c) is not None for c in ids.values())


def test_a_persons_link_merges_the_cards_of_words_that_are_no_cards_names(conn, cfg, fixtures):
    """A person's link through a word with no card of its own (リリア, linked to 実母 before) still makes the cards one
    (ADR-108, fifth review)."""
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    s, ids = _three_mothers(conn, cfg, fixtures)
    book = s["book"]
    set_glossary(conn, book.id, "リリア", "莉莉亞", category="character")
    assert link(conn, book.id, "リリア", "実母", "alias", origin="llm") == "accepted"
    assert link(conn, book.id, "リリア", "母上", "alias", origin="human") == "accepted"
    assert (_card(conn, ids["実母"]) is None) != (_card(conn, ids["母上"]) is None)
    assert _card(conn, ids["義母"]) is not None


def test_a_persons_link_merges_cards_a_reconcile_already_folded(conn, cfg, fixtures):
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    s, ids = _three_mothers(conn, cfg, fixtures)
    book, at = s["book"], s["intro"]
    set_glossary(conn, book.id, "リリア", "莉莉亞", category="character")
    assert link(conn, book.id, "実母", "リリア", "alias", origin="llm") == "accepted"
    lilia = repo.upsert_character(conn, book.id, "リリア", name_zh=None, aliases=[], unit_id=at["id"],
                                  ordinal=at["ordinal"], origin="test")[0]
    assert link(conn, book.id, "実母", "母上", "alias", origin="human") == "accepted"
    left = [c for c in (ids["実母"], ids["母上"], lilia) if _card(conn, c) is not None]
    assert len(left) == 1 and _card(conn, ids["義母"]) is not None


def test_a_third_card_with_both_words_makes_the_link_shared(conn, cfg, fixtures):
    """先生 and 師匠 are cards, and a third card is called both: linking the two words merges nothing (ADR-108, sixth
    review). Two words of only one card, no card named by either, still link."""
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    s = _setup(conn, cfg, fixtures)
    book, at = s["book"], s["intro"]
    ids = [repo.upsert_character(conn, book.id, name, name_zh=None, aliases=aliases, unit_id=at["id"],
                                 ordinal=at["ordinal"], origin="test")[0]
           for name, aliases in (("先生", []), ("師匠", []), ("別の先生", ["先生", "師匠"]),
                                 ("ソラ", ["ソラっち", "天野ソラ"]))]
    for name in ("先生", "師匠", "別の先生", "ソラ", "ソラっち", "天野ソラ"):
        set_glossary(conn, book.id, name, name, category="character")
    assert link(conn, book.id, "先生", "師匠", "alias", origin="llm") == "shared"
    assert all(_card(conn, c) is not None for c in ids)
    assert link(conn, book.id, "ソラっち", "天野ソラ", "nickname", origin="llm") == "accepted"


def test_a_persons_link_finds_the_card_a_reveal_folded_the_word_into(conn, cfg, fixtures):
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    s = _setup(conn, cfg, fixtures)
    book, at = s["book"], s["intro"]
    ids = {}
    for name in ("実母", "リリア", "義母"):
        ids[name] = repo.upsert_character(conn, book.id, name, name_zh=None, aliases=[], unit_id=at["id"],
                                          ordinal=at["ordinal"], origin="test")[0]
        set_glossary(conn, book.id, name, name, category="character")
    repo.merge_characters(conn, ids["リリア"], ids["実母"])  # a scene reveal: 実母 is リリア
    assert link(conn, book.id, "実母", "義母", "alias", origin="human") == "accepted"
    assert (_card(conn, ids["リリア"]) is None) != (_card(conn, ids["義母"]) is None)


def _cards(conn, book, names, aliases=None):
    from ncte.memory.repository import set_glossary

    at = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal LIMIT 1",
                      (book.id,)).fetchone()
    ids = {}
    for name in names:
        ids[name] = repo.upsert_character(conn, book.id, name, name_zh=None, aliases=(aliases or {}).get(name, []),
                                          unit_id=at[0], ordinal=at[1], origin="test")[0]
        set_glossary(conn, book.id, name, name, category="character")
    return ids


def test_a_persons_link_merges_only_the_two_cards_it_means(conn, cfg, fixtures):
    """先生 linked to ロセト, then 別の先生 also called 先生: a person linking ロセト to 銅の杖の先生 leaves 別の先生 alone
    (ADR-108, seventh review)."""
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    ids = _cards(conn, book, ("ロセト", "銅の杖の先生", "別の先生"))
    set_glossary(conn, book.id, "先生", "老師", category="character")
    assert link(conn, book.id, "先生", "ロセト", "title", origin="llm") == "accepted"
    at = s["intro"]
    repo.upsert_character(conn, book.id, "別の先生", name_zh=None, aliases=["先生"], unit_id=at["id"],
                          ordinal=at["ordinal"], origin="test")
    assert link(conn, book.id, "ロセト", "銅の杖の先生", "alias", origin="human") == "accepted"
    assert (_card(conn, ids["ロセト"]) is None) != (_card(conn, ids["銅の杖の先生"]) is None)
    assert _card(conn, ids["別の先生"]) is not None


def test_a_persons_link_to_a_shared_word_whose_card_was_folded(conn, cfg, fixtures):
    from ncte.memory.entities import add_sense, link

    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    ids = _cards(conn, book, ("実母", "リリア", "義母", "別人"))
    add_sense(conn, book.id, "実母", "別人", origin="test")
    repo.merge_characters(conn, ids["リリア"], ids["実母"])
    assert link(conn, book.id, "義母", "実母", "alias", origin="human") == "accepted"
    assert (_card(conn, ids["リリア"]) is None) != (_card(conn, ids["義母"]) is None)
    assert _card(conn, ids["別人"]) is not None


def test_a_nickname_of_one_card_links_to_a_new_full_name(conn, cfg, fixtures):
    from ncte.memory.entities import backfill_entities, link
    from ncte.memory.repository import set_glossary

    s = _setup(conn, cfg, fixtures)
    book = s["book"]
    _cards(conn, book, ("ソラ",), {"ソラ": ["ソラっち"]})
    for w in ("ソラっち", "天野ソラ"):
        set_glossary(conn, book.id, w, w, category="character")
    backfill_entities(conn, book.id)
    assert link(conn, book.id, "ソラっち", "天野ソラ", "full", origin="llm") == "accepted"


def test_a_link_of_a_nameless_cards_word_to_another_card_merges_nothing(conn, cfg, fixtures):
    """Spot-check regression (ADR-108): the present mother's card is named 「母親」 while she has no name; the memory
    update links 「母親 → トーネの母親」 in the baker's scene. The two mothers stay two cards, the later one marked as
    maybe the other; the second look's 'different' keeps them apart with their lines."""
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    book, _ = import_book(conn, cfg, fixtures["basic"])  # no other card is called 「母親」
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal", (book.id,)).fetchall()
    present = repo.upsert_character(conn, book.id, "母親", name_zh=None, aliases=["お母様"], unit_id=u[0][0],
                                    ordinal=u[0][1], origin="test")[0]
    baker = repo.upsert_character(conn, book.id, "トーネの母親", name_zh=None, aliases=[], unit_id=u[2][0],
                                  ordinal=u[2][1], origin="test")[0]
    for name in ("母親", "トーネの母親"):
        set_glossary(conn, book.id, name, name, category="character")
    assert link(conn, book.id, "母親", "トーネの母親", "alias", origin="llm", ordinal=u[3][1]) == "asked"
    assert _card(conn, present) is not None and _card(conn, baker) is not None
    stats, _ = _ask(conn, book.id, [(f"s:{baker}", "different")])
    assert stats["same_person_different"] == 1
    assert _card(conn, present) is not None and _card(conn, baker) is not None and repo.not_same(conn, baker, present)


def test_a_name_link_question_shows_where_the_link_was_read_and_is_asked_again_when_linked_again(conn, cfg, fixtures):
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    book, _ = import_book(conn, cfg, fixtures["basic"])
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal", (book.id,)).fetchall()
    ids = [repo.upsert_character(conn, book.id, n, name_zh=None, aliases=[], unit_id=u[i][0], ordinal=u[i][1],
                                 origin="test")[0] for i, n in ((0, "ロセト"), (1, "銅の杖の先生"))]
    for n in ("ロセト", "銅の杖の先生"):
        set_glossary(conn, book.id, n, n, category="character")
    assert link(conn, book.id, "ロセト", "銅の杖の先生", "alias", origin="llm", unit_id=u[-1][0],
                ordinal=u[-1][1]) == "asked"
    item = next(i for i in verify.collect(conn, book.id) if i["ref"] == f"s:{ids[1]}")
    assert u[-1][0] in item["_codes"].values() and item["linked_at"]
    _ask(conn, book.id, [(f"s:{ids[1]}", "unknown")])
    assert not any(i["ref"] == f"s:{ids[1]}" for i in verify.collect(conn, book.id))  # answered: not asked again
    # read again at another paragraph: asked again, shown both places
    link(conn, book.id, "ロセト", "銅の杖の先生", "alias", origin="llm", unit_id=u[-2][0], ordinal=u[-2][1])
    again = next(i for i in verify.collect(conn, book.id) if i["ref"] == f"s:{ids[1]}")
    assert {u[-1][0], u[-2][0]} <= set(again["_codes"].values())


def test_a_link_of_two_card_names_one_also_through_its_entity_is_shared(conn, cfg, fixtures):
    """「母親」 is a card and, through a link, the word of トーネの母親's entity: linking it to 実母 is a shared word,
    not a question about two cards (ADR-108, ninth review)."""
    from ncte.memory.entities import link
    from ncte.memory.repository import set_glossary

    book, _ = import_book(conn, cfg, fixtures["basic"])
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal", (book.id,)).fetchall()
    set_glossary(conn, book.id, "トーネの母親", "托涅的母親", category="character")
    set_glossary(conn, book.id, "母親", "母親", category="character")
    baker = repo.upsert_character(conn, book.id, "トーネの母親", name_zh=None, aliases=[], unit_id=u[0][0],
                                  ordinal=u[0][1], origin="test")[0]
    assert link(conn, book.id, "母親", "トーネの母親", "alias", origin="llm") == "accepted"
    ids = [repo.upsert_character(conn, book.id, n, name_zh=None, aliases=[], unit_id=u[i][0], ordinal=u[i][1],
                                 origin="test")[0] for i, n in ((1, "母親"), (2, "実母"))]
    set_glossary(conn, book.id, "実母", "生母", category="character")
    assert link(conn, book.id, "母親", "実母", "alias", origin="llm") == "shared"
    assert all(_card(conn, c) is not None for c in (baker, *ids))
