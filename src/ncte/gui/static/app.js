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
let namesAll = false, lastRename = null;
async function namesView(id) {
  const rows = await api(`book/${id}/proposals${namesAll ? "?all=1" : ""}`);
  const pend = rows.filter(r => r.status === "pending");
  const banner = lastRename ? `<div class="card" style="border-color:var(--accent)">
      <strong>已翻譯的 ${lastRename.affected} 段還在用舊譯法「${esc(lastRename.old)}」</strong>（${esc(lastRename.label)} → ${esc(lastRename.zh)}）
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn primary" id="rnReplace">直接把「${esc(lastRename.old)}」換成「${esc(lastRename.zh)}」</button>
      <button class="btn" id="rnList">列出段落與重翻指令</button><button class="btn" id="rnSkip">先不要</button></div>
      <div class="muted small">直接替換只改字，不花錢；想讓語氣一起調整就重翻那些段落。人工修改過的段落不會被改。</div><div id="rnOut"></div></div>` : "";
  view.innerHTML = banner + `<div class="row" style="align-items:center;margin-bottom:10px"><strong>待決定 ${pend.length} 項</strong>
    ${pend.length ? `<button class="btn" id="acceptAll" title="每一項都選標有「建議」的那個譯法">全部採用建議</button>` : ""}
    <label class="small muted"><input type="checkbox" id="showAll" ${namesAll ? "checked" : ""}> 也顯示已套用、已決定的</label></div>
    ${rows.map(r => `<div class="card" data-pid="${r.id}">
      <div class="row" style="align-items:baseline;gap:10px"><strong>${esc(r.label)}</strong>
        ${chip(r.status === "pending" ? "c-warn" : r.status === "decided" ? "c-ok" : "c-unk", {pending: "待決定", decided: "已決定", applied: "已套用"}[r.status] || r.status)}
        <span class="muted small">出現 ${r.importance} 次　目前：${esc(r.decided_zh || r.chosen_zh)}</span></div>
      ${r.reason && r.options.length && r.reason !== r.options[0].reason ? `<div class="issue">${esc(r.reason)}</div>` : ""}
      <div style="margin:6px 0">${r.options.map(o => { const cur = (r.decided_zh || r.chosen_zh) === o.zh;
        return `<div><button class="btn ${o.zh === r.chosen_zh ? "primary" : ""}" data-zh="${esc(o.zh)}">${esc(o.zh)}</button>
        ${o.zh === r.chosen_zh ? chip("c-acc", "建議") : ""}${r.status === "decided" && cur ? chip("c-ok", "你選的") : ""}
        <span class="muted small">${esc(o.reason)}</span></div>`; }).join("")}
        ${r.status === "decided" && !r.options.some(o => o.zh === r.decided_zh) ? `<div>${chip("c-ok", "你選的")} ${esc(r.decided_zh)}</div>` : ""}</div>
      <div class="row" style="gap:6px"><input type="text" placeholder="自己填譯法" data-own><button class="btn" data-ownok>用這個</button></div>
      ${r.context.length ? `<details ${r.status === "pending" ? "open" : ""} style="margin-top:6px"><summary class="small muted">${r.context.some(c => c.zh) ? "原文例句與目前譯文" : "原文例句（尚未翻譯）"}</summary>
        ${r.context.map(c => `<div class="small" style="margin-top:4px"><div class="ja">§${c.at}　${esc(c.text)}</div>
          ${c.zh ? `<div class="muted">　目前譯文：${esc(c.zh)}</div>` : ""}</div>`).join("")}</details>` : ""}</div>`).join("")
    || `<p class="muted">沒有待決定的譯法。翻譯前的「定名」階段會在這裡列出需要你選的名字與稱呼。</p>`}`;
  const choose = async (pid, zh) => {
    try {
      const r = await api(`book/${id}/proposals/${pid}/decide`, {zh}); invalidate(id); toast(`${r.label} → ${r.zh}`);
      lastRename = r.affected ? {...r, pid} : null; namesView(id);
    } catch (e) { toast(e.message); }
  };
  view.querySelectorAll("[data-pid]").forEach(card => {
    const pid = card.dataset.pid;
    card.querySelectorAll("[data-zh]").forEach(b => b.onclick = () => choose(pid, b.dataset.zh));
    card.querySelector("[data-ownok]").onclick = () => { const v = card.querySelector("[data-own]").value.trim(); if (v) choose(pid, v); };
  });
  const aa = $("#acceptAll"); if (aa) aa.onclick = async () => { const r = await api(`book/${id}/proposals/accept-all`, {}).catch(e => toast(e.message)); if (r) { invalidate(id); toast(`已採用 ${r.accepted} 項`); namesView(id); } };
  $("#showAll").onchange = e => { namesAll = e.target.checked; namesView(id); };
  if (lastRename) {
    const L = lastRename;
    $("#rnSkip").onclick = () => { lastRename = null; namesView(id); };
    $("#rnReplace").onclick = async () => {
      try { const r = await api(`book/${id}/proposals/${L.pid}/apply`, {old: L.old, new: L.zh}); invalidate(id);
        toast(`已替換 ${r.replaced} 段` + (r.human_left.length ? `；${r.human_left.length} 段人工修改的請自己改` : "")); lastRename = null; namesView(id); }
      catch (e) { toast(e.message); }
    };
    $("#rnList").onclick = async () => {
      const r = await api(`book/${id}/proposals/${L.pid}/preview`, {old: L.old});
      $("#rnOut").innerHTML = `<div class="scroll" style="margin-top:8px">${r.units.map(u => `<div class="small"><span class="mono muted">§${u.ordinal}</span> ${u.human ? chip("c-acc", "人工") : ""}${esc(u.zh)}</div>`).join("")}</div>
        ${r.retranslate ? `<div class="small" style="margin-top:6px">重翻指令：<code>ncte retranslate ${esc(id)} --mode context ${esc(r.retranslate)}</code></div>` : ""}`;
    };
  }
}

// ------------------------------------------------------------ reading (spoiler-aware)
let POSIDX = null;
async function readingData(id) { return readingDataFor(id, "characters"); }
async function readingDataFor(id, what) {
  const d = await loadBook(id, what);
  const pos = $("#pos"), story = d.scenes.filter(s => s.story && s.a >= 0);
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
let storyJump = null;
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
    return head + `<div class="card" id="scene-${esc(s.id)}"><div class="muted small">場景 ${i + 1}・段 ${s.a}–${s.b}</div><div>${esc(s.summary) || '<span class="muted">（沒有摘要）</span>'}</div>
      <div class="muted small">出場：${s.who.map(c => esc(nm(c))).join("、") || "—"}</div>
      <details data-a="${s.a}" data-b="${s.b}"><summary class="small muted">看這個場景的原文與譯文</summary><div class="body muted small">載入中…</div></details></div>`; }).join("");
  view.innerHTML = `<div class="card scroll"><strong>誰在哪一章出場</strong> <span class="muted small">數字＝場景數；點名字看角色檔案</span>
    <div class="grid" style="grid-template-columns:110px repeat(${usedCh.length},minmax(30px,1fr));margin-top:8px">${grid}</div></div>
    <p class="muted small">摘要是模型整理的，可能有誤；展開可對照原文。封面、目次、後記等書籍資訊不列入。</p>${list || "<p>還沒讀到任何場景。</p>"}`;
  view.querySelectorAll("[data-p]").forEach(e => e.onclick = () => { peopleState.person = e.dataset.p; location.hash = `#/b/${id}/people`; });
  if (storyJump) { const el = document.getElementById("scene-" + storyJump); storyJump = null;
    if (el) { el.scrollIntoView({block: "center"}); el.style.outline = "2px solid var(--accent)"; } }
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
const G = {mode: "talk", mentions: false, n: 12, hideMinor: true, series: false, focus: null, pair: null};
const GROUPISH = /(たち|みんな|観客|生徒たち|周り|の声|チーム|們|觀眾|路人|大家)$/;
function layout(nodes, edges, W, H) {
  // small deterministic force layout: nodes that share many scenes end up close together
  const P = {}; nodes.forEach((c, i) => { const t = -Math.PI / 2 + i * 2 * Math.PI / nodes.length; P[c] = {x: W / 2 + 180 * Math.cos(t), y: H / 2 + 180 * Math.sin(t)}; });
  const k = Math.sqrt(W * H / Math.max(1, nodes.length)) * .55, mx = Math.max(1, ...edges.map(e => e.w));
  for (let it = 0; it < 300; it++) {
    const F = {}; nodes.forEach(c => F[c] = {x: 0, y: 0});
    nodes.forEach((a, i) => nodes.slice(i + 1).forEach(b => { const dx = P[a].x - P[b].x, dy = P[a].y - P[b].y, d = Math.max(1, Math.hypot(dx, dy)), f = k * k / d;
      F[a].x += dx / d * f; F[a].y += dy / d * f; F[b].x -= dx / d * f; F[b].y -= dy / d * f; }));
    edges.forEach(e => { const dx = P[e.a].x - P[e.b].x, dy = P[e.a].y - P[e.b].y, d = Math.max(1, Math.hypot(dx, dy)), f = d * d / k * (.3 + e.w / mx);
      F[e.a].x -= dx / d * f; F[e.a].y -= dy / d * f; F[e.b].x += dx / d * f; F[e.b].y += dy / d * f; });
    const temp = 30 * (1 - it / 300) + 1;
    nodes.forEach(c => { F[c].x += (W / 2 - P[c].x) * .04; F[c].y += (H / 2 - P[c].y) * .04;
      const d = Math.max(1, Math.hypot(F[c].x, F[c].y)); P[c].x += F[c].x / d * Math.min(d, temp); P[c].y += F[c].y / d * Math.min(d, temp);
      P[c].x = Math.min(W - 50, Math.max(50, P[c].x)); P[c].y = Math.min(H - 40, Math.max(40, P[c].y)); });
  }
  return P;
}
async function graphView(id) {
  const ov = await loadBook(id, "overview");
  const {d, upto, nm} = await readingDataFor(id, G.series ? "characters?series=1" : "characters");
  const scenes = d.scenes.filter(s => s.story && s.a <= upto);
  const members = s => G.mode === "talk" && !G.mentions ? s.speakers : s.who;
  const ok = c => d.characters[c] && d.characters[c].from <= upto && !d.characters[c].info &&
    !(G.hideMinor && (GROUPISH.test(d.characters[c].ja) || GROUPISH.test(d.characters[c].zh)));
  const cnt = {}; scenes.forEach(s => members(s).forEach(c => { if (ok(c)) cnt[c] = (cnt[c] || 0) + 1; }));
  let nodes = Object.keys(cnt).sort((a, b) => cnt[b] - cnt[a]);
  if (G.hideMinor) nodes = nodes.filter(c => cnt[c] >= 2);
  if (G.n) nodes = nodes.slice(0, G.n);
  const inSet = new Set(nodes);
  const pairScenes = {};
  scenes.forEach(s => { const m = members(s).filter(c => inSet.has(c)); m.forEach((a, i) => m.slice(i + 1).forEach(b => {
    const k = [a, b].sort().join("|"); (pairScenes[k] = pairScenes[k] || []).push(s); })); });
  const addr = d.address.filter(a => inSet.has(a.speaker) && inSet.has(a.to) && a.speaker !== a.to && (a.at == null || a.at <= upto));
  const edges = G.mode === "addr"
    ? Object.values(addr.reduce((m, a) => { const k = [a.speaker, a.to].sort().join("|"); (m[k] = m[k] || {a: a.speaker, b: a.to, w: 0}).w += a.uses; return m; }, {}))
    : Object.entries(pairScenes).map(([k, ss]) => { const [a, b] = k.split("|"); return {a, b, w: ss.length}; });
  const W = 820, H = 560, P = layout(nodes, edges, W, H), mx = Math.max(1, ...edges.map(e => e.w));
  const maxN = Math.max(1, ...nodes.map(c => cnt[c]));
  const near = new Set(G.focus ? [G.focus, ...edges.filter(e => e.a === G.focus || e.b === G.focus).flatMap(e => [e.a, e.b])] : nodes);
  const dim = c => near.has(c) ? 1 : .15;
  let svg = `<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" fill="var(--accent)"/></marker></defs>`;
  if (G.mode === "addr") {
    const seen = {};
    addr.forEach(a => { const key = a.speaker + a.to; seen[key] = (seen[key] || 0) + 1;
      const p = P[a.speaker], q = P[a.to], dx = q.x - p.x, dy = q.y - p.y, L = Math.max(1, Math.hypot(dx, dy)), ux = dx / L, uy = dy / L, o = seen[key] * 9, nx = -uy * o, ny = ux * o;
      const op = Math.min(dim(a.speaker), dim(a.to)), pk = [a.speaker, a.to].sort().join("|");
      svg += `<g data-pair="${esc(pk)}" style="cursor:pointer;opacity:${op}"><line x1="${p.x + ux * 30 + nx}" y1="${p.y + uy * 30 + ny}" x2="${q.x - ux * 34 + nx}" y2="${q.y - uy * 34 + ny}" stroke="var(--accent)" stroke-width="1.4" marker-end="url(#ah)"/>
        <text x="${(p.x + q.x) / 2 + nx * 2.2}" y="${(p.y + q.y) / 2 + ny * 2.2}" font-size="11" text-anchor="middle">${esc(a.ja)}</text></g>`; });
  } else {
    edges.forEach(e => { const op = Math.min(dim(e.a), dim(e.b)), pk = [e.a, e.b].sort().join("|");
      svg += `<g data-pair="${esc(pk)}" style="cursor:pointer;opacity:${op}"><line x1="${P[e.a].x}" y1="${P[e.a].y}" x2="${P[e.b].x}" y2="${P[e.b].y}" stroke="transparent" stroke-width="12"/>
        <line x1="${P[e.a].x}" y1="${P[e.a].y}" x2="${P[e.b].x}" y2="${P[e.b].y}" stroke="${G.pair === pk ? "var(--accent)" : "var(--muted)"}" stroke-opacity="${.25 + .6 * e.w / mx}" stroke-width="${1 + 7 * e.w / mx}"><title>${esc(nm(e.a))}・${esc(nm(e.b))}：${e.w} 個場景</title></line></g>`; });
  }
  nodes.forEach(c => { const r = 20 + 16 * Math.sqrt(cnt[c] / maxN);
    svg += `<g data-k="${esc(c)}" style="cursor:pointer;opacity:${dim(c)}"><circle cx="${P[c].x}" cy="${P[c].y}" r="${r}" fill="${G.focus === c ? "var(--accent-soft)" : "var(--panel)"}" stroke="var(--accent)" stroke-width="${G.focus === c ? 3 : 1.6}"/>
      <text x="${P[c].x}" y="${P[c].y + 5}" text-anchor="middle" font-size="13" font-weight="600">${esc(nm(c))}</text><title>${esc(nm(c))}：${cnt[c]} 個場景</title></g>`; });
  const sel = (name, opts, cur) => `<select data-ctl="${name}">${opts.map(([v, l]) => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  view.innerHTML = `<div class="row" style="align-items:center;margin-bottom:10px;gap:10px">
      <div class="seg"><button data-m="talk" class="${G.mode === "talk" ? "on" : ""}">一起出場</button><button data-m="addr" class="${G.mode === "addr" ? "on" : ""}">誰怎麼叫誰</button></div>
      ${G.mode === "talk" ? `<label class="small"><input type="checkbox" data-ctl="mentions" ${G.mentions ? "checked" : ""}> 只被提到也算</label>` : ""}
      ${sel("n", [[12, "12 人"], [20, "20 人"], [0, "全部"]], G.n)}
      <label class="small"><input type="checkbox" data-ctl="hideMinor" ${G.hideMinor ? "checked" : ""}> 隱藏路人、群體</label>
      <div class="seg"><button data-s="0" class="${G.series ? "" : "on"}">本集</button><button data-s="1" class="${G.series ? "on" : ""}">系列累計</button></div>
      ${G.focus ? `<button class="btn" data-clear>取消聚焦</button>` : ""}</div>
    <div class="row" style="align-items:flex-start"><div class="card scroll" style="flex:1 1 560px;padding:6px"><svg viewBox="0 0 ${W} ${H}" width="100%" style="min-width:540px">${svg}</svg></div>
    <div class="card" style="flex:0 1 340px;min-width:280px" id="gside">${graphSide(d, nm, upto, pairScenes, addr, cnt)}</div></div>
    <p class="muted small">${G.mode === "talk" ? (G.mentions ? "線＝同一場景出現（含只被提到）" : "線＝兩人在同一場景都有台詞") : "箭頭＝誰用什麼稱呼叫誰"}；圓圈大小＝出場場景數。點人物聚焦，點線看兩人的互動。${ov.title}</p>`;
  view.querySelectorAll("[data-m]").forEach(b => b.onclick = () => { G.mode = b.dataset.m; G.pair = null; graphView(id); });
  view.querySelectorAll("[data-s]").forEach(b => b.onclick = () => { G.series = b.dataset.s === "1"; graphView(id); });
  view.querySelectorAll("[data-ctl]").forEach(el => el.onchange = () => { const k = el.dataset.ctl; G[k] = el.type === "checkbox" ? el.checked : +el.value; graphView(id); });
  const clr = view.querySelector("[data-clear]"); if (clr) clr.onclick = () => { G.focus = null; graphView(id); };
  view.querySelectorAll("g[data-k]").forEach(g => g.onclick = () => { G.focus = G.focus === g.dataset.k ? null : g.dataset.k; G.pair = null; graphView(id); });
  view.querySelectorAll("g[data-pair]").forEach(g => g.onclick = () => { G.pair = g.dataset.pair; graphView(id); });
  view.querySelectorAll("[data-goscene]").forEach(a => a.onclick = e => { e.preventDefault(); storyJump = a.dataset.goscene; location.hash = `#/b/${id}/story`; });
  view.querySelectorAll("[data-profile]").forEach(a => a.onclick = e => { e.preventDefault(); peopleState.person = a.dataset.profile; location.hash = `#/b/${id}/people`; });
}
function graphSide(d, nm, upto, pairScenes, addr, cnt) {
  if (G.pair) {
    const [a, b] = G.pair.split("|"), ss = pairScenes[G.pair] || [];
    const names = c => [d.characters[c].ja, d.characters[c].zh, ...d.characters[c].aka].filter(x => x && x.length >= 2);
    const facts = [a, b].flatMap(x => d.characters[x].facts.filter(f => f.at <= upto && names(x === a ? b : a).some(n => (f.object + f.predicate).includes(n)))
      .map(f => ({...f, who: x})));
    const between = d.address.filter(t => (t.speaker === a && t.to === b) || (t.speaker === b && t.to === a)).filter(t => t.at == null || t.at <= upto)
      .sort((x, y) => (x.at ?? -1) - (y.at ?? -1));
    return `<strong>${esc(nm(a))} ⇄ ${esc(nm(b))}</strong>
      <div style="margin-top:8px"><div class="muted small">互相怎麼稱呼（依首次出現）</div>${between.map(t => `<div>${esc(nm(t.speaker))} → ${esc(nm(t.to))}：<span class="ja">${esc(t.ja)}</span>（${esc(t.zh || "")}）<span class="muted small">${t.at < 0 ? "前集" : "§" + t.at}・×${t.uses}</span></div>`).join("") || '<div class="muted">—</div>'}</div>
      <div style="margin-top:8px"><div class="muted small">相關事實</div>${facts.map(f => `<div class="small">${esc(nm(f.who))} ${esc(f.predicate)}：${esc(f.object)} ${chip(f.certainty === "explicit" ? "c-ok" : "c-warn", f.certainty === "explicit" ? "明寫" : "推測")}<div class="ja muted">${esc(f.quote)}</div></div>`).join("") || '<div class="muted">—</div>'}</div>
      <div style="margin-top:8px"><div class="muted small">一起出場的場景（${ss.length}）</div>${ss.map(s => `<div class="small">${s.a < 0 ? "<span class='muted'>前集</span> " : `<a href="#" data-goscene="${esc(s.id)}">§${s.a}</a> `}${esc((s.summary || "").slice(0, 60))}</div>`).join("")}</div>`;
  }
  if (G.focus && d.characters[G.focus]) {
    const c = d.characters[G.focus];
    const partners = Object.entries(pairScenes).filter(([k]) => k.split("|").includes(G.focus)).map(([k, ss]) => [k.split("|").find(x => x !== G.focus), ss.length]).sort((x, y) => y[1] - x[1]);
    return `<strong>${esc(c.zh)}</strong> <span class="ja muted">${esc(c.ja)}</span><div class="muted small">${cnt[G.focus] || 0} 個場景</div>
      <div style="margin-top:8px">${partners.map(([o, n]) => `<div>${esc(nm(o))} <span class="muted small">${n} 個場景</span></div>`).join("") || '<div class="muted">—</div>'}</div>
      <p><a href="#" data-profile="${esc(G.focus)}">看角色檔案</a></p>`;
  }
  return `<div class="muted">點人物：只顯示他和他的連線。<br>點兩人之間的線：看兩人怎麼互相稱呼、相關事實、一起出場的場景。</div>`;
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
