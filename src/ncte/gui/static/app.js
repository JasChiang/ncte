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
              ["read", "閱讀譯文"], ["story", "故事進度"], ["people", "角色檔案"], ["graph", "人物關係圖"], ["glossary", "名詞與稱呼"]];
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
    tabs.innerHTML = TABS.map((t, i) => t.length === 1 ? (i ? `<span class="sep"></span>` : "")
      : `<a href="#/b/${id}/${t[0]}" class="${t[0] === tab ? "on" : ""}">${t[1]}</a>`).join("");
    $("#posbar").hidden = !READING.has(tab);
    const ov = await loadBook(id, "overview");
    $("#crumb").textContent = ov.title;
    await ({overview: overviewView, read: readView, review: reviewView, names: namesView, story: storyView, people: peopleView,
            graph: graphView, glossary: glossaryView}[tab] || overviewView)(id);
  } catch (e) { view.innerHTML = `<div class="card">讀取失敗：${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------ library
async function libraryView() {
  const [lib, k] = await Promise.all([api("library"), api("settings")]);
  const hasBook = lib.some(s => s.volumes.length);
  // first steps, shown until there is a key and a book: what to do, in order
  const step = (n, done, title, body) => `<div class="step ${done ? "done" : ""}"><span class="num">${done ? "✓" : n}</span><div><strong>${title}</strong><div class="small muted">${body}</div></div></div>`;
  const guide = k.set && hasBook ? "" : `<div class="card steps"><strong>開始使用</strong>
      ${k.provider === "codex"
        ? step(1, k.set, "找到 Codex", k.set ? "已找到：用你的 ChatGPT 訂閱額度翻譯，NCTE 不另外收費。" : `NCTE 用這台電腦上已登入的 Codex CLI 翻譯。請先安裝 Codex（<code>codex</code> 指令），並在終端機執行 <code>codex login</code> 用 ChatGPT 帳號登入；或到<a href="#/settings">設定</a>改用其他連線方式。`)
        : k.provider === "claude"
        ? step(1, k.set, "找到 Claude Code", k.set ? "已找到：用你的 Claude 訂閱額度翻譯，NCTE 不另外收費。" : `NCTE 用這台電腦上已登入的 Claude Code 翻譯。請先安裝 <a href="https://docs.claude.com/en/docs/claude-code" target="_blank" rel="noopener">Claude Code</a>，並在終端機執行 <code>claude</code> 登入；或到<a href="#/settings">設定</a>改用 OpenRouter。`)
        : step(1, k.set, "設定 OpenRouter 金鑰", k.set ? "已設定。" : `NCTE 透過 OpenRouter 使用翻譯模型，費用從你的 OpenRouter 帳戶扣。<a href="#/settings">去設定</a>（一本輕小說約 US$8–11，依長度而定）。`)}
      ${step(2, hasBook, "加入一本日文 EPUB", "把沒有 DRM 的日文 EPUB 拖到下面的框裡。檔案只存在這台電腦。")}
      ${step(3, false, "看估價，開始翻譯", "打開書以後會先估價（費用、時間），按「開始翻譯」就會自動讀完整本再翻，中途可以暫停。")}</div>`;
  const status = v => v.translatable == null ? chip("c-unk", "找不到資料夾") : v.translatable && v.translated >= v.translatable ? chip("c-ok", "翻譯完成")
    : v.translated ? chip("c-acc", "翻譯到一半") : v.read ? chip("c-acc", "讀完一部分") : chip("c-unk", "還沒開始");
  view.innerHTML = `<h2>書庫</h2>${guide}
    <div class="card drop" id="drop"><strong>加入一本書</strong>
      <p class="muted">把日文 EPUB（無 DRM）拖到這裡，或 <label class="link">選擇檔案<input type="file" id="file" accept=".epub" hidden></label>。檔案只存在這台電腦。</p>
      <div id="upl"></div></div>` + (lib.map(s => `<div class="card book">${s.volumes.length === 1 && s.volumes[0].title.startsWith(s.title) ? "" : `<div class="book-h"><strong>${esc(s.title)}</strong>
    ${s.volumes.length > 1 ? `<span class="muted small">共 ${s.volumes.length} 集</span>` : ""}</div>`}
    ${s.volumes.map(v => { const p = v.translatable ? Math.round(100 * v.translated / v.translatable) : 0;
      return `<a class="vol" href="#/b/${esc(v.book_id)}/overview"><span class="vol-t ${s.volumes.length === 1 ? "solo" : ""}">${s.volumes.length > 1 ? `第 ${v.volume_index} 集　` : ""}${esc(v.title)}</span>
      ${status(v)}<span class="muted small">${v.translatable == null ? "" : `${p}%（${v.translated} / ${v.translatable} 段）`}</span>
      <div class="bar"><i style="width:${p}%"></i></div></a>`; }).join("")}</div>`).join(""));
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
  const fal = k.provider === "fal", claude = k.provider === "claude", local = k.provider === "local", codex = k.provider === "codex", plan = claude || local || codex, label = fal ? "fal" : "OpenRouter";
  const localCard = `<div class="card"><strong>本機模型</strong>
    <p>用這台電腦上跑的模型伺服器（LM Studio、llama-server、mlx_lm.server 的 OpenAI 相容 API）翻譯：原文不會離開這台電腦，不需要金鑰，不收費；但速度和品質看你的電腦與模型，目前仍是實驗功能。</p>
    <p>${k.set ? `${chip("c-ok", "伺服器有回應")} <span class="small muted">${esc(k.url)}</span>` : `${chip("c-warn", "連不到模型伺服器")} <span class="small muted">${esc(k.url)}${k.error ? "：" + esc(k.error) : ""}</span>`}</p>
    ${k.set ? `<p class="small">可用模型：${(k.models || []).map(m => `<code>${esc(m.id)}</code>${m.state === "loaded" ? "（已載入）" : ""}`).join("、") || "無"}。預設用已載入的那個（<code>local/default</code>）。</p>` : `<p class="small muted">LM Studio：下載一個模型後在終端機執行 <code>lms server start</code> 並載入模型；伺服器位址可在設定檔的 <code>local_base_url</code> 改。</p>`}</div>`;
  const claudeCard = `<div class="card"><strong>Claude 方案</strong>
    <p>用這台電腦上已登入的 Claude Code（<code>claude</code> 指令）翻譯，使用你的 Claude 訂閱額度：不需要金鑰，NCTE 不另外收費。讀書的步驟（切場景、讀場景、記憶整理、角色印象、體例）用 Sonnet，翻譯、術語定義、定名、覆核、人物關係、規則確認、審稿、對照校稿、讀者檢查與修正驗收用 Opus。</p>
    <p>${k.set ? `${chip("c-ok", "找到 Claude Code")} <span class="small muted">${esc(k.claude)}</span>` : `${chip("c-warn", "找不到 Claude Code")} 請先安裝 <a href="https://docs.claude.com/en/docs/claude-code" target="_blank" rel="noopener">Claude Code</a>，並在終端機執行 <code>claude</code> 登入；或在下方「連線方式」改用 OpenRouter。`}</p>
    <p class="small muted">額度用完時會自動等到重置再接著翻，不會中斷；一本書通常要好幾個小時。畫面上的金額是照定價換算的參考，不會向你收費。對照校稿預設做兩輪（第一輪修完後再校整本一次，多抓一批錯；多用一些額度），OpenRouter 則預設一輪。</p></div>`;
  const codexCard = `<div class="card"><strong>Codex（ChatGPT 方案）</strong>
    <p>用這台電腦上已登入的 Codex CLI（<code>codex</code> 指令）翻譯，使用你的 ChatGPT 訂閱額度：不需要金鑰，NCTE 不另外收費。預設所有步驟都用 <code>codex/gpt-6.1-sol</code>。</p>
    <p>${k.set ? `${chip("c-ok", "找到 Codex")} <span class="small muted">${esc(k.codex)}</span>` : `${chip("c-warn", "找不到 Codex")} 請先安裝 Codex CLI，並在終端機執行 <code>codex login</code> 用 ChatGPT 帳號登入；或在下方「連線方式」改用 Claude 方案或 OpenRouter。`}</p>
    <p class="small muted">Codex 每個請求都會帶上自己的固定說明，固定負擔比 Claude 方案大（每次約一萬 token），也比較快用完額度。額度用完或連線失敗時會自動等待再接著翻；沒登入會立刻停下來告訴你。Codex 不回報定價，所以畫面上不顯示金額。還沒有整本書實測過，目前是實驗功能。</p></div>`;
  view.innerHTML = `<h2>設定</h2>
    ${claude ? claudeCard : local ? localCard : codex ? codexCard : `<div class="card"><strong>${label} 金鑰</strong>
    ${fal ? "" : `<p>NCTE 透過 <a href="https://openrouter.ai" target="_blank" rel="noopener">OpenRouter</a> 使用翻譯模型（Gemini），費用從你的 OpenRouter 帳戶扣，NCTE 不另外收費。</p>
      <ol class="small"><li>到 <a href="https://openrouter.ai/keys" target="_blank" rel="noopener">openrouter.ai/keys</a> 登入並建立一把金鑰（以 sk-or- 開頭）。</li>
      <li>到 <a href="https://openrouter.ai/settings/credits" target="_blank" rel="noopener">Credits</a> 儲值；一本輕小說約 US$8–11（依長度而定）。</li>
      <li>把金鑰貼在下面，按儲存。</li></ol>`}
    <p>${k.set ? `${chip("c-ok", "已設定")} 結尾 ${esc(k.hint)}` : chip("c-warn", "尚未設定")}</p>
    <p><input type="password" id="key" size="48" placeholder="${fal ? "FAL_KEY" : "sk-or-…"}" autocomplete="off"> <button class="btn primary" id="saveKey">儲存</button></p>
    <p class="muted small">只存在這台電腦的 ${esc(k.file)}，不會顯示在畫面上，也不會寫進紀錄。</p>
    <div id="bal"></div></div>`}
    <div class="card"><strong>翻譯設定</strong><p>模型與推理程度用預設（目前測試結果最好的組合），不需要選。${plan ? "" : "預算上限會依每本書估價的「最多」金額自動設定，到了會先停下來問你。"}</p></div>
    <details class="card" ${plan ? "" : "open"}><summary><strong>連線方式</strong> <span class="muted small">有 Claude 訂閱用 Claude 方案；沒有的話用 OpenRouter 付費</span></summary>
    <p><select id="provider"><option value="claude" ${claude ? "selected" : ""}>Claude 方案（用 Claude 訂閱額度，不需金鑰；建議）</option>
    <option value="codex" ${codex ? "selected" : ""}>Codex（ChatGPT 方案，用 ChatGPT 訂閱額度，不需金鑰；實驗）</option>
    <option value="openrouter" ${!fal && !plan ? "selected" : ""}>OpenRouter（Gemini，需要金鑰並儲值）</option>
    <option value="fal" ${fal ? "selected" : ""}>透過 fal 連 OpenRouter</option>
    <option value="local" ${local ? "selected" : ""}>本機模型（LM Studio 等，不收費；實驗）</option></select> <button class="btn" id="saveProvider">套用</button></p>
    <p class="small muted">新任務使用此設定；續譯沿用原任務的連線方式。</p>
    ${fal ? `<p class="small">費用由 fal 結算；估價參考 OpenRouter 價目，實際帳單以 fal 為準。此處不提供 fal 餘額查詢。</p>` : ""}</details>`;
  $("#saveProvider").onclick = async () => {
    try { await api("settings/provider", {provider: $("#provider").value}); toast("已套用"); settingsView(); }
    catch (e) { toast(e.message); }
  };
  if (!plan) $("#saveKey").onclick = async () => {
    try { await api("settings/key", {key: $("#key").value}); toast("已儲存"); settingsView(); }
    catch (e) { toast(e.message); }
  };
  if (k.set && !fal && !plan) {
    $("#bal").textContent = "查詢餘額…";
    try { const b = await api("balance");
      $("#bal").innerHTML = [b.account_usd != null ? `帳戶餘額 <b>US$${b.account_usd.toFixed(2)}</b>` : "",
        b.key_limit_usd != null ? `這把金鑰還能用 US$${b.key_limit_usd.toFixed(2)}` : ""].filter(Boolean).join("　") || "查不到餘額";
    } catch (e) { $("#bal").textContent = "查不到餘額：" + e.message; }
  }
}

// ------------------------------------------------------------ overview
const RUNSTATUS = {completed: "完成", completed_with_failures: "完成（有段落失敗，可再試一次）", running: "進行中",
  interrupted: "已暫停", running_elsewhere: "另一個程序正在翻譯", budget_exhausted: "達到預算上限", awaiting_names: "等你決定譯法", stopped_fatal: "停止（連線或帳戶問題）", created: "準備中"};
const FINDING = {read_blocked: "內容過濾擋下，這段沒有讀到（說話者與記憶只靠程式）", reader_blocked: "內容過濾擋下，讀者檢查沒看到這段",
  review_blocked: "內容過濾擋下，審稿沒看到這段", proofread_blocked: "內容過濾擋下，對照校稿沒看到這段",
  fix_still_wrong: "修正後對照原文，仍有同樣的問題", fix_regressed: "修正帶出新的錯，沒有採用（書裡仍是原譯文）",
  fix_undecided: "修正後對照原文，無法確定是否修好", fix_unchecked: "修正沒有對照原文確認（檢查失敗或被擋）",
  fix_unverified: "修正沒有對照原文確認（已關閉修正驗收）", fix_check_failed: "修正驗收的請求失敗", content_blocked: "內容過濾擋下，保留日文原文", second_person_gender: "對女性用了「你」",
  narration_first_person: "第三人稱的敘述出現「我」", narrator_self_form: "敘述者自稱和全書不一致", offensive_term: "可能是貶稱",
  latin_word: "原文沒有的英文單字", foreign_script: "原文沒有的外文字母", possible_added_name: "可能補了原文沒有的人名", glossary_mismatch: "譯名和名詞表不一致",
  quote_mismatch: "引號和原文不對應", untranslated: "沒有翻譯", japanese_residue: "殘留日文", non_taiwan_glyph: "簡體或日文字形",
  placeholder_error: "排版標記不對", number_mismatch: "數字和原文不同", too_short: "譯文比原文短很多", too_long: "譯文比原文長很多",
  empty_translation: "譯文是空的", narrator_name_leak: "譯文寫出了敘述者的記號", cantonese: "粵語用字", ruby_notation_leak: "讀音標記留在譯文裡", name_form_substitution: "稱呼被換成另一種"};
// what to look at for each kind of flag, in plain words (the program's own evidence stays in the tooltip)
const FINDING_HINT = {quote_mismatch: "引號的種類或數量和原文不同，看看是不是漏了或多了引號", too_short: "譯文明顯比原文短，可能漏譯了一部分",
  too_long: "譯文明顯比原文長，可能加了原文沒有的內容", glossary_mismatch: "名字的寫法和名詞表的譯名不同",
  second_person_gender: "說話的對象是女性（或男性），看看「你／妳」有沒有用對", narration_first_person: "這段是第三人稱敘述，卻寫了「我」",
  narrator_self_form: "敘述者稱呼自己的方式和全書不一致", offensive_term: "這個詞可能是貶義或歧視用語，看看原文是不是這個意思",
  latin_word: "譯文出現原文沒有的英文", foreign_script: "譯文混入原文沒有的其他文字（俄文、韓文等）", possible_added_name: "譯文可能補了原文沒寫的人名", untranslated: "這段沒有翻到",
  japanese_residue: "譯文裡還留著日文", non_taiwan_glyph: "出現簡體字或日文字形", placeholder_error: "排版標記的位置不對",
  number_mismatch: "數字和原文不同", empty_translation: "譯文是空的", cantonese: "出現粵語用字",
  ruby_notation_leak: "讀音標記留在譯文裡", name_form_substitution: "原文的稱呼被換成了另一種", narrator_name_leak: "譯文寫出了敘述者的內部記號"};
const hasCJK = s => /[\u3040-\u30ff\u4e00-\u9fff]/.test(s || "");
function findingLine(f) {
  const say = hasCJK(f.evidence) && !/^[\x00-\x7f]*$/.test(f.evidence.slice(0, 12)) ? f.evidence : (FINDING_HINT[f.type] || f.evidence);
  return `<div class="issue" title="${esc(f.type)}：${esc(f.evidence)}">⚑ <b>${esc(findingLabel(f.type))}</b>　${esc(say)}</div>`;
}
// what the reviewer and the reader's look call an issue (ReviewIssue / ReaderIssue types), in Chinese
const ISSUE = {meaning: "意思譯錯", tone: "語氣不對", speaker: "說話者或對象弄錯", mistranslation: "意思譯錯", omission: "漏譯", addition: "加了原文沒有的內容", speaker_error: "說話者弄錯",
  name_inconsistency: "譯名前後不一致", subject_object_error: "主詞或受詞弄錯", spoiler: "提前劇透", placeholder: "排版標記",
  style: "用語或文風", other: "其他", unclear: "讀不懂", wordplay: "文字遊戲（可能要譯註）", translationese: "翻譯腔", voice: "口吻不像這個角色",
  contradiction: "前後矛盾", ambiguity: "兩種讀法"};
// what the translator can mark on its own text (the translate prompt's `flags`), in Chinese
const TFLAG = {ruby_meaning_kept: "讀音另有含義，譯文保留了兩層意思", wordplay: "文字遊戲", subject_omitted_kept: "保留了原文省略的主詞",
  speaker_uncertain: "不確定說話者", term_uncertain: "名稱沒有定譯，譯者自取", possible_source_error: "原文可能有誤"};
FINDING["model_flag:subject_omitted_kept"] = "譯者保留了省略的主詞（待確認讀者不會讀錯）";
FINDING["model_flag:speaker_uncertain"] = "譯者不確定說話者"; FINDING["model_flag:term_uncertain"] = "譯者自取的譯名";
FINDING["model_flag:possible_source_error"] = "譯者認為原文可能有誤";
function findingLabel(t) {
  if (FINDING[t]) return FINDING[t];
  if (t.startsWith("after_review:")) return "審稿後仍有：" + findingLabel(t.slice(13));
  if (t.startsWith("review:reader_")) return "讀者檢查：" + (ISSUE[t.slice(14)] || t.slice(14));
  if (t.startsWith("review:proofread_")) return "對照校稿：" + (ISSUE[t.slice(17)] || t.slice(17));
  if (t.startsWith("review:")) return "審稿：" + (ISSUE[t.slice(7)] || t.slice(7));
  return t;
}
let poll = null;
async function overviewView(id) {
  const o = await loadBook(id, "overview");
  const stat = (label, v, extra = "") => `<div class="card stat"><span class="muted small">${label}</span><b>${v}</b>${extra}</div>`;
  // before anything is translated the counts are all zero: only the start card
  view.innerHTML = `<div class="card" id="runcard"><p class="muted">讀取翻譯狀態…</p></div>${!o.translated && !o.runs.length ? "" : `<div class="row">
    ${stat("已翻譯", `${o.translatable ? Math.round(100 * o.translated / o.translatable) : 0}%`, `<span class="small muted">${o.translated} / ${o.translatable} 段</span>`)}${stat("這本書總共花費", "US$" + o.spent_usd.toFixed(2), `<span class="small muted">所有執行加總${o.plan_usd ? `；另有 Claude 方案照定價約 US$${o.plan_usd.toFixed(2)}（不另收費）` : ""}</span>`)}${planStat(o.plan_usage)}
    ${stat("有疑點的段落", o.flagged_units, `<a class="small" href="#/b/${id}/review?f=flagged">去看</a>`)}${o.conflict_units ? stat("規則互相牽制", o.conflict_units, `<span class="small muted">退回後譯文沒改，不算錯誤</span>`) : ""}
    ${stat("可再確認的譯法", o.pending_names, `<a class="small" href="#/b/${id}/names">去看看</a>`)}
    ${stat("說話者未定", o.unknown_speakers)}${stat("譯註", o.notes)}${stat("人工修改", o.human_edits)}</div>
    <details class="card"><summary><strong>執行紀錄</strong></summary><div class="scroll"><table><tr><th>run</th><th>模式</th><th>模型</th><th>狀態</th><th>待處理</th><th>費用</th></tr>
    ${o.runs.map(r => `<tr><td class="mono small">${esc(r.id)}</td><td>${esc(r.mode)}</td><td>${esc(r.model)}${r.models_used && r.models_used.length ? `<div class="muted small">實際：${esc(r.models_used.join("、"))}</div>` : ""}</td>
      <td>${esc(RUNSTATUS[r.status] || r.status)}</td><td class="mono">${r.pending}</td><td class="mono">$${(r.cost_usd || 0).toFixed(3)}</td></tr>`).join("")}
    </table></div></details>`}`;
  await runCard(id, o);
}
async function runCard(id, o) {
  clearTimeout(poll);
  const card = $("#runcard"); if (!card) return;
  const p = await api(`book/${id}/run`), r = p.run;
  const money = v => "US$" + (+v || 0).toFixed(2);
  // a run on the Claude plan is not billed: say what it used at list price instead of a spend against a budget
  const cost = r => r.provider === "local" ? `本機模型・<b>不收費</b>` : r.provider === "codex" ? `Codex 方案（不另收費）` : r.provider === "claude" ? `Claude 方案・照定價約 <b>${money(r.plan_usd)}</b>（不另收費）`
    : `已花 <b>${money(r.spent_usd)}</b>（上限 ${money(r.budget_usd)}）`;
  const bars = (p.stages || []).map(s => { const pc = Math.round(100 * s.done / s.total);
    return `<div class="small" style="margin-top:6px">${esc(s.label)}　${s.done} / ${s.total}${s.failed ? `　<span class="issue">失敗 ${s.failed}</span>` : ""}<div class="bar"><i style="width:${pc}%"></i></div></div>`; }).join("");
  const exportBtns = o.translated ? `<button class="btn ${r && r.status === "completed" ? "primary big" : ""}" id="exp">匯出並下載 EPUB</button> <span id="expres" class="small"></span>` : "";
  // where the run is, in words, and about how long is left (from the share of the work done so far)
  // shares of a whole book's time (2026-10-18: read 56 min, check/cast/naming 12, translate 43, proofread 77, reader 5)
  const WEIGHT = {read: .29, verify: .02, cast: .03, terms: .01, naming: .01, translate: .22, review: .01, proofread: .39, reader: .03};
  const stages = p.stages || [], cur = stages.find(s => s.done < s.total);
  const SAY = {read: s => `讀全書：第 ${s.done + 1} / ${s.total} 項（先讀完整本，這時還看不到譯文）`, verify: () => "讀完了，正在覆核有疑點的地方",
    cast: () => "整理人物關係與角色印象", terms: () => "整理書中術語的意思", naming: () => "決定名字與稱呼的譯法", translate: s => `翻譯：${s.done} / ${s.total} 批`,
    review: () => "審稿：重看被標出問題的段落", proofread: s => `對照校稿：逐段對照原文找誤譯（${s.done} / ${s.total}）`, reader: () => "像讀者一樣把譯文讀一遍，修正讀不順的地方"};
  const share = stages.reduce((n, s) => n + (WEIGHT[s.key] || 0) * s.done / s.total, 0) / Math.max(.01, stages.reduce((n, s) => n + (WEIGHT[s.key] || 0), 0));
  const elapsed = r && r.started_at ? (Date.now() - Date.parse(r.started_at)) / 60000 : 0;
  const left = share > .03 && elapsed > 1 ? Math.max(1, Math.round(elapsed / share - elapsed)) : null;
  const now = cur ? (SAY[cur.key] || (() => cur.label))(cur) : stages.length ? "收尾中" : "準備中：先把全書切成場景";
  let body;
  if (p.busy) {
    body = `<div class="run-now"><strong>翻譯中</strong><span>${esc(now)}</span></div>
      <div class="run-meta">${cost(r)}${left ? `・大約還要 <b>${left < 60 ? left + " 分鐘" : (left / 60).toFixed(1) + " 小時"}</b>` : ""}・已進行 ${Math.round(elapsed)} 分鐘</div>
      ${bars}<p class="muted small">可以關掉這個分頁，但不要關掉 NCTE 視窗。暫停後按「繼續翻譯」會從停下的地方接著做。</p>
      <button class="btn" id="pause">暫停</button>
      <details class="small"><summary class="muted">詳細紀錄（給除錯用）</summary><pre class="log">${esc((p.log || []).join("\n"))}</pre></details>`;
  } else if (!r) {
    const k = await api("settings");
    body = `<strong class="big-title">開始翻譯</strong><div id="est" class="est">估價中…</div>
      <ol class="flow small"><li>先把整本讀完：誰在說話、對誰說、角色、稱呼、人物關係（這時還看不到譯文）</li>
        <li>決定名字與稱呼的譯法（自動，不會停下來問）</li><li>一個場景一個場景翻譯，翻完自動檢查、審稿</li></ol>
      <p class="small muted">讀的過程中，「故事進度」「角色檔案」「人物關係圖」會一路長出內容。中途可以暫停，之後接著做。</p>
      ${k.set ? "" : `<p class="issue">還沒設定金鑰：<a href="#/settings">去設定</a></p>`}
      <button class="btn primary big" id="start" disabled>開始翻譯</button>`;
  } else {
    const st = RUNSTATUS[r.status] || r.status;
    let action = "";
    if (r.status === "awaiting_names") action = `<p class="small">有些名字和稱呼的譯法要你決定：<a href="#/b/${id}/names">去定名</a>，決定完回來按繼續。</p><button class="btn primary" data-go>繼續翻譯</button>`;
    else if (r.status === "budget_exhausted") { const more = Math.round((r.spent_usd + (r.left_usd || 0) * 2) * 100) / 100;
      action = `<p class="small">已花 ${money(r.spent_usd)}，達到這次的上限 ${money(r.budget_usd)}。${r.left_usd ? `估計剩下的部分還要約 ${money(r.left_usd)}。` : ""}已完成的部分都保留著，繼續會從停下的地方接著做。</p>
        <button class="btn primary" data-go data-budget="${Math.max(more, r.budget_usd + 1)}">把上限提高到 ${money(Math.max(more, r.budget_usd + 1))} 並繼續</button>`; }
    else if (r.status === "running_elsewhere") action = `<p class="small">${esc(p.elsewhere.source)}（pid ${esc(p.elsewhere.pid)}）正在翻譯這本書；這裡不能同時繼續，等它結束或在那邊暫停後再回來。</p>`;
    else if (r.status !== "completed") action = `<button class="btn primary" data-go>繼續翻譯</button>`;
    body = `<div class="run-now"><strong>${esc(st)}</strong><span class="muted small">最近一次執行：${cost(r)}</span></div>
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
      // Claude Code answers more slowly (a whole book took about 6 hours, waits for the plan's usage window aside)
      const slow = e.provider === "claude" || e.provider === "codex" ? 2.5 : e.provider === "local" ? 6 : 1;
      const lo = Math.max(5, Math.round(e.units * .025 * slow)), hi = Math.max(10, Math.round(e.units * .035 * slow * 1.15));
      const t = m => m < 60 ? `${m} 分鐘` : `${(m / 60).toFixed(1)} 小時`;
      $("#est").innerHTML = `<div><b>${e.units}</b> 段要翻譯</div>${e.provider === "codex" ? `<div>使用你的 ChatGPT 方案額度（Codex），<b>不另收費</b>；額度用完或連線失敗會自動等待再繼續。</div>` : e.provider === "local" ? `<div>在這台電腦上翻譯，<b>不收費</b>；速度看你的電腦與模型，一本書通常要好幾個小時。</div>` : e.provider === "claude" ? `<div>使用你的 Claude 方案額度，<b>不另收費</b>；額度用完會自動等重置再繼續。</div>${e.plan_low_usd != null ? `<div>照定價約 <b>${money(e.plan_low_usd)}–${money(e.plan_high_usd)}</b>（不另收費；依你之前的 Claude 方案執行換算）</div>` : ""}` : `<div>預估費用約 <b>${money(e.estimate_usd)}</b>，最多約 ${money(e.high_usd)}${e.calibrated ? "（依你之前的實際花費校正）" : "（還沒有實際花費可以校正，偏保守）"}</div>`}
        <div>預估時間約 <b>${t(lo)}–${t(hi)}</b></div>${["claude", "codex"].includes(e.provider) ? "" : `<div class="small muted">預算上限自動設為 ${money(e.budget_usd)}，到了會先停下來問你。</div>`}`;
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
  const m = location.hash.match(/[?&]f=(\w+)/); if (m) reviewState.filter = m[1];
  const mc = location.hash.match(/[?&]ch=(\d+)/); if (mc) reviewState.chapter = +mc[1];  // a link to one chapter
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
      ${u.findings.map(findingLine).join("")}
      ${(u.translator_flags || []).length ? `<div class="note" title="譯者自己在這段標的記號">譯者標記：${u.translator_flags.map(f => esc(TFLAG[f] || f)).join("、")}</div>` : ""}
      ${(u.translator_notes || []).map(n => `<div class="note muted" title="譯者留給審稿者的話，不在書裡">譯者說明：${esc(n)}</div>`).join("")}
      ${(u.readings || []).map(r => `<div class="note" title="對照校稿認為原文容得下兩種讀法，不算錯，沒有重譯">兩種讀法：${esc(r)}</div>`).join("")}
      ${(u.conflicts || []).map(r => `<div class="note" title="退回重譯後譯文沒有改：檢查的要求和翻譯的規則互相牽制，這次執行不再退回，請人看一下">規則互相牽制：${esc(r)}</div>`).join("")}</div>
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

// ------------------------------------------------------------ reading the translation
// the translation as a book: one chapter at a time, Chinese only or with the source beside it
const readState = {chapter: null, both: false};
async function readView(id) {
  const chs = (await loadBook(id, "chapters")).filter(c => c.units);
  if (readState.chapter == null || !chs.some(c => c.index === readState.chapter))
    readState.chapter = (chs.find(c => c.kind === "story") || chs[0] || {}).index;
  const i = chs.findIndex(c => c.index === readState.chapter), ch = chs[i];
  if (!ch) { view.innerHTML = `<p class="muted">還沒有可以讀的內容。</p>`; return; }
  const data = await api(`book/${id}/units?start=${ch.start}&end=${ch.end}&limit=5000`);  // the whole chapter
  const nav = `<div class="read-nav"><button class="btn" data-go="-1" ${i > 0 ? "" : "disabled"}>← 上一章</button>
      <select id="rchap">${chs.map(c => `<option value="${c.index}" ${c.index === ch.index ? "selected" : ""}>${esc(c.title)}</option>`).join("")}</select>
      <button class="btn" data-go="1" ${i < chs.length - 1 ? "" : "disabled"}>下一章 →</button></div>`;
  const para = u => u.zh == null ? `<p class="untr ja">${esc(u.ja)}<span class="chip c-unk">還沒翻譯</span></p>`
    : readState.both ? `<div class="read-pair"><p class="ja">${esc(u.ja)}</p><p>${esc(u.zh)}${u.note ? `<span class="rnote">（${esc(u.note)}）</span>` : ""}</p></div>`
    : `<p class="${u.kind === "heading" ? "rh" : ""}">${esc(u.zh)}${u.note ? `<span class="rnote">（${esc(u.note)}）</span>` : ""}</p>`;
  view.innerHTML = `<div class="row" style="align-items:center;margin-bottom:10px;justify-content:space-between">${nav}
      <div class="seg"><button data-b="0" class="${readState.both ? "" : "on"}">只看中文</button><button data-b="1" class="${readState.both ? "on" : ""}">中日對照</button></div></div>
    <article class="card reader ${readState.both ? "both" : ""}">${data.units.length && data.units[0].kind === "heading" ? "" : `<h3>${esc(ch.title)}</h3>`}${data.units.map(para).join("")}
      </article>${nav}`;
  view.querySelectorAll("[data-go]").forEach(b => b.onclick = () => { readState.chapter = chs[i + +b.dataset.go].index; readView(id); window.scrollTo(0, 0); });
  view.querySelectorAll("#rchap").forEach(s => s.onchange = () => { readState.chapter = +s.value; readView(id); window.scrollTo(0, 0); });
  view.querySelectorAll("[data-b]").forEach(b => b.onclick = () => { readState.both = b.dataset.b === "1"; readView(id); });
}

// ------------------------------------------------------------ naming
let namesAll = false, lastRename = null;
async function namesView(id) {
  const [rows, st, terms] = await Promise.all([api(`book/${id}/proposals${namesAll ? "?all=1" : ""}`), api(`book/${id}/style`).catch(() => null),
    api(`book/${id}/terms`).catch(() => [])]);
  const pend = rows.filter(r => r.status === "pending");
  // the house style (ADR-053): how honorifics are handled and why, small, under the names
  const styleCard = st && !st.default ? `<details class="card small"><summary><strong>全書體例</strong> <span class="muted">${st.auto ? "（模型讀完全書後定的）" : "（人工設定）"}</span></summary>
    <div>敬稱：${esc(st.honorifics_zh)}</div>${st.reason ? `<div class="muted">理由：${esc(st.reason)}</div>` : ""}
    ${st.rules.length ? `<div style="margin-top:4px">規則：${st.rules.map(r => esc(r)).join("；")}</div>` : ""}</details>` : "";
  // what the book's recurring terms mean (ADR-069), as the translator gets them where the term occurs; a meaning the
  // book reveals bit by bit has several stages, each shown from its own position (the rows come in story order)
  const byTerm = new Map();
  (terms || []).forEach(t => { if (!byTerm.has(t.ja)) byTerm.set(t.ja, []); byTerm.get(t.ja).push(t); });
  const stageLine = t => `${esc(t.definition)}${t.contrast_with ? ` <span class="muted">（對照：${esc(t.contrast_with)}）</span>` : ""}
      <span class="muted">${t.origin === "human" ? "人工" : `§${t.revealed_at} 起`}</span>`;
  const termsCard = byTerm.size ? `<details class="card small"><summary><strong>術語的意思</strong> <span class="muted">（${byTerm.size} 個；讀完全書後整理，翻譯與校稿時附給模型參考）</span></summary>
    ${[...byTerm.values()].map(st => `<div style="margin-top:4px"><span class="ja">${esc(st[0].ja)}</span>${st[0].zh ? ` → ${esc(st[0].zh)}` : ""}${st.length === 1 ? `　${stageLine(st[0])}`
      : `<span class="muted">（逐步揭露，${st.length} 段說明；各自從所寫的位置起附上）</span>${st.map(t => `<div style="margin-left:1.5em">${stageLine(t)}</div>`).join("")}`}</div>`).join("")}</details>` : "";
  const banner = lastRename ? `<div class="card" style="border-color:var(--accent)">
      <strong>已翻譯的 ${lastRename.affected} 段還在用舊譯法「${esc(lastRename.old)}」</strong>（${esc(lastRename.label)} → ${esc(lastRename.zh)}）
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn primary" id="rnReplace">直接把「${esc(lastRename.old)}」換成「${esc(lastRename.zh)}」</button>
      <button class="btn" id="rnList">列出段落與重翻指令</button><button class="btn" id="rnSkip">先不要</button></div>
      <div class="muted small">直接替換只改字，不花錢；想讓語氣一起調整就重翻那些段落。人工修改過的段落不會被改。</div><div id="rnOut"></div></div>` : "";
  view.innerHTML = banner + `<div class="row" style="align-items:center;margin-bottom:10px"><strong>待決定 ${pend.length} 項</strong>
    ${pend.length ? `<button class="btn" id="acceptAll" title="每一項都選標有「建議」的那個譯法">全部採用建議</button>` : ""}
    <label class="small muted"><input type="checkbox" id="showAll" ${namesAll ? "checked" : ""}> 也顯示已套用、已決定的</label></div>
    ${rows.map(r => `<div class="card" data-pid="${r.id}">
      <div class="row" style="align-items:baseline;gap:10px"><strong>${esc(r.label)}</strong>${readingsTag(r.readings)}
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
    || `<p class="muted">沒有待決定的譯法。翻譯前的「定名」階段會在這裡列出需要你選的名字與稱呼。</p>`}${styleCard}${termsCard}`;
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
let POSIDX = null, AT_LINK = null;
async function readingData(id) { return readingDataFor(id, "characters"); }
async function readingDataFor(id, what) {
  const d = await loadBook(id, what);
  const pos = $("#pos"), story = d.scenes.filter(s => s.story && s.a >= 0);
  pos.max = story.length;
  // a link to a point in the story (?at=): applied whenever the link changes, so the slider still moves freely after it
  const at = location.hash.match(/[?&]at=(\d+)/);
  if (at && at[0] !== AT_LINK) { POSIDX = +at[1]; } AT_LINK = at ? at[0] : null;
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
    return head + `<div class="card" id="scene-${esc(s.id)}"><div class="muted small">場景 ${i + 1}・段 ${s.a}–${s.b}${s.unsure != null ? ` ${chip("c-warn", "模型標需複核")} <span title="切場景時模型自己標的：這個換場點要人看一下">${esc(s.unsure)}</span>` : ""}</div><div>${esc(s.summary) || '<span class="muted">（沒有摘要）</span>'}</div>
      <div class="muted small">出場：${s.who.map(c => esc(nm(c))).join("、") || "—"}</div>
      ${(s.questions || []).map(q => `<div class="note small" title="讀這個場景時模型自己提出的疑問；回答是讀完全書後帶前後文覆核的">疑問：${esc(q.question)}<br>${q.answer ? "回答：" + esc(q.answer) : '<span class="muted">（還沒有回答）</span>'}</div>`).join("")}
      <details data-a="${s.a}" data-b="${s.b}"><summary class="small muted">看這個場景的原文與譯文</summary><div class="body muted small">載入中…</div></details></div>`; }).join("");
  view.innerHTML = `<div class="card scroll"><strong>誰在哪一章出場</strong> <span class="muted small">數字＝場景數；點名字看角色檔案</span>
    <div class="grid" style="grid-template-columns:110px repeat(${usedCh.length},minmax(30px,1fr));margin-top:8px">${grid}</div></div>
    <p class="muted small">摘要是模型整理的，可能有誤；展開可對照原文。封面、目次、後記等書籍資訊不列入。</p>${list || `<p class="muted">還沒讀到任何場景。開始翻譯後，引擎每讀完一個場景，這裡就會多一則摘要。</p>`}`;
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
  let card = `<p class="muted">還沒有角色。讀的階段會一路記下角色，讀完整本後這裡會列出每個人的稱呼、關係與事實。</p>`;
  const p = d.characters[peopleState.person];
  if (p) {
    const pid = peopleState.person;
    const addr = d.address.filter(a => a.at == null || a.at <= upto);
    const called = addr.filter(a => a.to === pid && a.speaker !== pid), calls = addr.filter(a => a.speaker === pid);
    const co = {}; seen.forEach(s => { if (s.who.includes(pid)) s.who.forEach(o => { if (o !== pid) co[o] = (co[o] || 0) + 1; }); });
    const F = p.facts.filter(f => f.at <= upto), EX = p.examples.filter(e => e.at <= upto);
    const ftype = {objective: "客觀", character_belief: "角色認為", narrator_claim: "敘述者說"};
    card = `<div class="row" style="align-items:baseline;gap:10px"><h2 style="margin:0">${esc(p.zh)}</h2><span class="ja">${esc(p.ja)}</span>${readingsTag(p.readings)}
      <span class="muted small">初登場 §${p.from}・出場 ${p.scenes} 場景・台詞 ${p.lines} 句</span></div>
      ${p.aka.length ? `<div class="muted small">其他稱呼：${p.aka.map(a => `<span class="ja">${esc(a.ja)}</span>${readingsTag(a.readings)}${a.zh && a.zh !== a.ja ? `（${esc(a.zh)}）` : ""}`).join("・")}</div>` : ""}
      ${(p.first_person || []).length ? `<div class="small" style="margin-top:4px">自稱：${p.first_person.map(([w, n]) => `<span class="form-tag"><span class="ja">${esc(w)}</span><span class="n">×${n}</span></span>`).join(" ")}</div>` : ""}
      ${genderLine(p, upto)}
      ${groupLine(p, d, nm)}
      <div class="secnav">${[["sec-rel", "人物關係"], ["sec-addr", "稱呼"], ["sec-co", "常一起出場"], ["sec-ex", "說話例句"], ["sec-facts", "已知事實"]]
        .map(([k, t]) => `<button class="btn small" data-sec="${k}">${t}</button>`).join("")}</div>
      ${p.impression ? `<div class="card" style="margin-top:10px;background:var(--accent-soft)"><strong>角色印象</strong> <span class="muted small">${p.impression.read_all ? "讀完全書後整理" : `讀到 §${p.impression.read_to ?? "?"} 為止整理`}</span>
        <div class="small" style="margin-top:4px">個性：${esc(p.impression.personality)}</div>
        <div class="small">口吻：${esc(p.impression.voice)}</div>
        <div class="small">定位：${esc(p.impression.role)}</div></div>` : ""}
      <div id="sec-rel">${relationList(d, pid, upto, nm)}</div>
      <div class="row" id="sec-addr" style="margin-top:12px;gap:24px">
        <div style="flex:1 1 320px"><div class="addr-h"><strong>別人怎麼叫他</strong><span class="muted small">左邊是說話的人</span></div>${byPerson(called, a => a.speaker, k => esc(nm(k))) || '<div class="muted">—</div>'}</div>
        <div style="flex:1 1 320px"><div class="addr-h"><strong>他怎麼叫別人</strong><span class="muted small">左邊是被叫的人</span></div>${byPerson(calls, a => a.to, k => k === pid ? "（提到自己）" : esc(nm(k))) || '<div class="muted">—</div>'}</div></div>
      <div id="sec-co" style="margin-top:12px"><strong>常一起出場</strong><div>${Object.entries(co).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([o, k]) => `<a href="#" data-k="${esc(o)}">${esc(nm(o))}</a> <span class="muted small">${k}</span>`).join("　") || "—"}</div></div>
      <div id="sec-ex" style="margin-top:12px"><strong>說話例句</strong> <span class="muted small">原文與該段譯文</span>${EX.map(e => `<div style="margin-top:6px">${bi(e.text, e.zh)}</div>`).join("") || '<div class="muted">—</div>'}</div>
      <div id="sec-facts" style="margin-top:12px"><strong>已知事實</strong> <span class="muted small">${F.length} 筆</span><div class="scroll"><table><tr><th>段</th><th>內容</th><th>類型</th><th>原文依據</th></tr>
      ${F.map(f => `<tr><td class="mono small">${f.at}</td><td>${esc(f.predicate)}：${esc(f.object)}</td><td>${chip("c-acc", ftype[f.type] || f.type)}${chip(f.certainty === "explicit" ? "c-ok" : "c-warn", f.certainty === "explicit" ? "明寫" : "推測")}</td><td class="small">${bi(f.quote, f.quote_zh)}</td></tr>`).join("")}</table></div></div>`;
  }
  view.innerHTML = `<div class="row" style="align-items:flex-start"><div class="card plist" style="flex:0 1 210px;max-height:80vh;overflow:auto">
    <div class="muted small">主要角色</div>${groups.main.map(btn).join("")}<div class="muted small" style="margin-top:8px">次要角色</div>${groups.sub.map(btn).join("")}
    <details><summary class="muted small">其他 ${groups.minor.length} 個</summary>${groups.minor.map(btn).join("")}</details>
    <details><summary class="muted small">書籍資訊裡的人名 ${groups.info.length} 個</summary>${groups.info.map(btn).join("")}</details></div>
    <div class="card" style="flex:1 1 480px">${card}</div></div>`;
  view.querySelectorAll("[data-k]").forEach(b => b.onclick = e => { e.preventDefault(); peopleState.person = b.dataset.k; peopleView(id); });
  view.querySelectorAll("[data-sec]").forEach(b => b.onclick = () => { const el = document.getElementById(b.dataset.sec); if (el) el.scrollIntoView({behavior: "smooth", block: "start"}); });
  const gs = $("#genderSet"); if (gs) gs.onchange = async () => {
    try { await api(`book/${id}/characters/${peopleState.person}/gender`, {gender: gs.value}); invalidate(id); toast("性別已更新（之後的翻譯會用）"); peopleView(id); }
    catch (e) { toast(e.message); } };
}
// relations of one person as known at the slider position (begun, not yet replaced), each with its source quote
// a source line with the translation of its paragraph under it (both languages, for comparison)
// the readings the source annotates on a name (彩葉《いろは》): small, next to the name
function readingsTag(r) { return (r || []).length ? ` <span class="muted small" title="原文標註的讀音">《${r.map(esc).join("、")}》</span>` : ""; }
function bi(ja, zh, cls = "") {
  return `<span class="bi ${cls}"><span class="ja">${esc(ja || "")}</span>${zh ? `<span class="zh">${esc(zh)}</span>` : ""}</span>`;
}
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
      <td>${kindChip(r)}${r.certainty === "inferred" ? chip("c-warn", "推測") : ""}${r.origin !== "llm" ? chip("c-ok", "人工") : ""}</td>
      <td class="mono small">§${r.at}</td><td class="small">${ev ? bi(ev.quote, ev.zh) : ""}</td></tr>`;
  }).join("");
  return `<div style="margin-top:12px"><strong>人物關係</strong> <span class="muted small">對方是他的什麼</span>
    <div class="scroll"><table class="rel-table"><tr><th>對方</th><th>關係</th><th>類型</th><th>從</th><th>原文依據（附譯文）</th></tr>${rows}</table></div></div>`;
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
  return `<div class="small" style="margin-top:4px">性別：${g ? `${label[g.g]} ${chip(...sure[g.sure])} ${g.quote ? `<span class="ja muted">「${esc(g.quote)}」</span>${g.quote_zh ? `<span class="muted">（譯：${esc(g.quote_zh.slice(0, 80))}${g.quote_zh.length > 80 ? "…" : ""}）</span>` : ""}` : ""}`
    : '<span class="muted">不明（翻譯時用「你」）</span>'}
    <select id="genderSet" title="改成你確定的性別">${[["unknown", "不明"], ["female", "女"], ["male", "男"]].map(([v, t]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${t}</option>`).join("")}</select></div>`;
}
const G = {mode: "rel", mentions: false, n: 12, hideMinor: true, fade: true, series: false, focus: null, pair: null, layout: null};
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
// kinds of relation: one person in two identities (the model's judgement, kind "same_person") first, then identity,
// then feeling; within a kind, by where it begins
const KIND_RANK = {same_person: 0, identity: 1, feeling: 2};
const relOrder = (x, y) => (KIND_RANK[x.kind] ?? 1) - (KIND_RANK[y.kind] ?? 1) || x.at - y.at;
const kindChip = r => r.kind === "same_person" ? chip("c-ok", "同一人") : chip(r.kind === "feeling" ? "c-warn" : "c-acc", r.kind === "feeling" ? "情感" : "身分");
async function graphView(id) {
  view.classList.add("wide");  // the graph uses the whole window width
  const ov = await loadBook(id, "overview");
  const {d, upto, nm} = await readingDataFor(id, G.series ? "characters?series=1" : "characters");
  const story = d.scenes.filter(s => s.story);
  const members = s => G.mode === "talk" && !G.mentions ? s.speakers : s.who;
  const shown = c => d.characters[c] && !d.characters[c].info &&
    !(G.hideMinor && (GROUPISH.test(d.characters[c].ja) || GROUPISH.test(d.characters[c].zh)));
  // who and what is known at a point of the story
  const at = u => {
    const ok = c => shown(c) && d.characters[c].from <= u;
    const scenes = story.filter(s => s.a <= u);
    const cnt = {}; scenes.forEach(s => members(s).forEach(c => { if (ok(c)) cnt[c] = (cnt[c] || 0) + 1; }));
    let nodes = Object.keys(cnt).sort((a, b) => cnt[b] - cnt[a]);
    if (G.hideMinor) nodes = nodes.filter(c => cnt[c] >= 2);
    if (G.n) nodes = nodes.slice(0, G.n);
    const rels = (d.relations || []).filter(r => r.at <= u && (r.until == null || r.until > u) && ok(r.a) && ok(r.b));
    if (G.mode === "rel") {  // the people the relations are about (most present first), nobody without one
      const inRel = new Set(rels.flatMap(r => [r.a, r.b]));
      inRel.forEach(c => { cnt[c] = cnt[c] || 1; });
      nodes = [...inRel].sort((a, b) => cnt[b] - cnt[a]);
      if (G.n && nodes.length > Math.max(G.n, 20)) { const keep = new Set(nodes.slice(0, G.n)); nodes = nodes.filter(c => keep.has(c)); }
    }
    const inSet = new Set(nodes), pairScenes = {};
    scenes.forEach(s => { const m = members(s).filter(c => inSet.has(c)); m.forEach((a, i) => m.slice(i + 1).forEach(b => {
      const k = [a, b].sort().join("|"); (pairScenes[k] = pairScenes[k] || []).push(s); })); });
    const addr = d.address.filter(a => inSet.has(a.speaker) && inSet.has(a.to) && a.speaker !== a.to && (a.at == null || a.at <= u));
    const edges = G.mode === "addr"
      ? Object.values(addr.reduce((m, a) => { const k = [a.speaker, a.to].sort().join("|"); (m[k] = m[k] || {a: a.speaker, b: a.to, w: 0}).w += a.uses; return m; }, {}))
      : G.mode === "rel"
      ? Object.values(rels.filter(r => inSet.has(r.a) && inSet.has(r.b)).reduce((m, r) => { const k = [r.a, r.b].sort().join("|");
          (m[k] = m[k] || {a: r.a, b: r.b, w: 1 + (pairScenes[k] || []).length, rels: []}).rels.push(r); return m; }, {}))
      : Object.entries(pairScenes).map(([k, ss]) => { const [a, b] = k.split("|"); return {a, b, w: ss.length}; });
    edges.forEach(e => e.rels && e.rels.sort(relOrder));
    return {scenes, cnt, nodes, rels, pairScenes, addr, edges};
  };
  const now = at(upto);
  // positions come from the whole book and stay put while the slider moves: people appear in their place, lines grow
  const key = [id, G.mode, G.n, G.hideMinor, G.mentions, G.series].join("|");
  if (!G.layout || G.layout.key !== key) {
    const full = at(Infinity), maxF = Math.max(1, ...full.nodes.map(c => full.cnt[c]));
    const radF = c => 26 + 18 * Math.sqrt((full.cnt[c] || 1) / maxF);
    let center = null;
    if (G.mode === "rel" && full.nodes.length) {  // the person with the most relations over the whole book
      const deg = {}; full.edges.forEach(e => { deg[e.a] = (deg[e.a] || 0) + 1; deg[e.b] = (deg[e.b] || 0) + 1; });
      center = full.nodes.slice().sort((a, b) => (deg[b] || 0) - (deg[a] || 0) || full.cnt[b] - full.cnt[a])[0];
    }
    const P = !full.nodes.length ? {_box: {x: 0, y: 0, w: 900, h: 620}}
      : center ? radial(full.nodes, full.edges, center, radF) : layout(full.nodes, full.edges, 900, 620, radF);
    G.layout = {key, P, maxN: maxF};
  }
  const P = G.layout.P, W = P._box.w, H = P._box.h;
  const nodes = now.nodes.filter(c => P[c]), inSet = new Set(nodes), cnt = now.cnt;
  const edges = now.edges.filter(e => inSet.has(e.a) && inSet.has(e.b));
  const rad = c => 26 + 18 * Math.sqrt((cnt[c] || 1) / G.layout.maxN);
  const mx = Math.max(1, ...edges.map(e => e.w));
  // the previous step of the slider: relations that began since then are new (or a change, when one ended there)
  const idx = story.findIndex(s => s.a - 1 === upto), prev = idx > 0 ? story[idx - 1].a - 1 : null;
  const isNew = r => prev != null && r.at > prev && r.at <= upto;
  const isChange = r => isNew(r) && (d.relations || []).some(o => o !== r && o.until === r.at && [o.a, o.b].sort().join() === [r.a, r.b].sort().join());
  // people not seen in the last eight scenes fade (still known; just not around lately)
  const recent = new Set(now.scenes.slice(-8).flatMap(s => s.who));
  const near = new Set(G.focus ? [G.focus, ...edges.filter(e => e.a === G.focus || e.b === G.focus).flatMap(e => [e.a, e.b])] : nodes);
  const fading = G.fade && upto !== Infinity;  // the whole book: nobody is "lately absent"
  const dim = c => (near.has(c) ? 1 : .15) * (fading && !recent.has(c) ? .45 : 1);
  const crowded = nodes.length > 12;  // many people: labels on the lines of the middle / chosen person only, others on hover
  const middle = G.focus || (G.mode === "rel" ? nodes.slice().sort((a, b) => edges.filter(e => e.a === b || e.b === b).length - edges.filter(e => e.a === a || e.b === a).length)[0] : null);
  let svg = (nodes.length ? "" : `<text x="${P._box.x + W / 2}" y="${P._box.y + H / 2}" font-size="16" text-anchor="middle" fill="var(--muted)">${
    G.mode === "rel" ? "讀到這裡還沒有人物關係" : "讀到這裡還沒有人物"}</text>`) + `<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" fill="var(--accent)"/></marker></defs>`;
  if (G.mode === "addr") {
    const seen = {};
    now.addr.filter(a => inSet.has(a.speaker) && inSet.has(a.to)).forEach(a => { const key = a.speaker + a.to; seen[key] = (seen[key] || 0) + 1;
      const p = P[a.speaker], q = P[a.to], dx = q.x - p.x, dy = q.y - p.y, L = Math.max(1, Math.hypot(dx, dy)), ux = dx / L, uy = dy / L, o = seen[key] * 9, nx = -uy * o, ny = ux * o;
      const op = Math.min(dim(a.speaker), dim(a.to)), pk = [a.speaker, a.to].sort().join("|");
      svg += `<g data-pair="${esc(pk)}" style="cursor:pointer;opacity:${op}"><line x1="${p.x + ux * 30 + nx}" y1="${p.y + uy * 30 + ny}" x2="${q.x - ux * 34 + nx}" y2="${q.y - uy * 34 + ny}" stroke="var(--accent)" stroke-width="1.4" marker-end="url(#ah)"/>
        <text x="${(p.x + q.x) / 2 + nx * 2.2}" y="${(p.y + q.y) / 2 + ny * 2.2}" font-size="11" text-anchor="middle">${esc(a.ja)}${a.zh ? "（" + esc(a.zh) + "）" : ""}</text></g>`; });
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
    const pt = (e, h, t) => { const p = P[e.a], q = P[e.b], L = Math.max(1, Math.hypot(q.x - p.x, q.y - p.y)),
      cx = (p.x + q.x) / 2 - (q.y - p.y) / L * h, cy = (p.y + q.y) / 2 + (q.x - p.x) / L * h, u = 1 - t;
      return {x: u * u * p.x + 2 * t * u * cx + t * t * q.x, y: u * u * p.y + 2 * t * u * cy + t * t * q.y, cx, cy}; };
    // labels are placed after all lines are known: each tries a few spots along its line (and a little to either
    // side) and takes the first that covers no person and no label already placed — any label, any book
    const reqs = [], parts = [];
    edges.forEach((e, k) => {
      const op = Math.min(dim(e.a), dim(e.b)), pk = [e.a, e.b].sort().join("|"), h = bend(e), mid = pt(e, h, 0.5);
      const dpath = `M${P[e.a].x},${P[e.a].y} Q${mid.cx},${mid.cy} ${P[e.b].x},${P[e.b].y}`;
      const rs = e.rels || [], main = rs[0], more = rs.length - 1;
      const same = main && main.kind === "same_person", fresh = rs.some(isNew), change = rs.some(isChange);
      // line: identity solid, feeling dashed, inferred lighter; a reveal (one person) a double line; new ones in colour
      const color = fresh || G.pair === pk ? "var(--accent)" : "var(--muted)";
      const dash = main && main.kind === "feeling" ? ` stroke-dasharray="6 5"` : "";
      const alpha = (main && main.certainty === "inferred" ? .5 : 1) * (.3 + .6 * e.w / mx);
      const width = 1 + 6 * e.w / mx;
      const title = `${esc(nm(e.a))}・${esc(nm(e.b))}：${rs.length ? rs.map(r => esc(r.rel)).join("、") : e.w + " 個場景"}`;
      // many people: only the middle person's labels show, others on hover — except two identities of one person,
      // a reveal the reader needs to see
      const soft = crowded && G.mode === "rel" && middle && e.a !== middle && e.b !== middle && !same;
      const ask = (text, t0, size) => reqs.push({k, e, h, text, t0, size, soft, w: e.w});
      if (main) {
        const tag = change ? "（變）" : fresh ? "（新）" : "";
        if (same) ask(`＝ ${main.rel}${main.certainty === "inferred" ? "（推測）" : ""}${tag}`, 0.5, 14);
        else if (directed(main)) {  // two different roles (父親／女兒): each written next to its person
          [[main.a, main.a_is], [main.b, main.b_is]].forEach(([who, role]) => ask(role, who === e.a ? 0.3 : 0.7, 14));
          if (more || tag) ask(`${more ? "＋" + more : ""}${tag}`, 0.5, 12);
        } else ask(`${main.rel}${more ? " ＋" + more : ""}${tag}`, 0.5, 14);
      }
      const stroke = same  // a double line; dashed when it is inferred
        ? `<path d="${dpath}" fill="none" stroke="var(--accent)" stroke-width="7" stroke-opacity=".9"${main.certainty === "inferred" ? ` stroke-dasharray="10 6"` : ""}/><path d="${dpath}" fill="none" stroke="var(--panel)" stroke-width="3"/>`
        : `<path d="${dpath}" fill="none" stroke="${color}" stroke-opacity="${alpha}" stroke-width="${width}"${dash}/>`;
      parts.push({pk, op, head: `<path d="${dpath}" fill="none" stroke="transparent" stroke-width="12"/>${stroke}<title>${title}</title>`, labels: ""});
    });
    const placed = [];
    const widthOf = (text, size) => [...text].reduce((n, ch) => n + (ch.charCodeAt(0) > 255 ? size : size * .58), 0) + 6;
    const hits = r => nodes.some(c => { const cx = Math.max(r.x, Math.min(P[c].x, r.x + r.w)), cy = Math.max(r.y, Math.min(P[c].y, r.y + r.h));
        return Math.hypot(P[c].x - cx, P[c].y - cy) < rad(c) + 2; })
      || placed.some(q => r.x < q.x + q.w && q.x < r.x + r.w && r.y < q.y + q.h && q.y < r.y + r.h);
    reqs.sort((a, b) => a.soft - b.soft || b.w - a.w);  // visible labels and busy lines choose first
    reqs.forEach(q => {
      const w = widthOf(q.text, q.size), hgt = q.size * 1.3, p = P[q.e.a], o = P[q.e.b];
      const L = Math.max(1, Math.hypot(o.x - p.x, o.y - p.y)), nx = -(o.y - p.y) / L, ny = (o.x - p.x) / L;
      let best = null;
      for (const dt of [0, .07, -.07, .14, -.14, .21, -.21]) {
        const t = Math.min(.85, Math.max(.15, q.t0 + dt));
        for (const off of [0, 14, -14, 28, -28]) {
          const c = pt(q.e, q.h, t), r = {x: c.x + nx * off - w / 2, y: c.y + ny * off - hgt / 2 - 4, w, h: hgt};
          if (!hits(r)) { best = r; break; }
        }
        if (best) break;
      }
      if (!best) { const c = pt(q.e, q.h, q.t0); best = {x: c.x - w / 2, y: c.y - hgt / 2 - 4, w, h: hgt}; }
      placed.push(best);
      parts[q.k].labels += `<text x="${best.x + w / 2}" y="${best.y + hgt * .75}" font-size="${q.size}" text-anchor="middle" class="rel-label${q.soft ? " soft" : ""}">${esc(q.text)}</text>`;
    });
    parts.forEach(g => { svg += `<g data-pair="${esc(g.pk)}" class="edge" style="cursor:pointer;opacity:${g.op}">${g.head}${g.labels}</g>`; });
  }
  nodes.forEach(c => { const r = rad(c);
    svg += `<g data-k="${esc(c)}" style="cursor:pointer;opacity:${dim(c)}"><circle cx="${P[c].x}" cy="${P[c].y}" r="${r}" fill="${G.focus === c ? "var(--accent-soft)" : "var(--panel)"}" stroke="var(--accent)" stroke-width="${G.focus === c ? 3 : 1.6}"/>
      <text x="${P[c].x}" y="${P[c].y + 5}" text-anchor="middle" font-size="15" font-weight="600">${esc(nm(c))}</text><title>${esc(nm(c))}：${cnt[c]} 個場景${recent.has(c) ? "" : "（最近沒出場）"}</title></g>`; });
  const sel = (name, opts, cur) => `<select data-ctl="${name}">${opts.map(([v, l]) => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  const legend = G.mode === "rel" ? `<div class="legend small">
      <span><svg width="36" height="10"><line x1="0" y1="5" x2="36" y2="5" stroke="var(--muted)" stroke-width="3"/></svg> 身分（親屬、師生、婚約…）</span>
      <span><svg width="36" height="10"><line x1="0" y1="5" x2="36" y2="5" stroke="var(--muted)" stroke-width="3" stroke-dasharray="6 5"/></svg> 情感、立場（好友、愛慕、敵對…）</span>
      <span><svg width="36" height="10"><line x1="0" y1="5" x2="36" y2="5" stroke="var(--accent)" stroke-width="3"/></svg> 這一步新出現（新）或改變（變）</span>
      <span><svg width="36" height="12"><line x1="0" y1="6" x2="36" y2="6" stroke="var(--accent)" stroke-width="7"/><line x1="0" y1="6" x2="36" y2="6" stroke="var(--panel)" stroke-width="3"/></svg> 同一人的不同身分（虛線＝推測）</span>
      <span>線越粗＝一起出場越多；淡色＝推測；＋N＝還有 N 個關係（點線看全部）${fading ? "；淡化的人＝最近 8 個場景沒出場" : ""}</span></div>` : "";
  view.innerHTML = `<div class="row" style="align-items:center;margin-bottom:10px;gap:10px">
      <div class="seg"><button data-m="rel" class="${G.mode === "rel" ? "on" : ""}">關係</button><button data-m="talk" class="${G.mode === "talk" ? "on" : ""}">一起出場</button><button data-m="addr" class="${G.mode === "addr" ? "on" : ""}">誰怎麼叫誰</button></div>
      ${G.mode === "talk" ? `<label class="small"><input type="checkbox" data-ctl="mentions" ${G.mentions ? "checked" : ""}> 只被提到也算</label>` : ""}
      ${sel("n", [[12, "12 人"], [20, "20 人"], [0, "全部"]], G.n)}
      <label class="small"><input type="checkbox" data-ctl="hideMinor" ${G.hideMinor ? "checked" : ""}> 隱藏路人、群體</label>
      <label class="small"><input type="checkbox" data-ctl="fade" ${G.fade ? "checked" : ""}> 淡化最近沒出場的人</label>
      <div class="seg"><button data-s="0" class="${G.series ? "" : "on"}">本集</button><button data-s="1" class="${G.series ? "on" : ""}">系列累計</button></div>
      ${G.focus ? `<button class="btn" data-clear>取消聚焦</button>` : ""}</div>
    <div class="card scroll" style="padding:6px"><svg viewBox="${P._box.x} ${P._box.y} ${W} ${H}" width="100%" style="min-width:640px;min-height:560px;max-height:82vh">${svg}</svg>${legend}</div>
    <div class="row" style="align-items:flex-start"><div class="card" style="flex:1 1 340px" id="gside">${graphSide(d, nm, upto, now.pairScenes, now.addr, cnt)}</div></div>
    <p class="muted small">${G.mode === "rel" ? `線＝故事中的關係（讀到揭露處才出現）${now.rels.length || (d.relations || []).length ? "" : "；這本書還沒有整理關係（ncte names relations）"}${crowded ? "；人多時只標出中心人物的關係，其他的滑過線條才顯示" : ""}` : G.mode === "talk" ? (G.mentions ? "線＝同一場景出現（含只被提到）" : "線＝兩人在同一場景都有台詞") : "箭頭＝誰用什麼稱呼叫誰"}；圓圈大小＝出場場景數；人物位置依全書固定，拖動「讀到」只會增減人物與關係。點人物聚焦，點線看兩人的關係與互動。${ov.title}</p>`;
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
    const names = c => [d.characters[c].ja, d.characters[c].zh, ...d.characters[c].aka.map(x => x.ja)].filter(x => x && x.length >= 2);
    const facts = [a, b].flatMap(x => d.characters[x].facts.filter(f => f.at <= upto && names(x === a ? b : a).some(n => (f.object + f.predicate).includes(n)))
      .map(f => ({...f, who: x})));
    const between = d.address.filter(t => (t.speaker === a && t.to === b) || (t.speaker === b && t.to === a)).filter(t => t.at == null || t.at <= upto)
      .sort((x, y) => (x.at ?? -1) - (y.at ?? -1));
    // the pair's relations as a timeline: each with where it began, where it ended, and the source with its translation
    const timeline = (d.relations || []).filter(r => [r.a, r.b].sort().join("|") === G.pair && r.at <= upto).sort((x, y) => x.at - y.at);
    return `<strong>${esc(nm(a))} ⇄ ${esc(nm(b))}</strong>
      <div style="margin-top:8px"><div class="muted small">關係的變化（讀到這裡為止）</div>${timeline.map(r => {
        const ended = r.until != null && r.until <= upto, ev = (r.evidence || []).find(e => e.at <= upto);
        return `<div class="small" style="margin-top:6px${ended ? ";opacity:.6" : ""}"><span class="mono">${r.at < 0 ? "前集" : "§" + r.at}${ended ? "–§" + r.until : " 起"}</span>
          <strong>${esc(r.rel)}</strong>${directed(r) ? `（${esc(nm(r.a))}是${esc(r.a_is)}，${esc(nm(r.b))}是${esc(r.b_is)}）` : ""}
          ${kindChip(r)}${r.certainty === "inferred" ? chip("c-warn", "推測") : ""}${ended ? chip("c-unk", "已結束") : ""}
          ${ev ? `<div style="margin-top:2px">${bi(ev.quote, ev.zh, "small")}</div>` : ""}</div>`; }).join("") || '<div class="muted">—</div>'}</div>
      <div style="margin-top:8px"><div class="muted small">互相怎麼稱呼（依首次出現）</div>${between.map(t => `<div>${esc(nm(t.speaker))} → ${esc(nm(t.to))}：<span class="ja">${esc(t.ja)}</span>（${esc(t.zh || "")}）<span class="muted small">${t.at < 0 ? "前集" : "§" + t.at}・×${t.uses}</span></div>`).join("") || '<div class="muted">—</div>'}</div>
      <div style="margin-top:8px"><div class="muted small">相關事實</div>${facts.map(f => `<div class="small">${esc(nm(f.who))} ${esc(f.predicate)}：${esc(f.object)} ${chip(f.certainty === "explicit" ? "c-ok" : "c-warn", f.certainty === "explicit" ? "明寫" : "推測")}<div class="muted">${bi(f.quote, f.quote_zh)}</div></div>`).join("") || '<div class="muted">—</div>'}</div>
      <div style="margin-top:8px"><div class="muted small">一起出場的場景（${ss.length}）</div>${ss.map(s => `<div class="small">${s.a < 0 ? "<span class='muted'>前集</span> " : `<a href="#" data-goscene="${esc(s.id)}">§${s.a}</a> `}${esc((s.summary || "").slice(0, 60))}</div>`).join("")}</div>`;
  }
  if (G.focus && d.characters[G.focus]) {
    const c = d.characters[G.focus];
    const partners = Object.entries(pairScenes).filter(([k]) => k.split("|").includes(G.focus)).map(([k, ss]) => [k.split("|").find(x => x !== G.focus), ss.length]).sort((x, y) => y[1] - x[1]);
    return `<strong>${esc(c.zh)}</strong> <span class="ja muted">${esc(c.ja)}</span><div class="muted small">${cnt[G.focus] || 0} 個場景</div>
      <div style="margin-top:8px">${partners.map(([o, n]) => `<div>${esc(nm(o))} <span class="muted small">${n} 個場景</span></div>`).join("") || '<div class="muted">—</div>'}</div>
      <p><a href="#" data-profile="${esc(G.focus)}">看角色檔案</a></p>`;
  }
  return `<div class="muted">點人物：只顯示他和他的連線。<br>點兩人之間的線：看兩人的關係如何變化（附原文與譯文）、怎麼互相稱呼、相關事實、一起出場的場景。</div>`;
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
  // the program's own words for where a rendering came from, in plain Chinese
  const CAT = {character: "人物", place: "地名", item: "物品", term: "名詞", organization: "組織", group: "群體", title: "稱號", skill: "技能", creature: "生物", event: "事件"};
  const ORIGIN = {user: "你設定的", human: "你設定的", naming: "定名", memory_update: "讀的時候記下", scene_analysis: "讀的時候記下",
    scene_analyze: "讀的時候記下", glossary: "名詞表", series: "前一集"};
  const draw = q => { $("#gt").innerHTML = `<tr><th>日文</th><th>譯名</th><th>類別</th><th>來源</th></tr>` + g.filter(x => !q || x.ja.includes(q) || x.zh.includes(q))
    .map(x => `<tr><td class="ja">${esc(x.ja)}${readingsTag(x.readings)}</td><td>${esc(x.zh)}</td><td class="muted small">${esc(CAT[x.category] || x.category)}</td>
      <td>${x.verified ? chip("c-ok", "已確認") : chip("c-unk", ORIGIN[x.origin] || x.origin)}</td></tr>`).join(""); };
  draw(""); $("#gq").oninput = e => draw(e.target.value.trim());
}

route();

// How much of the user's own plan this book used (each plan differs, so a share of the window, not an amount).
const PLAN_NAMES = {claude: "Claude 方案", codex: "Codex（ChatGPT 方案）"};
const WINDOW_NAMES = {weekly: "每週", "5h": "5 小時", daily: "每日"};
function planStat(pu) {
  const entries = Object.entries(pu || {});
  if (!entries.length) return "";
  const pct = v => `${(v.used * 100).toFixed(1)}%`;
  let main = "";
  const lines = entries.map(([src, p]) => {
    const ws = Object.entries(p.windows || {});
    if (!main && ws.length) main = `${pct(ws[ws.length - 1][1])}`;
    const t = p.tokens || {};
    const shares = ws.map(([w, v]) => `${WINDOW_NAMES[w] || w} ${pct(v)}${v.resets ? `（中途重置 ${v.resets} 次）` : ""}`).join("、") || "沒有讀到額度";
    return `<div class="small">${PLAN_NAMES[src] || src}：${shares}</div><div class="small muted">${p.requests} 個請求；輸入 ${(t.input || 0).toLocaleString()}（快取 ${(t.cache_read || 0).toLocaleString()}）、輸出 ${(t.output || 0).toLocaleString()} token</div>`;
  });
  return `<div class="card stat"><span class="muted small">用掉的方案額度</span><b>${main || "—"}</b>${lines.join("")}<span class="small muted">你自己方案的百分比；同一時段帳號的其他用量也算在內。</span></div>`;
}
