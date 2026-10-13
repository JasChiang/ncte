"""Self-contained local HTML report (no web server, no external assets)."""

from __future__ import annotations

import json
from html import escape
from typing import Any

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e1db;--accent:#2f6f5e;--warn:#b26b00;--bad:#b3261e}
@media (prefers-color-scheme: dark){:root{--bg:#18181a;--fg:#ececee;--muted:#a0a0a8;--line:#333338;--accent:#7cc4ad;--warn:#f0b35a;--bad:#ff8a80}}
body{background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"PingFang TC","Hiragino Sans",sans-serif;margin:0;padding:24px 16px;max-width:1100px;margin:auto}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.muted{color:var(--muted)} table{border-collapse:collapse;width:100%;font-size:13px} th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600} .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:10px 12px} .kpi b{display:block;font-size:20px}
.sev-error,.sev-high{color:var(--bad);font-weight:600} .sev-medium{color:var(--warn)} pre{white-space:pre-wrap;font-size:12px}
.ok{color:var(--accent)} .wrap{overflow-x:auto}
"""


def _kv_table(d: dict[str, Any]) -> str:
    return "<table>" + "".join(f"<tr><th>{escape(str(k))}</th><td>{escape(_fmt(v))}</td></tr>" for k, v in d.items()) + "</table>"


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.6f}".rstrip("0").rstrip(".") if v else "0"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def render_html(title: str, cost: dict[str, Any], qa: dict[str, Any], status: dict[str, Any] | None = None,
                extra: dict[str, Any] | None = None) -> str:
    t = cost["totals"]
    kpis = [("Total cost (upper bound)", f"${t['total_cost_upper_usd']:.4f}"),
            ("Reported by API", f"${t['cost_reported_usd']:.4f}"),
            ("Unknown-billing estimate", f"${t['cost_unknown_estimate_usd']:.4f}"),
            ("Requests", t["requests"]), ("Prompt / completion tokens", f"{t['prompt_tokens']:,} / {t['completion_tokens']:,}"),
            ("QA findings", qa["findings_total"]), ("Needs review", len(qa["needs_review"])),
            ("Untranslated (kept JA)", len(qa["untranslated"]))]
    parts = [f"<!doctype html><html lang='zh-Hant-TW'><head><meta charset='utf-8'><meta name='viewport' "
             f"content='width=device-width,initial-scale=1'><title>{escape(title)}</title><style>{CSS}</style></head><body>",
             f"<h1>{escape(title)}</h1>"]
    if "run" in cost:
        r = cost["run"]
        parts.append(f"<div class='muted'>run {escape(r['id'])} · {escape(r['mode'])} · {escape(r['model'])} · "
                     f"status <b>{escape(r['status'])}</b> · budget ${r['budget_usd']}</div>")
    parts.append("<div class='kpis' style='margin-top:14px'>" + "".join(
        f"<div class='kpi'><span class='muted'>{escape(k)}</span><b>{escape(str(v))}</b></div>" for k, v in kpis) + "</div>")
    if cost.get("metrics"):
        parts.append("<h2>Metrics</h2>" + _kv_table(cost["metrics"]))
    parts.append("<h2>Cost by stage</h2><div class='wrap'><table><tr><th>stage</th><th>requests</th><th>cost USD</th>"
                 "<th>prompt tok</th><th>completion tok</th></tr>" + "".join(
                     f"<tr><td>{escape(k)}</td><td>{v['requests']}</td><td>{v['cost_usd']:.6f}</td>"
                     f"<td>{v['prompt_tokens']}</td><td>{v['completion_tokens']}</td></tr>"
                     for k, v in cost["by_stage"].items()) + "</table></div>")
    parts.append("<h2>Billing status of requests</h2>" + _kv_table(cost["by_cost_status"]))
    parts.append("<h2>Structured output mode</h2>" + _kv_table(cost["structured_modes"]))
    if status:
        parts.append("<h2>Book progress</h2>" + _kv_table({k: v for k, v in status.items() if k != "runs"}))
    if extra:
        for k, v in extra.items():
            parts.append(f"<h2>{escape(k)}</h2>" + (_kv_table(v) if isinstance(v, dict) else f"<pre>{escape(_fmt(v))}</pre>"))
    parts.append("<h2>QA findings by type</h2>" + _kv_table(qa["by_type"]))
    parts.append("<h2>Needs review</h2><div class='wrap'><table><tr><th>unit</th><th>source</th><th>translation</th></tr>"
                 + "".join(f"<tr><td>{escape(x['unit_id'])}</td><td>{escape(x['source'])}</td><td>{escape(x['translation'] or '')}</td></tr>"
                           for x in qa["needs_review"][:300]) + "</table></div>")
    parts.append("<h2>Findings</h2><div class='wrap'><table><tr><th>severity</th><th>type</th><th>unit</th><th>evidence</th>"
                 "<th>source</th><th>translation</th></tr>" + "".join(
                     f"<tr><td class='sev-{escape(x['severity'])}'>{escape(x['severity'])}</td><td>{escape(x['type'])}</td>"
                     f"<td>{escape(x['unit_id'] or '')}</td><td>{escape(x['evidence'] or '')}</td>"
                     f"<td>{escape(x['source'] or '')}</td><td>{escape(x['translation'] or '')}</td></tr>"
                     for x in qa["findings"] if x["severity"] != "low") + "</table></div>")
    parts.append("<h2>Kept original (untranslated / unsafe)</h2><div class='wrap'><table><tr><th>unit</th><th>reason</th><th>source</th></tr>"
                 + "".join(f"<tr><td>{escape(x['unit_id'])}</td><td>untranslated</td><td>{escape(x['source'])}</td></tr>"
                           for x in qa["untranslated"][:300])
                 + "".join(f"<tr><td>{escape(x['unit_id'])}</td><td>{escape(x['reason'] or '')}</td><td>{escape(x['source'])}</td></tr>"
                           for x in qa["unsafe_units"][:300]) + "</table></div>")
    parts.append("</body></html>")
    return "\n".join(parts)
