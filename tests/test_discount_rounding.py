import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scraper import calc_discount_rate


class DiscountRoundingTests(unittest.TestCase):
    def test_live_detail_page_discount_matches(self):
        # Same ASIN/price/reference captured from Amazon on 2026-09-10.
        for price, reference, displayed in (
            (3172, 4580, "31%OFF"),
            (5333, 6580, "19%OFF"),
            (10501, 12465, "16%OFF"),
        ):
            with self.subTest(price=price, reference=reference):
                self.assertEqual(calc_discount_rate(price, reference), displayed)

    def test_round_half_up_without_float_loss(self):
        self.assertEqual(calc_discount_rate(79, 100), "21%OFF")
        self.assertEqual(calc_discount_rate(675, 1000), "33%OFF")
        self.assertEqual(calc_discount_rate(3298, 3798), "13%OFF")

    def test_missing_or_non_discounted_price_has_no_discount(self):
        for price, reference in ((0,100),(100,0),(100,100),(110,100),(-1,100)):
            self.assertEqual(calc_discount_rate(price,reference), "")


if __name__ == '__main__':
    unittest.main()
