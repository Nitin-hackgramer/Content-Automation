"""Group stories about the same thing into one candidate, and say what evidence backs it.

Dedup lives here, not in fetching or ranking. Two items are the same story when they share a canonical URL
(tracking params, www., youtu.be, twitter.com and the like folded away) or most of their meaningful title words.
Official announcements lead each group, so they name it.

evidence_level says what the data supports, and is deliberately modest:
    multi_source   two or more independent sources: different URLs on different platforms
    engagement     one source with a measured number over the threshold in sources.ENGAGEMENT
    single_source  one source, no measured traction
Independent means a different page from a different speaker. The same article found by two fetchers (an OpenAI
feed entry and a web search hit for that same page) is one source; three tweets about a story are one platform;
an organisation posting its own news (OpenAI's feed + @OpenAI) is one speaker.

claim_status says who backs the story: official / corroborated / unverified. needs_verification marks an
unverified story that says a company released or announced something: a claim, not an established fact.
Nothing here calls a story "viral". That word needs a number, and only multi_source/engagement have one.
"""

import hashlib
import re
import urllib.parse

from agent.discovery.sources import (ENGAGEMENT, OFFICIAL, OFFICIAL_ACCOUNTS, OFFICIAL_DOMAINS, OFFICIAL_GITHUB_OWNERS,
                                     OFFICIAL_ORG)

TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|ref_src)$")

SAME_STORY = 0.5  # share of title words (Jaccard) above which two items are one story
STOP = {"the", "and", "for", "with", "from", "that", "this", "your", "what", "how", "why", "new", "its",
        "are", "you", "can", "just", "now", "will", "into", "about", "more", "out", "via"}


def canonical(url):
    """One spelling per page: https, no www./m., no fragment or tracking params, one form per tweet and video."""
    p = urllib.parse.urlsplit(url.strip())
    host = re.sub(r"^(www\.|m\.|mobile\.)", "", (p.hostname or "").lower())
    path = p.path.rstrip("/")
    query = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    if host == "twitter.com":
        host = "x.com"
    if host == "x.com" and (m := re.match(r"^/[^/]+/status/(\d+)", path)):
        return f"https://x.com/i/status/{m[1]}"
    video = (path[1:] if host == "youtu.be" else
             path[len("/shorts/"):] if host == "youtube.com" and path.startswith("/shorts/") else
             dict(query).get("v") if host == "youtube.com" and path == "/watch" else None)
    if video:
        return f"https://youtube.com/watch?v={video}"
    query = sorted((k, v) for k, v in query if not TRACKING.match(k.lower()))
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(query), ""))


def words(title):
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 2 and w not in STOP}


def overlap(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def engaged(item):
    limit = ENGAGEMENT.get(item.get("metric_name"))
    return bool(limit and (item.get("metric") or 0) >= limit)


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _host_org(host):
    return next((org for dom, org in OFFICIAL_DOMAINS.items() if host == dom or host.endswith("." + dom)), None)


def url_org(url):
    """The organisation whose own page this link is, else None (see sources.OFFICIAL_DOMAINS / OFFICIAL_GITHUB_OWNERS)."""
    p = urllib.parse.urlsplit(url or "")
    host = re.sub(r"^www\.", "", (p.hostname or "").lower())
    if host == "github.com":
        return OFFICIAL_GITHUB_OWNERS.get(p.path.strip("/").split("/")[0].lower())
    return _host_org(host)


def _org(item):
    """The organisation behind an item when it is that organisation's own post: its feed, its account or its page."""
    return (OFFICIAL_ORG.get(item["source"]) or OFFICIAL_ACCOUNTS.get(_norm(item.get("author")))
            or url_org(item["url"]))


def _origin(item):
    """Who is speaking. @OpenAI tweeting its own post is OpenAI again, not a second voice. A repost or quote tweet
    repeats the account it reposts: its voice is that account's (or X's, for an ordinary account), not a new one."""
    if item.get("via"):
        return OFFICIAL_ACCOUNTS.get(_norm(item["via"])) or (item.get("platform") or item["source"]).lower()
    return _org(item) or (item.get("platform") or item["source"]).lower()


def origins(items):
    """The independent voices behind `items`: one origin per canonical URL (its first), affiliated accounts, feeds and
    reposts folded into the voice they repeat. Works on items and on a candidate's evidence entries alike."""
    first = {}
    for i in items:
        first.setdefault(canonical(i["url"]), _origin(i))
    return set(first.values())


def independent(items):
    """Number of independent sources."""
    return len(origins(items))


# A post that says a company released something. Only matters when nobody reliable backs it (see claim_status).
COMPANIES = (r"openai|anthropic|claude|google|gemini|deepmind|meta|microsoft|copilot|github|nvidia|apple|xai|grok"
             r"|mistral|deepseek|amazon|aws|cursor|vercel")
VERBS = (r"released?|launch(?:es|ed)?|drop(?:s|ped)?|announce[sd]?|unveil(?:s|ed)?|ship(?:s|ped)?|open[- ]sourc(?:es|ed)"
         r"|introduc(?:es|ed)|publish(?:es|ed)|rolls? out|rolled out|acquir(?:es|ed)|partner(?:s|ed)?"
         r"|turn(?:s|ed) .{0,30}into|debut(?:s|ed)|reveal(?:s|ed)|rais(?:es|ed)|bann?(?:s|ed)|kill(?:s|ed)|retire[sd]?"
         r"|now (?:supports|offers|lets|allows)")
ANNOUNCE_RE = re.compile(rf"\b(?:{COMPANIES})\b.{{0,40}}\b(?:{VERBS})\b|\b(?:{VERBS})\b.{{0,40}}\b(?:{COMPANIES})\b",
                         re.IGNORECASE)


def claim_status(items):
    """Who backs the story:
        first_party   the organisation itself posted it (feed, account or its own page). That it announced
                      something is established; what it says about its own product is still only its claim.
        corroborated  two or more independent voices (affiliated accounts, feeds and reposts do not count)
        unverified    one third-party voice
    Nothing here has read the claim against its source: this is who said it, not whether it is true.
    """
    if any(_org(i) for i in items):
        return "first_party"
    return "corroborated" if independent(items) >= 2 else "unverified"


def first_party_links(items):
    """Organisations whose own site a tweet's link card points to. Checkable, not checked: no page is fetched."""
    return sorted({org for i in items if (org := _host_org((i.get("link_domain") or "").lower()))})


def announces(items):
    return any(ANNOUNCE_RE.search(f"{i['title']} {i.get('summary') or ''}") for i in items)


def evidence_level(items):
    if independent(items) >= 2:
        return "multi_source"
    if any(engaged(i) for i in items):
        return "engagement"
    return "single_source"


def _candidate(items):
    rep = items[0]
    dates = [i["published"] for i in items if i["published"]]
    status = claim_status(items)
    return {
        "id": hashlib.sha1(rep["url"].encode()).hexdigest()[:12],
        "title": rep["title"],
        "url": rep["url"],
        "canonical_url": canonical(rep["url"]),
        "summary": rep["summary"],
        "sources": sorted({i["source"] for i in items}),
        "independent_sources": independent(items),
        "evidence_level": evidence_level(items),
        "claim_status": status,
        "needs_verification": status == "unverified" and announces(items),  # keyword guess; rank.py asks the model
        "links_to_first_party": first_party_links(items),
        "published": max(dates) if dates else None,
        "evidence": [{"source": i["source"], "platform": i.get("platform") or i["source"], "title": i["title"],
                      "url": i["url"], "author": i.get("author"), "via": i.get("via"),
                      "published": i["published"].isoformat() if i["published"] else None,
                      "metric": i["metric"], "metric_name": i["metric_name"], "metrics": i.get("metrics") or {},
                      "collected_at": i["collected_at"].isoformat() if i.get("collected_at") else None}
                     for i in items],
    }


def cluster(items):
    """→ one candidate per story, most-supported first. Items with no title or URL are dropped."""
    groups = []  # each: {"words": words of the first item, "urls": set, "items": [...]}
    # Feed links are untrusted: only plain web links go on, so a later UI can never get a javascript: or file: link.
    ordered = sorted((i for i in items if i.get("title") and i.get("url", "").startswith(("https://", "http://"))),
                     key=lambda i: (i["source"] not in OFFICIAL, i["url"]))
    for item in ordered:
        w, url = words(item["title"]), canonical(item["url"])
        match = next((g for g in groups if url in g["urls"] or overlap(w, g["words"]) >= SAME_STORY), None)
        if match:
            if any(x["source"] == item["source"] and canonical(x["url"]) == url for x in match["items"]):
                continue  # the same page from the same source, found by a second query: one sighting, not two
            match["items"].append(item)
            match["urls"].add(url)
        else:
            groups.append({"words": w, "urls": {url}, "items": [item]})
    candidates = [_candidate(g["items"]) for g in groups]
    rank_of = {"multi_source": 0, "engagement": 1, "single_source": 2}
    return sorted(candidates, key=lambda c: (rank_of[c["evidence_level"]], -c["independent_sources"], c["title"]))


if __name__ == "__main__":
    a = {"title": "Anthropic ships Claude Code desktop app", "url": "https://a.com/1", "source": "OpenAI",
         "summary": "", "published": None, "metric": None, "metric_name": None}
    b = {"title": "Claude Code desktop app ships from Anthropic", "url": "https://hn.com/2", "source": "HackerNews",
         "summary": "", "published": None, "metric": 300, "metric_name": "points"}
    c = {"title": "Tiny unrelated blog about gardening", "url": "https://g.com/3", "source": "dev.to",
         "summary": "", "published": None, "metric": 5, "metric_name": "reactions"}
    out = cluster([c, b, a])
    assert len(out) == 2, out
    assert out[0]["evidence_level"] == "multi_source" and out[0]["sources"] == ["HackerNews", "OpenAI"], out[0]
    assert out[0]["url"] == "https://a.com/1", "official item should name the story"
    assert out[1]["evidence_level"] == "single_source"
    assert cluster([{**c, "metric": 500, "metric_name": "reactions"}])[0]["evidence_level"] == "engagement"
    assert len(cluster([a, {**a, "source": "Google News"}])) == 1  # same URL merges even with a new source name
    assert cluster([{**a, "title": ""}]) == []
    print("cluster self-check passed")
