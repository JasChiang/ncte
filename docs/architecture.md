# 架構

```
EPUB ──► epub/security (Zip Slip/Bomb, XXE, DRM, fixed-layout)
     ──► epub/parser (container.xml → OPF → manifest/spine/NAV/NCX)
     ──► epub/units (leaf blocks + inline runs → ⟦n⟧ placeholders, ruby → 漢字《かな》)
     ──► library.import_book → SQLite: books, resources, units, memory_fts
     ──► analysis/scene.segment_book (deterministic scenes)

translate (baseline)                          translate (context)
  orchestrator: jobs → batches                  per scene, in reading order:
  (per chapter, ~1400 JA chars, parallel)         scene_analyze  → analysis/scene.apply_scene_analysis
  context_builder: neighbours (same scene)                          (dialogue.find_quotes, detect_tags,
  + relevant glossary                                                speaker.resolve, reference_resolution)
                                                  translate batches (sequential) with memory/retrieval
                                                     (visibility-filtered characters, examples, speaker
                                                      status, summaries, facts, props)
                                                  memory_update → analysis/memory_update (verbatim evidence)
  translator: strict JSON → Pydantic → ID alignment → qa/rules → retry → split → fail(keep JA)
  review (flagged only, ≤1 pass) → retranslate with feedback → QA
  DbRecorder: every request (hash, model, prompt version, usage, cost, generation id, status)
  budget pre-check before each request; Ctrl+C → checkpoint; resume re-queues jobs

export ──► epub/reassemble (rebuild only validated text nodes; other files byte-for-byte; mimetype first)
       ──► epub/validate (internal + EPUBCheck) ──► reports (cost / QA / HTML)
eval   ──► eval/runner (A/B/C/D in fresh workspaces) ──► eval/metrics (annotation-based)
```

## 資料流與交易邊界
- 每批翻譯一個交易（譯文、QA 發現、job 狀態一起提交）；請求紀錄在送出前寫入（`sent`），回應後更新。
- 場景分析、記憶更新、複查各自獨立交易與 job；任何一步失敗都不會回滾已提交的譯文。
- 快取鍵：`source_hash` + prompt 主版本（v1、v2…）+ 模型 + 模式 + 相關術語 hash（只算人工確認的術語）；`context_hash` 只記錄。同一主版本內修改 prompt 檔、自動發現的術語都不會讓舊譯文失效（ADR）；dry-run 會列出用舊修訂翻譯的段落數，要套用新 prompt 請用 `retranslate`／`translate --force`，做品質比較時每組用全新 workspace。
- 匯出時每個單元取「人工 > 最新機器譯文（succeeded/needs_review）」，且 `source_hash` 必須相符。

## 證據分級
| 層級 | 來源 | 可否進入口癖庫／確定記憶 |
|---|---|---|
| explicit | 程式偵測的發話標記、explicit 引號的續段、逐字證據支持的事實 | 是 |
| human_verified | `ncte speaker-set`、`ncte edit`、`ncte glossary set` | 是（最高優先） |
| inferred | 動作敘述、LLM 推測、只有弱證據 | 否（只作為翻譯提示，標註為推測） |
| unknown | 無證據或證據衝突 | 否；`speaker_id = NULL` |
| contradicted | 後續事實撤回（`facts.retracted_by`） | 撤回點之後不可見 |

## 主要資料表
`books, resources, units, scenes, scene_overrides, glossary, runs, jobs, translations, llm_requests, qa_findings,
human_edits, model_snapshots` (migration 1) · `characters, utterances, speaker_hypotheses, speech_examples, facts,
chapter_summaries, prop_states, memory_rejections, memory_fts` (migration 2)。
