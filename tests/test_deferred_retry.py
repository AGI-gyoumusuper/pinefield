import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from scraper import (
    merge_deferred_search_stats,
    needs_deferred_search_retry,
    scrape_search,
)


class DeferredRetryHelperTests(unittest.TestCase):
    def test_zero_result_exhausted_apology_is_retryable(self):
        self.assertTrue(
            needs_deferred_search_retry(
                {
                    "taken": 0,
                    "error": "",
                    "error_page_hits": 2,
                    "error_page_exhausted_pages": [1],
                }
            )
        )

    def test_nonzero_result_is_not_retryable(self):
        self.assertFalse(
            needs_deferred_search_retry(
                {
                    "taken": 1,
                    "error": "",
                    "error_page_hits": 2,
                    "error_page_exhausted_pages": [1],
                }
            )
        )

    def test_zero_result_transient_error_is_retryable(self):
        self.assertTrue(
            needs_deferred_search_retry(
                {
                    "taken": 0,
                    "error": "network timeout",
                    "error_page_hits": 0,
                }
            )
        )

    def test_single_nonexhausted_apology_is_not_retryable(self):
        self.assertFalse(
            needs_deferred_search_retry(
                {
                    "taken": 0,
                    "error": "",
                    "error_page_hits": 1,
                }
            )
        )

    def test_stats_merge_preserves_initial_and_retry_snapshots(self):
        initial = {
            "pages": [0],
            "taken": 0,
            "skipped_posted": 1,
            "error": "",
            "error_page_hits": 2,
            "error_page_exhausted_pages": [1],
            "page1_title": "ご迷惑をおかけしています！",
        }
        retry = {
            "pages": [48],
            "taken": 10,
            "skipped_posted": 2,
            "error": "",
            "error_page_hits": 0,
        }
        initial_before = deepcopy(initial)
        retry_before = deepcopy(retry)

        merged = merge_deferred_search_stats(initial, retry, unique_added=9)

        self.assertEqual(initial_before, initial)
        self.assertEqual(retry_before, retry)
        self.assertEqual([0, 48], merged["pages"])
        self.assertEqual(9, merged["taken"])
        self.assertEqual(3, merged["skipped_posted"])
        self.assertEqual(2, merged["error_page_hits"])
        self.assertTrue(merged["deferred_retry_attempted"])
        self.assertTrue(merged["deferred_retry_recovered"])
        self.assertEqual(9, merged["deferred_retry_unique_added"])
        self.assertEqual(initial_before, merged["deferred_retry"]["initial"])
        self.assertEqual(retry_before, merged["deferred_retry"]["retry"])


class ScrapeSearchApologyTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_apology_pages_record_two_hits_and_exhaust_page_one(self):
        page = SimpleNamespace(
            goto=AsyncMock(),
            wait_for_timeout=AsyncMock(),
            evaluate=AsyncMock(),
            query_selector_all=AsyncMock(side_effect=[[], []]),
            title=AsyncMock(return_value="ご迷惑をおかけしています！"),
        )
        stats = {}
        search_url = "https://www.amazon.co.jp/s?rh=n%3A2356841051"

        products = await scrape_search(
            page,
            search_url,
            "口紅",
            max_items=1,
            stats=stats,
        )

        self.assertEqual([], products)
        self.assertEqual(2, stats["口紅"]["error_page_hits"])
        self.assertEqual([1], stats["口紅"]["error_page_exhausted_pages"])
        self.assertEqual([0], stats["口紅"]["pages"])
        self.assertEqual(0, stats["口紅"]["taken"])
        self.assertEqual(
            [search_url, "https://www.amazon.co.jp/", search_url],
            [call.args[0] for call in page.goto.await_args_list],
        )


if __name__ == "__main__":
    unittest.main()
