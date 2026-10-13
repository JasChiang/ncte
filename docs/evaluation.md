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
