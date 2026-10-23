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

from ..epub.units import source_form
from ..errors import ContentBlockedError, FatalAPIError, LLMError

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


QUIET_REACH = 15  # paragraphs an even cut may move to fall at a quiet point


def even_parts(units: list[dict[str, Any]], size: int) -> list[list[str]]:
    """A long scene's units in even consecutive parts of about ``size``, each cut moved (within QUIET_REACH) to fall
    between two narration paragraphs and never inside a speech running over paragraphs (structure, not meaning)."""
    from .dialogue import UnitText, clean, find_quotes

    ids = [u["unit_id"] for u in units]
    text = [clean(u.get("source", "")).strip(" 　") for u in units]
    inside = {q.unit_id for q in find_quotes([UnitText(i, t, n) for n, (i, t) in enumerate(zip(ids, text, strict=True))])
              if q.continuation_of is not None}

    def talk(t: str) -> bool:
        return t.startswith(("「", "『")) or "」" in t[-3:] or "』" in t[-3:]

    def quiet(j: int) -> bool:
        return ids[j] not in inside and not talk(text[j]) and not talk(text[j - 1])

    def free(j: int) -> bool:  # at least not in the middle of a speech
        return ids[j] not in inside

    n = -(-len(ids) // size)
    cuts: list[int] = []
    for k in range(1, n):
        start = cuts[-1] if cuts else 0
        t = start + (len(ids) - start) // (n - k + 1)  # even over what is left after the cuts made so far
        lo = start + 1
        near = sorted(range(max(lo, t - QUIET_REACH), min(len(ids) - 1, t + QUIET_REACH) + 1), key=lambda j: abs(j - t))
        j = next((j for j in near if quiet(j)), next((j for j in near if free(j)), t))
        if j > lo - 1:
            cuts.append(j)
    edges = [0, *cuts, len(ids)]
    return [ids[a:b] for a, b in zip(edges, edges[1:], strict=False) if b > a]


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

    Lists are joined in order. A new character a later part introduces again (same Japanese name, same "another
    person" mark and time) becomes the earlier one; other temporary ids of later parts are made unique, everywhere they are referred to. The scene's
    single values come from the first part that has them (narration: the first that is not 'unclear'); summaries
    are joined.
    """
    cls = type(parts[0])
    dumped = [p.model_dump() for p in parts]
    # the same person met again: the same words, and the same "another person" mark and time (「母親」 of a past life
    # is not the mother of the present; two people under one word in one time are beyond what the words can tell)
    def same_person(c: dict[str, Any]) -> tuple[str, bool, str]:
        return source_form(c["name_ja"]), bool(c.get("other_person")), str(c.get("period") or "").strip()

    by_name: dict[tuple[str, bool, str], dict[str, Any]] = {}
    for i, d in enumerate(dumped):
        chars = d.get("new_characters")
        if chars is None:
            continue
        mapping: dict[str, str] = {}
        kept = []
        for c in chars:
            first = by_name.get(same_person(c))
            if first is not None:
                mapping[c["temp_id"]] = first["temp_id"]
                first["aliases_ja"] += [a for a in c["aliases_ja"] if a not in first["aliases_ja"]]
                first["evidence_unit_ids"] += c["evidence_unit_ids"]
                # what the later part says about the person is kept too: what each alias is (「少女」 common), who
                # the person turns out to be, that the words name several people
                kinds = {k["ja"] for k in first.get("alias_kinds") or []}
                first["alias_kinds"] = [*(first.get("alias_kinds") or []),
                                        *(k for k in c.get("alias_kinds") or [] if k["ja"] not in kinds)]
                first["same_as_ref"] = first.get("same_as_ref") or c.get("same_as_ref")
                first["group"] = bool(first.get("group") or c.get("group"))
                continue
            if i:
                mapping[c["temp_id"]] = f"{c['temp_id']}~{i}"
            kept.append(c)
        d = dumped[i] = _replace({**d, "new_characters": kept}, mapping)
        for c in d["new_characters"]:
            by_name.setdefault(same_person(c), c)
    out: dict[str, Any] = {}
    for key, value in dumped[0].items():
        values = [d[key] for d in dumped]
        if isinstance(value, list):
            out[key] = [x for v in values for x in v]
        elif key == "scene_summary":
            out[key] = "\n".join(v for v in values if v)
        elif key == "narration":
            told = [v for v in values if v and v.get("person") != "unclear"]
            first = told[0] if told else next((v for v in values if v), None)
            # each part's stretches in another voice are its own (ADR-083)
            out[key] = {**first, "segments": [g for v in values if v for g in v.get("segments") or []]} \
                if first is not None else None
        else:
            out[key] = next((v for v in values if v is not None), None)
    return cls.model_validate(out)


def _label(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    return str(next((item[k] for k in ("unit_id", "ref", "pair", "name", "ja") if item.get(k)), "?"))


def call_in_parts(call: Callable[[dict[str, Any]], Any], payload: dict[str, Any], key: str | tuple[str, ...],
                  blocked: list[str], *, min_part: int = MIN_PART,
                  failures: list[tuple[dict[str, Any], LLMError]] | None = None) -> Any:
    """``call(payload)``; when the content filter refuses it, the list under ``key`` is halved until each part goes
    through or is small enough to give up on (its items — unit ids, refs, pairs — go to ``blocked``). The parts'
    answers are merged. None when nothing went through. Several keys (``("names", "address")``): the longest list is
    halved and the other lists go with the first half only, so nothing is asked twice. ``failures``: a part whose
    request failed otherwise (not fatal) is kept there with its error instead of raising, so the parts that answered
    are not lost with it (the caller asks the failed ones again)."""
    keys = (key,) if isinstance(key, str) else key
    split = max(keys, key=lambda k: len(payload.get(k) or []))
    items = payload.get(split) or []
    try:
        return call(payload)
    except ContentBlockedError:
        if len(items) <= min_part:
            blocked.extend(_label(i) for k in keys for i in payload.get(k) or [])
            return None
    except FatalAPIError:
        raise
    except LLMError as exc:
        if failures is None:
            raise
        failures.append((payload, exc))
        return None
    half = max(1, len(items) // 2)
    first = {**payload, split: items[:half]}
    second = {**payload, split: items[half:], **{k: [] for k in keys if k != split}}
    parts = [p for chunk in (first, second)
             if any(chunk.get(k) for k in keys)
             and (p := call_in_parts(call, chunk, key, blocked, min_part=min_part, failures=failures)) is not None]
    return merge_outputs(parts) if parts else None
