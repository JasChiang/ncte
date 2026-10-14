"""Local web UI: API, safety checks (host, token) and writes."""

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from ncte.config import load_config
from ncte.gui.server import App, make_handler
from ncte.llm.fake import FakeLLM
from ncte.workspace import Workspace

from ..scripted import RESPONDERS


@pytest.fixture
def gui(tmp_path, fixtures):
    from ncte.translation.context_pipeline import ContextOrchestrator
    from ncte.translation.orchestrator import RunOptions, create_run

    cfg = load_config(workspace=str(tmp_path / "ws"))
    ws = Workspace(cfg)
    conn, book, _ = ws.import_book(fixtures["basic"])
    opts = RunOptions(mode="context", model=cfg.openrouter.model, client="fake")
    rid = create_run(conn, cfg, book, opts)
    ContextOrchestrator(conn, cfg, FakeLLM(responders=RESPONDERS), book, rid, opts, log=lambda m: None).run()
    app = App(ws, token="t0k")
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(app, port)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()

    def call(path, body=None, token="t0k", host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        req.add_header("X-NCTE-Token", token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call, book, conn
    httpd.shutdown()
    httpd.server_close()


def test_page_carries_the_token_and_api_needs_it(gui):
    call, book, _ = gui
    status, page = call("/")
    assert status == 200 and b"t0k" in page
    assert call("/api/library", token="wrong")[0] == 403
    assert call("/api/library", host="evil.example:80")[0] == 403  # DNS rebinding
    status, lib = call("/api/library")
    assert status == 200 and lib[0]["volumes"][0]["book_id"] == book.id


def test_reading_endpoints(gui):
    call, book, _ = gui
    for what in ("overview", "chapters", "story", "characters", "glossary", "proposals"):
        status, data = call(f"/api/book/{book.id}/{what}")
        assert status == 200, (what, data)
    status, data = call(f"/api/book/{book.id}/units?filter=all")
    assert status == 200 and data["units"] and {"ja", "zh", "quotes", "findings"} <= set(data["units"][0])


def test_edit_and_revert_a_unit(gui):
    call, book, conn = gui
    u = call(f"/api/book/{book.id}/units?filter=all")[1]["units"][3]
    status, r = call(f"/api/book/{book.id}/units/{u['id']}/edit", {"text": "人工的譯文", "pre": u["edit_pre"],
                                                                  "post": u["edit_post"]})
    assert status == 200 and r["ok"], r
    again = next(x for x in call(f"/api/book/{book.id}/units?filter=human")[1]["units"] if x["id"] == u["id"])
    assert again["status"] == "human" and again["zh"] == "人工的譯文"
    assert call(f"/api/book/{book.id}/units/{u['id']}/revert", {})[0] == 200
    assert call(f"/api/book/{book.id}/units?filter=human")[1]["units"] == []
    assert call(f"/api/book/{book.id}/units/{u['id']}/edit", {"text": "  "})[0] == 400


def test_split_edges():
    from ncte.gui.data import split_edges

    assert split_edges("⟦1⟧　好⟦2/⟧啊⟦/1⟧") == ("⟦1⟧", "　好⟦2/⟧啊", "⟦/1⟧")
    assert split_edges("沒有標記") == ("", "沒有標記", "")
