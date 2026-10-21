"""Characters still without a gender after reading: the program gathers sentences where they speak, are named or are
spoken to, spread over the book; the model says the gender citing them; an answer citing nothing it was given for that
person is not kept. Reading itself counts a description that fixes a gender (a gendered role, the uniform, a
single-gender group) as evidence, inferred, never a stereotype."""

import pytest

from ncte.analysis import gender_gaps
from ncte.llm.fake import FakeLLM
from ncte.llm.prompt_loader import load_prompt

from ..helpers import run_book
from ..scripted import RESPONDERS


def _asked(client):
    return [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "verify:genders"]


def _answer(payload, req):
    out = []
    for c in payload["characters"]:
        if c["name"] == "ミナ":
            out.append({"ref": c["ref"], "gender": "female", "certainty": "inferred",
                        "evidence": [c["sentences"][0]["ref"]], "reason": "描述"})
        elif c["name"] == "カイト":  # cites a sentence it was not given
            out.append({"ref": c["ref"], "gender": "male", "certainty": "explicit", "evidence": ["s999"], "reason": "x"})
        else:
            out.append({"ref": c["ref"], "gender": "unknown", "certainty": "inferred", "evidence": [], "reason": "無"})
    return {"characters": out}


@pytest.fixture
def low(monkeypatch):
    monkeypatch.setattr(gender_gaps, "MIN_LINES", 1)
    monkeypatch.setattr(gender_gaps, "MIN_MENTIONS", 1)


def test_reading_counts_a_description_that_fixes_a_gender_but_not_a_stereotype():
    for task in ("memory_update", "scene_read"):
        text = load_prompt(task).text
        rule = text.split("`genders`：", 1)[1].split("\n\n", 1)[0]
        placed = rule.split("敘述定下性別的描述", 1)[1].split("\n", 1)[0]  # the narration placing them is stated
        assert "制服" in placed and "只限一種性別" in placed and "`certainty: \"explicit\"`" in placed, task
        assert "自稱或口吻" in rule and "`certainty: \"inferred\"`" in rule and "單獨都不算依據" in rule, task
    ask = load_prompt("gender_gaps").text
    assert "`evidence`" in ask and "`unknown`" in ask and "刻板印象" in ask and "制服" in ask
    assert "敘述本身定下了這個人的性別，也是原文寫明的" in ask


def test_a_gap_answer_citing_its_sentences_fills_the_gender_and_an_uncited_one_is_rejected(conn, cfg, fixtures, low):
    client = FakeLLM(responders={**RESPONDERS, "verify:genders": _answer})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    asked = _asked(client)
    assert len(asked) == 1
    chars = {c["name"]: c for c in asked[0]["characters"]}
    assert {"ミナ", "カイト"} <= set(chars)
    mina = chars["ミナ"]
    assert mina["lines"] >= 1 and 1 <= len(mina["sentences"]) <= gender_gaps.SENTENCES
    assert all(s["how"] and s["text"] for s in mina["sentences"])
    assert any("speaks" in s["how"] for s in mina["sentences"]) and any("named" in s["how"] for s in mina["sentences"])
    refs = [s["ref"] for c in asked[0]["characters"] for s in c["sentences"]]
    assert len(refs) == len(set(refs))  # every code names one sentence
    rows = {r["canonical_name"]: r for r in conn.execute(
        "SELECT c.canonical_name, g.* FROM character_gender g JOIN characters c ON c.id=g.character_id WHERE c.book_id=?",
        (book.id,))}
    assert rows["ミナ"]["gender"] == "female" and rows["ミナ"]["origin"] == "llm" and rows["ミナ"]["certainty"] == "inferred"
    assert rows["ミナ"]["evidence_quote"] == mina["sentences"][0]["text"][:200]
    assert "カイト" not in rows  # its citation was not among what it was given
    job = conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage='gender_gaps'", (rid,)).fetchone()
    assert job["status"] == "succeeded" and '"genders": 1' in job["checkpoint_json"] and '"rejected": 1' in \
        job["checkpoint_json"]
    order = [c.stage for c in client.calls]
    assert max(i for i, s in enumerate(order) if s == "verify") < order.index("verify:genders") < order.index("naming")
    # the gender reaches the translation (from the earliest sentence cited on), marked as inferred
    tr = [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "translate"]
    assert any(str(ch.get("gender", "")).startswith("female") for p in tr for ch in p.get("characters", [])
               if ch.get("name_ja") == "ミナ")


def test_characters_below_the_threshold_are_not_asked(conn, cfg, fixtures, monkeypatch):
    monkeypatch.setattr(gender_gaps, "MIN_LINES", 1000)
    monkeypatch.setattr(gender_gaps, "MIN_MENTIONS", 1000)
    client = FakeLLM(responders={**RESPONDERS, "verify:genders": _answer})
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert not _asked(client)


def test_switched_off_nothing_is_asked(conn, cfg, fixtures, low):
    cfg.context.gender_gaps = False
    client = FakeLLM(responders={**RESPONDERS, "verify:genders": _answer})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert not _asked(client)
    assert not conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND stage='gender_gaps'", (rid,)).fetchone()
    assert not conn.execute("SELECT 1 FROM character_gender").fetchone()


def _empty_then(n):
    """A CLI source whose turn ends with no answer ``n`` times in a row, then answers."""
    from ncte import errors

    empty = getattr(errors, "EmptyAnswerError", errors.ModelOutputError)
    left = [n]

    def responder(payload, req):
        if left[0] > 0:
            left[0] -= 1
            raise empty("empty answer")
        return _answer(payload, req)

    return responder


def test_empty_answers_in_a_row_are_asked_again(conn, cfg, fixtures, low):
    """2026-10-21: both tries of the step came back empty and every character was left unasked. An empty answer has
    nothing to correct: the same request is asked again a few times."""
    client = FakeLLM(responders={**RESPONDERS, "verify:genders": _empty_then(3)})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert len(_asked(client)) == 4
    assert conn.execute("SELECT gender FROM character_gender g JOIN characters c ON c.id=g.character_id WHERE "
                        "c.canonical_name='ミナ'").fetchone()[0] == "female"
    job = conn.execute("SELECT status FROM jobs WHERE run_id=? AND stage='gender_gaps'", (rid,)).fetchone()
    assert job["status"] == "succeeded"


def test_a_step_that_never_answers_is_partial_and_said_in_the_report(conn, cfg, fixtures, low):
    client = FakeLLM(responders={**RESPONDERS, "verify:genders": _empty_then(99)})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    job = conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage='gender_gaps'", (rid,)).fetchone()
    assert job["status"] == "partial" and '"failed": 0' not in job["checkpoint_json"]
    f = conn.execute("SELECT severity, evidence FROM qa_findings WHERE book_id=? AND type='gender_gaps_failed'",
                     (book.id,)).fetchone()
    assert f["severity"] == "medium" and "ミナ" in f["evidence"]  # who was left unasked, by name


def test_a_gender_shown_by_the_narration_keeps_its_kind_and_the_cards_say_so(conn, cfg, fixtures):
    """User decision 2026-10-21: a gender the narration shows (a uniform skirt, the girls' dorm) still counts as stated
    (ADR-081), but what kind of evidence it is, is kept, and translation and proofreading are told — a later reveal
    (dressed as the other sex) can overturn that kind. The kind is the model's reading; the program only keeps it."""
    from ..scripted import known, memory_update, uid

    def memory(payload, req):
        out = memory_update(payload, req)
        u, mina = uid(payload, "カイト、遅いですよ"), known(payload, "ミナ")
        if u and mina:
            out["genders"] = [{"character_ref": mina, "gender": "female", "certainty": "explicit", "basis": "described",
                               "evidence_unit_ids": [u], "evidence_quote": "カイト、遅いですよ"}]
        return out

    cfg.context.proofread_passes = 1
    client = FakeLLM(responders={**RESPONDERS, "memory_update": memory})
    book, _, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    row = conn.execute("SELECT g.* FROM character_gender g JOIN characters c ON c.id=g.character_id WHERE "
                       "c.canonical_name='ミナ'").fetchone()
    assert (row["gender"], row["certainty"], row["basis"]) == ("female", "explicit", "described")
    cards = [ch for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "translate"
             for ch in p.get("characters", []) if ch.get("name_ja") == "ミナ"]
    assert cards and all(ch["gender"] == "female" and ch["gender_basis"] == "described" for ch in cards)
    checked = [ch for c, p in zip(client.calls, client.payloads, strict=True) if c.stage.startswith("proofread")
               for ch in p.get("characters", []) if ch.get("ja") == "ミナ"]
    assert checked and all(ch.get("gender_basis") == "described" for ch in checked)
    for task in ("memory_update", "scene_read", "gender_gaps"):
        assert "`described`" in load_prompt(task).text and "`belief`" in load_prompt(task).text, task
