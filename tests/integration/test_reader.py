"""ADR-038: a reader's look at the Chinese; what a reader cannot follow goes back with the source."""

from ncte.llm.fake import FakeLLM
from ncte.translation.cache import latest_texts

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_reader_points_go_back_to_the_translator(conn, cfg, fixtures):
    picked = {}

    def reader(payload, req):
        p = payload["paragraphs"][len(payload["paragraphs"]) // 2]
        picked.setdefault("uid", p["unit_id"])
        assert "characters" in payload and all("zh" in x and "source" not in x for x in payload["paragraphs"])
        return {"issues": [{"unit_id": picked["uid"], "kind": "unclear", "severity": "medium",
                            "problem": "讀者看不懂這個指涉"},
                           {"unit_id": "u_elsewhere", "kind": "other", "severity": "high", "problem": "x"}]}

    fake = FakeLLM(responders={**RESPONDERS, "reader": reader})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    stages = [c.stage.split(":")[0] for c in fake.calls]
    assert "reader" in stages and stages.index("reader") > max(i for i, x in enumerate(stages) if x == "translate"
                                                               and i < stages.index("reader"))
    best = latest_texts(conn, book.id)
    assert "review_retranslation" in best[picked["uid"]]["flags_json"]  # sent back with the reader's point
    feedback = [c for c in fake.payloads if c.get("review_feedback")]
    assert feedback and "讀者" in feedback[-1]["review_feedback"][0]["issues"][0]["suggestion"]


def test_reader_check_command(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "reader-check", epub, "--fake"])
    assert out.exit_code == 0, out.output
    assert "reader's look:" in out.output


def test_each_stretch_after_the_first_reads_the_end_of_the_one_before(conn, cfg, fixtures, monkeypatch):
    """2.4: like proofreading, a stretch gets the last paragraphs (Chinese only) of the stretch before in the same
    chapter, to read, not to report on; also when a paused pass resumes."""
    from ncte.analysis import reader as rd

    monkeypatch.setattr(rd, "CHUNK_UNITS", 3)
    seen = []

    def reader(payload, req):
        seen.append(payload)
        return {"issues": []}

    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "reader": reader}),
                            mode="context")
    assert len(seen) > 2 and "context_before" not in seen[0]
    later = [p for p in seen[1:] if "context_before" in p]
    assert later and all(1 <= len(p["context_before"]) <= 6 and set(p["context_before"][0]) == {"zh"} for p in later)
    # the second stretch reads the end of the first when both are in one chapter
    parts = rd.chunks(conn, book.id)
    if parts[0][-1]["_href"] == parts[1][0]["_href"]:
        assert seen[1]["context_before"][-1]["zh"] == parts[0][-1]["zh"]
    # resumed after the first stretch, the second still gets it
    tail = rd.tail(conn, book.id, parts[0][-1]["_at"])
    assert tail and tail[-1]["unit_id"] == parts[0][-1]["unit_id"]
    from ncte.llm.prompt_loader import load_prompt
    assert "context_before" in load_prompt("reader").render({})
