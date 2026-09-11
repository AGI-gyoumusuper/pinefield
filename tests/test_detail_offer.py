import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from detail_offer import (OBSERVE_OFFER_JS, observation_sha256, observe_detail_offer,
                          public_evidence, validate_offer, without_search_price_filter)
from product_identity import ProductIdentityRegistry
from scraper import (Product, fetch_and_save, fetch_products, filter_and_sort,
                     make_affiliate_url, select_enrich_unique_products, verify_candidate_offers)


ASIN = "B0GKR53JKK"
CHECKED = "2026-09-12T02:00:00+00:00"
KEYS = {"asin", "title", "price", "price_int", "original_price", "discount_rate", "image_url",
        "affiliate_url", "category", "rating", "review_count", "description", "specs"}


def product(asin=ASIN, category="Nintendo Switch 2", price=12070, rate="7%OFF"):
    return Product(asin, asin, f"￥{price:,}", price, "￥12,959", rate, "https://images.example/product.jpg",
                   make_affiliate_url(asin, "noteamazon20-22"), category + "#1", "4.5", "72")


def observed(asin=ASIN, price=11859, rate=8, original=12959, label="タイムセール"):
    return {"selected_asins": [asin], "page_asins": [], "center_count": 1, "product_title": asin,
            "price_region_count": 1, "price_region_selector": "#corePriceDisplay_desktop_feature_div",
            "price_texts": [f"{price:,}"], "currency_texts": ["￥"], "discount_texts": [f"-{rate}%"],
            "reference_price_texts": [f"過去価格: ￥{original:,}"] if original else [],
            "sale_region_count": 1, "sale_region_selector": "#dealBadge_feature_div", "sale_label": label,
            "timer_count": 0, "timer": None, "challenge_detected": False}


def evidence(**kw):
    return public_evidence(observed(**kw), kw.get("asin", ASIN), f"https://www.amazon.co.jp/dp/{kw.get('asin', ASIN)}?th=1", 200, CHECKED)


class OfferValidationTests(unittest.TestCase):
    def test_card_pdp_mismatch_uses_observed_values(self):
        result, reason = validate_offer(evidence())
        self.assertIsNone(reason)
        self.assertEqual({"price": "￥11,859", "price_int": 11859, "original_price": "￥12,959", "discount_rate": "8%OFF"}, result)

    def test_ordinary_discount_without_time_sale_is_rejected(self):
        result, reason = validate_offer(evidence(price=9954, rate=23, label=""))
        self.assertIsNone(result)
        self.assertEqual("detail_time_sale_unverified", reason)

    def test_other_selected_variant_or_parent_marker_rejected(self):
        for field in ("selected_asins", "page_asins"):
            value = evidence()
            value[field] = ["B000000099"]
            self.assertEqual("detail_selected_asin_mismatch", validate_offer(value)[1])

    def test_redirect_or_challenge_or_unavailable_price_rejected(self):
        cases = [("url", "https://www.amazon.co.jp/dp/B000000099", "detail_url_asin_mismatch"),
                 ("challenge_detected", True, "detail_challenge"),
                 ("http_status", 503, "detail_http_error"),
                 ("price_texts", [], "detail_primary_price_missing_or_ambiguous"),
                 ("price_texts", ["11,859", "12,070"], "detail_primary_price_missing_or_ambiguous"),
                 ("currency_texts", ["$"], "detail_primary_price_missing_or_ambiguous")]
        for key, replacement, expected in cases:
            with self.subTest(key=key, replacement=replacement):
                value = evidence(); value[key] = replacement
                self.assertEqual(expected, validate_offer(value)[1])

    def test_reference_absent_allowed_conflicting_or_inconsistent_rejected(self):
        result, reason = validate_offer(evidence(original=None))
        self.assertIsNone(reason); self.assertEqual("", result["original_price"])
        value = evidence(); value["reference_price_texts"] += ["￥13,959"]
        self.assertEqual("detail_reference_ambiguous_or_invalid", validate_offer(value)[1])
        value = evidence(); value["discount_texts"] = ["-9%"]
        self.assertEqual("detail_discount_reference_inconsistent", validate_offer(value)[1])

    def test_valid_countdown_requires_same_visible_sale_region_and_positive_duration(self):
        value = evidence(label="終了まで: 02:03:04")
        value.update(timer_count=1, timer={"timer_selector": "#detailpage-dealBadge-countdown-timer", "timer_text": "02:03:04"})
        result, reason = validate_offer(value)
        self.assertIsNone(reason); self.assertEqual("deal_countdown", value["evidence_kind"])
        self.assertEqual(7384, value["countdown"]["remaining_seconds"])
        self.assertEqual("2026-09-12T04:03:04+00:00", value["countdown"]["expires_at"])
        for field, replacement in (("timer_count", 0), ("sale_label", "配送まで:02:03:04"), ("sale_region_count", 2)):
            invalid = copy.deepcopy(value); invalid[field] = replacement
            self.assertIsNone(validate_offer(invalid)[0])
        value.update(sale_label="終了まで:00:00:00", timer={"timer_selector": ".detailpage-dealBadge-countdown-timer", "timer_text": "00:00:00"})
        self.assertEqual("detail_countdown_expired", validate_offer(value)[1])

    def test_whitelist_omits_auth_and_url_query(self):
        raw = observed(); raw.update(cookie="secret", token="secret", account="private")
        value = public_evidence(raw, ASIN, f"https://www.amazon.co.jp/dp/{ASIN}?token=secret", 200, CHECKED)
        self.assertNotIn("secret", json.dumps(value)); self.assertNotIn("?", value["url"])
        self.assertEqual(64, len(observation_sha256(value)))

    def test_search_defers_only_numerical_price_constraint(self):
        url = "https://www.amazon.co.jp/s?rh=n%3A1%2Cp_36%3A300000-%2Cp_n_deal_type%3A10343614051&low-price=3000&qid=123"
        result = without_search_price_filter(url)
        self.assertNotIn("p_36", result); self.assertNotIn("low-price", result)
        self.assertIn("p_n_deal_type", result); self.assertIn("qid=123", result)


class OfferPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_challenge_stops_batch_without_next_product_navigation(self):
        pool = [product(f"B00000000{index}") for index in range(1, 7)]
        records = [{"asin": candidate.asin, "category": candidate.category, "status": "accepted", "reason": None} for candidate in pool[:4]]
        records.append({"asin": pool[4].asin, "category": pool[4].category, "status": "rejected", "reason": "detail_challenge"})
        stats = {}
        with patch("scraper.observe_detail_offer", new=AsyncMock(side_effect=records)) as observer, \
             patch("scraper.asyncio.sleep", new=AsyncMock()):
            selected = await verify_candidate_offers(None, pool, stats)
        self.assertEqual([], selected); self.assertEqual(5, observer.await_count)
        self.assertEqual(4, stats["_detail_offer_verification"]["accepted_count"])
        self.assertEqual("detail_challenge", stats["_detail_offer_verification"]["aborted_reason"])
        self.assertEqual("B000000006", stats["_detail_offer_verification"]["unobserved_candidates"][0]["asin"])

    async def test_accepted_offer_updates_four_keys_and_enrichment_only(self):
        candidate = product(); before = asdict(candidate)
        page = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)), wait_for_timeout=AsyncMock(),
                               url=f"https://www.amazon.co.jp/dp/{ASIN}", evaluate=AsyncMock(side_effect=[observed(), [ASIN]]))
        reader = AsyncMock(return_value=("same product description", "same product specs"))
        record = await observe_detail_offer(page, candidate, reader)
        self.assertEqual("accepted", record["status"]); self.assertEqual(11859, candidate.price_int)
        self.assertEqual(KEYS, set(asdict(candidate)))
        for key in KEYS - {"price", "price_int", "discount_rate", "original_price", "description", "specs"}:
            self.assertEqual(before[key], asdict(candidate)[key])
        self.assertEqual(12070, record["card_offer"]["price_int"])
        self.assertEqual(record["evidence_sha256"], observation_sha256(record["evidence"]))

    async def test_rejected_offer_preserves_card_and_skips_enrichment(self):
        candidate = product(); before = asdict(candidate)
        page = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)), wait_for_timeout=AsyncMock(),
                               url=f"https://www.amazon.co.jp/dp/{ASIN}", evaluate=AsyncMock(return_value=observed(label="")))
        reader = AsyncMock()
        record = await observe_detail_offer(page, candidate, reader)
        self.assertEqual("rejected", record["status"]); self.assertEqual(before, asdict(candidate)); reader.assert_not_called()

    async def test_all_candidates_verified_before_price_rank_quota_in_both_identity_modes(self):
        for identity_enabled in (False, True):
            # Card leaders become below-minimum or low-discount; sixth candidate must lead after PDP update.
            pool = [product(f"B0000000{n:02}", "A" if n <= 6 else "B", price=10000-n, rate=f"{90-n}%OFF") for n in range(1, 9)]
            cats = [{"name": "A", "url": "https://www.amazon.co.jp/s?rh=n%3A1%2Cp_36%3A300000-", "max_items": 10, "is_search": True},
                    {"name": "B", "url": "https://www.amazon.co.jp/s?rh=n%3A2", "max_items": 10, "is_search": True}]
            config = {"categories": cats, "filters": {"verify_detail_offer": True, "sort_order": "sale_first", "selection_mode": "category_quota", "min_price": 3000, "max_total_items": 4, "max_per_category": 2},
                      "exclusion": {"exclude_product_identifiers": identity_enabled}}
            page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
            context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page))
            browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
            manager = MagicMock(); manager.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
            visited = []
            async def verify(_page, candidate, _reader):
                visited.append(candidate.asin)
                number = int(candidate.asin[-2:]); candidate.price_int = 2000 if number == 1 else 5000
                candidate.price = f"￥{candidate.price_int:,}"; candidate.discount_rate = "50%OFF" if number == 6 else "10%OFF"
                candidate.specs = ""
                return {"asin": candidate.asin, "category": candidate.category, "status": "accepted", "reason": None}
            with patch("scraper.load_config", return_value=config), patch("scraper.load_product_exclusion_registry", return_value=ProductIdentityRegistry()), \
                 patch("scraper.async_playwright", return_value=manager), patch("scraper.asyncio.sleep", new=AsyncMock()), \
                 patch("scraper.scrape_search", new=AsyncMock(side_effect=[pool[:6], pool[6:]])) as search, \
                 patch("scraper.observe_detail_offer", new=AsyncMock(side_effect=verify)), \
                 patch("scraper.enrich_product", new=AsyncMock(side_effect=AssertionError("must not revisit verified PDP"))), \
                 patch("scraper.enrich_products", new=AsyncMock(side_effect=AssertionError("must not revisit verified PDP"))):
                selected, stats = await fetch_products("categories20.yaml", "noteamazon20-22")
            self.assertEqual(8, len(visited)); self.assertEqual(["B000000006", "B000000002", "B000000007", "B000000008"], [p.asin for p in selected])
            self.assertTrue(all(call.kwargs["defer_offer_validation"] for call in search.await_args_list))
            self.assertTrue(all("p_36" not in call.args[1] for call in search.await_args_list))
            self.assertEqual(4, stats["_detail_offer_verification"]["final_selected_count"])
            self.assertEqual(8, stats["_detail_offer_verification"]["candidate_count"])

    async def test_opt_out_never_runs_verifier_or_adds_summary_fields(self):
        candidate = product()
        config = {"categories": [], "filters": {"sort_order": "sale_first"}, "exclusion": {}}
        page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
        context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        manager = MagicMock(); manager.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        with patch("scraper.load_config", return_value=config), patch("scraper.load_product_exclusion_registry", return_value=ProductIdentityRegistry()), \
             patch("scraper.async_playwright", return_value=manager), patch("scraper.verify_candidate_offers", new=AsyncMock(side_effect=AssertionError("opt-out verifier"))):
            selected, stats = await fetch_products("categories1.yaml")
        self.assertEqual([], selected); self.assertNotIn("_detail_offer_verification", stats)
        self.assertNotIn("verify_detail_offer", stats["_selection_policy"])

    async def test_opt_in_other_account_is_rejected_before_browser(self):
        with patch("scraper.load_config", return_value={"filters": {"verify_detail_offer": True}}), \
             patch("scraper.async_playwright", side_effect=AssertionError("no browser")):
            with self.assertRaisesRegex(ValueError, "restricted"):
                await fetch_products("categories19.yaml")


if __name__ == "__main__":
    unittest.main()
