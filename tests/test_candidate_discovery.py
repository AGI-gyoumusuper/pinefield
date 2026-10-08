"""Keep eligible offers behind cheap/posted cards within the bounded search pool."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from detail_offer import without_search_price_filter
from product_identity import ProductIdentityRegistry
import scraper
from test_original_price import fake_page, price_card


ROOT = Path(__file__).resolve().parents[1]


def config(account):
    return yaml.safe_load((ROOT / f"categories{account}.yaml").read_text(encoding="utf-8-sig"))


def card(number, price):
    result = price_card(
        f'<span class="a-price"><span class="a-offscreen">{price}</span></span>'
        f'<span data-a-strike="true"><span class="a-offscreen">{price * 2}</span></span>')
    result.node["data-asin"] = f"B{number:09d}"
    return result


class CandidateDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, account, pages, *, excluded=None, max_pages=2):
        cfg = config(account)
        category = cfg["categories"][0]
        page = fake_page([])
        page.query_selector_all = AsyncMock(side_effect=pages)
        page.query_selector = AsyncMock(return_value=object())
        stats = {}
        found = await scraper.scrape_search(
            page, without_search_price_filter(category["url"]), category["name"],
            max_items=category["max_items"], excluded=excluded, stats=stats,
            require_sale_info=True, defer_offer_validation=True, max_pages=max_pages)
        return found, stats[category["name"]], page, cfg

    async def test_eligible_offers_after_ten_subfloor_cards_are_discovered(self):
        for account in (12, 14):
            with self.subTest(account=account):
                cards = [card(n, 1000 if n <= 10 else 5000) for n in range(1, 15)]
                found, _, _, cfg = await self.search(account, [cards, []])
                eligible = scraper.filter_and_sort(
                    found, min_price=cfg["filters"]["min_price"],
                    max_price=cfg["filters"]["max_price"], max_total=24,
                    exclude_title_patterns=cfg["filters"]["exclude_title_patterns"])
                self.assertEqual([f"B{n:09d}" for n in range(11, 15)],
                                 [item.asin for item in eligible])
                self.assertTrue(all(item.price_int >= 3000 for item in eligible))

    async def test_posted_cards_do_not_consume_expanded_candidate_slots(self):
        posted = {f"B{n:09d}" for n in range(1, 101)}
        cards = [card(n, 5000) for n in range(1, 105)]
        found, stats, _, _ = await self.search(14, [cards, []], excluded=posted)
        self.assertEqual([f"B{n:09d}" for n in range(101, 105)],
                         [item.asin for item in found])
        self.assertEqual(100, stats["skipped_posted"])

    async def test_larger_candidate_pool_still_stops_after_two_pages(self):
        pages = [[card(n, 5000) for n in range(1, 21)],
                 [card(n, 5000) for n in range(21, 41)],
                 [card(41, 5000)]]
        found, stats, page, _ = await self.search(12, pages, max_pages=99)
        self.assertEqual(40, len(found))
        self.assertEqual([20, 20], stats["pages"])
        self.assertEqual(2, page.goto.await_count)
        self.assertEqual(2, page.query_selector_all.await_count)

    async def test_initial_and_deferred_search_use_the_configured_candidate_limit(self):
        for account in (12, 14):
            with self.subTest(account=account):
                cfg = config(account)
                cfg["categories"] = cfg["categories"][:1]
                category = cfg["categories"][0]
                page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
                context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page),
                                          close=AsyncMock())
                browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
                manager = MagicMock()
                manager.__aenter__.return_value = SimpleNamespace(
                    chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
                main_calls = []

                async def search(_page, url, name, max_items, _tag, **kwargs):
                    if kwargs.get("coupon_only"):
                        return []
                    main_calls.append((url, max_items))
                    kwargs["stats"][name] = dict(
                        pages=[0], taken=0, skipped_posted=0,
                        error="fixture initial search failure" if len(main_calls) == 1 else "")
                    return []

                with patch("scraper.load_config", return_value=cfg), \
                     patch("scraper.load_product_exclusion_registry", return_value=ProductIdentityRegistry()), \
                     patch("scraper.async_playwright", return_value=manager), \
                     patch("scraper.asyncio.sleep", new=AsyncMock()), \
                     patch("scraper.scrape_search", new=AsyncMock(side_effect=search)):
                    _, stats = await scraper.fetch_products(
                        f"categories{account}.yaml", f"noteamazon{account}-22", "2026-10-08")
                self.assertEqual([(without_search_price_filter(category["url"]), 100)] * 2, main_calls)
                self.assertTrue(stats[category["name"]]["deferred_retry_attempted"])
                self.assertEqual(2, browser.new_context.await_count)


if __name__ == "__main__":
    unittest.main()
