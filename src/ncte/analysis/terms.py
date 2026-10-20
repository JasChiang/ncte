"""What the book's recurring terms mean (ADR-069), asked once after reading.

A term the book defines for itself (two kinds of magic, a rank, an organisation) is read wrong by a translator who
has only the rendering in the glossary and the handful of facts chosen for a batch. After reading, the recurring
terms of the glossary (not people's names) are put to the model with what the program finds by string match: example
sentences that contain the term, spread over the book, and the facts whose predicate, object or source sentence
contains it, each with where it is revealed. The model says what the term means here, citing those; the program
checks that what it cites was given and shows the definition from the latest evidence it cites. Nothing here decides
what a term means (ADR-059).

A term the reading never put in the glossary would never be asked (ADR-073: a book read by another model had no entry
for 魔術, and 魔法じゃなくて…魔術士 was misread by every model given no definition). So first the model is shown the
facts and chapter summaries of the whole book and the terms already known, and names the book's own terms missing
from them, citing what it saw them in; the program keeps a name only when it occurs in the book's text often enough
and is not known yet, and those go through the same definition request as the glossary's.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import TermsOutput
from ..memory.db import Transaction
from ..memory.terms import human_terms, set_definition
from ..series import SERIES_SCOPE

CATEGORIES = ("term", "skill", "item", "organization", "place")  # not people's names
MIN_COUNT = 2  # occurrences in the book's text
MAX_TERMS = 60  # the most frequent
EXAMPLES = 3  # example sentences per term, spread over the book
FACTS = 5  # facts per term, spread over the book
PER_REQUEST = 20  # terms per request
MAX_FACTS = 400  # facts shown when naming missing terms (spread over the book)
MAX_CANDIDATES = 20  # named terms kept
SENTENCE_CHARS = 160
_ENDS = "。！？!?"
_IDS = re.compile(r"(?<![0-9A-Za-z_])c_[0-9a-f]{12}(?![0-9a-f])")


def _spread[T](items: list[T], k: int) -> list[T]:
    """At most ``k`` of ``items``, evenly over them (the first and the last included)."""
    if len(items) <= k:
        return items
    if k <= 1:
        return items[:k]
    return [items[j * (len(items) - 1) // (k - 1)] for j in range(k)]


def sentence(text: str, term: str) -> str:
    """The sentence of ``text`` the term first occurs in (between sentence ends), cut around it when very long."""
    i = text.find(term)
    if i < 0:
        return text[:SENTENCE_CHARS]
    start = max(text.rfind(c, 0, i) for c in _ENDS) + 1
    ends = [j for c in _ENDS if (j := text.find(c, i + len(term))) >= 0]
    end = min(ends) + 1 if ends else len(text)
    while end < len(text) and text[end] in "」』）)":
        end += 1
    if end - start > SENTENCE_CHARS:
        start = max(start, i - SENTENCE_CHARS // 2)
        end = min(end, start + SENTENCE_CHARS)
    return text[start:end].strip(" 　")


def _paras(conn: sqlite3.Connection, book_id: str) -> list[tuple[int, str]]:
    return [(r[0], match_text(r[1])) for r in conn.execute(
        "SELECT ordinal, plain_text FROM units WHERE book_id=? AND kind IN ('text','heading') AND spine_index IS NOT NULL "
        "ORDER BY ordinal", (book_id,))]


def _names(conn: sqlite3.Connection, book_id: str) -> dict[str, str]:
    return {r[0]: r[1] or r[2] for r in conn.execute(
        f"SELECT id, display_name_zh, canonical_name FROM characters WHERE {SERIES_SCOPE}", (book_id,))}


def known_terms(conn: sqlite3.Connection, book_id: str) -> set[str]:
    """Every source term of the glossary (people's names too), and terms a person has defined."""
    return {(r[0] or "").strip() for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE}", (book_id,))
            } | human_terms(conn, book_id)


def candidate_request(conn: sqlite3.Connection, book_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(payload, lookup) asking which of the book's own terms are missing from ``known``: the facts (at most
    MAX_FACTS, spread) and the chapter summaries of the whole book, each with a code (``f``, ``m``). None when the
    reading left neither."""
    names = _names(conn, book_id)

    def nm(text: str | None) -> str:
        return _IDS.sub(lambda m: names.get(m.group(0), m.group(0)), text or "")

    facts = conn.execute("SELECT * FROM facts WHERE book_id=? AND retracted_by IS NULL ORDER BY first_revealed_ordinal, "
                         "id", (book_id,)).fetchall()
    rows, ev = [], {}
    for k, f in enumerate(_spread(list(facts), MAX_FACTS), 1):
        ev[f"f{k}"] = int(f["first_revealed_ordinal"])
        rows.append({"ref": f"f{k}", "at": int(f["first_revealed_ordinal"]),
                     "text": f"{nm(names.get(f['subject'], f['subject']))}：{nm(f['predicate'])} {nm(f['object'])}".strip(),
                     "source": match_text(f["evidence_quote"] or "")[:120]})
    sums = []
    for k, r in enumerate(conn.execute(
            "SELECT summary, horizon_ordinal FROM chapter_summaries c WHERE book_id=? AND version=(SELECT MAX(version) "
            "FROM chapter_summaries d WHERE d.book_id=c.book_id AND d.chapter_ref=c.chapter_ref) ORDER BY horizon_ordinal",
            (book_id,)), 1):
        ev[f"m{k}"] = int(r[1] or 0)
        sums.append({"ref": f"m{k}", "at": int(r[1] or 0), "text": nm(r[0])})
    if not rows and not sums:
        return None
    known = sorted(t for t in known_terms(conn, book_id) if t)
    return ({"task": "term_candidates", "known": known, "facts": rows, "summaries": sums}, {"_ev": ev})


def take_candidates(conn: sqlite3.Connection, book_id: str, parsed: Any, lookup: dict[str, Any]) -> list[str]:
    """The named terms the program keeps: cited codes that were given (at least one), not known yet, and occurring at
    least MIN_COUNT times in the book's text (string matching only; whether it is a term is the model's word)."""
    whole = "\n".join(t for _, t in _paras(conn, book_id))
    known = known_terms(conn, book_id)
    out: list[str] = []
    for c in parsed.candidates:
        term = match_text(c.ja or "").strip(" 　「」『』")
        cited = [x.strip() for x in c.evidence]
        if (len(term) < 2 or term in known or term in out or not cited or any(x not in lookup["_ev"] for x in cited)
                or whole.count(term) < MIN_COUNT):
            continue
        out.append(term)
    return out[:MAX_CANDIDATES]


def candidates(conn: sqlite3.Connection, book_id: str, extra: list[str] | None = None) -> list[dict[str, Any]]:
    """The glossary's terms (``CATEGORIES``) that occur at least MIN_COUNT times in this book's text, most frequent
    first (at most MAX_TERMS), and the ``extra`` terms the model named (``take_candidates``; no rendering yet), each
    with its example sentences and the facts that mention it. Terms a person has defined are left out. String
    matching only."""
    paras = _paras(conn, book_id)
    whole = "\n".join(t for _, t in paras)
    skip = human_terms(conn, book_id)
    seen: dict[str, dict[str, Any]] = {}
    for g in conn.execute(f"SELECT source_term, target_term, category, book_id FROM glossary WHERE {SERIES_SCOPE} AND "
                          f"category IN ({','.join('?' * len(CATEGORIES))}) ORDER BY book_id=? DESC, id",
                          (book_id, *CATEGORIES, book_id)):
        term = (g["source_term"] or "").strip()
        if len(term) < 2 or term in seen or term in skip:
            continue
        n = whole.count(term)
        if n >= MIN_COUNT:
            seen[term] = {"ja": term, "zh": g["target_term"], "category": g["category"], "count": n}
    chosen = sorted(seen.values(), key=lambda c: -c["count"])[:MAX_TERMS]
    for term in extra or []:
        if term not in seen and term not in skip and (n := whole.count(term)) >= MIN_COUNT:
            chosen.append({"ja": term, "zh": "", "category": "term", "count": n})
    names = _names(conn, book_id)

    def nm(text: str | None) -> str:
        return _IDS.sub(lambda m: names.get(m.group(0), m.group(0)), text or "")

    facts = conn.execute("SELECT * FROM facts WHERE book_id=? AND retracted_by IS NULL ORDER BY first_revealed_ordinal, "
                         "id", (book_id,)).fetchall()
    for c in chosen:
        term = c["ja"]
        c["examples"] = [(o, sentence(t, term)) for o, t in _spread([p for p in paras if term in p[1]], EXAMPLES)]
        about = [f for f in facts if term in (f["predicate"] or "") or term in (f["object"] or "")
                 or term in match_text(f["evidence_quote"] or "")]
        c["facts"] = [(int(f["first_revealed_ordinal"]), {
            "text": f"{nm(names.get(f['subject'], f['subject']))}：{nm(f['predicate'])} {nm(f['object'])}".strip(),
            "source": match_text(f["evidence_quote"] or "")[:120],
            **({"guess": True} if f["certainty"] == "inferred" else {}),
            **({"period": f["period"]} if f["period"] else {})}) for f in _spread(about, FACTS)]
    return chosen


def requests(conn: sqlite3.Connection, book_id: str,
             extra: list[str] | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(payload, lookup) per PER_REQUEST terms. Codes are unique over the whole step: ``t`` a term, ``s`` an example
    sentence, ``f`` a fact; the lookup maps each back to its term or to where its evidence is."""
    out = []
    chosen = candidates(conn, book_id, extra)
    n_ev = 0
    for i in range(0, len(chosen), PER_REQUEST):
        items, terms, ev = [], {}, {}
        for k, c in enumerate(chosen[i:i + PER_REQUEST], i + 1):
            ref = f"t{k}"
            terms[ref] = c["ja"]
            examples, facts = [], []
            for at, text in c["examples"]:
                n_ev += 1
                ev[f"s{n_ev}"] = at
                examples.append({"ref": f"s{n_ev}", "at": at, "text": text})
            for at, f in c["facts"]:
                n_ev += 1
                ev[f"f{n_ev}"] = at
                facts.append({"ref": f"f{n_ev}", "at": at, **f})
            items.append({"ref": ref, "ja": c["ja"], "zh": c["zh"], "category": c["category"], "count": c["count"],
                          "examples": examples, "facts": facts})
        out.append(({"task": "terms", "terms": items}, {"_terms": terms, "_ev": ev}))
    return out


def apply(conn: sqlite3.Connection, book_id: str, results: list[tuple[TermsOutput, dict[str, Any]]],
          run_id: str | None = None) -> dict[str, int]:
    """Keep each answer whose evidence codes were all given (at least one): shown from the latest position of what it
    cites (and of ``revealed_from``, where the definition holds from). A term asked again loses the model's earlier
    definition (an unanswered one is then undefined); a person's definition is never replaced. ``lookup["_blocked"]``:
    refs the content filter refused (not asked, so their earlier definitions stay)."""
    stats = {"defined": 0, "rejected": 0, "kept_human": 0}
    human = human_terms(conn, book_id)
    with Transaction(conn):
        for parsed, lk in results:
            asked = {ref: term for ref, term in lk["_terms"].items() if ref not in lk.get("_blocked", set())}
            got: dict[str, tuple[Any, list[str]]] = {}
            for d in parsed.terms:
                term = asked.get(d.ref)
                if term is None:
                    stats["rejected"] += 1
                    continue
                if term in human:
                    stats["kept_human"] += 1
                    continue
                cited = [x.strip() for x in d.evidence]
                if not d.definition.strip() or not cited or any(x not in lk["_ev"] for x in cited):
                    stats["rejected"] += 1  # it cites what it was not given: not kept
                    continue
                got[term] = (d, cited)
            for term in asked.values():
                if term not in human:
                    conn.execute("DELETE FROM term_definitions WHERE book_id=? AND source_term=? AND origin='model'",
                                 (book_id, term))
            for term, (d, cited) in got.items():
                at = [lk["_ev"][x] for x in cited]
                since = lk["_ev"].get(d.revealed_from.strip(), min(at))
                set_definition(conn, book_id, term, d.definition, contrast_with=d.contrast_with, origin="model",
                               evidence=[{"kind": "fact" if x.startswith("f") else "example", "at": lk["_ev"][x]}
                                         for x in cited],
                               since=since, revealed=max(*at, since), run_id=run_id)
                stats["defined"] += 1
    return stats
