"""A fix request carries what fixing its paragraphs needs (``translation.fix_scope = "window"``): the paragraphs around
it and the memory of those paragraphs — not the whole scene's. A woman named two paragraphs after the fix is on the
cards with her gender (her 妳 was once changed to 你 when she was missing). ``"scene"`` keeps the earlier request."""

import json

import pytest

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


def flag(*needles):
    """A reviewer finding a serious error in each paragraph holding one of ``needles``."""
    def review(payload, req):
        return {"issues": [{"unit_id": i["unit_id"], "issue_type": "mistranslation", "severity": "high",
                            "evidence": "原文是疑問", "suggestion": "改成疑問句", "needs_retranslation": True}
                           for i in payload["items"] if any(n in i["source"] for n in needles)]}
    return review


def fix_requests(conn, cfg, tmp_path, scope, *needles):
    cfg.review.automatic_review = "all"
    cfg.translation.fix_scope = scope
    client = FakeLLM(responders={"scene_analyze": analyze, "memory_update": memory, "review": flag(*needles)})
    epub = write_book(spec(len(scope)), tmp_path / f"{scope}.epub")
    book, rid, _ = run_book(conn, cfg, epub, client, mode="context")
    return book, rid, [p for p in client.payloads if p.get("task") == "translate" and "review_feedback" in p]


def fix_run(conn, cfg, tmp_path, scope):
    book, rid, (fix,) = fix_requests(conn, cfg, tmp_path, scope, FIXED)
    return book, rid, fix


def by_source(conn, book):
    return {r["source_text"]: r for r in conn.execute("SELECT * FROM units WHERE book_id=?", (book.id,))}


def test_a_fix_in_the_window_scope_carries_the_window_and_its_people(conn, cfg, tmp_path):
    book, _rid, p = fix_run(conn, cfg, tmp_path, "window")
    plain = {r["id"]: r["plain_text"] for r in conn.execute("SELECT id, plain_text FROM units WHERE book_id=?",
                                                            (book.id,))}
    unit = by_source(conn, book)
    assert [FIXED in plain[u["unit_id"]] for u in p["units"]] == [True]
    assert "context_before" not in p and "context_after" not in p
    (fb,) = p["review_feedback"]
    for side in (fb["around"]["before"], fb["around"]["after"]):  # nearest paragraphs, within the window
        assert len(side) >= 2 and sum(len(unit[c["source"]]["plain_text"]) for c in side) \
            <= cfg.translation.fix_context_chars
    shown = " ".join(unit[c["source"]]["plain_text"] for side in fb["around"].values() for c in side)
    assert "花子が戸口に" in shown and "次郎は遠くで" not in shown and "三郎は最後に" not in shown
    cards = {c["name_ja"]: c for c in p["characters"]}
    assert set(cards) == {"太郎", "花子"}  # not the scene's others (次郎, 三郎: named only far from the fix)
    assert cards["花子"]["gender"] == "female"
    assert p["address_terms"] and all({a["speaker"], a["to"]} <= {"太郎", "花子"} for a in p["address_terms"])
    assert any(a["ja"] == "花子さん" for a in p["address_terms"])
    mem = p["memory"]
    assert 0 < len(mem["facts"]) <= cfg.context.fix_fact_quota
    assert "previous_chapter_summary" not in mem and len(mem["this_chapter_so_far"]) <= 1
    window = {unit[c["source"]]["id"] for side in fb["around"].values() for c in side} | {p["units"][0]["unit_id"]}
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


FAR = ("次郎、待ってくれ", FIXED, "三郎は最後に")  # the second paragraph of the scene, the middle, the last


@pytest.fixture(autouse=True)
def one_review_request(monkeypatch):
    """The scene reviewed in one request (its fixes come back together; a scene reviewed in parts is fixed a part at a
    time)."""
    from ncte.translation import review as rv

    monkeypatch.setattr(rv, "REVIEW_BATCH", 100)


def test_a_scene_s_fixes_far_apart_go_in_one_request_each_with_its_own_window(conn, cfg, tmp_path):
    book, rid, (p,) = fix_requests(conn, cfg, tmp_path, "window", *FAR)
    unit = by_source(conn, book)
    ids = [u["unit_id"] for u in p["units"]]
    assert [next(n for n in FAR if n in unit_src) for unit_src in (u["source"] for u in p["units"])] == list(FAR)
    assert [f["unit_id"] for f in p["review_feedback"]] == ids  # in story order
    assert "context_before" not in p and "context_after" not in p
    for fb in p["review_feedback"]:
        me = next(u for u in unit.values() if u["id"] == fb["unit_id"])
        before, after = fb["around"]["before"], fb["around"]["after"]
        assert all(set(c) == {"source", "translation"} for c in before)  # the current translation with the source
        assert all(set(c) == {"source"} for c in after)
        assert all(unit[c["source"]]["ordinal"] < me["ordinal"] for c in before)
        assert all(unit[c["source"]]["ordinal"] > me["ordinal"] and unit[c["source"]]["scene_id"] == me["scene_id"]
                   for c in after)
        assert not {unit[c["source"]]["id"] for c in before + after} & set(ids)
    first = p["review_feedback"][0]["around"]
    assert "次郎は遠くで" in first["before"][-1]["source"] and len(first["after"]) >= 2  # the scene begins just before
    assert p["review_feedback"][-1]["around"]["after"] == []  # the scene's last paragraph
    shown = " ".join(c["source"] for fb in p["review_feedback"] for side in fb["around"].values() for c in side)
    assert "花子が戸口に" in shown
    assert {"太郎", "次郎", "花子", "三郎"} <= {c["name_ja"] for c in p["characters"]}  # the union of the windows
    (row,) = [r for r in conn.execute("SELECT unit_ids_json, usage_json FROM llm_requests WHERE run_id=? AND "
                                      "stage='translate'", (rid,)) if json.loads(r["unit_ids_json"]) == ids]
    assert json.loads(row["usage_json"])["payload_chars"] > 0
    fixed = conn.execute("SELECT COUNT(DISTINCT unit_id) FROM translations WHERE run_id=? AND flags_json LIKE "
                         "'%review_retranslation%'", (rid,)).fetchone()[0]
    assert fixed == 3


def test_the_scene_scope_still_sends_a_request_per_run(conn, cfg, tmp_path):
    _book, _rid, sent = fix_requests(conn, cfg, tmp_path, "scene", *FAR)
    assert [len(p["units"]) for p in sent] == [1, 1, 1]
    assert all("context_before" in p and "around" not in p["review_feedback"][0] for p in sent)


def test_a_scene_s_fixes_are_split_into_even_parts_past_the_paragraph_cap(conn, cfg, tmp_path):
    cfg.translation.fix_batch_units = 2
    _book, _rid, sent = fix_requests(conn, cfg, tmp_path, "window", *FAR)
    assert [[next(n for n in FAR if n in u["source"]) for u in p["units"]] for p in sent] == [[FAR[0]], list(FAR[1:])]


def test_a_scene_s_fixes_are_split_past_the_character_cap(conn, cfg, tmp_path):
    cfg.translation.fix_batch_chars = 1  # each paragraph with its window is past it: one request each
    _book, _rid, sent = fix_requests(conn, cfg, tmp_path, "window", *FAR)
    assert [len(p["units"]) for p in sent] == [1, 1, 1]


def test_a_fix_next_to_another_fix_is_not_shown_as_its_context(conn, cfg, tmp_path):
    _book, rid, (p,) = fix_requests(conn, cfg, tmp_path, "window", FIXED, "道は20番目")
    srcs = [u["source"] for u in p["units"]]
    assert len(srcs) == 2 and FIXED in srcs[0] and srcs[1].startswith("　道は20番目")
    first, second = p["review_feedback"]
    assert srcs[1] not in [c["source"] for c in first["around"]["after"]]
    assert srcs[0] not in [c["source"] for c in second["around"]["before"]]
    assert any("花子が戸口に" in c["source"] for c in first["around"]["after"])  # the window goes on past it
    assert len(first["around"]["after"]) >= 2 and len(second["around"]["before"]) >= 2
    assert conn.execute("SELECT COUNT(DISTINCT unit_id) FROM translations WHERE run_id=? AND flags_json LIKE "
                        "'%review_retranslation%'", (rid,)).fetchone()[0] == 2
