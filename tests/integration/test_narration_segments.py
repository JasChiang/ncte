"""ADR-083: a stretch of a scene told in another voice (a storyteller, a letter) is recorded with evidence and reaches
the translator and proofreading for those paragraphs only; the narrator rules leave it alone."""

from ncte.llm.fake import DEFAULTS, FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def _setup(segment):
    seen = {"translate": [], "proofread": []}

    def scene(payload, req):
        out = RESPONDERS["scene_analyze"](payload, req)
        units = [u["unit_id"] for u in payload["units"]]
        out["narration"] = {"person": "first", "narrator_ref": "new_narrator", "focal_ref": None,
                            "evidence_unit_ids": units[:1], "segments": segment(units)}
        return out

    def translate(payload, req):
        seen["translate"].append(payload)
        return (RESPONDERS.get("translate") or DEFAULTS["translate"])(payload, req)

    def proofread(payload, req):
        seen["proofread"].append(payload)
        return {"issues": []}

    return seen, {**RESPONDERS, "scene_analyze": scene, "translate": translate, "proofread": proofread}


def test_a_stretch_in_another_voice_is_kept_and_shown_where_it_is(conn, cfg, fixtures):
    seen, responders = _setup(lambda u: [{"unit_ids": [u[1], u[2]], "voice": "仿古說書旁白", "teller_ref": None,
                                          "person": "third", "evidence_unit_ids": [u[1]]}] if len(u) > 3 else [])
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=responders), mode="context")
    assert s.status == "completed"
    import json

    stored = [g for (st,) in conn.execute("SELECT state_json FROM scenes WHERE book_id=?", (book.id,))
              for g in (json.loads(st).get("narration") or {}).get("segments") or []]
    assert stored and all(len(g["units"]) == 2 and g["voice"] == "仿古說書旁白" and "teller" not in g for g in stored)
    told = {u for g in stored for u in g["units"]}
    with_seg = [p for p in seen["translate"] if (p.get("narration") or {}).get("segments")]
    assert with_seg
    for p in with_seg:  # only paragraphs the request shows, as its own codes
        shown = {x["unit_id"] for x in p["units"]} | {x["unit_id"] for x in p.get("context_before", [])} | \
                {x["unit_id"] for x in p.get("context_after", [])}
        assert all(set(g["units"]) <= shown for g in p["narration"]["segments"])
        assert p["narration"]["person"] == "first"  # the scene's own narration is still told
    paras = [x for p in seen["proofread"] for x in p["paragraphs"]]
    marked = [x for x in paras if x.get("told_as")]
    assert marked and len(marked) == len(told) and all("說的人原文沒交代" in x["told_as"] for x in marked)


def test_a_stretch_citing_nothing_of_the_scene_is_not_kept(conn, cfg, fixtures):
    seen, responders = _setup(lambda u: [{"unit_ids": [u[0]], "voice": "信件", "evidence_unit_ids": ["P999"]},
                                         {"unit_ids": ["P998"], "voice": "日記", "evidence_unit_ids": [u[0]]}])
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=responders), mode="context")
    assert s.status == "completed"
    assert not conn.execute("SELECT 1 FROM scenes WHERE book_id=? AND state_json LIKE '%\"segments\"%'",
                            (book.id,)).fetchone()
    assert not any((p.get("narration") or {}).get("segments") for p in seen["translate"])


def test_split_reading_keeps_every_parts_stretches():
    from ncte.analysis.split_read import merge_outputs
    from ncte.llm.schemas import SceneAnalysisOutput

    def part(seg):
        return {"pov": None, "location": None, "time": None, "new_characters": [], "participants": [],
                "utterances": [], "actions": [], "props": [], "uncertainties": [],
                "narration": {"person": "first", "segments": [seg]}}

    merged = merge_outputs([SceneAnalysisOutput.model_validate(part(x)) for x in (
        {"unit_ids": ["a"], "voice": "信件", "evidence_unit_ids": ["a"]},
        {"unit_ids": ["b"], "voice": "日記", "evidence_unit_ids": ["b"]})])
    assert [g.voice for g in merged.narration.segments] == ["信件", "日記"]


def test_how_someone_talks_to_some_people_is_shown_from_its_first_evidence(conn):
    from ncte.memory.repository import impression_at

    conn.execute("INSERT INTO character_impressions(character_id, book_id, since_ordinal, period, personality, voice, "
                 "role, evidence_json, revealed_ordinal, run_id, updated_at, voice_toward, toward_revealed_ordinal) "
                 "VALUES ('c1','b1',0,NULL,'p','句子短','r','[]',0,NULL,'t','對女兒常用諷刺的敬語',2830)")
    assert "voice_toward" not in impression_at(conn, "c1", 113)  # not before the story shows it
    assert impression_at(conn, "c1", 2830)["voice_toward"] == "對女兒常用諷刺的敬語"
    assert impression_at(conn, "c1", None)["voice_toward"] == "對女兒常用諷刺的敬語"
