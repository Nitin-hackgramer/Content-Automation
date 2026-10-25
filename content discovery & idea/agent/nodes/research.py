import html
import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import feedparser
import httpx
from dotenv import load_dotenv

from agent import llm
from agent.paths import ENV_FILE

load_dotenv(ENV_FILE, override=True)

BBC_TECH_RSS = "https://feeds.bbci.co.uk/news/technology/rss.xml"


def _safe(fn):
    # every source is best-effort: one dead feed/API returns [] instead of killing the run
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return []
    return wrap


def _clean(text, limit=300):
    """Feed summaries come as HTML: strip tags/entities, collapse whitespace, cut to `limit`."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return " ".join(text.split())[:limit]


def _snippet(entry, title):
    # Google News "summaries" are just the title again plus the publisher: not worth keeping.
    text = _clean(getattr(entry, "summary", ""))
    headline = title.rpartition(" - ")[0] or title  # the summary repeats the headline without its " - Publisher" tail
    return "" if headline[:40].lower() in text.lower() else text


@_safe
def _parse_feed(source, url, limit=5):
    feed = feedparser.parse(url)
    return [{"title": e.title, "url": e.link, "source": source, "summary": _snippet(e, e.title)}
            for e in feed.entries[:limit] if getattr(e, "title", None) and getattr(e, "link", None)]


def _google_news_search(query, limit=5):
    # Google News RSS supports keyword search → real article URLs from legit outlets
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return _parse_feed("GoogleNews", url, limit)


@_safe
def _hn_search(query, limit=5):
    # HN Algolia search → best source for tech topics mainstream news ignores
    q = urllib.parse.quote(query)
    r = httpx.get(f"https://hn.algolia.com/api/v1/search?query={q}&tags=story", timeout=10)
    items = []
    for h in r.json().get("hits", [])[:limit]:
        if not h.get("title"):
            continue
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        items.append({"title": h["title"], "url": url, "source": "HackerNews"})
    return items


@_safe
def _github_trending(limit=3, days=7):
    # repos with the most stars created in the last week ≈ "going viral"
    # (the daily digest passes days=2 so it doesn't list the same repos all week)
    since = (date.today() - timedelta(days=days)).isoformat()
    r = httpx.get(
        "https://api.github.com/search/repositories",
        params={"q": f"created:>{since}", "sort": "stars", "order": "desc", "per_page": limit},
        headers={"Accept": "application/vnd.github+json"},
        timeout=10,
    )
    return [{
        "title": f"{repo['full_name']} ({repo['stargazers_count']} stars): {(repo.get('description') or '').strip()}".strip(),
        "url": repo["html_url"],
        "source": "GitHub",
        "short": repo["full_name"],  # the digest shows this as the headline and the description under it
        "points": repo["stargazers_count"],
        "summary": _clean(f"{repo.get('description') or ''} ({repo['stargazers_count']:,} stars)"),
    } for repo in r.json().get("items", [])[:limit]]


def _model_facts(m):
    """The only facts the models API gives us, so the digest has something real to show under the name."""
    facts = [m.get("pipeline_tag"), f"{m['downloads']:,} downloads" if m.get("downloads") else None,
             f"{m['likes']:,} likes" if m.get("likes") else None]
    return " · ".join(f for f in facts if f)


@_safe
def _huggingface_trending(limit=3):
    # Hugging Face trending models → the canonical source for "new models"
    r = httpx.get(
        "https://huggingface.co/api/models",
        params={"sort": "trendingScore", "direction": -1, "limit": limit},
        timeout=10,
    )
    items = []
    for m in r.json()[:limit]:
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        items.append({
            "title": f"Trending AI model on Hugging Face: {mid}",
            "url": f"https://huggingface.co/{mid}",
            "source": "HuggingFace",
            "short": mid,
            "summary": _model_facts(m),
            "points": m.get("trendingScore"),  # how hot it is now; likes only say how long it has been around
        })
    return items


@_safe
def _arxiv_recent(query="cat:cs.AI OR cat:cs.LG OR cat:cs.CL", limit=3):
    # newest AI/ML papers (new research, models, algos) — Atom feed, feedparser reads it
    url = (f"http://export.arxiv.org/api/query?search_query={urllib.parse.quote(query)}"
           f"&sortBy=submittedDate&sortOrder=descending&max_results={limit}")
    feed = feedparser.parse(url)
    return [{"title": f"New AI paper: {' '.join(e.title.split())}", "url": e.link, "source": "arXiv",
             "short": " ".join(e.title.split()), "summary": _clean(getattr(e, "summary", ""))}
            for e in feed.entries[:limit]]


@_safe
def _hf_papers(limit=3):
    # Hugging Face daily papers → what the ML community is actually reading today
    r = httpx.get("https://huggingface.co/api/daily_papers", timeout=10)
    items = []
    for p in r.json()[:limit]:
        paper = p.get("paper", {})
        title = paper.get("title") or p.get("title")
        pid = paper.get("id") or p.get("id", "")
        if not title:
            continue
        items.append({"title": f"Trending AI paper: {title}", "url": f"https://huggingface.co/papers/{pid}", "source": "HFPapers",
                      "short": " ".join(title.split()), "summary": _clean(paper.get("summary")),
                      "points": paper.get("upvotes")})
    return items


def _clean_query(feedback):
    # turn a free-form reject reason ("do one about the new Gemini model") into a
    # tight search query so the re-research actually hits the topic. No key → raw string.
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return feedback
    try:
        q = llm.ask(llm.FAST,
                    f"Extract a concise web search query (3-6 words) capturing the topic of this request. "
                    f"Reply with ONLY the query.\nRequest: {feedback}", api_key)
        return q or feedback
    except Exception:
        return feedback


@_safe
def _hn_top(limit=3):
    ids = httpx.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10).json()[:limit]
    items = []
    for i in ids:
        s = httpx.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=10).json()
        items.append({"title": s.get("title", ""), "url": s.get("url", f"https://news.ycombinator.com/item?id={i}"), "source": "HackerNews",
                      "points": s.get("score")})  # upvotes: the digest ranks heat on real numbers like this
    return items


def _gather(*fetchers):
    """Run the source fetchers concurrently, keeping their declared order in the result.

    They're independent network calls, so running them serially just adds up every
    source's latency (~9.5s for the default mix, vs ~2s in parallel — which matters on
    Render's free tier where the service is cold-starting anyway). Each fetcher is
    already @_safe, so a failure still yields [] and never escapes the pool.
    """
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        return [item for result in pool.map(lambda f: f(), fetchers) for item in result]


def research_node(state):
    feedback = state.get("rejection_feedback")
    if feedback:
        q = _clean_query(feedback)  # LLM-tightened topic so re-research hits the mark
        data = _gather(lambda: _google_news_search(q),
                       lambda: _hn_search(q),
                       lambda: _arxiv_recent(f"all:{q}", 2))
    else:
        # tech/AI mix: news, viral repos, new models, latest AI, fresh papers, top HN stories
        data = _gather(
            lambda: _parse_feed("BBC Tech", BBC_TECH_RSS, 2),
            lambda: _github_trending(3),
            lambda: _huggingface_trending(3),
            lambda: _arxiv_recent(limit=3),
            lambda: _hf_papers(3),
            lambda: _google_news_search("new AI model release OR LLM OR AI research when:7d", 3),
            lambda: _hn_top(3),
        )

    if not data:
        data = [{"title": f"No sources found for: {feedback or 'top news'}", "url": "https://example.com", "source": "mock"}]
    state["research_data"] = data
    return state


if __name__ == "__main__":
    default = research_node({})
    assert default["research_data"], "expected default research items"
    sources = {i["source"] for i in default["research_data"]}
    assert {"GitHub", "HuggingFace", "GoogleNews"} & sources, f"default mix should include tech/AI sources, got {sources}"
    targeted = research_node({"rejection_feedback": "Claude Code AI agent"})
    assert targeted["research_data"], "expected feedback-targeted items"
    assert any(i["source"] in ("GoogleNews", "HackerNews") for i in targeted["research_data"]), "feedback search should hit real search sources"
    print("DEFAULT:")
    for i in default["research_data"]:
        print(f"  [{i['source']}] {i['title']} ({i['url']})")
    print("TARGETED (feedback='Claude Code AI agent'):")
    for i in targeted["research_data"]:
        print(f"  [{i['source']}] {i['title']} ({i['url']})")
