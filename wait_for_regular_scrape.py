"""Wait for the regular scrape workflow if it is still running.

The insurance workflow is scheduled shortly after account3. If the normal
scrape is still queued or running, wait instead of starting a duplicate repair.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO = os.environ.get("GITHUB_REPOSITORY", "AGI-gyoumusuper/note-amazon-auto")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
CURRENT_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "1200"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))


def api_json(url: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "note-amazon-auto-insurance",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def active_regular_runs() -> list[dict]:
    # The account3 schedule is JST 00:24, i.e. UTC 15:24 on the previous day.
    # A 3-hour UTC window is enough to catch delayed queued/running scrape runs.
    since = datetime.now(timezone.utc) - timedelta(hours=3)
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/scrape.yml/runs?per_page=20"
    data = api_json(url)
    active = []
    for run in data.get("workflow_runs", []):
        if str(run.get("id")) == str(CURRENT_RUN_ID):
            continue
        if run.get("status") not in {"queued", "in_progress", "waiting", "requested", "pending"}:
            continue
        created = datetime.fromisoformat(str(run.get("created_at")).replace("Z", "+00:00"))
        if created >= since:
            active.append(run)
    return active


def main() -> int:
    if not TOKEN:
        print("GITHUB_TOKEN is not set; skipping regular-scrape wait.", flush=True)
        return 0

    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while True:
        try:
            active = active_regular_runs()
        except (HTTPError, URLError) as exc:
            print(f"Could not query regular scrape runs; continuing without wait: {exc}", flush=True)
            return 0
        if not active:
            print("No active regular scrape workflow runs found.", flush=True)
            return 0
        for run in active:
            print(
                f"Waiting for regular scrape run id={run.get('id')} "
                f"status={run.get('status')} created_at={run.get('created_at')}",
                flush=True,
            )
        if time.monotonic() >= deadline:
            print("Regular scrape still active after wait limit; insurance will proceed.", flush=True)
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
