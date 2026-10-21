"""ADR-085: chapters are cut into scenes side by side (each reads only its own paragraphs, so the result is the same as
one after another); a chapter's first scene says where the chapter came from; a scene's translation request sees the
end of the scene before."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def _breaks(conn, book_id):
    return sorted((r[0], r[1], r[2]) for r in conn.execute(
        "SELECT unit_id, origin, reason FROM scene_breaks WHERE book_id=?", (book_id,)))


def test_chapters_cut_side_by_side_give_what_one_after_another_gives(conn, cfg, fixtures, tmp_path):
    from ncte.memory.db import connect

    cfg.openrouter.max_concurrency = 1
    book, _, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    one_by_one = _breaks(conn, book.id)
    cfg.openrouter.max_concurrency = 4
    cfg.app.workspace = str(tmp_path / "ws2")
    conn2 = connect(cfg.workspace / "ncte.db")
    book2, _, s2 = run_book(conn2, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    assert s.status == s2.status == "completed"
    assert one_by_one and _breaks(conn2, book2.id) == one_by_one


def test_a_chapter_start_says_it_came_from_the_toc_not_the_model(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    starts = [r for r in _breaks(conn, book.id) if r[1] == "chapter"]
    assert starts and all(r[2] == "章節開頭（目錄）" for r in starts)


def test_a_scene_request_sees_the_end_of_the_scene_before(conn, cfg, fixtures):
    from ncte.llm.fake import DEFAULTS

    seen = []

    def translate(payload, req):
        seen.append(payload)
        return (RESPONDERS.get("translate") or DEFAULTS["translate"])(payload, req)

    cfg.translation.scene_start_context_chars = 600
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "translate": translate}),
                          mode="context")
    scene_of = {r[0]: r[1] for r in conn.execute("SELECT id, scene_id FROM units WHERE book_id=?", (book.id,))}
    later = [p for p in seen if p.get("units") and p.get("context_before")
             and scene_of.get(p["context_before"][-1]["unit_id"]) != scene_of.get(p["units"][0]["unit_id"])]
    assert later, "no scene start saw the scene before"
    ordinal = {r[0]: r[1] for r in conn.execute("SELECT id, ordinal FROM units WHERE book_id=?", (book.id,))}
    for p in later:
        before = conn.execute("SELECT COUNT(*) FROM units WHERE book_id=? AND ordinal<? AND translatable=1 AND kind IN "
                              "('text','heading') AND spine_index IS NOT NULL",
                              (book.id, ordinal[p["units"][0]["unit_id"]])).fetchone()[0]
        got = [c for c in p["context_before"]]
        plain = {r[0]: r[1] for r in conn.execute("SELECT id, plain_text FROM units WHERE book_id=?", (book.id,))}
        chars = sum(len(plain[c["unit_id"]]) for c in got)
        # the end of the scene before: up to 600 characters, or all there is (never fewer than the old two)
        assert len(got) >= min(2, before) and (chars >= 600 or len(got) == min(before, 40))
    cfg.translation.scene_start_context_chars = 0  # off: the old two paragraphs
    seen.clear()
    import tempfile
    from pathlib import Path as P

    from ncte.memory.db import connect
    cfg.app.workspace = str(P(tempfile.mkdtemp()) / "ws")
    c2 = connect(cfg.workspace / "ncte.db")
    run_book(c2, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "translate": translate}), mode="context")
    assert all(len(p.get("context_before", [])) <= 2 for p in seen)
