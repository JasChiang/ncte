"""What the judging steps are handed (ADR-067, second batch): the data the reading and the translation already have,
sent to the step that judges, each as far as that step may see. The program only sends data, matches strings and
keeps positions; it judges nothing."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS, uid


def _stage(client, stage):
    return [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage.split(":")[0] == stage]


def test_review_gets_the_scenes_facts_and_summaries_and_its_questions(conn, cfg, fixtures):
    """Review reads the facts and summaries the translator had for the scene (chosen the same way, before the scene
    begins) and the read pass's questions about the reviewed paragraphs."""
    def analyze(payload, req):
        out = RESPONDERS["scene_analyze"](payload, req)
        if u := uid(payload, "カイトは目をそらした"):
            out["uncertainties"] = [{"unit_ids": [u], "question": "鑰匙為何在米娜的包包裡？"}]
        return out

    cfg.review.automatic_review = "all"
    client = FakeLLM(responders={**RESPONDERS, "scene_analyze": analyze})
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    reviews = _stage(client, "review")
    later = next(p for p in reviews if any("カイトは目をそらした" in i["source"] for i in p["items"]))
    first = next(p for p in _stage(client, "translate")
                 if any("カイトは目をそらした" in u["source"] for u in p["units"]) and "review_feedback" not in p)
    assert later["memory"] == first["memory"]  # the same memory, as visible at the scene's start
    assert later["memory"]["previous_chapter_summary"] or later["memory"]["this_chapter_so_far"]
    assert [q["question"] for q in later["scene"]["uncertainties"]] == ["鑰匙為何在米娜的包包裡？"]
    opening = next(p for p in reviews if any("朝の市場" in i["source"] for i in p["items"]))
    assert "memory" not in opening  # nothing was known before the book's first scene


REVIEW_MEMORY = ("- `memory`（可能不存在）：這一場開始以前已揭露的劇情記憶：`previous_chapter_summary`（上一章回顧）、"
                 "`this_chapter_so_far`（本章前面的摘要）、`facts`（挑出來和這一場相關的事實，`source` 是它的原文依據）、"
                 "`props`（物品與持有者）。標示 `inferred` 或 `unknown` 的**不是確定事實**。事實與摘要只用來理解人物與前情，"
                 "**不可改變本句原文的意思、對象或方向**（本句怎麼說，以本句原文為準）；譯文沒有寫出這些背景不是錯。")


def _line_only_with(task, line, key, value):
    from ncte.llm.prompt_loader import load_prompt

    p = load_prompt(task)
    static, extra = p.render_parts({key: value})
    assert line not in static and line in extra, task  # after the shared (cached) part, only with the data
    assert line not in p.render({"items": []}), task


def test_the_review_prompt_says_what_the_memory_is_for():
    _line_only_with("review", REVIEW_MEMORY, "memory", {"facts": []})
