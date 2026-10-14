"""Pydantic models for every structured LLM output (spec §6.2) + strict JSON-Schema export."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ translate
class UnitTranslation(_M):
    unit_id: str
    translated_text: str
    flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    translator_note: str = ""  # ADR-025: a reader-facing note, only when the series style allows notes


class TranslationBatchOutput(_M):
    translations: list[UnitTranslation]


# ------------------------------------------------------------------ scene analysis
EvidenceType = Literal[
    "dialogue_tag", "action", "address", "knowledge", "continuity",  # strong
    "first_person", "sentence_ending", "verbal_tic", "honorific", "rhythm", "other",  # weak
]
STRONG_EVIDENCE = {"dialogue_tag", "action", "address", "knowledge", "continuity"}
Resolution = Literal["explicit", "inferred", "unknown"]


class NewCharacter(_M):
    temp_id: str
    name_ja: str
    aliases_ja: list[str]
    proposed_name_zh: str
    evidence_unit_ids: list[str]


class Participant(_M):
    character_ref: str
    presence: Literal["explicit", "inferred", "left"]
    evidence_unit_ids: list[str]


class SpeakerCandidate(_M):
    character_ref: str
    evidence_types: list[EvidenceType]
    evidence_unit_ids: list[str]
    note: str


class UtteranceAnalysis(_M):
    unit_id: str
    quote_index: int
    speaker_candidates: list[SpeakerCandidate]
    resolution_status: Resolution
    speaker_ref: str | None
    contradictions: list[str]


class ActionAnalysis(_M):
    unit_id: str
    summary: str
    subject_candidates: list[str]
    object_candidates: list[str]
    resolution_status: Resolution


class PropState(_M):
    name_ja: str
    holder_ref: str | None
    evidence_unit_ids: list[str]


class Uncertainty(_M):
    unit_ids: list[str]
    question: str


class SceneAnalysisOutput(_M):
    pov: str | None
    location: str | None
    time: str | None
    new_characters: list[NewCharacter]
    participants: list[Participant]
    utterances: list[UtteranceAnalysis]
    actions: list[ActionAnalysis]
    props: list[PropState]
    uncertainties: list[Uncertainty]


# ------------------------------------------------------------------ memory update
class FactOut(_M):
    subject_ref: str
    predicate: str
    object: str
    fact_type: Literal["objective", "character_belief", "narrator_claim"]
    certainty: Literal["explicit", "inferred"]
    evidence_unit_ids: list[str]
    evidence_quote: str
    source_viewpoint: str | None
    retracts_fact_ids: list[str]


class GlossaryCandidate(_M):
    source_term: str
    target_term: str
    category: Literal["character", "place", "item", "skill", "organization", "term"]
    evidence_unit_ids: list[str]


class NameLink(_M):
    term: str  # a name form used in this scene (nickname, short form, alias …)
    term_zh: str  # its Chinese rendering (keep the nickname feel; do not reuse the official rendering)
    same_as: str  # the other (usually official) name form of the same entity
    form_type: Literal["full", "short", "nickname", "alias", "title"]
    note: str  # e.g. the wordplay behind a nickname
    evidence_unit_ids: list[str]
    evidence_quote: str


class AddressTermOut(_M):
    speaker_ref: str  # character_id of the one who says it
    addressee_ref: str  # character_id of the one called / referred to (same as speaker_ref = refers to self)
    term: str  # the form as written, including honorifics (「島村さん」「あだっち」)
    term_zh: str  # the rendering used in this scene's translation
    note: str
    evidence_unit_ids: list[str]
    evidence_quote: str


class WordplayOut(_M):
    unit_id: str
    term: str  # the word / phrase as written
    explanation: str  # what a Chinese reader would miss
    note_zh: str  # a short reader-facing note, if one is needed
    evidence_quote: str


class MemoryUpdateOutput(_M):
    scene_summary: str
    facts: list[FactOut]
    glossary_candidates: list[GlossaryCandidate]
    prop_states: list[PropState]
    name_links: list[NameLink] = Field(default_factory=list)
    address_terms: list[AddressTermOut] = Field(default_factory=list)
    wordplay: list[WordplayOut] = Field(default_factory=list)


# ------------------------------------------------------------------ naming (ADR-023)
class NamingOption(_M):
    zh: str
    reason: str


class NamingItem(_M):
    ref: str
    zh: str
    reason: str
    alternatives: list[NamingOption] = Field(default_factory=list)


class NamingOutput(_M):
    names: list[NamingItem] = Field(default_factory=list)
    address: list[NamingItem] = Field(default_factory=list)


# ------------------------------------------------------------------ review
IssueType = Literal["mistranslation", "omission", "addition", "speaker_error", "name_inconsistency",
                    "subject_object_error", "spoiler", "placeholder", "style", "other"]


class ReviewIssue(_M):
    unit_id: str
    issue_type: IssueType
    severity: Literal["low", "medium", "high"]
    evidence: str
    suggestion: str
    needs_retranslation: bool


class ReviewOutput(_M):
    issues: list[ReviewIssue]


SCHEMAS: dict[str, type[BaseModel]] = {
    "translate": TranslationBatchOutput,
    "scene_analyze": SceneAnalysisOutput,
    "memory_update": MemoryUpdateOutput,
    "naming": NamingOutput,
    "review": ReviewOutput,
}


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Inline $defs, forbid additional properties and mark every property as required
    (the subset accepted by OpenAI-style ``strict: true`` structured outputs)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return fix(copy.deepcopy(defs[name]))
            out = {k: fix(v) for k, v in node.items() if k not in ("title", "default")}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return fix(raw)
