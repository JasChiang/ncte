"""A fix sees what the first translation saw: the scene around it (not two paragraphs before and one after), and that
scene's memory, also when the whole book is reviewed (`ncte review`) and a batch would span scenes."""

from ncte.llm.fake import FakeLLM
from ncte.samples import story_spec, write_book

from ..helpers import orchestrator_for, run_book, start_run


def flag(pick):
    """A reviewer that finds one serious error on the units ``pick`` chooses from each request."""
    def review(payload, req):
        return {"issues": [{"unit_id": i["unit_id"], "issue_type": "mistranslation", "severity": "high",
                            "evidence": "原文是否定", "suggestion": "改成否定", "needs_retranslation": True}
                           for i in pick(payload["items"])]}
    return review


def fixes(client):
    return [p for p in client.payloads if p.get("task") == "translate" and "review_feedback" in p]


def scene_units(conn, book):
    out: dict[str, list[str]] = {}
    for r in conn.execute("SELECT id, scene_id FROM units WHERE book_id=? AND translatable=1 AND kind IN "
                          "('text','heading') ORDER BY ordinal", (book.id,)):
        out.setdefault(r["scene_id"], []).append(r["id"])
    return out


def test_a_fix_sees_its_scene_and_fixes_far_apart_are_sent_apart(conn, cfg, tmp_path):
    cfg.review.automatic_review = "all"
    epub = write_book(story_spec(), tmp_path / "story.epub")
    client = FakeLLM(responders={"review": flag(lambda items: [items[0], items[-1]] if len(items) > 4 else [])})
    book, _rid, _ = run_book(conn, cfg, epub, client, mode="context")
    scenes = scene_units(conn, book)
    sent = fixes(client)
    assert sent
    for p in sent:
        (uid,) = [u["unit_id"] for u in p["units"]]  # first and last of a scene: two requests, not one
        scene = next(ids for ids in scenes.values() if uid in ids)
        shown = {c["unit_id"] for c in p["context_before"] + p["context_after"]}
        assert shown == set(scene) - {uid}  # the rest of the (short) scene, not two paragraphs and one
        assert "scene" in p  # the scene's memory


def test_reviewing_the_whole_book_asks_about_one_scene_at_a_time(conn, cfg, tmp_path):
    cfg.review.automatic_review = "off"
    epub = write_book(story_spec(), tmp_path / "story.epub")
    client = FakeLLM()
    book, _rid, _ = run_book(conn, cfg, epub, client, mode="context")
    scene_of = {u: s for s, ids in scene_units(conn, book).items() for u in ids}
    assert len({s for s in scene_of.values() if s}) > 1

    asked: list[set] = []

    def review(payload, req):
        asked.append({scene_of.get(i["unit_id"]) for i in payload["items"]})
        return flag(lambda items: items[::5])(payload, req)

    client.responders["review"] = review
    cfg.review.automatic_review = "all"
    book2, rid2, opts2 = start_run(conn, cfg, epub, client, mode="context", review_only=True)
    n0 = len(client.payloads)
    orchestrator_for(conn, cfg, book2, rid2, opts2, client).run()
    assert asked and all(len(s) == 1 for s in asked)
    sent = [p for p in client.payloads[n0:] if p.get("task") == "translate" and "review_feedback" in p]
    assert sent and all("scene" in p for p in sent)  # who speaks to whom, the people, the address table
