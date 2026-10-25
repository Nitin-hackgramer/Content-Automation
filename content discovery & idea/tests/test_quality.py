"""Offline regression tests for the second quality pass: seen-filter, claim verification, ranking, brief accuracy.
No network, no Groq, no Supabase; the real ledger is never touched.

    .venv\\Scripts\\python -m tests.test_quality
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import store  # noqa: E402
from agent.discovery import cluster, rank, reach, scripts, sources  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
TWEET_BASE = {"url": "https://x.com/i/status/99", "author": "someone", "published_at": "Wed Oct 21 19:09:35 +0000 2026",
              "text": "A new coding agent for developers"}
PERFECT = {"audience_appeal": 100, "creator_relevance": 100, "video_fit": 100, "production_effort": 1,
           "format": rank.FORMATS[0], "hook": "h", "reason": "r"}


def it(title, url, source, platform=None, author=None, summary="", likes=None, published=NOW, **extra):
    return sources.normalize({"title": title, "url": url, "source": source, "platform": platform or source,
                              "author": author, "summary": summary, "published": published,
                              "metric": likes, "metric_name": "likes" if likes else None, **extra}, NOW)


def cand(*items):
    return cluster.cluster(list(items))[0]


def score(c):
    return rank._assemble(c, rank.heuristic(c, NOW), PERFECT)


# ---- 2. claim verification -----------------------------------------------------------------------------------

def check_claim_status():
    pdf = "Anthropic just dropped a 15-page PDF on building your own Claude Duet"
    third = cand(it(pdf, "https://x.com/i/status/1", "X search", "X", "choopyplug1", likes=900))
    assert third["claim_status"] == "unverified" and third["needs_verification"] is True
    own = cand(it(pdf, "https://x.com/i/status/2", "X search", "X", "claudeai", likes=900))
    assert own["claim_status"] == "first_party" and own["needs_verification"] is False, "the company's own post"
    feed = cand(it("OpenAI releases Decisions API", "https://openai.com/a", "OpenAI"))
    assert feed["claim_status"] == "first_party" and not feed["needs_verification"]
    two = cand(it(pdf, "https://x.com/i/status/3", "X search", "X", "dev_a"),
               it(pdf + " explained", "https://youtu.be/v1", "YouTube search", "YouTube", "Some Channel"))
    assert two["claim_status"] == "corroborated" and two["needs_verification"] is False
    tutorial = cand(it("Learn 99% of Claude Code in one video", "https://youtu.be/v2", "YouTube search", "YouTube", "Tech With Tim"))
    assert tutorial["claim_status"] == "unverified" and tutorial["needs_verification"] is False, "no announcement claimed"
    # the same speaker twice is still one voice
    same = cand(it(pdf, "https://x.com/i/status/4", "X search", "X", "a"), it(pdf, "https://x.com/i/status/5", "X search", "X", "b"))
    assert same["claim_status"] == "unverified"


def check_unverified_claim_cannot_rank_high():
    pdf = "Anthropic just dropped a 15-page PDF on building your own Claude Duet"
    third = cand(it(pdf, "https://x.com/i/status/1", "X search", "X", "choopyplug1", likes=900))
    own = cand(it(pdf, "https://x.com/i/status/2", "X search", "X", "claudeai", likes=900))
    got, ref = score(third), score(own)
    assert got["score"] <= 55 and "unverified_claim" in got["penalties"], got["score"]
    assert ref["score"] > 80 and "unverified_claim" not in ref["penalties"], "official posts are not demoted"
    assert got["evidence_level"] == "engagement", "the engagement label alone used to let it rank high"


# ---- 3. ranking ----------------------------------------------------------------------------------------------

def check_low_value_and_bait_are_capped():
    changelog = cand(it("Copilot now supports custom agents in the IDE", "https://github.blog/c1", "GitHub changelog"))
    # literal numbers on purpose: a test that compares with the constant would pass if the constant were loosened
    assert score(changelog)["score"] <= 65 and "changelog_only" in score(changelog)["penalties"]
    admin = cand(it("Usage metrics API adds usage from tools like OpenCode", "https://github.blog/c2", "GitHub changelog"))
    assert score(admin)["score"] <= 55 and "low_value_update" in score(admin)["penalties"]
    monthly = cand(it("GitHub Copilot in Visual Studio — September update", "https://github.blog/c3", "GitHub changelog"))
    assert "low_value_update" in score(monthly)["penalties"]
    bait = cand(it("STOP WASTING HOURS TRYING TO FIGURE OUT WHAT TO LEARN IN AI. Practical roadmap", "https://x.com/i/status/9",
                   "X search", "X", "someone", likes=1963,
                   summary="roadmap VIDEOS: 1. LLM Intro https://t.co/a 2. LLMs from Scratch https://t.co/b 3. Agents https://t.co/c"))
    assert score(bait)["score"] <= 50 and "engagement_bait" in score(bait)["penalties"]
    goldmine = cand(it("GitHub is a GOLDMINE for AI game developers", "https://x.com/i/status/8", "X search", "X", "z", likes=1800))
    assert "engagement_bait" in score(goldmine)["penalties"]
    # a real explainer is untouched
    good = cand(it("How coding agents use tools: a beginner explainer", "https://x.com/i/status/7", "X search", "X", "dev", likes=900,
                   summary="A short walkthrough with one link https://t.co/a"))
    assert not score(good)["penalties"], score(good)["penalties"]
    # an official, non-admin changelog line that is not low-value keeps its official credibility
    assert rank.penalties(cand(it("Copilot coding agent can now open pull requests", "https://github.blog/c4", "GitHub changelog"))) == {
        "changelog_only": rank.CHANGELOG_CAP}


def check_penalised_items_do_not_crowd_the_pool():
    noisy = [cand(it(f"Copilot admin audit log update number {n}", f"https://github.blog/n{n}", "GitHub changelog"))
             for n in range(rank.LLM_POOL + 2)]
    good = [cand(it(f"Beginner walkthrough of coding agents part {n}", f"https://x.com/i/status/{n + 100}", "X search", "X",
                    f"dev{n}", likes=900)) for n in range(3)]
    ranked, _ = rank.rank(noisy + good, NOW, None)
    assert len(ranked) == rank.LLM_POOL
    assert {g["id"] for g in good} <= {r["id"] for r in ranked}, "good stories lost their pool slots to changelog lines"
    assert [r["id"] for r in ranked[:3]] == [r["id"] for r in sorted(ranked, key=lambda r: -r["score"])[:3]]
    assert {r["id"] for r in ranked[:3]} == {g["id"] for g in good}, "good stories rank above the capped ones"


def check_rank_prompt_states_priorities():
    for phrase in ("beginner-friendly explainers", "practical AI tools", "developer experiences", "startup stories",
                   "engagement bait", "changelog", "not a fact"):
        assert phrase in rank.RANK_PROMPT, phrase


# ---- 4. brief accuracy ---------------------------------------------------------------------------------------

def check_hook_hype_is_dropped():
    c = cand(it("Build more responsive apps with the Decisions API", "https://openai.com/d", "OpenAI",
                summary="Fast, focused, low-cost decisions for any application."))
    hyped = rank._assemble(c, rank.heuristic(c, NOW), {**PERFECT, "hook": "Build faster with OpenAI's brand-new Decisions API!"})
    assert hyped["hook"] == "" and "brand new" in hyped["warnings"][0], hyped["warnings"]
    c2 = cand(it("Introducing the brand-new Decisions API", "https://openai.com/e", "OpenAI"))
    kept = rank._assemble(c2, rank.heuristic(c2, NOW), {**PERFECT, "hook": "Meet the brand-new Decisions API"})
    assert kept["hook"] and kept["warnings"] == [], "hype the source itself uses is allowed"


def check_brief_attribution_and_warnings():
    c = cand(it("STOP WASTING HOURS. One roadmap with videos and repos", "https://x.com/i/status/5", "X search", "X", "Mohiniuni",
                likes=1963, summary="I put together one practical roadmap with videos"))
    brief = scripts.finish({
        "hook": "Here's the ultimate AI roadmap",
        "beats": [{"say": "I'm sharing a practical AI learning roadmap so you stop guessing.", "show": "x"},
                  {"say": "Mohiniuni's roadmap lists three videos to start with.", "show": "y"}],
        "on_screen_text": ["My roadmap: videos, repos, courses"], "claims_to_verify": [], "cta": "c"}, c)
    text = " | ".join(brief["warnings"])
    assert "unsupported wording 'ultimate'" in text, text
    assert "beat 1: claims experience" in text and "on-screen text: claims experience" in text, text
    assert "beat 2" not in text, "naming the author is the right way to do it"
    assert brief["credit"] == ["Mohiniuni on X"], brief["credit"]
    assert brief["verified_facts"] == [], "missing field filled, not invented"

    # hype the source itself uses is not flagged
    c2 = cand(it("The ultimate guide to agents", "https://dev.to/u", "dev.to", author="writer"))
    assert scripts.check({"hook": "The ultimate guide", "beats": [], "on_screen_text": []}, c2) == []


def check_brief_prompt_separates_facts_from_claims():
    prompts = []
    saved = scripts.llm.ask_json
    try:
        scripts.llm.ask_json = lambda model, prompt, schema, key, **o: (prompts.append(prompt) if prompt.startswith("Write a") else None) or {"hook": "h", "beats": [], "on_screen_text": [], "verified_facts": [], "claims_to_verify": [], "cta": "c"}
        pdf = "Anthropic just dropped a 15-page PDF on building your own Claude Duet"
        third = score(cand(it(pdf, "https://x.com/i/status/1", "X search", "X", "choopyplug1", likes=900)))
        official = score(cand(it(pdf, "https://x.com/i/status/2", "X search", "X", "claudeai", likes=900)))
        brief = scripts.build_script(third, "k")
        scripts.build_script(official, "k")
    finally:
        scripts.llm.ask_json = saved
    unverified, official_prompt = prompts
    assert "UNVERIFIED" in unverified and "choopyplug1 on X" in unverified, "claim status and author reach the model"
    assert "FIRST-PARTY" in official_prompt and "UNVERIFIED" not in official_prompt
    for rule in ("belongs to them", "verified_facts", "No hype words", "has not yet tried"):
        assert rule in unverified, rule
    assert any("unverified claim" in w for w in brief["warnings"]), "standing warning on an unverified claim"
    assert brief["claim_status"] == "unverified"
    assert "verified_facts" in scripts.SCRIPT_SCHEMA["required"]


def check_found_in_live_run():
    """Three things the first live run of this pass showed (25 Oct 2026)."""
    # the same tweet returned by two queries was listed twice in a story's evidence
    a = it("Claude and formal verification at scale", "https://x.com/i/status/9", "X search", "X", "AnthropicAI", likes=9650)
    b = it("Claude and formal verification at scale", "https://twitter.com/AnthropicAI/status/9", "X search", "X", "AnthropicAI", likes=9651)
    assert len(cand(a, b)["evidence"]) == 1, "one sighting per source and page"
    web = it("Claude and formal verification at scale", "https://x.com/i/status/9", "Web search", "Web", None)
    assert len(cand(a, web)["evidence"]) == 2, "a different source finding the same page is still provenance"

    # an idea hook spoke for the creator about someone else's launch
    c = cand(it("Today, I'm launching - the hands-on way to learn AI engineering", "https://x.com/i/status/10", "X search", "X",
                "adxtyahq", likes=1536))
    got = rank._assemble(c, rank.heuristic(c, NOW), {**PERFECT, "hook": "I'm launching a hands-on AI engineering lab with 50+ labs."})
    assert got["hook"] == "" and "creator has not done" in got["warnings"][0], got["warnings"]
    ok = rank._assemble(c, rank.heuristic(c, NOW), {**PERFECT, "hook": "adxtyahq just launched a hands-on AI engineering course."})
    assert ok["hook"], "naming the person who launched it is the right way"

    # run 2's #1: hype tweet, claim phrased "turned ... into", hook stated it as fact and fused two claims
    text = "THIS IS F*CKING INSANE Anthropic turned Agent Skills into an open spec and GitHub filled up with ready-made teams"
    hype = cand(it(text, "https://x.com/i/status/12", "X search", "X", "randomdev", likes=2000))
    assert hype["needs_verification"] is True, "'turned ... into' is an announcement claim"
    assert not cand(it(text, "https://x.com/i/status/13", "X search", "X", "claudeai", likes=2000))["needs_verification"]
    got = score(hype)
    assert got["score"] <= 50 and {"engagement_bait", "unverified_claim"} <= set(got["penalties"]), got["penalties"]
    stated = rank._assemble(hype, rank.heuristic(hype, NOW), {**PERFECT, "hook": "Anthropic just open sourced an Agent Skills spec."})
    assert stated["hook"] == "" and any("attributed" in w for w in stated["warnings"]), stated["warnings"]
    assert rank.BAIT_RE.search("This is insane") and not rank.BAIT_RE.search("How coding agents use tools")
    assert "says" in rank.RANK_PROMPT and "benchmark" in rank.RANK_PROMPT.lower()

    # a company's own launch post from an account the list should know
    kimi = cand(it("Introducing Kimi K3.1 Pro: our most capable open-weight model", "https://x.com/i/status/11", "X search", "X",
                   "Kimi_Moonshot", likes=28759))
    assert kimi["claim_status"] == "first_party"


def check_claim_structure():
    """Who said it is read from accounts, feeds and URLs, not guessed from wording. Nothing here reads the claim itself."""
    # an organisation's own pages count; lookalikes and open-publishing hosts do not
    for url, want in [("https://openai.com/index/decisions-api", "openai"), ("https://www.anthropic.com/news/x", "anthropic"),
                      ("https://github.com/NVIDIA/OpenShell", "nvidia"), ("https://blog.google/technology/ai/x/", "google"),
                      ("https://notopenai.com/a", None), ("https://openai.com.evil.io/a", None),
                      ("https://github.com/randomuser/openai-clone", None), ("https://huggingface.co/someone/model", None),
                      ("https://example.com/a", None)]:
        assert cluster.url_org(url) == want, (url, cluster.url_org(url))
    hn = cand(it("Show HN: OpenShell, a runtime for agents", "https://github.com/NVIDIA/OpenShell", "HackerNews", author="submitter"))
    assert hn["claim_status"] == "first_party", "the repo itself is NVIDIA's"
    clone = cand(it("Show HN: OpenAI clone in 200 lines", "https://github.com/randomuser/openai-clone", "HackerNews", author="submitter"))
    assert clone["claim_status"] == "unverified"

    # reposts and quote tweets repeat a voice; they do not add one
    claim = "Anthropic turned Agent Skills into an open spec"
    original = it(claim, "https://x.com/i/status/21", "X search", "X", "randomdev", likes=900)
    quote = it(claim + " wow", "https://x.com/i/status/22", "X search", "X", "otherdev", likes=300, via="randomdev")
    video = it(claim + " explained", "https://youtu.be/v9", "YouTube search", "YouTube", "A Channel")
    assert cluster.independent([original, quote]) == 1, "X plus a quote of X is still X"
    assert cluster.claim_status([original, quote]) == "unverified"
    assert cluster.claim_status([original, quote, video]) == "corroborated", "a different platform does corroborate"
    relay = it("OpenAI releases Decisions API", "https://x.com/i/status/23", "X search", "X", "fan", via="OpenAI")
    feed = it("OpenAI releases Decisions API", "https://openai.com/index/decisions-api", "OpenAI")
    assert cluster.independent([feed, relay]) == 1, "a repost of @OpenAI next to OpenAI's own page is OpenAI once"

    # a link card into an organisation's site is surfaced as checkable, and is not treated as verified
    linked = cand(it("Anthropic released a new thing", "https://x.com/i/status/24", "X search", "X", "randomdev", likes=900,
                     link_domain="anthropic.com"))
    assert linked["claim_status"] == "unverified" and linked["links_to_first_party"] == ["anthropic"]

    # parse_tweets fills via and link_domain from what the platform gave
    tweet = dict(TWEET_BASE)
    parsed = reach.parse_tweets([{**tweet, "raw": {"likes": 5, "quoted_tweet": {"author": "claudeai"},
                                                   "card": {"domain": "anthropic.com", "title": "Claude agents"}}}])[0]
    assert parsed["via"] == "claudeai" and parsed["link_domain"] == "anthropic.com"
    rt = reach.parse_tweets([{**tweet, "text": "RT @OpenAI: Decisions API for agents", "raw": {"likes": 5}}])[0]
    assert rt["via"] == "OpenAI"
    assert reach.parse_tweets([{**tweet, "raw": {"likes": 5}}])[0]["via"] is None, "no repost, no via"

    # no evidence, no verification: a story with one anonymous voice stays unverified
    assert cand(it("Claude released a thing", "https://x.com/i/status/25", "X search", "X"))["claim_status"] == "unverified"


def check_model_decides_whether_a_claim_is_news():
    """rank asks the model; the keyword guess is only the fallback. Both directions."""
    # the keywords see no announcement ("showed up"), the model does
    missed = cand(it("Anthropic showed up with agent skills as a standard", "https://x.com/i/status/31", "X search", "X", "randomdev",
                     likes=900))
    assert missed["needs_verification"] is False
    by_model = rank._assemble(missed, rank.heuristic(missed, NOW), {**PERFECT, "asserts_company_news": True})
    assert by_model["needs_verification"] is True and "unverified_claim" in by_model["penalties"]
    assert by_model["claim_check"] == "model" and by_model["score"] <= 55
    # the keywords fire on a tutorial title; the model says it is not news
    tutorial = cand(it("Claude launches into a full tutorial on agents", "https://x.com/i/status/32", "X search", "X", "randomdev",
                       likes=900))
    assert tutorial["needs_verification"] is True
    by_model = rank._assemble(tutorial, rank.heuristic(tutorial, NOW), {**PERFECT, "asserts_company_news": False})
    assert by_model["needs_verification"] is False and "unverified_claim" not in by_model["penalties"]
    # model gave nothing usable: fall back to the keyword guess and say so
    for bad in ({**PERFECT}, {**PERFECT, "asserts_company_news": "yes"}):
        got = rank._assemble(tutorial, rank.heuristic(tutorial, NOW), bad)
        assert got["claim_check"] == "keyword fallback" and got["needs_verification"] is True
    # a first-party post is never marked as needing verification, whatever the model says
    own = cand(it("We released a new thing", "https://x.com/i/status/33", "X search", "X", "claudeai", likes=900))
    assert rank._assemble(own, rank.heuristic(own, NOW), {**PERFECT, "asserts_company_news": True})["needs_verification"] is False
    assert "asserts_company_news" in rank.RANK_SCHEMA["properties"]["ideas"]["items"]["required"]


if __name__ == "__main__":
    check_claim_structure()
    check_model_decides_whether_a_claim_is_news()
    check_claim_status()
    check_unverified_claim_cannot_rank_high()
    check_low_value_and_bait_are_capped()
    check_penalised_items_do_not_crowd_the_pool()
    check_rank_prompt_states_priorities()
    check_hook_hype_is_dropped()
    check_brief_attribution_and_warnings()
    check_brief_prompt_separates_facts_from_claims()
    check_found_in_live_run()
    print("quality tests passed")
