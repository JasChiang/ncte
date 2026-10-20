"""What the judging steps are handed (ADR-067, second batch): the data the reading and the translation already have,
sent to the step that judges, each as far as that step may see. The program only sends data, matches strings and
keeps positions; it judges nothing."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS, known, uid


def _stage(client, stage):
    return [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage.split(":")[0] == stage]


def test_review_gets_the_scenes_facts_and_summaries_and_its_questions(conn, cfg, fixtures):
    """Review reads the facts and summaries the translator had for the scene (chosen the same way, before the scene
    begins) and the read pass's questions about the reviewed paragraphs."""
    def analyze(payload, req):
        out = RESPONDERS["scene_analyze"](payload, req)
        if u := uid(payload, "カイトは目をそらした"):
            out["uncertainties"] = [{"unit_ids": [u], "question": "鑰匙為何在米娜的包包裡？"}]
        return out

    cfg.review.automatic_review = "all"
    client = FakeLLM(responders={**RESPONDERS, "scene_analyze": analyze})
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    reviews = _stage(client, "review")
    later = next(p for p in reviews if any("カイトは目をそらした" in i["source"] for i in p["items"]))
    first = next(p for p in _stage(client, "translate")
                 if any("カイトは目をそらした" in u["source"] for u in p["units"]) and "review_feedback" not in p)
    assert later["memory"] == first["memory"]  # the same memory, as visible at the scene's start
    assert later["memory"]["previous_chapter_summary"] or later["memory"]["this_chapter_so_far"]
    assert [q["question"] for q in later["scene"]["uncertainties"]] == ["鑰匙為何在米娜的包包裡？"]
    opening = next(p for p in reviews if any("朝の市場" in i["source"] for i in p["items"]))
    assert "memory" not in opening  # nothing was known before the book's first scene


REVIEW_MEMORY = ("- `memory`（可能不存在）：這一場開始以前已揭露的劇情記憶：`previous_chapter_summary`（上一章回顧）、"
                 "`this_chapter_so_far`（本章前面的摘要）、`facts`（挑出來和這一場相關的事實，`source` 是它的原文依據）、"
                 "`props`（物品與持有者）。標示 `inferred` 或 `unknown` 的**不是確定事實**。事實與摘要只用來理解人物與前情，"
                 "**不可改變本句原文的意思、對象或方向**（本句怎麼說，以本句原文為準）；譯文沒有寫出這些背景不是錯。")


def _line_only_with(task, line, key, value):
    from ncte.llm.prompt_loader import load_prompt

    p = load_prompt(task)
    static, extra = p.render_parts({key: value})
    assert line not in static and line in extra, task  # after the shared (cached) part, only with the data
    assert line not in p.render({"items": []}), task


def test_the_review_prompt_says_what_the_memory_is_for():
    _line_only_with("review", REVIEW_MEMORY, "memory", {"facts": []})


def _proofread_finds(*needles):
    def proofread(payload, req):
        return {"issues": [{"unit_id": p["unit_id"], "kind": "meaning", "severity": "high", "quote": p["zh"][:3],
                            "problem": "意思弄錯", "fix": "改"} for p in payload["paragraphs"]
                           if any(n in p["ja"] for n in needles)]}
    return proofread


def test_the_fix_check_gets_the_renderings_and_forms_of_address_proofreading_has(conn, cfg, fixtures):
    """Whether a fix keeps the decided names and the way these speakers call these people: the glossary entries of
    the paragraphs' source and the forms of address of their speakers, picked as for proofreading."""
    client = FakeLLM(responders={**RESPONDERS, "proofread": _proofread_finds("銀の鍵をミナに")})
    cfg.context.reader_pass = False
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    checks = _stage(client, "fix_check")
    assert checks
    assert {"ja": "銀の鍵", "zh": "銀之鑰"}.items() <= next(g for c in checks for g in c.get("glossary", [])
                                                         if g["ja"] == "銀の鍵").items()
    forms = [a for c in checks for a in c.get("address_terms", [])]
    assert any(a["ja"] == "カイト" and a["speaker"] == "米娜" for a in forms)  # first used before this paragraph


def test_the_fix_check_is_not_given_a_form_first_used_later(conn, cfg, fixtures):
    """A form of address first used at or after the fixed paragraph (and not decided) is not given yet."""
    client = FakeLLM(responders={**RESPONDERS, "proofread": _proofread_finds("カイト、遅いですよ")})
    cfg.context.reader_pass = False
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    checks = _stage(client, "fix_check")
    assert checks and not any(c.get("address_terms") for c in checks)


FIX_CHECK_NAMES = ("- `glossary`、`address_terms`（可能不存在）：這些段落原文裡已定的譯名（`ja` → `zh`），以及說話者對某人慣用的"
                   "稱呼與既有譯法（`usage: mention` 是談到、不是當面叫）。用來判斷 `after` 的名字與稱呼是否照既有譯法；"
                   "稱呼本身在原文改變時不算錯，第二人稱與敬稱的語氣隨場合、情緒調整也不算錯。")


def test_the_fix_check_prompt_says_what_the_renderings_are_for():
    _line_only_with("fix_check", FIX_CHECK_NAMES, "glossary", [{"ja": "a", "zh": "b"}])
    _line_only_with("fix_check", FIX_CHECK_NAMES, "address_terms", [{"ja": "a"}])


def _said_to(payload, req):
    """The read pass as scripted, with who each of the scene-one lines is said to."""
    out = RESPONDERS["scene_analyze"](payload, req)
    mina, kaito = known(payload, "ミナ") or "new_1", known(payload, "カイト") or "new_2"
    to = {"カイト、遅いですよ": kaito, "ありがとうございます": kaito, "本当だろうな": mina}
    for u in out.get("utterances", []):
        q = next((q["text"] for q in payload.get("quotes", []) if q["unit_id"] == u["unit_id"] and
                  q["quote_index"] == u["quote_index"]), "")
        for needle, who in to.items():
            if needle in q:
                u["to"], u["to_status"] = [who], "explicit"
    return out


def _partners(payload, req):
    """海斗 and 米娜 are partners, shown by the fact of the key handed over (so from that paragraph on)."""
    return {"relations": [{"a": p["a"], "b": p["b"], "relation": "搭檔", "a_is": "搭檔", "b_is": "搭檔",
                           "facts": [p["facts"][0]["ref"]], "reason": "一起行動", "kind": "feeling",
                           "certainty": "inferred"} for p in payload["pairs"] if p["facts"]]}


def test_proofreading_and_the_fix_check_know_how_the_two_speaking_are_related(conn, cfg, fixtures):
    """A line's speaker and listener, and only how those two are related as of that paragraph (named as the line's
    speaker is): for judging 你／您, honorifics and how heavy a form of address is."""
    proofs: list = []

    def proofread(payload, req):
        proofs.append(payload)
        return _proofread_finds("ありがとうございます")(payload, req)

    client = FakeLLM(responders={**RESPONDERS, "scene_analyze": _said_to, "relations": _partners,
                                 "proofread": proofread})
    cfg.context.reader_pass = False
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    paras = [p for s in proofs for p in s["paragraphs"]]
    thanks = next(p for p in paras if "ありがとうございます" in p["ja"])
    assert thanks["to"] == "海斗" and set(thanks["relation"]) == {"米娜是海斗的搭檔（推測）", "海斗是米娜的搭檔（推測）"}
    early = next(p for p in paras if "カイト、遅いですよ" in p["ja"])
    assert early.get("to") == "海斗" and "relation" not in early  # the relation is shown from its evidence on
    assert all("relation" not in p for p in paras if "to" not in p and "speaker" not in p)
    item = next(i for c in _stage(client, "fix_check") for i in c["items"] if "ありがとうございます" in i["source"])
    assert item["relation"] == thanks["relation"]


RELATION = ("`relation`（可能不存在）：這段台詞的說話者與對象兩人，在這個位置已成立的關係（只有這兩人）。判斷敬語、你／您、"
            "稱呼的輕重時參考；標（推測）的是從劇情推論的，不確定。")


def test_the_proofreading_and_fix_check_prompts_say_what_the_relation_is_for():
    _line_only_with("proofread", "- `paragraphs[]." + RELATION[1:], "relation", ["a"])
    _line_only_with("fix_check", "- `items[]." + RELATION[1:], "relation", ["a"])


def test_the_rule_check_gets_the_names_and_the_listener_its_rules_need(conn, cfg, fixtures):
    """An added name is judged with the people's renderings around it; a 你 said to a woman with who the line is said
    to and that person's gender as known there. Each rule gets only what it needs."""
    from ncte.memory.db import Transaction

    from ..helpers import orchestrator_for, start_run

    client = FakeLLM(responders={**RESPONDERS, "scene_analyze": _said_to})
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], client, mode="context")
    orch = orchestrator_for(conn, cfg, book, rid, opts, client)
    orch.run()
    unit = {n: conn.execute("SELECT id FROM units WHERE book_id=? AND plain_text LIKE ?", (book.id, f"%{n}%")).fetchone()[0]
            for n in ("ありがとうございます", "本当だろうな", "朝の市場")}
    mina = conn.execute("SELECT id FROM characters WHERE canonical_name='ミナ'").fetchone()[0]
    with Transaction(conn):
        conn.execute("INSERT OR REPLACE INTO character_gender(character_id, gender, book_id, ordinal, evidence_quote, "
                     "origin, updated_at, certainty) VALUES (?,?,?,?,?,?,?,?)",
                     (mina, "female", book.id, 0, "x", "llm", "2026-01-01T00:00:00Z", "inferred"))
        for name, rule in (("ありがとうございます", "possible_added_name"), ("本当だろうな", "second_person_gender"),
                           ("朝の市場", "offensive_term")):
            conn.execute("INSERT INTO qa_findings(book_id, unit_id, run_id, type, severity, evidence, status, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", (book.id, unit[name], rid, rule, "high", "x", "open",
                                                      "2026-01-01T00:00:00Z"))
    orch.confirm_flags(list(unit.values()))
    items = {i["rule"]: i for p in _stage(client, "confirm") for i in p["items"]}
    added = items["possible_added_name"]
    assert {g["ja"] for g in added["glossary"]} >= {"ミナ", "カイト"}
    assert all(g["category"] == "character" for g in added["glossary"])
    assert "to" not in added
    you = items["second_person_gender"]
    assert you["to"] == [{"name": "米娜", "gender": "female (inferred)"}] and "glossary" not in you
    assert "glossary" not in items["offensive_term"] and "to" not in items["offensive_term"]


def test_the_rule_check_prompt_says_what_each_rule_is_given():
    from ncte.llm.prompt_loader import load_prompt

    text = load_prompt("confirm").render({"items": []})
    assert "補錯人或提前透露了劇情才是問題；可參考附上的 `glossary`（這段與前後文出現的人名與譯名）。" in text
    assert ("可能是對男性、或對多人說）；可參考附上的 `to`（讀的時候判斷的說話對象與性別，標（推測）或 `(inferred)` 的"
            "不確定）。") in text
