"""ADR-023: read the whole volume first, name, then translate."""

from ncte.library import import_book
from ncte.memory import visibility as vis
from ncte.memory.db import utcnow
from ncte.memory.repository import ensure_glossary, upsert_character


def _units(conn, book_id):
    return conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal",
                        (book_id,)).fetchall()


def test_character_names_follow_the_reveal(conn, cfg, fixtures):
    """A character merged after an identity reveal keeps its earlier name before the reveal."""
    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    early, late = us[1], us[5]
    cid, _ = upsert_character(conn, book.id, "かぐや", name_zh="輝夜", aliases=["謎の少女"], unit_id=early["id"],
                              ordinal=early["ordinal"], origin="t")
    ensure_glossary(conn, book.id, "謎の少女", "謎之少女", category="character", origin="t", first_unit_id=early["id"])
    ensure_glossary(conn, book.id, "かぐや", "輝夜", category="character", origin="t", first_unit_id=late["id"])
    conn.execute("INSERT INTO entity_links(book_id, term, target_term, form_type, origin, status, revealed_ordinal, "
                 "created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (book.id, "謎の少女", "かぐや", "alias", "llm", "accepted", late["ordinal"], utcnow()))
    c = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()

    before = vis.character_view(conn, book.id, c, late["ordinal"] - 1)
    assert before == {"name_ja": "謎の少女", "name_zh": "謎之少女", "aliases_ja": []}
    after = vis.character_view(conn, book.id, c, late["ordinal"])
    assert after == {"name_ja": "かぐや", "name_zh": "輝夜", "aliases_ja": ["謎の少女"]}
    assert vis.character_view(conn, book.id, c, early["ordinal"] - 1) is None


# ---------------------------------------------------------------- naming (pass 2)
import json  # noqa: E402

import pytest  # noqa: E402

from ncte.analysis import naming  # noqa: E402
from ncte.llm.fake import FakeLLM  # noqa: E402

from ..helpers import resume_run, run_book  # noqa: E402
from ..scripted import RESPONDERS  # noqa: E402


def naming_responder(payload, req):
    refs = {n["ref"] for n in payload["names"]}
    out = {"names": [], "address": []}
    if "カイト" in refs:
        out["names"].append({"ref": "カイト", "zh": "海斗", "reason": "沿用漢字感的譯名",
                             "alternatives": [{"zh": "凱特", "reason": "音譯"}]})
    if "銀の鍵" in refs:
        out["names"].append({"ref": "銀の鍵", "zh": "銀之鑰", "reason": "道具名", "alternatives": []})
    out["names"].append({"ref": "not_asked", "zh": "x", "reason": "", "alternatives": []})  # rejected
    return out


@pytest.fixture
def low_thresholds(monkeypatch):
    monkeypatch.setattr(naming, "REVIEW_NAME_COUNT", 1)
    monkeypatch.setattr(naming, "REVIEW_ADDRESS_USES", 1)


def test_naming_pauses_for_choices_then_translates(conn, cfg, fixtures, low_thresholds):
    fake = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "awaiting_names"
    stages = [c.stage.split(":")[0] for c in fake.calls]
    assert "naming" in stages and "translate" not in stages  # read, named, nothing translated yet
    left = naming.pending(conn, book.id)
    assert [(p["label"], [o["zh"] for o in p["options"]]) for p in left] == [("カイト", ["海斗", "凱特"])]
    applied = {p["ref"]: p["status"] for p in naming.pending(conn, book.id, include_done=True)}
    assert applied == {"カイト": "pending", "銀の鍵": "applied"}  # no alternatives → applied without asking

    naming.decide(conn, book.id, left[0]["id"], "凱特")
    g = conn.execute("SELECT target_term, verified FROM glossary WHERE source_term='カイト'").fetchone()
    assert tuple(g) == ("凱特", 1)
    assert conn.execute("SELECT display_name_zh FROM characters WHERE canonical_name='カイト'").fetchone()[0] == "凱特"

    fake2 = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    s2 = resume_run(conn, cfg, rid, fake2)
    assert s2.status == "completed"
    tr = [json.loads(c.messages[1]["content"]) for c in fake2.calls if c.stage.startswith("translate")]
    kaito = [g for p in tr for g in p.get("glossary", []) if g["ja"] == "カイト"]
    assert kaito and all(g["zh"] == "凱特" and g["verified"] for g in kaito)
    assert "naming" not in [c.stage for c in fake2.calls]  # not proposed again


def test_naming_auto_does_not_pause(conn, cfg, fixtures, low_thresholds):
    cfg.context.naming = "auto"
    fake = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    assert [p["label"] for p in naming.pending(conn, book.id)] == ["カイト"]  # still listed for a later look


def test_naming_off_and_legacy_flow_skip_it(conn, cfg, fixtures):
    cfg.context.naming = "off"
    fake = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    _, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed" and "naming" not in [c.stage for c in fake.calls]


def test_single_character_name_needs_a_decision(conn, cfg, fixtures, low_thresholds):
    def one_char(payload, req):
        return {"names": [{"ref": "カイト", "zh": "凱", "reason": "簡短", "alternatives": []}], "address": []}

    fake = FakeLLM(responders={**RESPONDERS, "naming": one_char})
    book, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "awaiting_names"
    assert [p["chosen_zh"] for p in naming.pending(conn, book.id)] == ["凱"]


def test_human_renderings_are_not_proposed_or_overwritten(conn, cfg, fixtures, low_thresholds):
    from ncte.library import import_book
    from ncte.memory.repository import set_glossary

    book, _ = import_book(conn, cfg, fixtures["basic"])
    set_glossary(conn, book.id, "カイト", "阿凱", category="character")
    fake = FakeLLM(responders={**RESPONDERS, "naming": naming_responder})
    _, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    payload = next(json.loads(c.messages[1]["content"]) for c in fake.calls if c.stage == "naming")
    assert "カイト" not in {n["ref"] for n in payload["names"]}
    assert conn.execute("SELECT target_term FROM glossary WHERE source_term='カイト'").fetchone()[0] == "阿凱"
    assert s.status == "completed"  # nothing left to decide (カイト rejected as not asked)


def test_naming_payload_has_counts_and_samples(conn, cfg, fixtures):
    fake = FakeLLM(responders=RESPONDERS)
    book, _, _ = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    p = naming.build_naming_payload(conn, book.id)
    kaito = next(n for n in p["names"] if n["ref"] == "カイト")
    assert kaito["count"] >= 3 and kaito["samples"] and all("カイト" in s for s in kaito["samples"])


def test_cli_proposals_and_decide(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "names", "proposals", epub])
    assert out.exit_code == 0 and "no proposals waiting" in out.output
    bad = r.invoke(app, ["-w", str(ws), "names", "decide", epub, "--id", "1"])
    assert bad.exit_code != 0
    out = r.invoke(app, ["-w", str(ws), "names", "propose", epub, "--fake"])
    assert out.exit_code == 0, out.output


def test_address_renderings_proposed_per_speaker(conn, cfg, fixtures, low_thresholds):
    def addr(payload, req):
        return {"names": [], "address": [
            {"ref": a["ref"], "zh": "阿海", "reason": "同伴間親暱", "alternatives": [{"zh": "海斗", "reason": "直呼"}]}
            for a in payload["address"] if a["ja"] == "カイト"]}

    fake = FakeLLM(responders={**RESPONDERS, "naming": addr})
    book, _, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "awaiting_names"
    p = naming.pending(conn, book.id)
    assert [x["label"] for x in p] == ["米娜 → 海斗：カイト"]
    row = conn.execute("SELECT form_zh, zh_origin FROM address_terms WHERE form_ja='カイト'").fetchone()
    assert tuple(row) == ("阿海", "naming")
    naming.decide(conn, book.id, p[0]["id"], "海斗")
    row = conn.execute("SELECT form_zh, zh_origin FROM address_terms WHERE form_ja='カイト'").fetchone()
    assert tuple(row) == ("海斗", "human")
