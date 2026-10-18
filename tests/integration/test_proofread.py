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
