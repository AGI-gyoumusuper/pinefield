"""Wait for the matching regular scrape workflow if it is still running.

An unknown or still-active regular run must never start a duplicate repair.
Queued work has no age limit: delayed GitHub jobs can outlive the evening wave.
"""

from __future__ import annotations

import json
import argparse
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPO = os.environ.get("GITHUB_REPOSITORY", "AGI-gyoumusuper/pinefield")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
CURRENT_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "1200"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
ACTIVE_RUN_STATUSES = ("requested", "pending", "waiting", "queued", "in_progress")
MAX_RUN_PAGES = 10
RUNS_PER_PAGE = 100


class RunStateUnavailable(RuntimeError):
    pass


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
    workflow = workflow_for_account(account)
    endpoint = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow}/runs"
    active = {}
    # Status filters keep completed history from hiding old active jobs. Shared
    # account1-5/manual workflows are conservatively waited on as a whole.
    for status in ACTIVE_RUN_STATUSES:
        seen = set()
        total = 0
        for page in range(1, MAX_RUN_PAGES + 1):
            query = urlencode({"status": status, "per_page": RUNS_PER_PAGE, "page": page})
            data = api_json(endpoint + "?" + query)
            if (not isinstance(data, dict) or type(data.get("total_count")) is not int
                    or data["total_count"] < 0 or not isinstance(data.get("workflow_runs"), list)):
                raise RunStateUnavailable("regular_runs_invalid_response")
            rows = data["workflow_runs"]
            total = max(total, data["total_count"])
            if total > MAX_RUN_PAGES * RUNS_PER_PAGE:
                raise RunStateUnavailable("regular_runs_pagination_limit")
            if len(rows) > RUNS_PER_PAGE or len(rows) > data["total_count"]:
                raise RunStateUnavailable("regular_runs_invalid_count")
            for run in rows:
                if (not isinstance(run, dict) or type(run.get("id")) is not int
                        or run["id"] <= 0
                        or run.get("status") not in (*ACTIVE_RUN_STATUSES, "completed")):
                    raise RunStateUnavailable("regular_runs_invalid_row")
                seen.add(run["id"])
                if str(run["id"]) != str(CURRENT_RUN_ID) and run["status"] != "completed":
                    active[run["id"]] = run
            if len(seen) >= total:
                break
            if len(rows) < RUNS_PER_PAGE:
                raise RunStateUnavailable("regular_runs_incomplete_pagination")
        else:
            raise RunStateUnavailable("regular_runs_pagination_limit")
    return list(active.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, choices=[f"account{i}" for i in range(1, 21)])
    args = parser.parse_args(argv)
    if not TOKEN:
        print("GITHUB_TOKEN is not set; refusing an unverified overlapping repair.", flush=True)
        return 1

    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while True:
        try:
            active = active_regular_runs(args.account)
        except (HTTPError, URLError, OSError, ValueError, RunStateUnavailable) as exc:
            print(f"Could not confirm regular scrape state ({type(exc).__name__}); repair stopped.", flush=True)
            return 1
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
            print("Regular scrape still active after wait limit; repair stopped.", flush=True)
            return 1
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
