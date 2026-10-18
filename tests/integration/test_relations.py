"""ADR-039 / ADR-045: relations noted while reading, put to the model pair by pair after reading."""

from ncte.analysis import relations
from ncte.llm.fake import FakeLLM
from ncte.memory.db import utcnow

from ..helpers import run_book
from ..scripted import RESPONDERS


def _two_characters(conn, book_id):
    rows = conn.execute("SELECT id FROM characters WHERE book_id=? ORDER BY known_from_ordinal LIMIT 2", (book_id,)).fetchall()
    return rows[0]["id"], rows[1]["id"]


def _note(conn, book_id, a, b, rel, at, a_is="父親", b_is="女兒", kind="identity"):
    conn.execute("INSERT INTO relation_notes(book_id, scene_id, a_id, b_id, relation_zh, a_is, b_is, kind, "
                 "evidence_unit_ids_json, evidence_quote, first_ordinal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (book_id, "s", a, b, rel, a_is, b_is, kind, "[]", "お父様", at, utcnow()))


def test_memory_update_notes_relations_the_text_states(conn, cfg, fixtures):
    def memory(payload, req):
        out = RESPONDERS["memory_update"](payload, req)
        ks = payload["known_characters"]
        if len(ks) >= 2 and payload["units"]:
            u = payload["units"][0]
            out["relations"] = [
                {"a_ref": ks[0]["character_id"], "b_ref": ks[1]["character_id"], "relation": "同伴", "a_is": "同伴",
                 "b_is": "同伴", "kind": "identity", "evidence_unit_ids": [u["unit_id"]], "evidence_quote": u["source"][:4]},
                {"a_ref": ks[0]["character_id"], "b_ref": "c_nobody000000", "relation": "不明", "a_is": "", "b_is": "",
                 "kind": "feeling", "evidence_unit_ids": [u["unit_id"]], "evidence_quote": u["source"][:4]}]
        return out

    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "memory_update": memory}),
                            mode="context")
    assert s.status == "completed"
    notes = conn.execute("SELECT * FROM relation_notes WHERE book_id=?", (book.id,)).fetchall()
    assert notes and all(n["relation_zh"] == "同伴" for n in notes)  # an unknown character is dropped


def test_pairs_cite_their_own_evidence_change_over_time_and_keep_a_persons(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    a, b = _two_characters(conn, book.id)
    conn.execute("DELETE FROM relations")
    _note(conn, book.id, a, b, "宿敵", 5, "宿敵", "宿敵", "feeling")
    _note(conn, book.id, a, b, "未婚夫妻", 9, "未婚夫", "未婚妻")
    conn.execute("INSERT INTO relations(book_id, a_id, b_id, relation_zh, a_is, b_is, since_ordinal, fact_ids_json, reason, "
                 "created_at, origin) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (book.id, a, b, "同鄉", "", "", 1, "[]", "", utcnow(), "human"))
    pairs, chars = relations.candidate_pairs(conn, book.id)
    req, lk = relations.requests(pairs, chars)[0]
    p = next(x for x in req["pairs"] if {lk["_chars"][x["a"]], lk["_chars"][x["b"]]} == {a, b})
    n_old, n_new = p["notes"][0]["ref"], p["notes"][1]["ref"]
    parsed = relations.RelationsOutput.model_validate({"relations": [
        {"a": p["a"], "b": p["b"], "relation": "前世宿敵", "a_is": "宿敵", "b_is": "宿敵", "kind": "feeling",
         "certainty": "explicit", "facts": [n_old], "reason": "t", "replaces": None},
        {"a": p["a"], "b": p["b"], "relation": "未婚夫妻", "a_is": "未婚夫", "b_is": "未婚妻", "kind": "identity",
         "certainty": "explicit", "facts": [n_new], "reason": "t", "replaces": "前世宿敵"},
        {"a": p["a"], "b": p["b"], "relation": "沒證據", "a_is": "", "b_is": "", "facts": ["n999"], "reason": "t"}]})
    assert relations.apply(conn, book.id, [(parsed, lk)]) == 2
    rows = {r["relation_zh"]: r for r in conn.execute("SELECT * FROM relations WHERE book_id=?", (book.id,))}
    assert rows["前世宿敵"]["since_ordinal"] == 5 and rows["前世宿敵"]["until_ordinal"] == 9
    assert rows["未婚夫妻"]["since_ordinal"] == 9 and rows["未婚夫妻"]["until_ordinal"] is None
    assert rows["同鄉"]["origin"] == "human"  # a person's relation stays through a new run
    at7 = {r["relation_zh"] for r in relations.visible(conn, book.id, 7)}
    at12 = {r["relation_zh"] for r in relations.visible(conn, book.id, 12)}
    assert "前世宿敵" in at7 and "未婚夫妻" not in at7 and "未婚夫妻" in at12 and "前世宿敵" not in at12


def test_pairs_that_should_be_related_are_asked_again(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    a, b = _two_characters(conn, book.id)
    _note(conn, book.id, a, b, "父女", 3)
    asked = []

    def rel(payload, req):
        asked.append(bool(payload.get("recheck")))
        return {"relations": []}

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "relations": rel}), mode="context", force=True)
    assert False in asked and True in asked  # first look, then the pairs with notes once more


def test_relations_command(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "names", "relations", epub, "--fake"])
    assert out.exit_code == 0, out.output and "relations:" in out.output


def test_a_character_called_mother_does_not_take_every_mothers_fact(conn, cfg, fixtures):
    from ncte.memory.repository import upsert_character

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    a, _ = _two_characters(conn, book.id)
    u = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()
    mom, _ = upsert_character(conn, book.id, "母", name_zh="母親", aliases=[], unit_id=u["id"], ordinal=u["ordinal"], origin="t")
    conn.execute("INSERT INTO facts(id, book_id, subject, predicate, object, fact_type, certainty, fact_json, "
                 "evidence_unit_ids_json, evidence_quote, first_revealed_unit_id, first_revealed_ordinal, created_at) "
                 "VALUES ('f_x', ?, ?, '被母親稱讚過', '小時候', 'objective', 'explicit', '{}', '[]', 'q', ?, ?, ?)",
                 (book.id, a, u["id"], u["ordinal"], utcnow()))
    pairs, _ = relations.candidate_pairs(conn, book.id)
    assert not any({p["a"], p["b"]} == {a, mom} for p in pairs)  # 「母親」 is not a proper name to match by


def _three_notes(conn, book_id, a, b, rels):
    for rel, at in rels:
        _note(conn, book_id, a, b, rel, at, rel, rel, "feeling")
    pairs, chars = relations.candidate_pairs(conn, book_id)
    req, lk = relations.requests(pairs, chars)[0]
    p = next(x for x in req["pairs"] if {lk["_chars"][x["a"]], lk["_chars"][x["b"]]} == {a, b})
    return p, lk, {n["at"]: n["ref"] for n in p["notes"]}


def test_enemy_ally_enemy_keeps_every_stretch(conn, cfg, fixtures):
    """敵人 → 同盟 → 敵人: the second 敵人 is a new stretch, not the first one rewritten (the first stretch stays)."""
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    a, b = _two_characters(conn, book.id)
    conn.execute("DELETE FROM relations")
    conn.execute("DELETE FROM relation_notes")
    p, lk, at = _three_notes(conn, book.id, a, b, [("敵人", 5), ("同盟", 9), ("敵人", 13)])
    rel = lambda name, ref, replaces: {"a": p["a"], "b": p["b"], "relation": name, "a_is": name, "b_is": name,  # noqa: E731
                                       "kind": "feeling", "certainty": "explicit", "facts": [ref], "reason": "t",
                                       "replaces": replaces}
    parsed = relations.RelationsOutput.model_validate({"relations": [
        rel("敵人", at[5], None), rel("同盟", at[9], "敵人"), rel("敵人", at[13], "同盟")]})
    assert relations.apply(conn, book.id, [(parsed, lk)]) == 3
    seen = {x: {r["relation_zh"] for r in relations.visible(conn, book.id, x)} for x in (7, 11, 15)}
    assert seen == {7: {"敵人"}, 11: {"同盟"}, 15: {"敵人"}}


def test_a_replacement_ends_the_relation_of_its_own_time(conn, cfg, fixtures):
    """「朋友」 of a past life replaced by 「宿敵」 of that life ends 「前世：朋友」 (stored with its time); the friends of
    the present are not ended by what happened in a past life."""
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    a, b = _two_characters(conn, book.id)
    conn.execute("DELETE FROM relations")
    conn.execute("DELETE FROM relation_notes")
    p, lk, at = _three_notes(conn, book.id, a, b, [("朋友", 5), ("宿敵", 9), ("朋友", 13)])
    rel = lambda name, ref, replaces, period: {"a": p["a"], "b": p["b"], "relation": name, "a_is": name,  # noqa: E731
                                               "b_is": name, "kind": "feeling", "certainty": "explicit", "facts": [ref],
                                               "reason": "t", "replaces": replaces, "period": period}
    parsed = relations.RelationsOutput.model_validate({"relations": [
        rel("朋友", at[13], None, ""), rel("朋友", at[5], None, "前世"), rel("宿敵", at[9], "朋友", "前世")]})
    relations.apply(conn, book.id, [(parsed, lk)])
    rows = {(r["relation_zh"], r["since_ordinal"]): r["until_ordinal"] for r in conn.execute(
        "SELECT * FROM relations WHERE book_id=?", (book.id,))}
    assert rows[("前世：朋友", 5)] == 9 and rows[("前世：宿敵", 9)] is None and rows[("朋友", 13)] is None


def test_a_relation_said_again_while_it_holds_is_one_stretch(conn, cfg, fixtures):
    """敵人 at 5 and 敵人 at 13 with nothing between: one stretch from 5. With 同盟 between but its "replaces" left out
    on the second 敵人, 同盟 is not ended — and neither 敵人 stretch is lost."""
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    a, b = _two_characters(conn, book.id)
    conn.execute("DELETE FROM relations")
    conn.execute("DELETE FROM relation_notes")
    p, lk, at = _three_notes(conn, book.id, a, b, [("敵人", 5), ("同盟", 9), ("敵人", 13)])
    rel = lambda name, ref, replaces: {"a": p["a"], "b": p["b"], "relation": name, "a_is": name, "b_is": name,  # noqa: E731
                                       "kind": "feeling", "certainty": "explicit", "facts": [ref], "reason": "t",
                                       "replaces": replaces}
    relations.apply(conn, book.id, [(relations.RelationsOutput.model_validate(
        {"relations": [rel("敵人", at[5], None), rel("敵人", at[13], None)]}), lk)])
    assert [(r["relation_zh"], r["since_ordinal"]) for r in conn.execute("SELECT * FROM relations")] == [("敵人", 5)]
    relations.apply(conn, book.id, [(relations.RelationsOutput.model_validate(
        {"relations": [rel("敵人", at[5], None), rel("同盟", at[9], "敵人"), rel("敵人", at[13], None)]}), lk)])
    got = sorted((r["relation_zh"], r["since_ordinal"], r["until_ordinal"]) for r in conn.execute("SELECT * FROM relations"))
    assert got == [("同盟", 9, None), ("敵人", 5, 9), ("敵人", 13, None)]


def test_the_models_replacement_does_not_end_a_persons_relation(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    a, b = _two_characters(conn, book.id)
    conn.execute("DELETE FROM relations")
    conn.execute("DELETE FROM relation_notes")
    conn.execute("INSERT INTO relations(book_id, a_id, b_id, relation_zh, a_is, b_is, since_ordinal, fact_ids_json, reason, "
                 "created_at, origin) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (book.id, a, b, "朋友", "", "", 1, "[]", "", utcnow(), "human"))
    p, lk, at = _three_notes(conn, book.id, a, b, [("宿敵", 9)])
    parsed = relations.RelationsOutput.model_validate({"relations": [
        {"a": p["a"], "b": p["b"], "relation": "宿敵", "a_is": "宿敵", "b_is": "宿敵", "kind": "feeling",
         "certainty": "inferred", "facts": [at[9]], "reason": "t", "replaces": "朋友"}]})
    relations.apply(conn, book.id, [(parsed, lk)])
    assert conn.execute("SELECT until_ordinal FROM relations WHERE relation_zh='朋友'").fetchone()[0] is None
