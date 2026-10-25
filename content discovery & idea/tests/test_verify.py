"""Offline regression tests for brief and hook accuracy: invented implementation details, vendor benchmarks,
unsupported superlatives, false first-person experience, thin evidence, skipped statements, a failed audit.

The fake model is deliberately LENIENT: it labels every statement and offers the whole evidence as the supporting
quote, i.e. it blesses everything. So these tests prove the code decides, not the model's good behaviour. What the
real model does is checked by reading a live run (see VALIDATION_REPORT.md), not here.

    .venv\\Scripts\\python -m tests.test_verify
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import llm  # noqa: E402
from agent.discovery import scripts, verify  # noqa: E402

DECISIONS = {
    "title": "Build more responsive apps with the Decisions API",
    "summary": "Fast, focused, low-cost decisions for any application.",
    "claim_status": "first_party", "needs_verification": False, "format": "tool walkthrough", "hook": "",
    "evidence": [{"source": "OpenAI", "platform": "OpenAI", "author": None, "url": "https://openai.com/index/decisions-api",
                  "title": "Build more responsive apps with the Decisions API"}],
}
KIMI = {
    "title": "Introducing Kimi K3.1 Pro: our most capable open-weight model",
    "summary": "On Terminal-Bench 4.0, K3.1 Pro scores 65.7%, ahead of GPT-6 Astra (58.2%) and narrowly behind the best "
               "closed model. It is the best open-weight model for coding agents.",
    "claim_status": "first_party", "needs_verification": False, "format": "launch reaction", "hook": "",
    "evidence": [{"source": "X search", "platform": "X", "author": "Kimi_Moonshot", "url": "https://x.com/i/status/1",
                  "title": "Introducing Kimi K3.1 Pro: our most capable open-weight model"}],
}


def lenient(text, evidence):
    """What an over-eager model returns: a type, the whole evidence as the 'supporting quote', and an honest 'attributed'."""
    plan = re.match(r"(?:today|next|then|finally|first|let'?s|hey|i'?(?:ll|m|ve)|i (?:built|tested|made)|my |follow|stay|want|ready|here)",
                    text, re.I) or text.endswith("?") or len(text.split()) <= 3  # short shot notes: "A laptop.", "The post."
    claim = re.search(r"fast|best|beat|top|ahead|scores|%|rank|faster|leader", text, re.I)
    kind = "plan" if plan else "claim" if claim else "fact"
    return {"kind": kind, "quote": evidence, "attributed": bool(verify.ATTRIBUTION_RE.search(text)), "reason": "supported"}


class Model:
    """Stands in for llm.ask_json: `brief` for the writing call, scripted verdicts for audit calls. Counts both."""

    def __init__(self, brief=None, decide=lenient, skip=None, boom=None, bad_replies=0):
        self.brief, self.decide, self.skip, self.boom, self.bad_replies = brief, decide, skip, boom, bad_replies
        self.writing, self.audits = [], []

    def __call__(self, model, prompt, schema, api_key=None, **opts):
        if schema is not verify.AUDIT_SCHEMA:
            self.writing.append(prompt)
            return dict(self.brief)
        self.audits.append(prompt)
        if self.boom:
            raise self.boom
        if len(self.audits) <= self.bad_replies:
            raise json.JSONDecodeError("Expecting value", "", 0)  # the model's reply was not JSON
        verdicts = []
        for m in re.finditer(r"=== GROUP (\d+) ===\nEVIDENCE:\n<<<\n(.*?)\n>>>\nSTATEMENTS:\n(.*?)(?=\n\n=== GROUP|\n\nReply with ONLY|\Z)",
                             prompt, re.S):
            rows = [(int(a), b) for a, b in re.findall(r"^\[(\d+)\] (.*)$", m[3], re.M)]
            if self.skip:
                rows = self.skip(rows, len(self.audits))
            verdicts += [{"g": int(m[1]), "s": s, **self.decide(text, m[2])} for s, text in rows]
        return {"verdicts": verdicts}


def run_with(model, fn):
    saved = llm.ask_json
    llm.ask_json = model
    try:
        return fn()
    finally:
        llm.ask_json = saved


def brief(**kw):
    base = {"hook": "", "beats": [], "on_screen_text": [], "verified_facts": [], "claims_to_verify": [], "cta": "Follow"}
    return {**base, **kw}


def removed_reasons(result):
    return " | ".join(f"{r['text']} => {r['reason']}" for r in result["audit"]["removed"])


def check_invented_implementation_details():
    """The Decisions API case, and the class it belongs to."""
    written = brief(
        hook="OpenAI's Decisions API makes responsive apps easy.",
        beats=[{"say": "Today I'm testing OpenAI's Decisions API.", "show": "Creator at a desk."},
               {"say": "OpenAI says it offers fast, focused, low-cost decisions for any application.", "show": "The OpenAI post."},
               {"say": "It is a simple HTTP call that returns a decision in milliseconds.", "show": "A terminal running curl."},
               {"say": "You send JSON and get back a score with a confidence of 0.92.", "show": "The JSON response."},
               {"say": "Next, I'll check how it behaves in a small app.", "show": "A small app."}],
        on_screen_text=["Decisions API", "HTTP POST -> decision in ms", "Rate limit: 100 requests per minute"],
        verified_facts=["OpenAI's post is titled: Build more responsive apps with the Decisions API",
                        "The API returns results in under 50 ms"])
    model = Model(written)
    got = run_with(model, lambda: scripts.build_script(DECISIONS, "key"))
    text = " ".join(b["say"] for b in got["beats"]) + " " + " ".join(got["on_screen_text"])
    for invented in ("HTTP", "milliseconds", "JSON", "0.92", "ms", "100 requests"):
        assert invented not in text, f"{invented!r} survived: {text}"
    assert "OpenAI says it offers fast, focused, low-cost decisions" in text, "the supported statement is kept"
    assert "Next, I'll check how it behaves" in text, "a plan is kept"
    reasons = removed_reasons(got)
    assert "details not in the evidence" in reasons and "testing" in reasons, reasons
    assert got["audit"]["status"] == "trimmed" and len(got["beats"]) == 2
    assert "Decisions API" in got["on_screen_text"] and len(got["on_screen_text"]) == 1
    assert got["verified_facts"] == ["OpenAI's post is titled: Build more responsive apps with the Decisions API"]
    assert "The API returns results in under 50 ms" in got["claims_to_verify"], "an unsupported 'fact' becomes a claim to check"
    assert got["hook"].startswith("OpenAI says:") and "Decisions API" in got["hook"], got["hook"]  # unattributed claim -> true headline
    assert len(model.audits) == 1, "one audit call covers the whole brief"


def check_vendor_benchmarks_and_superlatives():
    written = brief(
        hook="K3.1 Pro tops the Terminal-Bench leaderboard.",
        beats=[{"say": "K3.1 Pro tops the Terminal-Bench leaderboard with 65.7%.", "show": "The chart."},
               {"say": "Kimi_Moonshot says K3.1 Pro scores 65.7% on Terminal-Bench 4.0.", "show": "The post."},
               {"say": "It beats GPT-6 Astra.", "show": "The comparison."},
               {"say": "According to Kimi_Moonshot, it is ahead of GPT-6 Astra (58.2%).", "show": "The post."},
               {"say": "It scores 70.1% on a harder test.", "show": "The post."},
               {"say": "This is the best open-weight model for coding agents.", "show": "The post."},
               {"say": "Kimi_Moonshot calls it the best open-weight model for coding agents.", "show": "The post."},
               {"say": "It is also the fastest model ever shipped.", "show": "The post."}])
    scripts.MAX_BEATS, saved_max = 20, scripts.MAX_BEATS  # this test packs in more beats than a real brief has
    try:
        got = run_with(Model(written), lambda: scripts.build_script(KIMI, "key"))
    finally:
        scripts.MAX_BEATS = saved_max
    said = [b["say"] for b in got["beats"]]
    assert "Kimi_Moonshot says K3.1 Pro scores 65.7% on Terminal-Bench 4.0." in said, said
    assert "According to Kimi_Moonshot, it is ahead of GPT-6 Astra (58.2%)." in said
    assert "Kimi_Moonshot calls it the best open-weight model for coding agents." in said, "attributed superlative is allowed"
    gone = {r["text"]: r["reason"] for r in got["audit"]["removed"]}
    assert "must say who claims it" in gone["K3.1 Pro tops the Terminal-Bench leaderboard with 65.7%."], gone
    assert "must say who claims it" in gone["It beats GPT-6 Astra."]
    assert "70.1" in gone["It scores 70.1% on a harder test."]
    assert "must say who" in gone["This is the best open-weight model for coding agents."], "an unattributed superlative"
    assert "fastest" in gone["It is also the fastest model ever shipped."], "a hype word the sources never used"
    assert got["hook"].startswith("Kimi_Moonshot on X says:"), got["hook"]  # who, where, then the source's own headline


def check_false_first_person_experience():
    written = brief(
        beats=[{"say": "I built this roadmap myself over a weekend.", "show": "x"},
               {"say": "My roadmap has three videos.", "show": "x"},
               {"say": "Kimi_Moonshot says K3.1 Pro scores 65.7% on Terminal-Bench 4.0.", "show": "x"},
               {"say": "I'm launching a course on this.", "show": "x"},
               {"say": "Next I'll read the post and explain it.", "show": "x"}],
        on_screen_text=["I tested it for a week"])
    got = run_with(Model(written), lambda: scripts.build_script(KIMI, "key"))
    said = " ".join(b["say"] for b in got["beats"])
    for lie in ("I built", "My roadmap", "launching", "tested it"):
        assert lie not in said and not any(lie in t for t in got["on_screen_text"]), lie
    assert "Next I'll read the post" in said, "a plan to do something later is not a false claim of past experience"
    assert got["audit"]["removed"] and all("experience" in r["reason"] for r in got["audit"]["removed"] if "I " in r["text"]), \
        removed_reasons(got)


def check_thin_evidence_is_cautious_or_rejected():
    nothing = {**DECISIONS, "title": "Cool thing", "summary": "", "evidence": [{**DECISIONS["evidence"][0], "title": "Cool thing"}]}
    model = Model(brief(beats=[{"say": "x", "show": "y"}]))
    got = run_with(model, lambda: scripts.build_script(nothing, "key"))
    assert "rejected" in got and model.writing == [] and model.audits == [], "no model call when there is nothing to write from"

    model = Model(brief(hook="h", beats=[{"say": "Next I'll look at it.", "show": "a"}, {"say": "Then I'll explain it.", "show": "b"}]))
    got = run_with(model, lambda: scripts.build_script(DECISIONS, "key"))
    assert scripts.THIN_NOTE in model.writing[0], "short evidence gets the cautious instruction"
    long = {**DECISIONS, "summary": DECISIONS["summary"] + " " + "It is described in OpenAI's launch post for developers. " * 3}
    model = Model(brief(hook="h", beats=[{"say": "Next I'll look at it.", "show": "a"}]))
    run_with(model, lambda: scripts.build_script(long, "key"))
    assert scripts.THIN_NOTE not in model.writing[0]

    # nothing supportable survives -> rejected, not a hollow brief
    model = Model(brief(beats=[{"say": "It is a simple HTTP call.", "show": "x"}, {"say": "It returns in milliseconds.", "show": "x"},
                               {"say": "I built an app with it.", "show": "x"}]))
    got = run_with(model, lambda: scripts.build_script(DECISIONS, "key"))
    assert got["audit"]["status"] == "rejected" and "not enough to film" in got["rejected"], got["audit"]


def check_audit_failures_are_not_hidden():
    written = brief(hook="OpenAI built a thing.", beats=[{"say": "It is simple.", "show": "x"}, {"say": "It is fast.", "show": "y"}])
    got = run_with(Model(written, boom=TimeoutError()), lambda: scripts.build_script(DECISIONS, "key"))
    assert got["audit"]["status"] == "unaudited" and any("NOT AUDITED" in w for w in got["warnings"]), got["audit"]

    # statements the model skips are asked about again; if still skipped they are removed, never assumed fine
    def skip_first_time(rows, call):
        return rows[:1] if call == 1 else rows
    model = Model(written, skip=skip_first_time)
    got = run_with(model, lambda: scripts.build_script(DECISIONS, "key"))
    assert len(model.audits) == 2, "one retry for the skipped statements"
    assert got["audit"]["statements"] >= 3

    model = Model(written, skip=lambda rows, call: rows[:1])
    got = run_with(model, lambda: scripts.build_script(DECISIONS, "key"))
    assert any("no verdict" in r["reason"] for r in got["audit"]["removed"]), removed_reasons(got)
    assert len(model.audits) == verify.MAX_CALLS, "bounded: asked at most MAX_CALLS times, not forever"


def check_the_quote_must_be_real():
    written = brief(hook="x", beats=[{"say": "OpenAI says it is a decision engine for apps.", "show": "a"},
                                     {"say": "Next I'll check it.", "show": "b"}, {"say": "Then I'll compare.", "show": "c"}])

    def fabricated(text, evidence):
        return {**lenient(text, evidence), "quote": "OpenAI says it is a decision engine for apps"}  # not in the evidence
    got = run_with(Model(written, decide=fabricated), lambda: scripts.build_script(DECISIONS, "key"))
    assert "no passage of the evidence supports it" in removed_reasons(got)

    def unrelated(text, evidence):
        return {**lenient(text, evidence), "quote": "Fast, focused, low-cost decisions for any application"}
    wrong = brief(hook="x", beats=[{"say": "The weather forecast model predicts rain across Europe tomorrow.", "show": "a"},
                                   {"say": "Next I'll check it.", "show": "b"}, {"say": "Then I'll compare.", "show": "c"}])
    got = run_with(Model(wrong, decide=unrelated), lambda: scripts.build_script(DECISIONS, "key"))
    assert "about something else" in removed_reasons(got) or "no passage" in removed_reasons(got), removed_reasons(got)
    assert verify.quote_in("Fast, focused, low-cost decisions for any application", verify.evidence_text(DECISIONS))
    assert not verify.quote_in("fast", verify.evidence_text(DECISIONS)), "a stub is not a quote"


def check_hooks_are_audited():
    def idea(hook, base=DECISIONS):
        return {**base, "hook": hook, "warnings": []}
    ok = idea("OpenAI says the Decisions API offers fast, focused, low-cost decisions.")
    invented = idea("Build faster apps with an HTTP API that answers in milliseconds.")
    unattributed = idea("Kimi K3.1 Pro tops the Terminal-Bench leaderboard.", KIMI)
    attributed = idea("Kimi_Moonshot says K3.1 Pro scores 65.7% on Terminal-Bench 4.0.", KIMI)
    hype = idea("OpenAI's brand-new Decisions API is here.")
    ideas = [ok, invented, unattributed, attributed, hype]
    model = Model()
    run_with(model, lambda: verify.audit_hooks(ideas, "key"))
    assert [i["hook"] != "" for i in ideas] == [True, False, False, True, False], [i["hook"] for i in ideas]
    assert ok["hook_audit"] == "verified" and invented["hook_audit"] == "dropped"
    assert "details not in the evidence" in invented["warnings"][0] and "must say who claims it" in unattributed["warnings"][0]
    assert len(model.audits) == 1, "all hooks audited in one call"

    # no audit, no hook: an unchecked hook is never passed off as fine
    ideas = [idea("OpenAI says the Decisions API offers fast, focused, low-cost decisions.")]
    run_with(Model(boom=TimeoutError()), lambda: verify.audit_hooks(ideas, "key"))
    assert ideas[0]["hook"] == "" and ideas[0]["hook_audit"] == "unaudited" and "could not be audited" in ideas[0]["warnings"][0]


def check_plans_do_not_become_facts():
    """Live run A: the source said OpenAI models are 'coming to Google Cloud'; a hook said 'now available'."""
    coming = {**DECISIONS, "title": "OpenAI models are coming to Google Cloud",
              "summary": "OpenAI and Google Cloud are working together so customers can use OpenAI models in Vertex AI.",
              "evidence": [{**DECISIONS["evidence"][0], "title": "OpenAI models are coming to Google Cloud"}], "warnings": []}
    ev = verify.evidence_text(coming)
    now = "OpenAI models are now available on Google Cloud Marketplace."
    ok, why = verify.judge(now, lenient(now, ev), ev, names=[])
    assert not ok and "coming" in why, why  # even though the (lenient) model said it was supported
    plan = "OpenAI says its models are coming to Google Cloud."
    assert verify.judge(plan, lenient(plan, ev), ev, names=["OpenAI"])[0], "saying 'coming' is accurate"
    live = "OpenAI models are available on Google Cloud."
    assert verify.judge(live, lenient(live, "OpenAI models are available on Google Cloud for customers today."),
                        "OpenAI models are available on Google Cloud for customers today.", names=[])[0], "no hedge in the source"
    # hooks go through the same rule
    ideas = [{**coming, "hook": now}]
    run_with(Model(), lambda: verify.audit_hooks(ideas, "key"))
    assert ideas[0]["hook"] == "" and "coming" in ideas[0]["warnings"][0], ideas[0]["warnings"]
    # "The post mentions ..." is attributed
    ev2 = "AT&T's team is rethinking how legal work is taken in and how legal judgment scales with OpenAI."
    s = "The post mentions AT&T's team is rethinking how legal judgment scales."
    assert verify.judge(s, {**lenient(s, ev2), "kind": "claim"}, ev2, names=[])[0]


def check_audit_survives_bad_replies():
    """Live run A2: one brief came back UNAUDITED because a single model reply was not JSON."""
    written = brief(hook="x", beats=[{"say": "Next I'll check it.", "show": "a"}, {"say": "Then I'll compare.", "show": "b"}])
    model = Model(written, bad_replies=1)
    got = run_with(model, lambda: scripts.build_script(DECISIONS, "key"))
    assert got["audit"]["status"] != "unaudited" and len(model.audits) == 2, "one bad reply is retried"
    model = Model(written, bad_replies=9)
    got = run_with(model, lambda: scripts.build_script(DECISIONS, "key"))
    assert got["audit"]["status"] == "unaudited" and got["audit"]["reason"] == "JSONDecodeError", got["audit"]
    assert len(model.audits) == verify.MAX_CALLS, "bounded: three calls, then it says so"
    assert any("NOT AUDITED" in w for w in got["warnings"])


def check_numbers_keep_their_meaning():
    """Live run A2: the source said '94% less time'; the brief said '94% faster'. The digits matched, the meaning did not."""
    ev = ("Franchisee reconciles cash in 94% less time with ChatGPT Work. An independent franchisee cut menu repricing time "
          "by 95% and built a workflow that could free up $100K in cash flow.")
    s = "It also says they could reconcile cash 94% faster."
    ok, why = verify.judge(s, lenient(s, ev), ev, names=["OpenAI"])
    assert not ok and "94%" in why and "other way" in why, why
    same = "OpenAI says the franchisee reconciles cash in 94% less time."
    assert verify.judge(same, lenient(same, ev), ev, names=["OpenAI"])[0]
    cut = "OpenAI says a franchisee cut menu repricing time by 95%."
    assert verify.judge(cut, lenient(cut, ev), ev, names=["OpenAI"])[0], "a figure with mixed words around it is not flagged"
    assert verify.reversed_numbers("Costs fell 40%.", "Costs rose 40% last year.") == ["40"]
    assert verify.reversed_numbers("Costs fell 40%.", "Something else entirely.") == []

    # a question that carries a figure is a claim, not a plan
    q = "What if AI could cut your business tasks by 95%?"
    ok, why = verify.judge(q, lenient(q, ev), ev, names=[])
    assert not ok, "removed whichever rule fires first: unsupported wording, or an unattributed claim"
    q2 = "What if the franchisee's result, a 95% cut in menu repricing time, was yours?"
    ok2, why2 = verify.judge(q2, lenient(q2, ev), ev, names=[])
    assert not ok2 and "must say who claims it" in why2, why2  # fully grounded, so only attribution is missing
    assert verify.judge("What if you could try it on a small task?", lenient("What if you could try it on a small task?", ev), ev)[0]


def check_hooks_do_not_speak_for_the_creator():
    """Live run A2: 'We modeled 30% of Git in Lean 4' was a hook about someone else's project."""
    git = {"title": "Small announcement: formal-git", "claim_status": "unverified", "needs_verification": False,
           "summary": "As an experiment, over the last 2 months we've modelled ~30% of Git in Lean 4, making heavy use of coding agents.",
           "evidence": [{"source": "X search", "platform": "X", "author": "someone", "url": "https://x.com/i/status/1",
                         "title": "Small announcement: formal-git"}], "warnings": []}
    for hook in ("We modeled 30% of Git in Lean 4 using coding agents.", "I'm launching a Git proof project.",
                 "Our team modelled Git in Lean 4."):
        ideas = [{**git, "hook": hook, "warnings": []}]
        run_with(Model(), lambda: verify.audit_hooks(ideas, "key"))
        assert ideas[0]["hook"] == "" and hook[:25] in ideas[0]["warnings"][0], (hook, ideas[0]["warnings"])
    fine = [{**git, "hook": "A post by someone says they modelled about 30% of Git in Lean 4 using coding agents.", "warnings": []}]
    run_with(Model(), lambda: verify.audit_hooks(fine, "key"))
    assert fine[0]["hook"] and fine[0]["hook_audit"] == "verified", fine[0]["warnings"]


def check_a_statement_may_combine_two_passages():
    """Dropping 7 of 10 hooks was too strict: support is checked against all the evidence, not one fragment."""
    ev = "Acme released Foo. Foo runs on a laptop. Foo is open source and free to use."
    s = "Acme released Foo, an open source tool that runs on a laptop."
    verdict = {"kind": "fact", "quote": "Foo runs on a laptop", "attributed": False, "reason": ""}
    assert verify.judge(s, verdict, ev)[0]
    # but a quote about something else, or a statement the evidence mostly lacks, still fails
    assert not verify.judge("The weather forecast predicts heavy rain across Europe tomorrow.", verdict, ev)[0]
    assert not verify.judge("Acme released Foo with built-in blockchain payments and telemetry dashboards.", verdict, ev)[0]



def check_short_figures_and_cited_links():
    """Live replay: '88.6% match' was rejected because the model's quote '88.6%' is short; a cited status id was 'a number'."""
    ev = "Terminal X matches senior investor judgment 88.6% with GPT-6.1. Terminal X's Eval Agent grades outputs."
    figure = {"kind": "fact", "quote": "88.6%", "attributed": False, "reason": ""}
    assert verify.judge("88.6% match", figure, ev)[0], "a short quote is fine when it is a figure"
    assert not verify.judge("Terminal X is fast", {**figure, "quote": "fast"}, ev)[0], "but a short non-figure quote is a stub"
    assert not verify.quote_in("it", ev) and not verify.quote_in("88", "no digits here 5")
    fact = "OpenAI says Terminal X matches senior investor judgment 88.6% (https://x.com/i/status/2113977142421725535)"
    assert verify.judge(fact, lenient(fact, ev), ev, names=["OpenAI"])[0], "the cited link's id is not a claimed number"
    wrong = "Terminal X matches investor judgment 91.2% of the time"
    assert not verify.judge(wrong, lenient(wrong, ev), ev)[0], "an invented figure is still caught"


def check_comparisons_are_claims():
    """Live run C: a hook said 'running models on Trainium is now easier'; the source (the vendor's blog) said 'easier than ever'."""
    ev = "TorchNeuron: running HF models on AWS Trainium is easier than ever"
    as_fact = {"kind": "fact", "quote": ev, "attributed": False, "reason": ""}  # the model called it a plain fact
    s = "Running Hugging Face models on AWS Trainium is now easier with TorchNeuron."
    ok, why = verify.judge(s, as_fact, ev, names=[])
    assert not ok and "must say who claims it" in why, why
    said = "The Hugging Face blog says running HF models on AWS Trainium is easier than ever."
    assert verify.judge(said, as_fact, ev, names=[])[0], "attributed to its source, it is fine"
    ideas = [{"title": ev, "summary": "", "claim_status": "first_party", "needs_verification": False, "hook": s, "warnings": [],
              "evidence": [{"source": "Hugging Face blog", "platform": "Hugging Face blog", "author": None, "url": "https://huggingface.co/blog/x",
                            "title": ev}]}]
    run_with(Model(decide=lambda text, evidence: {**lenient(text, evidence), "kind": "fact"}), lambda: verify.audit_hooks(ideas, "key"))
    assert ideas[0]["hook"] == "", "a vendor's comparison is not stated as fact in a hook"
    ev2 = "OpenAI acquires Orchestra Bio. OpenAI is acquiring Orchestra Bio to bring AI-powered operations to life sciences."
    plain = "OpenAI is acquiring Orchestra Bio."
    fact = {"kind": "fact", "quote": "OpenAI is acquiring Orchestra Bio", "attributed": False, "reason": ""}
    assert verify.judge(plain, fact, ev2, names=[])[0], "a plain fact about what happened needs no 'says'"


def story(title, summary="", status="unverified", author=None, source="X search", platform="X"):
    return {"title": title, "summary": summary, "claim_status": status, "needs_verification": False, "format": "tool walkthrough",
            "hook": "", "warnings": [],
            "evidence": [{"source": source, "platform": platform, "author": author, "url": "https://x.com/i/status/9", "title": title}]}


def check_third_party_claims_stay_attributed():
    """Validation report issue 2: a tweet's own 'works the night shift' was a hook, stated flatly, because the source said it."""
    tweet = story("Build an absolute monster. Merge six Claude Code pieces into one system, it works the NIGHT SHIFT while you sleep",
                  "Opus 5.5 + subagents + routines + web search + Playwright + CLAUDE.md. Six pieces. One system.", author="builder99")
    ev = verify.evidence_text(tweet)
    flat = "Six Claude Code pieces combined into one night-shift AI worker."
    said = "builder99 says six Claude Code pieces merge into one system that works the night shift."
    ok, why = verify.judge(flat, lenient(flat, ev), ev, names=["builder99", "X search"], third_party=True)
    assert not ok and "third party's claim" in why, why
    assert verify.judge(said, lenient(said, ev), ev, names=["builder99", "X search"], third_party=True)[0]
    assert verify.judge("A post on X claims six Claude Code pieces merge into one system.", lenient("x", ev), ev, names=[], third_party=True)[0]
    assert verify.judge(flat, lenient(flat, ev), ev, names=[], third_party=False)[0], "the organisation's own post may state its own news"
    plan = "Next I'll read the post and explain each piece."
    assert verify.judge(plan, lenient(plan, ev), ev, names=[], third_party=True)[0], "what the creator will do needs no attribution"

    # the hook: dropped, and the replacement is the source's own headline, credited
    ideas = [{**tweet, "hook": flat}]
    run_with(Model(), lambda: verify.audit_hooks(ideas, "key"))
    assert ideas[0]["hook"] == "" and "say who says it" in ideas[0]["warnings"][0], ideas[0]["warnings"]
    assert verify.fallback_hook(tweet).startswith("builder99 on X says: Build an absolute monster")

    # the script: spoken sentences must name the source; on-screen text and shot notes sit under the credit line
    written = brief(hook="Want a coding system that never sleeps?",
                    beats=[{"say": "Six Claude Code pieces make one system that works the night shift.", "show": "The post."},
                           {"say": "builder99 says the system works the night shift while you sleep.", "show": "The post."},
                           {"say": "Next I'll read the post and explain each piece.", "show": "A notes app."},
                           {"say": "Then I'll try to check the claim myself.", "show": "A terminal."}],
                    on_screen_text=["Six pieces. One system."],
                    verified_facts=["Six Claude Code pieces merge into one system.", "builder99 posted that six pieces make one system."])
    got = run_with(Model(written), lambda: scripts.build_script(tweet, "key"))
    said_beats = [b["say"] for b in got["beats"]]
    assert "Six Claude Code pieces make one system that works the night shift." not in said_beats, said_beats
    assert "builder99 says the system works the night shift while you sleep." in said_beats
    assert got["on_screen_text"] == ["Six pieces. One system."]
    assert got["verified_facts"] == ["builder99 posted that six pieces make one system."]
    assert "Six Claude Code pieces merge into one system." in got["claims_to_verify"], "the flat version becomes a claim to check"
    assert any("third party's claim" in r["reason"] for r in got["audit"]["removed"]), removed_reasons(got)
    # and a first-party story is not forced to say 'OpenAI says' on every sentence
    own = story("OpenAI acquires Orchestra Bio", "OpenAI is acquiring Orchestra Bio to bring AI-powered operations to life sciences.",
                "first_party", None, "OpenAI", "OpenAI")
    own_ev = verify.evidence_text(own)
    s = "OpenAI is acquiring Orchestra Bio."
    assert verify.judge(s, lenient(s, own_ev), own_ev, names=["OpenAI"], third_party=verify._third_party(own))[0]


def check_plausible_invented_characterizations_are_flagged():
    """Validation report issue 3: invented detail made of ordinary words that no rule names."""
    ev = "Patch the Planet connects security researchers, maintainers, and incident responders to help address critical vulnerabilities across open source."
    fact = {"kind": "fact", "quote": ev, "attributed": False, "reason": "", "unsupported_details": []}  # a model that blessed it
    invented = "Patch the Planet connects security researchers, maintainers and responders through a shared dashboard with automated severity scoring."
    ok, why = verify.judge(invented, fact, ev)
    assert not ok and "adds details the sources do not give" in why and "dashboard" in why, why
    faithful = "Patch the Planet connects security researchers, maintainers, and incident responders."
    assert verify.judge(faithful, fact, ev)[0]

    # one new word is under the bar for the wording check; the model's own flag is what catches it
    one = "Patch the Planet connects security researchers and maintainers via a dashboard."
    assert verify.judge(one, fact, ev)[0], "one new word alone does not trip the wording check"
    flagged = {**fact, "unsupported_details": ["a dashboard"]}
    ok, why = verify.judge(one, flagged, ev)
    assert not ok and "a dashboard" in why, why
    # the model cannot flag what is not there, or what the sources did say
    assert verify.judge(one, {**fact, "unsupported_details": ["a mobile app"]}, ev)[0], "a phrase not in the statement is ignored"
    assert verify.judge(faithful, {**fact, "unsupported_details": ["security researchers"]}, ev)[0], "sourced words cannot be flagged"
    assert verify.added_details(faithful, None, ev) == [] and verify.added_details(faithful, {"unsupported_details": None}, ev) == []

    # spelling out an abbreviation, or naming the source, is not invention
    ev2 = "TorchNeuron: running HF models on AWS Trainium is easier than ever"
    s = "The Hugging Face blog says running Hugging Face models on AWS Trainium is easier than ever."
    assert verify.judge(s, lenient(s, ev2), ev2, names=[])[0]

    # through the whole brief: the made-up sentence goes, the faithful one stays
    idea = story("Patch the Planet: global response coordination as agents uncover critical vulnerabilities", ev, "first_party", None, "OpenAI", "OpenAI")
    written = brief(beats=[{"say": invented, "show": "The page."}, {"say": "OpenAI says " + faithful[0].lower() + faithful[1:], "show": "The page."},
                           {"say": "Next I'll read the page.", "show": "x"}])
    got = run_with(Model(written), lambda: scripts.build_script(idea, "key"))
    assert all("dashboard" not in b["say"] for b in got["beats"]) and any("incident responders" in b["say"] for b in got["beats"]), got["beats"]


def check_thin_evidence_ends_in_a_cautious_brief_or_rejection():
    """Validation report: 'What FDE means at OpenAI' had only a title; every definition in the draft came from the model."""
    fde = story("What FDE means at OpenAI", "What forward deployed engineering means at OpenAI", "first_party", None, "OpenAI", "OpenAI")
    invented = brief(beats=[
        {"say": "OpenAI's own post says Forward-Deployed Engineering is their term for engineers who bring research models into production and work directly with customers.", "show": "The post."},
        {"say": "The post also notes FDEs collaborate across teams to adapt models, handle scaling, and ensure safety.", "show": "The post."},
        {"say": "Next, I'll look at what tools an FDE might use.", "show": "A laptop."},
        {"say": "Then I'll compare it with a regular engineering role.", "show": "A chart."}],
        verified_facts=["OpenAI's post says FDEs adapt models and handle scaling."])
    model = Model(invented)
    got = run_with(model, lambda: scripts.build_script(fde, "key"))
    assert scripts.THIN_NOTE in model.writing[0], "the writing prompt told the model to stay cautious"
    assert got["audit"]["status"] == "rejected" and "only the creator's own plans" in got["rejected"], (got["audit"], got.get("rejected"))
    assert any("FDEs" in r["text"] for r in got["audit"]["removed"]), removed_reasons(got)
    assert "OpenAI's post says FDEs adapt models and handle scaling." in got["claims_to_verify"]

    # one supported, attributed statement is enough to keep a short, cautious brief
    modest = brief(beats=[{"say": "OpenAI published a post titled “What FDE means at OpenAI”.", "show": "The post."},
                          {"say": "Next, I'll look at what tools an FDE might use.", "show": "A laptop."},
                          {"say": "Then I'll compare it with a regular engineering role.", "show": "A chart."}])
    got = run_with(Model(modest), lambda: scripts.build_script(fde, "key"))
    assert got["audit"]["status"] in ("verified", "trimmed") and len(got["beats"]) == 3, (got["audit"], removed_reasons(got))
    # nothing to write from at all
    nothing = story("Cool thing", "", "unverified", "x1")
    assert "rejected" in run_with(Model(modest), lambda: scripts.build_script(nothing, "key"))


def check_the_rules_are_in_the_prompts():
    audit_prompt = " ".join(verify.AUDIT_PROMPT.split())
    for phrase in ("unsupported_details", "Plausible is not supported"):
        assert phrase in audit_prompt, phrase
    assert "unsupported_details" in verify.AUDIT_SCHEMA["properties"]["verdicts"]["items"]["required"]
    for phrase in ("every factual sentence must say who says it", "a short, cautious brief beats a plausible-sounding one"):
        assert phrase in " ".join(scripts.SCRIPT_PROMPT.split()), phrase
    from agent.discovery import rank
    assert "must say who says it" in " ".join(rank.RANK_PROMPT.split())


def check_a_cue_word_or_a_nested_source_is_not_attribution():
    """Live run E, idea 7: 'A Harvard study finds AI agents write 30% more code ...' passed as attributed because 'write' is a cue verb.
    The sentence rests on a dev.to article's mention of a study; it must credit the article's author, not a study nobody here has read."""
    idea = story("926 Steps, Two Lobotomies, and Zero Nudges: Flight-Testing the Commitment Machine",
                 "A 2026 Harvard study of 718 companies found that AI agents write 30% more code and close zero additional tickets "
                 "because review times jump 49%.", "unverified", "randalschwartz", "dev.to", "dev.to")
    ev = verify.evidence_text(idea)
    names = verify._names(idea)
    flat = "A Harvard study finds AI agents write 30% more code but don't close extra tickets, review times jump 49%."
    ok, why = verify.judge(flat, lenient(flat, ev), ev, names=names, third_party=True)
    assert not ok and "who" in why, why  # whichever attribution rule fires first (it also carries a percentage)
    for good in ("randalschwartz says a 2026 Harvard study found that AI agents write 30% more code.",
                 "A dev.to article claims a 2026 Harvard study found that AI agents write 30% more code.",
                 "According to the article, AI agents write 30% more code and close zero additional tickets."):
        assert verify.judge(good, lenient(good, ev), ev, names=names, third_party=True)[0], good
    # the pieces on their own
    assert not verify.is_attributed("AI agents write more code than they review.", ["dev.to"])
    assert not verify.is_attributed("The report says AI agents write more code.", [])  # 'the report' is not our source
    assert verify.is_attributed("The post says AI agents write more code.", [])
    assert verify.is_attributed("randalschwartz wrote that agents write more code.", ["randalschwartz"])
    assert not verify.is_attributed("Six pieces make one system, says nobody.", ["X"]), "a one-letter name is not matched inside words"
    assert verify.is_attributed("A thread on X claims six pieces make one system.", [])
    # a news item's publisher is a name
    news = story("AI coding agents hijacked using AGENTS.md file in Downloads folder", "BleepingComputer", "unverified", None,
                 "Google News", "Google News")
    assert "BleepingComputer" in verify._names(news)
    s = "BleepingComputer reports AI coding agents were hijacked via an AGENTS.md file in the Downloads folder."
    news_ev = verify.evidence_text(news)
    assert verify.judge(s, lenient(s, news_ev), news_ev, names=verify._names(news), third_party=True)[0]
    # through the hook audit, with the exact words
    ideas = [{**idea, "hook": flat}]
    run_with(Model(), lambda: verify.audit_hooks(ideas, "key"))
    assert ideas[0]["hook"] == "" and "A Harvard study finds" in ideas[0]["warnings"][0], ideas[0]["warnings"]
    assert verify.fallback_hook(idea).startswith("randalschwartz on dev.to says:")


def check_a_plan_cannot_carry_a_product_claim():
    """Live run E: two beats were labelled plans, so nothing checked them, yet each asserts something the source never says."""
    ev = ("Uber brings ChatGPT Work to all employees with OpenAI Uber is rolling out ChatGPT Work to more than 30,000 employees, "
          "helping teams research, analyze data, create presentations, and complete complex workflows.")
    plan = {"kind": "plan", "quote": "", "attributed": False, "reason": "", "unsupported_details": []}
    smuggled = ["First, I open the ChatGPT Work portal that Uber's internal rollout uses, showing the dashboard that lets you start a new chat or project.",
                "Hey creators, today I'm walking through the tool that Uber just announced – ChatGPT Work – and how it could be a stepping stone from vibe coding to real AI engineering.",
                "Let's see how that works: I'll open the new app that just launched in Teams."]
    for s in smuggled:
        ok, why = verify.judge(s, plan, ev, names=["OpenAI", "Uber"])
        assert not ok, s
    for honest in ("Next, I'll try ChatGPT Work on a small task and report back.", "Let's see how it handles a research question.",
                   "Then I'll compare it with a regular engineering role.", "First, I'll read the OpenAI post."):
        assert verify.judge(honest, plan, ev, names=["OpenAI"])[0], honest
    # the model's own flag applies to plans too, but only for phrases that are really there
    s = "Next I'll open the settings panel where policies live."
    assert verify.judge(s, plan, ev)[0]
    ok, why = verify.judge(s, {**plan, "unsupported_details": ["the settings panel where policies live"]}, ev)
    assert not ok and "a plan that asserts what the sources do not say" in why, why
    assert verify.judge(s, {**plan, "unsupported_details": ["a mobile app"]}, ev)[0]
    assert verify.judge("Next I'll try ChatGPT Work on a small task.", {**plan, "unsupported_details": ["ChatGPT Work"]}, ev)[0], "sourced words"

    uber = story("Uber brings ChatGPT Work to all employees with OpenAI", "Uber is rolling out ChatGPT Work to more than 30,000 employees, "
                 "helping teams research, analyze data, create presentations, and complete complex workflows.", "first_party", None, "OpenAI", "OpenAI")
    written = brief(beats=[{"say": smuggled[0], "show": "The post."}, {"say": smuggled[1], "show": "The post."},
                           {"say": "OpenAI says Uber is rolling out ChatGPT Work to more than 30,000 employees.", "show": "The post."},
                           {"say": "Next, I'll try ChatGPT Work on a small task and report back.", "show": "A laptop."},
                           {"say": "Then I'll compare it with a regular engineering role.", "show": "A chart."}])
    got = run_with(Model(written), lambda: scripts.build_script(uber, "key"))
    kept = " ".join(b["say"] for b in got["beats"])
    assert "dashboard" not in kept and "just announced" not in kept, kept
    assert "more than 30,000 employees" in kept and "Next, I'll try ChatGPT Work" in kept
    assert got["audit"]["status"] == "trimmed" and len(got["audit"]["removed"]) >= 2, removed_reasons(got)


def check_judge_rules_directly():
    ev = verify.evidence_text(DECISIONS)
    lenient_ok = lenient("OpenAI says it is fast.", ev)
    assert verify.judge("OpenAI says it is fast.", lenient_ok, ev, names=["OpenAI"])[0]
    assert not verify.judge("OpenAI says it is fast.", lenient_ok, ev, names=[])[0], "a cue with no source behind it is not attribution"
    assert not verify.judge("It is fast.", {**lenient_ok, "kind": "claim"}, ev, names=[])[0], "unattributed claim"
    assert verify.judge("It is fast.", {**lenient_ok, "kind": "claim"}, ev, names=["OpenAI"])[0] is False, "name not in the text"
    assert not verify.judge("OpenAI is fast.", {**lenient_ok, "kind": "claim"}, ev, names=["OpenAI"])[0], "a name with no cue is a statement"
    assert verify.judge("The blog says it is fast.", {**lenient_ok, "kind": "claim"}, ev, names=[])[0], "'the blog' points at the source"
    assert not verify.judge("It is fast.", {"kind": "bogus"}, ev)[0] and not verify.judge("It is fast.", None, ev)[0]
    assert verify.ungrounded_specifics("It costs 5 dollars and uses a GPU in 3 seconds", ev) == ["3", "5", "GPU", "dollars", "seconds"]
    assert verify.ungrounded_specifics("In 3 seconds I will show the AI", ev, numbers=False) == [], "a plan may name its own timing"
    assert verify.split("One. Two! Three? four") == ["One.", "Two!", "Three? four"]
    quoted = 'I will cover the video “Forget Claude. This Is What Google’s New AI Can Do (Gemini 4 Argon)”. He says it is good.'
    assert verify.split(quoted) == ['I will cover the video “Forget Claude. This Is What Google’s New AI Can Do (Gemini 4 Argon)”.',
                                    "He says it is good."], verify.split(quoted)
    assert verify.split('He said "Wait. Stop." Then left.') == ['He said "Wait. Stop."', "Then left."]


if __name__ == "__main__":
    check_judge_rules_directly()
    check_plans_do_not_become_facts()
    check_a_cue_word_or_a_nested_source_is_not_attribution()
    check_a_plan_cannot_carry_a_product_claim()
    check_third_party_claims_stay_attributed()
    check_plausible_invented_characterizations_are_flagged()
    check_thin_evidence_ends_in_a_cautious_brief_or_rejection()
    check_the_rules_are_in_the_prompts()
    check_comparisons_are_claims()
    check_short_figures_and_cited_links()
    check_audit_survives_bad_replies()
    check_numbers_keep_their_meaning()
    check_hooks_do_not_speak_for_the_creator()
    check_a_statement_may_combine_two_passages()
    check_invented_implementation_details()
    check_vendor_benchmarks_and_superlatives()
    check_false_first_person_experience()
    check_thin_evidence_is_cautious_or_rejected()
    check_audit_failures_are_not_hidden()
    check_the_quote_must_be_real()
    check_hooks_are_audited()
    print("verify tests passed")
