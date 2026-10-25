"""Checks on model-written text (hooks, scripts) against the evidence it was written from. Plain regexes, no model.

The model is told not to hype or to claim the creator's own experience, and sometimes does anyway. These checks
catch it afterwards, so the prompt is not the only defence. They flag; nothing here rewrites the text.
"""

import re

# Hype words. Fine when the source itself uses the word, unsupported when only the model does.
SUPERLATIVE_RE = re.compile(
    r"\b(brand[- ]new|ultimate|revolutionary|groundbreaking|game[- ]chang\w+|incredible|unbelievable|mind[- ]blowing"
    r"|insane|best ever|the best|most powerful|biggest|fastest|once and for all)\b", re.IGNORECASE)

# The creator has not done anything yet; a brief is a plan. "I built", "my roadmap" claim a past they do not have.
EXPERIENCE_RE = re.compile(
    r"\b(?:i|i've|i'm|i have|we|we've)\s+(?:already\s+|just\s+|now\s+|also\s+)?(?:built|tested|tried|made|created"
    r"|put together|compiled|curated|wrote|cracked|mapped|watched|read|ran|used|am testing|testing|building|sharing"
    r"|dived|dove|dug into|studied|followed|explored|reviewed|spent|launching|launched|releasing|released"
    r"|announcing|announced|introducing|saw|noticed|came across|stumbled|discovered|found|modeled|modelled|proved"
    r"|trained|deployed|shipped)\b"
    r"|\bmy\s+(?:own\s+)?(?:roadmap|list|guide|course|framework|cheat ?sheet|pdf|setup|workflow|results)\b",
    re.IGNORECASE)


# Evaluative words: a judgement of quality or ease. Fine when the source says it, an invention when only the model does
# ("a simple HTTP call", "makes apps easy"). Unlike a superlative, a source that uses one does not need to be named.
EVALUATIVE_RE = re.compile(
    r"\b(simple|simply|easy|easily|effortless(?:ly)?|seamless(?:ly)?|powerful|amazing|impressive|stunning|massive|huge"
    r"|dramatic(?:ally)?|magic(?:al)?|blazing|lightning[- ]fast|instant(?:ly)?)\b", re.IGNORECASE)


# A one-line idea hook is about someone else's story. "We modeled 30% of Git" / "I'm launching" speaks for the creator.
FIRST_PERSON_RE = re.compile(r"\b(?:i|i'm|i've|i'll|i'd|we|we're|we've|we'll|our|my|me|us)\b", re.IGNORECASE)


def first_person(text):
    return [m.group(0) for m in FIRST_PERSON_RE.finditer(text or "")]


def _flat(text):
    return (text or "").lower().replace("-", " ")


def unsupported_superlatives(text, evidence):
    """Hype and evaluative words in `text` that `evidence` does not contain."""
    support = _flat(evidence)
    found = (m.group(0) for rx in (SUPERLATIVE_RE, EVALUATIVE_RE) for m in rx.finditer(text or ""))
    return sorted({w for w in map(_flat, found) if w not in support})


def experience_claims(text):
    """First-person 'I built / my roadmap' phrases in `text`."""
    return [m.group(0) for m in EXPERIENCE_RE.finditer(text or "")]


if __name__ == "__main__":
    assert unsupported_superlatives("Try OpenAI's brand-new API", "A low-cost API") == ["brand new"]
    assert unsupported_superlatives("Try the brand-new API", "OpenAI's Brand-New API") == []
    assert unsupported_superlatives("Plain hook about agents", "") == []
    assert experience_claims("I'm already testing it, and my roadmap is below") == ["I'm already testing", "my roadmap"]
    assert experience_claims("Here is how the post's author explains it") == []
    print("guard self-check passed")
