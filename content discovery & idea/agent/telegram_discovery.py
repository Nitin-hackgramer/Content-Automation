"""Telegram front end for content discovery: /discover, /trending, /ideas, /brief <id>.

This file is the only place that knows about Telegram. It turns a command into a call on agent/discovery/service.py and the
answer into messages; discovery, ranking and trend logic never import it. It reuses the project's existing Telegram plumbing:
the same bot (TELEGRAM_BOT) and owner chat (TELEGRAM_CHAT_ID), the owner check in agent/nodes/approval.py, the webhook route and
secret in main.py, and the HTML helpers in agent/digest.py.

    /discover [n]    run the pipeline in the background (3-4 min); the top n ideas (default 10) arrive when it finishes
    /trending        what is new, rising or popular, with the evidence for each
    /ideas           recently saved ideas, newest run first
    /brief <id> [new]  the brief for an idea: the saved one, or one written now (about a minute); "new" rewrites it

Results come in pages of three with Prev/Next buttons (the message is edited in place) and a Brief button per idea. Every idea
shows its source links, confidence, trend and any verification warnings. Long work never blocks the bot: the command answers at
once and the result is sent when the job finishes.

Secrets: the token is read from the environment when needed, kept inside Api, and never printed. A failed call logs the method
name and a status only, never the URL (which contains the token) or an exception message.

Local use (no public URL needed):
    .venv\\Scripts\\python -m agent.telegram_discovery --chat-id    # message your bot first; prints your chat id
    .venv\\Scripts\\python -m agent.telegram_discovery             # poll for commands until Ctrl+C
"""

import html
import os
import re
import sys
import threading
import time
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

from agent import digest
from agent.discovery import jobs, service
from agent.nodes import approval
from agent.paths import ENV_FILE

load_dotenv(ENV_FILE, override=True)

COMMANDS = ("discover", "trending", "ideas", "brief")
LIMIT = digest.LIMIT  # Telegram's cap on visible text per message
CLIP_TITLE, CLIP_TREND, CLIP_WARN, CLIP_HOOK = 110, 150, 100, 120
MAX_WARNINGS = 3
esc = digest.esc

HELP = ("Discovery commands:\n"
        "/discover [n] - find and rank fresh ideas (takes 3-4 minutes; results arrive here)\n"
        "/trending - what is new, rising or popular, with evidence\n"
        "/ideas - recently saved ideas\n"
        "/brief <id> - the brief for an idea (use the id shown under each idea; add 'new' to rewrite it)")


# ---- Telegram API, with the token kept out of every log ---------------------------------------------------------

class Api:
    def __init__(self, token):
        self._base = f"https://api.telegram.org/bot{token}"
        self.last_error = None

    def __repr__(self):
        return "Api(<token hidden>)"

    def call(self, method, **payload):
        """→ Telegram's result, or None (see last_error). Logs only the method and a status code: the URL holds the token and
        an exception message can quote the URL."""
        self.last_error = None
        for attempt in range(2):
            try:
                body = httpx.post(f"{self._base}/{method}", json=payload, timeout=30).json()
            except Exception as e:
                print(f"Telegram {method} failed ({type(e).__name__}).", flush=True)
                self.last_error = type(e).__name__
                return None
            if body.get("ok"):
                return body.get("result") if body.get("result") is not None else True
            self.last_error = body.get("error_code")
            wait = (body.get("parameters") or {}).get("retry_after")
            if self.last_error == 429 and wait and attempt == 0:
                time.sleep(min(int(wait), 10))  # told to slow down: wait as asked, once
                continue
            print(f"Telegram {method} refused (error {self.last_error}).", flush=True)
            return None


def plain(markup):
    """HTML message → plain text, for the fallback when Telegram rejects the markup."""
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def send(api, chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    result = api.call("sendMessage", **payload)
    if result is None and api.last_error == 400:  # the markup was refused: the content must still arrive
        payload.pop("parse_mode")
        payload["text"] = plain(text)
        result = api.call("sendMessage", **payload)
    return result


def edit(api, chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    result = api.call("editMessageText", **payload)
    if result is None and api.last_error == 400:
        payload.pop("parse_mode")
        payload["text"] = plain(text)
        result = api.call("editMessageText", **payload)
    return result


# ---- formatting: pure functions, no I/O -------------------------------------------------------------------------

def clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def link(url, label):
    """A real link only for http(s); anything else is shown as text, so a feed can never put a javascript: or file: link in a chat."""
    url = url or ""
    if url.startswith(("http://", "https://")) and len(url) <= 900:
        return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'
    return esc(label)


def _site(url):
    host = urlsplit(url or "").hostname or "link"
    return host.removeprefix("www.")


def _who(e):
    name = e.get("platform") or e.get("source") or _site(e.get("url"))
    return f"{name} @{e['author']}" if e.get("author") and e.get("platform") in ("X", "YouTube") else name


def short(idea):
    return idea["id"][:8]


def trend_line(idea):
    t = idea.get("trend")
    if not t or not t.get("class"):
        return "trend: n/a"
    if t["class"] == "insufficient_data":
        return f"trend: not enough history yet ({esc(clip(t.get('explanation'), CLIP_TREND))})"
    return f"trend: <b>{t['class'].upper()}</b> ({esc(clip(t.get('explanation'), CLIP_TREND))})"


def warnings_for(idea):
    """What the reader should check before trusting or filming this idea. Capped at MAX_WARNINGS lines."""
    out = []  # most decision-relevant first, because the cap drops the tail
    if idea.get("needs_verification"):
        out.append("unverified claim about an announcement: say who claims it, do not state it as fact")
    if idea.get("penalties"):
        out.append("ranking capped: " + ", ".join(idea["penalties"]))
    out += [clip(w, CLIP_WARN) for w in idea.get("warnings") or []]
    return out[:MAX_WARNINGS]


def format_idea(idea, n, age=None):
    level, why = service.confidence(idea)
    score = idea.get("score")
    lines = [f"<b>{n}.</b> {link(idea.get('url'), clip(idea.get('title'), CLIP_TITLE))}",
             f"<code>{short(idea)}</code> · score {score:.0f} · confidence <b>{level}</b> ({esc(why)})" if isinstance(score, (int, float))
             else f"<code>{short(idea)}</code> · confidence <b>{level}</b> ({esc(why)})",
             "📈 " + trend_line(idea)]
    lines += [f"⚠ {esc(w)}" for w in warnings_for(idea)]
    if idea.get("hook"):
        lines.append(f"💬 {esc(clip(idea['hook'], CLIP_HOOK))}")
    links = [link(e.get("url"), _who(e)) for e in (idea.get("evidence") or [])[:3] if e.get("url")]
    if links:
        lines.append("🔗 " + " · ".join(links))
    if age:
        lines.append(f"<i>saved {esc(age)}</i>")
    return "\n".join(lines)


def _age(iso):
    from datetime import datetime, timezone
    try:
        secs = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None
    return f"{int(secs // 60)} min ago" if secs < 3600 else f"{int(secs // 3600)} h ago" if secs < 172800 else f"{int(secs // 86400)} days ago"


def partial_line(failed):
    """'web x4, x x1' from run's failed_sources ({'web:query': 'error:RATE_LIMITED ...'})."""
    if not failed:
        return ""
    by_channel = {}
    for name in failed:
        by_channel[name.split(":")[0]] = by_channel.get(name.split(":")[0], 0) + 1
    return "⚠ <b>Partial results</b>: " + ", ".join(f"{c} ×{n}" for c, n in by_channel.items()) + " did not deliver, so some stories may be missing."


def nav_row(view, page, pages):
    if pages <= 1:
        return []
    prev = {"text": "◀ Prev", "callback_data": f"dsc|{view}|{page - 1}"} if page > 1 else {"text": "·", "callback_data": "dsn"}
    nxt = {"text": "Next ▶", "callback_data": f"dsc|{view}|{page + 1}"} if page < pages else {"text": "·", "callback_data": "dsn"}
    return [[prev, {"text": f"{page}/{pages}", "callback_data": "dsn"}, nxt]]


def ideas_view(view, ideas, page, title, extra=""):
    chunk, page, pages, total = service.paginate(ideas, page)
    if not total:
        return "No ideas yet. Send /discover to find some.", []
    base = (page - 1) * service.PAGE_SIZE
    blocks = [format_idea(i, base + n, _age(i.get("saved_at")) if view == "ideas" else None) for n, i in enumerate(chunk, 1)]
    head = f"<b>{esc(title)}</b> · page {page}/{pages} · {total} ideas" + (f"\n{extra}" if extra else "")
    text = head + "\n\n" + "\n\n".join(blocks) + "\n\n<i>/brief &lt;id&gt; or tap Brief below</i>"
    rows = nav_row(view, page, pages) + [[{"text": f"📝 Brief {short(i)}", "callback_data": f"dsb|{i['id']}"}] for i in chunk]
    return text, rows


def trend_view(page):
    pic = service.trending()
    if not pic["run_days"]:
        return "No trend history yet. Trends need a few days of /discover runs; the first day classifies everything as not enough data.", []
    items = pic["items"]
    head = f"📈 <b>Trends</b> as of {esc(pic['as_of'])} · {pic['topics']} topics over {pic['run_days']} run-day(s)"
    fallback = "\n⚠ history is only on this machine (Supabase not used)" if pic["state"].get("backend") != "supabase" else ""
    if not items:
        c = pic["counts"]
        return (head + fallback + f"\n\nNo topic is new, rising or popular yet. <i>new</i> needs 3 earlier run-days; <i>rising</i> and "
                f"<i>popular</i> need repeat sightings. Right now: {c['insufficient_data']} topics without enough data.", [])
    chunk, page, pages, total = service.paginate(items, page, service.TREND_PAGE_SIZE)
    blocks = []
    for it in chunk:
        links = " · ".join(link(u, _site(u)) for u in it["links"])
        first = link(it["links"][0], clip(it["title"], CLIP_TITLE)) if it["links"] else esc(clip(it["title"], CLIP_TITLE))
        blocks.append(f"<b>{it['class'].upper()}</b> {first}\n{esc(clip(it['explanation'], 330))}" + (f"\n🔗 {links}" if links else ""))
    return f"{head} · page {page}/{pages}{fallback}\n\n" + "\n\n".join(blocks), nav_row("trend", page, pages)


def render_view(view, page):
    """→ (html text, inline keyboard rows) for one page of a view. Stateless: callbacks re-read the data."""
    if view == "run":
        summary, ideas = service.last_run()
        extra = partial_line((summary or {}).get("failed_sources"))
        return ideas_view("run", ideas, page, "🔎 Discovery run", extra)
    if view == "ideas":
        return ideas_view("ideas", service.saved_ideas(), page, "💡 Saved ideas")
    if view == "trend":
        return trend_view(page)
    return "That page is no longer available.", []


def pack(sections, limit=LIMIT - 40):
    """Sections (HTML strings) → as few messages as fit, never splitting inside a section unless it alone is too long."""
    messages, cur = [], ""
    for sec in sections:
        pieces = [sec]
        if digest.visible_len(sec) > limit:  # one huge section: split on its lines
            pieces, cur_piece = [], ""
            for line in sec.split("\n"):
                if cur_piece and digest.visible_len(cur_piece + "\n" + line) > limit:
                    pieces.append(cur_piece)
                    cur_piece = line
                else:
                    cur_piece = cur_piece + "\n" + line if cur_piece else line
            pieces.append(cur_piece)
        for piece in pieces:
            if cur and digest.visible_len(cur + "\n\n" + piece) > limit:
                messages.append(cur)
                cur = piece
            else:
                cur = cur + "\n\n" + piece if cur else piece
    if cur:
        messages.append(cur)
    return messages


def format_brief(idea, brief):
    """→ list of HTML messages (one unless the brief is long)."""
    level, why = service.confidence(idea)
    audit = brief.get("audit") or {}
    status = "REJECTED" if brief.get("rejected") else (audit.get("status") or "unaudited").upper()
    head = [f"📝 <b>Brief</b> · <code>{short(idea)}</code> · {status}", link(idea.get("url"), clip(idea.get("title"), CLIP_TITLE)),
            f"confidence <b>{level}</b> ({esc(why)}) · 📈 {trend_line(idea)}"]
    sections = ["\n".join(head)]
    notes = [f"⚠ {esc(clip(w, 200))}" for w in (brief.get("warnings") or [])]
    if status == "UNAUDITED":
        notes.append("⚠ This brief was NOT checked against the sources. Verify every sentence before filming.")
    if audit.get("removed"):
        notes.append(f"{len(audit['removed'])} statement(s) were removed because the sources do not support them.")
    if notes:
        sections.append("\n".join(notes))
    if brief.get("rejected"):
        sections.append(f"<b>Rejected:</b> {esc(clip(brief['rejected'], 300))}\nThe sources do not support a script. Open the links, or pick another idea.")
    else:
        sections.append(f"<b>Hook</b>\n{esc(brief.get('hook'))}")
        beats = [f"{n}. {esc(b.get('say'))}\n   <i>show: {esc(b.get('show'))}</i>" for n, b in enumerate(brief.get("beats") or [], 1)]
        if beats:
            sections.append("<b>Script</b>\n" + "\n".join(beats))
        if brief.get("on_screen_text"):
            sections.append("<b>On screen</b>\n" + "\n".join(f"• {esc(t)}" for t in brief["on_screen_text"]))
        if brief.get("verified_facts"):
            sections.append("<b>Backed by the sources</b>\n" + "\n".join(f"• {esc(t)}" for t in brief["verified_facts"]))
        if brief.get("claims_to_verify"):
            sections.append("<b>Check before filming</b>\n" + "\n".join(f"⚠ {esc(t)}" for t in brief["claims_to_verify"]))
        if brief.get("cta"):
            sections.append(f"<b>Call to action</b>\n{esc(brief['cta'])}")
        if brief.get("credit"):
            sections.append("<b>Credit on screen</b>: " + esc(", ".join(brief["credit"])))
    sections.append("🔗 " + " · ".join(link(e.get("url"), _who(e)) for e in (idea.get("evidence") or [])[:4] if e.get("url")))
    messages = pack([s for s in sections if s.strip() and s.strip() != "🔗"])
    if len(messages) > 1:
        messages = [m + f"\n<i>part {n}/{len(messages)}</i>" for n, m in enumerate(messages, 1)]
    return messages


# ---- commands -------------------------------------------------------------------------------------------------------

def _config():
    token, chat_id = os.getenv("TELEGRAM_BOT"), os.getenv("TELEGRAM_CHAT_ID")
    return (Api(token), chat_id) if token and chat_id else (None, None)


def _mins(secs):
    return f"{int(secs // 60)}m{int(secs % 60):02d}s"


def _discover_done(api, chat_id, job):
    if job.state != "done":
        send(api, chat_id, f"❌ Discovery failed ({esc(job.error or 'error')}). Nothing new was saved; /ideas still shows earlier runs.")
        return
    r = job.result
    notes = [f"✅ Done in {_mins(job.elapsed())} · {r['items']} items → {r['candidates']} stories → {r['after_seen_filter']} not shown before"]
    for key in ("seen_message", "trend_message"):
        if "LOCAL FALLBACK" in r.get(key, "") or "FAILED" in r.get(key, ""):
            notes.append("⚠ " + esc(clip(r[key], 200)))
    if r.get("saved_where") != "supabase":
        notes.append(f"⚠ ideas were saved {'only on this machine' if r.get('saved_where') == 'local' else 'NOWHERE'}; /ideas may not survive a restart")
    text, rows = render_view("run", 1)
    send(api, chat_id, "\n".join(notes) + "\n\n" + text, rows)


def cmd_discover(api, chat_id, arg):
    try:
        top = int(arg.split()[0]) if arg.split() else service.DEFAULT_TOP
    except ValueError:
        top = service.DEFAULT_TOP
    acked = threading.Event()  # a job that fails at once must not report before the "started" message has gone out
    job, started = service.start_discover(top, on_done=lambda j: (acked.wait(10), _discover_done(api, chat_id, j)))
    if started:
        send(api, chat_id, f"🔎 Discovery started (job <code>{job.id}</code>, top {max(1, min(service.MAX_TOP, top))}). Fetching sources, ranking and "
                           "checking claims takes about 3-4 minutes. I'll send the results here; the other commands keep working meanwhile.")
    else:
        send(api, chat_id, f"⏳ A discovery run is already going (job <code>{job.id}</code>, {_mins(job.elapsed())} so far). "
                           "I'll send the results when it finishes.")
    acked.set()


def _send_brief(api, chat_id, idea, brief):
    for message in format_brief(idea, brief):
        send(api, chat_id, message)


def _brief_done(api, chat_id, idea, job):
    if job.state != "done":
        send(api, chat_id, f"❌ Could not write the brief for <code>{short(idea)}</code> ({esc(job.error or 'error')}). Try again in a minute.")
        return
    _send_brief(api, chat_id, idea, job.result)


def cmd_brief(api, chat_id, arg):
    parts = arg.split()
    if not parts:
        _, ideas = service.last_run()
        hint = "\n".join(f"<code>{short(i)}</code> {esc(clip(i.get('title'), 70))}" for i in ideas[:3])
        send(api, chat_id, "Usage: /brief &lt;id&gt; (add <i>new</i> to rewrite). Ids are shown under each idea." + (f"\n\n{hint}" if hint else ""))
        return
    status, found = service.find_idea(parts[0])
    if status == "none":
        send(api, chat_id, f"No saved idea matches <code>{esc(parts[0][:20])}</code>. Use /ideas to see ids (at least 4 characters).")
        return
    if status == "ambiguous":
        listing = "\n".join(f"<code>{short(i)}</code> {esc(clip(i.get('title'), 70))}" for i in found[:5])
        send(api, chat_id, f"More than one idea starts with that. Use more characters:\n{listing}")
        return
    force = len(parts) > 1 and parts[1].lower() == "new"
    if found.get("brief") and not force:
        _send_brief(api, chat_id, found, found["brief"])
        return
    if not service.can_write_briefs():
        send(api, chat_id, "Can't write a brief: GROQ_API_KEY is not set.")
        return
    acked = threading.Event()
    job, brief, started = service.start_brief(found, force=force, on_done=lambda j: (acked.wait(10), _brief_done(api, chat_id, found, j)))
    if started:
        send(api, chat_id, f"✍️ Writing a brief for <code>{short(found)}</code> ({esc(clip(found.get('title'), 70))}). About a minute.")
    else:
        send(api, chat_id, f"⏳ That brief is already being written (job <code>{job.id}</code>, {_mins(job.elapsed())} so far).")
    acked.set()


def _parse(text):
    parts = (text or "").strip().split(None, 1)
    cmd = parts[0].lower().lstrip("/").split("@")[0] if parts and parts[0].startswith("/") else ""
    return cmd, parts[1] if len(parts) > 1 else ""


def _is_ours(update):
    cq = update.get("callback_query")
    if cq:
        return str(cq.get("data") or "").startswith(("dsc|", "dsb|", "dsn"))
    return _parse((update.get("message") or {}).get("text"))[0] in COMMANDS


def handle_update(update, api=None):
    """Handle one Telegram update if it is a discovery command or button → True (consumed). False means "not mine": the caller
    passes it on (main.py gives it to approval.handle_idle_update). Updates from any chat but the owner's are consumed and ignored."""
    if not isinstance(update, dict) or not _is_ours(update):
        return False
    real_api, chat_id = _config()
    api = api or real_api
    if api is None or not chat_id or not approval._from_owner(update, chat_id):
        return True  # not configured, or not the owner: ours to ignore, nobody else's to answer
    try:
        cq = update.get("callback_query")
        if cq:
            data = str(cq.get("data") or "")
            if data.startswith("dsb|"):
                api.call("answerCallbackQuery", callback_query_id=cq["id"], text="Working on the brief…")
                cmd_brief(api, chat_id, data.split("|", 1)[1])
            else:
                api.call("answerCallbackQuery", callback_query_id=cq["id"])
                bits = data.split("|")
                if data.startswith("dsc|") and len(bits) == 3 and bits[2].lstrip("-").isdigit():
                    text, rows = render_view(bits[1], int(bits[2]))
                    msg = (cq.get("message") or {})
                    edit(api, chat_id, msg.get("message_id"), text, rows)
            return True
        cmd, arg = _parse((update.get("message") or {}).get("text"))
        if cmd == "discover":
            cmd_discover(api, chat_id, arg)
        elif cmd == "brief":
            cmd_brief(api, chat_id, arg)
        elif cmd in ("trending", "ideas"):
            text, rows = render_view("trend" if cmd == "trending" else "ideas", 1)
            send(api, chat_id, text, rows)
    except Exception as e:  # a bug here must not take the webhook down or leave the user waiting in silence
        print(f"Discovery command failed ({type(e).__name__}).", flush=True)
        send(api, chat_id, f"⚠ Something went wrong ({esc(type(e).__name__)}). Try again, or check the server log.")
    return True


# ---- local use: long polling, no public URL --------------------------------------------------------------------------

def _updates(api, offset, timeout):
    return api.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"]) or []


def run_polling(api=None, once=False, timeout=25):
    """Poll Telegram for updates and answer discovery commands until Ctrl+C. For local use: a webhook and getUpdates cannot both
    be active, so this refuses to start while one is set (delete it deliberately, or use the deployed webhook instead)."""
    real_api, chat_id = _config()
    api = api or real_api
    if api is None:
        print("TELEGRAM_BOT / TELEGRAM_CHAT_ID are not set in .env (see README: Telegram control).")
        return 1
    info = api.call("getWebhookInfo") or {}
    if isinstance(info, dict) and info.get("url"):
        print("A webhook is set for this bot (a deployed instance is using it), so polling would conflict. Not starting.")
        return 1
    print("Polling for /discover /trending /ideas /brief. Ctrl+C to stop.")
    offset = None
    try:
        while True:
            for update in _updates(api, offset, timeout):
                offset = update["update_id"] + 1
                if not handle_update(update, api) and approval._from_owner(update, chat_id) and (update.get("message") or {}).get("text"):
                    send(api, chat_id, HELP)  # anything else typed to the bot in local mode
            if once:
                return 0
    except KeyboardInterrupt:
        print("Stopped.")
        return 0


def show_chat_ids(api=None):
    """Print the chat ids that have messaged the bot, to fill TELEGRAM_CHAT_ID in .env. Prints no token and no message text."""
    token = os.getenv("TELEGRAM_BOT")
    api = api or (Api(token) if token else None)
    if api is None:
        print("TELEGRAM_BOT is not set in .env.")
        return 1
    updates = api.call("getUpdates", timeout=1)
    if updates is None:
        print("Could not read updates (a webhook may be set for this bot, or the token is wrong).")
        return 1
    seen = {}
    for u in updates:
        chat = ((u.get("message") or {}).get("chat")) or (((u.get("callback_query") or {}).get("message") or {}).get("chat")) or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = chat.get("type", "?")
    if not seen:
        print("No messages yet. Send any message to your bot in Telegram, then run this again.")
        return 1
    for cid, kind in seen.items():
        print(f"chat id: {cid} ({kind})")
    print("Put the one that is you (type 'private') in .env as TELEGRAM_CHAT_ID.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(show_chat_ids() if "--chat-id" in sys.argv else run_polling())
