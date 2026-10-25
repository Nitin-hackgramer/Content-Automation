# Content Discovery Agent

Independent copy of the Agent P news/posting codebase, prepared for content discovery. It has its own
Telegram bot, Supabase project and Groq key. It does not post to X or LinkedIn. The content
discovery workflow is not built yet; what runs today is the inherited research, draft, and
approval pipeline.

## Setup

```
uv venv --python 3.12 .venv
.venv\Scripts\activate
uv pip install -r requirements.txt
copy .env.example .env      # then fill in every value
```

Run the SQL in `docs/supabase.sql` once in this project's Supabase (SQL Editor).

### Seen-filter setup (so ideas do not repeat across days)

Discovery remembers the ideas it showed. The ledger is the Supabase table `discovery_seen`. A local file is a
fallback, used only when Supabase cannot be used, and every run says which one it used:

| Ledger | Needs | Shared across machines |
|---|---|---|
| Supabase table `discovery_seen` | the SQL above, plus `SUPABASE_URL` and `SUPABASE_SECRET_KEY` in `.env` (the secret key, never the anon key) | yes |
| `data/discovery/seen_ideas.json` (fallback) | nothing | no, this machine only |

```
.venv\Scripts\python -m agent.discovery.seen           # which ledger is in use (prints no keys)
.venv\Scripts\python -m agent.discovery.seen --test    # live proof: write, read back, delete, confirm gone
```

The first command only shows that the tables answer. Only `--test` passing proves writes persist.
The run line reads `Seen-filter: SUPABASE (shared ledger)` or `Seen-filter: LOCAL FALLBACK` with the reason:
`off` (variables not set), `tables_missing` (run `docs/supabase.sql`), or `error` (for example the key was
rejected, or a read or write failed mid-run). Ideas found only in the fallback file are copied into Supabase once it
works, so nothing already shown comes back. If neither ledger can be written, the run warns that ideas will repeat.

## Configuration

Only this folder's `.env` is read (`agent/paths.py`). Variables already set in the shell do not override it.

| Variable | Required | Used for |
|---|---|---|
| `TELEGRAM_BOT` | for Telegram | bot token: approvals, digest |
| `TELEGRAM_CHAT_ID` | for Telegram | the one chat allowed to drive the bot |
| `TRIGGER_SECRET` | **required before any deployment** | guards `/news` (header `X-Trigger-Secret` or `?key=`) and the Telegram webhook. If unset, the webhook accepts any POST, so an unset value is not safe on a public URL |
| `GROQ_API_KEY` | for LLM steps | ranking, drafts, editor. Without it, plain fallbacks run |
| `SUPABASE_URL`, `SUPABASE_SECRET_KEY` | for memory | tables are prefixed `discovery_`. Without them, local files are used |
| `RENDER_EXTERNAL_URL` | deployment only | Telegram webhook base URL |

## Test locally (no deploy, no recurring jobs)

Offline self-checks (Telegram and Supabase stubbed):

```
.venv\Scripts\python -m agent.store
.venv\Scripts\python -m agent.nodes.select
.venv\Scripts\python -m agent.nodes.generate
.venv\Scripts\python -m agent.nodes.approval
```

Live checks (read public feeds and Groq; use the real keys):

```
.venv\Scripts\python -m agent.nodes.research
.venv\Scripts\python -m agent.llm            # confirms the Groq model ids on this key
.venv\Scripts\python -m agent.news           # prints usage
.venv\Scripts\python -m agent.news --send    # builds a real digest and sends it to TELEGRAM_BOT
```

Import check for the app (starts nothing):

```
.venv\Scripts\python -c "import main; print('ok')"
```

Run the server only for manual checks. Startup registers no webhook unless `RENDER_EXTERNAL_URL` is set:

```
.venv\Scripts\python -m uvicorn main:app --port 8000
```

## Content discovery

Finds current AI and developer stories, groups duplicates, scores them for short-video potential,
and optionally writes a short-video brief for each chosen idea. It posts nothing and downloads no assets.

```
.venv\Scripts\python -m agent.discovery.run                  # top 10, saved to data/discovery/*.json
.venv\Scripts\python -m agent.discovery.run --top 5 --briefs 2   # plus briefs (needs GROQ_API_KEY)
.venv\Scripts\python -m tests.test_discovery                 # offline tests (no network, no Groq)
.venv\Scripts\python -m tests.test_reach                     # offline Agent Reach adapter tests (CLI mocked)
.venv\Scripts\python -m tests.test_quality                   # offline claim, ranking and brief tests
.venv\Scripts\python -m tests.test_seen                      # offline Supabase/fallback ledger tests (fake PostgREST)
.venv\Scripts\python -m tests.test_verify                    # offline brief-accuracy tests (fake lenient model)
.venv\Scripts\python -m tests.test_trends                    # offline trend tests, incl. an 8-day simulation (fake PostgREST)
.venv\Scripts\python -m tests.test_web_limits                # offline web rate-limit, spacing and cache tests
.venv\Scripts\python -m tests.test_service                   # offline jobs, saved ideas and service tests
.venv\Scripts\python -m tests.test_telegram                  # offline Telegram front-end tests (fake Telegram, network blocked)
```

Stages, each a separate module:

| Stage | File | Does |
|---|---|---|
| Fetch | `agent/discovery/sources.py` | OpenAI, Google AI, Hugging Face and GitHub changelog feeds; Hacker News, GitHub, dev.to, Hugging Face models and Google News searches. Reports `ok:<n>` or `error:<reason>` per source. |
| Fetch | `agent/discovery/reach.py` | Agent Reach CLI: X search, YouTube search and channel uploads (dated by `youtube read`), web search. Keyword search only, never "trending". Needs `agent-reach` on PATH; a missing or failing channel is reported, not fatal. |
| Dedup | `agent/discovery/cluster.py` | Groups stories by canonical URL or title words. Records the evidence (`multi_source`, `engagement`, `single_source`) and who backs the story: `claim_status` is `first_party` (the organisation's own account, feed or page: it established that the organisation said it, not that it is true), `corroborated` (two independent voices; affiliated accounts, reposts and quote tweets do not count) or `unverified`. `needs_verification` marks an unverified story that claims company news; the ranking model decides that, with a keyword guess as the fallback. |
| Rank | `agent/discovery/rank.py` | Audience, relevance, video fit and effort from the LLM (prompt favours beginner explainers, coding agents, practical tools, developer experiences, product stories); timeliness and credibility from code. Hard score caps for single-source, unverified claims, engagement bait and minor changelog or admin entries. The `penalties` field says which applied. |
| Briefs | `agent/discovery/scripts.py` | Hook, beats, on-screen text, `verified_facts` vs `claims_to_verify`, sources and credit line (from the evidence list, not the model). Too little evidence: rejected, or a cautious brief. |
| Audit | `agent/discovery/verify.py` | Every sentence of a hook or brief is checked against the source text. The model labels each one and quotes its support; code decides: the quote must be in the evidence, numbers, units and acronyms must be too, claims and superlatives must say who claims them, plans must not become facts, no past experience for the creator. Failing statements are removed and listed in `brief["audit"]`; a brief with too few beats is rejected; if the audit cannot run it is marked `unaudited` and hooks are dropped. |
| Remember | `agent/discovery/seen.py` | Drops ideas shown in the last 14 days (same link, or same story under a new link). Supabase is the ledger; a local file is an explicitly reported fallback. |
| Trends | `agent/discovery/trends.py` | Stores one observation per topic per day (Supabase `discovery_topic_obs`; local fallback, reported) and classifies each topic `new`, `rising`, `popular` or `insufficient_data` with the evidence behind it. See below. Annotates ideas; does not change ranking. |
| Wiring | `agent/discovery/run.py` | Runs the stages and saves JSON. |
| Service | `agent/discovery/service.py`, `jobs.py`, `saved.py` | What the chat commands do, with no Telegram in it: background jobs (one per key, a stuck one is abandoned), saved ideas (Supabase `discovery_kv`, local fallback reported), confidence, pagination. |
| Telegram | `agent/telegram_discovery.py` | The only file that knows Telegram: `/discover`, `/trending`, `/ideas`, `/brief <id>`. See "Telegram control" below. |

### Telegram control

Four commands on the project's own bot, answered only to the owner chat (`TELEGRAM_CHAT_ID`):

| Command | Does |
|---|---|
| `/discover [n]` | Runs the whole pipeline in the background (about 3-4 minutes) and sends the top `n` ideas (default 10, max 15) when it finishes. It answers at once, a second `/discover` while one runs just reports the running one, and the other commands keep working meanwhile. |
| `/trending` | Topics that are new, rising or popular, each with the evidence behind the label and up to three links. If nothing qualifies yet it says why (see Trend detection). |
| `/ideas` | Recently saved ideas, newest run first. |
| `/brief <id> [new]` | The brief for an idea. A saved brief comes back at once; otherwise one is written (about a minute, uses the model key) and sent. `new` rewrites it. A long brief arrives in numbered parts. |

Lists come three ideas to a page with Prev/Next buttons (the message is edited in place) and a Brief button per idea. Each idea shows:
a linked title, its short id, score, **confidence**, **trend**, **warnings**, and up to three source links.

- **Confidence** is how well the story is backed, not how good a video it would make: `high` = the organisation's own post or two independent
  sources (for a first-party post that means it really said it, not that its claims are true), `medium` = one source with measured
  engagement, `low` = one source with no traction, or an unverified claim about an announcement.
- **Trend** is the classification from Trend detection with its one-line reason, or "not enough history yet".
- **Warnings** (at most three, most important first): an unverified claim, what capped the ranking, and checks that removed or dropped
  wording. A brief shows its audit status (`VERIFIED`, `TRIMMED`, `REJECTED`, `UNAUDITED`), how many statements were removed, and a
  "check before filming" list.

Local setup (no public URL, nothing deployed):

1. `.env` needs `TELEGRAM_BOT` (this project's own bot from @BotFather, never another app's) and `TELEGRAM_CHAT_ID`. For ranking, claim checks and
   briefs also `GROQ_API_KEY`; for ideas and history that survive a restart also the Supabase settings and `docs/supabase.sql`.
2. Message your bot once in Telegram, then `.venv\Scripts\python -m agent.telegram_discovery --chat-id` prints your chat id. It prints no token
   and no message text. Put the `private` one in `.env`.
3. `.venv\Scripts\python -m agent.telegram_discovery` polls for commands until Ctrl+C. Send `/discover`.
4. It refuses to start while a webhook is set for the bot (a deployed instance owns the updates), and only one poller can run at a time.

Deployed: `main.py` already routes `/telegram-webhook`; discovery commands are tried first, everything else goes to the existing handler.
That needs `RENDER_EXTERNAL_URL` and `TRIGGER_SECRET`; nothing here deploys it.

Limits: jobs live in the server process, so a restart forgets a run in progress (finished ideas are saved and survive). While a draft is
waiting for approval the existing code switches to long polling and removes the webhook, so discovery commands are not answered until it
ends. Telegram is never given the token in a log or an error message: a failed call logs the method and a status code only.
Offline tests (a fake Telegram, no network, and a guard that fails if anything tries to connect):
`python -m tests.test_telegram`, `python -m tests.test_service`.

### Trend detection

```
.venv\Scripts\python -m agent.discovery.trends          # where history is kept, and which topics are new/rising/popular now
.venv\Scripts\python -m agent.discovery.trends --test   # live schema check on Supabase (needs docs/supabase.sql applied first)
```

Setup: run `docs/supabase.sql` again (it now also creates `discovery_topic_obs`; safe to run twice). Until then the run prints
`Trends: LOCAL FALLBACK` and keeps history in `data/discovery/topic_obs.json`; it is copied into Supabase once the table exists.

| Class | Meaning | Needs |
|---|---|---|
| `new` | first seen today, and the tracker ran on 3+ earlier days without seeing it | 3 earlier run-days. Says "not seen before", never "growing" |
| `rising` | since a recent reference sighting, more independent sources carry it, or the same links gained engagement | 2+ sightings on different days |
| `popular` | seen on 3+ days and carried by 2+ independent sources on 2+ of them, and not rising now | 3 days of history |
| `insufficient_data` | anything else; the explanation says what is missing | |

What does not count: growth from a single snapshot; raw engagement; a spike in one post (engagement gain must be x1.5, at least a
per-metric minimum such as +200 likes, 12+ hours apart, and not mostly one link); affiliated voices (an organisation's feed, its own
accounts and reposts of them are one source); metrics that are missing, of a different kind, or on one day only. Day one classifies
everything `insufficient_data`; `new` first appears on day four; `rising` and `popular` need real repeat sightings. Thresholds are
knobs at the top of `trends.py`, set by eye, not calibrated.

Honest limits:
- "Trending" is never claimed without evidence. Most current items are `single_source`, which is the honest
  reading of a single snapshot. Only multi-source coverage or a measured number moves an item up that path.
- Hooks are drafts. Check the claims list and the source before filming.
- Reddit returns HTTP 403 from this machine, so it is not used. Anthropic's news page has no feed, so it is not used.
- The model may skip candidates; the run reports how many it scored. Candidates outside the top 20 by timeliness and credibility are not sent to the model.
- Cross-day dedup works without Supabase, but only on this machine (fallback file). The run says `LOCAL FALLBACK` until the tables from `docs/supabase.sql` exist.
- The audit checks statements against what the sources said, not against the world, and it depends on a model for judgement
  calls (is this a claim? does this passage support it?). Code enforces the rules on top, but a soft invented characterisation
  that no rule names can still get through. Read the claims list before filming.
- X queries retry with backoff (3 attempts, 45 s each, 100 s per query). A query that still fails is listed in a `PARTIAL RESULTS` line
  and in `failed_sources`; the rest of the run continues.
- Web search is Exa's free hosted endpoint with no key. After one rate limit (HTTP 429) Agent Reach locks the whole web channel for an
  hour and answers every later call with "Wait N seconds" without contacting Exa. So a rate limit is never retried through (a limit of
  30 s or less is waited out once); the channel is marked cooling down and the remaining web queries are reported as not sent, not
  hammered. Web searches run one at a time, 4 s apart, and each result is cached 6 hours in `data/discovery/web_cache.json`, so repeat runs
  do not ask again. While rate limited, a cached copy up to 24 h old is used and labelled `STALE copy`. A web query that could not run
  means those stories are missing from the run, which the `PARTIAL RESULTS` line says.
- Third-party claims stay attributed: unless a story is the organisation's own post, every factual sentence in a hook or script must say
  who says it, even when the source states it flatly. Statements that add wording the sources do not carry are removed, and a brief with
  nothing left about the story itself (only the creator's plans) is rejected rather than shipped as a hollow outline.
- `needs_verification` is a keyword check (a company name near a verb like "released"). It does not look the claim up.
  Only the accounts and feeds in `sources.OFFICIAL_ACCOUNTS` / `OFFICIAL_ORG` count as the company speaking.

## Not included

- `scripts/optimize_linkedin.py` needs `dspy`, a dev-only dependency that is not in `requirements.txt`.
- `scripts/scheduler_setup.py` would create Windows tasks prefixed `DiscoveryAgent_`. Its default times
  are Agent P's posting slots. Do not run `install` until the discovery schedule exists.
- Posting to X or LinkedIn was removed on purpose (`agent/publish.py` deleted).
