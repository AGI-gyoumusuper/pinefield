"""Regression coverage for validated candidates surviving later page failures."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from product_identity import ProductIdentityRegistry
import scraper
from test_original_price import fake_page, price_card
from test_sale_ranking import product


URL = "https://example.test/search?rh=n%3A123%2Cp_n_deal_type%3A10343614051"


class PartialSearchPageTests(unittest.IsolatedAsyncioTestCase):
    async def scrape(self, page, *, track_errors=True):
        stats = {}
        with patch.object(scraper, "save_search_failure_diagnostic", AsyncMock(return_value=True)):
            products = await scraper.scrape_search(
                page, URL, "fixture", max_items=2, require_sale_info=True,
                track_exhausted_error_pages=track_errors, stats=stats,
            )
        return products, stats["fixture"]

    def assert_preserved_and_retryable(self, products, stats):
        self.assertEqual(["B000000001"], [item.asin for item in products])
        self.assertEqual(1, stats["taken"])
        self.assertTrue(stats["error"])
        self.assertTrue(scraper.needs_deferred_search_retry(stats))

    async def test_page_two_503_keeps_validated_page_one(self):
        page = fake_page([price_card()])
        page.goto = AsyncMock(side_effect=[SimpleNamespace(status=200), SimpleNamespace(status=503),
                                          SimpleNamespace(status=200), SimpleNamespace(status=503)])
        page.query_selector_all = AsyncMock(side_effect=[[price_card()], [], []])
        page.query_selector = AsyncMock(side_effect=[object(), None])
        page.title = AsyncMock(return_value="ご迷惑をおかけしています")
        products, stats = await self.scrape(page)
        self.assert_preserved_and_retryable(products, stats)
        self.assertEqual([2], stats["error_page_exhausted_pages"])
        self.assertEqual("search_results_unavailable", stats["error_kind"])

    async def test_page_two_shopping_continue_keeps_validated_page_one(self):
        page = fake_page([price_card()])
        page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        page.query_selector = AsyncMock(side_effect=[object(), None])
        page.title = AsyncMock(return_value="ショッピングを続ける")
        products, stats = await self.scrape(page)
        self.assert_preserved_and_retryable(products, stats)
        self.assertEqual("unknown", stats["requested_deal_filter_state"])
        self.assertEqual([1, 0], stats["pages"])

    async def test_page_two_selector_exception_keeps_candidates_and_taken_without_opt_in(self):
        page = fake_page([price_card()])
        page.query_selector = AsyncMock(side_effect=[object(), RuntimeError("selector unavailable")])
        products, stats = await self.scrape(page, track_errors=False)
        self.assert_preserved_and_retryable(products, stats)
        self.assertIn("selector unavailable", stats["error"])

    async def test_page_two_navigation_timeout_keeps_candidates_and_taken_without_opt_in(self):
        page = fake_page([price_card()])
        page.goto = AsyncMock(side_effect=[SimpleNamespace(status=200), TimeoutError("navigation timeout")])
        page.query_selector = AsyncMock(return_value=object())
        products, stats = await self.scrape(page, track_errors=False)
        self.assert_preserved_and_retryable(products, stats)
        self.assertIn("navigation timeout", stats["error"])

    async def test_page_two_cards_without_required_filter_are_never_consumed(self):
        untrusted = SimpleNamespace(get_attribute=AsyncMock())
        page = fake_page([price_card()])
        page.query_selector_all = AsyncMock(side_effect=[[price_card()], [untrusted]])
        page.query_selector = AsyncMock(side_effect=[object(), None])
        products, stats = await self.scrape(page)
        self.assert_preserved_and_retryable(products, stats)
        self.assertEqual("inactive", stats["requested_deal_filter_state"])
        untrusted.get_attribute.assert_not_awaited()

    async def test_first_page_inactive_filter_still_rejects_every_candidate(self):
        untrusted = SimpleNamespace(get_attribute=AsyncMock())
        page = fake_page([untrusted])
        page.query_selector = AsyncMock(return_value=None)
        products, stats = await self.scrape(page)
        self.assertEqual([], products)
        self.assertEqual(0, stats["taken"])
        self.assertTrue(scraper.needs_deferred_search_retry(stats))
        untrusted.get_attribute.assert_not_awaited()

    async def test_normal_page_two_exhaustion_does_not_retry(self):
        page = fake_page([price_card()])
        page.query_selector = AsyncMock(return_value=object())
        products, stats = await self.scrape(page)
        self.assertEqual(["B000000001"], [item.asin for item in products])
        self.assertEqual(1, stats["taken"])
        self.assertEqual("", stats["error"])
        self.assertFalse(scraper.needs_deferred_search_retry(stats))


class PartialSearchFetchTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, initial, retried, *, successful_other=None):
        config = {
            "categories": [{"name": "A", "url": URL, "max_items": 3, "is_search": True}],
            "filters": {"sort_order": "sale_first", "min_price": 0, "max_total_items": 10,
                        "deferred_retry_failed_searches": True},
        }
        if successful_other:
            config["categories"].append({"name": "B", "url": URL, "max_items": 3, "is_search": True})
        page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
        context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        manager = MagicMock()
        manager.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        calls = []

        async def search(*args, **kwargs):
            category = args[2]
            is_retry = category in calls
            calls.append(category)
            products = successful_other if category == "B" else (retried if is_retry else initial)
            kwargs["stats"][category] = {
                "taken": len(products), "pages": [len(products)], "skipped_posted": 0,
                "error": "page two unavailable" if category == "A" and not is_retry else "",
            }
            return products

        with patch.object(scraper, "load_config", return_value=config), \
             patch.object(scraper, "load_product_exclusion_registry", return_value=ProductIdentityRegistry()), \
             patch.object(scraper, "async_playwright", return_value=manager), \
             patch.object(scraper.asyncio, "sleep", AsyncMock()), \
             patch.object(scraper, "enrich_products", AsyncMock()), \
             patch.object(scraper, "scrape_search", AsyncMock(side_effect=search)):
            selected, stats = await scraper.fetch_products()
        return selected, stats, calls

    async def test_retry_retains_prior_candidates_deduplicates_and_honors_category_limit(self):
        first = [product("A1", discount="50%OFF"), product("A2", discount="40%OFF")]
        retry = [product("A2", discount="90%OFF"), product("A3", discount="30%OFF"), product("A4", discount="20%OFF")]
        selected, stats, calls = await self.fetch(first, retry)
        self.assertEqual(["A", "A"], calls)
        self.assertEqual({"A1", "A2", "A3"}, {item.asin for item in selected})
        self.assertIs(first[1], next(item for item in selected if item.asin == "A2"))
        self.assertEqual(3, stats["A"]["taken"])
        self.assertEqual(1, stats["A"]["deferred_retry_unique_added"])

    async def test_retry_does_not_repeat_successful_category_or_duplicate_its_asin(self):
        first = [product("A1", discount="50%OFF")]
        other = [product("B1", "B", discount="60%OFF")]
        retry = [product("B1", discount="90%OFF"), product("A2", discount="30%OFF"), product("A3", discount="20%OFF")]
        selected, stats, calls = await self.fetch(first, retry, successful_other=other)
        self.assertEqual(["A", "B", "A"], calls)
        self.assertEqual({"A1", "A2", "A3", "B1"}, {item.asin for item in selected})
        self.assertEqual(4, len(selected))
        self.assertIs(other[0], next(item for item in selected if item.asin == "B1"))
        self.assertEqual(3, stats["A"]["taken"])
        self.assertEqual(2, stats["A"]["deferred_retry_unique_added"])
        self.assertNotIn("deferred_retry_attempted", stats["B"])


if __name__ == "__main__":
    unittest.main()
