"""Offline tests for web-search rate limits: no retry storm, a clear per-query status, spacing, and a cache that cuts volume.

The failure they reproduce (25 Oct 2026): Exa's free hosted search answered one query with a rate limit, Agent Reach then
locked the whole web channel for an hour ("Wait 1023 seconds before retrying this channel."), and every later query, plus
the pipeline's own 2s/5s retries, bounced off that lock: "RATE_LIMITED after 3 attempts" on all four queries, for hours.
Nothing here touches the network, the real web cache, or any real data folder.

    .venv\\Scripts\\python -m tests.test_web_limits
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.discovery import reach, run, sources  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
HINT = "Wait 3540 seconds before retrying this channel."
RESULT = {"title": "Meta launches Muse Code, an AI agent for large code bases", "url": "https://techcrunch.com/muse",
          "author": "Lucas Ropek", "published_at": "2026-10-24T21:21:28.000Z", "text": "Meta launches Muse Code"}


def ok(items):
    return json.dumps({"ok": True, "items": items, "error": None})


def limited(wait=3540):
    return json.dumps({"ok": False, "items": [], "error": {"code": "RATE_LIMITED", "message": "Platform reported a rate limit",
                                                            "hint": f"Wait {wait} seconds before retrying this channel."}})


class Harness:
    """A pretend agent-reach plus a fake clock. `script(channel, action, query, call_no)` returns stdout text."""

    def __init__(self, script):
        self.script, self.calls, self.slept, self.clock, self.live = script, [], [], {"t": 1_000_000.0, "m": 0.0}, 0
        self.max_live = 0

    def run(self, cmd, **kw):
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        threading.Event().wait(0.01)  # let a second thread try to overlap if the code lets it
        try:
            self.calls.append((cmd[1], cmd[2], cmd[3]))
            out = self.script(cmd[1], cmd[2], cmd[3], len(self.calls))
            if isinstance(out, Exception):
                raise out
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        finally:
            self.live -= 1

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.clock["m"] += seconds
        self.clock["t"] += seconds


def harness(script, fn):
    h = Harness(script)
    saved = (reach.CLI, reach.subprocess.run, reach.time.sleep, reach.time.monotonic, reach.time.time, reach.WEB_CACHE)
    with tempfile.TemporaryDirectory() as tmp:
        reach.CLI = "agent-reach"
        reach.subprocess.run, reach.time.sleep = h.run, h.sleep
        reach.time.monotonic, reach.time.time = (lambda: h.clock["m"]), (lambda: h.clock["t"])
        reach.WEB_CACHE = os.path.join(tmp, "web_cache.json")
        reach._down.clear()
        reach._limited.clear()
        reach._web_state["ended"] = None
        try:
            return fn(h, tmp)
        finally:
            (reach.CLI, reach.subprocess.run, reach.time.sleep, reach.time.monotonic, reach.time.time, reach.WEB_CACHE) = saved
            reach._limited.clear()
            reach._web_state["ended"] = None


def check_a_long_limit_is_not_retried_through():
    def go(h, tmp):
        try:
            reach.call("web", "search", "q1")
        except reach.ReachError as e:
            first = e
        assert first.reach_code == "RATE_LIMITED" and first.retry_after == 3540, (first.reach_code, first.retry_after)
        assert first.attempts == 1 and h.slept == [], "no 2s/5s retries against a one-hour lock"
        assert "cooling down for 3540s" in first.detail
        # the channel is now known to be cooling down: the next query is not sent at all
        calls_before = len(h.calls)
        try:
            reach.call("web", "search", "q2")
        except reach.ReachError as e:
            second = e
        assert len(h.calls) == calls_before, "no request was made while cooling down"
        assert second.attempts == 0 and "this query was not sent" in second.detail and second.retry_after == 3540
        # other channels are not held up by web's limit
        assert reach.call("twitter", "search", "q3")[0]["url"]
        # and the cooldown counts down with the (fake) clock, then asks again
        h.clock["m"] += 3541
        assert reach.call("web", "search", "q4")
    harness(lambda ch, ac, q, n: limited() if ch == "web" and n == 1 else ok([RESULT]) if ch == "web" else ok([{**RESULT, "url": "https://x.com/i/status/1"}]), go)


def check_the_other_queries_are_skipped_not_hammered_and_reported():
    def go(h, tmp):
        job_list = [j for j in reach.jobs(NOW) if j[0].startswith("web:")] + [("feed:OpenAI", lambda: [sources._item("Claude agents news", "https://o", "OpenAI")])]
        items, report = sources.fetch_all(NOW, job_list)
        web_calls = [c for c in h.calls if c[0] == "web"]
        assert len(web_calls) == 1, f"{len(web_calls)} web requests were made; one limit must stop the rest"
        web = {k: v for k, v in report.items() if k.startswith("web:")}
        sent = [v for v in web.values() if "rate limited; cooling down" in v]
        skipped = [v for v in web.values() if "this query was not sent" in v]
        assert len(sent) == 1 and len(skipped) == 3, web
        assert all(v.startswith("error:RATE_LIMITED") for v in web.values())
        assert report["feed:OpenAI"] == "ok:1" and len(items) == 1, "other sources are unaffected"
        # the banner says it once, grouped, with the remaining wait
        failed = {k: v for k, v in report.items() if v.startswith("error")}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run._print({"sources": report, "complete": False, "failed_sources": failed, "items": 1, "candidates": 1,
                        "after_seen_filter": 1, "already_seen": {"same_url": 0, "same_story": 0}, "ideas": [], "briefs": {},
                        "seen_filter": {"message": "x"}, "trends": {"message": "y", "counts": {}}, "note": "", "path": "p"})
        text = buf.getvalue()
        assert text.count("PARTIAL RESULTS") == 1 and "web x4" in text and "3540s" in text, text
    harness(lambda ch, ac, q, n: limited(), go)


def check_a_short_limit_is_waited_out_once():
    def go(h, tmp):
        got = reach.call("web", "search", "q")
        assert got.note == " (retried 1x)" and h.slept == [6], (got.note, h.slept)
    harness(lambda ch, ac, q, n: limited(5) if n == 1 else ok([RESULT]), go)

    def not_short(h, tmp):
        try:
            reach.call("web", "search", "q")
        except reach.ReachError as e:
            assert e.attempts == 1 and h.slept == [], "31s is past MAX_LIMIT_WAIT: not waited out"
    harness(lambda ch, ac, q, n: limited(reach.MAX_LIMIT_WAIT + 1), not_short)

    def still_limited(h, tmp):
        try:
            reach.call("web", "search", "q")
        except reach.ReachError as e:
            assert e.attempts == 2 and len(h.calls) == 2 and len(h.slept) == 1, "waits once, then stops: bounded"
    harness(lambda ch, ac, q, n: limited(5), still_limited)


def check_web_searches_are_one_at_a_time_and_spaced():
    def go(h, tmp):
        job_list = [j for j in reach.jobs(NOW) if j[0].startswith("web:")]
        items, report = sources.fetch_all(NOW, job_list)
        assert h.max_live == 1, "two web searches were in flight at once"
        assert all(v.startswith("ok:") for v in report.values()), report
        spaced = [s for s in h.slept if s > 0]
        assert len(spaced) == 3 and all(abs(s - reach.WEB_SPACING) < 1e-6 for s in spaced), h.slept  # between 4 searches, 3 gaps
    harness(lambda ch, ac, q, n: ok([RESULT]), go)


def check_the_cache_cuts_repeat_calls_and_says_so():
    def go(h, tmp):
        first = reach._fetch_web("ai agent launch")
        assert len(h.calls) == 1 and first.note == "" and len(first) == 1
        assert os.path.exists(reach.WEB_CACHE) and os.path.dirname(reach.WEB_CACHE) == tmp, "cache goes where the test says"
        h.clock["t"] += 3600
        again = reach._fetch_web("ai agent launch")
        assert len(h.calls) == 1, "an hour later the cached copy is used, no request"
        assert again.note == " (cached 60m ago)" and again[0]["url"] == first[0]["url"], again.note
        other = reach._fetch_web("a different query")
        assert len(h.calls) == 2, "another query is not served from this one's cache"
        h.clock["t"] += reach.WEB_CACHE_TTL + 1
        reach._fetch_web("ai agent launch")
        assert len(h.calls) == 3, "after the TTL it asks again"
        # a damaged cache is a miss, not an error; an unwritable one costs a repeat search, not a failed run
        with open(reach.WEB_CACHE, "w", encoding="utf-8") as f:
            f.write("{not json")
        reach._fetch_web("ai agent launch")
        assert len(h.calls) == 4
        reach.WEB_CACHE = os.path.join(reach.WEB_CACHE, "nested", "c.json")  # parent is a file
        assert reach._fetch_web("brand new query")
    harness(lambda ch, ac, q, n: ok([RESULT]), go)


def check_a_stale_copy_is_used_only_while_limited_and_is_labelled():
    def go(h, tmp):
        reach._fetch_web("ai agent launch")  # fills the cache (call 1 succeeds)
        h.clock["t"] += reach.WEB_CACHE_TTL + 3600  # past the TTL, inside the stale window
        h.clock["m"] += 10
        stale = reach._fetch_web("ai agent launch")  # call 2 is rate limited
        assert len(stale) == 1 and "STALE copy" in stale.note and "rate limited" in stale.note, stale.note
        assert "7h old" in stale.note or "6h old" in stale.note, stale.note
        # nothing cached for this one: the error propagates and the query is reported as not run
        try:
            reach._fetch_web("never asked before")
        except reach.ReachError as e:
            assert e.reach_code == "RATE_LIMITED" and "this query was not sent" in e.detail
        # a copy older than WEB_STALE_MAX is not passed off as current
        h.clock["t"] += reach.WEB_STALE_MAX
        try:
            reach._fetch_web("ai agent launch")
            raise AssertionError("an expired copy was used")
        except reach.ReachError as e:
            assert e.reach_code == "RATE_LIMITED"
    harness(lambda ch, ac, q, n: ok([RESULT]) if n == 1 else limited(), go)


def check_each_run_starts_clean():
    def go(h, tmp):
        try:
            reach.call("web", "search", "q")
        except reach.ReachError:
            pass
        assert reach._limited, "the limit was remembered within the run"
        reach.jobs(NOW)
        assert not reach._limited and reach._web_state["ended"] is None, "a new run asks again (the real lock is agent-reach's own)"
    harness(lambda ch, ac, q, n: limited(), go)


def check_nothing_real_was_touched():
    # the module-level defaults point at the real data folder; every test above replaced them
    assert reach.WEB_CACHE.replace("\\", "/").endswith("data/discovery/web_cache.json")


if __name__ == "__main__":
    check_a_long_limit_is_not_retried_through()
    check_the_other_queries_are_skipped_not_hammered_and_reported()
    check_a_short_limit_is_waited_out_once()
    check_web_searches_are_one_at_a_time_and_spaced()
    check_the_cache_cuts_repeat_calls_and_says_so()
    check_a_stale_copy_is_used_only_while_limited_and_is_labelled()
    check_each_run_starts_clean()
    check_nothing_real_was_touched()
    print("web limit tests passed")
