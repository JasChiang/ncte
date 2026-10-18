"""Omitted subject/object candidates (spec §5.2 actions): validation only.

The LLM proposes who performs / receives an action whose argument is omitted. We keep these as
*candidates* for the translator; a subject is only marked ``explicit`` when one of its names is
literally present in that unit, otherwise ``inferred``/``unknown``.
"""

from __future__ import annotations

from typing import Any

from ..llm.schemas import ActionAnalysis


def validate_actions(actions: list[ActionAnalysis], unit_text: dict[str, str], ref_map: dict[str, str],
                     names_of: dict[str, list[str]]) -> tuple[list[dict[str, Any]], list[tuple[Any, str]]]:
    kept: list[dict[str, Any]] = []
    rejected: list[tuple[Any, str]] = []
    for a in actions:
        if a.unit_id not in unit_text:
            rejected.append((a.model_dump(), "unit_id outside scene"))
            continue
        subj = [ref_map[x] for x in a.subject_candidates if x in ref_map]
        obj = [ref_map[x] for x in a.object_candidates if x in ref_map]
        dropped = [x for x in a.subject_candidates + a.object_candidates if x not in ref_map]
        if dropped:
            rejected.append(({"unit_id": a.unit_id, "refs": dropped}, "unknown character refs dropped"))
        status = a.resolution_status
        if status == "explicit":
            text = unit_text[a.unit_id]
            if not (len(subj) == 1 and any(n in text for n in names_of.get(subj[0], []))):
                status = "inferred"
        if not subj:  # the status is about who does it: 「ミナを押した」 — ミナ is pushed, the one who pushes unknown
            status = "unknown"
        kept.append({"unit_id": a.unit_id, "summary": a.summary[:200], "subject_candidates": subj,
                     "object_candidates": obj, "status": status})
    return kept, rejected
