"""How much memory a translation request carries follows how long the batch is (ADR-069): a whole long scene in one
request got the same handful of facts as a short batch, and the facts it needed were crowded out."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_the_fact_quota_follows_the_batch_size():
    from ncte.memory.retrieval import batch_quota

    assert batch_quota(5, 10) == 5 and batch_quota(5, 25) == 5  # a short batch: as before
    assert batch_quota(5, 26) == 10
    assert batch_quota(5, 123) == 20  # at most four times
    assert batch_quota(5, 1000) == 20
    assert batch_quota(0, 123) == 0


def test_the_memory_block_grows_with_the_batch_up_to_a_cap():
    from ncte.memory.retrieval import MEMORY_CHARS_MAX, memory_chars

    assert memory_chars(2400, 10) == 2400
    assert 2400 < memory_chars(2400, 123) <= MEMORY_CHARS_MAX


def _scene_rows(conn, book_id):
    from ncte.analysis.scene import get_scene

    sid = conn.execute("SELECT scene_id FROM units WHERE plain_text LIKE '%丘に来ていた%'").fetchone()[0]
    rows = conn.execute("SELECT * FROM units WHERE scene_id=? ORDER BY ordinal", (sid,)).fetchall()
    return get_scene(conn, book_id, sid), rows


def test_a_long_batch_gets_more_facts_and_a_short_one_the_same(conn, cfg, fixtures, monkeypatch):
    from ncte.memory import retrieval

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    scene, rows = _scene_rows(conn, book.id)
    asked = []
    real = retrieval.select_facts

    def spy(*a, **kw):
        asked.append(kw["limit"])
        return real(*a, **kw)

    monkeypatch.setattr(retrieval, "select_facts", spy)
    retrieval.memory_context(conn, book.id, scene=scene, batch_rows=rows[:10], max_evidence=5)
    long = (rows * 20)[:123]  # a whole long scene in one request (the rows repeat: only their number matters here)
    retrieval.memory_context(conn, book.id, scene=scene, batch_rows=long, max_evidence=5)
    assert asked[0] == 5 and asked[1] > 5


def test_forms_of_address_follow_the_batch_size_too(conn, cfg, fixtures, monkeypatch):
    """The forms of address a request gets were capped at 12 whatever the batch's length (the forms the batch uses,
    most used first): a long scene crowded out some. Their cap follows the batch like the facts' (ADR-069)."""
    from ncte.analysis import proofread
    from ncte.memory import address, retrieval

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    scene, rows = _scene_rows(conn, book.id)
    asked = []
    real = address.context_items

    def spy(*a, **kw):
        asked.append(kw.get("limit", 12))
        return real(*a, **kw)

    monkeypatch.setattr(address, "context_items", spy)
    retrieval.memory_context(conn, book.id, scene=scene, batch_rows=rows[:10])
    retrieval.memory_context(conn, book.id, scene=scene, batch_rows=(rows * 20)[:123])
    part = [{"ja": r["plain_text"], "_at": r["ordinal"], "_who": []} for r in (rows * 20)[:60]]
    proofread.notes(conn, book.id, part, [], glossary=[], volume=1.0, revealed={})
    assert asked == [12, 48, 36]
