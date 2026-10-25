# Validation report: discovery pipeline with Agent Reach

Date: 25 Oct 2026. Scope: end-to-end check after the Agent Reach integration. No trend tracking, Telegram, scheduling or publishing was added. No credentials were printed.

## Result

Pipeline runs end to end. Three bugs found and fixed; the fixes were checked on a second live run. Remaining issues below are ranking and coverage quality, not crashes.

## What was run

| Check | Result |
|---|---|
| Existing tests (`test_discovery`, cluster/rank/scripts self-checks, `import main`) | pass |
| New `tests/test_reach.py` (incl. 3 regression checks added in this pass) | pass |
| Full run 1 (`--top 10 --briefs 3`), before fixes | exit 0, 128 s, 41 source jobs, 0 failed |
| Full run 2, after fixes | exit 0, 124 s, 41 source jobs, 0 failed |
| Separate fetch-only analysis of the full pool | 105 s, 250 items, 0 errors |

The regression tests were written against the observed bad data. I did not run them against the pre-fix code, so "fails before the fix" is not demonstrated.

## Checks

- **Runtime:** about 2 minutes. Mostly the 5 X queries, which run one at a time (about 15 s each), plus one `youtube read` per undated video.
- **Source failures:** none in 3 fetches. If a channel fails, its other queries are skipped and the run continues (covered by mocked tests, not triggered live).
- **Date filtering:** of 250 items, 0 older than 7 days, 0 undated, 0 future-dated. Schema complete on all 250. No zero-valued metrics were invented.
- **Duplicates:** 252 items became 223 stories (29 merged). No duplicate canonical URLs across stories. 7 near-miss title pairs (overlap 0.30–0.50): 6 were different stories, 1 was a real miss (two 1Password agent-login headlines at 0.36).
- **Ranking:** all 20 candidates were scored by the model each run; no partial-scoring note.
- **Briefs:** generated for the top 3 each run; sources come from the evidence list.

## Undated web results (fix verified)

Raw Agent Reach web search returned 32 hits over the 4 queries; 15 had no date (mostly listing and index pages, plus one 2025 Vercel post). All 15 were dropped and 0 undated web items reached the pool. A mocked test covers it.

## Bugs found and fixed

1. **Promo spam ranked #2.** A "free Claude, apply code DEVWEEK" tweet (572 likes) reached the top 2 and got a brief. Fix: `SPAM_RE` in `sources.py`, applied to tweets, videos and web hits. Run 2: gone. A pool scan with a looser pattern found no other spam.
2. **Self-corroboration counted as `multi_source`.** Both multi-source stories were OpenAI's feed plus @OpenAI's own tweet. Fix: `cluster._origin` treats an official feed and its own account as one voice. Run 2: that story is `engagement` (13,304 likes), not `multi_source`.
3. **Briefs invented details.** The brief prompt saw only titles and URLs. Run 1 briefs claimed "access controls, isolated compute, continuous monitoring" (not in the source), "Claude Fable 5 = GPT-5" and "I'm already testing it". Fix: the story summary is now in the prompt, plus a rule against claiming the creator already tried anything. Run 2 briefs no longer contain invented first-person experience.

## Top 10 review (run 2)

Dates, metrics and links check out against the evidence. Concerns:

- **#2** is a third-party tweet (900 likes) claiming Anthropic released a 15-page PDF on building Claude Duet. Claude Duet itself is real (verified earlier on @claudeai), but the PDF is **unverified**. The brief states it as fact in the script and lists it in `claims_to_verify`.
- **#3** is a "STOP WASTING HOURS" learning-roadmap link thread, which is engagement bait. Not caught as spam. Its brief presents the author's roadmap as the creator's own.
- **#7, #9, #10** are minor GitHub changelog entries. Weak fit for a beginner video.
- **#5** hook says "brand-new" API; the source does not say that. Minor.
- **#6** "12× faster reads" is supported by the OpenAI summary.

## Remaining issues (not fixed)

1. **Seen-filter inactive.** Supabase tables do not exist yet (HTTP 404 `discovery_seen`). Run `docs/supabase.sql`, otherwise ideas repeat across days.
2. **Unverified third-party claims rank high.** `engagement` level skips the single-source cap, so an unknown account's claim about an official release can rank #2.
3. **Official-account detection is narrow.** Only the 4 official feeds are recognised. @claudeai plus a Claude page would still count as two voices.
4. **Near-duplicates:** title-word overlap misses reworded headlines. Lowering the threshold would also merge unrelated stories, so left alone.
5. **Changelog items fill the top 10** because they are official and fresh. Ranking prompt not retuned.
6. **X titles** are the tweet text cut at 200 characters, sometimes mid-sentence.
7. **YouTube yield is low:** 25 search hits became 2–3 dated items in the window. Channel list has one handle.
8. **Thresholds** (50,000 views, 500 likes, 1,000 stars) are set by eye, not calibrated.
9. Reddit, Bilibili, Instagram, Telegram, scheduling, downloads and publishing remain out of scope.

## Files changed in this pass

`agent/discovery/cluster.py`, `sources.py`, `reach.py`, `scripts.py`, `tests/test_reach.py`, and this report.

---

# Follow-up: quality pass (25 Oct 2026, later)

Fixes for the top remaining issues above. Tests: `tests.test_quality` (new, 13 checks), `test_reach`, `test_discovery` and
all module self-checks pass. Two live runs (122 s and 241 s; the second had an X timeout, handled and reported).

| Issue | Fix | Verified |
|---|---|---|
| Seen-filter silent when Supabase tables missing | `seen.py`: probe says `off` / `tables_missing` / `error` / `ok`; a local ledger (`data/discovery/seen_ideas.json`) keeps the filter working; every run prints `LOCAL ONLY` and the fix. Same link or same story under a new link is dropped. | Live: run 2 dropped 9 of run 1's ideas, none came back. Tests cover corrupt ledger, write failure, no key in status text. |
| Unverified claims ranked as fact | `claim_status` (official / corroborated / unverified) and `needs_verification`; score cap 55; idea hook dropped (the brief attributes it); briefs split `verified_facts` from `claims_to_verify`. | Re-scoring run 2's saved record: the unverified "Anthropic turned Agent Skills into an open spec" tweet went from 74.8 to 50. |
| Low-value changelog, engagement bait | Prompt priorities; caps for changelog-only (65), admin/monthly-update titles (55), bait (50); penalised items lose model-pool slots first. | Live: no changelog or bait items in run 1's top 10. Tests use literal numbers and were mutation-checked. |
| Briefs credit others' work to the creator, hype words | Prompt rules, code-built credit line, `warnings` for first-person experience and unsupported hype; hooks that do this are dropped. | Live: run 1 briefs attribute to the author ("According to Kimi_Moonshot's post", "Tech With Tim says"). |

Found and fixed during live runs: duplicate evidence entries (same tweet from two queries); an idea hook saying "I'm launching"
about someone else's product; official accounts missing from the list; "turned … into" not recognised as an announcement.

## Remaining issues

1. **Supabase tables still missing.** The filter is local-only until `docs/supabase.sql` is run. `python -m agent.discovery.seen` checks it.
2. **Invented specifics in briefs are not caught.** Run 1's Decisions API brief added "a simple HTTP call that returns a decision in
   milliseconds"; only hype words and first-person claims are checked, not made-up details.
3. **Vendor claims stay vendor claims.** An official post's benchmark ("tops the leaderboard") is not penalised; the prompt asks for
   "X says", but nothing enforces it. Superlatives the source itself uses ("The Best Agent IDE") pass the hype check.
4. **`needs_verification` is a keyword check.** Misses claims phrased without a listed verb; official accounts are a hand-kept list
   (`sources.OFFICIAL_ACCOUNTS`). A GitHub repo under an official org, found via Hacker News, still counts as unverified.
5. **Pool gets weaker after dedup.** Once the best ideas are shown, run 2's top 10 was mostly Hugging Face blog posts and
   single-source items at 60–68. The remaining supply, not the ranking, limits quality on a second run the same day.
6. **Google News links are redirect URLs** (`news.google.com/rss/articles/...`), so they cannot be matched to the article's own URL.
7. **X is flaky:** one query timed out (60 s), doubling runtime to 241 s; the other X query was skipped after it, as designed.

## Files changed in the quality pass

New: `agent/discovery/seen.py`, `agent/discovery/guard.py`, `tests/test_quality.py`.
Changed: `agent/store.py` (probe, titles), `agent/discovery/cluster.py`, `sources.py`, `rank.py`, `scripts.py`, `run.py`,
`tests/test_discovery.py`, `tests/test_reach.py`, `docs/supabase.sql`, `README.md`.

---

# Hardening pass and trend detection (25 Oct 2026, evening)

## Acceptance results (hardening request)

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1a | Why `discovery_seen` was missing | PASS (diagnosed) | Same project (service_role key's project ref = URL host), public schema, names match. The SQL had not been applied at the time; all three `discovery_*` tables answer HTTP 200 now. The project also holds another app's tables, so the prefix matters. |
| 1b | Live Supabase read/write test | PASS | `python -m agent.discovery.seen --test`: write, read back, delete, confirm gone (5/5), re-run at the end. |
| 1c | Supabase is the ledger; local file only a reported fallback | PASS | Runs print `Seen-filter: SUPABASE (shared ledger)`; fallback prints `LOCAL FALLBACK` plus the reason; fallback rows are copied up once; tests cover read/write/sync failure. |
| 1d | Persistence across separate runs | PASS | A2 -> B and C -> D (separate processes): 0 shared ideas, 0 shared evidence URLs; every shown idea is in `discovery_seen`. |
| 2 | Brief accuracy, class of error | PASS offline, with a stated limit | `verify.py` audits every sentence; `tests/test_verify.py` (fake lenient model, mutation-checked) covers invented details, units, figures, vendor claims, superlatives, tense, false experience. The live model's judgement is not unit-tested. |
| 2b | Inspect >= 5 generated claims against sources | PASS | 14 inspected across runs A, A2, C. Defects found and fixed: "94% less time" restated as "94% faster"; "coming to" restated as "now available"; "We modeled 30% of Git" about someone else's project; a figure-bearing question generalising one franchisee's result; "easier than ever" restated as fact. Run C's surviving claims were clean. |
| 3 | Claim verification, structured | PASS offline | `first_party` / `corroborated` / `unverified` from accounts, feeds, official URLs and GitHub owners; reposts and affiliates are one voice; the ranking model decides "asserts company news", keywords are the fallback. No page is fetched, so nothing is called verified that was not. |
| 4 | X reliability | PASS | Bounded retries with backoff, 45 s per attempt, 100 s per query, per-query status (`ok:8 (retried 1x)`), `PARTIAL RESULTS` banner. X is serialised on measured evidence (5 queries: 62 s one at a time, 73 s three at a time; three at a time timed out 5 of 5 in a full run). X succeeded 5/5 in runs A2, B, C, D. |
| 5 | Tests + full live run + second pass + real Supabase test | PASS | 12 suites and self-checks pass; live runs A, A2, B, C, D completed. |

## Trend detection

Offline: PASS (`tests/test_trends.py`, 14 checks, mutation-checked, includes an 8-day simulation through the real storage code).
Live schema check on Supabase: BLOCKED. `python -m agent.discovery.trends --test` reports `tables_missing`; `discovery_topic_obs`
does not exist until `docs/supabase.sql` is run. Nothing about that table is claimed to work on Supabase.
Live data, day one: PASS. Run C stored 216 real observations (local fallback file). Run D, minutes later, stored 8 new and skipped 210
already recorded: 224 rows, 224 unique topic-days, 0 duplicates. All 218 topics `insufficient_data`, each explaining why.

## Remaining issues

1. `web:*` queries hit `RATE_LIMITED` in runs B, C and D (all four, three attempts each). Reported as partial, not hidden; the Exa limit
   is outside this code. Back-to-back runs make it likelier.
2. Unattributed facts from unverified sources still pass when the source text says them (a tweet's "works the night shift" as a hook).
3. Soft invented characterisations that no rule names can still pass; the audit depends on the model for judgement calls.
4. A first-party post's own claims ("easier than ever") are now forced to be attributed, but a vendor's non-comparative claim stated as a plain fact is allowed.
5. Trend thresholds are set by eye. `new` needs 3 earlier run-days, `rising` and `popular` need real repeat sightings: nothing but
   day-one behaviour can be shown on live data today.
6. Trends annotate ideas; they do not change ranking.

---

# Web limits, attribution and invented detail (25 Oct 2026, night)

| # | Issue | Result | Evidence |
|---|---|---|---|
| 1 | Why all four web queries were rate limited | PASS (diagnosed) | Web search is Exa's free hosted endpoint, no key. After one 429, Agent Reach locks the whole channel for `RATE_LIMIT_SECONDS = 3600` and answers every later call "Wait N seconds" without contacting Exa. Probe: 1023 s, then 1019 s four seconds later (blocked calls do not extend it). The pipeline's 2 s / 5 s retries could never succeed, and four concurrent searches per run across about nine runs a day is what reached the limit. |
| 1b | Bounded backoff, spacing, per-query status, no retry storm | PASS | A limit is never retried through (a limit of 30 s or less is waited out once, then stops); the channel is marked cooling down and later queries are reported `not sent`; web searches run one at a time 4 s apart; results cached 6 h; a cached copy up to 24 h old is used only while rate limited and is labelled `STALE copy`. `tests/test_web_limits.py` (8 checks, mutation-checked). Live run E: 1 request reached the CLI ("cooling down for 277s"), 3 were not sent, one grouped `PARTIAL RESULTS` line, all other sources unaffected. Live web test after the cooldown: 4 real searches, serialised, 26 s, all `ok`; a second pass took 0 s from the cache (`cached 0m ago`). |
| 1c | X stays serial | PASS | `X_PARALLEL = 1` untouched; X delivered 5 of 5 in run E. |
| 2 | Third-party claims stay attributed | PASS offline, live-checked on saved records | Unless a story is the organisation's own post, every factual sentence in a hook or script must name the source. Bug found in my own check by run E: "write" counted as an attribution cue, so "A Harvard study finds AI agents write 30% more code" passed. Attribution now needs a cue verb AND a reference to the real source (author, publisher, platform, or "the post / this video / a thread on X"). Re-judged on the saved run-E record, the Harvard hook is removed and the sound hooks are kept. |
| 3 | Plausible invented characterisations | PASS offline, with a stated limit | The audit model must list phrases that add mechanism, cause, outcome or quality (`unsupported_details`); code checks each is really in the statement and has a word the sources lack; a statement with 3+ new content words is flagged; a brief with nothing about the story left (only the creator's plans) is rejected. Run E found a loophole, fixed: a sentence labelled "plan" could carry "the dashboard that lets you start a new chat" or "the tool Uber just announced"; plans that assert an event or a capability are now checked as facts. |
| 4 | Regression tests for these exact cases | PASS | New: `tests/test_web_limits.py`. Added to `tests/test_verify.py`: third-party attribution, the Harvard hook, invented characterisations, thin evidence rejection, plans carrying claims. 13 suites and self-checks pass; each new rule was broken on purpose and a test failed. |
| 5 | No synthetic data in real folders or production Supabase | PASS | Tests redirect every file and use a fake PostgREST. Checked afterwards: production `discovery_topic_obs` has 236 rows, all dated 2026-10-25 from real runs, none with `selftest` or fixture titles; `discovery_seen` has no `selftest` rows. |
| 6 | Live trend schema (was BLOCKED) | PASS | The table now exists. `python -m agent.discovery.trends --test`: 8 of 8 steps, including the primary key ignoring a duplicate. Run E copied the 224 local observations up. |

## Not verified, and remaining issues

1. The attribution and plan fixes came after the one live run. They are verified by tests with the exact failing sentences and by re-judging the saved run-E records, not by a fresh end-to-end live run.
2. A meaning shift the model misses still gets through: GitHub's "global default policy for generally available features" became "makes Copilot features generally available" in a hook and one script line. No rule can see that; read the claims before filming.
3. Exa's actual quota is unknown. Whether the 6 h cache keeps runs under it can only be learned over a few days. The `STALE copy` path was tested offline and not observed live.
4. A changelog entry (e.g. "Global default for Copilot features generally available") still ranks #2 when little else survives the seen-filter and the missing web results; it is capped, not removed.
