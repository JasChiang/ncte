"""The post-read steps (the second look, genders, relations, impressions, terms) send their requests side by side
(``openrouter.max_concurrency`` at a time); the notes they write are the same as one request at a time, because the
answers are applied in the requests' order once all are back."""

from __future__ import annotations

import time
from typing import Any

from ncte.llm.fake import FakeLLM

from ..scripted import RESPONDERS
from .test_parallel_passes import Watching, _book, _db


def _notes(conn) -> dict[str, list[tuple[Any, ...]]]:
    """What the post-read steps wrote (ids and times aside)."""
    def rows(sql: str) -> list[tuple[Any, ...]]:
        return sorted((tuple(r) for r in conn.execute(sql)), key=lambda t: tuple(str(x) for x in t))

    return {
        "relations": rows("SELECT a_id, b_id, relation_zh, a_is, b_is, since_ordinal FROM relations"),
        "impressions": rows("SELECT character_id, personality, voice, role FROM character_impressions"),
        "genders": rows("SELECT character_id, gender, certainty FROM character_gender"),
        "terms": rows("SELECT * FROM term_definitions"),
        "verify": rows("SELECT unit_id, type, severity, evidence FROM qa_findings WHERE type LIKE 'verify%'"),
    }


class Slow(Watching):
    """The post-read steps' answers take a little while, so requests sent side by side overlap; ``most`` counts how
    many of a step were in flight at once."""

    def complete(self, req: Any) -> Any:
        st = req.stage.split(":")[0]
        if st not in ("verify", "relations", "impressions", "terms"):
            return super().complete(req)
        with self.guard:
            self.asked[st] = self.asked.get(st, 0) + 1
            self.now[st] = self.now.get(st, 0) + 1
            self.most[st] = max(self.most.get(st, 0), self.now[st])
        try:
            time.sleep(0.05)
            return FakeLLM.complete(self, req)
        finally:
            with self.guard:
                self.now[st] -= 1


def _relate(payload: dict[str, Any], req: Any) -> dict[str, Any]:
    """Every pair with evidence is 同伴, from its first piece of evidence."""
    out = []
    for p in payload["pairs"]:
        refs = [x["ref"] for k in ("notes", "facts", "passages") for x in p.get(k, [])]
        if refs:
            out.append({"a": p["a"], "b": p["b"], "relation": "同伴", "a_is": "同伴", "b_is": "同伴",
                        "facts": refs[:1], "known_from": refs[0], "reason": "一起行動"})
    return {"relations": out}


def test_post_read_steps_side_by_side_leave_what_one_at_a_time_leaves(cfg, fixtures, tmp_path, monkeypatch):
    from ncte.analysis import gender_gaps, impressions, relations, terms

    monkeypatch.setattr("ncte.translation.context_pipeline.VERIFY_BATCH", 1)
    monkeypatch.setattr(relations, "PAIRS_PER_REQUEST", 1)
    monkeypatch.setattr(impressions, "PER_REQUEST", 1)
    monkeypatch.setattr(gender_gaps, "PER_REQUEST", 1)
    monkeypatch.setattr(terms, "PER_REQUEST", 1)
    got = {}
    for conc in (1, 3):
        cfg.openrouter.max_concurrency = conc
        conn = _db(tmp_path, f"p{conc}")
        client = Slow(responders={**RESPONDERS, "relations": _relate})
        _b, _rid, summary = _book(conn, cfg, fixtures, client)
        assert summary.status == "completed", summary
        got[conc] = (_notes(conn), dict(client.asked), dict(client.most))
        conn.close()
    one, three = got[1], got[3]
    assert one[0] == three[0]  # the same notes
    assert one[0]["relations"]  # the sample book has relations to compare
    assert one[1] == three[1]  # the same requests, step by step
    steps = ("verify", "relations", "impressions", "terms")
    assert all(one[2].get(st, 0) <= 1 for st in steps)
    assert one[1]["verify"] > 1 and three[2]["verify"] > 1  # really side by side (the sample book's other steps
    # send one request each here; relations side by side: test_pause_checkpoint)
