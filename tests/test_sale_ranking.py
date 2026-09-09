import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from product_identity import ProductIdentityRegistry, identity_from_key
from scraper import (
    Product, fetch_and_save, fetch_products, filter_and_sort, make_affiliate_url, merge_deferred_search_stats,
    scrape_search, select_enrich_unique_products, sort_products,
)


def product(name, category="A", discount="", price=5000, reviews=1, original="", model=None):
    return Product(
        asin=name, title=name, price=f"￥{price:,}", price_int=price,
        original_price=original, discount_rate=discount, image_url="https://example.test/image.png",
        affiliate_url="", category=f"{category}#1", rating="4.0", review_count=str(reviews),
        specs=f"ブランド名 ACME メーカー型番 {model or name}",
    )


CATEGORIES = [
    {"name": "A", "url": "https://example.test/a"},
    {"name": "B", "url": "https://example.test/b"},
]


def opposing_pool():
    return [
        product("AREVIEW", "A", "5%OFF", reviews=9999),
        product("ASALE", "A", "50%OFF"),
        product("BREVIEW", "B", "5%OFF", reviews=9999),
        product("BSALE", "B", "90%OFF"),
    ]


class SaleRankingTests(unittest.TestCase):
    def test_sale_first_uses_presence_then_percentage_then_price_and_stable_ties(self):
        candidates = [
            product("NO_RATE", price=100000, original="￥200,000", reviews=99999),
            product("ZERO_RATE", discount="0%OFF", price=10),
            product("LOWER_RATE", discount="30%OFF", price=100000),
            product("LOWER_PRICE", discount="50%OFF", price=1000),
            product("TIE_FIRST", discount="50%OFF", price=5000, reviews=1),
            product("TIE_SECOND", discount="50%OFF", price=5000, reviews=9999),
            product("HIGHER_RATE", discount="70%OFF", price=500),
        ]
        original_order = list(candidates)
        selected = sort_products(candidates, "sale_first")
        self.assertEqual([
            "HIGHER_RATE", "TIE_FIRST", "TIE_SECOND", "LOWER_PRICE",
            "LOWER_RATE", "ZERO_RATE", "NO_RATE",
        ], [p.asin for p in selected])
        self.assertEqual(original_order, candidates)

    def test_review_order_does_not_add_discount_price_or_rating_tiebreakers(self):
        candidates = [product("FIRST", reviews=100), product("SECOND", discount="90%OFF", reviews=100)]
        self.assertEqual(["FIRST", "SECOND"], [p.asin for p in sort_products(candidates, "review_desc")])

    def test_other_sort_modes_keep_existing_behavior(self):
        candidates = [product("CHEAP", discount="80%OFF", price=100), product("EXPENSIVE", discount="20%OFF", price=10000)]
        for mode, expected in {
            "amount_first": ["EXPENSIVE", "CHEAP"],
            "price_desc": ["EXPENSIVE", "CHEAP"],
            "price_asc": ["CHEAP", "EXPENSIVE"],
            "discount_desc": ["CHEAP", "EXPENSIVE"],
            "unknown": ["CHEAP", "EXPENSIVE"],
        }.items():
            with self.subTest(mode=mode):
                self.assertEqual(expected, [p.asin for p in sort_products(candidates, mode)])

    def test_shelf_modes_honor_sale_order_without_globally_reordering_shelves(self):
        for mode in ("category_round_robin", "category_quota"):
            for sort, expected in (("sale_first", ["ASALE", "BSALE"]), ("review_desc", ["AREVIEW", "BREVIEW"])):
                with self.subTest(mode=mode, sort=sort):
                    selected = filter_and_sort(opposing_pool(), min_price=0, sort_order=sort,
                        max_total=2, max_per_category=1, selection_mode=mode, categories=CATEGORIES)
                    self.assertEqual(expected, [p.asin for p in selected])

    def test_sale_quota_preserves_five_per_shelf_and_overflow(self):
        for first_count, expected_counts in ((7, (5, 5)), (2, (2, 8))):
            with self.subTest(first_count=first_count):
                candidates = [product(f"A{n}", "A", f"{n}%OFF", reviews=100-n) for n in range(1, first_count+1)]
                candidates += [product(f"B{n}", "B", f"{n+50}%OFF", reviews=100-n) for n in range(1, 10)]
                selected = filter_and_sort(candidates, min_price=0, sort_order="sale_first", max_total=10,
                    max_per_category=5, selection_mode="category_quota", categories=CATEGORIES)
                self.assertEqual(expected_counts, tuple(sum(p.category.startswith(c) for p in selected) for c in ("A", "B")))
                self.assertEqual(f"A{first_count}", selected[0].asin)
                self.assertEqual(10, len({p.asin for p in selected}))

    def test_global_ranked_prioritizes_discount_across_shelves_with_soft_cap(self):
        candidates = [product("AREVIEW", "A", "1%OFF", reviews=99999),
            product("B90", "B", "90%OFF"), product("B80", "B", "80%OFF"),
            product("B70", "B", "70%OFF"), product("A60", "A", "60%OFF"),
            product("C50", "C", "50%OFF")]
        selected = filter_and_sort(candidates, min_price=0, sort_order="sale_first", max_total=4,
            max_per_category=2, selection_mode="global_ranked", categories=CATEGORIES,
            rotation_state_file="must-not-be-read.json")
        self.assertEqual(["B90", "B80", "A60", "C50"], [p.asin for p in selected])

    def test_global_ranked_posted_exclusion_precedes_cap_and_overflow(self):
        candidates = [product(f"A{n}", "A", f"{n}%OFF") for n in range(1, 7)]
        selected = filter_and_sort(candidates, min_price=0, sort_order="sale_first", max_total=4,
            max_per_category=2, selection_mode="global_ranked", posted_asins={"A6"})
        self.assertEqual(["A5", "A4", "A3", "A2"], [p.asin for p in selected])


class SaleIdentityRankingTests(unittest.IsolatedAsyncioTestCase):
    async def select(self, candidates, mode, sort="sale_first", registry=None, max_total=2, quota=1):
        with patch("scraper.asyncio.sleep", new=AsyncMock()):
            return await select_enrich_unique_products(None, candidates, registry or ProductIdentityRegistry(),
                CATEGORIES, max_total, quota, mode, "", {}, sort_order=sort)

    async def test_identity_paths_honor_sort_and_keep_category_output_order(self):
        for mode in ("category_round_robin", "category_quota"):
            for sort, expected in (("sale_first", ["ASALE", "BSALE"]), ("review_desc", ["AREVIEW", "BREVIEW"])):
                with self.subTest(mode=mode, sort=sort):
                    selected = await self.select(opposing_pool(), mode, sort)
                    self.assertEqual(expected, [p.asin for p in selected])

    async def test_sale_leader_identity_exclusion_uses_same_shelf_sale_runner_up(self):
        for mode in ("category_round_robin", "category_quota"):
            with self.subTest(mode=mode):
                registry = ProductIdentityRegistry()
                registry.add_identity(identity_from_key("ACME::DUPLICATE"))
                candidates = [product("AREVIEW", "A", "5%OFF", reviews=9999),
                    product("ADUPLICATE", "A", "70%OFF", model="DUPLICATE"),
                    product("ANEXT", "A", "50%OFF"), product("BSALE", "B", "90%OFF")]
                selected = await self.select(candidates, mode, registry=registry)
                self.assertEqual(["ANEXT", "BSALE"], [p.asin for p in selected])

    async def test_global_identity_skip_does_not_consume_category_cap(self):
        registry = ProductIdentityRegistry()
        registry.add_identity(identity_from_key("ACME::DUPLICATE"))
        candidates = [product("AREVIEW", "A", "1%OFF", reviews=99999),
            product("ADUPLICATE", "A", "95%OFF", model="DUPLICATE"),
            product("A90", "A", "90%OFF"), product("A80", "A", "80%OFF"),
            product("A70", "A", "70%OFF"), product("B60", "B", "60%OFF"),
            product("C50", "C", "50%OFF")]
        selected = await self.select(candidates, "global_ranked", registry=registry, max_total=4, quota=2)
        self.assertEqual(["A90", "A80", "B60", "C50"], [p.asin for p in selected])

    async def test_global_identity_overflow_skips_duplicates_and_keeps_searching(self):
        candidates = [product("A90", "A", "90%OFF", model="SAME"),
            product("A80", "A", "80%OFF"), product("ADUPLICATE", "A", "70%OFF", model="SAME"),
            product("A60", "A", "60%OFF"), product("A50", "A", "50%OFF"),
            product("A40", "A", "40%OFF")]
        selected = await self.select(candidates, "global_ranked", max_total=5, quota=2)
        self.assertEqual(["A90", "A80", "A60", "A50", "A40"], [p.asin for p in selected])

    async def test_global_identity_and_non_identity_modes_agree_without_duplicates(self):
        candidates = [product(f"{c}{n}", c, f"{n}%OFF", reviews=100-n)
            for c in ("A", "B", "C") for n in range(1, 7)]
        for limit in (4, 10):
            with self.subTest(limit=limit):
                expected = filter_and_sort(candidates, min_price=0, sort_order="sale_first", max_total=limit,
                    max_per_category=2, selection_mode="global_ranked")
                actual = await self.select(candidates, "global_ranked", max_total=limit, quota=2)
                self.assertEqual([p.asin for p in expected], [p.asin for p in actual])

    async def test_global_fetch_keeps_full_pool_until_identity_and_history_exclusion_finish(self):
        registry = ProductIdentityRegistry()
        for index in range(1, 10):
            registry.add_identity(identity_from_key(f"ACME::MODELA{index:02d}"))
        registry.asins.add("B10")
        candidates = {category: [product(f"{category}{index}", category, f"{base-index}%OFF", model=f"MODEL{category}{index:02d}")
            for index in range(1, 11)] for category, base in (("A", 100), ("B", 80))}
        config = {"categories": [dict(category, max_items=10, is_search=True) for category in CATEGORIES],
            "filters": {"sort_order": "sale_first", "selection_mode": "global_ranked", "min_price": 0,
                "max_total_items": 10, "max_per_category": 2},
            "exclusion": {"exclude_product_identifiers": True}}
        page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
        context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        manager = MagicMock()
        manager.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        async def search(*args, **kwargs):
            return candidates[args[2]]
        with patch("scraper.load_config", return_value=config), \
             patch("scraper.load_product_exclusion_registry", return_value=registry), \
             patch("scraper.async_playwright", return_value=manager), \
             patch("scraper.asyncio.sleep", new=AsyncMock()), \
             patch("scraper.scrape_search", new=AsyncMock(side_effect=search)):
            selected, stats = await fetch_products()
        self.assertEqual(["A10", *[f"B{index}" for index in range(1, 10)]], [p.asin for p in selected])
        self.assertEqual(9, stats["_skipped_product_identity"])

    async def test_fetch_passes_sort_and_sale_gate_to_initial_and_deferred_calls(self):
        for mode, sort, quota in (("global_ranked", "sale_first", 2), ("category_quota", "sale_first", 5),
                                  ("category_round_robin", "review_desc", 1), ("category_round_robin", "discount_desc", 1)):
            with self.subTest(mode=mode, sort=sort):
                candidate = product("ASALE", discount="5%OFF")
                config = {"categories": [{"name": "A", "url": "https://example.test/a", "max_items": 10, "is_search": True}],
                    "filters": {"sort_order": sort, "min_price": 0, "selection_mode": mode,
                        "max_total_items": 10, "max_per_category": quota, "deferred_retry_failed_searches": True},
                    "exclusion": {"exclude_product_identifiers": True}}
                page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
                context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock())
                browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
                manager = MagicMock()
                manager.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
                call_count = 0
                async def search(*args, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    kwargs["stats"]["A"] = {"taken": 0 if call_count == 1 else 1, "error": "synthetic" if call_count == 1 else ""}
                    # A later config change must not replace the policy actually used.
                    config["filters"].update(sort_order="changed_after_start", max_total_items=999)
                    return [] if call_count == 1 else [candidate]
                with patch("scraper.load_config", return_value=config), \
                     patch("scraper.load_product_exclusion_registry", return_value=ProductIdentityRegistry()), \
                     patch("scraper.async_playwright", return_value=manager), \
                     patch("scraper.asyncio.sleep", new=AsyncMock()), \
                     patch("scraper.scrape_search", new=AsyncMock(side_effect=search)) as search_mock, \
                     patch("scraper.select_enrich_unique_products", new=AsyncMock(return_value=[candidate])) as select_mock:
                    _, stats = await fetch_products()
                self.assertEqual(2, search_mock.await_count)
                self.assertEqual([sort == "sale_first"] * 2, [call.kwargs["require_sale_info"] for call in search_mock.await_args_list])
                self.assertEqual(sort, select_mock.await_args.kwargs["sort_order"])
                self.assertEqual({"selection_mode": mode, "sort_order": sort,
                    "require_sale_info": sort == "sale_first", "max_per_category": quota,
                    "max_total_items": 10}, stats["_selection_policy"])


class SelectionPolicyOutputTests(unittest.TestCase):
    def test_summary_records_captured_policy_without_changing_product_fields(self):
        candidate = product("B000000001", discount="5%OFF")
        candidate.affiliate_url = make_affiliate_url(candidate.asin, "noteamazon1-22")
        policy = {"selection_mode": "global_ranked", "sort_order": "sale_first",
            "require_sale_info": True, "max_per_category": 2, "max_total_items": 10}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "account1" / "products_2026-09-10.json"
            with patch("scraper.fetch_products", new=AsyncMock(return_value=([candidate], {"_selection_policy": policy}))), \
                 patch("scraper.save_scraped_asins_to_history"), \
                 patch("scraper.load_config", side_effect=AssertionError("do not reread config for summary")):
                fetch_and_save(str(output), str(root / "categories1.yaml"), "noteamazon1-22")
            summary = json.loads(output.with_name("scrape_summary_2026-09-10.json").read_text(encoding="utf-8"))
            rows = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(policy, summary["selection_policy"])
        self.assertNotIn("_selection_policy", summary["categories"])
        self.assertEqual({"asin", "title", "price", "price_int", "original_price", "discount_rate", "image_url",
            "affiliate_url", "category", "rating", "review_count", "description", "specs"}, set(rows[0]))


class FakeElement:
    def __init__(self, text="", src=""):
        self.text, self.src = text, src
    async def inner_text(self):
        return self.text
    async def get_attribute(self, name):
        return self.src if name == "src" else None


class FakeCard:
    def __init__(self, asin, original="", discount=""):
        self.asin, self.original, self.discount = asin, original, discount
    async def get_attribute(self, name):
        return self.asin if name == "data-asin" else None
    async def inner_text(self):
        return "5つ星のうち4.0 (10)"
    async def query_selector(self, selector):
        if selector.startswith("h2"):
            return FakeElement(self.asin)
        if selector.startswith(".a-price:not"):
            return FakeElement("￥5,000")
        if selector == "[data-a-strike='true'] .a-offscreen":
            return FakeElement(self.original) if self.original else None
        if selector.startswith("[class*='savingsPercentage']"):
            return FakeElement(self.discount) if self.discount else None
        if selector.startswith("img"):
            return FakeElement(src="https://example.test/image.png")
        return None


class SaleCandidateGateTests(unittest.IsolatedAsyncioTestCase):
    async def scrape(self, cards, require_sale_info, max_items=10):
        page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock(),
            query_selector_all=AsyncMock(side_effect=[cards, []]), title=AsyncMock(return_value="synthetic"))
        stats = {}
        result = await scrape_search(page, "https://example.test/a", "A", max_items=max_items,
            stats=stats, require_sale_info=require_sale_info)
        return result, stats["A"]

    async def test_gate_restores_both_empty_condition_without_a_percentage_threshold(self):
        cards = [FakeCard("NO_SALE"), FakeCard("RATE_ONLY", discount="5%OFF"),
            FakeCard("ORIGINAL_ONLY", original="￥5,000"), FakeCard("POINTS_ONLY", discount="10%ポイント")]
        result, stats = await self.scrape(cards, True)
        self.assertEqual(["RATE_ONLY", "ORIGINAL_ONLY"], [p.asin for p in result])
        self.assertEqual("5%OFF", result[0].discount_rate)
        self.assertEqual("", result[1].discount_rate)
        self.assertEqual(2, stats["skipped_nosale"])
        self.assertTrue(all(not hasattr(p, "sale_participation_verified") for p in result))

    async def test_other_sort_default_retains_products_without_sale_info(self):
        result, stats = await self.scrape([FakeCard("NO_SALE")], False)
        self.assertEqual(["NO_SALE"], [p.asin for p in result])
        self.assertNotIn("skipped_nosale", stats)

    async def test_rejected_non_sale_card_does_not_consume_candidate_limit(self):
        result, stats = await self.scrape([FakeCard("NO_SALE"), FakeCard("NEXT", discount="1%OFF")], True, max_items=1)
        self.assertEqual(["NEXT"], [p.asin for p in result])
        self.assertEqual(1, stats["skipped_nosale"])

    async def test_retry_summary_retains_non_sale_skip_counts(self):
        result = merge_deferred_search_stats({"skipped_nosale": 2}, {"skipped_nosale": 3}, 1)
        self.assertEqual(5, result["skipped_nosale"])
        self.assertEqual(2, result["deferred_retry"]["initial"]["skipped_nosale"])
        self.assertEqual(3, result["deferred_retry"]["retry"]["skipped_nosale"])


if __name__ == "__main__":
    unittest.main()
