"""Sequence-level speaker attribution: reconcile program evidence with the LLM's hypotheses.

Status rules (spec §5.1, §5.3):
* ``explicit``  – only from a program-detected dialogue tag (speech verb + known name) the LLM does not dispute,
  from a continuation of an explicit quote, or from a human. The LLM alone can never make a speaker explicit;
  when it disputes a tag, its speaker is kept as ``inferred`` (the tag is a pattern match, not a reading).
* ``inferred``  – a candidate supported by strong-but-indirect evidence (adjacent action, address,
  knowledge, continuity) or by the LLM with weak (style) evidence only.
* ``unknown``   – no candidate, or conflicting strong candidates. ``speaker_id`` is then ``None``.
Weak evidence (first person, endings, verbal tics, honorifics, rhythm) never overrides strong evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..llm.schemas import STRONG_EVIDENCE
from .dialogue import TagEvidence


@dataclass
class Candidate:
    character_id: str
    evidence_types: set[str]
    evidence_unit_ids: list[str]
    source: str  # program | llm
    note: str = ""

    @property
    def strong(self) -> bool:
        return bool(self.evidence_types & STRONG_EVIDENCE)

    def to_json(self) -> dict[str, object]:
        return {"character_id": self.character_id, "evidence_types": sorted(self.evidence_types),
                "evidence_unit_ids": self.evidence_unit_ids, "source": self.source, "note": self.note}


@dataclass
class Resolution:
    speaker_id: str | None
    status: str  # explicit | inferred | unknown
    source: str  # program | llm | program+llm | continuity | none
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class LlmClaim:
    speaker_ref: str | None  # already mapped to a character id (None = unknown)
    status: str
    candidates: list[Candidate]
    contradictions: list[str] = field(default_factory=list)
    listeners: list[str] = field(default_factory=list)  # character ids the line is said to


def resolve(program_ev: list[TagEvidence], llm: LlmClaim | None, head: Resolution | None = None) -> Resolution:
    cands: list[Candidate] = []
    for e in program_ev:
        cands.append(Candidate(e.character_id, {e.kind}, [e.unit_id], "program", e.snippet))
    if llm:
        cands.extend(llm.candidates)
    notes: list[str] = list(llm.contradictions) if llm else []

    # 0. continuation of a multi-paragraph quote: same speaker as its head
    if head is not None:
        if head.speaker_id and llm and llm.speaker_ref and llm.speaker_ref != head.speaker_id:
            notes.append(f"LLM proposed {llm.speaker_ref} for a continuation of {head.speaker_id}'s quote (ignored)")
        status = head.status if head.status in ("explicit", "human_verified") else head.status
        return Resolution(head.speaker_id, "explicit" if status == "human_verified" else status, "continuity",
                          cands, notes + ["continuation of previous paragraph's quote"])

    # 1. explicit dialogue tags found by the program
    tagged = {e.character_id for e in program_ev if e.kind == "dialogue_tag"}
    if len(tagged) == 1:
        sid = next(iter(tagged))
        if llm and llm.speaker_ref and llm.speaker_ref != sid and llm.status != "unknown":
            # a pattern match is evidence, not understanding (「…と日野に声をかけた」, a tag that belongs to the
            # previous line): the model, reading the whole exchange, decides — as an inference, not a fact
            where = "the neighbouring paragraph" if all(e.adjacent for e in program_ev if e.kind == "dialogue_tag") \
                else "the same paragraph"
            return Resolution(llm.speaker_ref, "inferred", "llm", cands, notes + [
                f"LLM proposed {llm.speaker_ref} against a tag for {sid} in {where} (LLM kept)"])
        return Resolution(sid, "explicit", "program", cands, notes)
    if len(tagged) > 1:
        return Resolution(None, "unknown", "program", cands, notes + [f"conflicting dialogue tags: {sorted(tagged)}"])

    # 2. indirect evidence
    action = {e.character_id for e in program_ev if e.kind == "action"}
    llm_ref = llm.speaker_ref if llm else None
    llm_cand = next((c for c in (llm.candidates if llm else []) if c.character_id == llm_ref), None)
    llm_strong = bool(llm_cand and llm_cand.strong)
    if llm and llm.status == "explicit":
        notes.append("LLM claimed explicit without a program-verifiable dialogue tag → at most inferred")

    if len(action) > 1:
        return Resolution(None, "unknown", "program", cands, notes + [f"conflicting action evidence: {sorted(action)}"])
    act = next(iter(action)) if action else None

    if llm_ref and act and llm_ref != act:
        if llm_strong:
            return Resolution(None, "unknown", "program+llm", cands,
                              notes + [f"conflict: action evidence → {act}, LLM strong evidence → {llm_ref}"])
        return Resolution(act, "inferred", "program", cands,
                          notes + [f"weak (style) evidence for {llm_ref} overridden by action evidence for {act}"])
    if llm_ref and (llm is None or llm.status != "unknown"):
        src = "program+llm" if act == llm_ref else "llm"
        if not llm_strong and act != llm_ref:
            notes.append("only weak (style) evidence")
        return Resolution(llm_ref, "inferred", src, cands, notes)
    if act:
        return Resolution(act, "inferred", "program", cands, notes + ["adjacent action narration only"])
    return Resolution(None, "unknown", "none", cands, notes)


# --------------------------------------------------------------------------- human confirmation / correction

def set_speaker_human(conn: Any, utterance_id: str, character_id: str | None, *, reason: str = "") -> dict[str, Any]:
    """Human verification/correction of a speaker (spec §M4 人工確認／修正資料流).

    * status → ``human_verified`` (never overwritten by later automatic analysis)
    * continuation paragraphs of the same quote follow
    * speech-example library: the utterance becomes a ``human_verified`` example (or is removed)
    * affected units get a ``speaker_corrected`` QA finding so they can be retranslated
    """
    import json

    from ..memory import repository as repo
    from ..memory.db import utcnow

    row = conn.execute("SELECT * FROM utterances WHERE id=?", (utterance_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown utterance {utterance_id}")
    if character_id is not None:
        from ..series import SERIES_SCOPE

        ch = conn.execute(f"SELECT id FROM characters WHERE id=? AND {SERIES_SCOPE}", (character_id, row["book_id"])).fetchone()
        if ch is None:
            raise KeyError(f"unknown character {character_id}")
    chain = [row]
    frontier = [utterance_id]
    while frontier:
        nxt = conn.execute(f"SELECT * FROM utterances WHERE continuation_of IN ({','.join('?' * len(frontier))})",
                           frontier).fetchall()
        chain.extend(nxt)
        frontier = [r["id"] for r in nxt]
    changed_units = []
    for u in chain:
        notes = json.loads(u["notes_json"]) + [f"human: {reason}" if reason else "human verified"]
        conn.execute("UPDATE utterances SET speaker_id_nullable=?, status='human_verified', resolution_source='human', "
                     "notes_json=?, updated_at=? WHERE id=?",
                     (character_id, json.dumps(notes, ensure_ascii=False), utcnow(), u["id"]))
        repo.remove_speech_examples_for(conn, u["id"])
        if character_id:
            repo.add_speech_example(conn, character_id, u["unit_id"], u["id"], u["quote_text"], "human_verified")
        if u["speaker_id_nullable"] != character_id:
            changed_units.append(u["unit_id"])
            conn.execute("INSERT INTO qa_findings(book_id, unit_id, type, severity, evidence, created_at) VALUES "
                         "(?,?,?,?,?,?)", (u["book_id"], u["unit_id"], "speaker_corrected", "high",
                                           f"{u['speaker_id_nullable']} → {character_id}: {reason}", utcnow()))
    repo.refresh_profiles(conn, row["book_id"])
    from ..memory.address import reharvest_scene

    for scene_id in {u["scene_id"] for u in chain if u["scene_id"]}:
        reharvest_scene(conn, row["book_id"], scene_id)
    return {"utterances": [u["id"] for u in chain], "changed_units": changed_units}
