"""Prune ASIN ledgers to the configured recent-day window.

The newest of ``posted_at`` and ``reserved_at`` is used.  Future reservations
and entries with an unreadable date are retained on the safe side.  Entry
fields are preserved verbatim.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BASE = Path(__file__).resolve().parent.parent
DEFAULT_KEEP_DAYS = 20


def entry_date(entry: dict[str, Any]) -> date | None:
    values: list[date] = []
    for name in ("posted_at", "reserved_at"):
        match = re.match(r"(\d{4}-\d{2}-\d{2})", str(entry.get(name) or "").strip())
        if not match:
            continue
        try:
            values.append(date.fromisoformat(match.group(1)))
        except ValueError:
            continue
    return max(values, default=None)


def prune_history_data(value: dict[str, Any], today: date, keep_days: int) -> tuple[dict[str, Any], int]:
    if keep_days < 1:
        raise ValueError("keep_days must be at least 1")
    rows = value.get("posted", [])
    if not isinstance(rows, list):
        raise ValueError("history posted must be an array")
    cutoff = today - timedelta(days=keep_days - 1)
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        used_on = entry_date(row)
        if used_on is None or used_on >= cutoff:
            kept.append(row)
    output = dict(value)
    output["posted"] = kept
    output["updated_at"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
    return output, len(rows) - len(kept)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temp_path, path)


def prune_file(path: Path, today: date, keep_days: int) -> tuple[int, int]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "schema": "note-amazon-asin-history-v1",
            "updated_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds"),
            "description": f"Canonical ASIN exclusion ledger for {path.parent.name}.",
            "posted": [],
        }
        atomic_write_json(path, value)
        return 0, 0
    with path.open("r", encoding="utf-8-sig") as file:
        value = json.load(file)
    before = len(value.get("posted", [])) if isinstance(value, dict) else 0
    if not isinstance(value, dict):
        raise ValueError(f"history root must be an object: {path}")
    output, removed = prune_history_data(value, today, keep_days)
    if removed > 0:
        atomic_write_json(path, output)
    return before, before - removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DEFAULT_KEEP_DAYS)
    parser.add_argument("--repo-dir", default=str(BASE))
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    repo_dir = Path(args.repo_dir).resolve()
    for number in range(1, 11):
        path = repo_dir / "data" / f"account{number}" / "asin_history.json"
        before, after = prune_file(path, today, args.days)
        print(f"account{number}: {before} -> {after} entries (keep {args.days} days)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
