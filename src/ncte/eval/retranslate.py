"""Does a fix change only what is wrong (audit 2026-10-17 §5.2)? Known wrong translations go back to the translator
with what was found and the current text, as a run sends them after review; another model then checks each result
against the source (``fix_check``), and the edit size is measured.

Cases come from the fix-check set (``fix_check_v2``): its source, ``before`` and issues, once per distinct
source/before/issue. An issue the source supports should end ``fixed`` (not ``still_wrong`` or with a new error); an
issue it does not support (a wrong suggestion, or one the source cannot settle) should leave the text as it was.

What a run sends that this does not: the scene's memory (summaries, facts, address table, house style) and more
than one neighbouring paragraph; the request carries the case's own neighbours, characters and who speaks to whom."""

from __future__ import annotations

import difflib
import json
import statistics
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import FatalAPIError, LLMError
from ..llm.base import MODEL_PARAMS, LLMClient
from ..llm.cost import Pricing, estimate_request_cost
from ..llm.prompt_loader import load_prompt
from ..translation.translator import Translator
from .fix_check import FixCase, FixSet, _Costs, pricing_for, reasoning_for, run_model

SUPPORTED = ("fixed", "still_wrong", "regressed")  # the source supports the issue (the before text is wrong)


def distinct_cases(fs: FixSet) -> list[tuple[FixCase, bool]]:
    """(case, the source supports its issue), one per source/before/issue."""
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    out = []
    for c in fs.cases:
        key = (c.source, c.before, tuple(c.issues))
        if key in seen:
            continue
        seen.add(key)
        out.append((c, c.gold in SUPPORTED and c.category != "bad_suggestion"))
    return out


def _issue(text: str) -> dict[str, str]:
    kind, _, rest = text.partition(": ")
    evidence, _, suggestion = rest.partition(" → ")
    return {"issue_type": kind, "evidence": evidence, "suggestion": suggestion}


def payload(fs: FixSet, c: FixCase) -> dict[str, Any]:
    """The translate request a run sends after review, for one case (its own neighbours and people)."""
    text = c.source + "".join(c.around) + "".join(c.lines)
    chars = [{"name_ja": ch.ja, "name_zh": ch.name, "gender": ch.gender, **({"narrator": True} if ch.narrator else {})}
             for ch in fs.characters if ch.ja in text or ch.name in text]
    out: dict[str, Any] = {"context_before": [{"unit_id": f"{c.id}-b{i}", "source": s} for i, s in enumerate(c.around)],
                           "context_after": []}
    if chars:
        out["characters"] = chars
    utts = []
    for i, line in enumerate(c.lines):
        who, _, to = line.partition(" → ")
        guess = "（推測）" in who
        item: dict[str, Any] = {"unit_id": c.id, "quote_index": i, "speaker": who.replace("（推測）", "").strip("？") or None,
                                "status": "inferred" if guess else "explicit"}
        if to:
            item["to"] = [to.replace("（推測）", "")]
        utts.append(item)
    if utts:
        out["scene"] = {"utterances": utts}
    out["review_feedback"] = [{"unit_id": c.id, "current_translation": c.before, "issues": [_issue(x) for x in c.issues]}]
    return out


def edit_ratio(a: str, b: str) -> float:
    """How much of the text changed (0 = the same, 1 = all of it)."""
    return round(1 - difflib.SequenceMatcher(None, a, b).ratio(), 3)


def retranslate(fs: FixSet, cfg: Config, client: LLMClient, model: str, pricing: Pricing, *, budget_usd: float,
                log: Callable[[str], None] = print) -> tuple[FixSet, dict[str, Any]]:
    """Each case through the translator; returns the set with each ``after`` replaced by the new text (cases that
    failed are left out) and what it cost. Stops when what was spent reaches the budget."""
    MODEL_PARAMS[model] = set(pricing.supported_parameters)
    prompt = load_prompt("translate")
    rec = _Costs(pricing)

    def est(p: dict[str, Any], out_chars: int) -> float:
        return estimate_request_cost(pricing, prompt.text + json.dumps(p, ensure_ascii=False), out_chars)[2]

    tr = Translator(client, cfg, rec, model=model, structured_mode=pricing.structured_mode,  # type: ignore[arg-type]
                    estimate=est, prompt=prompt, on_downgrade=log)
    from ..translation.translator import UnitInput

    done: list[FixCase] = []
    failed: dict[str, str] = {}
    for c, _supported in distinct_cases(fs):
        if rec.total >= budget_usd:
            log(f"  budget reached (${rec.total:.4f}); {c.id} and later not sent")
            failed[c.id] = "budget"
            continue
        unit = UnitInput(unit_id=c.id, kind="text", source_text=c.source, plain_text=c.source, inline_map={})
        try:
            res = tr.translate([unit], payload(fs, c))
        except FatalAPIError:
            raise
        except LLMError as exc:
            failed[c.id] = str(exc)[:300]
            continue
        if c.id in res.ok:
            done.append(c.model_copy(update={"after": res.ok[c.id].text}))
        else:
            failed[c.id] = res.failed.get(c.id, "no translation")[:300]
    return fs.model_copy(update={"cases": done}), {"cost_usd": rec.total, "requests": rec.requests, "failed": failed}


def summarize(cases: list[tuple[FixCase, bool]], after: dict[str, str], judged: dict[str, Any]) -> dict[str, Any]:
    """Per kind of issue: how the fixes ended (as the judge saw them), how often the text was left as it was, and
    how much of it changed."""
    outcome = {r["id"]: r for r in judged["cases"]}
    rows = []
    for c, supported in cases:
        if c.id not in after:
            continue
        a = after[c.id]
        rows.append({"id": c.id, "category": c.category, "supported": supported, "before": c.before, "after": a,
                     "kept": a.strip() == c.before.strip(), "edit": edit_ratio(c.before, a),
                     "outcome": outcome[c.id]["outcome"], "reason": outcome[c.id]["reason"],
                     "new_error": outcome[c.id]["new_error"]})

    def part(rs: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rs)
        return {"n": n, "outcomes": dict(Counter(r["outcome"] for r in rs)),
                "kept_original": sum(r["kept"] for r in rs),
                "median_edit": statistics.median([r["edit"] for r in rs]) if rs else None}

    sup = [r for r in rows if r["supported"]]
    uns = [r for r in rows if not r["supported"]]
    return {
        # a real error: fixed, nothing new broken
        "supported": {**part(sup), "fixed_rate": round(sum(r["outcome"] == "fixed" for r in sup) / len(sup), 4)
                      if sup else None},
        # a wrong or unsupported suggestion: the text should stay (judged issue_invalid / undecided, no new error)
        "unsupported": {**part(uns), "followed_rate": round(sum(not r["kept"] and r["outcome"] == "regressed"
                                                               for r in uns) / len(uns), 4) if uns else None},
        "cases": rows,
    }


def run_retranslate_eval(fs: FixSet, cfg: Config, client: LLMClient, *, model: str, judge: str, budget_usd: float,
                         out_dir: Path, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Translate the fixes with ``model``, judge them with ``judge`` (another family than the translator's)."""
    prices = pricing_for(client, list(dict.fromkeys([model, judge])))
    started = time.monotonic()
    fixed, spent = retranslate(fs, cfg, client, model, prices[model], budget_usd=budget_usd, log=log)
    log(f"  translated {len(fixed.cases)} fixes with {model}: ${spent['cost_usd']:.4f}")
    judged = run_model(fixed, cfg, client, judge, prices[judge], log=log)
    log(f"  judged with {judge} (reasoning {reasoning_for(cfg, prices[judge])}): ${judged['cost_usd']:.4f}")
    report = {"set": f"{fs.name} v{fs.version}", "translator": model, "judge": judge,
              **summarize(distinct_cases(fs), {c.id: c.after for c in fixed.cases}, judged),
              "translate_cost_usd": spent["cost_usd"], "judge_cost_usd": judged["cost_usd"],
              "failed": spent["failed"], "seconds": round(time.monotonic() - started, 1)}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "retranslate_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # for the human spot check the audit asks for: one line per case, before → after, the judge's call
    lines = ["| case | issue supported | before | after | judge | why |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {'yes' if r['supported'] else 'no'} | {r['before']} | {r['after']} | {r['outcome']} | "
              f"{(r['reason'] + ' ' + r['new_error']).strip()} |" for r in report["cases"]]
    (out_dir / "retranslate_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
