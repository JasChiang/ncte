"""Short stand-ins for long ids in what the model reads and writes.

Unit, character, fact and line ids (``u_a0ee6359b742dd15``, ``c_5cda59a1ea27``) cost about ten tokens each, and a
scene analysis repeats them three or four times per line of dialogue. The model gets ``P1`` (paragraph), ``C1``
(character), ``F1`` (fact), ``Q1`` (line) instead and its answer is mapped back before it is checked, so every step
keeps working with the real ids. Only exact values are encoded, and only fields that hold ids or references are mapped
back: a translation or a note is never touched (a source that says 「P1」 stays 「P1」).
"""

from __future__ import annotations

import re
from typing import Any

_ID = re.compile(r"^(?:[ucf]_[0-9a-f]{12,16}|q_[0-9a-f]{16}_\d+)$")
_LETTER = {"u": "P", "c": "C", "f": "F", "q": "Q"}
_REF_KEYS = {"ref", "a", "b", "same_as", "object", "subject", "speaker", "evidence", "replaces_ref"}


def _holds_ids(key: str | None) -> bool:
    return key is not None and (key in _REF_KEYS or key.endswith(("_id", "_ids", "_ref", "_refs", "_candidates")))


class ShortIds:
    def __init__(self) -> None:
        self.to_short: dict[str, str] = {}
        self.to_long: dict[str, str] = {}
        self._n: dict[str, int] = {}

    @classmethod
    def for_payload(cls, payload: Any) -> ShortIds:
        ids = cls()
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
            return self.to_long.get(value, value) if _holds_ids(key) else value
        if isinstance(value, dict):
            return {k: self.decode(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.decode(v, key) for v in value]
        return value
