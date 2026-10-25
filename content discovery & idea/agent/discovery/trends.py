"""Persistent trend detection: is a topic new, rising or popular, and why.

One observation is stored per topic per day (Supabase table discovery_topic_obs; a local file is an explicitly
reported fallback, as in seen.py). An observation is what a run saw of that story: when, which independent sources
carried it, and the engagement of each link. Topics are the stories cluster.py already forms; a story seen again
under a reworded title or a new link is matched to its earlier topic (same overlap test as cluster.py).

Classes, each with the evidence behind it:
    new                the first sighting is today and the tracker has run on at least MIN_BASELINE_DAYS earlier
                       days without seeing it. This says "not seen before", never "growing".
    rising             two or more observations, and since a recent reference observation either more independent
                       sources carry it, or the same links gained engagement (see engagement rules).
    popular            seen on at least POPULAR_DAYS days, carried by two or more independent sources on at least
                       POPULAR_MULTI_DAYS of them, and not currently rising. Sustained and corroborated, not loud.
    insufficient_data  everything else, and the reason is spelled out: one snapshot, too few run-days to call
                       anything new, or two sightings with no growth and no sustained corroboration.

What never counts, by construction:
    - Growth from a single snapshot. One observation can be new at most; it can never be rising or popular.
    - Raw engagement. A big number alone changes nothing; only the same link's metric rising between two
      observations at least MIN_HOURS_APART apart counts, by GROWTH_RATIO and MIN_GAIN, and not when one link
      supplies more than SPIKE_SHARE of the gain (a spike in one post is not a topic gaining ground) unless
      independent sources also grew.
    - Affiliated voices. An organisation's feed, its own accounts and reposts of them are one source
      (cluster.origins), so a company repeating itself is not diversity.
    - Metrics that are missing, from different kinds (likes vs views), or on only one of the two days.

It needs days of history: day 1 classifies everything insufficient_data; "new" needs MIN_BASELINE_DAYS earlier run-days;
"rising" and "popular" need real repeat sightings. Nothing here posts, schedules or messages anyone.

    .venv\\Scripts\\python -m agent.discovery.trends          # where history is kept, and the current picture
    .venv\\Scripts\\python -m agent.discovery.trends --test   # live schema check on Supabase (needs docs/supabase.sql)
"""

import hashlib
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from agent import store
from agent.discovery import cluster
from agent.paths import DATA

LOOKBACK_DAYS = 14
RECENT_DAYS = 3  # "rising" compares today with the earliest sighting in this many days, so it fades once growth stops
MIN_BASELINE_DAYS = 3
POPULAR_DAYS = 3
POPULAR_MULTI_DAYS = 2
MIN_HOURS_APART = 12
GROWTH_RATIO = 1.5
SPIKE_SHARE = 0.8
# Smallest absolute gain that counts as growth, per metric. Knobs: set by eye, not calibrated.
MIN_GAIN = {"likes": 200, "views": 20000, "points": 50, "reactions": 50, "stars": 200}
MAX_ITEMS = 8
LOCAL = os.path.join(DATA, "discovery", "topic_obs.json")
CLASSES = ("new", "rising", "popular", "insufficient_data")


# ---- observations ---------------------------------------------------------------------------------------------

def _metrics(entry):
    return {k: v for k, v in (entry.get("metrics") or {}).items()
            if k in MIN_GAIN and isinstance(v, int) and not isinstance(v, bool)}


def observation(group, topic_key, now, run_id):
    """One day's row for a topic from the candidates that map to it (usually one)."""
    items = {}
    for cand in group:
        for e in cand["evidence"]:
            u = cluster.canonical(e["url"])
            have = items.setdefault(u, {})
            for k, v in _metrics(e).items():
                have[k] = max(have.get(k, 0), v)
    return {"topic_key": topic_key, "obs_date": now.date().isoformat(), "observed_at": now.isoformat(), "run_id": run_id,
            "title": group[0]["title"][:200], "origins": sorted(cluster.origins([e for c in group for e in c["evidence"]])),
            "items": [{"u": u, "m": m} for u, m in list(items.items())[:MAX_ITEMS]],
            "claim_status": group[0].get("claim_status"), "n_evidence": sum(len(c["evidence"]) for c in group)}


def assign(candidates, history):
    """→ {candidate id: topic_key}. A candidate joins the earlier topic it shares a link with (most links wins), else the
    one whose title overlaps enough to be the same story; otherwise it starts a topic named by its own canonical URL."""
    known = {}
    for r in sorted(history, key=lambda r: r["obs_date"]):
        k = known.setdefault(r["topic_key"], {"urls": set(), "words": set()})
        k["urls"] |= {i["u"] for i in r.get("items") or []}
        k["words"] = cluster.words(r.get("title"))
    out = {}
    for c in candidates:
        urls = {cluster.canonical(e["url"]) for e in c["evidence"]} | {c["canonical_url"]}
        words = cluster.words(c["title"])
        best, best_score = None, 0.0
        for key, k in known.items():
            shared = len(urls & k["urls"])
            score = 10 + shared if shared else (cluster.overlap(words, k["words"]) if cluster.overlap(words, k["words"]) >= cluster.SAME_STORY else 0)
            if score > best_score:
                best, best_score = key, score
        if best is None:
            best = hashlib.sha1(c["canonical_url"].encode()).hexdigest()[:12]
            known[best] = {"urls": set(urls), "words": words}
        else:
            known[best]["urls"] |= urls
        out[c["id"]] = best
    return out


# ---- classification -------------------------------------------------------------------------------------------

def _hours(a, b):
    def when(r):
        try:
            return datetime.fromisoformat(r["observed_at"])
        except (KeyError, TypeError, ValueError):
            return None
    x, y = when(a), when(b)
    return None if x is None or y is None else (y - x).total_seconds() / 3600


def engagement_change(ref, last):
    """Compare the same links' metrics between two observations → dict, or None when nothing is comparable.
    grew:         ratio >= GROWTH_RATIO and gain >= MIN_GAIN[metric], at least MIN_HOURS_APART apart
    concentrated: grew, but from one link or mostly one link (SPIKE_SHARE)
    counted:      grew and not concentrated; only this feeds a classification."""
    hours = _hours(ref, last)
    before = {i["u"]: i.get("m") or {} for i in ref.get("items") or []}
    after = {i["u"]: i.get("m") or {} for i in last.get("items") or []}
    best = None
    for metric, min_gain in MIN_GAIN.items():
        pairs = [(before[u][metric], after[u][metric]) for u in before
                 if u in after and isinstance(before[u].get(metric), int) and isinstance(after[u].get(metric), int)]
        if not pairs:
            continue
        a, b = sum(p[0] for p in pairs), sum(p[1] for p in pairs)
        gains = [max(0, y - x) for x, y in pairs]
        info = {"metric": metric, "before": a, "after": b, "links": len(pairs), "hours": None if hours is None else round(hours, 1),
                "ratio": round(b / a, 2) if a else None, "gain": b - a,
                "top_share": round(max(gains) / sum(gains), 2) if sum(gains) else 0.0}
        info["grew"] = bool(hours is not None and hours >= MIN_HOURS_APART and a > 0 and b / a >= GROWTH_RATIO and b - a >= min_gain)
        info["concentrated"] = info["grew"] and (len(pairs) == 1 or info["top_share"] > SPIKE_SHARE)
        info["counted"] = info["grew"] and not info["concentrated"]
        if best is None or (info["counted"], info["grew"], info["gain"]) > (best["counted"], best["grew"], best["gain"]):
            best = info
    return best


def _result(cls, facts, why):
    return {"class": cls, "explanation": why, "evidence": facts}


def analyse(series, baseline_days):
    """Classify one topic. `series`: its observations, oldest first, the last being today's. `baseline_days`: days before
    today on which the tracker ran at all. → {"class", "explanation", "evidence"}; pure, no storage."""
    last = series[-1]
    days = [r["obs_date"] for r in series]
    facts = {"days_seen": len(days), "first_seen": days[0], "last_seen": days[-1], "baseline_days": baseline_days,
             "origins_now": sorted(last.get("origins") or [])}
    if len(series) == 1:
        if baseline_days >= MIN_BASELINE_DAYS:
            return _result("new", facts, f"first observed {days[0]}; the tracker ran on {baseline_days} earlier days and this topic was on none of "
                                         f"them. Not seen before is all this says: one snapshot cannot show growth, so engagement is not used.")
        return _result("insufficient_data", facts, f"one snapshot ({days[0]}) and only {baseline_days} earlier run-day(s). Calling a topic new needs "
                                                   f"{MIN_BASELINE_DAYS}; calling it rising or popular needs at least two sightings. Engagement is not used.")
    today = date.fromisoformat(days[-1])
    earlier = series[:-1]
    recent = [r for r in earlier if (today - date.fromisoformat(r["obs_date"])).days <= RECENT_DAYS]
    ref = (recent or earlier[-1:])[0]
    facts["reference_date"] = ref["obs_date"]
    o_ref, o_now = set(ref.get("origins") or []), set(last.get("origins") or [])
    diversity_up = len(o_now) > len(o_ref)
    facts.update(origins_reference=sorted(o_ref), new_origins=sorted(o_now - o_ref))
    eng = engagement_change(ref, last)
    facts["engagement"] = eng
    multi_days = sum(1 for r in series if len(r.get("origins") or []) >= 2)
    facts["days_with_two_or_more_sources"] = multi_days

    parts = [f"seen on {len(days)} days ({days[0]} to {days[-1]})"]
    parts.append(f"independent sources {len(o_ref)} -> {len(o_now)} since {ref['obs_date']}"
                 + (f" (new: {', '.join(sorted(o_now - o_ref))})" if diversity_up else ""))
    if eng is None:
        parts.append("no engagement metric is on both days for the same link, so engagement is not used")
    else:
        line = f"{eng['metric']} on {eng['links']} shared link(s) {eng['before']} -> {eng['after']} over {eng['hours']}h"
        if eng["counted"]:
            line += f" (x{eng['ratio']}, spread across links: counted)"
        elif eng["concentrated"]:
            line += (" - a spike in one link" if eng["links"] == 1 else f" - {int(eng['top_share'] * 100)}% of the gain is one link")
            line += "; not counted as the topic growing"
        elif eng["after"] < eng["before"]:
            line += " (fell)"
        else:
            line += f" (below the bar: needs x{GROWTH_RATIO}, +{MIN_GAIN[eng['metric']]} and {MIN_HOURS_APART}h apart)"
        parts.append(line)
    eng_grew = bool(eng and eng["counted"])  # a spike concentrated in one link is not in here
    sustained = len(days) >= POPULAR_DAYS and multi_days >= POPULAR_MULTI_DAYS
    if diversity_up or eng_grew:
        why = ("independent sources and engagement both grew" if diversity_up and eng_grew else
               "independent sources grew" if diversity_up else "engagement grew across several links")
        return _result("rising", facts, f"{why}: " + "; ".join(parts))
    if sustained:
        return _result("popular", facts, f"sustained and corroborated, not growing right now: " + "; ".join(parts)
                       + f"; carried by 2+ independent sources on {multi_days} of those days")
    need = []
    if len(days) < POPULAR_DAYS:
        need.append(f"{POPULAR_DAYS - len(days)} more day(s) to be popular")
    if multi_days < POPULAR_MULTI_DAYS:
        need.append(f"2+ independent sources on {POPULAR_MULTI_DAYS} days (has {multi_days})")
    return _result("insufficient_data", facts, "no growth signal and not yet sustained across sources: " + "; ".join(parts)
                   + "; would need " + " and ".join(need))


# ---- storage --------------------------------------------------------------------------------------------------

def _read_local():
    try:
        with open(LOCAL, encoding="utf-8") as f:
            rows = json.load(f)["rows"]
        if not isinstance(rows, list):
            raise ValueError
        return [r for r in rows if isinstance(r, dict) and r.get("topic_key") and r.get("obs_date")], "ok"
    except FileNotFoundError:
        return [], "missing"
    except (OSError, ValueError, KeyError, TypeError):
        return [], "corrupt"


def _write_local(new_rows, now):
    rows, _ = _read_local()
    have = {(r["topic_key"], r["obs_date"]) for r in rows}
    keep = (now.date() - timedelta(days=LOOKBACK_DAYS * 3)).isoformat()
    rows = [r for r in rows if r["obs_date"] >= keep] + [r for r in new_rows if (r["topic_key"], r["obs_date"]) not in have]
    try:
        os.makedirs(os.path.dirname(LOCAL), exist_ok=True)
        tmp = LOCAL + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rows": rows}, f, ensure_ascii=False)
        os.replace(tmp, LOCAL)
        return True
    except OSError:
        return False


def load(now):
    """→ (observations of the last LOOKBACK_DAYS days, state). Supabase when usable, else the local fallback file."""
    since = (now.date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    local, local_status = _read_local()
    local = [r for r in local if r["obs_date"] >= since]
    remote, detail = store.probe("topic_obs")
    state = {"backend": "local_fallback", "remote": remote, "remote_detail": detail, "local": local_status, "synced": 0,
             "sync_failed": 0}
    if remote == "ok":
        rows = store.topic_obs_rows(since)
        if rows is None:
            state["remote"], state["remote_detail"] = "error", "reading discovery_topic_obs failed"
        else:
            state["backend"] = "supabase"
            have = {(r["topic_key"], r["obs_date"]) for r in rows}
            missing = [r for r in local if (r["topic_key"], r["obs_date"]) not in have]
            if missing:
                if store.add_topic_obs(missing):
                    state["synced"] = len(missing)
                else:
                    state["sync_failed"] = len(missing)
                rows = rows + missing
            return rows, state
    return local, state


def save(new_rows, now, state):
    """Store today's new observations in the ledger in use → state plus remote_wrote / local_wrote (None = not attempted)."""
    remote_wrote = local_wrote = None
    if state["backend"] == "supabase" and new_rows:
        remote_wrote = store.add_topic_obs(new_rows)
        if not remote_wrote:
            state = {**state, "backend": "local_fallback", "remote": "error", "remote_detail": "writing discovery_topic_obs failed"}
    if state["backend"] == "local_fallback" and new_rows:
        local_wrote = _write_local(new_rows, now)
    return {**state, "remote_wrote": remote_wrote, "local_wrote": local_wrote}


def describe(state):
    if state["backend"] == "supabase":
        head = f"Trends: SUPABASE (shared history), {state.get('observations', 0)} observations over {state.get('run_days', 0)} run-days."
        if state.get("synced"):
            head += f" Copied {state['synced']} observations from the local fallback file."
        if state.get("sync_failed"):
            head += f" WARNING: {state['sync_failed']} local observations could not be copied to Supabase."
    else:
        why = {"off": "Supabase is not configured.",
               "tables_missing": "table discovery_topic_obs is missing: run docs/supabase.sql in the Supabase SQL Editor.",
               "error": f"Supabase is not usable ({state['remote_detail']}).", "ok": "Supabase is not usable."}[state["remote"]]
        head = (f"Trends: LOCAL FALLBACK, {state.get('observations', 0)} observations over {state.get('run_days', 0)} run-days. {why} "
                f"History is only on this machine.")
    if state.get("remote_wrote") is False:
        head += " WARNING: the Supabase write failed; today's observations went to the local fallback file."
    if state.get("local_wrote") is False:
        head += " WARNING: today's observations could not be saved anywhere; tomorrow will have no history of today."
    return head


# ---- the one call the pipeline makes --------------------------------------------------------------------------

def update(candidates, now):
    """Record today's observations and classify every candidate. → ({candidate id: {"topic_key", "class", "explanation",
    "evidence"}}, state). Idempotent: a topic already observed today keeps its first row, and nothing is written twice."""
    history, state = load(now)
    today = now.date().isoformat()
    run_id = now.isoformat()
    keys = assign(candidates, history)
    groups = {}
    for c in candidates:
        groups.setdefault(keys[c["id"]], []).append(c)
    stored_today = {r["topic_key"]: r for r in history if r["obs_date"] == today}
    series_by_key = {}
    for r in history:
        if r["obs_date"] < today:
            series_by_key.setdefault(r["topic_key"], []).append(r)
    new_rows, rows_today = [], {}
    for key, group in groups.items():
        row = stored_today.get(key)
        if row is None:
            row = observation(group, key, now, run_id)
            new_rows.append(row)
        rows_today[key] = row
    baseline_days = len({r["obs_date"] for r in history if r["obs_date"] < today})
    results, per_topic = {}, {}
    for key, group in groups.items():
        series = sorted(series_by_key.get(key, []), key=lambda r: r["obs_date"]) + [rows_today[key]]
        per_topic[key] = analyse(series, baseline_days)
        for c in group:
            results[c["id"]] = {"topic_key": key, **per_topic[key]}
    state = save(new_rows, now, state)
    state.update(observations=len(history) + len(new_rows), run_days=baseline_days + 1, new_observations=len(new_rows),
                 already_recorded_today=len(groups) - len(new_rows), topics=len(groups),
                 counts={cls: sum(1 for v in per_topic.values() if v["class"] == cls) for cls in CLASSES})
    state["message"] = describe(state)
    return results, state


# ---- live schema check and a look at the stored picture -------------------------------------------------------

def roundtrip():
    """Live check of the real table: write a full-shaped row, write it again (must be ignored), read it back with the right
    types, delete it, confirm it is gone. → [(step, ok, detail)]. The row is topic_key "selftest-<random>"."""
    key = f"selftest-{uuid.uuid4().hex[:12]}"
    day = datetime.now(timezone.utc).date().isoformat()
    steps = []

    def step(name, ok, detail=""):
        steps.append((name, bool(ok), detail))
        return bool(ok)
    state, detail = store.probe("topic_obs")
    if not step("table discovery_topic_obs reachable", state == "ok", f"{state}: {detail}" if detail else state):
        return steps
    row = {"topic_key": key, "obs_date": day, "observed_at": datetime.now(timezone.utc).isoformat(), "run_id": "selftest",
           "title": "persistence self-test", "origins": ["x", "youtube"], "items": [{"u": "https://selftest.invalid/a", "m": {"likes": 5}}],
           "claim_status": "unverified", "n_evidence": 2}
    if not step("write a full row", store.add_topic_obs([row])):
        return steps
    step("write the same topic and day again (must be ignored)", store.add_topic_obs([{**row, "title": "changed", "n_evidence": 9}]))
    got = [r for r in store.topic_obs_rows(day) or [] if r.get("topic_key") == key]
    step("exactly one row exists (primary key holds)", len(got) == 1, f"{len(got)} rows")
    r = got[0] if got else {}
    step("first write kept, second ignored", r.get("title") == "persistence self-test" and r.get("n_evidence") == 2, r.get("title", ""))
    step("types survive (jsonb lists, date string)", isinstance(r.get("origins"), list) and r.get("origins") == ["x", "youtube"]
         and isinstance(r.get("items"), list) and r["items"][0]["m"] == {"likes": 5} and r.get("obs_date") == day)
    step("delete it", store.delete_topic_obs(key))
    after = store.topic_obs_rows(day)
    step("confirm it is gone", after is not None and all(x.get("topic_key") != key for x in after))
    return steps


def overview(now=None):
    """The stored picture as data, for display (the Telegram /trending, the CLI). Reads history; changes nothing.
    → {"state", "as_of", "run_days", "topics", "counts", "items"}. items are the topics seen on the latest run day that are new,
    rising or popular (rising first), each with its class, explanation, evidence facts, up to 3 links and days seen. A topic that is
    insufficient_data is counted but not listed: there is nothing to show about it yet."""
    now = now or datetime.now(timezone.utc)
    rows, state = load(now)
    days = sorted({r["obs_date"] for r in rows})
    state = {**state, "observations": len(rows), "run_days": len(days)}
    counts = dict.fromkeys(CLASSES, 0)
    out = {"state": state, "as_of": days[-1] if days else None, "run_days": len(days), "topics": 0, "counts": counts, "items": []}
    if not rows:
        return out
    last_day, baseline = days[-1], len(days) - 1
    by_key = {}
    for r in rows:
        by_key.setdefault(r["topic_key"], []).append(r)
    for key, series in by_key.items():
        series.sort(key=lambda r: r["obs_date"])
        if series[-1]["obs_date"] != last_day:
            continue
        verdict = analyse(series, baseline)
        counts[verdict["class"]] += 1
        out["topics"] += 1
        if verdict["class"] != "insufficient_data":
            links = [i["u"] for i in series[-1].get("items") or [] if str(i.get("u", "")).startswith(("http://", "https://"))][:3]
            out["items"].append({"topic_key": key, "title": series[-1].get("title") or "", **verdict, "links": links,
                                 "days_seen": len(series)})
    order = {"rising": 0, "popular": 1, "new": 2}
    out["items"].sort(key=lambda i: (order[i["class"]], -i["days_seen"], i["title"]))
    return out


def _cli():
    if "--test" in sys.argv:
        steps = roundtrip()
        for name, ok, detail in steps:
            print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        passed = len(steps) == 8 and all(ok for _, ok, _ in steps)
        print("\nSupabase trend schema: " + ("PASS" if passed else "FAIL / not verified"))
        if steps and not steps[0][1]:
            print("Create the table first: paste docs/supabase.sql into the Supabase SQL Editor and Run (safe to run twice).")
        sys.exit(0 if passed else 1)
    pic = overview()
    print(describe(pic["state"]))
    if not pic["run_days"]:
        print("No history yet. Trends need a few days of discovery runs; day one classifies everything insufficient_data.")
        return
    print(f"As of {pic['as_of']}: " + ", ".join(f"{k} {v}" for k, v in pic["counts"].items()))
    for item in pic["items"][:15]:
        print(f"- {item['class'].upper():<9} {item['title'][:70]}\n    {item['explanation']}")


if __name__ == "__main__":
    _cli()
