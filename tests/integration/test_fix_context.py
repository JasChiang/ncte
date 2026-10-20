"""A fix sees the scene around it (not two paragraphs before and one after: ``fix_context_chars`` a side, which a short
scene fits in whole) and that scene's memory, also when the whole book is reviewed (`ncte review`) and a batch would
span scenes. What a fix carries of the memory in a long scene: ``test_fix_scope``."""

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


def test_a_scene_s_fixes_go_together_each_seeing_its_scene(conn, cfg, tmp_path):
    cfg.review.automatic_review = "all"
    epub = write_book(story_spec(), tmp_path / "story.epub")
    client = FakeLLM(responders={"review": flag(lambda items: [items[0], items[-1]] if len(items) > 4 else [])})
    book, _rid, _ = run_book(conn, cfg, epub, client, mode="context")
    scenes = scene_units(conn, book)
    unit_of = {r["source_text"]: r["id"] for r in conn.execute("SELECT id, source_text FROM units WHERE book_id=?",
                                                               (book.id,))}
    sent = fixes(client)
    assert sent and any(len(p["units"]) > 1 for p in sent)  # first and last of a scene: one request, not two
    for p in sent:
        ids = [u["unit_id"] for u in p["units"]]
        scene = next(s for s in scenes.values() if ids[0] in s)
        assert set(ids) <= set(scene)
        assert "context_before" not in p and "context_after" not in p  # each fix has its own window instead
        for fb in p["review_feedback"]:
            shown = {unit_of[c["source"]] for side in fb["around"].values() for c in side}
            assert shown == set(scene) - set(ids)  # the rest of the (short) scene, within the window
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
    ctx = [c for p in client.payloads[n0:] if p.get("task") == "review" for c in p["context"]]
    assert ctx and {c["at"] for c in ctx} <= {"before", "after", "between"}  # which side of the reviewed lines
    sent = [p for p in client.payloads[n0:] if p.get("task") == "translate" and "review_feedback" in p]
    assert sent and all("scene" in p for p in sent)  # who speaks to whom, the people, the address table


def test_parts_of_a_scene_are_even():
    from ncte.translation.review import even_parts

    assert [len(p) for p in even_parts(list(range(43)), 20)] == [14, 14, 15]
    assert [len(p) for p in even_parts(list(range(62)), 20)] == [15, 16, 15, 16]
    assert [len(p) for p in even_parts(list(range(20)), 20)] == [20] and even_parts([], 20) == []


def test_every_part_of_a_scene_is_reviewed_knowing_its_people(conn, cfg, fixtures, monkeypatch):
    """A scene reviewed in parts: each part gets the people the translator had for the whole scene, not only those
    named up to the part's last paragraph."""
    from ncte.translation import review as rv

    from ..scripted import RESPONDERS

    monkeypatch.setattr(rv, "REVIEW_BATCH", 2)
    cfg.review.automatic_review = "all"
    seen: dict[str, list[set]] = {}
    scene_of: dict[str, str] = {}

    def review(payload, req):
        sids = {scene_of[i["unit_id"]] for i in payload["items"]}
        seen.setdefault(next(iter(sids)), []).append({c["name_ja"] for c in payload["characters"]})
        return {"issues": []}

    client = FakeLLM(responders={**RESPONDERS, "review": review})
    book, _rid, opts = start_run(conn, cfg, fixtures["basic"], client, mode="context")
    scene_of.update({r[0]: r[1] for r in conn.execute("SELECT id, scene_id FROM units WHERE book_id=?", (book.id,))})
    orchestrator_for(conn, cfg, book, _rid, opts, client).run()
    translated = {}
    for p in client.payloads:
        if p.get("task") == "translate" and "review_feedback" not in p and p.get("units"):
            translated[scene_of[p["units"][0]["unit_id"]]] = {c["name_ja"] for c in p.get("characters", [])}
    parted = {s: parts for s, parts in seen.items() if len(parts) > 1 and translated.get(s)}
    assert parted
    for s, parts in parted.items():
        assert all(people >= translated[s] for people in parts)
