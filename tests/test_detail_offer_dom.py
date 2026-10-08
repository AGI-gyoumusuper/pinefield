"""Local DOM tests: no network, no user profiles, no Amazon requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from playwright.async_api import async_playwright
from detail_offer import OBSERVE_OFFER_JS, observe_detail_offer, public_evidence, validate_offer


ASIN = "B0GM224VBV"
HTML = '''<html><head><style>
.a-offscreen {position:absolute;clip:rect(1px,1px,1px,1px);width:1px;height:1px;overflow:hidden}
.gone {display:none} .invisible{visibility:hidden}
</style></head><body><input type="hidden" id="ASIN" value="B0GM224VBV">
<div id="centerCol"><h1 id="productTitle">FINAL FANTASY VII REBIRTH – Switch 2</h1>
<div id="corePriceDisplay_desktop_feature_div">
<span class="savingsPercentage">-7%</span><span class="priceToPay">
<span class="a-offscreen">￥8,888</span><span aria-hidden="true"><span class="a-price-symbol">￥</span><span class="a-price-whole">9,138</span></span></span>
<span class="basisPrice" aria-hidden="true">過去価格: ￥9,865</span>
<span hidden class="basisPrice">￥100,000</span><span class="gone priceToPay"><span class="a-price-whole">999</span></span>
</div><div id="dealBadge_feature_div">タイムセール</div></div>
<div id="recommendations"><span class="priceToPay"><span class="a-price-whole">777</span></span></div>
<template><input id="ASIN" value="B000000099"><div id="dealBadge_feature_div">タイムセール</div></template>
</body></html>'''


class LocalDomTests(unittest.IsolatedAsyncioTestCase):
    async def test_challenge_controls_and_dedicated_pages_without_copy_false_positives(self):
        captcha = '<form action="/errors/validateCaptcha"><input id="captchacharacters"></form>'
        cases = [
            ("normal product", HTML, []),
            ("hidden attribute", HTML.replace('</body>', '<div hidden>' + captcha + '</div></body>'), []),
            ("hidden ancestor", HTML.replace('</body>', '<div style="display:none">' + captcha + '</div></body>'), []),
            ("invisible ancestor", HTML.replace('</body>', '<div style="visibility:hidden">' + captcha + '</div></body>'), []),
            ("template", HTML.replace('</body>', '<template>' + captcha + '</template></body>'), []),
            ("incidental title and copy", HTML.replace('<head>', '<head><title>Amazon.co.jp: CAPTCHA security training book</title>')
                .replace('FINAL FANTASY VII REBIRTH', 'CAPTCHA security training book'), []),
            ("visible controls over product", HTML.replace('</body>', captcha + '</body>'), ['visible_challenge_control']),
            ("instruction overlay over product", HTML.replace('</body>', '<div role="dialog">ロボットではないことを確認してください</div></body>'), ['visible_challenge_instruction']),
            ("iframe instruction over product", HTML.replace('</body>', '<div role="dialog">文字を入力してください<iframe title="captcha"></iframe></div></body>'), ['visible_challenge_instruction']),
            ("heading overlay over product", HTML.replace('</body>', '<div role="dialog"><h1>Robot Check</h1></div></body>'), ['visible_challenge_heading']),
            ("dedicated controls", '<html><body>' + captcha + '</body></html>', ['visible_challenge_control']),
            ("dedicated title", '<html><title>Amazon CAPTCHA</title><body>Please verify access.</body></html>', ['challenge_page_title']),
            ("dedicated heading", '<html><body><h1>Robot Check</h1></body></html>', ['visible_challenge_heading', 'challenge_page_text']),
            ("dedicated instruction", '<html><body>画像に表示されている文字を入力してください</body></html>', ['visible_challenge_instruction', 'challenge_page_text']),
        ]
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            try:
                for name, html, signals in cases:
                    with self.subTest(name=name):
                        await page.set_content(html)
                        observed = await page.evaluate(OBSERVE_OFFER_JS)
                        self.assertEqual(signals, observed["challenge_signals"])
                        self.assertEqual(bool(signals), observed["challenge_detected"])
                        value = public_evidence(observed, ASIN, f"https://www.amazon.co.jp/dp/{ASIN}", 200, "2026-10-08T00:00:00+00:00")
                        offer, reason = validate_offer(value)
                        if signals:
                            self.assertIsNone(offer)
                            self.assertEqual("detail_challenge", reason)
                        else:
                            self.assertIsNone(reason)
                            self.assertEqual(9138, offer["price_int"])
            finally:
                await browser.close()

    async def test_missing_or_failed_challenge_observation_cannot_accept_product(self):
        missing = public_evidence({}, ASIN, f"https://www.amazon.co.jp/dp/{ASIN}", 200, "2026-10-08T00:00:00+00:00")
        self.assertEqual((None, "detail_challenge"), validate_offer(missing))
        self.assertNotIn("challenge_signals", missing)
        page = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)),
                               wait_for_timeout=AsyncMock(), url=f"https://www.amazon.co.jp/dp/{ASIN}",
                               evaluate=AsyncMock(side_effect=RuntimeError("private diagnostic content")))
        candidate = SimpleNamespace(asin=ASIN, category="fixture", price="￥9,138", price_int=9138,
                                    original_price="￥9,865", discount_rate="7%OFF")
        reader = AsyncMock()
        record = await observe_detail_offer(page, candidate, reader)
        self.assertEqual("rejected", record["status"])
        self.assertEqual("detail_observation_error", record["reason"])
        self.assertIsNone(record["evidence"]["challenge_detected"])
        self.assertNotIn("private diagnostic content", str(record))
        reader.assert_not_awaited()

    async def test_signals_are_optional_fixed_public_labels(self):
        raw = {"challenge_detected": True}
        legacy = public_evidence(raw, ASIN, f"https://www.amazon.co.jp/dp/{ASIN}", 200, "2026-10-08T00:00:00+00:00")
        self.assertNotIn("challenge_signals", legacy)
        self.assertEqual((None, "detail_challenge"), validate_offer(legacy))
        raw["challenge_signals"] = ["challenge_page_text", "private content", {"token": "private"}, "challenge_page_text"]
        value = public_evidence(raw, ASIN, f"https://www.amazon.co.jp/dp/{ASIN}", 200, "2026-10-08T00:00:00+00:00")
        self.assertEqual(["challenge_page_text"], value["challenge_signals"])
        self.assertNotIn("private", str(value))

    async def test_visible_aria_hidden_offer_and_hidden_decoys(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            try:
                await page.set_content(HTML)
                observed = await page.evaluate(OBSERVE_OFFER_JS)
                self.assertEqual(["9,138"], observed["price_texts"])
                self.assertEqual(["過去価格: ￥9,865"], observed["reference_price_texts"])
                self.assertEqual([ASIN], observed["selected_asins"])
                value = public_evidence(observed, ASIN, f"https://www.amazon.co.jp/dp/{ASIN}", 200, "2026-09-12T02:00:00+00:00")
                self.assertEqual(9138, validate_offer(value)[0]["price_int"])
                await page.locator("#dealBadge_feature_div").evaluate("node=>node.style.visibility='hidden'")
                hidden = await page.evaluate(OBSERVE_OFFER_JS)
                value = public_evidence(hidden, ASIN, f"https://www.amazon.co.jp/dp/{ASIN}", 200, "2026-09-12T02:00:00+00:00")
                self.assertEqual("detail_sale_region_missing_or_ambiguous", validate_offer(value)[1])
                await page.locator("#dealBadge_feature_div").evaluate("node=>{node.style.visibility='visible';node.innerHTML='終了まで: <span class=\"detailpage-dealBadge-countdown-timer\">01:02:03</span>'}")
                countdown = await page.evaluate(OBSERVE_OFFER_JS)
                value = public_evidence(countdown, ASIN, f"https://www.amazon.co.jp/dp/{ASIN}", 200, "2026-09-12T02:00:00+00:00")
                self.assertIsNone(validate_offer(value)[1]); self.assertEqual(3723, value["countdown"]["remaining_seconds"])
                await page.locator(".detailpage-dealBadge-countdown-timer").evaluate("node=>node.style.display='none'")
                no_timer = await page.evaluate(OBSERVE_OFFER_JS)
                value = public_evidence(no_timer, ASIN, f"https://www.amazon.co.jp/dp/{ASIN}", 200, "2026-09-12T02:00:00+00:00")
                self.assertEqual("detail_time_sale_unverified", validate_offer(value)[1])
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
