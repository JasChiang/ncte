"""Kin words carry a layer the Japanese often leaves out (ADR-094): which side of the family, which age, by marriage.
The relations step writes it when the source states it; translation and proofreading decide it from those records."""

from ncte.llm.fake import FakeLLM
from ncte.llm.prompt_loader import load_prompt
from ncte.memory.db import utcnow

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_proofreading_cards_carry_how_the_people_of_the_stretch_are_related(conn, cfg, fixtures):
    """Proofreading judges 爺爺 or 外公 against the relations the reading recorded: each person of the stretch carries
    them, as of its start, named as the stretch knows them."""
    from ncte.analysis import proofread as pr

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    ids = {r["canonical_name"]: r["id"] for r in conn.execute("SELECT id, canonical_name FROM characters WHERE book_id=?",
                                                              (book.id,))}
    a, b = ids["カイト"], ids["ミナ"]
    conn.execute("DELETE FROM relations")
    conn.execute("INSERT INTO relations(book_id, a_id, b_id, relation_zh, a_is, b_is, since_ordinal, fact_ids_json, reason, "
                 "created_at, origin) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (book.id, a, b, "祖孫", "祖父（父親那邊）", "孫女", 1, "[]", "", utcnow(), "llm"))
    rows = conn.execute("SELECT id, ordinal, plain_text FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal",
                        (book.id,)).fetchall()
    part = [{"unit_id": r["id"], "ja": r["plain_text"], "_at": r["ordinal"], "_who": [a, b]} for r in rows[:5]]
    cards = {c["ja"]: c for c in pr.cast(conn, book.id, part)}
    assert any("祖父（父親那邊）" in x for x in cards["カイト"]["relations"])
    assert any("孫女" in x for x in cards["ミナ"]["relations"])
    assert all("_id" not in c for c in cards.values())


def test_a_kin_word_s_side_is_decided_from_the_records_by_every_step_that_writes_or_judges_it():
    """One rule, in the words each step reads: relations write the side when the source states it; translation
    takes it from the cards and memory, else a word that does not assert it; proofreading counts a guessed side as
    an addition (the shared partial)."""
    assert "父方の祖父母" in load_prompt("relations").text
    tr = load_prompt("translate").text
    assert "お祖父ちゃん是爺爺還是外公" in tr and "祖父母" in tr
    for task in ("translate", "proofread", "review"):
        assert "外公與爺爺" in load_prompt(task).text  # the addition partial
    assert "`relations`（他和這一段其他人物的關係" in load_prompt("proofread").text
