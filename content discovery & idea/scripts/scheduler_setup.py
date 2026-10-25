"""Register/remove Windows Task Scheduler jobs that run main.py 5 min before
peak-engagement times on X (so you get the Telegram prompt in time to approve
and post right at the good slot).

Times match the deployed schedule (IST): 8:55am, 11:55am, 4:55pm, 6:55pm, 8:55pm.
ponytail: hardcoded, no "research" module — these are stable slots, not something
to compute at runtime. Override via TWEET_BEST_TIMES env var, e.g. "08:00,13:30,19:00".
Local mode always drafts both platforms (main.py has no per-slot target split);
the X-only/LinkedIn-only 6-slot split lives in the Hostinger cron, not here.

schtasks /st always uses the machine's local timezone (no UTC conversion
happens anywhere in this file) — set Windows' timezone to yours and the
times below are exactly when you'll be pinged.

Usage:
    uv run python scripts/scheduler_setup.py install
    uv run python scripts/scheduler_setup.py remove
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta

DEFAULT_TIMES = ["08:55", "11:55", "16:55", "18:55", "20:55"]
TASK_PREFIX = "DiscoveryAgent_"  # not "TwitterAgent_": that prefix belongs to Agent P's tasks


def _best_times():
    raw = os.getenv("TWEET_BEST_TIMES")
    return [t.strip() for t in raw.split(",")] if raw else DEFAULT_TIMES


def _notify_time(hhmm):
    t = datetime.strptime(hhmm, "%H:%M") - timedelta(minutes=5)
    return t.strftime("%H:%M")


def install():
    py = sys.executable
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    for t in _best_times():
        notify_at = _notify_time(t)
        name = TASK_PREFIX + t.replace(":", "")
        subprocess.run([
            "schtasks", "/create", "/f", "/tn", name, "/sc", "DAILY",
            "/st", notify_at, "/tr", f'"{py}" "{script}"',
        ], check=True)
        print(f"Scheduled {name}: runs daily at {notify_at} (5 min before {t})")


def remove():
    for t in _best_times():
        name = TASK_PREFIX + t.replace(":", "")
        subprocess.run(["schtasks", "/delete", "/f", "/tn", name])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "install":
        install()
    elif cmd == "remove":
        remove()
    else:
        print(__doc__)
