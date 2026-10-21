"""The cost report counts reasoning and cached tokens in both forms a request's usage can have: OpenRouter's nested
details and the plan sources' (claude, codex, agy) top-level counts — each request in one form, never both added."""

from __future__ import annotations

import json
from typing import Any

from ncte.reports.cost_report import cost_report


def _add(conn: Any, usage: dict[str, Any]) -> None:
    conn.execute("INSERT INTO llm_requests(run_id, book_id, stage, request_hash, model, provider, usage_json, "
                 "cost_status, status, created_at) VALUES ('r1','b1','translate','h','m','p',?, 'reported','succeeded', "
                 "'2026-10-21T00:00:00Z')",
                 (json.dumps(usage),))


def test_reasoning_and_cache_reads_of_every_source_are_counted(conn):
    _add(conn, {"prompt_tokens": 100, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 10},
                "completion_tokens_details": {"reasoning_tokens": 5}})  # OpenRouter
    _add(conn, {"prompt_tokens": 9000, "completion_tokens": 1200, "reasoning_tokens": 1000,
                "cache_read_tokens": 0})  # agy
    _add(conn, {"prompt_tokens": 7000, "completion_tokens": 300, "reasoning_tokens": 200,
                "cache_read_tokens": 4000})  # codex
    # both forms in one usage (should not happen): the nested one is the request's own, not added to the other
    _add(conn, {"prompt_tokens": 10, "completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 3},
                "reasoning_tokens": 3, "prompt_tokens_details": {"cached_tokens": 2}, "cache_read_tokens": 2})
    tot = cost_report(conn, run_id="r1")["totals"]
    assert tot["reasoning_tokens"] == 5 + 1000 + 200 + 3
    assert tot["cached_tokens"] == 10 + 0 + 4000 + 2
    assert tot["requests_without_reasoning_detail"] == 0


def test_a_request_with_no_reasoning_count_at_all_is_still_marked(conn):
    _add(conn, {"prompt_tokens": 100, "completion_tokens": 50, "cache_read_tokens": 30})  # claude: no thinking count
    tot = cost_report(conn, run_id="r1")["totals"]
    assert tot["reasoning_tokens"] == 0 and tot["cached_tokens"] == 30
    assert tot["requests_without_reasoning_detail"] == 1
