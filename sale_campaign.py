"""Date-bounded campaign overlay and validation of its frozen source metadata.

The normal account config and its exclusion ledger remain authoritative.  A
campaign only overlays acquisition/selection settings for accounts 1 through 20.
Frozen sources replay the campaign for their own date, even after the event ends;
they are never reselected or reordered here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


JST = timezone(timedelta(hours=9))
CAMPAIGN_CONFIG_PATH = Path(__file__).with_name("sale_campaign.yaml")
CAMPAIGN_ID = "prime_appreciation_2026"
CAMPAIGN_POLICY = "prime_appreciation_2026_electronics_v1"
_STARTS_AT = "2026-10-13T00:00:00+09:00"
_ENDS_AT = "2026-10-20T00:00:00+09:00"


class CampaignConfigError(ValueError):
    """Campaign config or frozen source does not match the approved contract."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise CampaignConfigError("campaign metadata must contain JSON values") from error


def _today_jst() -> Date:
    return datetime.now(JST).date()


def _reference_day(value: Any, *, required: bool = False) -> Date:
    if value is None:
        if required:
            raise CampaignConfigError("frozen campaign source date is required")
        return _today_jst()
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=JST)
        return aware.astimezone(JST).date()
    if isinstance(value, Date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return Date.fromisoformat(value)
        except ValueError as error:
            raise CampaignConfigError("campaign date is invalid") from error
    raise CampaignConfigError("campaign date must be an ISO date or date/datetime value")


def _account_number(value: Any) -> int | None:
    if type(value) is int:
        return value if 1 <= value <= 20 else None
    if isinstance(value, str):
        match = re.fullmatch(r"(?:account)?([1-9]|1[0-9]|20)", value)
        if match:
            return int(match.group(1))
    return None


def _load_campaign() -> tuple[dict[str, Any], str]:
    try:
        campaign = yaml.safe_load(CAMPAIGN_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as error:
        raise CampaignConfigError("cannot read canonical sale_campaign.yaml") from error
    if not isinstance(campaign, dict):
        raise CampaignConfigError("sale campaign root must be an object")
    fixed = {
        "schema": 1, "id": CAMPAIGN_ID, "policy": CAMPAIGN_POLICY,
        "timezone": "Asia/Tokyo", "starts_at": _STARTS_AT, "ends_at": _ENDS_AT,
        "min_price": 8000, "selection_mode": "global_ranked", "sort_order": "sale_first",
        "max_total_items": 10, "max_per_category": 2, "max_items": 20,
        "allocation_method": "daily_rotation_20_nodes_offsets_0_1_4_5_shift_3",
        "daily_shift": 3, "offsets": [0, 1, 4, 5],
        "official_source": "https://www.aboutamazon.jp/news/amazon-prime/prime-appreciation-sale-2026",
    }
    for key, expected in fixed.items():
        if _canonical_json(campaign.get(key)) != _canonical_json(expected):
            raise CampaignConfigError(f"sale campaign {key} violates the approved policy")
    nodes = campaign.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 20:
        raise CampaignConfigError("sale campaign must define exactly 20 category nodes")
    names: set[str] = set()
    node_ids: set[str] = set()
    for position, node in enumerate(nodes):
        if not isinstance(node, dict) or set(node) != {"name", "node", "family"}:
            raise CampaignConfigError("campaign node must contain name, node, and family")
        name, node_id = node.get("name"), node.get("node")
        family = "pc" if position % 2 == 0 else "appliance"
        if not isinstance(name, str) or not name or name != name.strip() or name in names:
            raise CampaignConfigError("campaign category names must be nonempty and unique")
        if not isinstance(node_id, str) or not re.fullmatch(r"[1-9][0-9]*", node_id) or node_id in node_ids:
            raise CampaignConfigError("campaign category nodes must be decimal strings and unique")
        if node.get("family") != family:
            raise CampaignConfigError("campaign categories must alternate PC and appliance families")
        names.add(name)
        node_ids.add(node_id)
    if set(campaign) != set(fixed) | {"nodes"}:
        raise CampaignConfigError("sale campaign contains unsupported policy keys")
    digest = hashlib.sha256(_canonical_json(campaign).encode("utf-8")).hexdigest()
    return campaign, digest


def _active_on(campaign: dict[str, Any], day: Date) -> bool:
    return Date.fromisoformat(campaign["starts_at"][:10]) <= day < Date.fromisoformat(campaign["ends_at"][:10])


def _marker(campaign: dict[str, Any], digest: str, account: int, day: Date) -> dict[str, Any]:
    elapsed = (day - Date.fromisoformat(campaign["starts_at"][:10])).days
    start = (account - 1 + campaign["daily_shift"] * elapsed) % len(campaign["nodes"])
    categories = []
    for offset in campaign["offsets"]:
        node = campaign["nodes"][(start + offset) % len(campaign["nodes"])]
        categories.append({
            "name": node["name"],
            "url": f"https://www.amazon.co.jp/s?rh=n%3A{node['node']}%2Cp_n_deal_type%3A10343616051%2Cp_36%3A800000-",
            "is_search": True, "max_items": campaign["max_items"], "min_price": campaign["min_price"],
        })
    return {
        "schema": campaign["schema"], "id": campaign["id"], "policy": campaign["policy"],
        "account": account, "date": day.isoformat(),
        "starts_at": campaign["starts_at"], "ends_at": campaign["ends_at"],
        "min_price": campaign["min_price"], "selection_mode": campaign["selection_mode"],
        "categories": categories, "allocation_method": campaign["allocation_method"],
        "official_source": campaign["official_source"], "config_sha256": digest,
    }


def _overlay(base_config: dict[str, Any], marker: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(base_config, dict):
        raise CampaignConfigError("base account config must be an object")
    result = copy.deepcopy(base_config)
    filters = result.setdefault("filters", {})
    if not isinstance(filters, dict):
        raise CampaignConfigError("base account filters must be an object")
    filters.update({
        "selection_mode": "global_ranked", "sort_order": "sale_first", "max_total_items": 10,
        "max_per_category": 2, "min_price": 8000,
    })
    result["categories"] = copy.deepcopy(marker["categories"])
    result["_sale_campaign"] = copy.deepcopy(marker)
    return result


def apply_campaign_config(config: dict[str, Any], config_path: str | Path, reference_date: Any = None) -> dict[str, Any]:
    """Return a deep-copy overlay only for categories1..20 and campaign dates."""
    if not isinstance(config, dict):
        raise CampaignConfigError("base account config must be an object")
    # PureWindowsPath semantics also work when this repository runs on Linux.
    filename = re.split(r"[/\\]", str(config_path))[-1]
    match = re.fullmatch(r"categories([1-9]|1[0-9]|20)\.yaml", filename)
    if not match:
        return copy.deepcopy(config)
    account = int(match.group(1))
    day = _reference_day(reference_date)
    campaign, digest = _load_campaign()
    if not _active_on(campaign, day):
        return copy.deepcopy(config)
    return _overlay(config, _marker(campaign, digest, account, day))


def source_campaign_config(
    base_config: dict[str, Any], summary: dict[str, Any] | None,
    products: list[dict[str, Any]], account: Any, date: Any,
) -> dict[str, Any]:
    """Validate and replay an existing source's marker without changing products.

    A marker-less legacy source keeps its base config.  A claimed campaign source
    must match all metadata fields, account, source date, selected categories, and
    the sales-price floor.  Current wall-clock time has no effect on replay.
    Extra derived product keys are preserved and do not affect validation.
    """
    if not isinstance(base_config, dict):
        raise CampaignConfigError("base account config must be an object")
    if summary is None or (isinstance(summary, dict) and "campaign" not in summary):
        return copy.deepcopy(base_config)
    if not isinstance(summary, dict) or not isinstance(summary.get("campaign"), dict):
        raise CampaignConfigError("source campaign marker must be an object")
    account_number = _account_number(account)
    if account_number is None:
        raise CampaignConfigError("source campaign account must be account1 through account20")
    day = _reference_day(date, required=True)
    if summary.get("date") != day.isoformat():
        raise CampaignConfigError("campaign summary date does not match the fixed source date")
    campaign, digest = _load_campaign()
    if not _active_on(campaign, day):
        raise CampaignConfigError("claimed campaign source date is outside the campaign period")
    expected = _marker(campaign, digest, account_number, day)
    if _canonical_json(summary["campaign"]) != _canonical_json(expected):
        raise CampaignConfigError("campaign marker does not match account, date, categories, or policy")
    if "selection_policy" in summary:
        policy = summary["selection_policy"]
        if not isinstance(policy, dict):
            raise CampaignConfigError("source selection_policy must be an object")
        for key, value in {
            "selection_mode": "global_ranked", "sort_order": "sale_first",
            "max_total_items": 10, "max_per_category": 2,
        }.items():
            if _canonical_json(policy.get(key)) != _canonical_json(value):
                raise CampaignConfigError(f"campaign source selection_policy.{key} is inconsistent")
        if "min_price" in policy and _canonical_json(policy["min_price"]) != "8000":
            raise CampaignConfigError("campaign source selection_policy.min_price is inconsistent")
    if not isinstance(products, list) or not 1 <= len(products) <= 10:
        raise CampaignConfigError("campaign products must be an array of 1 through 10 fixed products")
    if "total_taken" in summary and (type(summary["total_taken"]) is not int or summary["total_taken"] != len(products)):
        raise CampaignConfigError("campaign source total_taken does not match products")
    allowed_categories = {category["name"] for category in expected["categories"]}
    seen_asins: set[str] = set()
    for position, product in enumerate(products, 1):
        if not isinstance(product, dict):
            raise CampaignConfigError(f"campaign product {position} must be an object")
        price = product.get("price_int")
        if type(price) is not int or price < 8000:
            raise CampaignConfigError(f"campaign product {position} sales price must be an integer of at least 8000")
        category = product.get("category")
        if not isinstance(category, str) or category != category.strip():
            raise CampaignConfigError(f"campaign product {position} category must be a canonical string")
        category_name = re.sub(r"#[1-9][0-9]*$", "", category)
        if category_name not in allowed_categories:
            raise CampaignConfigError(f"campaign product {position} is outside its fixed category allocation")
        if "asin" in product:
            asin = product["asin"]
            if not isinstance(asin, str) or not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen_asins:
                raise CampaignConfigError(f"campaign product {position} ASIN is invalid or duplicated")
            seen_asins.add(asin)
    return _overlay(base_config, expected)
