"""Memory that survives a Render restart: Supabase Postgres, reached over its REST API.

Render's free disk is wiped on every restart and deploy, which used to reset the posted
history (same story drafted twice) and the last draft (/repurpose, /latest forgot).

What lives here (tables in docs/supabase.sql):
    seen      urls already posted ("posted") or already shown in a digest ("digest")
    kv        small JSON blobs by key; "last_run" is the last draft
    feedback  every approve / reject / expire, with the draft before and after your edits

Not configured (no SUPABASE_URL / SUPABASE_SECRET_KEY) or failing → every function quietly does
nothing or returns empty, and callers fall back to the local files in data/. A store
outage must never stop a draft or a digest.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from agent.paths import ENV_FILE

load_dotenv(ENV_FILE, override=True)

TIMEOUT = 10
MAX_ROWS = 1000  # PostgREST's default page; ponytail: newest 1000 urls per kind is years of posts, page if it ever isn't
# Tables are prefixed so this app can never read or write another app's seen/kv/feedback tables,
# even if both point at the same Supabase project.
TABLE_PREFIX = "discovery_"


def _conf():
    # secret/service_role key only. Never the anon key (RLS is on with no policies, so anon sees nothing anyway).
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY")
    if not (url and key):
        return None
    return f"{url.rstrip('/')}/rest/v1", {"apikey": key, "Authorization": f"Bearer {key}"}


def enabled():
    return _conf() is not None


def _call(method, table, params=None, body=None, prefer=None):
    """→ parsed rows for GET, True for a successful write, None when off or on any failure."""
    conf = _conf()
    if not conf:
        return None
    base, headers = conf
    if prefer:
        headers = {**headers, "Prefer": prefer}
    try:
        url = f"{base}/{TABLE_PREFIX}{table}"
        if method == "GET":
            r = httpx.get(url, params=params, headers=headers, timeout=TIMEOUT)
        elif method == "DELETE":
            r = httpx.delete(url, params=params, headers=headers, timeout=TIMEOUT)
        else:
            r = httpx.post(url, params=params, headers=headers, json=body, timeout=TIMEOUT)
        if r.status_code >= 300:
            # e.g. 404 = tables not created yet (run docs/supabase.sql), 401 = wrong key
            print(f"Store: {method} {table} failed, HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return None
        return r.json() if method == "GET" else True
    except Exception as e:
        print(f"Store: {method} {table} failed ({type(e).__name__}).", flush=True)  # never str(e): may carry headers
        return None


def probe(table="seen"):
    """→ (state, detail): is the store usable? state is "off" (not configured), "ok", "tables_missing" (run
    docs/supabase.sql) or "error". Reads one row of `table`; prints nothing; detail never carries the key or a response body."""
    conf = _conf()
    if not conf:
        return "off", "SUPABASE_URL / SUPABASE_SECRET_KEY not set"
    base, headers = conf
    try:
        r = httpx.get(f"{base}/{TABLE_PREFIX}{table}", params={"select": "*", "limit": "1"}, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        return "error", type(e).__name__
    if r.status_code < 300:
        return "ok", ""
    try:
        code = r.json().get("code")
    except Exception:
        code = None
    if r.status_code == 404 and code == "PGRST205":
        return "tables_missing", f"table {TABLE_PREFIX}{table} not found"
    if r.status_code in (401, 403):
        return "error", f"HTTP {r.status_code}: SUPABASE_SECRET_KEY rejected"
    return "error", f"HTTP {r.status_code}"


def seen_rows(kind, days=None):
    """→ [(url, title)] recorded under `kind`, optionally only the last `days` days.
    None when the read failed or the store is off: "nothing recorded" ([]) and "could not read" are different."""
    params = {"select": "url,title", "kind": f"eq.{kind}", "order": "at.desc", "limit": str(MAX_ROWS)}
    if days:
        params["at"] = f"gte.{(datetime.now(timezone.utc) - timedelta(days=days)).isoformat()}"
    rows = _call("GET", "seen", params)
    if not isinstance(rows, list):
        return None
    return [(r["url"], r.get("title") or "") for r in rows
            if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"]]


def seen(kind, days=None):
    """→ set of urls recorded under `kind`, optionally only the last `days` days. Empty when off or failing."""
    return {url for url, _ in seen_rows(kind, days) or []}


PAGE = 1000  # Supabase answers at most this many rows per request, whatever limit is asked for
MAX_PAGES = 50


def topic_obs_rows(since):
    """→ every topic observation with obs_date >= `since` ("YYYY-MM-DD"), oldest first, as dicts. Reads page after page,
    because one request silently stops at PAGE rows. None when a read fails, the store is off, or there are more than
    MAX_PAGES pages (a truncated history would be read as a short one)."""
    out = []
    for page in range(MAX_PAGES):
        rows = _call("GET", "topic_obs", {"select": "*", "obs_date": f"gte.{since}", "order": "obs_date.asc,topic_key.asc",
                                          "limit": str(PAGE), "offset": str(page * PAGE)})
        if not isinstance(rows, list):
            return None
        out += [r for r in rows if isinstance(r, dict)]
        if len(rows) < PAGE:
            return out
    return None


def add_topic_obs(rows, chunk=500):
    """Insert observations; a (topic_key, obs_date) that already exists is left alone, so a re-run adds nothing.
    True only if every chunk was accepted."""
    rows = list(rows)
    return all(_call("POST", "topic_obs", {"on_conflict": "topic_key,obs_date"}, rows[i:i + chunk],
                     "resolution=ignore-duplicates") for i in range(0, len(rows), chunk)) if rows else True


def delete_topic_obs(topic_key):
    return bool(_call("DELETE", "topic_obs", {"topic_key": f"eq.{topic_key}"}))


def roundtrip():
    """Live check that persistence really works: write a throwaway row, read it back, delete it, confirm it is gone.
    → [(step, ok, detail)], stopping early only when the store is unusable. The row is kind "selftest" with a
    random url, so no real data is read, changed or deleted; it is removed even if the read-back fails."""
    url = f"https://selftest.invalid/{uuid.uuid4().hex}"
    steps = []

    def step(name, ok, detail=""):
        steps.append((name, bool(ok), detail))
        return bool(ok)

    state, detail = probe()
    if not step("store reachable and tables exist", state == "ok", f"{state}: {detail}" if detail else state):
        return steps
    if not step("write a row", mark_seen("selftest", [(url, "persistence self-test")])):
        return steps
    rows = seen_rows("selftest")
    step("read it back", rows is not None and any(u == url for u, _ in rows))
    step("delete it", _call("DELETE", "seen", {"kind": "eq.selftest", "url": f"eq.{url}"}))
    rows = seen_rows("selftest")
    step("confirm it is gone", rows is not None and all(u != url for u, _ in rows))
    return steps


def mark_seen(kind, pairs):
    """pairs: iterable of (url, title). Already-recorded urls are left alone."""
    rows = {url: {"kind": kind, "url": url, "title": (title or "")[:300]}
            for url, title in pairs if isinstance(url, str) and url}  # dict: one row per url in a batch
    if not rows:
        return False
    return bool(_call("POST", "seen", {"on_conflict": "kind,url"}, list(rows.values()), "resolution=ignore-duplicates"))


def save(key, value):
    body = {"key": key, "value": value, "at": datetime.now(timezone.utc).isoformat()}
    return bool(_call("POST", "kv", {"on_conflict": "key"}, body, "resolution=merge-duplicates"))


def load(key):
    """→ the stored value, or None if absent / off / failed."""
    rows = _call("GET", "kv", {"select": "value", "key": f"eq.{key}", "limit": "1"})
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0].get("value")
    return None


def log_feedback(**row):
    """One row per decision on a draft. The raw material for tuning prompts later."""
    return bool(_call("POST", "feedback", None, row))


if __name__ == "__main__":
    # no network: with no Supabase config every call must quietly do nothing
    for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY"):
        os.environ.pop(k, None)
    assert not enabled() and seen("posted") == set() and load("last_run") is None
    assert save("k", {}) is False and mark_seen("posted", [("https://a", "t")]) is False
    # with config, every table name carries the prefix, so no unprefixed table is ever touched
    os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"] = "https://example.supabase.co", "k"
    calls = []
    globals()["httpx"] = type("H", (), {"get": staticmethod(lambda url, **kw: calls.append(url) or type("R", (), {"status_code": 200, "json": lambda self: []})()),
                                         "post": staticmethod(lambda url, **kw: calls.append(url) or type("R", (), {"status_code": 201, "text": ""})())})()
    seen("posted"), save("last_run", {}), log_feedback(status="x")
    assert calls and all("/rest/v1/discovery_" in u for u in calls), calls
    print("store offline + table-prefix self-check passed")
