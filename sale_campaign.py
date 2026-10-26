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
import unicodedata
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


JST = timezone(timedelta(hours=9))
CAMPAIGN_CONFIG_PATH = Path(__file__).with_name("sale_campaign.yaml")
CAMPAIGN_ID = "prime_appreciation_2026"
CAMPAIGN_POLICY = "prime_appreciation_2026_electronics_price_desc_v3"
PRIME_V2_POLICY = "prime_appreciation_2026_electronics_prime_verified_v2"
PRICE_POLICY = "profit_price_desc_v1"
ACTUAL_SALE_PRICE_POLICY = "actual_sale_price_gt_8000_v1"
ACTUAL_SALE_PRICE_EXCLUSIVE_MINIMUM = 8000
LEGACY_CAMPAIGN_POLICY = "prime_appreciation_2026_electronics_v1"
LEGACY_CONFIG_SHA256 = "6161f0c6ae9828bd7de6f6b8f1c06ffacb0245afff2744e4ad9392d7c37b1e6c"
PRIME_CONFIG_SHA256 = "8726b65d5e8cf2ebd9393a13f462e63c3087e6503597749730332a3e6cbbb8ea"
PRICE_CONFIG_SHA256 = "a3511e2163a71a3063ea42755d51a8fea9732155e6c6f58d8072af69f63b644a"
CONFIG_REGISTRY = {LEGACY_CAMPAIGN_POLICY: LEGACY_CONFIG_SHA256, PRIME_V2_POLICY: PRIME_CONFIG_SHA256, CAMPAIGN_POLICY: PRICE_CONFIG_SHA256}
_STARTS_AT = "2026-10-13T00:00:00+09:00"
_ENDS_AT = "2026-10-20T00:00:00+09:00"


class CampaignConfigError(ValueError):
    """Campaign config or frozen source does not match the approved contract."""


def actual_sale_price_rejection(product, coupon=None):
    """Fail closed on ambiguous current prices; never substitute reference prices.

    ``price_int`` and ``price`` are the canonical current offer. A verified
    coupon's payable price is an additional bound, never a guessed discount.
    This helper does not mutate, reorder, replace or replenish source products.
    """
    get = product.get if isinstance(product, dict) else lambda key: getattr(product, key, None)
    amount, display = get("price_int"), get("price")
    if type(amount) is not int or not 0 < amount <= 9007199254740991:
        return "actual_sale_price_unknown"
    if not isinstance(display, str):
        return "actual_sale_price_unknown"
    normalized = unicodedata.normalize("NFKC", display).strip()
    match = re.fullmatch(r"(?:[¥￥]\s*)?([1-9][0-9]{0,2}(?:,[0-9]{3})+|0|[1-9][0-9]*)(?:\s*円)?", normalized)
    if not match:
        return "actual_sale_price_unknown"
    if int(match[1].replace(",", "")) != amount:
        return "actual_sale_price_mismatch"
    if amount <= ACTUAL_SALE_PRICE_EXCLUSIVE_MINIMUM:
        return "actual_sale_price_at_or_below_8000"
    if isinstance(coupon, dict) and coupon.get("status") == "verified":
        final = coupon.get("final_price_yen")
        if type(final) is not int or not 0 < final <= amount:
            return "actual_coupon_price_unknown"
        if final <= ACTUAL_SALE_PRICE_EXCLUSIVE_MINIMUM:
            return "actual_coupon_price_at_or_below_8000"
    return None


def validate_actual_sale_price_policy(summary, products):
    """Apply the explicit new marker without reinterpreting historical sources."""
    policy = summary.get("selection_policy", {})
    if not isinstance(policy, dict) or "actual_sale_price_policy" not in policy:
        return
    if policy["actual_sale_price_policy"] != ACTUAL_SALE_PRICE_POLICY:
        raise CampaignConfigError("unregistered actual sale price policy")
    contract = summary.get("discount_contract")
    rows = contract.get("products", []) if isinstance(contract, dict) else []
    for position, product in enumerate(products, 1):
        matches = [row for row in rows if row.get("asin") == product.get("asin")]
        if contract is not None and (len(matches) != 1 or matches[0].get("source_position") != position):
            raise CampaignConfigError("missing or ambiguous discount row for actual sale price")
        reason = actual_sale_price_rejection(product, matches[0].get("coupon") if matches else None)
        if reason:
            raise CampaignConfigError(f"{product.get('asin', 'unknown')}: {reason}")


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
    if campaign.get("policy") not in CONFIG_REGISTRY:
        raise CampaignConfigError("unregistered campaign policy")
    fixed = {
        "schema": 1, "id": CAMPAIGN_ID, "policy": campaign["policy"],
        "timezone": "Asia/Tokyo", "starts_at": _STARTS_AT, "ends_at": _ENDS_AT,
        "min_price": 8000, "selection_mode": "global_ranked",
        "sort_order": "price_desc" if campaign["policy"] == CAMPAIGN_POLICY else "sale_first",
        "max_total_items": 10, "max_per_category": 0 if campaign["policy"] == CAMPAIGN_POLICY else 2, "max_items": 20,
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
    if digest != CONFIG_REGISTRY[campaign["policy"]]:
        raise CampaignConfigError("registered campaign configuration changed")
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
        "selection_mode": "global_ranked", "sort_order": "price_desc" if marker["policy"] == CAMPAIGN_POLICY else "sale_first", "max_total_items": 10,
        "max_per_category": 0 if marker["policy"] == CAMPAIGN_POLICY else 2, "min_price": 8000, "max_price": 0, "require_sale_info": True,
    })
    if marker["policy"] == CAMPAIGN_POLICY:
        filters["selection_policy_version"] = PRICE_POLICY
    else:
        filters.pop("selection_policy_version", None)
    if marker["policy"] in (PRIME_V2_POLICY, CAMPAIGN_POLICY):
        filters.update(require_prime_event=True, prime_event_source_date=marker["date"])
    else:
        filters.pop("require_prime_event", None)
        filters.pop("prime_event_source_date", None)
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
    source_policy = summary["campaign"].get("policy")
    if source_policy not in CONFIG_REGISTRY:
        raise CampaignConfigError("source campaign policy is unregistered")
    frozen_config = copy.deepcopy(campaign)
    frozen_config["policy"] = source_policy
    frozen_config["sort_order"] = "price_desc" if source_policy == CAMPAIGN_POLICY else "sale_first"
    frozen_config["max_per_category"] = 0 if source_policy == CAMPAIGN_POLICY else 2
    frozen_digest = hashlib.sha256(_canonical_json(frozen_config).encode("utf-8")).hexdigest()
    if frozen_digest != CONFIG_REGISTRY[source_policy]:
        raise CampaignConfigError("frozen campaign configuration changed")
    expected = _marker(frozen_config, frozen_digest, account_number, day)
    if _canonical_json(summary["campaign"]) != _canonical_json(expected):
        raise CampaignConfigError("campaign marker does not match account, date, categories, or policy")
    if "selection_policy" in summary:
        policy = summary["selection_policy"]
        if not isinstance(policy, dict):
            raise CampaignConfigError("source selection_policy must be an object")
        for key, value in {
            "selection_mode": "global_ranked", "sort_order": frozen_config["sort_order"],
            "max_total_items": 10, "max_per_category": frozen_config["max_per_category"],
        }.items():
            if _canonical_json(policy.get(key)) != _canonical_json(value):
                raise CampaignConfigError(f"campaign source selection_policy.{key} is inconsistent")
        if "min_price" in policy and _canonical_json(policy["min_price"]) != "8000":
            raise CampaignConfigError("campaign source selection_policy.min_price is inconsistent")
        if source_policy == CAMPAIGN_POLICY and any(policy.get(key) != value for key, value in {
            "selection_policy_version": PRICE_POLICY, "min_price": 8000, "max_price": 0, "require_sale_info": True,
        }.items()):
            raise CampaignConfigError("campaign price selection contract is missing")
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
    if source_policy == CAMPAIGN_POLICY:
        prices = [product["price_int"] for product in products]
        if prices != sorted(prices, reverse=True):
            raise CampaignConfigError("campaign products are not in descending sales-price order")
    if source_policy in (PRIME_V2_POLICY, CAMPAIGN_POLICY):
        try:
            # Controllers inject the validator from the same pinned Git commit.
            # Sealed v1 replay needs no new detail_offer module dependency.
            validator = globals().get("validate_discount_contract_summary")
            if validator is None:
                from detail_offer import validate_discount_contract_summary as validator
            valid, reason = validator(f"account{account_number}", products, summary)
        except (TypeError, ValueError, KeyError, AttributeError, ImportError) as error:
            raise CampaignConfigError("prime campaign PDP proof validation failed") from error
        if not valid:
            raise CampaignConfigError(reason)
    return _overlay(base_config, expected)
