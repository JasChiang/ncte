"""ADR-033: a second look at the read pass's disputed judgments."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS, known, memory_update, scene_analyze, uid, utt


def _analyze(payload, req):
    out = scene_analyze(payload, req)
    kaito = known(payload, "カイト") or "new_2"
    if uid(payload, "ミナは腕を組んで言った"):
        # the model disputes the tag in the next paragraph (「ミナは腕を組んで言った」) → kept as inferred, disputed
        out["utterances"] = [x for x in out["utterances"] if not (x["unit_id"] == uid(payload, "カイト、遅いですよ"))]
        u = utt(payload, "カイト、遅いですよ", kaito, "inferred", ("first_person",))
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
                mina = next(k for k, v in it["candidates"].items() if v.startswith("ミナ"))
                out.append({"ref": it["ref"], "answer": mina, "reason": "「ミナは腕を組んで言った」"})
            elif it["kind"] == "gender":
                out.append({"ref": it["ref"], "answer": "unknown", "reason": "no word says so"})
            elif it["kind"] == "merge":
                out.append({"ref": it["ref"], "answer": "different", "reason": "test"})
        return {"items": out}

    fake = FakeLLM(responders={**RESPONDERS, "scene_analyze": _analyze, "memory_update": _memory, "verify": verify})
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert {it["kind"] for it in seen} == {"speaker", "gender", "merge"}
    assert all(not k.startswith("_") for it in seen for k in it)
    line = conn.execute("SELECT u.*, c.canonical_name FROM utterances u JOIN characters c ON c.id=u.speaker_id_nullable "
                        "WHERE u.quote_text LIKE '%カイト、遅いですよ%'").fetchone()
    assert (line["canonical_name"], line["status"], line["resolution_source"]) == ("ミナ", "inferred", "verify")
    assert conn.execute("SELECT COUNT(*) FROM character_gender").fetchone()[0] == 0  # doubtful inference dropped
    assert conn.execute("SELECT COUNT(*) FROM qa_findings WHERE type='merge_doubted'").fetchone()[0] == 1
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
