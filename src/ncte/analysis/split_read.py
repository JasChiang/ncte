"""Reading a scene the content filter refuses: read it in parts and put the answers back together.

A provider's filter (Gemini's PROHIBITED_CONTENT) sometimes refuses a whole scene over one passage, often a
harmless one. Translation already retries such units in smaller groups; the read pass did not, so a refused scene
lost its speakers and memory. Here the scene's units are halved until each part is read or is small enough to give
up on; the parts' answers are merged into one answer for the scene, and the units no part could read are reported.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ..errors import ContentBlockedError

MIN_PART = 4  # units: a refused part this small is not split further


def subset_payload(payload: dict[str, Any], keep: list[str]) -> dict[str, Any]:
    """The same request about only some of the scene's units (their quotes and utterances follow them)."""
    ids = set(keep)
    out = {**payload, "units": [u for u in payload["units"] if u["unit_id"] in ids]}
    if "quotes" in payload:
        out["quotes"] = [q for q in payload["quotes"] if q["unit_id"] in ids]
    if isinstance(payload.get("scene"), dict) and "utterances" in payload["scene"]:
        out["scene"] = {**payload["scene"],
                        "utterances": [u for u in payload["scene"]["utterances"] if u["unit_id"] in ids]}
    return out


def _replace(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_replace(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _replace(v, mapping) for k, v in value.items()}
    return value


def merge_outputs[M: BaseModel](parts: list[M]) -> M:
    """One answer from the answers about consecutive parts of a scene.

    Lists are joined in order. A new character a later part introduces again (same Japanese name) becomes the
    earlier one; other temporary ids of later parts are made unique, everywhere they are referred to. The scene's
    single values come from the first part that has them (narration: the first that is not 'unclear'); summaries
    are joined.
    """
    cls = type(parts[0])
    dumped = [p.model_dump() for p in parts]
    by_name: dict[str, dict[str, Any]] = {}
    for i, d in enumerate(dumped):
        chars = d.get("new_characters")
        if chars is None:
            continue
        mapping: dict[str, str] = {}
        kept = []
        for c in chars:
            first = by_name.get(c["name_ja"])
            if first is not None:
                mapping[c["temp_id"]] = first["temp_id"]
                first["aliases_ja"] += [a for a in c["aliases_ja"] if a not in first["aliases_ja"]]
                first["evidence_unit_ids"] += c["evidence_unit_ids"]
                continue
            if i:
                mapping[c["temp_id"]] = f"{c['temp_id']}~{i}"
            kept.append(c)
        d = dumped[i] = _replace({**d, "new_characters": kept}, mapping)
        for c in d["new_characters"]:
            by_name.setdefault(c["name_ja"], c)
    out: dict[str, Any] = {}
    for key, value in dumped[0].items():
        values = [d[key] for d in dumped]
        if isinstance(value, list):
            out[key] = [x for v in values for x in v]
        elif key == "scene_summary":
            out[key] = "\n".join(v for v in values if v)
        elif key == "narration":
            told = [v for v in values if v and v.get("person") != "unclear"]
            out[key] = told[0] if told else next((v for v in values if v), None)
        else:
            out[key] = next((v for v in values if v is not None), None)
    return cls.model_validate(out)


def call_in_parts(call: Callable[[dict[str, Any]], Any], payload: dict[str, Any], key: str, blocked: list[str],
                  *, min_part: int = MIN_PART) -> Any:
    """``call(payload)``; when the content filter refuses it, the list under ``key`` (items carrying ``unit_id``) is
    halved until each part goes through or is small enough to give up on (its unit ids go to ``blocked``). The parts'
    answers are merged. None when nothing went through. Used by the reader's look, review and the rule-flag check."""
    items = payload[key]
    try:
        return call(payload)
    except ContentBlockedError:
        if len(items) <= min_part:
            blocked.extend(str(i.get("unit_id")) for i in items if isinstance(i, dict))
            return None
    half = len(items) // 2
    parts = [p for chunk in (items[:half], items[half:])
             if (p := call_in_parts(call, {**payload, key: chunk}, key, blocked, min_part=min_part)) is not None]
    return merge_outputs(parts) if parts else None
