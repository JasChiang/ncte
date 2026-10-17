"""M3 acceptance: scenes, characters, glossary, events, timeline, visibility (no spoilers), FTS."""

import json

from ncte.analysis.scene import segment_book
from ncte.llm.fake import FakeLLM
from ncte.memory.retrieval import search
from ncte.translation.cache import latest_texts

from ..helpers import run_book
from ..scripted import RESPONDERS


def context_run(conn, cfg, fixtures, fake=None, **kw):
    fake = fake or FakeLLM(responders=RESPONDERS)
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context", **kw)
    return fake, book, rid, s


def scene_of(conn, book, text):
    return conn.execute("SELECT scene_id FROM units WHERE book_id=? AND plain_text LIKE ?", (book.id, f"%{text}%")).fetchone()[0]


def test_scene_segmentation(conn, cfg, fixtures):
    from ncte.library import import_book

    book, _ = import_book(conn, cfg, fixtures["basic"])
    scenes = conn.execute("SELECT id, break_reason FROM scenes WHERE book_id=? ORDER BY seq", (book.id,)).fetchall()
    # each TOC chapter starts a scene; inside one not cut by the model yet, the author's separator cuts for now
    assert [tuple(r) for r in scenes] == [("s_000_01", "chapter"), ("s_000_02", "separator"),
                                          ("s_001_01", "chapter"), ("s_002_01", "chapter")]
    # manual override: split before 「待って」
    uid = conn.execute("SELECT id FROM units WHERE book_id=? AND plain_text='「待って」'", (book.id,)).fetchone()[0]
    conn.execute("INSERT INTO scene_overrides VALUES (?,?,?,?)", (book.id, uid, "split", "t"))
    segment_book(conn, cfg, book.id)
    assert conn.execute("SELECT break_reason FROM scenes WHERE book_id=? AND start_unit_id=?", (book.id, uid)).fetchone()[0] == "manual"


def test_context_run_builds_validated_memory(conn, cfg, fixtures):
    fake, book, rid, s = context_run(conn, cfg, fixtures)
    assert s.status == "completed", s
    chars = {r["canonical_name"]: r for r in conn.execute("SELECT * FROM characters WHERE book_id=?", (book.id,))}
    assert set(chars) == {"ミナ", "カイト", "リリア"}
    # リリア was hallucinated in chapter 1 (rejected) and only created when actually named in chapter 3
    ch3_first = conn.execute("SELECT MIN(ordinal) FROM units WHERE book_id=? AND scene_id='s_002_01'", (book.id,)).fetchone()[0]
    assert chars["リリア"]["known_from_ordinal"] >= ch3_first
    rej = [json.loads(r["item_json"]) for r in conn.execute(
        "SELECT * FROM memory_rejections WHERE book_id=? AND reason LIKE 'name_ja not found%'", (book.id,))]
    assert any(x["name_ja"] == "リリア" for x in rej)
    assert json.loads(chars["カイト"]["aliases_json"]) == []  # alias カイ only occurs inside カイト → rejected
    gl = {r["source_term"]: r for r in conn.execute("SELECT * FROM glossary WHERE book_id=?", (book.id,))}
    assert gl["ミナ"]["target_term"] == "米娜" and not gl["ミナ"]["verified"]
    assert gl["銀の鍵"]["target_term"] == "銀之鑰"
    # facts: verbatim evidence kept, fabricated evidence rejected, inference not upgraded
    facts = conn.execute("SELECT * FROM facts WHERE book_id=?", (book.id,)).fetchall()
    assert {(f["predicate"], f["certainty"]) for f in facts} == {("把銀之鑰交給", "explicit"), ("似乎", "inferred"),
                                                                 ("真實身分是", "explicit")}
    assert any("not found verbatim" in r["reason"] for r in conn.execute(
        "SELECT reason FROM memory_rejections WHERE stage='memory_update'"))
    hand = next(f for f in facts if f["predicate"] == "把銀之鑰交給")
    first = conn.execute("SELECT plain_text FROM units WHERE id=?", (hand["first_revealed_unit_id"],)).fetchone()[0]
    assert "手渡した" in first
    # summaries per scene with horizon
    assert conn.execute("SELECT COUNT(*) FROM chapter_summaries WHERE book_id=?", (book.id,)).fetchone()[0] == 4


def test_memory_reaches_later_translation_requests(conn, cfg, fixtures):
    fake, book, rid, s = context_run(conn, cfg, fixtures)
    ch2_payloads = [p for p, r in zip(fake.payloads, fake.calls, strict=True)
                    if r.stage == "translate" and any("丘" in u["source"] for u in p["units"])]
    assert ch2_payloads
    p = ch2_payloads[0]
    names = {c["name_ja"]: c for c in p["characters"]}
    assert names["ミナ"]["name_zh"] == "米娜"
    assert any("遅いですよ" in e["ja"] for e in names["ミナ"]["verified_examples"])
    facts = json.dumps(p["memory"]["facts"], ensure_ascii=False)
    assert "把銀之鑰交給" in facts
    assert "米娜在市場等海斗" in json.dumps(p["memory"], ensure_ascii=False)
    assert {g["ja"] for g in p["glossary"]} >= {"ミナ", "カイト", "銀の鍵"}


def test_no_future_knowledge_in_earlier_scenes(conn, cfg, fixtures):
    fake, book, rid, s = context_run(conn, cfg, fixtures)
    # after the whole book (incl. the chapter-3 reveal) is in memory, retranslate a chapter-1 unit
    target = conn.execute("SELECT id FROM units WHERE book_id=? AND plain_text LIKE '%本当だろうな%'", (book.id,)).fetchone()[0]
    fake2 = FakeLLM(responders=RESPONDERS)
    _, rid2, s2 = run_book(conn, cfg, fixtures["basic"], fake2, mode="context", unit_ids=[target], force=True)
    assert s2.status == "completed"
    stages = [c.stage for c in fake2.calls]
    assert stages == ["translate"]  # scene already analysed, memory already present
    blob = json.dumps(fake2.payloads[0], ensure_ascii=False)
    for spoiler in ("リリア", "莉莉亞", "王女", "真實身分", "兜帽少女"):
        assert spoiler not in blob, spoiler
    assert fake2.payloads[0]["memory"]["facts"] == []  # nothing revealed before chapter 1, scene 1
    # FTS search respects the horizon as well
    horizon = conn.execute("SELECT start_ordinal FROM scenes WHERE book_id=? AND id='s_000_02'", (book.id,)).fetchone()[0]
    assert search(conn, book.id, "王女リリア", horizon=horizon) == []
    hits = search(conn, book.id, "王女リリア")
    assert hits and {h["kind"] for h in hits} >= {"unit", "fact"}
    assert search(conn, book.id, "ミナ", horizon=horizon)[0]["kind"] == "unit"  # 2-char LIKE fallback


def test_memory_failure_keeps_translations(conn, cfg, fixtures):
    fake = FakeLLM(responders=RESPONDERS, faults={"memory_update": ["bad_json"] * 20})
    _, book, rid, s = context_run(conn, cfg, fixtures, fake=fake)
    assert s.counts.get("memory_update:failed") == 4
    translatable = conn.execute("SELECT COUNT(*) FROM units WHERE book_id=? AND translatable=1", (book.id,)).fetchone()[0]
    assert len(latest_texts(conn, book.id)) == translatable
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_analysis_failure_falls_back_to_program(conn, cfg, fixtures):
    fake = FakeLLM(responders=RESPONDERS, faults={"scene_analyze": ["bad_json"] * 20})
    _, book, rid, s = context_run(conn, cfg, fixtures, fake=fake)
    states = [json.loads(r[0])["analysis"] for r in conn.execute("SELECT state_json FROM scenes WHERE book_id=?", (book.id,))]
    assert states == ["program_only"] * 4
    assert conn.execute("SELECT COUNT(*) FROM utterances WHERE book_id=?", (book.id,)).fetchone()[0] > 0


def test_context_resume_after_interrupt(conn, cfg, fixtures):
    from ncte.llm.fake import default_translate

    from ..helpers import orchestrator_for, resume_run, start_run

    n = {"i": 0}

    def flaky(payload, req):
        n["i"] += 1
        if n["i"] == 2:
            raise KeyboardInterrupt
        return default_translate(payload, req)

    fake = FakeLLM(responders={**RESPONDERS, "translate": flaky})
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], fake, mode="context")
    s1 = orchestrator_for(conn, cfg, book, rid, opts, fake).run()
    assert s1.status == "interrupted"
    s2 = resume_run(conn, cfg, rid, FakeLLM(responders=RESPONDERS))
    assert s2.status == "completed"
    assert s2.counts.get("memory_update:succeeded") == 4


def test_rerunning_memory_update_does_not_duplicate(conn, cfg, fixtures):
    _, book, _, _ = context_run(conn, cfg, fixtures)
    n_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    n_fts = conn.execute("SELECT COUNT(*) FROM memory_fts WHERE kind IN ('fact','summary')").fetchone()[0]
    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context", force=True)
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == n_facts
    assert conn.execute("SELECT COUNT(*) FROM memory_fts WHERE kind IN ('fact','summary')").fetchone()[0] == n_fts


def test_facts_are_chosen_by_relevance_and_carry_their_source():
    from ncte.memory.retrieval import select_facts

    rows = [{"subject": "c_000000000001", "predicate": "把銀之鑰交給", "object": "ミナ", "fact_type": "objective",
             "certainty": "explicit", "source_viewpoint": None, "evidence_quote": "カイトは銀の鍵をミナに手渡した"},
            {"subject": "c_000000000001", "predicate": "喜歡", "object": "釣魚", "fact_type": "objective",
             "certainty": "explicit", "source_viewpoint": None, "evidence_quote": "カイトは釣りが趣味だ"}]
    present = [{"character_id": "c_000000000001", "name_ja": "カイト", "aliases_ja": []}]
    got = select_facts(rows, "ミナは銀の鍵を握りしめた", present, {"c_000000000001": "海斗"})
    assert [f["predicate"] for f in got] == ["把銀之鑰交給"]  # the fishing fact has nothing to do with this batch
    assert got[0]["source"] == "カイトは銀の鍵をミナに手渡した" and got[0]["subject"] == "海斗"


def test_a_fact_about_the_world_is_given_where_the_text_takes_it_up():
    """「アルテインは歴史書から消えていた」 is about no one present; a later batch about the vanished kingdom needs it."""
    from ncte.memory.retrieval import select_facts

    rows = [{"subject": "アルテイン", "predicate": "從史書中", "object": "消失", "fact_type": "objective",
             "certainty": "explicit", "source_viewpoint": None, "evidence_quote": "アルテイン王国の名は歴史書から消えていた"},
            {"subject": "王都", "predicate": "有", "object": "市場", "fact_type": "objective",
             "certainty": "explicit", "source_viewpoint": None, "evidence_quote": "王都には大きな市場がある"}]
    present = [{"character_id": "c_000000000001", "name_ja": "カイト", "aliases_ja": []}]
    got = select_facts(rows, "なぜアルテイン王国の名が歴史書に残っていないのか", present, {})
    assert [f["subject"] for f in got] == ["アルテイン"]
