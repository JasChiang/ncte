"""Who says each line: the model's reading, kept with how sure it is (ADR-059).

The model reads the scene and answers for every line: who says it, to whom, and how sure it is. The program does not
judge who speaks; it checks the evidence the model gives:

* ``explicit`` – the model says the text states it, and quotes the words that do (「とミナは言った」「ミナが答えた」):
  those words must be in the narration of the line's paragraph or the one before or after (not inside a quote) and
  name the speaker (a name or alias of that character). Otherwise the answer is kept as ``inferred``.
* ``inferred`` – the model's answer from the surrounding text.
* ``unknown``  – the model gave no speaker. ``speaker_id`` is then ``None``.

A paragraph that continues an unclosed quote is the same speech: it keeps the speaker of the quote's start; when the
model answers someone else there, its answer stands as ``inferred`` and goes to the second look (``(to look at)``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..llm.schemas import STRONG_EVIDENCE


@dataclass
class Candidate:
    character_id: str
    evidence_types: set[str]
    evidence_unit_ids: list[str]
    source: str  # llm
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
    source: str  # llm | continuity | none | human
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class LlmClaim:
    speaker_ref: str | None  # already mapped to a character id (None = unknown)
    status: str
    candidates: list[Candidate]
    contradictions: list[str] = field(default_factory=list)
    listeners: list[str] = field(default_factory=list)  # character ids the line is said to
    tag: str = ""  # the words of the text that say who speaks (for ``explicit``)
    listener_status: str = "inferred"  # how sure ``listeners`` is (the model's reading)


TO_LOOK_AT = "(to look at)"  # the second look (verify) reads these lines again with the text around them


def resolve(llm: LlmClaim | None, head: Resolution | None = None,
            tag_ok: Callable[[str, str], bool] | None = None) -> Resolution:
    """``tag_ok(tag, speaker_id)``: whether the quoted words are in the narration next to the line and name that
    speaker — a check that the evidence exists, not a reading of it."""
    cands = list(llm.candidates) if llm else []
    notes: list[str] = list(llm.contradictions) if llm else []
    said = llm.speaker_ref if llm and llm.status != "unknown" else None

    if head is not None:  # the rest of a speech begun in an earlier paragraph
        if said and not head.speaker_id:  # the speech's start was left unknown: the model's reading here, to look at
            return Resolution(said, "inferred", "llm", cands, notes + [
                f"the model answers {said} for the rest of a speech whose start has no speaker {TO_LOOK_AT}"])
        if said and head.speaker_id and said != head.speaker_id:
            return Resolution(said, "inferred", "llm", cands, notes + [
                f"the model answers {said} for a continuation of {head.speaker_id}'s speech {TO_LOOK_AT}"])
        status = "explicit" if head.status == "human_verified" else head.status
        return Resolution(head.speaker_id, status, "continuity", cands, notes + ["continuation of the quote before"])
    if not said:  # unsure between people it named: worth a second look; nobody named: nothing to ask
        return Resolution(None, "unknown", "llm" if llm else "none", cands,
                          notes + ([f"the model named candidates but no speaker {TO_LOOK_AT}"] if cands else []))
    if llm is not None and llm.status == "explicit":
        if llm.tag.strip() and tag_ok is not None and tag_ok(llm.tag.strip(), said):
            return Resolution(said, "explicit", "llm", cands, notes)
        notes.append("the model said explicit, but the words it gave are not in the narration next to the line or do "
                     f"not name the speaker: kept as inferred {TO_LOOK_AT}")
    return Resolution(said, "inferred", "llm", cands, notes)


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
            repo.speaker_changed_addressees(conn, u["id"], character_id)
            changed_units.append(u["unit_id"])
            conn.execute("INSERT INTO qa_findings(book_id, unit_id, type, severity, evidence, created_at) VALUES "
                         "(?,?,?,?,?,?)", (u["book_id"], u["unit_id"], "speaker_corrected", "high",
                                           f"{u['speaker_id_nullable']} → {character_id}: {reason}", utcnow()))
    repo.refresh_profiles(conn, row["book_id"])
    from ..memory.address import follow_speakers

    follow_speakers(conn, row["book_id"], {u["id"]: u["speaker_id_nullable"] for u in chain
                                           if u["speaker_id_nullable"] != character_id})
    return {"utterances": [u["id"] for u in chain], "changed_units": changed_units}
