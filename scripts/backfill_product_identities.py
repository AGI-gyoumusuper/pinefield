"""Backfill verified ASIN history rows with stable product identifiers."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from product_identity import (
    ProductIdentity,
    extract_product_identity,
    identity_from_key,
    merge_product_identities,
)


SUCCESS_STATUSES = {"posted", "published", "reserved", "scheduled"}
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def array_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for name in ("products", "items", "data", "results", "posted"):
            rows = value.get(name)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def normalized_asin(value: Any) -> str:
    asin = str(value or "").strip().upper()
    return asin if ASIN_PATTERN.fullmatch(asin) else ""


def merge_for_asin(
    identities: dict[str, ProductIdentity], asin: str, identity: ProductIdentity
) -> None:
    if not asin or not identity.usable:
        return
    identities[asin] = merge_product_identities(
        identities.get(asin, ProductIdentity()), identity
    )


def product_identities(account_dir: Path) -> tuple[dict[str, ProductIdentity], set[str]]:
    result: dict[str, ProductIdentity] = {}
    seen: set[str] = set()
    for path in sorted(account_dir.glob("products_*.json")):
        for row in array_items(read_json(path)):
            asin = normalized_asin(row.get("asin"))
            if asin:
                seen.add(asin)
            merge_for_asin(result, asin, extract_product_identity(row))
    return result, seen


def legacy_identities(path: Path | None) -> dict[str, ProductIdentity]:
    result: dict[str, ProductIdentity] = {}
    if path is None:
        return result
    value = read_json(path)
    rows = value.get("automatic_keys", []) if isinstance(value, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = identity_from_key(row.get("identity_key"))
        for raw_asin in row.get("asins", []) or []:
            merge_for_asin(result, normalized_asin(raw_asin), identity)
    return result


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temp_path, path)


def backfill(repo_dir: Path, legacy_path: Path | None = None) -> dict[str, Any]:
    legacy = legacy_identities(legacy_path)
    total_updated = 0
    total_usable = 0
    accounts: dict[str, dict[str, int]] = {}
    for number in range(1, 21):
        account_id = f"account{number}"
        account_dir = repo_dir / "data" / account_id
        history_path = account_dir / "asin_history.json"
        if not history_path.is_file():
            continue
        history = read_json(history_path)
        rows = history.get("posted", []) if isinstance(history, dict) else []
        if not isinstance(rows, list):
            raise ValueError(f"invalid history schema: {history_path}")

        discovered, discovered_asins = product_identities(account_dir)
        updated = 0
        usable = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("status", "")).strip().lower() not in SUCCESS_STATUSES:
                continue
            asin = normalized_asin(row.get("asin"))
            source_identity = merge_product_identities(
                discovered.get(asin, ProductIdentity()),
                legacy.get(asin, ProductIdentity()),
            )
            source_is_authoritative = asin in discovered_asins or asin in legacy
            combined = (
                source_identity
                if source_is_authoritative
                else extract_product_identity(row)
            )
            if not combined.usable:
                if source_is_authoritative and "product_identity" in row:
                    del row["product_identity"]
                    updated += 1
                continue
            usable += 1
            output = combined.to_dict()
            if row.get("product_identity") != output:
                row["product_identity"] = output
                updated += 1

        if updated:
            history["updated_at"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
            atomic_write_json(history_path, history)
        accounts[account_id] = {"updated": updated, "usable": usable}
        total_updated += updated
        total_usable += usable

    return {
        "ok": True,
        "updated": total_updated,
        "usable_verified_rows": total_usable,
        "legacy_identity_asins": len(legacy),
        "accounts": accounts,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--legacy-identity-file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = backfill(
        Path(args.repo_dir).resolve(),
        Path(args.legacy_identity_file).resolve() if args.legacy_identity_file else None,
    )
    print("PRODUCT_IDENTITY_BACKFILL=" + json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
