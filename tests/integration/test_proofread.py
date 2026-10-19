"""ADR-051: every paragraph checked against its source; a real error goes back to the translator."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_proofreading_sends_a_wrong_paragraph_back_to_the_translator(conn, cfg, fixtures):
    seen = []

    def proofread(payload, req):
        seen.append(payload)
        p = next(x for x in payload["paragraphs"] if x.get("speaker"))
        return {"issues": [{"unit_id": p["unit_id"], "kind": "meaning", "severity": "high", "quote": p["zh"][:4],
                            "problem": "原文是否定，譯文成了肯定", "fix": "改成否定"},
                           {"unit_id": p["unit_id"], "kind": "style", "severity": "low", "quote": "，",
                            "problem": "標點", "fix": "。"}]}  # a small slip: not worth a retranslation

    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "proofread": proofread}),
                            mode="context")
    assert seen and all("ja" in p and "zh" in p for p in seen[0]["paragraphs"])
    assert all(not k.startswith("_") for p in seen[0]["paragraphs"] for k in p)  # the program's own keys stay home
    assert "characters" in seen[0]
    found = conn.execute("SELECT type, severity FROM qa_findings WHERE run_id=? AND type LIKE 'review:proofread%'",
                         (rid,)).fetchall()
    # one stretch per chapter: one real error each, the low-severity slip dropped
    assert [tuple(f) for f in found] == [("review:proofread_meaning", "high")] * len(seen)
    job = conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage='proofread'", (rid,)).fetchone()
    assert job["status"] == "succeeded" and f'"retranslated": {len(seen)}' in job["checkpoint_json"]


def test_proofread_command(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "proofread", epub, "--fake"])
    assert out.exit_code == 0 and "proofreading:" in out.output, out.output


def test_proofreading_reads_the_stretch_before_and_knows_who_lines_are_said_to(conn, cfg, fixtures):
    """Small stretches: each after the first gets the end of the one before (to read, not to check) when it is in the
    same chapter; a line's addressee, as read, comes with the paragraph."""
    seen = []

    def proofread(payload, req):
        seen.append(payload)
        return {"issues": []}

    cfg.context.proofread_chunk_units = 3
    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "proofread": proofread}), mode="context")
    assert len(seen) > 2
    assert "context_before" not in seen[0]
    later = [p for p in seen[1:] if "context_before" in p]
    assert later and all(1 <= len(p["context_before"]) <= 6 and set(p["context_before"][0]) == {"ja", "zh"}
                         for p in later)
    checked = {x["unit_id"] for p in seen for x in p["paragraphs"]}
    assert len(checked) == sum(len(p["paragraphs"]) for p in seen)  # every paragraph is checked once
    # a line said to someone (as read): the paragraph carries who, marked as a guess unless stated
    from ncte.analysis import proofread as pr

    u = conn.execute("SELECT * FROM utterances WHERE speaker_id_nullable IS NOT NULL LIMIT 1").fetchone()
    other = conn.execute("SELECT id FROM characters WHERE id!=? LIMIT 1", (u["speaker_id_nullable"],)).fetchone()[0]
    conn.execute("UPDATE utterances SET addressee_ids_json=?, addressee_status='inferred' WHERE id=?",
                 (f'["{other}"]', u["id"]))
    item = next(x for part in pr.chunks(conn, u["book_id"]) for x in part if x["unit_id"] == u["unit_id"])
    assert item["to"].endswith("（推測）") and other in item["_who"]


def test_with_verdicts_every_paragraph_is_answered_for(conn, cfg, fixtures):
    """Asked for a verdict on each paragraph, the request says so; paragraphs left without one are counted."""
    seen = []

    def proofread(payload, req):
        seen.append(payload)
        first = payload["paragraphs"][0]
        return {"checked": [{"unit_id": first["unit_id"], "gist": "打招呼", "ok": True}], "issues": []}

    cfg.context.proofread_verdicts = True
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "proofread": proofread}),
                            mode="context")
    assert seen and all(p.get("verdicts") is True for p in seen)
    job = conn.execute("SELECT checkpoint_json FROM jobs WHERE run_id=? AND stage='proofread'", (rid,)).fetchone()
    total = sum(len(p["paragraphs"]) for p in seen)
    assert f'"no_verdict": {total - len(seen)}' in job["checkpoint_json"]


def test_the_commands_record_their_pass_so_a_whole_book_rerun_makes_it_complete(tmp_path, fixtures):
    """`ncte proofread` and `ncte reader-check` record a job of their own: run on chosen paragraphs only, the pass is
    partial (the book was not checked whole); a later whole-book run that succeeds makes it complete again."""
    from typer.testing import CliRunner

    from ncte.cli import app
    from ncte.config import Config
    from ncte.reports.acceptance import acceptance
    from ncte.workspace import Workspace

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    cfg = Config()
    cfg.app.workspace = str(ws)

    def db(sql: str = "", stage: str = "proofread") -> tuple[str, bool, str]:
        w = Workspace(cfg)
        try:
            conn, book = w.open_book(epub)
            if sql:
                conn.execute(sql, (book.id, stage))
                conn.commit()
            last = conn.execute("SELECT status FROM jobs WHERE book_id=? AND stage=? ORDER BY id DESC LIMIT 1",
                                (book.id, stage)).fetchone()[0]
            ok = acceptance(conn, book.id)["checks"][f"{stage}_complete"]["ok"]
            uid = conn.execute("SELECT id FROM units WHERE book_id=? AND kind='text' AND translatable=1 "
                               "ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()[0]
            return last, ok, uid
        finally:
            w.close()

    last, ok, uid = db()
    assert (last, ok) == ("succeeded", True)
    out = r.invoke(app, ["-w", str(ws), "proofread", epub, "--fake", "--unit-id", uid])
    assert out.exit_code == 0, out.output
    assert db()[:2] == ("partial", False)  # chosen paragraphs only: recorded, never complete
    out = r.invoke(app, ["-w", str(ws), "proofread", epub, "--fake"])
    assert out.exit_code == 0, out.output
    assert db()[:2] == ("succeeded", True)  # the whole book again: complete

    # an earlier reader's look left partial (a stretch refused): the command over the whole book completes it
    assert db("UPDATE jobs SET status='partial' WHERE book_id=? AND stage=?", "reader")[:2] == ("partial", False)
    out = r.invoke(app, ["-w", str(ws), "reader-check", epub, "--fake"])
    assert out.exit_code == 0, out.output
    assert db(stage="reader")[:2] == ("succeeded", True)


def test_a_second_reading_is_recorded_for_a_person_and_not_retranslated(conn, cfg, fixtures):
    """ADR-063: a passage the source lets be read two ways is not an error: it is kept as「兩種讀法」for a person to
    look at, never sent back to the translator."""
    from ncte.gui import data
    from ncte.llm.schemas import ProofreadIssue

    seen = []

    def proofread(payload, req):
        seen.append(payload)
        p = payload["paragraphs"][0]
        return {"issues": [{"unit_id": p["unit_id"], "kind": "ambiguity", "severity": "medium", "quote": p["zh"][:4],
                            "problem": "原文也可以讀成在比年紀", "fix": "另一種讀法：比年紀"}]}

    assert "ambiguity" in ProofreadIssue.model_fields["kind"].annotation.__args__
    fake = FakeLLM(responders={**RESPONDERS, "proofread": proofread})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    found = conn.execute("SELECT unit_id, type, severity, evidence FROM qa_findings WHERE run_id=? AND "
                         "type LIKE 'review:proofread%'", (rid,)).fetchall()
    assert [(f["type"], f["severity"]) for f in found] == [("review:proofread_ambiguity", "low")] * len(seen)
    assert all("比年紀" in f["evidence"] for f in found)
    # no retranslation was asked for those paragraphs
    marks = ",".join("?" * len(found))
    assert not conn.execute(f"SELECT 1 FROM translations WHERE unit_id IN ({marks}) AND flags_json LIKE "
                            "'%review_retranslation%'", [f["unit_id"] for f in found]).fetchone()
    job = conn.execute("SELECT checkpoint_json FROM jobs WHERE run_id=? AND stage='proofread'", (rid,)).fetchone()
    assert '"ambiguities": %d' % len(seen) in job["checkpoint_json"] and '"retranslated": 0' in job["checkpoint_json"]
    # the review page shows it as a second reading
    shown = [u for u in data.units(conn, book)["units"] if u["readings"]]
    assert {u["id"] for u in shown} == {f["unit_id"] for f in found}
    assert "比年紀" in shown[0]["readings"][0]
