"""Run the discovery workflow: fetch -> cluster -> rank -> (optional) briefs -> save.

    .venv\\Scripts\\python -m agent.discovery.run                 # top 10 ideas, saved to data/discovery/
    .venv\\Scripts\\python -m agent.discovery.run --top 5 --briefs 3

This file only wires the four stages together. Each stage is a module of its own, so one can be
improved without touching the others: sources.py (fetch), cluster.py (dedup), rank.py (score),
scripts.py (briefs). Nothing here posts, downloads assets or sends messages.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from agent.discovery import cluster, rank, scripts, seen, sources, trends, verify
from agent.paths import DATA

OUT_DIR = os.path.join(DATA, "discovery")
SEEN_DAYS = 14  # an idea shown in the last two weeks is not shown again (see seen.py for where that is remembered)


def run(top=10, briefs=0, now=None):
    now = now or datetime.now(timezone.utc)
    api_key = os.getenv("GROQ_API_KEY")
    items, report = sources.fetch_all(now)
    candidates = cluster.cluster(items)
    try:  # trend history is a bonus: whatever goes wrong with it must not stop discovery
        trend_info, trend_state = trends.update(candidates, now)
    except Exception as e:
        trend_info, trend_state = {}, {"message": f"Trends: FAILED ({type(e).__name__}); no trend data this run.", "counts": {}}
    known, seen_state = seen.load(SEEN_DAYS, now)
    fresh, repeats = seen.filter(candidates, known)
    ranked, note = rank.rank(fresh, now, api_key)
    shown = ranked[:top]
    for idea in shown:
        idea["trend"] = trend_info.get(idea["id"])  # classification and the evidence behind it; ranking is not changed by it
    if api_key:
        verify.audit_hooks(shown, api_key)  # a hook is a claim too: unsupported ones are dropped
    brief_out = {}
    for idea in shown[:briefs]:
        try:
            brief_out[idea["id"]] = scripts.build_script(idea, api_key)  # None when there is no key
        except Exception as e:  # one failed brief must not lose the others
            brief_out[idea["id"]] = {"error": type(e).__name__}
    seen_state = seen.mark([(c["canonical_url"], c["title"]) for c in shown], now, seen_state)
    failed = {k: v for k, v in report.items() if v.startswith("error")}
    result = {"generated_at": now.isoformat(), "sources": report, "complete": not failed, "failed_sources": failed,
              "items": len(items),
              "candidates": len(candidates), "already_seen": repeats, "after_seen_filter": len(fresh),
              "seen_filter": {**seen_state, "message": seen.describe(seen_state)}, "trends": trend_state, "note": note,
              "ideas": shown, "briefs": brief_out}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, now.strftime("%Y-%m-%d_%H%M%S") + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    result["path"] = path
    return result


def _print(result):
    if not result["complete"]:
        bad, groups = result["failed_sources"], {}
        for name, status in bad.items():  # one entry per channel and reason code, not one per query
            head, _, detail = status[6:].partition(" (")
            groups.setdefault((name.split(":")[0], head), []).append(detail.rstrip(")"))
        parts = []
        for (channel, head), details in groups.items():
            unsent = sum("not sent" in d for d in details)
            shown = next((d for d in details if d and "not sent" not in d), next((d for d in details if d), ""))
            note = "; ".join(x for x in (shown, f"{unsent} of {len(details)} not sent" if unsent else "") if x)
            parts.append(f"{channel} x{len(details)}: {head}" + (f" ({note})" if note else ""))
        print(f"PARTIAL RESULTS: {len(bad)} of {len(result['sources'])} source queries did not deliver, so some stories may be missing: "
              + "; ".join(parts))
    print("Sources: " + ", ".join(f"{k} {v}" for k, v in result["sources"].items()))
    rep = result["already_seen"]
    print(f"{result['items']} items -> {result['candidates']} stories -> {result['after_seen_filter']} not shown recently "
          f"({rep['same_url']} same link, {rep['same_story']} same story already shown)")
    print(result["seen_filter"]["message"])
    tr = result["trends"]
    print(tr["message"])
    if tr.get("counts"):
        print("Trend classes today: " + ", ".join(f"{k} {v}" for k, v in tr["counts"].items())
              + f" ({tr.get('new_observations', 0)} new observations stored, {tr.get('already_recorded_today', 0)} already recorded today)")
    if result["note"]:
        print(f"Note: {result['note']}")
    for n, idea in enumerate(result["ideas"], 1):
        print(f"\n{n}. [{idea['score']:5.1f}] {idea['title']}  ({idea['format'] or 'unscored'})")
        print(f"   evidence: {idea['evidence_level']} across {', '.join(idea['sources'])}"
              f" | claim: {idea['claim_status']}{' (NEEDS VERIFICATION)' if idea['needs_verification'] else ''}")
        if idea.get("trend"):
            print(f"   trend: {idea['trend']['class'].upper()} - {idea['trend']['explanation'][:230]}")
        if idea["penalties"]:
            print(f"   capped by: {', '.join(idea['penalties'])}")
        for w in idea["warnings"]:
            print(f"   warning: {w}")
        for e in idea["evidence"][:3]:
            print(f"   - {e['source']}: {e['url']}")
        if idea["hook"]:
            print(f"   hook: {idea['hook']}")
    for idea_id, brief in result["briefs"].items():
        if brief is None:
            print(f"\nBrief {idea_id}: skipped (no GROQ_API_KEY)")
        elif "error" in brief:
            print(f"\nBrief {idea_id}: failed: {brief['error']}")
        elif "rejected" in brief:
            print(f"\nBrief {idea_id}: REJECTED, {brief['rejected']}")
        else:
            audit = brief["audit"]
            print(f"\nBrief {idea_id} [{audit['status'].upper()}, {len(audit['removed'])} of {audit['statements']} statements removed]: "
                  f"{brief['hook']}")
            for r in audit["removed"]:
                print(f"   removed ({r['where']}): {r['text'][:90]} -- {r['reason']}")
            for w in brief["warnings"]:
                print(f"   warning: {w}")
    print(f"\nSaved: {result['path']}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Content discovery: fetch, cluster, rank, save.")
    parser.add_argument("--top", type=int, default=10, help="how many ideas to show (default 10)")
    parser.add_argument("--briefs", type=int, default=0, help="write short-video briefs for the top N (needs GROQ_API_KEY)")
    args = parser.parse_args()
    _print(run(args.top, args.briefs))
