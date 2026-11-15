"""The pipeline as a small HTTP service: Telegram webhook plus the API the web app calls.

Every route except `/` needs TRIGGER_SECRET (at least 32 characters). Unset or short = closed, never open.
State of running jobs lives in this one process (agent/discovery/jobs.py), so run ONE worker:
    uvicorn main:app --host 127.0.0.1 --port 8000
"""

import re
import sys

from fastapi import FastAPI, Header, HTTPException

from agent import telegram_discovery
from agent.discovery import jobs, service

# Windows console defaults to cp1252; titles carry unicode → force UTF-8 so prints don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ID = re.compile(r"^[a-f0-9]{8,64}$", re.I)  # idea ids; the same rule the web app applies
KINDS = ("discover", "brief")

app = FastAPI()


@app.on_event("startup")
def _startup():
    if not telegram_discovery.trigger_secret():
        print(f"TRIGGER_SECRET is missing or shorter than {telegram_discovery.MIN_SECRET} characters: every route except / is closed.", flush=True)
    telegram_discovery.ensure_webhook()


def guard(secret):
    if not telegram_discovery.secret_ok(secret):
        raise HTTPException(status_code=403, detail="bad key")


@app.get("/")
def health():
    return {"ok": True}


@app.post("/discover", status_code=202)
def discover(top: int = service.DEFAULT_TOP, x_trigger_secret: str = Header("")):
    guard(x_trigger_secret)
    service.start_discover(top, on_done=lambda job: telegram_discovery.notify_discover(job))  # cron runs report to Telegram
    return jobs.info(jobs.latest("discover"))


@app.post("/brief", status_code=202)
def brief(body: dict, x_trigger_secret: str = Header("")):
    guard(x_trigger_secret)
    idea_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(idea_id, str) or not ID.match(idea_id):
        raise HTTPException(status_code=400, detail="bad id")
    status, idea = service.find_idea(idea_id)
    if status != "ok":
        raise HTTPException(status_code=404, detail="idea is not saved")
    service.start_brief(idea, force=True)
    return jobs.info(jobs.latest("brief"))


@app.get("/jobs")
def job_log(x_trigger_secret: str = Header("")):
    guard(x_trigger_secret)
    return jobs.recent()


@app.get("/jobs/{kind}")
def job_status(kind: str, x_trigger_secret: str = Header("")):
    guard(x_trigger_secret)
    if kind not in KINDS:
        raise HTTPException(status_code=404)
    return jobs.info(jobs.latest(kind))


@app.post("/telegram-webhook")
def telegram_webhook(update: dict, x_telegram_bot_api_secret_token: str = Header("")):
    # Sync on purpose: handlers make blocking HTTP/LLM calls, so FastAPI runs this in its threadpool.
    if not telegram_discovery.webhook_authorized(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="bad secret token")
    telegram_discovery.handle_update(update)  # anything that is not a discovery command is ignored
    return {"ok": True}
