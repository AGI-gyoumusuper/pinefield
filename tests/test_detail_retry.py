import asyncio
import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from product_identity import ProductIdentityRegistry, identity_from_key
from scraper import (
    DetailRetryBudget,
    Product,
    enrich_product,
    enrich_products,
    select_enrich_unique_products,
)


def product(asin="B005EJHC0S", *, description="", specs="", reviews=100):
    return Product(
        asin=asin,
        title="Original product title",
        price="￥1,000",
        price_int=1000,
        original_price="￥2,000",
        discount_rate="50%OFF",
        image_url="https://example.com/original.jpg",
        affiliate_url=f"https://www.amazon.co.jp/dp/{asin}?tag=noteamazon3-22",
        category="クリーム#1",
        rating="4.5",
        review_count=str(reviews),
        description=description,
        specs=specs,
    )


class DetailRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_or_partial_first_read_never_adds_a_request(self):
        for details in (("description", "specs"), ("description", ""), ("", "specs")):
            with self.subTest(details=details):
                candidate = product()
                budget = DetailRetryBudget()
                page = object()
                with patch("scraper.scrape_product_detail", new=AsyncMock(return_value=details)) as fetch:
                    self.assertTrue(await enrich_product(page, candidate, retry_budget=budget))
                fetch.assert_awaited_once_with(page, candidate.asin)
                self.assertEqual(details, (candidate.description, candidate.specs))
                self.assertEqual([], budget.observations)
                self.assertEqual(set(), budget.handled_asins)

    async def test_direct_retry_skips_products_with_either_field(self):
        for details in (("description", "specs"), ("description", ""), ("", "specs")):
            with self.subTest(details=details):
                candidate = product(description=details[0], specs=details[1])
                before = asdict(candidate)
                budget = DetailRetryBudget()
                with patch("scraper.scrape_product_detail", new=AsyncMock()) as fetch:
                    await budget.retry(object(), candidate)
                fetch.assert_not_awaited()
                self.assertEqual(before, asdict(candidate))
                self.assertEqual([], budget.observations)

    async def test_empty_first_read_recovers_same_asin_and_only_detail_fields(self):
        candidate = product()
        before = asdict(candidate)
        budget = DetailRetryBudget()
        page = object()
        with patch("scraper.scrape_product_detail", new=AsyncMock(side_effect=[("", ""), ("Recovered description", "Recovered specs")])) as fetch:
            self.assertTrue(await enrich_product(page, candidate, retry_budget=budget))
        self.assertEqual([call(page, candidate.asin), call(page, candidate.asin)], fetch.await_args_list)
        expected = {**before, "description": "Recovered description", "specs": "Recovered specs"}
        self.assertEqual(expected, asdict(candidate))
        self.assertEqual({candidate.asin}, budget.handled_asins)
        self.assertEqual("recovered", budget.observations[0]["outcome"])
        self.assertEqual(candidate.asin, budget.observations[0]["asin"])
        self.assertGreaterEqual(budget.observations[0]["elapsed_seconds"], 0)

    async def test_persistent_empty_is_false_and_same_asin_is_not_retried_again(self):
        candidate = product()
        before = asdict(candidate)
        budget = DetailRetryBudget()
        page = object()
        with patch("scraper.scrape_product_detail", new=AsyncMock(return_value=("", ""))) as fetch:
            self.assertFalse(await enrich_product(page, candidate, retry_budget=budget))
            await budget.retry(page, candidate)
            await budget.retry(page, product(candidate.asin))
        self.assertEqual(2, fetch.await_count)
        self.assertEqual(before, asdict(candidate))
        self.assertEqual(1, len(budget.observations))
        self.assertEqual("empty", budget.observations[0]["outcome"])

    async def test_retry_exception_preserves_candidate_and_prevents_more_attempts(self):
        candidate = product()
        before = asdict(candidate)
        budget = DetailRetryBudget()
        with patch("scraper.scrape_product_detail", new=AsyncMock(side_effect=RuntimeError("network unavailable"))) as fetch:
            await budget.retry(object(), candidate)
            await budget.retry(object(), candidate)
        self.assertEqual(before, asdict(candidate))
        self.assertEqual(1, fetch.await_count)
        self.assertEqual("error", budget.observations[0]["outcome"])

    async def test_real_timeout_cancels_request_and_preserves_candidate(self):
        candidate = product()
        before = asdict(candidate)
        budget = DetailRetryBudget(timeout_seconds=0.01, budget_seconds=0.2)
        cancelled = asyncio.Event()

        async def blocked_request(page, asin):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch("scraper.scrape_product_detail", new=AsyncMock(side_effect=blocked_request)) as fetch:
            await asyncio.wait_for(budget.retry(object(), candidate), timeout=2)
            await budget.retry(object(), candidate)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(before, asdict(candidate))
        self.assertEqual(1, fetch.await_count)
        self.assertEqual("timeout", budget.observations[0]["outcome"])

    async def test_shared_budget_exhaustion_does_not_fetch_next_asin(self):
        first = product()
        second = product("B000000002")
        budget = DetailRetryBudget(timeout_seconds=60, budget_seconds=60)
        # Patch only scraper's imported clock, leaving asyncio's real clock intact.
        with patch("scraper.time", SimpleNamespace(monotonic=Mock(side_effect=[100.0, 160.0]))), \
             patch("scraper.scrape_product_detail", new=AsyncMock(return_value=("", ""))) as fetch:
            await budget.retry(None, first)
            await budget.retry(None, second)
        fetch.assert_awaited_once_with(None, first.asin)
        self.assertEqual(60, budget.spent_seconds)
        self.assertEqual(["empty", "budget_exhausted"], [row["outcome"] for row in budget.observations])
        self.assertEqual(second.asin, budget.observations[-1]["asin"])
        self.assertEqual(1, budget.summary()["attempted_count"])
        self.assertEqual(0, budget.summary()["recovered_count"])

    async def test_remaining_shared_budget_limits_next_request_timeout(self):
        budget = DetailRetryBudget(timeout_seconds=15, budget_seconds=60)
        budget.spent_seconds = 59.99
        cancelled = asyncio.Event()

        async def blocked_request(page, asin):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch("scraper.scrape_product_detail", new=AsyncMock(side_effect=blocked_request)):
            await asyncio.wait_for(budget.retry(None, product()), timeout=2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual("timeout", budget.observations[0]["outcome"])
        self.assertGreaterEqual(budget.spent_seconds, 59.99)

    async def test_budget_and_same_asin_tracking_are_independent_between_runs(self):
        first = DetailRetryBudget()
        second = DetailRetryBudget()
        with patch("scraper.scrape_product_detail", new=AsyncMock(return_value=("", ""))) as fetch:
            await first.retry(None, product())
            await second.retry(None, product())
        self.assertEqual(2, fetch.await_count)
        self.assertIsNot(first.handled_asins, second.handled_asins)
        self.assertIsNot(first.observations, second.observations)
        self.assertEqual(1, len(first.observations))
        self.assertEqual(1, len(second.observations))
        self.assertIsInstance(first.summary(), dict)
        self.assertEqual(1, first.summary()["attempt_limit_per_asin"])
        self.assertEqual(15, first.summary()["timeout_seconds"])
        self.assertEqual(60, first.summary()["budget_seconds"])
        self.assertEqual(1, first.summary()["attempted_count"])
        json.dumps(first.summary())

    async def test_bulk_enrichment_shares_retry_tracking_across_duplicate_asins(self):
        candidates = [product(), product()]
        budget = DetailRetryBudget()
        with patch("scraper.scrape_product_detail", new=AsyncMock(return_value=("", ""))) as fetch, \
             patch("scraper.asyncio.sleep", new=AsyncMock()):
            await enrich_products(None, candidates, retry_budget=budget)
        # Two ordinary reads, plus only one additional read for the shared ASIN.
        self.assertEqual(3, fetch.await_count)
        self.assertEqual(1, len(budget.observations))


class DetailRetrySelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovered_specs_still_apply_existing_identity_exclusion(self):
        duplicate = product(reviews=1000)
        replacement = product("B000000002", specs="ブランド名 Other メーカー型番 MODEL999", reviews=900)
        registry = ProductIdentityRegistry()
        registry.add_identity(identity_from_key("SONY::WH-1000XM5"))
        budget = DetailRetryBudget()
        stats = {}
        with patch("scraper.scrape_product_detail", new=AsyncMock(side_effect=[("", ""), ("", "ブランド名 Sony メーカー型番 WH-1000XM5")])) as fetch, \
             patch("scraper.asyncio.sleep", new=AsyncMock()):
            selected = await select_enrich_unique_products(
                None, [duplicate, replacement], registry, [{"name": "クリーム"}],
                1, 1, "global_ranked", "", stats, retry_budget=budget,
            )
        self.assertEqual([replacement], selected)
        self.assertEqual(2, fetch.await_count)
        self.assertEqual(1, stats["_skipped_product_identity"])
        self.assertEqual("recovered", budget.observations[0]["outcome"])

    async def test_persistent_empty_does_not_create_new_selection_rejection(self):
        candidate = product()
        budget = DetailRetryBudget()
        with patch("scraper.scrape_product_detail", new=AsyncMock(return_value=("", ""))) as fetch, \
             patch("scraper.asyncio.sleep", new=AsyncMock()):
            selected = await select_enrich_unique_products(
                None, [candidate], ProductIdentityRegistry(), [{"name": "クリーム"}],
                1, 1, "global_ranked", "", {}, retry_budget=budget,
            )
        self.assertEqual([candidate], selected)
        self.assertEqual(2, fetch.await_count)
        self.assertEqual("empty", budget.observations[0]["outcome"])


if __name__ == "__main__":
    unittest.main()
