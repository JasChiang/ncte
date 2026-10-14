"""ADR-020: series of several volumes share knowledge; story position = (volume, ordinal)."""

import json

from typer.testing import CliRunner

from ncte.cli import app
from ncte.library import import_book
from ncte.llm.fake import FakeLLM, default_scene_analyze
from ncte.memory import visibility as vis
from ncte.memory.db import connect, utcnow
from ncte.samples import BookSpec, Doc, _section, write_book
from ncte.series import ensure_series
from ncte.translation.orchestrator import RunOptions, create_run

from ..helpers import orchestrator_for
from ..scripted import RESPONDERS

VOL2 = ["　あれから一年が過ぎた。", "「ミナ、元気だった？」", "　王女リリアは微笑んだ。", "「はい、リリア様」",
        "　ミナは深く頭を下げた。"]


def vol2_epub(tmp_path):
    spec = BookSpec("銀の鍵と王女２", "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-000000000022",
                    [Doc("v2", "Text/v2.xhtml", "第一章　再会", _section("第一章　再会", VOL2, None, "v2"))])
    return write_book(spec, tmp_path / "vol2.epub")


def series_of_two(conn, cfg, fixtures, tmp_path):
    sid = ensure_series(conn, "銀の鍵シリーズ", series_id="sr_test")
    b1, _ = import_book(conn, cfg, fixtures["basic"], series_id=sid, volume=1)
    b2, _ = import_book(conn, cfg, vol2_epub(tmp_path), series_id=sid, volume=2)
    return b1, b2


def names_in_scene(payload, req):
    out = default_scene_analyze(payload, req)
    known = {c["name_ja"] for c in payload["known_characters"]}
    for i, n in enumerate(["ミナ", "カイト", "リリア"]):
        ev = [u["unit_id"] for u in payload["units"] if n in u["source"]]
        if ev and n not in known:
            out["new_characters"].append({"temp_id": f"new_{i}", "name_ja": n, "aliases_ja": [],
                                          "proposed_name_zh": {"ミナ": "米娜", "カイト": "海斗", "リリア": "莉莉亞"}[n],
                                          "evidence_unit_ids": ev[:1]})
    return out


def run(conn, cfg, book, fake, **kw):
    opts = RunOptions(model=cfg.openrouter.model, client="fake", mode="context", **kw)
    rid = create_run(conn, cfg, book, opts)
    return orchestrator_for(conn, cfg, book, rid, opts, fake).run()


def test_visibility_uses_story_position(conn, cfg, fixtures, tmp_path):
    b1, b2 = series_of_two(conn, cfg, fixtures, tmp_path)
    u1 = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1 OFFSET 5", (b1.id,)).fetchone()
    u2 = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? ORDER BY ordinal LIMIT 1 OFFSET 2", (b2.id,)).fetchone()
    for fid, book, u in (("f_v1", b1.id, u1), ("f_v2", b2.id, u2)):
        conn.execute("INSERT INTO facts(id, book_id, subject, predicate, object, fact_type, certainty, fact_json, "
                     "evidence_unit_ids_json, evidence_quote, first_revealed_unit_id, first_revealed_ordinal, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (fid, book, "s", "p", "o", "objective", "explicit", "{}", "[]", "q", u["id"], u["ordinal"], utcnow()))
    # volume 2, first unit: sees volume 1's fact, not its own later one
    assert [f["id"] for f in vis.visible_facts(conn, b2.id, 0)] == ["f_v1"]
    # volume 1, last unit: never sees volume 2
    assert [f["id"] for f in vis.visible_facts(conn, b1.id, 10_000)] == ["f_v1"]
    assert {f["id"] for f in vis.visible_facts(conn, b2.id, 10_000)} == {"f_v1", "f_v2"}


def test_later_volume_translated_first_does_not_spoil_earlier_one(conn, cfg, fixtures, tmp_path):
    b1, b2 = series_of_two(conn, cfg, fixtures, tmp_path)
    responders = {**RESPONDERS, "scene_analyze": names_in_scene}
    s2 = run(conn, cfg, b2, FakeLLM(responders=responders))  # the user starts with volume 2
    assert s2.status == "completed"
    lilia = conn.execute("SELECT * FROM characters WHERE canonical_name='リリア'").fetchone()
    assert lilia["book_id"] == b2.id
    fake = FakeLLM(responders=responders)
    s1 = run(conn, cfg, b1, fake)
    assert s1.status == "completed"
    ch1 = [p for p, c in zip(fake.payloads, fake.calls, strict=True)
           if c.stage == "translate" and any("朝の市場" in u["source"] or "遅いですよ" in u["source"] for u in p["units"])]
    blob = json.dumps(ch1, ensure_ascii=False)
    assert "リリア" not in blob and "莉莉亞" not in blob  # volume-2 character invisible in volume 1, chapter 1
    # but renderings decided while translating volume 2 are reused for names that occur in volume 1's text
    gl = {g["ja"]: g["zh"] for p in ch1 for g in p["glossary"]}
    assert gl.get("ミナ") == "米娜"
    # one character row per name for the whole series; its first appearance moved back to volume 1
    rows = conn.execute("SELECT book_id FROM characters WHERE canonical_name='ミナ'").fetchall()
    assert [r[0] for r in rows] == [b1.id]


def test_volume_two_sees_volume_one_memory(conn, cfg, fixtures, tmp_path):
    b1, b2 = series_of_two(conn, cfg, fixtures, tmp_path)
    run(conn, cfg, b1, FakeLLM(responders=RESPONDERS))
    fake = FakeLLM(responders=RESPONDERS)
    run(conn, cfg, b2, fake)
    ps = [p for p, c in zip(fake.payloads, fake.calls, strict=True) if c.stage == "translate" and "memory" in p]
    # a volume-1 fact (the chapter-3 reveal) reaches the volume-2 batch it relates to; facts unrelated to the
    # batch's text (the key handed over in chapter 1) are not sent just because a character is present
    blob = json.dumps([p["memory"] for p in ps], ensure_ascii=False)
    assert "真實身分是" in blob and "把銀之鑰交給" not in blob
    assert any(c["name_ja"] == "ミナ" for c in ps[0]["characters"])


def test_cli_series_folders_and_legacy_migration(tmp_path, fixtures):
    ws = tmp_path / "ws"
    r = CliRunner()
    # a legacy single-database workspace with one translated book
    from ncte.config import Config

    cfg = Config()
    cfg.app.workspace = str(ws)
    legacy = connect(ws / "ncte.db")
    import_book(legacy, cfg, fixtures["ruby"])
    legacy.close()
    out = r.invoke(app, ["-w", str(ws), "status", str(fixtures["ruby"])])
    assert out.exit_code == 0, out.output
    assert not (ws / "ncte.db").exists() and list(ws.glob("ncte.legacy-*.db"))
    # new books: explicit series and volumes, one folder + database per series
    assert r.invoke(app, ["-w", str(ws), "import", str(fixtures["basic"]), "--series", "銀の鍵シリーズ",
                          "--volume", "1"]).exit_code == 0
    v2 = vol2_epub(tmp_path)
    out = r.invoke(app, ["-w", str(ws), "import", str(v2), "--series", "銀の鍵シリーズ", "--label", "第2集"])
    assert out.exit_code == 0 and "volume 2" in out.output, out.output
    clash = r.invoke(app, ["-w", str(ws), "import", str(fixtures["media"]), "--series", "銀の鍵シリーズ", "--volume", "2"])
    assert clash.exit_code != 0
    data = json.loads(r.invoke(app, ["-w", str(ws), "series", "list", "--json"]).output)
    series = {s["title"]: s for s in data}
    assert [v["volume_index"] for v in series["銀の鍵シリーズ"]["volumes"]] == [1.0, 2.0]
    assert len(list((ws / "series").glob("*/ncte.db"))) == 2  # legacy book's series + the new series
    # run ids are found across series databases
    out = r.invoke(app, ["-w", str(ws), "translate", str(v2), "--fake", "--json"])
    run_id = json.loads(out.output[out.output.index("{"):])["run_id"]
    assert r.invoke(app, ["-w", str(ws), "report", "--run-id", run_id, "--format", "json"]).exit_code == 0
    assert r.invoke(app, ["-w", str(ws), "series", "set-volume", str(v2), "3"]).exit_code == 0


def test_move_book_into_another_series(tmp_path, fixtures):
    from ncte.config import Config
    from ncte.workspace import Workspace, move_book

    cfg = Config()
    cfg.app.workspace = str(tmp_path / "ws")
    ws = Workspace(cfg)
    responders = {**RESPONDERS, "scene_analyze": names_in_scene}
    conn1, b1, _ = ws.import_book(fixtures["basic"])  # standalone series
    conn2, b2, _ = ws.import_book(vol2_epub(tmp_path))  # imported as another standalone book by mistake
    run(conn1, cfg, b1, FakeLLM(responders=responders))
    run(conn2, cfg, b2, FakeLLM(responders=responders))
    n_tr = conn2.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
    assert n_tr > 0
    old_folder = ws.series_dir(ws.series_row(ws.find_book(b2.id)[0])["slug"])
    target = ws.series_row(ws.find_book(b1.id)[0])["title"]
    stats = move_book(ws, b2.id, target, volume=2, label="第2集")
    assert stats["characters_merged"] >= 1 and stats["glossary_merged"] >= 1
    sid, _ = ws.find_book(b2.id)
    assert sid == ws.find_book(b1.id)[0]
    dst = ws.series_conn(sid)
    assert dst.execute("SELECT COUNT(*) FROM translations t JOIN units u ON u.id=t.unit_id WHERE u.book_id=?",
                       (b2.id,)).fetchone()[0] == n_tr
    assert [r[0] for r in dst.execute("SELECT book_id FROM characters WHERE canonical_name='ミナ'")] == [b1.id]
    assert dst.execute("SELECT volume_index, volume_label FROM books WHERE id=?", (b2.id,)).fetchone()[:] == (2.0, "第2集")
    assert not old_folder.exists() and list(old_folder.parent.glob(old_folder.name + ".moved-*"))
    # the moved volume now sees volume 1's memory and still exports
    from ncte.exporter import export_book
    from ncte.library import resolve_book

    out = export_book(dst, cfg, resolve_book(dst, b2.id), run_epubcheck=False)
    assert out.validation.ok and out.stats["untranslated_kept_original"] == 0
    assert len(vis.visible_facts(dst, b2.id, 0)) > 0
