"""Current-price floor, conservative unknown handling and historical continuity."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sale_campaign import (ACTUAL_SALE_PRICE_POLICY, actual_sale_price_rejection,
                           validate_actual_sale_price_policy)
from scraper import Product, fetch_and_save, filter_and_sort


def product(amount=8800, display=None, asin="B000000001", category="A"):
    return Product(asin=asin, title="Product", price=f"￥{amount:,}" if display is None else display,
                   price_int=amount, original_price="￥100,000", discount_rate="10%OFF",
                   image_url="https://example.test/image.jpg",
                   affiliate_url=f"https://www.amazon.co.jp/dp/{asin}?tag=noteamazon1-22",
                   category=category + "#1", rating="4.0", review_count="100")


class ActualSalePriceTests(unittest.TestCase):
    def test_boundary_and_price_formats(self):
        for amount, expected in [(7999, False), (8000, False), (8001, True), (8800, True), (9800, True)]:
            for display in [f"￥{amount:,}", f"¥{amount:,}", f"{amount:,}円", str(amount)]:
                with self.subTest(amount=amount, display=display):
                    self.assertEqual(expected, actual_sale_price_rejection(product(amount, display)) is None)
        self.assertIsNone(actual_sale_price_rejection(product(8800, "￥８，８００")))

    def test_unknown_or_ambiguous_never_uses_high_reference_price(self):
        for amount in [None, 0, -1, True, 8800.0, "8800", 9007199254740992, float("nan"), float("inf")]:
            with self.subTest(amount=amount):
                self.assertIsNotNone(actual_sale_price_rejection(product(amount, "￥8,800")))
        for display in ["", "価格不明", "￥8,000～9,800", "￥8,800/月", "参考価格￥100,000",
                        "￥8,800 ￥9,800", "USD 8800", "￥8,8", "￥88.00", "￥8,000", "08800", 8800]:
            with self.subTest(display=display):
                self.assertIsNotNone(actual_sale_price_rejection(product(8800, display)))
        missing = vars(product()).copy()
        missing.pop("price")
        self.assertIsNotNone(actual_sale_price_rejection(missing))

    def test_verified_coupon_payable_price_and_unknown_rounding(self):
        for amount, ok in [(7999, False), (8000, False), (8001, True), (8800, True),
                           (None, False), (8801, False), (8001.0, False), (True, False)]:
            with self.subTest(amount=amount):
                self.assertEqual(ok, actual_sale_price_rejection(product(),
                    {"status": "verified", "final_price_yen": amount}) is None)
        self.assertIsNone(actual_sale_price_rejection(product(), {"status": "unverified", "value": 50}))

    def test_filter_preserves_stricter_floor_ranking_ties_and_input(self):
        rows = [product(8000, asin="BOUNDARY"), product(4000, asin="HIGHREF"),
                product(19999, asin="BELOWVR", category="VR"), product(20000, asin="ATVR", category="VR"),
                product(9800, asin="TIE1"), product(9800, asin="TIE2"), product(8001, asin="FIRSTOK")]
        before = copy.deepcopy(rows)
        selected = filter_and_sort(rows, min_price=0, sort_order="price_desc", category_min_prices={"VR": 20000}, max_total=10)
        self.assertEqual(["ATVR", "TIE1", "TIE2", "FIRSTOK"], [p.asin for p in selected])
        self.assertEqual(before, rows)
        self.assertEqual(["BELOWVR", "ATVR"], [p.asin for p in filter_and_sort(rows, min_price=15000, sort_order="review_desc")])

    def test_coupon_cannot_bypass_stricter_category_floor(self):
        row = product(22000, category="VR")
        row._actual_sale_coupon = {"status": "verified", "final_price_yen": 19999}
        self.assertEqual([], filter_and_sort([row], category_min_prices={"VR": 20000}))

    def test_summary_marker_rejects_boundary_unknown_and_bad_coupon_without_mutation(self):
        for amount, ok in [(8000, False), (8001, True)]:
            summary = {"selection_policy": {"actual_sale_price_policy": ACTUAL_SALE_PRICE_POLICY}}
            rows = [vars(product(amount))]
            before = copy.deepcopy((summary, rows))
            if ok:
                validate_actual_sale_price_policy(summary, rows)
            else:
                with self.assertRaises(ValueError):
                    validate_actual_sale_price_policy(summary, rows)
            self.assertEqual(before, (summary, rows))
        summary["discount_contract"] = {"products": [{"asin": rows[0]["asin"], "source_position": 1, "coupon":
            {"status": "verified", "final_price_yen": 8000}}]}
        with self.assertRaises(ValueError):
            validate_actual_sale_price_policy(summary, rows)
        summary["selection_policy"]["actual_sale_price_policy"] = "unknown"
        with self.assertRaises(ValueError):
            validate_actual_sale_price_policy(summary, rows)

    def test_unmarked_historical_summary_is_unchanged(self):
        summary = {"selection_policy": {"selection_policy_version": "profit_price_desc_v1", "min_price": 8000}}
        rows = [vars(product(8000))]
        before = copy.deepcopy((summary, rows))
        validate_actual_sale_price_policy(summary, rows)
        self.assertEqual(before, (summary, rows))

    def test_save_guard_stops_before_writing_any_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "products_2026-10-26.json"
            with patch("scraper.fetch_products", new=AsyncMock(return_value=([product(8000)], {}))):
                with self.assertRaisesRegex(ValueError, "actual_sale_price_at_or_below_8000"):
                    fetch_and_save(str(output), "categories1.yaml", "noteamazon1-22")
            self.assertEqual([], list(Path(folder).iterdir()))


if __name__ == "__main__":
    unittest.main()
