"""ADR-103, from two more real runs (synthetic books): a person known only by a word other cards own, someone of the
present said to be another person, a card attributed a line before its name is revealed, gender evidence chosen by
card, a question about a line's addressee, several lines answered at once, a gender recorded only late."""

import json

from ncte.analysis import gender_gaps, verify
from ncte.analysis.scene import apply_scene_analysis, get_scene
from ncte.library import import_book
from ncte.llm.schemas import SceneAnalysisOutput, VerifyOutput
from ncte.memory import repository as repo
from ncte.memory import visibility as vis

from .test_unconfirmed_cards import _card, _speaker
from .test_unconfirmed_realrun import _memory


def _mothers(conn, cfg, fixtures, intro_text="　路地の奥に、母親が立っていた。"):
    """Two mothers seen in the first scene (「母親」, and 「義母」 also called 「母親」); the second scene not read yet."""
    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    first = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal LIMIT 1",
                         (book.id, scenes[0]["id"])).fetchone()
    a, _ = repo.upsert_character(conn, book.id, "母親", name_zh="母親", aliases=[], unit_id=first["id"],
                                 ordinal=first["ordinal"], origin="test")
    b, _ = repo.upsert_character(conn, book.id, "義母", name_zh="繼母", aliases=["母親"], unit_id=first["id"],
                                 ordinal=first["ordinal"], origin="test")
    rows = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND kind='text' ORDER BY ordinal",
                        (book.id, scenes[1]["id"])).fetchall()
    intro = next(r for r in rows if "少女が立っていた" in r["plain_text"])
    line = next(r for r in rows if "どこで手に入れたの" in r["plain_text"])
    after = next(r for r in rows if "誰だ、お前" in r["plain_text"])
    conn.execute("UPDATE units SET plain_text=? WHERE id=?", (intro_text, intro["id"]))
    return {"book": book, "scenes": scenes, "a": a, "b": b, "intro": intro, "line": line, "after": after,
            "first": first}


def _read(conn, s, nc, lines=()):
    out = {"pov": None, "location": None, "time": None, "participants": [], "actions": [], "props": [],
           "uncertainties": [], "new_characters": [{"temp_id": "new_1", "proposed_name_zh": "",
                                                    "evidence_unit_ids": [s["intro"]["id"]], "aliases_ja": [], **nc}],
           "utterances": [{"unit_id": u["id"], "qi": 0, "status": "inferred", "speaker": "new_1", "conflicts": [],
                           "candidates": [{"ref": "new_1", "types": ["continuity"], "evidence": [u["id"]],
                                           "note": ""}]} for u in lines]}
    refs: dict[str, str] = {}
    stats = apply_scene_analysis(conn, s["book"].id, get_scene(conn, s["book"].id, s["scenes"][1]["id"]),
                                 SceneAnalysisOutput.model_validate(out), refs=refs)
    return refs, stats


def test_a_new_person_known_only_by_a_word_cards_own_gets_a_marked_card_even_if_the_word_is_common(conn, cfg,
                                                                                                    fixtures):
    s = _mothers(conn, cfg, fixtures)
    refs, stats = _read(conn, s, {"name_ja": "ルカの母親", "aliases_ja": ["母親"],
                                  "alias_kinds": [{"ja": "母親", "kind": "common"}]}, [s["line"]])
    p = _card(conn, refs["new_1"])
    assert stats["rejected"] == 0 and p["canonical_name"] == "母親" and p["label"]
    assert set(repo.may_be(p)) == {s["a"], s["b"]} and _speaker(conn, s["line"]["id"]) == p["id"]
    assert [r[0] for r in conn.execute("SELECT text FROM character_notes WHERE character_id=?", (p["id"],))] == \
        ["ルカの母親"]


def test_a_common_word_no_card_owns_still_names_nobody(conn, cfg, fixtures):
    s = _mothers(conn, cfg, fixtures, intro_text="　路地の奥に、子供が立っていた。")
    refs, stats = _read(conn, s, {"name_ja": "ルカの子", "aliases_ja": ["子供"],
                                  "alias_kinds": [{"ja": "子供", "kind": "common"}]})
    assert "new_1" not in refs and stats["rejected"] == 1


def test_someone_of_the_present_said_to_be_another_person_is_a_marked_card_not_another_time(conn, cfg, fixtures):
    s = _mothers(conn, cfg, fixtures)
    refs, _ = _read(conn, s, {"name_ja": "母親", "other_person": True, "period": ""})
    p = _card(conn, refs["new_1"])
    assert repo.period_of(p) is None and p["label"].isdigit() and set(repo.may_be(p)) == {s["a"], s["b"]}
    refs, _ = _read(conn, s, {"name_ja": "母親", "other_person": True, "period": "前世"})
    assert repo.period_of(_card(conn, refs["new_1"])) == "前世"  # a real other time keeps its card


def test_a_card_named_later_in_the_scene_is_there_from_the_first_line_it_is_given(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    rows = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND kind='text' ORDER BY ordinal",
                        (book.id, scenes[1]["id"])).fetchall()
    intro = next(r for r in rows if "少女が立っていた" in r["plain_text"])
    line = next(r for r in rows if "どこで手に入れたの" in r["plain_text"])
    named = next(r for r in rows if "誰だ、お前" in r["plain_text"])
    conn.execute("UPDATE units SET plain_text='　路地の奥に、母親が立っていた。' WHERE id=?", (intro["id"],))
    conn.execute("UPDATE units SET plain_text='　その人はイレナと名乗った。' WHERE id=?", (named["id"],))
    out = {"pov": None, "location": None, "time": None, "participants": [], "actions": [], "props": [],
           "uncertainties": [],
           "new_characters": [{"temp_id": "new_1", "name_ja": "イレナ", "aliases_ja": ["母親"], "proposed_name_zh": "",
                               "alias_kinds": [{"ja": "母親", "kind": "description"}],
                               "evidence_unit_ids": [named["id"], intro["id"]]}],
           "utterances": [{"unit_id": line["id"], "qi": 0, "status": "inferred", "speaker": "new_1", "conflicts": [],
                           "candidates": [{"ref": "new_1", "types": ["continuity"], "evidence": [line["id"]],
                                           "note": ""}]}]}
    refs: dict[str, str] = {}
    apply_scene_analysis(conn, book.id, get_scene(conn, book.id, scenes[1]["id"]),
                         SceneAnalysisOutput.model_validate(out), refs=refs)
    c = _card(conn, refs["new_1"])
    assert c["known_from_ordinal"] <= line["ordinal"] and _speaker(conn, line["id"]) == c["id"]
    at_line = vis.character_view(conn, book.id, c, line["ordinal"])
    assert at_line is not None and at_line["name_ja"] == "母親" and "イレナ" not in at_line["aliases_ja"]
    assert vis.character_view(conn, book.id, c, named["ordinal"])["name_ja"] == "イレナ"


def test_gender_evidence_is_chosen_by_card_never_by_a_word_another_card_shares(conn, cfg, fixtures, monkeypatch):
    monkeypatch.setattr(gender_gaps, "MIN_LINES", 1)
    s = _mothers(conn, cfg, fixtures)
    refs, _ = _read(conn, s, {"name_ja": "母親"}, [s["line"]])
    p = refs["new_1"]
    got = {c["id"]: c for c in gender_gaps.candidates(conn, s["book"].id)}
    known = _card(conn, p)["known_from_ordinal"]
    assert got[p]["sentences"] and all(at >= known for at, _, _ in got[p]["sentences"])
    assert all("named" not in how for _, how, _ in got[p]["sentences"])  # 「母親」 is three cards' word
    assert all("named" not in how for _, how, _ in got.get(s["a"], {"sentences": []})["sentences"])


def test_a_gender_recorded_only_late_leaves_the_stretch_before_it_a_gap(conn, cfg, fixtures, monkeypatch):
    monkeypatch.setattr(gender_gaps, "MIN_LINES", 1)
    s = _mothers(conn, cfg, fixtures)
    refs, _ = _read(conn, s, {"name_ja": "母親"}, [s["line"]])
    p = refs["new_1"]
    last = conn.execute("SELECT * FROM units WHERE book_id=? ORDER BY ordinal DESC LIMIT 1", (s["book"].id,)).fetchone()
    repo.set_gender(conn, s["book"].id, p, "female", ordinal=last["ordinal"], quote="彼女", origin="llm")
    got = {c["id"]: c for c in gender_gaps.candidates(conn, s["book"].id)}
    assert p in got and all(at < last["ordinal"] for at, _, _ in got[p]["sentences"])
    repo.set_gender(conn, s["book"].id, p, "female", ordinal=s["line"]["ordinal"], quote="母親", origin="llm")
    assert p not in {c["id"] for c in gender_gaps.candidates(conn, s["book"].id)}  # known from the start: no gap


def _ask_question(conn, s, unit_ids, answer):
    book, s1 = s["book"], s["scenes"][1]
    state = json.loads(get_scene(conn, book.id, s1["id"])["state_json"])
    state["uncertainties"] = [{"unit_ids": unit_ids, "question": "この台詞は誰に言ったのか"}]
    state["participants"] = [{"character_id": s["a"], "presence": "explicit", "evidence_unit_ids": []}]
    conn.execute("UPDATE scenes SET state_json=? WHERE id=?", (json.dumps(state, ensure_ascii=False), s1["id"]))
    conn.execute("DELETE FROM verify_answers")
    items = verify.collect(conn, book.id)
    item = next(i for i in items if i["kind"] == "question")
    parsed = VerifyOutput.model_validate({"items": [{"ref": item["ref"], "reason": "x", **answer}]})
    return verify.apply(conn, book.id, items, parsed, lambda *a: None)


def test_an_answer_about_who_a_line_is_said_to_sets_its_addressee_not_its_speaker(conn, cfg, fixtures):
    s = _mothers(conn, cfg, fixtures)
    refs, _ = _read(conn, s, {"name_ja": "母親"}, [s["line"]])
    _ask_question(conn, s, [s["line"]["id"]], {"answer": "母親に", "character_id": s["a"], "line_role": "addressee"})
    row = conn.execute("SELECT * FROM utterances WHERE unit_id=?", (s["line"]["id"],)).fetchone()
    assert row["speaker_id_nullable"] == refs["new_1"]
    assert json.loads(row["addressee_ids_json"]) == [s["a"]] and row["addressee_status"] == "inferred"
    _ask_question(conn, s, [s["line"]["id"]], {"answer": "母親", "character_id": s["a"]})  # says neither
    assert _speaker(conn, s["line"]["id"]) == refs["new_1"]


def test_an_answer_may_set_the_speaker_of_several_lines(conn, cfg, fixtures):
    s = _mothers(conn, cfg, fixtures)
    _read(conn, s, {"name_ja": "母親"}, [s["line"], s["after"]])
    utts = [conn.execute("SELECT id FROM utterances WHERE unit_id=?", (u["id"],)).fetchone()[0]
            for u in (s["line"], s["after"])]
    stats = _ask_question(conn, s, [s["line"]["id"], s["after"]["id"]],
                          {"answer": "母親", "character_id": s["a"], "line_role": "speaker", "line_refs": utts})
    assert [_speaker(conn, u["id"]) for u in (s["line"], s["after"])] == [s["a"], s["a"]]
    assert stats["speakers_changed"] == 2


def test_a_relation_of_a_card_with_itself_has_its_own_reason(conn, cfg, fixtures):
    s = _mothers(conn, cfg, fixtures)
    _memory(conn, s["book"], s["scenes"][1], relations=[{
        "a_ref": s["a"], "b_ref": s["a"], "relation": "同一人", "a_is": "母親", "b_is": "母親", "kind": "same_person",
        "evidence_unit_ids": [s["intro"]["id"]], "evidence_quote": "母親が立って"}])
    reasons = [r[0] for r in conn.execute("SELECT reason FROM memory_rejections WHERE stage='memory_update'")]
    assert any("with itself" in r for r in reasons) and not any("unknown characters" in r for r in reasons)
