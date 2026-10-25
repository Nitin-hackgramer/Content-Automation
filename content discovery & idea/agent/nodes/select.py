"""Pick the single best research item to post, replacing the old random.choice.

Dedup (stdlib: URL + fuzzy title + already-posted history) → one 8B call ranks the
survivors and returns {index, angle}. Any failure falls back to the first item, so
selection never blocks the run. No embeddings/vector store — difflib covers a ~20-item batch.
"""

import difflib
import os

from dotenv import load_dotenv

from agent import llm, store
from agent.paths import DATA, ENV_FILE

load_dotenv(ENV_FILE, override=True)

HISTORY_PATH = os.path.join(DATA, "approved_tweets.md")  # written by approval._save_tweet; tweets end with their URL
SIMILAR = 0.85  # title-ratio above this = same story

RANK_PROMPT = """Select the single best item to post for an AI-engineering audience (founders, CTOs, startup builders).
Score each on novelty, practical value, and audience fit, then pick the ONE best.

{items}

Reply with ONLY JSON: {{"index": <0-based index>, "angle": "<one sentence: the hook / why it matters>"}}"""

RANK_SCHEMA = {
    "type": "object",
    "properties": {"index": {"type": "integer"}, "angle": {"type": "string"}},
    "required": ["index", "angle"],
    "additionalProperties": False,
}


def _norm_url(url):
    # drop scheme/query/www so the same article via different trackers collapses
    u = (url or "").split("?")[0].rstrip("/").lower()
    for p in ("https://", "http://", "www."):
        u = u.replace(p, "")
    return u


def _history(path=HISTORY_PATH):
    """Everything already posted, as one lowercase string: the local file plus the urls
    remembered in the store (the file alone resets on every Render restart)."""
    try:
        with open(path, encoding="utf-8") as f:
            local = f.read()
    except OSError:
        local = ""
    return "\n".join([local, *store.seen("posted")]).lower()


def _dedup(items, history=""):
    out = []
    for it in items:
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        if not title:
            continue
        if url and url.lower() in history:  # already posted (approved tweets end with the URL)
            continue
        nurl = _norm_url(url)
        dup = any(
            (nurl and nurl == _norm_url(k["url"]))
            or difflib.SequenceMatcher(None, title.lower(), k["title"].lower()).ratio() > SIMILAR
            for k in out
        )
        if not dup:
            out.append(it)
    return out


def _parse_pick(obj, n):
    # the schema fixes the shape, not the range: clamp an out-of-range index to 0
    idx = int(obj["index"])
    return (idx if 0 <= idx < n else 0), str(obj.get("angle") or "").strip()


def _rank(items, api_key):
    if not api_key or len(items) <= 1:
        return 0, ""
    try:
        listing = "\n".join(f"{i}. [{it['source']}] {it['title']}" for i, it in enumerate(items))
        return _parse_pick(llm.ask_json(llm.FAST, RANK_PROMPT.format(items=listing), RANK_SCHEMA, api_key), len(items))
    except Exception:
        return 0, ""  # graceful: first survivor, no angle


def select_node(state):
    items = _dedup(state.get("research_data") or [], _history()) or state.get("research_data") or []
    idx, angle = _rank(items, os.getenv("GROQ_API_KEY"))
    state["selected_item"] = items[idx]
    state["angle"] = angle
    return state


if __name__ == "__main__":
    os.environ.pop("GROQ_API_KEY", None)  # deterministic: rank falls back to index 0
    items = [
        {"title": "New AI model released by OpenAI", "url": "https://a.com/x", "source": "News"},
        {"title": "New AI model released by OpenAI!", "url": "https://a.com/x?utm=1", "source": "Dup"},  # same url+title
        {"title": "A totally different story about Rust compilers", "url": "https://b.com/y", "source": "HN"},
    ]
    assert len(_dedup(items)) == 2, _dedup(items)  # near-duplicate collapses
    kept = _dedup(items, history="we posted https://b.com/y last week")
    assert all("b.com/y" not in i["url"] for i in kept), kept  # already-posted URL excluded
    assert _parse_pick({"index": 1, "angle": "hook"}, 3) == (1, "hook")
    assert _parse_pick(llm.extract_json('sure: {"index": 2, "angle": "x"} ok'), 3) == (2, "x")  # JSON in prose
    assert _parse_pick({"index": 9, "angle": ""}, 3) == (0, "")  # out-of-range clamps to 0
    st = select_node({"research_data": items})
    assert st["selected_item"] in items and st["angle"] == ""  # valid pick without a key
    print("select dedup + parse + fallback self-check passed")
