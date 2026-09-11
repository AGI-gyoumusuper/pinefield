"""Local DOM tests: no network, no user profiles, no Amazon requests."""
import unittest
from playwright.async_api import async_playwright
from detail_offer import OBSERVE_OFFER_JS, public_evidence, validate_offer


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
