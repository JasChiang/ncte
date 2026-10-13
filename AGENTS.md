# AGENTS.md — 給接手的 Coding Agent

需求來源：維護者的開發規格（未納入 repo；程式註解中的「spec §x」指該文件章節，摘要即下方守則）。工作狀態：`docs/changelog.md`。決策：`docs/decisions.md`。

## 執行守則（規格 §0 摘要）
1. 自行實作；不得 clone／fork／搬用任何小說翻譯器 Repo（AiNiee、LinguaGacha、BookTrans、AutoNovel…）。一般 Python 套件可用；EPUBCheck 只當外部驗證工具。
2. 先確保可端到端執行，再擴充功能；每個 Milestone 都要能獨立執行與測試。
3. 不虛構 API、模型能力、費率或執行結果；模型與價格由 OpenRouter `/models` 動態查詢。沒有金鑰就不要宣稱完成線上實測。
4. 不上傳 EPUB 以外的資料；不把完整 EPUB 放進 git（`tests/fixtures` 只放自製的小型 EPUB）。
5. 不破壞原始 EPUB；無法安全翻譯的單元保留原文並列入報告。
6. 忠於原作：可以推理，但譯文不得加入原文沒有的確定資訊（人名、性別、說話者、劇透）。
7. 所有 LLM 輸出經 Pydantic 驗證；記憶事實需附可回溯原文證據；推測不得自動升級為確定事實。
8. 每個 Milestone：跑測試、列完成事項／限制／重現指令，更新 `docs/changelog.md`。

## 開發指令
```bash
source .venv/bin/activate
pytest                 # 全部離線（FakeLLM、MockTransport），不需金鑰
ruff check src tests
mypy src
```
EPUBCheck 測試會在找到 `epubcheck`（PATH）、`EPUBCHECK_JAR` 或 `workspace/tools/**/epubcheck.jar`＋Java 時執行，否則 skip。

## 模組地圖
- `epub/` 安全解析、單元抽取、重建、驗證
- `llm/` 介面、OpenRouter adapter、FakeLLM、schema、prompt 載入、費用
- `translation/` orchestrator（jobs/預算/續譯）、translator（對齊/QA/切批）、context builder、dry-run
- `analysis/` 場景切分、對話偵測、說話者歸屬、記憶更新驗證
- `memory/` SQLite migrations、repository、檢索（FTS5）、可見性
- `qa/` 程式 QA 規則；`reports/` 成本／QA／HTML；`eval/` A/B 評測
