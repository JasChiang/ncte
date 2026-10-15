"""ADR-039: relations between characters from the evidence-backed facts."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_relations_cite_facts_and_appear_from_their_evidence(conn, cfg, fixtures):
    seen = {}

    def relations(payload, req):
        seen["p"] = payload
        cs, fs = payload["characters"], payload["facts"]
        if len(cs) < 2 or not fs:
            return {"relations": []}
        f = fs[0]
        a = f["about"][0]
        b = next(c["ref"] for c in cs if c["ref"] != a)
        return {"relations": [
            {"a": a, "b": b, "relation": "同伴", "a_is": "同伴", "b_is": "同伴", "facts": [f["ref"]], "reason": "t"},
            {"a": a, "b": b, "relation": "沒證據", "a_is": "", "b_is": "", "facts": ["f999"], "reason": "t"},
            {"a": a, "b": "c999", "relation": "不明", "a_is": "", "b_is": "", "facts": [f["ref"]], "reason": "t"}]}

    fake = FakeLLM(responders={**RESPONDERS, "relations": relations})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed" and "p" in seen
    stages = [c.stage for c in fake.calls]
    assert stages.index("relations") < stages.index("naming")  # before translating: cards can carry them
    rows = conn.execute("SELECT * FROM relations").fetchall()
    if seen["p"]["facts"] and len(seen["p"]["characters"]) >= 2:
        assert [r["relation_zh"] for r in rows] == ["同伴"]  # uncited and unknown people are dropped
        fact = conn.execute("SELECT MIN(first_revealed_ordinal) FROM facts").fetchone()[0]
        assert rows[0]["since_ordinal"] >= fact


def test_relations_command(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "names", "relations", epub, "--fake"])
    assert out.exit_code == 0, out.output and "relations:" in out.output
