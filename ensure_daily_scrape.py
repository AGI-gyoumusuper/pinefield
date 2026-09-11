"""Ensure today's scrape outputs exist and are usable.

This is a GitHub Actions safety net. It never falls back to older dates:
missing or invalid files are recreated for today's JST date only.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
TODAY = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
MIN_ITEMS = 4  # 現行20工場は4商品で運転。10件は取得目標であり必須件数ではない。
ACCOUNTS = tuple(f"account{number}" for number in range(1, 21))  # account1〜20（account0は退役）
MIN_ITEMS_BY_ACCOUNT = {}
REQUIRED_PRODUCT_FIELDS = frozenset(
    {
        "asin",
        "title",
        "price",
        "price_int",
        "original_price",
        "discount_rate",
        "image_url",
        "affiliate_url",
        "category",
        "rating",
        "review_count",
        "description",
        "specs",
    }
)
FAILURE_ARTIFACTS_ENV = "PINEFIELD_FAILURE_ARTIFACTS_DIR"
SEARCH_DIAGNOSTICS_ENV = "PINEFIELD_SEARCH_DIAGNOSTICS_DIR"


def run(
    command: list[str],
    root: Path = ROOT,
    environment: dict[str, str] | None = None,
) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=root, check=True, env=environment)


def load_json(path: Path):
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def annotate_search_diagnostics(account: str, today: str, attempt: int | str) -> None:
    """Label new public captures without resetting the scraper's shared two-page budget."""
    configured = os.environ.get(SEARCH_DIAGNOSTICS_ENV, "").strip()
    if not configured:
        return
    try:
        for number in (1, 2):
            capture = Path(configured) / f"failure-{number:02d}"
            context = capture / "context.json"
            if capture.is_dir() and not context.exists():
                write_report(context, {"account": account, "date": today, "attempt": attempt})
    except OSError as exc:
        print(f"public diagnostic context unavailable: {type(exc).__name__}", flush=True)


def archive_failure_candidate(account: str, root: Path, today: str, attempt: int | str,
                              *, valid: bool, reason: str, scrape_exit_code: int | None = None) -> bool:
    """Opt-in diagnostic copy only: never archive a ledger or promote a candidate."""
    configured = os.environ.get(FAILURE_ARTIFACTS_ENV, "").strip()
    if not configured:
        return False
    if account not in ACCOUNTS or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
        return False
    label = f"attempt-{attempt:02d}" if type(attempt) is int else "final"
    destination = Path(configured) / account / today / label
    try:
        destination.mkdir(parents=True, exist_ok=True)
        files = {}
        for name in (f"products_{today}.json", f"scrape_summary_{today}.json"):
            source = root / "data" / account / name
            if source.is_file() and not source.is_symlink():
                content = source.read_bytes()
                (destination / name).write_bytes(content)
                files[name] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
        write_report(destination / "validation.json", {
            "schema": "pinefield-failure-candidate-v1", "account": account, "date": today,
            "attempt": attempt, "validation_valid": valid, "reason": reason,
            "scrape_exit_code": scrape_exit_code, "diagnostic_only": True, "files": files,
        })
        return True
    except OSError as exc:
        # Evidence storage must not change scrape acceptance or retry behavior.
        print(f"{account}: failure candidate archive unavailable: {type(exc).__name__}", flush=True)
        return False


def valid_product_list(
    path: Path,
    account: str,
    min_items: int = MIN_ITEMS,
) -> tuple[bool, str, list[dict]]:
    if not path.exists():
        return False, f"missing: {path}", []
    try:
        data = load_json(path)
    except Exception as exc:
        return False, f"invalid json: {path}: {exc}", []
    if not isinstance(data, list):
        return False, f"not list: {path}", []
    if len(data) < min_items:
        return False, f"too few items: {path}: {len(data)} < {min_items}", []
    if len(data) > 10:
        return False, f"too many items: {path}: {len(data)} > 10", []

    asins: list[str] = []
    account_number = account.removeprefix("account")
    expected_tag = f"noteamazon{account_number}-22"
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            return False, f"item {index} is not an object: {path}", []
        missing_fields = sorted(REQUIRED_PRODUCT_FIELDS - item.keys())
        if missing_fields:
            return (
                False,
                f"item {index} missing fields {missing_fields}: {path}",
                [],
            )
        asin = str(item.get("asin", "")).strip()
        title = str(item.get("title", "")).strip()
        if not asin or not title:
            return False, f"item {index} has empty ASIN or title: {path}", []
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            return False, f"item {index} has invalid ASIN {asin!r}: {path}", []
        price_value = item.get("price_int")
        if (
            isinstance(price_value, bool)
            or not isinstance(price_value, int)
            or price_value <= 0
            or not str(item.get("price", "")).strip()
        ):
            return False, f"item {index} has invalid price: {path}", []
        if not str(item.get("category", "")).strip():
            return False, f"item {index} has empty category: {path}", []
        image_url = str(item.get("image_url", "")).strip()
        if not image_url.startswith(("https://", "http://")):
            return False, f"item {index} has invalid image URL: {path}", []
        expected_url = f"https://www.amazon.co.jp/dp/{asin}?tag={expected_tag}"
        if item.get("affiliate_url") != expected_url:
            return (
                False,
                f"item {index} affiliate URL mismatch for {asin}: {path}",
                [],
            )
        asins.append(asin)

    if len(asins) != len(set(asins)):
        return False, f"duplicate ASINs: {path}", []
    return True, f"ok: {path}: {len(data)} items", data


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


def validate(
    account: str,
    root: Path = ROOT,
    today: str = TODAY,
) -> tuple[bool, str]:
    if account not in ACCOUNTS:
        raise ValueError(account)

    products_path = root / "data" / account / f"products_{today}.json"
    ok, message, products = valid_product_list(
        products_path,
        account,
        MIN_ITEMS_BY_ACCOUNT.get(account, MIN_ITEMS),
    )
    if not ok:
        return False, message

    if account == "account20":
        shelves = {"Nintendo Switch 2": 0, "PS5ゲームソフト": 0}
        for item in products:
            category = re.sub(r"#\d+$", "", str(item["category"]).strip()).strip()
            if category not in shelves:
                return False, f"account20 has unexpected category: {category}"
            shelves[category] += 1
        if any(count < 2 for count in shelves.values()):
            return False, f"account20 needs at least 2 products per category: {shelves}"

    summary_path = root / "data" / account / f"scrape_summary_{today}.json"
    if not summary_path.exists():
        return False, f"missing: {summary_path}"
    try:
        summary = load_json(summary_path)
    except Exception as exc:
        return False, f"invalid json: {summary_path}: {exc}"
    if not isinstance(summary, dict):
        return False, f"not object: {summary_path}"
    if summary.get("date") != today:
        return False, f"summary date mismatch: {summary_path}: {summary.get('date')} != {today}"
    if summary.get("total_taken") != len(products):
        return (
            False,
            f"summary count mismatch: {summary_path}: "
            f"{summary.get('total_taken')} != {len(products)}",
        )
    if not isinstance(summary.get("categories"), dict):
        return False, f"summary categories is not an object: {summary_path}"

    # The safety net must not accept an older popularity/rotation snapshot as
    # today's discount-first result just because its date and count are valid.
    expected_policy = {
        "selection_mode": "category_quota" if account == "account20" else "global_ranked",
        "sort_order": "sale_first",
        "require_sale_info": True,
        "max_per_category": 5 if account == "account20" else 2,
        "max_total_items": 10,
    }
    policy = summary.get("selection_policy")
    if not isinstance(policy, dict) or any(
        type(policy.get(key)) is not type(expected) or policy.get(key) != expected
        for key, expected in expected_policy.items()
    ):
        return False, f"summary selection policy is not current discount-first mode: {summary_path}"

    ok, message = validate_detail_offer_summary(account, root, products, summary)
    if not ok:
        return False, message

    history_path = root / "data" / account / "asin_history.json"
    if not history_path.exists():
        return False, f"missing: {history_path}"
    try:
        history = load_json(history_path)
    except Exception as exc:
        return False, f"invalid json: {history_path}: {exc}"
    if not isinstance(history, dict):
        return False, f"not object: {history_path}"
    if history.get("schema") != "note-amazon-asin-history-v1":
        return False, f"history schema mismatch: {history_path}"
    posted = history.get("posted")
    if not isinstance(posted, list):
        return False, f"history posted is not a list: {history_path}"
    for index, entry in enumerate(posted, start=1):
        if not isinstance(entry, dict):
            return False, f"history entry {index} is not an object: {history_path}"
        asin = str(entry.get("asin", "")).strip()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            return False, f"history entry {index} has invalid ASIN: {history_path}"

    return (
        True,
        f"ok: {products_path}: {len(products)} items; summary and history matched",
    )


def validate_detail_offer_summary(account: str, root: Path, products: list[dict],
                                  summary: dict) -> tuple[bool, str]:
    """Bind account20's opt-in PDP verification to the saved 13-field output.

    This checks the scraper's record, not the live offer again. A source date
    can be the next JST day, so observation date is not equated to source date.
    """
    if account != "account20":
        return True, "detail offer verification not required"
    config_path = root / "categories20.yaml"
    if not config_path.exists():
        return True, "detail offer verification not configured"
    try:
        import yaml
        with config_path.open("r", encoding="utf-8-sig") as file:
            config = yaml.safe_load(file)
        if not isinstance(config, dict) or not isinstance(config.get("filters", {}), dict):
            raise ValueError("invalid filters")
        enabled = config.get("filters", {}).get("verify_detail_offer", False)
        if type(enabled) is not bool:
            raise ValueError("verify_detail_offer must be boolean")
    except Exception as exc:
        return False, f"detail offer verification config invalid: {config_path}: {exc}"
    if not enabled:
        return True, "detail offer verification not required"

    def failure(reason: str) -> tuple[bool, str]:
        return False, f"detail offer verification invalid: {reason}"

    # The root may be the controller's read-only Git-blob view. Resolve the
    # shared parser from that same root, never from an older local sys.path.
    detail_module = root / "detail_offer.py"
    try:
        namespace = {"__name__": "_pinefield_recorded_detail_offer", "__file__": str(detail_module)}
        code = detail_module.read_text(encoding="utf-8-sig")
        exec(compile(code, str(detail_module), "exec"), namespace)
        validate_offer = namespace["validate_offer"]
        if not callable(validate_offer):
            raise ValueError("validate_offer is not callable")
    except Exception as exc:
        return failure(f"shared PDP validator unavailable: {type(exc).__name__}")

    verification = summary.get("detail_offer_verification")
    if (not isinstance(verification, dict) or type(verification.get("schema_version")) is not int
            or verification.get("schema_version") != 1 or verification.get("enabled") is not True):
        return failure("missing or unsupported summary")
    if verification.get("aborted_reason"):
        return failure("PDP observation batch was aborted")
    observations = verification.get("observations")
    if not isinstance(observations, list):
        return failure("observations is not a list")
    counts = {name: verification.get(name) for name in ("candidate_count", "accepted_count", "rejected_count")}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        return failure("invalid counts")
    if counts["candidate_count"] != len(observations):
        return failure("candidate count mismatch")
    accepted = {}
    seen = set()
    rejected_count = 0
    offer_fields = ("price", "price_int", "original_price", "discount_rate")
    for record in observations:
        if not isinstance(record, dict):
            return failure("observation is not an object")
        asin = record.get("asin")
        if not isinstance(asin, str) or not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen:
            return failure("invalid or duplicate observation ASIN")
        seen.add(asin)
        if record.get("status") == "rejected":
            rejected_count += 1
            continue
        if record.get("status") != "accepted" or record.get("reason") is not None:
            return failure(f"unsupported observation status: {asin}")
        offer = record.get("pdp_offer")
        if (not isinstance(offer, dict) or set(offer) != set(offer_fields)
                or type(offer.get("price_int")) is not int or offer["price_int"] <= 0
                or any(not isinstance(offer.get(key), str) for key in offer_fields if key != "price_int")):
            return failure(f"invalid PDP offer: {asin}")
        evidence = record.get("evidence")
        if not isinstance(evidence, dict):
            return failure(f"missing evidence: {asin}")
        try:
            serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            return failure(f"invalid evidence JSON: {asin}")
        if record.get("evidence_sha256") != hashlib.sha256(serialized).hexdigest().upper():
            return failure(f"evidence SHA mismatch: {asin}")
        selected = evidence.get("selected_asins")
        page_asins = evidence.get("page_asins")
        if (type(evidence.get("schema_version")) is not int or evidence.get("schema_version") != 1
                or evidence.get("requested_asin") != asin
                or not isinstance(selected, list) or not selected or any(value != asin for value in selected)
                or not isinstance(page_asins, list) or any(value != asin for value in page_asins)
                or type(evidence.get("http_status")) is not int or evidence.get("http_status") != 200
                or evidence.get("challenge_detected") is not False
                or evidence.get("evidence_kind") not in ("label", "deal_countdown")
                or not isinstance(record.get("checked_at"), str) or not record["checked_at"]
                or evidence.get("checked_at") != record["checked_at"]):
            return failure(f"evidence identity/status mismatch: {asin}")
        try:
            observed_at = datetime.fromisoformat(record["checked_at"].replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                raise ValueError("timezone missing")
            replayed_evidence = copy.deepcopy(evidence)
            replayed_offer, rejection = validate_offer(replayed_evidence)
        except Exception:
            return failure(f"recorded PDP verification failed: {asin}")
        if rejection or replayed_offer != offer or replayed_evidence != evidence:
            return failure(f"recorded PDP offer mismatch: {asin}")
        accepted[asin] = record
    if (counts["accepted_count"] != len(accepted) or counts["rejected_count"] != rejected_count
            or counts["candidate_count"] != len(accepted) + rejected_count):
        return failure("accepted/rejected count mismatch")
    for product in products:
        record = accepted.get(product["asin"])
        if record is None:
            return failure(f"output ASIN has no accepted PDP record: {product['asin']}")
        if record.get("category") != product.get("category"):
            return failure(f"output category mismatch: {product['asin']}")
        if any(type(record["pdp_offer"][key]) is not type(product.get(key))
               or record["pdp_offer"][key] != product.get(key) for key in offer_fields):
            return failure(f"output offer mismatch: {product['asin']}")
    return True, "detail offer verification matched"


def account_artifact_paths(account: str, root: Path, today: str) -> tuple[Path, ...]:
    if account not in ACCOUNTS:
        raise ValueError(account)
    account_root = root / "data" / account
    return (
        account_root / f"products_{today}.json",
        account_root / f"scrape_summary_{today}.json",
        account_root / "asin_history.json",
    )


def snapshot_artifacts(paths: tuple[Path, ...]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def restore_artifacts(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def cleanup(account: str, root: Path = ROOT, today: str = TODAY) -> None:
    account_root = root / "data" / account
    for path in (
        account_root / f"products_{today}.json",
        account_root / f"scrape_summary_{today}.json",
    ):
        if path.exists():
            path.unlink()
            print(f"removed partial output: {path}", flush=True)


def scrape(account: str, root: Path = ROOT, today: str = TODAY) -> None:
    if account in ACCOUNTS:
        account_number = account.removeprefix("account")
        environment = os.environ.copy()
        environment["PINEFIELD_TARGET_DATE"] = today
        run(
            [sys.executable, f"scrape_main{account_number}.py"],
            root,
            environment,
        )
    else:
        raise ValueError(account)


def ensure(account: str, root: Path = ROOT, today: str = TODAY) -> bool:
    ok, message = validate(account, root, today)
    print(f"{account}: {message}", flush=True)
    if ok:
        return False

    snapshot = snapshot_artifacts(account_artifact_paths(account, root, today))
    try:
        for attempt in range(1, 4):
            restore_artifacts(snapshot)
            print(f"{account}: repair attempt {attempt}/3 for {today}", flush=True)
            cleanup(account, root, today)
            scrape_exit_code = 0
            try:
                scrape(account, root, today)
            except subprocess.CalledProcessError as exc:
                scrape_exit_code = exc.returncode
                print(f"{account}: scrape failed: {exc}", flush=True)
            finally:
                annotate_search_diagnostics(account, today, attempt)
            ok, message = validate(account, root, today)
            print(f"{account}: {message}", flush=True)
            if ok:
                return True
            archive_failure_candidate(account, root, today, attempt, valid=False,
                                      reason=message, scrape_exit_code=scrape_exit_code)
            if attempt < 3:
                time.sleep(30)

        raise RuntimeError(f"{account}: failed to create valid output for {today}")
    except Exception:
        restore_artifacts(snapshot)
        raise


def process_accounts(
    accounts: tuple[str, ...],
    root: Path,
    today: str,
    validate_only: bool,
    report_path: Path | None = None,
) -> dict:
    valid_accounts: list[str] = []
    repaired_accounts: list[str] = []
    failed_accounts: list[dict[str, str]] = []

    def current_report() -> dict:
        return {
            "date": today,
            "checked_accounts": list(accounts),
            "valid_accounts": valid_accounts,
            "repaired_accounts": repaired_accounts,
            "failed_accounts": failed_accounts,
        }

    if report_path:
        write_report(report_path, current_report())

    for account in accounts:
        try:
            if validate_only:
                ok, message = validate(account, root, today)
                print(f"{account}: {message}", flush=True)
                if not ok:
                    raise RuntimeError(message)
            elif ensure(account, root, today):
                repaired_accounts.append(account)
            valid_accounts.append(account)
        except Exception as exc:
            reason = str(exc)
            failed_accounts.append({"account": account, "reason": reason})
            print(f"{account}: FAILED: {reason}", flush=True)
        finally:
            if report_path:
                write_report(report_path, current_report())

    return current_report()


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", choices=ACCOUNTS, action="append")
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--archive-failure", action="store_true",
                        help="Copy current candidates and validation reason to the configured failure artifact directory; never scrape.")
    args = parser.parse_args(argv)
    accounts = tuple(args.account) if args.account else ACCOUNTS
    root = args.root.resolve()

    if args.archive_failure:
        saved = []
        for account in accounts:
            try:
                ok, reason = validate(account, root, args.date)
            except Exception as exc:
                ok, reason = False, f"validation raised {type(exc).__name__}"
            saved.append(archive_failure_candidate(account, root, args.date, "final", valid=ok, reason=reason))
        if len(accounts) == 1:
            annotate_search_diagnostics(accounts[0], args.date, "final")
        return 0 if all(saved) else 1

    print(f"ensure daily scrape date: {args.date}", flush=True)
    print(f"ensure daily scrape root: {root}", flush=True)
    report = process_accounts(accounts, root, args.date, args.validate_only, args.report)

    repaired = report["repaired_accounts"]
    failed = report["failed_accounts"]
    print("repaired accounts:", ",".join(repaired) if repaired else "none", flush=True)
    print(
        "failed accounts:",
        ",".join(item["account"] for item in failed) if failed else "none",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
