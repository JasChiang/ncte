# NCTE — Novel Context Translation Engine

把**有權使用、無 DRM** 的日文 EPUB 輕小說，透過 OpenRouter 上的模型翻成臺灣繁體中文，並輸出可在一般閱讀器開啟的完整 EPUB。macOS 本機執行（網頁介面或指令），資料全部存在本機 SQLite。

## 不用指令的用法（網頁介面）
1. 安裝 [Python 3.12 以上](https://www.python.org/downloads/)，下載本專案資料夾。
2. 在 Finder 雙擊 **`NCTE.command`**（第一次會自動安裝，約幾分鐘；macOS 若擋下，請按右鍵 →「打開」）。瀏覽器會打開 NCTE。
3. 右上角「設定」貼上 [OpenRouter](https://openrouter.ai/keys) 金鑰並儲值；書庫頁把 EPUB 拖進去 → 選系列 → 書的概況頁按「開始翻譯」。
   - 開始前會顯示估價；預算上限自動設為估價的 2 倍（只擋異常），到了會先停下來問你。
   - 先讀完整本再翻：讀的階段（約占費用一半）還看不到譯文。名字與稱呼的譯法由模型決定、程式檢查，不會停下來問；想改的話翻完後到「定名」頁改，已翻好的段落會一起換掉。
   - 可以暫停；關掉視窗也不會遺失進度，下次按「繼續」接著做。
4. 翻完在概況頁按「匯出 EPUB」→「下載」。「對照審稿」可以逐段修改譯文。

模型與推理程度不用選：預設 `google/gemini-3.8-flash`（推理 medium，模型自己的預設）。比較紀錄見 [`docs/changelog.md`](docs/changelog.md)：同一章節裡，便宜的模型會忽略角色性別（你／妳）、或整批改寫成簡體。

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

**實測紀錄**：三本日文輕小說（青春、奇幻；其中兩本有正式中文版可對照）。奇幻一本（約 3,700 段、對話多）從網頁以 context 模式全自動重跑：US$6.2、約 2.5 小時；與正式版比對，對女性誤用「你」由 19 句降到 0 句；與正式版逐段比對後找到的問題（對女性用「你」、同一人被建成兩個角色、程式標記把說話者鎖錯…）都改成通用的修正，記錄在 [`docs/changelog.md`](docs/changelog.md) 與 [`docs/decisions.md`](docs/decisions.md)。
**尚未有的數據**：`ncte eval run`（人工標註指標）的正式評測尚未執行；目前的品質判斷來自與正式中文版的逐段比對。

## 隱私與權利（請先讀）
- 翻譯時（包括從網頁介面開始的翻譯），**所需的原文段落與上下文會送到 OpenRouter**，再由 OpenRouter 路由給第三方模型供應商（例如 Google）。各供應商的資料保留／訓練政策不同，請在 OpenRouter 帳號的 Privacy 設定中確認。目前沒有純本機模型模式。
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
#   --model / --reasoning-effort low|medium|high|default 可用來比較模型（default＝模型自己的預設）
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
ncte names gender 書.epub                    # 角色性別（原文明寫／推測／人工）與依據；翻譯時決定「你／妳」「他／她」
ncte names gender 書.epub エルリア female      # 人工設定（unknown＝不提供，翻譯用「你」）
ncte names gender-scan 書.epub               # 已讀過的舊書補記性別（只呼叫記憶更新，不動其他記憶）
ncte names address-check 書.epub            # 已讀過的舊書補做稱呼歸類（當面叫／提到／自稱／轉述／無關，ADR-037）
```

### 系列翻譯風格（每一集都適用）
```bash
ncte style show 書.epub
ncte style set 書.epub --honorifics omit --rule "先輩一律譯「學姊」" --voice "安達=話少、句子短、常結巴"
```
敬稱處理有 `natural`（預設，依語氣自然處理）、`explicit`（固定譯出）、`omit`（原則省略）。譯註預設開啟（`--notes off` 關閉）：雙關或文化梗無法用譯文傳達時，匯出的段落後會加「（註：……）」，可用 `ncte notes list` 檢視、`ncte notes drop` 刪除。設定只影響之後的翻譯；既有譯文要重翻才會套用。

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

**先讀再翻**（預設）：作者沒有標出換場的長段落，先請模型決定場景切點（ADR-036）；接著 context 模式會把整集依序讀完（說話者、角色、性別、稱呼、事實、摘要），把有爭議的判斷帶著前後文再覆核一次（ADR-033，沒有爭議時不發請求），再請模型為名字與稱呼提出譯法（含其他選項與理由），最後才逐場景翻譯。重要且有不同譯法的項目（例如主要角色名、常用稱呼、中文只剩一個字的人名）列為「可再確認」；預設不暫停（`context.naming = "auto"`，ADR-035），設為 `"review"` 時會先停下來等你決定：
```bash
ncte names proposals 書.epub                 # 待確認的譯法與選項
ncte names decide 書.epub --id 3 --pick 2    # 選第 2 個選項；或 --zh 自訂譯法；或 --accept-all 全部採用建議
ncte resume --run-id r_xxx                   # 決定後繼續翻譯
ncte names propose 書.epub                   # 已分析過的書（例如要重翻前）單獨產生提案
ncte names proposals 書.epub --all --context   # 所有提案（含已套用的）與原文例句
```
`"off"` 則完全不做定名；`context.read_first = false` 恢復逐場景交錯的舊流程。你決定的譯法在同系列之後的集數會沿用，不會再問。待確認的項目會附上幾句原文例句方便判斷。封面、目次、注意事項、後記、版權頁照常翻譯，但不做角色與劇情分析（`context.skip_book_matter`）。翻譯時仍只附上該場景之前已揭露的資訊（例如身分揭露前，角色只以當時的稱呼出現）。

### 本機網頁介面
```bash
ncte gui            # 開啟 http://127.0.0.1:8780/ （只有這台電腦能開；Ctrl+C 結束）；或雙擊 NCTE.command
```
上傳 EPUB、開始／暫停／繼續翻譯（各階段進度與花費）、匯出與下載、金鑰與餘額；對照審稿（點譯文直接修改、刪除譯註；每段可展開前後各 3 段；標出程式與 LLM 有爭議的台詞與覆核結果）、定名（看例句選譯法；同一暱稱已定的譯法）、故事進度、角色檔案（性別與依據，可修改）、人物關係圖（拖動「讀到」滑桿防劇透）、名詞與稱呼。網頁開始的翻譯固定用預設模型，預算依估價自動設定（ADR-034）。

### 品質控制與人工校正
```bash
ncte glossary set    ./samples/book.epub ミナ 米娜 --category character   # 列出受影響單元；只有這些會重翻
ncte glossary impact ./samples/book.epub ミナ
ncte unit ./samples/book.epub u_xxx                  # 原文、placeholder、所有譯文版本、QA 發現
ncte edit ./samples/book.epub --unit-id u_xxx --text "還有，明天⟦1⟧一定⟦/1⟧要早點來。」"
ncte review ./samples/book.epub --scope flagged --budget-usd 0.5
ncte reader-check ./samples/book.epub                # 以不懂日文的讀者身分讀中文，看不懂的地方對照原文修正或加註（ADR-038）
```
自動複查由設定 `review.automatic_review: off | flagged_only | all` 控制（預設只複查被程式 QA 或說話者衝突標記的單元，最多一輪）。審稿看前後各 5 段與每句說話者；提出具體修正就重翻並再經程式檢查，程式判定為錯誤的（例如殘留假名）一律修正。規則改進後可用 `ncte recheck 書.epub` 重新檢查既有譯文（不呼叫 API），再用 `ncte review 書.epub` 修正被標記的段落。

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
- Gemini 等推理模型的推理 token 以輸出價計費（翻譯的輸出約一半是推理）。試算會以同一工作區的實際花費校正；全新工作區沒有校正資料時估價偏低（英雄賢者試算上限 $4.3，實際約 $5.6）。
- 更便宜的模型目前不建議：第二章比較中 claude-haiku-5.5 忽略角色卡的性別（你／妳錯 36 句），deepseek-v4-flash、qwen3.8-flash 會整批改寫成簡體（現在會被程式擋下重翻，但多花費用）。
- context 模式依場景順序執行（為了讓記憶依序更新），不使用併發；baseline 可併發。先讀再翻時，第一段譯文要等整集讀完（約占總費用四成）才會出現；預算若在讀的階段用完，不會有任何譯文。
- 說話者 `explicit` 只在程式的發話標記與 LLM 一致時成立；LLM 有異議時採用 LLM 的判斷並標為 `inferred`（ADR-031）。
- 同一角色的不同稱呼只在有原文證據（或人工連結）時才合併；名字揭曉時的合併只在揭曉位置之後可見（ADR-032），被覆核質疑的合併會列為待確認，不會自動拆開。
- 網頁介面同時只翻一本書；翻譯中關掉 NCTE 會在下次「繼續」時從最後完成的請求接著做（中斷的那個請求可能已計費）。
- 人工譯文永遠優先；目前沒有「撤銷人工譯文」指令，需再以 `ncte edit` 覆寫。
- 不支援 fixed-layout、DRM、段落內 SVG/MathML、複雜 ruby（這些單元保留原文並列在報告中）。
- 譯本不輸出 ruby；讀音只作為翻譯提示。
- `dc:identifier` 保持原值，部分閱讀器可能把譯本與原書視為同一本。
- 預算檢查以保守估計進行；併發時可能有一個批次量級的偏差（報告中顯示上限）。

## 專案結構
```
src/ncte/  cli.py cli_memory.py config.py library.py workspace.py series.py lexicon.py exporter.py samples.py
  epub/        security parser units reassemble validate matter series_meta
  llm/         base openrouter fake schemas prompt_loader cost
  translation/ orchestrator context_pipeline translator context_builder review rename edits cache dryrun
  analysis/    scene dialogue speaker reference_resolution memory_update naming verify
  memory/      db repository retrieval visibility entities address style
  gui/         server data actions manage runner static/
  qa/          rules recheck   reports/ (cost, qa, html)   eval/ (dataset, metrics, runner, data/ncte_eval_v1.json)
prompts/<task>/v1.md   版本化 prompt（translate / scene_analyze / memory_update / naming / verify / review）
NCTE.command           Finder 雙擊啟動網頁介面
tests/                 unit / integration / eval（全部離線）
scripts/make_sample_epub.py   產生自製測試 EPUB
```
