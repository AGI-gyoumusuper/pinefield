import ast
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scraper import (  # noqa: E402
    Product,
    expected_associate_tag,
    make_affiliate_url,
    validate_affiliate_output,
)


def make_product(asin: str, tag: str) -> Product:
    return Product(
        asin=asin,
        title="test",
        price="1,000 yen",
        price_int=1000,
        original_price="",
        discount_rate="",
        image_url="https://example.com/image.jpg",
        affiliate_url=make_affiliate_url(asin, tag),
        category="test",
        rating="",
        review_count="",
    )


class AffiliateTagTests(unittest.TestCase):
    def test_account_specific_url(self):
        self.assertEqual(
            make_affiliate_url("B000000001", "noteamazon8-22"),
            "https://www.amazon.co.jp/dp/B000000001?tag=noteamazon8-22",
        )

    def test_expected_tag_uses_active_account_route(self):
        self.assertEqual(
            expected_associate_tag(
                r"C:\work\data\account10\products_2026-08-02.json",
                r"C:\work\categories10.yaml",
            ),
            "noteamazon10-22",
        )

    def test_expected_tag_supports_account20(self):
        self.assertEqual(
            expected_associate_tag(
                r"C:\work\data\account20\products_2026-08-14.json",
                r"C:\work\categories20.yaml",
            ),
            "noteamazon20-22",
        )

    def test_route_rejects_account11_output_with_account20_config(self):
        with self.assertRaisesRegex(ValueError, "Account routing mismatch"):
            expected_associate_tag(
                r"C:\work\data\account11\products.json",
                r"C:\work\categories20.yaml",
            )

    def test_one_output_check_accepts_matching_urls(self):
        products = [make_product("B000000001", "noteamazon6-22")]
        validate_affiliate_output(
            products,
            r"C:\work\data\account6\products.json",
            r"C:\work\categories6.yaml",
            "noteamazon6-22",
        )

    def test_one_output_check_rejects_wrong_account_tag(self):
        products = [make_product("B000000001", "noteamazon1-22")]
        with self.assertRaisesRegex(ValueError, "Affiliate tag mismatch"):
            validate_affiliate_output(
                products,
                r"C:\work\data\account7\products.json",
                r"C:\work\categories7.yaml",
                "noteamazon1-22",
            )

    def test_one_output_check_rejects_bare_product_url(self):
        products = [make_product("B000000001", "")]
        with self.assertRaisesRegex(ValueError, "Affiliate URL mismatch"):
            validate_affiliate_output(
                products,
                r"C:\work\data\account4\products.json",
                r"C:\work\categories4.yaml",
                "noteamazon4-22",
            )

    def test_one_output_check_rejects_missing_tag(self):
        products = [make_product("B000000001", "")]
        with self.assertRaisesRegex(ValueError, "Affiliate tag mismatch"):
            validate_affiliate_output(
                products,
                r"C:\work\data\account3\products.json",
                r"C:\work\categories3.yaml",
                "",
            )

    def test_active_entrypoints_have_exact_account_tags(self):
        for account in range(1, 21):
            path = REPO_ROOT / f"scrape_main{account}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "fetch_and_save"
            ]
            self.assertEqual(len(calls), 1, path.name)
            keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
            self.assertEqual(
                ast.literal_eval(keywords["associate_tag"]),
                f"noteamazon{account}-22",
                path.name,
            )


if __name__ == "__main__":
    unittest.main()
