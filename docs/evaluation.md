# 評測方法（M6）

## 原則
- 指標只由**人工標註**與程式比對產生，不使用 LLM 自評或「偏好分數」。
- 模型與原文固定；每個實驗在全新 workspace 執行（不共用翻譯快取與記憶）；`--repeat` 量測波動。
- 少數例句不能代表全體：本資料集只有 10 個項目，用來驗證流程與做方向性比較；正式結論需更大的標註集。

## 資料集格式（`src/ncte/eval/data/ncte_eval_v1.json`）
```json
{
  "name": "ncte-eval-v1", "version": 1,
  "characters": [{"name_ja": "ミナ", "allowed_zh": ["米娜", "美奈"]}],
  "items": [{
    "id": "two_person_unmarked", "category": "兩人無標記對話",
    "lines": ["段落一", "「台詞」とミナは言った。", "<p>可直接寫 XHTML 段落</p>"],
    "gold": {
      "utterances": [{"line": 1, "quote_index": 0, "speaker": "ミナ", "explicit": true, "weak_only": false}],
      "units": [{"line": 2, "forbidden": ["她"], "required_any": [["信"], ["拆", "打開"]]}],
      "spoiler": {"before_line": 3, "terms": ["雷恩"]}
    }
  }]
}
```
- `line` 從 0 起算；每個項目產生 EPUB 中的一章，`lines` 一行一段落。
- `speaker: null` 表示「原文資訊不足，正解是未知」。
- `forbidden`：不得出現的添加（憑空的人名、未知性別的代名詞、讀音外洩…）；`required_any`：每組至少一個詞要出現（漏譯檢查）。
- `spoiler.before_line` 之前的譯文不得出現 `terms`。

## 實驗
| 代號 | 內容 | 模式 |
|---|---|---|
| A | 純段落翻譯：每個單元單獨請求、無前後文 | baseline |
| B | ＋鄰近上下文（前 2、後 1，限同場景） | baseline |
| C | ＋場景與人物／劇情記憶（無 LLM 說話者分析、無複查） | context |
| D | ＋LLM 場景／說話者／省略分析＋選擇性複查 | context |

## 指標
| 指標 | 定義 | 方向 |
|---|---|---|
| speaker_accuracy_explicit | 有標註說話者的句子中，系統說話者（explicit 或 inferred）正確的比例 | ↑ |
| speaker_explicit_precision | 系統標為 explicit 的句子中正確的比例（應接近 1） | ↑ |
| unknown_asserted_rate | 正解為未知的句子被系統標為 explicit 的比例 | ↓ |
| weak_evidence_asserted | 只有口癖線索的句子被標為 explicit 的數量（應為 0） | ↓ |
| unknown_false_assertion_rate | 正解為未知的句子，譯文中出現原文沒有的角色名的比例 | ↓ |
| omitted_argument_error_rate | 有 `forbidden` 標註的單元中，譯文出現禁止添加的比例 | ↓ |
| omission_rate | `required_any` 組別缺失的比例 | ↓ |
| omission_addition_rate | （缺失＋添加）／標註單元數 | ↓ |
| entity_terminology_consistency | 角色名出現處使用「最多數的允許譯名」的比例 | ↑ |
| spoiler_violations | 揭露點之前的譯文出現禁止詞的段數 | ↓（0） |
| epub_structure_integrity | 匯出 EPUB 的內部檢查、連結、重建、EPUBCheck 通過比例 | ↑（1） |
| total_cost_usd / usd_per_10k_ja_chars | API 回報費用（未知帳單以估計計入） | ↓ |
| units_per_minute / retries_per_100_units | 吞吐、重試率 | — |

baseline 實驗沒有說話者狀態，相關欄位為 n/a；譯文層指標（unknown_false_assertion_rate 等）所有實驗皆有。

## 執行
```bash
ncte eval check                                   # 驗證資料集
ncte eval run --dry-run                           # 估價
ncte eval run --experiments A,B,C,D --repeat 2 --budget-usd 0.5
ncte eval run --fake                              # 只檢查評測流程（指標無意義，報告會警告）
```
輸出在 `workspace/eval/<timestamp>/`：`results.json`（含每次執行的錯誤明細）、`results.csv`、`report.html`。

## 工程硬性門檻（規格 §11）
無遺漏 `unit_id`（對齊檢查）、placeholder 檢查全過（不通過即阻擋）、有效來源的 EPUBCheck 不新增錯誤、無金鑰外洩（redact＋測試）、無越權劇透記憶（visibility＋測試）。語意準確率門檻需先以真實模型收集 baseline 後再訂定。

## 修正驗收的判斷品質（`ncte eval fix-check`）
審查報告第五節第 1 項。`src/ncte/eval/data/fix_check_v1.json`：40 題自製短句，已知答案為已修 14、仍錯 14、問題不成立 8、改出新錯 4；同一句原文配不同的 `after`（例如否定改對／只加了副詞／改對但「昨日」變「明天」），看模型是不是只要讀起來通順就判 `fixed`。送出的內容與正式流程相同：同一份 `fix_check` 提示詞、schema、每次 20 題、相關人物的譯名與性別、`lines`；推理照正式流程（模型提供 `high` 時用 `high`，`stage_reasoning.fix_check` 可覆寫）。

```bash
ncte eval fix-check --dry-run --model google/gemini-3.8-flash --model anthropic/claude-sonnet-5.5   # 只查 /models 估價與餘額
ncte eval fix-check --model google/gemini-3.8-flash --model anthropic/claude-haiku-5.5 --budget-usd 0.2
ncte eval fix-check --fake --model google/gemini-3.8-flash                                         # 只檢查流程
```
指標（`workspace/eval/fix_check_<時間>/fix_check_results.json`，每題附模型的理由）：
- `false_settle_rate`：仍錯或改出新錯的修正被結案（判 `fixed`／`issue_invalid` 且沒有新錯）的比例，**目標 < 5%**（18 題中 0 題）。
- `false_fixed_rate`：不是「已修」卻判 `fixed` 的比例；`settle_recall`：該結案的有結案；`regressed_caught`：新錯有抓到；`new_error_on_clean`：沒有新錯卻報新錯；`undecided_rate`、`exact`、混淆表。
- 限制：題目短、錯誤明顯，量的是「會不會放過明顯沒修好的」，不是長段落的判斷力；通過不代表真實作品上的誤判率也低。題目由開發者撰寫並標註，尚未經第二人覆核。

## 語義挑戰集（設計，未建立）
字詞級的 `required_any`／`forbidden` 只能抓漏譯與憑空添加，抓不到意思相反：原文「読まなければよかった」（後悔讀了），錯譯「讀了真是太好了」仍含「讀」，照樣通過。現有資料集保留作回歸測試；另建挑戰集，先 100–200 題，之後再做整章、整本。

**每題的格式**（在現有 `items` 上加欄位）：
```json
{"id": "cf_regret_01", "category": "反事實", "lines": ["…前文…", "「読まなければよかった」"],
 "target": {"line": 1},
 "meaning": "說話者後悔讀了（讀已經發生）",
 "wrong_readings": ["讀了很好", "沒讀很可惜"],
 "accept": ["早知道就不讀了", "真不該讀的"],
 "judge": "譯文是否表達『已經讀了、而且後悔』；只有『讀』字不算"}
```
- `meaning` 與 `wrong_readings` 由人寫，是評分依據；`accept` 只是例子，不是唯一答案。
- 判定：人工（主要）＋另一家族的模型評審（輔助、需與人工一致率 ≥ 90% 才採用）。**不使用產生譯文的同一模型評分**。

**類別與題數**（初版）：否定／反問／條件／反事實 30；主受詞、授受（あげる・くれる・もらう）、被動、使役 30；省略的主語、賓語與說話對象 25；人物、稱呼、性別、敘事視角（含第三人稱中的直接內心獨白）20；晚揭露身分與不可靠敘事（`spoiler.before_line`） 15；刻意的說話方式與語氣變化（偽裝、模仿、情緒失控） 15；笑點、雙關、文化指涉（是否錯誤推斷品牌梗） 15；整段一致性（同一段落群的稱呼與口吻） 10。題目全部自行撰寫，不取自版權作品。

**分開報告的指標**：
1. 日中對照忠實度：挑戰題正確率（依類別）。
2. 中文文學表現：人工盲評自然度（1–5）與偏好比較，和忠實度分開。
3. 校稿修復率：送去修的問題中，`fix_check` 判 `fixed` 且人工同意的比例。
4. 校稿引入新錯的比例：`fix_regressed` 加上人工抽查發現的新錯。
5. 未審或無法裁決的比例：`blocked`、`failed`、`fix_undecided`、`fix_unchecked` 佔送審單元的比例。
6. 成本與時間：沿用 `total_cost_usd`、`units_per_minute`。
7. 人工修到可接受的工作量：抽 100 段，記錄人工修改的段數與編輯距離。
