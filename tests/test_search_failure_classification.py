from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import scraper


URL = 'https://www.amazon.co.jp/s?rh=n%3A123%2Cp_n_deal_type%3A10343614051'


class SearchFailureClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def scrape(self, cards, *, active=None, status=200, title='Amazon.co.jp: fixture'):
        page = SimpleNamespace(
            goto=AsyncMock(return_value=SimpleNamespace(status=status)),
            wait_for_timeout=AsyncMock(), evaluate=AsyncMock(),
            query_selector_all=AsyncMock(return_value=cards),
            title=AsyncMock(return_value=title), query_selector=AsyncMock(return_value=active),
        )
        stats = {}
        with patch.object(scraper, 'save_search_failure_diagnostic', AsyncMock(return_value=True)) as capture:
            products = await scraper.scrape_search(page, URL, 'fixture', max_items=1,
                require_sale_info=True, track_exhausted_error_pages=True, stats=stats)
        return products, stats['fixture'], page, capture

    async def test_empty_response_is_unknown_and_keeps_deferred_retry(self):
        products, stats, page, capture = await self.scrape([])
        self.assertEqual(products, [])
        self.assertEqual(stats['taken'], 0)
        self.assertEqual(stats['error_kind'], 'search_results_unavailable')
        self.assertEqual(stats['requested_deal_filter_state'], 'unknown')
        self.assertIn('filter state unknown', stats['error'])
        self.assertNotIn('filter not active', stats['error'])
        self.assertTrue(scraper.needs_deferred_search_retry(stats))
        self.assertEqual(page.goto.await_count, 1)
        self.assertEqual(capture.call_args.kwargs['reason'], 'search_results_unavailable')

    async def test_cards_with_inactive_facet_are_still_rejected_and_retried(self):
        card = SimpleNamespace(get_attribute=AsyncMock())
        products, stats, page, capture = await self.scrape([card])
        self.assertEqual(products, [])
        self.assertEqual(stats['taken'], 0)
        self.assertEqual(stats['error_kind'], 'requested_deal_filter_not_active')
        self.assertEqual(stats['requested_deal_filter_state'], 'inactive')
        self.assertIn('requested deal filter not active', stats['error'])
        self.assertTrue(scraper.needs_deferred_search_retry(stats))
        card.get_attribute.assert_not_awaited()
        self.assertEqual(page.goto.await_count, 1)
        self.assertEqual(capture.call_args.kwargs['reason'], 'requested_deal_filter_not_active')

    async def test_http403_does_not_become_a_product_or_a_confirmed_inactive_facet(self):
        products, stats, page, capture = await self.scrape([], status=403)
        self.assertEqual(products, [])
        self.assertEqual(stats['error_kind'], 'search_results_unavailable')
        self.assertEqual(stats['requested_deal_filter_state'], 'unknown')
        self.assertTrue(scraper.needs_deferred_search_retry(stats))
        self.assertEqual(page.goto.await_count, 1)
        self.assertEqual(capture.call_args.kwargs['response_status'], 403)

    async def test_error_page_keeps_existing_two_attempts_and_specific_capture_reason(self):
        products, stats, page, capture = await self.scrape([], status=503, title='ご迷惑をおかけしています')
        self.assertEqual(products, [])
        self.assertEqual(stats['error_kind'], 'search_results_unavailable')
        self.assertEqual(stats['error_page_hits'], 2)
        self.assertEqual(stats['error_page_exhausted_pages'], [1])
        self.assertTrue(scraper.needs_deferred_search_retry(stats))
        self.assertEqual([call.args[0] for call in page.goto.await_args_list], [URL, 'https://www.amazon.co.jp/', URL])
        self.assertEqual([call.kwargs['reason'] for call in capture.await_args_list], ['amazon_error_page', 'amazon_error_page'])

    async def test_empty_but_selected_facet_keeps_existing_exhausted_result_behavior(self):
        products, stats, page, capture = await self.scrape([], active=object())
        self.assertEqual(products, [])
        self.assertEqual(stats['taken'], 0)
        self.assertEqual(stats['error'], '')
        self.assertNotIn('error_kind', stats)
        self.assertFalse(scraper.needs_deferred_search_retry(stats))
        self.assertEqual(page.goto.await_count, 1)
        self.assertEqual(capture.call_args.kwargs['reason'], 'no_search_results')


if __name__ == '__main__':
    unittest.main()
