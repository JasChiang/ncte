"""A reader's look at the translation (ADR-038).

After a volume is translated, the model reads the Chinese alone, as a reader who knows no Japanese, and points at
what such a reader would stumble on: a reference left unexplained (水野 §92: 「たかぎ」 is also a clothing chain, kept
in kana with no word of it), a joke on sound or writing that does not come across, heavy translationese, a voice or
你／妳 that changes, a contradiction. Each point goes back to the translator with the source and the context, which
decides: render it differently, add a translator's note, or keep it. The program supplies hints (kana kept in the
translation, a source that talks about words) and checks the answers.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..epub.units import PH_RE
from ..memory.repository import character_basics
from ..translation.cache import latest_texts

CHUNK_UNITS = 120  # paragraphs per request (one stretch of reading)
_KANA = re.compile("[ぁ-ゟァ-ヺ]{2,}")
# the source talks about words themselves: a likely play on sound or writing
_ABOUT_WORDS = re.compile(r"名前|名字|読み|響き|発音|漢字|平仮名|ひらがな|片仮名|カタカナ|ダジャレ|駄洒落|語呂|同じ字|同じ音|聞き間違")


def chunks(conn: sqlite3.Connection, book_id: str, unit_ids: set[str] | None = None) -> list[list[dict[str, Any]]]:
    """Consecutive translated paragraphs (story and book matter alike), chapter by chapter, in reading order."""
    best = latest_texts(conn, book_id)
    out: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    href = None
    for r in conn.execute("SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND "
                          "spine_index IS NOT NULL ORDER BY ordinal", (book_id,)):
        t = best.get(r["id"])
        if t is None or (unit_ids is not None and r["id"] not in unit_ids):
            continue
        if cur and (r["resource_href"] != href and len(cur) >= CHUNK_UNITS // 3 or len(cur) >= CHUNK_UNITS):
            out.append(cur)
            cur = []
        href = r["resource_href"]
        item: dict[str, Any] = {"unit_id": r["id"], "zh": PH_RE.sub("", t["text"])}
        look = hint(r, t["text"])
        if look:
            item["look"] = look
        cur.append(item)
    if cur:
        out.append(cur)
    return out


def hint(unit: sqlite3.Row, text: str) -> str | None:
    if _KANA.search(PH_RE.sub("", text)):
        return "譯文保留了日文假名"
    if _ABOUT_WORDS.search(unit["plain_text"]):
        return "原文在談論字詞本身（讀音、寫法、名字）"
    return None


def characters(conn: sqlite3.Connection, book_id: str) -> list[dict[str, str]]:
    from ..series import SERIES_SCOPE

    out = []
    for c in conn.execute(f"SELECT c.*, g.gender FROM characters c LEFT JOIN character_gender g ON g.character_id=c.id "
                          f"WHERE c.{SERIES_SCOPE} ORDER BY c.known_from_ordinal LIMIT 40", (book_id,)):
        item = {"name": c["display_name_zh"] or c["canonical_name"],
                **{k: v for k, v in character_basics(conn, c["id"]).items() if k != "aliases_ja"}}
        out.append(item)
    return out


FIX_NOTE = ("讀者（不懂日文）讀到這段會卡住。請對照原文與前後文判斷：能換個譯法讓讀者懂就改寫；譯法無法傳達（雙關、文化梗）"
            "就保留譯文並加上簡短譯註；若其實讀得懂、或原文本來就是這樣，就維持原譯。不要為了這點改動其他意思。")
