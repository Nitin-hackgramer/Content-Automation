import hmac
import os
import sys
import threading

from fastapi import FastAPI, Header, HTTPException

from agent import news, telegram_discovery
from agent.graph import graph
from agent.nodes import approval
from agent.nodes.generate import generate_node

# Windows console defaults to cp1252; tweets/titles carry unicode → force UTF-8 so prints don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

INITIAL_STATE = {
    "target": "both",
    "research_data": [],
    "selected_item": None,
    "angle": None,
    "selected_draft": None,
    "linkedin_draft": None,
    "selected_image": None,
    "approval_status": "pending",
    "rejection_feedback": None,
    "retry_count": 0,
}


def run_agent(target="both", item=None, angle=None):
    state = dict(INITIAL_STATE)
    state["target"] = target
    if item:
        # /repurpose: the item is already chosen, so skip research+select and go straight
        # to drafting. ponytail: outside the graph, so a rejection here ends the run
        # instead of re-researching. Route it through the graph if that's ever wanted.
        state.update(research_data=[item], selected_item=item, angle=angle)
        final_state = approval.approval_node(generate_node(state))
    else:
        final_state = graph.compile().invoke(state)
    print(f"\nDone. status={final_state['approval_status']}")
    return final_state


def _start_run(target="both", item=None, angle=None):
    # graph.invoke blocks up to ~2h on Telegram approval → run detached so the
    # triggering HTTP request (cron hit or webhook) returns immediately.
    threading.Thread(target=run_agent, args=(target, item, angle), daemon=True).start()


# ASGI app so Render binds $PORT. schtasks is Windows-only and dead on Render;
# schedule by hitting /run from a cron (Hostinger/cron-job.org), one per target/time.
app = FastAPI()


@app.on_event("startup")
def _startup():
    # register the Telegram webhook so /help, /latest, /generate, /x, /li work
    # anytime, not just while a draft is pending. No-op if no bot token is set.
    approval.ensure_webhook()


@app.get("/")
def health():
    return {"ok": True}


@app.api_route("/run", methods=["GET", "POST"])
def trigger(target: str = "both"):
    if target not in ("x", "li", "both"):
        target = "both"
    _start_run(target)
    return {"started": True, "target": target}


@app.api_route("/news", methods=["GET", "POST"])
def news_trigger(key: str = "", x_trigger_secret: str = Header("")):
    # Unlike /run this is guarded: an open URL would let anyone spam the chat and burn
    # the Groq quota. No TRIGGER_SECRET configured = closed, not open.
    # Prefer the X-Trigger-Secret header: a ?key= in the URL ends up in access logs.
    # ponytail: no once-a-day guard, a double cron fire sends two digests.
    secret = os.getenv("TRIGGER_SECRET") or ""
    given = x_trigger_secret or key
    if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(status_code=403, detail="bad key")
    threading.Thread(target=news.run, daemon=True).start()  # fetch+filter+send takes ~40s; don't hold the cron
    return {"started": True}


@app.post("/telegram-webhook")
def telegram_webhook(update: dict, x_telegram_bot_api_secret_token: str = Header("")):
    # only reachable when no run is active (approval.py deletes the webhook while polling).
    # Sync on purpose: handlers make blocking HTTP/LLM calls, so FastAPI runs this in its threadpool.
    if not approval.webhook_authorized(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="bad secret token")
    # /discover /trending /ideas /brief and their buttons go to the discovery front end; everything else is unchanged
    if not telegram_discovery.handle_update(update):
        approval.handle_idle_update(update, _start_run)
    return {"ok": True}


if __name__ == "__main__":
    run_agent()
