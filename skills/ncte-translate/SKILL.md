---
name: ncte-translate
description: Translate a Japanese EPUB (light novel, novel) into Taiwan Traditional Chinese with the NCTE engine on the user's Claude plan or with you as the model — no API key. Use when the user asks to translate a Japanese EPUB, to continue such a translation, or to compare models on it.
---

# ncte-translate

NCTE does the bookkeeping a careful translator does — reads the whole book first (scenes, who speaks to whom,
genders, relations, names), fixes renderings, translates scene by scene with that memory, then proofreads against
the source and checks every fix. Install it once from its checkout: `pip install -e '.[agent]'`.

## Usually: let NCTE call Claude itself

```bash
NCTE_LLM_PROVIDER=claude ncte translate BOOK.epub --mode context   # or: ncte gui → 設定 → Claude 方案
ncte export BOOK.epub
```

Each request is one fresh `claude -p` call on the user's Claude login (reading on Sonnet, translating and every
check on Opus; each step at its own effort). Nothing is billed; what each call would cost at list price is kept in
`llm_requests.usage_json`. A whole book takes hours; a reached usage limit is waited out. Run it in the background
and tell the user how to follow it (`ncte status BOOK.epub`, or the web UI). Read `/usage` before and after if the user wants
to know the quota it used.

## You as the model (`ncte agent`, or the MCP tools of the same names after `claude mcp add ncte -- ncte mcp`)

Every request the engine would send becomes a task, and you answer it. The engine checks each answer (schema,
unit ids, evidence) exactly as it checks a model's answer and asks again with a correction when it does not fit.

| step | command |
|---|---|
| start | `ncte agent start BOOK.epub -w WORKSPACE [--limit-units N]` |
| next task | `ncte agent next -w WORKSPACE` → prints `task_id`, `stage`, `effort`, and a `file` with the instructions, input and schema |
| answer | write ONE JSON object to a file, then `ncte agent submit -w WORKSPACE TASK_ID answer.json` |
| progress | `ncte agent status -w WORKSPACE` |
| finish | `ncte agent export OUT.epub -w WORKSPACE` |
| pause / continue | `ncte agent stop -w WORKSPACE` / `ncte agent resume -w WORKSPACE` |

1. `start` once (one workspace per book, and per model when comparing models).
2. Repeat until `next` reports no task **and** `status` shows `run_status` `completed` (or another final status):
   `next`; if the engine is working, wait a few seconds and ask again; answer the task; `submit`; if it lists
   problems, fix the JSON and submit again.
3. `export`.

Answering a task:
- Read the task file whole. Its **Instructions** are that step's system prompt; its **Input** is the request.
  Answer exactly as the instructions say: one JSON object, no prose, no code fence. Think at the task's `effort`.
- Each task is independent. Judge only from the instructions and the input — the engine gives you the context it
  has (earlier paragraphs, characters, genders, glossary, who speaks to whom, only what is known at that point of
  the book). Do not use what you know of the book from elsewhere, and do not carry conclusions between tasks.
- Ids in the input are short codes (P1, C2 …): answer with the same codes. Optional fields may be left out.

To use other models for the tasks, hand each task to a fresh subagent with that model (one task per subagent).
This costs far more than the `claude` provider (a subagent's own context is tens of thousands of tokens per task),
so prefer the provider unless the user wants an agent in the loop.
