"""Scenes inside a chapter, cut by the model in overlapping windows; the author's marks are candidates."""

from ncte.analysis import scene_windows as sw
from ncte.llm.fake import FakeLLM
from ncte.llm.schemas import SceneWindowsOutput
from ncte.samples import BookSpec, Doc, _section, paras, write_book, xhtml

from ..helpers import run_book
from ..scripted import RESPONDERS

UID = "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-0000000000d{}"


def _rows(texts, *, sep=(), blank=()):
    return [{"id": f"u{i}", "ordinal": i, "plain_text": t, "is_separator": int(i in sep), "blank_before": 2 if i in blank
             else 0, "resource_href": "a.xhtml", "kind": "text"} for i, t in enumerate(texts)]


def test_a_window_keeps_its_breaks_and_shows_the_unfinished_scene_again(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW_PARAGRAPHS", 10)
    rows = _rows([f"　段落{i}。" for i in range(25)])
    asked: list[list[str]] = []

    def ask(pl):
        ids = [p["unit_id"] for p in pl["paragraphs"]]
        asked.append(ids)
        wanted = [u for u in ("u4", "u8", "u16") if u in ids[1:]]
        return SceneWindowsOutput.model_validate({"first_kind": "flashback", "breaks": [
            {"unit_id": u, "kind": "present", "sure": True, "new_chapter": False, "reason": "t"} for u in wanted]})

    breaks, first = sw.cut_chapter(rows, ask, title="一章", known=True, max_units=80)
    assert [i for i, _ in breaks] == [4, 8, 16] and first == "flashback"
    assert asked[1][0] == "u8"  # the scene begun at u8 had not ended: the next window starts there again
    assert all(len(set(a)) == len(a) for a in asked) and asked[-1][-1] == "u24"


def test_the_program_drops_breaks_it_cannot_accept():
    rows = _rows(["　地の文。"] * 6 + ["「まだ続く", "続きの台詞」"] + ["　地の文。"] * 6 + ["◇", "　翌朝。"] + ["　地の文。"] * 4,
                 sep=(14,))
    got = sw.cut_chapter(rows, lambda pl: SceneWindowsOutput.model_validate({"breaks": [
        {"unit_id": u, "reason": "t"} for u in ("u0", "u2", "u7", "u15")]}), title="", known=True, max_units=80)[0]
    # u0 is the first paragraph, u2 leaves a 2-paragraph scene, u7 is inside a quote; u15 (after the separator) moves
    # onto the separator, which opens the scene
    assert [i for i, _ in got] == [14]


def test_an_unanswered_window_keeps_the_author_marks_as_unsure():
    rows = _rows(["　地の文。"] * 12, sep=(4,), blank=(9,))
    got = sw.cut_chapter(rows, lambda pl: None, title="", known=True, max_units=80)[0]
    assert [(i, b.sure) for i, b in got] == [(4, False), (9, False)]


def _book(tmp_path, body, nav=True, n=1):
    docs = [Doc("ch1", "Text/ch1.xhtml", "第一章", body)]
    extra = {} if nav else {"OEBPS/Text/nav.xhtml": xhtml("目次", '<nav epub:type="toc" id="toc"><ol></ol></nav>').encode()}
    return write_book(BookSpec("場面の本", UID.format(n), docs, extra_files=extra), tmp_path / f"s{n}.epub")


def test_blank_lines_inside_one_exchange_are_not_a_scene_break(conn, cfg, tmp_path):
    lines = ["　ミナは腕を組んで言った。", "「カイト、遅いですよ」", "", "", "「悪い悪い」",  # blank lines mid-exchange
             "　カイトは頭をかいた。", "「次は気をつけて」", "◇", "　翌朝、雨だった。", "　ミナは窓の外を見た。",
             "　三年前のあの日も、雨だった。", "　カイトはまだ子供だった。", "　二人は軒下で雨宿りをした。"]
    body = _section("第一章", lines[:7], lines[8:], "c1").replace("＊　＊　＊", "◇")

    def windows(payload, req):
        ps = payload["paragraphs"]
        sep = next(p for p in ps if "separator" in p.get("marks", []))
        past = next(p for p in ps if p["text"].startswith("三年前"))
        return {"first_kind": "present", "breaks": [
            {"unit_id": sep["unit_id"], "kind": "present", "sure": True, "new_chapter": False, "reason": "翌朝"},
            {"unit_id": past["unit_id"], "kind": "flashback", "sure": False, "new_chapter": False, "reason": "三年前"}]}

    fake = FakeLLM(responders={**RESPONDERS, "scene_windows": windows})
    book, _, s = run_book(conn, cfg, _book(tmp_path, body), fake, mode="context")
    assert s.status == "completed"
    scenes = conn.execute("SELECT id, kind, break_reason FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    first = [r[0] for r in conn.execute("SELECT plain_text FROM units WHERE book_id=? AND scene_id=? AND kind='text' "
                                        "ORDER BY ordinal", (book.id, scenes[0]["id"]))]
    assert "「悪い悪い」" in first and "「次は気をつけて」" in first  # one exchange, one scene
    assert [(r["kind"], r["break_reason"]) for r in scenes] == [("present", "chapter"), ("present", "llm"),
                                                                ("flashback", "llm")]
    read = [p for p in fake.payloads if p.get("task") == "scene_analyze"]
    kinds = {p["scene_id"]: p.get("scene_kind") for p in read}
    assert list(kinds.values()) == [None, None, "flashback"]  # the reader knows a flashback


def test_a_book_without_a_toc_gets_its_chapters_from_the_model(conn, cfg, tmp_path):
    lines = ["　第一話　朝"] + [f"　朝の段落{i}。" for i in range(6)] + ["　第二話　夜"] + [f"　夜の段落{i}。" for i in range(6)]
    body = "<section>" + paras(lines) + "</section>"

    def windows(payload, req):
        assert payload["chapter"]["known"] is False
        night = next(p for p in payload["paragraphs"] if "第二話" in p["text"])
        return {"first_kind": "present", "breaks": [
            {"unit_id": night["unit_id"], "kind": "present", "sure": True, "new_chapter": True, "reason": "第二話"}]}

    book, _, s = run_book(conn, cfg, _book(tmp_path, body, nav=False, n=2),
                          FakeLLM(responders={**RESPONDERS, "scene_windows": windows}), mode="context")
    assert s.status == "completed"
    chs = conn.execute("SELECT source, start_ordinal FROM chapters WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    assert [r[0] for r in chs] == ["model", "model"]
    night = conn.execute("SELECT ordinal FROM units WHERE book_id=? AND plain_text LIKE '%第二話%'", (book.id,)).fetchone()[0]
    assert chs[1][1] == night
