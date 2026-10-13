"""Scripted LLM responders that behave like a plausible (and sometimes wrong) model for the story fixture."""

from __future__ import annotations

from typing import Any

from ncte.llm.fake import default_memory_update, default_scene_analyze


def uid(payload: dict[str, Any], text: str) -> str | None:
    for u in payload["units"]:
        if text in u["source"]:
            return u["unit_id"]
    return None


def qidx(payload: dict[str, Any], text: str) -> tuple[str, int] | None:
    for q in payload.get("quotes", []):
        if text in q["text"]:
            return q["unit_id"], q["quote_index"]
    return None


def known(payload: dict[str, Any], name: str) -> str | None:
    for c in payload.get("known_characters", []):
        if c["name_ja"] == name:
            return c["character_id"]
    return None


def utt(payload, text, ref, status, types=("dialogue_tag",), ev_text=None):
    k = qidx(payload, text)
    if k is None:
        return None
    ev = [uid(payload, ev_text)] if ev_text and uid(payload, ev_text) else [k[0]]
    cands = [{"character_ref": ref, "evidence_types": list(types), "evidence_unit_ids": ev, "note": ""}] if ref else []
    return {"unit_id": k[0], "quote_index": k[1], "speaker_candidates": cands, "resolution_status": status,
            "speaker_ref": ref, "contradictions": []}


def scene_analyze(payload: dict[str, Any], req: Any) -> dict[str, Any]:
    out = default_scene_analyze(payload, req)
    out["utterances"] = []
    mina = known(payload, "ミナ") or "new_1"
    kaito = known(payload, "カイト") or "new_2"
    new = []
    if uid(payload, "ミナは腕を組んで言った"):
        new.append({"temp_id": "new_1", "name_ja": "ミナ", "aliases_ja": [], "proposed_name_zh": "米娜",
                    "evidence_unit_ids": [uid(payload, "ミナは腕を組んで言った")]})
        new.append({"temp_id": "new_2", "name_ja": "カイト", "aliases_ja": ["カイ"], "proposed_name_zh": "海斗",
                    "evidence_unit_ids": [uid(payload, "カイト、遅いですよ")]})
        # hallucinated character with fake evidence → must be rejected
        new.append({"temp_id": "new_3", "name_ja": "リリア", "aliases_ja": [], "proposed_name_zh": "莉莉亞",
                    "evidence_unit_ids": [uid(payload, "朝の市場")]})
        utts = [utt(payload, "カイト、遅いですよ", mina, "explicit", ev_text="ミナは腕を組んで言った"),
                utt(payload, "悪い悪い", kaito, "inferred", ("action",), "カイトは頭をかいた"),
                utt(payload, "ありがとうございます", mina, "inferred", ("sentence_ending",)),
                utt(payload, "本当だろうな", kaito, "explicit", ("sentence_ending",))]  # false explicit claim
        out["utterances"] = [u for u in utts if u]
        out["participants"] = [{"character_ref": mina, "presence": "explicit", "evidence_unit_ids": []},
                               {"character_ref": kaito, "presence": "explicit", "evidence_unit_ids": []}]
        out["props"] = [{"name_ja": "銀の鍵", "holder_ref": mina,
                         "evidence_unit_ids": [uid(payload, "カイトは銀の鍵をミナに手渡した")]}]
    if uid(payload, "口調を真似て"):
        # imitation: style points to カイト (俺様) but the action narration says ミナ
        utts = [utt(payload, "俺様に任せろ", kaito, "inferred", ("first_person", "verbal_tic")),
                utt(payload, "待って", "c_does_not_exist", "inferred", ("rhythm",))]  # bad id → rejected
        out["utterances"] = [u for u in utts if u]
    if uid(payload, "王女リリアだった"):
        new.append({"temp_id": "new_9", "name_ja": "リリア", "aliases_ja": ["王女"], "proposed_name_zh": "莉莉亞",
                    "evidence_unit_ids": [uid(payload, "王女リリアだった")]})
    out["new_characters"] = new
    return out


def memory_update(payload: dict[str, Any], req: Any) -> dict[str, Any]:
    out = default_memory_update(payload, req)
    kaito = known(payload, "カイト")
    hood = uid(payload, "フードの少女の正体は")
    hand = uid(payload, "カイトは銀の鍵をミナに手渡した")
    if hand and kaito:
        out["facts"] = [
            {"subject_ref": kaito, "predicate": "把銀之鑰交給", "object": "ミナ", "fact_type": "objective",
             "certainty": "explicit", "evidence_unit_ids": [hand], "evidence_quote": "カイトは銀の鍵をミナに手渡した",
             "source_viewpoint": None, "retracts_fact_ids": []},
            # fabricated evidence → rejected
            {"subject_ref": kaito, "predicate": "是", "object": "王子", "fact_type": "objective", "certainty": "explicit",
             "evidence_unit_ids": [hand], "evidence_quote": "カイトは王子だった", "source_viewpoint": None,
             "retracts_fact_ids": []},
            # inference stays inferred
            {"subject_ref": kaito, "predicate": "似乎", "object": "常常遲到", "fact_type": "character_belief",
             "certainty": "inferred", "evidence_unit_ids": [uid(payload, "寝坊しちまった")],
             "evidence_quote": "寝坊しちまった", "source_viewpoint": "ミナ", "retracts_fact_ids": []},
        ]
        out["glossary_candidates"] = [{"source_term": "銀の鍵", "target_term": "銀之鑰", "category": "item",
                                       "evidence_unit_ids": [hand]}]
        out["scene_summary"] = "米娜在市場等海斗，海斗把銀之鑰交給米娜。"
    if hood:
        out["facts"] = [{"subject_ref": "フードの少女", "predicate": "真實身分是", "object": "王女リリア",
                         "fact_type": "objective", "certainty": "explicit", "evidence_unit_ids": [hood],
                         "evidence_quote": "フードの少女の正体は、この国の王女リリアだった", "source_viewpoint": None,
                         "retracts_fact_ids": []}]
        out["scene_summary"] = "兜帽少女來訪，揭露自己是王女莉莉亞。"
    return out


RESPONDERS = {"scene_analyze": scene_analyze, "memory_update": memory_update}
