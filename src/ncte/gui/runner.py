"""Translation runs started from the web UI (ADR-034).

One run at a time, in a background thread with its own database connections (the server keeps answering while it
works). The person chooses nothing technical: the configured model and reasoning effort are used, and the budget is
set automatically from the dry-run estimate (×1.5) as a guard against runaway cost — when it is reached the page asks
whether to continue, showing what was spent and what is still left.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from ..config import Config
from ..errors import NCTEError

BUDGET_FACTOR = 1.5
MIN_BUDGET_USD = 0.5
# what the person sees, in pipeline order (context mode reads the whole book before translating)
STAGES = [("read", "讀", ("scene_analyze", "memory_update")), ("verify", "覆核", ("verify",)),
          ("naming", "定名", ("naming",)), ("translate", "翻譯", ("translate",)), ("review", "審稿", ("review",))]
DONE = ("succeeded", "skipped", "needs_review", "failed")


def default_client(cfg: Config) -> Any:
    from ..cli import make_client

    return make_client(cfg)


class Runner:
    def __init__(self, cfg: Config, client_factory: Callable[[Config], Any] | None = None, *, client: str = "openrouter"):
        self.cfg = cfg
        self.client_factory = client_factory or default_client
        self.client = client  # recorded on the run ("fake" in tests)
        self._left: dict[str, float] = {}
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.orch: Any = None
        self.run_id: str | None = None
        self.book_id: str | None = None
        self.error: str | None = None
        self.log: deque[str] = deque(maxlen=200)

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    # ------------------------------------------------------------ estimate / start / continue / pause
    def options(self, budget: float) -> Any:
        from ..translation.orchestrator import RunOptions

        return RunOptions(mode="context", model=self.cfg.openrouter.model, budget_usd=budget, client=self.client)

    def estimate(self, ws: Any, ref: str) -> dict[str, Any]:
        from ..translation.dryrun import dry_run

        conn, book = ws.open_book(ref)
        history = [ws.series_conn(r[0]) for r in ws.lib.execute("SELECT id FROM series").fetchall()]
        plan = dry_run(conn, self.cfg, book, self.options(self.cfg.openrouter.per_book_budget_usd),
                       self.client_factory(self.cfg), log=lambda m: None, history=history)
        est = plan.calibrated_cost_usd if plan.calibrated_cost_usd else plan.cost_high_usd
        return {"units": plan.units_to_translate, "cached": plan.units_cached, "estimate_usd": round(est, 2),
                "calibrated": plan.calibrated_cost_usd is not None, "low_usd": round(plan.cost_low_usd, 2),
                "high_usd": round(plan.cost_high_usd, 2), "budget_usd": budget_for(est),
                "model": plan.model, "reasoning_effort": plan.reasoning_effort}

    def start(self, ws: Any, ref: str) -> str:
        from ..translation.orchestrator import create_run

        with self.lock:
            if self.busy:
                raise NCTEError("另一本書正在翻譯，請等它完成或先暫停")
            conn, book = ws.open_book(ref)
            budget = self.estimate(ws, ref)["budget_usd"]
            rid = create_run(conn, self.cfg, book, self.options(budget))
            self._launch(rid, book.id)
            return rid

    def resume(self, ws: Any, run_id: str, budget: float | None = None) -> str:
        import json

        from ..memory.db import Transaction

        with self.lock:
            if self.busy:
                raise NCTEError("另一本書正在翻譯，請等它完成或先暫停")
            conn = ws.find_run(run_id)
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if budget is not None:
                data = json.loads(row["config_json"])
                data["_run_options"]["budget_usd"] = budget
                with Transaction(conn):
                    conn.execute("UPDATE runs SET budget_usd=?, config_json=? WHERE id=?",
                                 (budget, json.dumps(data, ensure_ascii=False), run_id))
            self._left.pop(run_id, None)
            self._launch(run_id, row["book_id"])
            return run_id

    def pause(self) -> bool:
        """Stops after the request in flight (scenes and batches are never cut in half)."""
        if self.busy and self.orch is not None:
            self.orch._stop_reason = "interrupted"
            self.log.append("暫停中：等目前這個請求完成…")
            return True
        return False

    def _launch(self, run_id: str, book_id: str) -> None:
        self.run_id, self.book_id, self.error, self.orch = run_id, book_id, None, None
        self.log.clear()
        self.thread = threading.Thread(target=self._work, args=(run_id,), daemon=True, name=f"ncte-{run_id}")
        self.thread.start()

    def _work(self, run_id: str) -> None:
        from ..library import resolve_book
        from ..translation.context_pipeline import ContextOrchestrator
        from ..translation.orchestrator import Orchestrator, load_run
        from ..workspace import Workspace

        ws = Workspace(self.cfg)  # connections of its own: the server thread keeps reading
        try:
            conn = ws.find_run(run_id)
            row, cfg, opts = load_run(conn, run_id)
            cfg.app.workspace = str(self.cfg.workspace)
            cls = ContextOrchestrator if opts.mode == "context" else Orchestrator
            self.orch = cls(conn, cfg, self.client_factory(cfg), resolve_book(conn, row["book_id"]), run_id, opts,
                            log=self.log.append)
            self.orch.run()
        except Exception as exc:  # shown on the page; the run stays resumable
            from ..config import get_api_key, redact

            self.error = redact(f"{type(exc).__name__}: {exc}", get_api_key())
        finally:
            ws.close()

    # ------------------------------------------------------------ progress
    def progress(self, ws: Any, conn: Any, book_id: str) -> dict[str, Any]:
        from ..translation.orchestrator import run_spent

        row = conn.execute("SELECT * FROM runs WHERE book_id=? ORDER BY started_at DESC LIMIT 1", (book_id,)).fetchone()
        if row is None:
            return {"run": None, "busy": self.busy and self.book_id == book_id, "other_busy": self.busy}
        live = self.busy and self.run_id == row["id"]
        status = row["status"]
        if status == "running" and not live:
            status = "interrupted"  # the program was closed mid-run: resumable
        counts: dict[str, dict[str, int]] = {}
        for r in conn.execute("SELECT stage, status, COUNT(*) n FROM jobs WHERE run_id=? GROUP BY 1,2", (row["id"],)):
            counts.setdefault(r["stage"], {})[r["status"]] = r["n"]
        stages = []
        for key, label, names in STAGES:
            total = sum(n for s in names for n in counts.get(s, {}).values())
            if not total:
                continue
            done = sum(counts.get(s, {}).get(st, 0) for s in names for st in DONE)
            failed = sum(counts.get(s, {}).get("failed", 0) for s in names)
            stages.append({"key": key, "label": label, "done": done, "total": total, "failed": failed})
        spent, _ = run_spent(conn, row["id"])
        left = None
        if status == "budget_exhausted":  # what the rest would cost, from the same estimate as before the start
            if row["id"] not in self._left:
                try:
                    self._left[row["id"]] = self.estimate(ws, book_id)["estimate_usd"]
                except Exception:
                    self._left[row["id"]] = 0.0
            left = self._left[row["id"]]
        return {"run": {"id": row["id"], "status": status, "budget_usd": row["budget_usd"], "spent_usd": round(spent, 4),
                        "left_usd": left, "model": row["model"]},
                "stages": stages, "busy": live, "other_busy": self.busy and not live,
                "log": list(self.log)[-8:] if live or self.run_id == row["id"] else [],
                "error": self.error if self.run_id == row["id"] else None}


def budget_for(estimate: float) -> float:
    return round(max(MIN_BUDGET_USD, estimate * BUDGET_FACTOR), 2)

