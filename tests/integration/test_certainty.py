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
    # a fact on its subject's own card reads as before; on another's card it says whose it is (雷德：…)
    cards = [c for st, p in sent if st == "impressions" for c in p["characters"]]
    late_cards = {c["name"]: f["text"] for c in cards for f in c["facts"] if "常常遲到" in f["text"]}
    assert list(late_cards) == ["海斗"] and "：" not in late_cards["海斗"]  # on his own card, no name before it
    # a line put to someone only by how it sounds is marked for the house style, which must not make it a habit
    # (「俺様に任せろ」 is ミナ imitating him); one placed by the action beside it is not a guess
    hs = [p for st, p in sent if st == "house_style"]
    assert hs
    kaito = next(c for c in hs[0]["characters"] if c["ja"] == "カイト")
    by_voice = [x for x in kaito["lines"] if "俺様に任せろ" in x["ja"]]
    by_action = [x for x in kaito["lines"] if "悪い悪い" in x["ja"]]
    assert by_voice and by_voice[0].get("guess") is True
    assert by_action and "guess" not in by_action[0]


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


def test_what_held_in_another_time_says_so_everywhere(conn, cfg, fixtures):
    """A reincarnation story: 「前世の宿敵」 and 「the subordinate of a past life」 are not the story's present. A fact or
    relation read with its time shows it in every later task; 「母親」 of the past life is not merged with this one's."""
    from ..scripted import known, memory_update, uid

    def read(payload, req):
        out = memory_update(payload, req)
        kaito, mina = known(payload, "カイト"), known(payload, "ミナ")
        hand = uid(payload, "カイトは銀の鍵をミナに手渡した")
        if hand and kaito and mina:
            out["facts"] = [{"subject_ref": kaito, "predicate": "是", "object": "敵國的將軍", "fact_type": "objective",
                             "certainty": "explicit", "evidence_unit_ids": [hand], "evidence_quote": "カイトは銀の鍵",
                             "source_viewpoint": None, "retracts_fact_ids": [], "period": "前世"}]
            out["relations"] = [{"a_ref": kaito, "b_ref": mina, "relation": "宿敵", "a_is": "宿敵", "b_is": "宿敵",
                                 "kind": "feeling", "evidence_unit_ids": [hand], "evidence_quote": "カイトは銀の鍵",
                                 "period": "前世"}]
        return out

    fake = FakeLLM(responders={**RESPONDERS, "memory_update": read})
    book, _, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    f = conn.execute("SELECT * FROM facts WHERE book_id=? AND period='前世'", (book.id,)).fetchone()
    assert f is not None and qualified_fact(f, "是敵國的將軍", {}) == "〔前世〕是敵國的將軍"
    n = conn.execute("SELECT * FROM relation_notes WHERE book_id=? AND period='前世'", (book.id,)).fetchone()
    assert n["relation_zh"] == "前世：宿敵" and n["a_is"] == "前世：宿敵"
    later = [json.dumps(p, ensure_ascii=False) for p, c in zip(fake.payloads, fake.calls, strict=True)
             if c.stage in ("impressions", "relations")]
    assert any("〔前世〕" in t or "前世：宿敵" in t for t in later)


def test_the_same_word_for_another_person_gets_a_card_of_its_own(conn, cfg, fixtures):
    from ncte.analysis.scene import apply_scene_analysis
    from ncte.library import import_book
    from ncte.llm.schemas import SceneAnalysisOutput
    from ncte.memory.repository import upsert_character

    book, _ = import_book(conn, cfg, fixtures["basic"])
    word = "ミナ"
    unit = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='text' AND plain_text LIKE '%ミナ%' ORDER BY ordinal "
                        "LIMIT 1", (book.id,)).fetchone()
    scene = conn.execute("SELECT * FROM scenes WHERE book_id=? AND id=?", (book.id, unit["scene_id"])).fetchone()
    mother = upsert_character(conn, book.id, word, name_zh=None, aliases=[], unit_id=unit["id"], ordinal=0, origin="t")[0]
    out = SceneAnalysisOutput.model_validate({
        "pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [], "props": [],
        "uncertainties": [], "new_characters": [{"temp_id": "new_1", "name_ja": word, "aliases_ja": [],
                                                 "proposed_name_zh": "", "evidence_unit_ids": [unit["id"]],
                                                 "other_person": True, "period": "前世"}]})
    apply_scene_analysis(conn, book.id, scene, out)
    cards = {(r[0], r[1]): r[2] for r in conn.execute("SELECT canonical_name, label, id FROM characters WHERE book_id=?",
                                                       (book.id,))}
    assert cards[(word, "")] == mother and (word, "前世") in cards  # the book's words, its time as the label (ADR-103)


def test_another_person_met_first_in_the_scene_still_gets_the_time_label(conn, cfg, fixtures):
    """The past-life 「母親」 mentioned before the present one in the same scene: the order of mention must not give
    her the bare word (and then fold the present mother into her card)."""
    from ncte.analysis.scene import apply_scene_analysis
    from ncte.library import import_book
    from ncte.llm.schemas import SceneAnalysisOutput

    book, _ = import_book(conn, cfg, fixtures["basic"])
    word = "ミナ"
    unit = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='text' AND plain_text LIKE '%ミナ%' ORDER BY ordinal "
                        "LIMIT 1", (book.id,)).fetchone()
    scene = conn.execute("SELECT * FROM scenes WHERE book_id=? AND id=?", (book.id, unit["scene_id"])).fetchone()
    nc = lambda t, other, period: {"temp_id": t, "name_ja": word, "aliases_ja": [], "proposed_name_zh": "",  # noqa: E731
                                   "evidence_unit_ids": [unit["id"]], "other_person": other, "period": period}
    out = SceneAnalysisOutput.model_validate({
        "pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [], "props": [],
        "uncertainties": [], "new_characters": [nc("new_1", True, "前世"), nc("new_2", False, "")]})
    apply_scene_analysis(conn, book.id, scene, out)
    cards = {(r[0], r[1]) for r in conn.execute("SELECT canonical_name, label FROM characters WHERE book_id=?", (book.id,))}
    assert {(word, ""), (word, "前世")} <= cards


def test_a_flashback_does_not_change_who_holds_a_prop_now(conn, cfg, fixtures):
    """Read in order: 鍵 is with A now; a later flashback says it was with B three years before. Now it is still A's;
    the flashback state is kept apart, with its time."""
    from ncte.library import import_book
    from ncte.memory.db import utcnow
    from ncte.memory.repository import upsert_character
    from ncte.memory.visibility import visible_props

    book, _ = import_book(conn, cfg, fixtures["basic"])
    u = conn.execute("SELECT id FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()[0]
    a = upsert_character(conn, book.id, "甲", name_zh=None, aliases=[], unit_id=u, ordinal=0, origin="t")[0]
    b = upsert_character(conn, book.id, "乙", name_zh=None, aliases=[], unit_id=u, ordinal=0, origin="t")[0]
    for holder, at, period in ((a, 10, None), (b, 20, "三年前")):
        conn.execute("INSERT INTO prop_states(book_id, name_ja, holder_character_id, evidence_unit_ids_json, horizon_ordinal, "
                     "created_at, period) VALUES (?,?,?,?,?,?,?)", (book.id, "鍵", holder, "[]", at, utcnow(), period))
    states = {(p["period"] or ""): p["holder_character_id"] for p in visible_props(conn, book.id, 30)}
    assert states == {"": a, "三年前": b}
