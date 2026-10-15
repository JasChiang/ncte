"""ADR-045: an unnamed first-person narrator gets a card while reading and is matched after reading."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def _first_person(answer_merge=None):
    seen = {"known": [], "cards": [], "verify": []}

    def scene(payload, req):
        out = RESPONDERS["scene_analyze"](payload, req)
        known = payload.get("known_characters", [])
        seen["known"].append([k for k in known if k.get("narrator")])
        narr = next((k["character_id"] for k in known if k.get("narrator")), "new_narrator")
        out["narration"] = {"person": "first", "narrator_ref": narr, "focal_ref": None, "evidence_unit_ids": []}
        for u in out.get("utterances", [])[:1]:
            u["speaker_ref"], u["resolution_status"], u["speaker_candidates"] = narr, "inferred", [
                {"character_ref": narr, "evidence_types": ["action"], "evidence_unit_ids": [u["unit_id"]], "note": "我"}]
        return out

    def translate(payload, req):
        seen["cards"].append([c for c in payload.get("characters", []) if c.get("narrator")])
        from ncte.llm.fake import DEFAULTS

        return (RESPONDERS.get("translate") or DEFAULTS["translate"])(payload, req)

    def verify(payload, req):
        seen["verify"].append(payload)
        items = []
        for i in payload["items"]:
            ans = "keep" if i["kind"] != "narrator" else "unknown"  # an answer verify does not act on
            if i["kind"] == "narrator" and answer_merge and i["candidates"]:
                ans = next(iter(i["candidates"]))
            items.append({"ref": i["ref"], "answer": ans, "reason": "t"})
        return {"items": items}

    return seen, {**RESPONDERS, "scene_analyze": scene, "translate": translate, "verify": verify}


def test_unnamed_narrator_gets_a_card_while_reading(conn, cfg, fixtures):
    seen, responders = _first_person()
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=responders), mode="context")
    assert s.status == "completed"
    cards = conn.execute("SELECT * FROM characters WHERE book_id=? AND is_narrator=1", (book.id,)).fetchall()
    assert len(cards) == 1 and cards[0]["canonical_name"] == "（敘述者）"
    assert any(seen["known"][1:])  # later scenes see the card as the narrator
    said = conn.execute("SELECT COUNT(*) FROM utterances WHERE speaker_id_nullable=?", (cards[0]["id"],)).fetchone()[0]
    assert said >= 1
    assert any(seen["cards"])  # the translator is told who 「我」 is
    assert not conn.execute("SELECT 1 FROM glossary WHERE source_term LIKE '（敘述者%'").fetchone()  # never a name to use
    narr = [i for p in seen["verify"] for i in p["items"] if i["kind"] == "narrator"]
    assert narr and narr[0]["candidates"]  # matched against the named characters after reading


def test_narrator_card_matched_to_a_named_character(conn, cfg, fixtures):
    seen, responders = _first_person(answer_merge=True)
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=responders), mode="context")
    assert s.status == "completed"
    assert not conn.execute("SELECT 1 FROM characters WHERE book_id=? AND canonical_name='（敘述者）'", (book.id,)).fetchone()
    merged = conn.execute("SELECT * FROM characters WHERE book_id=? AND is_narrator=1", (book.id,)).fetchone()
    assert merged is not None and merged["canonical_name"] != "（敘述者）"  # the named character carries the lines now
