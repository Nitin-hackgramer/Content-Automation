r"""Offline tests for main.py: the pipeline's HTTP API (what the web app calls) and its secret rules.

    .venv\Scripts\python -m tests.test_api
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from agent.discovery import jobs, saved, service  # noqa: E402

GOOD = "k" * 32
H = {"X-Trigger-Secret": GOOD}


def check_secret_rules():
    c = TestClient(main.app)
    for secret in (None, "", "short", "k" * 31):  # unset or weak = closed, never open
        os.environ.pop("TRIGGER_SECRET", None)
        if secret is not None:
            os.environ["TRIGGER_SECRET"] = secret
        for r in (c.post("/discover", headers={"X-Trigger-Secret": secret or "x"}), c.get("/jobs/discover", headers={"X-Trigger-Secret": secret or "x"}),
                  c.post("/telegram-webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": ""})):
            assert r.status_code == 403, (secret, r.url, r.status_code)
    os.environ["TRIGGER_SECRET"] = GOOD
    assert c.post("/discover").status_code == 403 and c.post("/discover", headers={"X-Trigger-Secret": "k" * 31 + "x"}).status_code == 403
    assert c.get("/").json() == {"ok": True}
    assert c.post("/run").status_code == 404, "the old Agent P drafting route is gone"


def check_discover_and_status():
    os.environ["TRIGGER_SECRET"] = GOOD
    c = TestClient(main.app)
    jobs.reset()
    assert c.get("/jobs/discover", headers=H).json() == {"state": "idle"}
    assert c.get("/jobs/nope", headers=H).status_code == 404
    real = service._discover, main.telegram_discovery.notify_discover
    service._discover = lambda top: (time.sleep(0.2) or {"ideas": []})
    told = []
    main.telegram_discovery.notify_discover = lambda job: told.append(job.state)
    try:
        r = c.post("/discover", headers=H)
        assert r.status_code == 202 and r.json()["state"] == "running" and isinstance(r.json()["startedAt"], int)
        assert c.post("/discover", headers=H).json()["state"] == "running", "a second tap does not start a second run"
        end = time.time() + 5
        while c.get("/jobs/discover", headers=H).json()["state"] == "running" and time.time() < end:
            time.sleep(0.05)
        s = c.get("/jobs/discover", headers=H).json()
        assert s["state"] == "done" and s["endedAt"] >= s["startedAt"] and "error" not in s
        assert told == ["done"], "a run started over HTTP (the cron) reports to Telegram when it ends"
        service._discover = lambda top: 1 / 0
        c.post("/discover", headers=H)
        end = time.time() + 5
        while c.get("/jobs/discover", headers=H).json()["state"] == "running" and time.time() < end:
            time.sleep(0.05)
        s = c.get("/jobs/discover", headers=H).json()
        assert s["state"] == "failed" and s["error"] == "ZeroDivisionError"
        log = c.get("/jobs", headers=H).json()
        assert [j["kind"] for j in log] == ["discover", "discover"] and log[0]["ok"] is False and log[1]["ok"] is True
        assert told == ["done", "failed"], "a failed run is reported too"
    finally:
        service._discover, main.telegram_discovery.notify_discover = real


def check_brief():
    os.environ["TRIGGER_SECRET"] = GOOD
    c = TestClient(main.app)
    jobs.reset()
    assert c.post("/brief", json={"id": "../x"}, headers=H).status_code == 400
    assert c.post("/brief", json={}, headers=H).status_code == 400
    real = (service._write_brief, saved.find)
    service._write_brief = lambda i: {"hook": "h"}
    saved.find = lambda i: ("ok", {"id": i, "title": "T"}) if i == "a1b2c3d4e5f6" else ("none", None)
    assert c.post("/brief", json={"id": "deadbeef00"}, headers=H).status_code == 404
    try:
        r = c.post("/brief", json={"id": "a1b2c3d4e5f6"}, headers=H)
        assert r.status_code == 202 and r.json()["state"] in ("running", "done")
        end = time.time() + 5
        while c.get("/jobs/brief", headers=H).json()["state"] == "running" and time.time() < end:
            time.sleep(0.05)
        assert c.get("/jobs/brief", headers=H).json()["state"] == "done"
    finally:
        service._write_brief, saved.find = real


if __name__ == "__main__":
    check_secret_rules()
    check_discover_and_status()
    check_brief()
    print("api tests passed")
