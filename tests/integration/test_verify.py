"""ADR-033: a second look at the read pass's disputed judgments."""

from ncte.llm.fake import FakeLLM, default_translate

from ..helpers import run_book
from ..scripted import RESPONDERS, known, memory_update, scene_analyze, uid, utt


def _analyze(payload, req):
    out = scene_analyze(payload, req)
    kaito = known(payload, "カイト") or "new_2"
    mina = known(payload, "ミナ") or "new_1"
    if uid(payload, "ミナは腕を組んで言った"):
        # the model is unsure between the two (names both, answers no one): the line goes to the second look
        out["utterances"] = [x for x in out["utterances"] if not (x["unit_id"] == uid(payload, "カイト、遅いですよ"))]
        u = utt(payload, "カイト、遅いですよ", None, "unknown")
        u["speaker_candidates"] = [{"character_ref": r, "evidence_types": ["address"], "evidence_unit_ids": [u["unit_id"]],
                                    "note": ""} for r in (mina, kaito)]
        out["utterances"].append(u)
    hood = known(payload, "フードをかぶった少女")
    if uid(payload, "フードをかぶった少女が立っていた"):
        out["new_characters"].append({"temp_id": "new_7", "name_ja": "フードをかぶった少女", "aliases_ja": [],
                                      "proposed_name_zh": "戴兜帽的少女",
                                      "evidence_unit_ids": [uid(payload, "フードをかぶった少女が立っていた")]})
    for nc in out["new_characters"]:
        if nc["name_ja"] == "リリア" and hood:
            nc["same_as_ref"] = hood
    return out


def _memory(payload, req):
    out = memory_update(payload, req)
    u, mina = uid(payload, "カイト、遅いですよ"), known(payload, "ミナ")
    if u and mina:  # no word in the quote says so: an inference
        out["genders"] = [{"character_ref": mina, "gender": "female", "certainty": "inferred",
                           "evidence_unit_ids": [u], "evidence_quote": "カイト、遅いですよ"}]
    return out


def test_second_look_changes_speaker_drops_doubtful_gender_and_flags_merge(conn, cfg, fixtures):
    seen = []

    def verify(payload, req):
        seen.extend(payload["items"])
        out = []
        for it in payload["items"]:
            if it["kind"] == "speaker":
                mina = next((k for k, v in it["candidates"].items() if v.startswith("ミナ")), None)
                if mina and "遅いですよ" in it["quote"]:
                    out.append({"ref": it["ref"], "answer": mina, "reason": "「ミナは腕を組んで言った」"})
            elif it["kind"] == "gender":
                out.append({"ref": it["ref"], "answer": "unknown", "reason": "no word says so"})
            elif it["kind"] == "merge":
                out.append({"ref": it["ref"], "answer": "different", "reason": "test"})
            elif it["kind"] == "address":
                out.append({"ref": it["ref"], "answer": "unrelated" if it["self"] else "address", "reason": "test"})
        return {"items": out}

    fake = FakeLLM(responders={**RESPONDERS, "scene_analyze": _analyze, "memory_update": _memory, "verify": verify})
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    # forms of address come from the model with their use: nothing of the program's to classify (ADR-059)
    assert {it["kind"] for it in seen} - {"identity", "group"} == {"speaker", "gender", "merge"}
    assert all(not k.startswith("_") for it in seen for k in it)
    line = conn.execute("SELECT u.*, c.canonical_name FROM utterances u JOIN characters c ON c.id=u.speaker_id_nullable "
                        "WHERE u.quote_text LIKE '%カイト、遅いですよ%'").fetchone()
    assert (line["canonical_name"], line["status"], line["resolution_source"]) == ("ミナ", "inferred", "verify")
    assert conn.execute("SELECT COUNT(*) FROM character_gender").fetchone()[0] == 0  # doubtful inference dropped
    assert conn.execute("SELECT COUNT(*) FROM qa_findings WHERE type='merge_doubted'").fetchone()[0] == 1
    merge = next(it for it in seen if it["kind"] == "merge")
    assert merge["merged_forms"] == ["フードをかぶった少女"]  # only what the merge brought, not the name's own forms
    lilia = conn.execute("SELECT aliases_json FROM characters WHERE canonical_name='リリア'").fetchone()
    assert "フードをかぶった少女" not in lilia[0]  # judged different: the description no longer names her
    # verify runs once per run, before naming
    jobs = [r[0] for r in conn.execute("SELECT stage FROM jobs WHERE stage IN ('verify','naming') ORDER BY seq, id")]
    assert jobs[0] == "verify"


def test_nothing_in_doubt_means_no_request(conn, cfg, fixtures):
    calls = []

    def verify(payload, req):
        calls.append(payload)
        return {"items": []}

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "verify": verify}), mode="context")
    assert all(p["items"] for p in calls)  # a request only when something is in doubt


def test_each_scene_tells_the_translator_whose_voice_it_is(conn, cfg, fixtures):
    """ADR-040: the read pass judges narration per scene; the translation of that scene is told."""

    def analyze(payload, req):
        out = scene_analyze(payload, req)
        assert "narration_hint" in payload
        kaito = known(payload, "カイト")
        out["narration"] = {"person": "third", "focal_ref": kaito, "narrator_ref": None, "evidence_unit_ids": []}
        return out

    fake = FakeLLM(responders={**RESPONDERS, "scene_analyze": analyze})
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    told = [p.get("narration") for p, c in zip(fake.payloads, fake.calls, strict=True) if c.stage == "translate"]
    assert any(t and t["person"] == "third" for t in told)
    assert any(t and t.get("focal") for t in told)  # the focal character by name, once known
    later = [p for p, c in zip(fake.payloads, fake.calls, strict=True) if c.stage == "scene_analyze"][1:]
    assert any(p.get("previous_narration", {}).get("person") == "third" for p in later)


def test_program_checks_the_judged_narration():
    from ncte.analysis.narration import judged

    third = [{"kind": "text", "plain_text": "　カイルは村長の後を追った。"}] * 20
    first = [{"kind": "text", "plain_text": "　私はそう思った。"}] * 20
    assert judged("third", third) is None and judged("first", first) is None
    assert judged("third", first) == "conflict" and judged("first", third) == "conflict"


def test_the_second_look_does_not_ask_again_what_it_answered(conn, cfg, fixtures):
    """Asking again until the model agrees adds no evidence: an item answered with the same content is not asked by the
    next run; a line answered by the second look is not picked again."""
    asked: list[list[str]] = []

    def verify(payload, req):
        asked.append([i["ref"] for i in payload["items"]])
        return {"items": [{"ref": i["ref"], "answer": "same" if i["kind"] == "merge" else "unknown",
                           "reason": "t"} for i in payload["items"]]}

    fake = FakeLLM(responders={**RESPONDERS, "scene_analyze": _analyze, "memory_update": _memory, "verify": verify})
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    first = {r for refs in asked for r in refs}
    assert first
    asked.clear()
    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "scene_analyze": _analyze,
                                                               "memory_update": _memory, "verify": verify}), mode="context")
    assert not ({r for refs in asked for r in refs} & first)


def test_an_answer_that_cannot_be_applied_is_asked_again():
    """An id not among the candidates, or a value the kind does not take, is not an answer."""
    from ncte.analysis.verify import answer_valid

    item = {"kind": "identity", "candidates": {"c_1": "米娜"}}
    assert answer_valid(item, "c_1") and answer_valid(item, "unknown") and not answer_valid(item, "c_9")
    assert not answer_valid({"kind": "narration"}, "second") and answer_valid({"kind": "gender"}, "female")


def test_a_judgment_written_again_after_a_reread_is_asked_again(conn, cfg, fixtures):
    """The second look dropped an inferred gender; reading the scene again writes the same guess back — it is new data
    (a new version), so it is asked again rather than slipping through to translation."""
    from ncte.analysis.verify import collect
    from ncte.memory.db import utcnow

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "scene_analyze": _analyze,
                                                                            "memory_update": _memory}), mode="context")
    cid = conn.execute("SELECT id FROM characters WHERE canonical_name='ミナ'").fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO character_gender(character_id, gender, book_id, ordinal, evidence_quote, origin, "
                 "updated_at, certainty) VALUES (?,?,?,?,?,?,?,?)", (cid, "female", book.id, 1, "q", "llm", "t1", "inferred"))
    item = next(i for i in collect(conn, book.id) if i["ref"] == f"g:{cid}")
    conn.execute("INSERT OR REPLACE INTO verify_answers(book_id, ref, fingerprint, answer, created_at) VALUES (?,?,?,?,?)",
                 (book.id, item["ref"], item["_fp"], "unknown", utcnow()))
    assert not [i for i in collect(conn, book.id) if i["ref"] == f"g:{cid}"]  # answered, unchanged
    conn.execute("UPDATE character_gender SET updated_at='t2' WHERE character_id=?", (cid,))  # written again
    assert [i for i in collect(conn, book.id) if i["ref"] == f"g:{cid}"]



def test_a_new_card_elsewhere_does_not_reopen_identity_questions():
    from ncte.analysis.verify import fingerprint

    item = {"ref": "i:c1", "kind": "identity", "name": "姊姊", "context": ["…"], "called_by": [], "candidates": {"c2": "高木"}}
    more = {**item, "candidates": {"c2": "高木", "c3": "新角色"}}
    assert fingerprint(item) == fingerprint(more)
    assert fingerprint(item) != fingerprint({**item, "called_by": ["高木：姉ちゃん"]})  # its own material changed


def test_an_identity_left_unknown_is_asked_again_when_new_cards_come():
    """Answered "unknown" (the right card may not exist yet): a new card is a reason to ask again. Answered with
    someone: the cards are one already, so a new card elsewhere is not."""
    from ncte.analysis.verify import fingerprint

    item = {"ref": "i:c1", "kind": "identity", "name": "姊姊", "context": ["…"], "called_by": [], "candidates": {"c2": "高木"}}
    more = {**item, "candidates": {"c2": "高木", "c3": "高木の姉"}}
    assert fingerprint(item, with_candidates=True) != fingerprint(more, with_candidates=True)  # unknown: asked again
    assert fingerprint(item) == fingerprint(more)  # answered with someone: not asked again


def test_the_read_pass_s_own_questions_are_answered_and_reach_translation_and_review(conn, cfg, fixtures):
    """B5 (ADR-067): a question the model raised while reading a scene (`uncertainties`) goes to the second look with
    the passage and the scene's people; the answer (a sentence, optionally who) is kept with the scene and given to
    translation and review with the question; it is not asked again."""
    from ncte.gui import data

    def analyze(payload, req):
        out = scene_analyze(payload, req)
        if (u := uid(payload, "カイトは銀の鍵をミナに手渡した")):
            out["uncertainties"] = [{"unit_ids": [u], "question": "鑰匙原本是誰的？"}]
        return out

    asked, translated, reviewed = [], [], []

    def verify(payload, req):
        out = []
        for it in payload["items"]:
            if it["kind"] == "question":
                asked.append(it)
                kaito = next(k for k, v in it["candidates"].items() if "海斗" in v or "カイト" in v)
                out.append({"ref": it["ref"], "answer": "原文只寫海斗交出，原本屬誰沒說", "character_id": kaito,
                            "reason": "「カイトは銀の鍵をミナに手渡した」"})
        return {"items": out}

    def translate(payload, req):
        translated.append(payload)
        return RESPONDERS.get("translate", default_translate)(payload, req)

    cfg.review.automatic_review = "all"
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={
        **RESPONDERS, "scene_analyze": analyze, "verify": verify, "translate": translate,
        "review": lambda p, r: reviewed.append(p) or {"issues": []}}), mode="context")
    assert len(asked) == 1
    q = asked[0]
    assert q["question"] == "鑰匙原本是誰的？" and any("▶" in x and "銀の鍵" in x for x in q["context"])
    assert not any(k.startswith("_") for k in q)
    state = conn.execute("SELECT state_json FROM scenes WHERE state_json LIKE '%鑰匙原本是誰的%'").fetchone()[0]
    assert "原本屬誰沒說" in state
    with_q = [p["scene"]["uncertainties"] for p in translated if p.get("scene", {}).get("uncertainties")]
    assert with_q and with_q[0][0]["answer"] == "原文只寫海斗交出，原本屬誰沒說" and with_q[0][0]["answer_who"] == "海斗"
    # review requests come in any order when chapters run side by side; one of them carries the answered question
    assert any(u.get("answer") for p in reviewed for u in p.get("scene", {}).get("uncertainties", []))
    shown = [s for s in data.story(conn, book)["scenes"] if s["questions"]]
    assert shown and shown[0]["questions"][0]["answer"].startswith("原文只寫")
    # answered: not asked again
    from ncte.analysis import verify as vf

    assert not [i for i in vf.collect(conn, book.id) if i["kind"] == "question"]


def test_the_asked_paragraph_and_its_evidence_are_sent_whole(conn, cfg, fixtures):
    """A clue late in a long paragraph decides who speaks: the marked paragraph and the paragraphs the reading cited as
    evidence go in whole (evidence outside the window too); the paragraphs merely around keep their opening."""
    import json

    from ncte.analysis import verify as v

    fake = FakeLLM(responders={**RESPONDERS, "scene_analyze": _analyze, "memory_update": _memory,
                               "verify": lambda p, r: {"items": []}})  # nothing answered: the line stays in doubt
    book, _, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    line = conn.execute("SELECT ut.id, ut.unit_id, un.ordinal FROM utterances ut JOIN units un ON un.id=ut.unit_id "
                        "WHERE ut.quote_text LIKE '%カイト、遅いですよ%'").fetchone()
    tail = "、" + "あ" * 400 + "――と言ったのは〈手がかり〉だった。"
    rows = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND translatable=1 ORDER BY ordinal",
                        (book.id,)).fetchall()
    before = next(r for r in rows if r["ordinal"] < line["ordinal"] and r["ordinal"] >= line["ordinal"] - 2)
    far = rows[-1]
    assert far["ordinal"] > line["ordinal"] + v.CONTEXT_UNITS  # the fixture has a paragraph well outside the window
    for unit in (before["id"], far["id"], line["unit_id"]):
        conn.execute("UPDATE units SET plain_text=plain_text||? WHERE id=?", (tail, unit))
    for h in conn.execute("SELECT id, evidence_json FROM speaker_hypotheses WHERE utterance_id=?", (line["id"],)):
        ev = json.loads(h["evidence_json"])  # the reading cited a paragraph well before the window
        ev["evidence_unit_ids"] = [far["id"], *ev["evidence_unit_ids"]]
        conn.execute("UPDATE speaker_hypotheses SET evidence_json=? WHERE id=?", (json.dumps(ev), h["id"]))
    item = next(i for i in v.collect(conn, book.id) if i["ref"] == f"q:{line['id']}")
    assert "〈手がかり〉" in next(p for p in item["context"] if p.startswith("▶ "))  # the asked paragraph whole
    around = [p for p in item["context"] if not p.startswith("▶ ") and "あああ" in p]
    assert around and all("〈手がかり〉" not in p for p in around)  # a paragraph around: its opening only
    assert any("〈手がかり〉" in p for p in item["evidence_paragraphs"])  # evidence outside the window, whole
