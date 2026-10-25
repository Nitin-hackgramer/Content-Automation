"""Tune the LinkedIn writer's rules with DSPy (GEPA). Run by hand, offline. Never on Render.

    uv run python scripts/optimize_linkedin.py --baseline     # just score the current rules (cheap)
    uv run python scripts/optimize_linkedin.py                # tune, compare, adopt only if better
    uv run python scripts/optimize_linkedin.py --budget 60    # let the optimizer try more

How it works
    1. Fetch fresh stories and split them: train / val for the optimizer, test held back.
    2. Score the current rules on the test stories, through the app's real prompt.
    3. GEPA rewrites the rules: it writes posts, reads what the checker complained about,
       and proposes better rules. The score needs no labels: agent/nodes/generate.quality()
       counts style violations and details that are not in the source.
    4. Score the tuned rules on the same test stories, again through the real prompt.
    5. Only if they beat the current ones by MARGIN are they written to data/linkedin_rules.txt,
       which the app picks up on its next run. Delete that file to go back.

DSPy is a dev dependency (`uv sync` installs it; requirements.txt does not), so the deployed
app never imports it: what ships is a plain text file.

Cost: each post is ~2,300 tokens on gpt-oss-120b. A default run is about 45 calls, ~100k tokens,
~15 minutes at the free tier's 8,000 tokens a minute. The free tier also has a daily token
cap, shared with the real posts, so do not run this just before a scheduled slot.

ponytail: the score only knows rules and invented details, not whether a post is any good.
When the feedback table in Supabase has ~50 decisions, add "would I approve this?" to the
metric (few-shot from approved vs rejected) and tune agent/nodes/select.py the same way.
"""

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # run as a script from anywhere

from agent import llm
from agent.nodes import generate, research
from agent.nodes.select import _dedup

MARGIN = 0.03        # tuned rules must beat the current ones by this much on the test stories
MAX_RULES = 4000     # characters; a rulebook longer than this costs more per post than it earns
PAUSE = 20           # seconds between scoring calls: keeps a ~2,300-token prompt under 8,000 tokens a minute
SPLIT = (8, 6, 6)    # train, val, test


def fetch_stories():
    """A mixed bag of real stories, like the ones the app drafts from."""
    return _dedup(research._gather(
        lambda: research._hn_top(10),
        lambda: research._google_news_search("new AI model release OR LLM OR AI agents when:3d", 8),
        lambda: research._hf_papers(6),
        lambda: research._github_trending(4),
        lambda: research._parse_feed("BBC Tech", research.BBC_TECH_RSS, 4),
    ))


def split(stories, sizes=SPLIT, seed=0):
    """→ (train, val, test), shuffled the same way every time for a given seed. Test is filled
    first: with too few stories the comparison matters more than the tuning."""
    stories = list(stories)
    random.Random(seed).shuffle(stories)
    n_train, n_val, n_test = sizes
    test, rest = stories[:n_test], stories[n_test:]
    return rest[:n_train], rest[n_train:n_train + n_val], test


def feedback(text, item):
    """What the checker has to say about a draft, as one string the optimizer can learn from."""
    issues = generate._li_issues(text, item)
    return "; ".join(issues) if issues else "Follows every rule and says nothing the source did not."


def evaluate(rules, stories, ask, pause=0, show=False):
    """Mean quality of `rules` over `stories`, written through the app's real prompt in one pass
    (no corrective retry: this measures the rules, not the repair loop). → (mean, [scores])"""
    scores = []
    for n, item in enumerate(stories):
        hook = generate.HOOKS[n % len(generate.HOOKS)]  # same hook per story for every rule set: a fair comparison
        try:
            text = ask(generate.linkedin_prompt(item, "", rules=rules, hook=hook))
            score = generate.quality(text, item)
        except Exception as e:
            print(f"  story {n}: call failed ({type(e).__name__}), scored 0", flush=True)
            text, score = "", 0.0
        scores.append(score)
        if show:
            print(f"  {score:.2f}  {item['title'][:60]:<60}  {feedback(text, item)[:110]}", flush=True)
        if pause and n < len(stories) - 1:
            time.sleep(pause)
    return (round(sum(scores) / len(scores), 3) if scores else 0.0), scores


def decide(base, tuned, rules, margin=MARGIN):
    """→ (adopt?, reason)."""
    if not rules or not rules.strip():
        return False, "the optimizer produced no rules"
    if len(rules) > MAX_RULES:
        return False, f"tuned rules are {len(rules)} characters, over the {MAX_RULES} cap"
    if rules.strip() == generate.RULES.strip():
        return False, "the optimizer kept the current rules"
    if round(tuned - base, 3) < margin:  # rounded: 0.83 - 0.80 is 0.0299999 in floating point
        return False, f"tuned {tuned:.3f} does not beat current {base:.3f} by {margin}"
    return True, f"tuned {tuned:.3f} beats current {base:.3f}"


def tune(train, val, budget, api_key):
    """Run GEPA. → the best rules text it found."""
    import dspy  # dev dependency, imported only when actually tuning

    lm = dspy.LM(f"groq/{llm.DEEP}", api_key=api_key, max_tokens=2000, num_retries=8, reasoning_effort="low")
    dspy.configure(lm=lm)

    class WritePost(dspy.Signature):
        title: str = dspy.InputField()
        source: str = dspy.InputField()
        what_the_source_says: str = dspy.InputField()
        how_to_open: str = dspy.InputField()
        reference_posts: str = dspy.InputField(desc="copy this rhythm, not the topics")
        post: str = dspy.OutputField(desc="only the post text")

    writer = dspy.Predict(WritePost.with_instructions(generate.RULES))

    def example(n, item):
        return dspy.Example(item=item, title=item["title"], source=item["source"],
                            what_the_source_says=item.get("summary") or "(nothing beyond the title)",
                            how_to_open=generate.HOOKS[n % len(generate.HOOKS)], reference_posts=generate._examples(),
                            ).with_inputs("title", "source", "what_the_source_says", "how_to_open", "reference_posts")

    def metric(gold, pred, trace=None, pred_name=None, pred_trace=None):
        text = getattr(pred, "post", "") or ""
        return dspy.Prediction(score=generate.quality(text, gold.item), feedback=feedback(text, gold.item))

    optimizer = dspy.GEPA(metric=metric, max_metric_calls=budget, reflection_lm=lm, num_threads=1,
                          reflection_minibatch_size=2, use_merge=False, seed=0)
    tuned = optimizer.compile(writer, trainset=[example(n, i) for n, i in enumerate(train)],
                              valset=[example(n, i) for n, i in enumerate(val)])
    return tuned.signature.instructions


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--baseline", action="store_true", help="only score the current rules, change nothing")
    parser.add_argument("--budget", type=int, default=30, help="how many drafts the optimizer may score (default 30)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        sys.exit("GROQ_API_KEY is not set.")
    train, val, test = split(fetch_stories())
    if len(test) < 3:
        sys.exit(f"Only {len(test)} test stories fetched; not enough to compare anything.")
    ask = lambda prompt: llm.ask(llm.DEEP, prompt, api_key)

    print(f"Stories: {len(train)} train, {len(val)} val, {len(test)} test\n\nCurrent rules ({generate._rules() is not generate.RULES and 'tuned file' or 'built-in'}):")
    base, _ = evaluate(generate._rules(), test, ask, PAUSE, show=True)
    print(f"  mean {base:.3f}")
    if args.baseline:
        return
    if not (train and val):
        sys.exit("Not enough stories left to tune on.")

    print(f"\nTuning with GEPA, budget {args.budget} drafts ...")
    rules = tune(train, val, args.budget, api_key)
    print(f"\nTuned rules ({len(rules)} characters):\n{rules}\n\nTuned rules on the same test stories:")
    tuned, _ = evaluate(rules, test, ask, PAUSE, show=True)
    print(f"  mean {tuned:.3f}")

    adopt, reason = decide(base, tuned, rules)
    if adopt:
        with open(generate.RULES_PATH, "w", encoding="utf-8") as f:
            f.write(rules.strip() + "\n")
        print(f"\nADOPTED: {reason}. Written to {generate.RULES_PATH}; delete it to go back.")
    else:
        print(f"\nNOT adopted: {reason}. Nothing changed.")


if __name__ == "__main__":
    main()
