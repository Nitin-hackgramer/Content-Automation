"""Where candidate content comes from. Fetching only: nothing here dedupes, scores or ranks.

Every item is a plain dict (the common schema; missing values are None or {}, never guessed):
    title, url, source (how it was found: "HackerNews", "X search"), platform (where it lives: "X", "YouTube"),
    summary, author, published (aware datetime or None),
    metric (int or None), metric_name ("points", "reactions", "stars", "likes", "views", "trending score" or None),
    metrics (every count the platform gave, e.g. {"likes": 9, "views": 120}), collected_at (set by fetch_all)

Agent Reach sources (X, YouTube, web search) live in reach.py and join through jobs().

A source that fails (network error, HTTP 429, bad payload) is reported as "error:<reason>" and
skipped. The other sources still run. Fetchers are new rather than reused from agent/nodes/research.py
because those drop dates and engagement numbers and swallow errors without a trace.
"""

import calendar
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from agent.nodes.research import _clean

WINDOW_DAYS = 7
TIMEOUT = 15
HEADERS = {"User-Agent": "content-discovery/0.1 (personal research)"}

# Companies' own announcements. Their stories need no keyword match and count as credible.
OFFICIAL = {"OpenAI", "Google AI", "Hugging Face blog", "GitHub changelog"}
OFFICIAL_FEEDS = [
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Google AI", "https://blog.google/technology/ai/rss/"),
    ("Hugging Face blog", "https://huggingface.co/blog/feed.xml"),
    ("GitHub changelog", "https://github.blog/changelog/feed/"),
]
# Who speaks for which organisation. Used to tell a company's own post from a third party's claim about it.
# Keys of OFFICIAL_ACCOUNTS are X/GitHub handles with everything but letters and digits removed. Knobs: add yours.
OFFICIAL_ORG = {"OpenAI": "openai", "Google AI": "google", "Hugging Face blog": "huggingface", "GitHub changelog": "github"}
OFFICIAL_ACCOUNTS = {"openai": "openai", "anthropicai": "anthropic", "claudeai": "anthropic", "googleai": "google",
                     "googledeepmind": "google", "google": "google", "huggingface": "huggingface", "github": "github",
                     "deepseekai": "deepseek", "mistralai": "mistral", "nvidiaai": "nvidia", "kimimoonshot": "moonshot",
                     "daggerio": "dagger", "alibabaqwen": "alibaba", "aiatmeta": "meta", "cursorai": "cursor"}
# Pages the organisation hosts itself (host or subdomain), and GitHub owners that are the organisation. A link
# into one of these is the organisation speaking. huggingface.co is left out: anyone can publish there.
OFFICIAL_DOMAINS = {"openai.com": "openai", "anthropic.com": "anthropic", "claude.com": "anthropic",
                    "blog.google": "google", "deepmind.google": "google", "ai.google.dev": "google",
                    "github.blog": "github", "nvidia.com": "nvidia", "mistral.ai": "mistral", "deepseek.com": "deepseek"}
OFFICIAL_GITHUB_OWNERS = {"openai": "openai", "anthropics": "anthropic", "google": "google", "google-deepmind": "google",
                          "huggingface": "huggingface", "nvidia": "nvidia", "deepseek-ai": "deepseek", "mistralai": "mistral",
                          "microsoft": "microsoft", "dagger": "dagger", "moonshotai": "moonshot"}
AI_NEWS_FEEDS = {"OpenAI", "Google AI"}
HN_QUERIES = ["claude code", "cursor", "coding agent", "vibe coding", "AI agent", "MCP server", "open source LLM",
              "AI engineering"]
GITHUB_QUERIES = ["ai agent", "coding agent", "mcp server", "llm", "developer tools ai"]
DEVTO_TAGS = ["ai", "llm", "programming", "beginners"]
GOOGLE_QUERIES = ["AI coding tool", "AI agent launch", "vibe coding", "AI startup launch"]

# Community sources are noisy, so a story must mention one of these to be kept.
TOPIC_RE = re.compile(r"\b(ai|llms?|agents?|agentic|mcp|claude|cursor|copilot|codex|gemini|openai|anthropic|chatgpt"
                      r"|gpt|vibe|coding|code|developers?|open[- ]source|models?)\b", re.IGNORECASE)

# Promo and giveaway posts. Open X search lets these through at hundreds of likes; they are ads, not stories.
SPAM_RE = re.compile(r"\b(promo|coupon|discount|referral|invite) ?code\b|\b(apply|use) code\b|\bgiveaway\b"
                     r"|\bairdrop\b|\bfree right now\b|\bhow to claim\b", re.IGNORECASE)

# Engagement at which a story counts as measured traction. Knobs: set by eye, not calibrated.
# GitHub is high on purpose: stars on new repos are often farmed.
ENGAGEMENT = {"points": 200, "reactions": 100, "stars": 1000, "likes": 500, "views": 50000}


def _get(url, **params):
    r = httpx.get(url, params=params or None, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r


def _iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _on_topic(text):
    return bool(TOPIC_RE.search(text or ""))


def _item(title, url, source, summary="", published=None, metric=None, metric_name=None,
          author=None, platform=None, metrics=None):
    if metrics is None:
        metrics = {metric_name: metric} if metric is not None and metric_name else {}
    return {"title": " ".join((title or "").split())[:200], "url": url, "source": source,
            "platform": platform or source, "summary": _clean(summary or "", 300), "author": author,
            "published": published, "metric": metric, "metric_name": metric_name, "metrics": metrics}


def normalize(item, collected_at):
    """Bring any fetcher's dict to the common schema. Fills absent keys with None/{}; never overwrites data."""
    full = {**_item(None, None, None), **item}  # not re-cleaned: _clean unescapes HTML, twice would double-decode
    full["platform"] = full["platform"] or full["source"]
    if not full["metrics"] and full["metric"] is not None and full["metric_name"]:
        full["metrics"] = {full["metric_name"]: full["metric"]}
    full["collected_at"] = collected_at
    return full


def parse_feed(source, text, limit=30):
    out = []
    for e in feedparser.parse(text).entries[:limit]:
        if not (getattr(e, "title", None) and getattr(e, "link", None)):
            continue
        pub = e.get("published_parsed")
        published = datetime.fromtimestamp(calendar.timegm(pub), timezone.utc) if pub else None
        out.append(_item(e.title, e.link, source, getattr(e, "summary", ""), published, author=e.get("author") or None))
    return out


def parse_hn(hits):
    return [_item(h["title"], h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}", "HackerNews",
                  "", datetime.fromtimestamp(h["created_at_i"], timezone.utc), h.get("points"), "points",
                  author=h.get("author"))
            for h in hits if h.get("title") and _on_topic(h["title"])]


def parse_devto(rows):
    return [_item(r["title"], r["url"], "dev.to", r.get("description") or "", _iso(r.get("published_at")),
                  r.get("positive_reactions_count"), "reactions", author=(r.get("user") or {}).get("username"))
            for r in rows if _on_topic(r.get("title"))]


def parse_github(rows):
    return [_item(r["full_name"], r["html_url"], "GitHub", r.get("description") or "", _iso(r["created_at"]),
                  r["stargazers_count"], "stars", author=(r.get("owner") or {}).get("login"))
            for r in rows if _on_topic(f"{r['full_name']} {r.get('description') or ''}")]


def parse_models(rows, now):
    # The trending list is a snapshot with no publish date, so it is dated to the run.
    return [_item(m["id"], f"https://huggingface.co/{m['id']}", "Hugging Face models", m.get("pipeline_tag") or "",
                  now, m.get("trendingScore"), "trending score")
            for m in rows if m.get("id")]


def feed_filter(name, entries):
    # OpenAI and Google AI feeds are AI news end to end. Changelogs and research blogs are not:
    # "EMU deletions fix" is not a content idea, so they need the topic check too.
    if name in AI_NEWS_FEEDS:
        return entries
    return [i for i in entries if _on_topic(i["title"] + " " + i["summary"])]


def _fetch_feed(name, url):
    return feed_filter(name, parse_feed(name, _get(url).text))


def _fetch_hn(query, now):
    since = int((now - timedelta(days=WINDOW_DAYS)).timestamp())
    r = _get("https://hn.algolia.com/api/v1/search_by_date", query=query, tags="story", hitsPerPage=15,
             numericFilters=f"created_at_i>{since},points>10")
    return parse_hn(r.json()["hits"])


def _fetch_github(query, now):
    since = (now - timedelta(days=WINDOW_DAYS)).date().isoformat()
    r = _get("https://api.github.com/search/repositories", q=f"{query} created:>{since}",
             sort="stars", order="desc", per_page=10)
    return parse_github(r.json()["items"])


def _fetch_devto(tag):
    return parse_devto(_get("https://dev.to/api/articles", tag=tag, top=7, per_page=20).json())


def _fetch_models(now):
    return parse_models(_get("https://huggingface.co/api/models", sort="trendingScore", direction=-1, limit=8).json(), now)


def _fetch_google(query):
    q = urllib.parse.quote(f"{query} when:3d")
    entries = parse_feed("Google News", _get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en").text, 8)
    for e in entries:  # headlines end in " - Publisher"; the publisher goes in the summary
        head, sep, publisher = e["title"].rpartition(" - ")
        if sep:
            e["title"], e["summary"] = head, publisher
    return entries


def jobs(now):
    """(report name, fetch function) for every source. Each function returns a list of items."""
    from agent.discovery import reach  # here, not at the top: reach imports this module

    return ([(f"feed:{n}", lambda n=n, u=u: _fetch_feed(n, u)) for n, u in OFFICIAL_FEEDS]
            + [(f"hn:{q}", lambda q=q: _fetch_hn(q, now)) for q in HN_QUERIES]
            + [(f"github:{q}", lambda q=q: _fetch_github(q, now)) for q in GITHUB_QUERIES]
            + [(f"devto:{t}", lambda t=t: _fetch_devto(t)) for t in DEVTO_TAGS]
            + [("hf:models", lambda: _fetch_models(now))]
            + [(f"google:{q}", lambda q=q: _fetch_google(q)) for q in GOOGLE_QUERIES]
            + reach.jobs(now))


def _reason(e):
    code = getattr(e, "reach_code", None)  # Agent Reach's own error code: TIMEOUT, UPSTREAM_BROKEN, ...
    if code:
        tries = getattr(e, "attempts", 1)
        why = code if tries <= 1 else f"{code} after {tries} attempts"
        detail = getattr(e, "detail", "")
        return f"{why} ({detail})" if detail else why
    status = getattr(getattr(e, "response", None), "status_code", None)
    return f"HTTP {status}" if status else type(e).__name__


def fetch_all(now, job_list=None):
    """→ (items inside the window, {source name: "ok:<count>" or "error:<reason>"})."""
    job_list = jobs(now) if job_list is None else job_list
    with ThreadPoolExecutor(max_workers=16) as pool:  # X jobs wait on one lock, so leave room for the rest
        futures = [(name, pool.submit(fn)) for name, fn in job_list]
    items, report = [], {}
    for name, fut in futures:
        try:
            got = fut.result()
            report[name] = f"ok:{len(got)}{getattr(got, 'note', '')}"  # reach.Result adds " (retried 1x)"
            items += got
        except Exception as e:
            report[name] = f"error:{_reason(e)}"
    collected_at = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    return [normalize(i, collected_at) for i in items if i.get("published") is None or i["published"] >= cutoff], report
