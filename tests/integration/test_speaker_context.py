"""ADR-104: what a reader has that the reading lacked — each card's voice from all its earlier lines (and a few facts),
the scene pictured before the lines are attributed, and one look back at lines whose neighbours do not fit."""

import json
import sqlite3
from typing import Any

from ncte.analysis import lookback, verify
from ncte.analysis.scene import apply_scene_analysis, build_analysis_payload, get_scene
from ncte.llm.fake import FakeLLM
from ncte.llm.prompt_loader import load_prompt
from ncte.llm.schemas import SceneAnalysisOutput, SpeakerLookbackOutput
from ncte.memory import repository as repo
from ncte.memory.db import utcnow

from ..helpers import orchestrator_for, start_run


def _units(conn: sqlite3.Connection, book_id: str) -> dict[int, sqlite3.Row]:
    return {r["ordinal"]: r for r in conn.execute("SELECT * FROM units WHERE book_id=?", (book_id,))}


def _scenes(conn: sqlite3.Connection, book_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book_id,)).fetchall()


def _text(conn: sqlite3.Connection, u: dict[int, sqlite3.Row], ordinal: int, text: str) -> None:
    conn.execute("UPDATE units SET plain_text=? WHERE id=?", (text, u[ordinal]["id"]))


def _line(unit_id: str, speaker: str | None, status: str = "inferred", tag: str = "",
          to: list[str] | None = None) -> dict[str, Any]:
    return {"unit_id": unit_id, "qi": 0, "status": status, "speaker": speaker, "conflicts": [], "tag": tag,
            "to": to or [], "candidates": [{"ref": speaker, "types": ["dialogue_tag" if tag else "continuity"],
                                            "evidence": [unit_id], "note": ""}] if speaker else []}


def _answer(lines: list[dict[str, Any]], **extra: Any) -> SceneAnalysisOutput:
    return SceneAnalysisOutput.model_validate({"pov": None, "location": None, "time": None, "new_characters": [],
                                               "participants": [], "actions": [], "props": [], "uncertainties": [],
                                               "utterances": lines, **extra})


def _read(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row, lines: list[dict[str, Any]], **extra: Any) -> None:
    apply_scene_analysis(conn, book_id, get_scene(conn, book_id, scene["id"]), _answer(lines, **extra))


def _cards(conn: sqlite3.Connection, book_id: str, u: dict[int, sqlite3.Row]) -> tuple[str, str, str]:
    def mk(name: str) -> str:
        return repo.upsert_character(conn, book_id, name, name_zh=name, aliases=[], unit_id=u[0]["id"], ordinal=0,
                                     origin="test")[0]
    return mk("ミナ"), mk("カイト"), mk("セラ")


def _fact(conn: sqlite3.Connection, book_id: str, fid: str, subject: str, obj: str, unit: sqlite3.Row) -> None:
    conn.execute("INSERT INTO facts(id, book_id, subject, predicate, object, fact_type, certainty, fact_json, "
                 "evidence_unit_ids_json, evidence_quote, first_revealed_unit_id, first_revealed_ordinal, created_at) "
                 "VALUES (?,?,?,?,?,?,?,'{}',?,?,?,?,?)", (fid, book_id, subject, "は", obj, "profile", "explicit",
                                                           json.dumps([unit["id"]]), unit["plain_text"], unit["id"],
                                                           unit["ordinal"], utcnow()))


def _address(conn: sqlite3.Connection, book_id: str, speaker: str, addressee: str, form: str, ordinal: int) -> None:
    conn.execute("INSERT INTO address_terms(book_id, speaker_id, addressee_id, form_ja, origin, certainty, first_ordinal, "
                 "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (book_id, speaker, addressee, form, "llm", "inferred", ordinal, utcnow(), utcnow()))


def _book(conn, cfg, fixtures, client=None):
    cfg.context.speaker_lookback = True  # off by default until the A/B (ADR-104)
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], client or FakeLLM(), mode="context")
    return book, rid, opts, _units(conn, book.id), _scenes(conn, book.id)


# ------------------------------------------------------------------ 1. voice from all lines


def test_cards_carry_their_earlier_lines_forms_and_facts_from_before_the_scene_only(conn, cfg, fixtures):
    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, sera = _cards(conn, book.id, u)
    _text(conn, u, 3, "「私、遅いのは嫌いですよ」")
    _text(conn, u, 5, "「僕は寝坊したんだ。カイ兄って呼ぶなよ」")
    _read(conn, book.id, sc[0], [_line(u[3]["id"], mina, "explicit", "ミナは腕を組んで言った"),
                                 _line(u[5]["id"], kaito)])
    # a line read later in the book is not part of what the reader of the third scene remembers
    _text(conn, u, 41, "「私はリリア。その鍵は、王家の宝物庫の鍵なの」")
    _read(conn, book.id, sc[3], [_line(u[41]["id"], kaito)])
    _fact(conn, book.id, "f_early", kaito, "パン屋の子", u[9])
    _fact(conn, book.id, "f_late", kaito, "王家の騎士", u[41])
    _address(conn, book.id, mina, kaito, "カイ兄", 5)
    _address(conn, book.id, sera, kaito, "カイちゃん", 41)  # first used after the scene: not shown
    payload = build_analysis_payload(conn, book.id, get_scene(conn, book.id, sc[2]["id"]), [])
    known = {c["character_id"]: c for c in payload["known_characters"]}
    assert known[mina]["earlier_lines"] == {"lines": {"explicit": 1, "inferred": 0},
                                            "self": {"私": {"explicit": 1, "inferred": 0}}}
    k = known[kaito]
    assert k["earlier_lines"]["lines"] == {"explicit": 0, "inferred": 1}
    assert k["earlier_lines"]["self"] == {"僕": {"explicit": 0, "inferred": 1}}  # the later 私 line is not counted
    assert k["called_as"] == [{"ja": "カイ兄", "by": "ミナ"}]
    assert [f["object"] for f in k["facts_so_far"]] == ["パン屋の子"] and k["facts_so_far"][0]["source"]
    assert "earlier_lines" not in known[sera] and "facts_so_far" not in known[sera]  # nothing recorded: no key
    # the prompt says what these are (the program's tally of earlier readings, maybe wrong); without them, no rule
    static, extra = load_prompt("scene_analyze").render_parts(payload)
    assert "earlier_lines" in extra and "之前模型的推測" in extra and "earlier_lines" not in static
    plain = build_analysis_payload(conn, book.id, get_scene(conn, book.id, sc[2]["id"]), [], card_voice=False)
    assert not any(key in c for c in plain["known_characters"] for key in ("earlier_lines", "called_as", "facts_so_far"))
    assert "earlier_lines" not in load_prompt("scene_analyze").render_parts(plain)[1]


# ------------------------------------------------------------------ 2. the scene pictured first


def test_the_scene_state_is_parsed_checked_stored_and_shown_to_the_next_scene(conn, cfg, fixtures):
    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, _ = _cards(conn, book.id, u)
    payload = build_analysis_payload(conn, book.id, get_scene(conn, book.id, sc[0]["id"]), [], scene_state=True)
    assert payload["write_situation"] is True
    for task in ("scene_analyze", "scene_read"):
        _, extra = load_prompt(task).render_parts(payload)
        assert "`situation`" in extra and "先寫好再判斷說話者" in extra
    assert "write_situation" not in build_analysis_payload(conn, book.id, get_scene(conn, book.id, sc[0]["id"]), [])
    situation = [{"at": u[9]["id"], "place": "", "people": [{"ref": kaito, "doing": "把銀鑰匙交給ミナ"},
                                                            {"ref": mina, "doing": "接過鑰匙"}]},
                 {"at": u[2]["id"], "place": "早上的市場", "people": [{"ref": mina, "doing": "抱著手臂等"},
                                                                    {"ref": None, "who": "人群", "doing": "擠滿市場"},
                                                                    {"ref": "c_nobody", "doing": "?"}]},
                 {"at": u[20]["id"], "people": []}]  # another scene's paragraph
    _read(conn, book.id, sc[0], [_line(u[3]["id"], mina)], situation=situation)
    state = json.loads(get_scene(conn, book.id, sc[0]["id"])["state_json"])
    assert [p["from"] for p in state["situation"]] == [u[2]["id"], u[9]["id"]]  # story order; outside dropped
    assert state["situation"][0] == {"from": u[2]["id"], "place": "早上的市場", "people": [
        {"character_id": mina, "doing": "抱著手臂等"}, {"who": "人群", "doing": "擠滿市場"}]}
    reasons = [r[0] for r in conn.execute("SELECT reason FROM memory_rejections")]
    assert "unknown situation ref" in reasons and "situation point at a paragraph outside the scene" in reasons
    # the next scene opens knowing how this one was left; the second look sees the picture at its line
    nxt = build_analysis_payload(conn, book.id, get_scene(conn, book.id, sc[1]["id"]), [], scene_state=True)
    assert nxt["previous_scene_state"] == {"people": [{"ref": kaito, "doing": "把銀鑰匙交給ミナ"},
                                                      {"ref": mina, "doing": "接過鑰匙"}]}
    assert "previous_scene_state" in load_prompt("scene_analyze").render_parts(nxt)[1]
    conn.execute("UPDATE utterances SET notes_json=? WHERE unit_id=?", (json.dumps(["(to look at)"]), u[3]["id"]))
    item = next(i for i in verify.collect(conn, book.id, situation=True) if i["kind"] == "speaker")
    assert item["situation"]["place"] == "早上的市場" and item["situation"]["people"][0]["who"].startswith("ミナ")
    assert "`situation`" in load_prompt("verify").render_parts({"items": [item]})[1]


# ------------------------------------------------------------------ 3. look back once


def _sister_scene(conn: sqlite3.Connection, book_id: str, u: dict[int, sqlite3.Row], sc: list[sqlite3.Row],
                  first_speaker: str) -> tuple[str, str, str]:
    """The third scene: 「この地図、どこで拾ったの？」 is followed by narration naming ミナ and カイト's reply calling the
    speaker 「姉さん」 — the form recorded for セラ."""
    mina, kaito, sera = _cards(conn, book_id, u)
    who = {"mina": mina, "kaito": kaito, "sera": sera}
    _text(conn, u, 26, "「この地図、どこで拾ったの？」")
    _text(conn, u, 28, "「姉さんが拾ったんだろ」")
    _address(conn, book_id, kaito, sera, "姉さん", 5)
    _read(conn, book_id, sc[2], [_line(u[26]["id"], who[first_speaker]), _line(u[28]["id"], kaito),
                                 _line(u[29]["id"], sera), _line(u[31]["id"], kaito), _line(u[33]["id"], mina),
                                 _line(u[34]["id"], kaito), _line(u[35]["id"], mina)],
          participants=[{"character_ref": c, "presence": "explicit", "evidence_unit_ids": [u[25]["id"]]}
                        for c in (mina, kaito, sera)])
    return mina, kaito, sera


def _speaker_of(conn: sqlite3.Connection, unit_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM utterances WHERE unit_id=? AND quote_index=0", (unit_id,)).fetchone()


def test_a_reply_calling_someone_else_gathers_the_line_and_a_cited_change_is_applied(conn, cfg, fixtures):
    asked: list[dict[str, Any]] = []

    def answer(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        asked.append(payload)
        line = payload["lines"][0]
        return {"lines": [{"ref": line["ref"], "speaker": sera_id[0], "evidence": [u[28]["id"]],
                           "reason": "カイト回話叫她姉さん"}]}

    sera_id: list[str] = []
    client = FakeLLM(responders={"speaker_lookback": answer})
    book, rid, opts, u, sc = _book(conn, cfg, fixtures, client)
    mina, kaito, sera = _sister_scene(conn, book.id, u, sc, "mina")
    sera_id.append(sera)
    items = lookback.gather(conn, book.id, get_scene(conn, book.id, sc[2]["id"]))
    assert [i["unit_id"] for i in items] == [u[26]["id"]]
    assert items[0]["noticed"] == [{"in": "next_line", "word": "姉さん", "of": sera, "recorded_as": "address",
                                    "line": "「姉さんが拾ったんだろ」", "said_by": kaito}]
    assert u[28]["id"] in items[0]["_units"]
    orch = orchestrator_for(conn, cfg, book, rid, opts, client)
    orch.prepare()
    orch.stage_speaker_lookback(sc[2]["id"])
    assert len(asked) == 1 and len(asked[0]["lines"]) == 1  # one small request, only the gathered line
    assert set(asked[0]["people"]) >= {mina, kaito, sera} and "_utt" not in asked[0]["lines"][0]
    row = _speaker_of(conn, u[26]["id"])
    assert (row["speaker_id_nullable"], row["status"], row["resolution_source"]) == (sera, "inferred", "lookback")
    assert "look back" in json.loads(row["notes_json"])[-1]
    state = json.loads(get_scene(conn, book.id, sc[2]["id"])["state_json"])
    assert state["lookback"]["speakers_changed"] == 1
    orch.stage_speaker_lookback(sc[2]["id"])  # once per reading of the scene
    assert len(asked) == 1
    # the prompt for the request names what it is given and what it answers
    text = load_prompt("speaker_lookback").render(asked[0])
    assert "`noticed`" in text and "`evidence`" in text


def test_an_uncited_change_is_ignored_and_a_person_s_line_is_never_touched(conn, cfg, fixtures):
    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, sera = _sister_scene(conn, book.id, u, sc, "mina")
    scene = get_scene(conn, book.id, sc[2]["id"])
    items = lookback.gather(conn, book.id, scene)
    ref = items[0]["ref"]
    stats = lookback.apply(conn, book.id, scene, items, SpeakerLookbackOutput.model_validate(
        {"lines": [{"ref": ref, "speaker": sera, "evidence": [], "reason": "猜的"}]}))
    assert stats["uncited"] == 1 and _speaker_of(conn, u[26]["id"])["speaker_id_nullable"] == mina
    # cited, but outside the item's paragraphs, or someone not among its people: nothing changes either
    stats = lookback.apply(conn, book.id, scene, items, SpeakerLookbackOutput.model_validate(
        {"lines": [{"ref": ref, "speaker": sera, "evidence": [u[3]["id"]], "reason": ""}]}))
    assert stats["uncited"] == 1 and _speaker_of(conn, u[26]["id"])["speaker_id_nullable"] == mina
    stats = lookback.apply(conn, book.id, scene, items, SpeakerLookbackOutput.model_validate(
        {"lines": [{"ref": ref, "speaker": "c_nobody", "evidence": [u[28]["id"]], "reason": ""}]}))
    assert stats["ignored"] == 1
    conn.execute("UPDATE utterances SET status='human_verified' WHERE unit_id=?", (u[26]["id"],))
    stats = lookback.apply(conn, book.id, scene, items, SpeakerLookbackOutput.model_validate(
        {"lines": [{"ref": ref, "speaker": sera, "evidence": [u[28]["id"]], "reason": ""}]}))
    assert stats["speakers_changed"] == 0 and _speaker_of(conn, u[26]["id"])["speaker_id_nullable"] == mina
    assert lookback.gather(conn, book.id, scene) == []  # a person's line is not gathered either


def test_nothing_jars_nothing_is_sent(conn, cfg, fixtures):
    client = FakeLLM()
    book, rid, opts, u, sc = _book(conn, cfg, fixtures, client)
    _sister_scene(conn, book.id, u, sc, "sera")  # the line is already hers: the reply's 「姉さん」 fits
    assert lookback.gather(conn, book.id, get_scene(conn, book.id, sc[2]["id"])) == []
    orch = orchestrator_for(conn, cfg, book, rid, opts, client)
    orch.prepare()
    orch.stage_speaker_lookback(sc[2]["id"])
    assert not [c for c in client.calls if c.stage == "speaker_lookback"]


def test_a_self_reference_recorded_for_another_person_gathers_the_line(conn, cfg, fixtures):
    """Words matched by the record only: the line's 「僕」 is in カイト's earlier lines and never in ミナ's."""
    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, sera = _cards(conn, book.id, u)
    _text(conn, u, 3, "「私、遅いのは嫌いですよ」")
    _text(conn, u, 5, "「僕は寝坊したんだ」")
    _read(conn, book.id, sc[0], [_line(u[3]["id"], mina), _line(u[5]["id"], kaito)])
    _text(conn, u, 26, "「僕が持つよ」")
    _read(conn, book.id, sc[2], [_line(u[26]["id"], mina)],
          participants=[{"character_ref": c, "presence": "explicit", "evidence_unit_ids": [u[25]["id"]]}
                        for c in (mina, kaito)])
    items = lookback.gather(conn, book.id, get_scene(conn, book.id, sc[2]["id"]))
    assert [(i["unit_id"], i["noticed"]) for i in items] == [
        (u[26]["id"], [{"in": "this_line", "word": "僕", "of": kaito, "recorded_as": "self_reference"}])]


def test_the_switches_reach_the_requests(conn, cfg, fixtures):
    """``context.scene_state`` asks for the picture in the reading requests; ``context.speaker_lookback`` off sends no
    look back even when a line jars (both off by default until the A/B, ADR-104)."""
    from ..helpers import run_book

    assert (cfg.context.card_voice, cfg.context.scene_state, cfg.context.speaker_lookback) == (True, False, False)
    client = FakeLLM()
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    reads = [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "scene_analyze"]
    assert reads and not any("write_situation" in p for p in reads)
    assert not [c for c in client.calls if c.stage == "speaker_lookback"]
    book, rid, opts, u, sc = _book(conn, cfg, fixtures, client)
    cfg.context.speaker_lookback = False
    _sister_scene(conn, book.id, u, sc, "mina")
    assert lookback.gather(conn, book.id, get_scene(conn, book.id, sc[2]["id"]))
    orch = orchestrator_for(conn, cfg, book, rid, opts, client)
    orch.prepare()
    orch.stage_speaker_lookback(sc[2]["id"])
    assert not [c for c in client.calls if c.stage == "speaker_lookback"]


def test_a_reply_calling_whom_the_line_was_said_to_still_jars_and_so_does_a_line_calling_its_speaker(conn, cfg,
                                                                                                     fixtures):
    """The reply calls 「姉さん」 and the line (no speaker read) was taken as said to her: the one a reply calls is
    who it answers, so the line is gathered. A line with a word recorded for its own speaker is gathered as well."""
    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, sera = _cards(conn, book.id, u)
    _text(conn, u, 26, "「この地図、どこで拾ったの？」")
    _text(conn, u, 28, "「姉さんが拾ったんだろ」")
    _text(conn, u, 33, "「カイト、そろそろ帰りましょうか」")
    _address(conn, book.id, kaito, sera, "姉さん", 5)
    _read(conn, book.id, sc[2], [_line(u[26]["id"], None, "unknown", to=[sera]), _line(u[28]["id"], kaito),
                                 _line(u[29]["id"], sera), _line(u[31]["id"], kaito), _line(u[33]["id"], kaito),
                                 _line(u[34]["id"], mina), _line(u[35]["id"], kaito)],
          participants=[{"character_ref": c, "presence": "explicit", "evidence_unit_ids": [u[25]["id"]]}
                        for c in (mina, kaito, sera)])
    items = {i["unit_id"]: i for i in lookback.gather(conn, book.id, get_scene(conn, book.id, sc[2]["id"]))}
    assert [n["word"] for n in items[u[26]["id"]]["noticed"]] == ["姉さん"]
    assert items[u[33]["id"]]["noticed"][0] == {"in": "this_line", "word": "カイト", "of": kaito, "recorded_as": "name"}


def test_the_look_back_follows_merges_as_the_second_look_does(conn, cfg, fixtures):
    """An answer naming a card merged away goes to the card it went into; one gone with no chain is not written."""
    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, sera = _sister_scene(conn, book.id, u, sc, "mina")
    scene = get_scene(conn, book.id, sc[2]["id"])
    items = lookback.gather(conn, book.id, scene)
    answer = SpeakerLookbackOutput.model_validate(
        {"lines": [{"ref": items[0]["ref"], "speaker": sera, "evidence": [u[28]["id"]], "reason": ""}]})
    stats = lookback.apply(conn, book.id, scene, items, answer, went={sera: "c_not_there"})
    assert stats["unapplied"] == 1 and _speaker_of(conn, u[26]["id"])["speaker_id_nullable"] == mina
    stats = lookback.apply(conn, book.id, scene, items, answer, went={sera: kaito})
    assert stats["speakers_changed"] == 1 and _speaker_of(conn, u[26]["id"])["speaker_id_nullable"] == kaito


# ------------------------------------------------------------------ review round (ADR-104)


def _observe(conn: sqlite3.Connection, book_id: str, speaker: str, addressee: str, form: str, unit: sqlite3.Row) -> None:
    """One use of a form of address, on a line of ``unit`` (as the memory step links it)."""
    aid = conn.execute("SELECT id FROM address_terms WHERE book_id=? AND speaker_id=? AND addressee_id=? AND form_ja=?",
                       (book_id, speaker, addressee, form)).fetchone()
    if aid is None:
        _address(conn, book_id, speaker, addressee, form, unit["ordinal"])
        aid = conn.execute("SELECT max(id) FROM address_terms").fetchone()
    else:
        conn.execute("UPDATE address_terms SET uses=uses+1 WHERE id=?", (aid[0],))
    n = 90 + conn.execute("SELECT count(*) FROM utterances").fetchone()[0]
    utt = f"t_{unit['id']}_{n}"
    conn.execute("INSERT INTO utterances(id, book_id, unit_id, quote_index, quote_start, quote_end, quote_text, status, "
                 "resolution_source, updated_at) VALUES (?,?,?,?,0,1,'「x」','unknown','none',?)",
                 (utt, book_id, unit["id"], n, utcnow()))
    conn.execute("INSERT INTO address_observations(utterance_id, address_id) VALUES (?,?)", (utt, aid[0]))


def test_called_as_ranks_forms_by_their_uses_before_the_scene_only(conn, cfg, fixtures):
    """Review 1: how often a form is used later in the book does not change what an earlier scene's request shows."""
    from ncte.analysis import scene as scene_mod

    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, sera = _cards(conn, book.id, u)
    for who, form in ((mina, "カイ兄"), (sera, "カイ君"), (mina, "カイ様"), (sera, "カイ殿")):
        _observe(conn, book.id, who, kaito, form, u[5])
    for _ in range(3):
        _observe(conn, book.id, mina, kaito, "カイ様", u[5])  # three more uses before the third scene
    payload = build_analysis_payload(conn, book.id, get_scene(conn, book.id, sc[2]["id"]), [])
    before = next(c for c in payload["known_characters"] if c["character_id"] == kaito)["called_as"]
    _address(conn, book.id, sera, kaito, "カイちゃん", 6)  # first used before the scene, used again only later
    for _ in range(9):
        _observe(conn, book.id, sera, kaito, "カイちゃん", u[41])
    _observe(conn, book.id, mina, kaito, "カイ兄", u[41])
    after = next(c for c in build_analysis_payload(conn, book.id, get_scene(conn, book.id, sc[2]["id"]), [])[
        "known_characters"] if c["character_id"] == kaito)["called_as"]
    assert before[0]["ja"] == "カイ様" and len(after) == scene_mod.CARD_FORMS
    assert [x["ja"] for x in after] == [x["ja"] for x in before]  # later uses changed nothing


def test_the_second_look_shows_a_situation_only_with_the_switch_and_by_names_known_there(conn, cfg, fixtures):
    """Review 2–3: a saved picture goes to the second look only when ``scene_state`` is on, and its people are named as
    known at the picture's point (a name revealed later is not shown)."""
    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, _ = _cards(conn, book.id, u)
    hooded, _ = repo.upsert_character(conn, book.id, "フードの少女", name_zh="兜帽少女", aliases=[], unit_id=u[0]["id"],
                                      ordinal=0, origin="test")
    _read(conn, book.id, sc[0], [_line(u[3]["id"], mina)],
          situation=[{"at": u[2]["id"], "people": [{"ref": hooded, "doing": "站在市場角落"}]}])
    conn.execute("UPDATE utterances SET notes_json=? WHERE unit_id=?", (json.dumps(["(to look at)"]), u[3]["id"]))
    from ncte.memory.entities import record_link

    record_link(conn, book.id, "リリア", "リリア", "official", origin="scene_analysis", status="accepted",
                unit_id=u[40]["id"], quote=None, ordinal=40)
    conn.execute("UPDATE characters SET canonical_name='リリア', display_name_zh='莉莉亞', aliases_json=? WHERE id=?",
                 (json.dumps(["フードの少女"]), hooded))
    repo.note_forms(conn, hooded, ["リリア"], book.id, 40)  # the name is first seen with the card at the reveal
    off = next(i for i in verify.collect(conn, book.id) if i["kind"] == "speaker")
    assert "situation" not in off
    on = next(i for i in verify.collect(conn, book.id, situation=True) if i["kind"] == "speaker")
    assert "莉莉亞" not in json.dumps(on["situation"], ensure_ascii=False)
    assert "リリア" not in json.dumps(on["situation"], ensure_ascii=False)


def test_a_card_of_another_time_is_not_found_by_its_forms_of_address_either(conn, cfg, fixtures):
    """Review 4: a past life's card is left out of the look back's words whatever the word's source."""
    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, sera = _cards(conn, book.id, u)
    conn.execute("UPDATE characters SET period='前世', label='前世' WHERE id=?", (sera,))
    _text(conn, u, 26, "「この地図、どこで拾ったの？」")
    _text(conn, u, 28, "「姉さんが拾ったんだろ」")
    _address(conn, book.id, kaito, sera, "姉さん", 5)
    _read(conn, book.id, sc[2], [_line(u[26]["id"], mina), _line(u[28]["id"], kaito)],
          participants=[{"character_ref": c, "presence": "explicit", "evidence_unit_ids": [u[25]["id"]]}
                        for c in (mina, kaito)])
    assert "姉さん" not in lookback.recorded_forms(conn, book.id, get_scene(conn, book.id, sc[2]["id"]))
    assert lookback.gather(conn, book.id, get_scene(conn, book.id, sc[2]["id"])) == []


def test_a_cited_answer_never_touches_a_stated_addressee_and_a_cited_empty_list_clears_an_inferred_one(conn, cfg,
                                                                                                       fixtures):
    """Review 5–6: whom a line is said to, when the text states it (or a person set the line), stays; an answer that
    leaves ``to`` out changes nothing; a cited empty ``to`` clears an inferred one."""
    book, _, _, u, sc = _book(conn, cfg, fixtures)
    mina, kaito, sera = _sister_scene(conn, book.id, u, sc, "mina")
    scene = get_scene(conn, book.id, sc[2]["id"])
    items = lookback.gather(conn, book.id, scene)
    ref, uid = items[0]["ref"], u[26]["id"]

    def answer(**kw: Any) -> SpeakerLookbackOutput:
        return SpeakerLookbackOutput.model_validate(
            {"lines": [{"ref": ref, "speaker": mina, "evidence": [u[28]["id"]], "reason": "", **kw}]})

    def to_of() -> tuple[list[str], str | None]:
        r = _speaker_of(conn, uid)
        return json.loads(r["addressee_ids_json"] or "[]"), r["addressee_status"]

    conn.execute("UPDATE utterances SET addressee_ids_json=?, addressee_status='explicit' WHERE unit_id=?",
                 (json.dumps([kaito]), uid))
    lookback.apply(conn, book.id, scene, items, answer(to=[sera]))
    assert to_of() == ([kaito], "explicit")
    conn.execute("UPDATE utterances SET addressee_status='inferred' WHERE unit_id=?", (uid,))
    lookback.apply(conn, book.id, scene, items, answer())  # ``to`` not given: unchanged
    assert to_of() == ([kaito], "inferred")
    lookback.apply(conn, book.id, scene, items, answer(to=[]))  # cited and empty: said to no one in particular
    assert to_of() == ([], None)
