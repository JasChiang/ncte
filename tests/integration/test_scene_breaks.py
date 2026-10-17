"""ADR-036: the model chooses where scenes begin inside long stretches the author did not divide. This is the cut of a
book read before chapters came from the TOC (``segmentation`` "files"); new books are cut by chapter
(tests/integration/test_scene_windows.py)."""

import pytest

from ncte.analysis import scene_breaks as sb
from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


@pytest.fixture(autouse=True)
def _files_cut(monkeypatch):
    import ncte.analysis.scene as scene

    monkeypatch.setattr(scene, "segmentation", lambda conn, book_id: "files")


def _rows(texts):
    return [{"id": f"u{i}", "ordinal": i, "plain_text": t} for i, t in enumerate(texts)]


def test_program_checks_the_model_breaks():
    rows = _rows(["　地の文。"] * 30 + ["「まだ続く", "続きの台詞」"] + ["　地の文。"] * 30)
    assert sb.check(rows, ["u31"], 40) is None or "u31" not in sb.check(rows, ["u31"], 40)  # inside a quote
    assert sb.check(rows, ["u20", "u25"], 40) == ["u20"]  # 25 would leave a 5-paragraph scene
    assert sb.check(rows, ["u20"], 40) == ["u20"]
    assert sb.check(rows, [], 20) is None  # one 62-paragraph scene is over twice the limit: keep the program's cuts
    assert sb.check(rows, ["nope"], 40) == []


def test_breaks_are_chosen_before_reading(conn, cfg, fixtures, monkeypatch):
    import ncte.analysis.scene as scene

    cfg.memory.max_scene_units = 5
    monkeypatch.setattr(scene, "MAX_SIZE_SLACK", 0)  # the small fixture is mostly dialogue: cut at the limit
    seen = []

    def scenes(payload, req):
        ps = payload["paragraphs"]
        seen.append(len(ps))
        return {"breaks": [{"unit_id": ps[len(ps) // 2]["unit_id"], "reason": "test"}]}

    fake = FakeLLM(responders={**RESPONDERS, "scenes": scenes})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    stages = [c.stage.split(":")[0] for c in fake.calls]
    assert seen, "the fixture has a stretch longer than the limit"
    assert stages.index("scenes") < stages.index("scene_analyze")
    decided = conn.execute("SELECT COUNT(*) FROM scene_breaks WHERE origin='stretch'").fetchone()[0]
    assert 1 <= decided <= len(seen)  # a stretch whose answer fails the checks keeps the program cuts
    # a second run does not ask again (scene ids are in use once read)…
    fake2 = FakeLLM(responders={**RESPONDERS, "scenes": scenes})
    run_book(conn, cfg, fixtures["basic"], fake2, mode="context")
    assert "scenes" not in [c.stage for c in fake2.calls]
    # …a forced re-read of the whole book chooses again (books cut before the model chose breaks)
    fake3 = FakeLLM(responders={**RESPONDERS, "scenes": scenes})
    _, _, s3 = run_book(conn, cfg, fixtures["basic"], fake3, mode="context", force=True)
    assert s3.status == "completed" and "scenes" in [c.stage for c in fake3.calls]
    orphans = conn.execute("SELECT COUNT(*) FROM chapter_summaries WHERE scene_id NOT IN (SELECT id FROM scenes)").fetchone()[0]
    stale = conn.execute("SELECT COUNT(*) FROM utterances u JOIN units x ON x.id=u.unit_id WHERE u.scene_id IS NOT x.scene_id"
                         ).fetchone()[0]
    assert orphans == 0 and stale == 0  # nothing left behind under scene ids that changed


def test_break_after_a_sentence_that_runs_on_is_asked_again():
    rows = _rows(["　地の文。"] * 20 + ["　彼女を眺めていた時――"] + ["「――叫び声!!」"] + ["　地の文。"] * 20)
    assert sb.runs_on(rows, ["u21", "u10"]) == ["u21"]
    p = sb.payload([{**r, "id": r["id"]} for r in rows], 40, 6000)
    assert p["max_chars"] == 6000 and p["paragraphs"][20]["chars"] == len("彼女を眺めていた時――")


def test_model_answer_on_reconsider_stands(conn, cfg, fixtures, monkeypatch):
    import ncte.analysis.scene as scene
    import ncte.analysis.scene_breaks as breaks

    cfg.memory.max_scene_units = 5
    monkeypatch.setattr(scene, "MAX_SIZE_SLACK", 0)
    monkeypatch.setattr(breaks, "runs_on", lambda rows, b: b[:1])  # every first proposal "runs on"
    asked = []

    def scenes(payload, req):
        asked.append("reconsider" in payload)
        ps = payload["paragraphs"]
        return {"breaks": [] if "reconsider" in payload else [{"unit_id": ps[len(ps) // 2]["unit_id"], "reason": "t"}]}

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "scenes": scenes}), mode="context")
    assert asked and asked.count(True) == asked.count(False)  # each stretch: proposal, then one second look
    assert conn.execute("SELECT COUNT(*) FROM scene_breaks WHERE origin='llm'").fetchone()[0] == 0


def test_rebreak_command_on_a_book_cut_before_the_model_chose(tmp_path, fixtures, monkeypatch):
    from typer.testing import CliRunner

    import ncte.analysis.scene as scene
    from ncte.cli import app

    monkeypatch.setattr(scene, "MAX_SIZE_SLACK", 0)
    (tmp_path / "ncte.yaml").write_text("memory:\n  max_scene_units: 5\ncontext:\n  scene_breaks: program\n")
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    ws = ["-w", str(tmp_path / "ws")]
    assert r.invoke(app, [*ws, "translate", str(fixtures["basic"]), "--mode", "context", "--fake"]).exit_code == 0
    out = r.invoke(app, [*ws, "scenes", str(fixtures["basic"]), "--rebreak", "--fake"])
    assert out.exit_code == 0, out.output
    assert "scene breaks chosen again" in out.output



def test_a_sample_divides_only_the_stretches_it_reaches(conn, cfg, fixtures, monkeypatch):
    """--limit-units: stretches past the sample are not asked about (they cost and the sample never reads them); the
    next run that reaches them divides them, and the ones decided are not asked again."""
    import ncte.analysis.scene as scene

    from ..helpers import orchestrator_for, start_run

    cfg.memory.max_scene_units = 5
    monkeypatch.setattr(scene, "MAX_SIZE_SLACK", 0)
    asked: list[str] = []

    def scenes(payload, req):
        ps = payload["paragraphs"]
        asked.append(ps[0]["unit_id"])
        return {"breaks": [{"unit_id": ps[len(ps) // 2]["unit_id"], "reason": "test"}]}

    fake = FakeLLM(responders={**RESPONDERS, "scenes": scenes})
    book, _, _ = start_run(conn, cfg, fixtures["basic"], fake, mode="context", limit=1)
    stretches = sb.stretches(conn, book.id, cfg.translation.scene_max_chars)
    assert len(stretches) >= 2, "the fixture needs two stretches"
    first = stretches[0][0]
    n = conn.execute("SELECT COUNT(*) FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') "
                     "AND ordinal<=?", (book.id, first["ordinal"])).fetchone()[0]
    _, rid, opts = start_run(conn, cfg, fixtures["basic"], fake, mode="context", limit=n)
    orchestrator_for(conn, cfg, book, rid, opts, fake).run()
    assert asked == [first["id"]]  # only the stretch the sample reaches
    _, rid, opts = start_run(conn, cfg, fixtures["basic"], fake, mode="context")
    orchestrator_for(conn, cfg, book, rid, opts, fake).run()
    assert asked[0] == first["id"] and first["id"] not in asked[1:] and len(asked) > 1  # the rest, once
