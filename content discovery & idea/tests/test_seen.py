"""Offline tests for the seen-filter: Supabase as the ledger, the local file as an explicit fallback.

An in-memory stand-in for PostgREST sits behind the same httpx calls agent/store.py makes, so the real store.py and
seen.py code run end to end. It proves the logic and the request shapes. It does NOT prove the real Supabase project
works: `python -m agent.discovery.seen --test` does that against the live project.

    .venv\\Scripts\\python -m tests.test_seen
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import store  # noqa: E402
from agent.discovery import cluster, run, seen, sources, trends  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
KEY = "sb-secret-DO-NOT-PRINT"


class FakePostgREST:
    """discovery_seen in memory. `fail` maps a method to (status, body) or an exception; `fail_select` fails a GET
    for one `select` value only, so the probe can pass while the real read fails."""

    def __init__(self, missing=False):
        self.rows, self.log, self.fail, self.fail_select, self.missing = [], [], {}, None, missing

    def _resp(self, status, body=None):
        return type("R", (), {"status_code": status, "text": json.dumps(body), "json": lambda s: body})()

    def _failure(self, method, params=None):
        got = self.fail.get(method)
        if self.fail_select and method == "GET" and (params or {}).get("select") == self.fail_select:
            got = (500, {})
        if isinstance(got, Exception):
            raise got
        return self._resp(*got) if got else None

    def _selected(self, params):
        out = []
        for r in self.rows:
            ok = True
            for col in ("kind", "url"):
                want = (params or {}).get(col)
                ok = ok and (want is None or r[col] == want.removeprefix("eq."))
            at = (params or {}).get("at")
            ok = ok and (at is None or r["at"] >= at.removeprefix("gte."))
            if ok:
                out.append(r)
        return sorted(out, key=lambda r: r["at"], reverse=True)

    def get(self, url, params=None, headers=None, timeout=None):
        self.log.append(("GET", url, params, headers))
        if (bad := self._failure("GET", params)):
            return bad
        if self.missing or not url.endswith("/rest/v1/discovery_seen"):
            return self._resp(404, {"code": "PGRST205", "message": "Could not find the table"})
        cols = (params or {}).get("select", "*").split(",")
        rows = self._selected(params)[:int((params or {}).get("limit", 1000))]
        return self._resp(200, [{c: r.get(c) for c in cols} for r in rows])

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.log.append(("POST", url, params, headers))
        if (bad := self._failure("POST")):
            return bad
        if self.missing:
            return self._resp(404, {"code": "PGRST205"})
        for row in json if isinstance(json, list) else [json]:
            dup = any(r["kind"] == row["kind"] and r["url"] == row["url"] for r in self.rows)
            if dup and "ignore-duplicates" in (headers or {}).get("Prefer", ""):
                continue
            self.rows.append({**row, "at": datetime.now(timezone.utc).isoformat()})
        return self._resp(201, None)

    def delete(self, url, params=None, headers=None, timeout=None):
        self.log.append(("DELETE", url, params, headers))
        if (bad := self._failure("DELETE")):
            return bad
        gone = self._selected(params)
        self.rows = [r for r in self.rows if r not in gone]
        return self._resp(204, None)


def with_supabase(fn, fake=None, configured=True):
    """Run fn(fake) with store.py talking to `fake`, a temp fallback file, and no real env or ledger touched."""
    fake = fake or FakePostgREST()
    saved = store.httpx, seen.LEDGER, {k: os.environ.get(k) for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY")}, trends.LOCAL
    with tempfile.TemporaryDirectory() as tmp:
        store.httpx, seen.LEDGER, trends.LOCAL = fake, os.path.join(tmp, "seen_ideas.json"), os.path.join(tmp, "topic_obs.json")
        for k in saved[2]:
            os.environ.pop(k, None)
        if configured:
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"] = "https://example.supabase.co", KEY
        try:
            return fn(fake)
        finally:
            store.httpx, seen.LEDGER, trends.LOCAL = saved[0], saved[1], saved[3]
            for k, v in saved[2].items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def item(title, url, source="OpenAI"):
    return sources.normalize({"title": title, "url": url, "source": source, "summary": "", "published": NOW,
                              "metric": None, "metric_name": None}, NOW)


def check_probe_states():
    def case(fake, want, configured=True):
        def go(f):
            state, detail = store.probe()
            assert state == want, (state, want, detail)
            assert KEY not in detail, "the key or a response body leaked into the status"
            return detail
        return with_supabase(go, fake, configured)
    case(FakePostgREST(), "ok")
    case(FakePostgREST(), "off", configured=False)
    case(FakePostgREST(missing=True), "tables_missing")
    f = FakePostgREST(); f.fail["GET"] = (404, {"code": "OTHER"}); case(f, "error")
    f = FakePostgREST(); f.fail["GET"] = (401, {"message": f"bad key {KEY}"})
    assert "SUPABASE_SECRET_KEY rejected" in case(f, "error")
    f = FakePostgREST(); f.fail["GET"] = (500, "oops"); case(f, "error")
    f = FakePostgREST(); f.fail["GET"] = OSError(f"connect failed {KEY}"); case(f, "error")


def check_roundtrip_over_http():
    def good(fake):
        steps = store.roundtrip()
        assert [ok for _, ok, _ in steps] == [True] * 5, steps
        assert fake.rows == [], "the throwaway row is deleted"
        post = next(entry for entry in fake.log if entry[0] == "POST")
        assert post[1].endswith("/rest/v1/discovery_seen") and post[2] == {"on_conflict": "kind,url"}
        assert post[3]["Prefer"] == "resolution=ignore-duplicates" and post[3]["Authorization"] == f"Bearer {KEY}"
        delete = next(entry for entry in fake.log if entry[0] == "DELETE")
        assert delete[2]["kind"] == "eq.selftest" and delete[2]["url"].startswith("eq.https://selftest.invalid/")
    with_supabase(good)

    def write_fails(fake):
        fake.fail["POST"] = (500, {})
        steps = store.roundtrip()
        assert len(steps) == 2 and steps[1][:2] == ("write a row", False), steps
        assert not any(entry[0] == "DELETE" for entry in fake.log)
    with_supabase(write_fails)

    def read_back_fails(fake):
        fake.fail_select = "url,title"  # the probe still passes
        steps = store.roundtrip()
        assert dict((n, ok) for n, ok, _ in steps)["read it back"] is False
        assert dict((n, ok) for n, ok, _ in steps)["delete it"] is True
        assert fake.rows == [], "cleaned up even though the read-back failed"
        assert dict((n, ok) for n, ok, _ in steps)["confirm it is gone"] is False, "an unreadable table is not 'gone'"
    with_supabase(read_back_fails)

    def tables_missing(fake):
        steps = store.roundtrip()
        assert len(steps) == 1 and steps[0][1] is False and "tables_missing" in steps[0][2]
    with_supabase(tables_missing, FakePostgREST(missing=True))


def check_fallback_is_explicit():
    def go(fake):
        a = cluster.cluster([item("Anthropic ships Claude Duet app", "https://a.com/1")])[0]
        rows, state = seen.load(14, NOW)
        assert rows == [] and state["backend"] == "local_fallback" and state["remote"] == "tables_missing"
        state = seen.mark([(a["canonical_url"], a["title"])], NOW, state)
        msg = seen.describe(state)
        assert state["remote_wrote"] is None and state["local_wrote"] is True and os.path.exists(seen.LEDGER)
        assert "LOCAL FALLBACK" in msg and "docs/supabase.sql" in msg and "SUPABASE (shared" not in msg, msg
        assert fake.rows == [], "nothing was written to Supabase"

        # the next day, the fallback file still excludes it: same link, and same story under a new link
        later = NOW + timedelta(days=1)
        rows, _ = seen.load(14, later)
        again = cluster.cluster([item("Anthropic ships Claude Duet app", "https://a.com/1?utm_source=x", "Google News")])[0]
        reworded = cluster.cluster([item("Claude Duet app shipped by Anthropic", "https://other.com/9", "dev.to")])[0]
        new = cluster.cluster([item("Gardening robots learn to weed", "https://g.com/1", "dev.to")])[0]
        fresh, dropped = seen.filter([again, reworded, new], rows)
        assert [c["title"] for c in fresh] == [new["title"]] and dropped == {"same_url": 1, "same_story": 1}, (fresh, dropped)
        assert seen.filter([again], seen.load(14, NOW + timedelta(days=15))[0])[0] == [again], "outside the window it may return"
    with_supabase(go, FakePostgREST(missing=True))

    def not_configured(fake):
        _, state = seen.load(14, NOW)
        assert state["backend"] == "local_fallback" and state["remote"] == "off"
        assert "SUPABASE_URL" in seen.describe(state)
        assert fake.log == [], "no request is made without configuration"
    with_supabase(not_configured, configured=False)


def check_supabase_is_the_ledger_across_two_runs():
    topics = ["Sandbox for Codex agents", "Realtime voice models launch", "Memory for coding assistants",
              "Evaluating agent benchmarks honestly", "Cheaper embeddings for search", "Open weights vision model"]
    items = [item(t, f"https://s{n}.com/a") for n, t in enumerate(topics)]
    saved_fetch, saved_out, saved_key = sources.fetch_all, run.OUT_DIR, os.environ.pop("GROQ_API_KEY", None)

    def go(fake):
        with tempfile.TemporaryDirectory() as out:
            run.OUT_DIR = out
            sources.fetch_all = lambda now: (items, {"feed:OpenAI": "ok:6"})
            first = run.run(top=3, briefs=0, now=NOW)
            # a separate run: nothing is carried over in memory; only what Supabase holds
            second = run.run(top=3, briefs=0, now=NOW + timedelta(days=1))
        a, b = {i["id"] for i in first["ideas"]}, {i["id"] for i in second["ideas"]}
        assert len(a) == 3 and len(b) == 3 and not a & b, "yesterday's ideas came back"
        assert second["already_seen"]["same_url"] == 3
        stored = [r for r in fake.rows if r["kind"] == "idea"]
        assert len(stored) == 6 and all(r["url"].startswith("https://") and r["title"] for r in stored)
        assert first["seen_filter"]["backend"] == second["seen_filter"]["backend"] == "supabase"
        assert first["seen_filter"]["message"].startswith("Seen-filter: SUPABASE"), first["seen_filter"]["message"]
        assert first["seen_filter"]["local_wrote"] is None and not os.path.exists(seen.LEDGER), "no silent local copy"
    try:
        with_supabase(go)
    finally:
        sources.fetch_all, run.OUT_DIR = saved_fetch, saved_out
        if saved_key:
            os.environ["GROQ_API_KEY"] = saved_key


def check_local_rows_are_copied_up():
    def go(fake):
        seen._write_ledger([("https://old.com/a", "Shown during a fallback run")], NOW)
        rows, state = seen.load(14, NOW)
        assert state["backend"] == "supabase" and state["synced"] == 1 and ("https://old.com/a", "Shown during a fallback run") in rows
        assert [r["url"] for r in fake.rows] == ["https://old.com/a"]
        assert "Copied 1 ideas" in seen.describe(state)
        _, again = seen.load(14, NOW)
        assert again["synced"] == 0, "copied once, not every run"
    with_supabase(go)


def check_remote_failures_are_reported():
    def read_fails(fake):
        fake.fail_select = "url,title"
        rows, state = seen.load(14, NOW)
        msg = seen.describe(state)
        assert state["backend"] == "local_fallback" and state["remote"] == "error", state
        assert "reading discovery_seen failed" in msg and "SUPABASE (shared" not in msg, msg
    with_supabase(read_fails)

    def write_fails(fake):
        _, state = seen.load(14, NOW)
        assert state["backend"] == "supabase"
        fake.fail["POST"] = (500, {})
        state = seen.mark([("https://a.com/z", "t")], NOW, state)
        msg = seen.describe(state)
        assert state["backend"] == "local_fallback" and state["remote_wrote"] is False and state["local_wrote"] is True
        assert "the Supabase write failed" in msg and "LOCAL FALLBACK" in msg and "SUPABASE (shared" not in msg, msg
        assert os.path.exists(seen.LEDGER), "the run is still remembered"
    with_supabase(write_fails)

    def sync_fails(fake):
        seen._write_ledger([("https://old.com/a", "Shown earlier")], NOW)
        fake.fail["POST"] = (500, {})
        rows, state = seen.load(14, NOW)
        assert state["backend"] == "supabase" and state["sync_failed"] == 1 and state["synced"] == 0
        assert ("https://old.com/a", "Shown earlier") in rows, "still excluded this run"
        assert "could not be copied" in seen.describe(state)
    with_supabase(sync_fails)


def check_local_file_edge_cases():
    def go(fake):
        os.makedirs(os.path.dirname(seen.LEDGER), exist_ok=True)
        for junk in ("{not json", "[]", '{"ideas": 5}', ""):
            with open(seen.LEDGER, "w", encoding="utf-8") as f:
                f.write(junk)
            rows, state = seen.load(14, NOW)
            assert rows == [] and state["local"] == "corrupt", (junk, state)
            assert "reset" in seen.describe(state)
        with open(seen.LEDGER, "w", encoding="utf-8") as f:  # a hand-edited, zone-less time still compares
            json.dump({"ideas": {"https://a.com/x": {"title": "t", "at": "2026-10-24T10:00:00"}, "https://b.com": 7}}, f)
        rows, _ = seen.load(14, NOW)
        assert rows == [("https://a.com/x", "t")], rows
        seen.LEDGER = os.path.join(seen.LEDGER, "nested", "x.json")  # parent is a file: cannot be written
        state = seen.mark([("https://a.com/y", "t")], NOW, {"backend": "local_fallback", "remote": "off", "remote_detail": "",
                                                          "local": "ok", "known": 0})
        assert state["local_wrote"] is False and "WILL repeat" in seen.describe(state)
    with_supabase(go, FakePostgREST(missing=True))


if __name__ == "__main__":
    check_probe_states()
    check_roundtrip_over_http()
    check_fallback_is_explicit()
    check_supabase_is_the_ledger_across_two_runs()
    check_local_rows_are_copied_up()
    check_remote_failures_are_reported()
    check_local_file_edge_cases()
    print("seen tests passed")
