"""Download every discovery_ table from Supabase into one JSON file (the free plan has no downloadable backups).

    python scripts/backup.py                 # writes data/backups/discovery_<date>.json
Restore: paste rows back with the Supabase table editor or a POST to /rest/v1/discovery_<table>.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import store  # noqa: E402
from agent.paths import DATA  # noqa: E402

TABLES = ("kv", "seen", "feedback", "topic_obs")
PAGE = 1000


def dump(table):
    rows, offset = [], 0
    while True:
        got = store._call("GET", table, {"select": "*", "limit": str(PAGE), "offset": str(offset)})
        if got is None:
            raise SystemExit(f"Could not read discovery_{table} (see the line above). Nothing written.")
        rows += got
        if len(got) < PAGE:
            return rows
        offset += PAGE


if __name__ == "__main__":
    out = {"at": datetime.now(timezone.utc).isoformat(), "tables": {t: dump(t) for t in TABLES}}
    path = os.path.join(DATA, "backups", f"discovery_{datetime.now(timezone.utc):%Y-%m-%d}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"Saved {', '.join(f'{t} {len(r)}' for t, r in out['tables'].items())} rows to {path}")
