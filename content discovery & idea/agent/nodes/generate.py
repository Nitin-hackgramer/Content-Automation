import os
import random
import re

import httpx
from dotenv import load_dotenv

from agent import llm
from agent.paths import DATA, ENV_FILE

load_dotenv(ENV_FILE, override=True)

PROMPT = """Write one engaging tweet (max 260 characters, max 3 relevant hashtags) based on this item.
Structure: Hook -> Insight -> optional CTA. Punchy, 1-2 sentences.
End the tweet with the URL so readers can click through.
Angle to lead with: {angle}
Source: {source}
Title: {title}
URL: {url}

Reply with ONLY the tweet text, no surrounding quotes."""

TEMPLATE_PATH = os.path.join(DATA, "linkedin_post_template.md")
MAX_WORDS_PER_LINE = 14
LI_MAX_CHARS = 1900  # past this LinkedIn reach drops off; 3000 is only the hard API cap

# Voice rules and hook formulas condensed from github.com/sergebulaev/linkedin-skills (MIT):
# references/voice-rules.md and references/hook-formulas.md.
# Word stems that read as AI-written. "harness"/"ecosystem" are left out: real terms in this niche.
AI_TELLS = re.compile(
    r"\b(leverag|utiliz|facilitat|streamlin|robust|seamless|delv|navigat|unlock|foster|cultivat"
    r"|fundamentally|essentially|ultimately|crucially|notably|landscape|paradigm|realm|tapestry|journey"
    r"|game.?changer|deep dive|at the end of the day|fast.paced world)\w*", re.IGNORECASE)  # \w*: report the whole word, not the stem
GENERIC_CLOSES = {"what do you think?", "thoughts?", "agree?", "do you agree?"}
# Only formulas that work from a news item alone; the personal-story ones would make the model invent a past.
# Written as plain instructions with no "Name:" label, because the model copies a label into the post.
HOOKS = [
    "say that something happened, hold back what, and reveal the specific thing by line three.",
    "name a belief everyone repeats about this topic, then show why this item breaks it.",
    "everyone picks answer A or answer B, kill each in one line, then give the third way.",
    "contrast two approaches that look alike today, and where each one ends up in six months.",
    "state the obvious reason this matters, say it is wrong, then give the real reason.",
    "take the jargon in the title and say it the way you would to a smart friend.",
    "start with 'I don't know who needs to hear this, but' and a reassuring truth this item proves.",
]


def _examples(path=TEMPLATE_PATH):
    # few-shot the writer with the user's own reference posts; absent file just drops the section
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


LINKEDIN_PROMPT = """Act as my LinkedIn content strategist for AI engineering. Write about AI agents, RAG, MCP,
multi-agent systems, and production AI for founders, CTOs, and startup builders.

Write ONE post based on the item below, 120-250 words, matching the STYLE of the reference posts exactly.

STYLE RULES — mandatory:
{rules}
- How to open (follow this, never name or label it): {hook}

REFERENCE POSTS (copy this rhythm, not the topics):
{examples}

NOW WRITE ABOUT THIS:
Angle to lead with: {angle}
Source: {source}
Title: {title}
What the source says: {snippet}

Reply with ONLY the post text. No preamble, no title, no quotes, no URL."""

# The rules block is the part scripts/optimize_linkedin.py tunes (DSPy, offline). A tuned
# version is only written to RULES_PATH when it scores better than this one on held-out
# stories; delete that file to come back to these.
RULES = f"""- One short sentence per line. Never join two sentences on one line. Max {MAX_WORDS_PER_LINE} words a line.
- A blank line between every single line. No paragraph blocks, ever.
- No hashtags. No em-dashes. No bullet characters ("-", "*", numbers) starting a line.
- Open with a hook line that stands alone. Close with a specific question to the reader, never "Thoughts?".
- Conversational and specific, like the reference posts. Not a press release.
- Name the actual product or company from the title. You only know the title and what the source says:
  never invent statistics, features, plans, or personal stories that they do not state."""
RULES_PATH = os.path.join(DATA, "linkedin_rules.txt")


def _rules(path=RULES_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or RULES
    except OSError:
        return RULES


def linkedin_prompt(item, angle, rules=None, hook=None):
    """The full writer prompt. rules/hook can be pinned so the optimizer and tests get a repeatable prompt."""
    fields = {**item, "angle": angle or "(none)", "examples": _examples(), "rules": rules or _rules(),
              "hook": hook or random.choice(HOOKS), "snippet": item.get("summary") or "(nothing beyond the title)"}
    return LINKEDIN_PROMPT.format(**fields)


def _ensure_link(draft, url, limit=280):
    # LLM sometimes drops the URL; guarantee it's attached (repos/articles need the link).
    if not url or url == "https://example.com" or url in draft:
        return draft[:limit]
    # Google News links run 300+ chars. Without the max(0, ...) the slice index goes
    # negative, keeps almost the whole draft, and the result blows past the limit.
    room = limit - len(url) - 1
    if room <= 0:
        return url[:limit]  # link alone doesn't fit; it's the only part that must survive
    return f"{draft[:room].rstrip()} {url}"


def _mock_draft(item):
    return f"{item['title']} — {item['url']}"[:280]


def _parse_og(html_text):
    m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html_text)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html_text))
    return m.group(1) if m else None


def _og_image(url):
    # scrape the source page's social-preview image so the tweet draft ships with a picture
    if not url or url.startswith("https://example.com"):
        return None
    try:
        r = httpx.get(url, timeout=8, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        return _parse_og(r.text)
    except Exception:
        return None


# Capitalised words that are fine in a post even when the source never used them.
KNOWN_WORDS = {"i", "ai", "rag", "mcp", "llm", "llms", "api", "apis", "gpu", "gpus", "cto", "ctos", "ceo", "ceos",
               "linkedin", "saas", "ml", "ui", "ux", "sdk", "cli", "os", "it", "us", "uk", "eu"}


def _ungrounded(text, item, angle=""):
    """Numbers and names in the post that the source never mentioned: the usual shape of an
    invented fact. A heuristic, not a fact-checker. It can only say "this did not come from
    the title or snippet", and it misses anything spelled in plain lowercase words.
    """
    source = " ".join(str(item.get(k) or "") for k in ("title", "short", "summary", "source")).lower() + " " + (angle or "").lower()
    found = []
    # not digits glued to a name ("GPT-6", "Tool0"): those are judged as part of the name below
    for number in re.findall(r"(?<![A-Za-z\-‑])(?:\d[\d,.]*\d|\d)", text):
        if number.replace(",", "") not in source.replace(",", ""):
            found.append(number)
    for sentence in re.split(r"(?<=[.!?:])\s+|\n+", text):
        for word in re.findall(r"[A-Za-z][\w+\-']*", sentence)[1:]:  # [1:] a sentence always starts with a capital
            bare = word[:-2] if word.lower().endswith("'s") else word
            if word[0].isupper() and bare.lower() not in KNOWN_WORDS and bare.lower() not in source:
                found.append(bare)
    return list(dict.fromkeys(found))  # each once, in reading order


def quality(text, item, angle=""):
    """0-1 score of a LinkedIn draft against the rules above. No labels needed, so it can
    judge any draft: used by the offline optimizer and to compare prompts before adopting one."""
    style = len(_li_issues(text))
    invented = len(_ungrounded(text, item, angle))
    return max(0.0, round(1 - 0.15 * style - 0.08 * min(invented, 6), 2))


def _li_issues(text, item=None, angle=""):
    """Style violations vs. linkedin_post_template.md, plus invented details when the source
    item is given. Empty list = matches the template."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    issues = []
    if len(lines) < 6:
        issues.append("too few lines, break every sentence onto its own line")
    if any(len(l.split()) > MAX_WORDS_PER_LINE for l in lines):
        issues.append(f"some lines exceed {MAX_WORDS_PER_LINE} words, split them")
    if any(l[0] in "-*•" or l[:2].rstrip(".").isdigit() for l in lines):
        issues.append("remove bullet/number line prefixes, the reference posts use none")
    if "#" in text:
        issues.append("remove all hashtags")
    if "—" in text or "–" in text:
        issues.append("remove em-dashes and en-dashes")
    if lines and not lines[-1].endswith("?"):
        issues.append("end with a question to the reader")
    elif lines and lines[-1].lower() in GENERIC_CLOSES:
        issues.append("the closing question is generic, ask something specific to this topic")
    tells = sorted({m.group(0).lower() for m in AI_TELLS.finditer(text)})
    if tells:
        issues.append(f"remove these AI-sounding words: {', '.join(tells)}")
    if "http://" in text or "https://" in text:
        issues.append("remove the URL, the link is posted separately")
    if len(text) > LI_MAX_CHARS:
        issues.append(f"too long, cut it to under {LI_MAX_CHARS} characters")
    invented = _ungrounded(text, item, angle) if item else []
    if invented:
        issues.append(f"these are not in the source, remove them or say only what the source says: {', '.join(invented[:8])}")
    return issues


def _linkedin_draft(item, angle, api_key):
    # long-form strategist post from the same research item; the bigger model for prose
    if not api_key:
        return None
    prompt = linkedin_prompt(item, angle)
    try:
        text = llm.ask(llm.DEEP, prompt, api_key)
        issues = _li_issues(text, item, angle)
        if issues:  # ponytail: one corrective retry, not a critic loop — 2nd pass fixes it in practice
            text = llm.ask(llm.DEEP, f"{prompt}\n\nYour previous attempt:\n{text}\n\n"
                                     f"It broke these rules: {'; '.join(issues)}.\nRewrite it fixing exactly those.",
                           api_key)
    except Exception as e:
        # Don't swallow this into None: a text-less run used to still send the og:image
        # with an empty caption, which can't be posted. Surface it and fall back to text.
        print(f"LinkedIn generation failed ({e!r}); falling back to a templated draft.")
        text = item["title"]
    # No link in the body: LinkedIn throttles posts that carry one (the posting step that
    # used to add it as the first comment was removed). Strip it in case the model ignored the instruction; an empty
    # result falls back to the title so there is always something to approve.
    text = text.replace(item["url"], "").strip() if item.get("url") else text
    return (text or item["title"])[:3000]  # LinkedIn commentary limit


def _tweet_draft(item, angle, api_key):
    if not api_key:
        return _mock_draft(item)
    try:
        draft = llm.ask(llm.FAST, PROMPT.format(angle=angle or "(none)", **item))[:280]
    except Exception as e:
        # same contract as LinkedIn: always return text, never crash the graph
        print(f"Tweet generation failed ({e!r}); falling back to a templated draft.")
        draft = _mock_draft(item)
    return _ensure_link(draft, item["url"])


def generate_node(state):
    item = state.get("selected_item") or random.choice(state["research_data"])  # select_node picks; random is a fallback
    angle = state.get("angle") or ""
    api_key = os.getenv("GROQ_API_KEY")
    target = state.get("target", "both")  # which platform(s) this run should draft

    draft = _tweet_draft(item, angle, api_key) if target in ("x", "both") else None
    state["selected_draft"] = draft
    state["linkedin_draft"] = _linkedin_draft(item, angle, api_key) if target in ("li", "both") else None
    state["selected_image"] = _og_image(item["url"])
    state.setdefault("retry_count", 0)
    return state


if __name__ == "__main__":
    os.environ.pop("GROQ_API_KEY", None)  # deterministic no-LLM path for the check
    item = {"title": "Test headline", "url": "https://example.com", "source": "mock"}
    result = generate_node({"research_data": [item], "selected_item": item, "angle": ""})
    assert len(result["selected_draft"]) <= 280
    assert "https://example.com" in result["selected_draft"]  # draft is grounded in the sourced item
    # _ensure_link appends a dropped URL and stays under 280
    linked = _ensure_link("cool repo no link here", "https://github.com/a/b")
    assert linked.endswith("https://github.com/a/b") and len(linked) <= 280
    assert _ensure_link("already https://x.co", "https://x.co") == "already https://x.co"  # no dupe
    # regression: a Google-News-length URL must not push the tweet over the limit
    long_url = "https://news.google.com/rss/articles/" + "A" * 400
    assert len(_ensure_link("some draft text", long_url)) <= 280
    assert len(_ensure_link("some draft text", "https://a.co/" + "b" * 200)) <= 280
    # _parse_og pulls the image URL regardless of meta attribute order, None when absent
    assert _parse_og('<meta property="og:image" content="https://img.png">') == "https://img.png"
    assert _parse_og('<meta content="https://i2.png" property="og:image">') == "https://i2.png"
    assert _parse_og("<html>no og here</html>") is None
    # target="li" skips drafting a tweet entirely (no wasted call)
    li_only = generate_node({"research_data": [item], "selected_item": item, "angle": "", "target": "li"})
    assert li_only["selected_draft"] is None, li_only
    # _li_issues: a template-shaped post passes, each violation is caught
    good = "Agents keep breaking in production.\n\nNot because the models are weak.\n\nBecause the tools lie.\n\nI shipped one last week.\n\nIt failed on retries.\n\nWhat broke your last agent?"
    assert _li_issues(good) == [], _li_issues(good)
    assert any("14 words" in i for i in _li_issues(good.replace("Because the tools lie.", "x " * 20)))
    assert any("hashtag" in i for i in _li_issues(good + " #ai"))
    assert any("em-dash" in i for i in _li_issues(good.replace("weak.", "weak — really.")))
    assert any("question" in i for i in _li_issues(good.rstrip("?") + "."))
    assert any("bullet" in i for i in _li_issues(good.replace("It failed", "- It failed")))
    assert any("too few lines" in i for i in _li_issues("One line only?"))
    assert _examples(), "linkedin_post_template.md should load as few-shot examples"
    print(result["selected_draft"])
