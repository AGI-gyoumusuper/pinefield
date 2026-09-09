from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bs4 import BeautifulSoup

from scraper import extract_original_price, needs_deferred_search_retry, scrape_search, scrape_timesale


PRICE_HTML = (Path(__file__).parent / 'fixtures' / 'search_price_with_unit_and_reference.html').read_text(encoding='utf-8')
FACET_HTML = (Path(__file__).parent / 'fixtures' / 'selected_deal_filter.html').read_text(encoding='utf-8')


class HtmlElement:
    """Use saved HTML/CSS selection while replacing browser/network operations."""
    def __init__(self, node, card=None):
        self.node = node
        self.card = card or self

    async def query_selector(self, selector):
        found = self.node.select_one(selector)
        return HtmlElement(found, self.card) if found is not None else None

    async def inner_text(self):
        return self.node.get_text(' ', strip=True)

    async def get_attribute(self, name):
        return self.node.get(name)

    async def evaluate_handle(self, _expression):
        return self.card


def price_card(price_html=PRICE_HTML):
    # Only synthetic title/link/image wrappers surround the saved price subtree.
    soup = BeautifulSoup(
        '<div data-asin="B000000001" data-component-type="s-search-result">'
        '<h2><a href="https://www.amazon.co.jp/dp/B000000001"><span>Fixture product</span></a></h2>'
        + price_html + '<img src="https://example.test/image.png" /></div>', 'html.parser')
    return HtmlElement(soup.select_one('[data-asin]'))


def fake_page(cards):
    return SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(), evaluate=AsyncMock(),
        query_selector_all=AsyncMock(side_effect=[cards, []]), title=AsyncMock(return_value='synthetic'))


class OriginalPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_unit_price_is_not_the_reference_price(self):
        card = price_card()
        legacy_match = await card.query_selector('.a-text-price .a-offscreen')
        self.assertEqual('￥1,649', await legacy_match.inner_text())
        self.assertIn('/個', card.node.get_text())
        self.assertEqual('￥3,798', await extract_original_price(card))

    async def test_unit_price_without_reference_does_not_become_original_price(self):
        card = price_card()
        card.node.select_one('[data-a-strike]').parent.decompose()
        self.assertEqual('', await extract_original_price(card))

    async def test_explicit_reference_markers_are_read_once_without_duplicate_visible_price(self):
        examples = (
            '<span data-testid="original-price"><span class="a-offscreen">￥3,798</span><span aria-hidden="true">￥3,798</span></span>',
            '<span class="a-text-strike"><span class="a-offscreen">￥3,798</span><span aria-hidden="true">￥3,798</span></span>',
            '<span class="a-text-strike">￥3,798</span>',
            '<span data-a-strike="true">￥3,798</span>',
        )
        for html in examples:
            with self.subTest(html=html):
                self.assertEqual('￥3,798', await extract_original_price(price_card(html)))

    async def test_semantic_marker_is_used_instead_of_numerically_hiding_lower_prices(self):
        self.assertEqual('￥2,498', await extract_original_price(price_card('<span data-a-strike="true">￥2,498</span>')))

    async def test_search_parser_recovers_sale_price_reference_and_existing_rate_calculation(self):
        products = await scrape_search(fake_page([price_card()]), 'https://example.test/search', 'fixture',
            max_items=1, require_sale_info=True)
        self.assertEqual(1, len(products))
        self.assertEqual(('￥3,298', 3298, '￥3,798', '13%OFF'),
            (products[0].price, products[0].price_int, products[0].original_price, products[0].discount_rate))

    async def test_timesale_parser_uses_the_same_reference_price_rule(self):
        card = price_card()
        link = await card.query_selector('a')
        products = await scrape_timesale(fake_page([link]), 'https://example.test/deals', 'fixture', max_items=1)
        self.assertEqual(1, len(products))
        self.assertEqual(('￥3,298', 3298, '￥3,798', '13%OFF'),
            (products[0].price, products[0].price_int, products[0].original_price, products[0].discount_rate))

    async def test_search_sale_gate_rejects_unit_only_price_information(self):
        card = price_card()
        card.node.select_one('[data-a-strike]').parent.decompose()
        stats = {}
        products = await scrape_search(fake_page([card]), 'https://example.test/search', 'fixture',
            max_items=1, require_sale_info=True, stats=stats)
        self.assertEqual([], products)
        self.assertEqual(1, stats['fixture']['skipped_nosale'])


class SelectedDealFilterTests(unittest.IsolatedAsyncioTestCase):
    async def scrape(self, html=FACET_HTML, deal='10343614051', require_sale_info=True):
        page = fake_page([price_card()])
        facet = HtmlElement(BeautifulSoup(html, 'html.parser'))
        page.query_selector = AsyncMock(side_effect=facet.query_selector)
        stats = {}
        url = 'https://example.test/search?rh=n%3A123'
        if deal is not None:
            url += '%2Cp_n_deal_type%3A' + deal
        products = await scrape_search(page, url, 'fixture', max_items=1,
            require_sale_info=require_sale_info, stats=stats)
        return products, stats['fixture'], page

    async def test_saved_active_anchor_allows_sale_candidates(self):
        products, stats, page = await self.scrape()
        self.assertEqual(1, len(products))
        self.assertEqual('', stats['error'])
        page.query_selector.assert_awaited_once_with('[id="p_n_deal_type/10343614051"] a[aria-current="true"]')

    async def test_inactive_or_unrecognized_requested_filter_rejects_candidates(self):
        for deal in ('10343616051', '23534876051'):
            with self.subTest(deal=deal):
                products, stats, page = await self.scrape(deal=deal)
                self.assertEqual([], products)
                self.assertEqual(0, stats['taken'])
                self.assertIn('requested deal filter not active', stats['error'])
                self.assertTrue(needs_deferred_search_retry(stats))
                self.assertEqual(1, page.goto.await_count)

    async def test_current_marker_on_parent_does_not_replace_selected_anchor(self):
        html = '<li id="p_n_deal_type/10343614051" aria-current="true"><a aria-current="false">本日のタイムセール</a></li>'
        products, stats, _ = await self.scrape(html=html)
        self.assertEqual([], products)
        self.assertIn('requested deal filter not active', stats['error'])

    async def test_no_deal_value_or_non_sale_mode_keeps_existing_search_compatibility(self):
        for deal, required in ((None, True), ('10343614051', False)):
            with self.subTest(deal=deal, required=required):
                products, stats, page = await self.scrape(html='', deal=deal, require_sale_info=required)
                self.assertEqual(1, len(products))
                self.assertEqual('', stats['error'])
                page.query_selector.assert_not_awaited()

    async def test_lost_filter_on_second_page_discards_the_whole_category(self):
        page = fake_page([price_card()])
        page.query_selector_all = AsyncMock(side_effect=[[price_card()], [price_card()]])
        page.query_selector = AsyncMock(side_effect=[object(), None])
        stats = {}
        products = await scrape_search(page,
            'https://example.test/search?rh=n%3A123%2Cp_n_deal_type%3A10343614051', 'fixture',
            max_items=2, require_sale_info=True, stats=stats)
        self.assertEqual([], products)
        self.assertEqual(0, stats['fixture']['taken'])
        self.assertIn('requested deal filter not active', stats['fixture']['error'])
        self.assertEqual(2, page.goto.await_count)


if __name__ == '__main__':
    unittest.main()
