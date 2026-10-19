"""Short stand-ins for long ids in what the model reads and writes.

Unit, character, fact and line ids (``u_a0ee6359b742dd15``, ``c_5cda59a1ea27``) cost about ten tokens each, and a
scene analysis repeats them three or four times per line of dialogue. The model gets ``P1`` (paragraph), ``C1``
(character), ``F1`` (fact), ``Q1`` (line) instead and its answer is mapped back before it is checked, so every step
keeps working with the real ids. Only exact values are encoded, and only fields that hold ids or references are mapped
back: a translation is never touched (a source that says 「P1」 stays 「P1」). In what the model writes about people
(a fact, a reason, a summary) a character's code becomes the character's name: the code means someone else in the
next request, and 「C1 前世的兵服是黑色」 kept as written would point at whoever is C1 there.
"""

from __future__ import annotations

import re
from typing import Any

_ID = re.compile(r"^(?:[ucf]_[0-9a-f]{12,16}|q_[0-9a-f]{16}_\d+)$")
_LETTER = {"u": "P", "c": "C", "f": "F", "q": "Q"}
READING_ORDER = ("context_before", "units", "items", "paragraphs", "context_after", "context")
_REF_KEYS = {"ref", "a", "b", "same_as", "object", "subject", "speaker", "evidence", "replaces_ref", "to",
             "source_viewpoint"}
# what the model writes in its own words about people and is kept (memory, cards, notes): codes become names
_PROSE_KEYS = {"a_is", "b_is", "contradictions", "explanation", "location", "note", "notes", "object", "personality",
               "predicate", "question", "reason", "relation", "role", "scene_summary", "summary", "time", "voice", "pov"}
_CODE = re.compile(r"(?<![A-Za-z0-9_])C\d+(?![0-9])")
# what the model writes about paragraphs of the request (the read-through list, ADR-064): a paragraph's code becomes
# its id, a character's code its name — the codes mean other paragraphs and people in the next request
_PARAGRAPH_PROSE_KEYS = {"read_through"}
_PARA_CODE = re.compile(r"(?<![A-Za-z0-9_])P\d+(?![0-9])")
_NAME_KEYS = ("name_zh", "name_ja", "name")


def _holds_ids(key: str | None) -> bool:
    return key is not None and (key in _REF_KEYS or key.endswith(("_id", "_ids", "_ref", "_refs", "_candidates")))


class ShortIds:
    def __init__(self) -> None:
        self.to_short: dict[str, str] = {}
        self.to_long: dict[str, str] = {}
        self._n: dict[str, int] = {}
        self.names: dict[str, str] = {}  # character code → the name the request gives it

    @classmethod
    def for_payload(cls, payload: Any) -> ShortIds:
        """Paragraphs are numbered in reading order first (the text before, the paragraphs asked about, the text
        after), then whatever else the payload mentions: numbered by first mention, a scene's utterance list placed
        before ``units`` made the paragraphs read P26, P27, P10, P28 — "out of order" to the model."""
        ids = cls()
        if isinstance(payload, dict):
            for key in READING_ORDER:
                if key in payload:
                    ids._collect(payload[key])
        ids._collect(payload)
        return ids

    def _collect(self, value: Any) -> None:
        if isinstance(value, str):
            if _ID.match(value) and value not in self.to_short:
                letter = _LETTER[value[0]]
                self._n[letter] = self._n.get(letter, 0) + 1
                code = f"{letter}{self._n[letter]}"
                self.to_short[value], self.to_long[code] = code, value
        elif isinstance(value, dict):
            for v in value.values():
                self._collect(v)
            cid = value.get("character_id") or value.get("id")
            if isinstance(cid, str) and cid in self.to_short and cid.startswith("c_"):
                name = next((value[k] for k in _NAME_KEYS if isinstance(value.get(k), str) and value[k]), None)
                if name:
                    self.names.setdefault(self.to_short[cid], name)
        elif isinstance(value, list):
            for v in value:
                self._collect(v)

    def encode(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.to_short.get(value, value)
        if isinstance(value, dict):
            return {k: self.encode(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.encode(v) for v in value]
        return value

    def decode(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            if _holds_ids(key) and value in self.to_long:
                return self.to_long[value]
            if key in _PARAGRAPH_PROSE_KEYS:
                value = _PARA_CODE.sub(lambda m: self.to_long.get(m.group(0), m.group(0)), value)
                return _CODE.sub(lambda m: self.names.get(m.group(0), m.group(0)), value)
            if key in _PROSE_KEYS and self.names:
                return _CODE.sub(lambda m: self.names.get(m.group(0), m.group(0)), value)
            return value
        if isinstance(value, dict):
            return {k: self.decode(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.decode(v, key) for v in value]
        return value
