"""Remember which ideas a run already showed, so tomorrow's run does not show them again.

The ledger is the Supabase table discovery_seen (kind "idea"): shared across machines and durable. The local
file data/discovery/seen_ideas.json is a FALLBACK, used only when Supabase cannot be used, and every run says
which of the two it used and why. The two are not treated as equivalent:
    Supabase usable   read and write Supabase only. Ideas found in the local file but not in Supabase (shown
                      during an earlier fallback run) are copied up once, so nothing already shown comes back.
    Supabase unusable read and write the local file, and say LOCAL FALLBACK with the reason and the fix.
A Supabase write that fails mid-run falls back to the local file for that run and says so.

An idea counts as seen when its canonical URL (or any evidence URL) was shown, or when its title is the same
story as a shown title (same title-word overlap test as cluster.py). The second rule catches a story that
comes back tomorrow under a different link.

    .venv\\Scripts\\python -m agent.discovery.seen          # which ledger is in use, and what to do if not Supabase
    .venv\\Scripts\\python -m agent.discovery.seen --test   # live write/read/delete round trip against Supabase
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from agent import store
from agent.discovery import cluster
from agent.paths import DATA

KIND = "idea"
LEDGER = os.path.join(DATA, "discovery", "seen_ideas.json")
KEEP_DAYS = 60  # older fallback entries are dropped on write; the filter only looks back SEEN_DAYS (run.py)


def _read_ledger():
    """→ ({url: {"title", "at"}}, "ok" | "missing" | "corrupt")."""
    try:
        with open(LEDGER, encoding="utf-8") as f:
            ideas = json.load(f)["ideas"]
        if not isinstance(ideas, dict):
            raise ValueError
        return {u: v for u, v in ideas.items() if isinstance(v, dict)}, "ok"
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, ValueError, KeyError, TypeError):  # unreadable or not our format: start over, say so
        return {}, "corrupt"


def _when(value):
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)  # a hand-edited, zone-less time must still compare


def _write_ledger(pairs, now):
    ideas, _ = _read_ledger()
    keep = now - timedelta(days=KEEP_DAYS)
    ideas = {u: v for u, v in ideas.items() if (_when(v.get("at")) or now) >= keep}
    for url, title in pairs:
        ideas.setdefault(url, {"title": (title or "")[:300], "at": now.isoformat()})
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        tmp = LEDGER + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ideas": ideas}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, LEDGER)  # whole file or nothing: a crash cannot leave half a ledger
        return True
    except OSError:
        return False


def load(days, now):
    """→ ([(url, title)] shown in the last `days` days, state dict). state["backend"] is "supabase" or "local_fallback"."""
    ideas, local = _read_ledger()
    cutoff = now - timedelta(days=days)
    local_rows = [(u, v.get("title") or "") for u, v in ideas.items() if (_when(v.get("at")) or now) >= cutoff]
    remote, detail = store.probe()
    state = {"backend": "local_fallback", "remote": remote, "remote_detail": detail, "local": local,
             "synced": 0, "sync_failed": 0}
    if remote == "ok":
        rows = store.seen_rows(KIND, days)
        if rows is None:  # the probe passed but the real read did not: do not pretend the ledger is empty
            state["remote"], state["remote_detail"] = "error", "reading discovery_seen failed"
        else:
            state["backend"] = "supabase"
            have = {u for u, _ in rows}
            missing = [(u, t) for u, t in local_rows if u not in have]
            if missing:
                if store.mark_seen(KIND, missing):
                    state["synced"] = len(missing)
                else:
                    state["sync_failed"] = len(missing)
                rows = rows + missing  # shown ideas are excluded either way
            state["known"] = len(rows)
            return rows, state
    state["known"] = len(local_rows)
    return local_rows, state


def mark(pairs, now, state):
    """Record shown ideas in the ledger in use. → `state` plus remote_wrote and local_wrote (None = not attempted)."""
    pairs = [(u, t) for u, t in pairs if isinstance(u, str) and u]
    remote_wrote = local_wrote = None
    if state["backend"] == "supabase" and pairs:
        remote_wrote = bool(store.mark_seen(KIND, pairs))
        if not remote_wrote:
            state = {**state, "backend": "local_fallback", "remote": "error", "remote_detail": "writing discovery_seen failed"}
    if state["backend"] == "local_fallback":
        local_wrote = _write_ledger(pairs, now)
    return {**state, "remote_wrote": remote_wrote, "local_wrote": local_wrote}


def filter(candidates, rows):
    """→ (candidates not shown before, {"same_url": n, "same_story": n} dropped)."""
    urls = {cluster.canonical(u) for u, _ in rows}
    titles = [cluster.words(t) for _, t in rows if t]
    fresh, dropped = [], {"same_url": 0, "same_story": 0}
    for c in candidates:
        if c["canonical_url"] in urls or any(cluster.canonical(e["url"]) in urls for e in c["evidence"]):
            dropped["same_url"] += 1
        elif any(cluster.overlap(cluster.words(c["title"]), t) >= cluster.SAME_STORY for t in titles):
            dropped["same_story"] += 1
        else:
            fresh.append(c)
    return fresh, dropped


def describe(state):
    """One honest line (plus the fix when it is not Supabase) for the run report."""
    if state["backend"] == "supabase":
        head = f"Seen-filter: SUPABASE (shared ledger), {state['known']} ideas remembered."
        if state.get("synced"):
            head += f" Copied {state['synced']} ideas from the local fallback file into Supabase."
        if state.get("sync_failed"):
            head += f" WARNING: {state['sync_failed']} local ideas could not be copied to Supabase; they are excluded this run only."
    else:
        why = {"off": "Supabase is not configured: set SUPABASE_URL and SUPABASE_SECRET_KEY in .env.",
               "tables_missing": "Supabase tables are missing: run docs/supabase.sql in the Supabase SQL Editor.",
               "error": f"Supabase is not usable ({state['remote_detail']}).",
               "ok": "Supabase is not usable."}[state["remote"]]
        head = (f"Seen-filter: LOCAL FALLBACK, {state.get('known', 0)} ideas remembered. {why} "
                f"The local file is only on this machine; ideas shown elsewhere will repeat.")
    if state["local"] == "corrupt" and state["backend"] == "local_fallback":
        head += " The local file was unreadable and was reset."
    if state.get("remote_wrote") is False:
        head += " WARNING: the Supabase write failed; this run was saved to the local fallback file only."
    if state.get("local_wrote") is False:
        head += " WARNING: the local fallback file could not be written; shown ideas WILL repeat next run."
    return head


def _cli():
    if "--test" in sys.argv:
        steps = store.roundtrip()
        for name, ok, detail in steps:
            print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        passed = len(steps) == 5 and all(ok for _, ok, _ in steps)
        print("\nSupabase persistence: " + ("PASS" if passed else "FAIL (not verified)"))
        sys.exit(0 if passed else 1)
    state, detail = store.probe()
    print(f"Supabase: {state}" + (f" ({detail})" if detail else ""))
    _, local = _read_ledger()
    print(f"Local fallback file: {local} ({LEDGER})")
    print(describe({"backend": "supabase" if state == "ok" else "local_fallback", "remote": state, "remote_detail": detail,
                    "local": local, "known": 0}))
    if state != "ok":
        print("\nTo make the ledger shared and durable:\n"
              "  1. Supabase dashboard -> SQL Editor -> paste docs/supabase.sql -> Run (safe to run twice)\n"
              "  2. .env must have SUPABASE_URL and SUPABASE_SECRET_KEY (the secret/service key, never the anon key)\n"
              "  3. run this command again; it should say 'Supabase: ok'")
    print("\nThis only shows the tables answer. To prove writes persist, run it with --test.")


if __name__ == "__main__":
    _cli()
