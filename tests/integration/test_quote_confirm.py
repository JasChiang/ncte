"""Quotes by meaning: a pair the translation adds or drops relative to the source goes to the rule check, where the
model decides with the text around whether it marks what the source marks (a name given with という may be put in
「」 in Chinese) or not (quotes around narration or an unquoted thought). Found fine, the text stays; found wrong, it
goes on to review and the fix."""

from ncte.llm.fake import FakeLLM, default_translate
from ncte.llm.prompt_loader import load_prompt

from ..helpers import run_book
from ..scripted import RESPONDERS

NARRATION = "朝の市場は"  # a paragraph of narration without quotes in the source


def _translate(fixes):
    def answer(payload, req):
        out = default_translate(payload, req)
        if "review_feedback" in payload:
            fixes.extend(payload["review_feedback"])
            return out
        for t, u in zip(out["translations"], payload["units"], strict=True):
            if NARRATION in u["source"]:
                t["translated_text"] = f"「{t['translated_text']}」"  # quotes the source does not have
        return out
    return answer


def _confirm(real, seen):
    def answer(payload, req):
        seen.extend(payload["items"])
        return {"checks": [{"unit_id": i["unit_id"], "rule": i["rule"], "real": real, "reason": "看前後文"}
                           for i in payload["items"]]}
    return answer


def _unit(conn, book_id):
    return conn.execute("SELECT id FROM units WHERE book_id=? AND plain_text LIKE ?", (book_id, f"%{NARRATION}%")
                        ).fetchone()[0]


def test_the_prompts_say_quotes_follow_the_source_by_meaning():
    from ncte.qa.rules import CONFIRM_RULES

    assert "quote_mismatch" in CONFIRM_RULES
    rule = load_prompt("confirm").text.split("- `quote_mismatch`：", 1)[1].split("\n", 1)[0]
    assert "という" in rule and "不是問題" in rule and "內心話" in rule
    shared = load_prompt("translate").text
    assert "中文可以加「」標出，這不是錯" in shared and "沒有加引號的內心話，不要加上引號" in shared


def test_an_added_pair_found_fine_keeps_the_text(conn, cfg, fixtures):
    fixes, seen = [], []
    client = FakeLLM(responders={**RESPONDERS, "translate": _translate(fixes), "confirm": _confirm(False, seen)})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    uid = _unit(conn, book.id)
    item = next(i for i in seen if i["unit_id"] == uid)
    assert item["rule"] == "quote_mismatch" and "added" in item["evidence"] and item["around"]
    f = conn.execute("SELECT status FROM qa_findings WHERE run_id=? AND unit_id=? AND type='quote_mismatch'",
                     (rid, uid)).fetchone()
    assert f["status"] == "dismissed_by_check"
    assert not [x for x in fixes if x["unit_id"] == uid]
    t = conn.execute("SELECT text, status FROM translations WHERE unit_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    assert "「" in t["text"] and t["status"] == "succeeded"


def test_an_added_pair_found_wrong_goes_to_review_and_the_fix(conn, cfg, fixtures):
    fixes, seen = [], []
    client = FakeLLM(responders={**RESPONDERS, "translate": _translate(fixes), "confirm": _confirm(True, seen)})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    uid = _unit(conn, book.id)
    assert any(i["unit_id"] == uid and i["rule"] == "quote_mismatch" for i in seen)
    sent = [i for x in fixes if x["unit_id"] == uid for i in x["issues"]]
    assert any(i["issue_type"] == "quote_mismatch" and "看前後文" in i["evidence"] for i in sent)
    t = conn.execute("SELECT text FROM translations WHERE unit_id=? AND status='succeeded' ORDER BY id DESC LIMIT 1",
                     (uid,)).fetchone()
    assert "「" not in t["text"]  # the fix is the book's text
