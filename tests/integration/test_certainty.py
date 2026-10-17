"""Whose view and how sure travel with a fact or a relation into every later step: 「米娜認為海斗是她哥哥」 never
reaches a later task as 「海斗是她哥哥」, and a line only guessed to be someone's does not set their habits."""

import json

from ncte.llm.fake import FakeLLM
from ncte.memory.repository import qualified_fact, relation_guess

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_a_belief_or_a_guess_is_marked_as_such():
    names = {"c_mina": "米娜"}
    belief = {"fact_type": "character_belief", "source_viewpoint": "c_mina", "certainty": "explicit"}
    assert qualified_fact(belief, "海斗是她哥哥", names) == "〔米娜認為〕海斗是她哥哥"
    guess = {"fact_type": "objective", "source_viewpoint": None, "certainty": "inferred"}
    assert qualified_fact(guess, "海斗是她哥哥", names) == "海斗是她哥哥（推測）"
    told = {"fact_type": "narrator_claim", "source_viewpoint": None, "certainty": "explicit"}
    assert qualified_fact(told, "海斗是她哥哥", names) == "〔敘述者的說法〕海斗是她哥哥"
    plain = {"fact_type": "objective", "source_viewpoint": None, "certainty": "explicit"}
    assert qualified_fact(plain, "海斗是她哥哥", names) == "海斗是她哥哥"
    assert relation_guess({"certainty": "inferred"}) == "（推測）" and relation_guess({"certainty": None}) == ""


def test_later_steps_see_beliefs_and_guesses_marked(conn, cfg, fixtures):
    fake = FakeLLM(responders=RESPONDERS)
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    sent = [(c.stage, p) for p, c in zip(fake.payloads, fake.calls, strict=True)]
    # the scripted read records 「ミナ thinks Kaito is often late」 as an inferred belief
    imp = [f["text"] for st, p in sent if st == "impressions" for c in p["characters"] for f in c["facts"]]
    late = [t for t in imp if "常常遲到" in t]
    assert late and all(t.startswith("〔ミナ認為〕") and t.endswith("（推測）") for t in late)
    # a line read as someone's (not stated) is marked for the house style, which must not make it a habit
    hs = [p for st, p in sent if st == "house_style"]
    assert hs
    kaito = next(c for c in hs[0]["characters"] if c["ja"] == "カイト")
    guessed = [x for x in kaito["lines"] if "悪い悪い" in x["ja"]]
    assert guessed and guessed[0].get("guess") is True


def test_a_guessed_relation_reaches_the_translator_marked(conn, cfg, fixtures):
    from ncte.analysis.scene import get_scene
    from ncte.memory.db import utcnow
    from ncte.memory.retrieval import memory_context

    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    mina, kaito = (conn.execute("SELECT id FROM characters WHERE canonical_name=?", (n,)).fetchone()[0]
                   for n in ("ミナ", "カイト"))
    conn.execute("INSERT INTO relations(book_id, a_id, b_id, relation_zh, a_is, b_is, since_ordinal, fact_ids_json, "
                 "reason, created_at, kind, certainty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (book.id, kaito, mina, "兄妹", "哥哥", "妹妹", 0, "[]", "t", utcnow(), "identity", "inferred"))
    sid = conn.execute("SELECT scene_id FROM units WHERE plain_text LIKE '%丘に来ていた%'").fetchone()[0]
    scene = get_scene(conn, book.id, sid)
    rows = conn.execute("SELECT * FROM units WHERE scene_id=? ORDER BY ordinal", (sid,)).fetchall()
    ctx = memory_context(conn, book.id, scene=scene, batch_rows=rows)
    rels = [r for c in ctx["characters"] for r in c.get("relations", [])]
    assert rels and all(r.get("certainty") == "inferred" for r in rels), json.dumps(rels, ensure_ascii=False)
