"""How much of a subscription plan a book used: the share of each window between the first and the last request,
counting from 0 again after a reset; token counts per source."""

from __future__ import annotations

import json
from typing import Any

from ncte.reports.cost_report import plan_usage


def _row(i: int, provider: str, used: float | None, resets: int | None, **tokens: Any) -> dict[str, Any]:
    u: dict[str, Any] = {"prompt_tokens": 1000, "completion_tokens": 100, "cache_read_tokens": 400, **tokens}
    if used is not None:
        u["plan"] = {"windows": {"weekly": {"used": used, "resets_at": resets}}, "source": provider}
    return {"provider": provider, "usage_json": json.dumps(u), "finished_at": f"2026-10-20T05:{i:02d}:00Z",
            "created_at": f"2026-10-20T05:{i:02d}:00Z"}


def test_the_share_used_counts_from_the_first_request_and_again_from_zero_after_a_reset():
    rows = [_row(1, "codex", 0.18, 1792972798), _row(3, "codex", 0.20, 1792972799),  # a second's jitter: same window
            _row(2, "codex", 0.19, 1792972798),  # finished out of order: sorted by time
            _row(4, "codex", 0.01, 1793082845), _row(5, "codex", 0.03, 1793082845),  # reset: from 0 again
            _row(6, "openrouter", None, None)]  # not a plan: left out
    pu = plan_usage(rows)
    assert set(pu) == {"codex"}
    w = pu["codex"]["windows"]["weekly"]
    assert w == {"used": 0.05, "resets": 1, "first": 0.18, "last": 0.03, "samples": 5}
    assert pu["codex"]["requests"] == 5
    assert pu["codex"]["tokens"]["input"] == 5000 and pu["codex"]["tokens"]["cache_read"] == 2000


def test_a_plan_request_without_a_reading_still_counts_its_tokens_and_failed_tries():
    rows = [_row(1, "claude", None, None, failed_tries={"input_tokens": 16000, "output_tokens": 128000})]
    pu = plan_usage(rows)
    assert pu["claude"]["windows"] == {} and pu["claude"]["tokens"]["failed_tries"] == 144000
