# Content Discovery Agent: investigation of copied Agent P code

Scope: read-only review of every file in this folder. Nothing run, nothing changed, original Agent P not touched. No manifest, `.env`, `tests/`, or deploy config was copied.

## Current architecture

- **Stack** (inferred from imports): Python, FastAPI (`main.py`), LangGraph, Groq via `langchain_groq`, Supabase REST via httpx, Composio (X and LinkedIn posting), feedparser.
- **Pipeline** (`agent/graph.py`): `research -> select -> generate -> approval -> (rejected: back to research, max 3 retries) -> END`.
- **Entry points** (`main.py`):
  - `GET/POST /run?target=` starts a run in a daemon thread. No auth.
  - `/news` runs the digest. Guarded by `TRIGGER_SECRET`.
  - `/telegram-webhook` handles idle commands. Secured by a hash of `TRIGGER_SECRET`.
  - `/` health check.
  - `__main__` calls `run_agent()`.
  - `python -m agent.news --send` sends the digest.
- **Approval** (`agent/nodes/approval.py`): Telegram long-poll with inline buttons and a reminder ladder (about 2 hours). Also handles `/generate /x /li /repurpose /reply /latest /help`. Terminal fallback when no token is set.
- **Persistence**:
  - Supabase tables `seen`, `kv`, `feedback` (`agent/store.py`, `docs/supabase.sql`).
  - Local files in `data/`: `approved_tweets.md`, `last_run.json`, `linkedin_rules.txt` (written by optimizer).
- **Scheduling**:
  - `scripts/scheduler_setup.py` creates Windows `schtasks` jobs named `TwitterAgent_HHMM`.
  - `docs/SPEC.md` describes an external Hostinger cron hitting `/run` and `/news` on Render. The cron is not in the code.

## Environment variables (all read in code)

- Groq: `GROQ_API_KEY`, `GROQ_FAST_MODEL`, `GROQ_DEEP_MODEL`
- Telegram: `TELEGRAM_BOT_TOKEN` (approval bot, "AgentX"), `AGENTP_BOT_TOKEN` (digest bot, "AgentP"), `TELEGRAM_CHAT_ID`, `TRIGGER_SECRET`, `RENDER_EXTERNAL_URL`
- Supabase: `SUPABASE_URL`, `SUPABASE_KEY` or `SUPABASE_SERVICE_ROLE_KEY`
- Composio / publishing: `COMPOSIO_API_KEY` (SDK reads it), `COMPOSIO_USER_ID` or `LINKEDIN_USER_ID`, `LINKEDIN_ACCOUNT_ID`, `X_ACCOUNT_ID`, `LINKEDIN_AUTHOR_URN`
- Scheduler script: `TWEET_BEST_TIMES`

## Reusable components

| Component | File | Notes |
|---|---|---|
| Source fetchers | `agent/nodes/research.py` | HN, GitHub, HF models and papers, arXiv, Google News, RSS. `_safe` and `_gather` are generic. |
| LLM helpers | `agent/llm.py` | `ask`, `ask_json` (strict schema, plain-text fallback), model ids from env. |
| Dedup | `select._dedup`, `_norm_url` | `history` arg is tied to posted tweets. |
| Store | `agent/store.py` | Generic `seen` / `kv` / `feedback` pattern. Helpers are domain-neutral. |
| Telegram transport | `approval.py` | Owner check, webhook set/delete, long-poll, keyboard, command parse, `_send`. Entangled with draft logic. |
| Digest | `digest.py`, `news.py` | Chunking, HTML escape, send with token redaction, heat scoring, curate. |
| Paths | `agent/paths.py` | |
| Graph skeleton | `graph.py`, `state.py` | |

## Components to replace or modify

- **`generate.py`**: tweet and LinkedIn writing, AI-tells regex, `_ungrounded` / `quality`, hook formulas, og:image scrape. Output (post draft) is the wrong product for discovery. Rewrite as idea generator.
- **`publish.py`**: Composio X and LinkedIn posting. Discovery agent should not post. Drop.
- **`approval.py`**: tweet/LinkedIn semantics (`Post to X`, `/x`, `/li`, `/repurpose`, `/reply`, edit tweet, `y/n` publish). Keep only transport and owner check.
- **`select.py`**: `RANK_PROMPT` targets "AI-engineering audience". `HISTORY_PATH` = `approved_tweets.md`. Retarget.
- **`research.py`**: `research_node` hardcodes AI/tech mix. `_clean_query` assumes reject feedback.
- **`news.py`, `digest.py`**: India-tech morning briefing specific (IST, India section, `EDITOR_PROMPT`). Replace or adapt if output is a briefing.
- **`state.py`, `graph.py`**: fields `target`, `selected_draft`, `linkedin_draft`, `selected_image`; retry loop.
- **`main.py`**: `/run?target=` semantics, `/news` route, `INITIAL_STATE`, `/repurpose` shortcut.
- **`docs/supabase.sql`**: `feedback` columns `tweet_before`, `tweet`, `linkedin` are post-specific. Generic table names collide if the same Supabase project is reused.
- **`scripts/optimize_linkedin.py`**: LinkedIn-only DSPy tuning. Drop.
- **`scripts/scheduler_setup.py`**: task prefix and tweet slot times.
- **`data/`**: `approved_tweets.md` holds one real approved Agent P post. `linkedin_post_template.md` holds personal reference posts. Neither belongs here.

## Isolation risks (ranked)

1. **Shared Supabase** (high). Fixed table names, no namespace. Same URL/key means shared `seen` (`posted`/`digest`), `kv` (`last_run`), `feedback`. Agent P's dedup history and `/latest` / `/repurpose` state get overwritten. Use a **new Supabase project**.
2. **Shared Telegram bot tokens** (high).
   - `ensure_webhook()` runs at startup and calls `setWebhook`. Using Agent P's `TELEGRAM_BOT_TOKEN` steals its webhook.
   - Two `getUpdates` pollers on one token race and return 409.
   - Same for `AGENTP_BOT_TOKEN`.
   - Create a **new bot** via @BotFather.
3. **Webhook secret derived from `TRIGGER_SECRET`** (medium). Reusing the secret makes webhook auth identical across apps. Generate a fresh one.
4. **Composio / X / LinkedIn credentials** (high if copied). An approval in the copy publishes publicly. Do not provide these; delete `publish.py`.
5. **`RENDER_EXTERNAL_URL`** (medium). If set to Agent P's URL, webhook and Generate button point at Agent P's service. Give the copy its own.
6. **Unauthenticated `/run`** (medium). Burns Groq quota; sends into `TELEGRAM_CHAT_ID`. Add a secret like `/news`.
7. **Scheduled jobs** (medium).
   - `scheduler_setup.py` creates `TwitterAgent_*` tasks that run `main.py` from its own directory. Same prefix as Agent P would be overwritten by `install` and deleted by `remove`.
   - Rename prefix before running it.
   - External cron entries must point at the new URL.
8. **Shared Groq key** (low). Splits rate-limit budget. Digest is token-heavy. Prefer a separate key.
9. **`TELEGRAM_CHAT_ID`** (low to medium). Sharing a chat with distinct bots is workable but confusing. Prefer a separate chat.
10. **Local `data/` copies** (low). Not shared with the original, but carry Agent P's posted history and LinkedIn style. Clear for a clean start.

## Missing dependencies and config

- **No dependency manifest.** From imports: `fastapi`, `uvicorn`, `langgraph`, `langchain-groq`, `httpx`, `feedparser`, `python-dotenv`, `composio` (only if kept). `dspy` is dev-only. `SPEC.md` mentions Python 3.12 and `uv`.
- **No `.env` / `.env.example`.** Create fresh with new values only.
- **No `tests/`.** `agent/news.py` docstring references `tests/test_agent.py`, which was not copied. `__main__` self-checks exist in several files.
- **No deploy config** (`render.yaml` or equivalent). `main.py` assumes Render.
- **New Supabase project** needs `docs/supabase.sql` run, or the store silently no-ops.
- Run from project root.

## Minimum changes for an independently runnable agent

1. New Telegram bot, new Supabase project, new `TRIGGER_SECRET`, separate Groq key if possible.
2. Fresh `.env` with only the variables the new design needs. No Composio/LinkedIn vars. No `RENDER_EXTERNAL_URL` locally.
3. Add a manifest (`pyproject.toml` or `requirements.txt`).
4. Clear `data/approved_tweets.md` and `data/last_run.json`. Drop `linkedin_post_template.md`.
5. Delete or disable `publish.py`, the `/x` `/li` `/repurpose` `/reply` paths, and `optimize_linkedin.py`.
6. Rename `TwitterAgent_` task prefix.
7. Retarget `select.py` rank prompt; replace `generate.py` with an idea generator.

## Recommended implementation order

1. **Isolate**: new `.env`, bot, Supabase, secrets, manifest. Confirm boot with no Composio keys.
2. **Verify plumbing**: run `llm.py`, `select.py`, `research.py` `__main__` checks (public APIs + Groq only).
3. **Strip posting**: remove `publish.py` and publish branch in `approval.py`.
4. **Define output**: what a "content idea" is (topic, angle, hook, source links). Update `state.py`; add idea node.
5. **Retarget discovery**: `research_node` sources, topic, `select.py` prompt.
6. **Approval**: simplify to approve / reject / save; log to store.
7. **Store schema**: adjust `supabase.sql` columns and table names in the new project.
8. **Schedule**: new cron entry or renamed `schtasks` set.
9. **Rewrite self-checks** for new behaviour.

## Files likely to change

- **Heavy rewrite or removal**: `agent/nodes/generate.py`, `agent/publish.py`, `agent/nodes/approval.py`, `scripts/optimize_linkedin.py`, `data/*`
- **Retarget**: `agent/nodes/select.py`, `agent/nodes/research.py`, `agent/state.py`, `agent/graph.py`, `main.py`, `docs/supabase.sql`, `agent/store.py` (only if tables renamed)
- **Optional**: `agent/news.py`, `agent/digest.py` (if output is a briefing)
- **Minor**: `scripts/scheduler_setup.py` (prefix)
- **Unchanged**: `agent/llm.py`, `agent/paths.py`
- **New**: `.env`, manifest, README

## Open questions before step 4

- What should a "content idea" look like, and where should it land (Telegram, Supabase, file)?
- Daily digest still wanted, or on-demand idea generation only?
