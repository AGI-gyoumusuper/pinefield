"""Bounded, anonymous country-only diagnosis. Never produces products or updates ledgers."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import re
import sys
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from playwright.async_api import async_playwright
from scraper import (USER_AGENT, SEARCH_DIAGNOSTICS_ENV, load_config,
                     public_diagnostic_url, save_search_failure_diagnostic,
                     search_url_with_min_price)

# Only explicit country names in the public delivery header are recognized.
# An unknown header (including a domestic postal label) is never guessed or saved.
COUNTRIES = {
    "JP": ("日本", (r"日本", r"\bJapan\b")),
    "US": ("アメリカ合衆国", (r"アメリカ(?:合衆国)?", r"米国", r"\bUnited States\b", r"\bUSA\b")),
    "GB": ("イギリス", (r"イギリス", r"英国", r"\bUnited Kingdom\b")),
    "CA": ("カナダ", (r"カナダ", r"\bCanada\b")),
    "DE": ("ドイツ", (r"ドイツ", r"\bGermany\b")),
    "FR": ("フランス", (r"フランス", r"\bFrance\b")),
    "AU": ("オーストラリア", (r"オーストラリア", r"\bAustralia\b")),
    "SG": ("シンガポール", (r"シンガポール", r"\bSingapore\b")),
    "IN": ("インド", (r"インド(?!ネシア)", r"\bIndia\b")),
}
HEADER = "#glow-ingress-line1, #glow-ingress-line2"
MODAL = "#a-popover-content-GLUXAddressBlock, #GLUXContainer, [role='dialog']"
POSTAL = ("input[id*='Zip'], input[id*='Postal'], input[name*='zip'], "
          "input[name*='postal'], input[autocomplete='postal-code']")
JAPAN_LABEL = re.compile(r"^\s*(?:日本|Japan)\s*$", re.I)


def country_from_labels(labels):
    text = " ".join(labels)
    matches = [code for code, (_, patterns) in COUNTRIES.items()
               if any(re.search(pattern, text, re.I) for pattern in patterns)]
    code = matches[0] if len(matches) == 1 else None
    return {"country_code": code, "country_name": COUNTRIES[code][0] if code else None,
            "country_explicitly_shown": code is not None}


def first_search(config):
    category = config["categories"][0]
    if not category.get("is_search"):
        raise ValueError("first_category_is_not_search")
    minimum = int(category.get("min_price", config.get("filters", {}).get("min_price", 3000)))
    raw = str(category["url"])
    effective = search_url_with_min_price(raw, minimum)
    parsed = urlparse(effective)
    if parsed.scheme != "https" or parsed.hostname != "www.amazon.co.jp" or parsed.path != "/s":
        raise ValueError("first_category_is_not_public_amazon_search")
    # Keep the effective string itself for navigation; do not reserialize its query.
    return {"category": str(category["name"]), "configured_url": public_diagnostic_url(raw),
            "effective_url": effective}


async def visible(locator):
    return [item for item in await locator.all() if await item.is_visible()]


async def country_header(page):
    return country_from_labels(await page.locator(HEADER).all_text_contents())


async def restriction(page):
    parsed = urlparse(page.url)
    if parsed.hostname not in {"amazon.co.jp", "www.amazon.co.jp"} or parsed.path.startswith("/ap/"):
        return "non_public_destination"
    if await page.locator("#captchacharacters, form[action*='validateCaptcha']").count():
        return "captcha"
    text = await page.locator("body").inner_text(timeout=10000)
    if re.search(r"文字を入力してください|ロボットではない|Enter the characters|Sorry, we just need to make sure", text, re.I):
        return "captcha"
    if re.search(r"アクセスが拒否|Access Denied|automated access to Amazon", text, re.I):
        return "access_restricted"
    return None


async def inspect_search(page, target, status):
    rh = parse_qs(urlparse(target).query).get("rh", [""])[0]
    match = re.search(r"(?:^|,)p_n_deal_type:(\d+)(?:,|$)", rh)
    facet = (await page.locator(f'[id="p_n_deal_type/{match.group(1)}"] a[aria-current="true"]').count()
             if match else 0)
    return {"http_status": status, "page_url": public_diagnostic_url(page.url),
            "cards": await page.locator('[data-component-type="s-search-result"][data-asin]').count(),
            "requested_facet_active": bool(facet), "restriction": await restriction(page),
            **await country_header(page)}


async def open_search(page, target):
    response = await page.goto(target, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(random.randint(2500, 4500))
    for _ in range(4):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(600)
    return response.status if response else None


async def choose_japan_from_public_ui(page):
    """Use visible public controls only; never fill/type an input or call a location API."""
    ui = {"opened": False, "japan_option_present": False, "japan_selected": False,
          "confirmation_clicked": False, "postal_input_visible": False}
    openers = await visible(page.locator("#nav-global-location-popover-link"))
    if len(openers) != 1:
        return {**ui, "stop_reason": "delivery_opener_unavailable"}
    await openers[0].click(timeout=10000)
    ui["opened"] = True
    await page.wait_for_timeout(1800)
    blocked = await restriction(page)
    if blocked:
        return {**ui, "stop_reason": blocked}

    # Real country controls must be present in the live DOM. No hidden select is forced.
    native = page.locator("select#GLUXCountryList, select#GLUXCountryListDropdown")
    selects = await native.all()
    ui["native_country_control_present"] = bool(selects)
    # A separate postal field may coexist with a country-only choice. Record its
    # presence without reading its value; only country selection is attempted.
    ui["postal_input_visible"] = bool(await visible(page.locator(POSTAL)))
    jp_selects = [element for element in selects if await element.locator('option[value="JP"]').count()]
    ui["japan_option_present"] = bool(jp_selects)
    visible_selects = [element for element in jp_selects if await element.is_visible()]
    if len(visible_selects) == 1:
        await visible_selects[0].select_option("JP", timeout=10000)
    else:
        dropdowns = await visible(page.locator("#GLUXCountryListDropdown"))
        if len(dropdowns) != 1:
            return {**ui, "stop_reason": "country_only_control_unavailable"}
        await dropdowns[0].click(timeout=10000)
        await page.wait_for_timeout(500)
        options = await visible(page.locator('.a-dropdown-link, [role="option"]').filter(has_text=JAPAN_LABEL))
        ui["japan_option_present"] = len(options) == 1
        if len(options) != 1:
            return {**ui, "stop_reason": "japan_option_unavailable"}
        await options[0].click(timeout=10000)
    ui["japan_selected"] = True
    await page.wait_for_timeout(1800)
    blocked = await restriction(page)
    if blocked:
        return {**ui, "stop_reason": blocked}
    # Conservatively stop even for a possibly optional visible postal field.
    ui["postal_input_visible"] = bool(await visible(page.locator(POSTAL)))
    if ui["postal_input_visible"]:
        return {**ui, "stop_reason": "postal_input_present_after_japan_selection"}
    dialogs = await visible(page.locator(MODAL))
    if dialogs:
        done = await visible(page.locator('#GLUXConfirmClose').filter(has_text=re.compile(r"^\s*(?:完了|Done)\s*$", re.I)))
        if len(done) != 1:
            return {**ui, "stop_reason": "country_confirmation_unavailable"}
        await done[0].click(timeout=10000)
        ui["confirmation_clicked"] = True
        await page.wait_for_timeout(1500)
    if await visible(page.locator(MODAL)):
        return {**ui, "stop_reason": "delivery_dialog_still_open"}
    return {**ui, "stop_reason": None}


async def diagnose_on_page(page, target):
    """Exactly one category before and at most one revisit of the identical URL."""
    status = await open_search(page, target["effective_url"])
    before = await inspect_search(page, target["effective_url"], status)
    await save_search_failure_diagnostic(page, requested_url=target["effective_url"],
        category=target["category"], page_no=1, attempt=1, reason="delivery_country_before", response_status=status)
    result = {"before": before, "ui": None, "after": None}
    if before["restriction"]:
        return {**result, "stop_reason": before["restriction"]}
    if status != 200:
        return {**result, "stop_reason": "search_http_not_200"}
    if before["country_code"] is None:
        return {**result, "stop_reason": "delivery_country_not_explicit"}
    if before["country_code"] == "JP":
        return {**result, "stop_reason": "delivery_country_already_japan"}
    ui = await choose_japan_from_public_ui(page)
    result["ui"] = ui
    if ui["stop_reason"]:
        return {**result, "stop_reason": ui["stop_reason"]}
    status = await open_search(page, target["effective_url"])
    result["after"] = await inspect_search(page, target["effective_url"], status)
    await save_search_failure_diagnostic(page, requested_url=target["effective_url"],
        category=target["category"], page_no=1, attempt=2, reason="delivery_country_after", response_status=status)
    return {**result, "stop_reason": result["after"]["restriction"] or "comparison_complete",
            "japan_country_confirmed_after": result["after"]["country_code"] == "JP"}


async def run(output):
    output = Path(output).resolve()
    if output == REPO_ROOT or REPO_ROOT in output.parents:
        raise ValueError("diagnostic_output_must_be_outside_repository")
    output.mkdir(parents=True, exist_ok=False)
    # Fresh destination plus existing helper enforces at most two public search captures.
    os.environ[SEARCH_DIAGNOSTICS_ENV] = str(output / "public-search")
    target = first_search(load_config(str(REPO_ROOT / "categories13.yaml")))
    result = {"account": "account13", "diagnostic_only": True,
              "started_at_utc": datetime.now(timezone.utc).isoformat(), **target,
              "anonymous_context": True, "postal_values_saved": False,
              "product_writes": 0, "ledger_writes": 0}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--remote-debugging-port=0"])
            try:
                context = await browser.new_context(user_agent=USER_AGENT, locale="ja-JP",
                    viewport={"width": 1280, "height": 900},
                    extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9"})
                await context.add_cookies([
                    {"name": "i18n-prefs", "value": "JPY", "domain": ".amazon.co.jp", "path": "/"},
                    {"name": "lc-main", "value": "ja_JP", "domain": ".amazon.co.jp", "path": "/"},
                ])
                page = await context.new_page()
                await page.goto("https://www.amazon.co.jp/", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(random.randint(4000, 6000))
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(random.randint(1500, 2500))
                blocked = await restriction(page)
                if blocked:
                    result["stop_reason"] = "warmup_" + blocked
                else:
                    result.update(await diagnose_on_page(page, target))
            finally:
                await browser.close()
    except Exception as exc:
        # Exception strings can contain URLs or input state: retain the type only.
        result.update(stop_reason="diagnostic_error", error_type=type(exc).__name__)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"account": "account13", "diagnostic_only": True,
                      "stop_reason": result.get("stop_reason")}, ensure_ascii=False))
    return 1 if result.get("stop_reason") == "diagnostic_error" else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.output_dir)))
