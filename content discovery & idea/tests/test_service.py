"""Offline tests for the layers under the chat front end: background jobs, saved ideas, and the service. No Telegram, no network,
no model, and nothing written outside a temp folder.

    .venv\\Scripts\\python -m tests.test_service
"""

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import store  # noqa: E402
from agent.discovery import jobs, saved, service  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def wait_for(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def idea(n, **extra):
    base = {"id": f"{n:04x}" + "ab" * 4, "title": f"Idea number {n}", "summary": "a summary", "url": f"https://example.com/{n}",
            "score": 70.0 - n, "format": "tool walkthrough", "hook": "", "reason": "r", "evidence_level": "single_source",
            "claim_status": "unverified", "needs_verification": False, "independent_sources": 1, "penalties": [], "warnings": [],
            "scored_by": "llm", "published": NOW, "trend": {"class": "insufficient_data", "explanation": "one snapshot", "topic_key": "k"},
            "evidence": [{"source": "HackerNews", "platform": "HackerNews", "author": "u", "url": f"https://news.ycombinator.com/{n}",
                          "title": "t", "published": NOW.isoformat(), "metrics": {"points": 100}, "extra_field": "dropped"}]}
    return {**base, **extra}


class Env:
    """Isolated saved-ideas storage: a dict standing in for Supabase's kv table, and a temp file for the fallback."""

    def __init__(self, supabase=True):
        self.supabase, self.kv = supabase, {}

    def __enter__(self):
        self.saved = (store.save, store.load, saved.LOCAL, jobs.MAX_RUNTIME)
        self.tmp = tempfile.TemporaryDirectory()
        store.save = lambda k, v: (self.kv.__setitem__(k, json.loads(json.dumps(v))) or True) if self.supabase else False
        store.load = lambda k: self.kv.get(k) if self.supabase else None
        saved.LOCAL = os.path.join(self.tmp.name, "saved_ideas.json")
        jobs.reset()
        return self

    def __exit__(self, *a):
        store.save, store.load, saved.LOCAL, jobs.MAX_RUNTIME = self.saved
        self.tmp.cleanup()
        jobs.reset()


# ---- jobs --------------------------------------------------------------------------------------------------------

def check_jobs():
    with Env():
        gate, done = threading.Event(), threading.Event()
        calls = []
        job, fresh = jobs.start("discover", "discover", "run", lambda: gate.wait(5) and calls.append(1) or "result", on_done=lambda j: done.set())
        again, fresh2 = jobs.start("discover", "discover", "run", lambda: calls.append("second"))
        assert fresh and not fresh2 and again is job and job.state == "running", "one job per key"
        assert jobs.running("discover") == [job] and jobs.running("brief") == []
        gate.set()
        assert done.wait(5) and job.state == "done" and job.result == "result" and calls == [1]
        assert jobs.latest("discover", "done") is job and jobs.latest("brief") is None
        # a different key runs alongside
        other, fresh3 = jobs.start("brief", "brief:x", "b", lambda: 1)
        assert fresh3

        # a failure keeps the exception's type only, never its message (which can carry a URL or a key)
        def boom():
            raise RuntimeError("https://api.telegram.org/bot123:SECRET/sendMessage failed")
        bad, _ = jobs.start("discover", "bad", "bad", boom)
        assert wait_for(lambda: bad.state == "failed") and bad.error == "RuntimeError" and "SECRET" not in repr(vars(bad)), vars(bad)

        # a notifier that raises does not break the job
        job2, _ = jobs.start("discover", "k2", "k2", lambda: 7, on_done=lambda j: 1 / 0)
        assert wait_for(lambda: job2.state == "done") and job2.result == 7


def check_a_stuck_job_is_abandoned_and_its_late_result_dropped():
    with Env():
        jobs.MAX_RUNTIME = 0.05
        gate, notified = threading.Event(), []
        stuck, _ = jobs.start("discover", "discover", "stuck", lambda: gate.wait(5) and "late", on_done=lambda j: notified.append(j))
        time.sleep(0.1)
        fresh, started = jobs.start("discover", "discover", "again", lambda: "new")
        assert started and fresh is not stuck and stuck.state == "failed" and stuck.error == "TimedOut", "the key is free again"
        gate.set()
        time.sleep(0.2)
        assert stuck.state == "failed" and stuck.result is None and notified == [], "the late result changes nothing and notifies no one"


# ---- saved ideas -------------------------------------------------------------------------------------------------

def check_saved_records_and_ordering():
    with Env() as env:
        assert saved.save_run([idea(1), idea(2), idea(3)], "run1", NOW) == "supabase"
        got = saved.list_ideas()
        assert [r["id"] for r in got] == [idea(n)["id"] for n in (1, 2, 3)] and got[0]["rank"] == 1
        json.dumps(got)  # JSON-safe: the run's datetimes became strings
        assert got[0]["published"] == NOW.isoformat() and "extra_field" not in got[0]["evidence"][0]
        assert got[0]["trend"]["class"] == "insufficient_data" and got[0]["brief"] is None
        # a later run goes in front; an idea that returns keeps its brief
        saved.attach_brief(idea(2)["id"], {"hook": "my brief"})
        saved.save_run([idea(4), idea(2, score=99)], "run2", NOW + timedelta(days=1))
        ids = [r["id"] for r in saved.list_ideas()]
        assert ids == [idea(n)["id"] for n in (4, 2, 1, 3)], ids
        assert next(r for r in saved.list_ideas() if r["id"] == idea(2)["id"])["brief"] == {"hook": "my brief"}
        assert [r["id"] for r in saved.run_ideas("run2")] == [idea(4)["id"], idea(2)["id"]] and saved.latest_run_id() == "run2"
        # bounded
        saved.save_run([idea(n) for n in range(10, 10 + saved.MAX_SAVED + 5)], "run3", NOW + timedelta(days=2))
        assert len(saved.list_ideas()) == saved.MAX_SAVED
        assert saved.attach_brief("ffffffffffff", {}) is None, "an idea that is no longer saved"


def check_saved_find():
    with Env():
        saved.save_run([idea(1), idea(2), {**idea(3), "id": idea(1)["id"][:6] + "99999999"[:6]}], "run1", NOW)
        full = idea(2)["id"]
        assert saved.find(full) == ("ok", saved.list_ideas()[1])
        assert saved.find("#" + full[:5].upper())[0] == "ok", "case and a leading # are ignored"
        assert saved.find("abc")[0] == "none", "under 4 characters is not an id"
        assert saved.find("zzzzzz")[0] == "none" and saved.find("")[0] == "none" and saved.find(None)[0] == "none"
        status, hits = saved.find(idea(1)["id"][:4])
        assert status == "ambiguous" and len(hits) == 2


def check_saved_fallback_is_explicit():
    with Env(supabase=False):
        assert saved.where() == "none" and saved.list_ideas() == []
        assert saved.save_run([idea(1)], "run1", NOW) == "local", "Supabase refused: the local file is used, and it says so"
        assert os.path.exists(saved.LOCAL) and saved.where() == "local" and len(saved.list_ideas()) == 1
        # corrupt local file: no crash, nothing invented
        with open(saved.LOCAL, "w", encoding="utf-8") as f:
            f.write("{broken")
        assert saved.list_ideas() == [] and saved.where() == "none"
        with open(saved.LOCAL, "w", encoding="utf-8") as f:
            json.dump({"ideas": [{"no_id": 1}, "junk", {"id": "abcd1234abcd", "title": "ok"}]}, f)
        assert [r["id"] for r in saved.list_ideas()] == ["abcd1234abcd"], "malformed rows are skipped"
    with Env(supabase=False) as env:
        saved.LOCAL = os.path.join(env.tmp.name, "nested", "x", "f.json")
        os.makedirs(os.path.join(env.tmp.name, "nested"), exist_ok=True)
        open(os.path.join(env.tmp.name, "nested", "x"), "w").close()  # a file where a folder should be
        assert saved.save_run([idea(1)], "r", NOW) == "none", "unsavable is reported, not hidden"
    # Supabase comes back: the next save moves the local list up
    with Env(supabase=False) as env:
        saved.save_run([idea(1)], "run1", NOW)
        env.supabase = True
        assert saved.where() == "local"
        assert saved.save_run([idea(2)], "run2", NOW + timedelta(days=1)) == "supabase"
        assert {r["id"] for r in env.kv["discovery_ideas"]["ideas"]} == {idea(1)["id"], idea(2)["id"]}


# ---- service -----------------------------------------------------------------------------------------------------

def check_confidence_and_pages():
    c = service.confidence
    assert c({"needs_verification": True, "claim_status": "first_party"}) == ("low", "unverified claim about an announcement")
    assert c({"claim_status": "first_party"})[0] == "high"
    assert c({"claim_status": "corroborated", "independent_sources": 3}) == ("high", "3 independent sources")
    assert c({"claim_status": "unverified", "independent_sources": 2})[0] == "high"
    assert c({"claim_status": "unverified", "independent_sources": 1, "evidence_level": "engagement"})[0] == "medium"
    assert c({"claim_status": "unverified", "independent_sources": 1, "evidence_level": "single_source"})[0] == "low"
    assert c({})[0] == "low", "missing fields never raise confidence"
    items = list(range(8))
    assert service.paginate(items, 1) == ([0, 1, 2], 1, 3, 8) and service.paginate(items, 3) == ([6, 7], 3, 3, 8)
    assert service.paginate(items, 99)[1] == 3 and service.paginate(items, -4)[1] == 1 and service.paginate([], 3) == ([], 1, 1, 0)
    assert service.paginate(items, "2")[1] == 2


def check_discover_saves_what_it_shows_and_survives_a_restart():
    ran = []

    def fake_run(top, briefs, now):
        ran.append((top, briefs))
        return {"ideas": [idea(1), idea(2)], "generated_at": "2026-10-25T12:00:00+00:00", "complete": False,
                "failed_sources": {"web:q": "error:RATE_LIMITED"}, "items": 100, "candidates": 80, "after_seen_filter": 60,
                "already_seen": {"same_url": 20, "same_story": 0}, "seen_filter": {"message": "Seen-filter: SUPABASE"},
                "trends": {"message": "Trends: SUPABASE", "counts": {"new": 0}}, "note": ""}
    with Env():
        real = service.run.run
        service.run.run = fake_run
        try:
            done = threading.Event()
            job, started = service.start_discover(99, on_done=lambda j: done.set())
            assert started and done.wait(5) and job.state == "done"
            assert ran == [(service.MAX_TOP, 0)], "top is clamped and no briefs are written for every idea"
            r = job.result
            assert r["saved_where"] == "supabase" and r["complete"] is False and r["failed_sources"] == {"web:q": "error:RATE_LIMITED"}
            summary, ideas = service.last_run()
            assert summary is r and [i["id"] for i in ideas] == [idea(1)["id"], idea(2)["id"]]
            jobs.reset()  # a restart: the job is forgotten, the ideas are not
            summary, ideas = service.last_run()
            assert summary is None and len(ideas) == 2
            service.start_discover(0)
            time.sleep(0.2)
            assert ran[-1][0] == 1, "top below 1 becomes 1"
        finally:
            service.run.run = real


def check_briefs_are_fetched_or_written_once():
    calls = []

    def fake_build(idea_, api_key):
        calls.append((idea_["id"], api_key))
        return {"hook": "h", "beats": [], "audit": {"status": "verified", "removed": []}}
    with Env():
        real, key = service.scripts.build_script, os.environ.get("GROQ_API_KEY")
        service.scripts.build_script = fake_build
        os.environ["GROQ_API_KEY"] = "test-key"
        try:
            saved.save_run([idea(1)], "run1", NOW)
            record = saved.list_ideas()[0]
            done = threading.Event()
            job, brief, started = service.start_brief(record, on_done=lambda j: done.set())
            assert started and brief is None and done.wait(5) and job.state == "done"
            assert calls == [(record["id"], "test-key")]
            cached = saved.list_ideas()[0]
            assert cached["brief"]["hook"] == "h" and cached["brief_at"]
            j2, b2, s2 = service.start_brief(cached)
            assert j2 is None and b2["hook"] == "h" and not s2 and len(calls) == 1, "the saved brief is returned, the model is not asked"
            done.clear()
            j3, b3, s3 = service.start_brief(cached, force=True, on_done=lambda j: done.set())
            assert s3 and done.wait(5) and len(calls) == 2, "force rewrites it"

            # the idea vanished before the job ran, or the key went: failure names the type only
            saved.save_run([], "run2", NOW)
            ghost = {**record, "brief": None}
            saved.LOCAL, store.load = saved.LOCAL, (lambda k: {"ideas": []})
            j4, _, _ = service.start_brief(ghost)
            assert wait_for(lambda: j4.state == "failed") and j4.error == "LookupError"
            os.environ.pop("GROQ_API_KEY", None)
            assert service.can_write_briefs() is False
        finally:
            service.scripts.build_script = real
            os.environ.pop("GROQ_API_KEY", None) if key is None else os.environ.__setitem__("GROQ_API_KEY", key)


if __name__ == "__main__":
    check_jobs()
    check_a_stuck_job_is_abandoned_and_its_late_result_dropped()
    check_saved_records_and_ordering()
    check_saved_find()
    check_saved_fallback_is_explicit()
    check_confidence_and_pages()
    check_discover_saves_what_it_shows_and_survives_a_restart()
    check_briefs_are_fetched_or_written_once()
    print("service tests passed")
