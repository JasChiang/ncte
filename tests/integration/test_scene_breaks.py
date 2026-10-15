"""ADR-036: the model chooses where scenes begin inside long stretches the author did not divide."""

from ncte.analysis import scene_breaks as sb
from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


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
    # a second run does not ask again (scene ids are in use once read)
    fake2 = FakeLLM(responders={**RESPONDERS, "scenes": scenes})
    run_book(conn, cfg, fixtures["basic"], fake2, mode="context", force=True)
    assert "scenes" not in [c.stage for c in fake2.calls]
