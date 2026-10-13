import json

from typer.testing import CliRunner

from ncte.cli import app

runner = CliRunner()


def invoke(ws, *args):
    r = runner.invoke(app, ["-w", str(ws), *args])
    return r


def test_memory_cli_flow(tmp_path, fixtures):
    ws = tmp_path / "ws"
    epub = str(fixtures["basic"])
    assert invoke(ws, "translate", epub, "--mode", "context", "--fake").exit_code == 0
    r = invoke(ws, "scenes", epub, "--json")
    assert len(json.loads(r.output)) == 4
    first_unit = json.loads(r.output)[0]["start_unit_id"]
    assert invoke(ws, "characters", epub).exit_code == 0
    r = invoke(ws, "speakers", epub, "--json")
    utts = json.loads(r.output)
    assert utts and all(u["status"] in ("explicit", "inferred", "unknown") for u in utts)
    r = invoke(ws, "memory", epub, "--search", "手渡した", "--at", first_unit, "--json")
    assert json.loads(r.output)["search"] == []
    r = invoke(ws, "memory", epub, "--search", "手渡した", "--json")
    assert json.loads(r.output)["search"]
    # glossary change → impact listing
    r = invoke(ws, "glossary", "set", epub, "銀の鍵", "白銀之鑰", "--category", "item")
    assert r.exit_code == 0 and "affected units: 6, translations not using the new term: 6" in r.output
    r = invoke(ws, "glossary", "impact", epub, "銀の鍵", "--json")
    assert all(not x["uses_current_target"] for x in json.loads(r.output))
    r = invoke(ws, "glossary", "list", epub, "--json")
    assert any(g["source_term"] == "銀の鍵" and g["verified"] for g in json.loads(r.output))
    # human speaker confirmation
    r = invoke(ws, "speaker-set", epub, "--utterance-id", utts[0]["utterance_id"], "--unknown", "--reason", "test")
    assert r.exit_code == 0, r.output
    r = invoke(ws, "speakers", epub, "--status", "human_verified", "--json")
    assert len(json.loads(r.output)) == 1


def test_glossary_change_invalidates_only_affected_units(tmp_path, fixtures):
    ws = tmp_path / "ws"
    epub = str(fixtures["basic"])
    invoke(ws, "translate", epub, "--fake")
    invoke(ws, "glossary", "set", epub, "ミナ", "米娜", "--category", "character")
    r = invoke(ws, "translate", epub, "--fake", "--json")
    out = json.loads(r.output[r.output.index("{"):])
    import sqlite3

    db = sqlite3.connect(next((ws / "series").glob("*/ncte.db")))
    n_mina, total = db.execute("SELECT SUM(plain_text LIKE '%ミナ%'), COUNT(*) FROM units WHERE translatable=1").fetchone()
    assert n_mina >= 3
    assert out["counts"] == {"succeeded": n_mina, "skipped": total - n_mina}
