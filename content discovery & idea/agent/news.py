"""Daily news digest, sent to Telegram by the TELEGRAM_BOT bot (the same bot that runs approvals).

Pipeline:  fetch feeds -> editor LLM (keep, rewrite headline, one-line summary, name topics)
-> heat scored in code from real numbers -> agent/digest.py lays it out -> send.

Built fresh at send time from feeds that already cover the last ~24h, so nothing is
kept on disk between runs: Render's free disk is wiped on restart anyway, and the Telegram
chat is the archive.
Stories already shown in the last week are skipped when the store (agent/store.py) is
configured; without it a story trending two days running shows twice.

    uv run python -m agent.news --send   # build a real digest and send it (prints it if the bot isn't configured)
"""

import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from agent import digest, llm, store
from agent.nodes import research
from agent.nodes.select import _dedup
from agent.paths import ENV_FILE

load_dotenv(ENV_FILE, override=True)

IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset: no tzdata needed on Windows

MODELS, PAPERS, REPOS, TECH = "🧠 New models", "📄 Research papers", "⭐ Fresh GitHub repos", "🚀 AI & tech news"
WORLD, BUSINESS, INDIA = "🌍 World", "💼 Business", "🇮🇳 India"

# AI/tech is the bulk of the digest, grouped by where each item came from.
TECH_SECTIONS = [
    (MODELS, {"HuggingFace"}),
    (PAPERS, {"arXiv", "HFPapers"}),
    (REPOS, {"GitHub"}),
    (TECH, {"GoogleNews", "HackerNews", "BBC Tech"}),
]
TECH_QUERIES = ["AI model release", "AI startup launch OR funding", "open source AI tool", "developer tools launch"]
GENERAL = [(WORLD, "WORLD"), (BUSINESS, "BUSINESS"), (INDIA, "NATION")]
TOPIC_RSS = "https://news.google.com/rss/headlines/section/topic/{}?hl=en-IN&gl=IN&ceid=IN:en"

# One editor call per group keeps each prompt small enough for Groq's free per-minute token limit.
GROUPS = [[MODELS, PAPERS, REPOS, TECH], [WORLD, BUSINESS, INDIA]]
CAPS = {MODELS: 6, PAPERS: 8, REPOS: 8, TECH: 20, WORLD: 5, BUSINESS: 5, INDIA: 5}
FLOOR = 3  # top headlines are important by definition: the editor may trim world/business/india but not below this
RETRY_WAIT = 30  # seconds: a 429 from Groq clears within the minute
MAX_TOPICS = 4

# Heat is computed, not guessed by the model. Each story gets 0-100 from the numbers its
# source publishes, plus a bonus when several stories are about the same topic.
# ponytail: these are calibration knobs, set by eye from a few real days. Sources are not
# truly comparable (is 300 HN points hotter than the top world headline?), so tune them if
# the 🔥 picks feel off. Upgrade path: z-scores per source once Supabase has a history.
SCALE = {            # log10(points) that counts as 100: source -> what "points" means
    "HackerNews": 3.0,   # 1,000 upvotes
    "GitHub": 4.0,       # 10,000 stars. Set high on purpose: stars on 2-day-old repos are often farmed
    "HuggingFace": 4.0,  # trending score of 10,000
    "HFPapers": 3.0,     # 1,000 upvotes
}
BADGE = {"HackerNews": "▲", "GitHub": "★", "HuggingFace": "↗", "HFPapers": "▲"}
RANKED_TOP, RANKED_STEP, RANKED_MIN = 60, 5, 20  # top-headline feeds are ordered by importance: 60, 55, 50 ...
UNRANKED = 35        # search results and plain feeds carry no ranking signal
TOPIC_BONUS = 20     # per extra story on the same topic, up to 3 extra. News has no numbers, so coverage is its signal
FIRE_MIN = 70        # 🔥 = the day's top MUST_READ stories, one per section at most, if they clear this
NOTABLE = 55         # ⚡
SEEN_DAYS = 7    # a story shown in the last week is not shown again
# Not a "topic": every story in a section would match these, so they would top every chart.
GENERIC_TOPICS = {"ai", "a.i.", "india", "world", "tech", "technology", "news", "business", "startup", "startups",
                  "market", "markets", "ai market", "economy", "government"}

# Display names for sources that aren't news publishers (their titles have no " - Publisher" tail).
SOURCES = {"HuggingFace": "Hugging Face", "arXiv": "arXiv", "HFPapers": "Hugging Face Papers",
           "GitHub": "GitHub", "HackerNews": "Hacker News", "BBC Tech": "BBC"}
PREFIXES = ("Trending AI model on Hugging Face: ", "Trending AI paper: ", "New AI paper: ")

EDITOR_PROMPT = """You edit a morning briefing for a tech/AI professional in India.
Below are candidate stories, each numbered and tagged with its [section].

Keep at most: {caps}.
- Tech sections: keep real AI, software and technology items. Drop anything not about technology.
- Repos: many are spam. Drop any repo named after commercial software (AutoCAD, Microsoft Project,
  Ableton, Photoshop ...), any "optimizer", "booster", "spoofer", "crack" or "cheat", and any whose
  description is vague marketing. When unsure, drop it.
- World / Business / India: keep only genuinely major events, or stories connected to technology or AI.

For every story you keep write:
- "h": a headline of at most 9 words, plain words, no clickbait, no source name.
- "s": ONE sentence of at most 22 words restating the key facts from the title and snippet.
  Do not guess what something does or why it matters if the text does not say so.
Use only facts in the title and snippet. Never add details that are not there.
List stories most important first within each section.

Also name up to {topics} topics that appear in two or more candidates: a product, company, person or
event, 1-3 words, spelled exactly as it appears in the titles.

{items}

Reply with ONLY JSON, like:
{{"keep": [{{"i": 3, "h": "Google ships Gemini 4", "s": "Gemini 4 is out after months of delay and tops coding benchmarks."}}],
  "topics": ["Gemini 4", "OpenAI"]}}"""

EDITOR_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "array", "items": {
            "type": "object",
            "properties": {"i": {"type": "integer"}, "h": {"type": "string"}, "s": {"type": "string"}},
            "required": ["i", "h", "s"], "additionalProperties": False}},
        "topics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["keep", "topics"],
    "additionalProperties": False,
}


def _fetch():
    """→ ordered {section heading: items}. Every source is @_safe, so a dead one is just empty."""
    tech = _dedup(research._gather(
        lambda: research._huggingface_trending(8),
        lambda: research._hf_papers(8),
        lambda: research._arxiv_recent(limit=8),
        lambda: research._github_trending(10, days=2),
        lambda: research._hn_top(15),
        lambda: research._parse_feed("BBC Tech", research.BBC_TECH_RSS, 5),
        *[lambda q=q: research._google_news_search(f"{q} when:1d", 6) for q in TECH_QUERIES],
    ))
    feeds = research._gather(*[lambda h=h, t=t: research._parse_feed(h, TOPIC_RSS.format(t), 15) for h, t in GENERAL])
    sections = {h: [i for i in tech if i.get("source") in sources] for h, sources in TECH_SECTIONS}
    sections.update({h: _dedup([i for i in feeds if i.get("source") == h]) for h, _ in GENERAL})
    return sections


# --------------------------------------------------------------------------- entries

def _text(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _parts(item):
    """→ (headline, source) for an item. Publisher feeds put it after the last " - "."""
    if item.get("short"):
        return _text(item["short"], 120), SOURCES.get(item.get("source"), "")
    title = _text(item.get("title"), 300)
    for prefix in PREFIXES:
        title = title.removeprefix(prefix)
    if item.get("source") in SOURCES:
        return title, SOURCES[item["source"]]
    head, sep, publisher = title.rpartition(" - ")
    if sep and head.strip() and 0 < len(publisher.strip()) <= 40:
        return head.strip(), publisher.strip()
    return title, ""


def _plain(item):
    """An entry without the editor: cleaned headline, first sentence of the snippet. Heat is added by _score."""
    headline, meta = _parts(item)
    summary = _text(item.get("summary"), 400)
    first = summary.split(". ")[0].rstrip(".") if summary else ""
    return {"h": headline, "s": first[:140], "url": item.get("url") or "", "meta": meta}


def _candidates(flat):
    lines = []
    for n, (heading, item) in enumerate(flat):
        headline, _ = _parts(item)
        snippet = _text(item.get("summary"), 160)
        lines.append(f"{n}. [{heading}] {headline}" + (f" :: {snippet}" if snippet else ""))
    return "\n".join(lines)


def _parse(obj, flat, headings):
    """Editor answer → ({heading: [(entry, item)]}, [topic name]). The schema fixes the shape;
    this checks what a schema can't: real indexes, caps, no invented summaries."""
    kept, seen = {h: [] for h in headings}, set()
    for k in obj["keep"]:
        n = k.get("i") if isinstance(k, dict) else None
        if type(n) is not int or n in seen or not 0 <= n < len(flat):
            continue
        heading, item = flat[n]
        if len(kept[heading]) >= CAPS[heading]:
            continue
        plain = _plain(item)
        seen.add(n)
        # A bare title with no snippet gives the editor nothing to summarise but guesses.
        summary = _text(k.get("s"), 220) if item.get("summary") or len(plain["h"].split()) > 6 else ""
        kept[heading].append(({**plain, "h": _text(k.get("h"), 90) or plain["h"], "s": summary}, item))
    topics = [_text(t, 30) for t in obj.get("topics") or []]
    return kept, [t for t in topics if t]


def _edit(flat, headings, api_key):
    """Ask the editor model; one retry after a pause. → (kept, topics) or (None, [])."""
    prompt = EDITOR_PROMPT.format(caps=", ".join(f"{CAPS[h]} in {h}" for h in headings),
                                  topics=MAX_TOPICS, items=_candidates(flat))
    for attempt in (1, 2):
        try:
            # DEEP, not FAST: on real data the 20b model kept an HWID-spoofer repo and dropped the genuine ones.
            # Low effort + a roomy budget: at default effort gpt-oss burns it all reasoning and answers "".
            obj = llm.ask_json(llm.DEEP, prompt, EDITOR_SCHEMA, api_key, reasoning_effort="low", max_tokens=6000)
            kept, topics = _parse(obj, flat, headings)
            if any(kept.values()):
                return kept, topics
            print("News editor kept nothing.", flush=True)  # a confused model, not a day with no news
        except Exception as e:
            # say why: a silent fallback looks like a working editor that just happens to keep the spam
            print(f"News editor failed ({type(e).__name__}: {str(e)[:200]}).", flush=True)
        if attempt == 1:
            time.sleep(RETRY_WAIT)
    print(f"Sending {', '.join(headings)} unedited.", flush=True)
    return None, []


# --------------------------------------------------------------------------- heat

def _points(item):
    p = item.get("points")
    return p if type(p) in (int, float) and p > 0 else 0


def _signal(item, position):
    """0-100 from the number the source publishes, else from where the feed ranked the story."""
    scale, points = SCALE.get(item.get("source")), _points(item)
    if scale and points:
        return min(100, round(100 * math.log10(points + 1) / scale))
    if item.get("source") in (WORLD, BUSINESS, INDIA):
        return max(RANKED_MIN, RANKED_TOP - RANKED_STEP * position)
    return UNRANKED


def _mentions(name, item):
    text = " ".join(str(item.get(k) or "") for k in ("title", "short", "summary"))
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) is not None


def _topic_counts(names, items):
    """The editor only names topics; the counting is done here, by looking for the name in
    every fetched story. → [(name, stories)] for real topics found in two or more."""
    counts = {}
    for name in names:
        if name.lower() in GENERIC_TOPICS or name.lower() in counts:  # first spelling wins: "OpenAI", not "openai"
            continue
        found = sum(_mentions(name, it) for it in items)
        if found >= 2:
            counts[name.lower()] = (name, found)
    return list(counts.values())


def _badge(item):
    mark, points = BADGE.get(item.get("source")), _points(item)
    return f"{mark} {points:,}" if mark and points else ""


def _score(picked, heat, position):
    """picked: {heading: [(entry, item)]}. Adds heat, 🔥/⚡ level and the source's own number to
    each entry, hottest first inside a section. → {heading: [entry]}"""
    for pairs in picked.values():
        for entry, item in pairs:
            together = max((count for name, count in heat if _mentions(name, item)), default=1)
            entry["heat"] = min(100, _signal(item, position.get(id(item), 0)) + TOPIC_BONUS * min(3, together - 1))
            entry["meta"] = " ".join(x for x in (entry["meta"], _badge(item)) if x)
    # 🔥: the hottest story of a section, for the three hottest sections. One per section, or a
    # single busy source (trending models, say) would take all three must-read slots.
    leads = [max(pairs, key=lambda p: p[0]["heat"])[0] for pairs in picked.values() if pairs]
    fire = {id(e) for e in sorted(leads, key=lambda e: -e["heat"])[:digest.MUST_READ] if e["heat"] >= FIRE_MIN}
    out = {}
    for heading, pairs in picked.items():
        for entry, _ in pairs:
            entry["hot"] = 3 if id(entry) in fire else 2 if entry["heat"] >= NOTABLE else 1
        out[heading] = sorted((e for e, _ in pairs), key=lambda e: -e["heat"])  # stable: ties keep the editor's order
    return out


def curate(sections, api_key):
    """→ ({heading: [entry]}, [(topic, stories)]). Any editor failure degrades to plain headlines;
    heat still works then, because it never depended on the model."""
    picked, names = {h: [] for h in sections}, []
    for headings in GROUPS:
        flat = [(h, it) for h in headings for it in sections.get(h, [])]
        if not flat:
            continue
        kept, topics = _edit(flat, headings, api_key) if api_key else (None, [])
        if kept is None:
            kept = {h: [(_plain(i), i) for i in sections.get(h, [])[:CAPS[h]]] for h in headings}
        else:
            for h in (WORLD, BUSINESS, INDIA):
                if h in kept:
                    have = {id(item) for _, item in kept[h]}
                    spare = [(_plain(i), i) for i in sections.get(h, []) if id(i) not in have]
                    kept[h] += spare[:max(0, FLOOR - len(kept[h]))]
        picked.update(kept)
        names += topics
    heat = _topic_counts(names, [it for items in sections.values() for it in items])
    position = {id(it): n for items in sections.values() for n, it in enumerate(items)}
    return _score(picked, heat, position), heat


# --------------------------------------------------------------------------- send

def send(messages, token, chat_id):
    ok = True
    for text in messages:
        try:
            r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, json={
                "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
            }).json()
        except Exception as e:
            r = {"description": type(e).__name__}  # never str(e): httpx errors can carry the token-bearing URL
        if not r.get("ok"):
            ok = False
            # "chat not found" / "bot can't initiate conversation" = press Start on the bot first
            print(f"Digest send failed: {r.get('description')}", flush=True)
    return ok


def run():
    """Fetch → edit → lay out → send. Returns True only if every message was delivered."""
    sections, shown = _fetch(), store.seen("digest", SEEN_DAYS)
    if shown:
        sections = {h: [i for i in items if i.get("url") not in shown] for h, items in sections.items()}
    entries, heat = curate(sections, os.getenv("GROQ_API_KEY"))
    messages = digest.render(entries, heat, datetime.now(IST))
    token, chat_id = os.getenv("TELEGRAM_BOT"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("TELEGRAM_BOT / TELEGRAM_CHAT_ID not set; digest not sent:\n" + "\n\n".join(messages), flush=True)
        return False
    ok = send(messages, token, chat_id)
    if ok:  # only what actually reached you counts as shown; a failed send gets retried in full
        store.mark_seen("digest", [(e["url"], e["h"]) for v in entries.values() for e in v])
    return ok


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--send" in sys.argv:
        print("sent" if run() else "not sent")
    else:
        print(__doc__)
