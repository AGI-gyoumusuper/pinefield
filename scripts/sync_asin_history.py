"""Merge verified note results into the canonical ASIN ledger and category cursor.

Only evidence-backed note reservations/posts are accepted.  This module performs
no Git operations; ``sync_asin_history1.ps1`` supplies an isolated worktree,
pushes ``HEAD:main``, and verifies the remote ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from product_identity import extract_product_identity, merge_product_identities

try:
    from .update_category_rotation import build_updated_state
except ImportError:  # direct script execution
    from update_category_rotation import build_updated_state


SUCCESS_RESERVED = {"reserved", "scheduled"}
SUCCESS_POSTED = {"posted", "published"}
IGNORED_STATUSES = {"scraped", "draft", "rejected", "failed", "error", "post_error"}
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")
ASIN_TEXT_PATTERNS = (
    re.compile(r"(?:/DP/|/GP/PRODUCT/)([A-Z0-9]{10})", re.IGNORECASE),
    re.compile(r"[?&]ASIN=([A-Z0-9]{10})", re.IGNORECASE),
    re.compile(r"(?<![A-Z0-9])(B0[A-Z0-9]{8})(?![A-Z0-9])", re.IGNORECASE),
)
PRODUCT_FIELDS = {
    "asin", "title", "price", "price_int", "original_price", "discount_rate",
    "image_url", "affiliate_url", "category", "rating", "review_count",
    "description", "specs",
}


class SyncError(RuntimeError):
    """Input evidence is unsafe or incomplete."""


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def array_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for name in ("results", "posted", "items", "articles", "products", "data"):
            rows = value.get(name)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return [value]
    raise SyncError("JSON root must be an object or array")


def text_value(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def extract_asin(row: dict[str, Any]) -> str:
    for name in ("asin", "ASIN", "product_asin", "productAsin"):
        value = str(row.get(name, "")).strip().upper()
        if ASIN_PATTERN.fullmatch(value):
            return value
    for value in row.values():
        if not isinstance(value, str):
            continue
        for pattern in ASIN_TEXT_PATTERNS:
            match = pattern.search(value)
            if match:
                return match.group(1).upper()
    return ""


def normalize_category(value: Any) -> str:
    return re.sub(r"#\d+$", "", str(value or "").strip()).strip()


def event_date(entry: dict[str, Any]) -> str:
    dates = []
    for name in ("posted_at", "reserved_at"):
        match = re.match(r"(\d{4}-\d{2}-\d{2})", str(entry.get(name) or "").strip())
        if match:
            dates.append(match.group(1))
    return max(dates, default="unknown")


def event_timestamp(entry: dict[str, Any]) -> str:
    values = [str(entry.get(name) or "").strip() for name in ("posted_at", "reserved_at")]
    present = [value for value in values if value]
    return max(present, key=parse_event_instant, default="")


def parse_event_instant(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise SyncError("event timestamp is empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise SyncError(f"invalid event timestamp: {text}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)))
    return parsed.astimezone(timezone.utc)


def history_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("asin", "")).strip().upper(),
        event_date(entry),
        str(entry.get("account_id", "")).strip(),
    )


def row_to_entry(
    row: dict[str, Any], account_id: str, account_name: str, source_label: str, row_number: int
) -> tuple[dict[str, Any] | None, str]:
    status = text_value(row, "status").lower()
    if status.startswith("skipped") or status in IGNORED_STATUSES:
        return None, ""
    if status not in SUCCESS_RESERVED | SUCCESS_POSTED:
        raise SyncError(f"{source_label} row {row_number}: unknown status {status or '<missing>'}")

    asin = extract_asin(row)
    if not asin:
        raise SyncError(f"{source_label} row {row_number}: successful row has no valid ASIN")

    posted_at = text_value(row, "posted_at", "postedAt") or None
    reserved_at = text_value(
        row, "reserved_at", "reservedAt", "publish_at", "scheduled_at", "scheduledAt"
    ) or None
    if status in SUCCESS_RESERVED:
        if row.get("reserved_list_confirmed") is not True:
            raise SyncError(
                f"{source_label} row {row_number}: {status} is not confirmed in note reserved list"
            )
        if not reserved_at:
            raise SyncError(f"{source_label} row {row_number}: confirmed reservation has no reserved_at")
        canonical_status = "reserved"
    else:
        posted_confirmed = any(
            row.get(name) is True
            for name in ("posted_list_confirmed", "published_list_confirmed", "management_list_confirmed")
        )
        if not posted_confirmed:
            raise SyncError(
                f"{source_label} row {row_number}: {status} is not confirmed in note management list"
            )
        if not posted_at:
            raise SyncError(f"{source_label} row {row_number}: published row has no posted_at")
        canonical_status = "posted"

    parse_event_instant(posted_at or reserved_at or "")

    entry: dict[str, Any] = {
        "asin": asin,
        "status": canonical_status,
        "posted_at": posted_at,
        "reserved_at": reserved_at,
        "account_id": account_id,
        "account_name": account_name,
    }
    category = normalize_category(row.get("category"))
    if category:
        entry["category"] = category
    return entry, category


def product_metadata_sources(
    paths: list[Path], account_dir: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
    mapping: dict[str, dict[str, Any]] = {}
    sources: dict[str, Path] = {}

    def add_file(path: Path, overwrite: bool) -> None:
        try:
            rows = array_items(read_json(path))
        except Exception as error:
            raise SyncError(f"cannot read product JSON {path}: {error}") from error
        for row in rows:
            asin = extract_asin(row)
            if not asin:
                continue
            if overwrite or asin not in mapping:
                mapping[asin] = row
                sources[asin] = path

    for path in paths:
        add_file(path, overwrite=True)
    for path in sorted(account_dir.glob("products_*.json"), reverse=True):
        add_file(path, overwrite=False)
    return mapping, sources


def product_metadata_map(paths: list[Path], account_dir: Path) -> dict[str, dict[str, Any]]:
    return product_metadata_sources(paths, account_dir)[0]


def product_source_config(
    product_path: Path, base_config: dict[str, Any], account: int
) -> dict[str, Any]:
    """Restore a campaign only from the frozen source paired with these products.

    The PowerShell wrapper forwards absolute ProductJson paths into its isolated
    worktree, so evidence remains beside the original input, rather than in the
    new checkout. Dated Git products use their same-day Git summary instead.
    """
    parent = product_path.parent
    dated = re.fullmatch(r"products_(\d{4}-\d{2}-\d{2})\.json", product_path.name)
    input_date = dated.group(1) if dated else ""
    if dated:
        raw_path = product_path
        summary_path = parent / f"scrape_summary_{input_date}.json"
    else:
        raw_path = parent / "products.json"
        summary_path = parent / "scrape_summary.json"
        if not summary_path.is_file():
            # ①part products retain their account/date/source positions, while
            # their frozen raw source lives in the same factory's ② tree.
            part = re.fullmatch(
                r"products_account(\d+)_(\d{4}-\d{2}-\d{2})_(p12|p34)\.json",
                product_path.name,
            )
            factory_name = f"★全体的なワークフロー_1_{account}"
            factory = next((p for p in product_path.parents if p.name == factory_name), None)
            if factory is not None:
                relative = product_path.relative_to(factory).parts
                reservation_date = ""
                if (len(relative) == 7 and relative[:2] == ("④note予約", "実行結果")
                    and re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative[3])
                    and relative[4] in {"p12", "p34"}
                    and relative[5:] == ("reservation_inputs", "products.json")):
                    if relative[2] != f"account{account}":
                        raise SyncError(f"reservation product source account mismatch: {product_path}")
                    reservation_date = relative[3]
                if part or reservation_date:
                    input_date = part.group(2) if part else reservation_date
                    source_dir = factory / "②記事制作" / "実行結果" / f"account{account}" / input_date / "source"
                    raw_path = source_dir / "products.json"
                    summary_path = source_dir / "scrape_summary.json"
                    if part and summary_path.is_file() and int(part.group(1)) != account:
                        raise SyncError(f"product source account mismatch: {product_path}")
    if not summary_path.is_file():
        return base_config
    summary = read_json(summary_path)
    if not isinstance(summary, dict):
        raise SyncError(f"invalid source summary: {summary_path}")
    manifest_path = summary_path.parent / "source_manifest.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else None
    source_date = str(summary.get("date") or "")
    if manifest is not None:
        if not isinstance(manifest, dict):
            raise SyncError(f"invalid source manifest: {manifest_path}")
        if manifest.get("account") != account or str(manifest.get("date") or "") != source_date:
            raise SyncError(f"source manifest account/date mismatch: {manifest_path}")
        for key, path in (("products_sha256", raw_path), ("summary_sha256", summary_path)):
            expected = str(manifest.get(key) or "").upper()
            if not re.fullmatch(r"[0-9A-F]{64}", expected) or not path.is_file():
                raise SyncError(f"source manifest has no valid {key}: {manifest_path}")
            if hashlib.sha256(path.read_bytes()).hexdigest().upper() != expected:
                raise SyncError(f"source manifest SHA mismatch: {path}")
    if input_date and source_date != input_date:
        raise SyncError(f"product/source date mismatch: {product_path}")
    if "campaign" not in summary:
        return base_config
    if not raw_path.is_file():
        raise SyncError(f"campaign raw product JSON not found: {raw_path}")
    raw_products = read_json(raw_path)
    if not isinstance(raw_products, list) or any(
        not isinstance(row, dict) or set(row) != PRODUCT_FIELDS for row in raw_products
    ):
        raise SyncError(f"campaign source requires the original 13 product fields: {raw_path}")
    if manifest is not None and (
        manifest.get("product_count") != len(raw_products)
        or manifest.get("source_order_preserved") is not True
        or manifest.get("asin_refiltered") is not False
    ):
        raise SyncError(f"campaign source manifest product contract mismatch: {manifest_path}")
    try:
        from sale_campaign import source_campaign_config

        config = source_campaign_config(base_config, summary, raw_products, account, source_date)
    except (ValueError, ImportError) as error:
        raise SyncError(f"invalid campaign source {summary_path}: {error}") from error
    if product_path.resolve() != raw_path.resolve():
        products = read_json(product_path)
        if not isinstance(products, list):
            raise SyncError(f"campaign derived products must be an array: {product_path}")
        positions: list[int] = []
        for row in products:
            if not isinstance(row, dict):
                raise SyncError(f"campaign derived product is not an object: {product_path}")
            position = row.get("source_position")
            if type(position) is not int or position < 1 or position > len(raw_products) or row.get("account") != account:
                raise SyncError(f"campaign derived product account/source_position mismatch: {product_path}")
            projected = {name: row[name] for name in PRODUCT_FIELDS if name in row}
            original = raw_products[position - 1]
            if json.dumps(projected, sort_keys=True) != json.dumps(original, sort_keys=True):
                raise SyncError(f"campaign derived product differs from original 13 fields: {product_path} position {position}")
            positions.append(position)
        if positions != sorted(set(positions)):
            raise SyncError(f"campaign derived product source order changed: {product_path}")
    return config


def product_category_map(paths: list[Path], account_dir: Path) -> dict[str, str]:
    """Backward-compatible category-only view of the product metadata."""
    return {
        asin: category
        for asin, row in product_metadata_map(paths, account_dir).items()
        if (category := normalize_category(row.get("category")))
    }


def load_history(path: Path, account_id: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema": "note-amazon-asin-history-v1",
            "updated_at": "",
            "description": f"Verified note post/reservation ASIN ledger for {account_id}.",
            "posted": [],
        }
    value = read_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("posted", []), list):
        raise SyncError(f"invalid history schema: {path}")
    return value


def merge_history(
    history: dict[str, Any], incoming: list[dict[str, Any]], account_id: str
) -> tuple[dict[str, Any], int, int, bool]:
    raw_rows = history.get("posted", [])
    if not isinstance(raw_rows, list):
        raise SyncError("history posted must be an array")
    existing_rows: list[dict[str, Any]] = []
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, raw_row in enumerate(raw_rows, 1):
        if not isinstance(raw_row, dict):
            raise SyncError(f"history row {index} is not an object")
        row = dict(raw_row)
        asin = extract_asin(row)
        if not asin:
            raise SyncError(f"history row {index} has no valid ASIN")
        row["asin"] = asin
        key = history_key(row)
        if key in indexed:
            raise SyncError(f"history contains duplicate event key: {'|'.join(key)}")
        indexed[key] = row
        existing_rows.append(row)

    added = 0
    updated = 0
    for entry in incoming:
        key = history_key(entry)
        if key not in indexed:
            indexed[key] = dict(entry)
            added += 1
            continue
        old = indexed[key]
        merged = dict(old)
        status_rank = {"scheduled": 1, "reserved": 2, "published": 3, "posted": 4}
        if status_rank.get(str(entry.get("status", "")), 0) >= status_rank.get(str(old.get("status", "")), 0):
            merged["status"] = entry["status"]
        for name in (
            "posted_at",
            "reserved_at",
            "account_id",
            "account_name",
            "category",
        ):
            if entry.get(name) not in (None, ""):
                merged[name] = entry[name]
        if entry.get("product_identity"):
            combined_identity = merge_product_identities(
                extract_product_identity(old),
                extract_product_identity(entry),
            )
            if combined_identity.usable:
                merged["product_identity"] = combined_identity.to_dict()
        if merged != old:
            indexed[key] = merged
            updated += 1

    rows = list(indexed.values())
    rows.sort(key=lambda row: (event_date(row), str(row.get("reserved_at") or ""), str(row.get("asin") or "")))
    changed = added > 0 or updated > 0 or rows != existing_rows
    output = dict(history)
    output["schema"] = "note-amazon-asin-history-v1"
    output["description"] = (
        f"Canonical ASIN ledger for {account_id}. Product exclusion uses only verified "
        "note posts/reservations; scraped and rejected legacy rows are not exclusion evidence."
    )
    output["posted"] = rows
    if changed:
        output["updated_at"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
    return output, added, updated, changed


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temp_path, path)


def sync(
    repo_dir: Path,
    account_id: str,
    account_name: str,
    source_paths: list[Path],
    product_paths: list[Path],
    require_category: bool,
    dry_run: bool,
    external_selection: bool = False,
) -> dict[str, Any]:
    if external_selection and require_category:
        raise SyncError("external-selection and require-category cannot be combined")
    account_number = account_id.removeprefix("account")
    account_dir = repo_dir / "data" / account_id
    history_path = account_dir / "asin_history.json"
    rotation_path = account_dir / "category_rotation.json"
    config_path = repo_dir / f"categories{account_number}.yaml"

    accepted: list[dict[str, Any]] = []
    direct_categories: dict[str, str] = {}
    external_source_categories: dict[str, str] = {}
    successful_rows: dict[str, list[tuple[dict[str, Any], str]]] = {}
    skipped = 0
    for source_path in source_paths:
        if not source_path.is_file():
            raise SyncError(f"source JSON not found: {source_path}")
        rows = array_items(read_json(source_path))
        for index, row in enumerate(rows, 1):
            entry, category = row_to_entry(row, account_id, account_name, source_path.name, index)
            if entry is None:
                skipped += 1
                continue
            if external_selection:
                validate_external_account(row, account_id, f"{source_path.name} row {index}")
                raw_category = str(row.get("category") or "").strip()
                prior = external_source_categories.get(entry["asin"])
                if raw_category and prior and raw_category != prior:
                    raise SyncError(f"conflicting result categories for {entry['asin']}")
                if raw_category:
                    external_source_categories[entry["asin"]] = raw_category
            accepted.append(entry)
            successful_rows.setdefault(entry["asin"], []).append((row, f"{source_path.name} row {index}"))
            if category:
                direct_categories[entry["asin"]] = category

    if not accepted:
        return {
            "ok": True,
            "account": account_id,
            "accepted_count": 0,
            "accepted_asins": [],
            "accepted_events": [],
            "added": 0,
            "updated": 0,
            "skipped": skipped,
            "history_changed": False,
            "rotation_changed": False,
            "rotation_warning": None,
            "rotation_state": {},
            **({
                "external_selection": True,
                "rotation_applicable": False,
                "rotation_matched_asins": [],
                "rotation_skip_reason": "external_selection",
            } if external_selection else {}),
        }

    if external_selection:
        return sync_external_selection(
            history_path, account_id, accepted, product_paths,
            external_source_categories, skipped, dry_run,
        )

    history = load_history(history_path, account_id)
    history_output, added, updated, history_changed = merge_history(history, accepted, account_id)

    if not config_path.is_file():
        raise SyncError(f"category config not found: {config_path}")
    with config_path.open("r", encoding="utf-8-sig") as file:
        config = yaml.safe_load(file) or {}
    categories = config.get("categories", [])
    selection_mode = str(
        config.get("filters", {}).get("selection_mode", "category_round_robin")
    ).strip()
    if selection_mode not in {"category_round_robin", "category_quota", "global_ranked"}:
        raise SyncError(f"unsupported selection_mode: {selection_mode or '(empty)'}")
    active_names = {
        normalize_category(category.get("name"))
        for category in categories
        if isinstance(category, dict) and category.get("name") and category.get("url")
    }
    product_metadata, product_sources = product_metadata_sources(product_paths, account_dir)
    source_configs: dict[Path, dict[str, Any]] = {}
    campaign_configs: dict[str, dict[str, Any]] = {}
    for product_path in product_paths:
        source_configs[product_path] = product_source_config(product_path, config, int(account_number))
    campaign_input_present = any(row.get("_sale_campaign") for row in source_configs.values())
    for entry in accepted:
        product_path = product_sources.get(entry["asin"])
        if campaign_input_present and product_path not in product_paths:
            raise SyncError(f"campaign successful ASIN is absent from explicit product JSON: {entry['asin']}")
        if product_path is None:
            continue
        if product_path not in source_configs:
            source_configs[product_path] = product_source_config(product_path, config, int(account_number))
        source_config = source_configs[product_path]
        if source_config.get("_sale_campaign"):
            campaign_configs[entry["asin"]] = source_config
            for row, label in successful_rows[entry["asin"]]:
                validate_external_account(row, account_id, label)
                result_category = normalize_category(row.get("category"))
                product_category = normalize_category(product_metadata[entry["asin"]].get("category"))
                if result_category and result_category != product_category:
                    raise SyncError(f"campaign result/product category mismatch for {entry['asin']}: {label}")
    # A delayed sale sync uses the source's captured config even after the
    # campaign ends. Legacy inputs always retain their ordinary account config.
    rotation_entries = [entry for entry in accepted if entry["asin"] not in campaign_configs]
    rotation_applicable = selection_mode == "category_round_robin" and bool(rotation_entries)
    mapped_categories = {
        asin: normalize_category(row.get("category"))
        for asin, row in product_metadata.items()
        if normalize_category(row.get("category"))
    }
    mapped_categories.update(direct_categories)
    resolved: dict[str, str] = {}
    missing: list[str] = []
    required_missing: list[str] = []
    for entry in accepted:
        asin = entry["asin"]
        product_row = product_metadata.get(asin)
        if product_row:
            identity = extract_product_identity(product_row)
            if identity.usable:
                entry["product_identity"] = identity.to_dict()
        category = normalize_category(mapped_categories.get(asin))
        campaign_config = campaign_configs.get(asin)
        entry_active_names = active_names
        entry_selection_mode = selection_mode
        if campaign_config is not None:
            entry_active_names = {
                normalize_category(row.get("name"))
                for row in campaign_config.get("categories", [])
                if isinstance(row, dict) and row.get("name") and row.get("url")
            }
            entry_selection_mode = str(campaign_config.get("filters", {}).get("selection_mode") or "")
            product_category = normalize_category(product_row.get("category")) if product_row else ""
            if asin in direct_categories and direct_categories[asin] != product_category:
                raise SyncError(f"campaign result/product category mismatch for {asin}")
            category = product_category
        if category in entry_active_names:
            resolved[asin] = category
            entry["category"] = category
        else:
            missing.append(asin)
            if require_category or entry_selection_mode == "global_ranked":
                required_missing.append(asin)
    missing = list(dict.fromkeys(missing))
    if required_missing:
        raise SyncError("successful ASINs have no active category mapping: " + ",".join(dict.fromkeys(required_missing)))

    # Re-merge after category resolution so the ledger records the exact shelf used.
    history_output, added, updated, history_changed = merge_history(history, accepted, account_id)
    current_state = read_json(rotation_path) if rotation_applicable and rotation_path.exists() else {}
    rotation_output = current_state
    rotation_changed = False
    rotation_warning = None
    matched_asins: list[str] = []
    if not rotation_applicable:
        # Global ranking and fixed quotas do not use a shelf cursor. Preserve
        # any old cursor file while recording verified posts and their identity.
        matched_asins = []
    elif missing:
        rotation_warning = "cursor unchanged because some successful ASINs lack an active category: " + ",".join(missing)
    else:
        ordered_accepted = [
            entry for _, entry in sorted(
                enumerate(rotation_entries), key=lambda pair: (parse_event_instant(event_timestamp(pair[1])), pair[0])
            )
        ]
        accepted_asins_in_order = [entry["asin"] for entry in ordered_accepted]
        candidate_state, matched_asins = build_updated_state(
            categories, current_state, accepted_asins_in_order, resolved
        )
        incoming_event_at = event_timestamp(ordered_accepted[-1])
        current_event_at = str(current_state.get("last_event_at") or "")
        if current_event_at and parse_event_instant(incoming_event_at) < parse_event_instant(current_event_at):
            rotation_warning = (
                f"cursor unchanged for stale result ({incoming_event_at} < {current_event_at})"
            )
            matched_asins = []
        elif matched_asins:
            candidate_state["last_event_at"] = incoming_event_at
            comparable_current = dict(current_state)
            comparable_candidate = dict(candidate_state)
            comparable_current.pop("updated_at", None)
            comparable_candidate.pop("updated_at", None)
            if comparable_candidate != comparable_current:
                rotation_output = candidate_state
                rotation_changed = True

    if not dry_run:
        if history_changed:
            atomic_write_json(history_path, history_output)
        if rotation_changed:
            atomic_write_json(rotation_path, rotation_output)

    accepted_events = [
        {
            "asin": entry["asin"],
            "status": entry["status"],
            "posted_at": entry.get("posted_at"),
            "reserved_at": entry.get("reserved_at"),
            "event_date": event_date(entry),
            "account_id": account_id,
            "category": entry.get("category"),
        }
        for entry in accepted
    ]
    return {
        "ok": True,
        "account": account_id,
        "accepted_count": len(accepted),
        "accepted_asins": list(dict.fromkeys(entry["asin"] for entry in accepted)),
        "accepted_events": accepted_events,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "history_changed": history_changed,
        "rotation_changed": rotation_changed,
        "rotation_applicable": rotation_applicable,
        "rotation_matched_asins": matched_asins,
        "rotation_warning": rotation_warning,
        "rotation_state": {
            "last_asin": rotation_output.get("last_asin"),
            "last_category_name": rotation_output.get("last_category_name"),
            "last_category_position": rotation_output.get("last_category_position"),
            "next_category_name": rotation_output.get("next_category_name"),
            "next_category_position": rotation_output.get("next_category_position"),
            "last_event_at": rotation_output.get("last_event_at"),
        },
        "dry_run": dry_run,
        **({
            "campaign_source_asins": list(dict.fromkeys(entry["asin"] for entry in accepted if entry["asin"] in campaign_configs)),
            "campaign_ids": sorted({row["_sale_campaign"]["id"] for row in campaign_configs.values()}),
        } if campaign_configs else {}),
    }


def validate_external_account(row: dict[str, Any], account_id: str, label: str) -> None:
    expected = account_id.removeprefix("account")
    for key in ("account", "account_id", "assigned_account"):
        value = row.get(key)
        if value is not None and str(value).strip():
            got = str(value).strip().removeprefix("account")
            if got != expected:
                raise SyncError(f"external selection account mismatch in {label}: {key}={value}")


def sync_external_selection(
    history_path: Path,
    account_id: str,
    accepted: list[dict[str, Any]],
    product_paths: list[Path],
    source_categories: dict[str, str],
    skipped: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Record verified externally selected products without reading or moving a shelf cursor."""
    if not product_paths:
        raise SyncError("external selection requires explicit product JSON")
    metadata: dict[str, dict[str, Any]] = {}
    for product_path in product_paths:
        for index, row in enumerate(array_items(read_json(product_path)), 1):
            label = f"{product_path.name} row {index}"
            validate_external_account(row, account_id, label)
            asin = extract_asin(row)
            category = str(row.get("category") or "").strip()
            if not asin or not category:
                raise SyncError(f"external selection product requires ASIN and nonempty category: {label}")
            prior = metadata.get(asin)
            if prior and str(prior.get("category") or "").strip() != category:
                raise SyncError(f"conflicting external product categories for {asin}")
            metadata[asin] = row

    for entry in accepted:
        asin = entry["asin"]
        product = metadata.get(asin)
        if product is None:
            raise SyncError(f"verified external ASIN is absent from explicit product JSON: {asin}")
        category = str(product["category"]).strip()
        source_category = source_categories.get(asin)
        if source_category and source_category != category:
            raise SyncError(f"external result/product category mismatch for {asin}: {source_category} / {category}")
        entry["category"] = category
        identity = extract_product_identity(product)
        if identity.usable:
            entry["product_identity"] = identity.to_dict()

    history = load_history(history_path, account_id)
    output, added, updated, changed = merge_history(history, accepted, account_id)
    if changed and not dry_run:
        atomic_write_json(history_path, output)
    return {
        "ok": True,
        "account": account_id,
        "external_selection": True,
        "accepted_count": len(accepted),
        "accepted_asins": list(dict.fromkeys(entry["asin"] for entry in accepted)),
        "accepted_events": [
            {
                "asin": entry["asin"], "status": entry["status"],
                "posted_at": entry.get("posted_at"), "reserved_at": entry.get("reserved_at"),
                "event_date": event_date(entry), "account_id": account_id,
                "category": entry["category"],
            }
            for entry in accepted
        ],
        "added": added, "updated": updated, "skipped": skipped,
        "history_changed": changed,
        "rotation_changed": False,
        "rotation_applicable": False,
        "rotation_matched_asins": [],
        "rotation_warning": None,
        "rotation_skip_reason": "external_selection",
        "rotation_state": {},
        "dry_run": dry_run,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--account", required=True, choices=[f"account{i}" for i in range(1, 21)])
    parser.add_argument("--account-name", default="")
    parser.add_argument("--source-json", action="append", required=True)
    parser.add_argument("--product-json", action="append", default=[])
    parser.add_argument("--require-category", action="store_true")
    parser.add_argument("--external-selection", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = sync(
            repo_dir=Path(args.repo_dir).resolve(),
            account_id=args.account,
            account_name=args.account_name or args.account,
            source_paths=[Path(path).resolve() for path in args.source_json],
            product_paths=[Path(path).resolve() for path in args.product_json],
            require_category=args.require_category,
            dry_run=args.dry_run,
            external_selection=args.external_selection,
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, SyncError) as error:
        print(f"ASIN_SYNC_ERROR={error}", file=sys.stderr)
        return 2
    print("ASIN_SYNC_RESULT=" + json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
