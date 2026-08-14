import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from product_identity import (
    ProductIdentityRegistry,
    canonical_gtin,
    extract_product_identity,
    identity_from_key,
    normalize_brand,
    normalize_model,
)
from scraper import Product, select_enrich_unique_products


class ProductIdentityTests(unittest.TestCase):
    def test_model_normalization_absorbs_case_spaces_and_hyphens(self):
        self.assertEqual("WH1000XM5", normalize_model(" wh-1000 xm5 "))
        self.assertEqual("WH1000XM5", normalize_model("WH－1000‑XM5"))

    def test_brand_normalization_removes_trademark_marks_before_nfkc(self):
        self.assertEqual("SONY", normalize_brand(" Sony®™℠ "))

    def test_upc_and_gtin_are_the_same_canonical_identifier(self):
        self.assertEqual("00077924051524", canonical_gtin("077924051524"))
        self.assertEqual("00077924051524", canonical_gtin("00077924051524"))
        self.assertEqual("", canonical_gtin("077924051525"))

    def test_extracts_only_structured_brand_model_and_codes(self):
        specs = (
            "ブランド名 ソト(SOTO) UPC 077924051524 "
            "GTIN (Global Trade Identification Number) 00077924051524 "
            "メーカー名 SOTO 品番・型番 ST-340 メーカー型番 ST - 340 "
            "Amazon 売れ筋ランキング 1位 ASIN B000000001"
        )
        identity = extract_product_identity({"specs": specs})
        self.assertIn("SOTO::ST340", identity.brand_model_keys)
        self.assertIn("ソト::ST340", identity.brand_model_keys)
        self.assertEqual(("00077924051524",), identity.global_trade_numbers)

    def test_different_asin_matches_by_brand_and_normalized_model(self):
        registry = ProductIdentityRegistry()
        registry.add_identity(identity_from_key("SONY::WH-1000XM5"))
        candidate = extract_product_identity(
            {"brand": "sony", "manufacturer_model_number": "WH 1000 XM5"}
        )
        self.assertEqual("BRAND_MODEL:SONY::WH1000XM5", registry.match_identity(candidate))

    def test_different_asin_matches_by_global_trade_number(self):
        registry = ProductIdentityRegistry()
        registry.add_identity(extract_product_identity({"upc": "077924051524"}))
        candidate = extract_product_identity({"gtin": "00077924051524"})
        self.assertEqual("GTIN:00077924051524", registry.match_identity(candidate))

    def test_brand_or_model_alone_never_matches(self):
        registry = ProductIdentityRegistry()
        registry.add_identity(identity_from_key("SONY::WH-1000XM5"))
        different_brand = extract_product_identity(
            {"brand": "Acme", "manufacturer_model_number": "WH-1000XM5"}
        )
        different_model = extract_product_identity(
            {"brand": "Sony", "manufacturer_model_number": "WH-1000XM4"}
        )
        self.assertEqual("", registry.match_identity(different_brand))
        self.assertEqual("", registry.match_identity(different_model))

    def test_placeholder_brand_never_creates_brand_model_key(self):
        for brand in ("ノーブランド品", "Generic", "Generic(ジェネリック)", "OEM"):
            identity = extract_product_identity(
                {"brand": brand, "manufacturer_model_number": "AB-1234"}
            )
            self.assertEqual((), identity.brand_model_keys)

    def test_description_is_not_used_for_identity(self):
        identity = extract_product_identity(
            {"description": "ブランド名 SONY メーカー型番 WH-1000XM5 UPC 077924051524"}
        )
        self.assertFalse(identity.usable)


class ProductIdentitySelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_robin_refills_same_category_after_identity_exclusion(self):
        registry = ProductIdentityRegistry()
        registry.add_identity(identity_from_key("SONY::WH-1000XM5"))

        def product(asin, title, reviews, specs):
            return Product(
                asin=asin,
                title=title,
                price="￥10,000",
                price_int=10000,
                original_price="",
                discount_rate="",
                image_url="",
                affiliate_url="",
                category="ヘッドホン#1",
                rating="4.5",
                review_count=str(reviews),
                specs=specs,
            )

        duplicate = product(
            "B000000001",
            "Sony WH-1000XM5",
            1000,
            "ブランド名 Sony メーカー型番 WH 1000 XM5 Amazon 売れ筋ランキング 1位",
        )
        replacement = product(
            "B000000002",
            "Audio-Technica ATH-M50x",
            900,
            "ブランド名 Audio-Technica メーカー型番 ATH-M50x Amazon 売れ筋ランキング 2位",
        )
        with tempfile.TemporaryDirectory() as temp:
            with patch("scraper.asyncio.sleep", new=AsyncMock()):
                selected = await select_enrich_unique_products(
                    None,
                    [duplicate, replacement],
                    registry,
                    [{"name": "ヘッドホン", "url": "https://example.com"}],
                    1,
                    1,
                    "category_round_robin",
                    str(Path(temp) / "rotation.json"),
                    {},
                )
        self.assertEqual(["B000000002"], [item.asin for item in selected])

    async def test_category_quota_refills_rejected_candidate_inside_same_shelf(self):
        registry = ProductIdentityRegistry()
        registry.add_identity(identity_from_key("GAME::DUPLICATE"))

        def candidate(category: str, index: int, model: str) -> Product:
            asin = f"B{category[-1]}{index:08d}"[:10]
            return Product(
                asin=asin,
                title=f"{category} game {index}",
                price="￥5,000",
                price_int=5000,
                original_price="",
                discount_rate="",
                image_url="",
                affiliate_url="",
                category=f"{category}#{index}",
                rating="4.5",
                review_count=str(10000 - index),
                specs=f"ブランド名 GAME メーカー型番 {model}",
            )

        products = [candidate("C1", 1, "DUPLICATE")]
        products += [candidate("C1", index, f"C1-{index}") for index in range(2, 7)]
        products += [candidate("C2", index, f"C2-{index}") for index in range(1, 7)]
        with patch("scraper.asyncio.sleep", new=AsyncMock()):
            selected = await select_enrich_unique_products(
                None,
                products,
                registry,
                [{"name": "C1"}, {"name": "C2"}],
                10,
                5,
                "category_quota",
                "",
                {},
            )
        counts = {
            name: sum(item.category.split("#")[0] == name for item in selected)
            for name in ("C1", "C2")
        }
        self.assertEqual({"C1": 5, "C2": 5}, counts)
        self.assertNotIn("B100000001", {item.asin for item in selected})

    async def test_category_quota_combines_short_shelf_identity_skip_and_overflow(self):
        registry = ProductIdentityRegistry()
        registry.add_identity(identity_from_key("GAME::DUPLICATE"))

        def candidate(category: str, index: int, model: str) -> Product:
            asin = f"B{category[-1]}{index:08d}"[:10]
            return Product(
                asin=asin,
                title=f"{category} game {index}",
                price="￥5,000",
                price_int=5000,
                original_price="",
                discount_rate="",
                image_url="",
                affiliate_url="",
                category=f"{category}#{index}",
                rating="4.5",
                review_count=str(10000 - index),
                specs=f"ブランド名 GAME メーカー型番 {model}",
            )

        products = [candidate("C1", 1, "DUPLICATE"), candidate("C1", 2, "ONLY")]
        products += [candidate("C2", index, f"C2-{index}") for index in range(1, 11)]
        with patch("scraper.asyncio.sleep", new=AsyncMock()):
            selected = await select_enrich_unique_products(
                None,
                products,
                registry,
                [{"name": "C1"}, {"name": "C2"}],
                10,
                5,
                "category_quota",
                "",
                {},
            )
        counts = {
            name: sum(item.category.split("#")[0] == name for item in selected)
            for name in ("C1", "C2")
        }
        self.assertEqual({"C1": 1, "C2": 9}, counts)
        self.assertEqual(10, len(selected))


if __name__ == "__main__":
    unittest.main()
