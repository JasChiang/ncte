"""M2 acceptance: FakeLLM end-to-end, cost records, budget, resume, errors never destroy results."""

import json
import zipfile

import pytest

from ncte.exporter import export_book
from ncte.llm.fake import FakeLLM
from ncte.translation.cache import latest_texts

from ..helpers import orchestrator_for, resume_run, run_book, start_run


def n_requests(conn, rid):
    return conn.execute("SELECT COUNT(*) FROM llm_requests WHERE run_id=?", (rid,)).fetchone()[0]


@pytest.mark.parametrize("key", ["basic", "media", "ruby"])
def test_end_to_end_fake(conn, cfg, fixtures, key):
    fake = FakeLLM()
    book, rid, s = run_book(conn, cfg, fixtures[key], fake)
    assert s.status == "completed", s
    translatable = conn.execute("SELECT COUNT(*) FROM units WHERE book_id=? AND translatable=1", (book.id,)).fetchone()[0]
    assert len(latest_texts(conn, book.id)) == translatable
    out = export_book(conn, cfg, book, run_epubcheck=False)
    assert out.validation.ok, out.validation.errors
    assert out.stats["untranslated_kept_original"] == 0 and out.stats["rebuild_errors"] == 0
    # every request has a reported cost, usage and generation id
    rows = conn.execute("SELECT * FROM llm_requests WHERE run_id=?", (rid,)).fetchall()
    assert rows and all(r["cost_status"] == "reported" and r["generation_id"] for r in rows)
    assert all(json.loads(r["usage_json"])["prompt_tokens"] > 0 for r in rows)
    # traceability: each translation points to model, prompt version, request
    t = conn.execute("SELECT * FROM translations WHERE run_id=? LIMIT 1", (rid,)).fetchone()
    assert t["model_id"] and t["prompt_version"].startswith("translate/v1#") and t["source_hash"].startswith("sha256:")


def test_output_text_actually_replaced(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM())
    out = export_book(conn, cfg, book, run_epubcheck=False)
    ch1 = zipfile.ZipFile(out.result.output_path).read("OEBPS/Text/ch1.xhtml").decode()
    assert "遅いですよ" not in ch1 and "「カイト、" not in ch1


def test_second_run_uses_cache(conn, cfg, fixtures):
    fake = FakeLLM()
    run_book(conn, cfg, fixtures["basic"], fake)
    calls = len(fake.calls)
    _, rid2, s2 = run_book(conn, cfg, fixtures["basic"], fake)
    assert s2.status == "completed" and len(fake.calls) == calls
    assert s2.counts == {"skipped": s2.counts["skipped"]}


def test_interrupt_and_resume_no_duplicate_billing(conn, cfg, fixtures):
    cfg.openrouter.max_concurrency = 1
    cfg.translation.unit_target_japanese_chars = 120  # many small batches
    state = {"n": 0}

    def flaky(payload, req):
        state["n"] += 1
        if state["n"] == 3:
            raise KeyboardInterrupt
        from ncte.llm.fake import default_translate

        return default_translate(payload, req)

    fake = FakeLLM(responders={"translate": flaky})
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], fake)
    s1 = orchestrator_for(conn, cfg, book, rid, opts, fake).run()
    assert s1.status == "interrupted"
    done_before = conn.execute("SELECT COUNT(*) FROM translations WHERE run_id=?", (rid,)).fetchone()[0]
    assert done_before > 0
    interrupted = conn.execute("SELECT COUNT(*) FROM llm_requests WHERE run_id=? AND cost_status='unknown'",
                               (rid,)).fetchone()[0]
    assert interrupted == 1  # the in-flight request is kept as "billing unknown", not zero
    s2 = resume_run(conn, cfg, rid, FakeLLM())
    assert s2.status == "completed"
    # no unit translated twice in this run
    dup = conn.execute("SELECT unit_id, COUNT(*) c FROM translations WHERE run_id=? GROUP BY unit_id HAVING c>1",
                       (rid,)).fetchall()
    assert dup == []
    translatable = conn.execute("SELECT COUNT(*) FROM units WHERE book_id=? AND translatable=1", (book.id,)).fetchone()[0]
    assert len(latest_texts(conn, book.id)) == translatable


def test_budget_stop_then_resume(conn, cfg, fixtures):
    cfg.openrouter.max_concurrency = 1
    cfg.translation.unit_target_japanese_chars = 120
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(), budget_usd=0.004)
    assert s.status == "budget_exhausted"
    assert s.spent_usd <= 0.004
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND status='pending'", (rid,)).fetchone()[0]
    assert pending > 0
    s2 = resume_run(conn, cfg, rid, FakeLLM(), budget=1.0)
    assert s2.status == "completed"


def test_fatal_error_preserves_committed_work(conn, cfg, fixtures):
    cfg.openrouter.max_concurrency = 1
    cfg.translation.unit_target_japanese_chars = 120
    fake = FakeLLM(faults={"translate": ["ok", "ok", "fatal"]})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake)
    assert s.status == "stopped_fatal"
    kept = conn.execute("SELECT COUNT(*) FROM translations WHERE run_id=?", (rid,)).fetchone()[0]
    assert kept > 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND status='running'", (rid,)).fetchone()[0] == 0
    s2 = resume_run(conn, cfg, rid, FakeLLM())
    assert s2.status == "completed"


def test_failed_unit_keeps_original_and_is_reported(conn, cfg, fixtures):
    cfg.translation.unit_target_japanese_chars = 10_000

    def bad_first(payload, req):
        from ncte.llm.fake import default_translate

        out = default_translate(payload, req)
        for t in out["translations"]:
            if "朝の市場" in next(u["source"] for u in payload["units"] if u["unit_id"] == t["unit_id"]):
                t["translated_text"] = ""
        return out

    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={"translate": bad_first}))
    assert s.status == "completed_with_failures" and s.counts.get("failed") == 1
    f = conn.execute("SELECT * FROM qa_findings WHERE run_id=? AND type='translation_failed'", (rid,)).fetchall()
    assert len(f) == 1
    out = export_book(conn, cfg, book, run_epubcheck=False)
    assert out.stats["untranslated_kept_original"] == 1
    ch1 = zipfile.ZipFile(out.result.output_path).read("OEBPS/Text/ch1.xhtml").decode()
    assert "朝の市場は、もう人であふれていた。" in ch1
    assert out.validation.ok


def test_unknown_model_is_fatal(conn, cfg, fixtures):
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(models=["other/model"]))
    assert s.status == "stopped_fatal" and n_requests(conn, rid) == 0


def test_fallback_only_when_configured(conn, cfg, fixtures):
    cfg.openrouter.fallback_models = ["fake/alt"]
    fake = FakeLLM(models=["fake/alt"])
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake)
    assert s.status == "completed"
    assert any("fallback" in n for n in s.notes)
    assert conn.execute("SELECT DISTINCT model_id FROM translations WHERE run_id=?", (rid,)).fetchall()[0][0] == "fake/alt"


def test_retranslate_single_unit_preserves_others(conn, cfg, fixtures):
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM())
    before = {k: v["id"] for k, v in latest_texts(conn, book.id).items()}
    target = next(iter(before))
    _, rid2, s2 = run_book(conn, cfg, fixtures["basic"], FakeLLM(), unit_ids=[target], force=True)
    assert s2.status == "completed" and s2.counts == {"succeeded": 1}
    after = {k: v["id"] for k, v in latest_texts(conn, book.id).items()}
    assert after[target] != before[target]
    assert {k: v for k, v in after.items() if k != target} == {k: v for k, v in before.items() if k != target}


def test_limit_units_smoke(conn, cfg, fixtures):
    fake = FakeLLM()
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, limit=3)
    assert s.counts == {"succeeded": 3}


def test_structured_output_downgrade_is_recorded(conn, cfg, fixtures):
    fake = FakeLLM(structured=False)
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, limit=2)
    assert s.status == "completed"
    assert any("structured_outputs" in n for n in s.notes)
    modes = {r[0] for r in conn.execute("SELECT structured_mode FROM llm_requests WHERE run_id=?", (rid,))}
    assert modes == {"json_object"}


def test_baseline_lookahead_does_not_cross_scene(conn, cfg, fixtures):
    cfg.translation.unit_target_japanese_chars = 1  # one unit per batch
    fake = FakeLLM()
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake)
    last_of_scene1 = next(p for p in fake.payloads if p.get("units") and "本当だろうな" in p["units"][0]["source"])
    assert last_of_scene1["context_after"] == []  # the next unit is a scene separator / next scene
    mid = next(p for p in fake.payloads if p.get("units") and "なくしませんよ" in p["units"][0]["source"])
    assert "本当だろうな" in mid["context_after"][0]["source"]


def test_content_filter_blocks_only_the_offending_unit(conn, cfg, fixtures):
    """Real-book regression: a provider content filter must not stop the whole run."""
    cfg.translation.unit_target_japanese_chars = 10_000  # big batch → must be split to isolate
    fake = FakeLLM(blocked_terms=["頭をかいた"])
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake)
    assert s.status == "completed_with_failures"
    failed = [r[0] for r in conn.execute("SELECT target_id FROM jobs WHERE run_id=? AND status='failed'", (rid,))]
    assert len(failed) == 1
    src = conn.execute("SELECT plain_text FROM units WHERE id=?", (failed[0],)).fetchone()[0]
    assert "頭をかいた" in src
    assert conn.execute("SELECT COUNT(*) FROM qa_findings WHERE run_id=? AND type='content_blocked'", (rid,)).fetchone()[0] == 1
    # refused requests are not billed
    assert conn.execute("SELECT COUNT(*) FROM llm_requests WHERE run_id=? AND status='failed' AND cost_status!='none'",
                        (rid,)).fetchone()[0] == 0
    out = export_book(conn, cfg, book, run_epubcheck=False)
    assert out.validation.ok and out.stats["untranslated_kept_original"] == 1


def test_content_filter_in_context_stages_is_not_fatal(conn, cfg, fixtures):
    fake = FakeLLM(blocked_terms=["フードをかぶった少女"])
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed_with_failures"
    assert s.counts.get("failed") == 1


def test_retranslating_distant_units_never_batches_them_together(conn, cfg, fixtures):
    """Real-book regression: two non-adjacent paragraphs batched together lost the text between them and the
    model carried the addressee of the first into the second (妳 instead of 她)."""
    fake = FakeLLM()
    book, _, _ = run_book(conn, cfg, fixtures["basic"], fake)
    ids = [r[0] for r in conn.execute("SELECT id FROM units WHERE book_id=? AND scene_id='s_000_01' AND kind='text' "
                                      "ORDER BY ordinal", (book.id,))]
    pick = [ids[1], ids[4], ids[5]]  # 1 is far from 4; 4 and 5 are adjacent
    fake2 = FakeLLM()
    run_book(conn, cfg, fixtures["basic"], fake2, unit_ids=pick, force=True)
    groups = [[u["unit_id"] for u in p["units"]] for p in fake2.payloads]
    assert groups == [[ids[1]], [ids[4], ids[5]]]
    assert ids[3] in [c["unit_id"] for c in fake2.payloads[1]["context_before"]]  # the connecting text is context


def test_each_step_can_reason_differently(conn, cfg, fixtures):
    from ncte.config import reasoning_param, reasoning_setting
    from ncte.translation.orchestrator import reasoning_label

    cfg.openrouter.reasoning_effort = "medium"
    cfg.openrouter.stage_reasoning = {"memory_update": "low", "translate": "max:3000", "scene_analyze": "default"}
    assert reasoning_setting(cfg, "translate:retry") == "max:3000" and reasoning_setting(cfg, "verify") == "medium"
    assert reasoning_param("max:3000") == {"max_tokens": 3000} and reasoning_setting(cfg, "scene_analyze") is None
    fake = FakeLLM()
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    sent = {c.stage.split(":")[0]: c.extra.get("reasoning") for c in fake.calls}
    assert sent["memory_update"] == {"effort": "low"} and sent["translate"] == {"max_tokens": 3000}
    assert sent["scene_analyze"] is None
    recorded = dict(conn.execute("SELECT stage, reasoning_effort FROM llm_requests WHERE run_id=? GROUP BY stage", (rid,)))
    assert recorded["translate"] == "max:3000" and recorded["memory_update"] == "low"
    assert reasoning_label({"effort": "low"}) == "low"


def test_context_mode_translates_a_scene_per_request(conn, cfg, fixtures):
    """ADR-044 (read-first, the default path): a whole scene per request, not slices of unit_target_japanese_chars."""
    cfg.translation.unit_target_japanese_chars = 40  # would slice every scene if it were still used
    fake = FakeLLM()
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    per_scene = {}
    for c, p in zip(fake.calls, fake.payloads, strict=True):
        if c.stage == "translate" and p.get("units"):
            sids = {conn.execute("SELECT scene_id FROM units WHERE id=?", (u["unit_id"],)).fetchone()[0] for u in p["units"]}
            assert len(sids) == 1
            per_scene[next(iter(sids))] = per_scene.get(next(iter(sids)), 0) + 1
    assert per_scene and all(n == 1 for n in per_scene.values()), per_scene


def test_rule_flags_are_checked_with_context_before_review(conn, cfg, fixtures, monkeypatch):
    """A slur rule that hits a name (「阿三」) or an idiom is dismissed by the model's second look; real ones go on."""
    import ncte.qa.rules as rules

    seen = []

    def confirm(payload, req):
        seen.append(payload)
        return {"checks": [{"unit_id": i["unit_id"], "rule": i["rule"], "real": False, "reason": "人名音譯"}
                           for i in payload["items"]]}

    orig = rules.check_translation

    def flag_first(**kw):
        out = orig(**kw)
        if not seen and kw["kind"] == "text":
            out.append(rules.Finding("offensive_term", "high", "「阿三」 is a slur", kw["unit_id"]))
        return out

    monkeypatch.setattr(rules, "check_translation", flag_first)
    import ncte.translation.translator as tr
    monkeypatch.setattr(tr, "check_translation", flag_first)
    reviewed = []
    fake = FakeLLM(responders={"confirm": confirm, "review": lambda p, r: reviewed.append(p) or {"issues": []}})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed" and seen and seen[0]["items"][0]["source"] and "around" in seen[0]["items"][0]
    st = conn.execute("SELECT status, evidence FROM qa_findings WHERE run_id=? AND type='offensive_term'", (rid,)).fetchall()
    assert st and all(r["status"] == "dismissed_by_check" and "人名音譯" in r["evidence"] for r in st)
    assert not any(p for p in reviewed if any(u.get("unit_id") in {x["unit_id"] for x in seen[0]["items"]}
                                              for u in p.get("units", [])))


def test_a_flag_the_check_dismisses_also_settles_the_translate_job(conn, cfg, fixtures, monkeypatch):
    """A whole-book run left five translate jobs at needs_review whose current translation was succeeded: the rule
    check called the flag a false alarm and cleared the translation but not the job. Both change together."""
    import ncte.qa.rules as rules
    import ncte.translation.translator as tr

    flagged: list[str] = []
    orig = rules.check_translation

    def flag_first(**kw):
        out = orig(**kw)
        if not flagged and kw["kind"] == "text":
            flagged.append(kw["unit_id"])
            out.append(rules.Finding("offensive_term", "high", "「阿三」 is a slur", kw["unit_id"]))
        return out

    monkeypatch.setattr(rules, "check_translation", flag_first)
    monkeypatch.setattr(tr, "check_translation", flag_first)

    def confirm(payload, req):
        return {"checks": [{"unit_id": i["unit_id"], "rule": i["rule"], "real": False, "reason": "人名音譯"}
                           for i in payload["items"]]}

    fake = FakeLLM(responders={"confirm": confirm, "review": lambda p, r: {"issues": []}})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert flagged and any(c.stage == "confirm" for c in fake.calls)
    t = conn.execute("SELECT status FROM translations WHERE unit_id=? AND run_id=? ORDER BY id DESC LIMIT 1",
                     (flagged[0], rid)).fetchone()["status"]
    job = conn.execute("SELECT status FROM jobs WHERE run_id=? AND stage='translate' AND target_id=?",
                       (rid, flagged[0])).fetchone()["status"]
    assert (t, job) == ("succeeded", "succeeded")
    assert not conn.execute("SELECT 1 FROM jobs j JOIN translations t ON t.unit_id=j.target_id AND t.run_id=j.run_id "
                            "WHERE j.run_id=? AND j.stage='translate' AND j.status='needs_review' AND "
                            "t.id=(SELECT MAX(id) FROM translations WHERE unit_id=j.target_id AND run_id=j.run_id) "
                            "AND t.status='succeeded'", (rid,)).fetchone()


def test_a_rule_flag_the_check_confirms_is_fixed_even_if_review_reports_something_else(conn, cfg, fixtures,
                                                                                      monkeypatch):
    """「我們」 in third-person narration, confirmed real with context: review reports only a small other point (low)
    — the confirmed flag is not cleared by that, it goes to the translator with the check's reason."""
    import ncte.qa.rules as rules
    import ncte.translation.translator as tr

    flagged: list[str] = []
    orig = rules.check_translation

    def flag_first(**kw):
        out = orig(**kw)
        if not flagged and kw["kind"] == "text":
            flagged.append(kw["unit_id"])
            out.append(rules.Finding("narration_first_person", "high", "我 in third-person narration", kw["unit_id"]))
        return out

    monkeypatch.setattr(rules, "check_translation", flag_first)
    monkeypatch.setattr(tr, "check_translation", flag_first)
    fixes: list = []

    def translate(payload, req):
        from ncte.llm.fake import default_translate

        if "review_feedback" in payload:
            fixes.append(payload["review_feedback"])
        return default_translate(payload, req)

    def confirm(payload, req):
        return {"checks": [{"unit_id": i["unit_id"], "rule": i["rule"], "real": True, "reason": "敘述是第三人稱"}
                           for i in payload["items"]]}

    def review(payload, req):
        return {"issues": [{"unit_id": i["unit_id"], "issue_type": "style", "severity": "low", "evidence": "小地方",
                            "suggestion": "", "needs_retranslation": False} for i in payload["items"]]}

    fake = FakeLLM(responders={"translate": translate, "confirm": confirm, "review": review})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert flagged and fixes
    sent = [i for f in fixes for x in f if x["unit_id"] == flagged[0] for i in x["issues"]]
    assert any(i["issue_type"] == "narration_first_person" and "敘述是第三人稱" in i["evidence"] for i in sent)
    st = conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='narration_first_person'",
                      (flagged[0],)).fetchone()["status"]
    assert st != "cleared_by_review"


def test_reader_look_and_review_are_split_when_the_filter_refuses(conn, cfg, fixtures):
    from ncte.errors import ContentBlockedError

    cfg.review.automatic_review = "all"

    import json

    class Filter(FakeLLM):  # refuses any reader's look or review over 10 paragraphs / 2 items
        def complete(self, req):
            p = json.loads(req.messages[-1]["content"])
            if (req.stage == "reader" and len(p["paragraphs"]) > 10) or (req.stage == "review" and len(p["items"]) > 2):
                raise ContentBlockedError("HTTP 403 content filter: fake PROHIBITED_CONTENT")
            return super().complete(req)

    fake = Filter()
    book, rid, s = run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    assert s.status == "completed"
    calls = list(zip(fake.calls, fake.payloads, strict=True))
    reader_ok = [len(p["paragraphs"]) for c, p in calls if c.stage == "reader" and len(p["paragraphs"]) <= 10]
    assert len(reader_ok) > 1  # read in parts after the refusal
    review_ok = [len(p["items"]) for c, p in calls if c.stage == "review" and len(p["items"]) <= 2]
    assert review_ok  # reviewed in parts
    assert not conn.execute("SELECT 1 FROM qa_findings WHERE run_id=? AND type IN ('reader_blocked','review_blocked')",
                            (rid,)).fetchone()  # every part went through


def test_line_said_to_a_woman_with_you_is_flagged(conn, cfg, fixtures):
    """The reading records who a line is said to; 你 to a woman (written female in the source) is flagged."""
    import json as _json

    from ncte.qa import second_person

    from ..scripted import RESPONDERS

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    u = conn.execute("SELECT x.*, n.ordinal, n.id uid, n.plain_text FROM utterances x JOIN units n ON n.id=x.unit_id "
                     "WHERE x.book_id=? LIMIT 1", (book.id,)).fetchone()
    her = conn.execute("SELECT id FROM characters WHERE book_id=? LIMIT 1", (book.id,)).fetchone()[0]
    conn.execute("UPDATE utterances SET addressee_ids_json=? WHERE unit_id=?", (_json.dumps([her]), u["uid"]))
    conn.execute("INSERT OR REPLACE INTO character_gender(character_id, book_id, gender, certainty, evidence_quote, ordinal, "
                 "origin, updated_at) VALUES (?,?,?,?,?,?,?,?)", (her, book.id, "female", "explicit", "彼女", 0, "llm", "t"))
    unit = conn.execute("SELECT * FROM units WHERE id=?", (u["uid"],)).fetchone()
    assert "妳" in (second_person.check(conn, book.id, unit, "「你在做什麼？」") or "")
    assert second_person.check(conn, book.id, unit, "「妳在做什麼？」") is None
    assert second_person.check(conn, book.id, unit, "「你們在做什麼？」") is None
