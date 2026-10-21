"""Same-form words (false friends): after the first translation, the kanji words carried over unchanged (found by
string match, Japanese forms converted to Taiwan forms) go to the model; one that reads otherwise in Taiwan Chinese
gets an unverified glossary entry and its paragraphs go back to the translator with the suggestion, the fix checked
against the source like any other."""

import pytest

from ncte.analysis.false_friends import FIX_NOTE, carried, converter
from ncte.llm.fake import FakeLLM, default_translate
from ncte.llm.prompt_loader import load_prompt

from ..helpers import orchestrator_for, run_book, start_run
from ..scripted import RESPONDERS

WORD = "市場"  # in 「朝の市場は、もう人であふれていた。」, kept as 市場 by the stand-in translator


def _stage(client, stage):
    return [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == stage]


def _answer(payload, req):
    out = []
    for w in payload["words"]:
        ev = [w["examples"][0]["ref"]]
        if w["ja"] == WORD:
            out.append({"ref": w["ref"], "verdict": "differs", "suggestion": "菜市場", "reason": "臺灣口語說菜市場",
                        "evidence": ev})
        else:
            out.append({"ref": w["ref"], "verdict": "same", "reason": "同義", "evidence": ev})
    return {"words": out}


def _fixes(sent):
    def answer(payload, req):
        sent.extend(payload.get("review_feedback", []))
        return default_translate(payload, req)
    return answer


def test_words_carried_over_are_found_in_their_taiwan_form():
    pytest.importorskip("opencc")  # without it only the program's short list of Japanese glyphs is converted
    assert converter()("応接室") == "應接室" and converter()("学校") == "學校"
    assert carried("応接室で待っていた。", "在應接室等著。") == ["応接室"]
    assert carried("学校に行く。", "去學校。") == ["学校"]
    assert carried("二学期初日の朝", "第二學期第一天的早上") == ["二学期"]  # the longest stretch found in a run
    assert carried("応接室で待っていた。", "在會客室等著。") == []
    assert carried("ミナは笑った。", "米娜笑了。") == []
    rules = load_prompt("false_friends").text
    assert "臺灣中文" in rules and "`same`" in rules and "`differs`" in rules and "專有名稱不要改" in rules


def test_a_word_that_differs_gets_a_glossary_entry_and_its_paragraphs_a_fix(conn, cfg, fixtures):
    sent: list = []
    client = FakeLLM(responders={**RESPONDERS, "false_friends": _answer, "translate": _fixes(sent)})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    asked = _stage(client, "false_friends")
    assert asked
    words = {w["ja"]: w for p in asked for w in p["words"]}
    assert WORD in words and words[WORD]["zh"] == WORD and words[WORD]["examples"][0]["ja"].startswith("朝の市場")
    assert "学校" not in words  # not in the book
    g = conn.execute("SELECT * FROM glossary WHERE book_id=? AND source_term=?", (book.id, WORD)).fetchone()
    assert g["target_term"] == "菜市場" and g["verified"] == 0 and g["origin"] == "model" and g["category"] == "term"
    assert g["note"].startswith("同形異義")
    assert not conn.execute("SELECT 1 FROM glossary WHERE book_id=? AND origin='model' AND source_term!=?",
                            (book.id, WORD)).fetchone()  # the words said to be the same stay as they are
    fix = [i for x in sent for i in x["issues"] if i["issue_type"] == "false_friend"]
    assert fix and all("菜市場" in i["suggestion"] and FIX_NOTE in i["suggestion"] and WORD in i["evidence"] for i in fix)
    f = conn.execute("SELECT status FROM qa_findings WHERE run_id=? AND type='review:false_friend'", (rid,)).fetchall()
    assert f and all(r["status"] == "resolved" for r in f)  # the fix check accepted the changed text
    order = [c.stage for c in client.calls]
    assert order.index("false_friends") < order.index("proofread")
    assert max(i for i, s in enumerate(order) if s == "naming") < order.index("false_friends")
    fc = [i for p in _stage(client, "fix_check") for i in p["items"]]
    assert fc and not any(FIX_NOTE in str(i) for i in fc)  # the instruction to the translator is not a claim


def test_a_verified_glossary_word_is_not_asked(conn, cfg, fixtures):
    from ncte.memory.db import Transaction
    from ncte.memory.repository import set_glossary

    client = FakeLLM(responders={**RESPONDERS, "false_friends": _answer})
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], client, mode="context")
    with Transaction(conn):
        set_glossary(conn, book.id, WORD, "市集")
    orchestrator_for(conn, cfg, book, rid, opts, client).run()
    assert all(w["ja"] != WORD for p in _stage(client, "false_friends") for w in p["words"])
    assert conn.execute("SELECT target_term FROM glossary WHERE source_term=?", (WORD,)).fetchone()[0] == "市集"


def test_switched_off_nothing_is_asked(conn, cfg, fixtures):
    cfg.context.false_friends = False
    client = FakeLLM(responders={**RESPONDERS, "false_friends": _answer})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert not _stage(client, "false_friends")
    assert not conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND stage='false_friends'", (rid,)).fetchone()
    assert not conn.execute("SELECT 1 FROM glossary WHERE origin='model'").fetchone()
