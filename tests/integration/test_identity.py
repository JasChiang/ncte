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
    last = conn.execute("SELECT max(ordinal) FROM units WHERE book_id=?", (book.id,)).fetchone()[0]  # noted elsewhere too
    sis, _ = upsert_character(conn, book.id, "姉ちゃん", name_zh="姊姊", aliases=[], unit_id=u["id"], ordinal=u["ordinal"], origin="t")
    grp, _ = upsert_character(conn, book.id, "取り巻きの二人", name_zh="跟班兩人", aliases=[], unit_id=u["id"], ordinal=u["ordinal"],
                              origin="t")
    conn.execute("UPDATE characters SET is_group=1 WHERE id=?", (grp,))  # as the model read the words (several people)
    for c in (sis, grp):
        conn.execute("INSERT INTO relation_notes(book_id, scene_id, a_id, b_id, relation_zh, a_is, b_is, kind, "
                     "evidence_unit_ids_json, evidence_quote, first_ordinal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (book.id, "s", named[0]["id"], c, "主從", "主子", "跟班", "identity", "[]", "q", last, utcnow()))
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


def test_plural_you_follows_the_people_the_line_is_said_to(conn, cfg, fixtures):
    import json

    from ncte.memory.repository import set_gender
    from ncte.qa import second_person

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    utt = conn.execute("SELECT * FROM utterances WHERE book_id=? LIMIT 1", (book.id,)).fetchone()
    unit = conn.execute("SELECT * FROM units WHERE id=?", (utt["unit_id"],)).fetchone()
    a, b = [r[0] for r in conn.execute("SELECT id FROM characters WHERE book_id=? LIMIT 2", (book.id,))]
    conn.execute("DELETE FROM utterances WHERE unit_id=? AND id!=?", (unit["id"], utt["id"]))
    conn.execute("UPDATE utterances SET addressee_ids_json=? WHERE id=?", (json.dumps([a, b]), utt["id"]))
    for x in (a, b):
        conn.execute("DELETE FROM character_gender WHERE character_id=?", (x,))
        set_gender(conn, book.id, x, "female", ordinal=unit["ordinal"], quote="彼女", origin="llm", certainty="explicit")
    assert "妳們" in (second_person.check(conn, book.id, unit, "「你們，走吧」") or "")
    assert second_person.check(conn, book.id, unit, "「妳們，走吧」") is None
    conn.execute("DELETE FROM character_gender WHERE character_id=?", (b,))
    set_gender(conn, book.id, b, "male", ordinal=unit["ordinal"], quote="彼", origin="llm", certainty="explicit")
    assert "你們" in (second_person.check(conn, book.id, unit, "「妳們，走吧」") or "")


def test_rereading_a_scene_drops_cards_only_its_last_reading_made(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    u = _first_unit(conn, book.id)
    stale, _ = upsert_character(conn, book.id, "姉ちゃん", name_zh="姊姊", aliases=[], unit_id=u["id"], ordinal=u["ordinal"],
                                origin="llm")
    kept, _ = upsert_character(conn, book.id, "母", name_zh="母親", aliases=[], unit_id=u["id"], ordinal=u["ordinal"],
                               origin="llm")
    from ncte.memory.repository import set_gender
    set_gender(conn, book.id, kept, "female", ordinal=u["ordinal"], quote=None, origin="human")  # a person decided
    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context", force=True)
    assert conn.execute("SELECT 1 FROM characters WHERE id=?", (stale,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM characters WHERE id=?", (kept,)).fetchone() is not None
    named = conn.execute("SELECT count(*) FROM characters WHERE canonical_name IN ('ミナ','カイト','リリア')").fetchone()[0]
    assert named == 3  # the story's characters are read again as before


def test_people_page_shows_group_members(conn, cfg, fixtures):
    from ncte.gui import data

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    a, b = [r[0] for r in conn.execute("SELECT id FROM characters WHERE book_id=? LIMIT 2", (book.id,))]
    conn.execute("INSERT INTO group_members(group_id, member_id, book_id, created_at) VALUES (?,?,?,?)", (a, b, book.id, utcnow()))
    chars = data.characters(conn, book)["characters"]
    assert chars[a]["members"] == [b] and chars[b]["member_of"] == [a]


def test_pairs_get_passages_where_the_two_appear_together(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    pairs, chars = relations.candidate_pairs(conn, book.id)
    with_text = [p for p in pairs if any(g["via"] is None for g in p["passages"])]
    assert with_text  # the two people of the sample story are named and speak in the same stretches
    p = with_text[0]
    names = {chars[p["a"]]["canonical_name"], chars[p["b"]]["canonical_name"]}
    g = next(g for g in p["passages"] if g["via"] is None)
    assert any(n in g["text"] for n in names) and len(p["passages"]) <= relations.MAX_PASSAGES + relations.PAIR_PASSAGES
    req = relations.requests([p], chars)[0][0]
    assert req["pairs"][0]["passages"] and not req["pairs"][0]["passages"][0]["text"].startswith("〔")


def test_pair_passages_come_only_from_what_was_read(conn, cfg, fixtures):
    """An afterword (book matter, never read) and the chapters a sample run has not reached are not evidence: an
    afterword's 「二人が夫婦となり」 made a couple of two the story calls 「夫婦じゃなくて婚約者」."""
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    pairs, _ = relations.candidate_pairs(conn, book.id)
    used = {g["id"] for p in pairs for g in p["passages"]}
    assert used
    scene_of = {r[0]: r[1] for r in conn.execute("SELECT id, scene_id FROM units WHERE book_id=?", (book.id,))}
    unread = scene_of[min(used, key=lambda u: -conn.execute("SELECT ordinal FROM units WHERE id=?", (u,)).fetchone()[0])]
    conn.execute("UPDATE scenes SET version=0 WHERE book_id=? AND id=?", (book.id, unread))  # as if not read
    pairs, _ = relations.candidate_pairs(conn, book.id)
    again = {g["id"] for p in pairs for g in p["passages"]}
    assert not {u for u in again if scene_of[u] == unread}


def test_people_page_quotes_come_with_their_translation(conn, cfg, fixtures):
    from ncte.gui import data

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    chars = data.characters(conn, book)["characters"]
    facts = [f for c in chars.values() for f in c["facts"] if f["quote"]]
    assert facts and all(f["quote_zh"] for f in facts)  # each source quote shown with its paragraph's translation
    assert all(isinstance(a, dict) and "ja" in a for c in chars.values() for a in c["aka"])


def test_impression_says_how_much_of_the_book_was_read(conn, cfg, fixtures):
    """An impression from a sample is labelled with where the reading stopped, not as the whole book's."""
    from ncte.gui import data

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    imps = [c["impression"] for c in data.characters(conn, book)["characters"].values() if c.get("impression")]
    assert imps and all(i["read_all"] for i in imps)
    conn.execute("UPDATE scenes SET version=0 WHERE id=(SELECT id FROM scenes WHERE book_id=? ORDER BY seq DESC LIMIT 1)",
                 (book.id,))
    imps = [c["impression"] for c in data.characters(conn, book)["characters"].values() if c.get("impression")]
    assert imps and not imps[0]["read_all"] and imps[0]["read_to"]


def test_every_task_that_names_people_gets_their_gender_and_narrator(conn, cfg, fixtures):
    """One set of basics (gender, narrator) goes with the name to every task after reading: a self-reference chosen
    for the house style, a kana name or a 你／妳 chosen in naming, a relation label all depend on it."""
    from ncte.analysis import house_style, impressions, naming, proofread, reader
    from ncte.memory.db import utcnow

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    ids = [r[0] for r in conn.execute("SELECT id FROM characters WHERE book_id=?", (book.id,))]
    for cid in ids:
        conn.execute("INSERT OR REPLACE INTO character_gender(character_id, gender, book_id, ordinal, origin, updated_at, "
                     "certainty) VALUES (?,?,?,?,?,?,?)", (cid, "female", book.id, 0, "human", utcnow(), "explicit"))
    conn.execute("UPDATE characters SET is_narrator=1 WHERE id=?", (ids[0],))

    def people(payload):
        return [c for c in payload["characters"]]

    hs, _ = house_style.request(conn, book.id)
    imp = [r for r, _ in impressions.requests(conn, book.id)]
    nm = naming.build_naming_payload(conn, book.id)
    rd = {"characters": reader.characters(conn, book.id)}
    part = [p for ch in proofread.chunks(conn, book.id) for p in ch]
    pr = {"characters": proofread.cast(conn, book.id, part)}
    for name, payload in [("house_style", hs), *[("impressions", r) for r in imp], ("reader", rd), ("proofread", pr)]:
        assert people(payload) and all(c.get("gender") == "female" for c in people(payload)), name
    assert all(c.get("gender") == "female" for c in nm["characters"]), "naming"
    everyone = [c for p in (hs, rd, pr, *imp) for c in people(p)]
    assert any(c.get("narrator") for c in everyone)


def test_guessed_speakers_are_marked_and_the_narrator_is_named(conn, cfg, fixtures):
    """A line read as someone's (inferred) is marked where it shapes a judgment — the impression of a voice, the
    proofreader's view of who speaks; naming sees the narrator, often the one most addressed."""
    from ncte.analysis import impressions, naming, proofread

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    u = conn.execute("SELECT * FROM utterances WHERE book_id=? AND speaker_id_nullable IS NOT NULL LIMIT 1",
                     (book.id,)).fetchone()
    conn.execute("UPDATE utterances SET status='inferred' WHERE id=?", (u["id"],))
    conn.execute("UPDATE characters SET is_narrator=1 WHERE id=?", (u["speaker_id_nullable"],))
    lines = [ln for r, _ in impressions.requests(conn, book.id) for c in r["characters"] for ln in c["lines"]]
    assert any(ln.get("guess") for ln in lines) and not all(ln.get("guess") for ln in lines)
    paras = [p for ch in proofread.chunks(conn, book.id) for p in ch]
    assert any("（推測）" in p.get("speaker", "") for p in paras)
    assert any(c.get("narrator") for c in naming.build_naming_payload(conn, book.id)["characters"])


def test_a_gender_revealed_later_is_not_shown_for_an_earlier_stretch(conn, cfg, fixtures):
    """Proofreading, the fix check (which reads the proofreading cards) and the reader's look name people and their
    gender as known where the stretch is: a gender the text gives later must not judge an earlier 你."""
    from ncte.analysis import proofread, reader
    from ncte.memory.db import utcnow
    from ncte.memory.visibility import visible_names

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    u = conn.execute("SELECT u.*, x.speaker_id_nullable sp FROM utterances x JOIN units u ON u.id=x.unit_id WHERE "
                     "x.book_id=? AND x.speaker_id_nullable IS NOT NULL ORDER BY u.ordinal LIMIT 1", (book.id,)).fetchone()
    late = conn.execute("SELECT MAX(ordinal) FROM units WHERE book_id=?", (book.id,)).fetchone()[0]
    conn.execute("DELETE FROM character_gender WHERE character_id=?", (u["sp"],))
    conn.execute("INSERT INTO character_gender(character_id, gender, book_id, ordinal, origin, updated_at, certainty) "
                 "VALUES (?,?,?,?,?,?,?)", (u["sp"], "male", book.id, late, "llm", utcnow(), "explicit"))

    ja = conn.execute("SELECT canonical_name FROM characters WHERE id=?", (u["sp"],)).fetchone()[0]

    def gender_of(cards, at):
        name = visible_names(conn, book.id, at).get(u["sp"])
        return next((c.get("gender") for c in cards if c["name"] == name or c.get("ja") == ja), "missing")

    early = [{"ja": u["plain_text"], "_who": [u["sp"]], "_at": u["ordinal"]}]
    assert gender_of(proofread.cast(conn, book.id, early), u["ordinal"]) is None
    assert gender_of(reader.characters(conn, book.id, u["ordinal"]), u["ordinal"]) in (None, "missing")  # not known yet
    after = [{"ja": u["plain_text"], "_who": [u["sp"]], "_at": late + 1}]
    assert gender_of(proofread.cast(conn, book.id, after), late + 1) == "male"
    assert gender_of(reader.characters(conn, book.id, late + 1), late + 1) == "male"


def test_an_impression_in_stretches_gives_each_scene_the_voice_of_its_time(conn, cfg, fixtures):
    """ADR-060: a character whose way of speaking changes has two stretches; a scene before the change gets the first,
    one after it the second; personality and role wait for the evidence they rest on; proofreading lists the card once."""
    from ncte.analysis import impressions, proofread
    from ncte.llm.schemas import ImpressionsOutput
    from ncte.memory.repository import impression_at

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    req, lk = impressions.requests(conn, book.id)[0]
    c = next(x for x in req["characters"] if len(x["lines"]) >= 2)
    early, late = c["lines"][0], c["lines"][-1]
    parsed = ImpressionsOutput.model_validate({"impressions": [
        {"ref": c["ref"], "personality": "early", "voice": "冷淡", "role": "r1", "evidence": [early["ref"]]},
        {"ref": c["ref"], "personality": "late", "voice": "開朗", "role": "r2", "evidence": [late["ref"]],
         "from": late["ref"]}]})
    impressions.apply(conn, book.id, [(parsed, lk)])
    cid = lk["_chars"][c["ref"]]
    assert impression_at(conn, cid, early["at"])["voice"] == "冷淡"
    assert impression_at(conn, cid, late["at"])["voice"] == "開朗"
    assert "personality" not in impression_at(conn, cid, late["at"] - 1) or impression_at(conn, cid, late["at"] - 1)["voice"] == "冷淡"
    part = [{"ja": early["text"], "_who": [cid], "_at": early["at"]}]
    cards = [x for x in proofread.cast(conn, book.id, part) if x.get("ja") == conn.execute(
        "SELECT canonical_name FROM characters WHERE id=?", (cid,)).fetchone()[0]]
    assert len(cards) == 1 and cards[0].get("voice") == "冷淡"
