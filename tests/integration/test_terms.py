"""What the book's own terms mean (ADR-069): after reading, the model defines the recurring terms of the glossary from
example sentences and the facts that mention them; the program only gathers those by string match, checks that what
the answer cites exists, and shows a definition from the latest evidence it cites on, to every step that judges a
translation where the term occurs."""

from ncte.llm.fake import FakeLLM

from ..helpers import orchestrator_for, run_book, start_run
from ..scripted import RESPONDERS

TERM = "銀の鍵"
DEF = "米娜與海斗手上那把銀色鑰匙；後來才知道是王家寶庫的鑰匙。"
LATE = "鞄の中"  # the chapter-two sentence the definition is made to rest on


def _stage(client, stage):
    return [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage.split(":")[0] == stage]


def _terms(evidence=None, named=None):
    def answer(payload, req):
        if payload.get("task") == "term_candidates":  # ADR-073: the terms missing from the glossary
            return {"candidates": named(payload) if named else []}
        out = []
        for t in payload["terms"]:
            if t["ja"] != TERM:
                continue
            ev = evidence or [next(x["ref"] for x in t["examples"] if LATE in x["text"])]
            out.append({"ref": t["ref"], "definition": DEF, "evidence": ev, "revealed_from": ev[0],
                        "contrast_with": ""})
        return {"terms": out}
    return answer


def _proofread(payload, req):
    """Finds an error in the chapter-two paragraph about the key, so it is retranslated and the fix checked."""
    p = next((x for x in payload["paragraphs"] if LATE in x["ja"]), None)
    if p is None:
        return {"issues": []}
    return {"issues": [{"unit_id": p["unit_id"], "kind": "meaning", "severity": "high", "quote": p["zh"][:4],
                        "problem": "鑰匙在誰手上譯錯了", "fix": "改成在米娜的包包裡"}]}


def _defs(payload):
    return payload.get("term_definitions")


def _defining(client):
    return [p for p in _stage(client, "terms") if p.get("task") == "terms"]


def test_the_terms_request_carries_examples_and_facts_found_by_the_word(conn, cfg, fixtures):
    client = FakeLLM(responders={**RESPONDERS, "terms": _terms()})
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    asked = _defining(client)
    assert len(asked) == 1
    item = next(t for t in asked[0]["terms"] if t["ja"] == TERM)
    assert item["zh"] == "銀之鑰" and item["count"] >= 2
    assert 2 <= len(item["examples"]) <= 3 and all(TERM in x["text"] and "at" in x for x in item["examples"])
    assert item["facts"] and all(TERM in (f["text"] + f["source"]) for f in item["facts"])
    assert len({x["ref"] for t in asked[0]["terms"] for x in t["examples"] + t["facts"]}) == sum(
        len(t["examples"]) + len(t["facts"]) for t in asked[0]["terms"])  # every code names one piece of evidence
    # the step comes after the house style and before naming
    order = [c.stage.split(":")[0] for c in client.calls]
    assert order.index("house_style") < order.index("terms") < order.index("naming")


def test_a_definition_reaches_the_steps_that_judge_from_where_its_evidence_is(conn, cfg, fixtures):
    client = FakeLLM(responders={**RESPONDERS, "terms": _terms(), "proofread": _proofread})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    row = conn.execute("SELECT * FROM term_definitions WHERE book_id=? AND source_term=?", (book.id, TERM)).fetchone()
    late = conn.execute("SELECT ordinal FROM units WHERE plain_text LIKE ?", (f"%{LATE}%",)).fetchone()[0]
    assert row["definition"] == DEF and row["origin"] == "model" and row["revealed_ordinal"] == late
    want = [{"ja": TERM, "zh": "銀之鑰", "definition": DEF}]
    tr = _stage(client, "translate")
    after = [p for p in tr if any(LATE in u["source"] for u in p["units"])]
    before = [p for p in tr if any("手渡した" in u["source"] for u in p["units"])]
    assert after and all(_defs(p) == want for p in after)  # incl. the retranslation of the proofreading fix
    assert before and all(_defs(p) is None for p in before)  # the key is in it, the definition not yet shown
    pr = _stage(client, "proofread")
    assert any(_defs(p) == want for p in pr if any(LATE in x["ja"] for x in p["paragraphs"]))
    assert all(_defs(p) is None for p in pr if not any(LATE in x["ja"] for x in p["paragraphs"]))
    fc = _stage(client, "fix_check")
    assert fc and all(_defs(p) == want for p in fc)


def test_an_answer_citing_evidence_that_was_not_given_is_rejected(conn, cfg, fixtures):
    client = FakeLLM(responders={**RESPONDERS, "terms": _terms(evidence=["s999"])})
    book, _, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert conn.execute("SELECT COUNT(*) FROM term_definitions WHERE book_id=?", (book.id,)).fetchone()[0] == 0
    assert all(_defs(p) is None for p in _stage(client, "translate"))
    job = conn.execute("SELECT checkpoint_json FROM jobs WHERE stage='terms'").fetchone()
    assert '"rejected": 1' in job["checkpoint_json"]


def test_a_definition_a_person_wrote_is_kept(conn, cfg, fixtures):
    from ncte.memory.db import Transaction
    from ncte.memory.terms import set_definition

    client = FakeLLM(responders={**RESPONDERS, "terms": _terms()})
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], client, mode="context")
    with Transaction(conn):
        set_definition(conn, book.id, TERM, "人工寫的定義")
    orchestrator_for(conn, cfg, book, rid, opts, client).run()
    row = conn.execute("SELECT * FROM term_definitions WHERE book_id=? AND source_term=?", (book.id, TERM)).fetchone()
    assert row["definition"] == "人工寫的定義" and row["origin"] == "human"
    assert all(t["ja"] != TERM for p in _defining(client) for t in p["terms"])  # not asked again
    hit = [p for p in _stage(client, "translate") if any(TERM in u["source"] for u in p["units"])
           and "known_headings" not in p]  # chapter titles and the TOC are translated without the story's memory
    assert hit and all(_defs(p) == [{"ja": TERM, "zh": "銀之鑰", "definition": "人工寫的定義"}] for p in hit)


def test_switched_off_nothing_is_asked_or_sent(conn, cfg, fixtures):
    cfg.context.term_definitions = False
    client = FakeLLM(responders={**RESPONDERS, "terms": _terms()})
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert not _stage(client, "terms")
    assert all(_defs(p) is None for p in _stage(client, "translate"))
    assert all("definition" not in n for p in _stage(client, "naming") for n in p["names"])


def test_the_content_filter_halves_the_terms_request(conn, cfg, fixtures):
    """A refused request is asked in halves (``call_in_parts``), down to one term; the term refused alone is noted."""
    from ncte.errors import ContentBlockedError
    from ncte.memory.db import Transaction
    from ncte.memory.repository import set_glossary

    answer = _terms()

    def picky(payload, req):
        if any(t["ja"] == "フード" for t in payload.get("terms", [])):
            raise ContentBlockedError("HTTP 403 content filter: fake")
        return answer(payload, req)

    client = FakeLLM(responders={**RESPONDERS, "terms": picky})
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], client, mode="context")
    with Transaction(conn):
        set_glossary(conn, book.id, "フード", "兜帽", category="item", verified=False)
    orchestrator_for(conn, cfg, book, rid, opts, client).run()
    assert conn.execute("SELECT definition FROM term_definitions WHERE source_term=?", (TERM,)).fetchone()[0] == DEF
    assert conn.execute("SELECT 1 FROM qa_findings WHERE type='terms_blocked'").fetchone()
    assert len(_stage(client, "terms")) >= 3  # the whole, then its halves


def test_on_the_claude_plan_terms_are_defined_by_opus():
    from ncte.config import CLAUDE_READ_STAGES, Config, stage_model, use_claude_models

    cfg = Config()
    cfg.openrouter.provider = "claude"
    use_claude_models(cfg)
    assert "terms" not in CLAUDE_READ_STAGES
    assert stage_model(cfg, "terms", cfg.openrouter.model) == "claude/opus"


def test_the_estimate_progress_and_names_page_know_the_step(conn, cfg, fixtures):
    from ncte.gui import data
    from ncte.gui.runner import STAGES
    from ncte.translation.dryrun import dry_run
    from ncte.translation.orchestrator import RunOptions

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "terms": _terms()}),
                          mode="context")
    plan = dry_run(conn, cfg, book, RunOptions(mode="context", model=cfg.openrouter.model, force=True), FakeLLM(),
                   log=lambda m: None)
    assert plan.stage_estimates["terms"]["from"] == "sample share"
    assert plan.stage_estimates["terms"]["usd"] < plan.stage_estimates["relations"]["usd"]
    keys = [k for k, _, _ in STAGES]
    assert keys.index("cast") < keys.index("terms") < keys.index("naming")
    shown = data.term_definitions(conn, book)
    assert shown == [{"ja": TERM, "zh": "銀之鑰", "definition": DEF, "contrast_with": "", "origin": "model",
                      "revealed_at": shown[0]["revealed_at"]}]
    cfg.context.term_definitions = False
    off = dry_run(conn, cfg, book, RunOptions(mode="context", model=cfg.openrouter.model, force=True), FakeLLM(),
                  log=lambda m: None)
    assert "terms" not in off.stage_estimates


def test_the_commands(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "names", "terms", epub, "--fake"])
    assert out.exit_code == 0 and "terms:" in out.output, out.output
    out = r.invoke(app, ["-w", str(ws), "glossary", "define", epub, TERM, "人工的說明"])
    assert out.exit_code == 0 and "人工的說明" in out.output and "[person]" in out.output, out.output
    out = r.invoke(app, ["-w", str(ws), "glossary", "define", epub, TERM, "--drop"])
    assert out.exit_code == 0 and "removed" in out.output


def test_the_prompts_say_what_the_definitions_are_for():
    from ncte.llm.prompt_loader import load_prompt

    line = ("`term_definitions`：讀完全書後整理的術語意思（`ja` 原文、`zh` 譯名、`definition` 說明），"
            "用來理解原文裡這個詞指的是什麼；不是要寫進譯文的內容。")
    for task in ("translate", "proofread", "fix_check", "review"):
        p = load_prompt(task)
        static, extra = p.render_parts({"term_definitions": [{"ja": "a", "zh": "b", "definition": "c"}]})
        assert line not in static and line in extra, task  # only with the data, after the shared (cached) part
        assert line not in p.render({"units": []}), task
    terms = load_prompt("terms").render()
    for rule in ("外部知識", "不要劇透", "寫出差別", "不要回答"):
        assert rule in terms
    assert "「" not in terms  # principles only: no example taken from a book


def test_naming_gets_the_definition_of_a_term_it_is_asked_to_render(conn, cfg, fixtures):
    """Naming reads the whole book, so a definition is shown to it wherever its evidence is; only for the names that
    are not people's, and only a definition the model or a person wrote."""
    client = FakeLLM(responders={**RESPONDERS, "terms": _terms()})
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    names = [n for p in _stage(client, "naming") for n in p["names"]]
    item = next(n for n in names if n["ja"] == TERM)
    assert item["definition"] == DEF
    assert all("definition" not in n for n in names if n["category"] == "character")


def test_naming_leaves_out_a_definition_of_another_origin_and_of_a_person_name(conn, cfg, fixtures):
    from ncte.analysis.naming import build_naming_payload
    from ncte.memory.db import Transaction
    from ncte.memory.terms import set_definition

    client = FakeLLM(responders={**RESPONDERS, "terms": _terms()})
    book, _, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    conn.execute("DELETE FROM naming_proposals")
    person = conn.execute("SELECT source_term FROM glossary WHERE book_id=? AND category='character' AND verified=0",
                          (book.id,)).fetchone()
    with Transaction(conn):
        if person is not None:
            set_definition(conn, book.id, person[0], "一個人的說明", origin="human")
        conn.execute("UPDATE term_definitions SET origin='imported' WHERE source_term=?", (TERM,))
    names = build_naming_payload(conn, book.id)["names"]
    assert all("definition" not in n for n in names)
    with Transaction(conn):
        conn.execute("UPDATE term_definitions SET origin='human' WHERE source_term=?", (TERM,))
    names = build_naming_payload(conn, book.id)["names"]
    assert next(n for n in names if n["ja"] == TERM)["definition"] == DEF
    assert all("definition" not in n for n in build_naming_payload(conn, book.id, definitions=False)["names"])


def test_the_naming_prompt_says_what_a_definition_is_for():
    from ncte.llm.prompt_loader import load_prompt

    line = "`names[].definition`（可能不存在）：讀完全書後整理的這個術語的意思，用來決定譯名；不是要寫進譯名的內容。"
    p = load_prompt("naming")
    static, extra = p.render_parts({"names": [{"ja": "a", "definition": "c"}]})
    assert line not in static and line in extra
    assert line not in p.render({"names": [{"ja": "a"}]})


def test_terms_the_reading_left_out_of_the_glossary_are_named_by_the_model_and_defined(conn, cfg, fixtures):
    """ADR-073: the model sees the whole book's facts and chapter summaries and the known terms, and names the book's
    own terms missing from them. The program keeps a name that cites what it was given, is not known yet and occurs
    at least twice in the text; those are defined like the glossary's (no rendering yet)."""
    def named(payload):
        f = payload["facts"][0]["ref"]
        return [{"ja": "フード", "evidence": [f], "why": "x"},  # 3 times in the book, not in the glossary: kept
                {"ja": "宝物庫", "evidence": [f], "why": "x"},  # once: dropped
                {"ja": "銀の鍵", "evidence": [f], "why": "x"},  # already known: dropped
                {"ja": "鍵の色", "evidence": ["f999"], "why": "x"}]  # cites what it was not given: dropped
    client = FakeLLM(responders={**RESPONDERS, "terms": _terms(named=named)})
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    ask = [p for p in _stage(client, "terms") if p.get("task") == "term_candidates"]
    assert len(ask) == 1
    assert "銀の鍵" in ask[0]["known"] and "カイト" in ask[0]["known"]  # people's names count as known
    assert ask[0]["facts"] and all({"ref", "at", "text", "source"} <= set(f) for f in ask[0]["facts"])
    assert ask[0]["summaries"] and all(m["ref"].startswith("m") for m in ask[0]["summaries"])
    asked = [t for p in _defining(client) for t in p["terms"]]
    hood = next(t for t in asked if t["ja"] == "フード")
    assert hood["zh"] == "" and hood["count"] == 3 and hood["examples"]
    assert not any(t["ja"] in ("宝物庫", "鍵の色") for t in asked)
    assert sum(t["ja"] == "銀の鍵" for t in asked) == 1  # the glossary's own entry, not a second one
    job = conn.execute("SELECT checkpoint_json FROM jobs WHERE stage='terms'").fetchone()
    assert '"named": 1' in job["checkpoint_json"]


def test_with_term_candidates_off_only_the_glossary_is_asked(conn, cfg, fixtures):
    cfg.context.term_candidates = False
    client = FakeLLM(responders={**RESPONDERS, "terms": _terms()})
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert not [p for p in _stage(client, "terms") if p.get("task") == "term_candidates"]
    assert len(_defining(client)) == 1
