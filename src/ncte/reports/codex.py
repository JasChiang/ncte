"""Name index and character relationship map for a series (spoiler-aware).

Both are built only from validated knowledge: entity name forms (ADR-019), characters, and facts with verbatim
evidence. ``upto`` limits everything to what is revealed before a story position (volume, ordinal), so a
spoiler-free index/map "as of volume 2" can be produced.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from html import escape
from typing import Any

from ..epub.units import match_text
from ..memory.entities import entity_forms, senses
from ..series import INF_POS, SERIES_SCOPE, Pos, volumes

KIND_ZH = {"character": "人物", "organization": "團體", "place": "地點", "item": "物品", "skill": "技能", "term": "用語"}


def _series_units(conn: sqlite3.Connection, book_id: str) -> list[tuple[Pos, str, str]]:
    vols = volumes(conn)
    rows = conn.execute(f"SELECT id, book_id, ordinal, plain_text FROM units WHERE {SERIES_SCOPE} AND "
                        "spine_index IS NOT NULL AND kind IN ('text','heading') ORDER BY ordinal", (book_id,))
    out = [((vols.get(r[1], 1.0), int(r[2])), r[0], match_text(r[3])) for r in rows]
    return sorted(out, key=lambda x: x[0])


def _book_labels(conn: sqlite3.Connection, book_id: str) -> dict[float, str]:
    return {float(r[0]): (r[1] or f"第{r[0]:g}集") for r in
            conn.execute(f"SELECT volume_index, volume_label FROM books WHERE {SERIES_SCOPE.replace('book_id', 'id')}",
                         (book_id,))}


def build_index(conn: sqlite3.Connection, book_id: str, upto: Pos = INF_POS) -> list[dict[str, Any]]:
    units = [u for u in _series_units(conn, book_id) if u[0] < upto]
    ents = {r["id"]: r for r in conn.execute(f"SELECT * FROM entities WHERE {SERIES_SCOPE}", (book_id,))}
    every = conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,)).fetchall()
    # a name's card is the one named so, not one only known by other cards' words (ADR-103: those come first, so the
    # card named so is the one kept)
    chars = {r["canonical_name"]: r for r in sorted(every, key=lambda r: r["label"] == "")}
    cname = {r["id"]: r["display_name_zh"] or r["canonical_name"] for r in every}
    vols = volumes(conn)
    called: dict[str, list[dict[str, Any]]] = {}  # addressee id → forms others (or they themself) use
    for a in conn.execute(f"SELECT * FROM address_terms WHERE {SERIES_SCOPE}", (book_id,)):
        p = (vols.get(a["book_id"], 1.0), a["first_ordinal"])
        if p < upto and a["speaker_id"] in cname:
            called.setdefault(a["addressee_id"], []).append(
                {"speaker": "（自稱）" if a["speaker_id"] == a["addressee_id"] else cname[a["speaker_id"]],
                 "ja": a["form_ja"], "zh": a["form_zh"], "uses": a["uses"], "first": p})
    shared_to: dict[str, list[str]] = {}  # entity id → forms of OTHER entities that may also mean it
    shared_from: dict[str, list[str]] = {}  # term → note that it may also mean another entity
    for t, xs in senses(conn, book_id).items():
        for x in xs:
            if x["pos"] is None or x["pos"] < upto:
                shared_to.setdefault(x["entity_id"], []).append(t)
                shared_from.setdefault(t, []).append(x["entity_id"])
    out: list[dict[str, Any]] = []
    for eid, forms in entity_forms(conn, book_id).items():
        entry_forms = []
        first: Pos | None = None
        per_vol: Counter[float] = Counter()
        for f in forms:
            hits = [u for u in units if f["ja"] in u[2]]
            if not hits:
                continue
            first = min(first, hits[0][0]) if first else hits[0][0]
            for h in hits:
                per_vol[h[0][0]] += 1
            entry_forms.append({"ja": f["ja"], "zh": f["zh"], "form": f["form"], "note": f["note"],
                                "verified": f["verified"], "mentions": len(hits), "first": hits[0][0]})
        if not entry_forms:
            continue  # not yet mentioned before ``upto``
        entry_forms.sort(key=lambda f: (f["form"] != "official", f["first"]))
        e = ents.get(eid)
        kind = e["kind"] if e else forms[0]["category"]
        item: dict[str, Any] = {"entity_id": eid, "kind": kind, "name_zh": entry_forms[0]["zh"],
                                "name_ja": entry_forms[0]["ja"], "forms": entry_forms, "first": first,
                                "mentions_by_volume": dict(sorted(per_vol.items())),
                                "mentions": sum(f["mentions"] for f in entry_forms)}
        ch = chars.get(entry_forms[0]["ja"]) or next((chars.get(f["ja"]) for f in entry_forms if f["ja"] in chars), None)
        if shared_to.get(eid) or any(f["ja"] in shared_from for f in entry_forms):
            item["shared_forms"] = sorted(set(shared_to.get(eid, []) + [f["ja"] for f in entry_forms
                                                                       if f["ja"] in shared_from]))
        if ch is not None and called.get(ch["id"]):
            item["called"] = sorted(called[ch["id"]], key=lambda c: (-c["uses"], c["first"]))
        if ch is not None and kind == "character":
            prof = json.loads(ch["speech_profile_json"] or "{}")
            if prof.get("n_examples"):
                item["speech"] = {k: prof.get(k) for k in ("first_person", "second_person", "sentence_endings",
                                                           "formality", "n_examples")}
        out.append(item)
    out.sort(key=lambda x: (list(KIND_ZH).index(x["kind"]) if x["kind"] in KIND_ZH else 99, -x["mentions"]))
    return out


def build_relations(conn: sqlite3.Connection, book_id: str, upto: Pos = INF_POS, *,
                    min_shared_scenes: int = 2) -> dict[str, Any]:
    """Nodes: characters revealed before ``upto``. Edges: (a) facts whose subject is a character and whose
    object names another character, (b) co-appearance in the same scene (weight = number of scenes)."""
    vols = volumes(conn)
    chars = [c for c in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,))
             if (vols.get(c["book_id"], 1.0), c["known_from_ordinal"]) < upto]
    # characters whose names are forms of the same entity (ミナ / 星野ミナ) become one node
    ent_of = {r[0]: r[1] for r in conn.execute(f"SELECT source_term, entity_id FROM glossary WHERE {SERIES_SCOPE} "
                                               "AND entity_id IS NOT NULL", (book_id,))}
    official = {r[0]: r[1] for r in conn.execute(f"SELECT entity_id, source_term FROM glossary WHERE {SERIES_SCOPE} "
                                                 "AND form_type='official'", (book_id,))}
    rep: dict[str, str] = {}
    by_entity: dict[str, str] = {}
    for c in sorted(chars, key=lambda c: (official.get(ent_of.get(c["canonical_name"], "")) != c["canonical_name"],
                                          c["known_from_ordinal"])):
        eid = ent_of.get(c["canonical_name"])
        if eid and eid in by_entity:
            rep[c["id"]] = by_entity[eid]
        else:
            rep[c["id"]] = c["id"]
            if eid:
                by_entity[eid] = c["id"]
    names: dict[str, list[str]] = {}
    label: dict[str, str] = {}
    for c in chars:
        if rep[c["id"]] != c["id"]:
            names.setdefault(rep[c["id"]], []).extend(
                [x for x in [c["canonical_name"], *json.loads(c["aliases_json"]), c["display_name_zh"] or ""] if len(x) >= 2])
            continue
        forms = [c["canonical_name"], *json.loads(c["aliases_json"])]
        if c["display_name_zh"]:
            forms.append(c["display_name_zh"])
        names.setdefault(c["id"], []).extend(f for f in forms if len(f) >= 2)
        label[c["id"]] = c["display_name_zh"] or c["canonical_name"]
    for g in conn.execute(f"SELECT g.source_term, g.target_term, c.id FROM glossary g JOIN characters c ON "
                          f"c.canonical_name=(SELECT source_term FROM glossary g2 WHERE g2.entity_id=g.entity_id AND "
                          f"g2.form_type='official' LIMIT 1) WHERE g.{SERIES_SCOPE}", (book_id,)):
        if g[2] in names:
            names[g[2]] += [x for x in (g[0], g[1]) if x and len(x) >= 2]
    facts = []
    for f in conn.execute(f"SELECT * FROM facts WHERE {SERIES_SCOPE}", (book_id,)):
        p = (vols.get(f["book_id"], 1.0), f["first_revealed_ordinal"])
        subject = rep.get(f["subject"], f["subject"])
        if p >= upto or subject not in names:
            continue
        targets = [cid for cid, ns in names.items() if cid != subject and any(n in f["object"] for n in ns)]
        facts.append({"subject": subject, "predicate": f["predicate"], "object": f["object"],
                      "targets": targets, "type": f["fact_type"], "certainty": f["certainty"],
                      "evidence": f["evidence_quote"], "position": p})
    # scene co-appearance
    pair: Counter[tuple[str, str]] = Counter()
    appearances: Counter[str] = Counter()
    for s in conn.execute(f"SELECT s.*, (SELECT GROUP_CONCAT(plain_text, ' ') FROM units u WHERE u.book_id=s.book_id "
                          f"AND u.scene_id=s.id) AS txt FROM scenes s WHERE s.{SERIES_SCOPE}", (book_id,)):
        if (vols.get(s["book_id"], 1.0), s["start_ordinal"]) >= upto:
            continue
        text = match_text(s["txt"] or "")
        present = sorted(cid for cid, ns in names.items() if any(n in text for n in ns))
        for cid in present:
            appearances[cid] += 1
        for i, a in enumerate(present):
            for b in present[i + 1:]:
                pair[(a, b)] += 1
    nodes: list[dict[str, Any]] = [{"id": cid, "label": label[cid], "scenes": appearances[cid]}
                                   for cid in names if appearances[cid]]
    nodes.sort(key=lambda n: -n["scenes"])
    keep = {n["id"] for n in nodes}
    edges = [{"a": a, "b": b, "shared_scenes": n} for (a, b), n in pair.items() if n >= min_shared_scenes
             and a in keep and b in keep]
    rel = [f for f in facts if f["targets"]]
    attrs = [f for f in facts if not f["targets"]]
    return {"nodes": nodes, "co_appearance": sorted(edges, key=lambda e: -e["shared_scenes"]),
            "relations": rel, "attributes": attrs, "labels": label}


# --------------------------------------------------------------------------- rendering

def _pos_text(p: Pos, labels: dict[float, str]) -> str:
    return f"{labels.get(p[0], f'第{p[0]:g}集')} §{p[1]}"


def _called_text(c: dict[str, Any]) -> str:
    return f"{c['speaker']}→{c['ja']}" + (f"（{c['zh']}）" if c["zh"] else "") + f"×{c['uses']}"


def index_markdown(idx: list[dict[str, Any]], labels: dict[float, str], title: str) -> str:
    lines = [f"# {title}：名詞索引", ""]
    kind = None
    for e in idx:
        if e["kind"] != kind:
            kind = e["kind"]
            lines += ["", f"## {KIND_ZH.get(kind, kind)}", ""]
        forms = "、".join(f"{f['ja']}→{f['zh']}" + (f"（{f['form']}）" if f["form"] != "official" else "")
                         for f in e["forms"])
        lines.append(f"- **{e['name_zh']}**（{e['name_ja']}）　{forms}　首次出現：{_pos_text(e['first'], labels)}"
                     f"　出現 {e['mentions']} 段" + (f"　注：{'；'.join(f['note'] for f in e['forms'] if f['note'])}"
                                                  if any(f["note"] for f in e["forms"]) else ""))
        if e.get("called"):
            lines.append("  - 被怎麼稱呼：" + "；".join(_called_text(c) for c in e["called"][:12]))
        if e.get("shared_forms"):
            lines.append("  - 與他人共用的稱呼：" + "、".join(e["shared_forms"]))
    return "\n".join(lines) + "\n"


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e1db;--accent:#2f6f5e;--node:#ffffff}
@media (prefers-color-scheme: dark){:root{--bg:#18181a;--fg:#ececee;--muted:#a0a0a8;--line:#3a3a40;--accent:#7cc4ad;--node:#232327}}
body{background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"PingFang TC","Hiragino Sans",sans-serif;max-width:1100px;margin:auto;padding:24px 16px}
h1{font-size:22px}h2{font-size:17px;border-bottom:1px solid var(--line);padding-bottom:4px;margin-top:28px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}.muted{color:var(--muted)}.wrap{overflow-x:auto}
svg text{fill:var(--fg);font-size:13px}svg .edge{stroke:var(--muted)}svg .rel{stroke:var(--accent)}svg circle{fill:var(--node);stroke:var(--accent);stroke-width:2}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='zh-Hant-TW'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{escape(title)}</title><style>{CSS}</style></head>"
            f"<body>{body}</body></html>")


def index_html(idx: list[dict[str, Any]], labels: dict[float, str], title: str, scope: str) -> str:
    parts = [f"<h1>{escape(title)}：名詞索引</h1><p class='muted'>{escape(scope)}</p>"]
    kind = None
    for e in idx:
        if e["kind"] != kind:
            if kind is not None:
                parts.append("</table></div>")
            kind = e["kind"]
            parts.append(f"<h2>{escape(KIND_ZH.get(kind, kind))}</h2><div class='wrap'><table><tr><th>譯名</th><th>原文</th>"
                         "<th>其他稱呼</th><th>首次出現</th><th>出現段數</th><th>附註</th></tr>")
        others = "<br>".join(f"{escape(f['ja'])} → {escape(f['zh'])} <span class='muted'>({escape(f['form'])})</span>"
                             for f in e["forms"][1:])
        if e.get("called"):
            others += ("<br>" if others else "") + "<span class='muted'>被怎麼稱呼：</span>" + "；".join(
                escape(_called_text(c)) for c in e["called"][:12])
        if e.get("shared_forms"):
            others += ("<br>" if others else "") + "<span class='muted'>與他人共用：</span>" + escape("、".join(e["shared_forms"]))
        notes = "；".join(f["note"] for f in e["forms"] if f["note"])
        speech = e.get("speech")
        if speech:
            notes = (notes + "；" if notes else "") + f"自稱 {'、'.join(speech['first_person'] or []) or '—'}，{speech['formality']}"
        parts.append(f"<tr><td><b>{escape(e['name_zh'])}</b></td><td>{escape(e['name_ja'])}</td><td>{others}</td>"
                     f"<td>{escape(_pos_text(e['first'], labels))}</td><td>{e['mentions']}</td><td>{escape(notes)}</td></tr>")
    if kind is not None:
        parts.append("</table></div>")
    return _page(f"{title} 名詞索引", "".join(parts))


def relations_svg(rel: dict[str, Any], max_nodes: int = 18) -> str:
    nodes = rel["nodes"][:max_nodes]
    if not nodes:
        return "<p class='muted'>（沒有足夠資料）</p>"
    w = h = 640
    cx, cy, r = w / 2, h / 2, 250
    posn = {n["id"]: (cx + r * math.cos(2 * math.pi * i / len(nodes) - math.pi / 2),
                      cy + r * math.sin(2 * math.pi * i / len(nodes) - math.pi / 2)) for i, n in enumerate(nodes)}
    maxw = max([e["shared_scenes"] for e in rel["co_appearance"]] or [1])
    out = [f"<svg viewBox='0 0 {w} {h}' width='100%' style='max-width:{w}px' role='img' aria-label='人物關係圖'>"]
    for e in rel["co_appearance"]:
        if e["a"] in posn and e["b"] in posn:
            (x1, y1), (x2, y2) = posn[e["a"]], posn[e["b"]]
            out.append(f"<line class='edge' x1='{x1:.0f}' y1='{y1:.0f}' x2='{x2:.0f}' y2='{y2:.0f}' "
                       f"stroke-width='{0.5 + 4 * e['shared_scenes'] / maxw:.1f}' opacity='0.5'>"
                       f"<title>{escape(rel['labels'][e['a']])}—{escape(rel['labels'][e['b']])}：同場 {e['shared_scenes']} 個場景</title></line>")
    for f in rel["relations"]:
        for t in f["targets"]:
            if f["subject"] in posn and t in posn:
                (x1, y1), (x2, y2) = posn[f["subject"]], posn[t]
                out.append(f"<line class='rel' x1='{x1:.0f}' y1='{y1:.0f}' x2='{x2:.0f}' y2='{y2:.0f}' stroke-width='2' "
                           f"stroke-dasharray='5 4'><title>{escape(rel['labels'][f['subject']])} {escape(f['predicate'])}："
                           f"{escape(f['object'])}</title></line>")
    for n in nodes:
        x, y = posn[n["id"]]
        rad = 10 + min(n["scenes"], 40) / 2
        out.append(f"<circle cx='{x:.0f}' cy='{y:.0f}' r='{rad:.0f}'><title>{escape(n['label'])}：{n['scenes']} 個場景"
                   f"</title></circle><text x='{x:.0f}' y='{y + rad + 15:.0f}' text-anchor='middle'>{escape(n['label'])}</text>")
    out.append("</svg>")
    return "".join(out)


def relations_html(rel: dict[str, Any], labels: dict[float, str], title: str, scope: str) -> str:
    lab = rel["labels"]
    rows = "".join(
        f"<tr><td>{escape(lab[f['subject']])}</td><td>{escape(f['predicate'])}</td><td>{escape(f['object'])}</td>"
        f"<td>{escape(', '.join(lab[t] for t in f['targets']))}</td><td class='muted'>{escape(f['certainty'])}"
        f"{'' if f['type'] == 'objective' else ' · ' + escape(f['type'])}</td>"
        f"<td class='muted'>{escape(f['evidence'][:60])}<br>{escape(_pos_text(f['position'], labels))}</td></tr>"
        for f in sorted(rel["relations"], key=lambda f: f["position"]))
    co = "".join(f"<tr><td>{escape(lab[e['a']])}</td><td>{escape(lab[e['b']])}</td><td>{e['shared_scenes']}</td></tr>"
                 for e in rel["co_appearance"][:60])
    body = (f"<h1>{escape(title)}：人物關係圖</h1><p class='muted'>{escape(scope)}。實線＝同場出現（越粗越多），"
            f"虛線＝有原文證據的關係。滑過線條可看說明。</p>{relations_svg(rel)}"
            f"<h2>有原文證據的人物關係</h2><div class='wrap'><table><tr><th>人物</th><th>關係</th><th>內容</th><th>相關人物</th>"
            f"<th>確定度</th><th>原文證據／位置</th></tr>{rows}</table></div>"
            f"<h2>同場出現次數</h2><div class='wrap'><table><tr><th>人物</th><th>人物</th><th>共同場景數</th></tr>{co}</table></div>")
    return _page(f"{title} 人物關係圖", body)


def relations_mermaid(rel: dict[str, Any], max_edges: int = 40) -> str:
    lab = rel["labels"]
    ids = {cid: f"C{i}" for i, cid in enumerate(lab)}
    edges = []
    used = [n["id"] for n in rel["nodes"]]
    for e in rel["co_appearance"][:max_edges]:
        edges.append(f"  {ids[e['a']]} ---|同場{e['shared_scenes']}| {ids[e['b']]}")
    for f in rel["relations"][:max_edges]:
        for t in f["targets"]:
            edges.append(f"  {ids[f['subject']]} -.->|{f['predicate'][:12]}| {ids[t]}")
            used += [f["subject"], t]  # a fact may name someone with no counted scene: still needs a label
    lines = ["graph LR", *(f'  {ids[cid]}["{lab[cid]}"]' for cid in dict.fromkeys(used)), *edges]
    return "\n".join(lines) + "\n"
