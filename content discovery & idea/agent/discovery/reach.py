"""Agent Reach adapter: X search, YouTube search and channel uploads, web search. Fetching only, like sources.py.

Uses only channels verified working on this machine (agent-reach check, 25 Oct 2026). Reddit, Bilibili and the
login-only channels are left out. GitHub is not called through Agent Reach: its github search uses the same
public GitHub API that sources.py already queries, so it would only duplicate those results.

Everything here is keyword search or a channel's upload list. None of it is a trending feed, and nothing says so:
the source names ("X search", "YouTube search") keep that visible all the way to the saved ideas.

Missing values stay None. YouTube search returns no dates, so each hit is read once for its date; when that read
fails the date stays None. YouTube dates are whole days (midnight UTC), not exact times. Undated web-search hits
are dropped (see _fetch_web).

Failures. Each call gets bounded retries with backoff for transient errors (timeouts, upstream breakage, bad
output), a per-attempt timeout and a total budget per query. A call that still fails raises
ReachError(code, attempts), which sources.fetch_all reports as "error:<code> after <n> attempts", and the run
is marked partial. A query that succeeded only after retrying says so ("ok:8 (retried 1x)"). One failed query
never suppresses another: only a setup problem (agent-reach missing, channel not set up) skips the rest of a
channel, because every later query would fail the same way. X queries run one at a time (see X_PARALLEL for the
measurements); once two X queries have used up their retries the rest get one attempt each, so a dead backend
costs a bounded amount of time without any query being skipped.
"""

import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from agent.discovery.sources import SPAM_RE, WINDOW_DAYS, _item, _iso, _on_topic
from agent.paths import DATA

CLI = shutil.which("agent-reach")
# Channel upload lists come from the yt-dlp inside Agent Reach's own tool env: `agent-reach read` on a channel
# page timed out at 30s in testing. Override with YTDLP_PYTHON if uv keeps its tools somewhere else.
YTDLP_PYTHON = os.getenv("YTDLP_PYTHON") or os.path.join(
    os.getenv("APPDATA", ""), "uv", "tools", "agent-reach", "Scripts", "python.exe")
TIMEOUT = 60  # seconds for the yt-dlp channel listing
ATTEMPT_TIMEOUT = 45  # seconds per attempt, passed to agent-reach; X answers in 9-28s when it answers
QUERY_BUDGET = 100  # seconds for one query including every retry and backoff
MIN_RETRY_BUDGET = 15  # no retry is started with less than this left
MAX_ATTEMPTS = 3
BACKOFF = (2, 5)  # seconds before retry 1 and 2, plus up to a second of jitter
# Measured 25 Oct 2026 on this machine: 5 real X queries took 62s one at a time and 73s three at a time (one needed
# a retry), so parallelism bought nothing; in a full pipeline run three at a time timed out 5 of 5, while one at a
# time completed in earlier runs. The backend (a shared browser) wants serialising, so X runs one query at a time.
# Other channels are not serialised, and a failed X query never stops the next one.
X_PARALLEL = 1
X_DEGRADED_AFTER = 2  # once this many X queries have used up their retries, the rest get one attempt each: bounded, not skipped
# Codes that retrying cannot fix. SETUP codes also mean every later call on the channel fails the same way.
NO_RETRY = {"NOT_INSTALLED", "NEED_SETUP", "BAD_INPUT", "NOT_FOUND", "LOGIN_REQUIRED", "AUTH_REQUIRED"}
SETUP = {"NOT_INSTALLED", "NEED_SETUP"}
# A rate limit is different from a glitch. Web search is Exa's free hosted endpoint with no key, and after ONE 429 Agent Reach
# locks the whole channel for an hour (agent_reach.runtime.dispatch.RATE_LIMIT_SECONDS = 3600) and answers every later call with
# "Wait N seconds" without contacting Exa. Retrying a 2s/5s backoff against an hour-long lock cannot work, so a rate limit is
# never retried through; the channel is marked cooling down for N seconds and later queries are skipped, not sent.
RATE_LIMIT_DEFAULT = 3600  # assumed cooldown when the reply does not say
MAX_LIMIT_WAIT = 30  # a limit this short is waited out once; a long one is not
# Web search volume. Four concurrent searches per run, repeated across a day of runs, is what reaches the limit in the first place.
WEB_SPACING = 4  # seconds between one web search ending and the next starting
WEB_CACHE_TTL = 6 * 3600  # a cached search is reused this long instead of asking again
WEB_STALE_MAX = 24 * 3600  # while rate limited, a cached search up to this old is better than nothing, and is labelled
WEB_CACHE = os.path.join(DATA, "discovery", "web_cache.json")

# Niche: AI engineering, coding agents, AI tools, developer workflows, open source, beginner explainers,
# startup and product stories. Knobs, not tuned: each X query costs ~15s of browser time.
TWITTER_QUERIES = ["claude code", "coding agent", "AI engineering", "open source AI model", "AI dev tool launch"]
TWITTER_MIN_LIKES = 100  # X search filter; keeps out most spam
YOUTUBE_QUERIES = ["claude code tutorial", "AI coding agent", "AI tools for developers", "AI explained for beginners"]
YOUTUBE_CHANNELS = ["@vaibhavsisinty"]  # creators whose latest uploads are worth watching; add handles here
WEB_QUERIES = ["AI coding agent launch", "open source AI developer tool release", "AI startup product launch",
               "AI engineering workflow"]
LIMIT = 8

_down = set()  # channels with a setup problem this run
_limited = {}  # channel -> time.monotonic() deadline of its rate-limit cooldown, this run
_x_slots = threading.BoundedSemaphore(X_PARALLEL)
_x_state = {"exhausted": 0}  # X queries that failed after every retry, this run
_web_lock = threading.Lock()  # web searches go one at a time, spaced
_web_state = {"ended": None}


class ReachError(Exception):
    def __init__(self, code, attempts=1, detail="", retry_after=None):
        super().__init__(code)
        self.reach_code = code
        self.attempts = attempts
        self.detail = detail  # shown after the code in the per-query status
        self.retry_after = retry_after  # seconds, when the platform said how long


class Result(list):
    """A list of items that also says how it was got: note is "" or " (retried 2x)"."""
    note = ""


def _once(channel, action, args, timeout):
    """One agent-reach command, no retries → its items list. Raises ReachError."""
    try:
        p = subprocess.run([CLI, channel, action, *map(str, args), "--timeout", str(int(timeout))],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=timeout + 5)
        out = json.loads(p.stdout or "null")
    except subprocess.TimeoutExpired:
        raise ReachError("TIMEOUT")
    except ValueError:  # stdout was not JSON
        raise ReachError("MALFORMED")
    if not isinstance(out, dict):
        raise ReachError("MALFORMED")
    if not out.get("ok"):
        err = out.get("error") or {}
        wait = re.search(r"Wait (\d+) seconds", str(err.get("hint") or ""))  # how agent-reach says how long a limit lasts
        raise ReachError(err.get("code") or "UNKNOWN_ERROR", retry_after=int(wait[1]) if wait else None)
    if not isinstance(out.get("items"), list):
        raise ReachError("MALFORMED")
    return out["items"]


def call(channel, action, *args, timeout=ATTEMPT_TIMEOUT, track=True, attempts=MAX_ATTEMPTS):
    """Run one agent-reach command with bounded retries → Result (items + note). Raises ReachError.
    Transient failures are retried with backoff inside QUERY_BUDGET; NO_RETRY codes stop at once.
    track=False: a setup problem does not mark the channel down (per-video reads)."""
    if track and channel in _down:
        raise ReachError("SKIPPED_SETUP_PROBLEM")
    if not CLI:
        raise ReachError("NOT_INSTALLED")
    left_cool = _limited.get(channel, 0) - time.monotonic()
    if left_cool > 0:  # a limit already seen this run: do not ask again, say so
        raise ReachError("RATE_LIMITED", attempts=0, retry_after=int(left_cool),
                         detail=f"{channel} is cooling down for another {int(left_cool)}s; this query was not sent")
    start, err = time.monotonic(), None
    for n in range(1, attempts + 1):
        left = QUERY_BUDGET - (time.monotonic() - start)
        if n > 1 and left < MIN_RETRY_BUDGET:
            break
        try:
            out = Result(_once(channel, action, args, min(timeout, max(5, left))))
            out.note = f" (retried {n - 1}x)" if n > 1 else ""
            return out
        except ReachError as e:
            err, err.attempts = e, n
            if e.reach_code == "RATE_LIMITED":
                cool = e.retry_after if e.retry_after is not None else RATE_LIMIT_DEFAULT
                _limited[channel] = time.monotonic() + cool
                e.detail = f"{channel} rate limited; cooling down for {cool}s"
                if (cool <= MAX_LIMIT_WAIT and n == 1 and attempts > 1
                        and QUERY_BUDGET - (time.monotonic() - start) > cool + MIN_RETRY_BUDGET):
                    time.sleep(cool + 1)  # a short limit is waited out ONCE; if it comes straight back, stop: no retry storm
                    continue
                break
            if e.reach_code in NO_RETRY:
                break
            if n < attempts:
                time.sleep(BACKOFF[min(n, len(BACKOFF)) - 1] + random.random())
    if track and err.reach_code in SETUP:
        _down.add(channel)
    raise err


def _int(v):
    """X sends counts as strings ("10558624"). Anything not a whole number is None, not 0."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    return int(v) if isinstance(v, str) and v.isdigit() else None


def _metrics(**counts):
    return {k: v for k, v in counts.items() if v is not None}


def _x_time(value):
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    except (TypeError, ValueError):
        return None


def _day(value):
    """'2026-10-25' or an ISO time → aware datetime, else None."""
    try:
        return _iso(value) if value and "T" in value else datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _author(v):
    return v if isinstance(v, str) and v.strip() and v != "N/A" else None


def parse_tweets(rows):
    out = []
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("url"), str):
            continue
        raw = r.get("raw") or {}
        text = r.get("text") or raw.get("text") or ""
        card = raw.get("card") or {}
        # A tweet has no headline: the link card's title is the best one, else the text without t.co links.
        title = card.get("title") or re.sub(r"https://t\.co/\S+", "", text)
        if not _on_topic(f"{title} {text}") or SPAM_RE.search(f"{title} {text}"):
            continue
        likes = _int(raw.get("likes"))
        tweet = _item(title, r["url"], "X search", text, _x_time(r.get("published_at")), likes,
                      "likes" if likes is not None else None, author=_author(r.get("author")), platform="X",
                      metrics=_metrics(likes=likes, retweets=_int(raw.get("retweets")), views=_int(raw.get("views"))))
        # Whose words this really is. A repost or quote tweet repeats another account's claim: it is not a second voice.
        quoted = raw.get("quoted_tweet") if isinstance(raw.get("quoted_tweet"), dict) else {}
        reposted = re.match(r"RT @(\w+):", text)
        tweet["via"] = _author(quoted.get("author")) or (reposted[1] if reposted else None)
        tweet["link_domain"] = card.get("domain") if isinstance(card.get("domain"), str) else None  # where its link card points
        out.append(tweet)
    return out


def parse_videos(rows, source):
    out = []
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("url"), str) or not r.get("title") \
                or SPAM_RE.search(f"{r['title']} {r.get('text') or ''}"):
            continue
        raw = r.get("raw") or {}
        views = _int(raw.get("view_count"))
        out.append(_item(r["title"], r["url"], source, r.get("text") or "", _day(r.get("published_at")), views,
                         "views" if views is not None else None, author=_author(r.get("author")), platform="YouTube",
                         metrics=_metrics(views=views, likes=_int(raw.get("like_count")),
                                          duration_s=_int(raw.get("duration")))))
    return out


def parse_web(rows):
    return [_item(r.get("title"), r["url"], "Web search", r.get("text") or "", _day(r.get("published_at")),
                  author=_author(r.get("author")), platform="Web")
            for r in rows
            if isinstance(r, dict) and isinstance(r.get("url"), str) and r.get("title") and _on_topic(r["title"])
            and not SPAM_RE.search(f"{r['title']} {r.get('text') or ''}")]


def _with_dates(videos):
    """Read each undated video once for its date, exact views and likes. A failed read leaves the hit as it was."""
    for v in videos:
        if v["published"]:
            continue
        try:  # one attempt: a video that will not read costs 45s, not 100s, and the others still get read
            got = parse_videos(call("youtube", "read", v["url"], timeout=45, track=False, attempts=1), v["source"])
        except ReachError:
            continue
        if got:
            v.update({k: got[0][k] for k in ("published", "metric", "metric_name", "metrics", "author")
                      if got[0][k] not in (None, {})})
    return videos


def _noted(parsed, raw):
    out = Result(parsed)
    out.note = raw.note  # " (retried 1x)" travels to the per-query status line
    return out


def _fetch_x(query, now):
    since = (now - timedelta(days=WINDOW_DAYS)).date().isoformat()
    with _x_slots:
        tries = 1 if _x_state["exhausted"] >= X_DEGRADED_AFTER else MAX_ATTEMPTS
        try:
            raw = call("twitter", "search", f"{query} min_faves:{TWITTER_MIN_LIKES} lang:en since:{since}", "--limit", LIMIT,
                       attempts=tries)
        except ReachError as e:
            if e.attempts > 1:
                _x_state["exhausted"] += 1
            raise
    return _noted(parse_tweets(raw), raw)


def _fetch_youtube(query):
    raw = call("youtube", "search", query, "--limit", 5)
    return _noted(_with_dates([v for v in parse_videos(raw, "YouTube search") if _on_topic(v["title"])]), raw)


def _fetch_channel(handle, latest=5):
    if not os.path.exists(YTDLP_PYTHON):
        raise ReachError("NOT_INSTALLED")
    try:
        p = subprocess.run([YTDLP_PYTHON, "-I", "-m", "yt_dlp", "--flat-playlist", "--playlist-end", str(latest), "-J",
                            f"https://www.youtube.com/{handle}/videos"],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
        entries = (json.loads(p.stdout or "null") or {}).get("entries")
    except subprocess.TimeoutExpired:
        raise ReachError("TIMEOUT")
    except (ValueError, AttributeError):
        raise ReachError("MALFORMED")
    if not isinstance(entries, list):
        raise ReachError("MALFORMED")
    # The flat list has no dates and rounds views ("100K"), so only the ids are kept; each video is then read.
    rows = [{"title": e.get("title"), "url": f"https://www.youtube.com/watch?v={e['id']}", "author": handle.lstrip("@")}
            for e in entries if isinstance(e, dict) and e.get("id")]
    return _with_dates(parse_videos(rows, "YouTube channel"))


def _cache_read():
    try:
        with open(WEB_CACHE, encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        return entries if isinstance(entries, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}  # a missing or damaged cache is just a cache miss


def _cache_write(query, items):
    """Best effort: a cache that cannot be written costs a repeated search, never a failed run."""
    entries = _cache_read()
    entries[query] = {"at": time.time(), "items": list(items)}
    keep = time.time() - 3 * WEB_STALE_MAX
    entries = {q: v for q, v in entries.items() if isinstance(v, dict) and v.get("at", 0) >= keep}
    try:
        os.makedirs(os.path.dirname(WEB_CACHE), exist_ok=True)
        tmp = WEB_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f, ensure_ascii=False)
        os.replace(tmp, WEB_CACHE)
    except OSError:
        pass


def _web_items(raw):
    # Undated hits are dropped: Exa returns year-old pages with no date (smoke test: a 2025 Vercel post),
    # and an undated item would pass the 7-day window. X and YouTube keep undated items; they are rare there.
    return [w for w in parse_web(raw) if w["published"]]


def _fetch_web(query):
    """One web search, without adding to the load on a free, unkeyed endpoint: answered from a recent cached copy when there
    is one, otherwise sent alone and spaced from the previous search. If the endpoint is rate limited, a cached copy up to
    WEB_STALE_MAX old is used and says so; with none, the error propagates and the query is reported as not run."""
    hit = _cache_read().get(query)
    age = time.time() - hit["at"] if isinstance(hit, dict) and isinstance(hit.get("at"), (int, float)) else None
    if age is not None and 0 <= age <= WEB_CACHE_TTL and isinstance(hit.get("items"), list):
        raw = Result(hit["items"])
        raw.note = f" (cached {int(age // 60)}m ago)"
        return _noted(_web_items(raw), raw)
    with _web_lock:
        if _web_state["ended"] is not None:
            time.sleep(max(0.0, WEB_SPACING - (time.monotonic() - _web_state["ended"])))
        try:
            raw = call("web", "search", query, "--limit", LIMIT)
        except ReachError as e:
            if e.reach_code == "RATE_LIMITED" and age is not None and age <= WEB_STALE_MAX and isinstance(hit.get("items"), list):
                raw = Result(hit["items"])
                raw.note = f" (STALE copy, {int(age // 3600)}h old: web is rate limited, {e.retry_after or '?'}s to go)"
                return _noted(_web_items(raw), raw)
            raise
        finally:
            _web_state["ended"] = time.monotonic()
    _cache_write(query, raw)
    return _noted(_web_items(raw), raw)


def jobs(now):
    """(report name, fetch function) for sources.fetch_all. Resets the per-run failure memory."""
    _down.clear()
    _limited.clear()
    _web_state["ended"] = None
    _x_state["exhausted"] = 0
    return ([(f"x:{q}", lambda q=q: _fetch_x(q, now)) for q in TWITTER_QUERIES]
            + [(f"youtube:{q}", lambda q=q: _fetch_youtube(q)) for q in YOUTUBE_QUERIES]
            + [(f"youtube-channel:{h}", lambda h=h: _fetch_channel(h)) for h in YOUTUBE_CHANNELS]
            + [(f"web:{q}", lambda q=q: _fetch_web(q)) for q in WEB_QUERIES])
