"""How well a model judges fixes (audit 2026-10-17 §5.1): known cases sent to ``fix_check`` exactly as a run sends
them; the number that matters is how often a fix that is still wrong, or broke something, is settled (target < 5%).

Cases are self-made (``data/fix_check_v*.json``); the same source comes with different ``after`` texts, so a model that
settles whatever reads well is caught. v2 adds subtle meaning, a wrong suggestion the translator followed, longer
passages fixed in one place and broken in another, and cases the source cannot settle (only ``undecided`` is right). Each model gets the same batches (shuffled once, 20 per request
like a run) with the run's prompt, schema, structured mode and reasoning (``high`` when the model offers it)."""

from __future__ import annotations

import json
import random
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from ..config import Config
from ..errors import FatalAPIError, LLMError, ModelOutputError
from ..llm.base import MODEL_PARAMS, LLMClient, LLMRequest, LLMResponse
from ..llm.cost import Pricing, estimate_request_cost, estimate_tokens, find_model
from ..llm.prompt_loader import load_prompt
from ..llm.schemas import FixCheck, FixCheckOutput
from ..reports.cost_report import reasoning_tokens
from ..translation.orchestrator import DEFAULT_STAGE_REASONING
from ..translation.review import FIX_CHECK_BATCH, fix_outcome
from ..translation.translator import auto_max_tokens, call_structured

DATA = Path(__file__).parent / "data"
DEFAULT_FIX_SET = DATA / "fix_check_v2.json"
Gold = Literal["fixed", "still_wrong", "issue_invalid", "regressed", "undecided"]
SETTLED = ("fixed", "issue_invalid")  # what closes an issue (fix_outcome)
OPEN_GOLD = ("still_wrong", "regressed", "undecided")  # must stay open (the source does not settle an undecided one)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FixCase(_M):
    id: str
    category: str
    gold: Gold
    accept: list[Gold] = []  # other answers that also hold (and, like the gold one, settle or not alike)
    genre: str = ""
    source: str
    before: str
    after: str
    issues: list[str]
    around: list[str] = []
    lines: list[str] = []
    note: str = ""


class FixCharacter(_M):
    name: str
    ja: str
    gender: str
    narrator: bool = False


class FixSet(_M):
    name: str
    version: int
    description: str = ""
    characters: list[FixCharacter]
    cases: list[FixCase]

    @model_validator(mode="after")
    def _unique(self) -> FixSet:
        ids = [c.id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case ids")
        for c in self.cases:
            if any((a in SETTLED) != (c.gold in SETTLED) for a in c.accept):
                raise ValueError(f"{c.id}: an accepted answer must settle (or not) like the gold one")
        return self


def load_fix_set(path: str | Path | None = None) -> FixSet:
    """A bundled set by name (``fix_check_v1``) or a JSON path; default the latest bundled set."""
    p = DATA / f"{path}.json" if path and (DATA / f"{path}.json").exists() else Path(path) if path else DEFAULT_FIX_SET
    return FixSet.model_validate(json.loads(p.read_text(encoding="utf-8")))


def payloads(fs: FixSet, seed: int = 0) -> list[dict[str, Any]]:
    """The requests a run would send: items in the run's shape, 20 per request, with the characters their text names."""
    cases = list(fs.cases)
    random.Random(seed).shuffle(cases)
    out = []
    for i in range(0, len(cases), FIX_CHECK_BATCH):
        items = []
        for c in cases[i:i + FIX_CHECK_BATCH]:
            item: dict[str, Any] = {"unit_id": c.id, "source": c.source, "before": c.before, "after": c.after,
                                    "issues": c.issues, "around": [{"source": s} for s in c.around]}
            if c.lines:
                item["lines"] = c.lines
            items.append(item)
        payload: dict[str, Any] = {"task": "fix_check", "items": items}
        text = "".join(x["source"] + "".join(a["source"] for a in x["around"]) + "".join(x.get("lines", []))
                       for x in items)
        cast = [{"name": ch.name, "ja": ch.ja, "gender": ch.gender, **({"narrator": True} if ch.narrator else {})}
                for ch in fs.characters if ch.ja in text or ch.name in text]
        if cast:
            payload["characters"] = cast
        out.append(payload)
    return out


def reasoning_for(cfg: Config, pricing: Pricing) -> str | None:
    """The fix check's reasoning setting for this model, as a run would choose it."""
    own = cfg.openrouter.stage_reasoning.get("fix_check")
    if own is not None:
        return None if own in ("", "default") else own
    if "reasoning" in pricing.supported_parameters and (not pricing.reasoning_efforts or "high" in pricing.reasoning_efforts):
        return DEFAULT_STAGE_REASONING["fix_check"]
    return cfg.openrouter.reasoning_effort


def estimate(fs: FixSet, pricing: Pricing, seed: int = 0, reasoning: str | None = None) -> float:
    """Upper bound for one pass: every request writes up to its output cap (reasoning included). The run's own
    estimate (200 characters per item) missed high reasoning by 3.8× (2026-10-17); a retry after a cut-off is not
    covered."""
    prompt = load_prompt("fix_check")
    total = 0.0
    for p in payloads(fs, seed):
        user = json.dumps(p, ensure_ascii=False)
        total += pricing.cost(estimate_tokens(prompt.text + user), auto_max_tokens(user, reasoning))
    return total


def score(fs: FixSet, checks: dict[str, FixCheck]) -> dict[str, Any]:
    """Outcomes as a run would record them (no program check), against the known answer."""
    rows = []
    for c in fs.cases:
        got = fix_outcome(checks.get(c.id), program_high=False)
        rows.append({"id": c.id, "category": c.category, "genre": c.genre, "gold": c.gold, "accept": c.accept,
                     "outcome": got, "ok": got == c.gold or got in c.accept,
                     "reason": checks[c.id].reason if c.id in checks else "",
                     "new_error": checks[c.id].new_error if c.id in checks else ""})
    open_gold = [r for r in rows if r["gold"] in OPEN_GOLD]
    settle_gold = [r for r in rows if r["gold"] in SETTLED]
    false_settled = [r for r in open_gold if r["outcome"] in SETTLED]

    def rate(n: int, d: int) -> float | None:
        return round(n / d, 4) if d else None

    return {
        # the target: a fix that is still wrong or broke something must not be closed
        "false_settle_rate": rate(len(false_settled), len(open_gold)),
        "false_settled": [r["id"] for r in false_settled],
        # judged fixed when it is not (an invalid issue judged fixed also closes it, so it is reported apart)
        "false_fixed_rate": rate(sum(r["outcome"] == "fixed" for r in rows if r["gold"] != "fixed"),
                                 sum(r["gold"] != "fixed" for r in rows)),
        "settle_recall": rate(sum(r["outcome"] in SETTLED for r in settle_gold), len(settle_gold)),
        "exact": rate(sum(r["outcome"] == r["gold"] for r in rows), len(rows)),
        "acceptable": rate(sum(bool(r["ok"]) for r in rows), len(rows)),  # the gold answer or one also accepted
        "undecided_caught": rate(sum(r["outcome"] == "undecided" for r in rows if r["gold"] == "undecided"),
                                 sum(r["gold"] == "undecided" for r in rows)),
        "false_settled_by_category": dict(Counter(r["category"] for r in false_settled)),
        "regressed_caught": rate(sum(r["outcome"] == "regressed" for r in rows if r["gold"] == "regressed"),
                                 sum(r["gold"] == "regressed" for r in rows)),
        "new_error_on_clean": rate(sum(r["outcome"] == "regressed" for r in settle_gold), len(settle_gold)),
        "undecided_rate": rate(sum(r["outcome"] == "undecided" for r in rows), len(rows)),
        # settle recall's other side: an issue that should close stays open (more work, not a wrong close)
        "kept_open": [r["id"] for r in settle_gold if r["outcome"] not in SETTLED],
        "unchecked": sum(r["outcome"] == "unchecked" for r in rows),
        "confusion": {g: dict(Counter(r["outcome"] for r in rows if r["gold"] == g)) for g in Gold.__args__},  # type: ignore[attr-defined]
        "cases": rows,
    }


class _Costs:
    """Recorder that keeps what each request cost (as the API reported it, else from /models prices)."""

    def __init__(self, pricing: Pricing) -> None:
        self.pricing = pricing
        self.requests: list[dict[str, Any]] = []

    def start(self, req: LLMRequest, unit_ids: list[str], est_cost: float) -> int:
        self.requests.append({"est_usd": est_cost, "status": "started"})
        return len(self.requests) - 1

    def success(self, rid: int, resp: LLMResponse, parse_status: str) -> None:
        u = resp.usage or {}
        pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        cost = resp.cost_usd if resp.cost_usd is not None else self.pricing.cost(pt, ct)
        self.requests[rid].update(status=parse_status, cost_usd=cost, prompt_tokens=pt, completion_tokens=ct,
                                  reasoning_tokens=reasoning_tokens(u), latency_ms=resp.latency_ms,
                                  provider=resp.provider)

    def failure(self, rid: int, exc: Exception) -> None:
        self.requests[rid].update(status="failed", error=str(exc)[:300])

    @property
    def total(self) -> float:
        return round(sum(r.get("cost_usd") or 0 for r in self.requests), 6)


def run_model(fs: FixSet, cfg: Config, client: LLMClient, model: str, pricing: Pricing, *, seed: int = 0,
              log: Callable[[str], None] = print) -> dict[str, Any]:
    """One pass of the set through one model."""
    c = cfg.model_copy(deep=True)
    effort = reasoning_for(cfg, pricing)
    c.openrouter.stage_reasoning["fix_check"] = effort or "default"
    MODEL_PARAMS[model] = set(pricing.supported_parameters)
    prompt = load_prompt("fix_check")
    rec = _Costs(pricing)
    checks: dict[str, FixCheck] = {}
    errors: list[str] = []
    started = time.monotonic()
    for p in payloads(fs, seed):
        est = estimate_request_cost(pricing, prompt.text + json.dumps(p, ensure_ascii=False), 200 * len(p["items"]))[2]
        try:
            parsed, _rid, _mode = call_structured(client, rec, stage="fix_check", model=model, prompt=prompt, payload=p,
                                                  mode=pricing.structured_mode, cfg=c,  # type: ignore[arg-type]
                                                  unit_ids=[x["unit_id"] for x in p["items"]], est_cost=est,
                                                  on_downgrade=log)
        except FatalAPIError:
            raise  # no credit, bad key, model gone: stop, as a run does
        except (LLMError, ModelOutputError) as exc:
            errors.append(str(exc)[:300])
            log(f"  {model}: request failed: {exc}")
            continue
        assert isinstance(parsed, FixCheckOutput)
        asked = {x["unit_id"] for x in p["items"]}
        for ch in parsed.checks:
            if ch.unit_id in asked:
                checks.setdefault(ch.unit_id, ch)
    result = score(fs, checks)
    result.update(model=model, reasoning=effort, cost_usd=rec.total, requests=rec.requests, errors=errors,
                  seconds=round(time.monotonic() - started, 1))
    return result


def pricing_for(client: LLMClient, models: list[str]) -> dict[str, Pricing]:
    """Each model as /models lists it now (a model not listed is refused, never substituted)."""
    listed = client.list_models()
    out = {}
    for m in models:
        found = find_model(listed, m)
        if found is None:
            raise LLMError(f"model '{m}' is not listed by {client.name} /models")
        out[m] = Pricing.from_model_json(found)
    return out


def run_comparison(fs: FixSet, cfg: Config, client: LLMClient, models: list[str], *, repeat: int = 1,
                   budget_usd: float, out_dir: Path, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Every model, ``repeat`` passes (a different shuffle each), stopping before a pass the budget cannot cover."""
    prices = pricing_for(client, models)
    spent = 0.0
    runs: list[dict[str, Any]] = []
    for m in models:
        for k in range(repeat):
            est = estimate(fs, prices[m], seed=k, reasoning=reasoning_for(cfg, prices[m]))
            if spent + est > budget_usd:
                log(f"  {m} pass {k + 1}: estimate ${est:.4f} would pass the budget (${spent:.4f} of ${budget_usd:.2f}"
                    " spent) — skipped")
                continue
            log(f"  {m} pass {k + 1}: estimate ≤ ${est:.4f}")
            r = run_model(fs, cfg, client, m, prices[m], seed=k, log=log)
            r["pass"] = k + 1
            spent += r["cost_usd"]
            runs.append(r)
            log(f"  {m} pass {k + 1}: false settle {r['false_settle_rate']} exact {r['exact']} "
                f"undecided {r['undecided_rate']} cost ${r['cost_usd']:.4f}")
    report = {"set": f"{fs.name} v{fs.version}", "cases": len(fs.cases), "models": models, "repeat": repeat,
              "spent_usd": round(spent, 6), "runs": runs}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "fix_check_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
