"""4.3 (ADR-065): ``context.read_in_one_request`` (off by default) reads a scene with one request whose answer holds
both the scene analysis and the memory update; the program applies each as before (the analysis first, then the
memory, with the new characters' temporary ids turned into the cards the analysis made)."""

from __future__ import annotations

from typing import Any

from ncte.llm.fake import FakeLLM
from ncte.llm.prompt_loader import load_prompt
from ncte.llm.schemas import SceneReadOutput, output_model
from ncte.memory.db import connect, migrate

from ..helpers import run_book
from ..scripted import RESPONDERS, memory_update, scene_analyze


def scene_read(payload: dict[str, Any], req: Any) -> dict[str, Any]:
    """Like the model: it reads the scene once; in the memory part a person met in this scene is named by the
    temporary id its analysis gave them."""
    analysis = scene_analyze(payload, req)
    seen = [{"character_id": c["temp_id"], "name_ja": c["name_ja"]} for c in analysis["new_characters"]]
    memory = memory_update({**payload, "known_characters": [*payload.get("known_characters", []), *seen]}, req)
    return {"analysis": analysis, "memory": memory}


class Counting(FakeLLM):
    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.asked: dict[str, int] = {}

    def complete(self, req: Any) -> Any:
        st = req.stage.split(":")[0]
        self.asked[st] = self.asked.get(st, 0) + 1
        return super().complete(req)


def _memory(conn, book_id):
    names = {r[0]: r[1] for r in conn.execute("SELECT id, canonical_name FROM characters WHERE book_id=?", (book_id,))}
    facts = sorted((names.get(r[0], r[0]), r[1], r[2], r[3]) for r in conn.execute(
        "SELECT subject, predicate, object, certainty FROM facts WHERE book_id=?", (book_id,)))
    address = sorted((names[r[0]], names[r[1]], r[2]) for r in conn.execute(
        "SELECT speaker_id, addressee_id, form_ja FROM address_terms WHERE book_id=?", (book_id,)))
    speakers = sorted((r[0], r[1], names.get(r[2]), r[3]) for r in conn.execute(
        "SELECT unit_id, quote_index, speaker_id_nullable, status FROM utterances WHERE book_id=?", (book_id,)))
    return sorted(names.values()), facts, address, speakers


def test_off_by_default_and_the_schema_holds_both_answers():
    from ncte.config import Config

    assert Config().context.read_in_one_request is False
    assert output_model("scene_read") is SceneReadOutput
    assert list(SceneReadOutput.model_json_schema()["properties"]) == ["analysis", "memory"]
    text = load_prompt("scene_read").render({})
    assert "<!--" not in text
    for task in ("scene_analyze", "memory_update"):  # the rules of both prompts, none added
        body = load_prompt(task).text
        rules = [ln for ln in body.splitlines() if ln[:2] in {f"{n}." for n in range(1, 10)} or ln[:3] in
                 {f"{n}." for n in range(10, 20)} or ln.startswith("   - ")]
        assert rules
        for ln in rules:
            assert ln in text, (task, ln[:40])


def test_one_request_a_scene_and_the_memory_is_written_as_with_two(cfg, fixtures, tmp_path):
    results = {}
    for one in (False, True):
        cfg.context.read_in_one_request = one
        conn = connect(tmp_path / f"r{int(one)}.db")
        migrate(conn)
        client = Counting(responders={**RESPONDERS, "scene_read": scene_read})
        book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
        assert summary.status == "completed", summary
        scenes = conn.execute("SELECT COUNT(*) FROM scenes WHERE book_id=?", (book.id,)).fetchone()[0]
        jobs = {tuple(r) for r in conn.execute("SELECT stage, status FROM jobs WHERE run_id=? AND stage IN "
                                               "('scene_analyze','memory_update')", (rid,))}
        assert jobs == {("scene_analyze", "succeeded"), ("memory_update", "succeeded")}
        if one:
            assert client.asked.get("scene_read") == scenes
            assert not client.asked.get("scene_analyze") and not client.asked.get("memory_update")
        else:
            assert client.asked.get("scene_analyze") == scenes == client.asked.get("memory_update")
            assert not client.asked.get("scene_read")
        results[one] = _memory(conn, book.id)
        conn.close()
    two, one_ = results[False], results[True]
    assert one_[0] == two[0]  # the same characters
    assert one_[3] == two[3]  # the same speakers
    assert ("カイト", "把銀之鑰交給", "ミナ", "explicit") in one_[1]  # a temporary id became the card
    assert one_[1] == two[1] and one_[2] == two[2] and one_[2]  # the same facts and forms of address


def test_the_one_request_uses_the_reading_steps_model_and_reasoning():
    from ncte.config import Config, reasoning_setting, stage_model, use_claude_models

    cfg = Config()
    cfg.openrouter.provider = "claude"
    use_claude_models(cfg)
    assert stage_model(cfg, "scene_read", cfg.openrouter.model) == stage_model(cfg, "scene_analyze", "x") \
        == "claude/sonnet"  # read with the reading model, not the translating one
    cfg.openrouter.stage_reasoning["scene_analyze"] = "low"
    assert reasoning_setting(cfg, "scene_read") == "low"
    cfg.openrouter.stage_models["scene_read"] = "claude/opus"
    cfg.openrouter.stage_reasoning["scene_read"] = "high"
    assert stage_model(cfg, "scene_read", "x") == "claude/opus" and reasoning_setting(cfg, "scene_read") == "high"
