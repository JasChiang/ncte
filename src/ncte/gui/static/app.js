"use strict";
const TOKEN = document.querySelector('meta[name="ncte-token"]').content;
const $ = s => document.querySelector(s);
const view = $("#view");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

async function api(path, body) {
  const opt = {headers: {"X-NCTE-Token": TOKEN}};
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/api/" + path, opt);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), 2600);
}
const STATUS = {explicit: ["c-ok", "明寫"], inferred: ["c-warn", "推測"], unknown: ["c-unk", "未定"], human_verified: ["c-acc", "人工"]};
const chip = (cls, text) => `<span class="chip ${cls}">${esc(text)}</span>`;

// ------------------------------------------------------------ routing
const TABS = [["翻譯"], ["overview", "概況"], ["review", "對照審稿"], ["names", "定名"], ["閱讀"],
              ["story", "故事進度"], ["people", "角色檔案"], ["graph", "人物關係圖"], ["glossary", "名詞與稱呼"]];
const READING = new Set(["story", "people", "graph"]);
const cache = {};
async function loadBook(id, what) {
  const key = id + "/" + what;
  if (!cache[key]) cache[key] = await api(`book/${id}/${what}`);
  return cache[key];
}
function invalidate(id) { for (const k of Object.keys(cache)) if (k.startsWith(id + "/")) delete cache[k]; }

window.addEventListener("hashchange", route);
async function route() {
  const [, b, id, tab = "overview"] = location.hash.replace(/^#\/?/, "#/").split("/");
  try {
    if (b !== "b" || !id) { $("#tabs").hidden = true; $("#posbar").hidden = true; $("#crumb").textContent = ""; return await libraryView(); }
    const tabs = $("#tabs"); tabs.hidden = false;
    tabs.innerHTML = TABS.map(t => t.length === 1 ? `<span class="grp">${t[0]}</span>`
      : `<a href="#/b/${id}/${t[0]}" class="${t[0] === tab ? "on" : ""}">${t[1]}</a>`).join("");
    $("#posbar").hidden = !READING.has(tab);
    const ov = await loadBook(id, "overview");
    $("#crumb").textContent = ov.title;
    await ({overview: overviewView, review: reviewView, names: namesView, story: storyView, people: peopleView,
            graph: graphView, glossary: glossaryView}[tab] || overviewView)(id);
  } catch (e) { view.innerHTML = `<div class="card">讀取失敗：${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------ library
async function libraryView() {
  const lib = await api("library");
  view.innerHTML = `<h2>書庫</h2>` + (lib.map(s => `<div class="card"><strong>${esc(s.title)}</strong>
    <span class="muted small">　${s.volumes.length} 集</span>
    ${s.volumes.map(v => { const p = v.translatable ? Math.round(100 * v.translated / v.translatable) : 0;
      return `<div style="margin-top:8px"><a href="#/b/${esc(v.book_id)}/overview">第 ${v.volume_index} 集　${esc(v.title)}</a>
      <span class="muted small">　${v.translatable == null ? "（找不到資料夾）" : `${v.translated} / ${v.translatable} 段`}</span>
      <div class="bar"><i style="width:${p}%"></i></div></div>`; }).join("")}</div>`).join("")
    || `<p class="muted">書庫是空的。用 <code>ncte import 書.epub</code> 匯入。</p>`);
}

// ------------------------------------------------------------ overview
async function overviewView(id) {
  const o = await loadBook(id, "overview");
  const stat = (label, v, extra = "") => `<div class="card stat"><span class="muted small">${label}</span><b>${v}</b>${extra}</div>`;
  view.innerHTML = `<div class="row">
    ${stat("已翻譯", `${o.translated} / ${o.translatable}`)}${stat("已花費", "US$" + o.spent_usd.toFixed(2))}
    ${stat("有疑點的段落", o.flagged_units, `<a class="small" href="#/b/${id}/review?f=flagged">去看</a>`)}
    ${stat("待決定的譯法", o.pending_names, `<a class="small" href="#/b/${id}/names">去決定</a>`)}
    ${stat("說話者未定", o.unknown_speakers)}${stat("譯註", o.notes)}${stat("人工修改", o.human_edits)}</div>
    <div class="card"><strong>執行紀錄</strong><div class="scroll"><table><tr><th>run</th><th>模式</th><th>模型</th><th>狀態</th><th>待處理</th><th>費用</th></tr>
    ${o.runs.map(r => `<tr><td class="mono small">${esc(r.id)}</td><td>${esc(r.mode)}</td><td>${esc(r.model)}</td>
      <td>${esc(r.status)}</td><td class="mono">${r.pending}</td><td class="mono">$${(r.cost_usd || 0).toFixed(3)}</td></tr>`).join("")}
    </table></div><p class="muted small">繼續中斷的翻譯：<code>ncte resume --run-id …</code>　匯出：<code>ncte export ${esc(id)}</code></p></div>`;
}

// ------------------------------------------------------------ review
const reviewState = {chapter: null, filter: "flagged"};
async function reviewView(id) {
  const m = location.hash.match(/\?f=(\w+)/); if (m) reviewState.filter = m[1];
  const chs = await loadBook(id, "chapters");
  if (reviewState.chapter == null) reviewState.chapter = (chs.find(c => c.kind === "story") || chs[0] || {}).index;
  const ch = chs.find(c => c.index === reviewState.chapter);
  const wholeBook = reviewState.filter !== "all";
  const q = wholeBook ? `filter=${reviewState.filter}` : `start=${ch.start}&end=${ch.end}`;
  const data = await api(`book/${id}/units?${q}`);
  const F = [["flagged", "有疑點（全書）"], ["unknown", "說話者未定（全書）"], ["notes", "譯註與梗（全書）"], ["human", "人工修改（全書）"], ["all", "逐章全部"]];
  view.innerHTML = `<div class="row" style="align-items:center;margin-bottom:10px">
    <div class="seg">${F.map(f => `<button data-f="${f[0]}" class="${f[0] === reviewState.filter ? "on" : ""}">${f[1]}</button>`).join("")}</div>
    ${wholeBook ? "" : `<select id="chap">${chs.map(c => `<option value="${c.index}" ${c.index === reviewState.chapter ? "selected" : ""}>${esc(c.title)}${c.kind === "story" ? "" : "（書籍資訊）"}</option>`).join("")}</select>`}
    <span class="muted small">${data.units.length} 段${data.truncated ? "（只顯示前 400 段）" : ""}；點譯文可以修改</span></div>
    <div class="card" style="padding:0"><div class="pair muted small"><div>段</div><div>原文</div><div>譯文</div><div>說話者</div></div>
    ${data.units.map(rowHtml).join("") || `<p class="muted" style="padding:12px">沒有符合的段落。</p>`}</div>`;
  view.querySelectorAll("[data-f]").forEach(b => b.onclick = () => { reviewState.filter = b.dataset.f; history.replaceState(null, "", `#/b/${id}/review`); reviewView(id); });
  const sel = $("#chap"); if (sel) sel.onchange = () => { reviewState.chapter = +sel.value; reviewView(id); };
  const byId = Object.fromEntries(data.units.map(u => [u.id, u]));
  view.querySelectorAll(".pair[data-id]").forEach(el => bindRow(id, el, byId[el.dataset.id]));
}
function rowHtml(u) {
  const cls = u.status === "human" ? "human" : u.findings.length ? "flag" : "";
  return `<div class="pair ${cls}" data-id="${esc(u.id)}"><div class="mono small muted">${u.ordinal}</div>
    <div class="ja">${esc(u.ja)}</div>
    <div><div class="zh">${u.zh == null ? chip("c-bad", "未翻譯") : esc(u.zh)}</div>
      ${u.status === "human" ? chip("c-acc", "人工修改") + ` <button class="btn small" data-act="revert">改回機器譯文</button>` : ""}
      ${u.note ? `<div class="note">（註：${esc(u.note)}） <button class="btn small" data-act="drop-note">刪除譯註</button></div>` : ""}
      ${u.wordplay.map(w => `<div class="note">梗「${esc(w.term)}」：${esc(w.explanation)}</div>`).join("")}
      ${u.findings.map(f => `<div class="issue">⚑ ${esc(f.type)}：${esc(f.evidence)}</div>`).join("")}</div>
    <div>${u.quotes.map(q => { const [c, l] = STATUS[q.status] || ["c-unk", q.status];
      return `<div>${esc(q.speaker || "？")} ${chip(c, l)}</div>`; }).join("") || `<span class="muted small">敘述</span>`}</div></div>`;
}
function bindRow(id, el, u) {
  const zh = el.querySelector(".zh");
  zh.onclick = () => {
    if (el.querySelector("textarea")) return;
    zh.innerHTML = `<textarea rows="3">${esc(u.edit)}</textarea>
      <div class="muted small">⟦數字⟧ 是排版標記，請保留。</div>
      <div style="display:flex;gap:6px;margin-top:4px"><button class="btn primary" data-s>儲存</button><button class="btn" data-c>取消</button></div>`;
    const ta = zh.querySelector("textarea"); ta.focus();
    zh.querySelector("[data-c]").onclick = e => { e.stopPropagation(); zh.textContent = u.zh ?? ""; };
    zh.querySelector("[data-s]").onclick = async e => {
      e.stopPropagation();
      try {
        const r = await api(`book/${id}/units/${u.id}/edit`, {text: ta.value, pre: u.edit_pre, post: u.edit_post});
        invalidate(id); toast(r.warnings.length ? `已儲存（提醒：${r.warnings.map(w => w.type).join("、")}）` : "已儲存為人工譯文");
        reviewView(id);
      } catch (err) { toast("儲存失敗：" + err.message); }
    };
  };
  el.querySelectorAll("[data-act]").forEach(b => b.onclick = async e => {
    e.stopPropagation();
    try { await api(`book/${id}/units/${u.id}/${b.dataset.act}`, {}); invalidate(id); toast("已更新"); reviewView(id); }
    catch (err) { toast(err.message); }
  });
}

// ------------------------------------------------------------ naming
let namesAll = false;
async function namesView(id) {
  const rows = await api(`book/${id}/proposals${namesAll ? "?all=1" : ""}`);
  const pend = rows.filter(r => r.status === "pending");
  view.innerHTML = `<div class="row" style="align-items:center;margin-bottom:10px"><strong>待決定 ${pend.length} 項</strong>
    ${pend.length ? `<button class="btn" id="acceptAll">全部採用建議</button>` : ""}
    <label class="small muted"><input type="checkbox" id="showAll" ${namesAll ? "checked" : ""}> 也顯示已套用、已決定的</label></div>
    ${rows.map(r => `<div class="card" data-pid="${r.id}">
      <div class="row" style="align-items:baseline;gap:10px"><strong>${esc(r.label)}</strong>
        ${chip(r.status === "pending" ? "c-warn" : r.status === "decided" ? "c-ok" : "c-unk", {pending: "待決定", decided: "已決定", applied: "已套用"}[r.status] || r.status)}
        <span class="muted small">出現 ${r.importance} 次　目前：${esc(r.decided_zh || r.chosen_zh)}</span></div>
      ${r.reason && r.options.length && r.reason !== r.options[0].reason ? `<div class="issue">${esc(r.reason)}</div>` : ""}
      <div style="margin:6px 0">${r.options.map(o => `<div><button class="btn" data-zh="${esc(o.zh)}">${esc(o.zh)}</button>
        <span class="muted small">${esc(o.reason)}</span></div>`).join("")}</div>
      <div class="row" style="gap:6px"><input type="text" placeholder="自己填譯法" data-own><button class="btn" data-ownok>用這個</button></div>
      ${r.context.length ? `<details ${r.status === "pending" ? "open" : ""} style="margin-top:6px"><summary class="small muted">原文例句</summary>
        ${r.context.map(c => `<div class="ja small">§${c.at}　${esc(c.text)}</div>`).join("")}</details>` : ""}</div>`).join("")
    || `<p class="muted">沒有待決定的譯法。翻譯前的「定名」階段會在這裡列出需要你選的名字與稱呼。</p>`}`;
  const choose = async (pid, zh) => {
    try { const r = await api(`book/${id}/proposals/${pid}/decide`, {zh}); invalidate(id); toast(`${r.label} → ${r.zh}`); namesView(id); }
    catch (e) { toast(e.message); }
  };
  view.querySelectorAll("[data-pid]").forEach(card => {
    const pid = card.dataset.pid;
    card.querySelectorAll("[data-zh]").forEach(b => b.onclick = () => choose(pid, b.dataset.zh));
    card.querySelector("[data-ownok]").onclick = () => { const v = card.querySelector("[data-own]").value.trim(); if (v) choose(pid, v); };
  });
  const aa = $("#acceptAll"); if (aa) aa.onclick = async () => { const r = await api(`book/${id}/proposals/accept-all`, {}).catch(e => toast(e.message)); if (r) { invalidate(id); toast(`已採用 ${r.accepted} 項`); namesView(id); } };
  $("#showAll").onchange = e => { namesAll = e.target.checked; namesView(id); };
}

// ------------------------------------------------------------ reading (spoiler-aware)
let POSIDX = null;
async function readingData(id) {
  const d = await loadBook(id, "characters");
  const pos = $("#pos"), story = d.scenes.filter(s => s.story);
  pos.max = story.length;
  if (POSIDX == null || POSIDX > story.length) POSIDX = story.length;
  pos.value = POSIDX;
  const upto = POSIDX >= story.length ? Infinity : story[POSIDX].a - 1;
  const label = POSIDX >= story.length ? "全書（不隱藏）" : `第 ${POSIDX + 1} 個場景之前・${(d.chapters.find(c => c.index === story[POSIDX].ch) || {}).title || ""}`;
  $("#posl").textContent = label;
  pos.oninput = () => { POSIDX = +pos.value; route(); };
  return {d, upto, seen: story.filter(s => s.a <= upto), nm: cid => (d.characters[cid] || {}).zh || cid};
}
function topChars(d, seen, upto, n) {
  const cnt = {}; seen.forEach(s => s.who.forEach(c => cnt[c] = (cnt[c] || 0) + 1));
  return Object.keys(cnt).filter(c => d.characters[c] && d.characters[c].from <= upto && !d.characters[c].info)
    .sort((a, b) => cnt[b] - cnt[a]).slice(0, n);
}
async function storyView(id) {
  const {d, upto, seen, nm} = await readingData(id);
  const top = topChars(d, seen, upto, 14);
  const usedCh = [...new Set(d.scenes.filter(s => s.story && s.who.length).map(s => s.ch))];
  const cnt = {}; seen.forEach(s => s.who.forEach(c => { const k = c + "|" + s.ch; cnt[k] = (cnt[k] || 0) + 1; }));
  const title = i => (d.chapters.find(c => c.index === i) || {}).title || i;
  let grid = `<div></div>` + usedCh.map(i => `<div class="h" title="${esc(title(i))}">${esc(String(title(i)).slice(0, 6))}</div>`).join("");
  top.forEach(c => { grid += `<div class="nm" data-p="${esc(c)}">${esc(nm(c))}</div>` + usedCh.map(i => {
    const x = cnt[c + "|" + i] || 0, a = x ? Math.min(.2 + x * .18, 1) : 0;
    return `<div class="cell" style="background:color-mix(in srgb,var(--accent) ${a * 100}%,var(--unk-soft));${a > .6 ? "color:var(--panel)" : ""}">${x || ""}</div>`; }).join(""); });
  let last = null;
  const list = seen.map((s, i) => { const head = s.ch !== last ? `<h3>${esc(title(s.ch))}</h3>` : ""; last = s.ch;
    return head + `<div class="card"><div class="muted small">場景 ${i + 1}・段 ${s.a}–${s.b}</div><div>${esc(s.summary) || '<span class="muted">（沒有摘要）</span>'}</div>
      <div class="muted small">出場：${s.who.map(c => esc(nm(c))).join("、") || "—"}</div>
      <details data-a="${s.a}" data-b="${s.b}"><summary class="small muted">看這個場景的原文與譯文</summary><div class="body muted small">載入中…</div></details></div>`; }).join("");
  view.innerHTML = `<div class="card scroll"><strong>誰在哪一章出場</strong> <span class="muted small">數字＝場景數；點名字看角色檔案</span>
    <div class="grid" style="grid-template-columns:110px repeat(${usedCh.length},minmax(30px,1fr));margin-top:8px">${grid}</div></div>
    <p class="muted small">摘要是模型整理的，可能有誤；展開可對照原文。封面、目次、後記等書籍資訊不列入。</p>${list || "<p>還沒讀到任何場景。</p>"}`;
  view.querySelectorAll("[data-p]").forEach(e => e.onclick = () => { peopleState.person = e.dataset.p; location.hash = `#/b/${id}/people`; });
  view.querySelectorAll("details[data-a]").forEach(det => det.addEventListener("toggle", async () => {
    if (!det.open || det.dataset.loaded) return; det.dataset.loaded = 1;
    const r = await api(`book/${id}/units?start=${det.dataset.a}&end=${det.dataset.b}`);
    det.querySelector(".body").innerHTML = r.units.map(u => `<div class="row" style="gap:12px;margin-top:6px;color:var(--ink)">
      <div class="ja" style="flex:1 1 300px">${esc(u.ja)}</div><div style="flex:1 1 300px">${esc(u.zh || "")}</div></div>`).join("");
  }));
}
const peopleState = {person: null};
async function peopleView(id) {
  const {d, upto, seen, nm} = await readingData(id);
  const n = seen.length || 1;
  const vis = Object.entries(d.characters).filter(([, c]) => c.from <= upto);
  const tier = c => c.scenes >= Math.max(5, n * .15) || c.lines >= 30 ? "main" : c.scenes >= 3 || c.lines >= 5 ? "sub" : "minor";
  vis.sort((a, b) => (b[1].scenes + b[1].lines / 5) - (a[1].scenes + a[1].lines / 5));
  const groups = {main: [], sub: [], minor: [], info: []};
  vis.forEach(([k, c]) => groups[c.info ? "info" : tier(c)].push([k, c]));
  if (!peopleState.person || !d.characters[peopleState.person] || d.characters[peopleState.person].from > upto)
    peopleState.person = (groups.main[0] || groups.sub[0] || [])[0];
  const btn = ([k, c]) => `<button data-k="${esc(k)}" class="${k === peopleState.person ? "on" : ""}">${esc(c.zh)}<span class="cnt">${c.scenes}</span></button>`;
  let card = "<p>沒有角色。</p>";
  const p = d.characters[peopleState.person];
  if (p) {
    const pid = peopleState.person;
    const addr = d.address.filter(a => a.at == null || a.at <= upto);
    const called = addr.filter(a => a.to === pid && a.speaker !== pid), calls = addr.filter(a => a.speaker === pid);
    const co = {}; seen.forEach(s => { if (s.who.includes(pid)) s.who.forEach(o => { if (o !== pid) co[o] = (co[o] || 0) + 1; }); });
    const F = p.facts.filter(f => f.at <= upto), EX = p.examples.filter(e => e.at <= upto);
    const ftype = {objective: "客觀", character_belief: "角色認為", narrator_claim: "敘述者說"};
    card = `<div class="row" style="align-items:baseline;gap:10px"><h2 style="margin:0">${esc(p.zh)}</h2><span class="ja">${esc(p.ja)}</span>
      <span class="muted small">初登場 §${p.from}・出場 ${p.scenes} 場景・台詞 ${p.lines} 句</span></div>
      ${p.aka.length ? `<div class="muted small">其他稱呼：<span class="ja">${p.aka.map(esc).join("・")}</span></div>` : ""}
      <div class="row" style="margin-top:12px;gap:24px">
        <div style="flex:1 1 240px"><strong>被怎麼叫</strong>${called.map(a => `<div>${esc(nm(a.speaker))} → <span class="ja">${esc(a.ja)}</span>（${esc(a.zh || "")}）<span class="muted small">×${a.uses}</span></div>`).join("") || '<div class="muted">—</div>'}</div>
        <div style="flex:1 1 240px"><strong>怎麼叫別人</strong>${calls.map(a => `<div>→ ${a.to === pid ? "自稱" : esc(nm(a.to))}：<span class="ja">${esc(a.ja)}</span>（${esc(a.zh || "")}）<span class="muted small">×${a.uses}</span></div>`).join("") || '<div class="muted">—</div>'}</div></div>
      <div style="margin-top:12px"><strong>常一起出場</strong><div>${Object.entries(co).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([o, k]) => `<a href="#" data-k="${esc(o)}">${esc(nm(o))}</a> <span class="muted small">${k}</span>`).join("　") || "—"}</div></div>
      <div style="margin-top:12px"><strong>說話例句</strong>${EX.map(e => `<div class="ja">${esc(e.text)}</div>`).join("") || '<div class="muted">—</div>'}</div>
      <div style="margin-top:12px"><strong>已知事實</strong> <span class="muted small">${F.length} 筆</span><div class="scroll"><table><tr><th>段</th><th>內容</th><th>類型</th><th>原文依據</th></tr>
      ${F.map(f => `<tr><td class="mono small">${f.at}</td><td>${esc(f.predicate)}：${esc(f.object)}</td><td>${chip("c-acc", ftype[f.type] || f.type)}${chip(f.certainty === "explicit" ? "c-ok" : "c-warn", f.certainty === "explicit" ? "明寫" : "推測")}</td><td class="ja small">${esc(f.quote)}</td></tr>`).join("")}</table></div></div>`;
  }
  view.innerHTML = `<div class="row" style="align-items:flex-start"><div class="card plist" style="flex:0 1 210px;max-height:80vh;overflow:auto">
    <div class="muted small">主要角色</div>${groups.main.map(btn).join("")}<div class="muted small" style="margin-top:8px">次要角色</div>${groups.sub.map(btn).join("")}
    <details><summary class="muted small">其他 ${groups.minor.length} 個</summary>${groups.minor.map(btn).join("")}</details>
    <details><summary class="muted small">書籍資訊裡的人名 ${groups.info.length} 個</summary>${groups.info.map(btn).join("")}</details></div>
    <div class="card" style="flex:1 1 480px">${card}</div></div>`;
  view.querySelectorAll("[data-k]").forEach(b => b.onclick = e => { e.preventDefault(); peopleState.person = b.dataset.k; peopleView(id); });
}
let gmode = "co";
async function graphView(id) {
  const {d, upto, seen, nm} = await readingData(id);
  const top = topChars(d, seen, upto, 10), W = 760, H = 520, R = 200;
  const P = {}; top.forEach((c, i) => { const t = -Math.PI / 2 + i * 2 * Math.PI / top.length; P[c] = {x: W / 2 + R * Math.cos(t), y: H / 2 + R * Math.sin(t)}; });
  let s = `<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" fill="var(--accent)"/></marker></defs>`;
  if (gmode === "co") {
    const co = {}; seen.forEach(sc => { const w = sc.who.filter(c => P[c]); w.forEach((a, i) => w.slice(i + 1).forEach(b => { const k = [a, b].sort().join("|"); co[k] = (co[k] || 0) + 1; })); });
    const mx = Math.max(1, ...Object.values(co));
    Object.entries(co).forEach(([k, n]) => { const [a, b] = k.split("|");
      s += `<line x1="${P[a].x}" y1="${P[a].y}" x2="${P[b].x}" y2="${P[b].y}" stroke="var(--muted)" stroke-opacity="${.15 + .6 * n / mx}" stroke-width="${1 + 7 * n / mx}"><title>${esc(nm(a))}・${esc(nm(b))}：同場 ${n} 次</title></line>`; });
  } else {
    const seenPair = {};
    d.address.filter(a => P[a.speaker] && P[a.to] && a.speaker !== a.to && (a.at == null || a.at <= upto)).forEach(a => {
      const key = a.speaker + a.to; seenPair[key] = (seenPair[key] || 0) + 1;
      const p = P[a.speaker], q = P[a.to], dx = q.x - p.x, dy = q.y - p.y, L = Math.hypot(dx, dy), ux = dx / L, uy = dy / L, o = seenPair[key] * 10, nx = -uy * o, ny = ux * o;
      s += `<line x1="${p.x + ux * 34 + nx}" y1="${p.y + uy * 34 + ny}" x2="${q.x - ux * 38 + nx}" y2="${q.y - uy * 38 + ny}" stroke="var(--accent)" stroke-width="1.4" marker-end="url(#ah)"></line>
        <text x="${(p.x + q.x) / 2 + nx * 2.4}" y="${(p.y + q.y) / 2 + ny * 2.4}" font-size="11" text-anchor="middle">${esc(a.ja)}<title>${esc(a.zh || "")} ×${a.uses}</title></text>`; });
  }
  top.forEach(c => { s += `<g style="cursor:pointer" data-k="${esc(c)}"><circle cx="${P[c].x}" cy="${P[c].y}" r="32" fill="var(--panel)" stroke="var(--accent)" stroke-width="1.6"/><text x="${P[c].x}" y="${P[c].y + 5}" text-anchor="middle" font-size="13" font-weight="600">${esc(nm(c))}</text></g>`; });
  view.innerHTML = `<div class="row" style="align-items:center;margin-bottom:10px"><div class="seg">
    <button data-g="co" class="${gmode === "co" ? "on" : ""}">同場次數</button><button data-g="addr" class="${gmode === "addr" ? "on" : ""}">稱呼（有方向）</button></div>
    <span class="muted small">出場最多的 10 人；只算讀到的位置以前。滑過線條看數字，點人物看檔案。</span></div>
    <div class="card scroll"><svg viewBox="0 0 ${W} ${H}" width="100%" style="min-width:560px">${s}</svg></div>`;
  view.querySelectorAll("[data-g]").forEach(b => b.onclick = () => { gmode = b.dataset.g; graphView(id); });
  view.querySelectorAll("g[data-k]").forEach(g => g.onclick = () => { peopleState.person = g.dataset.k; location.hash = `#/b/${id}/people`; });
}

// ------------------------------------------------------------ names & address terms
async function glossaryView(id) {
  const [g, d] = await Promise.all([loadBook(id, "glossary"), loadBook(id, "characters")]);
  const nm = c => (d.characters[c] || {}).zh || c;
  view.innerHTML = `<div class="row"><div class="card" style="flex:1 1 440px"><strong>名詞表</strong> <span class="muted small">${g.length} 筆</span>
      <input type="search" id="gq" placeholder="搜尋日文或中文" style="margin-left:8px"><div class="scroll"><table id="gt"></table></div></div>
    <div class="card" style="flex:1 1 440px"><strong>誰怎麼叫誰</strong> <span class="muted small">${d.address.length} 組</span><div class="scroll"><table>
      <tr><th>說話者 → 對象</th><th>稱呼</th><th>譯法</th><th>次數</th></tr>
      ${d.address.map(a => `<tr><td>${esc(nm(a.speaker))} → ${a.speaker === a.to ? "（自稱）" : esc(nm(a.to))}</td><td class="ja">${esc(a.ja)}</td>
        <td>${esc(a.zh || "")} ${a.origin === "human" ? chip("c-ok", "人工") : a.origin === "naming" ? chip("c-acc", "定名") : ""}</td><td class="mono">${a.uses}</td></tr>`).join("")}</table></div></div></div>`;
  const draw = q => { $("#gt").innerHTML = `<tr><th>日文</th><th>譯名</th><th>類別</th><th>狀態</th></tr>` + g.filter(x => !q || x.ja.includes(q) || x.zh.includes(q))
    .map(x => `<tr><td class="ja">${esc(x.ja)}</td><td>${esc(x.zh)}</td><td class="muted small">${esc(x.category)}</td>
      <td>${x.verified ? chip("c-ok", "已確認") : chip("c-unk", esc(x.origin))}</td></tr>`).join(""); };
  draw(""); $("#gq").oninput = e => draw(e.target.value.trim());
}

route();
