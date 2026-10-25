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


def _follow(conn, book_id):
    from ncte.memory.db import Transaction

    with Transaction(conn):
        return follow_headings(conn, book_id)


def test_a_copy_of_a_heading_no_longer_clear_is_dropped(conn, cfg, toc_book):
    """Codex review: two headings 「第一章…」 read the same, the entries copy them; one heading is corrected later and
    the match is no longer clear: the entries' copies are not their text any more, nor counted as translated."""
    from ncte.translation.cache import is_cached

    book, _ = import_book(conn, cfg, toc_book)
    ch1 = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?", (book.id, CH1)).fetchone()
    bonus = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?",
                         (book.id, BONUS)).fetchone()
    nav = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='nav' AND plain_text=?", (book.id, CH1)).fetchone()
    # the NAV entry's own translation first, as a run makes it
    conn.execute("INSERT INTO translations(unit_id, target_lang, text, source_hash, prompt_version, model_id, "
                 "glossary_version, mode, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (nav["id"], "zh-TW", "⟦1⟧第一章　自己的譯法⟦/1⟧", nav["source_hash"], "translate/v1#x", "m", "g",
                  "context", "succeeded", "2026-10-25"))
    conn.execute("UPDATE units SET plain_text=?, source_text=?, source_hash=? WHERE id=?",
                 (CH1, CH1, ch1["source_hash"], bonus["id"]))
    apply_human_edit(conn, book.id, ch1["id"], "第一章　銀之鑰")
    apply_human_edit(conn, book.id, bonus["id"], "第一章　銀之鑰")
    assert nav["id"] in _follow(conn, book.id)["followed"]
    assert _inner(_text(conn, nav)) == "第一章　銀之鑰"

    apply_human_edit(conn, book.id, bonus["id"], "第一章　銀色的鑰匙")
    assert nav["id"] in _follow(conn, book.id)["dropped"]
    assert _text(conn, nav) == "⟦1⟧第一章　自己的譯法⟦/1⟧"  # its own again
    from ncte.gui.data import chapters

    titles = [c["title"] for c in chapters(conn, book)]  # the web page's chapter list too, not the copy taken off
    assert "第一章　自己的譯法" in titles and "第一章　銀之鑰" not in titles
    tr = latest_translation(conn, nav["id"], nav["source_hash"])
    assert not (tr["context_hash"] or "").startswith("heading:")
    copies = conn.execute("SELECT status FROM translations WHERE unit_id=? AND context_hash LIKE 'heading:%'",
                          (nav["id"],)).fetchall()
    assert copies and {r["status"] for r in copies} == {"unfollowed"}
    assert not is_cached(tr, prompt_version="translate/v1#y", model="other", mode="context", gl_hash="g")


def test_an_untranslatable_heading_with_the_same_source_blocks_the_copy(conn, cfg, toc_book):
    """Codex review: of two headings 「第一章…」 one is not translated at all (an image in it): which one an entry
    means is not clear, so no entry takes the other one's text."""
    book, _ = import_book(conn, cfg, toc_book)
    ch1 = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?", (book.id, CH1)).fetchone()
    bonus = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?",
                         (book.id, BONUS)).fetchone()
    conn.execute("UPDATE units SET plain_text=?, source_text=?, source_hash=?, translatable=0 WHERE id=?",
                 (CH1, CH1, ch1["source_hash"], bonus["id"]))
    apply_human_edit(conn, book.id, ch1["id"], "第一章　銀之鑰")
    from ncte.epub.matter import story_hrefs

    assert key(CH1) not in heading_texts(conn, book.id, story_hrefs(conn, book.id))
    assert _follow(conn, book.id)["written"] == 0


def test_a_title_whose_copy_is_dropped_is_translated_again(conn, cfg, toc_book):
    """Codex review: the NAV / NCX labels only ever held copies; one of two same-source headings is changed: the next
    run translates those labels for themselves (their jobs were skipped for the copies)."""
    from ncte.translation.orchestrator import RunOptions, create_run

    from ..helpers import orchestrator_for

    book, _, s = run_book(conn, cfg, toc_book, FakeLLM(responders=RESPONDERS), mode="context")
    assert s.status == "completed"
    ch1 = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?", (book.id, CH1)).fetchone()
    bonus = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?",
                         (book.id, BONUS)).fetchone()
    labels = conn.execute("SELECT * FROM units WHERE book_id=? AND kind IN ('nav','ncx') AND plain_text=?",
                          (book.id, CH1)).fetchall()
    assert labels and all(conn.execute("SELECT COUNT(*) FROM translations WHERE unit_id=? AND (context_hash IS NULL OR "
                                       "context_hash NOT LIKE 'heading:%')", (r["id"],)).fetchone()[0] == 0
                          for r in labels)  # only copies
    conn.execute("UPDATE units SET plain_text=?, source_text=?, source_hash=? WHERE id=?",
                 (CH1, CH1, ch1["source_hash"], bonus["id"]))
    apply_human_edit(conn, book.id, bonus["id"], "第一章　另一種譯法")
    fake = FakeLLM(responders=RESPONDERS)
    opts = RunOptions(model=cfg.openrouter.model, client="fake", mode="context")
    rid2 = create_run(conn, cfg, book, opts)
    assert orchestrator_for(conn, cfg, book, rid2, opts, fake).run().status == "completed"
    for r in labels:
        tr = latest_translation(conn, r["id"], r["source_hash"])
        assert tr is not None and not (tr["context_hash"] or "").startswith("heading:"), r["kind"]
        job = conn.execute("SELECT status FROM jobs WHERE run_id=? AND stage='translate' AND target_id=?",
                           (rid2, r["id"])).fetchone()
        assert job["status"] == "succeeded"


def _ambiguous(conn, book_id):
    """Give the bonus heading 第一章's source and another Chinese: the NAV / NCX labels of 第一章 no longer follow."""
    ch1 = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?", (book_id, CH1)).fetchone()
    bonus = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' AND plain_text=?",
                         (book_id, BONUS)).fetchone()
    conn.execute("UPDATE units SET plain_text=?, source_text=?, source_hash=? WHERE id=?",
                 (CH1, CH1, ch1["source_hash"], bonus["id"]))
    apply_human_edit(conn, book_id, bonus["id"], "第一章　另一種譯法")
    return conn.execute("SELECT * FROM units WHERE book_id=? AND kind IN ('nav','ncx') AND plain_text=?",
                        (book_id, CH1)).fetchall()


def _own(conn, r):
    tr = latest_translation(conn, r["id"], r["source_hash"])
    return tr is not None and not (tr["context_hash"] or "").startswith("heading:")


def test_a_title_taken_off_its_heading_is_translated_however_its_job_was_settled(conn, cfg, toc_book, tmp_path):
    """Codex review (third round): the copy taken off by an export while the run is paused, and a title whose job
    succeeded by the copy, are translated for themselves when the run goes on."""
    from ncte.translation.orchestrator import RunOptions, create_run

    from ..helpers import orchestrator_for

    book, rid, s = run_book(conn, cfg, toc_book, FakeLLM(responders=RESPONDERS), mode="context")
    assert s.status == "completed"
    # a second run whose jobs are made (the titles skipped for their copies), then paused
    fake = FakeLLM(responders=RESPONDERS)
    opts = RunOptions(model=cfg.openrouter.model, client="fake", mode="context")
    rid2 = create_run(conn, cfg, book, opts)
    orch = orchestrator_for(conn, cfg, book, rid2, opts, fake)
    orch.prepare()
    orch.ensure_jobs()
    labels = _ambiguous(conn, book.id)
    assert {conn.execute("SELECT status FROM jobs WHERE run_id=? AND stage='translate' AND target_id=?",
                         (rid2, r["id"])).fetchone()[0] for r in labels} == {"skipped"}
    export_book(conn, cfg, book, output=tmp_path / "out.epub", run_epubcheck=False)  # takes the copies off
    assert not any(_own(conn, r) for r in labels)
    assert resume_run(conn, cfg, rid2, fake).status == "completed"
    assert all(_own(conn, r) for r in labels)


def test_a_title_whose_job_succeeded_by_its_copy_is_translated_on_resume(conn, cfg, toc_book):
    book, rid, s = run_book(conn, cfg, toc_book, FakeLLM(responders=RESPONDERS), mode="context")
    assert s.status == "completed"
    labels = _ambiguous(conn, book.id)
    assert {conn.execute("SELECT status FROM jobs WHERE run_id=? AND stage='translate' AND target_id=?",
                         (rid, r["id"])).fetchone()[0] for r in labels} == {"succeeded"}
    assert resume_run(conn, cfg, rid, FakeLLM(responders=RESPONDERS)).status == "completed"
    assert all(_own(conn, r) for r in labels)


def test_a_person_edited_title_is_not_sent_again_when_its_old_copy_is_taken_off(conn, cfg, toc_book):
    from ncte.translation.orchestrator import RunOptions, create_run

    from ..helpers import orchestrator_for

    book, rid, s = run_book(conn, cfg, toc_book, FakeLLM(responders=RESPONDERS), mode="context")
    labels = conn.execute("SELECT * FROM units WHERE book_id=? AND kind IN ('nav','ncx') AND plain_text=?",
                          (book.id, CH1)).fetchall()
    for r in labels:
        apply_human_edit(conn, book.id, r["id"], "⟦1⟧第一章　人工⟦/1⟧" if r["source_text"].startswith("⟦1⟧") else
                         "第一章　人工")
    _ambiguous(conn, book.id)
    fake = FakeLLM(responders=RESPONDERS)
    opts = RunOptions(model=cfg.openrouter.model, client="fake", mode="context")
    rid2 = create_run(conn, cfg, book, opts)
    assert orchestrator_for(conn, cfg, book, rid2, opts, fake).run().status == "completed"
    for r in labels:
        assert conn.execute("SELECT status FROM jobs WHERE run_id=? AND stage='translate' AND target_id=?",
                            (rid2, r["id"])).fetchone()[0] == "skipped"
        assert "人工" in _text(conn, r)
    sent = {u for (j,) in conn.execute("SELECT unit_ids_json FROM llm_requests WHERE run_id=? AND stage='translate'",
                                        (rid2,)) for u in __import__("json").loads(j or "[]")}
    assert not sent & {r["id"] for r in labels}
