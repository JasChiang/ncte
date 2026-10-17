"""One story position, one set of names: a name revealed later never reaches a request about an earlier passage,
whatever field carries it (cards, speakers, who a line is said to, forms of address, whose eyes the scene follows)."""

import json

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS, known, memory_update, scene_analyze, uid, utt

LATER = ("リリア", "莉莉亞")  # revealed in chapter 3


def analyze(payload, req):
    out = scene_analyze(payload, req)
    if uid(payload, "フードをかぶった少女が立っていた"):
        out["new_characters"].append({"temp_id": "new_7", "name_ja": "フードをかぶった少女", "aliases_ja": [],
                                      "proposed_name_zh": "戴兜帽的少女",
                                      "evidence_unit_ids": [uid(payload, "フードをかぶった少女が立っていた")]})
        kaito = known(payload, "カイト")
        said = utt(payload, "名乗るほどの者じゃない", "new_7", "inferred", ("action",))
        asked = utt(payload, "誰だ、お前", kaito, "inferred", ("address",))
        asked["to"] = ["new_7"]  # Kaito asks her
        out["utterances"] = [x for x in [*out["utterances"], said, asked] if x]
        out["narration"] = {"person": "third", "narrator_ref": None, "focal_ref": "new_7", "evidence_unit_ids": [],
                            "self_forms": []}
    hood = known(payload, "フードをかぶった少女")
    for nc in out["new_characters"]:
        if nc["name_ja"] == "リリア" and hood:
            nc["same_as_ref"] = hood  # chapter 3 reveals who she is
    return out


def remember(payload, req):
    out = memory_update(payload, req)
    hood, kaito = known(payload, "フードをかぶった少女"), known(payload, "カイト")
    if hood and kaito and (u := uid(payload, "誰だ、お前")):
        out["address_terms"] = [{"speaker_ref": kaito, "addressee_ref": hood, "term": "お前", "term_zh": "妳",
                                 "note": "", "evidence_unit_ids": [u], "evidence_quote": "誰だ、お前"}]
    return out


def test_an_early_scene_never_sees_a_later_name(conn, cfg, fixtures):
    fake = FakeLLM(responders={**RESPONDERS, "scene_analyze": analyze, "memory_update": remember})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    lilia = conn.execute("SELECT id FROM characters WHERE canonical_name='リリア'").fetchone()
    assert lilia is not None  # merged with the hooded girl at the reveal
    assert conn.execute("SELECT 1 FROM address_terms WHERE addressee_id=?", (lilia[0],)).fetchone()
    early = [p for p, c in zip(fake.payloads, fake.calls, strict=True) if c.stage == "translate"
             and any("名乗るほどの者" in u["source"] for u in p["units"])]
    assert early
    for p in early:
        blob = json.dumps({k: v for k, v in p.items() if k != "units"}, ensure_ascii=False)
        assert not any(n in blob for n in LATER), blob
        assert "戴兜帽的少女" in blob or "フードをかぶった少女" in blob  # she is there, by the name known so far
    # the bilingual proofreading of chapter 1 names the speaker as the passage knows her
    proof = [p for p, c in zip(fake.payloads, fake.calls, strict=True) if c.stage == "proofread"]
    ch1 = [x for p in proof for x in p["paragraphs"] if "名乗るほどの者" in x["ja"]]
    assert ch1 and not any(n in ch1[0].get("speaker", "") for n in LATER)
    cards = [c for p in proof for c in p["characters"]
             if any("名乗るほどの者" in x["ja"] for x in p["paragraphs"])]
    assert cards and not any(n in json.dumps(cards, ensure_ascii=False) for n in LATER)
