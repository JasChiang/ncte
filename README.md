# NCTE — Novel Context Translation Engine

把**有權使用、無 DRM** 的日文 EPUB 輕小說，透過 OpenRouter 上的模型翻成臺灣繁體中文，並輸出可在一般閱讀器開啟的完整 EPUB。macOS 本機 CLI，資料全部存在本機 SQLite。

> 開發狀態與測試紀錄：[`docs/changelog.md`](docs/changelog.md)　決策：[`docs/decisions.md`](docs/decisions.md)　架構：[`docs/architecture.md`](docs/architecture.md)

| Milestone | 狀態 |
|---|---|
| M0 骨架、設定、SQLite、FakeLLM、測試 | ✅ |
| M1 EPUB 安全解析與原樣重建（EPUBCheck 驗證） | ✅ |
| M2 Baseline 翻譯（OpenRouter、成本、預算、斷點續譯） | ✅ 離線測試通過；已用一本真實輕小說（約 3,300 段）完整實測 |
| M3 場景、角色及劇情記憶（`--mode context`、防劇透、FTS5） | ✅ 離線測試通過 |
| M4 說話者序列分析、口癖、人工確認 | ✅ 離線測試通過 |
| M5 選擇性複查、人工校正、報告 | ✅ 離線測試通過 |
| M6 A/B 評測（人工標註集、CSV/HTML 報告） | ✅ 流程可執行；真實模型的評測數據尚未產生 |

之後新增：系列書（多集共用記憶、跨集防劇透）、實體卡（正式名稱／愛稱／通稱）、名詞索引、人物關係圖。

**實測紀錄**：一本約 11.6 萬字的日文輕小說，以 `--mode context --reasoning-effort low` 翻譯，總花費約 US$4（含試翻與修正），輸出 EPUB 沒有新增任何 EPUBCheck 錯誤。過程中發現並修正的問題記錄在 [`docs/changelog.md`](docs/changelog.md)。
**尚未有的數據**：記憶與說話者分析是否確實提升品質，需以真實模型跑 `ncte eval run`（人工標註指標）驗證，目前尚未執行。

## 隱私與權利（請先讀）
- 翻譯時，**所需的原文段落與上下文會送到 OpenRouter**，再由 OpenRouter 路由給第三方模型供應商（例如 Google）。各供應商的資料保留／訓練政策不同，請在 OpenRouter 帳號的 Privacy 設定中確認。目前沒有純本機模型模式。
- 只處理你有權使用的無 DRM EPUB。翻譯輸出不代表你取得再散布或公開分享的權利。
- 原始 EPUB、資料庫、匯出檔都在 `./workspace/`（已列入 `.gitignore`）。API Key 不會寫入 log、報告或錯誤訊息。

## 安裝（macOS，全新環境）
```bash
cd ncte
python3 -m venv .venv            # Python 3.12+
source .venv/bin/activate
pip install -e '.[dev]'
ncte --version
pytest                            # 全部離線測試，不需要 API Key
```

### EPUBCheck（建議，選用）
```bash
brew install openjdk              # 或任何 Java 17+
mkdir -p workspace/tools && cd workspace/tools
curl -LO https://github.com/w3c/epubcheck/releases/download/v5.4.0/epubcheck-5.4.0.zip
unzip epubcheck-5.4.0.zip && cd ../..
```
NCTE 會自動尋找 PATH 上的 `epubcheck`、`EPUBCHECK_JAR` 環境變數或 `workspace/tools/**/epubcheck.jar`；也可在設定檔指定 `export.epubcheck_path`。找不到時只做內部檢查，並標示「尚未經 EPUBCheck 驗證」。

### API Key
```bash
export OPENROUTER_API_KEY=sk-or-v1-...     # 或 cp .env.example .env 後填入
```
`import`、`inspect`、`validate`、`report`、`status`、`--dry-run` 都不需要金鑰。

## 使用流程
```bash
# 1. 匯入並檢查（不呼叫 LLM）
ncte import  ./samples/book.epub
ncte inspect ./samples/book.epub          # 結構、單元數、會保留原文的單元

# 2. 確認模型存在與當下費率（公開 /models API，價格不寫死）
ncte models --refresh
ncte models --refresh --filter gemini

# 3. 無付費試算
ncte translate ./samples/book.epub --mode baseline --dry-run

# 4. 正式翻譯（預算上限，超過即停止，可續跑）
ncte translate ./samples/book.epub --mode baseline --model google/gemini-3.8-flash --budget-usd 5

# 5. 進度、續譯（Ctrl+C 可安全中斷）
ncte status ./samples/book.epub
ncte resume --run-id <run-id>                    # 可加 --budget-usd 10 提高上限

# 6. 匯出、驗證、報告
ncte export   ./samples/book.epub --lang zh-TW   # → workspace/exports/book.zh-TW.epub
ncte validate ./workspace/exports/book.zh-TW.epub --original ./samples/book.epub
ncte report --run-id <run-id> --format html      # → workspace/reports/<book>/run-<id>.html
ncte report --run-id <run-id> --reconcile        # 以 /generation 回填最終費用

# 7. 只重翻部分單元（其他譯文不受影響）
ncte retranslate ./samples/book.epub --unit-id u_xxxxxxxx --budget-usd 0.5
```

### 系列書（多集共用角色、譯名、劇情記憶）
```bash
ncte import 第1集.epub --series "作品名" --volume 1
ncte import 第3集.epub --series "作品名" --volume 3 --label 第3集   # 可以先翻後面的集數
ncte series list
ncte series set-volume 外傳.epub 2.5                               # 調整閱讀順序
ncte import 第4集.epub --series auto                              # 套用自動偵測的系列與集數
```
匯入時會從 EPUB 3 系列欄位（`belongs-to-collection`）、Calibre 欄位（`calibre:series`）、書名（「〇〇 2」「〇〇（3）」「第4巻」「Ⅱ」「上／下」…）偵測系列與集數並提示，但**不會自動加入**（放錯系列會混到角色與譯名）；確認後用 `--series auto` 或 `ncte series move` 加入。`ncte inspect` 也會顯示偵測結果。
每個系列一個資料夾（`workspace/series/<作品名>/`）。第 N 集看得到前面集數的記憶；較早的集數（即使較晚才翻）看不到後面集數才揭露的內容，但會沿用已決定的譯名。

### 稱呼與實體卡（正式名稱、全名、暱稱、通稱…）
```bash
ncte names list 書.epub                     # 每個人物／團體的所有稱呼與譯名、警告
ncte names detect 書.epub                   # 從原文「〜。通称〜」等句型自動連結
ncte names link 書.epub ソラっち --to ソラ --type nickname --zh 小索拉
ncte names link 書.epub しまちゃん --to しまむら妹 --shared   # 兩個人共用的稱呼（不合併，翻譯時依上下文判斷）
ncte names address 書.epub --character 安達                   # 誰怎麼稱呼誰（含敬稱、譯法、使用次數、首次出現）
ncte names address-set 書.epub --speaker 日野 --to 安達 --ja あだっち --zh 小安達
```

### 系列翻譯風格（每一集都適用）
```bash
ncte style show 書.epub
ncte style set 書.epub --honorifics omit --rule "先輩一律譯「學姊」" --voice "安達=話少、句子短、常結巴"
```
敬稱處理有 `natural`（預設，依語氣自然處理）、`explicit`（固定譯出）、`omit`（原則省略）。設定只影響之後的翻譯；既有譯文要重翻才會套用。

### 有上下文的翻譯（劇情記憶、說話者分析）
```bash
ncte translate ./samples/book.epub --mode context --dry-run
ncte translate ./samples/book.epub --mode context --budget-usd 5
ncte scenes     ./samples/book.epub                 # 場景切分；--split/--merge UNIT 手動調整
ncte characters ./samples/book.epub                 # 角色、譯名、口癖統計（只來自已驗證例句）
ncte speakers   ./samples/book.epub --status unknown
ncte speaker-set ./samples/book.epub --utterance-id q_xxx --character c_xxx   # 或 --unknown
ncte memory     ./samples/book.epub --facts --at u_xxx                        # 只看該位置之前已揭露的記憶
ncte memory     ./samples/book.epub --search 銀の鍵
```

**先讀再翻**（預設）：context 模式會先把整集依序讀完（說話者、角色、稱呼、事實、摘要），再請模型為名字與稱呼提出譯法（含其他選項與理由），最後才逐場景翻譯。重要且有不同譯法的項目（例如主要角色名、常用稱呼、中文只剩一個字的人名）會先停下來等你決定：
```bash
ncte names proposals 書.epub                 # 待確認的譯法與選項
ncte names decide 書.epub --id 3 --pick 2    # 選第 2 個選項；或 --zh 自訂譯法；或 --accept-all 全部採用建議
ncte resume --run-id r_xxx                   # 決定後繼續翻譯
ncte names propose 書.epub                   # 已分析過的書（例如要重翻前）單獨產生提案
ncte names proposals 書.epub --all --context   # 所有提案（含已套用的）與原文例句
```
不想暫停可在設定檔設 `context.naming = "auto"`（直接採用建議）或 `"off"`；`context.read_first = false` 恢復逐場景交錯的舊流程。你決定的譯法在同系列之後的集數會沿用，不會再問。待確認的項目會附上幾句原文例句方便判斷。封面、目次、注意事項、後記、版權頁照常翻譯，但不做角色與劇情分析（`context.skip_book_matter`）。翻譯時仍只附上該場景之前已揭露的資訊（例如身分揭露前，角色只以當時的稱呼出現）。

### 品質控制與人工校正
```bash
ncte glossary set    ./samples/book.epub ミナ 米娜 --category character   # 列出受影響單元；只有這些會重翻
ncte glossary impact ./samples/book.epub ミナ
ncte unit ./samples/book.epub u_xxx                  # 原文、placeholder、所有譯文版本、QA 發現
ncte edit ./samples/book.epub --unit-id u_xxx --text "還有，明天⟦1⟧一定⟦/1⟧要早點來。」"
ncte review ./samples/book.epub --scope flagged --budget-usd 0.5
```
自動複查由設定 `review.automatic_review: off | flagged_only | all` 控制（預設只複查被程式 QA 或說話者衝突標記的單元，最多一輪）。

### A/B 評測
```bash
ncte eval check
ncte eval run --dry-run
ncte eval run --experiments A,B,C,D --repeat 2 --budget-usd 0.5     # → workspace/eval/<時間>/report.html
```
說明見 [`docs/evaluation.md`](docs/evaluation.md)。
全域選項：`-w/--workspace DIR`、`-c/--config FILE`（預設自動讀 `./ncte.yaml`；範例見 `config.example.yaml`）。

### 真實 smoke test（需要金鑰、會產生少量費用）
```bash
python scripts/make_sample_epub.py
ncte translate tests/fixtures/basic.epub --dry-run                 # 先看估價（約 US$0.02–0.03）
ncte translate tests/fixtures/basic.epub --limit-units 5 --budget-usd 0.05
ncte report --book tests/fixtures/basic.epub --format json --reconcile
ncte export tests/fixtures/basic.epub
```
`--fake` 會改用離線 FakeLLM：**輸出不是翻譯**，只用來驗證流程。

## 運作方式（摘要）
（context 模式另外：先依序讀完整集——每個場景做說話者／角色分析並以程式驗證、以逐字證據整理記憶——再決定名字與稱呼的譯法，最後逐場景翻譯，翻譯時只帶入「該場景之前已揭露」的記憶，並對被標記的單元做一次複查。詳見 [`docs/architecture.md`](docs/architecture.md)。）

1. **安全匯入**：拒絕 Zip Slip、Zip Bomb、XXE、DRM 加密、fixed-layout；經 `container.xml` 找 OPF。
2. **翻譯單元**：段落／標題等葉節點；行內 `<em>`、`<a>`、`<br/>`、外字圖等轉成 `⟦n⟧` placeholder；ruby 轉成 `漢字《かな》` 作為提示。模型永遠看不到 HTML。
3. **翻譯**：依章節分批（約 1400 日文字），附前後文與術語表；`response_format` JSON Schema（strict）；嚴格驗證 unit_id 一一對應、placeholder 完整、程式 QA。失敗：重試 → 對半切批 → 保留原文並列入報告。
4. **成本**：每次請求記錄 tokens、`usage.cost`、generation id、延遲、重試；送出前預算預檢；中斷時尚未回應的請求記為「帳單未知」並計入估計。
5. **重建**：只替換已驗證的文字節點；圖片、CSS、字型 byte-for-byte 保留；`mimetype` 置首不壓縮；匯出後自動驗證（內部檢查＋EPUBCheck）。

## 已知限制
- 真實模型的 A/B 評測尚未執行；目前只用一本書實測過，其他出版社的 EPUB 格式可能遇到新的狀況。
- 內容審查：雲端模型可能誤擋正常段落；被擋的段落會保留原文並列入報告（可再重送或人工翻譯）。
- Gemini 等推理模型的推理 token 以輸出價計費；可用 `--reasoning-effort low` 降低成本（試算會以實際花費校正）。
- context 模式依場景順序執行（為了讓記憶依序更新），不使用併發；baseline 可併發。先讀再翻時，第一段譯文要等整集讀完（約占總費用四成）才會出現；預算若在讀的階段用完，不會有任何譯文。
- 說話者 `explicit` 只來自規則可驗證的發話標記；較隱晦的標記只會是 `inferred`（刻意保守）。
- 同一角色的不同稱呼只在有原文證據（或人工連結）時才合併，後文才揭露的身分不會提前合併。
- 人工譯文永遠優先；目前沒有「撤銷人工譯文」指令，需再以 `ncte edit` 覆寫。
- 不支援 fixed-layout、DRM、段落內 SVG/MathML、複雜 ruby（這些單元保留原文並列在報告中）。
- 譯本不輸出 ruby；讀音只作為翻譯提示。
- `dc:identifier` 保持原值，部分閱讀器可能把譯本與原書視為同一本。
- 預算檢查以保守估計進行；併發時可能有一個批次量級的偏差（報告中顯示上限）。

## 專案結構
```
src/ncte/  cli.py config.py library.py exporter.py samples.py
  epub/        security parser units reassemble validate
  llm/         base openrouter fake schemas prompt_loader cost
  translation/ orchestrator context_pipeline translator context_builder review edits cache dryrun
  analysis/    scene dialogue speaker reference_resolution memory_update
  memory/      db repository retrieval visibility
  qa/rules.py  reports/ (cost, qa, html)   eval/ (dataset, metrics, runner, data/ncte_eval_v1.json)
prompts/<task>/v1.md   版本化 prompt（translate / scene_analyze / memory_update / review）
tests/                 unit / integration / eval（全部離線）
scripts/make_sample_epub.py   產生自製測試 EPUB
```
