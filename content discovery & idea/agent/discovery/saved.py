"""Ideas a discovery run surfaced, kept so they can be listed later and a brief can be fetched or written for one by id.

A run's JSON in data/discovery/ is a local log. This is the shared copy: the Supabase table discovery_kv, key "discovery_ideas"
(created by docs/supabase.sql), so a restart or a second machine still finds yesterday's ideas. If Supabase cannot be used,
the same list is kept in data/discovery/saved_ideas.json, and where() says which one answered; it is never silent.

Each record is the idea as shown (title, links, score, claim status, trend, warnings, a few evidence entries) plus its brief
once one has been written. At most MAX_SAVED ideas are kept, newest run first, best rank first within a run.
"""

import json
import os
import threading
from datetime import datetime, timezone

from agent import store
from agent.paths import DATA

KV_KEY = "discovery_ideas"
LOCAL = os.path.join(DATA, "discovery", "saved_ideas.json")
MAX_SAVED = 40
MAX_EVIDENCE = 4
_lock = threading.RLock()  # read-modify-write: a discovery run and a brief can both save at once


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def record(idea, rank, run_id, saved_at):
    """A run's idea dict → the small, JSON-safe record that is stored. Keeps what a later /brief needs to write from."""
    trend = idea.get("trend") or {}
    return {
        "id": idea["id"], "rank": rank, "run_id": run_id, "saved_at": _iso(saved_at),
        "title": (idea.get("title") or "")[:200], "summary": (idea.get("summary") or "")[:300], "url": idea.get("url"),
        "score": idea.get("score"), "format": idea.get("format") or "", "hook": idea.get("hook") or "",
        "hook_audit": idea.get("hook_audit"), "reason": (idea.get("reason") or "")[:240],
        "evidence_level": idea.get("evidence_level"), "claim_status": idea.get("claim_status"),
        "needs_verification": bool(idea.get("needs_verification")), "independent_sources": idea.get("independent_sources"),
        "penalties": list(idea.get("penalties") or []), "warnings": list(idea.get("warnings") or []),
        "scored_by": idea.get("scored_by"), "published": _iso(idea.get("published")),
        "trend": {"class": trend.get("class"), "explanation": (trend.get("explanation") or "")[:400],
                  "topic_key": trend.get("topic_key")} if trend else None,
        "evidence": [{"source": e.get("source"), "platform": e.get("platform"), "author": e.get("author"), "url": e.get("url"),
                      "title": (e.get("title") or "")[:140], "published": _iso(e.get("published")), "metrics": e.get("metrics") or {}}
                     for e in (idea.get("evidence") or [])[:MAX_EVIDENCE]],
        "brief": None, "brief_at": None,
    }


def _read():
    """→ (records newest first, where) with where "supabase", "local" or "none"."""
    data = store.load(KV_KEY)
    if isinstance(data, dict) and isinstance(data.get("ideas"), list):
        return [r for r in data["ideas"] if isinstance(r, dict) and r.get("id")], "supabase"
    try:
        with open(LOCAL, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("ideas"), list):
            return [r for r in data["ideas"] if isinstance(r, dict) and r.get("id")], "local"
    except (OSError, ValueError):
        pass
    return [], "none"


def _write(records):
    """→ "supabase", "local" (the explicit fallback) or "none" (could not be saved anywhere)."""
    value = {"ideas": records[:MAX_SAVED], "at": datetime.now(timezone.utc).isoformat()}
    if store.save(KV_KEY, value):
        return "supabase"
    try:
        os.makedirs(os.path.dirname(LOCAL), exist_ok=True)
        tmp = LOCAL + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(tmp, LOCAL)
        return "local"
    except OSError:
        return "none"


def where():
    """Where saved ideas are read from right now: "supabase", "local" or "none"."""
    return _read()[1]


def save_run(ideas, run_id, now=None):
    """Save one run's shown ideas at the front of the list, keeping any brief already written for an idea that comes back.
    → where they went: "supabase", "local" or "none"."""
    now = now or datetime.now(timezone.utc)
    fresh = [record(idea, n, run_id, now) for n, idea in enumerate(ideas, 1)]
    with _lock:
        old, _ = _read()
        briefs = {r["id"]: (r.get("brief"), r.get("brief_at")) for r in old if r.get("brief")}
        for r in fresh:
            r["brief"], r["brief_at"] = briefs.get(r["id"], (None, None))
        ids = {r["id"] for r in fresh}
        return _write(fresh + [r for r in old if r["id"] not in ids])


def list_ideas():
    """→ every saved idea, newest run first."""
    return _read()[0]


def run_ideas(run_id):
    """→ the ideas saved for one run, best rank first."""
    return sorted((r for r in list_ideas() if r.get("run_id") == run_id), key=lambda r: r.get("rank") or 0)


def latest_run_id():
    ideas = list_ideas()
    return ideas[0]["run_id"] if ideas else None


def find(prefix):
    """→ ("ok", record) | ("none", None) | ("ambiguous", [records]) for an id or the start of one (at least 4 characters)."""
    prefix = (prefix or "").strip().lstrip("#").lower()
    if len(prefix) < 4:
        return "none", None
    hits = [r for r in list_ideas() if r["id"].lower().startswith(prefix)]
    if len(hits) == 1:
        return "ok", hits[0]
    return ("ambiguous", hits) if hits else ("none", None)


def attach_brief(idea_id, brief):
    """Store a written brief on its idea. → where it went, or None if the idea is no longer saved."""
    with _lock:
        records, _ = _read()
        for r in records:
            if r["id"] == idea_id:
                r["brief"], r["brief_at"] = brief, datetime.now(timezone.utc).isoformat()
                return _write(records)
    return None


if __name__ == "__main__":
    saved_store = dict(load=store.load, save=store.save)
    mem = {}
    store.save, store.load = (lambda k, v: mem.update({k: v}) or True), (lambda k: mem.get(k))
    try:
        base = {"id": "a1b2c3d4e5f6", "title": "T", "url": "https://x", "evidence": [], "score": 70}
        assert save_run([base], "run1") == "supabase" and find("a1b2")[0] == "ok" and find("zz")[0] == "none"
        attach_brief("a1b2c3d4e5f6", {"hook": "h"})
        save_run([{**base, "score": 80}], "run2")  # the idea comes back: its brief is kept
        assert list_ideas()[0]["brief"] == {"hook": "h"} and len(list_ideas()) == 1
    finally:
        store.save, store.load = saved_store["save"], saved_store["load"]
    print("saved self-check passed")
