"""A later scene can only retract what it is shown: the memory update gets the earlier facts with their ids; a
character's belief is not erased when the truth comes out (the misunderstanding happened), only by their own change
of mind."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS, known, memory_update, uid


def test_the_memory_update_sees_earlier_facts_and_retracts_by_their_ids(conn, cfg, fixtures):
    seen: list = []

    def remember(payload, req):
        out = memory_update(payload, req)
        seen.append(payload)
        kaito = known(payload, "カイト")
        if (u := uid(payload, "目をそらした")) and kaito:  # chapter 2
            facts = {f["predicate"]: f["fact_id"] for f in payload.get("known_facts", [])}
            out["facts"] = [
                # the truth (as the story tells it) does not erase what Mina believed …
                {"subject_ref": kaito, "predicate": "其實很守時", "object": "", "fact_type": "objective",
                 "certainty": "explicit", "evidence_unit_ids": [u], "evidence_quote": "カイトは目をそらした",
                 "source_viewpoint": None, "retracts_fact_ids": [facts["似乎"]]},
                # … but a fact the story itself refutes is retracted
                {"subject_ref": kaito, "predicate": "沒有交出", "object": "銀の鍵", "fact_type": "objective",
                 "certainty": "explicit", "evidence_unit_ids": [u], "evidence_quote": "カイトは目をそらした",
                 "source_viewpoint": None, "retracts_fact_ids": [facts["把銀之鑰交給"]]},
            ]
        return out

    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": remember}),
                            mode="context")
    ch2 = next(p for p in seen if any("目をそらした" in u["source"] for u in p["units"]))
    shown = {f["predicate"]: f for f in ch2["known_facts"]}
    assert {"把銀之鑰交給", "似乎"} <= set(shown)
    assert shown["似乎"]["fact_type"] == "character_belief" and shown["似乎"]["certainty"] == "inferred"
    belief = conn.execute("SELECT retracted_by FROM facts WHERE predicate='似乎'").fetchone()
    assert belief["retracted_by"] is None  # Mina's belief stays part of the story
    assert conn.execute("SELECT 1 FROM memory_rejections WHERE reason LIKE 'a belief is ended only%'").fetchone()
    handed = conn.execute("SELECT retracted_by FROM facts WHERE predicate='把銀之鑰交給'").fetchone()
    assert handed["retracted_by"] is not None
