"""A fix request carries what fixing its paragraphs needs (``translation.fix_scope = "window"``): the paragraphs around
it and the memory of those paragraphs — not the whole scene's. A woman named two paragraphs after the fix is on the
cards with her gender (her 妳 was once changed to 你 when she was missing). ``"scene"`` keeps the earlier request."""

import json

from ncte.llm.fake import FakeLLM, default_memory_update, default_scene_analyze
from ncte.samples import BookSpec, Doc, _section, write_book

from ..helpers import orchestrator_for, run_book
from ..scripted import known, uid

FILLER = "空は高く、風は丘の草をゆっくりと揺らしていた。遠くの森では鳥が鳴き、川の水は冷たく澄んで光っていた。" * 2


def _filler(i: int) -> str:
    return f"　道は{i}番目の丘を越えて続いていた。{FILLER}"


FACTS = ["　太郎は村に着いた。", "　太郎は剣を磨いた。", "　太郎は丘の道を歩いた。", "　太郎は空を見上げた。",
         "　太郎は川の水を飲んだ。", "　太郎は森の鳥を数えた。"]
CH1 = FACTS + ["　花子は太郎を迎えた。", "「花子さん、ただいま」と太郎は言った。", "「太郎、遅いぞ」と次郎は言った。"]
CH2 = (["　次郎は遠くで手を振った。", "「次郎、待ってくれ」と太郎は叫んだ。"]
       + [_filler(i) for i in range(12)]
       + ["　太郎は銀の指輪を見つめた。「これは誰のものだ」", _filler(20), "　花子が戸口に現れた。彼女は微笑んだ。",
          "「花子さん、来てくれたんだね」と太郎は言った。"]
       + [_filler(i) for i in range(30, 42)]
       + ["　三郎は最後にやって来た。"])
FIXED = "銀の指輪を見つめた"


def spec(n: int) -> BookSpec:
    return BookSpec("丘の道", f"urn:uuid:6f1d2a1e-0b7c-4c55-9a43-0000000000f{n}", [
        Doc("ch1", "Text/ch1.xhtml", "第一章", _section("第一章", CH1, None, "c1")),
        Doc("ch2", "Text/ch2.xhtml", "第二章", _section("第二章", CH2, None, "c2"))])


def analyze(payload, req):
    out = default_scene_analyze(payload, req)
    new, people = [], {}
    for temp, name, line in (("new_1", "太郎", "太郎は村に着いた"), ("new_2", "次郎", "と次郎は言った"),
                             ("new_3", "花子", "花子は太郎を迎えた"), ("new_4", "三郎", "三郎は最後に")):
        if cid := known(payload, name):
            people[name] = cid
        elif u := uid(payload, line):
            new.append({"temp_id": temp, "name_ja": name, "aliases_ja": [], "proposed_name_zh": name,
                        "evidence_unit_ids": [u]})
            people[name] = temp
    out["new_characters"] = new
    if uid(payload, FIXED):  # all four are in the scene
        out["participants"] = [{"character_ref": c, "presence": "explicit", "evidence_unit_ids": []}
                               for c in people.values()]
    return out


def memory(payload, req):
    out = default_memory_update(payload, req)
    taro = known(payload, "太郎")
    hanako, jiro = known(payload, "花子"), known(payload, "次郎")
    if taro and hanako and jiro and uid(payload, "太郎は村に着いた"):  # all before the second chapter
        out["facts"] = [{"subject_ref": taro, "predicate": "做了", "object": line.strip("　。"), "fact_type": "objective",
                         "certainty": "explicit", "evidence_unit_ids": [uid(payload, line.strip("　。"))],
                         "evidence_quote": line.strip("　"), "source_viewpoint": None, "retracts_fact_ids": []}
                        for line in FACTS]
        out["address_terms"] = [
            {"speaker_ref": taro, "addressee_ref": hanako, "term": "花子さん", "term_zh": "花子小姐", "note": "",
             "evidence_unit_ids": [uid(payload, "花子さん、ただいま")], "evidence_quote": "花子さん、ただいま"},
            # how 次郎 (not near the fix) calls 太郎: the form is in the fix's window, its speaker is not
            {"speaker_ref": jiro, "addressee_ref": taro, "term": "太郎", "term_zh": "太郎", "note": "",
             "evidence_unit_ids": [uid(payload, "太郎、遅いぞ")], "evidence_quote": "太郎、遅いぞ"}]
    if hanako and (u := uid(payload, "彼女は微笑んだ")):  # shown only two paragraphs after the fix
        out["genders"] = [{"character_ref": hanako, "gender": "female", "evidence_unit_ids": [u],
                           "evidence_quote": "彼女は微笑んだ"}]
    return out


def review(payload, req):
    return {"issues": [{"unit_id": i["unit_id"], "issue_type": "mistranslation", "severity": "high",
                        "evidence": "原文是疑問", "suggestion": "改成疑問句", "needs_retranslation": True}
                       for i in payload["items"] if FIXED in i["source"]]}


def fix_run(conn, cfg, tmp_path, scope):
    cfg.review.automatic_review = "all"
    cfg.translation.fix_scope = scope
    client = FakeLLM(responders={"scene_analyze": analyze, "memory_update": memory, "review": review})
    book, rid, _ = run_book(conn, cfg, write_book(spec(len(scope)), tmp_path / f"{scope}.epub"), client, mode="context")
    (fix,) = [p for p in client.payloads if p.get("task") == "translate" and "review_feedback" in p]
    return book, rid, fix


def test_a_fix_in_the_window_scope_carries_the_window_and_its_people(conn, cfg, tmp_path):
    book, _rid, p = fix_run(conn, cfg, tmp_path, "window")
    plain = {r["id"]: r["plain_text"] for r in conn.execute("SELECT id, plain_text FROM units WHERE book_id=?",
                                                            (book.id,))}
    assert [FIXED in plain[u["unit_id"]] for u in p["units"]] == [True]
    for side in (p["context_before"], p["context_after"]):  # nearest paragraphs, within the window
        assert len(side) >= 2 and sum(len(plain[c["unit_id"]]) for c in side) <= cfg.translation.fix_context_chars
    shown = " ".join(plain[c["unit_id"]] for c in p["context_before"] + p["context_after"])
    assert "花子が戸口に" in shown and "次郎は遠くで" not in shown and "三郎は最後に" not in shown
    cards = {c["name_ja"]: c for c in p["characters"]}
    assert set(cards) == {"太郎", "花子"}  # not the scene's others (次郎, 三郎: named only far from the fix)
    assert cards["花子"]["gender"] == "female"
    assert p["address_terms"] and all({a["speaker"], a["to"]} <= {"太郎", "花子"} for a in p["address_terms"])
    assert any(a["ja"] == "花子さん" for a in p["address_terms"])
    mem = p["memory"]
    assert 0 < len(mem["facts"]) <= cfg.context.fix_fact_quota
    assert "previous_chapter_summary" not in mem and len(mem["this_chapter_so_far"]) <= 1
    window = {c["unit_id"] for c in p["context_before"] + p["context_after"]} | {p["units"][0]["unit_id"]}
    assert p["scene"]["utterances"] and all(u["unit_id"] in window for u in p["scene"]["utterances"])


def test_the_scene_scope_sends_the_earlier_fix_request(conn, cfg, tmp_path):
    from ncte.translation.orchestrator import Batch, load_run

    book, rid, p = fix_run(conn, cfg, tmp_path, "scene")
    unit = conn.execute("SELECT * FROM units WHERE id=?", (p["units"][0]["unit_id"],)).fetchone()
    _row, rcfg, opts = load_run(conn, rid)
    rcfg.app.workspace = cfg.app.workspace
    orch = orchestrator_for(conn, rcfg, book, rid, opts, FakeLLM())
    orch.reload_glossary()
    old = orch.batch_context(Batch([unit], False, unit["scene_id"], whole_scene=True))
    assert {k: v for k, v in p.items() if k not in ("task", "target_language", "units", "review_feedback")} == old
    assert "次郎" in {c["name_ja"] for c in p["characters"]}  # the whole scene's people
    assert any(a["speaker"] == "次郎" for a in p.get("address_terms", []))
    assert "previous_chapter_summary" in p["memory"]


def test_each_request_records_its_payload_size(conn, cfg, tmp_path):
    sizes = {}
    for scope in ("scene", "window"):
        _book, rid, p = fix_run(conn, cfg, tmp_path, scope)
        rows = conn.execute("SELECT stage, unit_ids_json, usage_json FROM llm_requests WHERE run_id=? ORDER BY id",
                            (rid,)).fetchall()
        assert rows and all(json.loads(r["usage_json"])["payload_chars"] > 0 for r in rows)
        fix = [r for r in rows if r["stage"] == "translate" and json.loads(r["unit_ids_json"]) == [p["units"][0]["unit_id"]]]
        sizes[scope] = json.loads(fix[-1]["usage_json"])["payload_chars"]  # the fix, after the first translation
        assert json.loads(fix[-1]["usage_json"])["prompt_tokens"] > 0  # the usage the source reported is kept
    assert sizes["window"] < sizes["scene"]
