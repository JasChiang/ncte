"""ADR-086: someone the read pass names in an action whose doer the source leaves out — a quoted person who is not in
the scene (「〜って言われて」) — has their card (with gender) in the scene's translation and proofreading requests."""

from ncte.llm.fake import DEFAULTS, FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_a_quoted_person_not_in_the_scene_gets_a_card(conn, cfg, fixtures):
    picked: dict[str, str] = {}  # scene id → the absent person put in an action
    seen = {"translate": [], "proofread": []}

    def scene(payload, req):
        out = RESPONDERS["scene_analyze"](payload, req)
        text = "".join(u["source"] for u in payload["units"])
        absent = [k for k in payload.get("known_characters", [])
                  if k.get("name_ja") and k["name_ja"] not in text]
        if absent and payload["units"]:
            out["actions"] = [{"unit_id": payload["units"][0]["unit_id"], "summary": "被說了一句（轉述）",
                               "subject_candidates": [absent[0]["character_id"]], "object_candidates": [],
                               "resolution_status": "inferred"}]
            picked[payload["scene_id"]] = absent[0]["name_ja"]
        return out

    def translate(payload, req):
        seen["translate"].append(payload)
        return (RESPONDERS.get("translate") or DEFAULTS["translate"])(payload, req)

    def proofread(payload, req):
        seen["proofread"].append(payload)
        return {"issues": []}

    fake = FakeLLM(responders={**RESPONDERS, "scene_analyze": scene, "translate": translate, "proofread": proofread})
    book, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed" and picked, "the fixture has no scene without some known person"
    from ncte.analysis.scene import get_scene
    from ncte.memory.retrieval import memory_context

    checked = 0
    for sid, name in picked.items():
        first = conn.execute("SELECT id FROM units WHERE book_id=? AND scene_id=? AND translatable=1 AND kind='text' "
                             "ORDER BY ordinal LIMIT 1",
                             (book.id, sid)).fetchone()[0]
        rows = conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? AND translatable=1 ORDER BY ordinal",
                            (book.id, sid)).fetchall()
        if not memory_context(conn, book.id, scene=get_scene(conn, book.id, sid), batch_rows=rows)["characters"]:
            continue  # no one is known by a name there yet (visibility): nothing to add a card to
        checked += 1
        req = [p for p in seen["translate"] if any(u["unit_id"] == first for u in p.get("units", []))]
        assert req and any(c["name_ja"] == name for c in req[0]["characters"]), (sid, name)
        proof = [p for p in seen["proofread"] if any(x["unit_id"] == first for x in p["paragraphs"])]
        assert proof and any(c["ja"] == name for c in proof[0]["characters"]), (sid, name)
    assert checked
