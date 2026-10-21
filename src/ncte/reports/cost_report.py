"""Cost / usage aggregation (spec §1.2-6, §11 metrics)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any


def _ts(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ") if s else None


def cost_report(conn: sqlite3.Connection, *, run_id: str | None = None, book_id: str | None = None) -> dict[str, Any]:
    where, params = [], []
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if book_id:
        where.append("book_id=?")
        params.append(book_id)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"SELECT * FROM llm_requests{w} ORDER BY id", params).fetchall()
    tot: dict[str, Any] = {"requests": len(rows), "cost_reported_usd": 0.0, "cost_estimated_usd": 0.0,
                           "cost_unknown_estimate_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
                           "reasoning_tokens": 0, "cached_tokens": 0, "failed_requests": 0,
                           "requests_with_retries": 0, "latency_ms_total": 0,
                           "requests_without_reasoning_detail": 0}  # fal: reasoning is billed but not itemized
    by_stage: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_cost_status: dict[str, int] = {}
    structured: dict[str, int] = {}
    for r in rows:
        u = json.loads(r["usage_json"] or "{}")
        pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        rt = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        if ct and "completion_tokens_details" not in u:
            tot["requests_without_reasoning_detail"] += 1
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        tot["prompt_tokens"] += pt
        tot["completion_tokens"] += ct
        tot["reasoning_tokens"] += rt
        tot["cached_tokens"] += cached
        tot["latency_ms_total"] += int(r["latency_ms"] or 0)
        cs = r["cost_status"]
        by_cost_status[cs] = by_cost_status.get(cs, 0) + 1
        structured[r["structured_mode"] or "?"] = structured.get(r["structured_mode"] or "?", 0) + 1
        cost = 0.0
        if cs in ("reported", "reported_generation"):
            cost = float(r["cost_usd"] or 0)
            tot["cost_reported_usd"] += cost
        elif cs == "estimated_from_tokens":
            cost = float(r["cost_usd"] or 0)
            tot["cost_estimated_usd"] += cost
        elif cs in ("unknown", "pending"):
            cost = float(r["est_cost_usd"] or 0)
            tot["cost_unknown_estimate_usd"] += cost
        if r["status"] not in ("succeeded",):
            tot["failed_requests"] += 1
        if (r["attempts"] or 0) > 1:
            tot["requests_with_retries"] += 1
        for key, bucket in ((r["stage"], by_stage), (r["model"], by_model)):
            b = bucket.setdefault(key, {"requests": 0, "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0})
            b["requests"] += 1
            b["cost_usd"] += cost
            b["prompt_tokens"] += pt
            b["completion_tokens"] += ct
        if u.get("models"):  # the model ids a Claude Code alias was really run on
            b = by_model[r["model"]]
            b["models"] = sorted(set(b.get("models", [])) | {str(m) for m in u["models"]})
    tot["total_cost_upper_usd"] = round(tot["cost_reported_usd"] + tot["cost_estimated_usd"]
                                        + tot["cost_unknown_estimate_usd"], 6)
    for k in ("cost_reported_usd", "cost_estimated_usd", "cost_unknown_estimate_usd"):
        tot[k] = round(tot[k], 6)
    out: dict[str, Any] = {"totals": tot, "by_stage": by_stage, "by_model": by_model,
                           "by_cost_status": by_cost_status, "structured_modes": structured,
                           "plan_usage": plan_usage(rows)}
    if run_id:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run:
            chars = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(u.plain_text)),0), COUNT(*) FROM translations t JOIN units u ON "
                "u.id=t.unit_id WHERE t.run_id=?", (run_id,)).fetchone()
            ja_chars, units = int(chars[0]), int(chars[1])
            start, end = _ts(run["started_at"]), _ts(run["finished_at"])
            minutes = ((end - start).total_seconds() / 60) if start and end else None
            out["run"] = {"id": run_id, "status": run["status"], "mode": run["mode"], "model": run["model"],
                          "budget_usd": run["budget_usd"], "started_at": run["started_at"],
                          "finished_at": run["finished_at"]}
            out["metrics"] = {
                "japanese_chars_translated": ja_chars, "units_translated": units,
                "usd_per_10k_ja_chars": round(tot["total_cost_upper_usd"] / ja_chars * 10000, 6) if ja_chars else None,
                "units_per_minute": round(units / minutes, 2) if minutes else None,
                "retries_per_100_units": round(tot["requests_with_retries"] / units * 100, 2) if units else None,
            }
    return out


RESET_JITTER = 120  # seconds: one window's reset time as reported request to request moves by a second or so


def plan_usage(rows: list[Any]) -> dict[str, Any]:
    """How much of a subscription plan (Claude, ChatGPT through Codex) these requests used, per source: the token
    counts, and per window the share of the window used between the first and the last request (``usage["plan"]``,
    0–1). A window that reset in between counts from 0 again (its reset time moved on). The share is the whole
    account's: other use of the plan in the same hours is counted too; the first request's own share is not (the
    plan is read after each request). Plans differ, so this is a share of the user's own plan, not an amount."""
    out: dict[str, Any] = {}
    for r in rows:
        u = json.loads(r["usage_json"] or "{}")
        plan = u.get("plan")
        source = (plan or {}).get("source") or (r["provider"] if r["provider"] in ("claude", "codex", "agy") else None)
        if not source:
            continue
        s = out.setdefault(source, {"requests": 0, "tokens": {"input": 0, "output": 0, "cache_read": 0,
                                                             "cache_write": 0, "reasoning": 0, "failed_tries": 0},
                                    "_samples": {}})
        s["requests"] += 1
        t = s["tokens"]
        t["input"] += int(u.get("prompt_tokens") or 0)
        t["output"] += int(u.get("completion_tokens") or 0)
        t["cache_read"] += int(u.get("cache_read_tokens") or 0)
        t["cache_write"] += int(u.get("cache_write_tokens") or 0)
        t["reasoning"] += int(u.get("reasoning_tokens") or 0)
        t["failed_tries"] += sum(int(v or 0) for v in (u.get("failed_tries") or {}).values())
        for name, w in ((plan or {}).get("windows") or {}).items():
            if isinstance(w, dict) and isinstance(w.get("used"), (int, float)):
                s["_samples"].setdefault(name, []).append((r["finished_at"] or r["created_at"] or "",
                                                           float(w["used"]), w.get("resets_at")))
    for s in out.values():
        windows: dict[str, Any] = {}
        for name, samples in s.pop("_samples").items():
            samples.sort(key=lambda x: x[0])
            groups: list[list[tuple[str, float, Any]]] = []
            for smp in samples:
                prev = groups[-1][-1][2] if groups else None
                same = (groups and isinstance(smp[2], (int, float)) and isinstance(prev, (int, float))
                        and abs(smp[2] - prev) <= RESET_JITTER) or (groups and smp[2] is None and prev is None)
                if same:
                    groups[-1].append(smp)
                else:
                    groups.append([smp])
            used = 0.0
            for i, g in enumerate(groups):
                vals = [x[1] for x in g]
                used += max(vals) - (min(vals) if i == 0 else 0.0)
            windows[name] = {"used": round(used, 4), "resets": len(groups) - 1, "first": samples[0][1],
                             "last": samples[-1][1], "samples": len(samples)}
        s["windows"] = windows
    return out
