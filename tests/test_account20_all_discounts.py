import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import yaml
from detail_offer import validate_offer, observe_detail_offer
from scraper import fetch_products, filter_and_sort, verify_candidate_offers
from test_detail_offer import ASIN, evidence, observed, product, KEYS
import test_detail_offer_summary as legacy_summary


class AllDiscountOfferTests(unittest.TestCase):
    def test_ordinary_discount_requires_explicit_scope_and_keeps_truthful_evidence(self):
        value = evidence(price=6309, original=7990, rate=21, label="")
        self.assertEqual("detail_time_sale_unverified", validate_offer(copy.deepcopy(value))[1])
        offer, reason = validate_offer(value, offer_scope="all_discounts")
        self.assertIsNone(reason)
        self.assertEqual((6309, "￥7,990", "21%OFF"), (offer["price_int"], offer["original_price"], offer["discount_rate"]))
        self.assertEqual("ordinary_discount", value["evidence_kind"])
        self.assertEqual("", value["sale_label"])
        self.assertIsNone(value["countdown"])

    def test_ordinary_discount_without_badge_region_is_valid_but_reference_is_required(self):
        value = evidence(label=""); value["sale_region_count"] = 0
        self.assertIsNone(validate_offer(value, offer_scope="all_discounts")[1])
        self.assertEqual("detail_reference_missing", validate_offer(evidence(label="", original=None), offer_scope="all_discounts")[1])

    def test_price_rate_reference_identity_and_challenge_remain_fail_closed(self):
        cases = [("price_texts", [], "detail_primary_price_missing_or_ambiguous"),
                 ("discount_texts", [], "detail_discount_missing_or_ambiguous"),
                 ("discount_texts", ["-99%"], "detail_discount_reference_inconsistent"),
                 ("reference_price_texts", ["￥12,959", "￥13,000"], "detail_reference_ambiguous_or_invalid"),
                 ("selected_asins", ["B000000099"], "detail_selected_asin_mismatch"),
                 ("challenge_detected", True, "detail_challenge")]
        for key, replacement, expected in cases:
            with self.subTest(key=key):
                value = evidence(label=""); value[key] = replacement
                self.assertEqual(expected, validate_offer(value, offer_scope="all_discounts")[1])

    def test_actual_time_sale_evidence_is_preserved(self):
        value = evidence()
        self.assertIsNone(validate_offer(value, offer_scope="all_discounts")[1])
        self.assertEqual("label", value["evidence_kind"])

    def test_invalid_scope_is_rejected(self):
        self.assertEqual("detail_offer_scope_invalid", validate_offer(evidence(), offer_scope="anything")[1])

    def test_config_preserves_nodes_minimum_and_history_exclusion(self):
        root = Path(__file__).resolve().parents[1]
        cfg = yaml.safe_load((root / "categories20.yaml").read_text(encoding="utf-8"))
        expected = ["206233864051", "8019286051"]
        for category, node in zip(cfg["categories"], expected):
            facets = parse_qs(urlparse(category["url"]).query)["rh"][0].split(",")
            self.assertEqual([f"n:{node}", "p_36:300000-", "p_n_deal_type:10343616051"], facets)
        self.assertEqual(3000, cfg["filters"]["min_price"])
        self.assertEqual("all_discounts", cfg["filters"]["offer_scope"])
        self.assertEqual(20, cfg["exclusion"]["exclude_within_days"])
        self.assertFalse(cfg["exclusion"]["exclude_scraped_candidates"])
        self.assertTrue(cfg["exclusion"]["exclude_product_identifiers"])
        low = product("B000000001", price=2999)
        posted = product("B000000002", price=5000)
        valid = product("B000000003", price=3000)
        selected = filter_and_sort([low, posted, valid], min_price=3000, posted_asins={posted.asin}, sort_order="sale_first")
        self.assertEqual([valid.asin], [p.asin for p in selected])


class AllDiscountPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_verified_account20_may_enable_scope(self):
        for config_path, verify in [("categories1.yaml", False), ("categories19.yaml", True), ("categories20.yaml", False)]:
            with self.subTest(config_path=config_path, verify=verify), \
                 patch("scraper.load_config", return_value={"filters": {"offer_scope": "all_discounts", "verify_detail_offer": verify}}), \
                 patch("scraper.async_playwright", side_effect=AssertionError("must not open browser")):
                with self.assertRaisesRegex(ValueError, "restricted"):
                    await fetch_products(config_path)

    async def test_observer_reads_details_without_changing_the_13_key_schema(self):
        candidate = product()
        page = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)), wait_for_timeout=AsyncMock(),
                               url=f"https://www.amazon.co.jp/dp/{ASIN}", evaluate=AsyncMock(side_effect=[observed(label=""), [ASIN]]))
        record = await observe_detail_offer(page, candidate, AsyncMock(return_value=("description", "specs")), offer_scope="all_discounts")
        self.assertEqual("accepted", record["status"])
        self.assertEqual("ordinary_discount", record["evidence"]["evidence_kind"])
        self.assertEqual(KEYS, set(candidate.__dict__))
        self.assertEqual("description", candidate.description)

    async def test_scope_is_forwarded_and_recorded(self):
        candidate = product()
        record = {"asin": candidate.asin, "category": candidate.category, "status": "accepted", "reason": None}
        stats = {}
        with patch("scraper.observe_detail_offer", new=AsyncMock(return_value=record)) as observer, patch("scraper.asyncio.sleep", new=AsyncMock()):
            self.assertEqual([candidate], await verify_candidate_offers(None, [candidate], stats, offer_scope="all_discounts"))
        self.assertEqual("all_discounts", observer.await_args.kwargs["offer_scope"])
        self.assertEqual("Amazon セール", stats["_detail_offer_verification"]["sale_name"])


class AllDiscountSummaryTests(unittest.TestCase):
    setUp = legacy_summary.DetailOfferSummaryTests.setUp
    run_validation = legacy_summary.DetailOfferSummaryTests.run_validation
    assert_rejected = legacy_summary.DetailOfferSummaryTests.assert_rejected
    def ordinary_fixture(self):
        self.config.write_text('filters:\n  verify_detail_offer: true\n  offer_scope: all_discounts\n', encoding='utf-8')
        self.summary["selection_policy"]["offer_scope"] = "all_discounts"
        self.summary["sale_name"] = "Amazon セール"
        detail = self.summary["detail_offer_verification"]
        detail.update(offer_scope="all_discounts", sale_name="Amazon セール")
        for row, prod in zip(detail["observations"], self.products):
            original = prod["price_int"] * 5 // 4
            prod["original_price"] = f"￥{original:,}"
            row["pdp_offer"]["original_price"] = prod["original_price"]
            row["evidence"].update(reference_price_texts=[prod["original_price"]], sale_label="", evidence_kind="ordinary_discount")
            row["evidence_sha256"] = legacy_summary.digest(row["evidence"])
        self.product_path.write_text(json.dumps(self.products, ensure_ascii=False), encoding="utf-8")

    def test_ordinary_summary_replays_price_and_evidence(self):
        self.ordinary_fixture()
        self.assertTrue(self.run_validation()[0])
        row = self.summary["detail_offer_verification"]["observations"][0]
        row["evidence"]["reference_price_texts"] = []
        row["evidence_sha256"] = legacy_summary.digest(row["evidence"])
        self.assert_rejected("recorded PDP offer mismatch")

    def test_ordinary_summary_cannot_hide_scope_or_mislabel_sale(self):
        self.ordinary_fixture()
        self.summary["sale_name"] = "Amazon タイムセール"
        self.assert_rejected("ordinary discount scope/label mismatch")
        self.summary["sale_name"] = "Amazon セール"
        del self.summary["selection_policy"]["offer_scope"]
        self.assert_rejected("offer scope mismatch")

    def test_ordinary_summary_requires_config_authorization(self):
        self.ordinary_fixture()
        self.config.write_text('filters:\n  verify_detail_offer: true\n', encoding='utf-8')
        self.assert_rejected("ordinary discount scope/label mismatch")

    def test_existing_time_sale_daily_remains_valid_after_scope_change(self):
        self.config.write_text('filters:\n  verify_detail_offer: true\n  offer_scope: all_discounts\n', encoding='utf-8')
        self.assertTrue(self.run_validation()[0])


if __name__ == "__main__":
    unittest.main()
