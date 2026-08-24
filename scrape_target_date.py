"""Resolve the dated scrape artifact requested by a scheduler trigger."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
ACCOUNTS = tuple(f"account{number}" for number in range(1, 21))


def parse_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid {label}: {value!r}")
    return parsed


def jst_today(now: datetime | None = None) -> date:
    current = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return current.astimezone(ZoneInfo("Asia/Tokyo")).date()


def resolve_target_date(
    account: str,
    root: Path = ROOT,
    environment: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    if account not in ACCOUNTS:
        raise ValueError(f"unsupported account: {account}")

    env = os.environ if environment is None else environment
    override = str(env.get("PINEFIELD_TARGET_DATE", "")).strip()
    if override:
        return parse_date(override, "PINEFIELD_TARGET_DATE").isoformat()

    today = jst_today(now)
    if env.get("GITHUB_EVENT_NAME") != "push":
        return today.isoformat()

    trigger_path = root / ".github" / "triggers" / f"{account}.json"
    try:
        trigger = json.loads(trigger_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read target date from {trigger_path}: {exc}") from exc
    if not isinstance(trigger, dict) or trigger.get("account") != account:
        raise RuntimeError(f"trigger account mismatch: {trigger_path}")

    target_value = str(trigger.get("target_date", "")).strip()
    target = parse_date(target_value, f"target_date in {trigger_path}")
    day_offset = (target - today).days
    if day_offset not in {0, 1}:
        raise RuntimeError(
            f"trigger target_date must be JST today or tomorrow: {target_value}"
        )
    return target.isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, choices=ACCOUNTS)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    print(resolve_target_date(args.account, args.root.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
