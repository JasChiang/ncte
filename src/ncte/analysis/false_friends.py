"""Kanji words carried over unchanged into the Chinese (same-form words, 同形詞), asked once after translating.

A Japanese kanji word written the same in Chinese may mean something else there, or sound wrong in Taiwan (応接室 →
應接室, 二学期 → 二學期): the translator copies it, and proofreading reads past it because the sentence still makes
sense. After the first translation of the book (before proofreading), the program finds, by string match, the kanji
words of each translated paragraph's source that appear in its translation in their Taiwan traditional form
(Japanese forms converted with OpenCC ``jp2t`` then ``t2tw``; without the ``opencc`` package only the program's own
short list of Japanese glyphs, ``qa.rules.JA_TO_TW``, is converted). The most frequent go to the model with two
examples each (source and translation); it says which ones read otherwise in Taiwan Chinese here and how to say them,
citing the examples. Which words are fine is the model's call (ADR-059): the program only checks that what an answer
cites was given for that word. A word that differs gets a glossary entry (unverified, a note saying why) and every
paragraph that carried it over goes back to the translator with the suggestion, through the usual fix and its check.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from ..epub.units import PH_RE, match_text
from ..llm.schemas import FalseFriendsOutput
from ..memory import repository as repo
from ..memory.db import Transaction
from ..series import SERIES_SCOPE
from ..translation.cache import latest_texts
from .terms import sentence

MIN_LEN, MAX_LEN = 2, 6  # ideographs in a word
MAX_WORDS = 300  # the most frequent, per pass
PER_REQUEST = 40  # words per request
EXAMPLES = 2  # paragraphs shown per word
NOTE = "同形異義"
_KANJI_RUN = re.compile("[㐀-䶿一-鿿]{2,}")
_UNITS = ("SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND "
          "spine_index IS NOT NULL ORDER BY ordinal")
# what the translator is asked with each word sent back (kept out of what the fix check reads as the suggestion)
FIX_NOTE = ("這個詞照搬了原文的漢字，但在臺灣中文裡意思或用法不同（見說明）。請對照原文，改成臺灣讀者自然的說法，其他照舊；"
            "說明不成立（在這裡臺灣中文也這樣說、意思相同），就維持原譯。")


@lru_cache(maxsize=1)
def converter() -> Callable[[str], str]:
    """Japanese forms → Taiwan traditional forms (応接室 → 應接室, 学校 → 學校)."""
    try:
        import opencc
    except ImportError:  # the program's own short list of Japanese glyphs only
        from ..qa.rules import JA_TO_TW

        table = {ord(k): v for k, v in JA_TO_TW.items()}
        return lambda s: s.translate(table)
    jp, tw = opencc.OpenCC("jp2t"), opencc.OpenCC("t2tw")
    return lambda s: str(tw.convert(jp.convert(s)))


def carried(source: str, translation: str, conv: Callable[[str], str] | None = None) -> list[str]:
    """The kanji words of ``source`` found in ``translation`` in their Taiwan form (or as written): in each run of
    ideographs, from left to right, the longest stretch of MIN_LEN–MAX_LEN found."""
    conv = conv or converter()
    text = PH_RE.sub("", translation)
    out: list[str] = []
    for run in _KANJI_RUN.findall(match_text(source)):
        i = 0
        while i <= len(run) - MIN_LEN:
            for n in range(min(MAX_LEN, len(run) - i), MIN_LEN - 1, -1):
                w = run[i:i + n]
                if conv(w) in text or w in text:
                    out.append(w)
                    i += n
                    break
            else:
                i += 1
    return out


def _settled(conn: sqlite3.Connection, book_id: str) -> set[str]:
    """Words not to ask about: a person's glossary entries, and names (glossary characters, every form of a card)."""
    out = {r[0] for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND (verified=1 OR "
                                      "category='character')", (book_id,))}
    for c in repo.characters(conn, book_id):
        out |= {c["canonical_name"], *json.loads(c["aliases_json"] or "[]")}
    return out


def collect(conn: sqlite3.Connection, book_id: str, story: set[str] | None = None,
            since: int | None = None) -> list[dict[str, Any]]:
    """The words carried over, most frequent first (at most MAX_WORDS): each with its count and the paragraphs that
    carried it (``hits``: unit id, translation id, source, translation). A person's text is not looked at; ``story``:
    only these files; ``since``: only paragraphs whose current translation is newer (2.11)."""
    best = latest_texts(conn, book_id)
    skip = _settled(conn, book_id)
    conv = converter()
    found: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(_UNITS, (book_id,)):
        t = best.get(r["id"])
        if t is None or t["status"] == "human" or (story is not None and r["resource_href"] not in story):
            continue
        if since is not None and t["id"] <= since:
            continue
        for w in dict.fromkeys(carried(r["plain_text"], t["text"], conv)):
            if w not in skip:
                found.setdefault(w, []).append({"unit_id": r["id"], "tid": t["id"], "ja": match_text(r["plain_text"]),
                                                "zh": PH_RE.sub("", t["text"])})
    words = sorted(found, key=lambda w: (-len(found[w]), found[w][0]["unit_id"]))[:MAX_WORDS]
    return [{"ja": w, "zh": conv(w), "count": len(found[w]), "hits": found[w]} for w in words]


def requests(words: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(payload, lookup) per PER_REQUEST words. Codes are unique over the whole step: ``w`` a word, ``e`` an example;
    the lookup maps each back to its word."""
    out = []
    n = 0
    for i in range(0, len(words), PER_REQUEST):
        items, refs, ev = [], {}, {}
        for k, w in enumerate(words[i:i + PER_REQUEST], i + 1):
            ref = f"w{k}"
            refs[ref] = w
            examples = []
            for h in _spread(w["hits"]):
                n += 1
                ev[f"e{n}"] = w["ja"]
                zh = w["zh"] if w["zh"] in h["zh"] else w["ja"]
                examples.append({"ref": f"e{n}", "ja": sentence(h["ja"], w["ja"]), "zh": sentence(h["zh"], zh)})
            items.append({"ref": ref, "ja": w["ja"], "zh": w["zh"], "count": w["count"], "examples": examples})
        out.append(({"task": "false_friends", "words": items}, {"_words": refs, "_ev": ev}))
    return out


def _spread(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return hits if len(hits) <= EXAMPLES else [hits[0], hits[-1]]


def apply(conn: sqlite3.Connection, book_id: str, results: list[tuple[FalseFriendsOutput, dict[str, Any]]]
          ) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Keep each answer that cites at least one example, all of them given for that word. A word that differs (with
    a rendering other than the carried-over form) gets a glossary entry; returned with its paragraphs, to be sent back
    to the translator."""
    stats = {"same": 0, "differs": 0, "rejected": 0, "glossary": 0}
    differs: list[dict[str, Any]] = []
    with Transaction(conn):
        for parsed, lk in results:
            for a in parsed.words:
                w = lk["_words"].get(a.ref.strip())
                cited = [lk["_ev"].get(x.strip()) for x in a.evidence]
                if w is None or not cited or any(x != w["ja"] for x in cited):
                    stats["rejected"] += 1  # cites nothing, or what it was not given for this word: not kept
                    continue
                if a.verdict == "same":
                    stats["same"] += 1
                    continue
                zh = a.suggestion.strip()
                if not zh or zh in (w["zh"], w["ja"]):
                    stats["rejected"] += 1
                    continue
                stats["differs"] += 1
                if repo.note_false_friend(conn, book_id, w["ja"], zh, f"{NOTE}：{a.reason.strip()}"[:300],
                                          first_unit_id=w["hits"][0]["unit_id"]):
                    stats["glossary"] += 1
                differs.append({**w, "suggestion": zh, "reason": a.reason.strip()})
    return stats, differs
