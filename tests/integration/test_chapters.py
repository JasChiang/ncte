"""Chapters come from the TOC's targets, not from files or chapter numbers: two chapters in one file (anchors), one
chapter over two files, a chapter whose title is only in the TOC. A book without a TOC is one chapter until the model
finds its chapters."""

from ncte.library import import_book
from ncte.samples import BookSpec, Doc, paras, write_book, xhtml

UID = "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-0000000000c{}"


def _nav(items: list[tuple[str, str]]) -> bytes:
    lis = "\n".join(f'<li><a href="{h}">{t}</a></li>' for h, t in items)
    return xhtml("目次", f'<nav epub:type="toc" id="toc"><ol>\n{lis}\n</ol></nav>').encode()


def chapters(conn, book):
    return [(r["label"], r["source"], conn.execute("SELECT substr(plain_text,1,8) FROM units WHERE book_id=? AND "
                                                    "ordinal=?", (book.id, r["start_ordinal"])).fetchone()[0])
            for r in conn.execute("SELECT * FROM chapters WHERE book_id=? ORDER BY seq", (book.id,))]


def test_chapters_follow_the_toc_targets(conn, cfg, tmp_path):
    a = ('<h2 id="c1">一</h2>' + paras(["　朝が来た。", "　市場へ行った。"]) +
         '<p id="c2"><br/></p>' + paras(["　夜になった。", "　灯りが消えた。"]))
    b = paras(["　まだ夜は続く。", "　風が吹いた。"])  # the second chapter goes on in the next file
    c = paras(["　翌朝、雨だった。"])  # a chapter whose title is only in the TOC
    spec = BookSpec("章の本", UID.format(1), [Doc("a", "Text/a.xhtml", "a", a, in_toc=False),
                                             Doc("b", "Text/b.xhtml", "b", b, in_toc=False),
                                             Doc("c", "Text/c.xhtml", "c", c, in_toc=False)],
                    extra_files={"OEBPS/Text/nav.xhtml": _nav([("a.xhtml#c1", "第一話"), ("a.xhtml#c2", "第二話"),
                                                               ("c.xhtml", "雨の朝")])})
    book, _ = import_book(conn, cfg, write_book(spec, tmp_path / "ch.epub"))
    got = chapters(conn, book)
    assert [(label, src) for label, src, _ in got] == [("第一話", "toc"), ("第二話", "toc"), ("雨の朝", "toc")]
    assert [t for _, _, t in got] == ["一", "　夜になった。", "　翌朝、雨だった"]
    scenes = conn.execute("SELECT start_ordinal, end_ordinal FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    two = conn.execute("SELECT start_ordinal, end_ordinal FROM chapters WHERE book_id=? AND seq=1", (book.id,)).fetchone()
    assert [tuple(s) for s in scenes if two[0] <= s[0] <= two[1]][0][0] == two[0]  # a chapter starts a scene


def test_a_book_without_a_toc_is_one_chapter_for_the_model_to_divide(conn, cfg, tmp_path):
    spec = BookSpec("目次のない本", UID.format(2), [Doc("a", "Text/a.xhtml", "a", paras(["　一行目。", "　二行目。"]),
                                                    in_toc=False)],
                    extra_files={"OEBPS/Text/nav.xhtml": _nav([])})
    book, _ = import_book(conn, cfg, write_book(spec, tmp_path / "none.epub"))
    assert [(label, src) for label, src, _ in chapters(conn, book)] == [("", "none")]


def test_a_chapter_is_read_and_translated_with_the_recap_of_the_one_before(conn, cfg, tmp_path):
    """The chapter before is recapped (its scene summaries) for reading and translating the next one; this chapter's
    own summaries are "so far", never the recap."""
    from ncte.llm.fake import FakeLLM

    from ..helpers import run_book

    def memory(payload, req):
        first = payload["units"][0]["source"].strip("　")[:4]
        return {"scene_summary": f"摘要：{first}", "facts": [], "glossary_candidates": [], "prop_states": [],
                "name_links": [], "address_terms": [], "wordplay": []}

    one = paras(["　朝が来た。", "　市場へ行った。", "　パンを買った。"])
    two = paras(["　夜になった。", "　灯りが消えた。", "　眠った。"])
    spec = BookSpec("前情の本", UID.format(3), [Doc("a", "Text/a.xhtml", "a", one, in_toc=False),
                                                Doc("b", "Text/b.xhtml", "b", two, in_toc=False)],
                    extra_files={"OEBPS/Text/nav.xhtml": _nav([("a.xhtml", "第一話"), ("b.xhtml", "第二話")])})
    fake = FakeLLM(responders={"memory_update": memory})
    book, _, s = run_book(conn, cfg, write_book(spec, tmp_path / "r.epub"), fake, mode="context")
    assert s.status == "completed"
    read = [p for p in fake.payloads if p.get("task") == "scene_analyze"]
    night = next(p for p in read if any("夜になった" in u["source"] for u in p["units"]))
    assert night["previous_chapter"] == ["摘要：朝が来た"] and "story_so_far" not in night
    tr = next(p for p in fake.payloads if p.get("task") == "translate" and "review_feedback" not in p
              and any("夜になった" in u["source"] for u in p["units"]))
    assert tr["memory"]["previous_chapter_summary"] == ["摘要：朝が来た"] and tr["memory"]["this_chapter_so_far"] == []
