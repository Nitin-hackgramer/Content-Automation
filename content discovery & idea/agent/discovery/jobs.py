"""Background jobs for work that takes minutes: a discovery run (3-4 min), writing a brief (up to a minute).

Nothing here knows about Telegram or about discovery. A caller starts a job with a function and an optional on_done
callback; whoever started it decides what "done" means for the user.

    - One job per key at a time. Asking again while it runs returns the running job (started=False), so a double tap
      or a repeated command never starts the work twice.
    - A job that runs longer than MAX_RUNTIME is marked failed ("TimedOut") and its key is free again. The thread cannot
      be killed, so a late finish is dropped: it neither changes the state nor calls on_done.
    - Only the exception's type name is kept. Messages can carry URLs, headers or keys.
    - State lives in this process. A restart forgets running jobs; the results they saved are not lost (see saved.py).
"""

import threading
import time
import uuid
from datetime import datetime, timezone

MAX_RUNTIME = 15 * 60  # seconds before a running job is given up on
KEEP = 20  # finished jobs remembered

_lock = threading.Lock()
_jobs = []  # oldest first


class Job:
    def __init__(self, kind, key, label):
        self.id = uuid.uuid4().hex[:6]
        self.kind, self.key, self.label = kind, key, label
        self.state = "running"  # running | done | failed
        self.error = None  # exception type name when failed
        self.result = None
        self.started_at = datetime.now(timezone.utc)
        self._t0 = time.monotonic()
        self._t1 = None

    def elapsed(self):
        """Seconds so far (running) or in total (finished)."""
        return (self._t1 if self._t1 is not None else time.monotonic()) - self._t0


def _finish(job, state, error=None, result=None):
    job.state, job.error, job.result, job._t1 = state, error, result, time.monotonic()


def start(kind, key, label, fn, on_done=None):
    """Run fn() in a background thread → (job, started). started is False when a job with this key is already running."""
    with _lock:
        for existing in _jobs:
            if existing.key == key and existing.state == "running":
                if existing.elapsed() <= MAX_RUNTIME:
                    return existing, False
                _finish(existing, "failed", "TimedOut")  # abandoned; its thread may still be running
        job = Job(kind, key, label)
        _jobs.append(job)
        finished = [j for j in _jobs if j.state != "running"]
        for old in finished[:-KEEP]:
            _jobs.remove(old)

    def work():
        try:
            outcome = ("done", None, fn())
        except Exception as e:  # type name only: a message may hold a URL or a key
            outcome = ("failed", type(e).__name__, None)
        with _lock:
            if job.state != "running":  # abandoned while it ran: the late result is dropped
                return
            _finish(job, *outcome)
        if on_done:
            try:
                on_done(job)
            except Exception:
                pass  # a notifier that breaks must not break the job

    threading.Thread(target=work, daemon=True, name=f"job-{job.id}").start()
    return job, True


def running(kind=None):
    with _lock:
        return [j for j in _jobs if j.state == "running" and (kind is None or j.kind == kind)]


def latest(kind, state=None):
    """The newest job of this kind (optionally only in `state`), or None."""
    with _lock:
        for j in reversed(_jobs):
            if j.kind == kind and (state is None or j.state == state):
                return j
    return None


def reset():
    """Forget every job. For tests."""
    with _lock:
        _jobs.clear()


if __name__ == "__main__":
    reset()
    gate, done = threading.Event(), threading.Event()
    job, fresh = start("demo", "k", "demo", lambda: gate.wait(5) and "ok", on_done=lambda j: done.set())
    again, fresh2 = start("demo", "k", "demo", lambda: "never")
    assert fresh and not fresh2 and again is job, "one job per key"
    gate.set()
    assert done.wait(5) and job.state == "done" and job.result == "ok"
    bad, _ = start("demo", "bad", "bad", lambda: 1 / 0)
    time.sleep(0.2)
    assert bad.state == "failed" and bad.error == "ZeroDivisionError" and bad.result is None
    print("jobs self-check passed")
