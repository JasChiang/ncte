"""A small local HTTP server for the web UI.

Only for the person at this computer: it binds to 127.0.0.1, accepts only ``Host: 127.0.0.1/localhost`` (no DNS
rebinding) and requires a per-start random token on every API call (another web page in the same browser cannot
read or change the workspace). Requests are handled one at a time, so the SQLite connections need no locking.
"""

from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..errors import NCTEError
from . import actions, data

STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}


def _static(name: str) -> bytes:
    return resources.files("ncte.gui").joinpath("static", name).read_bytes()


class App:
    def __init__(self, ws: Any, token: str | None = None):
        self.ws = ws
        self.token = token or secrets.token_urlsafe(24)

    def book(self, ref: str) -> tuple[Any, Any]:
        return self.ws.open_book(ref)

    def get(self, path: str, q: dict[str, str]) -> Any:
        parts = [p for p in path.split("/") if p][1:]  # drop "api"
        if parts == ["library"]:
            return data.library(self.ws)
        if len(parts) >= 3 and parts[0] == "book":
            conn, book = self.book(parts[1])
            what = parts[2]
            if what == "overview":
                return data.overview(conn, book)
            if what == "chapters":
                return data.chapters(conn, book)
            if what == "units":
                s, e = q.get("start"), q.get("end")
                return data.units(conn, book, start=int(s) if s else None, end=int(e) if e else None,
                                  filt=q.get("filter", "all"))
            if what == "story":
                return data.story(conn, book)
            if what == "characters":
                return data.characters(conn, book, series=q.get("series") == "1")
            if what == "glossary":
                return data.glossary(conn, book)
            if what == "proposals":
                return data.proposals(conn, book, include_done=q.get("all") == "1")
        raise KeyError(path)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        parts = [p for p in path.split("/") if p][1:]
        if len(parts) >= 4 and parts[0] == "book":
            conn, book = self.book(parts[1])
            kind, ident = parts[2], parts[3]
            op = parts[4] if len(parts) > 4 else ""
            if kind == "units" and op == "edit":
                return actions.edit_unit(conn, book, ident, str(body.get("text", "")), pre=str(body.get("pre", "")),
                                         post=str(body.get("post", "")), reason=str(body.get("reason", "")))
            if kind == "units" and op == "revert":
                return actions.revert_unit(conn, book, ident)
            if kind == "units" and op == "drop-note":
                return actions.drop_note(conn, book, ident)
            if kind == "proposals" and ident == "accept-all":
                return actions.accept_all(conn, book)
            if kind == "proposals" and op == "apply":
                return actions.apply_rename(conn, book, int(ident), str(body.get("old", "")), str(body.get("new", "")))
            if kind == "proposals" and op == "preview":
                return actions.rename_preview(conn, book, int(ident), str(body.get("old", "")))
            if kind == "characters" and op == "gender":
                return actions.set_gender(conn, book, ident, str(body.get("gender", "")))
            if kind == "proposals" and op == "decide":
                return actions.decide(conn, book, int(ident), str(body.get("zh", "")))
        raise KeyError(path)


def make_handler(app: App, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "ncte-gui"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the terminal quiet
            return

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _guard(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(403, {"error": "forbidden host"})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            url = urlparse(self.path)
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                body = _static(name)
                if name == "index.html":
                    body = body.replace(b"__NCTE_TOKEN__", app.token.encode())
                self._send(200, body, ctype)
                return
            if not url.path.startswith("/api/"):
                self._json(404, {"error": "not found"})
                return
            self._api(lambda: app.get(url.path, {k: v[0] for k, v in parse_qs(url.query).items()}))

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            url = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                self._json(400, {"error": "invalid JSON"})
                return
            self._api(lambda: app.post(url.path, body))

        def _api(self, fn: Any) -> None:
            if not secrets.compare_digest(self.headers.get("X-NCTE-Token", ""), app.token):
                self._json(403, {"error": "missing or wrong token"})
                return
            try:
                self._json(200, fn())
            except KeyError as exc:
                self._json(404, {"error": f"not found: {exc}"})
            except (NCTEError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # report instead of dropping the connection
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(ws: Any, *, port: int = 8780, open_browser: bool = True) -> None:
    app = App(ws)
    httpd = HTTPServer(("127.0.0.1", port), make_handler(app, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"NCTE GUI: {url}  (only this computer can open it; Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
