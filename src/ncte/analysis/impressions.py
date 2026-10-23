"""What a translator keeps in mind about each main character: personality, way of speaking, place in the story (ADR-045).

After reading, the main characters (most lines, and the narrators) are described by the model from what reading
gathered — their lines spread over the book, facts about them, how they address others, their relations — citing the
lines and facts it rests on. The way of speaking goes onto the translator's cards for characters present; the page
shows all three. The place in the story is asked without endings or late reveals, and the translator never gets it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import ImpressionsOutput
from ..memory.address import calls_by
from ..memory.db import Transaction, utcnow
from ..memory.repository import character_basics, qualified_fact, relation_guess
from ..series import SERIES_SCOPE

MAIN = 12
PER_REQUEST = 4
LINES = 8


def main_characters(conn: sqlite3.Connection, book_id: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        f"SELECT c.*, (SELECT COUNT(*) FROM utterances u WHERE u.speaker_id_nullable=c.id) AS n FROM characters c "
        f"WHERE c.{SERIES_SCOPE} ORDER BY c.is_narrator DESC, n DESC", (book_id,)).fetchall()
    return [r for r in rows if r["n"] >= 3 or r["is_narrator"]][:MAIN]


def requests(conn: sqlite3.Connection, book_id: str, only: set[str] | None = None
             ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(payload, lookup) per PER_REQUEST main characters; ``only``: these (the ones a failed request left unasked)."""
    chars = [c for c in main_characters(conn, book_id) if only is None or c["id"] in only]
    names = {r["id"]: r["display_name_zh"] or r["canonical_name"]
             for r in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,))}
    out = []
    for i in range(0, len(chars), PER_REQUEST):
        group, items = chars[i:i + PER_REQUEST], []
        ev: dict[str, tuple[str, int]] = {}
        for k, c in enumerate(group, 1):
            said = conn.execute("SELECT x.quote_text, x.status, u.ordinal FROM utterances x JOIN units u ON u.id=x.unit_id WHERE "
                                "x.speaker_id_nullable=? ORDER BY u.ordinal", (c["id"],)).fetchall()
            spread = said if len(said) <= LINES else [said[j * (len(said) - 1) // (LINES - 1)] for j in range(LINES)]
            lines = []
            for s in spread:
                r = f"l{len(ev) + 1}"
                ev[r] = ("line", s["ordinal"])
                lines.append({"ref": r, "at": s["ordinal"], "text": match_text(s["quote_text"])[:120],
                              **({"guess": True} if s["status"] == "inferred" else {})})  # a line read as his, not stated
            facts = []
            for f in conn.execute("SELECT * FROM facts WHERE book_id=? AND (subject=? OR object_ref=?) AND retracted_by IS "
                                  "NULL ORDER BY first_revealed_ordinal LIMIT 8", (book_id, c["id"], c["id"])):
                r = f"f{len(ev) + 1}"
                ev[r] = ("fact", f["first_revealed_ordinal"])
                # a fact where this person is only the object says whose it is: 「凱爾：向諾艾兒自稱出身於山村」
                # under 諾艾兒's card read as hers
                who = "" if f["subject"] == c["id"] else f"{names.get(f['subject'], f['subject'])}："
                facts.append({"ref": r, "at": f["first_revealed_ordinal"],
                              "text": qualified_fact(f, f"{who}{f['predicate']} {f['object']}", names)})
            calls = [f"→{names.get(a['addressee_id'], '?')}：{a['form_ja']}" for a in conn.execute(
                "SELECT * FROM address_terms WHERE book_id=? AND speaker_id=? ORDER BY uses DESC", (book_id, c["id"]))
                if calls_by(conn, a)][:6]
            rels = [f"{r['a_is'] if r['a_id'] == c['id'] else r['b_is']}（對{names.get(r['b_id'] if r['a_id'] == c['id'] else r['a_id'], '?')}）"
                    f"{relation_guess(r)}"
                    for r in conn.execute("SELECT * FROM relations WHERE book_id=? AND (a_id=? OR b_id=?) LIMIT 6",
                                          (book_id, c["id"], c["id"]))]
            items.append({"ref": f"c{k}", "name": names[c["id"]], "narrator": bool(c["is_narrator"]),
                          **{k2: v for k2, v in character_basics(conn, c["id"]).items() if k2 != "narrator"}, "lines": lines,
                          "facts": facts, "calls_others": calls, "relations": rels})
        out.append(({"task": "impressions", "characters": items},
                    {"_chars": {f"c{k}": c["id"] for k, c in enumerate(group, 1)}, "_ev": ev}))
    return out


def apply(conn: sqlite3.Connection, book_id: str, results: list[tuple[ImpressionsOutput, dict[str, Any]]],
          run_id: str | None = None) -> int:
    """Each character asked gets its stretches anew (ADR-060): from where each holds (its ``from`` ref, or its earliest
    evidence), in which time, and the latest evidence it rests on (what it says is shown only once that is reached)."""
    n = 0
    with Transaction(conn):
        for parsed, lk in results:
            asked = {cid for cid in lk["_chars"].values()}
            got: dict[str, list[tuple[Any, list[str]]]] = {}
            for imp in parsed.impressions:
                cid = lk["_chars"].get(imp.ref)
                cited = [x for x in imp.evidence if x in lk["_ev"]]
                if not cid or not cited or not imp.voice.strip():
                    continue
                got.setdefault(cid, []).append((imp, cited))
            for cid in asked & set(got):
                conn.execute("DELETE FROM character_impressions WHERE character_id=?", (cid,))
                for imp, cited in got[cid]:
                    at = [lk["_ev"][x][1] for x in cited]
                    since = lk["_ev"][imp.since][1] if imp.since in lk["_ev"] else min(at)
                    # how they talk to some people differently: kept only with evidence of its own, and shown from the
                    # first of it (it may show a relation the story has not reached yet)
                    toward = [lk["_ev"][x][1] for x in imp.toward_evidence if x in lk["_ev"]]
                    shown = imp.voice_toward.strip()[:80] if toward else ""
                    conn.execute("INSERT INTO character_impressions(character_id, book_id, since_ordinal, period, "
                                 "personality, voice, role, evidence_json, revealed_ordinal, run_id, updated_at, "
                                 "voice_toward, toward_revealed_ordinal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (cid, book_id, since, imp.period.strip()[:20] or None, imp.personality.strip()[:200],
                                  imp.voice.strip()[:120], imp.role.strip()[:120],
                                  json.dumps([{"kind": lk["_ev"][x][0], "at": lk["_ev"][x][1]} for x in cited]),
                                  max(at), run_id, utcnow(), shown, min(toward) if shown else None))
                    n += 1
    return n
