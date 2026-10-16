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
  // NCTE was restarted: this page still carries the previous start's token
  if (r.status === 403 && /token/.test(j.error || "")) { location.reload(); throw new Error("NCTE 已重新啟動，重新載入頁面…"); }
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
  view.classList.remove("wide");
  const [, b, id, rawTab = "overview"] = location.hash.replace(/^#\/?/, "#/").split("/");
  const tab = rawTab.split("?")[0];  // 「review?f=flagged」 links to the review page with a filter
  try {
    if (b === "settings") { $("#tabs").hidden = true; $("#posbar").hidden = true; $("#crumb").textContent = "設定"; return await settingsView(); }
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
  view.innerHTML = `<h2>書庫</h2>
    <div class="card drop" id="drop"><strong>加入一本書</strong>
      <p class="muted small">把日文 EPUB（無 DRM）拖到這裡，或 <label class="link">選擇檔案<input type="file" id="file" accept=".epub" hidden></label>。檔案只存在這台電腦。</p>
      <div id="upl"></div></div>` + (lib.map(s => `<div class="card"><strong>${esc(s.title)}</strong>
    <span class="muted small">　${s.volumes.length} 集</span>
    ${s.volumes.map(v => { const p = v.translatable ? Math.round(100 * v.translated / v.translatable) : 0;
      return `<div style="margin-top:8px"><a href="#/b/${esc(v.book_id)}/overview">第 ${v.volume_index} 集　${esc(v.title)}</a>
      <span class="muted small">　${v.translatable == null ? "（找不到資料夾）" : `${v.translated} / ${v.translatable} 段`}</span>
      <div class="bar"><i style="width:${p}%"></i></div></div>`; }).join("")}</div>`).join("")
    || `<p class="muted">書庫是空的。先在上面加入一本書。</p>`);
  const drop = $("#drop"), file = $("#file");
  file.onchange = () => file.files[0] && upload(file.files[0]);
  drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); const f = e.dataTransfer.files[0]; if (f) upload(f); };
}
async function upload(f) {
  const box = $("#upl"); box.innerHTML = `<p class="muted">上傳並檢查「${esc(f.name)}」…</p>`;
  try {
    const r = await fetch("/api/upload", {method: "POST", body: f, headers: {"X-NCTE-Token": TOKEN, "X-Filename": encodeURIComponent(f.name)}});
    const u = await r.json();
    if (r.status === 403 && /token/.test(u.error || "")) { location.reload(); return; }
    if (!r.ok) throw new Error(u.error || r.statusText);
    if (u.imported_as) { box.innerHTML = `<p>這本書已經在書庫裡了：<a href="#/b/${esc(u.imported_as)}/overview">打開</a></p>`; return; }
    const s = u.suggestion;
    box.innerHTML = `<p><strong>${esc(u.title)}</strong></p>
      <p class="small">放在哪個系列？同一系列的各集共用角色、譯名和劇情記憶。</p>
      <label><input type="radio" name="ser" value="" ${s ? "" : "checked"}> 單獨一本（新系列）</label><br>
      ${s ? `<label><input type="radio" name="ser" value="${esc(s.series)}" checked> 「${esc(s.series)}」${s.existing ? "（書庫已有）" : "（新系列）"}${s.volume != null ? `第 ${s.volume} 集` : ""}
        <span class="muted small">— 依據：${esc(s.evidence)}</span></label><br>` : ""}
      ${u.series.filter(t => !s || t !== s.series).map(t => `<label><input type="radio" name="ser" value="${esc(t)}"> 「${esc(t)}」</label><br>`).join("")}
      <p class="small">集數 <input type="text" id="vol" size="4" value="${s && s.volume != null ? s.volume : ""}" placeholder="自動"></p>
      <button class="btn primary" id="doImport">加入書庫</button>`;
    $("#doImport").onclick = async () => {
      const series = (document.querySelector('input[name="ser"]:checked') || {}).value || null;
      try {
        const r = await api("import", {upload: u.upload, series, volume: $("#vol").value.trim() || null});
        location.hash = `#/b/${r.book_id}/overview`;
      } catch (e) { toast("加入失敗：" + e.message); }
    };
  } catch (e) { box.innerHTML = `<p class="issue">無法使用這個檔案：${esc(e.message)}</p>`; }
}

// ------------------------------------------------------------ settings
async function settingsView() {
  const k = await api("settings");
  const fal = k.provider === "fal", label = fal ? "fal" : "OpenRouter";
  view.innerHTML = `<h2>設定</h2><div class="card"><strong>連線方式</strong>
    <p><select id="provider"><option value="openrouter" ${fal ? "" : "selected"}>直連 OpenRouter</option>
    <option value="fal" ${fal ? "selected" : ""}>透過 fal 連 OpenRouter</option></select> <button class="btn" id="saveProvider">套用</button></p>
    <p class="small muted">新任務使用此設定；續譯沿用原任務的連線方式。</p>
    ${fal ? `<p class="small">費用由 fal 結算；估價參考 OpenRouter 價目，實際帳單以 fal 為準。此處不提供 fal 餘額查詢。</p>` : ""}</div>
    <div class="card"><strong>${label} 金鑰</strong>
    <p class="small">${k.set ? `已設定（結尾 ${esc(k.hint)}）` : `尚未設定。到 ${fal ? "fal.ai" : "openrouter.ai"} 建立金鑰，貼在下面。`}</p>
    <p><input type="password" id="key" size="48" placeholder="${fal ? "FAL_KEY" : "sk-or-…"}" autocomplete="off"> <button class="btn primary" id="saveKey">儲存</button></p>
    <p class="muted small">只存在這台電腦的 ${esc(k.file)}，不會顯示在畫面上，也不會寫進紀錄。</p>
    <div id="bal" class="small"></div></div>
    <div class="card"><strong>翻譯設定</strong><p class="small">模型與推理程度用預設（目前測試結果最好的組合），不需要選。預算上限會依每本書的估價自動設定。</p></div>`;
  $("#saveProvider").onclick = async () => {
    try { await api("settings/provider", {provider: $("#provider").value}); toast("已套用"); settingsView(); }
    catch (e) { toast(e.message); }
  };
  $("#saveKey").onclick = async () => {
    try { await api("settings/key", {key: $("#key").value}); toast("已儲存"); settingsView(); }
    catch (e) { toast(e.message); }
  };
  if (k.set) {
    $("#bal").textContent = "查詢餘額…";
    try { const b = await api("balance");
      $("#bal").innerHTML = [b.account_usd != null ? `帳戶餘額 US$${b.account_usd.toFixed(2)}` : "",
        b.key_limit_usd != null ? `這把金鑰還能用 US$${b.key_limit_usd.toFixed(2)}` : ""].filter(Boolean).join("　") || "查不到餘額";
    } catch (e) { $("#bal").textContent = "查不到餘額：" + e.message; }
  }
}

// ------------------------------------------------------------ overview
const RUNSTATUS = {completed: "完成", completed_with_failures: "完成（有段落失敗，可再試一次）", running: "進行中",
  interrupted: "已暫停", running_elsewhere: "另一個程序正在翻譯", budget_exhausted: "達到預算上限", awaiting_names: "等你決定譯法", stopped_fatal: "停止（連線或帳戶問題）", created: "準備中"};
const FINDING = {read_blocked: "內容過濾擋下，這段沒有讀到（說話者與記憶只靠程式）", reader_blocked: "內容過濾擋下，讀者檢查沒看到這段",
  review_blocked: "內容過濾擋下，審稿沒看到這段", content_blocked: "內容過濾擋下，保留日文原文", second_person_gender: "對女性用了「你」",
  narration_first_person: "第三人稱的敘述出現「我」", narrator_self_form: "敘述者自稱和全書不一致", offensive_term: "可能是貶稱",
  latin_word: "原文沒有的英文單字", possible_added_name: "可能補了原文沒有的人名", glossary_mismatch: "譯名和名詞表不一致",
  quote_mismatch: "引號和原文不對應", untranslated: "沒有翻譯", japanese_residue: "殘留日文", non_taiwan_glyph: "簡體或日文字形",
  placeholder_error: "排版標記不對", number_mismatch: "數字和原文不同", too_short: "譯文比原文短很多", too_long: "譯文比原文長很多",
  empty_translation: "譯文是空的", narrator_name_leak: "譯文寫出了敘述者的記號", cantonese: "粵語用字", ruby_notation_leak: "讀音標記留在譯文裡", name_form_substitution: "稱呼被換成另一種"};
function findingLabel(t) { return FINDING[t] || (t.startsWith("review:reader_") ? "讀者檢查：" + t.slice(14) : t.startsWith("review:") ? "審稿：" + t.slice(7) : t); }
let poll = null;
async function overviewView(id) {
  const o = await loadBook(id, "overview");
  const stat = (label, v, extra = "") => `<div class="card stat"><span class="muted small">${label}</span><b>${v}</b>${extra}</div>`;
  view.innerHTML = `<div class="card" id="runcard"><p class="muted">讀取翻譯狀態…</p></div><div class="row">
    ${stat("已翻譯", `${o.translated} / ${o.translatable}`)}${stat("已花費", "US$" + o.spent_usd.toFixed(2))}
    ${stat("有疑點的段落", o.flagged_units, `<a class="small" href="#/b/${id}/review?f=flagged">去看</a>`)}
    ${stat("可再確認的譯法", o.pending_names, `<a class="small" href="#/b/${id}/names">去看看</a>`)}
    ${stat("說話者未定", o.unknown_speakers)}${stat("譯註", o.notes)}${stat("人工修改", o.human_edits)}</div>
    <details class="card"><summary><strong>執行紀錄</strong></summary><div class="scroll"><table><tr><th>run</th><th>模式</th><th>模型</th><th>狀態</th><th>待處理</th><th>費用</th></tr>
    ${o.runs.map(r => `<tr><td class="mono small">${esc(r.id)}</td><td>${esc(r.mode)}</td><td>${esc(r.model)}</td>
      <td>${esc(RUNSTATUS[r.status] || r.status)}</td><td class="mono">${r.pending}</td><td class="mono">$${(r.cost_usd || 0).toFixed(3)}</td></tr>`).join("")}
    </table></div></details>`;
  await runCard(id, o);
}
async function runCard(id, o) {
  clearTimeout(poll);
  const card = $("#runcard"); if (!card) return;
  const p = await api(`book/${id}/run`), r = p.run;
  const money = v => "US$" + (+v || 0).toFixed(2);
  const bars = (p.stages || []).map(s => { const pc = Math.round(100 * s.done / s.total);
    return `<div class="small" style="margin-top:6px">${esc(s.label)}　${s.done} / ${s.total}${s.failed ? `　<span class="issue">失敗 ${s.failed}</span>` : ""}<div class="bar"><i style="width:${pc}%"></i></div></div>`; }).join("");
  const exportBtns = o.translated ? `<button class="btn" id="exp">匯出 EPUB</button> <span id="expres" class="small"></span>` : "";
  let body;
  if (p.busy) {
    body = `<strong>翻譯中</strong>　<span class="muted small">已花 ${money(r.spent_usd)}／上限 ${money(r.budget_usd)}</span>
      <p class="muted small">先讀完整本（說話者、角色、稱呼、劇情）再定名、翻譯；讀的階段還看不到譯文。可以關掉這個分頁，但不要關掉 NCTE 視窗。</p>
      ${bars}<pre class="log small">${esc((p.log || []).join("\n"))}</pre><button class="btn" id="pause">暫停</button>`;
  } else if (!r) {
    const k = await api("settings");
    body = `<strong>開始翻譯</strong><div id="est" class="small muted">估價中…</div>
      ${k.set ? "" : `<p class="issue">還沒設定目前連線方式的金鑰：<a href="#/settings">去設定</a></p>`}
      <button class="btn primary" id="start" disabled>開始翻譯</button>`;
  } else {
    const st = RUNSTATUS[r.status] || r.status;
    let action = "";
    if (r.status === "awaiting_names") action = `<p class="small">有些名字和稱呼的譯法要你決定：<a href="#/b/${id}/names">去定名</a>，決定完回來按繼續。</p><button class="btn primary" data-go>繼續翻譯</button>`;
    else if (r.status === "budget_exhausted") { const more = Math.round((r.spent_usd + (r.left_usd || 0) * 2) * 100) / 100;
      action = `<p class="small">已花 ${money(r.spent_usd)}，達到這次的上限 ${money(r.budget_usd)}。${r.left_usd ? `估計剩下的部分還要約 ${money(r.left_usd)}。` : ""}已完成的部分都保留著，繼續會從停下的地方接著做。</p>
        <button class="btn primary" data-go data-budget="${Math.max(more, r.budget_usd + 1)}">把上限提高到 ${money(Math.max(more, r.budget_usd + 1))} 並繼續</button>`; }
    else if (r.status === "running_elsewhere") action = `<p class="small">${esc(p.elsewhere.source)}（pid ${esc(p.elsewhere.pid)}）正在翻譯這本書；這裡不能同時繼續，等它結束或在那邊暫停後再回來。</p>`;
    else if (r.status !== "completed") action = `<button class="btn primary" data-go>繼續翻譯</button>`;
    body = `<strong>${esc(st)}</strong>　<span class="muted small">已花 ${money(r.spent_usd)}／上限 ${money(r.budget_usd)}</span>
      ${p.error ? `<p class="issue">${esc(p.error)}</p>` : ""}${r.status === "stopped_fatal" ? `<p class="small">如果是餘額不足，儲值後按繼續即可。</p>` : ""}
      ${bars}<div style="margin-top:10px">${action} ${exportBtns}</div>`;
  }
  if (p.elsewhere && (!r || r.status !== "running_elsewhere")) body += `<p class="muted small">${esc(p.elsewhere.source)}正在翻譯同系列的另一本，同一個系列一次只能翻一本。</p>`;
  else if (p.other_busy && !p.elsewhere) body += `<p class="muted small">另一本書正在翻譯，同時只能翻一本。</p>`;
  card.innerHTML = body;
  const on = (sel, fn) => { const b = card.querySelector(sel); if (b) b.onclick = fn; };
  on("#pause", async () => { await api("run/pause", {}); toast("會在目前這個請求完成後暫停"); });
  on("[data-go]", async e => {
    const b = e.target.dataset.budget;
    try { await api(`run/${r.id}/continue`, b ? {budget: +b} : {}); runCard(id, o); } catch (err) { toast(err.message); }
  });
  on("#start", async () => { try { await api(`book/${id}/start`, {}); runCard(id, o); } catch (err) { toast(err.message); } });
  on("#exp", async () => {
    const out = $("#expres"); out.textContent = "匯出並檢查中…";
    try { const x = await api(`book/${id}/export`, {});
      out.innerHTML = `${x.ok ? "完成" : "完成，但檢查有問題"}（EPUBCheck：${esc(x.epubcheck || "未安裝")}） <button class="btn" id="dl">下載</button>
        <span class="muted">${esc(x.path)}</span>${x.errors.map(e => `<div class="issue">${esc(e)}</div>`).join("")}`;
      $("#dl").onclick = () => download(id, x.name);
    } catch (err) { out.textContent = "匯出失敗：" + err.message; }
  });
  if ($("#est")) {
    try { const e = await api(`book/${id}/estimate`, {});
      $("#est").innerHTML = `${e.units} 段要翻譯。預估約 <b>${money(e.estimate_usd)}</b>${e.calibrated ? "（依你之前的實際花費校正）" : "（保守估計）"}；上限自動設為 ${money(e.budget_usd)}，到了會先問你。`;
      const k = await api("settings"); $("#start").disabled = !k.set || !e.units;
    } catch (err) { $("#est").textContent = "無法估價：" + err.message; }
  }
  const here = location.hash.startsWith(`#/b/${id}/overview`) || location.hash === `#/b/${id}`;
  // while running: every 3 s; otherwise every 15 s, so a run started, paused or continued elsewhere still shows up
  const watch = () => { poll = setTimeout(async () => {
    if (!location.hash.startsWith(`#/b/${id}/overview`)) return;
    const q = await api(`book/${id}/run`);
    const changed = q.busy !== p.busy || (q.run || {}).status !== (r || {}).status;
    if (changed) { invalidate(id); return overviewView(id); }
    if (p.busy) runCard(id, o); else watch();  // idle: only watch for a change, no re-estimate
  }, p.busy ? 3000 : 15000); };
  if (here) watch();
}
async function download(id, name) {
  const r = await fetch(`/api/book/${id}/download`, {headers: {"X-NCTE-Token": TOKEN}});
  if (!r.ok) { toast("下載失敗"); return; }
  const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
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
  return `<div class="pair ${cls}" data-id="${esc(u.id)}"><div class="mono small muted">${u.ordinal}<br><button class="btn small ctxbtn" data-ctx title="前後各 3 段">前後文</button></div>
    <div class="ja">${esc(u.ja)}</div>
    <div><div class="zh">${u.zh == null ? chip("c-bad", "未翻譯") : esc(u.zh)}</div>
      ${u.status === "human" ? chip("c-acc", "人工修改") + ` <button class="btn small" data-act="revert">改回機器譯文</button>` : ""}
      ${u.note ? `<div class="note">（註：${esc(u.note)}） <button class="btn small" data-act="drop-note">刪除譯註</button></div>` : ""}
      ${u.wordplay.map(w => `<div class="note">梗「${esc(w.term)}」：${esc(w.explanation)}</div>`).join("")}
      ${u.findings.map(f => `<div class="issue" title="${esc(f.type)}">⚑ ${esc(findingLabel(f.type))}：${esc(f.evidence)}</div>`).join("")}</div>
    <div>${u.quotes.map(q => { const [c, l] = STATUS[q.status] || ["c-unk", q.status];
      return `<div>${esc(q.speaker || "？")} ${chip(c, l)}${q.disputed ? chip("c-warn", "有爭議") : ""}${q.second ? `<div class="muted small" title="讀完後再確認一次的結果">再確認：${esc(q.second)}</div>` : ""}</div>`; }).join("") || `<span class="muted small">敘述</span>`}</div></div>`;
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
  el.querySelector("[data-ctx]").onclick = async e => {
    e.stopPropagation();
    const open = el.nextElementSibling;
    if (open && open.classList.contains("ctx")) { open.remove(); return; }
    try {
      const d = await api(`book/${id}/units?start=${u.ordinal - 3}&end=${u.ordinal + 3}`);
      const box = document.createElement("div"); box.className = "ctx";
      box.innerHTML = d.units.map(x => `<div class="ctxrow ${x.id === u.id ? "here" : ""}"><span class="mono muted">${x.ordinal}</span>
        <span class="ja">${esc(x.ja)}</span><span>${x.zh == null ? "" : esc(x.zh)}</span>
        <span class="muted small">${x.quotes.map(q => esc(q.speaker || "？")).join("、") || "敘述"}</span></div>`).join("");
      el.after(box);
    } catch (err) { toast(err.message); }
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
      ${(r.related || []).length ? `<div class="small">同一暱稱已定為：${r.related.map(x => `<span class="ja">${esc(x.ja)}</span> → ${esc(x.zh)}`).join("、")}</div>` : ""}
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
      ${(p.first_person || []).length ? `<div class="small" style="margin-top:4px">自稱：${p.first_person.map(([w, n]) => `<span class="form-tag"><span class="ja">${esc(w)}</span><span class="n">×${n}</span></span>`).join(" ")}</div>` : ""}
      ${genderLine(p, upto)}
      ${groupLine(p, d, nm)}
      ${p.impression ? `<div class="card" style="margin-top:10px;background:var(--accent-soft)"><strong>角色印象</strong> <span class="muted small">讀完全書後整理</span>
        <div class="small" style="margin-top:4px">個性：${esc(p.impression.personality)}</div>
        <div class="small">口吻：${esc(p.impression.voice)}</div>
        <div class="small">定位：${esc(p.impression.role)}</div></div>` : ""}
      ${relationList(d, pid, upto, nm)}
      <div class="row" style="margin-top:12px;gap:24px">
        <div style="flex:1 1 320px"><div class="addr-h"><strong>別人怎麼叫他</strong><span class="muted small">左邊是說話的人</span></div>${byPerson(called, a => a.speaker, k => esc(nm(k))) || '<div class="muted">—</div>'}</div>
        <div style="flex:1 1 320px"><div class="addr-h"><strong>他怎麼叫別人</strong><span class="muted small">左邊是被叫的人</span></div>${byPerson(calls, a => a.to, k => k === pid ? "（提到自己）" : esc(nm(k))) || '<div class="muted">—</div>'}</div></div>
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
  const gs = $("#genderSet"); if (gs) gs.onchange = async () => {
    try { await api(`book/${id}/characters/${peopleState.person}/gender`, {gender: gs.value}); invalidate(id); toast("性別已更新（之後的翻譯會用）"); peopleView(id); }
    catch (e) { toast(e.message); } };
}
// relations of one person as known at the slider position (begun, not yet replaced), each with its source quote
function groupLine(p, d, nm) {
  // a group card and who it is made of; a member shows the groups it belongs to (people not shown yet are left out)
  const link = ids => (ids || []).filter(k => d.characters[k]).map(k => `<a href="#" data-k="${esc(k)}">${esc(nm(k))}</a>`).join("・");
  const members = link(p.members), of = link(p.member_of);
  return (members ? `<div class="small" style="margin-top:4px">成員：${members}</div>` : "")
    + (of ? `<div class="small" style="margin-top:4px">屬於：${of}</div>` : "");
}

function relationList(d, pid, upto, nm) {
  const rs = (d.relations || []).filter(r => (r.a === pid || r.b === pid) && r.at <= upto && (r.until == null || r.until > upto));
  if (!rs.length) return "";
  const rows = rs.sort((x, y) => x.at - y.at).map(r => {
    const other = r.a === pid ? r.b : r.a, mine = r.a === pid ? r.a_is : r.b_is, theirs = r.a === pid ? r.b_is : r.a_is;
    const ev = (r.evidence || []).filter(e => e.at <= upto)[0];
    return `<tr><td><a href="#" data-k="${esc(other)}">${esc(nm(other))}</a></td><td>${esc(theirs || r.rel)}</td>
      <td>${chip(r.kind === "feeling" ? "c-warn" : "c-acc", r.kind === "feeling" ? "情感" : "身分")}${r.certainty === "inferred" ? chip("c-warn", "推測") : ""}${r.origin !== "llm" ? chip("c-ok", "人工") : ""}</td>
      <td class="mono small">§${r.at}</td><td class="ja small">${ev ? esc(ev.quote) : ""}</td></tr>`;
  }).join("");
  return `<div style="margin-top:12px"><strong>人物關係</strong> <span class="muted small">對方是他的什麼</span>
    <div class="scroll"><table><tr><th>對方</th><th>關係</th><th>類型</th><th>從</th><th>原文依據</th></tr>${rows}</table></div></div>`;
}
// one row per person: who on the left, each form they use as a small tag (most used first)
function byPerson(list, key, label) {
  const groups = new Map();
  list.forEach(a => { const k = key(a); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(a); });
  const total = fs => fs.reduce((n, a) => n + a.uses, 0);
  return [...groups.entries()].sort((x, y) => total(y[1]) - total(x[1])).map(([k, forms]) =>
    `<div class="addr-row"><div class="addr-who">${label(k)}</div><div class="addr-forms">${forms.sort((x, y) => y.uses - x.uses).map(a =>
      `<span class="form-tag"><span class="ja">${esc(a.ja)}</span><span class="zh">${a.zh ? esc(a.zh)
        : '<span class="muted" title="只出現一次的稱呼不固定譯法，翻譯時依名字的譯名與當下語境決定">依上下文</span>'}</span><span class="n">×${a.uses}</span>${a.usage === "mention" ? '<span class="n" title="談到不在對話中的人，不是當面叫">提到</span>' : a.usage === "quoted" ? '<span class="n" title="轉述別人怎麼叫，不是他自己這樣叫">轉述</span>' : ""}</span>`).join("")}</div></div>`).join("");
}
function genderLine(p, upto) {
  // gender as known at the slider position: explicit (a word in the text), inferred (tone), or set by you
  const g = p.gender && p.gender.at <= upto ? p.gender : null;
  const label = {female: "女", male: "男"}, sure = {explicit: ["c-ok", "原文明寫"], inferred: ["c-warn", "推測"], human: ["c-acc", "你設定的"]};
  const cur = g ? g.g : "unknown";
  return `<div class="small" style="margin-top:4px">性別：${g ? `${label[g.g]} ${chip(...sure[g.sure])} ${g.quote ? `<span class="ja muted">「${esc(g.quote)}」</span>` : ""}`
    : '<span class="muted">不明（翻譯時用「你」）</span>'}
    <select id="genderSet" title="改成你確定的性別">${[["unknown", "不明"], ["female", "女"], ["male", "男"]].map(([v, t]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${t}</option>`).join("")}</select></div>`;
}
const G = {mode: "rel", mentions: false, n: 12, hideMinor: true, series: false, focus: null, pair: null, center: null};
const GROUPISH = /(たち|みんな|観客|生徒たち|周り|の声|チーム|們|觀眾|路人|大家)$/;
// Each group of linked people is laid out on its own (repel, attract along links, keep circles and labels apart),
// then the groups are placed side by side, biggest first. The SVG view box is fitted around the result, so circles,
// lines and labels scale together and the spacing is never squeezed.
function layoutGroup(nodes, edges, R) {
  const P = {}, n = Math.max(1, nodes.length), ideal = 200;
  nodes.forEach((c, i) => { const t = -Math.PI / 2 + i * 2 * Math.PI / n; P[c] = {x: Math.cos(t) * 40 * Math.sqrt(n), y: Math.sin(t) * 40 * Math.sqrt(n)}; });
  for (let it = 0; it < 400; it++) {
    const F = {}; nodes.forEach(c => F[c] = {x: 0, y: 0});
    nodes.forEach((a, i) => nodes.slice(i + 1).forEach(b => {
      let dx = P[a].x - P[b].x, dy = P[a].y - P[b].y; const d = Math.max(1, Math.hypot(dx, dy)), f = ideal * ideal / d;
      dx /= d; dy /= d; F[a].x += dx * f; F[a].y += dy * f; F[b].x -= dx * f; F[b].y -= dy * f; }));
    edges.forEach(e => { let dx = P[e.a].x - P[e.b].x, dy = P[e.a].y - P[e.b].y; const d = Math.max(1, Math.hypot(dx, dy)), f = d * d / ideal * 0.7;
      dx /= d; dy /= d; F[e.a].x -= dx * f; F[e.a].y -= dy * f; F[e.b].x += dx * f; F[e.b].y += dy * f; });
    const temp = 30 * (1 - it / 400) + 0.5;
    nodes.forEach(c => { F[c].x -= P[c].x * 0.05; F[c].y -= P[c].y * 0.05;
      const d = Math.max(1, Math.hypot(F[c].x, F[c].y)); P[c].x += F[c].x / d * Math.min(d, temp); P[c].y += F[c].y / d * Math.min(d, temp); });
    nodes.forEach((a, i) => nodes.slice(i + 1).forEach(b => {
      const dx = P[a].x - P[b].x, dy = P[a].y - P[b].y, d = Math.max(0.1, Math.hypot(dx, dy)), min = R(a) + R(b) + 80;
      if (d < min) { const push = (min - d) / 2; P[a].x += dx / d * push; P[a].y += dy / d * push; P[b].x -= dx / d * push; P[b].y -= dy / d * push; } }));
  }
  return P;
}
function layout(nodes, edges, W, H, R) {
  const adj = {}; nodes.forEach(c => adj[c] = []);
  edges.forEach(e => { if (adj[e.a] && adj[e.b]) { adj[e.a].push(e.b); adj[e.b].push(e.a); } });
  const seen = new Set(), groups = [];
  nodes.forEach(c => { if (seen.has(c)) return; const g = [], st = [c]; seen.add(c);
    while (st.length) { const x = st.pop(); g.push(x); adj[x].forEach(y => { if (!seen.has(y)) { seen.add(y); st.push(y); } }); }
    groups.push(g); });
  groups.sort((a, b) => b.length - a.length);
  const P = {}, gap = 90;
  let x0 = 0, top = Infinity, bottom = -Infinity;
  groups.forEach(g => {
    const Q = layoutGroup(g, edges.filter(e => g.includes(e.a) && g.includes(e.b)), R);
    const xs = g.map(c => Q[c].x - R(c)), xe = g.map(c => Q[c].x + R(c));
    const minX = Math.min(...xs), maxX = Math.max(...xe);
    g.forEach(c => { P[c] = {x: Q[c].x - minX + x0, y: Q[c].y}; top = Math.min(top, Q[c].y - R(c)); bottom = Math.max(bottom, Q[c].y + R(c)); });
    x0 += maxX - minX + gap;
  });
  const m = 50, w = Math.max(400, x0 - gap + 2 * m), h = Math.max(300, bottom - top + 2 * m);
  P._box = nodes.length ? {x: -m, y: top - m, w, h} : {x: 0, y: 0, w: W, h: H};
  return P;
}
// the model says what each side is: the same on both sides (朋友／朋友) is one label in the middle, different roles
// (父親／女兒, 未婚夫／未婚妻) are written next to each person — no word list decides it
function directed(r) { return !!(r.a_is && r.b_is && r.a_is !== r.b_is); }
// relations mode: the person with the most relations in the middle, the others in rings by how many links away;
// the middle only changes when someone else has clearly more (two), or when you click someone
function radial(nodes, edges, center, R) {
  const adj = {}; nodes.forEach(c => adj[c] = []);
  edges.forEach(e => { if (adj[e.a] && adj[e.b]) { adj[e.a].push(e.b); adj[e.b].push(e.a); } });
  const ring = {[center]: 0}, order = [center], parent = {};
  for (let i = 0; i < order.length; i++) adj[order[i]].forEach(y => { if (!(y in ring)) { ring[y] = ring[order[i]] + 1; parent[y] = order[i]; order.push(y); } });
  const P = {[center]: {x: 0, y: 0}}, angle = {[center]: 0}, maxR = Math.max(...nodes.map(R));
  const rings = Math.max(0, ...Object.values(ring));
  for (let k = 1; k <= rings; k++) {
    const here = order.filter(c => ring[c] === k);
    const radius = Math.max(k * 230, here.length * (2 * maxR + 60) / (2 * Math.PI));
    const gapA = (2 * maxR + 50) / radius;  // the angle two circles on this ring need between them
    if (k === 1) here.forEach((c, i) => { angle[c] = -Math.PI / 2 + (i + 0.5) * 2 * Math.PI / here.length; });
    else {  // next to the person they are linked to, siblings side by side, then pushed apart where they crowd
      const kids = {}; here.forEach(c => (kids[parent[c]] = kids[parent[c]] || []).push(c));
      Object.entries(kids).forEach(([pa, cs]) => cs.forEach((c, j) => { angle[c] = angle[pa] + (j - (cs.length - 1) / 2) * gapA; }));
      for (let it = 0; it < 60; it++) {
        here.sort((a, b) => angle[a] - angle[b]);
        here.forEach((c, i) => { const nx = here[(i + 1) % here.length]; let d = angle[nx] - angle[c];
          if (i === here.length - 1) d += 2 * Math.PI;
          if (here.length > 1 && d < gapA) { angle[c] -= (gapA - d) / 2; angle[nx] += (gapA - d) / 2; } });
      }
    }
    here.forEach(c => { P[c] = {x: Math.cos(angle[c]) * radius, y: Math.sin(angle[c]) * radius}; });
  }
  const rest = nodes.filter(c => !(c in ring));  // people not linked to the middle: their own groups, to the side
  if (rest.length) {
    const Q = layout(rest, edges.filter(e => rest.includes(e.a) && rest.includes(e.b)), 0, 0, R);
    const right = Math.max(...Object.values(P).map(p => p.x)) + maxR + 120;
    rest.forEach(c => { P[c] = {x: Q[c].x - Q._box.x + right, y: Q[c].y - (Q._box.y + Q._box.h / 2)}; });
  }
  const xs = nodes.map(c => P[c].x), ys = nodes.map(c => P[c].y), m = maxR + 60;
  P._box = {x: Math.min(...xs) - m, y: Math.min(...ys) - m, w: Math.max(...xs) - Math.min(...xs) + 2 * m,
            h: Math.max(...ys) - Math.min(...ys) + 2 * m};
  return P;
}
async function graphView(id) {
  view.classList.add("wide");  // the graph uses the whole window width
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
  // relations known by this point (ADR-039): from the facts, shown from where the story reveals them
  // relations known at this point: begun and not yet replaced (宿敵 until they become 未婚夫妻)
  const rels = (d.relations || []).filter(r => r.at <= upto && (r.until == null || r.until > upto) && ok(r.a) && ok(r.b));
  if (G.mode === "rel") {  // the people the relations are about (most present first), nobody without one
    const inRel = new Set(rels.flatMap(r => [r.a, r.b]));
    inRel.forEach(c => { cnt[c] = cnt[c] || 1; });
    nodes = [...inRel].sort((a, b) => cnt[b] - cnt[a]);
    // people with a relation are few: show them all up to 20 (12 cut 馬可, one of two followers)
    if (G.n && nodes.length > Math.max(G.n, 20)) { const keep = new Set(nodes.slice(0, G.n)); nodes = nodes.filter(c => keep.has(c)); }
  }
  const inSet = new Set(nodes);
  const pairScenes = {};
  scenes.forEach(s => { const m = members(s).filter(c => inSet.has(c)); m.forEach((a, i) => m.slice(i + 1).forEach(b => {
    const k = [a, b].sort().join("|"); (pairScenes[k] = pairScenes[k] || []).push(s); })); });
  const addr = d.address.filter(a => inSet.has(a.speaker) && inSet.has(a.to) && a.speaker !== a.to && (a.at == null || a.at <= upto));
  const edges = G.mode === "addr"
    ? Object.values(addr.reduce((m, a) => { const k = [a.speaker, a.to].sort().join("|"); (m[k] = m[k] || {a: a.speaker, b: a.to, w: 0}).w += a.uses; return m; }, {}))
    : G.mode === "rel"
    ? Object.values(rels.filter(r => inSet.has(r.a) && inSet.has(r.b)).reduce((m, r) => { const k = [r.a, r.b].sort().join("|");
        const e = (m[k] = m[k] || {a: r.a, b: r.b, w: 1 + (pairScenes[k] || []).length, labels: [], roles: []});
        e.labels.push(r.rel); if (directed(r)) e.roles.push(r); return m; }, {}))
    : Object.entries(pairScenes).map(([k, ss]) => { const [a, b] = k.split("|"); return {a, b, w: ss.length}; });
  const maxN = Math.max(1, ...nodes.map(c => cnt[c]));
  const rad = c => 26 + 18 * Math.sqrt((cnt[c] || 1) / maxN);
  const linkEdges = edges.filter(e => nodes.includes(e.a) && nodes.includes(e.b));
  if (G.mode === "rel" && nodes.length) {
    const deg = {}; linkEdges.forEach(e => { deg[e.a] = (deg[e.a] || 0) + 1; deg[e.b] = (deg[e.b] || 0) + 1; });
    const best = nodes.slice().sort((a, b) => (deg[b] || 0) - (deg[a] || 0) || cnt[b] - cnt[a])[0];
    if (!G.center || !nodes.includes(G.center) || (deg[best] || 0) >= (deg[G.center] || 0) + 2) G.center = best;
  }
  const middle = G.mode === "rel" && G.focus && nodes.includes(G.focus) ? G.focus : G.center;
  const W = 900, H = 620, P = G.mode === "rel" && middle ? radial(nodes, linkEdges, middle, rad) : layout(nodes, linkEdges, W, H, rad),
    mx = Math.max(1, ...edges.map(e => e.w));
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
    // a line that would pass through another person bends around them: the smallest bend (either side) that clears
    // every other circle, as a quadratic curve; labels sit on the curve
    const bend = e => {
      const p = P[e.a], q = P[e.b], dx = q.x - p.x, dy = q.y - p.y, L = Math.max(1, Math.hypot(dx, dy)), nx = -dy / L, ny = dx / L;
      const others = nodes.filter(c => c !== e.a && c !== e.b);
      const clear = h => { const cx = (p.x + q.x) / 2 + nx * h, cy = (p.y + q.y) / 2 + ny * h;
        for (let i = 1; i < 24; i++) { const t = i / 24, u = 1 - t, x = u * u * p.x + 2 * t * u * cx + t * t * q.x, y = u * u * p.y + 2 * t * u * cy + t * t * q.y;
          if (others.some(c => Math.hypot(P[c].x - x, P[c].y - y) < rad(c) + 20)) return false; }
        return true; };
      for (let h = 0; h <= 360; h += 15) { if (clear(h)) return h; if (h && clear(-h)) return -h; }
      return 0;
    };
    const at = (e, h, t) => { const p = P[e.a], q = P[e.b], L = Math.max(1, Math.hypot(q.x - p.x, q.y - p.y)),
      cx = (p.x + q.x) / 2 - (q.y - p.y) / L * h, cy = (p.y + q.y) / 2 + (q.x - p.x) / L * h, u = 1 - t;
      return {x: u * u * p.x + 2 * t * u * cx + t * t * q.x, y: u * u * p.y + 2 * t * u * cy + t * t * q.y, cx, cy}; };
    // a relation with two different roles (父親／女兒, 老師／學生): one line, each role written next to its person
    const roleLabels = (e, h) => (e.roles || []).map(r => [[r.a, r.b, r.a_is], [r.b, r.a, r.b_is]].map(([who, other, role]) => {
      const pt = at(e, h, who === e.a ? 0.3 : 0.7);
      return `<text x="${pt.x}" y="${pt.y - 5}" font-size="14" text-anchor="middle" class="rel-label">${esc(role)}<title>${esc(nm(who))} 是 ${esc(nm(other))} 的${esc(role)}</title></text>`;
    }).join("")).join("");
    edges.forEach(e => {
      const op = Math.min(dim(e.a), dim(e.b)), pk = [e.a, e.b].sort().join("|"), h = bend(e), mid = at(e, h, 0.5);
      const d = `M${P[e.a].x},${P[e.a].y} Q${mid.cx},${mid.cy} ${P[e.b].x},${P[e.b].y}`;
      svg += `<g data-pair="${esc(pk)}" style="cursor:pointer;opacity:${op}"><path d="${d}" fill="none" stroke="transparent" stroke-width="12"/>
        <path d="${d}" fill="none" stroke="${G.pair === pk ? "var(--accent)" : "var(--muted)"}" stroke-opacity="${.25 + .6 * e.w / mx}" stroke-width="${1 + 7 * e.w / mx}"><title>${esc(nm(e.a))}・${esc(nm(e.b))}：${e.labels ? esc(e.labels.join("、")) : e.w + " 個場景"}</title></path>
        ${e.labels ? `<text x="${mid.x}" y="${mid.y - 4}" font-size="14" text-anchor="middle" class="rel-label">${esc(e.labels.filter(l => !(e.roles || []).some(r => r.rel === l)).join("・"))}</text>` : ""}
        ${G.mode === "rel" ? roleLabels(e, h) : ""}</g>`; });
  }
  nodes.forEach(c => { const r = rad(c);
    svg += `<g data-k="${esc(c)}" style="cursor:pointer;opacity:${dim(c)}"><circle cx="${P[c].x}" cy="${P[c].y}" r="${r}" fill="${G.focus === c ? "var(--accent-soft)" : "var(--panel)"}" stroke="var(--accent)" stroke-width="${G.focus === c ? 3 : 1.6}"/>
      <text x="${P[c].x}" y="${P[c].y + 5}" text-anchor="middle" font-size="15" font-weight="600">${esc(nm(c))}</text><title>${esc(nm(c))}：${cnt[c]} 個場景</title></g>`; });
  const sel = (name, opts, cur) => `<select data-ctl="${name}">${opts.map(([v, l]) => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  view.innerHTML = `<div class="row" style="align-items:center;margin-bottom:10px;gap:10px">
      <div class="seg"><button data-m="rel" class="${G.mode === "rel" ? "on" : ""}">關係</button><button data-m="talk" class="${G.mode === "talk" ? "on" : ""}">一起出場</button><button data-m="addr" class="${G.mode === "addr" ? "on" : ""}">誰怎麼叫誰</button></div>
      ${G.mode === "talk" ? `<label class="small"><input type="checkbox" data-ctl="mentions" ${G.mentions ? "checked" : ""}> 只被提到也算</label>` : ""}
      ${sel("n", [[12, "12 人"], [20, "20 人"], [0, "全部"]], G.n)}
      <label class="small"><input type="checkbox" data-ctl="hideMinor" ${G.hideMinor ? "checked" : ""}> 隱藏路人、群體</label>
      <div class="seg"><button data-s="0" class="${G.series ? "" : "on"}">本集</button><button data-s="1" class="${G.series ? "on" : ""}">系列累計</button></div>
      ${G.focus ? `<button class="btn" data-clear>取消聚焦</button>` : ""}</div>
    <div class="card scroll" style="padding:6px"><svg viewBox="${P._box.x} ${P._box.y} ${P._box.w} ${P._box.h}" width="100%" style="min-width:640px;min-height:560px;max-height:82vh">${svg}</svg></div>
    <div class="row" style="align-items:flex-start"><div class="card" style="flex:1 1 340px" id="gside">${graphSide(d, nm, upto, pairScenes, addr, cnt)}</div></div>
    <p class="muted small">${G.mode === "rel" ? `線＝故事中的關係（依讀到的事實整理，讀到揭露處才出現）${rels.length ? "" : "；這本書還沒有整理關係（ncte names relations）"}` : G.mode === "talk" ? (G.mentions ? "線＝同一場景出現（含只被提到）" : "線＝兩人在同一場景都有台詞") : "箭頭＝誰用什麼稱呼叫誰"}；圓圈大小＝出場場景數。點人物聚焦，點線看兩人的互動。${ov.title}</p>`;
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
