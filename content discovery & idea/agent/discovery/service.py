"""What the chat commands do, as plain Python: no Telegram, no message text, no formatting. An adapter (Telegram today) calls
these and decides how to show the answers; discovery, ranking and trend logic stay in their own modules and know nothing of it.

    start_discover()   run the whole pipeline in the background (3-4 min) and save the ideas it shows
    last_run()         the newest run's ideas, for paging through
    saved_ideas()      every saved idea, newest first
    trending()         the stored trend picture (trends.overview)
    start_brief()      write (or fetch) the brief for one saved idea, in the background (up to a minute)
"""

import os
from datetime import datetime, timezone

from agent.discovery import jobs, run, saved, scripts, trends

DEFAULT_TOP, MAX_TOP = 10, 15
PAGE_SIZE = 3  # ideas per page; small enough that three always fit one Telegram message
TREND_PAGE_SIZE = 4


def confidence(idea):
    """→ (level, why). How well the story is backed by what was found, NOT how good a video it would make. "high" for a
    first-party post means the organisation really said it; what it says about its own product is still its claim."""
    if idea.get("needs_verification"):
        return "low", "unverified claim about an announcement"
    if idea.get("claim_status") == "first_party":
        return "high", "the organisation's own post"
    n = idea.get("independent_sources") or 0
    if idea.get("claim_status") == "corroborated" or n >= 2:
        return "high", f"{n} independent sources"
    if idea.get("evidence_level") == "engagement":
        return "medium", "one source with measured engagement"
    return "low", "one source, no measured traction"


def paginate(items, page, size=PAGE_SIZE):
    """→ (this page's items, page number, number of pages, total). A page number out of range is brought back into range."""
    pages = max(1, -(-len(items) // size))
    page = min(max(1, int(page)), pages)
    return items[(page - 1) * size: page * size], page, pages, len(items)


# ---- discovery run -------------------------------------------------------------------------------------------

def _discover(top):
    now = datetime.now(timezone.utc)
    result = run.run(top=top, briefs=0, now=now)  # briefs are written on request, not for every idea
    where = saved.save_run(result["ideas"], result["generated_at"], now)
    return {"run_id": result["generated_at"], "ideas": [i["id"] for i in result["ideas"]], "saved_where": where,
            "complete": result["complete"], "failed_sources": result["failed_sources"], "items": result["items"],
            "candidates": result["candidates"], "after_seen_filter": result["after_seen_filter"],
            "already_seen": result["already_seen"], "seen_message": result["seen_filter"]["message"],
            "trend_message": result["trends"]["message"], "trend_counts": result["trends"].get("counts") or {},
            "note": result["note"]}


def start_discover(top=DEFAULT_TOP, on_done=None):
    """→ (job, started). started is False if a run is already going; the caller reports that job instead."""
    top = max(1, min(MAX_TOP, int(top)))
    return jobs.start("discover", "discover", f"discovery run (top {top})", lambda: _discover(top), on_done)


def last_run():
    """→ (summary, ideas): the newest finished run in this process (summary), else the newest saved run (summary None).
    The ideas always come from the saved copy, so they survive a restart."""
    job = jobs.latest("discover", "done")
    run_id = job.result["run_id"] if job and job.result else saved.latest_run_id()
    return (job.result if job else None), (saved.run_ideas(run_id) if run_id else [])


def saved_ideas():
    return saved.list_ideas()


def trending():
    return trends.overview()


# ---- briefs --------------------------------------------------------------------------------------------------

def can_write_briefs():
    return bool(os.getenv("GROQ_API_KEY"))


def find_idea(prefix):
    return saved.find(prefix)


def _write_brief(idea_id):
    status, idea = saved.find(idea_id)
    if status != "ok":
        raise LookupError("idea is no longer saved")
    brief = scripts.build_script(idea, os.getenv("GROQ_API_KEY"))
    if brief is None:
        raise RuntimeError("no model key")
    saved.attach_brief(idea["id"], brief)  # a rejected brief is saved too, so asking again does not spend the model again
    return brief


def start_brief(idea, force=False, on_done=None):
    """→ (job_or_None, brief_or_None, started). A saved brief is returned at once (brief set, no job) unless force."""
    if idea.get("brief") and not force:
        return None, idea["brief"], False
    job, started = jobs.start("brief", f"brief:{idea['id']}", f"brief for {idea['title'][:60]}",
                              lambda: _write_brief(idea["id"]), on_done)
    return job, None, started


if __name__ == "__main__":
    assert confidence({"needs_verification": True, "claim_status": "first_party"})[0] == "low", "a flagged claim is never high"
    assert confidence({"claim_status": "first_party"})[0] == "high"
    assert confidence({"claim_status": "unverified", "independent_sources": 1, "evidence_level": "engagement"})[0] == "medium"
    assert confidence({"claim_status": "unverified", "independent_sources": 1})[0] == "low"
    assert paginate(list(range(7)), 3) == ([6], 3, 3, 7) and paginate([], 5) == ([], 1, 1, 0) and paginate(list(range(7)), 0)[1] == 1
    print("service self-check passed")
