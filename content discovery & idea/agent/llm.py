"""Groq model ids in one place.

Groq retires model ids without warning: llama-3.1-8b-instant and llama-3.3-70b-versatile
both vanished from the account, which made every draft come back empty (the run then
posted a bare og:image with no text). Naming them here means the next retirement is an
env var on Render, not a code change across four files.

Check what your key can actually reach:
    curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"
"""

import json
import os

from dotenv import load_dotenv

from agent.paths import ENV_FILE

load_dotenv(ENV_FILE, override=True)

FAST = os.getenv("GROQ_FAST_MODEL", "openai/gpt-oss-20b")    # ranking, tweets, query cleanup
DEEP = os.getenv("GROQ_DEEP_MODEL", "openai/gpt-oss-120b")   # LinkedIn long-form


def chat(model, api_key=None, **opts):
    from langchain_groq import ChatGroq  # lazy: keeps import cost off the non-LLM paths
    return ChatGroq(model=model, api_key=api_key or os.getenv("GROQ_API_KEY"), **opts)


def ask(model, prompt, api_key=None, **opts):
    """One prompt in, stripped text out. Every call site wanted exactly this.

    opts go to ChatGroq. The gpt-oss models reason before answering and that reasoning
    counts against max_tokens: a long prompt can burn the whole budget thinking and come
    back with empty content (finish_reason=length). For big classification prompts pass
    reasoning_effort="low" and a roomier max_tokens.
    """
    return chat(model, api_key, **opts).invoke(prompt).content.strip().strip('"')


def extract_json(text):
    """The JSON object inside a reply that may wrap it in prose or a code fence."""
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def ask_json(model, prompt, schema, api_key=None, **opts):
    """Prompt in, parsed JSON object out, shaped by `schema`.

    Uses Groq's structured outputs: the model can only emit JSON matching the schema, so
    there is no prose to dig through and no half-valid reply. Strict mode needs every
    property listed in "required" and "additionalProperties": false on every object.

    If the request itself is refused (a model without structured outputs, e.g. one set
    through GROQ_FAST_MODEL) or the reply still isn't JSON, fall back to a plain call and
    dig the object out. Rate limits and outages are not retried here: they propagate, so
    callers keep control of the retry budget.
    """
    fmt = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
    try:
        return json.loads(chat(model, api_key, **opts).bind(response_format=fmt).invoke(prompt).content)
    except Exception as e:
        if not (isinstance(e, ValueError) or "BadRequest" in type(e).__name__):
            raise
        print(f"Structured output unavailable ({type(e).__name__}); asking again as plain text.", flush=True)
        return extract_json(ask(model, prompt, api_key, **opts))


if __name__ == "__main__":
    key = os.getenv("GROQ_API_KEY")
    assert FAST and DEEP
    if key:  # live check that the configured ids still exist on this account
        for name in (FAST, DEEP):
            print(f"{name}: {ask(name, 'Reply with the single word OK.')[:40]}")
    else:
        print("No GROQ_API_KEY; ids configured:", FAST, DEEP)
