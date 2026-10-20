# 架構

```
EPUB ──► epub/security (Zip Slip/Bomb, XXE, DRM, fixed-layout)
     ──► epub/parser (container.xml → OPF → manifest/spine/NAV/NCX)
     ──► epub/units (leaf blocks + inline runs → ⟦n⟧ placeholders, ruby → 漢字《かな》)
     ──► library.import_book → SQLite: books, resources, units, memory_fts
     ──► analysis/scene.segment_book (chapters from the TOC; a provisional cut at the author's marks until the model
         cuts each chapter)

translate (baseline)
  orchestrator: jobs → batches (per chapter, ~1400 JA chars, parallel)
  context_builder: neighbours (same scene) + relevant glossary

translate (context, read first — default; translation/context_pipeline.ContextOrchestrator.execute)
  0. chapters from the TOC (epub/chapters); inside each chapter the model cuts scenes in overlapping windows
     (analysis/scene_windows, ADR-057; replaces the ADR-036 cut); book matter keeps the program's cut (ADR-024)
  1. read, every scene in order (read_pass):
       scene_analyze  → analysis/scene.apply_scene_analysis (quotes, speakers and who each line is said to, new
                        characters, other name forms, sentences whose subject is left out (actions), questions
                        (uncertainties), narration: first/third, narrator or focal character, self forms — ADR-040)
       memory_update  → analysis/memory_update (facts with verbatim evidence, summaries, glossary candidates, name
                        links, address terms, wordplay, genders, relations the text states, group members — ADR-045/046)
       (context.read_in_one_request: both from one scene_read request, applied by the same two functions — ADR-065)
       a request the content filter refuses is read in halves and merged (analysis/split_read)
  2. verify: disputed speakers, inferred genders, reveal merges, narration conflicts, the read pass's own questions,
     unnamed narrators, cards that may be a character known by name, group members — asked again with the
     surrounding text, high reasoning (ADR-033, ADR-045, ADR-046, ADR-049, ADR-067)
  3. relations, a few pairs per request: the notes made while reading, the facts about both, how they address each
     other and the source passages where the two appear together (a member's pair gets its group's too); high
     reasoning (ADR-039, ADR-045, ADR-046, ADR-048, ADR-049, ADR-050)
     impressions: personality, voice and place in the story of the main characters (ADR-045, ADR-060)
     house_style: honorific policy, the main characters' speech habits, house rules → series style (ADR-053)
     terms: what the recurring glossary terms (not names) mean in this book (ADR-069)
  4. naming: renderings for names and address terms; the model's choice is kept, the program checks (ADR-035);
     the house style's voices follow a changed rendering (style_follows_naming)
  5. translate, chapters side by side, the scenes of a chapter in order, one request per scene (ADR-044, ADR-065),
     with memory/retrieval as known at that point (see 〈各階段傳遞了什麼〉)
     → program checks (second person 你／妳, narration 我, narrator's self form…) → confirm (the rule flags and the
       translator's own doubts, asked again with the lines around) → review of flagged units
  6. proofread: every paragraph against its source, 2 passes on the Claude plan and Codex, else 1 (ADR-051, ADR-066)
  7. reader pass: Chinese only, what a reader cannot follow goes back to the translator (ADR-038)
  8. table of contents and book title (run_meta)

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

## 各階段傳遞了什麼

以下全部依程式（2026-10-20，main `bbfc397`，加上第二批接線：ADR-067 補充）整理，並用 FakeLLM 實際跑一次範例書（`ncte.samples` 的 basic 書、context 模式、預設設定加兩輪校稿），把每一步送出的請求頂層鍵印出來對照過。欄位名是請求 JSON 裡的鍵；「只在有內容時」的鍵（例如 `term_definitions`、`narration`、`address_terms`）沒有資料就不送。

### 總表（依執行順序）

模型欄是 **Claude 方案的預設**（`config.use_claude_models`、`orchestrator.DEFAULT_STAGE_REASONING`）：讀的步驟（`CLAUDE_READ_STAGES`：切場景、讀場景、記憶整理、印象、體例）用 Sonnet，其餘用 Opus；推理程度整個 run 預設 medium，覆核、人物關係、修正檢查、校稿是 high。OpenRouter 上同樣這四步是 high（模型有提供 high 時），模型則是 run 的模型。

模型來源（`openrouter.provider`、`NCTE_LLM_PROVIDER`；`cli.make_client` 建 client）：`claude`（預設，`llm/claude_cli`：每個請求一次 `claude -p`，ADR-062）、`codex`（`llm/codex_cli`：每個請求一次 `codex exec`，用 ChatGPT 訂閱，所有步驟預設 `codex/gpt-6.1-sol`，ADR-071）、`openrouter`／`fal`（`llm/openrouter`、`llm/fal_openrouter`，付費）、`local`（`llm/local`，本機 OpenAI 相容伺服器，ADR-068）。模型 id 的前綴 `claude/`、`codex/`、`local/` 指名自己的來源：`stage_models`、`review_model`、`fallback_models` 可以把某一步交給另一個來源（`llm/routing.RoutingClient` 依前綴分流），執行的來源仍是主來源。Claude 方案的預設不含 Codex；`codex` 來源只留 `codex/` 的模型。

| # | 步驟 | 模型（推理） | 一次請求的範圍 | 模型拿到什麼 | 模型回傳什麼 | 程式核對什麼 | 寫到哪裡 | 之後誰會用 | ADR／程式 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 匯入 | 無 | 整本 | — | — | Zip Slip／Bomb、XXE、DRM、fixed-layout；葉節點與行內標記轉 `⟦n⟧`、ruby 轉讀音記號 | `books`、`resources`、`units`、`memory_fts`、`chapters`（依目錄）、暫定的 `scenes` | 所有步驟 | ADR-002～004、016、052／`epub/security`、`epub/units`、`library.import_book` |
| 2 | 切章與切場景 `scene_windows` | Sonnet（medium） | 一章的一個窗格（約 9,000 字或 220 段，前後重疊；上一窗格最後一個場景從頭再看） | 章名與有沒有目錄 `chapter`；每段開頭 120 字 `text`、長度 `chars`、是不是台詞 `line`、長段的結尾 20 字 `end`、作者標記（分隔線、空行）`marks`；`starts_scene`、`reaches_chapter_end`、場景大小提示 `scene_size` | `breaks`：每個新場景的第一段、種類（present／flashback／parallel／summary／other）、`sure`、`new_chapter`（只在沒有目錄時）、`reason`；`first_kind` | 切點在窗格內、不是第一段、不在跨段台詞中間；分隔線後一段的切點移到分隔線上 | `scene_breaks`、（沒有目錄的書）`chapters`；再由 `segment_book` 重建 `scenes`、`units.scene_id` | 之後每一步的「場景」 | ADR-057、059／`analysis/scene_windows`、`context_pipeline.cut_chapters` |
| 3 | 讀場景 `scene_analyze` | Sonnet（medium） | 一個場景（超過 120 段時平均切成幾部分，後面的部分附前一部分最後 4 段 `part_before`；被擋就對半切） | 全場原文 `units`；程式依括號找出的台詞 `quotes`（是否接續上一句）；**場景開頭以前**已知的人物 `known_characters`（名字、別名、性別、敘述者、程式算的說話特徵 `speech_profile`、讀時記下與已成立的關係 `relations`）；場景原文出現的譯名 `glossary`；前情 `previous_chapter`、`story_so_far`（本章最近 3 段摘要）、`previous_scene_end`（上一場最後約 400 字）；`previous_narration`、`established_self_forms`、`narration_hint`（程式數的敘述段數）、非現在時間的 `scene_kind` | 新人物 `new_characters`（附證據、`same_as_ref` 揭曉、別名種類、群體）、已知人物的其他稱呼 `other_forms`、在場者 `participants`、每句台詞 `utterances`（候選、證據類型、說話者、確定程度、對象 `to` 與 `to_status`、發話標記 `tag`）、省略主詞的句子 `actions`、物品 `props`、疑問 `uncertainties`、敘述 `narration`（人稱、敘述者或焦點人物、自稱）、`pov`／`location`／`time` | 名字逐字出現在證據段；`tag` 逐字在引號外並指向說話者，才算 explicit（ADR-011）；id 必須是已知人物或本次新人物；台詞必須對得上程式找到的引號；物品要在場景原文裡 | `characters`、`entity_links`、`glossary`（人名候選）、`utterances`（含 `addressee_ids_json`）、`speaker_hypotheses`、`speech_examples`、`scenes.state_json`（narration、actions、uncertainties、participants、props…）、`memory_rejections` | 記憶整理、覆核、關係、印象、體例、定名、翻譯、審稿、校稿、修正檢查 | ADR-011、012、031、032、040、044、059、060／`analysis/scene` |
| 4 | 記憶整理 `memory_update`（或合併的 `scene_read`） | Sonnet（medium） | 一個場景（同上的切法） | 全場原文 `units`（已有譯文時附 `translation`，先讀再翻的第一次沒有）；**到場景結尾為止**已知的人物 `known_characters`（含群體成員 `members`）；讀場景的結果 `scene`（說話者、narration、actions、uncertainties…）；`glossary`；**場景開頭以前**、和本場最相關的 30 筆舊事實 `known_facts`（附 id，可撤回）；`scene_kind` | 場景摘要 `scene_summary`、事實 `facts`（主體、述語、受詞、類型、確定程度、逐字證據 `evidence_quote`、觀點、撤回、時期）、譯名候選 `glossary_candidates`、物品持有 `prop_states`、名字連結 `name_links`、稱呼 `address_terms`、文字遊戲 `wordplay`、性別 `genders`、文中寫明的關係 `relations`、群體成員 `groups` | 證據段在本場、證據句逐字在證據段裡；主體／說話者／對象是已知 id；撤回只能撤回更早的事實，角色的想法只能由同一人改變；譯名候選至少 2 字且逐字出現 | `facts`、`chapter_summaries`、`glossary`、`entity_links`、`address_terms`／`address_observations`、`reader_notes`、`character_gender`、`relation_notes`、`group_members`、`prop_states`、`characters`（人名候選）、`memory_fts`、`memory_rejections` | 覆核、關係、印象、體例、術語、定名、翻譯、審稿、校稿 | ADR-019、022、026、030、037、045、046、058、065、069／`analysis/memory_update`；`scene_read` 見 `context_pipeline.scene_read_payload` |
| 5 | 覆核 `verify` | Opus（high） | 每 25 題一個請求 | 題目 `items`，每題 `ref`＋`kind`：有爭議的台詞 `speaker`（台詞、前後 4 段、候選人、第一次的答案）、推測的性別 `gender`（證據、分散全書的 5 段）、揭曉時的合併 `merge`、人稱衝突 `narration`、讀時疑問 `question`（疑問、前後原文、場景人物）、未具名敘述者 `narrator`（事實、別人怎麼叫他、台詞、其他第一人稱場景的敘述者）、可能是已知角色的卡 `identity`、群體成員 `group`（`called_by`、`members`） | 每題 `answer`（人物 id／unknown、female／male、same／different、first／third、一句話…）、`reason`、可選 `character_id` | 答案必須是該題接受的值（id 要在候選裡）；人工確認的台詞不動；同一題、同一份資料答過就不再問 | `utterances`（說話者、`resolution_source='verify'`）、`character_gender`（推測被否定就刪）、`scenes.state_json`（narration、uncertainties 的 `answer`）、`group_members`、合併 `characters`／`entity_links`、`verify_answers`、`qa_findings`（`merge_doubted`） | 之後所有用說話者、性別、敘述者、疑問的步驟 | ADR-033、045、046、049、067／`analysis/verify` |
| 6 | 人物關係 `relations` | Opus（high） | 每 8 對一個請求（最多 40 對）；該有關係卻空著的對再問一次（`recheck`） | 人物 `characters`（代號、名字、性別、敘述者、別名）；每對 `pairs`：讀時記下的關係 `notes`（最多 10，各種關係留最早與最晚一筆）、兩人的事實 `facts`（最多 10）、兩人一起出現的原文 `passages`（群體的 3 段＋兩人的 6 段，標說話者）、彼此的稱呼 `address`、同場說話次數 `scenes_together` | `relations`：兩人、關係、各自是對方的什麼、引用的證據代號 `facts`、`reason`、種類（身分／情感／同一人）、確定程度、`replaces`、時期 | 引用的代號必須是這次給過、而且屬於這一對的；人設的關係不覆蓋 | `relations`（`since_ordinal`＝最早引用的證據；`replaces` 讓舊關係在新關係開始處結束） | 翻譯、審稿、修正重譯（人物卡）、校稿與修正檢查（每段說話的兩人）、印象、定名（稱呼雙方）、讀場景（下次重讀）、網頁關係圖 | ADR-039、045、046、048、049、050、058／`analysis/relations` |
| 7 | 角色印象 `impressions` | Sonnet（medium） | 每 4 人一個請求（台詞最多的前 12 人＋敘述者） | `characters`：名字、敘述者、性別、別名、分散全書的 8 句台詞 `lines`（推測的標 `guess`）、事實 `facts`（最多 8）、他怎麼叫別人 `calls_others`（最多 6）、關係 `relations`（最多 6） | `impressions`：個性 `personality`、口吻 `voice`、定位 `role`、引用 `evidence`、從哪裡開始 `from`、時期 `period`（一人可分幾段） | 引用的代號必須是給過的；`voice` 不能空 | `character_impressions`（`since_ordinal`、`revealed_ordinal`＝最晚引用的證據） | 翻譯、審稿、修正重譯（只有 `voice`）、校稿與修正檢查（`voice`）、體例 | ADR-045、060／`analysis/impressions` |
| 8 | 體例 `house_style` | Sonnet（medium） | 全書一次 | 主要角色 `characters`（名字、性別、敘述者、別名、各段口吻 `voice`、分散全書的 12 句台詞＋每個重複句尾一句、程式數的重複句尾 `repeated_endings`）；全書稱呼與次數 `address_forms`（前 40）；目前的譯名 `names`（前 80） | 敬稱方針 `honorifics` 與理由、每人的說話習慣 `habits`（日文、中文譯法）、這本書的規則 `rules` | 人設過的體例不覆蓋；習慣要對到給過的人 | `series.style_json`（理由不送進翻譯） | 定名、翻譯、審稿、修正重譯、校稿、修正檢查（只有習慣） | ADR-053、067／`analysis/house_style` |
| 9 | 術語 `terms` | Opus（medium） | 先一個「補漏」請求（ADR-073），再每 20 個詞一個請求（最多 60 個，加補上的最多 20 個） | 補漏：全書事實（最多 400 筆，分散）`facts`、各章摘要 `summaries`、已知的詞 `known`（譯名表全部，含人名）→ 模型列出還沒列入的術語；程式只收引用了給過的代碼、還不在已知裡、正文出現 2 次以上的。定義：`terms`：譯名表裡 term／skill／item／organization／place、正文出現 2 次以上的詞（人名除外、人工定義過的除外），加上補漏收下的詞（`zh` 空）；各附次數、最多 3 句分散全書的例句 `examples`、最多 5 筆字串含這個詞的事實 `facts`（推測標 `guess`、時期） | `terms`：`definition`、引用 `evidence`、`revealed_from`、`contrast_with` | 引用的每個代號都必須是這次給過的、至少一個；人工定義不覆蓋 | `term_definitions`（`revealed_ordinal`＝引用的證據與 `revealed_from` 中最晚的位置） | 翻譯、修正重譯、審稿、校稿、修正檢查、定名 | ADR-069／`analysis/terms`、`memory/terms` |
| 10 | 定名 `naming` | Opus（medium） | 全書一次（被擋時分段） | 前 30 個人物 `characters`（名字、別名、性別、敘述者）；還沒決定的名字 `names`（最多 80：詞、目前譯名、類別、次數、2 段樣本、同一實體的其他形式；不是人名、有術語定義（模型或人寫的）的附 `definition`）；還沒決定的稱呼 `address`（最多 60：誰叫誰、詞、目前譯法、次數、說明、兩人的現行關係 `relation`、樣本）；名字也是別人稱呼的人物 `characters_to_check`；體例 `style` | `names`、`address`（譯法、理由、其他選項）、`characters`（different／same／unsure） | 只收問過的 ref；譯名不空、最多 24 字；撞名標記；`auto` 模式直接採用 | `glossary`、`naming_proposals`、`address_terms.form_zh`、`characters`；之後體例的說話習慣跟著改名 | 翻譯與之後所有帶譯名、稱呼的步驟 | ADR-023、028、035、056／`analysis/naming`、`context_pipeline.style_follows_naming` |
| 11 | 翻譯 `translate` | Opus（medium） | 一個場景一個請求（6,000 字、200 段以內；更長的在敘述段之間平均切開，每部分附場景其他部分）；各章同時進行，同一章的場景依序 | 原文 `units`；前 2 段（附譯文）與後 1 段（同場景）`context_before`／`context_after`；譯名 `glossary`；體例 `style`；文字遊戲 `wordplay`；人物卡 `characters`；本場 `scene`（說話者與對象 `utterances`、省略主詞 `omitted_arguments`、疑問與覆核的回答 `uncertainties`）；記憶 `memory`（上一章回顧、本章最近 4 段摘要、事實、物品）；稱呼 `address_terms`；術語 `term_definitions`；敘述 `narration` | `translations`：每段譯文、`flags`、給審稿者的 `notes`、給讀者的 `translator_note`（`read_through` 開著時先列 `read_through`） | 嚴格 JSON → Pydantic → id 一一對應 → placeholder → `qa/rules`（假名、簡體…）→ 重試／對半切／保留原文；context 模式另查你／妳、第三人稱敘述的「我」、敘述者自稱、「敘述者」字樣 | `translations`、`qa_findings`（含譯者旗標 `model_flag:…`）、`jobs` | 規則確認、審稿、校稿、讀者檢查、匯出；之後的翻譯當前文 | ADR-022、025、040、044、055、064、067、069／`translation/translator`、`orchestrator.batch_context`、`context_pipeline.batch_context`、`memory/retrieval.memory_context` |
| 12 | 規則確認 `confirm` | Opus（medium） | 一個場景有旗標時一個請求 | `items`：每個旗標（規則、程式的說明）、原文、譯文、說話者 `speakers`、前後各 5 段（原文、目前譯文、說話者）`around`；最早那段為止已知的人物 `characters`（名字、性別、敘述者，最多 40）；`possible_added_name` 的項目另附本段與前後文出現的人名譯名 `glossary`，`second_person_gender` 的項目另附這段台詞的對象與其性別 `to` | `checks`：每個旗標 `real` 與理由 | 只收真的有被標的（段, 規則） | `qa_findings`（真：加註 confirmed，審稿一定送修；假：`dismissed_by_check`）、`translations`（沒有其他嚴重發現就結案） | 審稿 | ADR-031、067／`context_pipeline.confirm_flags` |
| 13 | 審稿 `review` | Opus（medium） | 只看有旗標的段落，一個場景每 20 段一個請求，每段最多一輪 | `items`：原文、譯文、為什麼被標 `flags`、譯者的 `translator_notes`、說話者；前後各 5 段 `context`；**整個場景**的人物卡 `characters`、`scene`（只留受審段落的台詞、省略主詞、疑問）、`glossary`、`address_terms`、`style`、`term_definitions`、記憶 `memory`（同翻譯這一場的：上一章回顧、本章最近摘要、事實、物品；都空時不送） | `issues`：類型、嚴重度、說明、建議、`needs_retranslation` | 段落必須是送審的、說明不能空；medium 以上且有建議才送修；規則確認判真的一定送修；沒報問題的段落清掉程式旗標 | `qa_findings`（`review:…`、`cleared_by_review`）、`jobs` | 修正重譯 | ADR-014、018、055、067／`translation/review.review_scope` |
| 14 | 修正重譯（審稿、校稿、讀者檢查共用） | Opus（medium，同翻譯） | 同一場景、相鄰的一串段落一個請求 | 和翻譯相同的整包（`batch_context`，前後文改為同場景各 2,500 字內）＋ `review_feedback`：每段目前的譯文 `current_translation`（含譯註）與問題（類型、說明、建議；校稿與讀者檢查的建議附固定說明 `FIX_NOTE`） | 同翻譯 | 同翻譯；新譯文先記 `needs_review`、加 `fix_unchecked` 標記，等修正檢查 | `translations`（新版本）、`qa_findings` | 修正檢查 | ADR-055／`review._retranslate_with_feedback` |
| 15 | 修正檢查 `fix_check` | Opus（high） | 每 20 個修正一個請求 | `items`：原文、修正前 `before`、修正後 `after`、問題 `issues`（關於原文意思的問題只給類別，不給發現者的說法）、前後各 5 段 `around`、新譯文被字詞規則標到的 `program_flags`、每句「說話者 → 對象」`lines`、說話者與對象在這段已成立的關係 `relation`（最多 3 對）、省略主詞 `actions`；人物 `characters`（名字、性別、敘述者、口吻、說話習慣）；本批原文的 `glossary`、`address_terms`（和校稿同一套挑法）；`term_definitions` | `checks`：`gist`（先只看原文寫下原文在說什麼）、`verdict`（fixed／issue_invalid／still_wrong／undecided）、`new_error`、`reason` | 有新錯 → 修正不用；程式仍有嚴重發現 → 算 still_wrong；沒回答的算 unchecked | `translations.status`（succeeded／`unneeded_fix`／`rejected_fix`／needs_review）、`qa_findings`（resolved／dismissed／`fix_still_wrong`…） | 匯出（決定哪一版是書的內容）、網頁、可驗收報告 | ADR-055、063、067、070／`review._check_fixes`、`fix_outcome` |
| 16 | 對照校稿 `proofread` | Opus（high） | 每 60 段一個請求（依檔案分段），可同時進行；Claude 方案 2 輪、其他 1 輪，後一輪在前一輪修完並驗收後重看全書 | `paragraphs`：原文 `ja`、目前譯文 `zh`、說話者 `speaker`、對象 `to`、說話者與對象在這段已成立的關係 `relation`（最多 3 對）、省略主詞 `actions`、`translator_notes`；同一個檔案裡前一群的最後 6 段 `context_before`（只讀不查）；這段裡說話或被點名的人 `characters`（名字、性別、敘述者、口吻、說話習慣，最多 30）；`glossary`、`address_terms`、`style`、`term_definitions`；`verdicts` 開著時要每段判斷 | `issues`（類型含 ambiguity、嚴重度、引文、問題、修法）；`checked`（開 verdicts 時） | 段落必須是這次查的、被擋的不算看過；low 不送修；ambiguity 只記下給人看，不重譯 | `qa_findings`（`review:proofread_…`）、`jobs.checkpoint_json`（做到哪、第幾輪） | 修正重譯 → 修正檢查；讀者檢查 | ADR-051、063、065、066、067／`context_pipeline.proofread_pass`、`analysis/proofread` |
| 17 | 讀者檢查 `reader` | Opus（medium） | 每 120 段一個請求，可同時進行 | `paragraphs`：只有中文 `zh`，程式的提示 `look`（譯文留了假名、原文在談字詞本身）；同一個檔案裡前一群最後 6 段的中文 `context_before`；人物 `characters`（名字、性別、敘述者，最多 40） | `issues`：類型（unclear、wordplay、translationese、voice、contradiction、other）、嚴重度、問題 | 段落必須是這次讀的；low 不送修 | `qa_findings`（`review:reader_…`） | 修正重譯 → 修正檢查 | ADR-038、065／`context_pipeline.reader_pass`、`analysis/reader` |
| 18 | 目錄與書名 | Opus（medium，同翻譯） | 目錄、書名等每約 1,400 字一個請求（和章節標題完全相同的直接沿用） | 原文 `units`；已翻好的章節標題 `known_headings`；`glossary`；說明 `note`（沒有記憶、沒有術語定義） | 同翻譯 | 同翻譯 | `translations` | 匯出 | ADR-024／`orchestrator.run_meta` |
| 19 | 匯出 | 無 | 整本 | — | — | 每段取「人工 > 最新的機器譯文（succeeded／needs_review）」且 `source_hash` 相符；只替換驗證過的文字節點；內部檢查＋EPUBCheck | 輸出的 EPUB、報告 | 讀者 | ADR-010、055／`exporter`、`epub/reassemble`、`epub/validate`、`reports/acceptance` |

### 資料流向表

列是前面步驟產生的資料，欄是會讀資料的步驟。「有」＝送到；「部分」＝只送一部分（括號說明少了什麼）；「無」＝這一步的請求裡沒有；「—」＝這一步就是產生或改寫它的那一步。「規則確認」欄是多加的，因為它也判對錯。

| 資料（產生步驟） | 翻譯 | 規則確認 | 審稿 | 修正重譯 | 修正檢查 | 校稿 | 讀者檢查 | 定名 | 體例 | 關係 | 印象 | 覆核 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 人物卡與性別（讀場景、記憶整理、覆核） | 有（場景裡在場、說話或被點名的人：名字、別名、性別、說話特徵、最多 3 句已證實台詞、口吻、彼此關係） | 部分（名字、性別、敘述者） | 有（整個場景的卡，同翻譯） | 有 | 部分（名字、性別、敘述者、口吻、習慣；沒有別名、關係） | 部分（同修正檢查） | 部分（名字、性別、敘述者） | 部分（前 30 人的名字、別名、性別、敘述者） | 有（主要角色） | 部分（兩人的名字、性別、敘述者、別名） | 有（主要角色） | 部分（只有名字；性別題附第一次的答案與證據） |
| 敘述者（讀場景 narration、覆核） | 有（`narration`：人稱、敘述者或焦點人物、自稱；卡上 `narrator`） | 部分（卡上標記；人稱只在規則說明裡） | 部分（只有卡上標記，沒有 `narration`） | 有 | 部分（卡上標記） | 部分（卡上標記） | 部分（卡上標記） | 部分（卡上標記） | 部分（卡上標記） | 部分（卡上標記） | 部分（`narrator` 標記） | 有（人稱衝突、未具名敘述者兩種題） |
| 說話者與對象（讀場景、覆核） | 有（本批每句：說話者、確定程度、對象、對象確定程度） | 部分（說話者；`second_person_gender` 的項目另附對象與性別） | 有（受審段與前後 5 段的說話者；`scene.utterances` 有對象） | 有 | 有（`lines`：說話者 → 對象；前後 5 段的說話者） | 有（`speaker`、`to`） | 無 | 部分（稱呼的誰叫誰，不是逐句） | 部分（只有該角色自己的台詞） | 部分（原文片段標說話者、同場說話次數） | 部分（自己的台詞） | 部分（只有有爭議的台詞） |
| 省略主詞的 actions（讀場景） | 有（`omitted_arguments`） | 無 | 有（只限受審段落） | 有 | 有 | 有 | 無 | 無 | 無 | 無 | 無 | 無 |
| 讀時疑問（讀場景 uncertainties，覆核的回答） | 有（含 `answer`、`answer_who`） | 無 | 有（只限受審段落） | 有 | 無 | 無 | 無 | 無 | 無 | 無 | 無 | 有（`question` 題，問一次） |
| 事實（記憶整理） | 部分（場景開頭以前、依名額挑，見〈名額與挑選〉） | 無 | 部分（同翻譯，整個場景一份） | 部分（同翻譯） | 無 | 無 | 無 | 無 | 無 | 有（每對最多 10） | 有（每人最多 8） | 部分（只有未具名敘述者題，最多 10） |
| 場景摘要（記憶整理） | 有（上一章回顧＋本章最近 4 段） | 無 | 有（同翻譯） | 有 | 無 | 無 | 無 | 無 | 無 | 無 | 無 | 無 |
| 物品（記憶整理 prop_states） | 有（場景提到的物品與持有者） | 無 | 有（同翻譯） | 有 | 無 | 無 | 無 | 無 | 無 | 無 | 無 | 無 |
| 稱呼表（記憶整理、定名） | 部分（依名額） | 無 | 部分（依名額，同翻譯的挑法） | 部分 | 部分（依名額，同校稿的挑法） | 部分（依名額） | 無 | 有（還沒決定的，最多 60） | 部分（只有稱呼與次數，不分誰叫誰） | 有（兩人之間） | 部分（這人怎麼叫別人，最多 6） | 部分（`identity`、`narrator` 題附別人怎麼叫他） |
| 關係（人物關係；讀時記錄 relation_notes） | 有（在場兩人之間、場景開頭已成立的 `relations`） | 無 | 有（同翻譯） | 有 | 部分（只有本段說話者與對象，最多 3 對） | 部分（同修正檢查） | 無 | 部分（稱呼雙方的現行關係，最多 4 條） | 無 | 部分（讀時記錄當材料；上一輪模型的結論不送） | 有（最多 6） | 無 |
| 角色印象（印象） | 部分（只有口吻 `voice`；個性、定位不送） | 無 | 部分（`voice`） | 部分（`voice`） | 部分（`voice`） | 部分（`voice`） | 無 | 無 | 部分（各段的 `voice`） | 無 | — | 無 |
| 說話習慣與體例（體例） | 有（敬稱方針、規則、譯註、本批或在場者的習慣） | 無 | 有 | 有 | 部分（只有卡上的說話習慣，沒有敬稱方針與規則） | 有（`style`＋卡上習慣） | 無 | 部分（敬稱方針、規則、譯註；沒有說話習慣） | — | 無 | 無 | 無 |
| 譯名（讀場景、記憶整理、定名） | 有（本批與前後文出現的詞、已揭曉的其他形式） | 部分（只在 `possible_added_name` 的項目：本段與前後文的人名） | 有 | 有 | 有（本批原文出現的，同校稿） | 有 | 無 | — | 有（前 80 條） | 部分（只有人物的中文名） | 部分（只有人物的中文名） | 部分（只有人物的中文名） |
| 術語定義（術語） | 有 | 無 | 有 | 有 | 有 | 有 | 無 | 部分（不是人名的名字；模型或人寫的定義） | 無 | 無 | 無 | 無 |
| 讀時發現的文字遊戲（記憶整理 reader_notes） | 有（本批段落的） | 無 | 無 | 有 | 無 | 無 | 無（只有程式的 `look` 提示） | 無 | 無 | 無 | 無 | 無 |
| 譯者旗標與筆記（翻譯） | — | 部分（三種要確認的旗標，筆記寫在說明裡） | 有（`flags` 理由、`translator_notes`） | 部分（只有轉成問題的那些，在 `review_feedback` 裡） | 無 | 有（目前那一版的 `translator_notes`） | 無 | 無 | 無 | 無 | 無 | 無 |
| 修正前譯文（同一段的前一版） | 無 | 無 | —（看的就是目前譯文） | 有（`current_translation`） | 有（`before` 與 `after`） | —（看目前譯文） | —（看目前譯文） | 無 | 無 | 無 | 無 | 無 |

一眼看出的缺口（都是程式現況，不是判斷該不該送）：
- **審稿**拿不到文字遊戲，也沒有 `narration`（只有卡上的敘述者標記）——它的 payload 從 `batch_context` 挑 `characters`、`scene`、`glossary`、`address_terms`、`style`、`term_definitions`、`memory`。
- **修正檢查**拿不到敬稱方針與規則、事實、摘要；關係只有本段說話的兩人。
- **校稿**拿不到事實、摘要、疑問、文字遊戲、`narration`；關係只有每段說話的兩人。
- **讀者檢查**只有中文、名字、性別、敘述者——刻意的：它模擬不懂日文的讀者。
- **規則確認**只拿判斷那條規則需要的：補了人名附人名譯名、你／妳附對象與性別；其他規則沒有附加資料，也沒有稱呼、體例。
- **定名**拿不到事實、角色印象。
- **文字遊戲**只到翻譯與修正重譯；**角色印象的個性與定位**沒有任何判錯或翻譯的步驟拿到——刻意的：可能含劇情，對逐句翻譯幫助小。

### 可見範圍（防劇透）

程式只用「位置」控制可見：`memory/visibility.py` 以（集數, 段落序號）比較。人物從第一次出現起可見；名字的其他形式要等它出現、而且連到這個人的揭曉（`entity_links.revealed_ordinal`）也到了才顯示（`character_view`、`visible_names`）；性別要等證據位置；事實、摘要、物品、已證實台詞要「嚴格早於」起點；撤回要撤回的那筆也早於起點才生效。

| 步驟 | 取到哪裡為止 |
|---|---|
| 切章與切場景 | 只看這一章的原文，沒有記憶 |
| 讀場景 | 人物、關係、自稱：**場景開頭以前**；前情摘要：場景開頭以前；上一場的結尾 |
| 記憶整理 | 人物：**場景結尾**（讀場景剛建的人要能引用）；舊事實：場景開頭以前 |
| 覆核、人物關係、角色印象、體例、術語、定名 | **全書**（讀完才做；定名拿到的術語定義也看到全書）。產出本身帶位置：關係從最早的證據開始（`since_ordinal`，被取代時有 `until_ordinal`）；印象各段有開始位置，個性與定位要到最晚的證據才顯示（`revealed_ordinal`）；術語定義從最晚引用的證據才顯示；名字的揭曉位置照舊。體例沒有位置，全書一體適用 |
| 翻譯、修正重譯 | 人物卡、名字、性別、`narration` 的名字、譯名的其他形式、術語定義：**本批最後一段**；事實、摘要、物品、已證實台詞、關係、口吻：**場景開頭**；稱呼：場景開頭以前用過的，加上已決定譯法、在本批出現的（到本批最後一段） |
| 規則確認 | 人物：本場最早那個旗標的段落；說話者名字、對象的名字與性別、人名譯名：各段自己的位置 |
| 審稿 | 人物卡、稱呼、譯名、術語：**場景結尾**（整個場景的 context）；事實、摘要、物品：**場景開頭**（同翻譯）；說話者名字：各段自己的位置 |
| 修正檢查 | 人物：這批修正中最早那段；譯名、稱呼：最早那段起、最晚那段為止（同校稿的群頭、群尾）；術語定義：各段自己的位置；關係：各段自己的位置（已開始、還沒被取代）；說話者、對象、省略主詞與關係裡的名字：該場景結尾 |
| 對照校稿 | 人物卡：**這一段落群的開頭**（第一次出現在群內的人用群尾）；譯名、稱呼、術語：群尾；關係：各段自己的位置；說話者、對象、省略主詞與關係裡的名字：該場景結尾 |
| 讀者檢查 | 人物：到群尾為止出現的人，名字用群開頭時的（之後才出現的用群尾） |
| 目錄與書名 | 譯名：到這些段落的位置（目錄、書名在書末，等於全書） |

### 名額與挑選

有上限的（數字是程式常數或設定預設值）：

| 資料 | 送到 | 上限 | 怎麼挑 |
|---|---|---|---|
| 事實 | 翻譯、修正重譯、審稿 | 每 25 段 5 筆（`memory.max_retrieved_evidence`），最多 4 倍（20 筆）；記憶區塊另有字數預算（每 25 段 2,400 字，最多 8,000），超過時先丟最舊的事實、再丟摘要 | 候選：主體在場、主體字串出現在本批、或在場者名字出現在主體／受詞；和人無關的世界事實要字面重疊更高（+0.5）。分數＝本批與事實證據句的有意義雙字重疊（除以證據句長度的平方根）＋主體被本批點名 1.5＋越晚越多一點（≤0.3），分數 ≥1 才留；取分數最高的 N 筆，再依故事順序排（`retrieval.select_facts`、`batch_quota`、`memory_chars`） |
| 舊事實 | 記憶整理 | 30 筆 | 同樣的重疊打分，關於本場的人 +1（`memory_update.known_facts`） |
| 稱呼 | 翻譯、修正重譯、審稿、校稿、修正檢查 | 每 25 段 12 個，最多 4 倍（校稿一群 60 段算 36 個） | 本批原文出現的稱呼，加上本批說話者怎麼叫在場的人；超過時留用得最多的，順序照第一次使用（`address.context_items`） |
| 摘要 | 翻譯、修正重譯、審稿 | 本章最近 4 段；上一章回顧 1,200 字內 | 依故事位置，不看字面（`retrieval.story_so_far`） |
| 摘要 | 讀場景 | 本章最近 3 段；上一場最後約 400 字 | 同上 |
| 已證實台詞 | 翻譯的人物卡 | 每人 3 句 | 只要 explicit／人工確認的，人工優先、越近越先 |
| 人物 | 校稿、修正檢查、讀者檢查、規則確認、定名、印象、體例、關係 | 校稿、修正檢查 30；讀者檢查、規則確認 40；定名 30；印象與體例 12 個主要角色；關係 40 對 | 校稿：說話者、對象與段落裡點到名字（2 字以上）的人；主要角色：台詞 3 句以上依台詞數，敘述者優先 |
| 關係（說話的兩人） | 校稿、修正檢查 | 每段 3 對，每對最多 4 句 | 這段台詞的說話者與對象，依台詞順序；在該段已開始、還沒被取代的（`repository.line_relations`） |
| 關係材料 | 人物關係 | 每對讀時記錄與事實各 10、原文片段 3＋6、稱呼 10 | 讀時記錄同一種關係留最早與最晚；原文片段「同段點名或被談論」優先，從全書頭尾往中間取 |
| 印象材料 | 角色印象 | 台詞 8 句、事實 8、稱呼 6、關係 6 | 台詞平均分散全書 |
| 體例材料 | 體例 | 台詞 12 句（＋每個重複句尾 1 句）、稱呼 40、譯名 80 | 台詞平均分散全書；句尾是程式數的（2～4 字、出現 4 次以上） |
| 術語 | 術語 | 60 個詞、每個例句 3、事實 5，每請求 20 個 | 次數最多的；例句與事實分散全書 |
| 術語補漏 | 術語 | 事實 400 筆、摘要全部；收下最多 20 個詞 | 事實分散全書 |
| 定名 | 定名 | 名字 80、稱呼 60 | 名字依出現次數、稱呼依使用次數 |
| 覆核題目 | 覆核 | 台詞、性別、合併、人稱、疑問合計 150 題（其他種類先、台詞後）；未具名敘述者與身分、群體題另計；每請求 25 題 | 答過、資料沒變的不再問（`verify_answers` 的指紋） |

沒有上限的：翻譯的人物卡（場景裡在場、說話或被點名的人全部）、卡上的關係、本批的說話者、省略主詞的句子、疑問、物品、文字遊戲、譯名、術語定義。

**譯名與術語定義依字串比對，不靠打分**：本批原文（翻譯另含前後文）裡出現這個詞，就附上（`cache.relevant_glossary`、`memory/terms.definitions_for`，都先經 `match_text`）；可見位置之前不附。字串比對會把包含關係也算進去（詞 A 是詞 B 的一部分時，B 出現的段落也附 A）。

### 附註

- 先讀再翻的第一次跑，體例請求的台詞沒有 `zh`、記憶整理的 `units` 沒有 `translation`：程式會附「目前的譯文」，但那時還沒翻。只有重讀或交錯模式才有。
- 修正重譯的 payload 和翻譯一樣有 `memory`、`narration`、`wordplay`（它走同一個 `batch_context`）；審稿現在也有同一份 `memory`，但仍沒有 `narration`、`wordplay`，所以修正重譯拿到的還是比審稿多這兩項。

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
稱呼（address_terms、address_observations、form_senses）、定名提案（naming_proposals）、譯註（reader_notes）、性別（character_gender，migration 12–13）、人物關係（relations、讀時記錄 relation_notes，migration 16–18）、角色印象（character_impressions）、術語的意思（term_definitions，migration 31）、群體成員（group_members，migration 20–21）、場景切點（scene_breaks）與章節（chapters，ADR-057）、覆核答過的題目（verify_answers）。
