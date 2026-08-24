"""Wait for the matching regular scrape workflow if it is still running.

The insurance workflow is scheduled shortly after account3. If the normal
scrape is still queued or running, wait instead of starting a duplicate repair.
"""

from __future__ import annotations

import json
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO = os.environ.get("GITHUB_REPOSITORY", "AGI-gyoumusuper/pinefield")
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


def workflow_for_account(account: str) -> str:
    number = int(account.removeprefix("account"))
    if number < 1 or number > 20 or account != f"account{number}":
        raise ValueError(f"unsupported account: {account}")
    return "scrape.yml" if number <= 5 else f"scrape{number}.yml"


def active_regular_runs(account: str) -> list[dict]:
    # The evening wave starts at JST 20:00. Keep the whole wave visible to the
    # JST 23:30 insurance run so a delayed regular job is never duplicated.
    since = datetime.now(timezone.utc) - timedelta(hours=5)
    workflow = workflow_for_account(account)
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow}/runs?per_page=20"
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, choices=[f"account{i}" for i in range(1, 21)])
    args = parser.parse_args()
    if not TOKEN:
        print("GITHUB_TOKEN is not set; skipping regular-scrape wait.", flush=True)
        return 0

    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while True:
        try:
            active = active_regular_runs(args.account)
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
