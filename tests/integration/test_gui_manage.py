"""Web UI for people who do not use the command line: upload, translate, pause/continue, export, API key (ADR-034)."""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from urllib.parse import quote

import pytest

from ncte.config import load_config
from ncte.gui.runner import Runner, budget_for
from ncte.gui.server import App, make_handler
from ncte.llm.fake import FakeLLM
from ncte.workspace import Workspace

from ..scripted import RESPONDERS


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "a" * 40)  # restored after the test
    cfg = load_config(workspace=str(tmp_path / "ws"))
    gate = {"event": None, "calls": 0}

    def analyze(payload, req):
        gate["calls"] += 1
        if gate["event"] is not None:
            gate["event"].wait(10)
        return RESPONDERS["scene_analyze"](payload, req)

    fake = FakeLLM(models=[cfg.openrouter.model], responders={**RESPONDERS, "scene_analyze": analyze})
    ws = Workspace(cfg)
    app = App(ws, token="t0k", runner=Runner(cfg, lambda c: fake, client="fake"), env_path=tmp_path / ".env")
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     method="GET" if data is None else "POST")
        req.add_header("X-NCTE-Token", "t0k")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                out = r.read()
                return r.status, (json.loads(out) if r.headers.get_content_type() == "application/json" else out)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call, app, gate, tmp_path
    gate["event"] and gate["event"].set()
    httpd.shutdown()
    httpd.server_close()


def _upload_and_import(call, path):
    st, up = call("/api/upload", raw=path.read_bytes(), headers={"X-Filename": quote(path.name)})
    assert st == 200, up
    st, imp = call("/api/import", {"upload": up["upload"], "series": None, "volume": None})
    assert st == 200, imp
    return up, imp


def _wait(call, book_id, until=lambda p: not p["busy"]):
    for _ in range(200):
        p = call(f"/api/book/{book_id}/run")[1]
        if p["run"] and until(p):
            return p
        time.sleep(0.05)
    raise AssertionError("run did not settle")


def test_upload_import_translate_export_download(site, fixtures):
    call, app, _, _ = site
    up, imp = _upload_and_import(call, fixtures["basic"])
    assert up["title"] and up["imported_as"] is None and imp["units"] > 0
    assert call("/api/upload", raw=fixtures["basic"].read_bytes(),
                headers={"X-Filename": quote(fixtures["basic"].name)})[1]["imported_as"] == imp["book_id"]
    assert call("/api/upload", raw=b"not a zip", headers={"X-Filename": "x.epub"})[0] == 400
    assert call("/api/upload", raw=b"x", headers={"X-Filename": "x.pdf"})[0] == 400

    bid = imp["book_id"]
    est = call(f"/api/book/{bid}/estimate", {})[1]
    assert est["units"] > 0 and est["budget_usd"] == budget_for(est["high_usd"]) and est["high_usd"] >= est["estimate_usd"]
    assert call(f"/api/book/{bid}/run")[1]["run"] is None
    st, r = call(f"/api/book/{bid}/start", {})
    assert st == 200
    p = _wait(call, bid)
    assert p["run"]["status"] == "completed", p
    assert p["run"]["budget_usd"] == est["budget_usd"]  # the budget was set from the estimate, not asked
    assert [s["key"] for s in p["stages"]][:2] == ["read", "verify"] and all(s["done"] == s["total"] for s in p["stages"])

    st, x = call(f"/api/book/{bid}/export", {})
    assert st == 200 and x["path"].endswith(".zh-TW.epub")
    st, body = call(f"/api/book/{bid}/download")
    assert st == 200 and body[:2] == b"PK"


def test_pause_and_continue(site, fixtures):
    call, app, gate, _ = site
    _, imp = _upload_and_import(call, fixtures["basic"])
    bid = imp["book_id"]
    gate["event"] = threading.Event()
    call(f"/api/book/{bid}/start", {})
    for _ in range(100):
        if gate["calls"]:
            break
        time.sleep(0.02)
    assert call(f"/api/book/{bid}/start", {})[0] == 400  # one run at a time
    assert call("/api/run/pause", {})[1] == {"pausing": True}
    gate["event"].set()
    p = _wait(call, bid)
    assert p["run"]["status"] == "interrupted"
    assert any(s["done"] < s["total"] for s in p["stages"])  # stopped after the request in flight
    call(f"/api/run/{p['run']['id']}/continue", {"budget": 3})
    p = _wait(call, bid)
    assert p["run"]["status"] == "completed" and p["run"]["budget_usd"] == 3


def test_api_key_is_stored_locally_and_never_sent_back(site):
    call, app, _, tmp = site
    assert call("/api/settings/key", {"key": "not-a-key"})[0] == 400
    key = "sk-or-v1-" + "b" * 40
    st, out = call("/api/settings/key", {"key": key})
    assert st == 200 and out["set"] and out["hint"] == "…bbbb"
    assert key not in json.dumps(out) and key not in json.dumps(call("/api/settings")[1])
    env = (tmp / ".env").read_text()
    assert f"OPENROUTER_API_KEY={key}" in env and env.count("OPENROUTER_API_KEY=") == 1
    assert oct((tmp / ".env").stat().st_mode)[-3:] == "600"


def test_fal_settings_preserve_direct_key_and_resume_config(site, fixtures, monkeypatch):
    from ncte.config import Config
    from ncte.translation.orchestrator import RunOptions, create_run, load_run

    call, app, _, tmp = site
    # Ensure environment mutations performed by the local server are restored by pytest.
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("FAL_KEY", "initial-test-fal-key")
    direct = "sk-or-v1-" + "c" * 40
    assert call("/api/settings/key", {"key": direct})[0] == 200
    assert call("/api/settings/provider", {"provider": "fal"})[1]["provider"] == "fal"
    key = "test-fal-id:test-fal-secret"
    assert call("/api/settings/key", {"key": key})[0] == 200
    settings = call("/api/settings")[1]
    assert settings["set"] and settings["provider"] == "fal" and key not in json.dumps(settings)
    assert f"OPENROUTER_API_KEY={direct}" in (tmp / ".env").read_text()
    assert call("/api/settings/key", {"key": "injected\nOTHER=value"})[0] == 400
    assert call("/api/settings/provider", {"provider": "invalid"})[0] == 400
    _, imp = _upload_and_import(call, fixtures["basic"])
    conn, book = app.ws.open_book(imp["book_id"])
    cfg = Config()
    cfg.openrouter.provider = "fal"
    rid = create_run(conn, cfg, book, RunOptions(model=cfg.openrouter.model))
    assert call("/api/settings/provider", {"provider": "openrouter"})[0] == 200
    _, saved, opts = load_run(conn, rid)
    assert saved.openrouter.provider == opts.client == "fal"
    assert call("/api/settings")[1]["hint"] == "…cccc"


def test_page_does_not_continue_a_run_the_cli_is_working_on(site, fixtures):
    """fal real run: the page offered「繼續」for a run the CLI was still working on, and both sent the same requests."""
    from ncte.translation.orchestrator import RunOptions, create_run
    from ncte.translation.run_lock import SeriesLock

    call, app, _, _ = site
    _, imp = _upload_and_import(call, fixtures["basic"])
    bid = imp["book_id"]
    conn, book = app.ws.open_book(bid)
    rid = create_run(conn, app.ws.cfg, book, RunOptions(mode="context", model=app.ws.cfg.openrouter.model, client="fake"))
    conn.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
    cli = SeriesLock(conn)  # stands in for the CLI process holding the series
    cli.acquire(rid, "cli")
    try:
        p = call(f"/api/book/{bid}/run")[1]
        assert p["run"]["status"] == "running_elsewhere" and p["elsewhere"]["source"] == "指令列"
        st, err = call(f"/api/run/{rid}/continue", {})
        assert st == 400 and "指令列" in err["error"]
        assert call(f"/api/book/{bid}/start", {})[0] == 400
    finally:
        cli.release()
    assert call(f"/api/book/{bid}/run")[1]["run"]["status"] == "interrupted"  # the CLI ended: resumable
