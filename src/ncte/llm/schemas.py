"""Pydantic models for every structured LLM output (spec §6.2) + strict JSON-Schema export."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


READ_THROUGH_MAX = 5


# ``translation.read_through`` on (ADR-064): the list is the first field, so the model reads the whole batch before it
# writes the first translation. What it lists is kept for a reviewer, not judged. (A comment, not a docstring: a
# docstring would go into the JSON schema the model reads.)
class TranslationReadThroughOutput(_M):
    read_through: list[str]  # where a later paragraph constrains an earlier line: its code and one sentence
    translations: list[UnitTranslation]

    @field_validator("read_through")
    @classmethod
    def _at_most(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()][:READ_THROUGH_MAX]


# ------------------------------------------------------------------ scene analysis
EvidenceType = Literal[
    "dialogue_tag", "action", "address", "knowledge", "continuity",  # strong
    "first_person", "sentence_ending", "verbal_tic", "honorific", "rhythm", "other",  # weak
]
STRONG_EVIDENCE = {"dialogue_tag", "action", "address", "knowledge", "continuity"}
Resolution = Literal["explicit", "inferred", "unknown"]


class AliasKind(_M):
    ja: str
    # name / nickname / title (称号・役職: 学院長) / description (具體描述: 黒髪の女性) / common (普通名詞: 少女、兵士)
    kind: Literal["name", "nickname", "title", "description", "common"]


class NewCharacter(_M):
    temp_id: str
    name_ja: str
    aliases_ja: list[str]
    proposed_name_zh: str
    evidence_unit_ids: list[str]
    same_as_ref: str | None = None  # a known character (so far unnamed / described) this name now reveals
    alias_kinds: list[AliasKind] = Field(default_factory=list)  # what each alias is; common nouns are not kept
    # the same words as a known character's name but another person (the mother of a past life, not of this one)
    other_person: bool = False
    period: str = ""  # when this person lives, if not the story's present (前世, 三年前)
    group: bool = False  # the words name several people (「取り巻きの二人」「生徒たち」): the model's reading


class OtherForm(_M):
    """Words of this scene that name a known character but are not on their card yet (a given name used alone, a
    nickname): the model's reading, with where they occur."""
    ref: str  # a known character's id (or a temp_id of new_characters)
    ja: str  # verbatim
    kind: Literal["name", "nickname", "title", "description", "common"]
    evidence_unit_ids: list[str]


class Participant(_M):
    character_ref: str
    presence: Literal["explicit", "inferred", "left"]
    evidence_unit_ids: list[str]


# One of these per line of dialogue, so the model writes them with short keys (``alias``); the program reads the long
# names. Either spelling is accepted.
class SpeakerCandidate(_M):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    character_ref: str = Field(alias="ref")
    evidence_types: list[EvidenceType] = Field(alias="types")
    evidence_unit_ids: list[str] = Field(alias="evidence")
    note: str


class UtteranceAnalysis(_M):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    unit_id: str
    quote_index: int = Field(alias="qi")
    speaker_candidates: list[SpeakerCandidate] = Field(alias="candidates")
    resolution_status: Resolution = Field(alias="status")
    speaker_ref: str | None = Field(alias="speaker")
    contradictions: list[str] = Field(alias="conflicts")
    listener_refs: list[str] = Field(default_factory=list, alias="to")  # who the line is said to
    # how sure who it is said to is: explicit (the text says so — called by name, 「ミナに言った」) or inferred
    listener_status: Literal["explicit", "inferred"] = Field(default="inferred", alias="to_status")
    # for an explicit speaker: the words of the text that say who speaks (「とミナは言った」), checked verbatim
    speech_tag: str = Field(default="", alias="tag")


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
    period: str = ""  # when this holds, if not the story's present (a flashback: 三年前)


class Uncertainty(_M):
    unit_ids: list[str]
    question: str


class SelfForm(_M):
    ja: str  # what the narrator calls themself in the narration (私, 俺, 吾輩)
    zh: str  # its Chinese, kept for the whole book


class ImpressionOut(_M):
    """One stretch of a character's impression (ADR-060): usually one per character; several when the way of speaking
    or the personality clearly changes (growth, a disguise dropped, another time of their life)."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    ref: str
    personality: str  # 個性
    voice: str  # 說話口吻（翻譯時參考）
    role: str  # 在故事裡的定位
    evidence: list[str]  # refs of the lines / facts it rests on
    since: str = Field(default="", alias="from")  # the line / fact ref from which this stretch holds (empty: the first)
    period: str = ""  # another time of their life this describes (前世, 三年前); empty: the story's present


class ImpressionsOutput(_M):
    impressions: list[ImpressionOut]


class NarrationOut(_M):
    """Whose voice tells this scene (ADR-040): first person (``narrator_ref`` is the 「私」) or third person following
    ``focal_ref``."""
    person: Literal["first", "third", "unclear"]
    narrator_ref: str | None = None
    focal_ref: str | None = None
    evidence_unit_ids: list[str] = Field(default_factory=list)
    self_forms: list[SelfForm] = Field(default_factory=list)


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
    narration: NarrationOut | None = None
    other_forms: list[OtherForm] = Field(default_factory=list)


# ------------------------------------------------------------------ memory update
class FactOut(_M):
    subject_ref: str
    predicate: str
    object: str
    object_ref: str | None = None  # the character the fact is about besides the subject (by id, never by name)
    fact_type: Literal["objective", "character_belief", "narrator_claim"]
    certainty: Literal["explicit", "inferred"]
    evidence_unit_ids: list[str]
    evidence_quote: str
    source_viewpoint: str | None
    retracts_fact_ids: list[str]
    period: str = ""  # when it holds, if not the story's present: 前世, 千年前, 三年前 (empty = now)


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
    term: str  # the form as written, including honorifics (「高木さん」「みずっち」)
    term_zh: str  # the rendering used in this scene's translation
    note: str
    evidence_unit_ids: list[str]
    evidence_quote: str
    # calling the person (address), talking about someone (mention), oneself by one's own name (self)
    usage: Literal["address", "mention", "self"] = "address"


class WordplayOut(_M):
    unit_id: str
    term: str  # the word / phrase as written
    explanation: str  # what a Chinese reader would miss
    note_zh: str  # a short reader-facing note, if one is needed
    evidence_quote: str


class GenderOut(_M):
    character_ref: str
    gender: Literal["female", "male"]
    certainty: Literal["explicit", "inferred"] = "explicit"  # inferred: speech style, first person …
    evidence_unit_ids: list[str]
    evidence_quote: str  # verbatim: the words that show it (彼女, 少女, 娘, 少年 …)


class StatedRelation(_M):
    """A relation the text states or plainly shows in this scene (「お父様」, 婚約者, 先生と生徒), noted while reading."""
    a_ref: str
    b_ref: str
    relation: str
    a_is: str
    b_is: str
    kind: Literal["identity", "feeling", "same_person"]  # 身分／情感與立場／同一人的不同身分（分身、秘密身分、不同時期）
    evidence_unit_ids: list[str]
    evidence_quote: str
    period: str = ""  # when it holds, if not the story's present (前世的宿敵)


class GroupMembers(_M):
    """Who a group character (「取り巻きの二人」) is, as this scene shows it (ADR-046)."""
    group_ref: str
    member_refs: list[str]
    evidence_unit_ids: list[str]
    evidence_quote: str


class MemoryUpdateOutput(_M):
    scene_summary: str
    facts: list[FactOut]
    glossary_candidates: list[GlossaryCandidate]
    prop_states: list[PropState]
    name_links: list[NameLink] = Field(default_factory=list)
    address_terms: list[AddressTermOut] = Field(default_factory=list)
    wordplay: list[WordplayOut] = Field(default_factory=list)
    genders: list[GenderOut] = Field(default_factory=list)
    relations: list[StatedRelation] = Field(default_factory=list)
    groups: list[GroupMembers] = Field(default_factory=list)


class SceneReadOutput(_M):
    """A scene read with one request (``context.read_in_one_request``, ADR-065): the analysis, then the memory."""
    analysis: SceneAnalysisOutput
    memory: MemoryUpdateOutput


# ------------------------------------------------------------------ naming (ADR-023)
class NamingOption(_M):
    zh: str
    reason: str


class NamingItem(_M):
    ref: str
    zh: str
    reason: str
    alternatives: list[NamingOption] = Field(default_factory=list)


class NamingCharacter(_M):
    ref: str  # character id from characters_to_check
    verdict: Literal["different", "same", "unsure"]
    name_ja: str  # descriptive name when different (e.g. 「水野の母」)
    name_zh: str  # its Chinese label (e.g. 「水野媽媽」)
    reason: str


class NamingOutput(_M):
    names: list[NamingItem] = Field(default_factory=list)
    address: list[NamingItem] = Field(default_factory=list)
    characters: list[NamingCharacter] = Field(default_factory=list)


# ------------------------------------------------------------------ review
IssueType = Literal["mistranslation", "omission", "addition", "speaker_error", "name_inconsistency",
                    "subject_object_error", "spoiler", "placeholder", "tone", "voice", "style", "other"]


class ReviewIssue(_M):
    unit_id: str
    issue_type: IssueType
    severity: Literal["low", "medium", "high"]
    evidence: str
    suggestion: str
    needs_retranslation: bool


class ReviewOutput(_M):
    issues: list[ReviewIssue]


# ------------------------------------------------------------------ second look (ADR-033)
class VerifyItem(_M):
    ref: str
    answer: str  # character id | unknown ; female | male | unknown ; same | different | unsure ; a question: a sentence
    reason: str
    character_id: str = ""  # a question's answer may name who it is about (ADR-067)


class VerifyOutput(_M):
    items: list[VerifyItem]


# ------------------------------------------------------------------ scene windows (ADR-057)
# present: the story's now · flashback: a remembered past told as scenes · parallel: elsewhere at the same time ·
# summary: time passing told in brief (「それから数週間、」) · other: a dream, a letter, an in-story text
SceneKind = Literal["present", "flashback", "parallel", "summary", "other"]


class WindowBreak(_M):
    unit_id: str  # the first paragraph of a new scene
    kind: SceneKind = "present"  # of the scene it starts
    sure: bool = True  # false: a judgement call someone may want to look at
    new_chapter: bool = False  # only when the book has no table of contents
    reason: str = ""


class SceneWindowsOutput(_M):
    first_kind: SceneKind = "present"  # of the scene the window starts with, when it starts one
    breaks: list[WindowBreak]


# ------------------------------------------------------------ reader's look (ADR-038)
class ReaderIssue(_M):
    unit_id: str
    kind: Literal["unclear", "wordplay", "translationese", "voice", "contradiction", "other"]
    severity: Literal["low", "medium", "high"]
    problem: str  # what a reader without Japanese would stumble on


class ReaderOutput(_M):
    issues: list[ReaderIssue]


# ------------------------------------------------------------ house style (ADR-053)
class SpeechHabit(_M):
    ref: str  # character ref
    habit_ja: str
    render_zh: str
    note: str = ""


class HouseStyleOutput(_M):
    honorifics: Literal["natural", "explicit", "omit"]
    honorifics_reason: str = ""
    habits: list[SpeechHabit] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)


# ------------------------------------------------------------ bilingual proofreading (ADR-051)
class ProofreadIssue(_M):
    unit_id: str
    # ambiguity: the source allows another reading at least as natural as the current one (ADR-063) — not an error
    kind: Literal["meaning", "omission", "addition", "speaker", "tone", "voice", "style", "ambiguity"]
    severity: Literal["low", "medium", "high"]
    quote: str
    problem: str  # what is wrong, quoting the source
    fix: str


class ParagraphCheck(_M):
    """One paragraph looked at (when asked for: ``proofread_verdicts``): what the source says, in a few words, and
    whether the translation says it."""
    unit_id: str
    gist: str
    ok: bool


class ProofreadOutput(_M):
    checked: list[ParagraphCheck] = Field(default_factory=list)
    issues: list[ProofreadIssue]


# ------------------------------------------------------------ relations (ADR-039)
class RelationOut(_M):
    a: str  # character ref
    b: str
    relation: str  # 父女、未婚夫妻、師生、前世宿敵…
    a_is: str  # what a is to b: 父親
    b_is: str  # what b is to a: 女兒
    facts: list[str]  # evidence refs that show it (n… notes made while reading, f… facts)
    reason: str
    kind: Literal["identity", "feeling", "same_person"] = "identity"
    certainty: Literal["explicit", "inferred"] = "explicit"
    replaces: str | None = None  # an earlier relation of the same pair this one takes the place of (宿敵 → 未婚夫妻)
    period: str = ""  # when it holds, if not the story's present (前世)


class RelationsOutput(_M):
    relations: list[RelationOut]


# ------------------------------------------------------------ a second look at what simple rules found
class FlagCheck(_M):
    unit_id: str
    rule: str
    real: bool  # the rule found a real problem here (it goes to review); false = the text is fine as it is
    reason: str


class ConfirmOutput(_M):
    checks: list[FlagCheck]


# ------------------------------------------------------------ after a fix: did it fix what was found?
class FixCheck(_M):
    unit_id: str
    # what the source says, in a few words, written from the source alone before comparing before and after; first
    # in the schema so it is written before the verdict
    gist: str
    # fixed: the issue is gone · issue_invalid: the source shows the issue was not an error · still_wrong: the new text
    # still has it · undecided: the source does not settle it
    verdict: Literal["fixed", "issue_invalid", "still_wrong", "undecided"]
    new_error: str = ""  # an error the new text has that the old one did not (empty: none)
    reason: str


class FixCheckOutput(_M):
    checks: list[FixCheck]


SCHEMAS: dict[str, type[BaseModel]] = {
    "translate": TranslationBatchOutput,
    "scene_analyze": SceneAnalysisOutput,
    "memory_update": MemoryUpdateOutput,
    "scene_read": SceneReadOutput,
    "naming": NamingOutput,
    "review": ReviewOutput,
    "verify": VerifyOutput,
    "scene_windows": SceneWindowsOutput,
    "reader": ReaderOutput,
    "proofread": ProofreadOutput,
    "house_style": HouseStyleOutput,
    "relations": RelationsOutput,
    "confirm": ConfirmOutput,
    "impressions": ImpressionsOutput,
    "fix_check": FixCheckOutput,
}


def output_model(stage: str, payload: Any = None) -> type[BaseModel] | None:
    """The schema a step's answer is checked against: a translation asked to read through first (the payload says
    ``"read_through": true``) answers with the list before its translations."""
    base = stage.split(":")[0]
    if base == "translate" and isinstance(payload, dict) and payload.get("read_through") is True:
        return TranslationReadThroughOutput
    return SCHEMAS.get(base)


def _empty_default(prop: dict[str, Any]) -> bool:
    """An optional property whose default is empty: ``""``, ``None``, ``False`` or a list made by ``default_factory``
    (which leaves no ``default`` in the schema)."""
    return "default" not in prop or prop["default"] in ("", None, False, [], {})


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Inline $defs and forbid additional properties; every property is required except optional ones whose default
    is empty (see ``_empty_default``)."""
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
                # a field that is empty unless there is something to say (no flags, no note, no verdicts asked for)
                # may be left out: the model does not write 「"notes": []」 for every paragraph. A field whose default
                # says something (a certainty, a kind) stays required, so leaving it out never upgrades a guess.
                given = set(node.get("required", []))
                out["required"] = [k for k, v in node["properties"].items() if k in given or not _empty_default(v)]
            return out
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return fix(raw)
