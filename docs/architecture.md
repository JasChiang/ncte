# 架構

```
EPUB ──► epub/security (Zip Slip/Bomb, XXE, DRM, fixed-layout)
     ──► epub/parser (container.xml → OPF → manifest/spine/NAV/NCX)
     ──► epub/units (leaf blocks + inline runs → ⟦n⟧ placeholders, ruby → 漢字《かな》)
     ──► library.import_book → SQLite: books, resources, units, memory_fts
     ──► analysis/scene.segment_book (deterministic scenes)

translate (baseline)
  orchestrator: jobs → batches (per chapter, ~1400 JA chars, parallel)
  context_builder: neighbours (same scene) + relevant glossary

translate (context, read first — default)
  0. scene breaks: long stretches without a break marker → the model picks cuts, the program checks (ADR-036)
  1. read, every scene in order:
       scene_analyze  → analysis/scene.apply_scene_analysis (quotes, tags, speakers, new characters, genders,
                        narration: first/third, narrator or focal character, narrator's self forms — ADR-040)
       memory_update  → analysis/memory_update (facts with verbatim evidence, summaries, address terms, wordplay,
                        relations the text states, group members; who each line is said to — ADR-045/046)
       a request the content filter refuses is read in halves and merged (analysis/split_read)
  2. verify: disputed speakers, inferred genders, reveal merges, address usage, narration conflicts, unnamed
     narrators, cards that may be a character known by name, group members — asked again with the surrounding
     text, high reasoning (ADR-033, ADR-037, ADR-045, ADR-046, ADR-049)
  3. relations, one pair at a time: the notes made while reading, the facts about both, how they address each
     other and the source passages where the two appear together (a member's pair gets its group's too); the
     model writes identity / feeling / same-person relations from where each begins, high reasoning
     (ADR-039, ADR-045, ADR-046, ADR-048, ADR-049, ADR-050)
     impressions: personality, voice and place in the story of the main characters (ADR-045)
     terms: what the recurring glossary terms (not names) mean in this book, from example sentences and the facts
     that mention them (found by string match); the answer cites them, the program checks the codes and shows a
     definition from the latest evidence it cites; sent as term_definitions where the term occurs (ADR-069)
  4. naming: renderings for names and address terms; the model's choice is kept, the program checks (ADR-035)
  5. translate, per scene in reading order, with memory/retrieval (visibility-filtered characters, relations,
     examples, speaker status, summaries, facts, props, the scene's narration)
     → program checks (second person 你／妳, narration 我, narrator's self form…) → review of flagged units
  6. reader pass: Chinese only, what a reader cannot follow goes back to the translator (ADR-038)

translator: strict JSON → Pydantic → ID alignment → qa/rules → retry → split → fail(keep JA)
review (flagged only, ≤1 pass) → retranslate with feedback + current text → QA → fix_check against the source
  (fixed / issue_invalid settle the issue; still_wrong / undecided / unchecked stay open, new text needs_review;
   a new error → the fix is rejected, the old text stays; ADR-055). Proofreading and the reader's look use the same path.
  Units refused or failed are counted blocked / failed, never reviewed; their findings stay.
DbRecorder: every request (hash, model, prompt version, usage, cost, generation id, status)
budget pre-check before each request; Ctrl+C → checkpoint; resume re-queues jobs

export ──► epub/reassemble (rebuild only validated text nodes; other files byte-for-byte; mimetype first)
       ──► epub/validate (internal + EPUBCheck) ──► reports (cost / QA / HTML / acceptance: done ≠ exported)
eval   ──► eval/runner (A/B/C/D in fresh workspaces) ──► eval/metrics (annotation-based)
gui    ──► gui/server (127.0.0.1, token) ── data (reads; source quotes with their translation) · actions (edits)
                                         · manage (upload, export, key)
                                         └─ runner (one translation at a time in a background thread)
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
| inferred | 動作敘述（含台詞旁另起一段的笑、微笑、苦笑）、LLM 推測、LLM 推翻程式標記（ADR-031）、覆核後的答案 | 否（只作為翻譯提示，標註為推測） |
| unknown | 無證據或證據衝突 | 否；`speaker_id = NULL` |
| contradicted | 後續事實撤回（`facts.retracted_by`） | 撤回點之後不可見 |

## 主要資料表
`books, resources, units, scenes, scene_overrides, glossary, runs, jobs, translations, llm_requests, qa_findings,
human_edits, model_snapshots` (migration 1) · `characters, utterances, speaker_hypotheses, speech_examples, facts,
chapter_summaries, prop_states, memory_rejections, memory_fts` (migration 2) · 之後：系列（series）、實體連結（entity_links）、
稱呼（address_terms、address_observations、form_senses）、定名提案（naming_proposals）、譯註（reader_notes）、性別（character_gender，migration 12–13）、人物關係（relations、讀時記錄 relation_notes，migration 16–18）、角色印象（character_impressions）、術語的意思（term_definitions，migration 31）、群體成員（group_members，migration 20–21）。
