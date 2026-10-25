"""Lay the daily digest out for Telegram. Pure functions: curated entries in, HTML messages out.

Built to be read two ways:
  scan  - bold headlines only. The first message gives the day at a glance (hot topics,
          top stories, what's inside) and every section is a short list of headlines.
  dive  - under each headline a one-line quote says what happened and why it matters.

An entry is {"h": headline, "s": summary or "", "url": link, "meta": source, "hot": 1|2|3}.
"""

import html
import re

LIMIT = 4096            # Telegram sendMessage cap, counted on the visible text (tags and link targets are free)
URL_MAX = 1000          # a longer link (after escaping) is shown as plain text, never half-cut
BLOCK_MAX = 1800        # an entry with more visible text than this loses its summary
HOT = {3: "🔥", 2: "⚡", 1: "•"}
BAR = 10                # width of the heat bars
MUST_READ = 3
NUMBERS = ["1️⃣", "2️⃣", "3️⃣"]


def esc(text):
    return html.escape(text or "", quote=False)


def visible_len(markup):
    """Length Telegram counts: the text after tags are parsed away and entities decoded."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", markup)))


def _headline(entry):
    h = esc(entry["h"])
    url = entry.get("url") or ""
    href = html.escape(url, quote=True)  # escaping can grow a link 5x ("&" → "&amp;"), so measure after
    if url.startswith("http") and len(href) <= URL_MAX:
        return f'<b><a href="{href}">{h}</a></b>'
    return f"<b>{h}</b>"


def block(entry):
    """One story: marker + bold linked headline, then the summary as a quote."""
    head = f"{HOT.get(entry.get('hot'), HOT[1])} {_headline(entry)}"
    summary, meta = esc(entry.get("s")), esc(entry.get("meta"))
    if not summary:
        return f"{head} <i>· {meta}</i>" if meta else head
    detail = f"{summary} · <i>{meta}</i>" if meta else summary
    text = f"{head}\n<blockquote>{detail}</blockquote>"
    return text if visible_len(text) <= BLOCK_MAX else head


def heat_chart(heat):
    """heat: [(topic, number of stories)]. Monospace bars, hottest first, at most five."""
    top = sorted((t for t in heat if t[1] > 0), key=lambda t: -t[1])[:5]
    if not top:
        return ""
    width = min(max(len(name) for name, _ in top), 18)
    peak = top[0][1]
    rows = []
    for name, count in top:
        filled = max(1, round(count / peak * BAR))
        rows.append(f"{name[:width].ljust(width)} {'█' * filled}{'░' * (BAR - filled)} {count}")
    return "🔥 <b>HOT TODAY</b>  <i>(stories per topic)</i>\n<pre>" + esc("\n".join(rows)) + "</pre>"


def must_read(sections):
    """The day's biggest stories: entries the editor rated 🔥, in section order."""
    picks = [e for entries in sections.values() for e in entries if e.get("hot") == 3][:MUST_READ]
    if not picks:
        return ""
    lines = [f"{NUMBERS[n]} {_headline(e)}" for n, e in enumerate(picks)]
    return "⭐ <b>MUST READ</b>\n" + "\n".join(lines)


def overview(sections, heat, now):
    total = sum(len(v) for v in sections.values())
    head = f"📰 <b>DAILY DIGEST</b>\n<i>{now:%A %d %B %Y} · {total} stories</i>"
    if not total:
        return head + "\n\nNo news could be fetched today."
    inside = "\n".join(f"{esc(h)} · {len(v)}" for h, v in sections.items() if v)
    parts = [head, heat_chart(heat), must_read(sections), f"🧭 <b>INSIDE</b>\n{inside}",
             "<i>🔥 huge · ⚡ notable · bold = headline, quote = the detail</i>"]
    return "\n\n".join(p for p in parts if p)


def section_messages(heading, entries, limit=LIMIT):
    """Pack a section's stories into as few messages as fit, repeating the heading on each."""
    if not entries:
        return []
    title = f"<b>{esc(heading.upper())}</b>"
    first, later = f"{title}  <code>{len(entries)}</code>", f"{title} <i>(cont.)</i>"
    out, cur = [], first
    for entry in entries:
        add = "\n\n" + block(entry)
        if cur != first and cur != later and visible_len(cur) + visible_len(add) > limit:
            out.append(cur)
            cur = later
        cur += add
    out.append(cur)
    return out


def render(sections, heat, now):
    """sections: {heading: [entry]} in display order. → list of HTML messages, overview first."""
    messages = [overview(sections, heat, now)]
    for heading, entries in sections.items():
        messages += section_messages(heading, entries)
    return messages
