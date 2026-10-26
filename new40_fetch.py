"""Fetch immutable new40 sources using only the pinned new40 selection history.

The default CLI operation is an offline preflight.  This module never invokes
the legacy fetch-and-save route or records candidates as publication history.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import re
import subprocess
from contextlib import ExitStack
from dataclasses import asdict, is_dataclass
from datetime import date
from pathlib import Path

import yaml

from new40_history import read_new40_selection_context
from sale_campaign import (ACTUAL_SALE_PRICE_POLICY, actual_sale_price_rejection,
                           apply_campaign_config)


SOURCE_ACCOUNTS = frozenset(set(range(1, 21)) - {10, 13, 14})
PRODUCT_FIELDS = (
    "asin", "title", "price", "price_int", "original_price", "discount_rate",
    "image_url", "affiliate_url", "category", "rating", "review_count",
    "description", "specs",
)


class New40FetchError(ValueError):
    """The immutable new40 source contract could not be established."""


def _git_blob(repo: Path, commit: str, git_path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{git_path}"],
        capture_output=True, check=False, timeout=30,
    )
    if result.returncode:
        raise New40FetchError(f"pinned Git input is unavailable: {git_path}")
    return result.stdout


def _same_config(local: bytes, pinned: bytes) -> bool:
    # Git's Windows checkout may use CRLF. No other content normalization is
    # permitted, and the evidence digest always identifies the Git blob bytes.
    return local.replace(b"\r\n", b"\n") == pinned.replace(b"\r\n", b"\n")


def _output_paths(repo: Path, account: int, reference_date: str) -> tuple[Path, Path]:
    directory = repo / "data" / "new40" / "sources" / f"account{account}"
    paths = (directory / f"products_{reference_date}.json",
             directory / f"scrape_summary_{reference_date}.json")
    for path in paths:
        if path.resolve() != path.absolute():
            raise New40FetchError("new40 source output must not traverse a link")
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"immutable new40 source already exists: {path}")
    return paths


def _summary(stats: dict, reference_date: str, total: int,
             selection_evidence: dict, config_evidence: dict) -> dict:
    """Keep the existing summary contract without calling its ledger writer."""
    remaining = copy.deepcopy(stats)
    marker = remaining.pop("_new40_selection", None)
    if marker != selection_evidence:
        raise New40FetchError("fetch did not attest to the pinned new40 selection context")
    policy = remaining.pop("_selection_policy", None)
    if not isinstance(policy, dict) or policy.get("actual_sale_price_policy") != ACTUAL_SALE_PRICE_POLICY:
        raise New40FetchError("fetch omitted the actual sale price policy")
    campaign = remaining.pop("_campaign", None)
    detail = remaining.pop("_detail_offer_verification", None)
    detail_retry = remaining.pop("_detail_retry", None)
    discount = remaining.pop("_discount_contract", None)
    coupon_search = remaining.pop("_coupon_search", None)
    supply = remaining.pop("_supply_completion", None)
    result = {
        "date": reference_date, "total_taken": total, "selection_policy": policy,
        "excluded_loaded": remaining.pop("_excluded_loaded", 0),
        "excluded_gtins_loaded": remaining.pop("_excluded_gtins_loaded", 0),
        "excluded_brand_models_loaded": remaining.pop("_excluded_brand_models_loaded", 0),
        "skipped_product_identity": remaining.pop("_skipped_product_identity", 0),
        "skipped_product_identity_reasons": remaining.pop("_skipped_product_identity_reasons", {}),
        "categories": remaining,
        "new40_selection": copy.deepcopy(selection_evidence),
        "source_config": dict(config_evidence),
    }
    if campaign is not None:
        result["campaign"] = campaign
    if detail is not None:
        result["detail_offer_verification"] = detail
        if policy.get("offer_scope") == "all_discounts":
            result["sale_name"] = "Amazon セール"
    if detail_retry is not None:
        result["detail_retry"] = detail_retry
    if supply is not None:
        result["supply_completion"] = supply
    if discount is not None:
        discount["date"] = reference_date
        result["discount_contract"] = discount
        result["sale_name"] = "Amazon セール"
        result["coupon_search"] = coupon_search
        rows = discount.get("products", [])
        if total and rows and all(row.get("prime_event", {}).get("verified") is True for row in rows):
            labels = {row["prime_event"]["label"] for row in rows}
            if len(labels) == 1:
                result["sale_name"] = "Amazon " + next(iter(labels))
    return result


def _products_for_save(products: list, summary: dict, config: dict) -> list[dict]:
    if not isinstance(products, list) or not 0 <= len(products) <= 10:
        raise New40FetchError("new40 sources must contain zero through ten products")
    filters = config.get("filters", {})
    limit = filters.get("max_total_items", 10)
    if type(limit) is not int or not 0 <= limit <= 10 or len(products) > limit:
        raise New40FetchError("product count exceeds the unchanged source config limit")
    global_floor = int(filters.get("min_price", 3000))
    floors = {
        str(category["name"]): max(8001, global_floor, int(category.get("min_price", global_floor)))
        for category in config.get("categories", [])
    }
    contract = summary.get("discount_contract")
    if filters.get("discount_contract") and not isinstance(contract, dict):
        raise New40FetchError("fetch omitted the configured discount contract")
    rows = contract.get("products", []) if isinstance(contract, dict) else []
    if contract is not None and (not isinstance(rows, list) or len(rows) != len(products)):
        raise New40FetchError("discount evidence does not match the product sequence")
    saved, seen = [], set()
    for position, product in enumerate(products, 1):
        record = asdict(product) if is_dataclass(product) else copy.deepcopy(product)
        if not isinstance(record, dict) or set(record) != set(PRODUCT_FIELDS):
            raise New40FetchError("product must have exactly the canonical thirteen fields")
        asin = record["asin"]
        if not isinstance(asin, str) or not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen:
            raise New40FetchError("product ASIN is invalid or duplicated")
        seen.add(asin)
        coupons = [getattr(product, "_actual_sale_coupon", None)]
        if contract is not None:
            row = rows[position - 1]
            if not isinstance(row, dict) or row.get("asin") != asin or row.get("source_position") != position:
                raise New40FetchError("discount evidence order changed")
            coupons.append(row.get("coupon"))
        for coupon in coupons:
            reason = actual_sale_price_rejection(record, coupon)
            if reason:
                raise New40FetchError(f"{asin}: {reason}")
        category = str(record["category"]).split("#")[0]
        if category not in floors:
            raise New40FetchError("product category is absent from the source config")
        payable = min([record["price_int"]] + [coupon["final_price_yen"] for coupon in coupons
                                              if isinstance(coupon, dict) and coupon.get("status") == "verified"])
        if payable < floors[category]:
            raise New40FetchError(f"{asin}: actual sale price is below the existing category floor")
        record["affiliate_url"] = None
        saved.append(record)
    return saved


def run_new40_fetch(repo_dir: str | Path, commit: str, source_account: int,
                    reference_date: str, *, execute_fetch: bool = False) -> dict:
    """Preflight, or explicitly fetch one immutable daily source in source order."""
    if type(source_account) is not int or source_account not in SOURCE_ACCOUNTS:
        raise New40FetchError("source account must be 1..20 except 10, 13 and 14")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise New40FetchError("an immutable full lowercase Git commit is required")
    if not isinstance(reference_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", reference_date):
        raise New40FetchError("reference date must be YYYY-MM-DD")
    date.fromisoformat(reference_date)
    if type(execute_fetch) is not bool:
        raise New40FetchError("execute_fetch must be an explicit boolean")
    repo = Path(repo_dir).resolve(strict=True)
    product_path, summary_path = _output_paths(repo, source_account, reference_date)
    git_path = f"categories{source_account}.yaml"
    config_path = repo / git_path
    pinned = _git_blob(repo, commit, git_path)
    if not _same_config(config_path.read_bytes(), pinned):
        raise New40FetchError("working source config differs from its pinned Git content")
    config = yaml.safe_load(pinned.decode("utf-8"))
    if not isinstance(config, dict):
        raise New40FetchError("source config must be an object")
    effective = apply_campaign_config(config, config_path, reference_date)
    exclusion = effective.get("exclusion", {})
    within_days = exclusion.get("exclude_within_days", 0)
    identities = exclusion.get("exclude_product_identifiers", False)
    if type(within_days) is not int or within_days < 0 or type(identities) is not bool:
        raise New40FetchError("source exclusion policy is invalid")
    context = read_new40_selection_context(
        repo, commit, source_account, reference_date=reference_date,
        within_days=within_days, include_product_identities=identities,
    )
    evidence = context.public_evidence()
    config_evidence = {"git_path": git_path, "git_commit": commit,
                       "sha256": hashlib.sha256(pinned).hexdigest()}
    result = {
        "status": "DRY_RUN_READY", "execute_fetch": execute_fetch,
        "source_account": source_account, "date": reference_date,
        "products_path": str(product_path), "summary_path": str(summary_path),
        "new40_selection": evidence, "source_config": config_evidence,
    }
    if not execute_fetch:
        return result

    # Exclusive placeholders reserve both immutable outputs before any network
    # call. A failed attempt deliberately leaves an incomplete, non-reusable
    # source; neither this invocation nor a rerun can refill/overwrite it.
    import scraper
    product_path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        product_file = stack.enter_context(product_path.open("xb"))
        summary_file = stack.enter_context(summary_path.open("xb"))
        if not _same_config(config_path.read_bytes(), pinned):
            raise New40FetchError("source config changed before fetch")
        products, stats = asyncio.run(scraper.fetch_products(
            str(config_path), associate_tag="", reference_date=reference_date,
            new40_selection_context=context,
        ))
        if not _same_config(config_path.read_bytes(), pinned):
            raise New40FetchError("source config changed during fetch")
        summary = _summary(stats, reference_date, len(products), evidence, config_evidence)
        saved = _products_for_save(products, summary, effective)
        # Encode the complete pair before either reserved file receives data.
        product_bytes = (json.dumps(saved, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        summary_bytes = (json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        product_file.write(product_bytes)
        summary_file.write(summary_bytes)
    return {**result, "status": "SOURCE_SAVED", "total_taken": len(saved)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--commit", required=True)
    parser.add_argument("--source-account", type=int, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--execute-fetch", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_new40_fetch(args.repo, args.commit, args.source_account,
                                 args.date, execute_fetch=args.execute_fetch)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"new40 fetch stopped: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
