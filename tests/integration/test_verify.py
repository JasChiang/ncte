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
            elif it["kind"] == "address":
                out.append({"ref": it["ref"], "answer": "unrelated" if it["self"] else "address", "reason": "test"})
        return {"items": out}

    fake = FakeLLM(responders={**RESPONDERS, "scene_analyze": _analyze, "memory_update": _memory, "verify": verify})
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert {it["kind"] for it in seen} - {"identity", "group"} == {"speaker", "gender", "merge", "address"}
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


def test_forms_found_by_string_match_are_classified_before_naming(conn, cfg, fixtures):
    """ADR-037: a program-matched form gets a second look; a look-alike word is never given onwards."""
    from ncte.memory.address import context_items, usage_of

    asked = []

    def verify(payload, req):
        out = []
        for it in payload["items"]:
            if it["kind"] == "address":
                asked.append(it)
                out.append({"ref": it["ref"], "answer": "unrelated", "reason": "test: not about the person"})
        return {"items": out}

    fake = FakeLLM(responders={**RESPONDERS, "verify": verify})
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert asked and all(it["lines"] and it["form"] for it in asked)
    rows = conn.execute("SELECT * FROM address_terms WHERE origin='program'").fetchall()
    assert rows and all(usage_of(conn, r) == "unrelated" for r in rows if r["uses"] > 0)
    book = conn.execute("SELECT id FROM books").fetchone()[0]
    names = {r["id"]: r["canonical_name"] for r in conn.execute("SELECT * FROM characters")}
    forms = {r["form_ja"] for r in rows}
    items = context_items(conn, book, 10**6, "".join(forms), set(names), names)
    assert not ({x["ja"] for x in items} & forms - {r["form_ja"] for r in conn.execute(
        "SELECT form_ja FROM address_terms WHERE origin!='program' OR zh_origin='human'")})
    naming_calls = [c for c in fake.calls if c.stage == "naming"]
    for c in naming_calls:  # nothing to render for them either
        import json
        assert not ({a["ja"] for a in json.loads(c.messages[-1]["content"]).get("address", [])} & forms)


def test_address_check_command_for_a_book_already_read(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "names", "address-check", epub, "--fake"])
    assert out.exit_code == 0, out.output
    assert "address check:" in out.output


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

    third = [{"kind": "text", "plain_text": "　レイドは村長の後を追った。"}] * 20
    first = [{"kind": "text", "plain_text": "　私はそう思った。"}] * 20
    assert judged("third", third) is None and judged("first", first) is None
    assert judged("third", first) == "conflict" and judged("first", third) == "conflict"
