"""Offline tests for the Telegram front end of content discovery. Telegram, the pipeline, the model and trend history are all
fakes: nothing here sends a message, touches the network, or writes outside a temp folder.

    .venv\\Scripts\\python -m tests.test_telegram
"""

import contextlib
import hashlib
import html
import io
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from agent import digest, telegram_discovery as tg  # noqa: E402
from agent.discovery import jobs, saved, service  # noqa: E402
from tests.test_service import Env, NOW, idea, wait_for  # noqa: E402

TOKEN = "123456:SECRET-TOKEN-must-never-appear-anywhere"
OWNER = 42
STRANGER = 99


class Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeTelegram:
    """Stands in for httpx.post as the adapter calls it. Records (method, payload)."""

    def __init__(self):
        self.calls, self.reject_html, self.raise_with_url, self.rate_limit_once = [], False, False, False
        self.updates, self.webhook_url, self.refuse = [], "", {}
        self.mid = 100

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        if self.raise_with_url:
            raise httpx.ConnectError(f"All connection attempts failed for {url}")
        if self.rate_limit_once:
            self.rate_limit_once = False
            return Resp({"ok": False, "error_code": 429, "parameters": {"retry_after": 3}})
        if method in self.refuse:
            return Resp({"ok": False, "error_code": self.refuse[method]})
        if self.reject_html and (json or {}).get("parse_mode") == "HTML":
            return Resp({"ok": False, "error_code": 400, "description": "can't parse entities"})
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return Resp({"ok": True, "result": out})
        if method == "getWebhookInfo":
            return Resp({"ok": True, "result": {"url": self.webhook_url}})
        self.mid += 1
        return Resp({"ok": True, "result": {"message_id": self.mid}})

    def sent(self, *methods):
        return [p for m, p in self.calls if m in (methods or ("sendMessage",))]

    def texts(self):
        return [p["text"] for p in self.sent("sendMessage", "editMessageText")]


class Bot:
    """Everything a test needs: fake Telegram, token and chat id in the environment, isolated saved ideas and jobs, and stubbed
    pipeline, model and trend history."""

    def __init__(self, supabase=True, groq=True):
        self.env, self.tg, self.groq = Env(supabase), FakeTelegram(), groq
        self.run_gate = None
        self.run_calls, self.brief_calls = [], []
        self.run_result = lambda top: {"ideas": [idea(n) for n in range(1, 9)], "generated_at": "2026-10-25T12:00:00+00:00",
                                       "complete": True, "failed_sources": {}, "items": 200, "candidates": 150, "after_seen_filter": 120,
                                       "already_seen": {"same_url": 30, "same_story": 0}, "seen_filter": {"message": "Seen-filter: SUPABASE"},
                                       "trends": {"message": "Trends: SUPABASE", "counts": {}}, "note": ""}
        self.brief = {"hook": "A hook", "beats": [{"say": "say one", "show": "show one"}, {"say": "say two", "show": "show two"}],
                      "on_screen_text": ["screen"], "verified_facts": ["a fact"], "claims_to_verify": ["a claim"], "cta": "cta",
                      "credit": ["u on HackerNews"], "warnings": [], "audit": {"status": "verified", "removed": []}}
        self.overview = {"state": {"backend": "supabase"}, "as_of": "2026-10-25", "run_days": 4, "topics": 20,
                         "counts": {"new": 1, "rising": 1, "popular": 0, "insufficient_data": 18}, "items": []}

    def __enter__(self):
        self.env.__enter__()
        self.saved_env = {k: os.environ.get(k) for k in ("TELEGRAM_BOT", "TELEGRAM_CHAT_ID", "GROQ_API_KEY", "TRIGGER_SECRET")}
        os.environ["TELEGRAM_BOT"], os.environ["TELEGRAM_CHAT_ID"] = TOKEN, str(OWNER)
        os.environ.pop("GROQ_API_KEY", None)
        if self.groq:
            os.environ["GROQ_API_KEY"] = "test-model-key"
        self.patched = (tg.httpx.post, tg.time.sleep, service.run.run, service.scripts.build_script, service.trends.overview)
        tg.httpx.post, tg.time.sleep = self.tg.post, lambda s: None

        def fake_run(top, briefs, now):
            self.run_calls.append(top)
            if self.run_gate:
                self.run_gate.wait(10)
            return self.run_result(top)

        def fake_build(idea_, key):
            self.brief_calls.append(idea_["id"])
            if isinstance(self.brief, Exception):
                raise self.brief
            return self.brief
        service.run.run, service.scripts.build_script, service.trends.overview = fake_run, fake_build, lambda: self.overview
        return self

    def __exit__(self, *a):
        # a job thread sends its result after the job state changes: wait for the threads themselves, or a late message would
        # go out through the real httpx once the fake is put back
        end = time.monotonic() + 10
        while time.monotonic() < end and any(t.name.startswith("job-") and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.01)
        (tg.httpx.post, tg.time.sleep, service.run.run, service.scripts.build_script, service.trends.overview) = self.patched
        for k, v in self.saved_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.env.__exit__(*a)

    def say(self, text, chat=OWNER):
        return tg.handle_update(msg(text, chat))

    def tap(self, data, chat=OWNER, mid=555):
        return tg.handle_update(cb(data, chat, mid))

    def seed(self, n=8):
        saved.save_run([idea(i) for i in range(1, n + 1)], "run1", NOW)


def msg(text, chat=OWNER):
    return {"update_id": 1, "message": {"chat": {"id": chat, "type": "private"}, "text": text}}


def cb(data, chat=OWNER, mid=555):
    return {"update_id": 2, "callback_query": {"id": "cb-1", "data": data, "message": {"message_id": mid, "chat": {"id": chat}}}}


def visible(text):
    return digest.visible_len(text)


# ---- routing, ownership, secrets ----------------------------------------------------------------------------------

def check_routing_and_owner():
    with Bot() as bot:
        # not ours: handed on untouched (main.py gives it to the existing handler)
        for update in (msg("/help"), msg("/generate"), msg("hello"), msg("/discovery"), cb("y"), cb("draft:x"), {"update_id": 3},
                       {"message": {"chat": {"id": OWNER}}}, "junk", None, []):
            assert tg.handle_update(update) is False, update
        # ours but from somebody else: consumed and ignored, nothing sent, nothing started
        for update in (msg("/discover", STRANGER), msg("/ideas", STRANGER), msg("/brief abcd", STRANGER), cb("dsc|ideas|2", STRANGER),
                       cb("dsb|abc", STRANGER), cb("dsn", STRANGER)):
            assert tg.handle_update(update) is True
        assert bot.tg.calls == [] and bot.run_calls == [] and not jobs.running()
        # the same commands in every spelling Telegram sends
        bot.seed(1)
        for text in ("/ideas", "/IDEAS", "/ideas@MyDiscoveryBot", "  /ideas  "):
            bot.tg.calls.clear()
            assert bot.say(text) is True and len(bot.tg.sent()) == 1, text
        # not configured: still consumed (nobody else should answer), nothing sent
        bot.tg.calls.clear()
        os.environ.pop("TELEGRAM_CHAT_ID")
        assert bot.say("/ideas") is True and bot.tg.calls == []
        os.environ["TELEGRAM_CHAT_ID"] = str(OWNER)
        os.environ.pop("TELEGRAM_BOT")
        assert bot.say("/ideas") is True and bot.tg.calls == []


def check_the_token_never_leaks():
    out, err = io.StringIO(), io.StringIO()
    with Bot() as bot, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        bot.seed(2)
        bot.tg.raise_with_url = True  # the exception message quotes the URL, which holds the token
        bot.say("/ideas")
        bot.say("/trending")
        bot.tg.raise_with_url = False
        bot.tg.refuse["sendMessage"] = 403
        bot.say("/ideas")
        bot.tg.refuse.clear()
        bot.brief = RuntimeError(f"api.telegram.org/bot{TOKEN}/sendMessage")
        bot.say(f"/brief {idea(1)['id'][:6]}")
        wait_for(lambda: not jobs.running())
        bot.run_result = lambda top: (_ for _ in ()).throw(RuntimeError(f"boom {TOKEN}"))
        bot.say("/discover")
        wait_for(lambda: not jobs.running())
        time.sleep(0.2)
        everything = out.getvalue() + err.getvalue() + repr(bot.tg.calls) + repr(tg.Api(TOKEN)) + repr(vars(jobs.latest("discover") or object()))
    assert TOKEN not in everything and "SECRET-TOKEN" not in everything, "the bot token reached a log, a message or a job record"
    assert "failed (ConnectError)" in out.getvalue() and "refused (error 403)" in out.getvalue(), out.getvalue()
    assert repr(tg.Api(TOKEN)) == "Api(<token hidden>)"
    # failures are shown to the user by type only
    with Bot() as bot:
        bot.run_result = lambda top: (_ for _ in ()).throw(RuntimeError(f"boom {TOKEN}"))
        bot.say("/discover")
        assert wait_for(lambda: any("failed" in t for t in bot.tg.texts()))
        shown = " ".join(bot.tg.texts())
        assert "RuntimeError" in shown and TOKEN not in shown and "boom" not in shown


def check_telegram_hiccups():
    with Bot() as bot:
        bot.seed(2)
        # the markup is refused: the content still arrives, as plain text
        bot.tg.reject_html = True
        bot.say("/ideas")
        sent = bot.tg.sent()
        assert len(sent) == 2 and "parse_mode" in sent[0] and "parse_mode" not in sent[1], "retried once without markup"
        assert "<b>" not in sent[1]["text"] and "<a href" not in sent[1]["text"] and "Idea number 1" in sent[1]["text"]
        bot.tg.reject_html = False
        # told to slow down: waits as asked, once, then sends
        bot.tg.calls.clear()
        bot.tg.rate_limit_once = True
        bot.say("/ideas")
        assert [m for m, _ in bot.tg.calls] == ["sendMessage", "sendMessage"]


# ---- /discover: long job, pagination -----------------------------------------------------------------------------

def check_discover_runs_in_the_background_and_pages_its_results():
    with Bot() as bot:
        bot.run_gate = threading.Event()
        t0 = time.monotonic()
        assert bot.say("/discover 5") is True
        assert time.monotonic() - t0 < 1, "the command answered at once; the work did not block it"
        first = bot.tg.texts()[0]
        assert "Discovery started" in first and "top 5" in first and "3-4 minutes" in first, first
        assert wait_for(lambda: bot.run_calls == [5])
        bot.say("/discover")
        assert "already going" in bot.tg.texts()[1] and bot.run_calls == [5], "a second request does not start a second run"
        assert len(jobs.running("discover")) == 1
        # other commands keep working while it runs
        bot.say("/ideas")
        assert "No ideas yet" in bot.tg.texts()[2]
        bot.run_gate.set()
        assert wait_for(lambda: any("Done in" in t for t in bot.tg.texts()))
        final = [p for p in bot.tg.sent() if "Done in" in p["text"]][0]
        text, rows = final["text"], final["reply_markup"]["inline_keyboard"]
        assert "200 items → 150 stories → 120 not shown before" in text and "page 1/3" in text and "8 ideas" in text
        for n in (1, 2, 3):
            assert f"Idea number {n}<" in text or f"Idea number {n}</a>" in text
        assert "Idea number 4" not in text
        assert rows[0][2] == {"text": "Next ▶", "callback_data": "dsc|run|2"} and rows[0][0]["callback_data"] == "dsn"
        assert [r[0]["callback_data"] for r in rows[1:]] == [f"dsb|{idea(n)['id']}" for n in (1, 2, 3)]
        assert len(saved.list_ideas()) == 8, "what it showed was saved"
        # defaults and clamping
        bot.run_gate = None
        bot.say("/discover abc")
        assert wait_for(lambda: bot.run_calls[-1] == service.DEFAULT_TOP)
        wait_for(lambda: not jobs.running())
        bot.say("/discover 500")
        assert wait_for(lambda: bot.run_calls[-1] == service.MAX_TOP)


def check_discover_failures_and_warnings_are_reported():
    with Bot() as bot:
        bot.run_result = lambda top: (_ for _ in ()).throw(ValueError("internal detail"))
        bot.say("/discover")
        assert wait_for(lambda: any("failed" in t for t in bot.tg.texts()))
        shown = bot.tg.texts()[-1]
        assert "ValueError" in shown and "internal detail" not in shown and "Nothing new was saved" in shown
    with Bot() as bot:
        res = bot.run_result(10)
        res.update(complete=False, failed_sources={"web:a": "error:RATE_LIMITED", "web:b": "error:RATE_LIMITED", "x:c": "error:TIMEOUT"},
                   seen_filter={"message": "Seen-filter: LOCAL FALLBACK, 0 ideas remembered."},
                   trends={"message": "Trends: LOCAL FALLBACK, 5 observations", "counts": {}})
        bot.run_result = lambda top: res
        bot.say("/discover")
        assert wait_for(lambda: any("Done in" in t for t in bot.tg.texts()))
        text = [t for t in bot.tg.texts() if "Done in" in t][0]
        assert "Partial results" in text and "web ×2" in text and "x ×1" in text, text
        assert "LOCAL FALLBACK" in text, "a degraded ledger is shown, not hidden"
    with Bot(supabase=False) as bot:
        bot.say("/discover")
        assert wait_for(lambda: any("Done in" in t for t in bot.tg.texts()))
        assert "only on this machine" in [t for t in bot.tg.texts() if "Done in" in t][0]


def check_pagination_edits_in_place():
    with Bot() as bot:
        bot.seed(8)
        bot.say("/ideas")
        first = bot.tg.sent()[0]
        assert "page 1/3" in first["text"] and "Idea number 3" in first["text"] and "Idea number 4" not in first["text"]
        assert first["reply_markup"]["inline_keyboard"][0][2]["callback_data"] == "dsc|ideas|2"
        bot.tg.calls.clear()
        assert bot.tap("dsc|ideas|2", mid=555) is True
        assert [m for m, _ in bot.tg.calls] == ["answerCallbackQuery", "editMessageText"], "acknowledged, then edited the same message"
        edited = bot.tg.sent("editMessageText")[0]
        assert edited["message_id"] == 555 and edited["chat_id"] == str(OWNER) and "page 2/3" in edited["text"]
        assert "Idea number 4" in edited["text"] and "Idea number 6" in edited["text"] and "Idea number 3" not in edited["text"]
        nav = edited["reply_markup"]["inline_keyboard"][0]
        assert nav[0]["callback_data"] == "dsc|ideas|1" and nav[2]["callback_data"] == "dsc|ideas|3"
        bot.tg.calls.clear()
        bot.tap("dsc|ideas|3")
        last = bot.tg.sent("editMessageText")[0]
        assert "Idea number 7" in last["text"] and "Idea number 8" in last["text"] and "Next" not in str(last["reply_markup"])
        assert len(last["reply_markup"]["inline_keyboard"]) == 1 + 2, "one nav row plus a Brief button per idea on the page"
        # out-of-range pages come back into range; bad data never raises
        for data, expect in (("dsc|ideas|99", "page 3/3"), ("dsc|ideas|-4", "page 1/3"), ("dsc|ideas|0", "page 1/3")):
            bot.tg.calls.clear()
            bot.tap(data)
            assert expect in bot.tg.sent("editMessageText")[0]["text"], data
        for junk in ("dsc|ideas|x", "dsc|ideas", "dsc|nope|1", "dsc||", "dsc|ideas|1|2", "dsb|", "dsb|zzzz"):
            bot.tg.calls.clear()
            assert bot.tap(junk) is True
            assert bot.tg.calls[0][0] == "answerCallbackQuery", junk  # a tap is always acknowledged
        bot.tg.calls.clear()
        bot.tap("dsn")
        assert [m for m, _ in bot.tg.calls] == ["answerCallbackQuery"], "the page indicator button does nothing"


def check_messages_are_safe_and_fit():
    nasty = idea(1, title="<script>alert('x')</script> & " + "very long title " * 40, url='javascript:alert("x")',
                 hook="<b>hook</b> & " + "long hook words " * 30, needs_verification=True,
                 warnings=["hook dropped: " + "reason " * 40, "second warning", "third warning", "fourth warning is cut"],
                 penalties=["single_source", "unverified_claim", "engagement_bait"], score=55.5,
                 trend={"class": "rising", "explanation": "independent sources grew: " + "detail " * 60, "topic_key": "k"},
                 evidence=[{"source": "X search", "platform": "X", "author": 'bad"name', "url": f'https://x.com/i/status/{n}?a="b"&c=d',
                            "title": "t"} for n in range(6)])
    worst = [{**nasty, "id": f"{n:04x}" + "ab" * 4} for n in (1, 2, 3)]
    text, rows = tg.ideas_view("ideas", worst, 1, "💡 Saved ideas")
    assert digest.visible_len(text) <= digest.LIMIT, digest.visible_len(text)
    assert "<script>" not in text and "&lt;script&gt;" in text, "title is escaped"
    assert "javascript:" not in text.replace("&quot;", '"').split("<a href=")[1] if "<a href=" in text else True
    assert 'href="javascript' not in text, "only http(s) becomes a link"
    assert '"b"' not in text.split("🔗")[1].split("<a href=")[1].split(">")[0], "quotes inside a link are escaped"
    assert text.count("⚠") <= 3 * tg.MAX_WARNINGS and "fourth warning" not in text, "warnings are capped per idea"
    assert "unverified claim" in text and "ranking capped" in text and "RISING" in text and "confidence <b>low</b>" in text
    assert text.count("🔗") == 3 and text.count("<a href") <= 3 * 4, "at most three source links per idea"
    assert len(rows) == 3 and all(len(r) == 1 for r in rows), "single page: no nav row, one Brief button each"
    # every field the user asked for is on a normal idea
    normal, _ = tg.ideas_view("run", [idea(1, claim_status="first_party", score=80.0, warnings=["hook dropped: no passage supports it"],
                                         trend={"class": "insufficient_data", "explanation": "one snapshot", "topic_key": "k"})],
                              1, "🔎 Discovery run")
    for needle in ("href=\"https://example.com/1\"", "score 80", "confidence <b>high</b>", "the organisation's own post",
                   "not enough history yet", "⚠ hook dropped", "🔗", f"<code>{idea(1)['id'][:8]}</code>", "HackerNews"):
        assert needle in normal, needle
    assert "n/a" in tg.trend_line({"trend": None}) and "n/a" in tg.trend_line({}), "no trend data is said, not invented"


# ---- /trending and /ideas -------------------------------------------------------------------------------------------

def check_trending():
    items = [{"topic_key": f"k{n}", "title": f"Topic {n} <b>", "class": ("rising", "popular", "new")[n % 3], "days_seen": 3,
              "explanation": f"independent sources 1 -> 3 since 2026-10-23 (new: youtube, web) {n}", "evidence": {}, "links": [f"https://a.example/{n}", f"https://b.example/{n}"]}
             for n in range(6)]
    with Bot() as bot:
        bot.overview = {**bot.overview, "items": items, "counts": {"new": 2, "rising": 2, "popular": 2, "insufficient_data": 14}}
        bot.say("/trending")
        sent = bot.tg.sent()[0]
        text = sent["text"]
        assert "page 1/2" in text and "20 topics over 4 run-day(s)" in text and "as of 2026-10-25" in text
        assert "<b>RISING</b>" in text and "independent sources 1 -&gt; 3" in text and 'href="https://a.example/0"' in text
        assert "Topic 0 &lt;b&gt;" in text and "Topic 4" not in text and visible(text) <= digest.LIMIT
        assert sent["reply_markup"]["inline_keyboard"][0][2]["callback_data"] == "dsc|trend|2"
        bot.tap("dsc|trend|2")
        assert "Topic 4" in bot.tg.sent("editMessageText")[0]["text"]
        # fewer than a page: no buttons
        bot.overview = {**bot.overview, "items": items[:2]}
        bot.tg.calls.clear()
        bot.say("/trending")
        assert "reply_markup" not in bot.tg.sent()[0]
        # nothing to say yet: the reason, not an empty list
        bot.overview = {**bot.overview, "items": [], "counts": {"new": 0, "rising": 0, "popular": 0, "insufficient_data": 20}}
        bot.tg.calls.clear()
        bot.say("/trending")
        said = bot.tg.texts()[0]
        assert "No topic is new, rising or popular yet" in said and "3 earlier run-days" in said and "20 topics" in said
        bot.overview = {**bot.overview, "run_days": 0, "as_of": None}
        bot.tg.calls.clear()
        bot.say("/trending")
        assert "No trend history yet" in bot.tg.texts()[0]
        # history kept only locally is flagged
        bot.overview = {**bot.overview, "run_days": 3, "items": items[:1], "state": {"backend": "local_fallback"}}
        bot.tg.calls.clear()
        bot.say("/trending")
        assert "only on this machine" in bot.tg.texts()[0]
        # history unreadable: said, with the type only
        service.trends.overview = lambda: (_ for _ in ()).throw(OSError("disk path with secrets"))
        bot.tg.calls.clear()
        bot.say("/trending")
        shown = bot.tg.texts()[0]
        assert "Something went wrong (OSError)" in shown and "secrets" not in shown


def check_ideas_listing():
    with Bot() as bot:
        bot.say("/ideas")
        assert "No ideas yet" in bot.tg.texts()[0] and "/discover" in bot.tg.texts()[0]
        bot.seed(4)
        saved.save_run([idea(9)], "run2", NOW)
        bot.tg.calls.clear()
        bot.say("/ideas")
        text = bot.tg.texts()[0]
        assert text.index("Idea number 9") < text.index("Idea number 1"), "newest run first"
        assert "page 1/2" in text and "5 ideas" in text and re.search(r"saved \d+ (min|h|days?) ago", text), text


# ---- /brief --------------------------------------------------------------------------------------------------------

def check_brief_lookup_and_generation():
    with Bot() as bot:
        bot.seed(3)
        a = idea(1)["id"]
        # usage and bad ids
        bot.say("/brief")
        assert "Usage: /brief" in bot.tg.texts()[0] and a[:8] in bot.tg.texts()[0]
        for arg in ("abc", "zzzz", "#"):
            bot.tg.calls.clear()
            bot.say(f"/brief {arg}")
            assert "No saved idea matches" in bot.tg.texts()[0], arg
        # ambiguous prefix
        saved.save_run([{**idea(5), "id": a[:5] + "11111111"[:7]}], "run2", NOW)
        bot.tg.calls.clear()
        bot.say(f"/brief {a[:4]}")
        assert "More than one idea" in bot.tg.texts()[0]
        # written now: acknowledged at once, delivered when ready, saved, and not written twice
        bot.tg.calls.clear()
        bot.say(f"/brief {idea(2)['id'][:6]}")
        assert "Writing a brief" in bot.tg.texts()[0]
        assert wait_for(lambda: any("Brief</b>" in t for t in bot.tg.texts()))
        text = [t for t in bot.tg.texts() if "Brief</b>" in t][0]
        for needle in ("VERIFIED", "<b>Hook</b>\nA hook", "1. say one", "<i>show: show one</i>", "Backed by the sources", "Check before filming",
                       "⚠ a claim", "Credit on screen", "u on HackerNews", "confidence <b>low</b>", "🔗", "news.ycombinator.com"):
            assert needle in text, needle
        assert bot.brief_calls == [idea(2)["id"]] and saved.find(idea(2)["id"])[1]["brief"]["hook"] == "A hook"
        bot.tg.calls.clear()
        bot.say(f"/brief {idea(2)['id'][:6]}")
        assert bot.brief_calls == [idea(2)["id"]] and "Brief</b>" in bot.tg.texts()[0] and len(bot.tg.texts()) == 1, "saved: no second model call"
        bot.say(f"/brief {idea(2)['id'][:6]} new")
        assert wait_for(lambda: len(bot.brief_calls) == 2), "'new' rewrites it"
        # the button does the same as the command, and acknowledges the tap
        bot.tg.calls.clear()
        bot.tap(f"dsb|{idea(3)['id']}")
        assert bot.tg.calls[0][0] == "answerCallbackQuery" and bot.tg.calls[0][1].get("text")
        assert wait_for(lambda: idea(3)["id"] in bot.brief_calls)


def check_brief_edge_cases():
    with Bot() as bot:
        bot.seed(2)
        i = idea(1)["id"][:6]
        # no model key: said, nothing started
        os.environ.pop("GROQ_API_KEY")
        bot.say(f"/brief {i}")
        assert "GROQ_API_KEY is not set" in bot.tg.texts()[0] and bot.brief_calls == [] and not jobs.running()
        os.environ["GROQ_API_KEY"] = "k"
        # a second request while it is being written
        gate = threading.Event()
        service.scripts.build_script = lambda x, k: gate.wait(10) and bot.brief
        bot.tg.calls.clear()
        bot.say(f"/brief {i}")
        bot.say(f"/brief {i}")
        assert "Writing a brief" in bot.tg.texts()[0] and "already being written" in bot.tg.texts()[1]
        gate.set()
        assert wait_for(lambda: any("Brief</b>" in t for t in bot.tg.texts()))
        # failure: the type only, and the user can retry
        service.scripts.build_script = lambda x, k: (_ for _ in ()).throw(TimeoutError("model said https://secret"))
        bot.tg.calls.clear()
        bot.say(f"/brief {idea(2)['id'][:6]}")
        assert wait_for(lambda: any("Could not write" in t for t in bot.tg.texts()))
        shown = " ".join(bot.tg.texts())
        assert "TimeoutError" in shown and "secret" not in shown
        # rejected and unaudited briefs say so loudly
        for brief, needles in (({"rejected": "only 1 beat(s) are supported", "audit": {"status": "rejected", "removed": []}, "warnings": []},
                                ["REJECTED", "Rejected:", "do not support a script"]),
                               ({"hook": "h", "beats": [{"say": "s", "show": "w"}], "audit": {"status": "unaudited", "removed": []},
                                 "warnings": ["NOT AUDITED: statements were not checked against the evidence"]}, ["UNAUDITED", "NOT checked", "NOT AUDITED"]),
                               ({"hook": "h", "beats": [], "audit": {"status": "trimmed", "removed": [{}, {}, {}]}, "warnings": []},
                                ["TRIMMED", "3 statement(s) were removed"])):
            text = "\n".join(tg.format_brief(idea(1), brief))
            for needle in needles:
                assert needle in text, (needle, text)
        # escaping
        text = "\n".join(tg.format_brief(idea(1), {"hook": "<b>x</b> & y", "beats": [{"say": "<i>s</i>", "show": "<u>w</u>"}],
                                                    "audit": {"status": "verified", "removed": []}}))
        assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text and "<i>s</i>" not in text.replace("<i>show:", "")


def check_a_long_brief_is_split_into_parts():
    brief = {"hook": "H", "beats": [{"say": f"beat {n} " + "word " * 70, "show": "show " * 30} for n in range(12)],
             "on_screen_text": ["screen text " * 5] * 6, "verified_facts": ["fact " * 40] * 5, "claims_to_verify": ["claim " * 40] * 6,
             "cta": "c", "credit": ["a"], "warnings": [], "audit": {"status": "verified", "removed": []}}
    parts = tg.format_brief(idea(1), brief)
    assert len(parts) >= 2 and all(visible(p) <= digest.LIMIT for p in parts), [visible(p) for p in parts]
    assert all(f"part {n}/{len(parts)}" in p for n, p in enumerate(parts, 1))
    joined = "".join(parts)
    assert all(f"beat {n} " in joined for n in range(12)) and "screen text" in joined and "claim claim" in joined, "nothing is dropped"
    assert parts[0].startswith("📝 <b>Brief</b>"), "the header comes first"
    one_big_line = tg.pack(["x" * 9000])
    assert len(one_big_line) == 1 or all(visible(m) <= digest.LIMIT for m in one_big_line)
    lines = tg.pack(["\n".join("line %d " % n + "z" * 60 for n in range(200))])
    assert len(lines) > 1 and all(visible(m) <= digest.LIMIT for m in lines)
    with Bot() as bot:
        bot.seed(1)
        bot.brief = brief
        bot.say(f"/brief {idea(1)['id'][:6]}")
        assert wait_for(lambda: len([t for t in bot.tg.texts() if "part " in t]) == len(parts))


# ---- local polling, setup helper, and the real webhook route -----------------------------------------------------------

def check_local_polling_and_setup_helper():
    with Bot() as bot:
        bot.seed(2)
        api = tg.Api(TOKEN)
        # refuses while a webhook is set (a deployed instance owns the updates)
        bot.tg.webhook_url = "https://example.com/telegram-webhook"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert tg.run_polling(api, once=True) == 1
        assert "webhook is set" in out.getvalue() and "getUpdates" not in [m for m, _ in bot.tg.calls]
        # polls, answers its commands, points everything else at /help, ignores strangers
        bot.tg.webhook_url, bot.tg.calls = "", []
        bot.tg.updates = [{**msg("/ideas"), "update_id": 10}, {**msg("hello there"), "update_id": 11}, {**msg("/ideas", STRANGER), "update_id": 12}]
        with contextlib.redirect_stdout(io.StringIO()):
            assert tg.run_polling(api, once=True, timeout=0) == 0
        texts = bot.tg.texts()
        assert len(texts) == 2 and "Idea number 1" in texts[0] and "Discovery commands" in texts[1], texts
        polled = [p for m, p in bot.tg.calls if m == "getUpdates"]
        assert polled and polled[0]["offset"] is None
        # not configured
        os.environ.pop("TELEGRAM_BOT")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert tg.run_polling(once=True) == 1
        assert "not set" in out.getvalue()
        os.environ["TELEGRAM_BOT"] = TOKEN
        # the chat-id helper prints ids, never the token or the message text
        bot.tg.updates = [{"update_id": 1, "message": {"chat": {"id": 777, "type": "private"}, "text": "my private words"}},
                          {"update_id": 2, "message": {"chat": {"id": -100123, "type": "group"}, "text": "x"}}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert tg.show_chat_ids(api) == 0
        shown = out.getvalue()
        assert "chat id: 777 (private)" in shown and "-100123 (group)" in shown and "private words" not in shown and TOKEN not in shown
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert tg.show_chat_ids(api) == 1
        assert "No messages yet" in out.getvalue()
        bot.tg.refuse["getUpdates"] = 409
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert tg.show_chat_ids(api) == 1
        assert "webhook may be set" in out.getvalue() and TOKEN not in out.getvalue()


def check_the_webhook_route_sends_ours_to_discovery_and_the_rest_to_the_old_handler():
    from fastapi.testclient import TestClient

    import main
    with Bot() as bot:
        bot.seed(1)
        os.environ["TRIGGER_SECRET"] = "route-secret"
        header = {"X-Telegram-Bot-Api-Secret-Token": hashlib.sha256(b"route-secret").hexdigest()}
        passed_on = []
        real = main.approval.handle_idle_update
        main.approval.handle_idle_update = lambda update, trigger: passed_on.append(update)
        try:
            client = TestClient(main.app)
            r = client.post("/telegram-webhook", json=msg("/ideas"), headers=header)
            assert r.status_code == 200 and passed_on == [] and "Idea number 1" in bot.tg.texts()[0], "ours: handled here, not passed on"
            r = client.post("/telegram-webhook", json=msg("/help"), headers=header)
            assert r.status_code == 200 and len(passed_on) == 1, "not ours: the existing handler gets it unchanged"
            r = client.post("/telegram-webhook", json=msg("/discover", STRANGER), headers=header)
            assert r.status_code == 200 and len(passed_on) == 1 and not jobs.running(), "a stranger's command goes nowhere"
            assert client.post("/telegram-webhook", json=msg("/ideas"), headers={}).status_code == 403
            assert client.post("/telegram-webhook", json=msg("/ideas"), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
        finally:
            main.approval.handle_idle_update = real


if __name__ == "__main__":
    import socket
    attempts = []
    real_connect = socket.socket.connect

    def blocked(self, address, *args, **kwargs):  # these tests have no business on the network (loopback is asyncio's own)
        if not (isinstance(address, tuple) and str(address[0]) in ("127.0.0.1", "::1", "localhost")):
            attempts.append(address)
            raise OSError("network blocked in tests")
        return real_connect(self, address, *args, **kwargs)
    socket.socket.connect = blocked
    check_routing_and_owner()
    check_the_token_never_leaks()
    check_telegram_hiccups()
    check_discover_runs_in_the_background_and_pages_its_results()
    check_discover_failures_and_warnings_are_reported()
    check_pagination_edits_in_place()
    check_messages_are_safe_and_fit()
    check_trending()
    check_ideas_listing()
    check_brief_lookup_and_generation()
    check_brief_edge_cases()
    check_a_long_brief_is_split_into_parts()
    check_local_polling_and_setup_helper()
    check_the_webhook_route_sends_ours_to_discovery_and_the_rest_to_the_old_handler()
    socket.socket.connect = real_connect
    assert not attempts, f"the tests tried to reach the network: {attempts}"
    print("telegram tests passed")
