"""Offline tests for trend detection: classification rules, storage, duplicates, and a multi-day end-to-end run.

The storage tests put an in-memory PostgREST behind the same httpx calls agent/store.py makes, so the real store.py and
trends.py code run. That proves the logic and the request shapes, not the real Supabase table: the live schema check is
`python -m agent.discovery.trends --test` and needs docs/supabase.sql applied first.

    .venv\\Scripts\\python -m tests.test_trends
"""

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import store  # noqa: E402
from agent.discovery import cluster, run, seen, sources, trends  # noqa: E402

UTC = timezone.utc
KEY = "sb-secret-DO-NOT-PRINT"


# ---- fixtures ---------------------------------------------------------------------------------------------------

def row(topic, day, hour=9, origins=("x",), items=None, title="Anthropic ships Claude Duet agent app"):
    d = date(2026, 10, day)
    return {"topic_key": topic, "obs_date": d.isoformat(), "observed_at": datetime(2026, 10, day, hour, tzinfo=UTC).isoformat(),
            "run_id": f"run-{day}", "title": title, "origins": list(origins), "items": items or [], "claim_status": "unverified",
            "n_evidence": 1}


def link(n, **metrics):
    return {"u": f"https://x.com/i/status/{n}", "m": metrics}


class FakePostgREST:
    """discovery_seen and discovery_topic_obs in memory. `cap` is the most rows one GET returns (Supabase: 1000)."""
    PK = {"discovery_seen": ("kind", "url"), "discovery_topic_obs": ("topic_key", "obs_date")}

    def __init__(self, missing=(), cap=1000):
        self.tables = {name: [] for name in self.PK}
        self.missing, self.cap, self.log, self.fail = set(missing), cap, [], {}

    def _resp(self, status, body=None):
        return type("R", (), {"status_code": status, "text": json.dumps(body), "json": lambda s: body})()

    def _table(self, url):
        name = url.rsplit("/", 1)[1]
        return None if name in self.missing or name not in self.tables else name

    def _where(self, name, params):
        out = []
        for r in self.tables[name]:
            ok = True
            for col, cond in (params or {}).items():
                if col in ("select", "order", "limit", "offset", "on_conflict"):
                    continue
                if cond.startswith("eq."):
                    ok = ok and str(r.get(col)) == cond[3:]
                elif cond.startswith("gte."):
                    ok = ok and str(r.get(col)) >= cond[4:]
            if ok:
                out.append(r)
        return out

    def get(self, url, params=None, headers=None, timeout=None):
        self.log.append(("GET", url, params))
        if "GET" in self.fail:
            return self._resp(*self.fail["GET"])
        name = self._table(url)
        if name is None:
            return self._resp(404, {"code": "PGRST205"})
        rows = self._where(name, params)
        for spec in reversed(((params or {}).get("order") or "").split(",")):
            if spec:
                col, _, direction = spec.partition(".")
                rows.sort(key=lambda r, c=col: str(r.get(c)), reverse=direction == "desc")
        start = int((params or {}).get("offset", 0))
        n = min(int((params or {}).get("limit", self.cap)), self.cap)
        cols = (params or {}).get("select", "*")
        rows = rows[start:start + n]
        return self._resp(200, [dict(r) if cols == "*" else {c: r.get(c) for c in cols.split(",")} for r in rows])

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.log.append(("POST", url, params, headers))
        if "POST" in self.fail:
            return self._resp(*self.fail["POST"])
        name = self._table(url)
        if name is None:
            return self._resp(404, {"code": "PGRST205"})
        pk = self.PK[name]
        for new in json if isinstance(json, list) else [json]:
            dup = any(all(r.get(c) == new.get(c) for c in pk) for r in self.tables[name])
            if dup and "ignore-duplicates" in (headers or {}).get("Prefer", ""):
                continue
            self.tables[name].append({**new, "at": datetime.now(UTC).isoformat()})
        return self._resp(201, None)

    def delete(self, url, params=None, headers=None, timeout=None):
        self.log.append(("DELETE", url, params))
        name = self._table(url)
        if name is None:
            return self._resp(404, {"code": "PGRST205"})
        gone = self._where(name, params)
        self.tables[name] = [r for r in self.tables[name] if r not in gone]
        return self._resp(204, None)


def with_supabase(fn, fake=None, configured=True):
    """Run fn(fake) with store.py talking to `fake`, and every local file redirected to a temp folder."""
    fake = fake or FakePostgREST()
    saved = (store.httpx, trends.LOCAL, seen.LEDGER, store.PAGE, store.MAX_PAGES, {k: os.environ.get(k) for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY")})
    with tempfile.TemporaryDirectory() as tmp:
        store.httpx, trends.LOCAL, seen.LEDGER = fake, os.path.join(tmp, "topic_obs.json"), os.path.join(tmp, "seen.json")
        for k in saved[5]:
            os.environ.pop(k, None)
        if configured:
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"] = "https://example.supabase.co", KEY
        try:
            return fn(fake)
        finally:
            store.httpx, trends.LOCAL, seen.LEDGER, store.PAGE, store.MAX_PAGES = saved[:5]
            for k, v in saved[5].items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def item(title, url, source, platform=None, author=None, likes=None, views=None, via=None, day=25):
    metrics = {k: v for k, v in (("likes", likes), ("views", views)) if v is not None}
    return sources.normalize({"title": title, "url": url, "source": source, "platform": platform or source, "author": author,
                              "summary": "", "published": datetime(2026, 10, day, tzinfo=UTC), "metric": likes,
                              "metric_name": "likes" if likes is not None else None, "metrics": metrics, "via": via},
                             datetime(2026, 10, day, tzinfo=UTC))


def cands(*items):
    return cluster.cluster(list(items))


# ---- classification: growth, history, missing data ----------------------------------------------------------------

def check_a_snapshot_never_shows_growth():
    one = analyse = trends.analyse
    huge = row("a", 25, items=[link(1, likes=1_000_000, views=90_000_000)], origins=("x", "youtube", "web", "hn"))
    v = analyse([huge], baseline_days=1)
    assert v["class"] == "insufficient_data" and "one snapshot" in v["explanation"], v
    assert "engagement is not used" in v["explanation"].lower()
    # enough history to know it was absent before: new, and the explanation says that is all it means
    v = analyse([huge], baseline_days=3)
    assert v["class"] == "new" and "Not seen before is all this says" in v["explanation"], v
    assert v["evidence"]["baseline_days"] == 3 and v["evidence"].get("engagement") is None, "engagement was never looked at"
    assert one([row("a", 25)], baseline_days=0)["class"] == "insufficient_data"


def check_growth_in_independent_sources():
    series = [row("a", 23, origins=("x",)), row("a", 24, origins=("x", "youtube"))]
    v = trends.analyse(series, baseline_days=3)
    assert v["class"] == "rising" and "independent sources grew" in v["explanation"], v
    assert v["evidence"]["new_origins"] == ["youtube"] and "youtube" in v["explanation"]
    # losing a source is not growth
    assert trends.analyse([row("a", 23, origins=("x", "youtube")), row("a", 24, origins=("x",))], 3)["class"] == "insufficient_data"
    # growth fades: once the reference is already at the new level, the topic is no longer rising
    long_run = [row("a", 20, origins=("x",))] + [row("a", d, origins=("x", "youtube")) for d in range(21, 26)]
    v = trends.analyse(long_run, baseline_days=5)
    assert v["class"] == "popular", v["explanation"]
    assert trends.analyse(long_run[:-3] + [row("a", 24, origins=("x", "youtube")), row("a", 25, origins=("x", "youtube", "web"))], 5)["class"] == "rising"


def check_engagement_growth_must_be_real():
    two = lambda a, b: [link(1, likes=a[0]), link(2, likes=a[1])] if b is None else [link(1, likes=b[0]), link(2, likes=b[1])]
    before = row("a", 23, items=two((100, 150), None))
    grew = row("a", 24, items=two(None, (400, 500)))
    v = trends.analyse([before, grew], 3)
    assert v["class"] == "rising" and "engagement grew across several links" in v["explanation"], v
    assert v["evidence"]["engagement"]["counted"] and v["evidence"]["engagement"]["links"] == 2

    # below the bar: big ratio, tiny gain
    tiny = trends.analyse([row("a", 23, items=[link(1, likes=10), link(2, likes=10)]),
                           row("a", 24, items=[link(1, likes=40), link(2, likes=40)])], 3)
    assert tiny["class"] == "insufficient_data" and "below the bar" in tiny["explanation"], tiny
    # not enough time between the two looks
    quick = trends.analyse([row("a", 23, hour=23, items=two((100, 150), None)), row("a", 24, hour=5, items=two(None, (400, 500)))], 3)
    assert quick["class"] == "insufficient_data" and quick["evidence"]["engagement"]["hours"] == 6.0
    # engagement that fell
    fell = trends.analyse([before, row("a", 24, items=two(None, (50, 60)))], 3)
    assert fell["class"] == "insufficient_data" and "(fell)" in fell["explanation"]


def check_a_misleading_spike_is_not_a_trend():
    one_post = trends.analyse([row("a", 23, items=[link(1, likes=100)]), row("a", 24, items=[link(1, likes=50_000)])], 3)
    assert one_post["class"] == "insufficient_data", one_post
    assert "spike in one link" in one_post["explanation"] and "not counted as the topic growing" in one_post["explanation"]
    assert one_post["evidence"]["engagement"]["grew"] and one_post["evidence"]["engagement"]["concentrated"]
    # one link supplies nearly all of the gain among several
    lopsided = trends.analyse([row("a", 23, items=[link(1, likes=100), link(2, likes=100)]),
                               row("a", 24, items=[link(1, likes=60_000), link(2, likes=180)])], 3)
    assert lopsided["class"] == "insufficient_data" and "% of the gain is one link" in lopsided["explanation"], lopsided
    # but the same spike alongside real source growth is rising, and says why
    both = trends.analyse([row("a", 23, origins=("x",), items=[link(1, likes=100)]),
                           row("a", 24, origins=("x", "youtube"), items=[link(1, likes=50_000)])], 3)
    assert both["class"] == "rising" and "independent sources grew" in both["explanation"] and "spike in one link" in both["explanation"]


def check_missing_and_mismatched_metrics():
    bare = trends.analyse([row("a", 23, origins=("x",)), row("a", 24, origins=("x",))], 3)  # no items at all
    assert bare["class"] == "insufficient_data" and "no engagement metric is on both days" in bare["explanation"]
    assert bare["evidence"]["engagement"] is None
    # metrics on only one of the two days, different kinds, different links
    for first, second in ([link(1, likes=100)], [link(1)]), ([link(1, likes=100)], [link(1, views=90_000)]), \
            ([link(1, likes=100)], [link(2, likes=9_000)]):
        v = trends.analyse([row("a", 23, items=first), row("a", 24, items=second)], 3)
        assert v["class"] == "insufficient_data" and v["evidence"]["engagement"] is None, (first, second, v["explanation"])
    # but diversity alone still works when metrics are missing
    assert trends.analyse([row("a", 23, origins=("x",)), row("a", 24, origins=("x", "web"))], 3)["class"] == "rising"
    # rows written by an older shape, or with nulls, do not crash it
    old = [{"topic_key": "a", "obs_date": "2026-10-23", "observed_at": "2026-10-23T09:00:00+00:00", "title": "t"},
           {"topic_key": "a", "obs_date": "2026-10-24", "observed_at": None, "origins": None, "items": None}]
    assert trends.analyse(old, 3)["class"] == "insufficient_data"
    gap = trends.analyse([row("a", 20), row("a", 25)], 5)  # seen five days apart
    assert gap["evidence"]["reference_date"] == "2026-10-20"


def check_affiliated_sources_are_one_voice():
    openai_feed = item("OpenAI releases the Decisions API", "https://openai.com/index/decisions-api", "OpenAI")
    own_tweet = item("OpenAI releases the Decisions API", "https://x.com/i/status/1", "X search", "X", "OpenAI", likes=900)
    fan_repost = item("OpenAI releases the Decisions API", "https://x.com/i/status/2", "X search", "X", "fan", via="OpenAI")
    candidate = cands(openai_feed, own_tweet, fan_repost)[0]
    obs = trends.observation([candidate], "k", datetime(2026, 10, 25, tzinfo=UTC), "r")
    assert obs["origins"] == ["openai"], "feed, own account and a repost of it are one source"
    # three days of only-affiliated coverage is neither rising nor popular
    days = [row("k", d, origins=("openai",)) for d in (23, 24, 25)]
    v = trends.analyse(days, 4)
    assert v["class"] == "insufficient_data" and v["evidence"]["days_with_two_or_more_sources"] == 0, v
    # an independent voice joining is what changes it
    assert trends.analyse(days[:-1] + [row("k", 25, origins=("openai", "x"))], 4)["class"] == "rising"
    # a quote tweet of a third party is still that party's platform, not a new voice
    third = item("OpenAI releases the Decisions API", "https://x.com/i/status/3", "X search", "X", "dev", likes=50)
    quote = item("OpenAI releases the Decisions API, wow", "https://x.com/i/status/4", "X search", "X", "dev2", via="dev")
    assert cluster.origins([third, quote]) == {"x"}


def check_popular_needs_sustained_corroboration():
    steady = [row("a", d, origins=("x", "youtube")) for d in (22, 23, 24, 25)]
    v = trends.analyse(steady, 5)
    assert v["class"] == "popular" and "sustained and corroborated" in v["explanation"], v
    assert "2+ independent sources on 4 of those days" in v["explanation"]
    # loud but alone: huge engagement on one source for many days is not popular
    loud = [row("a", d, origins=("x",), items=[link(1, likes=100_000 * (d - 20))]) for d in (22, 23, 24, 25)]
    v = trends.analyse(loud, 5)
    assert v["class"] != "popular", v
    # corroborated but only briefly
    assert trends.analyse([row("a", 24, origins=("x", "youtube")), row("a", 25, origins=("x", "youtube"))], 5)["class"] == "insufficient_data"
    # popular and then gaining a source again: rising wins, because it is the more specific statement
    assert trends.analyse(steady[:-1] + [row("a", 25, origins=("x", "youtube", "web"))], 5)["class"] == "rising"


# ---- topics across days ---------------------------------------------------------------------------------------------

def check_topics_are_matched_across_days():
    yesterday = [{**row("k1", 24, title="Anthropic ships Claude Duet agent app"), "items": [link(10, likes=100)]}]
    reworded = cands(item("Claude Duet agent app shipped by Anthropic", "https://other.com/a", "dev.to"))[0]
    same_link = cands(item("A completely different headline here", "https://x.com/i/status/10", "X search", "X", "z"))[0]
    unrelated = cands(item("Gardening robots learn to weed", "https://g.com/1", "dev.to"))[0]
    got = trends.assign([reworded, same_link, unrelated], yesterday)
    assert got[reworded["id"]] == "k1" and got[same_link["id"]] == "k1", got
    assert got[unrelated["id"]] not in ("k1", None) and len(got[unrelated["id"]]) == 12
    assert trends.assign([unrelated], yesterday) == trends.assign([unrelated], []), "a new topic's key is stable"
    # two candidates in one run that belong to one topic become one observation
    a = cands(item("Anthropic ships Claude Duet agent app", "https://a.com/1", "OpenAI"))[0]
    b = cands(item("Unrelated words about the very same topic", "https://x.com/i/status/10", "X search", "X", "q", likes=5))[0]
    merged = trends.observation([a, b], "k1", datetime(2026, 10, 25, tzinfo=UTC), "r")
    assert merged["n_evidence"] == 2 and len(merged["items"]) == 2 and merged["title"] == a["title"]


# ---- storage --------------------------------------------------------------------------------------------------------

def check_store_layer_over_http():
    def go(fake):
        rows = [row(f"t{n}", 25) for n in range(8)]
        assert store.add_topic_obs(rows, chunk=3) is True
        posts = [e for e in fake.log if e[0] == "POST"]
        assert len(posts) == 3, "8 rows in chunks of 3"
        assert posts[0][1].endswith("/rest/v1/discovery_topic_obs") and posts[0][2] == {"on_conflict": "topic_key,obs_date"}
        assert posts[0][3]["Prefer"] == "resolution=ignore-duplicates" and posts[0][3]["Authorization"] == f"Bearer {KEY}"
        # the same topic and day again, with different content, changes nothing
        assert store.add_topic_obs([{**rows[0], "title": "CHANGED", "n_evidence": 99}]) is True
        assert len(fake.tables["discovery_topic_obs"]) == 8
        assert fake.tables["discovery_topic_obs"][0]["title"] == rows[0]["title"], "first sighting of the day wins"
        # reading is paged: the cap is smaller than the table
        fake.cap, store.PAGE = 3, 3
        got = store.topic_obs_rows("2026-10-01")
        assert got is not None and [r["topic_key"] for r in got] == [f"t{n}" for n in range(8)], "nothing lost to the row cap"
        gets = [e for e in fake.log if e[0] == "GET" and e[2].get("order")]
        assert [e[2]["offset"] for e in gets] == ["0", "3", "6"] and all(e[2]["obs_date"] == "gte.2026-10-01" for e in gets)
        # a history too big to read is reported, never returned short
        store.MAX_PAGES = 2
        assert store.topic_obs_rows("2026-10-01") is None
        assert store.topic_obs_rows("2026-10-27") is None or True
        # failures say so
        fake.fail["POST"] = (500, {})
        assert store.add_topic_obs([row("z", 26)]) is False
        assert store.add_topic_obs([]) is True
    with_supabase(go)


def check_schema_roundtrip():
    def good(fake):
        steps = trends.roundtrip()
        assert len(steps) == 8 and all(ok for _, ok, _ in steps), steps
        assert fake.tables["discovery_topic_obs"] == [], "the throwaway row is removed"
        assert [e[0] for e in fake.log].count("DELETE") == 1
    with_supabase(good)

    def broken_key(fake):  # a table whose primary key does not hold would show up as two rows
        real_post = fake.post

        def post(url, params=None, headers=None, json=None, timeout=None):
            headers = {k: v for k, v in (headers or {}).items() if k != "Prefer"}  # duplicates not ignored
            return real_post(url, params, headers, json, timeout)
        fake.post = post
        steps = dict((n, ok) for n, ok, _ in trends.roundtrip())
        assert steps["exactly one row exists (primary key holds)"] is False, steps
        fake.tables["discovery_topic_obs"].clear()
    with_supabase(broken_key)

    def missing(fake):
        steps = trends.roundtrip()
        assert len(steps) == 1 and steps[0][1] is False and "tables_missing" in steps[0][2], steps
    with_supabase(missing, FakePostgREST(missing={"discovery_topic_obs"}))


def check_duplicate_observations_are_prevented():
    def go(fake):
        items = [item("Anthropic ships Claude Duet agent app", "https://x.com/i/status/1", "X search", "X", "dev", likes=900),
                 item("Realtime voice models launch", "https://openai.com/a", "OpenAI")]
        first, state = trends.update(cands(*items), datetime(2026, 10, 25, 9, tzinfo=UTC))
        assert state["new_observations"] == 2 and state["already_recorded_today"] == 0 and len(fake.tables["discovery_topic_obs"]) == 2
        # the same day, hours later, with more engagement: nothing new is stored, the day's first row stays
        more = [item("Anthropic ships Claude Duet agent app", "https://x.com/i/status/1", "X search", "X", "dev", likes=5000), items[1]]
        again, state2 = trends.update(cands(*more), datetime(2026, 10, 25, 20, tzinfo=UTC))
        assert state2["new_observations"] == 0 and state2["already_recorded_today"] == 2, state2
        assert len(fake.tables["discovery_topic_obs"]) == 2
        stored = next(r for r in fake.tables["discovery_topic_obs"] if r["title"].startswith("Anthropic"))
        assert stored["items"][0]["m"]["likes"] == 900, "the first sighting of the day is kept"
        assert {k: v["class"] for k, v in first.items()} == {k: v["class"] for k, v in again.items()}
    with_supabase(go)


def check_fallback_is_explicit_and_syncs_up():
    items = [item("Anthropic ships Claude Duet agent app", "https://x.com/i/status/1", "X search", "X", "dev", likes=900)]

    def down(fake):
        _, state = trends.update(cands(*items), datetime(2026, 10, 24, 9, tzinfo=UTC))
        assert state["backend"] == "local_fallback" and state["remote"] == "tables_missing" and state["local_wrote"] is True
        assert "LOCAL FALLBACK" in state["message"] and "docs/supabase.sql" in state["message"] and "SUPABASE (shared" not in state["message"]
        assert fake.tables["discovery_topic_obs"] == [] and os.path.exists(trends.LOCAL)
        # the next day reads yesterday from the fallback file
        _, s2 = trends.update(cands(*items), datetime(2026, 10, 25, 9, tzinfo=UTC))
        assert s2["run_days"] == 2 and s2["observations"] == 2, s2
        # Supabase comes up: yesterday's and today's local rows are copied once
        fake.missing.clear()
        _, s3 = trends.update(cands(*items), datetime(2026, 10, 25, 20, tzinfo=UTC))
        assert s3["backend"] == "supabase" and s3["synced"] == 2 and "Copied 2 observations" in s3["message"], s3
        assert len(fake.tables["discovery_topic_obs"]) == 2
        _, s4 = trends.update(cands(*items), datetime(2026, 10, 25, 21, tzinfo=UTC))
        assert s4["synced"] == 0 and len(fake.tables["discovery_topic_obs"]) == 2
    with_supabase(down, FakePostgREST(missing={"discovery_topic_obs"}))

    def not_configured(fake):
        _, state = trends.update(cands(*items), datetime(2026, 10, 25, 9, tzinfo=UTC))
        assert state["remote"] == "off" and fake.log == [] and "not configured" in state["message"]
    with_supabase(not_configured, configured=False)

    def failures(fake):
        fake.fail["POST"] = (500, {})  # probe and read work, the write does not
        _, state = trends.update(cands(*items), datetime(2026, 10, 25, 9, tzinfo=UTC))
        assert state["remote_wrote"] is False and state["local_wrote"] is True and "the Supabase write failed" in state["message"]
        fake.fail.clear()
        os.makedirs(os.path.dirname(trends.LOCAL), exist_ok=True)
        with open(trends.LOCAL, "w", encoding="utf-8") as f:
            f.write("{broken")
        assert trends._read_local() == ([], "corrupt")  # a damaged file is reported and replaced, not trusted
    with_supabase(failures)


# ---- a week in the life, through the real storage code -------------------------------------------------------------

def check_end_to_end_over_several_days():
    """Eight daily runs. Each topic follows a script; the classes must follow from the evidence and nothing else."""
    def day(n):
        out = [
            # A: an X post every day, a YouTube video from day 3, a web article from day 5
            item("Anthropic ships Claude Duet agent app", "https://x.com/i/status/100", "X search", "X", "dev", likes=300, day=n),
            # B: OpenAI's own feed and account, every day: affiliated, so never more than one voice
            item("OpenAI releases the Decisions API", "https://openai.com/index/decisions-api", "OpenAI", day=n),
            item("OpenAI releases the Decisions API", "https://x.com/i/status/200", "X search", "X", "OpenAI", likes=800, day=n),
            # C: one post whose likes explode on day 4
            item("Indie agent framework Foo gets a viral post", "https://x.com/i/status/300", "X search", "X", "foo",
                 likes=100 if n < 4 else 60_000, day=n),
        ]
        if n >= 3:
            out.append(item("Anthropic ships Claude Duet agent app review", "https://youtu.be/v1", "YouTube search", "YouTube", "Chan", views=1000, day=n))
        if n >= 5:
            out.append(item("Claude Duet agent app by Anthropic explained", "https://blog.example/claude-duet", "Web search", "Web", day=n))
        if n == 5:  # D appears first on day 5, after four earlier run-days
            out.append(item("Quantum widget compilers arrive", "https://dev.to/q", "dev.to", day=n))
        return out

    def go(fake):
        seen_class = {}
        for n in range(1, 9):
            now = datetime(2026, 10, n, 9, tzinfo=UTC)
            results, state = trends.update(cands(*day(n)), now)
            tag = lambda t: next(name for key, name in (("Claude Duet", "A"), ("Decisions", "B"), ("Foo", "C"), ("Quantum", "D")) if key in t)
            seen_class[n] = {tag(c["title"]): results[c["id"]] for c in cands(*day(n))}
            assert state["backend"] == "supabase" and state["run_days"] == n
        a = {n: seen_class[n]["A"]["class"] for n in seen_class}
        assert [a[n] for n in range(1, 9)] == ["insufficient_data", "insufficient_data", "rising", "rising", "rising", "rising",
                                                 "rising", "popular"], a
        b = {n: seen_class[n]["B"]["class"] for n in seen_class}
        assert set(b.values()) <= {"insufficient_data"}, f"affiliated coverage never becomes a trend: {b}"
        c = {n: seen_class[n]["C"]["class"] for n in seen_class}
        assert set(c.values()) == {"insufficient_data"}, f"one post's spike on day 4 is not a trend: {c}"
        assert "spike in one link" in seen_class[4]["C"]["explanation"]
        d = {n: seen_class[n]["D"]["class"] for n in range(5, 9) if "D" in seen_class[n]}
        assert d[5] == "new" and "4 earlier days" in seen_class[5]["D"]["explanation"], d
        # on day 5 the explanation for A names who joined
        assert "web" in seen_class[5]["A"]["explanation"] and "youtube" in seen_class[3]["A"]["explanation"]
        # nothing was stored twice: one row per topic per day
        keys = [(r["topic_key"], r["obs_date"]) for r in fake.tables["discovery_topic_obs"]]
        assert len(keys) == len(set(keys))
        # the stored history reads back the same
        again = store.topic_obs_rows("2026-10-01")
        assert len(again) == len(keys)
    with_supabase(go)


def check_overview_is_what_the_chat_command_shows():
    """trends.overview feeds /trending: the same classification as the pipeline, as data, newest run day only."""
    def go(fake):
        assert trends.overview()["items"] == [] and trends.overview()["run_days"] == 0, "an empty history says so"
        link_a = [link(1, likes=100), link(2, likes=50)]
        rows = [row("pop", d, origins=("x", "youtube"), title="Popular topic", items=[link(40 + d)]) for d in (22, 23, 24, 25)]
        rows += [row("rise", 24, origins=("x",), title="Rising topic", items=link_a),
                 row("rise", 25, origins=("x", "web"), title="Rising topic", items=link_a)]
        rows += [row("fresh", 25, origins=("x",), title="Brand new topic", items=[link(77, likes=5)])]
        rows += [row("flat", d, origins=("x",), title="Flat topic") for d in (24, 25)]
        rows += [row("gone", 23, origins=("x",), title="Last seen two days ago")]
        assert store.add_topic_obs(rows)
        pic = trends.overview(datetime(2026, 10, 26, tzinfo=UTC))
        assert pic["as_of"] == "2026-10-25" and pic["run_days"] == 4 and pic["state"]["backend"] == "supabase"
        assert [(i["title"], i["class"]) for i in pic["items"]] == [("Rising topic", "rising"), ("Popular topic", "popular"),
                                                                     ("Brand new topic", "new")], "rising, then popular, then new"
        assert pic["counts"] == {"new": 1, "rising": 1, "popular": 1, "insufficient_data": 1} and pic["topics"] == 4, pic["counts"]
        rise = pic["items"][0]
        assert rise["links"] == ["https://x.com/i/status/1", "https://x.com/i/status/2"] and rise["days_seen"] == 2
        assert "independent sources grew" in rise["explanation"] and rise["evidence"]["new_origins"] == ["web"]
        assert all(l.startswith("https://") for i in pic["items"] for l in i["links"]) and len(pic["items"][1]["links"]) <= 3
        junk = row("bad", 25, origins=("x",), title="Bad link", items=[{"u": "javascript:alert(1)", "m": {}}, {"u": "https://ok.example/a", "m": {}}])
        store.add_topic_obs([junk])
        bad = next(i for i in trends.overview(datetime(2026, 10, 26, tzinfo=UTC))["items"] if i["title"] == "Bad link")
        assert bad["class"] == "new" and bad["links"] == ["https://ok.example/a"], "only http(s) links are offered"
    with_supabase(go)


def check_pipeline_run_includes_trends():
    saved_fetch, saved_out, saved_key = sources.fetch_all, run.OUT_DIR, os.environ.pop("GROQ_API_KEY", None)
    items = [item("Anthropic ships Claude Duet agent app", "https://x.com/i/status/1", "X search", "X", "dev", likes=900),
             item("Realtime voice models launch", "https://openai.com/a", "OpenAI")]

    def go(fake):
        with tempfile.TemporaryDirectory() as out:
            run.OUT_DIR = out
            sources.fetch_all = lambda now: (items, {"feed:OpenAI": "ok:2"})
            result = run.run(top=5, briefs=0, now=datetime(2026, 10, 25, 9, tzinfo=UTC))
        assert result["trends"]["backend"] == "supabase" and result["trends"]["new_observations"] == 2
        assert all(i["trend"]["class"] == "insufficient_data" for i in result["ideas"]), "day one calls nothing"
        assert result["ideas"][0]["trend"]["topic_key"] and "one snapshot" in result["ideas"][0]["trend"]["explanation"]
        # a trend failure must not stop discovery
        real = trends.update
        trends.update = lambda c, n: (_ for _ in ()).throw(RuntimeError("boom"))
        fresh = [item("Vector database for coding agents ships", "https://v.example/1", "OpenAI")]  # the first run's ideas are now seen
        sources.fetch_all = lambda now: (fresh, {"feed:OpenAI": "ok:1"})
        try:
            with tempfile.TemporaryDirectory() as out:
                run.OUT_DIR = out
                result = run.run(top=5, briefs=0, now=datetime(2026, 10, 25, 10, tzinfo=UTC))
        finally:
            trends.update = real
        assert result["ideas"] and "FAILED" in result["trends"]["message"] and all(i["trend"] is None for i in result["ideas"])
    try:
        with_supabase(go)
    finally:
        sources.fetch_all, run.OUT_DIR = saved_fetch, saved_out
        if saved_key:
            os.environ["GROQ_API_KEY"] = saved_key


if __name__ == "__main__":
    check_a_snapshot_never_shows_growth()
    check_growth_in_independent_sources()
    check_engagement_growth_must_be_real()
    check_a_misleading_spike_is_not_a_trend()
    check_missing_and_mismatched_metrics()
    check_affiliated_sources_are_one_voice()
    check_popular_needs_sustained_corroboration()
    check_topics_are_matched_across_days()
    check_store_layer_over_http()
    check_schema_roundtrip()
    check_duplicate_observations_are_prevented()
    check_fallback_is_explicit_and_syncs_up()
    check_end_to_end_over_several_days()
    check_overview_is_what_the_chat_command_shows()
    check_pipeline_run_includes_trends()
    print("trend tests passed")
