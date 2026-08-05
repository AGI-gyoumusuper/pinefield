"""Ensure today's scrape outputs exist and are usable.

This is a GitHub Actions safety net. It never falls back to older dates:
missing or invalid files are recreated for today's JST date only.
"""

from __future__ import annotations

import json
import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


ROOT = Path(__file__).resolve().parent
TODAY = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
ACCOUNTS = tuple(f"account{number}" for number in range(1, 11))  # 10人格＝account1〜10（account0は退役・2026-07-13裁定）


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def load_json(path: Path):
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def expected_item_count(config_path: Path) -> int:
    with config_path.open("r", encoding="utf-8-sig") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise ValueError(f"config is not a mapping: {config_path}")

    categories = config.get("categories", [])
    filters = config.get("filters", {})
    if not isinstance(categories, list):
        raise ValueError(f"categories is not a list: {config_path}")
    if not isinstance(filters, dict):
        raise ValueError(f"filters is not a mapping: {config_path}")

    active_categories = [
        category
        for category in categories
        if isinstance(category, dict)
        and str(category.get("name", "")).strip()
        and str(category.get("url", "")).strip()
    ]
    max_total = int(filters.get("max_total_items", 0))
    max_per_category = int(filters.get("max_per_category", 0))
    if not active_categories:
        raise ValueError(f"no active categories: {config_path}")
    if max_total <= 0:
        raise ValueError(f"max_total_items must be positive: {config_path}: {max_total}")
    if max_per_category <= 0:
        raise ValueError(f"max_per_category must be positive: {config_path}: {max_per_category}")

    return min(max_total, len(active_categories) * max_per_category)


def valid_product_list(path: Path, expected_items: int) -> tuple[bool, str]:
    if not path.exists():
        return False, f"missing: {path}"
    try:
        data = load_json(path)
    except Exception as exc:
        return False, f"invalid json: {path}: {exc}"
    if not isinstance(data, list):
        return False, f"not list: {path}"
    if len(data) < expected_items:
        return False, f"too few items: {path}: {len(data)} < {expected_items}"
    return True, f"ok: {path}: {len(data)} items"


def price_int(item: dict) -> int:
    for key in ("sale_price", "current_price", "price", "discounted_price", "price_int", "original_price"):
        value = item.get(key)
        if isinstance(value, int):
            price = value
        elif isinstance(value, float):
            price = int(value)
        elif isinstance(value, str):
            digits = "".join(ch for ch in value if ch.isdigit())
            price = int(digits) if digits else 0
        else:
            price = 0
        if price > 0:
            return price
    return 0


def validate(account: str, root: Path | None = None, today: str | None = None) -> tuple[bool, str]:
    if account not in ACCOUNTS:
        raise ValueError(account)
    root = ROOT if root is None else root
    today = TODAY if today is None else today
    account_number = account.removeprefix("account")
    expected_items = expected_item_count(root / f"categories{account_number}.yaml")
    return valid_product_list(
        root / "data" / account / f"products_{today}.json",
        expected_items,
    )


def cleanup(account: str) -> None:
    if account not in ACCOUNTS:
        raise ValueError(account)
    path = ROOT / "data" / account / f"products_{TODAY}.json"
    if path.exists():
        path.unlink()
        print(f"removed partial output: {path}", flush=True)


def scrape(account: str) -> None:
    if account in ACCOUNTS:
        account_number = account.removeprefix("account")
        run([sys.executable, f"scrape_main{account_number}.py"])
    else:
        raise ValueError(account)


def ensure(account: str) -> bool:
    ok, message = validate(account)
    print(f"{account}: {message}", flush=True)
    if ok:
        return False

    for attempt in range(1, 4):
        print(f"{account}: repair attempt {attempt}/3 for {TODAY}", flush=True)
        cleanup(account)
        try:
            scrape(account)
        except subprocess.CalledProcessError as exc:
            print(f"{account}: scrape failed: {exc}", flush=True)
        ok, message = validate(account)
        print(f"{account}: {message}", flush=True)
        if ok:
            return True
        if attempt < 3:
            time.sleep(30)

    raise RuntimeError(f"{account}: failed to create valid output for {TODAY}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", choices=ACCOUNTS, action="append")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate today's outputs without deleting files or running scrapers",
    )
    args = parser.parse_args()
    accounts = tuple(args.account) if args.account else ACCOUNTS

    print(f"ensure daily scrape date: {TODAY}", flush=True)
    if args.validate_only:
        failed = []
        for account in accounts:
            ok, message = validate(account)
            print(f"{account}: {message}", flush=True)
            if not ok:
                failed.append(account)
        if failed:
            print("invalid accounts:", ",".join(failed), flush=True)
            return 1
        return 0

    changed = []
    for account in accounts:
        if ensure(account):
            changed.append(account)
    print("repaired accounts:", ",".join(changed) if changed else "none", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
