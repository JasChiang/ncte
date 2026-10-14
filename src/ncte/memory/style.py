"""Series style guide (ADR-022): decisions that must hold for every volume of a series.

Stored in ``series.style_json``. Only what the human set is sent to the model; the default style adds nothing to
the request, so existing translations and their cache keys are unaffected until a style is set. Changing the
style does not invalidate existing translations (it is a direction for new ones); ``ncte retranslate`` /
``translate --force`` apply it to existing units.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..epub.units import match_text

HONORIFICS_ZH = {
    "natural": "敬稱依語氣與關係自然處理：さん多半省略或視情況用「小姐／先生」，ちゃん可用「小～」或暱稱語感，"
               "先輩譯「學長／學姊」，不硬套固定譯法。",
    "explicit": "敬稱盡量譯出並固定對應（さん→「小姐／先生」、ちゃん→「小～」、くん→「同學」、先輩→「學長／學姊」），"
                "讓角色之間的距離感明確。",
    "omit": "一般敬稱（さん、くん、ちゃん）原則上省略，只在敬稱本身有意義（關係變化、刻意疏遠或親暱）時譯出。",
}


class SeriesStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    honorifics: Literal["natural", "explicit", "omit"] = "natural"
    rules: list[str] = Field(default_factory=list)  # free-form house rules, e.g. 「先輩一律譯學姊」
    voices: dict[str, str] = Field(default_factory=dict)  # character name → how they should sound
    notes: Literal["off", "inline"] = "inline"  # ADR-025: translator's notes in parentheses (on unless turned off)

    def is_default(self) -> bool:
        return self == SeriesStyle()


def _series_id(conn: sqlite3.Connection, book_id: str) -> str | None:
    row = conn.execute("SELECT series_id FROM books WHERE id=?", (book_id,)).fetchone()
    return str(row[0]) if row and row[0] else None


def load_style(conn: sqlite3.Connection, book_id: str) -> SeriesStyle:
    sid = _series_id(conn, book_id)
    row = conn.execute("SELECT style_json FROM series WHERE id=?", (sid,)).fetchone() if sid else None
    return SeriesStyle.model_validate(json.loads(row[0] or "{}")) if row else SeriesStyle()


def save_style(conn: sqlite3.Connection, book_id: str, style: SeriesStyle) -> None:
    sid = _series_id(conn, book_id)
    if sid is None:
        raise KeyError(f"book {book_id} has no series")
    blob = json.dumps(style.model_dump(exclude_defaults=True), ensure_ascii=False)
    if conn.execute("UPDATE series SET style_json=? WHERE id=?", (blob, sid)).rowcount == 0:
        # no series row (e.g. a database used outside a workspace): create it
        from .db import utcnow

        title = conn.execute("SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
        conn.execute("INSERT INTO series(id, title, slug, created_at, style_json) VALUES (?,?,?,?,?)",
                     (sid, (title[0] if title else None) or sid, sid, utcnow(), blob))


def style_payload(style: SeriesStyle, text: str, names: list[str] = ()) -> dict[str, Any] | None:  # type: ignore[assignment]
    """The ``style`` block of a translation request: the honorific policy, every house rule and the voice notes
    of characters named in ``text`` or present in the scene (``names``), and whether notes are allowed. None when
    nothing differs from the defaults and notes are off."""
    t = match_text(text)
    out: dict[str, Any] = {}
    custom = style.model_copy(update={"notes": SeriesStyle().notes}) != SeriesStyle()
    if custom:
        out["honorifics"] = HONORIFICS_ZH[style.honorifics]
    if style.rules:
        out["rules"] = list(style.rules)
    if style.notes == "inline":
        out["notes"] = ("允許譯註：原文的雙關、文字遊戲或文化指涉在譯文中無法傳達、讀者會看不懂時，"
                        "可在 translator_note 寫一句簡短說明（不寫劇情、不劇透）；能用譯文本身表達的就不要加註。")
    voices = {k: v for k, v in style.voices.items() if k in t or k in names}
    if voices:
        out["voices"] = voices
    return out or None
