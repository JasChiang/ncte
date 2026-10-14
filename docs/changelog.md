# NCTE 開發紀錄（changelog / 交接紀錄）

> 每個 Milestone 完成時更新：已完成功能、測試結果、可重現指令、未完成項目與已知限制。
> 不同 Coding Agent 接手時，請先讀本檔與 `docs/decisions.md`。

環境：macOS（Apple Silicon）、Python 3.14.7（專案要求 ≥ 3.12）、SQLite 3.53（含 FTS5）、
EPUBCheck 5.4.0（Homebrew OpenJDK 25；jar 放在 `workspace/tools/`，不進 git）。

---

## M0–M2（2026-10-13）— 骨架、EPUB 安全解析／重建、Baseline 翻譯

### 已完成

**M0 專案骨架**
- `pyproject.toml`（hatchling；`pip install -e '.[dev]'`；console script `ncte`），ruff、mypy（pydantic plugin）設定。
- `ncte --version`、`--help`；全部子命令都有 `--help`。
- 設定載入 `config.py`：Pydantic v2，未知鍵直接報錯（避免把 api_key 寫進 YAML）；`config.example.yaml`、`.env.example`、`.gitignore`。
- API Key 只來自 `OPENROUTER_API_KEY` 或 `./.env`；錯誤訊息一律經 `redact()`。
- SQLite（WAL）＋版本化、只增不刪的 migrations（`memory/db.py`）。
- FakeLLM（`llm/fake.py`）：離線、可決定、可注入故障（429／timeout／402／壞 JSON／code fence／缺 ID／多 ID／重複 ID／壞 placeholder／空譯文／未翻譯）。
- 自製範例 EPUB 產生器 `scripts/make_sample_epub.py`（全部文本為本專案自行撰寫）。

**M1 EPUB 安全解析與原樣重建**
- `epub/security.py`：Zip Slip（絕對路徑、`..`、反斜線、磁碟代號、NUL）、Zip Bomb（項目數、單檔／總展開大小在**實際解壓時**計算、壓縮率）、DRM（加密 ZIP 項目、非字型混淆的 `encryption.xml`）、XXE（拒絕 `<!ENTITY>`、`resolve_entities=False`、`no_network`）。
- `epub/parser.py`：經 `container.xml` 找 OPF；manifest／spine／NAV／NCX；fixed-layout 明確拒絕。
- `epub/units.py`：翻譯單元抽取＋受保護 placeholder（`⟦n⟧…⟦/n⟧`、`⟦n/⟧`），ruby 平坦化成 `漢字《かな》`（輸出不套用日文 ruby），複雜 ruby／不支援行內元素／`pre` 保留原文並列入報告；mixed block（例如巢狀目錄 `<li><a/><ol/></li>`）以 inline-run 為單位處理；穩定 xpath 與 `unit_id`。
- `epub/reassemble.py`：只改寫有譯文的 XHTML／NAV／NCX／OPF，其餘檔案 byte-for-byte 原順序複製；`mimetype` 第一個且不壓縮；`xml:lang`／`dc:language` 改為 zh-TW；拒絕覆蓋原始 EPUB；`source_hash` 不符時不套用。
- `epub/validate.py`：mimetype、package、XHTML well-formed、內部連結與錨點、NCX、與原檔比較（資源遺失、非文字資源被改、spine 順序、圖片數、單元數）＋ EPUBCheck（可用時）。

**M2 Baseline 翻譯**
- OpenRouter adapter（`llm/openrouter.py`）：非串流；模型支援 `structured_outputs` 時用 `response_format=json_schema, strict=true` 並加 `provider.require_parameters=true`；不支援時明確降級為 `json_object`／prompt-only＋本機 Pydantic 驗證，並寫入 run notes 與每筆 request 的 `structured_mode`；400 拒絕 response_format 時也會降級並記錄。
- 重試：408/429/5xx/524/529/網路錯誤以 exponential backoff＋jitter（尊重 Retry-After）；401/402/403/404 立即停止整個 run；逾時標記 `possibly_billed`。
- 模型與費率：每次 run 開始呼叫 `GET /models` 驗證模型存在並取得價格（存快照）；找不到模型即停止，不會靜默換模型；只有 `fallback_models` 有設定時才切換並記錄。
- 費用：優先使用回應 `usage.cost`（`cost_status=reported`）；沒有時以 tokens×官方價格估算（`estimated_from_tokens`），`ncte report --reconcile` 以 `GET /generation` 的 `total_cost` 回填；逾時／中斷的請求記為 `unknown` 並把估計值計入花費（不當作 0）。
- 預算：送出每批前以「已花費（含未知帳單估計）＋下一批保守估計」預檢，超過即停止為 `budget_exhausted`，可 `ncte resume --run-id … --budget-usd …`。
- 翻譯器（`translation/translator.py`）：嚴格 JSON 解析 → 唯一本機修復是去掉一層 Markdown code fence → Pydantic 驗證 → unit_id 集合完全相等檢查 → 逐單元程式 QA；失敗時帶錯誤說明重試一次（只重送失敗單元）→ 對半切批 → 單一單元仍失敗則標記 failed、保留日文原文、寫入 QA。譯文絕不會被配到其他單元。
- 程式 QA（`qa/rules.py`）：空譯、placeholder、未翻譯、假名殘留、ruby 標記外洩、長度比、引號數、數字、簡體／日文新字體字形、術語表一致性、疑似憑空加入人名、批內重複譯文。
- 工作狀態機（`jobs`：pending/running/succeeded/failed/needs_review/skipped）；每批一個交易；Ctrl+C 後 run=`interrupted`，in-flight 請求標為帳單未知；`resume` 把 running 與 attempts<3 的 failed 重新排入。
- 快取：`source_hash`＋prompt 版本（含檔案 hash）＋模型＋模式＋「與該單元相關的術語」hash 全相同才跳過。
- 目錄／書名：與章節標題相同者直接沿用章節標題譯文（0 成本），其餘以專門批次翻譯並附已譯章節標題。
- `--dry-run`：查詢公開 `/models` 價格（不需金鑰），列出單元數、批數、token 與費用區間，**不呼叫付費端點**。
- 報告：`ncte report --format html|json`（費用依 stage／model／帳單狀態、token、QA 發現、待審、保留原文清單、usd_per_10k_ja_chars、units_per_minute、retries_per_100_units）；`ncte export` 另輸出 JSON 匯出報告。
- CLI：`import`、`inspect`、`models`、`translate`（`--mode --model --budget-usd --dry-run --limit-units --unit-id --force --fake`）、`status`、`resume`、`export`、`validate`、`report`、`retranslate`。

### 測試結果
```
$ .venv/bin/pytest
105 passed in 9.8s
$ .venv/bin/ruff check src tests   → All checks passed!
$ .venv/bin/mypy src               → Success: no issues found in 39 source files
```
- M1 驗收：3 個自製 fixture（一般章節／圖片＋超連結＋外字圖＋插圖頁／ruby＋註腳＋NCX）：無翻譯重建 byte-identical；經 DOM 的 identity 重建後重新抽取文字完全相同、spine 與圖片 byte 相同、內部連結 0 斷裂；**來源與輸出皆通過 EPUBCheck 5.4.0（0 error / 0 warning）**；5 個惡意 fixture（zip slip、zip bomb、XXE、DRM、fixed-layout）全部被拒絕。
- M2 驗收（FakeLLM）：三本 fixture 端到端翻譯＋匯出＋驗證；中斷後續譯不重複翻譯、in-flight 請求記為帳單未知；預算停止後提高預算續跑；402 致命錯誤保留已提交成果並可續跑；單一單元失敗時匯出保留原文並列入報告；模型不存在即停止；fallback 只在設定時使用；只重翻指定單元不影響其他譯文；不支援 structured outputs 時明確降級。
- 線上檢查：`GET https://openrouter.ai/api/v1/models`（公開、不需金鑰）確認 `google/gemini-3.8-flash` 存在，當下價格 US$0.75／M input、US$3.75／M output，支援 `structured_outputs`（2026-10-13 實測）。

### 未完成／無法驗證
- **真實 OpenRouter 翻譯 smoke test 尚未執行**：本環境沒有 `OPENROUTER_API_KEY`，且使用者未授權付費呼叫。程式路徑已用 `httpx.MockTransport` 測試，但真實回應格式（特別是 `usage.cost`、`provider` 欄位）尚未經實際請求驗證。執行方式見 README「真實 smoke test」。
- 只支援 reflowable EPUB 2／3；fixed-layout、DRM 會被拒絕。
- 不支援的行內結構（SVG/MathML 在段落內、複雜 ruby、`pre`、mixed block 中的不支援結構）保留原文並列入報告，不嘗試翻譯。

### 可重現指令
```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'
pytest
python scripts/make_sample_epub.py
ncte import tests/fixtures/basic.epub
ncte translate tests/fixtures/basic.epub --dry-run
ncte translate tests/fixtures/basic.epub --fake        # 離線假翻譯，只驗證流程
ncte export tests/fixtures/basic.epub
ncte report --book tests/fixtures/basic.epub --format html
```

---

## M3（2026-10-13）— 場景、角色及劇情記憶

### 已完成
- Migration 2（只增不刪）：`characters`、`utterances`、`speaker_hypotheses`、`speech_examples`、`facts`、`chapter_summaries`、`prop_states`、`memory_rejections`、`memory_fts`（FTS5 trigram）。既有資料庫自動升級並回填 FTS。
- 場景切分（確定性）：章節檔案、裝飾分隔線（＊◇◆…）、章節標題、≥2 空行、場景上限；`ncte scenes --split/--merge/--clear` 手動覆寫。
- `--mode context`（`translation/context_pipeline.py`）：每個場景依序 `scene_analyze` → 場景內逐批翻譯 → `memory_update`；每步獨立交易、獨立 job，可中斷續跑。baseline 模式保持不變供 A/B 比較。
- 場景分析驗證（`analysis/scene.py`）：新角色的 `name_ja` 必須逐字出現在證據單元，否則拒絕（防止模型用外部知識/後文幻覺出人名）；別名必須在場景中「獨立出現」（`カイ` 只出現在 `カイト` 裡面 → 拒絕）；所有角色 ID、證據單元 ID 做參照完整性檢查；被拒絕的主張寫入 `memory_rejections`。
- 記憶更新驗證（`analysis/memory_update.py`）：事實必須附場景內的證據單元與**逐字存在**的 `evidence_quote`（忽略空白與 ruby 標記）；`inferred` 不會被升級；`fact_type` 區分客觀／角色認知／敘述者說法；`first_revealed_unit_id` 取證據中最早的單元；撤回（retraction）只能針對更早的事實。術語候選必須逐字出現才收錄，且**不覆寫**既有／人工術語。
- 可見性（`memory/visibility.py`）：翻譯某場景時只提供「該場景開始前」揭露的事實、摘要、物件狀態、口癖例句；角色只在首次出現後可見；撤回只有在撤回本身已揭露時才生效。FTS 查詢同樣以 horizon 過濾（≥3 字 trigram，2 字以下 LIKE）。
- Context builder（`memory/retrieval.py`）：在場角色（譯名、別名、口癖統計、已驗證例句＋其譯文）、本批對話的說話者狀態（explicit/inferred/unknown）、省略主語候選、前章摘要（最後 2 段）、本章已發生摘要、相關事實（上限 `max_retrieved_evidence`）、物件持有者；記憶區塊有字元上限。
- CLI：`ncte scenes`、`ncte characters`、`ncte memory --search/--at/--facts`、`ncte glossary list|set|impact`。

### 測試結果（M3 新增）
`tests/integration/test_memory.py`（7）、`tests/integration/test_cli_memory.py`（2）：使用會「犯錯」的 scripted LLM（幻覺人名、偽造證據、把推測標成確定、無效 ID）。
- 明確角色譯名／事件被記住，查詢回傳原文位置（`first_revealed_unit_id`）。
- **防劇透**：整本（含第三章「フードの少女の正体は王女リリア」）處理完後，再以 context 模式重翻第一章單元，送出的請求中不含「リリア／莉莉亞／王女／真實身分」，事實清單為空；FTS 以 horizon 查詢也查不到。
- 偽造證據的事實被拒絕；`inferred` 事實維持 `inferred`；記憶步驟失敗（壞 JSON）時譯文全部保留；場景分析失敗時退回程式分析；context 模式中斷後可續跑。
- 修改術語後只有含該詞的單元被重新翻譯（其餘 `skipped`），`glossary impact` 列出受影響單元。

---

## M4（2026-10-13）— 省略主語／賓語與說話者序列分析

### 已完成
- 對話偵測（`analysis/dialogue.py`）：「」含巢狀『』、一段多句、跨段落未閉合引號（兩種排版慣例：續段無「、續段重開「）以 `continuation_of` 串接。
- 程式證據：引號後「と＋名字＋說話動詞」、引號段落後緊接「名字は…言った／呟いた…」→ `dialogue_tag`；引號前「名字が口を開いた。」→ `dialogue_tag`（排除其實是上一句後置標記的情況）；引號段落後緊接「名字は＋動作」→ `action`（只算推測）。
- 說話者裁定（`analysis/speaker.py`）：`explicit` 只能來自程式可驗證的發話標記、明確引號的續段或人工；LLM 單獨主張 explicit 一律降為 `inferred` 並註記；只有弱證據（自稱、句尾、口癖、敬語、節奏）最多 `inferred`；弱證據與動作證據衝突時以動作證據為準（防模仿口吻）；兩個強證據衝突 → `unknown`；無證據 → `unknown, speaker_id=NULL`。不以「輪流發言」為證據。
- 口癖資料庫防自我汙染：`speech_examples` 只收 `explicit` 或 `human_verified`（DB CHECK 約束），口癖統計（自稱、對稱、句尾、禮貌度）只由這些例句計算；重新分析時會移除不再 explicit 的例句。
- 省略主語／賓語：LLM 的動作候選經參照驗證，只有當候選人名字面出現在該單元時才可為 `explicit`；以「候選」形式提供給翻譯，prompt 要求缺證據時保留中文省略。
- 人工確認／修正：`ncte speakers`（列出狀態、候選、證據、註記）、`ncte speaker-set --utterance-id … --character …|--unknown`：狀態 `human_verified`（之後自動分析不會覆寫）、續段一併更新、例句庫同步、受影響單元產生 `speaker_corrected` QA 發現並提示重翻指令。

### 測試結果（M4 新增）
`tests/unit/test_dialogue.py`（14）、`tests/integration/test_speakers.py`（4）。在故事 fixture 上：
| 測試類型 | 原文 | 結果 |
|---|---|---|
| 已知發話者 | 「カイト、遅いですよ」＋ミナは腕を組んで言った | ミナ / explicit |
| 動作敘述 | 「悪い悪い…」＋カイトは頭をかいた | カイト / inferred |
| LLM 誤標 explicit（只有句尾證據） | 「本当だろうな」 | カイト / inferred（降級） |
| 模仿口吻 | 「ふふん、俺様に任せろ」＋ミナはカイトの口調を真似て | ミナ / inferred（「俺様」弱證據被動作證據覆蓋） |
| 未知發話者 | 「待って」（模型給了不存在的 ID） | — / unknown（ID 被拒絕） |
| 一人連續說兩段 | 「似てますよ。／それに…」 | 續段串接、同一狀態 |
| 明確標記 | 「お、王女様だったのかよ……」＋カイトは呆然と呟いた | カイト / explicit |
- 只有 explicit 例句進入口癖庫（「俺様…」「本当だろうな」都不在）；人工修正後重跑自動分析，人工結果保留。

```
$ pytest            → 132 passed
$ ruff check src tests → All checks passed!
$ mypy src          → Success: no issues found in 46 source files
```

### 未完成／限制
- 發話標記的規則以常見句型為主（と＋名字＋說話動詞、名字は…言った）；更隱晦的標記由 LLM 提出、只會成為 `inferred`。這是刻意偏保守的取捨：explicit 較少，但不會被模型誤判汙染。
- 角色合併（同一人不同稱呼，例如「フードの少女」與「リリア」）不自動進行，以免提前劇透；身分揭露以事實（fact）記錄，從揭露點之後可見。
- 真實模型下的分析品質尚未實測（無金鑰）。

---

## M5（2026-10-13）— 品質控制、選擇性校對與報告

### 已完成
- 程式 QA（每個譯文都跑，見 M2）在 M5 擴充為「嚴重度 → 動作」：`error` 阻擋（重試／保留原文）、`high` → 譯文狀態 `needs_review` 並觸發語意複查、其餘只標記。
- 選擇性語意複查（`translation/review.py`）：`review.automatic_review = off | flagged_only | all`。`flagged_only` 只送出有 high 等級 QA 發現、說話者證據衝突、或譯者標記 `possible_source_error` 的單元。複查模型只回傳「證據＋問題類型＋建議」，證據為空或 unit_id 不在本批的意見會被拒絕並記錄。`needs_retranslation` 且 medium/high 的單元帶著審稿意見（`review_feedback`）**重譯一次**，再跑程式 QA；通過 → `succeeded` 並解除原發現；仍有 high → 保持 `needs_review`。每個單元每次 run 最多一輪（`jobs.stage='review'`）。
  - baseline：所有批次完成後複查；context：每個場景翻完後複查（在下一場景分析之前）。
  - `openrouter.review_model` 可指定不同複查模型。
- 人工校正：`ncte edit --unit-id … --text …`：placeholder 不完整一律拒絕（`--force` 也不行）、其他 QA error 需 `--force`；寫入 `human_edits` 與狀態 `human` 的譯文；匯出時人工譯文永遠優先，之後機器重跑不會覆蓋。`ncte unit BOOK UNIT_ID` 顯示原文、placeholder、所有譯文版本、發現、說話者。
- `ncte review BOOK --scope flagged|all`：對既有譯文跑一次複查（獨立 run、預算控制）。
- 報告：費用依 stage（translate / scene_analyze / memory_update / review）分列；HTML 含待審清單、發現、保留原文清單。
- 角色譯名修改定位：`ncte glossary set` 立即列出含該詞的單元及其譯文是否已用新譯名；快取只讓這些單元失效（見 M3 測試）。

### 測試結果（M5 新增）
`tests/integration/test_quality.py`（8）：
| 人為注入 | 系統行為 |
|---|---|
| 漏譯（整段只剩「早上。」） | `too_short`(high) → `needs_review` → 複查 → 帶意見重譯 → 修正為 `succeeded` |
| 錯誤人名（米娜→米奈，術語已確認） | `glossary_mismatch`(high) |
| 憑空加說話者（「米娜說：」但原文無ミナ） | `possible_added_name`(high)；原文有該人名時不誤報 |
| 重複段落（兩段不同原文同一譯文） | `duplicate_translation`(high) |
| 壞 placeholder（持續） | 阻擋 → 切批重試 → 單元 failed、匯出保留日文原文與 `<em>` |
| 錯誤 speaker ID（不存在的角色） | 參照完整性拒絕（M4 測試） |
- 複查只送出被標記的單元（數量 < 全部單元）、乾淨輸出不產生複查費用、`all` 模式送出全部本文單元、每單元只複查一次、不在批次中的審稿意見被拒絕。
- 人工譯文：缺 placeholder 被拒（含 --force）、未翻譯被拒、合法修改在機器重跑後仍保留並出現在匯出 EPUB。

```
$ pytest → 140 passed;  ruff → All checks passed!;  mypy → no issues (48 files)
```

### 限制
- 複查品質取決於模型；本專案只保證流程（選擇、上限、證據檢查、重譯後再 QA）。真實模型尚未實測。
- 人工譯文優先於機器譯文；若要放棄人工譯文，目前需直接再 `ncte edit` 覆寫（尚無 revert 指令）。

---

## M6（2026-10-13）— A/B 評測與效能調整

### 已完成
- 人工標註評測集 `src/ncte/eval/data/ncte_eval_v1.json`（全部自行撰寫）：10 個項目對應規格 §11 的測試類型（兩人／多人無標記對話、一人連續發言、只有口癖、模仿口吻、主語賓語省略、人稱敬語、後文才揭露身分、物件跨段移動、ruby/斜體/連結/註腳），23 句標註說話者（含 `null`＝正解為未知）、15 個標註單元（禁止出現的添加詞、必須包含的內容）、1 組劇透限制。格式以 Pydantic 驗證（`ncte eval check`）。
- 實驗（`ncte eval run`，每個實驗用全新 workspace，不共用快取／記憶）：A 純段落（每單元一請求、無上下文）、B ＋鄰近上下文、C ＋場景與人物／劇情記憶、D ＋LLM 對話與省略分析＋選擇性複查；`--repeat N` 量測波動（mean/min/max/stdev）；`--model` 可換模型；`--dry-run` 只估價。
- 指標全部以人工標註程式計算（**不使用 LLM 主觀評分**）：`speaker_accuracy_explicit`、`speaker_explicit_precision`、`unknown_asserted_rate`、`weak_evidence_asserted`、`unknown_false_assertion_rate`（譯文層）、`omitted_argument_error_rate`、`omission_rate`、`omission_addition_rate`、`entity_terminology_consistency`、`spoiler_violations`、`epub_structure_integrity`、`total_cost_usd`、`usd_per_10k_ja_chars`、`units_per_minute`、`retries_per_100_units`、tokens、requests。輸出 `results.json`、`results.csv`、`report.html`。
- 修正（由評測發現）：
  1. 「narration before quote」規則誤把「「…」とミナは言った。」段落當成下一句的前置標記 → 錯誤的 explicit。現在只採用純敘述段落（新增回歸測試）。修正後離線評測 explicit precision = 1.0。
  2. baseline 模式的後文（`context_after`）原本可能跨越場景邊界，把下一場景的揭露送進請求；現在所有模式都限制在目前場景（`lookahead_scope`，可設 `none` 關閉）（新增回歸測試）。
- 效能／成本調整點（皆可設定）：`max_concurrency`（baseline 併發）、`unit_target_japanese_chars`（批次大小）、`include_previous_units`／`include_next_units`、`review.automatic_review`、`context.scene_analysis`／`memory_update`／`include_memory`、`review_model`。

### 測試結果
```
$ pytest            → 146 passed
$ ruff check src tests → All checks passed!
$ mypy src          → Success: no issues found in 51 source files
```
離線評測（FakeLLM＋只提出原文中逐字出現人名的分析器，`tests/eval/test_eval.py`）：
- 實驗 D：explicit 精確率 1.0、正解為未知的句子被斷言為 explicit 的比例 0、只有口癖的句子沒有被升為 explicit；無標記句子誠實地保留 unknown（因此 speaker_accuracy 約 0.3–1.0 之間，取決於分析模型）。
- 所有實驗 EPUB 結構完整性 1.0、劇透 0；注入的性別代名詞「她」被 `omitted_argument_error_rate` 量到；A 的請求數多於 B（每單元一請求）。
- **FakeLLM 下的翻譯品質指標沒有意義**（報告中會明確警告）。真實模型的 A/B 數據尚未產生（無金鑰）。

### 執行真實 A/B（需要金鑰）
```bash
ncte eval run --dry-run                       # 估價（不呼叫付費端點）
ncte eval run --experiments A,B,C,D --repeat 2 --budget-usd 0.5 --epubcheck
open workspace/eval/<timestamp>/report.html
```

---

## 目前整體狀態（交接摘要）
- M0–M6 皆已實作並有離線自動化測試；所有指令都有 `--help`。
- **尚未完成的實測**：真實 OpenRouter 翻譯 smoke test、真實模型的 A/B 評測（環境無 `OPENROUTER_API_KEY`，且未獲付費授權）。
- 建議下一步：(1) 使用者提供金鑰後執行 README「真實 smoke test」並把實際 `usage.cost`／generation 回應格式記錄在此；(2) 以真實模型跑 `ncte eval run`，依 baseline 數據訂定語意品質門檻；(3) 視需要加入 OpenRouter `:batch` 變體（ADR-008）。

---

## 實書付費試翻（2026-10-13）— 第一次真實 OpenRouter 呼叫

- 書：使用者提供的日文 EPUB 2（Kobo 格式，3,280 段、約 11.6 萬字）。`--mode context --limit-units 40 --budget-usd 0.30`。
- 結果：18 次請求全部成功，`json_schema` strict 正常，`usage.cost` 有回報（`cost_status=reported`），provider = Google AI Studio，generation id 格式 `gen-…`。40 段花費 **US$0.1529**。
- 發現與修正：
  1. **Kobo `kobospan` 包在 ruby 底字外** → 251 段被當成複雜 ruby 保留日文。修正：ruby 內純文字行內包裝視為底字（ADR-016），保留原文段落降為 0。
  2. **有 ruby 的人名比對不到術語表**（原文存成 `彩《いろ》葉《は》`）→ 術語沒送給模型、QA 誤報「憑空加人名」。修正：比對前移除讀音（`match_text`），FTS 以去讀音文字重建（migration 3）。
  3. **第一人稱「私」被收為角色別名**。修正：代名詞永遠不能當角色名或別名。
  4. **推理 token 佔翻譯輸出的 77%**（`gemini-3.8-flash` 的 reasoning 為強制開啟、預設 medium，可選 low/medium/high），以輸出價計費；原本的粗估因此低估約 3–4 倍。新增 `openrouter.reasoning_effort` / `--reasoning-effort`（依 `/models` 的 `supported_efforts` 驗證，每筆請求記錄），以及 dry-run 的「以實際花費校正」估計（同模型、同推理強度、各 stage 每字成本）。
- 測試：153 passed；ruff、mypy 通過。
- 整本 context／low 第一次執行（run `r_20261013094229_764569`）：翻到第 54／83 場景、1,938 段、US$2.41 時，因 Gemini 內容審查誤判（403 PROHIBITED_CONTENT）停止。修正見 ADR-017：內容審查改為只影響被拒段落（切批隔離），不停止 run；新增 2 個整合測試與 2 個 adapter 測試（158 passed）。
- 續跑完成：3,280／3,280 段全部翻譯並套用（含 2 次 resume），本書所有付費請求合計 375 次、**US$4.08**（全部為 API 回報費用；含兩次試翻 US$0.26）。輸出 EPUB：EPUBCheck 24 個錯誤**全部為原書既有**（原書 825 個，多為 EPUB 2 不允許 ruby；輸出無新增錯誤），內部驗證、連結、圖片全部通過。
- 實書再發現並修正 2 個 QA 誤報（`ncte recheck` 重新檢查既有譯文，不呼叫 API）：
  1. 「・」「ー」被當成假名 → 逐音節唱出的歌詞（「〇・〇・〇…」）的正確譯文被拒。
  2. 「憑空加人名」誤報：同譯名的多個寫法（本名／愛稱）、角色別名、普通名詞型角色（赤ちゃん→嬰兒、先生→老師）。待審從 108 段降為 5 段，其中 3 段是真的在原文省略主語處補上人名（應人工確認），2 段是角色名的譯名與普通詞（如「真實」）撞字。
- 測試：160 passed；ruff、mypy 通過。
- 愛稱統一：以 `glossary set` 把某角色的愛稱統一為模型自己用過的暱稱譯法，只重翻 5 段（US$0.012）。記憶整理 prompt 新增「愛稱不要用本名譯名」「不收普通名詞／單字」規則。
- ADR-018：自動譯名候選不再讓舊譯文過期（原本會誤重翻 185 段）；清除 18 個普通名詞譯名（含錯誤的「男→CIA男子」，經查未造成誤譯）；審稿確認無誤即清除程式標記（7 段）。審稿另抓到 1 個真錯誤：第一人稱敘述被譯成第三人稱（補上主角名字），待人工決定。全書累計 US$4.11。163 passed。
- 方言：模型把母親的關西腔回憶台詞（3 段）刻意譯成粵語（譯者附註「母の関西弁口調を反映」）。新增 QA 規則 `cantonese`（error：粵語用字→退回並要求臺灣國語，語氣詞「唔」除外）及「円」列為非臺灣字形；以 `--unit-id … --force` 重翻 3 段（US$0.008）。未修改翻譯 prompt，避免整本快取失效。164 passed。
- 實體卡（ADR-019）：migration 5（entities、entity_links、glossary.entity_id/form_type/form_note），`ncte names list|link|unlink|detect`，記憶整理新增 `name_links`，翻譯請求附同實體其他稱呼，QA 新增 `name_form_substitution`、`offensive_term`；`recheck` 尊重審稿已確認的結果。實書：自動偵測到 2 組「A。通称B」句型（一個團體、一個地點）；手動連結角色的本名／全名／愛稱與隊名的三種寫法。新檢查標出 19 段把團體通稱「黒鬼」直譯成中文蔑稱「黑鬼」，以及 1 段把正式名稱換成通稱；改用使用者選定的譯名後重翻 18 段。174 passed。

---

## 系列支援（2026-10-13，ADR-020）
- migration 6（books.series_id / volume_index / volume_label、series、library_books）；`ncte/series.py`（故事位置、系列範圍 SQL）、`ncte/workspace.py`（library.db＋每系列資料夾，自動搬移舊工作區、移動原書副本）。
- 所有知識查詢改為系列範圍；可見性改用（集數, 段落）比較。`ncte import --series --volume --label`、`ncte series list`、`ncte series set-volume`；`resume`／`report --run-id` 會在各系列資料庫中尋找 run。
- 新增 4 個整合測試：跨集可見性、先翻第 2 集再翻第 1 集不劇透但共用譯名、第 2 集看得到第 1 集的記憶、CLI 系列資料夾與舊工作區搬移。179 passed；ruff、mypy 通過。
- 實際工作區已搬移：`workspace/series/<書名>/`（第 1 集），3,280 段譯文全部保留（dry-run：0 段需要重翻）；備份 `workspace/ncte.before-series.db`、`ncte.legacy-*.db`。

## 名詞索引、人物關係圖、搬書、角色同步（2026-10-13）
- `ncte index`（名詞索引：每個實體的所有稱呼、譯名、首次出現位置、出現段數、口癖摘要）與 `ncte relations`（人物關係圖：有原文證據的事實＋同場出現；HTML 內嵌 SVG、Markdown Mermaid、JSON）；兩者都支援 `--volume N`／`--upto UNIT` 產生不劇透版本。實書：95 個條目、47 個角色、75 條有證據的關係。
- `ncte series move BOOK --to SERIES`：連同譯文、執行紀錄、記憶搬到另一系列，重複的譯名與角色合併（人工確認者優先），空的來源系列資料夾改名保留。
- ADR-021：連結稱呼時同步合併角色；`ncte names sync`。實書合併 3 筆（全名、愛稱併入本名角色）。
- 測試：183 passed；ruff、mypy 通過。

## 系列與集數偵測（2026-10-13）
- `epub/series_meta.py`：依序檢查 EPUB 3 `belongs-to-collection`／`group-position`（高）、Calibre `calibre:series`／`series_index`（高）、書名格式（中：「〇〇 2」「〇〇（3）」「第4巻」「Vol.5」「Ⅱ」「③」「上／下」「〇〇 2 ―副題―」「2.5」）、Calibre `title_sort` 結尾數字（低）。並比對書庫中已有的系列（正規化書名）。
- `ncte import` 不指定 `--series` 時照舊自成系列，但會顯示偵測結果與加入指令；`--series auto` 直接套用；`ncte inspect` 顯示 `series_suggestion`。
- 測試：23 個（書名格式、反例、兩種 metadata、CLI 建議與 auto 加入）；206 passed。
- 實書（兩集的電撃文庫系列）：書名「〇〇 (電撃文庫)」「〇〇2 (電撃文庫)」偵測失敗 → 新增去除書名後的文庫／版本標註（`clean_title`）與「集數緊接在日文書名後」的規則；新系列名稱也去除標註。第 2 集以 `--series auto` 正確加入第 1 集的系列。
- 試算的「以實際花費校正」改為參考書庫中所有系列的歷史請求（每個系列一個資料庫後，新系列原本沒有校正資料）。212 passed。
- 實書（第 1 集，context／low，US$2.96，2,639 段全數翻譯）：Google AI Studio 偶發回傳 HTTP 400「User location is not supported for the API use」（FAILED_PRECONDITION），導致 1 個場景分析與 3 段審稿未完成。這是上游路由的暫時問題，改為以 backoff 自動重試（`is_transient_rejection`）。213 passed。
- 第 1 集匯出：EPUBCheck 30 個錯誤全部是原檔既有（原檔另有 5,357 個 EPUB 2 不允許的 ruby 錯誤，譯文平坦化 ruby 後消失）。
- 第 2 集在 57/66 場景時金鑰達到 US$10 上限（剩 US$0.24）而以 402 停止：未設定輸出上限時 OpenRouter 依模型最大輸出（65,536 tokens）預扣額度。新增 `auto_max_tokens`：設定檔未指定 `max_output_tokens` 時，以請求內容 4 倍 tokens 為上限（16,384～65,536），被截斷（`finish_reason=length`）時加倍重試；使用者自行設定的上限不變。216 passed。
- 第 2 集（`ncte resume`）完成：US$2.80，2,447 段全數翻譯；1 個場景分析被 Gemini 內容過濾擋下（譯文不受影響，只少了該場景的說話者判定與記憶）。第 1 集補完失敗的場景分析與 3 段審稿（「User location」重試生效）。兩集 EPUBCheck 剩餘錯誤皆為原檔既有。系列合計約 US$5.80。
- 審稿意見落實為人工修正 2 段（叫名字到一半改口的「あだ……」→「安……」；被動句不補人名）。
- QA：口語親屬稱謂（おねえちゃん、姉ちゃん、ママ…）列入 `GENERIC_REFERENTS`，不再當作人名（誤報）；術語比對先去除 placeholder（「株式會社⟦2⟧KADOKAWA⟦/2⟧」）。
- 實體卡：「しまちゃん」是姊妹兩人共用的小名，模型先後連到兩人，`merge_into` 把整張卡一起搬走，結果把妹妹的稱呼併進島村，島村的實體卡也以妹妹的稱呼命名。修正：自動（llm/pattern）連結若會合併兩個各有多個稱呼的實體，只記為待確認候選，由人決定；實體名稱與類型改由正式稱呼決定（`refresh_entity`）；`unlink` 修正實體 id 撞號（實體以該稱呼命名時原本不會真的拆開），並只從原實體的角色移除別名。實書資料已修正（備份 `ncte.before-sisterfix.db`）。
- 人物關係圖：Mermaid 宣告所有被事實邊引用的節點（沒有出場計數的角色原本沒有標籤）。220 passed。

## 稱呼表、共用稱呼、系列翻譯風格（2026-10-14，ADR-022）
- migration 7：`address_terms`（說話者、對象、稱呼含敬稱、譯法與來源、首次／最近使用位置、次數、說話者確定度）、`address_observations`（每句台詞只計一次）、`form_senses`（共用稱呼）、`series.style_json`。
- 稱呼表：程式從說話者已判定的台詞自動整理（最長比對＋敬稱，身分詞＋敬稱如「店員さん」不算）；場景重新分析或人工改說話者時撤回重算；記憶整理模型可回報本場景譯文用的譯法；人工 `ncte names address-set` 不會被覆蓋。翻譯請求附「本批原文出現的稱呼」與「本批說話者對在場角色的慣用稱呼」（只含本場景之前已出現的，最多 12 筆），審稿也會拿到。名詞索引新增「被怎麼稱呼」。
- 共用稱呼：自動連結若會合併兩個已有多個稱呼的實體，改記為共用稱呼（並列為合併候選）；翻譯請求在揭露後附 `may_refer_to`；`ncte names link --shared`。
- 系列翻譯風格：`ncte style show|set`（敬稱 natural/explicit/omit、自訂規則、角色語氣）；預設風格不加入請求；不納入快取鍵。
- 順帶修正：`speaker-set` 只接受同一集的角色（系列中無法指定第 1 集登場的角色）；`style show` 先讀連線才切換到系列資料庫。
- 實書（兩集，已備份 `ncte.before-v7.db`）：整理出 53 組稱呼、301 次使用，例如 安達→島村「しまむら」×74、日野→安達「あだっちー」「あだっつぃー」等 7 種暱稱；「しまちゃん」標為姊妹共用。不需 API 呼叫。
- 測試：231 passed；ruff、mypy 通過。

## 方向一致規則、暫停新增功能（2026-10-14）
- 翻譯提示詞第 3 條新增：人物關係與動作的方向（誰對誰、單向／雙向、主動／被動）要與原文一致，不為通順改用常見說法；審稿的 `subject_object_error` 涵蓋方向被改變。屬同一主版本內的修訂，既有譯文不失效。
- 起因：實書抽查發現「Bと特別でありたい」型的句子被譯成「成為 B 的特別存在」（雙向變單向）。查證：當時的翻譯請求不含可能誤導的摘要（摘要只附前一章最後 2 段、本章最多 4 段），屬模型把罕見句型換成常見說法。
- 決定：在累積多部不同類型作品的錯誤分類之前，不新增記憶種類（關係表、摘要附原句先擱置）；先以 A/B 拿掉各項 context 量測效益。錯誤分類紀錄放在 `workspace/`（含書籍原文，不進 git）。
- 架構文件的快取鍵說明更正為 prompt 主版本。

## 先讀再翻、定名（2026-10-14，ADR-023）
- 起因：與第 1 集繁中正式版對照，稱呼的譯法依說話者而不同（島村媽媽叫「安達ちゃん」→「安達同學」，島村調侃時→「安達妹妹」），但原流程在第一次翻到時就由模型當場定下譯法，當下還不知道人物關係。
- context 模式改為三輪：讀（每個場景分析＋記憶整理，沒有譯文）→ 定名（一次呼叫，對本集出現的名字與稱呼提出建議、選項與理由）→ 翻（逐場景翻譯與審稿）。`context.read_first=false` 恢復舊流程。
- 定名：建議譯法直接套用為預設（不覆蓋人工譯名與人工稱呼；稱呼來源排序 譯名表 < 模型 < 定名 < 人工）；名詞出現 ≥5 次或稱呼使用 ≥3 次且有其他選項、或中文只剩一個字的人名，列為待確認。`context.naming`：`review`（預設，有待確認就停在 `awaiting_names`）／`auto`／`off`。指令：`ncte names proposals|decide|propose`。重翻指定段落（`--unit-id`）不做定名。migration 8：`naming_proposals`。
- 防劇透：記憶在翻譯前就涵蓋整集，所以角色名稱改為依揭露位置顯示（最早出現的稱呼，加上已出現且與角色的連結已揭露的其他稱呼；正式名稱尚未揭露時以當時的稱呼代替），用在翻譯、場景分析與記憶整理的請求。
- 測試：241 passed；ruff、mypy 通過。尚未以真實模型跑過定名。

## 實測定名、書籍資訊不分析、提案附例句（2026-10-14，ADR-024）
- 定名真實執行（第 1 集，gemini-3.8-flash，一次呼叫 US$0.04）：49 個名字、34 個稱呼、9 個待確認。發現兩個既有問題並修正：
  - 第 2 集把安達媽媽建成名為「安達さん」的角色，稱呼整理時把第 1 集同學叫安達的「安達さん」全歸給媽媽，提案因此建議「安達阿姨」。整理稱呼改為只用當時已登場的角色（`name_forms(upto=…)`）。重新整理後再跑：日野／永藤→安達「安達さん」建議「安達同學」，與正式版一致。
  - 插畫家「のん」被當角色、定名計次把「のんびり」算進去。純假名名字改為不在其他假名詞中計次。
- 書籍資訊（ADR-024）：front／story／back 判斷；只含書籍資訊的場景不做分析與記憶整理，定名只算故事本文。實書：三本書的封面、目次、後記、版權頁都分對（包含沒有列在目錄中的後記頁）。
- 提案附原文例句（migration 9，舊提案自動補上）；角色名是稱呼且與另一角色重疊時提醒。
- 測試：246 passed；ruff、mypy 通過。

## 先讀再翻完整實測（2026-10-14，第 1 集，全新工作區）
- 流程：讀 53 個場景（7 個書籍資訊場景跳過）→ 定名 35 名字／32 稱呼、4 項待確認 → 人工決定 → 翻譯 60 個場景＋審稿。2,639 段全數翻譯；US$3.05（讀 1.70、定名 0.04、翻譯與審稿 1.31），與舊流程第 1 集 US$3.00 相近。EPUBCheck 30 個錯誤皆為原檔既有。
- 與舊譯文、繁中正式版三方對照：社對島村的「あなた」舊譯混用你／妳／您，新譯大多改為妳；「きみ」→妳；前次發現的多加「妳」（2033）、用字重複（145）不再出現；同學叫安達的「安達さん」→「安達同學」與正式版一致。餐廳場景 62 句說話者全部正確（舊流程有 1 句 unknown）。
- 仍存在：關係方向（2600）未改善；「しまむら」雙關仍遺失（尚無譯註）；定名把島村媽媽的「安達ちゃん」與日野的「あだっちー」都譯成「小安達」（不同稱呼撞成同一譯法）。
- 實測中修正：代名詞稱呼改為保留並由定名決定譯法（ad07928）；人工／定名決定的稱呼譯法在首次出現的場景就提供（f6eda92；此次 693 段仍為「您」，修正後才生效）。

## 撞譯法提醒、名字決定同步稱呼、譯註、台詞口語（2026-10-14，ADR-025）
- 定名：同一人被不同稱呼叫卻譯成同一個詞（排除本名與同一暱稱的拼寫變體）時列為待確認並註明撞到哪個稱呼；提示詞要求區分。人工決定名字的譯法時，稱呼表中同一稱呼一併採用（人工逐對決定的除外）。
- 譯註（ADR-025）：`translator_note` 欄位、`style --notes inline`、匯出時括號附在段落後、`ncte notes list|drop`。migration 10。
- 翻譯規則：台詞要口語（起因：輝夜姬「黒鬼圧倒的だねえ」→「具壓倒性優勢呢」，與安達的翻譯腔同類，已在兩本書出現）。
- 測試：251 passed；ruff、mypy 通過。

## 模型比較、推理強度 medium、讀的階段找梗（2026-10-14，ADR-026）
- 同一組 189 段（安達 152、輝夜 37）比較 flash low（既有）、flash medium、claude-sonnet-5.5（medium）：medium 修正了 low 的讀錯（安達 2600 關係方向，與正式版一字不差；輝夜 1346「恰好」→打扮）；sonnet 台詞最口語、最能保留留白，但有自己的錯（對女生用「你」、「時々」→常有人、「くどい」→囉嗦），費用約為 medium 兩倍（測試 189 段：medium US$0.33、sonnet US$0.62）。三者都沒抓到「しまむら」雙關。本機預設改為 reasoning_effort medium（`ncte.yaml`，不進 git）。
- 修正：模型不支援 temperature 時不送（sonnet 5.5 在 require_parameters 下原本全部 404）。
- 讀的階段找梗（ADR-026）：記憶整理輸出 `wordplay` → `reader_notes`（migration 11）→ 翻譯請求附提示；`ncte notes found`。
- 測試：253 passed；ruff、mypy 通過。
