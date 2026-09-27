"""Unified discounts: offline evidence, replay, bounded acquisition and legacy compatibility."""
import asyncio
import copy
from dataclasses import asdict
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from detail_offer import (DISCOUNT_POLICY, coupon_hint, discount_comparison_rate, discount_row,
                          observation_sha256, validate_discount_contract_summary, validate_offer)
from scraper import coupon_search_url, fetch_products, sort_products, verify_discount_candidates
from product_identity import ProductIdentityRegistry
from test_detail_offer import ASIN, KEYS, evidence, product


def discount_evidence(text=None, *, price=8000, original=10000, rate=20):
    value = evidence(price=price, original=original, rate=rate, label="")
    if rate is None:
        value["discount_texts"] = []
    value["coupon_regions"] = [] if text is None else [dict(selector="#coupon_feature_div", text=text,
                                                            asins=[ASIN], links=[], seller_ids=[])]
    return value


def record_for(candidate, value):
    offer, reason = validate_offer(value, offer_scope="unified_discounts")
    if offer:
        for key, val in offer.items():
            setattr(candidate, key, val)
    return dict(asin=candidate.asin, category=candidate.category, checked_at=value["checked_at"],
                status="accepted" if offer else "rejected", reason=reason, pdp_offer=offer,
                evidence=value, evidence_sha256=observation_sha256(value))


class UnifiedDiscountTests(unittest.TestCase):
    def test_observed_amazon_coupon_only_fixture_replays(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/discount_coupon_2026-09-27.json').read_text(encoding='utf-8'))
        evidence=copy.deepcopy(fixture['observation']['evidence'])
        offer,reason=validate_offer(evidence,offer_scope='unified_discounts')
        self.assertIsNone(reason);self.assertEqual('',offer['discount_rate'])
        self.assertEqual((19980,4000,15980),(offer['price_int'],evidence['coupon']['value'],evidence['coupon']['final_price_yen']))
        self.assertEqual(fixture['observation']['evidence'],evidence)

    def test_ordinary_discount_without_badge_or_explicit_rate(self):
        value = discount_evidence(); value["discount_texts"] = []
        offer, reason = validate_offer(value, offer_scope="unified_discounts")
        self.assertIsNone(reason); self.assertEqual("20%OFF", offer["discount_rate"])
        self.assertEqual("not_displayed", value["coupon"]["status"])
        self.assertEqual("ordinary_discount", value["evidence_kind"])

    def test_percent_stacks_sequentially_with_base_discount(self):
        value = discount_evidence("10%OFF クーポンを適用")
        self.assertIsNone(validate_offer(value, offer_scope="unified_discounts")[1])
        self.assertEqual(7200, value["coupon"]["final_price_yen"])
        candidate = product(price=8000, rate="20%OFF"); candidate.original_price = "￥10,000"
        self.assertEqual(28, discount_comparison_rate(candidate, value["coupon"]))

    def test_amount_coupon_without_reference_keeps_raw_ordinary_rate_empty(self):
        value = discount_evidence("1,000円OFF クーポンを適用", original=None, rate=None)
        offer, reason = validate_offer(value, offer_scope="unified_discounts")
        self.assertIsNone(reason); self.assertEqual("", offer["discount_rate"])
        self.assertEqual(7000, value["coupon"]["final_price_yen"])
        self.assertEqual("coupon", value["evidence_kind"])

    def test_fractional_yen_coupon_keeps_verified_benefit_without_inventing_price(self):
        value = discount_evidence("10%OFF クーポンを適用", price=7999, original=None, rate=None)
        self.assertIsNone(validate_offer(value, offer_scope="unified_discounts")[1])
        self.assertEqual("verified", value["coupon"]["status"])
        self.assertIsNone(value["coupon"]["final_price_yen"])

    def test_automatic_clip_and_amazon_displayed_code(self):
        for text, application in [("10%OFF クーポン自動適用", "automatic"), ("10%OFF クーポンを取得", "clip"),
                                  ("10%OFF クーポン コード ABCD2026 を入力", "code")]:
            with self.subTest(text=text):
                value = discount_evidence(text)
                self.assertIsNone(validate_offer(value, offer_scope="unified_discounts")[1])
                self.assertEqual(application, value["coupon"]["application"])
        self.assertEqual("ABCD2026", value["coupon"]["code"])

    def test_unknown_conditions_do_not_destroy_known_ordinary_discount(self):
        for text in ("10%OFF クーポン", "初回限定 10%OFF クーポンを適用", "定期便 10%OFF クーポンを適用",
                     "2個以上 10%OFF クーポンを適用", "Prime会員 10%OFF クーポンを適用",
                     "10点以上購入で適用 10%OFFクーポン", "20セット購入 10%OFFクーポンを適用",
                     "100商品注文 10%OFFクーポンを適用", "2セットで 10%OFFクーポンを適用",
                     "5000円以上の注文で 10%OFFクーポンを適用", "二十個購入 10%OFFクーポンを適用",
                     "2台購入 10%OFFクーポンを適用", "2点目 10%OFFクーポンを適用",
                     "複数袋 10%OFFクーポンを適用", "同時購入 10%OFFクーポンを適用"):
            value = discount_evidence(text)
            offer, reason = validate_offer(value, offer_scope="unified_discounts")
            self.assertIsNone(reason); self.assertEqual(8000, offer["price_int"])
            self.assertEqual("unverified", value["coupon"]["status"])
            value = discount_evidence(text, original=None, rate=None)
            self.assertIsNone(validate_offer(value, offer_scope="unified_discounts")[0])

    def test_external_code_other_asin_and_ambiguous_seller_cannot_be_applied(self):
        for key, val in [("links", ["https://maker.example/code"]), ("asins", ["B000000099"]),
                         ("links", ["https://www.amazon.co.jp/dp/B000000099"]), ("seller_ids", ["ONE", "TWO"]),
                         ("seller_ids", ["OTHER_SELLER"])]:
            value = discount_evidence("10%OFF クーポンを適用")
            value["coupon_regions"][0][key] = val
            self.assertIsNone(validate_offer(value, offer_scope="unified_discounts")[1])
            self.assertEqual("unverified", value["coupon"]["status"])

    def test_expired_coupon_falls_back_but_missing_expiry_is_not_rejected(self):
        value = discount_evidence("10%OFF クーポンを適用 2026/09/11 23:59まで")
        self.assertIsNone(validate_offer(value, offer_scope="unified_discounts")[1])
        self.assertEqual("coupon_expired", value["coupon"]["reason"])
        value = discount_evidence("10%OFF クーポンを適用")
        validate_offer(value, offer_scope="unified_discounts")
        self.assertEqual("verified", value["coupon"]["status"])
        self.assertEqual("not_displayed", value["coupon"]["expiry_status"])
        value = discount_evidence("10%OFF クーポンを適用 2026年9月末まで有効", original=None, rate=None)
        self.assertIsNone(validate_offer(value, offer_scope="unified_discounts")[0])
        self.assertEqual("unverified", value["coupon"]["status"])
        self.assertEqual("unverified", value["coupon"]["expiry_status"])
        value = discount_evidence("10%OFF クーポンを適用 2026年9月末まで有効")
        self.assertIsNone(validate_offer(value, offer_scope="unified_discounts")[1])

    def test_card_hint_is_separate_and_output_remains_thirteen_keys(self):
        self.assertEqual({"kind": "percent", "value": 10}, coupon_hint("10%OFF クーポンを適用"))
        self.assertEqual({"kind": "amount", "value": 4000}, coupon_hint("￥15,980で購入可能 ￥4,000オフクーポンを適用しますか？"))
        self.assertIsNone(coupon_hint("10%ポイント"))
        candidate = product(); candidate._discount_comparison_rate = 50
        self.assertEqual(KEYS, set(asdict(candidate)))
        regular = product("B000000099", rate="40%OFF")
        self.assertEqual(candidate, sort_products([regular, candidate], "sale_first")[0])

    def test_summary_rows_are_bound_to_raw_product_and_replayed_public_evidence(self):
        candidate = product(); record = record_for(candidate, discount_evidence("10%OFF クーポンを適用"))
        summary = dict(date="2026-09-28", selection_policy=dict(offer_scope="unified_discounts", discount_contract=DISCOUNT_POLICY),
                       discount_contract=dict(schema_version=1, policy=DISCOUNT_POLICY, account=1, date="2026-09-28",
                                              products=[discount_row(candidate, record, 1)]),
                       detail_offer_verification=dict(schema_version=1, enabled=True, offer_scope="unified_discounts",
                                                      candidate_count=1, accepted_count=1, rejected_count=0, observations=[record]))
        self.assertTrue(validate_discount_contract_summary("account1", [asdict(candidate)], summary)[0])
        for field, val in [("source_position", 2), ("asin", "B000000099"), ("observation_status", "unverified")]:
            wrong = copy.deepcopy(summary); wrong["discount_contract"]["products"][0][field] = val
            self.assertFalse(validate_discount_contract_summary("account1", [asdict(candidate)], wrong)[0])
        wrong = copy.deepcopy(summary); wrong["detail_offer_verification"]["observations"][0]["evidence"]["coupon"]["value"] = 30
        self.assertFalse(validate_discount_contract_summary("account1", [asdict(candidate)], wrong)[0])

    def test_all_twenty_configs_preserve_account_selection_contract(self):
        root = Path(__file__).resolve().parents[1]
        for account in range(1, 21):
            cfg = yaml.safe_load((root/f"categories{account}.yaml").read_text(encoding="utf-8"))
            flt = cfg["filters"]
            self.assertEqual(DISCOUNT_POLICY, flt["discount_contract"])
            self.assertEqual("unified_discounts", flt["offer_scope"])
            self.assertEqual("sale_first", flt["sort_order"])
            self.assertEqual(10, flt["max_total_items"])
            self.assertEqual(5 if account == 20 else 2, flt["max_per_category"])
            for cat in cfg["categories"]:
                self.assertIn("10343616051", cat["url"])
                self.assertNotIn("p_n_deal_type", coupon_search_url(cat["url"]))


class BoundedAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_integrated_pipeline_binds_final_order_and_preserves_thirteen_keys(self):
        pool = [product(f"B{n:09}", category="A" if n % 2 else "B", price=8000, rate="20%OFF") for n in range(1,27)]
        cats = [dict(name=n, url=f"https://www.amazon.co.jp/s?rh=n%3A{i}%2Cp_n_deal_type%3A10343616051", is_search=True, max_items=20) for i,n in enumerate(("A","B"),1)]
        config = dict(categories=cats,filters=dict(verify_detail_offer=True,offer_scope="unified_discounts",discount_contract=DISCOUNT_POLICY,
                                                 sort_order="sale_first",selection_mode="global_ranked",min_price=3000,max_total_items=10,max_per_category=2),
                      exclusion=dict(exclude_product_identifiers=True))
        page = SimpleNamespace(goto=AsyncMock(),wait_for_timeout=AsyncMock(),evaluate=AsyncMock())
        context=SimpleNamespace(add_cookies=AsyncMock(),new_page=AsyncMock(return_value=page))
        browser=SimpleNamespace(new_context=AsyncMock(return_value=context),close=AsyncMock())
        manager=MagicMock();manager.__aenter__.return_value=SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        async def observer(_page,candidate,_reader,**kwargs):
            value=discount_evidence("10%OFF クーポンを適用" if candidate.asin.endswith("004") else None)
            value.update(requested_asin=candidate.asin, selected_asins=[candidate.asin],url=f"https://www.amazon.co.jp/dp/{candidate.asin}")
            if value["coupon_regions"]:
                value["coupon_regions"][0]["asins"]=[candidate.asin]
            candidate.specs=f"説明 {candidate.asin}"
            return record_for(candidate,value)
        with patch("scraper.load_config",return_value=config), patch("scraper.load_product_exclusion_registry",return_value=ProductIdentityRegistry()), \
             patch("scraper.async_playwright",return_value=manager),patch("scraper.asyncio.sleep",new=AsyncMock()), \
             patch("scraper.scrape_search",new=AsyncMock(side_effect=[pool[::2],pool[1::2],[],[]])), \
             patch("scraper.observe_detail_offer",new=AsyncMock(side_effect=observer)) as reader:
            selected,stats=await fetch_products("categories1.yaml","noteamazon1-22","2026-09-28")
        self.assertEqual(10,len(selected));self.assertEqual(24,reader.await_count)
        self.assertEqual("B000000004",selected[0].asin)
        self.assertTrue(all(set(asdict(item))==KEYS for item in selected))
        rows=stats["_discount_contract"]["products"]
        self.assertEqual(list(range(1,11)),[row["source_position"] for row in rows])
        self.assertEqual([item.asin for item in selected],[row["asin"] for row in rows])
        self.assertEqual(2,stats["_coupon_search"]["pages_attempted"])

    async def test_maximum_detail_visits_is_twenty_four(self):
        pool = [product(f"B{n:09}") for n in range(30)]
        async def observe(_page, candidate, _reader, **kwargs):
            self.assertEqual("unified_discounts", kwargs["offer_scope"])
            return dict(asin=candidate.asin, status="accepted", reason=None, evidence={"coupon": {}})
        stats = {}
        with patch("scraper.observe_detail_offer", new=AsyncMock(side_effect=observe)) as reader, patch("scraper.asyncio.sleep", new=AsyncMock()):
            result = await verify_discount_candidates(None, pool, stats)
        self.assertEqual(24, len(result)); self.assertEqual(24, reader.await_count)
        self.assertEqual(24, stats["_detail_offer_verification"]["limits"]["max_candidates"])

    async def test_timeout_does_not_start_another_visit(self):
        stats = {}
        with patch("scraper.observe_detail_offer", new=AsyncMock(side_effect=asyncio.TimeoutError)) as reader:
            result = await verify_discount_candidates(None, [product(), product("B000000099")], stats)
        self.assertEqual([], result); self.assertEqual(1, reader.await_count)
        self.assertEqual("detail_budget_exhausted", stats["_detail_offer_verification"]["budget_stop_reason"])

    async def test_challenge_stops_navigation_but_preserves_verified_normal_items(self):
        stats = {}
        records = [dict(status="accepted", reason=None, evidence={"coupon": {}}), dict(status="rejected", reason="detail_challenge")]
        with patch("scraper.observe_detail_offer", new=AsyncMock(side_effect=records)) as reader, patch("scraper.asyncio.sleep", new=AsyncMock()):
            result = await verify_discount_candidates(None, [product(), product("B000000099"), product("B000000088")], stats)
        self.assertEqual(1, len(result)); self.assertEqual(2, reader.await_count)


if __name__ == "__main__":
    unittest.main()
