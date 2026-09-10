import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bs4 import BeautifulSoup

import scraper


PUBLIC_URL = 'https://www.amazon.co.jp/s?rh=n%3A123%2Cp_n_deal_type%3A10343614051'
SYNTHETIC_HTML = '''<div id="search">
<script>window.session = "SCRIPT_SECRET";</script><!-- COMMENT_SECRET -->
<input type="hidden" name="csrf" value="INPUT_SECRET">
<span style="display: none">HIDDEN_SECRET</span>
<span data-session-token="ATTR_SECRET">Public result</span>
<nav>NAV_SECRET</nav><div id="csrf-token">TOKEN_SECRET</div>
<li id="p_n_deal_type/10343614051"><a aria-current="false"
href="/s?rh=p_n_deal_type%3A10343614051&amp;session-id=URL_SECRET">本日のタイムセール</a></li>
<img src="https://m.media-amazon.com/images/I/public.jpg?token=IMAGE_SECRET" alt="Public image">
</div>'''


def diagnostic_page(url=PUBLIC_URL):
    async def screenshot(**kwargs):
        Path(kwargs['path']).write_bytes(b'synthetic screenshot fixture only')
    return SimpleNamespace(
        url=url, title=AsyncMock(return_value='Amazon.co.jp: public search'),
        evaluate=AsyncMock(return_value={'scope': '#search', 'html': SYNTHETIC_HTML,
                                       'text': 'Public result\n本日のタイムセール'}),
        locator=lambda value: ('redaction-mask', value), screenshot=AsyncMock(side_effect=screenshot),
    )


class PublicDiagnosticSanitizationTests(unittest.TestCase):
    def test_embedded_state_forms_hidden_text_and_session_queries_are_not_saved(self):
        html = scraper.sanitize_public_diagnostic_html(SYNTHETIC_HTML)
        self.assertNotIn('_SECRET', html)
        self.assertNotIn('<script', html)
        self.assertNotIn('<input', html)
        self.assertNotIn('data-session-token', html)
        soup = BeautifulSoup(html, 'html.parser')
        self.assertIsNotNone(soup.select_one('[id="p_n_deal_type/10343614051"] a[aria-current="false"]'))
        self.assertIn('Public result', html)

    def test_url_credentials_fragments_and_unapproved_queries_are_removed(self):
        value = scraper.public_diagnostic_url(
            'https://user:URL_SECRET@www.amazon.co.jp/s?rh=n%3A123&page=2&token=QUERY_SECRET#FRAGMENT_SECRET')
        self.assertEqual(value, 'https://www.amazon.co.jp/s?rh=n%3A123&page=2')


class SearchDiagnosticCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, page, **kwargs):
        return await scraper.save_search_failure_diagnostic(page, requested_url=PUBLIC_URL, category='fixture',
            page_no=1, attempt=1, reason='requested_deal_filter_not_active', response_status=200, **kwargs)

    async def test_disabled_diagnostics_do_not_touch_page_or_files(self):
        with patch.dict(os.environ, {scraper.SEARCH_DIAGNOSTICS_ENV: ''}):
            self.assertFalse(await self.capture(None))

    async def test_first_two_failures_only_are_saved_with_hashes_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {scraper.SEARCH_DIAGNOSTICS_ENV: root}):
            page = diagnostic_page()
            self.assertTrue(await self.capture(page))
            self.assertTrue(await self.capture(page))
            self.assertFalse(await self.capture(page))
            self.assertEqual(page.screenshot.await_count, 2)
            self.assertEqual(sorted(path.name for path in Path(root).iterdir()), ['failure-01', 'failure-02'])
            first = Path(root) / 'failure-01'
            metadata = json.loads((first / 'metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['http_status'], 200)
            self.assertEqual(metadata['scope'], '#search')
            self.assertEqual(set(metadata['files_sha256']), {'page.html', 'page.txt', 'page.png'})
            for name, digest in metadata['files_sha256'].items():
                self.assertEqual(hashlib.sha256((first / name).read_bytes()).hexdigest(), digest)
            self.assertNotIn('_SECRET', (first / 'page.html').read_text(encoding='utf-8'))
            self.assertTrue(page.screenshot.call_args.kwargs['mask'])

    async def test_non_public_destination_has_metadata_only_and_never_reads_title(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {scraper.SEARCH_DIAGNOSTICS_ENV: root}):
            page = diagnostic_page('https://www.amazon.co.jp/ap/signin?token=AUTH_SECRET')
            self.assertTrue(await self.capture(page))
            page.evaluate.assert_not_awaited()
            page.screenshot.assert_not_awaited()
            page.title.assert_not_awaited()
            metadata_text = (Path(root) / 'failure-01/metadata.json').read_text(encoding='utf-8')
            self.assertNotIn('AUTH_SECRET', metadata_text)
            self.assertEqual(json.loads(metadata_text)['capture_omitted'], 'non_public_destination')

    async def test_capture_or_metadata_write_failure_does_not_escape_to_scraper(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {scraper.SEARCH_DIAGNOSTICS_ENV: root}):
            with patch.object(Path, 'write_text', side_effect=OSError('synthetic failure')):
                self.assertTrue(await self.capture(diagnostic_page()))

    async def test_empty_response_still_returns_no_products_and_is_recorded_once(self):
        page = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            wait_for_timeout=AsyncMock(), evaluate=AsyncMock(), query_selector_all=AsyncMock(return_value=[]),
            title=AsyncMock(return_value='Amazon.co.jp: synthetic'), query_selector=AsyncMock(return_value=None))
        stats = {}
        with patch.object(scraper, 'save_search_failure_diagnostic', new=AsyncMock(return_value=True)) as capture:
            products = await scraper.scrape_search(page, PUBLIC_URL, 'fixture', require_sale_info=True, stats=stats)
        self.assertEqual(products, [])
        self.assertIn('search_results_unavailable', stats['fixture']['error'])
        self.assertEqual(stats['fixture']['requested_deal_filter_state'], 'unknown')
        self.assertEqual(capture.await_count, 1)
        self.assertEqual(capture.call_args.kwargs['reason'], 'search_results_unavailable')

    async def test_error_page_is_captured_before_existing_homepage_retry(self):
        trace = []
        async def goto(url, **kwargs):
            trace.append(('goto', url))
            return SimpleNamespace(status=503)
        async def capture(*args, **kwargs):
            trace.append(('capture', kwargs['reason']))
            return True
        page = SimpleNamespace(goto=AsyncMock(side_effect=goto), wait_for_timeout=AsyncMock(), evaluate=AsyncMock(),
            query_selector_all=AsyncMock(return_value=[]), title=AsyncMock(return_value='ご迷惑をおかけしています'),
            query_selector=AsyncMock(return_value=None))
        with patch.object(scraper, 'save_search_failure_diagnostic', new=AsyncMock(side_effect=capture)):
            products = await scraper.scrape_search(page, PUBLIC_URL, 'fixture', require_sale_info=True)
        self.assertEqual(products, [])
        self.assertEqual(trace[:3], [('goto', PUBLIC_URL), ('capture', 'amazon_error_page'),
                                    ('goto', 'https://www.amazon.co.jp/')])


if __name__ == '__main__':
    unittest.main()
