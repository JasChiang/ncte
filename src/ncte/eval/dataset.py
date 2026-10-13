"""Evaluation dataset format (spec §11) and EPUB builder."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..samples import BookSpec, Doc, paras, write_book

DEFAULT_DATASET = Path(__file__).parent / "data" / "ncte_eval_v1.json"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldUtterance(_M):
    line: int
    quote_index: int = 0
    speaker: str | None  # None = the correct answer is "unknown"
    explicit: bool = False
    weak_only: bool = False


class GoldUnit(_M):
    line: int
    forbidden: list[str] = Field(default_factory=list)  # must NOT appear (added names, gender pronouns …)
    required_any: list[list[str]] = Field(default_factory=list)  # each group: at least one must appear


class Spoiler(_M):
    before_line: int
    terms: list[str]


class Gold(_M):
    utterances: list[GoldUtterance] = Field(default_factory=list)
    units: list[GoldUnit] = Field(default_factory=list)
    spoiler: Spoiler | None = None


class Item(_M):
    id: str
    category: str
    lines: list[str]
    gold: Gold

    @model_validator(mode="after")
    def _lines_exist(self) -> Item:
        n = len(self.lines)
        lines = [u.line for u in self.gold.utterances] + [u.line for u in self.gold.units]
        for line in lines:
            if not 0 <= line < n:
                raise ValueError(f"{self.id}: gold line {line} out of range")
        return self


class CharacterGold(_M):
    name_ja: str
    allowed_zh: list[str]


class Dataset(_M):
    name: str
    version: int
    description: str = ""
    characters: list[CharacterGold]
    items: list[Item]


def load_dataset(path: str | Path | None = None) -> Dataset:
    p = Path(path) if path else DEFAULT_DATASET
    return Dataset.model_validate(json.loads(p.read_text(encoding="utf-8")))


def item_doc_href(item: Item) -> str:
    return f"Text/{item.id}.xhtml"


def build_dataset_epub(ds: Dataset, path: Path) -> Path:
    docs = []
    for item in ds.items:
        body = f'<section id="{item.id}">\n<h1>{item.id}</h1>\n' + paras(item.lines) + "\n</section>"
        docs.append(Doc(item.id.replace("_", "-"), item_doc_href(item), item.id, body))
    spec = BookSpec(f"NCTE eval {ds.name}", "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-00000000e7a1", docs)
    return write_book(spec, path)
