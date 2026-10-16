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
