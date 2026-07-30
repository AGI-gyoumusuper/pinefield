"""Advance an account's category cursor from actually posted/reserved ASINs."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import yaml


def category_key(category: dict) -> str:
    name = str(category.get("name", "")).strip()
    decoded_url = unquote(str(category.get("url", "")))
    node_match = re.search(r"(?:^|[=,&])n:(\d+)", decoded_url)
    if node_match:
        return f"node:{node_match.group(1)}"
    return f"name:{name}"


def load_json(path: Path):
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def product_category_map(account_dir: Path) -> dict[str, str]:
    """Use the newest product file containing an ASIN as its category source."""
    mapping: dict[str, str] = {}
    for path in sorted(account_dir.glob("products_*.json"), reverse=True):
        try:
            rows = load_json(path)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            asin = str(row.get("asin", "")).strip().upper()
            category = str(row.get("category", "")).split("#")[0].strip()
            if re.fullmatch(r"[A-Z0-9]{10}", asin) and category:
                mapping.setdefault(asin, category)
    return mapping


def build_updated_state(
    categories: list[dict],
    current_state: dict,
    asins: list[str],
    asin_categories: dict[str, str],
) -> tuple[dict, list[str]]:
    active = [
        category for category in categories
        if str(category.get("name", "")).strip() and str(category.get("url", "")).strip()
    ]
    by_name = {str(category["name"]).strip(): category for category in active}
    matched: list[tuple[str, str]] = []
    for asin in asins:
        category_name = asin_categories.get(asin)
        if category_name in by_name:
            matched.append((asin, category_name))
    if not matched:
        return current_state, []

    last_asin, last_name = matched[-1]
    last_category = by_name[last_name]
    last_position = active.index(last_category) + 1
    next_position = (last_position % len(active)) + 1
    next_category = active[next_position - 1]
    updated = dict(current_state)
    updated.update(
        {
            "schema": "amazon-category-rotation-v1",
            "updated_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds"),
            "category_count": len(active),
            "last_category_key": category_key(last_category),
            "last_category_name": last_name,
            "last_category_position": last_position,
            "next_category_key": category_key(next_category),
            "next_category_name": str(next_category["name"]).strip(),
            "next_category_position": next_position,
            "last_asin": last_asin,
            "last_sync_posted_count": len(matched),
        }
    )
    return updated, [asin for asin, _ in matched]


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temp_path, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, choices=[f"account{i}" for i in range(1, 11)])
    parser.add_argument("--asin", action="append", default=[])
    parser.add_argument("--repo-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    account_number = args.account.removeprefix("account")
    config_path = repo_dir / f"categories{account_number}.yaml"
    state_path = repo_dir / "data" / args.account / "category_rotation.json"
    with config_path.open("r", encoding="utf-8-sig") as file:
        config = yaml.safe_load(file) or {}
    categories = config.get("categories", [])
    current_state = load_json(state_path) if state_path.exists() else {}
    normalized_asins = [
        asin.strip().upper() for asin in args.asin
        if re.fullmatch(r"[A-Z0-9]{10}", asin.strip().upper())
    ]
    updated_state, matched_asins = build_updated_state(
        categories,
        current_state,
        normalized_asins,
        product_category_map(repo_dir / "data" / args.account),
    )

    if not matched_asins:
        print(f"[rotation] no posted ASIN matched {args.account} product categories; cursor unchanged")
        return 0
    if args.dry_run:
        print(
            f"[rotation][dry-run] {args.account}: "
            f"last={updated_state['last_category_position']}/{updated_state['category_count']} "
            f"{updated_state['last_category_name']} matched={len(matched_asins)}"
        )
        return 0

    atomic_write_json(state_path, updated_state)
    print(
        f"[rotation] {args.account}: "
        f"last={updated_state['last_category_position']}/{updated_state['category_count']} "
        f"{updated_state['last_category_name']} next={updated_state['next_category_position']} "
        f"matched={len(matched_asins)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
