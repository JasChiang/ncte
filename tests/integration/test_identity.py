"""ADR-046: nameless and group characters after reading; relations so far given while reading."""

from ncte.analysis import relations
from ncte.llm.fake import FakeLLM
from ncte.memory.db import utcnow
from ncte.memory.repository import upsert_character

from ..helpers import run_book
from ..scripted import RESPONDERS


def _first_unit(conn, book_id):
    return conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal LIMIT 1",
                        (book_id,)).fetchone()


def test_reading_sees_relations_noted_before(conn, cfg, fixtures):
    seen = []

    def scene(payload, req):
        seen.append([k for k in payload.get("known_characters", []) if k.get("relations")])
        return RESPONDERS["scene_analyze"](payload, req)

    def memory(payload, req):
        out = RESPONDERS["memory_update"](payload, req)
        ks = payload["known_characters"]
        if len(ks) >= 2 and payload["units"]:
            u = payload["units"][0]
            out["relations"] = [{"a_ref": ks[0]["character_id"], "b_ref": ks[1]["character_id"], "relation": "姊妹",
                                 "a_is": "妹妹", "b_is": "姊姊", "kind": "identity", "evidence_unit_ids": [u["unit_id"]],
                                 "evidence_quote": u["source"][:4]}]
        return out

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "scene_analyze": scene, "memory_update": memory}),
             mode="context")
    assert any(any("的妹妹" in r or "的姊姊" in r for k in s for r in k["relations"]) for s in seen[1:])


def test_nameless_character_merged_and_group_members_recorded(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    u = _first_unit(conn, book.id)
    named = conn.execute("SELECT id FROM characters WHERE book_id=? AND canonical_name IN (SELECT source_term FROM glossary "
                         "WHERE category='character') LIMIT 2", (book.id,)).fetchall()
    sis, _ = upsert_character(conn, book.id, "姉ちゃん", name_zh="姊姊", aliases=[], unit_id=u["id"], ordinal=u["ordinal"], origin="t")
    grp, _ = upsert_character(conn, book.id, "取り巻きの二人", name_zh="跟班兩人", aliases=[], unit_id=u["id"], ordinal=u["ordinal"],
                              origin="t")
    for c in (sis, grp):
        conn.execute("INSERT INTO relation_notes(book_id, scene_id, a_id, b_id, relation_zh, a_is, b_is, kind, "
                     "evidence_unit_ids_json, evidence_quote, first_ordinal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (book.id, "s", named[0]["id"], c, "主從", "主子", "跟班", "identity", "[]", "q", u["ordinal"], utcnow()))
    asked = []

    def verify(payload, req):
        out = []
        for i in payload["items"]:
            asked.append(i["kind"])
            ans = "keep"
            if i["ref"] == f"i:{sis}":
                ans = named[1]["id"]
            if i["kind"] == "group":
                ans = named[1]["id"]
            out.append({"ref": i["ref"], "answer": ans, "reason": "t"})
        return {"items": out}

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "verify": verify}), mode="context", force=True)
    assert "identity" in asked and "group" in asked
    assert not conn.execute("SELECT 1 FROM characters WHERE canonical_name='姉ちゃん'").fetchone()  # one person now
    assert conn.execute("SELECT member_id FROM group_members WHERE group_id=?", (grp,)).fetchone()[0] == named[1]["id"]
    pairs, _ = relations.candidate_pairs(conn, book.id)
    p = next(p for p in pairs if {p["a"], p["b"]} == {named[0]["id"], named[1]["id"]})
    assert any(k[0] == "notes" for k in p["via"])  # the group's relation is material for its member


def test_verify_command(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "names", "verify", epub, "--fake"])
    assert out.exit_code == 0 and "second look:" in out.output, out.output


def test_reading_notes_group_members_and_rereading_replaces_them(conn, cfg, fixtures):
    groups = []

    def memory(payload, req):
        out = RESPONDERS["memory_update"](payload, req)
        ks = payload["known_characters"]
        groups.append([k for k in ks if k.get("members")])
        if len(ks) >= 3 and payload["units"]:
            u = payload["units"][0]
            out["groups"] = [{"group_ref": ks[0]["character_id"], "member_refs": [ks[1]["character_id"], ks[2]["character_id"]],
                              "evidence_unit_ids": [u["unit_id"]], "evidence_quote": u["source"][:4]},
                             {"group_ref": ks[0]["character_id"], "member_refs": [ks[1]["character_id"]],
                              "evidence_unit_ids": [u["unit_id"]], "evidence_quote": "原文にない言葉"}]  # not in the text
        return out

    llm = FakeLLM(responders={**RESPONDERS, "memory_update": memory})
    book, _, _ = run_book(conn, cfg, fixtures["basic"], llm, mode="context")
    rows = conn.execute("SELECT group_id, member_id, origin FROM group_members").fetchall()
    assert rows and all(r["origin"] == "read" for r in rows)
    run_book(conn, cfg, fixtures["basic"], llm, mode="context", force=True)
    assert len(conn.execute("SELECT * FROM group_members").fetchall()) == len(rows)  # replaced, not doubled
    assert any(groups)  # a group card shows its members to the reading


def test_group_passages_reach_member_pairs(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    u = conn.execute("SELECT id, ordinal, plain_text FROM units WHERE book_id=? AND kind='text' AND plain_text LIKE '%ミナ%' "
                     "ORDER BY ordinal LIMIT 1", (book.id,)).fetchone()
    mina = conn.execute("SELECT id FROM characters WHERE canonical_name='ミナ'").fetchone()[0]
    kaito = conn.execute("SELECT id FROM characters WHERE canonical_name='カイト'").fetchone()[0]
    word = next(w for w in ("「", "。", "、") if w in u["plain_text"])  # any text in the paragraph names "the group"
    grp, _ = upsert_character(conn, book.id, word * 2 if word * 2 in u["plain_text"] else u["plain_text"][:3], name_zh="跟班們",
                              aliases=[], unit_id=u["id"], ordinal=u["ordinal"], origin="t")
    conn.execute("INSERT INTO group_members(group_id, member_id, book_id, created_at) VALUES (?,?,?,?)",
                 (grp, kaito, book.id, utcnow()))
    conn.execute("INSERT INTO relation_notes(book_id, scene_id, a_id, b_id, relation_zh, a_is, b_is, kind, "
                 "evidence_unit_ids_json, evidence_quote, first_ordinal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (book.id, "s", mina, grp, "主從", "主子", "跟班", "identity", "[]", "q", u["ordinal"], utcnow()))
    pairs, chars = relations.candidate_pairs(conn, book.id)
    p = next(p for p in pairs if {p["a"], p["b"]} == {mina, kaito})
    assert p["passages"] and p["passages"][0]["id"] == u["id"]
    req = relations.requests([p], chars)[0][0]
    assert req["pairs"][0]["passages"][0]["text"].startswith("〔關於群體「跟班們」〕")
