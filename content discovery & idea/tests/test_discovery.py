"""Offline tests for the discovery workflow. No network, no Groq, no Supabase.

    .venv\\Scripts\\python -m tests.test_discovery
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import store  # noqa: E402
from agent.discovery import cluster, rank, run, scripts, seen, sources, trends  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Codex gets new sandbox</title><link>https://openai.com/a</link>
<pubDate>Fri, 23 Oct 2026 17:00:00 GMT</pubDate><description>&lt;p&gt;Sandbox for agents&lt;/p&gt;</description></item>
<item><title>No link here</title><pubDate>Fri, 23 Oct 2026 17:00:00 GMT</pubDate></item>
</channel></rss>"""


def item(title, url, source, metric=None, metric_name=None, published=NOW):
    return {"title": title, "url": url, "source": source, "summary": "", "published": published,
            "metric": metric, "metric_name": metric_name}


def check_parsers():
    feed = sources.parse_feed("OpenAI", RSS)
    assert len(feed) == 1 and feed[0]["url"] == "https://openai.com/a", feed  # entry without a link is skipped
    assert feed[0]["published"] == datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc)
    assert feed[0]["summary"] == "Sandbox for agents"  # HTML stripped
    hn = sources.parse_hn([{"title": "Claude Code tips", "objectID": "9", "url": "", "created_at_i": 1795000000, "points": 240},
                           {"title": "Gardening tips", "objectID": "10", "url": "", "created_at_i": 1795000000, "points": 300}])
    assert [i["title"] for i in hn] == ["Claude Code tips"], "off-topic HN stories are dropped"
    assert hn[0]["url"] == "https://news.ycombinator.com/item?id=9" and hn[0]["metric"] == 240
    gh = sources.parse_github([{"full_name": "a/mcp-tools", "html_url": "https://github.com/a/mcp-tools", "description": "MCP server",
                                "created_at": "2026-10-24T10:00:00Z", "stargazers_count": 1500}])
    assert gh[0]["metric_name"] == "stars" and gh[0]["published"].tzinfo is not None
    dv = sources.parse_devto([{"title": "Beginner AI agent", "url": "https://dev.to/x", "description": "d",
                               "published_at": "2026-10-24T10:00:00Z", "positive_reactions_count": 120}])
    assert dv[0]["metric"] == 120
    models = sources.parse_models([{"id": "org/model", "pipeline_tag": "text-generation", "trendingScore": 50}], NOW)
    assert models[0]["published"] == NOW  # snapshot dated to the run


def check_feed_filter_and_chunking():
    changelog = [item("EMU deletions fix", "https://c1", "GitHub changelog"),
                 item("Copilot coding agent now in IDE", "https://c2", "GitHub changelog")]
    kept = sources.feed_filter("GitHub changelog", changelog)
    assert [i["url"] for i in kept] == ["https://c2"], "changelog entries need an AI/dev topic"
    assert len(sources.feed_filter("OpenAI", changelog)) == 2, "the OpenAI feed is AI news as a whole"
    # rank scores in chunks, so a 25-candidate pool is asked about in 3 calls, each covering its own indexes
    calls = []
    real = rank.llm.ask_json
    try:
        def fake(model, prompt, schema, api_key=None, **opts):
            n = sum(1 for line in prompt.splitlines() if line.startswith("["))  # one listing line per candidate
            calls.append(n)
            return {"ideas": [{"i": k, "audience_appeal": 60, "creator_relevance": 60, "video_fit": 60,
                               "production_effort": 2, "format": rank.FORMATS[0], "hook": "h", "reason": "r"}
                              for k in range(n)]}
        rank.llm.ask_json = fake
        pool = [(cluster.cluster([item(f"Story number {n} about agents", f"https://p{n}", "dev.to")])[0],
                 {"timeliness": 40, "credibility": 40}) for n in range(25)]
        scores = rank._llm_scores(pool, "k")
    finally:
        rank.llm.ask_json = real
    assert calls == [10, 10, 5], calls
    assert sorted(scores) == list(range(25)), "every candidate in the pool is scored once"


def check_fetch_all_survives_failures():
    def boom():
        raise RuntimeError("rate limited")

    class HTTPErr(Exception):
        response = type("R", (), {"status_code": 429})()

    def limited():
        raise HTTPErr()

    old = NOW - timedelta(days=30)
    jobs = [("good", lambda: [item("Fresh claude story", "https://a", "HackerNews")]),
            ("bad", boom), ("limited", limited),
            ("old", lambda: [item("Old story", "https://old", "OpenAI", published=old)])]
    items, report = sources.fetch_all(NOW, jobs)
    assert report == {"good": "ok:1", "bad": "error:RuntimeError", "limited": "error:HTTP 429", "old": "ok:1"}, report
    assert [i["url"] for i in items] == ["https://a"], "stories outside the 7-day window are dropped"


def check_cluster_evidence():
    out = cluster.cluster([item("Anthropic ships Claude Code desktop app", "https://a", "OpenAI"),
                           item("Claude Code desktop app ships", "https://b", "HackerNews", 300, "points"),
                           item("Gardening tips for spring", "https://g", "dev.to", 5, "reactions")])
    assert len(out) == 2
    assert out[0]["evidence_level"] == "multi_source" and out[0]["sources"] == ["HackerNews", "OpenAI"]
    assert out[1]["evidence_level"] == "single_source"
    # a lone story is never called engagement unless the number clears the threshold
    assert cluster.cluster([item("x y z lone", "https://q", "dev.to", 99, "reactions")])[0]["evidence_level"] == "single_source"


def check_rank_rules():
    official = cluster.cluster([item("Official launch thing", "https://o", "OpenAI")])[0]
    lone = cluster.cluster([item("Random community post", "https://l", "dev.to", 5, "reactions")])[0]
    perfect = {"audience_appeal": 100, "creator_relevance": 100, "video_fit": 100, "production_effort": 1,
               "format": rank.FORMATS[0], "hook": "h", "reason": "r"}
    capped = rank._assemble(lone, rank.heuristic(lone, NOW), perfect)
    assert capped["score"] == rank.SINGLE_SOURCE_CAP, capped["score"]
    assert rank._assemble(official, rank.heuristic(official, NOW), perfect)["score"] > rank.SINGLE_SOURCE_CAP
    out, note = rank.rank([lone, official], NOW, None)
    assert out[0]["id"] == official["id"] and out[0]["scored_by"] == "heuristic" and "No GROQ_API_KEY" in note
    assert rank._num(250) == 100 and rank._num(True) == 50 and rank._effort(9) == 50 and rank._effort(1) == 100
    # model output with a bad or repeated index is ignored, not a crash
    saved = rank._llm_scores
    try:
        rank._llm_scores = lambda pool, key: {0: perfect}
        out, note = rank.rank([lone, official], NOW, "k")
        assert note == "LLM scored 1 of 2; the rest use neutral scores.", note  # partial scoring is reported
        assert sum(r["scored_by"] == "llm" for r in out) == 1
    finally:
        rank._llm_scores = saved


def check_link_scheme():
    odd = [item("Click me now", "javascript:alert(1)", "OpenAI"), item("Local file story", "file:///etc", "dev.to")]
    assert cluster.cluster(odd) == [], "only http(s) links become candidates"


def check_scripts_and_run():
    idea = {"title": "t", "format": "", "hook": "", "evidence": [{"url": f"https://x/{n}", "source": "s", "title": "t"} for n in range(3)]}
    assert scripts.build_script(idea, None) is None
    brief = scripts.finish({"beats": [{"say": "s", "show": "w"}] * 9, "hook": "h", "on_screen_text": [],
                            "claims_to_verify": [], "cta": "c", "sources": ["invented"]}, idea)
    assert len(brief["beats"]) == scripts.MAX_BEATS and brief["sources"][0] == "https://x/0"

    # end to end with stubbed sources and store: writes JSON to a temp folder only
    saved_fetch, saved_probe, saved_rows, saved_mark = sources.fetch_all, store.probe, store.seen_rows, store.mark_seen
    saved_out, saved_ledger = run.OUT_DIR, seen.LEDGER
    saved_key = os.environ.pop("GROQ_API_KEY", None)  # .env is loaded on import: keep the model out of this test
    marked = []
    try:
        sources.fetch_all = lambda now: ([item("Codex launches sandbox", "https://c", "OpenAI"),
                                          item("Codex sandbox launch", "https://d", "HackerNews", 250, "points"),
                                          item("Seen before story", "https://s", "OpenAI")],
                                         {"feed:OpenAI": "ok:3", "hn:x": "error:HTTP 429"})
        store.probe = lambda table="seen": ("ok", "")
        saved_update, trends.update = trends.update, lambda candidates, now: ({}, {"message": "Trends: stubbed.", "counts": {}})
        store.seen_rows = lambda kind, days=None: [("https://s", "Seen before story")]
        store.mark_seen = lambda kind, pairs: marked.extend(pairs) or True
        with tempfile.TemporaryDirectory() as tmp:
            run.OUT_DIR, seen.LEDGER = tmp, os.path.join(tmp, "seen_ideas.json")  # never the real ledger
            result = run.run(top=5, briefs=1, now=NOW)
            assert os.path.exists(result["path"]), result
            with open(result["path"], encoding="utf-8") as f:
                saved = json.load(f)
        assert result["sources"]["hn:x"] == "error:HTTP 429"
        assert result["after_seen_filter"] == 1 and len(result["ideas"]) == 1, result  # seen story dropped, duplicates merged
        assert result["ideas"][0]["evidence_level"] == "multi_source"
        assert result["briefs"] == {result["ideas"][0]["id"]: None}  # no key: brief skipped, not failed
        assert saved["ideas"][0]["url"] == "https://c" and marked == [("https://c", "Codex launches sandbox")]
        sf = result["seen_filter"]
        assert sf["backend"] == "supabase" and sf["remote_wrote"] is True and sf["local_wrote"] is None, sf
    finally:
        sources.fetch_all, store.probe, store.seen_rows, store.mark_seen = saved_fetch, saved_probe, saved_rows, saved_mark
        run.OUT_DIR, seen.LEDGER = saved_out, saved_ledger
        trends.update = saved_update
        if saved_key:
            os.environ["GROQ_API_KEY"] = saved_key


if __name__ == "__main__":
    check_parsers()
    check_feed_filter_and_chunking()
    check_fetch_all_survives_failures()
    check_cluster_evidence()
    check_link_scheme()
    check_rank_rules()
    check_scripts_and_run()
    print("discovery tests passed")
