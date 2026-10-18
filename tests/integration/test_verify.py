"""ADR-033: a second look at the read pass's disputed judgments."""

from ncte.llm.fake import FakeLLM

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
