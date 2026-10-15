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
