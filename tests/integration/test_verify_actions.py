"""The second look also asks who did an action the reading inferred and gave to someone absent from the scene (a line
quoted from someone not there: 「〜って言われて」), with the scene's text after it; the answer moves the action."""

import json

from ncte.analysis import verify as v
from ncte.llm.fake import FakeLLM
from ncte.llm.schemas import VerifyOutput

from ..helpers import run_book
from ..scripted import RESPONDERS


def _book(conn, cfg, fixtures):
    fake = FakeLLM(responders={**RESPONDERS, "verify": lambda p, r: {"items": []}})
    book, _, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    return book


def _setup(conn, book_id):
    """The first scene with text, an absent character and a present one; three actions on its first paragraph:
    inferred and absent (asked), inferred and present (not asked), explicit and absent (not asked)."""
    sc = conn.execute("SELECT * FROM scenes WHERE book_id=? AND start_ordinal IS NOT NULL ORDER BY seq", (book_id,)) \
        .fetchone()
    state = json.loads(sc["state_json"] or "{}")
    rows = conn.execute("SELECT id FROM units WHERE book_id=? AND scene_id=? AND translatable=1 ORDER BY ordinal",
                        (book_id, sc["id"])).fetchall()
    present = {p.get("character_id") for p in state.get("participants") or []}
    present |= {r[0] for r in conn.execute("SELECT DISTINCT speaker_id_nullable FROM utterances WHERE scene_id=? AND "
                                           "speaker_id_nullable IS NOT NULL", (sc["id"],))}
    absent = "c_absent_mother"
    conn.execute("INSERT INTO characters(id, book_id, canonical_name, display_name_zh, aliases_json, known_from_unit_id, "
                 "known_from_ordinal, status, origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (absent, book_id, "母", "母親", "[]", rows[0]["id"], 0, "active", "llm", "t", "t"))
    other = "c_absent_grandpa"
    conn.execute("INSERT INTO characters(id, book_id, canonical_name, display_name_zh, aliases_json, known_from_unit_id, "
                 "known_from_ordinal, status, origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (other, book_id, "祖父", "爺爺", "[]", rows[0]["id"], 0, "active", "llm", "t", "t"))
    here = sorted(x for x in present if x)[0]
    state["actions"] = [
        {"unit_id": rows[0]["id"], "summary": "祖父に言われた", "subject_candidates": [other],
         "object_candidates": [here], "status": "inferred"},
        {"unit_id": rows[0]["id"], "summary": "present", "subject_candidates": [here], "object_candidates": [],
         "status": "inferred"},
        {"unit_id": rows[0]["id"], "summary": "stated", "subject_candidates": [other], "object_candidates": [],
         "status": "explicit"},
    ]
    conn.execute("UPDATE scenes SET state_json=? WHERE id=?", (json.dumps(state, ensure_ascii=False), sc["id"]))
    return sc, rows, absent, other


def test_an_inferred_action_given_to_someone_absent_is_asked_with_the_rest_of_its_scene(conn, cfg, fixtures):
    book = _book(conn, cfg, fixtures)
    sc, rows, absent, other = _setup(conn, book.id)
    items = [i for i in v.collect(conn, book.id) if i["kind"] == "action"]
    assert [i["ref"] for i in items] == [f"a:{sc['id']}:0"]  # not the present one, not the explicit one
    item = items[0]
    assert "first_answer" not in item and "summary" not in item  # the reading's answer is not shown
    assert item["_first"] == [other] and absent in item["candidates"] and other in item["candidates"]
    assert item["paragraphs"][0]["text"].startswith("▶ ")
    assert len(item["paragraphs"]) == len(rows)  # a short scene: all of it, the later evidence included
    assert set(item["_codes"].values()) == {r["id"] for r in rows}
    assert v.collect(conn, book.id, max_actions=0) and not [i for i in v.collect(conn, book.id, max_actions=0)
                                                            if i["kind"] == "action"]  # the cap


def test_the_answer_moves_the_action_and_it_is_not_asked_again(conn, cfg, fixtures):
    book = _book(conn, cfg, fixtures)
    sc, rows, absent, other = _setup(conn, book.id)
    item = next(i for i in v.collect(conn, book.id) if i["kind"] == "action")
    last = item["paragraphs"][-1]["p"]
    parsed = VerifyOutput.model_validate({"items": [{"ref": item["ref"], "answer": absent, "reason": "孫なし確定",
                                                     "evidence": [last], "summary": "母親叮嚀了她"}]})
    stats = v.apply(conn, book.id, [item], parsed, lambda *a: None)
    assert stats["actions_changed"] == 1
    act = json.loads(conn.execute("SELECT state_json FROM scenes WHERE id=?", (sc["id"],)).fetchone()[0])["actions"][0]
    assert act["subject_candidates"] == [absent] and act["status"] == "inferred" and act["verified"]
    assert act["verify"]["before"] == [other] and act["verify"]["evidence"] == [item["_codes"][last]]
    # the reading's summary named the one it guessed: the second look's own line replaces it
    assert act["summary"] == "母親叮嚀了她" and act["verify"]["summary_before"] == "祖父に言われた"
    assert not [i for i in v.collect(conn, book.id) if i["kind"] == "action"]
    from ncte.memory.retrieval import memory_context

    rows_ = conn.execute("SELECT * FROM units WHERE scene_id=? AND translatable=1 ORDER BY ordinal", (sc["id"],)).fetchall()
    scene = conn.execute("SELECT * FROM scenes WHERE id=?", (sc["id"],)).fetchone()
    sent = memory_context(conn, book.id, scene=scene, batch_rows=rows_[:1])["scene"]["omitted_arguments"]
    assert sent[0]["subject_candidates"] == ["母親"] and "verify" not in sent[0] and "verified" not in sent[0]


def test_an_answer_without_the_passage_it_rests_on_changes_nothing(conn, cfg, fixtures):
    book = _book(conn, cfg, fixtures)
    sc, rows, absent, other = _setup(conn, book.id)
    item = next(i for i in v.collect(conn, book.id) if i["kind"] == "action")
    for evidence in ([], ["a999"]):
        parsed = VerifyOutput.model_validate({"items": [{"ref": item["ref"], "answer": absent, "reason": "x",
                                                         "evidence": evidence}]})
        assert v.apply(conn, book.id, [item], parsed, lambda *a: None)["ignored"] == 1
    parsed = VerifyOutput.model_validate({"items": [{"ref": item["ref"], "answer": "c_nobody", "reason": "x",
                                                     "evidence": ["a1"]}]})
    assert v.apply(conn, book.id, [item], parsed, lambda *a: None)["ignored"] == 1
    act = json.loads(conn.execute("SELECT state_json FROM scenes WHERE id=?", (sc["id"],)).fetchone()[0])["actions"][0]
    assert act["subject_candidates"] == [other] and not act.get("verified")


def test_unknown_leaves_who_did_it_open(conn, cfg, fixtures):
    book = _book(conn, cfg, fixtures)
    sc, rows, absent, other = _setup(conn, book.id)
    item = next(i for i in v.collect(conn, book.id) if i["kind"] == "action")
    parsed = VerifyOutput.model_validate({"items": [{"ref": item["ref"], "answer": "unknown", "reason": "x"}]})
    v.apply(conn, book.id, [item], parsed, lambda *a: None)
    act = json.loads(conn.execute("SELECT state_json FROM scenes WHERE id=?", (sc["id"],)).fetchone()[0])["actions"][0]
    assert act["subject_candidates"] == [] and act["status"] == "unknown" and act["verified"]
