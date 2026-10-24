"""ADR-106: a table-of-contents entry (the book's own contents page, a NAV / NCX label) follows the translation of the
chapter heading with the same source, through later fixes and edits of the heading; a bonus story after the
afterword is story."""

from __future__ import annotations

import zipfile

import pytest

from ncte.exporter import export_book
from ncte.library import import_book
from ncte.llm.fake import FakeLLM
from ncte.samples import CH1_SCENE1, CH2, BookSpec, Doc, _section, write_book
from ncte.translation.cache import latest_translation
from ncte.translation.edits import apply_human_edit
from ncte.translation.headings import follow_headings, heading_texts, key

from ..helpers import resume_run, run_book
from ..scripted import RESPONDERS

CH1, BONUS = "第一章　銀の鍵", "電子書籍版特典『この英雄と賢者は特別な訓練を受けています』"


@pytest.fixture
def toc_book(tmp_path):
    toc = "".join(f'<p><a href="{h}">{t}</a></p>' for h, t in (("ch1.xhtml", CH1), ("ch2.xhtml", "第二章　丘の上で"),
                                                               ("bonus.xhtml", BONUS)))
    docs = [Doc("toc", "Text/toc.xhtml", "目次", f"<h1>目次</h1>\n{toc}"),
            Doc("ch1", "Text/ch1.xhtml", CH1, _section(CH1, CH1_SCENE1, None, "c1")),
            Doc("ch2", "Text/ch2.xhtml", "第二章　丘の上で", _section("第二章　丘の上で", CH2, None, "c2")),
            Doc("after", "Text/after.xhtml", "あとがき", "<h1>あとがき</h1>\n<p>　読んでくださってありがとうございます。</p>"),
            Doc("bonus", "Text/bonus.xhtml", BONUS, _section(BONUS, CH1_SCENE1[:3], None, "b1")),
            Doc("colophon", "Text/colophon.xhtml", "奥付", "<p>二〇二六年十月二十四日　初版発行</p>")]
    return write_book(BookSpec("目次の本", "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-000000000106", docs, with_ncx=True),
                      tmp_path / "toc.epub")


def _units(conn, book_id, plain):
    return {r["kind"] if r["spine_index"] is not None or r["kind"] != "text" else "x": r for r in conn.execute(
        "SELECT * FROM units WHERE book_id=? AND plain_text=? ORDER BY ordinal", (book_id, plain))}


def _text(conn, r):
    tr = latest_translation(conn, r["id"], r["source_hash"])
    return tr["text"] if tr else None


def _inner(text):
    return text.removeprefix("⟦1⟧").removesuffix("⟦/1⟧")


def test_a_bonus_story_after_the_afterword_is_story():
    from ncte.epub.matter import classify

    hrefs = ["toc.xhtml", "c1.xhtml", "after.xhtml", "bonus.xhtml", "bonus2.xhtml", "colophon.xhtml"]
    labels = {"toc.xhtml": "目次", "c1.xhtml": "第一章", "after.xhtml": "あとがき", "bonus.xhtml": BONUS,
              "colophon.xhtml": "奥付"}
    assert classify(hrefs, labels) == {"toc.xhtml": "front", "c1.xhtml": "story", "after.xhtml": "back",
                                       "bonus.xhtml": "story", "bonus2.xhtml": "story", "colophon.xhtml": "back"}


def test_toc_entries_take_the_final_text_of_their_heading(conn, cfg, toc_book, tmp_path):
    book, rid, s = run_book(conn, cfg, toc_book, FakeLLM(responders=RESPONDERS), mode="context")
    assert s.status == "completed"
    from ncte.epub.matter import story_hrefs

    story = story_hrefs(conn, book.id)
    for title in (CH1, BONUS):
        rows = conn.execute("SELECT * FROM units WHERE book_id=? AND plain_text=? ORDER BY ordinal",
                            (book.id, title)).fetchall()
        heading = next(r for r in rows if r["kind"] == "heading")
        assert heading["resource_href"] in story  # the bonus chapter after the afterword too
        entries = [r for r in rows if r["id"] != heading["id"]]
        # the contents page's paragraph, the NAV label, the NCX label, the chapter file's <title>
        assert {r["kind"] for r in entries} >= {"text", "nav", "ncx"}
        zh = _text(conn, heading)
        for r in entries:
            assert _inner(_text(conn, r)) == zh, r["kind"]
            tr = latest_translation(conn, r["id"], r["source_hash"])
            assert tr["context_hash"] == f"heading:{heading['id']}"

    # a person corrects the heading later (as a proofread fix would): the entries follow it on export
    heading = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?",
                           (book.id, BONUS)).fetchone()
    apply_human_edit(conn, book.id, heading["id"], "電子書版特典《這對英雄與賢者受過特殊訓練》")
    out = export_book(conn, cfg, book, output=tmp_path / "out.epub", run_epubcheck=False)
    with zipfile.ZipFile(out.result.output_path) as z:
        nav = z.read("OEBPS/Text/nav.xhtml").decode()
        page = z.read("OEBPS/Text/toc.xhtml").decode()
        ncx = z.read("OEBPS/toc.ncx").decode()
    for doc in (nav, page, ncx):
        assert "這對英雄與賢者" in doc
    # a person's own wording of an entry is kept
    entry = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='nav' AND plain_text=?",
                         (book.id, BONUS)).fetchone()
    apply_human_edit(conn, book.id, entry["id"], "⟦1⟧特典短篇⟦/1⟧")
    export_book(conn, cfg, book, output=tmp_path / "out2.epub", run_epubcheck=False)
    assert _text(conn, entry) == "⟦1⟧特典短篇⟦/1⟧"

    # resumed / run again: a following entry is not asked again for itself
    from ncte.translation.orchestrator import RunOptions, create_run

    from ..helpers import orchestrator_for

    fake = FakeLLM(responders=RESPONDERS)
    opts = RunOptions(model=cfg.openrouter.model, client="fake", mode="context")
    rid2 = create_run(conn, cfg, book, opts)
    assert orchestrator_for(conn, cfg, book, rid2, opts, fake).run().status == "completed"
    page_entry = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='text' AND plain_text=? AND resource_href "
                              "LIKE '%toc.xhtml'", (book.id, CH1)).fetchone()
    job = conn.execute("SELECT status FROM jobs WHERE run_id=? AND stage='translate' AND target_id=?",
                       (rid2, page_entry["id"])).fetchone()
    assert job["status"] == "skipped"
    assert resume_run(conn, cfg, rid2, fake).status == "completed"


def test_an_entry_follows_only_a_clear_match(conn, cfg, toc_book):
    book, _ = import_book(conn, cfg, toc_book)
    from ncte.epub.matter import story_hrefs

    story = story_hrefs(conn, book.id)
    assert heading_texts(conn, book.id, story) == {}  # nothing translated: nothing to follow
    ch1 = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?", (book.id, CH1)).fetchone()
    apply_human_edit(conn, book.id, ch1["id"], "第一章　銀之鑰")
    assert key("第一章 銀の鍵") == key(CH1) == key("第一章　￹銀￺ぎん￻の鍵")  # whitespace, readings
    from ncte.memory.db import Transaction

    with Transaction(conn):
        got = follow_headings(conn, book.id)
    entries = conn.execute("SELECT * FROM units WHERE book_id=? AND plain_text=? AND id!=?", (book.id, CH1, ch1["id"])
                           ).fetchall()
    assert got["followed"] == {r["id"] for r in entries} and got["written"] == len(entries)
    assert all(_inner(_text(conn, r)) == "第一章　銀之鑰" for r in entries)
    with Transaction(conn):
        assert follow_headings(conn, book.id)["written"] == 0  # already so: nothing written again
    # a second story heading with the same source, translated differently: which one an entry means is not clear
    bonus = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?",
                         (book.id, BONUS)).fetchone()
    conn.execute("UPDATE units SET plain_text=?, source_text=?, source_hash=? WHERE id=?",
                 (CH1, CH1, ch1["source_hash"], bonus["id"]))
    apply_human_edit(conn, book.id, bonus["id"], "第一章　銀色的鑰匙")
    assert key(CH1) not in heading_texts(conn, book.id, story)
