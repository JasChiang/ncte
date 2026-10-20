"""A retranslation that comes back unchanged is a conflict between the finder's rule and the translator's, not a fix
to check; a later look that checks a paragraph as it is now and does not find an issue again closes the findings about
its older texts (``superseded``)."""

from __future__ import annotations

import json
from typing import Any

from ncte.errors import ContentBlockedError
from ncte.llm.fake import FIXED_MARK, FakeLLM, default_translate

from ..helpers import run_book
from ..scripted import RESPONDERS


def _setup(cfg) -> None:
    cfg.context.proofread_chunk_units = 3
    cfg.context.proofread_passes = 2
    cfg.context.reader_pass = False
    cfg.openrouter.max_concurrency = 1


class Proofreader:
    """Flags one paragraph (the first real sentence it is shown) with ``kind``: in the first pass only, or in both;
    ``refuse_again``: the second pass's stretch holding it is refused by the content filter."""

    def __init__(self, kind: str, *, both: bool = False, refuse_again: bool = False) -> None:
        self.kind, self.both, self.refuse_again = kind, both, refuse_again
        self.target: str | None = None
        self.seen: set[str] = set()
        self.sent: list[int] = []  # the pass of each request that held the target

    def __call__(self, payload: dict[str, Any], req: Any) -> dict[str, Any]:
        paras = payload["paragraphs"]
        if self.target is None:
            self.target = next(p["unit_id"] for p in paras if len(p["ja"].strip()) > 8)
        again = any(p["unit_id"] in self.seen for p in paras)
        self.seen.update(p["unit_id"] for p in paras)
        hit = [p for p in paras if p["unit_id"] == self.target]
        if not hit:
            return {"issues": []}
        self.sent.append(2 if again else 1)
        if again and self.refuse_again:
            raise ContentBlockedError("HTTP 403 content filter: fake PROHIBITED_CONTENT")
        if again and not self.both:
            return {"issues": []}
        p = hit[0]
        return {"issues": [{"unit_id": p["unit_id"], "kind": self.kind, "severity": "high" if self.kind == "meaning"
                            else "medium", "quote": p["zh"][:4], "problem": "引號換成了「」", "fix": "改成『』"}]}


def _job(conn, rid) -> dict[str, Any]:
    row = conn.execute("SELECT checkpoint_json FROM jobs WHERE run_id=? AND stage='proofread'", (rid,)).fetchone()
    return json.loads(row[0] or "{}")


def test_an_unchanged_retranslation_is_a_rule_conflict_and_is_not_sent_again(conn, cfg, fixtures):
    """The translator keeps the source's 『』 that proofreading asked to change: nothing goes to the fix check, the
    finding becomes ``conflict`` with a ``rule_conflict`` beside it (the kind and the finder's evidence), the text
    stays, and the second pass does not send the same kind of issue on that paragraph again."""
    from ncte.gui.data import _flagged
    from ncte.reports.acceptance import acceptance
    from ncte.translation.cache import latest_texts

    _setup(cfg)
    reader = Proofreader("style", both=True)
    asked: list[str] = []  # units sent back to the translator
    checked: list[str] = []

    def translate(payload, req):
        out = default_translate(payload, req)
        asked.extend(f["unit_id"] for f in payload.get("review_feedback", []))
        for t in out["translations"]:  # kept as it was
            t["translated_text"] = t["translated_text"].removesuffix(FIXED_MARK)
        return out

    def fix_check(payload, req):
        checked.extend(i["unit_id"] for i in payload["items"])
        return {"checks": []}

    client = FakeLLM(responders={**RESPONDERS, "proofread": reader, "translate": translate, "fix_check": fix_check})
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert summary.status == "completed", summary
    uid = reader.target
    assert reader.sent == [1, 2]  # both passes saw the paragraph and reported it
    assert asked.count(uid) == 1 and uid not in checked  # sent back once; the unchanged text was not "checked"
    found = conn.execute("SELECT type, status, severity, evidence, translation_id FROM qa_findings WHERE run_id=? AND "
                         "unit_id=? AND type IN ('review:proofread_style','rule_conflict') ORDER BY id",
                         (rid, uid)).fetchall()
    assert [(f["type"], f["status"]) for f in found] == [("review:proofread_style", "conflict"),
                                                         ("rule_conflict", "open")]
    best = latest_texts(conn, book.id)[uid]
    assert found[1]["severity"] == "medium" and found[1]["evidence"].startswith("proofread_style: 引號換成了「」")
    assert found[1]["translation_id"] == best["id"]
    assert conn.execute("SELECT COUNT(*) FROM translations WHERE unit_id=?", (uid,)).fetchone()[0] == 1  # text kept
    passes = _job(conn, rid)["passes"]
    assert [p.get("conflict", 0) for p in passes] == [1, 0]
    assert [p.get("conflict_skipped", 0) for p in passes] == [0, 1]
    rep = acceptance(conn, book.id, reader=False)
    assert rep["conflicts"] == {"count": 1, "units": [uid]}  # listed …
    assert uid not in rep["checks"]["no_open_serious_findings"]["units"]  # … not an open error
    flagged, conflicts = _flagged(conn, book.id, latest_texts(conn, book.id))
    assert uid in conflicts and uid not in flagged  # the GUI counts it the same way


def _stale_run(conn, cfg, fixtures, reader: Proofreader):
    """Pass 1 finds a meaning error on the target; its fix (a changed text) is judged still wrong, so the finding
    about the old text stays open."""
    _setup(cfg)

    def still_wrong(payload, req):
        return {"checks": [{"unit_id": i["unit_id"], "gist": "g", "verdict": "still_wrong", "new_error": "",
                            "reason": "r"} for i in payload["items"]]}

    client = FakeLLM(responders={**RESPONDERS, "proofread": reader, "fix_check": still_wrong})
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    rows = {r["type"]: r for r in conn.execute(
        "SELECT type, status, translation_id FROM qa_findings WHERE run_id=? AND unit_id=? AND type IN "
        "('review:proofread_meaning','fix_still_wrong')", (rid, reader.target))}
    return book, rid, rows


def test_a_finding_about_an_older_text_is_superseded_when_a_later_pass_checks_the_unit_and_does_not_find_it(
        conn, cfg, fixtures):
    reader = Proofreader("meaning")
    book, rid, rows = _stale_run(conn, cfg, fixtures, reader)
    assert reader.sent == [1, 2]
    old, fix = rows["review:proofread_meaning"], rows["fix_still_wrong"]
    assert old["translation_id"] < fix["translation_id"]
    assert old["status"] == "superseded"  # the paragraph was checked again as it is now, and the issue was not found
    # the fix's outcome is about the text pass 2 checked: two looks at the same words that disagree stay open
    assert fix["status"] == "open"
    passes = _job(conn, rid)["passes"]
    assert [p.get("superseded", 0) for p in passes] == [0, 1]
    from ncte.reports.acceptance import acceptance

    assert reader.target not in acceptance(conn, book.id, reader=False)["checks"]["no_open_serious_findings"]["units"]


def test_a_finding_is_not_superseded_when_the_later_pass_did_not_see_the_unit(conn, cfg, fixtures):
    reader = Proofreader("meaning", refuse_again=True)
    book, rid, rows = _stale_run(conn, cfg, fixtures, reader)
    assert 2 in reader.sent  # asked, refused
    assert rows["review:proofread_meaning"]["status"] == "open"
    assert conn.execute("SELECT 1 FROM qa_findings WHERE run_id=? AND unit_id=? AND type='proofread_blocked'",
                        (rid, reader.target)).fetchone()
    assert not any(p.get("superseded") for p in _job(conn, rid)["passes"])


def test_a_serious_meaning_issue_left_unchanged_stays_open_for_a_person(conn, cfg, fixtures):
    """A high-severity claim about the source that the translator would not change is not a disagreement between
    rules: it stays open for a person (acceptance counts it) and no rule_conflict is recorded."""
    from ncte.reports.acceptance import acceptance

    _setup(cfg)
    cfg.context.proofread_passes = 1
    reader = Proofreader("meaning")

    def translate(payload, req):
        out = default_translate(payload, req)
        for t in out["translations"]:
            t["translated_text"] = t["translated_text"].removesuffix(FIXED_MARK)
        return out

    client = FakeLLM(responders={**RESPONDERS, "proofread": reader, "translate": translate,
                                 "fix_check": lambda payload, req: {"checks": []}})
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    uid = reader.target
    found = conn.execute("SELECT type, status FROM qa_findings WHERE run_id=? AND unit_id=? AND type IN "
                         "('review:proofread_meaning','rule_conflict')", (rid, uid)).fetchall()
    assert [(f["type"], f["status"]) for f in found] == [("review:proofread_meaning", "open")]
    rep = acceptance(conn, book.id, reader=False)
    assert uid in rep["checks"]["no_open_serious_findings"]["units"] and rep["conflicts"]["count"] == 0
