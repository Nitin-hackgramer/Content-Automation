"""Offline tests for the Agent Reach adapter and cross-source dedup. agent-reach and yt-dlp are mocked.

    .venv\\Scripts\\python -m tests.test_reach
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.discovery import cluster, reach, scripts, sources  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

TWEET = {"url": "https://x.com/i/status/2112984664256241837", "author": "claudeai",
         "published_at": "Wed Oct 21 19:09:35 +0000 2026",
         "text": "We're opening a research preview of Claude Duet, a proactive agent. https://t.co/kt8gAbWuX8",
         "raw": {"likes": 54964, "views": "10558624", "card": None}}
VIDEO = {"title": "Complete Claude Code Course", "url": "https://www.youtube.com/watch?v=TAKDIvvUdc4",
         "author": "Krish Naik", "published_at": None, "text": "Claude Code is an agentic coding tool",
         "raw": {"view_count": 301971, "like_count": None, "duration": 7115}}
WEB = {"title": "Meta launches Muse Code, an AI agent for large code bases", "author": "N/A",
       "url": "https://techcrunch.com/2026/08/05/muse-code/?utm_source=x", "published_at": "2026-10-24T21:21:28.000Z",
       "text": "Meta launches Muse Code"}


def fake_run(stdout="", exc=None, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return run


def ok(items):
    return json.dumps({"ok": True, "items": items, "error": None})


SLEPT = []  # backoff sleeps asked for during a test; nothing really sleeps


def with_cli(fn):
    """Run fn with a pretend agent-reach on PATH, a clean failure memory, and no real sleeping."""
    saved = reach.CLI, reach.subprocess.run, reach.time.sleep, reach.time.monotonic, reach.WEB_CACHE
    reach.CLI, reach._down = "agent-reach", set()
    reach._limited.clear()
    reach._web_state["ended"] = None
    reach.WEB_CACHE = os.path.join(tempfile.mkdtemp(), "web_cache.json")  # never the real cache
    SLEPT.clear()
    reach.time.sleep = SLEPT.append
    try:
        return fn()
    finally:
        reach.CLI, reach.subprocess.run, reach.time.sleep, reach.time.monotonic, reach.WEB_CACHE = saved


def check_normalize_tweet():
    t = reach.parse_tweets([TWEET])[0]
    assert t["platform"] == "X" and t["source"] == "X search" and t["author"] == "claudeai"
    assert t["published"] == datetime(2026, 10, 21, 19, 9, 35, tzinfo=timezone.utc)
    assert t["metric"] == 54964 and t["metric_name"] == "likes"
    assert t["metrics"] == {"likes": 54964, "views": 10558624}, "string views become int; absent retweets left out"
    assert "t.co" not in t["title"], "link noise stripped from the headline"


def check_missing_fields_stay_missing():
    bare = {"url": "https://x.com/a/status/1", "text": "new coding agent", "raw": {"likes": "lots"}}
    t = reach.parse_tweets([bare])[0]
    assert t["published"] is None and t["author"] is None
    assert t["metric"] is None and t["metric_name"] is None and t["metrics"] == {}, "unparseable count is not 0"
    v = reach.parse_videos([VIDEO], "YouTube search")[0]
    assert v["published"] is None and v["metrics"] == {"views": 301971, "duration_s": 7115}, v["metrics"]
    w = reach.parse_web([WEB])[0]
    assert w["author"] is None, "'N/A' author is missing, not a name"
    assert w["published"] == datetime(2026, 10, 24, 21, 21, 28, tzinfo=timezone.utc)
    # junk rows are skipped, not crashed on
    assert reach.parse_tweets([None, {"text": "no url"}, {"url": 5}]) == []
    assert reach.parse_videos([{"url": "https://y"}], "YouTube search") == [], "no title, no item"
    assert reach.parse_tweets([{**TWEET, "text": "lunch photos", "raw": {}}]) == [], "off-topic tweet dropped"
    # an old-style item without the new keys still comes out in the full schema
    n = sources.normalize({"title": "t", "url": "https://u", "source": "HackerNews", "summary": "",
                           "published": None, "metric": 300, "metric_name": "points"}, NOW)
    assert n["platform"] == "HackerNews" and n["author"] is None and n["metrics"] == {"points": 300}
    assert n["collected_at"] == NOW


def check_youtube_dates_from_read():
    read = {"title": VIDEO["title"], "url": VIDEO["url"], "author": "Krish Naik", "published_at": "2026-10-23",
            "raw": {"view_count": 302000, "like_count": 5100, "duration": 7115}}

    def go():
        reach.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=ok([read] if cmd[2] == "read" else [VIDEO]), stderr="")
        return reach._fetch_youtube("claude code")
    v = with_cli(go)[0]
    assert v["published"] == datetime(2026, 10, 23, tzinfo=timezone.utc), "search hit dated by its read"
    assert v["metrics"]["likes"] == 5100 and v["metric"] == 302000

    def go_failed_read():
        reach.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=ok([VIDEO]) if cmd[2] == "search" else json.dumps({"ok": False, "error": {"code": "TIMEOUT"}}),
            stderr="")
        got = reach._fetch_youtube("claude code")
        assert "youtube" not in reach._down, "a failed per-video read does not take YouTube down"
        return got
    v = with_cli(go_failed_read)[0]
    assert v["published"] is None, "no date was invented when the read failed"


def check_undated_web_dropped():
    def go():
        reach.subprocess.run = fake_run(ok([WEB, {**WEB, "url": "https://old.example/a", "published_at": None}]))
        return reach._fetch_web("ai agent")
    assert [w["url"] for w in with_cli(go)] == [WEB["url"]], "an undated web page cannot prove it is recent"


def check_failures():
    cases = [(fake_run(exc=subprocess.TimeoutExpired("agent-reach", 75)), "TIMEOUT"),
             (fake_run("<html>blocked</html>"), "MALFORMED"),
             (fake_run(""), "MALFORMED"),
             (fake_run("[1, 2]"), "MALFORMED"),
             (fake_run(json.dumps({"ok": True, "items": "nope"})), "MALFORMED"),
             (fake_run(json.dumps({"ok": False, "error": {"code": "UPSTREAM_BROKEN"}})), "UPSTREAM_BROKEN")]
    for runner, code in cases:
        def go():
            reach.subprocess.run = runner
            try:
                reach.call("twitter", "search", "x")
            except reach.ReachError as e:
                assert e.reach_code == code and e.attempts == reach.MAX_ATTEMPTS, (e.reach_code, e.attempts)
                assert sources._reason(e) == f"{code} after 3 attempts", sources._reason(e)
                assert len(SLEPT) == 2, "backoff between attempts, none after the last"
                return
            raise AssertionError(f"expected {code}")
        with_cli(go)

    def not_installed():
        reach.CLI = None
        try:
            reach.call("web", "search", "x")
        except reach.ReachError as e:
            return e.reach_code
    assert with_cli(not_installed) == "NOT_INSTALLED"

def check_x_reliability():
    """Retries, backoff, budget, per-query status, and failures that stay local to their query."""
    tweet_ok = ok([TWEET])

    def sequence(*outcomes):
        """A subprocess stand-in that plays `outcomes` in order (an exception instance or stdout text)."""
        calls, it_ = [], iter(outcomes)

        def run(cmd, **kw):
            calls.append((cmd, kw))
            out = next(it_)
            if isinstance(out, Exception):
                raise out
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return run, calls

    timeout = subprocess.TimeoutExpired("agent-reach", 50)

    # 1. a transient failure is retried once and the query says so
    def retried():
        reach.subprocess.run, calls = sequence(timeout, tweet_ok)
        return reach._fetch_x("claude code", NOW), calls
    got, calls = with_cli(retried)
    assert len(got) == 1 and got.note == " (retried 1x)" and len(calls) == 2 and len(SLEPT) == 1
    assert 2 <= SLEPT[0] < 3, "first backoff is 2s plus jitter"

    # 2. each attempt has a strict timeout, and exhausting the attempts is reported with the count
    def exhausted():
        reach.subprocess.run, calls = sequence(timeout, timeout, timeout)
        try:
            reach._fetch_x("claude code", NOW)
        except reach.ReachError as e:
            return e, calls
    err, calls = with_cli(exhausted)
    assert err.reach_code == "TIMEOUT" and err.attempts == 3 and sources._reason(err) == "TIMEOUT after 3 attempts"
    assert len(calls) == 3 and 5 <= SLEPT[1] < 6, SLEPT
    for cmd, kw in calls:
        assert int(cmd[cmd.index("--timeout") + 1]) <= reach.ATTEMPT_TIMEOUT and kw["timeout"] <= reach.ATTEMPT_TIMEOUT + 5

    # 3. the whole query is bounded: a slow first attempt leaves too little budget for another
    def budget():
        clock = iter([0, 0, 90, 90])  # start, left (attempt 1), then 90s have passed when the retry is considered
        reach.time.monotonic = lambda: next(clock)
        reach.subprocess.run, calls = sequence(timeout, tweet_ok)
        try:
            reach.call("twitter", "search", "x")
        except reach.ReachError as e:
            return e.attempts, len(calls)
    assert with_cli(budget) == (1, 1), "no retry when under MIN_RETRY_BUDGET is left"

    # 4. setup problems are not retried and do skip the rest of that channel; nothing else is touched
    def setup():
        reach.subprocess.run, calls = sequence(json.dumps({"ok": False, "error": {"code": "NEED_SETUP"}}))
        errs = []
        for _ in range(2):
            try:
                reach.call("twitter", "search", "x")
            except reach.ReachError as e:
                errs.append((e.reach_code, e.attempts))
        reach.subprocess.run, other = sequence(ok([WEB]))
        reach.call("web", "search", "x")
        return errs, len(calls), len(other), list(SLEPT)
    errs, first_calls, other_calls, slept = with_cli(setup)
    assert errs == [("NEED_SETUP", 1), ("SKIPPED_SETUP_PROBLEM", 1)] and first_calls == 1 and other_calls == 1 and slept == []

    # 5. a transient failure in one query does not suppress the others, and the report says which failed and how
    def isolated():
        def run(cmd, **kw):
            if "coding agent" in cmd[3]:  # cmd = [cli, channel, action, query, ...]; this query never works
                raise timeout
            return subprocess.CompletedProcess(cmd, 0, stdout=tweet_ok, stderr="")
        reach.subprocess.run = run
        job_list = [(n, fn) for n, fn in reach.jobs(NOW) if n.startswith("x:")][:3]
        return sources.fetch_all(NOW, job_list)
    items, report = with_cli(isolated)
    assert report == {"x:claude code": "ok:1", "x:coding agent": "error:TIMEOUT after 3 attempts", "x:AI engineering": "ok:1"}, report
    assert len(items) == 2, "the two working queries still delivered their tweets"

    # 6. ok-after-retry shows in the per-query status
    def noted():
        reach.subprocess.run, _ = sequence(timeout, tweet_ok)
        job_list = [(n, fn) for n, fn in reach.jobs(NOW) if n.startswith("x:")][:1]
        return sources.fetch_all(NOW, job_list)[1]
    assert with_cli(noted) == {"x:claude code": "ok:1 (retried 1x)"}

    # 7. a dead backend costs a bounded number of attempts and still tries every query once
    def dead():
        reach.subprocess.run, calls = sequence(*[timeout] * 20)
        job_list = [(n, fn) for n, fn in reach.jobs(NOW) if n.startswith("x:")]
        assert reach.X_PARALLEL == 1, "X is serialised"
        items, report = sources.fetch_all(NOW, job_list)
        return report, len(calls)
    report, attempts = with_cli(dead)
    assert len(report) == 5 and all(v.startswith("error:TIMEOUT") for v in report.values()), report
    assert attempts == 3 + 3 + 1 + 1 + 1, f"{attempts} attempts: two queries retried fully, three tried once, none skipped"
    assert sorted(v for v in report.values()) == ["error:TIMEOUT"] * 3 + ["error:TIMEOUT after 3 attempts"] * 2, report


def check_pipeline_survives_reach_down():
    def boom():
        raise reach.ReachError("UPSTREAM_BROKEN")
    jobs = [("x:claude code", boom), ("feed:OpenAI", lambda: [sources._item("Claude agents news", "https://o", "OpenAI")])]
    items, report = sources.fetch_all(NOW, jobs)
    assert report == {"x:claude code": "error:UPSTREAM_BROKEN", "feed:OpenAI": "ok:1"}, report
    assert len(items) == 1 and items[0]["collected_at"] is not None


def check_canonical():
    c = cluster.canonical
    assert c("https://twitter.com/claudeai/status/42?s=20") == c("https://x.com/i/status/42") == "https://x.com/i/status/42"
    yt = "https://youtube.com/watch?v=abc"
    assert c("https://youtu.be/abc?si=zz") == c("https://www.youtube.com/watch?v=abc&t=30") == c("https://m.youtube.com/shorts/abc") == yt
    assert c("http://www.techcrunch.com/a/?utm_source=x&utm_medium=y#top") == "https://techcrunch.com/a"
    assert c("https://site.com/p?b=2&a=1&ref=hn") == "https://site.com/p?a=1&b=2", "real params kept, sorted"


def check_cross_source_dedup():
    def it(title, url, source, platform):
        return sources.normalize({"title": title, "url": url, "source": source, "platform": platform,
                                  "summary": "", "published": NOW, "metric": None, "metric_name": None}, NOW)
    # one page found twice (feed + web search) is ONE source, not corroboration
    same_page = cluster.cluster([it("Introducing Codex sandbox", "https://openai.com/index/codex", "OpenAI", "OpenAI"),
                                 it("Introducing Codex sandbox | OpenAI", "https://openai.com/index/codex/?utm_source=exa",
                                    "Web search", "Web")])
    assert len(same_page) == 1 and same_page[0]["evidence_level"] == "single_source", same_page
    assert same_page[0]["independent_sources"] == 1 and len(same_page[0]["evidence"]) == 2, "both sightings kept"
    # three tweets about one story are one platform
    tweets = cluster.cluster([it("Claude Duet research preview agent app", f"https://x.com/u{n}/status/{n}", "X search", "X")
                              for n in range(3)])
    assert len(tweets) == 1 and tweets[0]["evidence_level"] == "single_source"
    # a tweet and a YouTube video on different pages: independent
    both = cluster.cluster([it("Claude Duet research preview agent app", "https://x.com/i/status/1", "X search", "X"),
                            it("Claude Duet research preview agent app tested", "https://youtu.be/v1", "YouTube search", "YouTube")])
    assert len(both) == 1 and both[0]["evidence_level"] == "multi_source" and both[0]["independent_sources"] == 2
    ev = both[0]["evidence"][0]
    assert {"platform", "author", "metrics", "collected_at"} <= set(ev), ev
    # the same tweet under two URL spellings merges
    assert len(cluster.cluster([it("Totally different words one", "https://twitter.com/a/status/9", "X search", "X"),
                                it("Nothing alike here two", "https://x.com/i/status/9", "X search", "X")])) == 1


def check_validation_regressions():
    """Guards for three bugs found by the first full run (25 Oct 2026): promo spam, self-corroboration, invented brief details."""
    # 1. a promo-code tweet reached the top 2 and got a brief
    promo = {**TWEET, "text": "Claude Fable 5 for FREE Oxion AI Pro is free right now. how to claim it: apply code DEVWEEK",
             "raw": {"likes": 572}}
    assert reach.parse_tweets([promo]) == [], "promo tweet dropped"
    assert len(reach.parse_tweets([{**TWEET, "text": "Learn Claude Code for free: an open source course"}])) == 1, \
        "'free' alone is not spam"
    assert reach.parse_videos([{**VIDEO, "title": "Claude Code giveaway: win a keyboard"}], "YouTube search") == []
    assert reach.parse_web([{**WEB, "title": "AI agent airdrop: claim tokens"}]) == []

    # 2. OpenAI's feed + @OpenAI's tweet about it were counted as two independent sources
    def it(title, url, source, platform, author=None, likes=None):
        return sources.normalize({"title": title, "url": url, "source": source, "platform": platform, "author": author,
                                  "summary": "", "published": NOW, "metric": likes, "metric_name": "likes" if likes else None}, NOW)
    own = cluster.cluster([it("Securing our frontier model training environment", "https://openai.com/index/s", "OpenAI", "OpenAI"),
                           it("Securing our frontier model training environment", "https://x.com/i/status/1", "X search", "X",
                              "OpenAI", 13302)])
    assert len(own) == 1 and own[0]["independent_sources"] == 1, "a company tweeting its own post is one voice"
    assert own[0]["evidence_level"] == "engagement", own[0]["evidence_level"]  # the real traction still counts
    third = cluster.cluster([it("Securing our frontier model training environment", "https://openai.com/index/s", "OpenAI", "OpenAI"),
                             it("Securing our frontier model training environment explained", "https://x.com/i/status/2", "X search", "X",
                                "some_dev")])
    assert third[0]["evidence_level"] == "multi_source" and third[0]["independent_sources"] == 2, "a third party is independent"

    # 3. briefs invented details because the model never saw the summary, and claimed experience the creator lacks
    prompts = []
    saved = scripts.llm.ask_json
    try:
        scripts.llm.ask_json = lambda model, prompt, schema, key, **o: prompts.append(prompt) or {
            "hook": "h", "beats": [], "on_screen_text": [], "claims_to_verify": [], "cta": "c"}
        scripts.build_script({"title": "T", "summary": "OpenAI added offline environments and high-compute red-teaming for frontier model training and evaluation.", "format": "", "hook": "",
                              "evidence": [{"source": "OpenAI", "title": "T", "url": "https://o"}]}, "k")
    finally:
        scripts.llm.ask_json = saved
    assert "offline environments and high-compute red-teaming" in prompts[0], "the summary reaches the model"
    assert "has not yet tried, tested, built, read or watched" in prompts[0]


if __name__ == "__main__":
    check_normalize_tweet()
    check_missing_fields_stay_missing()
    check_youtube_dates_from_read()
    check_undated_web_dropped()
    check_failures()
    check_x_reliability()
    check_pipeline_survives_reach_down()
    check_canonical()
    check_cross_source_dedup()
    check_validation_regressions()
    print("reach tests passed")
